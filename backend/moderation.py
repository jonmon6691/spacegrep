import io
import json
import logging
import os
from typing import Optional

from PIL import Image

from backend.models import PolicyInspectionResult

logger = logging.getLogger(__name__)


def inspect_photo_policy(
    file_bytes: bytes,
    gemini_api_key: Optional[str] = None,
    gemini_model: Optional[str] = None,
) -> PolicyInspectionResult:
    """
    Evaluates an uploaded photo against workshop community guidelines:
    - Member privacy (no visible human faces)
    - Image quality (in focus, sufficient lighting)
    - Workshop relevance (not a meme, joke, or offensive material)

    This function doesn't block ingestion.
    It returns advisory policy warnings for public transparency and member education.
    """
    warnings: list[str] = []
    has_faces = False
    is_good_quality = True
    is_meme_or_unrelated = False
    advice = None

    # Basic Pillow sanity check
    try:
        with Image.open(io.BytesIO(file_bytes)) as img:
            w, h = img.size
            if w < 300 or h < 300:
                is_good_quality = False
                warnings.append(
                    "Image resolution is very low (< 300px). Higher resolution photos yield significantly better bin OCR."
                )
    except Exception as e:
        logger.warning("Failed to open image in policy inspection: %s", e)
        return PolicyInspectionResult(
            is_good_quality=False,
            policy_warnings=["Invalid or unreadable image file."],
        )

    api_key = gemini_api_key or os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        logger.info("GEMINI_API_KEY not configured. Running offline policy inspection.")
        return PolicyInspectionResult(
            has_faces=has_faces,
            is_good_quality=is_good_quality,
            is_meme_or_unrelated=is_meme_or_unrelated,
            policy_warnings=warnings,
        )

    try:
        from google import genai
        from google.genai import types
        from pydantic import BaseModel, Field

        class GeminiPolicyInspection(BaseModel):
            has_faces: bool = Field(description="True if recognizable human faces are visible")
            is_good_quality: bool = Field(description="True if photo is clear enough to discern shelves/bins")
            is_meme_or_unrelated: bool = Field(description="True if image is a meme, cartoon, or unrelated content")
            details: str = Field(description="Brief explanation of findings")

        client = genai.Client(api_key=api_key)

        # Downscale image to max 1024px for fast, token-efficient policy check
        with Image.open(io.BytesIO(file_bytes)) as img:
            if img.mode != "RGB":
                img = img.convert("RGB")
            max_dim = 1024
            if max(img.width, img.height) > max_dim:
                scale = max_dim / max(img.width, img.height)
                img = img.resize((int(img.width * scale), int(img.height * scale)), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=80)
            scaled_bytes = buf.getvalue()

        prompt = (
            "You are a policy and quality inspector for a hackerspace / workshop visual inventory system.\n"
            "Inspect this photo uploaded by a workshop member. Evaluate three community standards:\n"
            "1. Member Privacy (Faces): Are any recognizable human faces visible in the photo? (Workshop members, bystanders, selfies). Small cartoon icons or circuit logos are NOT human faces. Posters or printed material with human faces are NOT human faces.\n"
            "2. Image Quality: Is the image clear, sufficiently lit, and in focus so storage bins/shelves are discernible? (Flag false only if severely blurred, pitch black, or unreadable).\n"
            "3. Community Standards & Relevance: Is this a meme, internet joke, cartoon screenshot, offensive imagery, or completely unrelated to workshop tools/parts/shelving?\n"
            "Return a JSON object conforming to the schema."
        )

        contents = [
            types.Part.from_bytes(data=scaled_bytes, mime_type="image/jpeg"),
            prompt,
        ]

        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=GeminiPolicyInspection,
            temperature=0.1,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

        model = gemini_model or os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )

        if response.text:
            parsed = json.loads(response.text)
            has_faces = bool(parsed.get("has_faces", False))
            is_good_quality = bool(parsed.get("is_good_quality", True))
            is_meme_or_unrelated = bool(parsed.get("is_meme_or_unrelated", False))
            advice = parsed.get("details")

            if has_faces:
                warnings.append(
                    "Visible human face(s) detected. To respect workshop member privacy, please avoid having people in frame in shelf photos."
                )
            if not is_good_quality:
                warnings.append(
                    "Photo appears blurry or poorly lit. Sharper, well-lit photos yield significantly better search and OCR accuracy."
                )
            if is_meme_or_unrelated:
                warnings.append(
                    "Image appears to be unrelated to workshop storage. Spacegrep is intended for workshop inventory."
                )

    except Exception as e:
        logger.warning("Error running Gemini policy inspection: %s", e)
        # In case of API failure, do not halt or add false warnings

    return PolicyInspectionResult(
        has_faces=has_faces,
        is_good_quality=is_good_quality,
        is_meme_or_unrelated=is_meme_or_unrelated,
        policy_warnings=warnings,
        advice=advice,
    )
