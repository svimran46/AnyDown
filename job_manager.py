"""In-memory job tracking for download tasks.

This keeps things simple for local development or small deployments.
For production (multiple workers / multiple servers), replace this
with Redis, and consider moving the actual download work from
BackgroundTasks to a proper task queue like Celery or RQ.
"""

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
    created_at: float = field(default_factory=time.time)


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
                setattr(job, key, value)

    def cleanup_expired(self) -> None:
        """Remove job records (and their files) older than file_ttl_seconds."""
        now = time.time()
        with self._lock:
            expired_ids = [
                jid for jid, job in self._jobs.items()
                if now - job.created_at > self.file_ttl_seconds
            ]
            expired_jobs = [self._jobs.pop(jid) for jid in expired_ids]

        for job in expired_jobs:
            if job.filepath and os.path.exists(job.filepath):
                try:
                    os.remove(job.filepath)
                except OSError:
                    pass


job_manager = JobManager()
