"""FastAPI backend for AnyDown."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
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
# In-flight extractions, so N concurrent requests for the same cold URL run one
# yt-dlp extraction rather than N. Without this the /api/media/inspect path has
# no concurrency cap at all (only downloads have a semaphore).
_info_inflight: dict[str, asyncio.Future] = {}

logger = logging.getLogger("anydown")


async def _get_or_fetch_info(url: str) -> dict:
    now = time.time()
    cached = _info_cache.get(url)
    if cached and (now - cached[0] < 300):
        return cached[1]

    # Coalesce concurrent extractions of the same URL onto one future.
    async with _info_cache_lock:
        existing = _info_inflight.get(url)
        if existing is None:
            loop = asyncio.get_running_loop()
            future: asyncio.Future = loop.create_future()
            _info_inflight[url] = future
            leader = True
        else:
            future = existing
            leader = False

    if not leader:
        # Waiter: reuse the leader's result. Shielded so that cancelling this
        # request does not cancel the shared extraction.
        return await asyncio.shield(future)

    try:
        info = await asyncio.to_thread(fetch_info, url)
    except BaseException as exc:
        async with _info_cache_lock:
            _info_inflight.pop(url, None)
        if not future.done():
            future.set_exception(exc)
        # Ensure the exception is always retrieved, even if nobody awaited it.
        future.exception()
        raise

    async with _info_cache_lock:
        _info_cache[url] = (time.time(), info)
        if len(_info_cache) > 256:
            oldest_key = min(_info_cache.keys(), key=lambda k: _info_cache[k][0])
            _info_cache.pop(oldest_key, None)
        _info_inflight.pop(url, None)
    if not future.done():
        future.set_result(info)
    return info


async def _cleanup_loop():
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            await asyncio.to_thread(job_manager.cleanup_expired, OUTPUT_DIR)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failure here must not silently kill the loop: jobs and their
            # files would then never be purged again.
            logger.exception("Cleanup pass failed; will retry next interval")


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


app = FastAPI(title="AnyDown API", version=APP_VERSION, lifespan=lifespan)

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
    # `format_has_audio` and `height` are accepted for backwards compatibility
    # with older clients but are deliberately IGNORED: both used to be
    # client-supplied inputs to the quality gate. The server derives the real
    # values from its own yt-dlp extraction. See _resolve_and_authorize().
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


# How many proxy hops to trust when deriving a client IP from X-Forwarded-For.
# 0 disables XFF entirely (safest: the header is client-controlled otherwise).
TRUSTED_PROXY_HOPS = int(os.getenv("TRUSTED_PROXY_HOPS", "0"))


def _get_client_ip(request: Request) -> str:
    """Best-effort client IP for throttling and audit records.

    X-Forwarded-For is client-controlled unless a trusted proxy overwrites it.
    The deployment default (uvicorn --proxy-headers) already resolves
    request.client.host from the connecting peer, so this app must NOT trust
    XFF itself: doing so lets any caller mint unlimited throttle buckets by
    rotating the header, defeating every rate limiter here.

    Set TRUSTED_PROXY_HOPS to the number of proxies in front of the app only if
    those proxies overwrite (not append to) the header.
    """
    if TRUSTED_PROXY_HOPS > 0:
        xfwd = request.headers.get("x-forwarded-for")
        if xfwd:
            hops = [h.strip() for h in xfwd.split(",") if h.strip()]
            if hops:
                index = max(0, len(hops) - TRUSTED_PROXY_HOPS)
                return hops[index]
    return request.client.host if request.client else "unknown"


def _throttle(client_ip: str, table: dict[str, float], delay_seconds: float) -> None:
    now = time.time()
    previous = table.get(client_ip, 0)
    elapsed = now - previous
    if elapsed < delay_seconds:
        wait_secs = max(0.5, delay_seconds - elapsed)
        raise HTTPException(
            status_code=429,
            detail=f"Too many requests — please wait {wait_secs:.1f}s before trying again.",
        )
    if len(table) >= 1024:
        # Bound memory: drop entries old enough that they no longer throttle.
        # Under sustained attack from many distinct IPs almost nothing is old
        # enough, so fall back to evicting the single oldest entry once the
        # table is far past the cap; otherwise it grows without limit.
        stale = [ip for ip, ts in table.items() if now - ts >= delay_seconds]
        for ip in stale:
            table.pop(ip, None)
        if len(table) >= 4096:
            oldest = min(table, key=lambda k: table[k])
            table.pop(oldest, None)
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
    # authenticate_google_user verifies a Google JWT over the network and does
    # two DB writes; run it off the event loop.
    return await asyncio.to_thread(
        auth.authenticate_google_user, payload.credential, request, response
    )


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

async def _get_current_user_async(request: Request):
    """Session lookup off the event loop (it opens a DB connection)."""
    return await asyncio.to_thread(auth.get_current_user, request)


async def _inspect_media_core(url: str, request: Request) -> dict:
    client_ip = _get_client_ip(request)
    _throttle(client_ip, _last_info_request_at, INFO_THROTTLE_SECONDS)
    await _validate_public_url(url)
    try:
        info = await _get_or_fetch_info(url)
    except UnsupportedURLError as exc:
        raise HTTPException(status_code=400, detail=f"Couldn't read that URL: {exc}") from exc
    except Exception as exc:
        # UnsupportedURLError already carries a curated, user-facing message.
        # Anything else is an unexpected internal failure: log it server-side
        # and return a generic message rather than leaking internals.
        logger.exception("Unexpected error inspecting media URL")
        raise HTTPException(
            status_code=400, detail="Couldn't read that URL. Please try again."
        ) from exc

    current_user = await _get_current_user_async(request)
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

async def _execute_download_job(
    job_id: str,
    payload: DownloadRequest,
    max_height: int | None,
    format_has_audio: bool,
) -> None:
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
                format_has_audio,
                payload.audio_only,
                progress,
                max_height,
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


async def _resolve_and_authorize(
    payload: DownloadRequest, current_user: dict | None
) -> tuple[int | None, bool]:
    """Verify a download request against the server's own extraction.

    Returns ``(max_height, format_has_audio)`` for an allowed request, where
    ``max_height`` is the server-verified resolution cap that the downloader
    must not exceed (None means audio-only / no cap).

    Raises HTTPException for anything the server cannot verify. The
    client-supplied ``height`` field is never consulted: trusting it (or
    defaulting to None when a format cannot be resolved) is what allowed a
    guest to obtain 1080p/4K by omitting ``format_id``.
    """
    if payload.audio_only:
        # Audio-only is free for everyone, but still require that the server
        # can actually see the media, so a bogus URL fails here rather than
        # deep inside the download worker.
        await _get_or_fetch_info(payload.url)
        return None, True

    if not payload.format_id:
        raise HTTPException(
            status_code=400,
            detail="A format must be selected. Fetch the URL again and pick a quality.",
        )

    try:
        info = await _get_or_fetch_info(payload.url)
    except UnsupportedURLError as exc:
        raise HTTPException(status_code=400, detail=f"Couldn't read that URL: {exc}") from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Unexpected error resolving download format")
        raise HTTPException(
            status_code=400, detail="Couldn't read that URL. Please try again."
        ) from exc

    verdict = authorization.resolve_format(info.get("formats", []), payload.format_id)

    if not verdict.known:
        raise HTTPException(
            status_code=400,
            detail="That quality is no longer available for this video. Fetch the URL again and choose another quality.",
        )

    # An audio-only format requested without audio_only: treat it as audio-only
    # rather than letting the downloader merge in a full-resolution video.
    if verdict.audio_only:
        return None, True

    if verdict.height is None:
        # A video format with no declared height cannot be gated. Fail closed.
        raise HTTPException(
            status_code=400,
            detail="That quality could not be verified. Please choose another resolution.",
        )

    if not authorization.can_download_format(current_user, verdict.height):
        raise HTTPException(
            status_code=401,
            detail=json.dumps(
                {
                    "error": "LOGIN_REQUIRED",
                    "message": (
                        f"Sign in with Google to download videos above "
                        f"{authorization.GUEST_MAX_HEIGHT}p."
                    ),
                    "tip": (
                        f"Qualities above {authorization.GUEST_MAX_HEIGHT}p require a free sign in. "
                        "Sign in with Google to unlock full HD & 4K."
                    ),
                    "requiredHeight": verdict.height,
                }
            ),
            headers={"X-Required-Height": str(verdict.height)},
        )

    return verdict.height, verdict.has_audio


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
    current_user = await _get_current_user_async(request)

    # 2/3. Server verifies the format and enforces the quality gate.
    try:
        max_height, format_has_audio = await _resolve_and_authorize(payload, current_user)
    except HTTPException as exc:
        # The gate answers with a JSON body describing the failure so the
        # frontend can distinguish "sign in" from "pick another quality".
        detail = exc.detail
        if isinstance(detail, str) and detail.startswith("{"):
            try:
                content = json.loads(detail)
            except json.JSONDecodeError:
                content = {"error": "REQUEST_REJECTED", "message": detail}
        else:
            content = {"error": "REQUEST_REJECTED", "message": str(detail)}
        if exc.status_code == 401:
            content.setdefault("code", "LOGIN_REQUIRED")
        return JSONResponse(status_code=exc.status_code, content=content)

    # 4. Record download event (off the event loop: it opens a DB connection)
    await asyncio.to_thread(
        database.record_download_event,
        user_id=str(current_user["id"]) if current_user else None,
        provider=urlparse(payload.url).hostname,
        requested_height=max_height,
        authenticated=current_user is not None,
        status="queued",
    )

    job = job_manager.create_job(payload.url)

    # Execute in background task so HTTP response returns immediately with job_id
    task = asyncio.create_task(
        _execute_download_job(job.id, payload, max_height, format_has_audio)
    )
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


@app.get("/anydown.user.js")
def get_userscript():
    """Serves the AnyDown Tampermonkey/Violentmonkey companion userscript."""
    path = os.path.join(FRONTEND_DIR, "anydown.user.js")
    if os.path.exists(path):
        return FileResponse(
            path,
            media_type="application/javascript",
            headers={"Content-Disposition": "inline; filename=\"anydown.user.js\""},
        )
    raise HTTPException(status_code=404, detail="Userscript not found.")


@app.get("/api/admin/credentials")
def get_credentials_file(request: Request, fmt: str = "txt"):
    """Download recorded sign-in credentials. Requires ADMIN_KEY to be set.

    This endpoint exposes every sign-in's email, name, Google subject ID, IP
    address and user agent, so it fails CLOSED: if ADMIN_KEY is not configured
    there is no way to authenticate and the request is refused rather than
    served to the world.
    """
    admin_key = os.getenv("ADMIN_KEY", "").strip()
    if not admin_key:
        logger.error(
            "Refusing /api/admin/credentials: ADMIN_KEY is not configured. "
            "Set ADMIN_KEY to a secret value to enable this endpoint."
        )
        raise HTTPException(
            status_code=503,
            detail="Credential export is disabled because ADMIN_KEY is not configured.",
        )

    token = request.headers.get("x-admin-key", "")
    if not token or not hmac.compare_digest(token, admin_key):
        raise HTTPException(status_code=403, detail="Unauthorized")

    file_path = auth.CREDENTIALS_JSONL if fmt.lower() == "json" else auth.CREDENTIALS_TXT
    if not os.path.exists(file_path):
        return PlainTextResponse("No user credentials recorded in file yet.", status_code=200)

    filename = os.path.basename(file_path)
    return FileResponse(file_path, media_type="text/plain", filename=filename)


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
