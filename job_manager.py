"""Thread-safe in-memory job tracking for AnyDown."""

import os
import time
import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum


class JobStatus(str, Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Job:
    id: str
    url: str
    status: JobStatus = JobStatus.QUEUED
    filepath: str | None = None
    filename: str | None = None
    error: str | None = None
    progress: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    speed: float | None = None
    eta: int | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class JobManager:
    def __init__(self, file_ttl_seconds: int = 1800):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self.file_ttl_seconds = file_ttl_seconds

    def create_job(self, url: str) -> Job:
        job = Job(id=str(uuid.uuid4()), url=url)
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, **kwargs) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            for key, value in kwargs.items():
                if hasattr(job, key):
                    setattr(job, key, value)
            job.updated_at = time.time()

    def update_progress(self, job_id: str, data: dict) -> None:
        status = data.get("status")
        downloaded = data.get("downloaded_bytes") or 0
        total = data.get("total_bytes") or data.get("total_bytes_estimate")
        speed = data.get("speed")
        eta = data.get("eta")
        percent = 0.0
        if total and total > 0:
            percent = max(0.0, min(100.0, downloaded * 100.0 / total))
        elif data.get("_percent_str"):
            try:
                percent = float(str(data["_percent_str"]).replace("%", "").strip())
            except ValueError:
                pass

        updates = {
            "progress": percent,
            "downloaded_bytes": int(downloaded),
            "total_bytes": int(total) if total else None,
            "speed": float(speed) if speed else None,
            "eta": int(eta) if eta is not None else None,
        }
        if status == "downloading":
            updates["status"] = JobStatus.DOWNLOADING

        self.update(job_id, **updates)



    def cleanup_expired(self) -> None:
        now = time.time()
        with self._lock:
            expired_ids = [
                jid for jid, job in self._jobs.items()
                if now - job.created_at > self.file_ttl_seconds
                and job.status in (JobStatus.COMPLETED, JobStatus.FAILED)
            ]
            expired_jobs = [self._jobs.pop(jid) for jid in expired_ids]

        for job in expired_jobs:
            if job.filepath and os.path.exists(job.filepath):
                try:
                    os.remove(job.filepath)
                except OSError:
                    pass


job_manager = JobManager()
