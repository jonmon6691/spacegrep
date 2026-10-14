import hashlib
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def get_data_dir() -> Path:
    data_dir = os.getenv("DATA_DIR", "./data")
    path = Path(data_dir)
    try:
        path.mkdir(parents=True, exist_ok=True)
        (path / "images").mkdir(parents=True, exist_ok=True)
        (path / "seed_photos").mkdir(parents=True, exist_ok=True)
    except PermissionError as e:
        logger.error(
            "Permission denied creating data directories in %s (uid=%s): %s",
            path,
            os.getuid() if hasattr(os, "getuid") else "unknown",
            e,
        )
        raise
    return path


def get_db_path() -> Path:
    return get_data_dir() / "inventory.db"


def get_db_connection() -> sqlite3.Connection:
    db_path = get_db_path()
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn
    except sqlite3.OperationalError as e:
        logger.error(
            "Failed to open or initialize SQLite database at %s (uid=%s, gid=%s): %s. "
            "Ensure the directory and database file are writable by the current process user.",
            db_path,
            os.getuid() if hasattr(os, "getuid") else "unknown",
            os.getgid() if hasattr(os, "getgid") else "unknown",
            e,
        )
        raise


def init_db():
    conn = get_db_connection()
    try:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS photos (
                    id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    original_name TEXT NOT NULL,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    order_index INTEGER DEFAULT 0,
                    image_hash TEXT
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bins (
                    id TEXT PRIMARY KEY,
                    photo_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    semantic_tags TEXT NOT NULL,
                    bbox_x REAL NOT NULL,
                    bbox_y REAL NOT NULL,
                    bbox_w REAL NOT NULL,
                    bbox_h REAL NOT NULL,
                    FOREIGN KEY(photo_id) REFERENCES photos(id) ON DELETE CASCADE
                );
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_bins_photo_id ON bins(photo_id);
                """
            )
            # Ensure order_index column exists on photos table for custom ordering
            cursor = conn.execute("PRAGMA table_info(photos);")
            columns = [row["name"] for row in cursor.fetchall()]
            if "order_index" not in columns:
                conn.execute("ALTER TABLE photos ADD COLUMN order_index INTEGER DEFAULT 0;")
                existing_photos = conn.execute(
                    "SELECT id FROM photos ORDER BY created_at ASC"
                ).fetchall()
                for idx, row in enumerate(existing_photos):
                    conn.execute(
                        "UPDATE photos SET order_index = ? WHERE id = ?",
                        (idx, row["id"]),
                    )

            # Ensure image_hash column and index exist on photos table for deduplication
            if "image_hash" not in columns:
                conn.execute("ALTER TABLE photos ADD COLUMN image_hash TEXT;")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_photos_image_hash ON photos(image_hash);"
            )

            # Backfill image_hash for any existing records where it is NULL
            _backfill_missing_image_hashes(conn)
    finally:
        conn.close()


def _backfill_missing_image_hashes(conn: sqlite3.Connection) -> None:
    """
    Backfills image_hash for any existing photo rows where image_hash is NULL.
    Matches against seed photos or existing orig_ files.
    """
    missing = conn.execute(
        "SELECT id, filename, original_name FROM photos WHERE image_hash IS NULL"
    ).fetchall()
    if not missing:
        return

    data_dir = get_data_dir()
    seed_dir = data_dir / "seed_photos"
    img_dir = data_dir / "images"

    seed_raw_hashes: Dict[str, str] = {}
    seed_reencoded_hashes: Dict[str, str] = {}

    if seed_dir.exists():
        import io
        from PIL import Image, ImageOps

        for sf in seed_dir.iterdir():
            if sf.is_file() and sf.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                try:
                    raw_bytes = sf.read_bytes()
                    raw_h = hashlib.sha256(raw_bytes).hexdigest()
                    seed_raw_hashes[sf.name] = raw_h

                    # Try computing Pillow re-encoded JPEG quality 95 hash
                    try:
                        im = Image.open(io.BytesIO(raw_bytes))
                        im = ImageOps.exif_transpose(im)
                        if im.mode != "RGB":
                            im = im.convert("RGB")
                        buf = io.BytesIO()
                        im.save(buf, "JPEG", quality=95, optimize=True)
                        re_h = hashlib.sha256(buf.getvalue()).hexdigest()
                        seed_reencoded_hashes[re_h] = raw_h
                    except Exception:
                        pass
                except Exception as e:
                    logger.warning("Error reading seed file %s for hash backfill: %s", sf, e)

    for row in missing:
        resolved_hash: Optional[str] = None
        orig_name = row["original_name"]

        # 1. Direct match by original_name in seed photos
        if orig_name in seed_raw_hashes:
            resolved_hash = seed_raw_hashes[orig_name]

        # 2. Match by orig_ image file against re-encoded seed photos
        if not resolved_hash:
            orig_path = img_dir / f"orig_{row['filename']}"
            if orig_path.exists():
                try:
                    orig_h = hashlib.sha256(orig_path.read_bytes()).hexdigest()
                    resolved_hash = seed_reencoded_hashes.get(orig_h, orig_h)
                except Exception:
                    pass

        # 3. Fallback to main image file
        if not resolved_hash:
            main_path = img_dir / row["filename"]
            if main_path.exists():
                try:
                    resolved_hash = hashlib.sha256(main_path.read_bytes()).hexdigest()
                except Exception:
                    pass

        if resolved_hash:
            conn.execute(
                "UPDATE photos SET image_hash = ? WHERE id = ?",
                (resolved_hash, row["id"]),
            )
            logger.info(
                "Backfilled image_hash for photo %s ('%s'): %s",
                row["id"],
                row["original_name"],
                resolved_hash,
            )


def insert_photo(
    photo_id: str,
    filename: str,
    original_name: str,
    width: int,
    height: int,
    order_index: Optional[int] = None,
    image_hash: Optional[str] = None,
) -> Dict[str, Any]:
    conn = get_db_connection()
    try:
        with conn:
            if order_index is None:
                max_row = conn.execute(
                    "SELECT COALESCE(MAX(order_index), -1) as max_idx FROM photos"
                ).fetchone()
                order_index = (max_row["max_idx"] + 1) if max_row else 0

            conn.execute(
                """
                INSERT INTO photos (id, filename, original_name, width, height, order_index, image_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (photo_id, filename, original_name, width, height, order_index, image_hash),
            )
        return _fetch_photo_with_bins(conn, photo_id)
    finally:
        conn.close()


def insert_bin(
    bin_id: str,
    photo_id: str,
    label: str,
    semantic_tags: List[str],
    bbox: List[float],
):
    conn = get_db_connection()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO bins (
                    id, photo_id, label, semantic_tags,
                    bbox_x, bbox_y, bbox_w, bbox_h
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bin_id,
                    photo_id,
                    label,
                    json.dumps(semantic_tags),
                    bbox[0],
                    bbox[1],
                    bbox[2],
                    bbox[3],
                ),
            )
    finally:
        conn.close()


