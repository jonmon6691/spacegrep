import asyncio
import hashlib
import logging
import random
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from backend.database import get_photo_by_hash

logger = logging.getLogger(__name__)

ENCOURAGING_PREPARING = [
    "Optimizing shelf photo for high-speed browsing...",
    "Inspecting image resolution and generating optimized thumbnails...",
    "Looking sharp! Prepping your shelf photo for AI detection...",
]

ENCOURAGING_VISION_AI = [
    "Scanning workshop shelves row-by-row... Gemini is reading bin labels!",
    "Extracting part numbers, IC codes, and component labels...",
    "Recognizing organizer bins and generating smart search synonyms...",
    "Analyzing organizer drawers — looking fantastic!",
    "Reading component labels with precision vision AI...",
]

ENCOURAGING_DEDUP = [
    "Aligning overlapping bins and deduplicating labels across tiles...",
    "Fusing spatial detections into a unified shelf map...",
    "Polishing bin boundaries and combining semantic tags...",
]

ENCOURAGING_SAVING = [
    "Saving detected bins to the workshop database...",
    "Building real-time search index for instant queries...",
    "Almost done! Committing workshop organizer records...",
]


def pick_encouragement(stage: str, bins_count: int = 0, current_tile: int = 0, total_tiles: int = 0) -> str:
    if stage == "preparing":
        return random.choice(ENCOURAGING_PREPARING)
    elif stage == "vision_ai":
        if bins_count > 0 and random.random() < 0.6:
            return f"Gemini spotted {bins_count} bins so far — components and tools incoming!"
        if total_tiles > 1 and current_tile > 0:
            return f"Scanning tile {current_tile} of {total_tiles} with Vision AI..."
        return random.choice(ENCOURAGING_VISION_AI)
    elif stage == "deduplication":
        if bins_count > 0:
            return f"Organizing {bins_count} unique workshop bins into your inventory..."
        return random.choice(ENCOURAGING_DEDUP)
    elif stage == "saving":
        if bins_count > 0:
            return f"Indexing {bins_count} bins into the instant search engine..."
        return random.choice(ENCOURAGING_SAVING)
    elif stage == "complete":
        return f"Success! {bins_count} bins indexed and ready to search."
    return "Processing shelf photo..."


@dataclass
class IngestJob:
    job_id: str
    photo_id: str
    original_name: str
    status: str = "queued"  # "queued", "processing", "completed", "failed"
    progress: int = 0  # 0 - 100
    stage: str = "queued"  # "queued", "preparing", "vision_ai", "deduplication", "saving", "complete", "failed"
    message: str = "Job queued..."
    encouragement: str = "Waiting in queue to start processing..."
    bins_count: int = 0
    total_tiles: int = 1
    completed_tiles: int = 0
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    photo_record: Optional[Dict[str, Any]] = None
    is_seed: bool = False
    is_replace: bool = False

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d


