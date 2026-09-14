"""yt-dlp based media extraction/downloading for AnyDown."""

from __future__ import annotations

import logging
import os
import re
from typing import Callable
from urllib.parse import urlparse

import yt_dlp

# Configure logging for yt-dlp debug output
logger = logging.getLogger("yt_dlp")
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler()
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
handler.setFormatter(formatter)
logger.addHandler(handler)

# Additional logger for AnyDown-specific diagnostics
diagnostic_logger = logging.getLogger("anydown.diagnostics")
diagnostic_logger.setLevel(logging.DEBUG)
diagnostic_handler = logging.StreamHandler()
diagnostic_handler.setLevel(logging.DEBUG)
diagnostic_formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
diagnostic_handler.setFormatter(diagnostic_formatter)
diagnostic_logger.addHandler(diagnostic_handler)


class UnsupportedURLError(Exception):
    """Raised when yt-dlp cannot extract or download a URL."""


# Secrets/configuration are supplied by the deployment environment.
FACEBOOK_PROXY_URL = os.getenv("FACEBOOK_PROXY_URL", "").strip()
YTDLP_POT_PROVIDER_URL = os.getenv("YTDLP_POT_PROVIDER_URL", "").strip()
YOUTUBE_COOKIES_FILE = os.getenv("YOUTUBE_COOKIES_FILE", "").strip()
YTDLP_USER_AGENT = os.getenv(
    "YTDLP_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
)

_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}
_FACEBOOK_HOSTS = {"facebook.com", "www.facebook.com", "m.facebook.com", "fb.watch", "www.fb.watch"}


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().rstrip(".")


def is_youtube(url: str) -> bool:
    host = _host(url)
    return host in _YOUTUBE_HOSTS or host.endswith(".youtube.com")


def is_facebook(url: str) -> bool:
    host = _host(url)
    return host in _FACEBOOK_HOSTS or host.endswith(".facebook.com")


def _sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "_", name).strip()
    return name[:150] if name else "download"


def _find_downloaded_file(output_dir: str, job_id: str) -> str | None:
    candidates = []
    for fname in os.listdir(output_dir):
        if fname.startswith(job_id + ".") and not fname.endswith(".part"):
            candidates.append(os.path.join(output_dir, fname))
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def _youtube_options(ydl_opts: dict) -> None:
    """Add current YouTube helpers when configured/available."""
    extractor_args = ydl_opts.setdefault("extractor_args", {})

    # Current yt-dlp guidance recommends a PO-token provider for mweb/GVS.
    if YTDLP_POT_PROVIDER_URL:
        diagnostic_logger.info(
            f"[PO-TOKEN] bgutil POT provider configured: {YTDLP_POT_PROVIDER_URL}"
        )
        diagnostic_logger.info(
            "[PO-TOKEN] Setting up youtubepot-bgutilhttp extractor args for mweb client"
        )
        
        extractor_args["youtubepot-bgutilhttp"] = {
            "base_url": [YTDLP_POT_PROVIDER_URL]
        }
        extractor_args["youtube"] = {
            "player_client": ["mweb"]
        }
        
        diagnostic_logger.info(
            "[PO-TOKEN] mweb player client configured; awaiting token request during extraction"
        )
    else:
        diagnostic_logger.warning(
            "[PO-TOKEN] No bgutil POT provider URL configured (YTDLP_POT_PROVIDER_URL not set)"
        )

    # Cookies are optional and MUST be supplied as a server-side secret file.
    # Never accept cookies from website visitors or commit this file to Git.
    if YOUTUBE_COOKIES_FILE:
        if os.path.isfile(YOUTUBE_COOKIES_FILE):
            ydl_opts["cookiefile"] = YOUTUBE_COOKIES_FILE
        else:
            # Fail loudly enough to make Render configuration mistakes obvious.
            raise UnsupportedURLError(
                f"YOUTUBE_COOKIES_FILE is configured but the file does not exist: "
                f"{YOUTUBE_COOKIES_FILE}"
            )

    # yt-dlp's current YouTube support uses EJS + a JS runtime. If the runtime
    # is present, these options allow the current solver scripts to be fetched.
    # They are harmless when the runtime/provider is unavailable; yt-dlp will
    # report the exact missing dependency in its error.
    ydl_opts.setdefault("remote_components", ["ejs:github"])
    ydl_opts.setdefault("js_runtimes", {os.getenv("YTDLP_JS_RUNTIME", "node"): {}})


