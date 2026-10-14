from dotenv import load_dotenv
load_dotenv()

import hashlib
import json
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageOps


from backend.database import (
    get_data_dir,
    get_photo,
    insert_bin,
    insert_photo,
    update_photo_image,
)
from backend.models import GeminiDetectedBin

logger = logging.getLogger(__name__)


def normalize_box(box_2d: List[int]) -> List[float]:
    """
    Converts [ymin, xmin, ymax, xmax] (0 to 1000 scale)
    to [x, y, w, h] (0.0 to 1.0 scale).
    """
    ymin, xmin, ymax, xmax = box_2d
    x = max(0.0, min(1.0, xmin / 1000.0))
    y = max(0.0, min(1.0, ymin / 1000.0))
    w = max(0.0, min(1.0 - x, (xmax - xmin) / 1000.0))
    h = max(0.0, min(1.0 - y, (ymax - ymin) / 1000.0))
    return [round(x, 4), round(y, 4), round(w, 4), round(h, 4)]


def compress_image_to_target(
    image: Image.Image,
    target_bytes: int = 500 * 1024,
    max_dim: int = 2048,
) -> bytes:
    """
    Compresses image to approximately target_bytes (~500kB) while maintaining high visual clarity.
    Downscales so max(width, height) <= max_dim, then binary-searches JPEG quality.
    """
    w, h = image.size
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        img = image.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
    else:
        img = image.copy()

    # Binary search for optimal JPEG quality (range 30 to 95)
    low_q, high_q = 30, 95
    best_data = None

    for _ in range(7):
        mid_q = (low_q + high_q) // 2
        buf = BytesIO()
        img.save(buf, "JPEG", quality=mid_q, optimize=True)
        data = buf.getvalue()
        if len(data) <= target_bytes:
            best_data = data
            low_q = mid_q + 1
        else:
            high_q = mid_q - 1

    if best_data is None:
        # If even at low_q the image is too large, downscale slightly and save at reasonable quality
        scale = 0.8
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.Resampling.LANCZOS)
        buf = BytesIO()
        img.save(buf, "JPEG", quality=65, optimize=True)
        best_data = buf.getvalue()

    return best_data


def generate_tiles(
    image: Image.Image,
    overlap_ratio: float = 0.20,
) -> List[Tuple[Image.Image, Tuple[int, int, int, int]]]:
    """
    Slices an image into overlapping tiles for SAHI-style inference.
    Returns a list of (tile_image, (tile_left, tile_top, tile_right, tile_bottom))
    where the bounding tuple is in original image pixel coordinates.
    """
    w, h = image.size
    if max(w, h) <= 1500:
        return [(image.copy(), (0, 0, w, h))]

    # Determine grid division based on dimensions and aspect ratio
    cols = 2 if w > 1500 else 1
    rows = 2 if h > 1500 else 1
    if w / h >= 1.7:
        cols = 3
    elif h / w >= 1.7:
        rows = 3

    # If only 1 tile would be created despite large dimensions, force 2
    if cols == 1 and rows == 1:
        if w >= h:
            cols = 2
        else:
            rows = 2

    # Calculate tile size with overlap
    col_w = int(round(w / (cols - (cols - 1) * overlap_ratio))) if cols > 1 else w
    row_h = int(round(h / (rows - (rows - 1) * overlap_ratio))) if rows > 1 else h

    # Clamp tile dimensions to image bounds
    col_w = min(w, max(1, col_w))
    row_h = min(h, max(1, row_h))

    tiles = []
    x_step = int(round(col_w * (1.0 - overlap_ratio))) if cols > 1 else w
    y_step = int(round(row_h * (1.0 - overlap_ratio))) if rows > 1 else h

    for r in range(rows):
        top = r * y_step
        bottom = min(h, top + row_h)
        if r == rows - 1:
            bottom = h
            top = max(0, bottom - row_h)

        for c in range(cols):
            left = c * x_step
            right = min(w, left + col_w)
            if c == cols - 1:
                right = w
                left = max(0, right - col_w)

            tile_img = image.crop((left, top, right, bottom))
            tiles.append((tile_img, (left, top, right, bottom)))

    return tiles


