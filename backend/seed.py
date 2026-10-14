from dotenv import load_dotenv
load_dotenv()

import hashlib
import logging
from pathlib import Path
from PIL import Image, ImageDraw

from backend.database import (
    get_data_dir,
    get_manifest,
    get_photo_by_hash,
    get_photo_by_original_name,
    init_db,
)
from backend.ingestion import process_image

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def generate_default_sample_shelf_photo(target_path: Path):
    """
    Creates a realistic synthetic workshop parts organizer shelf photo
    with clearly drawn and labeled storage drawers so the app is instantly
    visual and functional even before physical photos are dropped in.
    """
    width, height = 1600, 1000
    img = Image.new("RGB", (width, height), color=(28, 32, 40))
    draw = ImageDraw.Draw(img)

    # Shelf frame
    draw.rectangle([40, 40, width - 40, height - 40], outline=(70, 80, 95), width=8, fill=(35, 42, 54))

    # Grid of bins (3 rows x 3 columns)
    bins_data = [
        # Row 1
        ("Kapton Tape & PVC Electrical Tape", ["#10b981", "#059669"], "TAPE / INSULATION"),
        ("CR2032 3V Coin Cell Batteries", ["#3b82f6", "#2563eb"], "COIN CELLS & 3V"),
        ("ESP32 & ESP8266 Dev Boards", ["#8b5cf6", "#7c3aed"], "MICROCONTROLLERS"),
        # Row 2
        ("Digital Multimeters & Test Leads", ["#f59e0b", "#d97706"], "TEST & MEASURE"),
        ("N-Channel MOSFETs (IRFZ44N / 2N7000)", ["#06b6d4", "#0891b2"], "DISCRETE SEMIS"),
        ("Weller Soldering Tips & Brass Sponges", ["#ef4444", "#dc2626"], "SOLDERING STATION"),
        # Row 3
        ("Heat Shrink Tubing Assortment", ["#ec4899", "#db2777"], "HEAT SHRINK SLEEVING"),
        ("Anti-Static ESD Wrist Straps", ["#14b8a6", "#0d9488"], "ESD PROTECTION"),
        ("0.1uF / 10uF Ceramic & Electrolytic Caps", ["#6366f1", "#4f46e5"], "CAPACITORS & PASSIVES"),
    ]

    cols, rows = 3, 3
    margin_x, margin_y = 70, 70
    spacing_x, spacing_y = 35, 35
    bin_w = (width - 2 * margin_x - (cols - 1) * spacing_x) // cols
    bin_h = (height - 2 * margin_y - (rows - 1) * spacing_y) // rows

    for idx, (title, (c_bg, c_border), category) in enumerate(bins_data):
        r = idx // cols
        c = idx % cols
        bx = margin_x + c * (bin_w + spacing_x)
        by = margin_y + r * (bin_h + spacing_y)

        # Draw drawer container
        draw.rectangle([bx, by, bx + bin_w, by + bin_h], outline=(90, 105, 125), width=3, fill=(45, 55, 72))

        # Drawer handle
        handle_w = bin_w // 3
        hx = bx + (bin_w - handle_w) // 2
        hy = by + 20
        draw.rectangle([hx, hy, hx + handle_w, hy + 12], fill=(130, 145, 165), outline=(160, 175, 195), width=1)

        # Drawer label card
        card_margin = 25
        cx1 = bx + card_margin
        cy1 = by + 45
        cx2 = bx + bin_w - card_margin
        cy2 = by + bin_h - 25
        draw.rectangle([cx1, cy1, cx2, cy2], fill=(245, 245, 248), outline=(200, 200, 210), width=2)

        # Accent bar
        draw.rectangle([cx1, cy1, cx2, cy1 + 8], fill=c_border)

        # Text on label
        draw.text((cx1 + 12, cy1 + 16), category, fill=(100, 116, 139))
        draw.text((cx1 + 12, cy1 + 38), title, fill=(15, 23, 42))

        # Barcode decoration on card
        for bar_i in range(18):
            b_x = cx1 + 12 + bar_i * 7
            b_w = 3 if bar_i % 3 == 0 else 1
            draw.rectangle([b_x, cy2 - 22, b_x + b_w, cy2 - 8], fill=(30, 41, 59))

    img.save(target_path, "JPEG", quality=95)
    logger.info("Generated default sample shelf image at %s", target_path)