def _base_options() -> dict:
    opts = {
        "quiet": False,
        "verbose": True,
        "no_warnings": False,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 4,
        "http_headers": {"User-Agent": YTDLP_USER_AGENT},
        "logger": logger,
    }
    
    # Log diagnostic info about PO-token provider availability at startup
    if YTDLP_POT_PROVIDER_URL:
        diagnostic_logger.debug(
            f"[PO-TOKEN] Base options prepared with bgutil provider: {YTDLP_POT_PROVIDER_URL}"
        )
    
    return opts


def _apply_platform_options(url: str, ydl_opts: dict) -> None:
    if is_youtube(url):
        diagnostic_logger.info(f"[EXTRACTION] YouTube URL detected: {url}")
        _youtube_options(ydl_opts)
    elif is_facebook(url) and FACEBOOK_PROXY_URL:
        ydl_opts["proxy"] = FACEBOOK_PROXY_URL


def _friendly_error(error: Exception) -> str:
    text = str(error)
    lower = text.lower()

    if "sign in to confirm you're not a bot" in lower or "confirm you're not a bot" in lower:
        # Log diagnostic info about the failure
        if YTDLP_POT_PROVIDER_URL:
            diagnostic_logger.error(
                "[PO-TOKEN] mweb extraction failed with LOGIN_REQUIRED despite bgutil provider. "
                "Check: bgutil connectivity, token acquisition, and video/session restrictions."
            )
        else:
            diagnostic_logger.error(
                "[PO-TOKEN] LOGIN_REQUIRED and no bgutil provider configured."
            )
        
        if YTDLP_POT_PROVIDER_URL and YOUTUBE_COOKIES_FILE:
            return (
                "YouTube rejected the server session as automated. The configured PO-token "
                "provider and cookie session were both supplied, so this is likely an "
                "IP/session block. Try again later or use a different server/IP."
            )
        if not YTDLP_POT_PROVIDER_URL:
            return (
                "YouTube rejected this server as automated. Configure a reachable bgutil "
                "PO-token provider with YTDLP_POT_PROVIDER_URL. If the video still requires "
                "authentication, use a server-side YouTube cookies secret file; never paste "
                "cookies into the website."
            )
        return (
            "YouTube rejected this server as automated. The PO-token provider is configured, "
            "but this video/session may also require authenticated cookies."
        )

    if "sign in to confirm your age" in lower or "age-restricted" in lower:
        return "This YouTube video requires an authenticated session. Configure YOUTUBE_COOKIES_FILE on the server."

    if "requested format is not available" in lower:
        return "That format is no longer available. Fetch the URL again and choose another quality."

    if "ffmpeg" in lower and "not found" in lower:
        return "FFmpeg is missing on the server. Install the FFmpeg binary and redeploy."

    return text