def project_box_to_global(
    box_2d: List[int],
    tile_rect: Tuple[int, int, int, int],
    orig_w: int,
    orig_h: int,
) -> List[int]:
    """
    Translates a bounding box [ymin, xmin, ymax, xmax] (0 to 1000 in tile space)
    into global image coordinates [ymin, xmin, ymax, xmax] (0 to 1000 in original image space).
    """
    tile_left, tile_top, tile_right, tile_bottom = tile_rect
    tile_w = tile_right - tile_left
    tile_h = tile_bottom - tile_top

    b_ymin, b_xmin, b_ymax, b_xmax = box_2d

    abs_ymin = tile_top + (b_ymin / 1000.0) * tile_h
    abs_xmin = tile_left + (b_xmin / 1000.0) * tile_w
    abs_ymax = tile_top + (b_ymax / 1000.0) * tile_h
    abs_xmax = tile_left + (b_xmax / 1000.0) * tile_w

    g_ymin = max(0, min(1000, int(round((abs_ymin / orig_h) * 1000))))
    g_xmin = max(0, min(1000, int(round((abs_xmin / orig_w) * 1000))))
    g_ymax = max(0, min(1000, int(round((abs_ymax / orig_h) * 1000))))
    g_xmax = max(0, min(1000, int(round((abs_xmax / orig_w) * 1000))))

    return [g_ymin, g_xmin, g_ymax, g_xmax]


def calculate_iou(box1: List[int], box2: List[int]) -> float:
    """
    Calculates Intersection over Union (IoU) between two boxes in [ymin, xmin, ymax, xmax] format.
    """
    y1_min, x1_min, y1_max, x1_max = box1
    y2_min, x2_min, y2_max, x2_max = box2

    inter_ymin = max(y1_min, y2_min)
    inter_xmin = max(x1_min, x2_min)
    inter_ymax = min(y1_max, y2_max)
    inter_xmax = min(x1_max, x2_max)

    inter_w = max(0, inter_xmax - inter_xmin)
    inter_h = max(0, inter_ymax - inter_ymin)
    inter_area = inter_w * inter_h

    area1 = max(0, x1_max - x1_min) * max(0, y1_max - y1_min)
    area2 = max(0, x2_max - x2_min) * max(0, y2_max - y2_min)
    union_area = area1 + area2 - inter_area

    if union_area <= 0:
        return 0.0
    return inter_area / union_area


def suppress_duplicate_bins(
    bins: List[GeminiDetectedBin],
    iou_threshold: float = 0.40,
) -> List[GeminiDetectedBin]:
    """
    Applies Non-Maximum Suppression (NMS) / deduplication on detected bins.
    When two boxes overlap with IoU >= iou_threshold, they are merged:
    - Retains the more specific label.
    - Combines unique semantic tags.
    - Uses the average bounding box.
    """
    if not bins:
        return []

    def box_area(b: GeminiDetectedBin) -> int:
        ymin, xmin, ymax, xmax = b.box_2d
        return (ymax - ymin) * (xmax - xmin)

    sorted_bins = sorted(bins, key=box_area, reverse=True)
    merged_bins: List[GeminiDetectedBin] = []

    for candidate in sorted_bins:
        matched = False
        for i, existing in enumerate(merged_bins):
            iou = calculate_iou(candidate.box_2d, existing.box_2d)
            if iou >= iou_threshold:
                chosen_label = existing.label
                if len(candidate.label.strip()) > len(existing.label.strip()):
                    chosen_label = candidate.label

                combined_tags = list(dict.fromkeys(existing.semantic_tags + candidate.semantic_tags))

                avg_box = [
                    int(round((existing.box_2d[0] + candidate.box_2d[0]) / 2)),
                    int(round((existing.box_2d[1] + candidate.box_2d[1]) / 2)),
                    int(round((existing.box_2d[2] + candidate.box_2d[2]) / 2)),
                    int(round((existing.box_2d[3] + candidate.box_2d[3]) / 2)),
                ]

                merged_bins[i] = GeminiDetectedBin(
                    box_2d=avg_box,
                    label=chosen_label,
                    semantic_tags=combined_tags,
                )
                matched = True
                break

        if not matched:
            merged_bins.append(candidate)

    return merged_bins



