"""FastAPI backend for AnyDown."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import time
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import yt_dlp
import auth
import authorization
import database
from downloader import (
    APP_VERSION,
    MAX_FILESIZE_BYTES,
    YOUTUBE_PROXY_URL,
    download_media,
    detect_js_runtime,
    fetch_info,
    pot_provider_status,
    UnsupportedURLError,
    youtube_cookies_status,
)
from job_manager import JobStatus, job_manager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "downloads")
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
CLEANUP_INTERVAL_SECONDS = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "300"))
THROTTLE_SECONDS = float(os.getenv("THROTTLE_SECONDS", "5"))
INFO_THROTTLE_SECONDS = float(os.getenv("INFO_THROTTLE_SECONDS", "1"))
AUTH_THROTTLE_SECONDS = float(os.getenv("AUTH_THROTTLE_SECONDS", "2"))
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2"))

os.makedirs(OUTPUT_DIR, exist_ok=True)
_download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
_last_download_request_at: dict[str, float] = {}
_last_info_request_at: dict[str, float] = {}
_last_auth_request_at: dict[str, float] = {}
_background_tasks: set[asyncio.Task] = set()

# Short-lived in-memory metadata cache so /api/download does not duplicate yt-dlp extraction
_info_cache: dict[str, tuple[float, dict]] = {}
_info_cache_lock = asyncio.Lock()


async def _get_or_fetch_info(url: str) -> dict:
    now = time.time()
    cached = _info_cache.get(url)
    if cached and (now - cached[0] < 300):
        return cached[1]

    info = await asyncio.to_thread(fetch_info, url)
    async with _info_cache_lock:
        _info_cache[url] = (now, info)
        if len(_info_cache) > 256:
            oldest_key = min(_info_cache.keys(), key=lambda k: _info_cache[k][0])
            _info_cache.pop(oldest_key, None)
    return info


async def _cleanup_loop():
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        await asyncio.to_thread(job_manager.cleanup_expired, OUTPUT_DIR)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize database tables and migrations
    try:
        await asyncio.to_thread(database.init_db)
    except Exception as exc:
        import logging
        logging.getLogger("anydown").error("Failed to initialize database: %s", exc)

    cleanup_task = asyncio.create_task(_cleanup_loop())
    yield
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    if _background_tasks:
        for t in list(_background_tasks):
            t.cancel()
        await asyncio.gather(*_background_tasks, return_exceptions=True)


app = FastAPI(title="AnyDown API", version="2.1", lifespan=lifespan)

# Same-origin is the normal deployment mode. Explicit origins can be supplied
# if a separate frontend is used.
allowed_origins = [x.strip() for x in os.getenv("CORS_ORIGINS", "").split(",") if x.strip()]
if allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )


class InfoRequest(BaseModel):
    url: str = Field(min_length=8, max_length=4096)


class DownloadRequest(BaseModel):
    url: str = Field(min_length=8, max_length=4096)
    format_id: str | None = Field(default=None, max_length=100)
    format_has_audio: bool = False
    audio_only: bool = False
    height: int | None = Field(default=None, ge=0, le=10000)


class GoogleAuthRequest(BaseModel):
    credential: str = Field(min_length=10, max_length=8192)


def _validate_url_syntax(url: str) -> str:
    """Cheap, DNS-free URL checks. Returns the validated hostname.

    Deliberately does NOT resolve DNS: getaddrinfo can block for many seconds
    and must never run on the event loop. DNS is checked separately (async)
    and is also re-validated at the yt-dlp layer (see downloader.py), because
    a resolution here cannot prevent a later DNS rebinding anyway.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(status_code=400, detail="Please provide a valid http(s) URL.")

    host = parsed.hostname.lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"}:
        raise HTTPException(status_code=400, detail="Local URLs are not supported.")

    # Block direct IP literals in private/link-local/loopback ranges.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip and (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        raise HTTPException(status_code=400, detail="Private or local network URLs are not supported.")

    # Avoid obvious credential-bearing URLs.
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="URLs containing embedded credentials are not supported.")

    return host


