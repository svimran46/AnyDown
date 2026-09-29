"""Thread-safe in-memory job tracking for a single AnyDown instance."""

from __future__ import annotations

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
    progress: float | None = None
    downloaded_bytes: int = 0
    total_bytes: int = 0
    speed: float | None = None
    eta: int | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    updated_at: float = field(default_factory=time.time)


class JobManager:
    def __init__(self, file_ttl_seconds: int = 1800):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self.file_ttl_seconds = file_ttl_seconds

    def create_job(self, url: str) -> Job:
        job = Job(id=str(uuid.uuid4()), url=url)
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def get_payload(self, job_id: str) -> dict | None:
        """Thread-safe snapshot of job state for API responses."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            status_val = job.status.value if isinstance(job.status, JobStatus) else str(job.status)
            return {
                "job_id": job.id,
                "status": status_val,
                "error": job.error,
                "filename": job.filename,
                "progress": job.progress,
                "downloaded_bytes": job.downloaded_bytes,
                "total_bytes": job.total_bytes,
                "speed": job.speed,
                "eta": job.eta,
            }

    def update(self, job_id: str, **kwargs) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            for key, value in kwargs.items():
                if hasattr(job, key):
                    setattr(job, key, value)
            # Stamp the terminal transition so TTL cleanup has a start time
            # for both COMPLETED and FAILED jobs.
            if "status" in kwargs and kwargs["status"] in (JobStatus.COMPLETED, JobStatus.FAILED):
                if job.finished_at is None:
                    job.finished_at = time.time()
            job.updated_at = time.time()

    def cleanup_expired(self, output_dir: str | None = None) -> None:
        """Drop expired terminal jobs and delete their files.

        A job that is still QUEUED or DOWNLOADING is never expired here, no
        matter how long it has been running. Evicting an active job deletes the
        file out from under the download, and Job.update() then silently no-ops
        on the missing job, so the download is never marked COMPLETED, its file
        is never registered for deletion, and /api/status starts returning 404
        for a download that is still running (a permanent disk leak).
        """
        now = time.time()
        with self._lock:
            expired_ids = []
            for jid, job in self._jobs.items():
                if job.status not in (JobStatus.COMPLETED, JobStatus.FAILED):
                    # Still in flight: keep it, but refresh updated_at so the
                    # job stays visible to the caller.
                    job.updated_at = now
                    continue
                stamp = job.finished_at if job.finished_at is not None else job.updated_at
                if now - stamp > self.file_ttl_seconds:
                    expired_ids.append(jid)
            expired_jobs = [self._jobs.pop(jid) for jid in expired_ids]

        for job in expired_jobs:
            if job.filepath:
                try:
                    if os.path.exists(job.filepath):
                        os.remove(job.filepath)
                except OSError:
                    pass

        # Sweep all residual files from expired jobs as well as orphaned temp files
        if output_dir and os.path.isdir(output_dir):
            cutoff = now - self.file_ttl_seconds
            try:
                listings = os.listdir(output_dir)
            except OSError:
                return

            # One listdir for all expired jobs, not one per job (was O(n^2)).
            for job in expired_jobs:
                prefix = job.id + "."
                for fname in listings:
                    if fname.startswith(prefix):
                        try:
                            os.remove(os.path.join(output_dir, fname))
                        except OSError:
                            # Per-file guard: one failure must not skip the rest.
                            continue

            for fname in listings:
                if not fname.endswith((".part", ".ytdl", ".temp", ".tmp", ".aria2")):
                    continue
                path = os.path.join(output_dir, fname)
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                except OSError:
                    continue


job_manager = JobManager()
