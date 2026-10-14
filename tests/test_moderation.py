import io
from PIL import Image

from backend.moderation import inspect_photo_policy


def test_inspect_photo_policy_small_image():
    # Image smaller than 300x300 should trigger a quality warning
    img = Image.new("RGB", (100, 100), color="blue")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    data = buf.getvalue()

    result = inspect_photo_policy(data)
    assert not result.is_good_quality
    assert any("resolution is very low" in w for w in result.policy_warnings)


def test_inspect_photo_policy_clean_offline():
    # In offline mode (no GEMINI_API_KEY), a normal image returns clean policy inspection
    img = Image.new("RGB", (800, 600), color="gray")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    data = buf.getvalue()

    result = inspect_photo_policy(data)
    assert result.is_good_quality
    assert not result.has_faces
    assert not result.is_meme_or_unrelated
    assert len(result.policy_warnings) == 0


def test_inspect_photo_policy_invalid_bytes():
    result = inspect_photo_policy(b"not an image file")
    assert not result.is_good_quality
    assert any("Invalid or unreadable" in w for w in result.policy_warnings)