async def _validate_dns(host: str) -> None:
    """Resolve the hostname off the event loop and reject private addresses.

    Note this is advisory against SSRF-by-DNS, not a complete rebinding fix;
    downloader.py also validates each connected address at download time.
    """
    import socket

    def _resolve() -> list[str]:
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            return []
        return [sockaddr[0] for *_, sockaddr in infos]

    addrs = await asyncio.to_thread(_resolve)
    if not addrs:
        raise HTTPException(status_code=400, detail="Unable to resolve hostname")
    for addr in addrs:
        try:
            resolved_ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (
            resolved_ip.is_private
            or resolved_ip.is_loopback
            or resolved_ip.is_link_local
            or resolved_ip.is_reserved
            or resolved_ip.is_multicast
            or resolved_ip.is_unspecified
        ):
            raise HTTPException(status_code=400, detail="Private or local network URLs are not supported.")


async def _validate_public_url(url: str) -> None:
    host = _validate_url_syntax(url)
    await _validate_dns(host)


def _get_client_ip(request: Request) -> str:
    xfwd = request.headers.get("x-forwarded-for")
    if xfwd:
        return xfwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _throttle(client_ip: str, table: dict[str, float], delay_seconds: float) -> None:
    now = time.time()
    previous = table.get(client_ip, 0)
    if now - previous < delay_seconds:
        raise HTTPException(status_code=429, detail="Too many requests — please slow down.")
    if len(table) >= 1024:
        # Bound memory: drop entries old enough that they no longer throttle.
        for ip, ts in list(table.items()):
            if now - ts >= delay_seconds:
                del table[ip]
    table[client_ip] = now


@app.get("/api/config")
def get_public_config():
    """Expose non-sensitive client configuration for Google Sign-In and feature gates."""
    return {
        "google_client_id": auth.GOOGLE_CLIENT_ID,
        "googleClientId": auth.GOOGLE_CLIENT_ID,
        "guest_max_height": authorization.GUEST_MAX_HEIGHT,
        "guestMaxHeight": authorization.GUEST_MAX_HEIGHT,
        "app_base_url": os.getenv("APP_BASE_URL", "").strip(),
    }


get_config = get_public_config


@app.get("/api/health")
def health():
    cookies = youtube_cookies_status()
    pot = pot_provider_status()
    js_runtime = os.getenv("YTDLP_JS_RUNTIME", "").strip() or detect_js_runtime()
    return {
        "status": "ok",
        "app_version": APP_VERSION,
        "yt_dlp": yt_dlp.version.__version__,
        "youtube_cookies": cookies,
        "youtube_cookies_configured": cookies["configured"],
        "youtube_proxy_configured": bool(YOUTUBE_PROXY_URL),
        "pot_provider_configured": pot["configured"],
        "pot_provider_reachable": pot["reachable"],
        "js_runtime": js_runtime or None,
        "max_concurrent_downloads": MAX_CONCURRENT_DOWNLOADS,
        "max_filesize_bytes": MAX_FILESIZE_BYTES,
    }


# --------------------------------------------------------------------------
# Authentication endpoints
# --------------------------------------------------------------------------

@app.post("/api/auth/google")
async def auth_google(payload: GoogleAuthRequest, request: Request, response: Response):
    client_ip = _get_client_ip(request)
    _throttle(client_ip, _last_auth_request_at, AUTH_THROTTLE_SECONDS)
    return auth.authenticate_google_user(payload.credential, request, response)


@app.get("/api/auth/me")
def auth_me(request: Request):
    user = auth.get_current_user(request)
    if not user:
        return {"authenticated": False, "user": None}
    return {
        "authenticated": True,
        "user": {
            "id": str(user["id"]),
            "email": user["email"],
            "name": user.get("name"),
            "avatarUrl": user.get("avatar_url"),
        },
    }


@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response):
    return auth.logout_user(request, response)


# --------------------------------------------------------------------------
# Media Inspection & Quality Gating
# --------------------------------------------------------------------------

