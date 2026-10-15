from dotenv import load_dotenv
load_dotenv()

import base64
import hashlib
import hmac
import io
import json
import logging
import os
import secrets
import time
from collections import defaultdict
from contextlib import asynccontextmanager
import asyncio
from pathlib import Path
from typing import Optional

from PIL import Image
from fastapi import FastAPI, HTTPException, UploadFile, File, Depends, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from backend.database import (
    delete_photo as db_delete_photo,
    get_data_dir,
    get_manifest,
    get_photo,
    get_photo_by_hash,
    init_db,
    move_photo,
    update_photo_name,
)
from backend.ingest_manager import ingest_manager
from backend.ingestion import process_image
from backend.models import (
    LoginRequest,
    LoginResponse,
    MovePhotoRequest,
    RenamePhotoRequest,
)
from backend.seed import enqueue_seed_photos
from backend.slack import start_slack_bot, stop_slack_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("spacegrep-app")

# Secret key for signing admin session tokens
SECRET_KEY = os.getenv("SECRET_KEY", "").strip()
if not SECRET_KEY:
    admin_pw = os.getenv("ADMIN_PASSWORD", "spacegrep-default-password")
    SECRET_KEY = hashlib.sha256(f"spacegrep-secret:{admin_pw}".encode("utf-8")).hexdigest()

TOKEN_EXPIRE_SECONDS = 3600  # 1 hour

# Rate limiting configuration for expensive LLM / vision AI operations (upload, replace): 10 req/min
LLM_RATE_LIMIT_WINDOW = 60.0  # seconds
LLM_RATE_LIMIT_MAX_REQUESTS = 10
_ip_llm_timestamps: dict[str, list[float]] = defaultdict(list)

# Rate limiting for lightweight administrative operations (move, rename, delete): 120 req/min (2/sec)
LIGHTWEIGHT_RATE_LIMIT_WINDOW = 60.0  # seconds
LIGHTWEIGHT_RATE_LIMIT_MAX_REQUESTS = 120
_ip_lightweight_timestamps: dict[str, list[float]] = defaultdict(list)

# Rate limiting for failed login attempts: 5 failed attempts per IP within 5 minutes
LOGIN_RATE_LIMIT_WINDOW = 300.0  # 5 minutes
LOGIN_RATE_LIMIT_MAX_FAILURES = 5
_failed_login_attempts: dict[str, list[float]] = defaultdict(list)

# Upload limits
MAX_UPLOAD_SIZE = 25 * 1024 * 1024  # 25 MB
CHUNK_SIZE = 1024 * 1024  # 1 MB chunks


async def read_and_validate_upload_image(file: UploadFile) -> tuple[bytes, str]:
    """Reads uploaded file chunks, validates size and image format, and returns bytes and sha256 hash."""
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File must be an image (jpeg, png, webp)",
        )

    contents = bytearray()
    while chunk := await file.read(CHUNK_SIZE):
        contents.extend(chunk)
        if len(contents) > MAX_UPLOAD_SIZE:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="File size exceeds maximum allowed limit (25MB)",
            )

    if len(contents) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty",
        )

    try:
        with Image.open(io.BytesIO(contents)) as test_img:
            test_img.verify()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or corrupt image file",
        )

    file_bytes_data = bytes(contents)
    file_hash = hashlib.sha256(file_bytes_data).hexdigest()
    return file_bytes_data, file_hash


def check_llm_rate_limit(request: Request) -> None:
    """Rate limits expensive Gemini Vision AI calls (upload, replace)."""
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    timestamps = _ip_llm_timestamps[client_ip]
    _ip_llm_timestamps[client_ip] = [t for t in timestamps if now - t < LLM_RATE_LIMIT_WINDOW]
    if len(_ip_llm_timestamps[client_ip]) >= LLM_RATE_LIMIT_MAX_REQUESTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded for AI vision ingestion. Maximum 10 requests per minute.",
        )
    _ip_llm_timestamps[client_ip].append(now)


def check_lightweight_rate_limit(request: Request) -> None:
    """Rate limits lightweight administrative actions (move, rename, delete)."""
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    timestamps = _ip_lightweight_timestamps[client_ip]
    _ip_lightweight_timestamps[client_ip] = [
        t for t in timestamps if now - t < LIGHTWEIGHT_RATE_LIMIT_WINDOW
    ]
    if len(_ip_lightweight_timestamps[client_ip]) >= LIGHTWEIGHT_RATE_LIMIT_MAX_REQUESTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Action rate limit exceeded. Please wait a moment.",
        )
    _ip_lightweight_timestamps[client_ip].append(now)