def _fetch_photo_with_bins(conn: sqlite3.Connection, photo_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT id, filename, original_name, width, height, created_at, order_index, image_hash FROM photos WHERE id = ?",
        (photo_id,),
    ).fetchone()
    if not row:
        return None
    photo_dict = dict(row)
    bin_rows = conn.execute(
        """
        SELECT id, photo_id, label, semantic_tags, bbox_x, bbox_y, bbox_w, bbox_h
        FROM bins WHERE photo_id = ?
        """,
        (photo_id,),
    ).fetchall()
    photo_dict["bins"] = [
        {
            "id": b["id"],
            "photo_id": b["photo_id"],
            "label": b["label"],
            "semantic_tags": json.loads(b["semantic_tags"]),
            "bbox": [b["bbox_x"], b["bbox_y"], b["bbox_w"], b["bbox_h"]],
        }
        for b in bin_rows
    ]
    return photo_dict


def get_photo(photo_id: str) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    try:
        return _fetch_photo_with_bins(conn, photo_id)
    finally:
        conn.close()


def get_photo_by_original_name(original_name: str) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT id FROM photos WHERE original_name = ?",
            (original_name,),
        ).fetchone()
        if not row:
            return None
        return _fetch_photo_with_bins(conn, row["id"])
    finally:
        conn.close()


def get_photo_by_hash(image_hash: str) -> Optional[Dict[str, Any]]:
    """Retrieves a photo record matching the given image SHA-256 hash."""
    if not image_hash:
        return None
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT id FROM photos WHERE image_hash = ?",
            (image_hash,),
        ).fetchone()
        if not row:
            return None
        return _fetch_photo_with_bins(conn, row["id"])
    finally:
        conn.close()


def get_manifest() -> List[Dict[str, Any]]:
    conn = get_db_connection()
    try:
        photos = conn.execute(
            "SELECT id, filename, original_name, width, height, created_at, order_index, image_hash FROM photos ORDER BY order_index ASC, created_at ASC"
        ).fetchall()
        results = []
        for p in photos:
            photo_dict = dict(p)
            bin_rows = conn.execute(
                """
                SELECT id, photo_id, label, semantic_tags, bbox_x, bbox_y, bbox_w, bbox_h
                FROM bins WHERE photo_id = ?
                """,
                (p["id"],),
            ).fetchall()
            photo_dict["bins"] = [
                {
                    "id": b["id"],
                    "photo_id": b["photo_id"],
                    "label": b["label"],
                    "semantic_tags": json.loads(b["semantic_tags"]),
                    "bbox": [b["bbox_x"], b["bbox_y"], b["bbox_w"], b["bbox_h"]],
                }
                for b in bin_rows
            ]
            results.append(photo_dict)
        return results
    finally:
        conn.close()