async def _inspect_media_core(url: str, request: Request) -> dict:
    await _validate_public_url(url)
    client_ip = _get_client_ip(request)
    _throttle(client_ip, _last_info_request_at, INFO_THROTTLE_SECONDS)
    try:
        info = await _get_or_fetch_info(url)
    except UnsupportedURLError as exc:
        raise HTTPException(status_code=400, detail=f"Couldn't read that URL: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Couldn't read that URL: {exc}") from exc

    current_user = auth.get_current_user(request)
    annotated_formats = authorization.annotate_formats_with_locks(info.get("formats", []), current_user)

    result = dict(info)
    result["formats"] = annotated_formats
    result["source"] = {
        "title": info.get("title", "untitled"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
        "extractor": info.get("extractor"),
    }
    return result


@app.post("/api/media/inspect")
async def media_inspect(payload: InfoRequest, request: Request):
    return await _inspect_media_core(payload.url, request)


@app.post("/api/info")
async def get_info(payload: InfoRequest, request: Request):
    return await _inspect_media_core(payload.url, request)


# --------------------------------------------------------------------------
# Download pipeline with server-enforced quality gate
# --------------------------------------------------------------------------

async def _execute_download_job(job_id: str, payload: DownloadRequest) -> None:
    async with _download_semaphore:
        job_manager.update(job_id, status=JobStatus.DOWNLOADING)
        try:
            def progress(data: dict):
                job_manager.update(
                    job_id,
                    progress=data.get("percent"),
                    downloaded_bytes=data.get("downloaded_bytes") or 0,
                    total_bytes=data.get("total_bytes") or 0,
                    speed=data.get("speed"),
                    eta=data.get("eta"),
                )

            filepath, display_name = await asyncio.to_thread(
                download_media,
                payload.url,
                OUTPUT_DIR,
                job_id,
                payload.format_id,
                payload.format_has_audio,
                payload.audio_only,
                progress,
            )
            job_manager.update(
                job_id,
                status=JobStatus.COMPLETED,
                filepath=filepath,
                filename=display_name,
                progress=100.0,
            )
        except UnsupportedURLError as exc:
            job_manager.update(job_id, status=JobStatus.FAILED, error=str(exc))
        except Exception as exc:
            job_manager.update(
                job_id,
                status=JobStatus.FAILED,
                error=f"Unexpected error: {type(exc).__name__}: {exc}",
            )


@app.post("/api/download")
async def start_download(payload: DownloadRequest, request: Request):
    # DNS check runs in a worker thread; never block the event loop here.
    await _validate_public_url(payload.url)
    client_ip = _get_client_ip(request)
    _throttle(client_ip, _last_download_request_at, THROTTLE_SECONDS)

    if payload.format_id:
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
        if not 1 <= len(payload.format_id) <= 100 or any(c not in allowed for c in payload.format_id):
            raise HTTPException(status_code=400, detail="Invalid format_id.")

    # 1. Authenticate user from session cookie
    current_user = auth.get_current_user(request)

    # 2. Server resolves actual format height to prevent client bypass
    actual_height: int | None = payload.height
    if not payload.audio_only and payload.format_id and payload.format_id != "audio-only":
        try:
            info = await _get_or_fetch_info(payload.url)
            resolved = authorization.resolve_format_height(
                info.get("formats", []), payload.format_id, payload.audio_only
            )
            if resolved is not None:
                actual_height = resolved
        except Exception:
            pass

    # 3. Centralized quality authorization enforcement
    if not authorization.can_download_format(current_user, actual_height):
        return JSONResponse(
            status_code=401,
            content={
                "error": "LOGIN_REQUIRED",
                "message": f"Sign in with Google to download videos above {authorization.GUEST_MAX_HEIGHT}p.",
                "requiredHeight": actual_height or 1080,
            },
        )

    # 4. Record download event
    database.record_download_event(
        user_id=str(current_user["id"]) if current_user else None,
        provider=urlparse(payload.url).hostname,
        requested_height=actual_height,
        authenticated=current_user is not None,
        status="queued",
    )

    job = job_manager.create_job(payload.url)

    # Execute in background task so HTTP response returns immediately with job_id
    task = asyncio.create_task(_execute_download_job(job.id, payload))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return {"job_id": job.id, "status": job.status}


@app.get("/api/status/{job_id}")
def get_status(job_id: str):
    payload = job_manager.get_payload(job_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Job not found (it may have expired).")
    return payload


@app.get("/api/file/{job_id}")
def get_file(job_id: str):
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found (it may have expired).")
    if job.status != JobStatus.COMPLETED or not job.filepath or not os.path.exists(job.filepath):
        raise HTTPException(status_code=409, detail=f"File not ready yet (status: {job.status}).")
    return FileResponse(job.filepath, filename=job.filename, media_type="application/octet-stream")


@app.get("/privacy")
def privacy_page():
    path = os.path.join(FRONTEND_DIR, "privacy.html")
    if os.path.exists(path):
        return FileResponse(path)
    raise HTTPException(status_code=404, detail="Privacy page not found.")


@app.get("/terms")
def terms_page():
    path = os.path.join(FRONTEND_DIR, "terms.html")
    if os.path.exists(path):
        return FileResponse(path)
    raise HTTPException(status_code=404, detail="Terms page not found.")


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
