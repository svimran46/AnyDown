"""Server-side verification of requested download formats.

There is no account system and no quality gate: every caller may download any
format the source actually offers. What remains here is verification, not
permission -- the server resolves a requested format_id against its own
yt-dlp extraction so a client cannot invent a quality or smuggle in a
format_id that does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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


def can_download_format(height: int | None, audio_only: bool = False) -> bool:
    """True when this resolution is servable.

    No quality ceiling and no accounts, so the only rejections are ones where
    the server cannot verify what it would be downloading: an *unknown* height
    for a video format. Failing closed there is what stops a client from
    omitting ``format_id`` and letting yt-dlp silently resolve to its best
    format, which is not what was asked for.

    Audio-only is always allowed; it carries no height to verify.
    """
    if audio_only:
        return True
    return height is not None


def annotate_formats(formats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return copies of the formats with a 'locked' flag for the client UI.

    Nothing is ever locked now, so the flag is always False. It is kept so the
    frontend's format rendering does not need to change shape.
    """
    annotated: list[dict[str, Any]] = []
    for fmt in formats:
        item = dict(fmt)
        audio_only = format_is_audio_only(fmt)
        height = item.get("height")
        item["locked"] = not can_download_format(
            None if audio_only else height, audio_only=audio_only
        )
        annotated.append(item)
    return annotated


@dataclass(frozen=True)
class FormatVerdict:
    """Server-verified truth about a client-requested format.

    ``known`` is False when the requested ``format_id`` does not appear in the
    server's own yt-dlp extraction. Callers must treat that as a rejection:
    falling back to "just give me your best" would silently return something
    other than what was asked for.
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

    Clients cannot request a quality that does not exist: the server resolves
    the request against yt-dlp's extracted formats and reports what it found.
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