def auto_seed(progress_callback=None):
    """
    Scans ./data/seed_photos/ on boot and ingests any photos not yet present in the database.
    If seed_photos/ is empty and database has no photos, creates a realistic demo shelf photo.
    Synchronous version used by CLI and tests.
    """
    init_db()
    data_dir = get_data_dir()
    seed_dir = data_dir / "seed_photos"
    seed_dir.mkdir(parents=True, exist_ok=True)

    # Check existing files
    seed_files = [f for f in seed_dir.iterdir() if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS]

    manifest = get_manifest()

    # If both seed_photos and database are empty, create initial sample shelf
    if not seed_files and len(manifest) == 0:
        sample_path = seed_dir / "sample_workshop_shelf_01.jpg"
        logger.info("No seed photos found and DB is empty. Creating starter sample shelf: %s", sample_path.name)
        generate_default_sample_shelf_photo(sample_path)
        seed_files = [sample_path]

    ingested_count = 0
    for file_path in sorted(seed_files):
        try:
            with open(file_path, "rb") as f:
                photo_bytes = f.read()
        except Exception as e:
            logger.error("Failed to read seed file %s: %s", file_path.name, e)
            continue

        file_hash = hashlib.sha256(photo_bytes).hexdigest()
        existing = get_photo_by_hash(file_hash)
        if existing is None:
            existing = get_photo_by_original_name(file_path.name)

        if existing is not None:
            logger.debug(
                "Seed photo %s is already indexed in database (id=%s, name='%s'). Skipping.",
                file_path.name,
                existing["id"],
                existing["original_name"],
            )
            continue

        logger.info("Auto-ingesting new seed photo: %s", file_path.name)
        try:
            record = process_image(
                photo_bytes,
                original_name=file_path.name,
                progress_callback=progress_callback,
                image_hash=file_hash,
            )
            bin_count = len(record.get("bins", []))
            logger.info("Successfully ingested %s (%d bins registered)", file_path.name, bin_count)
            ingested_count += 1
        except Exception as e:
            logger.exception("Failed to ingest seed photo %s: %s", file_path.name, e)

    return ingested_count


def enqueue_seed_photos(manager) -> int:
    """
    Scans ./data/seed_photos/ and enqueues any unindexed photos into IngestManager
    for non-blocking background ingestion.
    """
    init_db()
    data_dir = get_data_dir()
    seed_dir = data_dir / "seed_photos"
    seed_dir.mkdir(parents=True, exist_ok=True)

    seed_files = [f for f in seed_dir.iterdir() if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS]
    manifest = get_manifest()

    if not seed_files and len(manifest) == 0:
        sample_path = seed_dir / "sample_workshop_shelf_01.jpg"
        logger.info("No seed photos found and DB is empty. Creating starter sample shelf: %s", sample_path.name)
        generate_default_sample_shelf_photo(sample_path)
        seed_files = [sample_path]

    queued_count = 0
    for file_path in sorted(seed_files):
        try:
            with open(file_path, "rb") as f:
                photo_bytes = f.read()
        except Exception as e:
            logger.error("Failed to read seed file %s: %s", file_path.name, e)
            continue

        file_hash = hashlib.sha256(photo_bytes).hexdigest()
        existing = get_photo_by_hash(file_hash)
        if existing is None:
            existing = get_photo_by_original_name(file_path.name)

        if existing is not None:
            logger.debug(
                "Seed photo %s is already indexed in database (id=%s, name='%s'). Skipping.",
                file_path.name,
                existing["id"],
                existing["original_name"],
            )
            continue

        logger.info("Enqueueing seed photo for background ingestion: %s", file_path.name)
        manager.create_job(
            original_name=file_path.name,
            file_bytes=photo_bytes,
            file_path=file_path,
            is_seed=True,
        )
        queued_count += 1

    return queued_count


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    count = auto_seed()
    print(f"Seed complete. {count} new photos ingested.")