def check_login_rate_limit(request: Request) -> None:
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    attempts = _failed_login_attempts[client_ip]
    _failed_login_attempts[client_ip] = [t for t in attempts if now - t < LOGIN_RATE_LIMIT_WINDOW]
    if len(_failed_login_attempts[client_ip]) >= LOGIN_RATE_LIMIT_MAX_FAILURES:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Please try again after 5 minutes.",
        )


def record_failed_login(request: Request) -> None:
    client_ip = request.client.host if request.client else "unknown"
    _failed_login_attempts[client_ip].append(time.time())


def clear_failed_logins(request: Request) -> None:
    client_ip = request.client.host if request.client else "unknown"
    _failed_login_attempts.pop(client_ip, None)


def generate_admin_token(expires_in: int = TOKEN_EXPIRE_SECONDS) -> str:
    """Generates a stateless HMAC-SHA256 signed Bearer token."""
    now = time.time()
    payload = {
        "role": "admin",
        "iat": int(now),
        "exp": int(now + expires_in),
        "jti": secrets.token_hex(16),
    }
    payload_json = json.dumps(payload, separators=(",", ":"))
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode("utf-8")).decode("utf-8").rstrip("=")
    sig = hmac.new(SECRET_KEY.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).decode("utf-8").rstrip("=")
    return f"{payload_b64}.{sig_b64}"


http_bearer = HTTPBearer(auto_error=False)


def verify_admin_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(http_bearer),
) -> dict:
    """Verifies that the client provided a valid, unexpired HMAC-signed Bearer token."""
    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid authorization header. Bearer token required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials.strip()
    parts = token.split(".")
    if len(parts) != 2:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token format",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload_b64, sig_b64 = parts
    # Verify HMAC signature
    expected_sig = hmac.new(SECRET_KEY.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()
    expected_sig_b64 = base64.urlsafe_b64encode(expected_sig).decode("utf-8").rstrip("=")

    if not hmac.compare_digest(sig_b64, expected_sig_b64):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token signature",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Decode and inspect payload
    try:
        padding = 4 - (len(payload_b64) % 4)
        if padding != 4:
            payload_b64_padded = payload_b64 + ("=" * padding)
        else:
            payload_b64_padded = payload_b64
        payload_bytes = base64.urlsafe_b64decode(payload_b64_padded)
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Corrupt token payload",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Check expiration
    exp = payload.get("exp", 0)
    if time.time() > exp:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if payload.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions",
        )

    admin_password = os.getenv("ADMIN_PASSWORD", "").strip()
    if not admin_password:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin operations are disabled: ADMIN_PASSWORD is not set or is blank in .env",
        )

    return payload


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize database immediately
    init_db()
    # Start the async background ingestion worker
    await ingest_manager.start()
    try:
        queued_count = enqueue_seed_photos(ingest_manager)
        logger.info("Startup complete. Enqueued %d seed photos for background ingestion.", queued_count)
    except Exception as e:
        logger.exception("Error during background auto-seed check: %s", e)

    # Start the Slack Socket Mode bot if configured
    try:
        await start_slack_bot()
    except Exception as e:
        logger.exception("Failed to initialize Slack bot during startup: %s", e)

    yield

    # Shutdown Slack bot
    try:
        await stop_slack_bot()
    except Exception as e:
        logger.warning("Error stopping Slack bot during shutdown: %s", e)

    # Shutdown ingestion worker
    await ingest_manager.stop()