def detect_bins_with_gemini(
    image_bytes: bytes,
    api_key: str,
    model_name: Optional[str] = None,
) -> List[GeminiDetectedBin]:
    """Calls the Google Gemini API to identify workshop storage bins with structured JSON output."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)

    prompt = (
        "You are an inventory detection system for an electronics workshop / hackerspace.\n"
        "Carefully examine this shelf or organizer photo. Identify every storage bin, box, drawer, container, "
        "and tub that has visible labels, part numbers, or identifiable tools/components.\n"
        "Scan systematically row-by-row from top to bottom, and left-to-right within each row.\n"
        "Do not skip or omit drawers.\n"
        "For each detected item, provide:\n"
        "1. box_2d: normalized coordinates [ymin, xmin, ymax, xmax] scaled 0 to 1000 tightly bounding the bin, drawer, or container\n"
        "2. label: exact transcribed text from the bin's label or visible component name\n"
        "3. semantic_tags: 3 to 5 functional keywords, synonyms, component types, or common use-cases "
        "(e.g., for 'CR2032', include ['coin cell', '3V battery', 'CMOS battery', 'button cell'])\n"
        "Return a JSON array of detected bins."
    )

    contents = [
        types.Part.from_bytes(
            data=image_bytes,
            mime_type="image/jpeg",
        ),
        prompt,
    ]

    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=list[GeminiDetectedBin],
        temperature=0.1,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    candidate_models = [
        model_name or os.getenv("GEMINI_MODEL", "gemini-3.6-flash"),
        "gemini-3.6-flash",
        "gemini-3.8-flash",
        "gemini-flash-latest",
    ]
    # Deduplicate while preserving order
    seen = set()
    models_to_try = [m for m in candidate_models if m and not (m in seen or seen.add(m))]

    last_error = None
    for model in models_to_try:
        try:
            logger.info("Attempting Gemini VLM detection with model: %s", model)
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
            if response.text:
                raw_items = json.loads(response.text)
                detected = [GeminiDetectedBin(**item) for item in raw_items]
                logger.info("Successfully detected %d bins with %s", len(detected), model)
                return detected
        except Exception as e:
            logger.warning("Gemini model %s failed: %s", model, e)
            last_error = e

    raise RuntimeError(f"All candidate Gemini models failed. Last error: {last_error}")


def detect_bins_tiled(
    image: Image.Image,
    api_key: str,
    model_name: Optional[str] = None,
    max_workers: int = 4,
    progress_callback: Optional[Any] = None,
) -> List[GeminiDetectedBin]:
    """
    Divides image into overlapping tiles, runs Gemini Flash detection in parallel,
    projects tile-relative coordinates to global coordinates, and applies NMS.
    """
    orig_w, orig_h = image.size
    tiles = generate_tiles(image)
    logger.info("Generated %d tiles for image of size %dx%d", len(tiles), orig_w, orig_h)

    if progress_callback:
        progress_callback(
            15,
            "vision_ai",
            f"Image partitioned into {len(tiles)} tiles for high-res analysis...",
            0,
            0,
            len(tiles),
        )

    def process_tile(tile_item: Tuple[Image.Image, Tuple[int, int, int, int]]) -> List[GeminiDetectedBin]:
        tile_img, tile_rect = tile_item
        buf = BytesIO()
        tile_img.save(buf, "JPEG", quality=88)
        tile_bytes = buf.getvalue()

        tile_detections = detect_bins_with_gemini(
            image_bytes=tile_bytes,
            api_key=api_key,
            model_name=model_name,
        )

        projected: List[GeminiDetectedBin] = []
        for d in tile_detections:
            global_box = project_box_to_global(d.box_2d, tile_rect, orig_w, orig_h)
            projected.append(
                GeminiDetectedBin(
                    box_2d=global_box,
                    label=d.label,
                    semantic_tags=d.semantic_tags,
                )
            )
        return projected

    all_detections: List[GeminiDetectedBin] = []
    completed_tiles = 0

    if len(tiles) == 1:
        all_detections = process_tile(tiles[0])
        completed_tiles = 1
        if progress_callback:
            progress_callback(
                75,
                "vision_ai",
                f"Analyzed tile 1 of 1 ({len(all_detections)} bins found so far)",
                len(all_detections),
                1,
                1,
            )
    else:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(tiles))) as executor:
            future_to_tile = [executor.submit(process_tile, t) for t in tiles]
            for future in as_completed(future_to_tile):
                completed_tiles += 1
                try:
                    tile_results = future.result()
                    all_detections.extend(tile_results)
                except Exception as e:
                    logger.warning("Error processing tile in parallel: %s", e)

                if progress_callback:
                    pct = 15 + int((completed_tiles / len(tiles)) * 65)
                    progress_callback(
                        pct,
                        "vision_ai",
                        f"Analyzed tile {completed_tiles} of {len(tiles)} ({len(all_detections)} bins found so far)",
                        len(all_detections),
                        completed_tiles,
                        len(tiles),
                    )

    if progress_callback:
        progress_callback(
            82,
            "deduplication",
            f"Deduplicating {len(all_detections)} candidate bins across tiles...",
            len(all_detections),
            len(tiles),
            len(tiles),
        )

    deduped = suppress_duplicate_bins(all_detections, iou_threshold=0.40)
    logger.info(
        "Tiled detection complete: %d raw detections across %d tiles -> %d deduped bins",
        len(all_detections),
        len(tiles),
        len(deduped),
    )

    if progress_callback:
        progress_callback(
            88,
            "deduplication",
            f"Fusing detections into {len(deduped)} unique workshop bins...",
            len(deduped),
            len(tiles),
            len(tiles),
        )

    return deduped



def mock_detect_bins(width: int, height: int, filename: str = "") -> List[GeminiDetectedBin]:
    """
    Fallback mock detector when GEMINI_API_KEY is not configured.
    Generates a realistic grid of workshop storage bins for offline development and testing.
    """
    sample_components = [
        {
            "label": "Kapton Tape & PVC Electrical Tape",
            "tags": ["insulation", "heat resistant tape", "wiring", "soldering mask"],
            "box": [120, 80, 260, 280],
        },
        {
            "label": "CR2032 3V Coin Cell Batteries",
            "tags": ["coin cell", "3V battery", "CMOS battery", "button cell", "power"],
            "box": [120, 320, 260, 520],
        },
        {
            "label": "ESP32 & ESP8266 Dev Boards",
            "tags": ["microcontroller", "wifi", "bluetooth", "iot", "arduino"],
            "box": [120, 560, 260, 760],
        },
        {
            "label": "Digital Multimeters & Test Leads",
            "tags": ["voltmeter", "measure voltage", "resistance", "continuity", "dmm"],
            "box": [340, 80, 520, 340],
        },
        {
            "label": "N-Channel MOSFETs (IRFZ44N / 2N7000)",
            "tags": ["transistor", "switching", "power mosfet", "gate drive", "semiconductor"],
            "box": [340, 380, 520, 620],
        },
        {
            "label": "Weller Soldering Tips & Brass Sponges",
            "tags": ["soldering iron", "tip cleaner", "flux", "solder station", "wlc100"],
            "box": [340, 660, 520, 920],
        },
        {
            "label": "Heat Shrink Tubing Assortment",
            "tags": ["wire insulation", "heat gun", "polyolefin", "cable sleeve"],
            "box": [600, 80, 780, 320],
        },
        {
            "label": "Anti-Static ESD Wrist Straps",
            "tags": ["fix static shock", "grounding", "esd wristband", "static protection"],
            "box": [600, 360, 780, 600],
        },
        {
            "label": "0.1uF / 10uF Ceramic & Electrolytic Caps",
            "tags": ["capacitors", "decoupling", "filtering", "power rail", "farads"],
            "box": [600, 640, 780, 900],
        },
    ]

    return [
        GeminiDetectedBin(
            box_2d=c["box"],
            label=c["label"],
            semantic_tags=c["tags"],
        )
        for c in sample_components
    ]


def process_image(
    file_bytes: bytes,
    original_name: str,
    gemini_api_key: Optional[str] = None,
    gemini_model: Optional[str] = None,
    photo_id: Optional[str] = None,
    progress_callback: Optional[Any] = None,
    is_replace: bool = False,
    image_hash: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Complete ingestion pipeline:
    1. Preprocesses image (EXIF orientation, storage, thumbnail).
    2. Runs bin detection via Gemini API or mock fallback.
    3. Normalizes bounding boxes and formats semantic tags.
    4. Commits records to SQLite database.
    """
    if progress_callback:
        progress_callback(5, "preparing", "Inspecting image headers and metadata...", 0)

    computed_hash = image_hash or hashlib.sha256(file_bytes).hexdigest()

    data_dir = get_data_dir()
    images_dir = data_dir / "images"

    # 1. Image preprocessing via Pillow
    image = Image.open(BytesIO(file_bytes))
    image = ImageOps.exif_transpose(image)
    if image.mode != "RGB":
        image = image.convert("RGB")

    orig_width, orig_height = image.size
    MAX_IMAGE_DIMENSION = 10000
    if orig_width > MAX_IMAGE_DIMENSION or orig_height > MAX_IMAGE_DIMENSION:
        raise ValueError(
            f"Image dimensions ({orig_width}x{orig_height}) exceed maximum allowed limit of {MAX_IMAGE_DIMENSION}x{MAX_IMAGE_DIMENSION}"
        )

    assigned_photo_id = photo_id or str(uuid.uuid4())
    filename = f"{assigned_photo_id}.jpg"
    orig_filename = f"orig_{assigned_photo_id}.jpg"
    thumb_filename = f"thumb_{assigned_photo_id}.jpg"

    full_path = images_dir / filename
    orig_path = images_dir / orig_filename
    thumb_path = images_dir / thumb_filename

    if progress_callback:
        progress_callback(10, "preparing", "Compressing web-optimized display image and thumbnail...", 0)

    # Save full resolution JPEG
    image.save(orig_path, "JPEG", quality=95, optimize=True)

    # Save web-optimized display image compressed to ~500kB (max 2048px)
    web_bytes = compress_image_to_target(image, target_bytes=500 * 1024, max_dim=2048)
    with open(full_path, "wb") as f:
        f.write(web_bytes)
    logger.info("Saved ~500kB web-optimized image to %s (%d bytes)", filename, len(web_bytes))

    # Save web-optimized thumbnail (max 400px width)
    thumb_width = 400
    thumb_height = int(orig_height * (thumb_width / orig_width))
    thumb_image = image.resize((thumb_width, thumb_height), Image.Resampling.LANCZOS)
    thumb_image.save(thumb_path, "JPEG", quality=85)

    # 2. Vision detection
    api_key = gemini_api_key or os.getenv("GEMINI_API_KEY")
    detected_bins: List[GeminiDetectedBin] = []

    if api_key and api_key.strip():
        logger.info("Calling Gemini API with tiled inference for photo %s...", original_name)
        try:
            detected_bins = detect_bins_tiled(
                image=image,
                api_key=api_key,
                model_name=gemini_model,
                progress_callback=progress_callback,
            )
        except Exception as e:
            logger.error("Gemini detection error: %s", e)
            if os.getenv("ALLOW_MOCK_FALLBACK", "").lower() in ("true", "1"):
                logger.warning("Falling back to mock detection because ALLOW_MOCK_FALLBACK is enabled.")
                detected_bins = mock_detect_bins(orig_width, orig_height, original_name)
            else:
                raise
    else:
        logger.info("GEMINI_API_KEY not provided. Using offline mock detector for %s.", original_name)
        if progress_callback:
            progress_callback(30, "vision_ai", "Simulating bin detection (offline mock mode)...", 0)
        detected_bins = mock_detect_bins(orig_width, orig_height, original_name)
        if progress_callback:
            progress_callback(85, "deduplication", f"Mock detected {len(detected_bins)} bins.", len(detected_bins))

    # 3. Coordinate normalization and database persistence
    if progress_callback:
        progress_callback(90, "saving", f"Saving {len(detected_bins)} bins to inventory database...", len(detected_bins))

    # Insert or update photo in database only after successful vision detection
    if is_replace:
        photo_record = update_photo_image(
            photo_id=assigned_photo_id,
            filename=filename,
            original_name=original_name,
            width=orig_width,
            height=orig_height,
            image_hash=computed_hash,
        )
    else:
        photo_record = insert_photo(
            photo_id=assigned_photo_id,
            filename=filename,
            original_name=original_name,
            width=orig_width,
            height=orig_height,
            image_hash=computed_hash,
        )

    for b in detected_bins:
        bin_id = str(uuid.uuid4())
        norm_bbox = normalize_box(b.box_2d)

        insert_bin(
            bin_id=bin_id,
            photo_id=assigned_photo_id,
            label=b.label,
            semantic_tags=b.semantic_tags,
            bbox=norm_bbox,
        )

    if progress_callback:
        progress_callback(98, "saving", "Finalizing inventory records...", len(detected_bins))

    final_record = get_photo(assigned_photo_id) or photo_record

    if progress_callback:
        progress_callback(100, "complete", f"Successfully indexed {len(detected_bins)} bins!", len(detected_bins))

    return final_record
