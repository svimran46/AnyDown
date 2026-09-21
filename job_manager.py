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
        """Drop expired jobs (completed or failed) and delete their files."""
        now = time.time()
        with self._lock:
            expired_ids = [
                jid
                for jid, job in self._jobs.items()
                if job.finished_at is not None
                and now - job.finished_at > self.file_ttl_seconds
            ]
            expired_jobs = [self._jobs.pop(jid) for jid in expired_ids]

        for job in expired_jobs:
            if job.filepath:
                try:
                    if os.path.exists(job.filepath):
                        os.remove(job.filepath)
                except OSError:
                    pass

        # Sweep orphaned temp files from crashed or expired jobs. A leftover
        # file is removed once it is older than the TTL and its job is gone.
        if output_dir and os.path.isdir(output_dir):
            cutoff = now - self.file_ttl_seconds
            for fname in os.listdir(output_dir):
                if not (fname.endswith(".part") or fname.endswith(".ytdl")):
                    continue
                path = os.path.join(output_dir, fname)
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                except OSError:
                    continue


job_manager = JobManager()
