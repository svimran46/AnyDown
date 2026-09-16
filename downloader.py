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
if not logger.handlers:
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
if not diagnostic_logger.handlers:
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
YTDLP_VERBOSE = os.getenv("YTDLP_VERBOSE", "false").lower() in {"true", "1", "yes"}

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


def _find_downloaded_file(output_dir: str, job_id: str, info: dict | None = None) -> str | None:
    """Find downloaded file by prepare_filename or directory scan fallback."""
    if info:
        try:
            with yt_dlp.YoutubeDL({"outtmpl": os.path.join(output_dir, f"{job_id}.%(ext)s")}) as ydl:
                prepared_path = ydl.prepare_filename(info)
                if os.path.exists(prepared_path):
                    return prepared_path
        except Exception:
            pass
    
    # Fallback: directory scan with preference for base name match
    candidates = []
    expected_base = job_id + "."
    for fname in os.listdir(output_dir):
        if fname.startswith(expected_base) and not fname.endswith(".part"):
            candidates.append(os.path.join(output_dir, fname))
    
    if not candidates:
        return None
    
    # Prefer exact matches over mtime heuristic
    return max(candidates, key=os.path.getmtime)


def _youtube_options(ydl_opts: dict, client: str | None = None) -> None:
    """Configure yt-dlp's current YouTube helpers.

    bgutil is installed as the official yt-dlp plugin.  The HTTP provider is
    only needed for clients that use PO tokens (notably mweb).  Keep the
    provider available, but don't force mweb for every YouTube request: yt-dlp
    documents several clients with different requirements and limitations.
    """
    extractor_args = ydl_opts.setdefault("extractor_args", {})

    selected_client = client or os.getenv("YOUTUBE_PRIMARY_CLIENT", "mweb").strip() or "mweb"
    diagnostic_logger.info("[YOUTUBE] Using player client: %s", selected_client)

    if YTDLP_POT_PROVIDER_URL:
        diagnostic_logger.info(
            "[PO-TOKEN] bgutil POT provider configured: %s",
            YTDLP_POT_PROVIDER_URL,
        )
        extractor_args["youtubepot-bgutilhttp"] = {
            "base_url": [YTDLP_POT_PROVIDER_URL]
        }

    extractor_args["youtube"] = {
        "player_client": [selected_client]
    }

    if YTDLP_POT_PROVIDER_URL and selected_client == "mweb":
        diagnostic_logger.info(
            "[PO-TOKEN] mweb selected; bgutil will supply PO tokens when requested"
        )

    # Cookies are optional and MUST be supplied as a server-side secret file.
    if YOUTUBE_COOKIES_FILE:
        if os.path.isfile(YOUTUBE_COOKIES_FILE):
            ydl_opts["cookiefile"] = YOUTUBE_COOKIES_FILE
        else:
            raise UnsupportedURLError(
                f"YOUTUBE_COOKIES_FILE is configured but the file does not exist: "
                f"{YOUTUBE_COOKIES_FILE}"
            )

    # yt-dlp's current YouTube support uses EJS + a JS runtime.
    ydl_opts.setdefault("remote_components", ["ejs:github"])
    ydl_opts.setdefault(
        "js_runtimes", {os.getenv("YTDLP_JS_RUNTIME", "node"): {}}
    )


def _is_youtube_retryable_error(error: Exception) -> bool:
    """Return True for failures where another YouTube client is worth trying."""
    text = str(error).lower()
    markers = (
        "sign in to confirm you're not a bot",
        "confirm you're not a bot",
        "login_required",
        "http error 403",
        "http error 429",
        "forbidden",
        "temporarily blocked",
        "requested format is not available",
    )
    return any(marker in text for marker in markers)


def _youtube_clients() -> list[str]:
    """Return a small, configurable client fallback chain."""
    raw = os.getenv("YOUTUBE_CLIENTS", "mweb,tv,web_embedded")
    clients = []
    for item in raw.split(","):
        client = item.strip()
        if client and client not in clients:
            clients.append(client)
    return clients or ["mweb", "tv", "web_embedded"]


def _extract_info_with_youtube_fallback(url: str, base_opts: dict, download: bool = False):
    """Try the configured YouTube clients, falling back only on known blocks."""
    clients = _youtube_clients()
    last_error: Exception | None = None

    for index, client in enumerate(clients):
        opts = dict(base_opts)
        # Copy nested extractor args so each attempt is independent.
        opts["extractor_args"] = {
            key: dict(value) if isinstance(value, dict) else value
            for key, value in base_opts.get("extractor_args", {}).items()
        }
        _youtube_options(opts, client=client)

        # Format IDs can differ between YouTube clients. If the primary client
        # failed and we are falling back during an actual download, use a
        # client-neutral best format instead of carrying an incompatible ID.
        if download and index > 0 and opts.get("format"):
            has_audio_pp = any(
                pp.get("key") == "FFmpegExtractAudio"
                for pp in opts.get("postprocessors", [])
                if isinstance(pp, dict)
            )
            opts["format"] = "bestaudio/best" if has_audio_pp else "bestvideo+bestaudio/best"
            diagnostic_logger.info(
                "[YOUTUBE] Fallback client=%s using client-neutral format selector",
                client,
            )

        diagnostic_logger.info(
            "[YOUTUBE] Attempt %d/%d using client=%s",
            index + 1,
            len(clients),
            client,
        )
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=download)
            diagnostic_logger.info(
                "[YOUTUBE] Success using client=%s", client
            )
            return info, client
        except yt_dlp.utils.DownloadError as exc:
            last_error = exc
            if index == len(clients) - 1 or not _is_youtube_retryable_error(exc):
                raise
            diagnostic_logger.warning(
                "[YOUTUBE] client=%s failed with a retryable YouTube error; trying next client",
                client,
            )

    assert last_error is not None
    raise last_error

def _base_options() -> dict:
    opts = {
        "quiet": True,
        "verbose": YTDLP_VERBOSE,
        "no_warnings": True,
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
            "YouTube rejected the available server-side clients for this request. "
            "AnyDown tried its configured YouTube client fallbacks, but YouTube still "
            "requires authentication or is blocking this server/IP."
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

    diagnostic_logger.info("[EXTRACTION] Starting fetch_info extraction")

    try:
        if is_youtube(url):
            info, used_client = _extract_info_with_youtube_fallback(
                url, ydl_opts, download=False
            )
            diagnostic_logger.info(
                "[EXTRACTION] fetch_info completed with YouTube client=%s",
                used_client,
            )
        else:
            _apply_platform_options(url, ydl_opts)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
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

    diagnostic_logger.info("[EXTRACTION] Starting download_media extraction")

    try:
        if is_youtube(url):
            try:
                info, used_client = _extract_info_with_youtube_fallback(
                    url, ydl_opts, download=True
                )
            except yt_dlp.utils.DownloadError:
                raise
            diagnostic_logger.info(
                "[EXTRACTION] download_media completed with YouTube client=%s",
                used_client,
            )
        else:
            _apply_platform_options(url, ydl_opts)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
            diagnostic_logger.info("[EXTRACTION] download_media extraction completed successfully")
    except yt_dlp.utils.DownloadError as exc:
        diagnostic_logger.error(f"[EXTRACTION] download_media failed: {str(exc)[:100]}")
        raise UnsupportedURLError(_friendly_error(exc)) from exc

    filepath = _find_downloaded_file(output_dir, job_id, info)
    if not filepath:
        raise UnsupportedURLError("Download finished but the output file could not be located.")

    ext = os.path.splitext(filepath)[1]
    display_name = _sanitize_filename(info.get("title", job_id)) + ext
    return filepath, display_name
