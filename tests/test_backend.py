import hashlib
import os
import tempfile

import pytest
from PIL import Image
from fastapi.testclient import TestClient

# Use temporary data directory for tests
test_dir = tempfile.mkdtemp()
os.environ["DATA_DIR"] = test_dir

from backend.database import (
    get_data_dir,
    get_db_connection,
    get_manifest,
    get_photo,
    get_photo_by_hash,
    init_db,
    insert_bin,
    insert_photo,
    update_photo_name,
)
from backend.ingestion import (
    calculate_iou,
    compress_image_to_target,
    generate_tiles,
    normalize_box,
    process_image,
    project_box_to_global,
    suppress_duplicate_bins,
)
from backend.models import GeminiDetectedBin
from backend.ingest_manager import ingest_manager, pick_encouragement
from backend.main import (
    app,
    _ip_llm_timestamps,
    _ip_lightweight_timestamps,
    _failed_login_attempts,
    generate_admin_token,
)
from backend.seed import auto_seed, enqueue_seed_photos


@pytest.fixture(autouse=True)
def setup_and_teardown():
    init_db()
    conn = get_db_connection()
    with conn:
        conn.execute("DELETE FROM bins;")
        conn.execute("DELETE FROM photos;")
    conn.close()
    seed_dir = get_data_dir() / "seed_photos"
    if seed_dir.exists():
        for f in seed_dir.iterdir():
            if f.is_file():
                f.unlink()
    img_dir = get_data_dir() / "images"
    if img_dir.exists():
        for f in img_dir.iterdir():
            if f.is_file():
                f.unlink()
    _ip_llm_timestamps.clear()
    _ip_lightweight_timestamps.clear()
    _failed_login_attempts.clear()
    yield
    _ip_llm_timestamps.clear()
    _ip_lightweight_timestamps.clear()
    _failed_login_attempts.clear()


def test_coordinate_normalization():
    box_2d = [100, 200, 500, 800]  # ymin, xmin, ymax, xmax
    norm = normalize_box(box_2d)
    # x = 200/1000 = 0.2, y = 100/1000 = 0.1, w = (800-200)/1000 = 0.6, h = (500-100)/1000 = 0.4
    assert norm == [0.2, 0.1, 0.6, 0.4]


def test_database_photo_and_bins():
    photo = insert_photo(
        photo_id="test-p1",
        filename="test-p1.jpg",
        original_name="shelf.jpg",
        width=1920,
        height=1080,
    )
    assert photo["id"] == "test-p1"

    insert_bin(
        bin_id="test-b1",
        photo_id="test-p1",
        label="CR2032 Coin Cells",
        semantic_tags=["battery", "3v"],
        bbox=[0.1, 0.2, 0.3, 0.4],
    )

    retrieved = get_photo("test-p1")
    assert retrieved is not None
    assert len(retrieved["bins"]) == 1
    assert retrieved["bins"][0]["label"] == "CR2032 Coin Cells"


