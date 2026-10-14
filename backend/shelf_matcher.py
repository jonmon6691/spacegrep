import hashlib
import io
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image

from backend.database import get_data_dir, get_manifest, get_photo_by_hash
from backend.models import GeminiAreaComparisonResponse, ShelfMatchResult

logger = logging.getLogger(__name__)


def downscale_image_for_comparison(
    image_bytes: bytes,
    max_dim: int = 512,
) -> bytes:
    """Downscales an image to at most max_dim (e.g. 512px) to optimize token usage and latency."""
    with Image.open(io.BytesIO(image_bytes)) as img:
        if img.mode != "RGB":
            img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=80)
        return buf.getvalue()


def check_caption_intent(caption: str, existing_photos: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Checks if the user's message caption explicitly indicates replacing a specific shelf."""
    if not caption or not caption.strip():
        return None

    norm_caption = caption.lower().strip()

    # Look for explicit keywords: replace, updating, updated
    is_replace_intent = bool(re.search(r"\b(replac\w+|updat\w+|swap\w+)\b", norm_caption))

    for p in existing_photos:
        p_name = p.get("original_name", "").lower()
        p_id = p.get("id", "").lower()

        # Check for direct name match or ID match in caption
        if p_name and p_name in norm_caption:
            return p
        # Check without extension (e.g. "vhs_01" for "vhs_01.jpg")
        stem = Path(p_name).stem.lower()
        if len(stem) >= 3 and stem in norm_caption:
            return p
        if len(p_id) >= 6 and p_id[:8] in norm_caption:
            return p

    # If general "replace" keyword is used and there's only 1 shelf in inventory
    if is_replace_intent and len(existing_photos) == 1:
        return existing_photos[0]

    return None


def compare_two_images_area(
    new_img_bytes: bytes,
    existing_img_bytes: bytes,
    api_key: str,
    model_name: Optional[str] = None,
) -> GeminiAreaComparisonResponse:
    """
    Calls Gemini Flash with two scaled-down images to determine if they depict
    the same physical workshop location/area (shelves, walls, uprights, pillars).
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)

    prompt = (
        "You are an expert visual matching system for a hackerspace / workshop inventory locator.\n"
        "Compare Image 1 and Image 2. Your task is to determine whether both photos show the "
        "EXACT SAME physical location or shelf unit in the workshop.\n\n"
        "Examine the physical structure and environment:\n"
        "- Shelf framing, metal or wood uprights, shelf tier spacing, brackets, and construction.\n"
        "- Background architecture: walls, paint color/texture, brickwork, conduit pipes, power outlets, "
        "ceiling fixtures, adjacent tables or machinery.\n\n"
        "IMPORTANT: Ignore the contents inside the bins, drawer labels, or organizers. In a hackerspace, "
        "storage bins and drawers may have been completely rearranged, swapped, relabeled, repainted, or restocked.\n\n"
        "Do Image 1 and Image 2 show the same physical shelving unit or workshop area?\n"
        "Return a JSON object conforming to the schema with is_same_area (true/false), confidence (0.0 to 1.0), "
        "and structural_reasons describing the matching or differing architectural features."
    )

    contents = [
        types.Part.from_bytes(data=new_img_bytes, mime_type="image/jpeg"),
        types.Part.from_bytes(data=existing_img_bytes, mime_type="image/jpeg"),
        prompt,
    ]

    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=GeminiAreaComparisonResponse,
        temperature=0.1,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    model = model_name or os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=config,
    )

    if not response.text:
        return GeminiAreaComparisonResponse(
            is_same_area=False,
            confidence=0.0,
            structural_reasons="Empty response from Gemini vision comparison",
        )

    parsed = json.loads(response.text)
    return GeminiAreaComparisonResponse(**parsed)


def match_shelf_action(
    new_image_bytes: bytes,
    caption: str = "",
    existing_photos: Optional[List[Dict[str, Any]]] = None,
    gemini_api_key: Optional[str] = None,
    gemini_model: Optional[str] = None,
    confidence_threshold: float = 0.70,
) -> ShelfMatchResult:
    """
    Determines whether an uploaded image is a replacement of an existing shelf
    or a new shelf unit.

    Order of evaluation:
    1. Exact SHA-256 hash match against existing indexed images.
    2. Explicit user intent indicated in message caption (e.g. "Replacing VHS_01").
    3. Multimodal pairwise area comparison of two scaled-down images via Gemini Flash.
    """
    photos = existing_photos if existing_photos is not None else get_manifest()
    if not photos:
        return ShelfMatchResult(
            is_replacement=False,
            structural_reasons="No existing shelves in catalog; classified as new shelf.",
        )

    # 1. Exact SHA-256 duplicate check
    new_hash = hashlib.sha256(new_image_bytes).hexdigest()
    exact_match = get_photo_by_hash(new_hash)
    if exact_match:
        logger.info(
            "Exact SHA-256 match found with shelf id %s ('%s')",
            exact_match["id"],
            exact_match["original_name"],
        )
        return ShelfMatchResult(
            is_replacement=True,
            matched_photo_id=exact_match["id"],
            matched_photo_name=exact_match["original_name"],
            confidence=1.0,
            structural_reasons="Exact SHA-256 duplicate of existing shelf photo.",
        )

    # 2. Caption intent check
    caption_match = check_caption_intent(caption, photos)
    if caption_match:
        logger.info(
            "User caption '%s' matched shelf id %s ('%s')",
            caption,
            caption_match["id"],
            caption_match["original_name"],
        )
        return ShelfMatchResult(
            is_replacement=True,
            matched_photo_id=caption_match["id"],
            matched_photo_name=caption_match["original_name"],
            confidence=1.0,
            structural_reasons=f"User explicitly referenced shelf in caption: '{caption}'",
        )

    # 3. Pairwise two-image visual comparison via Gemini Flash
    api_key = gemini_api_key or os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        logger.info("GEMINI_API_KEY not configured. Treating as new shelf.")
        return ShelfMatchResult(
            is_replacement=False,
            structural_reasons="Offline mode; treated as new shelf.",
        )

    # Prepare downscaled new image bytes
    scaled_new_bytes = downscale_image_for_comparison(new_image_bytes, max_dim=512)
    images_dir = get_data_dir() / "images"

    def compare_shelf(photo: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[GeminiAreaComparisonResponse]]:
        # Prefer thumbnail image for existing shelf (fast, ~400px)
        thumb_path = images_dir / f"thumb_{photo['filename']}"
        full_path = images_dir / photo["filename"]

        target_path = thumb_path if thumb_path.exists() else full_path
        if not target_path.exists():
            return photo, None

        try:
            with open(target_path, "rb") as f:
                ref_bytes = f.read()
            scaled_ref_bytes = downscale_image_for_comparison(ref_bytes, max_dim=512)
            comp = compare_two_images_area(
                new_img_bytes=scaled_new_bytes,
                existing_img_bytes=scaled_ref_bytes,
                api_key=api_key,
                model_name=gemini_model,
            )
            return photo, comp
        except Exception as e:
            logger.warning("Error comparing with shelf %s: %s", photo.get("id"), e)
            return photo, None

    # Run comparisons in parallel (up to 4 concurrently)
    best_match: Optional[Dict[str, Any]] = None
    best_comp: Optional[GeminiAreaComparisonResponse] = None

    with ThreadPoolExecutor(max_workers=min(4, len(photos))) as executor:
        futures = [executor.submit(compare_shelf, p) for p in photos]
        for fut in as_completed(futures):
            photo, comp = fut.result()
            if comp and comp.is_same_area and comp.confidence >= confidence_threshold:
                if best_comp is None or comp.confidence > best_comp.confidence:
                    best_match = photo
                    best_comp = comp

    if best_match and best_comp:
        logger.info(
            "Visual comparator matched shelf %s ('%s') with confidence %.2f: %s",
            best_match["id"],
            best_match["original_name"],
            best_comp.confidence,
            best_comp.structural_reasons,
        )
        return ShelfMatchResult(
            is_replacement=True,
            matched_photo_id=best_match["id"],
            matched_photo_name=best_match["original_name"],
            confidence=best_comp.confidence,
            structural_reasons=best_comp.structural_reasons,
        )

    logger.info("Visual comparator found no matching existing shelf. Treating as new shelf.")
    return ShelfMatchResult(
        is_replacement=False,
        structural_reasons="Visual area did not match any registered workshop shelves.",
    )