def update_photo_name(photo_id: str, new_name: str) -> Optional[Dict[str, Any]]:
    """Updates the original_name of a photo."""
    conn = get_db_connection()
    try:
        with conn:
            cursor = conn.execute(
                "UPDATE photos SET original_name = ? WHERE id = ?",
                (new_name, photo_id),
            )
            if cursor.rowcount == 0:
                return None
        return get_photo(photo_id)
    finally:
        conn.close()


def move_photo(photo_id: str, direction: str) -> bool:
    """
    Moves a photo up or down in the manifest ordering.
    direction must be 'up' or 'down'.
    """
    if direction not in ("up", "down"):
        raise ValueError("Direction must be 'up' or 'down'")

    conn = get_db_connection()
    try:
        with conn:
            photos = conn.execute(
                "SELECT id, order_index FROM photos ORDER BY order_index ASC, created_at ASC"
            ).fetchall()
            photo_list = [dict(p) for p in photos]
            idx = next((i for i, p in enumerate(photo_list) if p["id"] == photo_id), -1)
            if idx == -1:
                return False

            if direction == "up" and idx > 0:
                other_idx = idx - 1
            elif direction == "down" and idx < len(photo_list) - 1:
                other_idx = idx + 1
            else:
                return True  # Already at extreme boundary

            # Normalize order_indices to 0..N-1
            for i, p in enumerate(photo_list):
                p["order_index"] = i

            # Swap order_index between idx and other_idx
            photo_list[idx]["order_index"], photo_list[other_idx]["order_index"] = (
                photo_list[other_idx]["order_index"],
                photo_list[idx]["order_index"],
            )

            # Persist updated order indices
            conn.executemany(
                "UPDATE photos SET order_index = ? WHERE id = ?",
                [(p["order_index"], p["id"]) for p in photo_list],
            )
            return True
    finally:
        conn.close()


def update_photo_image(
    photo_id: str,
    filename: str,
    original_name: str,
    width: int,
    height: int,
    image_hash: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Replaces a photo's image file and dimensions, clearing existing bins for re-indexing."""
    conn = get_db_connection()
    try:
        with conn:
            # Delete old bins
            conn.execute("DELETE FROM bins WHERE photo_id = ?", (photo_id,))
            # Update photo record
            cursor = conn.execute(
                """
                UPDATE photos
                SET filename = ?, original_name = ?, width = ?, height = ?, image_hash = COALESCE(?, image_hash)
                WHERE id = ?
                """,
                (filename, original_name, width, height, image_hash, photo_id),
            )
            if cursor.rowcount == 0:
                return None
        return get_photo(photo_id)
    finally:
        conn.close()


def delete_photo(photo_id: str) -> bool:
    conn = get_db_connection()
    try:
        with conn:
            cursor = conn.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
            return cursor.rowcount > 0
    finally:
        conn.close()


def search_inventory_summary(query: str) -> Dict[str, Any]:
    """
    Performs a fast substring scan across bin labels and semantic tags for Slack query previews.
    Returns match count, affected shelf names, and sample matching labels.
    """
    clean_q = query.strip().lower()
    if not clean_q:
        return {"total_matches": 0, "shelves": [], "sample_labels": []}

    conn = get_db_connection()
    try:
        # Match label or semantic_tags containing the query
        rows = conn.execute(
            """
            SELECT b.label, b.semantic_tags, p.original_name, p.id as photo_id
            FROM bins b
            JOIN photos p ON b.photo_id = p.id
            WHERE LOWER(b.label) LIKE ? OR LOWER(b.semantic_tags) LIKE ?
            ORDER BY p.order_index ASC, b.label ASC
            """,
            (f"%{clean_q}%", f"%{clean_q}%"),
        ).fetchall()

        matching_shelves: List[str] = []
        sample_labels: List[str] = []
        seen_shelves = set()
        seen_labels = set()

        for r in rows:
            shelf_name = r["original_name"]
            if shelf_name not in seen_shelves:
                seen_shelves.add(shelf_name)
                matching_shelves.append(shelf_name)

            label = r["label"]
            if label not in seen_labels and len(sample_labels) < 5:
                seen_labels.add(label)
                sample_labels.append(label)

        return {
            "total_matches": len(rows),
            "shelves": matching_shelves,
            "sample_labels": sample_labels,
        }
    finally:
        conn.close()