app = FastAPI(
    title="Hackerspace Visual Inventory Locator",
    version="1.0.0",
    lifespan=lifespan,
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response


app.add_middleware(SecurityHeadersMiddleware)

# Allowed origins configuration
default_origins = [
    "http://localhost:5173",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://127.0.0.1:5173",
]
env_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
domain_name = os.getenv("DOMAIN_NAME", "").strip()
if domain_name:
    default_origins.append(f"https://{domain_name}")
    default_origins.append(f"http://{domain_name}")

allowed_origins = list(dict.fromkeys(default_origins + env_origins))

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


@app.get("/api/health")
def health_check():
    return {"status": "ok", "service": "spacegrep-app"}


@app.get("/api/manifest")
def get_inventory_manifest():
    """Returns complete shelf photos and bin annotations for client search."""
    return get_manifest()


@app.get("/api/config")
def get_config():
    """Returns client configuration such as optional whitebox logo URL and link URL."""
    return {
        "whitebox_logo_url": os.getenv("WHITEBOX_LOGO_URL", "") or os.getenv("VITE_WHITEBOX_LOGO_URL", ""),
        "whitebox_link_url": os.getenv("WHITEBOX_LINK_URL", "") or os.getenv("VITE_WHITEBOX_LINK_URL", ""),
    }


@app.get("/api/ingest/active")
def get_active_ingest_jobs():
    """Returns all currently queued or processing ingestion jobs."""
    return [j.to_dict() for j in ingest_manager.get_active_jobs()]


@app.get("/api/ingest/jobs/{job_id}")
def get_ingest_job_status(job_id: str):
    """Returns the current status of an ingestion job."""
    job = ingest_manager.get_job(job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Ingestion job {job_id} not found",
        )
    return job.to_dict()


@app.get("/api/ingest/jobs/{job_id}/stream")
async def stream_job_progress(job_id: str, request: Request):
    """Server-Sent Events (SSE) stream for a single photo ingestion job."""
    job = ingest_manager.get_job(job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Ingestion job {job_id} not found",
        )

    async def event_generator():
        q = ingest_manager.subscribe_job(job_id)
        try:
            # Yield initial state
            current_job = ingest_manager.get_job(job_id)
            if current_job:
                yield f"data: {json.dumps(current_job.to_dict())}\n\n"
                if current_job.status in ("completed", "failed"):
                    return

            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {json.dumps(data)}\n\n"
                    if data.get("status") in ("completed", "failed"):
                        break
                except asyncio.TimeoutError:
                    # Keepalive ping to prevent proxy timeout
                    yield ": keepalive\n\n"
        finally:
            ingest_manager.unsubscribe_job(job_id, q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/ingest/stream")
async def stream_global_ingest_events(request: Request):
    """Server-Sent Events (SSE) stream for all background ingestion activities (seed & uploads)."""
    async def event_generator():
        q = ingest_manager.subscribe_global()
        try:
            # Send initial active jobs snapshot
            active_jobs = [j.to_dict() for j in ingest_manager.get_active_jobs()]
            yield f"event: initial\ndata: {json.dumps(active_jobs)}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {json.dumps(data)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            ingest_manager.unsubscribe_global(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post(
    "/api/auth/login",
    response_model=LoginResponse,
    dependencies=[Depends(check_login_rate_limit)],
)
def login(request: Request, body: LoginRequest):
    """Authenticates admin password and issues a signed Bearer token."""
    admin_password = os.getenv("ADMIN_PASSWORD", "").strip()
    if not admin_password:
        record_failed_login(request)
        logger.warning(
            "Admin login rejected: ADMIN_PASSWORD is not set or is blank in environment/.env"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin login is disabled: ADMIN_PASSWORD is not set or is blank in .env",
        )

    # Reject if submitted password is empty/whitespace or does not match
    submitted = body.password.strip() if body.password else ""
    if not submitted or not hmac.compare_digest(body.password, admin_password):
        record_failed_login(request)
        logger.warning(
            "Failed admin login attempt from IP %s",
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin password",
        )

    clear_failed_logins(request)
    token = generate_admin_token(expires_in=TOKEN_EXPIRE_SECONDS)
    return LoginResponse(
        token=token,
        token_type="bearer",
        expires_in=TOKEN_EXPIRE_SECONDS,
    )


@app.post(
    "/api/photos",
    dependencies=[Depends(check_llm_rate_limit), Depends(verify_admin_token)],
)
async def upload_photo(
    response: Response,
    file: UploadFile = File(...),
    sync: bool = False,
):
    """
    Ingests a new shelf photo.
    By default, initiates asynchronous processing and returns HTTP 202 with a job_id.
    If sync=true is specified, runs synchronously and returns HTTP 201 with the completed photo record.
    """
    file_bytes_data, file_hash = await read_and_validate_upload_image(file)
    filename = file.filename or "uploaded_shelf.jpg"

    existing = get_photo_by_hash(file_hash)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This image has already been indexed as '{existing['original_name']}'. Duplicate upload skipped.",
        )

    if sync:
        try:
            record = await asyncio.to_thread(
                process_image,
                file_bytes=file_bytes_data,
                original_name=filename,
                image_hash=file_hash,
            )
            response.status_code = status.HTTP_201_CREATED
            return {"status": "created", "photo": record}
        except ValueError as ve:
            logger.warning("Validation error in photo upload: %s", ve)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(ve),
            )
        except Exception as e:
            logger.exception("Failed to process photo upload: %s", e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to process photo due to an internal server error",
            )

    # Asynchronous ingestion flow via IngestManager
    job = ingest_manager.create_job(
        original_name=filename,
        file_bytes=file_bytes_data,
        is_seed=False,
    )
    response.status_code = status.HTTP_202_ACCEPTED
    return {
        "status": "queued",
        "job_id": job.job_id,
        "photo_id": job.photo_id,
        "message": f"Shelf photo '{filename}' queued for asynchronous processing.",
    }


