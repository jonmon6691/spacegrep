from typing import List, Optional
from pydantic import BaseModel, Field


class BinBase(BaseModel):
    label: str
    semantic_tags: List[str] = Field(default_factory=list)
    bbox: List[float] = Field(description="[x, y, w, h] normalized to 0.0 - 1.0")


class Bin(BinBase):
    id: str
    photo_id: str


class Photo(BaseModel):
    id: str
    filename: str
    original_name: str
    width: int
    height: int
    created_at: str
    order_index: Optional[int] = 0
    image_hash: Optional[str] = None
    bins: List[Bin] = Field(default_factory=list)


class RenamePhotoRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255, description="New display name for the shelf photo")


class MovePhotoRequest(BaseModel):
    direction: str = Field(pattern="^(up|down)$", description="Move direction ('up' or 'down')")


class ManifestResponse(BaseModel):
    photos: List[Photo]


class GeminiDetectedBin(BaseModel):
    box_2d: List[int] = Field(
        description="Bounding box [ymin, xmin, ymax, xmax] scaled 0 to 1000"
    )
    label: str = Field(description="Transcribed part/tool label on bin or shelf")
    semantic_tags: List[str] = Field(
        default_factory=list,
        description="3-5 functional keywords, synonyms, or use-cases",
    )


class SearchResult(BaseModel):
    photo_id: str
    bin_id: str
    label: str
    score: float
    confidence_tier: str  # "high" (>= 0.70) or "moderate" (0.50 - 0.69)


class LoginRequest(BaseModel):
    password: str


class LoginResponse(BaseModel):
    token: str
    token_type: str = "bearer"
    expires_in: int


class PolicyInspectionResult(BaseModel):
    has_faces: bool = False
    is_good_quality: bool = True
    is_meme_or_unrelated: bool = False
    policy_warnings: List[str] = Field(default_factory=list)
    advice: Optional[str] = None


class GeminiAreaComparisonResponse(BaseModel):
    is_same_area: bool
    confidence: float = Field(ge=0.0, le=1.0)
    structural_reasons: str


class ShelfMatchResult(BaseModel):
    is_replacement: bool = False
    matched_photo_id: Optional[str] = None
    matched_photo_name: Optional[str] = None
    confidence: float = 0.0
    structural_reasons: Optional[str] = None