def fetch_info(url: str) -> dict:
    """Extract metadata and normalized media formats without downloading."""
    ydl_opts = _base_options()
    ydl_opts.update({"skip_download": True})
    _apply_platform_options(url, ydl_opts)

    diagnostic_logger.info("[EXTRACTION] Starting fetch_info extraction")
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            diagnostic_logger.debug("[PO-TOKEN] YoutubeDL instance created; calling extract_info")
            info = ydl.extract_info(url, download=False)
            diagnostic_logger.info("[EXTRACTION] fetch_info extraction completed successfully")
    except yt_dlp.utils.DownloadError as exc:
        diagnostic_logger.error(f"[EXTRACTION] fetch_info failed: {str(exc)[:100]}")
        raise UnsupportedURLError(_friendly_error(exc)) from exc

    formats = []
    seen = set()
    for f in info.get("formats", []) or []:
        has_video = f.get("vcodec") not in (None, "none")
        has_audio = f.get("acodec") not in (None, "none")
        if not has_video and not has_audio:
            continue

        # Keep useful media streams; storyboards/images are filtered above.
        fmt_id = str(f.get("format_id") or "")
        if not fmt_id:
            continue
        key = (fmt_id, f.get("ext"), f.get("height"), has_video, has_audio)
        if key in seen:
            continue
        seen.add(key)

        formats.append({
            "format_id": fmt_id,
            "ext": f.get("ext"),
            "resolution": f.get("resolution") or (
                f"{f['height']}p" if f.get("height") else None
            ),
            "height": f.get("height"),
            "has_video": has_video,
            "has_audio": has_audio,
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "note": f.get("format_note"),
        })

    # Put useful video formats first, then audio formats.
    formats.sort(key=lambda f: (
        0 if f["has_video"] else 1,
        -(f.get("height") or 0),
        0 if f["has_audio"] else 1,
    ))

    return {
        "title": info.get("title", "untitled"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
        "extractor": info.get("extractor"),
        "formats": formats,
    }


def download_media(
    url: str,
    output_dir: str,
    job_id: str,
    format_id: str | None = None,
    format_has_audio: bool = False,
    audio_only: bool = False,
    progress_callback: Callable[[dict], None] | None = None,
) -> tuple[str, str]:
    """Download media. Returns (filepath_on_disk, display_filename)."""
    os.makedirs(output_dir, exist_ok=True)
    outtmpl = os.path.join(output_dir, f"{job_id}.%(ext)s")

    def progress_hook(data: dict) -> None:
        if not progress_callback:
            return
        status = data.get("status")
        downloaded = data.get("downloaded_bytes") or 0
        total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
        percent = (downloaded / total * 100.0) if total else None
        progress_callback({
            "status": status,
            "percent": percent,
            "downloaded_bytes": downloaded,
            "total_bytes": total,
            "speed": data.get("speed"),
            "eta": data.get("eta"),
            "filename": data.get("filename"),
        })

    ydl_opts = _base_options()
    ydl_opts.update({
        "outtmpl": outtmpl,
        "progress_hooks": [progress_hook],
    })

    if audio_only:
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        ydl_opts["merge_output_format"] = "mp4"
        if format_id:
            # Video-only formats need best audio merged; combined formats can
            # be downloaded directly. The frontend tells us which case applies.
            if format_has_audio:
                ydl_opts["format"] = f"{format_id}/bestvideo+bestaudio/best"
            else:
                ydl_opts["format"] = f"{format_id}+bestaudio/{format_id}/bestvideo+bestaudio/best"
        else:
            ydl_opts["format"] = "bestvideo+bestaudio/best"

    _apply_platform_options(url, ydl_opts)

    diagnostic_logger.info("[EXTRACTION] Starting download_media extraction")
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            diagnostic_logger.debug("[PO-TOKEN] YoutubeDL instance created; calling extract_info for download")
            info = ydl.extract_info(url, download=True)
            diagnostic_logger.info("[EXTRACTION] download_media extraction completed successfully")
    except yt_dlp.utils.DownloadError as exc:
        diagnostic_logger.error(f"[EXTRACTION] download_media failed: {str(exc)[:100]}")
        raise UnsupportedURLError(_friendly_error(exc)) from exc

    filepath = _find_downloaded_file(output_dir, job_id)
    if not filepath:
        raise UnsupportedURLError("Download finished but the output file could not be located.")

    ext = os.path.splitext(filepath)[1]
    display_name = _sanitize_filename(info.get("title", job_id)) + ext
    return filepath, display_name