@app.delete(
    "/api/photos/{photo_id}",
    dependencies=[Depends(check_lightweight_rate_limit), Depends(verify_admin_token)],
)
def delete_photo_endpoint(
    photo_id: str,
):
    """Deletes a shelf photo, its thumbnail, and all associated bin annotations."""
    photo = get_photo(photo_id)
    if not photo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Photo with id {photo_id} not found",
        )

    data_dir = get_data_dir()
    images_dir = data_dir / "images"

    # Remove image files from disk
    main_img = images_dir / photo["filename"]
    orig_img = images_dir / f"orig_{photo['filename']}"
    thumb_img = images_dir / f"thumb_{photo['filename']}"
    if main_img.exists():
        main_img.unlink(missing_ok=True)
    if orig_img.exists():
        orig_img.unlink(missing_ok=True)
    if thumb_img.exists():
        thumb_img.unlink(missing_ok=True)

    success = db_delete_photo(photo_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete database record",
        )

    return {"status": "deleted", "id": photo_id}


@app.patch(
    "/api/photos/{photo_id}",
    dependencies=[Depends(check_lightweight_rate_limit), Depends(verify_admin_token)],
)
def rename_photo_endpoint(
    photo_id: str,
    body: RenamePhotoRequest,
):
    """Renames the original display name of a shelf photo."""
    photo = get_photo(photo_id)
    if not photo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Photo with id {photo_id} not found",
        )

    clean_name = body.name.strip()
    if not clean_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Photo name cannot be empty",
        )

    updated = update_photo_name(photo_id, clean_name)
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update photo name in database",
        )

    return {"status": "renamed", "photo": updated}


@app.post(
    "/api/photos/{photo_id}/move",
    dependencies=[Depends(check_lightweight_rate_limit), Depends(verify_admin_token)],
)
def move_photo_endpoint(
    photo_id: str,
    body: MovePhotoRequest,
):
    """Moves a shelf photo up or down in the display order."""
    photo = get_photo(photo_id)
    if not photo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Photo with id {photo_id} not found",
        )

    success = move_photo(photo_id, body.direction)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to move photo {body.direction}",
        )

    return {"status": "moved", "photos": get_manifest()}


@app.post(
    "/api/photos/{photo_id}/replace",
    dependencies=[Depends(check_llm_rate_limit), Depends(verify_admin_token)],
)
@app.put(
    "/api/photos/{photo_id}",
    dependencies=[Depends(check_llm_rate_limit), Depends(verify_admin_token)],
)
async def replace_photo_endpoint(
    photo_id: str,
    response: Response,
    file: UploadFile = File(...),
    sync: bool = False,
):
    """
    Replaces a shelf photo with a new image and re-runs bin detection.
    Maintains the existing photo ID and display order.
    """
    photo = get_photo(photo_id)
    if not photo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Photo with id {photo_id} not found",
        )

    file_bytes_data, file_hash = await read_and_validate_upload_image(file)
    filename = photo["original_name"]

    existing = get_photo_by_hash(file_hash)
    if existing and existing["id"] != photo_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This image has already been indexed on shelf '{existing['original_name']}'. Duplicate replacement skipped.",
        )

    if sync:
        try:
            record = await asyncio.to_thread(
                process_image,
                file_bytes=file_bytes_data,
                original_name=filename,
                photo_id=photo_id,
                is_replace=True,
                image_hash=file_hash,
            )
            response.status_code = status.HTTP_200_OK
            return {"status": "replaced", "photo": record}
        except ValueError as ve:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(ve),
            )
        except Exception as e:
            logger.exception("Failed to replace photo: %s", e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(e),
            )

    job = ingest_manager.create_job(
        original_name=filename,
        file_bytes=file_bytes_data,
        photo_id=photo_id,
        is_seed=False,
        is_replace=True,
    )
    response.status_code = status.HTTP_202_ACCEPTED
    return {
        "status": "queued",
        "job_id": job.job_id,
        "photo_id": photo_id,
        "message": f"Shelf photo replacement '{filename}' queued for processing.",
    }


# Mount image files
data_dir = get_data_dir()
images_path = data_dir / "images"
images_path.mkdir(parents=True, exist_ok=True)
app.mount("/images", StaticFiles(directory=str(images_path)), name="images")


def safe_file_response(base_dir: Path, relative_path: str) -> Response:
    """Safely serves static files preventing directory traversal outside base_dir."""
    base_resolved = base_dir.resolve()
    target = (base_dir / relative_path).resolve()

    if not target.is_relative_to(base_resolved):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not found",
        )

    if target.exists() and target.is_file():
        return FileResponse(target)

    index_file = base_dir / "index.html"
    if index_file.exists() and index_file.is_file():
        return FileResponse(index_file)

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Not found",
    )


# Serve built frontend static assets if present
frontend_dist = Path(__file__).parent.parent / "frontend" / "dist"
static_dir = Path("/app/static")

if frontend_dist.exists() and (frontend_dist / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=str(frontend_dist / "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        return safe_file_response(frontend_dist, full_path)

elif static_dir.exists() and (static_dir / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=str(static_dir / "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa_docker(full_path: str):
        return safe_file_response(static_dir, full_path)

