import hashlib
import io
from PIL import Image

from backend.shelf_matcher import (
    check_caption_intent,
    downscale_image_for_comparison,
    match_shelf_action,
)


def test_downscale_image_for_comparison():
    img = Image.new("RGB", (2000, 1000), color="red")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    orig_bytes = buf.getvalue()

    downscaled = downscale_image_for_comparison(orig_bytes, max_dim=512)
    with Image.open(io.BytesIO(downscaled)) as check_img:
        assert max(check_img.size) <= 512
        assert check_img.size == (512, 256)


def test_check_caption_intent():
    existing_photos = [
        {"id": "shelf-1", "original_name": "VHS_01.jpg"},
        {"id": "shelf-2", "original_name": "Hardware_Rack.jpg"},
    ]

    # Explicit shelf name in caption
    match1 = check_caption_intent("Replacing VHS_01 with new photo", existing_photos)
    assert match1 is not None
    assert match1["id"] == "shelf-1"

    # Match by stem
    match2 = check_caption_intent("Update for hardware_rack shelf", existing_photos)
    assert match2 is not None
    assert match2["id"] == "shelf-2"

    # No match
    match3 = check_caption_intent("Here is a random picture", existing_photos)
    assert match3 is None


def test_match_shelf_action_empty_catalog():
    img = Image.new("RGB", (800, 600), color="green")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    bytes_data = buf.getvalue()

    result = match_shelf_action(bytes_data, caption="", existing_photos=[])
    assert not result.is_replacement


def test_match_shelf_action_exact_hash():
    img = Image.new("RGB", (640, 480), color="yellow")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    bytes_data = buf.getvalue()
    img_hash = hashlib.sha256(bytes_data).hexdigest()

    existing_photos = [
        {"id": "shelf-uuid-1", "original_name": "Shelf1.jpg", "image_hash": img_hash}
    ]

    # Mock get_photo_by_hash using existing_photos list check
    result = match_shelf_action(
        bytes_data,
        caption="Replace shelf",
        existing_photos=existing_photos,
    )
    # Either by caption or hash
    assert result.is_replacement
    assert result.matched_photo_id == "shelf-uuid-1"