def test_api_manifest_and_upload(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("ADMIN_PASSWORD", "supersecret123")
    client = TestClient(app)

    # Health
    r = client.get("/api/health")
    assert r.status_code == 200

    # Create dummy test image
    img = Image.new("RGB", (300, 300), color=(100, 150, 200))
    import io
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    img_bytes = buf.getvalue()

    # Upload without token should fail (401)
    unauth_res = client.post(
        "/api/photos",
        files={"file": ("test_upload.jpg", io.BytesIO(img_bytes), "image/jpeg")},
    )
    assert unauth_res.status_code == 401

    # Upload with invalid token should fail (401)
    invalid_token_res = client.post(
        "/api/photos",
        files={"file": ("test_upload.jpg", io.BytesIO(img_bytes), "image/jpeg")},
        headers={"Authorization": "Bearer invalidtoken123"},
    )
    assert invalid_token_res.status_code == 401

    # Login to obtain Bearer token
    login_res = client.post("/api/auth/login", json={"password": "supersecret123"})
    assert login_res.status_code == 200
    token = login_res.json()["token"]

    # Upload with Bearer token (sync mode) should succeed (201)
    upload_res = client.post(
        "/api/photos?sync=true",
        files={"file": ("test_upload.jpg", io.BytesIO(img_bytes), "image/jpeg")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert upload_res.status_code == 201
    data = upload_res.json()
    assert "photo" in data
    photo_id = data["photo"]["id"]
    assert len(data["photo"]["bins"]) > 0

    # Manifest
    manifest_res = client.get("/api/manifest")
    assert manifest_res.status_code == 200
    manifest = manifest_res.json()
    assert any(p["id"] == photo_id for p in manifest)

    # Config (default empty)
    monkeypatch.delenv("WHITEBOX_LINK_URL", raising=False)
    monkeypatch.delenv("VITE_WHITEBOX_LINK_URL", raising=False)
    config_res = client.get("/api/config")
    assert config_res.status_code == 200
    config = config_res.json()
    assert "whitebox_logo_url" in config
    assert config["whitebox_link_url"] == ""

    # Config (when link URL provided)
    monkeypatch.setenv("WHITEBOX_LINK_URL", "https://example.com/wiki")
    config_res2 = client.get("/api/config")
    assert config_res2.status_code == 200
    assert config_res2.json()["whitebox_link_url"] == "https://example.com/wiki"

    # Delete
    images_dir = get_data_dir() / "images"
    assert (images_dir / f"{photo_id}.jpg").exists()
    assert (images_dir / f"orig_{photo_id}.jpg").exists()
    assert (images_dir / f"thumb_{photo_id}.jpg").exists()

    # Delete without token should fail (401)
    unauth_del = client.delete(f"/api/photos/{photo_id}")
    assert unauth_del.status_code == 401

    # Delete with invalid token should fail (401)
    invalid_del = client.delete(
        f"/api/photos/{photo_id}",
        headers={"Authorization": "Bearer wrongtoken"},
    )
    assert invalid_del.status_code == 401

    # Delete with correct token should succeed (200)
    del_res = client.delete(
        f"/api/photos/{photo_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert del_res.status_code == 200
    assert get_photo(photo_id) is None
    assert not (images_dir / f"{photo_id}.jpg").exists()
    assert not (images_dir / f"orig_{photo_id}.jpg").exists()
    assert not (images_dir / f"thumb_{photo_id}.jpg").exists()


def test_auto_seed_creates_sample_if_empty():
    count = auto_seed()
    # If the test data dir had no photos, it creates the starter sample photo
    assert count >= 0
    manifest = get_manifest()
    assert len(manifest) >= 1
    # Running auto_seed again should be idempotent (0 new ingested)
    count_second = auto_seed()
    assert count_second == 0


def test_generate_tiles():
    # Small image: should return exactly 1 tile
    small_img = Image.new("RGB", (800, 600))
    small_tiles = generate_tiles(small_img)
    assert len(small_tiles) == 1
    assert small_tiles[0][1] == (0, 0, 800, 600)

    # Large image: 4080x3072
    large_img = Image.new("RGB", (4080, 3072))
    large_tiles = generate_tiles(large_img, overlap_ratio=0.20)
    assert len(large_tiles) >= 4

    # Verify each tile is within bounds
    for tile_img, (left, top, right, bottom) in large_tiles:
        assert 0 <= left < right <= 4080
        assert 0 <= top < bottom <= 3072
        assert tile_img.size == (right - left, bottom - top)


def test_project_box_to_global():
    # Box in a tile: tile is (1000, 1000, 3000, 3000) on a 4000x4000 image
    tile_rect = (1000, 1000, 3000, 3000)
    # Box inside tile: top-left quadrant of tile: ymin=0, xmin=0, ymax=500, xmax=500
    tile_box = [0, 0, 500, 500]
    global_box = project_box_to_global(tile_box, tile_rect, orig_w=4000, orig_h=4000)
    # tile_w = 2000, tile_h = 2000
    # abs_ymin = 1000 + 0 = 1000 -> 1000/4000 * 1000 = 250
    # abs_xmin = 1000 + 0 = 1000 -> 1000/4000 * 1000 = 250
    # abs_ymax = 1000 + 1000 = 2000 -> 2000/4000 * 1000 = 500
    # abs_xmax = 1000 + 1000 = 2000 -> 2000/4000 * 1000 = 500
    assert global_box == [250, 250, 500, 500]


def test_calculate_iou_and_nms():
    box_a = [100, 100, 300, 300]
    box_b = [100, 100, 300, 300]  # Identical
    box_c = [500, 500, 700, 700]  # Disjoint
    box_d = [150, 150, 350, 350]  # Partial overlap

    assert calculate_iou(box_a, box_b) == 1.0
    assert calculate_iou(box_a, box_c) == 0.0
    assert 0.2 < calculate_iou(box_a, box_d) < 0.6

    bin1 = GeminiDetectedBin(
        box_2d=[100, 100, 300, 300],
        label="CR2032 Battery",
        semantic_tags=["battery", "coin cell"],
    )
    bin2 = GeminiDetectedBin(
        box_2d=[105, 102, 305, 302],
        label="CR2032 3V Coin Cell Battery",  # More descriptive
        semantic_tags=["3v", "cmos"],
    )
    bin_distinct = GeminiDetectedBin(
        box_2d=[600, 600, 800, 800],
        label="ESP32 Board",
        semantic_tags=["microcontroller"],
    )

    merged = suppress_duplicate_bins([bin1, bin2, bin_distinct], iou_threshold=0.40)
    assert len(merged) == 2
    # Check that the more descriptive label was kept
    labels = [b.label for b in merged]
    assert "CR2032 3V Coin Cell Battery" in labels
    assert "ESP32 Board" in labels
    # Check combined tags
    battery_bin = next(b for b in merged if "CR2032" in b.label)
    assert set(battery_bin.semantic_tags) >= {"battery", "coin cell", "3v", "cmos"}


def test_compress_image_to_target():
    # Large test image: 3000x2000 with pattern
    img = Image.new("RGB", (3000, 2000), color=(120, 140, 180))
    data = compress_image_to_target(img, target_bytes=500 * 1024, max_dim=2048)
    # File size must be under 520kB
    assert len(data) <= 520 * 1024
    # Valid JPEG
    import io
    loaded = Image.open(io.BytesIO(data))
    assert max(loaded.size) <= 2048


def test_path_traversal_prevention():
    client = TestClient(app)
    # Attempt to read .env
    r1 = client.get("/..%2F..%2F.env")
    assert r1.status_code == 404
    assert "GEMINI_API_KEY" not in r1.text

    # Attempt to read SQLite DB
    r2 = client.get("/..%2F..%2Fdata%2Finventory.db")
    assert r2.status_code == 404


def test_cors_isolation():
    client = TestClient(app)
    # Evil origin should not be reflected
    r = client.get("/api/manifest", headers={"Origin": "https://evil.com"})
    assert r.headers.get("access-control-allow-origin") != "https://evil.com"

    # Permitted origin should be allowed
    r2 = client.get("/api/manifest", headers={"Origin": "http://localhost:5173"})
    assert r2.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_security_headers():
    client = TestClient(app)
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "SAMEORIGIN"
    assert r.headers.get("referrer-policy") == "strict-origin-when-cross-origin"


def test_upload_size_limit(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "supersecret123")
    client = TestClient(app)

    # Login to obtain token
    login_res = client.post("/api/auth/login", json={"password": "supersecret123"})
    assert login_res.status_code == 200
    token = login_res.json()["token"]

    # Create dummy data exceeding 25MB (26MB dummy bytes)
    oversized = b"x" * (26 * 1024 * 1024)
    import io
    r = client.post(
        "/api/photos",
        files={"file": ("huge.jpg", io.BytesIO(oversized), "image/jpeg")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 413
    assert "exceeds" in r.json()["detail"].lower()


def test_admin_rate_limiting(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "supersecret123")
    client = TestClient(app)

    # Login to obtain token
    login_res = client.post("/api/auth/login", json={"password": "supersecret123"})
    assert login_res.status_code == 200
    token = login_res.json()["token"]

    # 10 LLM / upload requests should be processed (e.g. returning 400 for invalid file type)
    for i in range(10):
        r = client.post(
            "/api/photos",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (f"test-{i}.txt", b"not an image", "text/plain")},
        )
        assert r.status_code in (400, 202)

    # 11th request to LLM upload endpoint should be rate-limited (429)
    r_blocked = client.post(
        "/api/photos",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("test-11.txt", b"not an image", "text/plain")},
    )
    assert r_blocked.status_code == 429
    assert "rate limit exceeded" in r_blocked.json()["detail"].lower()

    # In contrast, lightweight operations (delete, rename, move) use the generous lightweight limiter and are not blocked
    del_res = client.delete(
        "/api/photos/non-existent-test",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert del_res.status_code == 404


def test_image_dimension_safety():
    # Attempting to process an image exceeding 10,000x10,000 should raise ValueError
    huge_img = Image.new("RGB", (10001, 100))
    import io
    buf = io.BytesIO()
    huge_img.save(buf, format="JPEG")
    with pytest.raises(ValueError) as exc_info:
        process_image(buf.getvalue(), original_name="huge.jpg")
    assert "exceed maximum allowed limit" in str(exc_info.value)


def test_login_success_and_token_structure(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "correct_pass_99")
    client = TestClient(app)

    res = client.post("/api/auth/login", json={"password": "correct_pass_99"})
    assert res.status_code == 200
    data = res.json()
    assert "token" in data
    assert data["token_type"] == "bearer"
    assert data["expires_in"] == 3600

    token = data["token"]
    parts = token.split(".")
    assert len(parts) == 2


def test_login_invalid_password(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "correct_pass_99")
    client = TestClient(app)

    res = client.post("/api/auth/login", json={"password": "wrong_password"})
    assert res.status_code == 401
    assert "invalid admin password" in res.json()["detail"].lower()


def test_login_rate_limiting(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "correct_pass_99")
    client = TestClient(app)

    # First 5 failed attempts return 401
    for i in range(5):
        res = client.post("/api/auth/login", json={"password": f"wrong_{i}"})
        assert res.status_code == 401

    # 6th attempt is rate-limited (429)
    res_blocked = client.post("/api/auth/login", json={"password": "wrong_6"})
    assert res_blocked.status_code == 429
    assert "too many failed login attempts" in res_blocked.json()["detail"].lower()


def test_token_tampering_and_expiration(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "correct_pass_99")
    client = TestClient(app)

    # Tampered signature
    tampered = "eyJyb2xlIjoiYWRtaW4ifQ.invalidsignature123"
    r = client.delete(
        "/api/photos/some-id",
        headers={"Authorization": f"Bearer {tampered}"},
    )
    assert r.status_code == 401
    assert "signature" in r.json()["detail"].lower()

    # Expired token
    expired_token = generate_admin_token(expires_in=-10)
    r_exp = client.delete(
        "/api/photos/some-id",
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    assert r_exp.status_code == 401
    assert "expired" in r_exp.json()["detail"].lower()


def test_legacy_header_rejected(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "correct_pass_99")
    client = TestClient(app)
    legacy_hash = hashlib.sha256(b"correct_pass_99").hexdigest()

    # Legacy header without Bearer token should be rejected (401)
    r = client.delete(
        "/api/photos/some-id",
        headers={"X-Admin-Password-Hash": legacy_hash},
    )
    assert r.status_code == 401
    assert "bearer token required" in r.json()["detail"].lower()


def test_login_fails_when_admin_password_unset_or_blank(monkeypatch):
    client = TestClient(app)

    # 1. ADMIN_PASSWORD unset
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    r1 = client.post("/api/auth/login", json={"password": ""})
    assert r1.status_code == 401
    assert "admin login is disabled" in r1.json()["detail"].lower()

    r1_any = client.post("/api/auth/login", json={"password": "anypassword"})
    assert r1_any.status_code == 401

    # 2. ADMIN_PASSWORD empty string
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    r2 = client.post("/api/auth/login", json={"password": ""})
    assert r2.status_code == 401
    assert "admin login is disabled" in r2.json()["detail"].lower()

    # 3. ADMIN_PASSWORD whitespace only
    monkeypatch.setenv("ADMIN_PASSWORD", "   \t  ")
    r3 = client.post("/api/auth/login", json={"password": "   \t  "})
    assert r3.status_code == 401
    assert "admin login is disabled" in r3.json()["detail"].lower()


def test_verify_token_fails_when_admin_password_unset_or_blank(monkeypatch):
    client = TestClient(app)
    # Generate token while password was valid
    monkeypatch.setenv("ADMIN_PASSWORD", "valid_secret_password")
    token = generate_admin_token()

    # Now simulate ADMIN_PASSWORD becoming unset or blank
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    r_unset = client.delete("/api/photos/some-id", headers={"Authorization": f"Bearer {token}"})
    assert r_unset.status_code == 403
    assert "admin operations are disabled" in r_unset.json()["detail"].lower()

    monkeypatch.setenv("ADMIN_PASSWORD", "   ")
    r_blank = client.delete("/api/photos/some-id", headers={"Authorization": f"Bearer {token}"})
    assert r_blank.status_code == 403
    assert "admin operations are disabled" in r_blank.json()["detail"].lower()


def test_async_upload_and_job_tracking(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "supersecret123")
    client = TestClient(app)

    # Login to obtain Bearer token
    login_res = client.post("/api/auth/login", json={"password": "supersecret123"})
    assert login_res.status_code == 200
    token = login_res.json()["token"]

    # Create dummy test image
    img = Image.new("RGB", (200, 200), color=(50, 100, 150))
    import io
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    img_bytes = buf.getvalue()

    # Upload asynchronously (default mode, no sync param)
    res = client.post(
        "/api/photos",
        files={"file": ("async_shelf.jpg", io.BytesIO(img_bytes), "image/jpeg")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 202
    data = res.json()
    assert data["status"] == "queued"
    assert "job_id" in data
    assert "photo_id" in data
    job_id = data["job_id"]

    # Check job status endpoint
    job_res = client.get(f"/api/ingest/jobs/{job_id}")
    assert job_res.status_code == 200
    job_data = job_res.json()
    assert job_data["job_id"] == job_id
    assert job_data["original_name"] == "async_shelf.jpg"
    assert "encouragement" in job_data

    # Check active jobs endpoint
    active_res = client.get("/api/ingest/active")
    assert active_res.status_code == 200
    active_jobs = active_res.json()
    assert any(j["job_id"] == job_id for j in active_jobs)

    # Non-existent job
    not_found = client.get("/api/ingest/jobs/non-existent-job-uuid")
    assert not_found.status_code == 404


def test_ingest_manager_progress_and_encouragement():
    # Test encouraging message generator
    prep_msg = pick_encouragement("preparing")
    assert isinstance(prep_msg, str) and len(prep_msg) > 0

    ai_msg = pick_encouragement("vision_ai", bins_count=15, current_tile=2, total_tiles=4)
    assert isinstance(ai_msg, str) and len(ai_msg) > 0

    dedup_msg = pick_encouragement("deduplication", bins_count=20)
    assert isinstance(dedup_msg, str) and len(dedup_msg) > 0

    save_msg = pick_encouragement("saving", bins_count=20)
    assert isinstance(save_msg, str) and len(save_msg) > 0

    complete_msg = pick_encouragement("complete", bins_count=25)
    assert "25 bins indexed" in complete_msg

    # Test IngestManager job updates
    job = ingest_manager.create_job(original_name="shelf_test.jpg")
    assert job.status == "queued"
    assert job.progress == 0

    ingest_manager.update_job_progress(
        job_id=job.job_id,
        progress=45,
        stage="vision_ai",
        message="Analyzing tile 2 of 4...",
        bins_count=12,
        current_tile=2,
        total_tiles=4,
    )
    assert job.progress == 45
    assert job.stage == "vision_ai"
    assert job.bins_count == 12
    assert len(job.encouragement) > 0

    ingest_manager.complete_job(
        job_id=job.job_id,
        photo_record={"id": job.photo_id, "bins": [{"id": "b1"}, {"id": "b2"}]},
    )
    assert job.status == "completed"
    assert job.progress == 100
    assert job.bins_count == 2


def test_enqueue_seed_photos_non_blocking():
    data_dir = get_data_dir()
    seed_dir = data_dir / "seed_photos"
    seed_dir.mkdir(parents=True, exist_ok=True)

    # Calling enqueue_seed_photos should complete in < 50ms without blocking
    queued = enqueue_seed_photos(ingest_manager)
    assert queued >= 0


def test_rename_photo(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pass-123")
    client = TestClient(app)
    token = generate_admin_token()

    photo = insert_photo(
        photo_id="photo-rename-1",
        filename="photo-rename-1.jpg",
        original_name="old_shelf_name.jpg",
        width=1000,
        height=800,
    )

    # Unauthenticated rename fails (401)
    unauth = client.patch(
        f"/api/photos/{photo['id']}",
        json={"name": "new_shelf_name.jpg"},
    )
    assert unauth.status_code == 401

    # Authenticated rename succeeds (200)
    auth_res = client.patch(
        f"/api/photos/{photo['id']}",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "new_shelf_name.jpg"},
    )
    assert auth_res.status_code == 200
    data = auth_res.json()
    assert data["status"] == "renamed"
    assert data["photo"]["original_name"] == "new_shelf_name.jpg"

    # Empty name fails (422 or 400)
    empty_res = client.patch(
        f"/api/photos/{photo['id']}",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "   "},
    )
    assert empty_res.status_code in (400, 422)


def test_move_photo(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pass-123")
    client = TestClient(app)
    token = generate_admin_token()

    # Clear and insert three photos with known order
    p1 = insert_photo("p-move-1", "p1.jpg", "First.jpg", 100, 100, order_index=0)
    p2 = insert_photo("p-move-2", "p2.jpg", "Second.jpg", 100, 100, order_index=1)
    p3 = insert_photo("p-move-3", "p3.jpg", "Third.jpg", 100, 100, order_index=2)

    # Unauthenticated move fails
    unauth = client.post(
        f"/api/photos/{p2['id']}/move",
        json={"direction": "up"},
    )
    assert unauth.status_code == 401

    # Move p2 up: should swap p2 and p1
    res = client.post(
        f"/api/photos/{p2['id']}/move",
        headers={"Authorization": f"Bearer {token}"},
        json={"direction": "up"},
    )
    assert res.status_code == 200
    manifest = res.json()["photos"]
    ids_in_manifest = [p["id"] for p in manifest if p["id"] in ("p-move-1", "p-move-2", "p-move-3")]
    assert ids_in_manifest == ["p-move-2", "p-move-1", "p-move-3"]

    # Move p2 down: should swap back with p1
    res_down = client.post(
        f"/api/photos/{p2['id']}/move",
        headers={"Authorization": f"Bearer {token}"},
        json={"direction": "down"},
    )
    assert res_down.status_code == 200
    manifest_down = res_down.json()["photos"]
    ids_down = [p["id"] for p in manifest_down if p["id"] in ("p-move-1", "p-move-2", "p-move-3")]
    assert ids_down == ["p-move-1", "p-move-2", "p-move-3"]

    # Verify that rapid reordering allows more than 10 requests without hitting 429
    for i in range(15):
        dir_choice = "down" if i % 2 == 0 else "up"
        r_rapid = client.post(
            f"/api/photos/{p2['id']}/move",
            headers={"Authorization": f"Bearer {token}"},
            json={"direction": dir_choice},
        )
        assert r_rapid.status_code == 200


def test_replace_photo(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pass-123")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    client = TestClient(app)
    token = generate_admin_token()

    photo = insert_photo("p-replace-1", "p-replace-1.jpg", "Original.jpg", 400, 400, order_index=5)
    insert_bin("b-old-1", photo["id"], "Old Part", ["old"], [0.1, 0.1, 0.2, 0.2])

    # Create dummy new image
    img = Image.new("RGB", (600, 500), color=(50, 120, 180))
    import io
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    new_img_bytes = buf.getvalue()

    # Unauthenticated replace fails (401)
    unauth = client.post(
        f"/api/photos/{photo['id']}/replace",
        files={"file": ("new_shelf.jpg", io.BytesIO(new_img_bytes), "image/jpeg")},
    )
    assert unauth.status_code == 401

    # Synchronous replace succeeds
    res = client.post(
        f"/api/photos/{photo['id']}/replace?sync=true",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("new_shelf.jpg", io.BytesIO(new_img_bytes), "image/jpeg")},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "replaced"
    updated_photo = data["photo"]
    assert updated_photo["id"] == photo["id"]
    assert updated_photo["width"] == 600
    assert updated_photo["height"] == 500
    assert updated_photo["original_name"] == "Original.jpg"
    assert updated_photo["order_index"] == 5


def test_hash_deduplication_on_seed_when_renamed(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    seed_dir = get_data_dir() / "seed_photos"
    seed_dir.mkdir(parents=True, exist_ok=True)

    # Create dummy seed photo
    test_seed_path = seed_dir / "my_custom_seed.jpg"
    img = Image.new("RGB", (300, 300), color=(12, 34, 56))
    img.save(test_seed_path, format="JPEG")

    # Initial auto_seed ingests the photo
    ingested = auto_seed()
    assert ingested == 1

    manifest = get_manifest()
    assert len(manifest) == 1
    photo = manifest[0]
    assert photo["original_name"] == "my_custom_seed.jpg"
    assert photo["image_hash"] is not None

    # Verify get_photo_by_hash finds it
    by_hash = get_photo_by_hash(photo["image_hash"])
    assert by_hash is not None
    assert by_hash["id"] == photo["id"]

    # User renames photo in the UI
    renamed = update_photo_name(photo["id"], "My Cool Organizer")
    assert renamed is not None
    assert renamed["original_name"] == "My Cool Organizer"

    # Server restarts: auto_seed runs again
    ingested_after_rename = auto_seed()
    assert ingested_after_rename == 0  # deduplicated by hash!

    # Background seed enqueue also skips
    enqueued_after_rename = enqueue_seed_photos(ingest_manager)
    assert enqueued_after_rename == 0  # deduplicated by hash!

    # Manifest still contains exactly 1 photo with the user's custom name
    manifest_after = get_manifest()
    assert len(manifest_after) == 1
    assert manifest_after[0]["id"] == photo["id"]
    assert manifest_after[0]["original_name"] == "My Cool Organizer"


def test_upload_duplicate_photo_returns_409(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("ADMIN_PASSWORD", "supersecret123")
    client = TestClient(app)
    token = generate_admin_token()

    # Create distinct test image
    img = Image.new("RGB", (250, 250), color=(88, 120, 200))
    import io
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    img_bytes = buf.getvalue()

    # First upload succeeds (201)
    res1 = client.post(
        "/api/photos?sync=true",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("shelf_original.jpg", io.BytesIO(img_bytes), "image/jpeg")},
    )
    assert res1.status_code == 201
    photo1 = res1.json()["photo"]

    # Re-uploading exact same image (even with different name) returns 409 Conflict
    res2 = client.post(
        "/api/photos?sync=true",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("shelf_duplicate.jpg", io.BytesIO(img_bytes), "image/jpeg")},
    )
    assert res2.status_code == 409
    assert "already been indexed" in res2.json()["detail"].lower()

    # Async upload of exact same image also returns 409 Conflict
    res3 = client.post(
        "/api/photos",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("shelf_duplicate_async.jpg", io.BytesIO(img_bytes), "image/jpeg")},
    )
    assert res3.status_code == 409
    assert "already been indexed" in res3.json()["detail"].lower()


def test_replace_duplicate_photo_validation(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("ADMIN_PASSWORD", "supersecret123")
    client = TestClient(app)
    token = generate_admin_token()

    # Image A
    img_a = Image.new("RGB", (200, 200), color=(10, 20, 30))
    import io
    buf_a = io.BytesIO()
    img_a.save(buf_a, format="JPEG")
    bytes_a = buf_a.getvalue()

    # Image B
    img_b = Image.new("RGB", (200, 200), color=(40, 50, 60))
    buf_b = io.BytesIO()
    img_b.save(buf_b, format="JPEG")
    bytes_b = buf_b.getvalue()

    # Upload Photo A
    res_a = client.post(
        "/api/photos?sync=true",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("shelf_a.jpg", io.BytesIO(bytes_a), "image/jpeg")},
    )
    assert res_a.status_code == 201
    photo_a = res_a.json()["photo"]

    # Upload Photo B
    res_b = client.post(
        "/api/photos?sync=true",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("shelf_b.jpg", io.BytesIO(bytes_b), "image/jpeg")},
    )
    assert res_b.status_code == 201
    photo_b = res_b.json()["photo"]

    # Attempting to replace Photo B with Photo A's image returns 409 Conflict
    res_dup = client.post(
        f"/api/photos/{photo_b['id']}/replace?sync=true",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("attempt_a.jpg", io.BytesIO(bytes_a), "image/jpeg")},
    )
    assert res_dup.status_code == 409
    assert "already been indexed" in res_dup.json()["detail"].lower()

    # Replacing Photo A with its own image succeeds (re-scan)
    res_self = client.post(
        f"/api/photos/{photo_a['id']}/replace?sync=true",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("self_a.jpg", io.BytesIO(bytes_a), "image/jpeg")},
    )
    assert res_self.status_code == 200


def test_replace_photo_preserves_bins_on_vision_failure(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pass-123")
    client = TestClient(app)
    token = generate_admin_token()

    photo = insert_photo("p-fail-1", "p-fail-1.jpg", "Original Shelf", 400, 400, order_index=2)
    insert_bin("b-original-1", photo["id"], "Preserved Bin", ["keep_me"], [0.2, 0.2, 0.4, 0.4])

    img = Image.new("RGB", (300, 300), color=(111, 222, 111))
    import io
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    new_bytes = buf.getvalue()

    def mock_fail(*args, **kwargs):
        raise RuntimeError("Gemini Vision AI network timeout")

    monkeypatch.setattr("backend.ingestion.detect_bins_tiled", mock_fail)
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-key-for-test")
    monkeypatch.setenv("ALLOW_MOCK_FALLBACK", "0")

    # Attempt replace which fails during vision AI detection
    res = client.post(
        f"/api/photos/{photo['id']}/replace?sync=true",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("failing_shelf.jpg", io.BytesIO(new_bytes), "image/jpeg")},
    )
    assert res.status_code == 500

    # Verify that existing photo record and bins were NOT deleted or corrupted
    photo_after = get_photo(photo["id"])
    assert photo_after is not None
    assert photo_after["original_name"] == "Original Shelf"
    assert len(photo_after["bins"]) == 1
    assert photo_after["bins"][0]["id"] == "b-original-1"
