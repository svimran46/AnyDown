"""Centralized download authorization and quality access policy."""

from __future__ import annotations

import os
from typing import Any

# Maximum video height accessible to unauthenticated guests (default 720p).
GUEST_MAX_HEIGHT = int(os.getenv("GUEST_MAX_HEIGHT", "720"))


def can_download_format(user: dict[str, Any] | None, height: int | None, audio_only: bool = False) -> bool:
    """Centralized download access policy.
    
    - Audio-only (audio_only=True or height is None): ALLOWED for all.
    - Video height <= GUEST_MAX_HEIGHT (e.g. 360p, 480p, 720p): ALLOWED for all.
    - Video height > GUEST_MAX_HEIGHT (e.g. 1080p, 1440p, 2160p/4K): LOGIN REQUIRED.
    """
    if audio_only or height is None or height <= GUEST_MAX_HEIGHT:
        return True
    return user is not None


def annotate_formats_with_locks(formats: list[dict[str, Any]], user: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Annotate format dictionary items with a 'locked' boolean based on user authentication."""
    annotated: list[dict[str, Any]] = []
    for fmt in formats:
        item = dict(fmt)
        height = item.get("height")
        item["locked"] = not can_download_format(user, height)
        annotated.append(item)
    return annotated


def resolve_format_height(formats: list[dict[str, Any]], format_id: str | None, audio_only: bool = False) -> int | None:
    """Find the true server-verified height of a requested format from extracted metadata.
    
    Clients cannot bypass the quality gate by supplying a fake height or manipulating
    the request parameters; the server verifies the format against yt-dlp's extracted formats.
    """
    if audio_only or not format_id or format_id == "audio-only":
        return None

    for f in formats:
        if str(f.get("format_id")) == str(format_id):
            return f.get("height")

    return None
