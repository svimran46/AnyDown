"""FastAPI backend for AnyDown, a yt-dlp based multi-platform downloader."""

import asyncio
import ipaddress
import os
import socket
import time
import re
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from downloader import fetch_info, download_media, UnsupportedURLError
from job_manager import job_manager, JobStatus

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "downloads")
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
CLEANUP_INTERVAL_SECONDS = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "300"))
FILE_TTL_SECONDS = int(os.getenv("FILE_TTL_SECONDS", "1800"))
THROTTLE_SECONDS = float(os.getenv("THROTTLE_SECONDS", "5"))
MAX_CONCURRENT_DOWNLOADS = max(1, int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2")))
MAX_URL_LENGTH = 4096
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

os.makedirs(OUTPUT_DIR, exist_ok=True)
job_manager.file_ttl_seconds = FILE_TTL_SECONDS


async def _cleanup_loop():
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            await asyncio.to_thread(job_manager.cleanup_expired)
        except asyncio.CancelledError:
            raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_cleanup_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="AnyDown API", version="2.0.0", lifespan=lifespan)

allowed_origins = [x.strip() for x in os.getenv("ALLOWED_ORIGINS", "").split(",") if x.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins or ["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


class InfoRequest(BaseModel):
    url: str = Field(min_length=1, max_length=MAX_URL_LENGTH)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _validate_public_url(value)


class DownloadRequest(BaseModel):
    url: str = Field(min_length=1, max_length=MAX_URL_LENGTH)
    format_id: str | None = Field(default=None, max_length=100)
    audio_only: bool = False

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _validate_public_url(value)

    @field_validator("format_id")
    @classmethod
    def validate_format_id(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Za-z0-9_+.,\-]{1,100}", value):
            raise ValueError("Invalid format_id.")
        return value


def _validate_public_url(url: str) -> str:
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Please provide a valid http(s) URL.")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing embedded credentials are not allowed.")
    if len(url) > MAX_URL_LENGTH:
        raise ValueError("URL is too long.")

    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("Private/local URLs are not allowed.")

    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, None)}
    except socket.gaierror:
        # yt-dlp will provide the final extraction error; this avoids leaking
        # low-level resolver details through the API.
        raise ValueError("The hostname could not be resolved.")

    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise ValueError("Private or local network URLs are not allowed.")
    return url


_last_request_at: dict[str, float] = {}
_last_request_lock = asyncio.Lock()


async def _throttle(client_ip: str) -> None:
    now = time.monotonic()
    async with _last_request_lock:
        previous = _last_request_at.get(client_ip, 0.0)
        if now - previous < THROTTLE_SECONDS:
            raise HTTPException(status_code=429, detail="Too many requests — please slow down.")
        _last_request_at[client_ip] = now


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "service": "AnyDown",
        "yt_dlp_version": yt_dlp.version.__version__,
        "max_concurrent_downloads": MAX_CONCURRENT_DOWNLOADS,
    }


@app.post("/api/info")
def get_info(payload: InfoRequest):
    try:
        return fetch_info(payload.url)
    except UnsupportedURLError as exc:
        raise HTTPException(status_code=400, detail=f"Couldn't read that URL: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Metadata extraction failed: {exc}") from exc


def _run_download(job, payload: DownloadRequest):
    job_manager.update(job.id, status=JobStatus.DOWNLOADING)

    def progress_hook(data):
        job_manager.update_progress(job.id, data)

    try:
        filepath, display_name = download_media(
            payload.url,
            OUTPUT_DIR,
            job.id,
            format_id=payload.format_id,
            audio_only=payload.audio_only,
            progress_hook=progress_hook,
        )
        job_manager.update(
            job.id,
            status=JobStatus.COMPLETED,
            filepath=filepath,
            filename=display_name,
            progress=100.0,
        )
    except UnsupportedURLError as exc:
        job_manager.update(job.id, status=JobStatus.FAILED, error=str(exc))
    except Exception as exc:
        job_manager.update(job.id, status=JobStatus.FAILED, error=f"Unexpected error: {exc}")


async def _download_task(job, payload: DownloadRequest):
    async with DOWNLOAD_SEMAPHORE:
        await asyncio.to_thread(_run_download, job, payload)


@app.post("/api/download")
async def start_download(payload: DownloadRequest, background_tasks: BackgroundTasks, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    await _throttle(client_ip)

    job = job_manager.create_job(payload.url)
    background_tasks.add_task(_download_task, job, payload)
    return {"job_id": job.id, "status": job.status}


@app.get("/api/status/{job_id}")
def get_status(job_id: str):
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found (it may have expired).")
    return {
        "job_id": job.id,
        "status": job.status,
        "error": job.error,
        "filename": job.filename,
        "progress": round(job.progress, 1),
        "downloaded_bytes": job.downloaded_bytes,
        "total_bytes": job.total_bytes,
        "speed": job.speed,
        "eta": job.eta,
    }


@app.get("/api/file/{job_id}")
def get_file(job_id: str):
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found (it may have expired).")
    if job.status != JobStatus.COMPLETED or not job.filepath or not os.path.isfile(job.filepath):
        raise HTTPException(status_code=409, detail=f"File not ready yet (status: {job.status}).")
    return FileResponse(job.filepath, filename=job.filename, media_type="application/octet-stream")


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
