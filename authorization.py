"""Centralized download authorization and quality access policy."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

# Maximum video height accessible to unauthenticated guests (default 720p).
GUEST_MAX_HEIGHT = int(os.getenv("GUEST_MAX_HEIGHT", "720"))


def format_is_audio_only(fmt: dict[str, Any]) -> bool:
    """True when a format carries no video track.

    Audio-only formats legitimately have no ``height``, which is why the
    quality gate cannot simply treat "height is None" as an unknown video
    resolution. The normalized formats served by /api/media/inspect carry an
    explicit ``has_video`` flag; raw yt-dlp formats carry ``vcodec``.
    """
    if "has_video" in fmt:
        return not fmt.get("has_video")
    vcodec = fmt.get("vcodec")
    if vcodec is not None:
        return vcodec == "none"
    return fmt.get("height") is None


def can_download_format(user: dict[str, Any] | None, height: int | None, audio_only: bool = False) -> bool:
    """Centralized download access policy.

    - Audio-only downloads: ALLOWED for everyone.
    - Video with a known height <= GUEST_MAX_HEIGHT: ALLOWED for everyone.
    - Video with a known height > GUEST_MAX_HEIGHT: LOGIN REQUIRED.
    - Video with an *unknown* height: DENIED. The gate fails closed, because
      guessing "allow" here is what let a client bypass the gate by omitting
      ``format_id`` and letting yt-dlp fall back to its best format.
    """
    if audio_only:
        return True
    if height is None:
        return False
    if height <= GUEST_MAX_HEIGHT:
        return True
    return user is not None


def annotate_formats_with_locks(formats: list[dict[str, Any]], user: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Annotate format dictionary items with a 'locked' boolean based on user authentication."""
    annotated: list[dict[str, Any]] = []
    for fmt in formats:
        item = dict(fmt)
        audio_only = format_is_audio_only(fmt)
        height = item.get("height")
        # Audio-only formats have no height; ask the gate about the video height
        # only when the format actually is a video.
        item["locked"] = not can_download_format(
            user, None if audio_only else height, audio_only=audio_only
        )
        annotated.append(item)
    return annotated


@dataclass(frozen=True)
class FormatVerdict:
    """Server-verified truth about a client-requested format.

    ``known`` is False when the requested ``format_id`` does not appear in the
    server's own yt-dlp extraction. Callers must treat that as a rejection:
    falling back to a client-supplied height (or to no format at all) is
    exactly how the quality gate used to be bypassed.
    """

    known: bool
    audio_only: bool
    height: int | None
    has_video: bool
    has_audio: bool = False


def _format_has_audio(fmt: dict[str, Any]) -> bool:
    if "has_audio" in fmt:
        return bool(fmt.get("has_audio"))
    acodec = fmt.get("acodec")
    if acodec is not None:
        return acodec != "none"
    return True


def resolve_format(
    formats: list[dict[str, Any]],
    format_id: str | None,
    audio_only: bool = False,
) -> FormatVerdict:
    """Find the true, server-verified shape of a requested format.

    Clients cannot bypass the quality gate by supplying a fake height or by
    requesting a format that does not exist: the server resolves the request
    against yt-dlp's extracted formats and reports what it actually found.
    """
    if audio_only:
        return FormatVerdict(
            known=True, audio_only=True, height=None, has_video=False, has_audio=True
        )

    if not format_id:
        # No format requested means "give me your best", which is precisely the
        # request the gate exists to constrain. Reject instead of guessing.
        return FormatVerdict(
            known=False, audio_only=False, height=None, has_video=False, has_audio=False
        )

    for f in formats:
        if str(f.get("format_id")) == str(format_id):
            is_audio = format_is_audio_only(f)
            return FormatVerdict(
                known=True,
                audio_only=is_audio,
                height=None if is_audio else f.get("height"),
                has_video=not is_audio,
                has_audio=_format_has_audio(f),
            )

    return FormatVerdict(
        known=False, audio_only=False, height=None, has_video=False, has_audio=False
    )


def resolve_format_height(formats: list[dict[str, Any]], format_id: str | None, audio_only: bool = False) -> int | None:
    """Backwards-compatible wrapper around :func:`resolve_format`.

    Returns the verified video height, or None when the request is audio-only
    or could not be resolved. Prefer :func:`resolve_format`, which also reports
    whether the format was actually found.
    """
    return resolve_format(formats, format_id, audio_only).height
