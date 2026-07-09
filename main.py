"""FastAPI backend for a multi-platform media downloader.

Endpoints:
  POST /api/info             -> metadata + available formats for a URL
  POST /api/download         -> kicks off a background download job
  GET  /api/status/{job_id}  -> poll job status
  GET  /api/file/{job_id}    -> download the finished file
"""

import os
import re
import time
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from downloader import fetch_info, download_media, UnsupportedURLError
from job_manager import job_manager, JobStatus

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "downloads")
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
CLEANUP_INTERVAL_SECONDS = 300  # sweep for expired files every 5 minutes
THROTTLE_SECONDS = 5            # min seconds between download requests, per IP

os.makedirs(OUTPUT_DIR, exist_ok=True)


async def _cleanup_loop():
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        await asyncio.to_thread(job_manager.cleanup_expired)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_cleanup_loop())
    yield
    task.cancel()


app = FastAPI(title="Media Downloader API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # replace with your actual frontend origin before deploying
    allow_methods=["*"],
    allow_headers=["*"],
)


class InfoRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    format_id: str | None = None
    audio_only: bool = False


def _require_http_url(url: str) -> None:
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Please provide a valid http(s) URL.")


# Naive in-memory per-IP throttle — fine for a single instance, swap for
# slowapi / nginx rate limiting (or a Redis-backed limiter) in production.
_last_request_at: dict[str, float] = {}


def _throttle(client_ip: str) -> None:
    now = time.time()
    if now - _last_request_at.get(client_ip, 0) < THROTTLE_SECONDS:
        raise HTTPException(status_code=429, detail="Too many requests — please slow down.")
    _last_request_at[client_ip] = now


@app.get("/api/health")
def health():
    return {"status": "ok", "message": "Media downloader API is running."}


@app.post("/api/info")
def get_info(payload: InfoRequest):
    _require_http_url(payload.url)
    try:
        return fetch_info(payload.url)
    except UnsupportedURLError as e:
        raise HTTPException(status_code=400, detail=f"Couldn't read that URL: {e}")


@app.post("/api/download")
def start_download(payload: DownloadRequest, background_tasks: BackgroundTasks, request: Request):
    _require_http_url(payload.url)
    client_ip = request.client.host if request.client else "unknown"
    _throttle(client_ip)

    if payload.format_id and not re.fullmatch(r"[\w+/.\-]{1,100}", payload.format_id):
        raise HTTPException(status_code=400, detail="Invalid format_id.")

    job = job_manager.create_job(payload.url)

    def run_download():
        job_manager.update(job.id, status=JobStatus.DOWNLOADING)
        try:
            filepath, display_name = download_media(
                payload.url,
                OUTPUT_DIR,
                job.id,
                format_id=payload.format_id,
                audio_only=payload.audio_only,
            )
            job_manager.update(
                job.id, status=JobStatus.COMPLETED, filepath=filepath, filename=display_name
            )
        except UnsupportedURLError as e:
            job_manager.update(job.id, status=JobStatus.FAILED, error=str(e))
        except Exception as e:  # keep the API alive even on unexpected failures
            job_manager.update(job.id, status=JobStatus.FAILED, error=f"Unexpected error: {e}")

    background_tasks.add_task(run_download)
    return {"job_id": job.id, "status": job.status}


@app.get("/api/status/{job_id}")
def get_status(job_id: str):
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found (it may have expired).")
    return {"job_id": job.id, "status": job.status, "error": job.error, "filename": job.filename}


@app.get("/api/file/{job_id}")
def get_file(job_id: str):
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found (it may have expired).")
    if job.status != JobStatus.COMPLETED or not job.filepath or not os.path.exists(job.filepath):
        raise HTTPException(status_code=409, detail=f"File not ready yet (status: {job.status}).")
    return FileResponse(job.filepath, filename=job.filename, media_type="application/octet-stream")


# Mounted last and at "/" so it only catches requests that don't match an
# /api/* route above — this serves index.html, style.css, and app.js, and
# means the frontend and API are same-origin (no CORS needed in practice).
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
