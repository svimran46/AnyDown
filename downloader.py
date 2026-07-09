"""Thin wrapper around yt-dlp: metadata extraction + actual downloading."""

import os
import re
import yt_dlp

# Proxy configuration to route traffic through residential nodes
PROXY_URL = "http://spz3hzzvu2:tVmz3i_0htJc9WY6cl@gate.decodo.com:10001"


class UnsupportedURLError(Exception):
    """Raised when yt-dlp can't extract or download a given URL."""


def _sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "_", name).strip()
    return name[:150] if name else "download"


def _find_downloaded_file(output_dir: str, job_id: str) -> str | None:
    for fname in os.listdir(output_dir):
        if fname.startswith(job_id + "."):
            return os.path.join(output_dir, fname)
    return None


def fetch_info(url: str) -> dict:
    """Extract metadata only — no download."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "proxy": PROXY_URL,  # Injected residential proxy
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise UnsupportedURLError(str(e)) from e

    formats = []
    for f in info.get("formats", []) or []:
        if f.get("vcodec") in (None, "none") and f.get("acodec") in (None, "none"):
            continue  # skip storyboards / non-media formats
        formats.append({
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "resolution": f.get("resolution") or (f"{f['height']}p" if f.get("height") else None),
            "has_video": f.get("vcodec") not in (None, "none"),
            "has_audio": f.get("acodec") not in (None, "none"),
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "note": f.get("format_note"),
        })

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
    audio_only: bool = False,
) -> tuple[str, str]:
    """Downloads the media. Returns (filepath_on_disk, display_filename)."""
    os.makedirs(output_dir, exist_ok=True)
    outtmpl = os.path.join(output_dir, f"{job_id}.%(ext)s")

    ydl_opts = {
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "proxy": PROXY_URL,  # Injected residential proxy
    }

    if audio_only:
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        ydl_opts["merge_output_format"] = "mp4"
        ydl_opts["format"] = (
            f"{format_id}/bestvideo+bestaudio/best" if format_id else "bestvideo+bestaudio/best"
        )

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        raise UnsupportedURLError(str(e)) from e

    filepath = _find_downloaded_file(output_dir, job_id)
    if not filepath:
        raise UnsupportedURLError("Download finished but the output file could not be located.")

    ext = os.path.splitext(filepath)[1]
    display_name = _sanitize_filename(info.get("title", job_id)) + ext
    return filepath, display_name