class IngestManager:
    def __init__(self):
        self.jobs: Dict[str, IngestJob] = {}
        self._queue: asyncio.Queue[tuple[str, Optional[bytes], Optional[Path]]] = asyncio.Queue()
        self._global_subscribers: Set[asyncio.Queue[dict]] = set()
        self._job_subscribers: Dict[str, Set[asyncio.Queue[dict]]] = {}
        self._worker_task: Optional[asyncio.Task] = None
        self._is_running = False

    def get_job(self, job_id: str) -> Optional[IngestJob]:
        return self.jobs.get(job_id)

    def get_active_jobs(self) -> List[IngestJob]:
        return [j for j in self.jobs.values() if j.status in ("queued", "processing")]

    def create_job(
        self,
        original_name: str,
        file_bytes: Optional[bytes] = None,
        file_path: Optional[Path] = None,
        photo_id: Optional[str] = None,
        is_seed: bool = False,
        is_replace: bool = False,
    ) -> IngestJob:
        job_id = str(uuid.uuid4())
        assigned_photo_id = photo_id or str(uuid.uuid4())
        job = IngestJob(
            job_id=job_id,
            photo_id=assigned_photo_id,
            original_name=original_name,
            status="queued",
            stage="queued",
            message=f"Queued {original_name} for ingestion",
            encouragement=pick_encouragement("preparing"),
            is_seed=is_seed,
            is_replace=is_replace,
        )
        self.jobs[job_id] = job
        self._queue.put_nowait((job_id, file_bytes, file_path))
        logger.info(
            "Created ingest job %s for '%s' (photo_id=%s, seed=%s, replace=%s)",
            job_id,
            original_name,
            assigned_photo_id,
            is_seed,
            is_replace,
        )
        self._notify_subscribers(job)
        return job

    def update_job_progress(
        self,
        job_id: str,
        progress: int,
        stage: str,
        message: str,
        encouragement: Optional[str] = None,
        bins_count: Optional[int] = None,
        current_tile: int = 0,
        total_tiles: int = 0,
    ) -> None:
        job = self.jobs.get(job_id)
        if not job:
            return

        job.progress = max(0, min(100, progress))
        job.stage = stage
        job.message = message
        if bins_count is not None:
            job.bins_count = bins_count
        if total_tiles > 0:
            job.total_tiles = total_tiles
        if current_tile > 0:
            job.completed_tiles = current_tile

        job.encouragement = encouragement or pick_encouragement(
            stage=stage,
            bins_count=job.bins_count,
            current_tile=current_tile,
            total_tiles=job.total_tiles,
        )
        job.updated_at = time.time()
        self._notify_subscribers(job)

    def complete_job(self, job_id: str, photo_record: Dict[str, Any]) -> None:
        job = self.jobs.get(job_id)
        if not job:
            return

        bin_count = len(photo_record.get("bins", []))
        job.status = "completed"
        job.stage = "complete"
        job.progress = 100
        job.bins_count = bin_count
        job.message = f"Successfully ingested {job.original_name} with {bin_count} bins."
        job.encouragement = pick_encouragement("complete", bins_count=bin_count)
        job.photo_record = photo_record
        job.updated_at = time.time()
        logger.info("Ingest job %s completed successfully (%d bins)", job_id, bin_count)
        self._notify_subscribers(job)

    def fail_job(self, job_id: str, error_message: str) -> None:
        job = self.jobs.get(job_id)
        if not job:
            return

        job.status = "failed"
        job.stage = "failed"
        job.error = error_message
        job.message = f"Ingestion failed: {error_message}"
        job.encouragement = "Ingestion encountered an error. You can retry or check logs."
        job.updated_at = time.time()
        logger.error("Ingest job %s failed: %s", job_id, error_message)
        self._notify_subscribers(job)

    def _notify_subscribers(self, job: IngestJob) -> None:
        data = job.to_dict()

        # Notify global subscribers
        dead_global = set()
        for q in list(self._global_subscribers):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                dead_global.add(q)
            except Exception:
                dead_global.add(q)
        self._global_subscribers.difference_update(dead_global)

        # Notify job-specific subscribers
        job_subs = self._job_subscribers.get(job.job_id)
        if job_subs:
            dead_job = set()
            for q in list(job_subs):
                try:
                    q.put_nowait(data)
                except asyncio.QueueFull:
                    dead_job.add(q)
                except Exception:
                    dead_job.add(q)
            job_subs.difference_update(dead_job)

    def subscribe_global(self) -> asyncio.Queue[dict]:
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=100)
        self._global_subscribers.add(q)
        return q

    def unsubscribe_global(self, q: asyncio.Queue[dict]) -> None:
        self._global_subscribers.discard(q)

    def subscribe_job(self, job_id: str) -> asyncio.Queue[dict]:
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=100)
        if job_id not in self._job_subscribers:
            self._job_subscribers[job_id] = set()
        self._job_subscribers[job_id].add(q)
        return q

    def unsubscribe_job(self, job_id: str, q: asyncio.Queue[dict]) -> None:
        if job_id in self._job_subscribers:
            self._job_subscribers[job_id].discard(q)
            if not self._job_subscribers[job_id]:
                del self._job_subscribers[job_id]

    async def start(self) -> None:
        if self._is_running:
            return
        self._is_running = True
        self._worker_task = asyncio.create_task(self._worker_loop())
        logger.info("IngestManager worker started.")

    async def stop(self) -> None:
        self._is_running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
        logger.info("IngestManager worker stopped.")

    async def _worker_loop(self) -> None:
        while self._is_running:
            try:
                job_id, file_bytes, file_path = await self._queue.get()
            except asyncio.CancelledError:
                break

            job = self.jobs.get(job_id)
            if not job:
                self._queue.task_done()
                continue

            try:
                job.status = "processing"
                job.stage = "preparing"
                self.update_job_progress(
                    job_id=job_id,
                    progress=5,
                    stage="preparing",
                    message=f"Starting ingestion for {job.original_name}...",
                )

                # Read bytes if given path
                if file_bytes is None and file_path is not None:
                    def read_file():
                        with open(file_path, "rb") as f:
                            return f.read()
                    file_bytes = await asyncio.to_thread(read_file)

                if not file_bytes:
                    raise ValueError(f"No image bytes available for job {job_id}")

                file_hash = hashlib.sha256(file_bytes).hexdigest()

                if job.is_seed:
                    existing = get_photo_by_hash(file_hash)
                    if existing:
                        logger.info(
                            "Seed job %s for '%s' already indexed as photo id %s ('%s'). Skipping vision processing.",
                            job_id,
                            job.original_name,
                            existing["id"],
                            existing["original_name"],
                        )
                        self.complete_job(job_id, existing)
                        continue

                # Progress callback adapter bridging sync ingestion to async updates
                def progress_cb(
                    progress: int,
                    stage: str,
                    message: str,
                    bins_count: int = 0,
                    current_tile: int = 0,
                    total_tiles: int = 0,
                ):
                    self.update_job_progress(
                        job_id=job_id,
                        progress=progress,
                        stage=stage,
                        message=message,
                        bins_count=bins_count,
                        current_tile=current_tile,
                        total_tiles=total_tiles,
                    )

                # Offload heavy synchronous image processing to thread pool
                from backend.ingestion import process_image

                record = await asyncio.to_thread(
                    process_image,
                    file_bytes=file_bytes,
                    original_name=job.original_name,
                    photo_id=job.photo_id,
                    progress_callback=progress_cb,
                    is_replace=job.is_replace,
                    image_hash=file_hash,
                )

                self.complete_job(job_id, record)

            except Exception as e:
                logger.exception("Error during job %s execution: %s", job_id, e)
                self.fail_job(job_id, str(e))
            finally:
                self._queue.task_done()


# Global singleton instance
ingest_manager = IngestManager()
