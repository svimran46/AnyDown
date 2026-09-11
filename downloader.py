"""yt-dlp based media extraction and downloading for AnyDown."""

import os
import re
from urllib.parse import urlparse

import yt_dlp


PROXY_URL = os.getenv("DOWNLOADER_PROXY_URL", "").strip() or None
POT_PROVIDER_URL = os.getenv("YOUTUBE_POT_PROVIDER_URL", "").strip() or None


class UnsupportedURLError(Exception):
    """Raised when yt-dlp cannot extract or download a URL."""


def _sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "_", name).strip().strip(".")
    return name[:150] if name else "download"


def _find_downloaded_file(output_dir: str, job_id: str) -> str | None:
    matches = []
    for fname in os.listdir(output_dir):
        if fname.startswith(job_id + ".") and not fname.endswith(('.part', '.ytdl')):
            path = os.path.join(output_dir, fname)
            if os.path.isfile(path):
                matches.append(path)
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def _base_options() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 20,
        "file_access_retries": 3,
        "continuedl": True,
    }
    if PROXY_URL:
        # A proxy is useful for deployments that need it; it is never hard-coded.
        opts["proxy"] = PROXY_URL
    if POT_PROVIDER_URL:
        opts["extractor_args"] = {
            "youtubepot-bgutilhttp": {"base_url": [POT_PROVIDER_URL]}
        }
    return opts


def _format_info(f: dict) -> dict | None:
    has_video = f.get("vcodec") not in (None, "none")
    has_audio = f.get("acodec") not in (None, "none")
    if not has_video and not has_audio:
        return None

    height = f.get("height")
    resolution = f.get("resolution") or (f"{height}p" if height else None)
    return {
        "format_id": str(f.get("format_id")) if f.get("format_id") is not None else None,
        "ext": f.get("ext"),
        "resolution": resolution,
        "height": height,
        "width": f.get("width"),
        "fps": f.get("fps"),
        "has_video": has_video,
        "has_audio": has_audio,
        "filesize": f.get("filesize") or f.get("filesize_approx"),
        "note": f.get("format_note"),
        "vcodec": f.get("vcodec"),
        "acodec": f.get("acodec"),
    }


def fetch_info(url: str) -> dict:
    """Extract metadata and useful media formats without downloading."""
    opts = _base_options()
    opts["skip_download"] = True

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise UnsupportedURLError(str(exc)) from exc

    if not info:
        raise UnsupportedURLError("No media information was returned.")

    formats = []
    seen = set()
    for raw in info.get("formats", []) or []:
        item = _format_info(raw)
        if not item or not item["format_id"]:
            continue
        # Don't flood the UI with duplicate format IDs.
        if item["format_id"] in seen:
            continue
        seen.add(item["format_id"])
        formats.append(item)

    # Prefer useful video formats first, highest resolution first, then audio.
    formats.sort(key=lambda f: (
        0 if f["has_video"] else 1,
        -(f["height"] or 0),
        0 if f["has_audio"] else 1,
        f["ext"] or "",
    ))

    return {
        "title": info.get("title") or "untitled",
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader") or info.get("channel"),
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "webpage_url": info.get("webpage_url") or url,
        "formats": formats,
    }


def download_media(
    url: str,
    output_dir: str,
    job_id: str,
    format_id: str | None = None,
    audio_only: bool = False,
    progress_hook=None,
) -> tuple[str, str]:
    """Download media with yt-dlp and return (filepath, display filename)."""
    os.makedirs(output_dir, exist_ok=True)
    outtmpl = os.path.join(output_dir, f"{job_id}.%(ext)s")

    opts = _base_options()
    opts.update({
        "outtmpl": outtmpl,
        "overwrites": True,
        "continuedl": True,
    })

    if progress_hook:
        opts["progress_hooks"] = [progress_hook]

    if audio_only or format_id == "audio-only":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    elif format_id:
        # A selected video-only format is paired with the best available audio.
        # If the selected format already contains audio, it is used as-is.
        opts["format"] = f"{format_id}+bestaudio/{format_id}/bestvideo+bestaudio/best"
        opts["merge_output_format"] = "mp4"
    else:
        opts["format"] = "bestvideo+bestaudio/best"
        opts["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as exc:
        raise UnsupportedURLError(str(exc)) from exc

    filepath = _find_downloaded_file(output_dir, job_id)
    if not filepath:
        raise UnsupportedURLError("Download finished but the output file could not be located.")

    ext = os.path.splitext(filepath)[1]
    display_name = _sanitize_filename(info.get("title", job_id)) + ext
    return filepath, display_name
