"""yt-dlp based media extraction/downloading for AnyDown."""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import shutil
import socket
import threading
import urllib.request
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


# Hard cap on download size (bytes). 0 disables the cap. This is enforced two
# ways: yt-dlp's own max_filesize option, and filtering oversized formats out
# of /api/info so users aren't offered qualities they cannot download.
MAX_FILESIZE_BYTES = int(os.getenv("MAX_FILESIZE_BYTES", str(2 * 1024 * 1024 * 1024)))


def _is_oversized(fmt: dict) -> bool:
    """True if this format's size is known and exceeds the configured cap."""
    if MAX_FILESIZE_BYTES <= 0:
        return False
    filesize = fmt.get("filesize") or fmt.get("filesize_approx")
    return bool(filesize) and filesize > MAX_FILESIZE_BYTES


# --------------------------------------------------------------------------
# SSRF guard at the socket layer
#
# Validating a URL's DNS before handing it to yt-dlp cannot stop DNS
# rebinding: the attacker simply returns a public IP for the first resolution
# and a private one for yt-dlp's later resolutions. Instead, we patch
# socket.create_connection so every *actual* connection is resolved-and-
# checked as one atomic step, then connected by validated IP.
#
# Exemptions are limited to server-configured peers (never attacker
# controlled): the bgutil PO-token provider (local loopback or a private
# service name like http://bgutil-provider:4416) and the configured Facebook
# proxy. Everything a user submits goes through the strict path.
# --------------------------------------------------------------------------

class BlockedAddressError(RuntimeError):
    """A connection target resolved to a disallowed address."""


_ssrf_guard_lock = threading.Lock()
_ssrf_guard_installed = False


def _assert_routable(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    ):
        raise BlockedAddressError("Private or local network addresses are not supported.")


def _resolve_host(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    infos = socket.getaddrinfo(host, None)
    resolved: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for family, _socktype, _proto, _canonname, sockaddr in infos:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        ip = ipaddress.ip_address(sockaddr[0])
        if ip not in resolved:
            resolved.append(ip)
    return resolved


def _socket_connect(
    address: tuple,
    timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
    source_address=None,
):
    host = str(address[0])
    try:
        ip = ipaddress.ip_address(host)
        family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    except ValueError:
        family = socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
            sock.settimeout(timeout)
        if source_address:
            sock.bind(source_address)
        sock.connect(address)
        return sock
    except OSError:
        sock.close()
        raise


def _connect_protected(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
    host = str(address[0])
    port = address[1] if len(address) > 1 and isinstance(address[1], int) else None

    # 1) Server-configured peers pass through on their configured port.
    if _is_exempt_target(host, port):
        return socket.create_original_connection(address, timeout, source_address)

    # 2) IP literals: validate directly against SSRF blocklist (never loopback/private).
    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        _assert_routable(literal_ip)
        return socket.create_original_connection(address, timeout, source_address)

    # 3) Hostnames: atomic resolve-validate-connect per address. Connecting
    #    to the validated IP (not re-resolving inside the OS) closes the
    #    DNS-rebinding race.
    resolved = _resolve_host(host)
    last_err: OSError | None = None
    for ip in resolved:
        _assert_routable(ip)
        try:
            return _socket_connect((str(ip), *address[1:]), timeout, source_address)
        except OSError as exc:
            last_err = exc
    raise last_err if last_err else OSError(f"Could not resolve {host!r}")


def _install_ssrf_guard() -> None:
    """Patch socket.create_connection and urllib3 once (thread-safe, idempotent)."""
    global _ssrf_guard_installed
    with _ssrf_guard_lock:
        if _ssrf_guard_installed:
            return
        if not hasattr(socket, "create_original_connection"):
            socket.create_original_connection = socket.create_connection

        def guarded_create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                                      source_address=None, **kwargs):
            try:
                return _connect_protected(address, timeout, source_address)
            except BlockedAddressError as err:
                raise yt_dlp.utils.DownloadError(str(err)) from err

        socket.create_connection = guarded_create_connection

        try:
            import urllib3.util.connection as urllib3_conn
            if not hasattr(urllib3_conn, "create_original_connection"):
                urllib3_conn.create_original_connection = urllib3_conn.create_connection
                urllib3_conn.create_connection = guarded_create_connection
        except (ImportError, AttributeError):
            pass

        _ssrf_guard_installed = True


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

# Default YouTube player-client fallback chain, tuned for datacenter IPs
# where YouTube's bot checks are most aggressive. See _youtube_clients().
DEFAULT_YOUTUBE_CLIENTS = ("mweb", "tv", "")

# App version, surfaced in /api/health and in YouTube error messages so a
# deployment running stale code is identifiable at a glance: if the version
# reported there does not match the latest commit on main, the service is
# running an older image and must be redeployed (Render: "Clear build cache
# & deploy" — Docker layer caching can otherwise serve a stale image).
APP_VERSION = "2.1.1"


# Loopback peers are always exempt: the bgutil provider runs on 127.0.0.1 in
# the supported Docker deployment (and the plugin may use that default even
def _is_exempt_target(host: str, port: int | None) -> bool:
    """True if (host, port) matches a server-configured peer (e.g. POT provider, proxy)."""
    host_clean = host.lower().rstrip(".")
    for url in (YTDLP_POT_PROVIDER_URL, FACEBOOK_PROXY_URL):
        if not url:
            continue
        try:
            parsed = urlparse(url)
            target_host = (parsed.hostname or "").lower().rstrip(".")
            if not target_host:
                continue
            target_port = parsed.port or (443 if parsed.scheme == "https" else 80)

            is_same_host = (host_clean == target_host)
            if not is_same_host and target_host in {"127.0.0.1", "localhost", "::1", "localhost.localdomain"}:
                is_same_host = host_clean in {"127.0.0.1", "localhost", "::1", "localhost.localdomain"}

            if is_same_host and (port is None or port == target_port):
                return True
        except Exception:
            continue
    return False


def _config_exempt_hosts() -> set[str]:
    """Hostnames the app is *designed* to reach, from server config only."""
    hosts = set()
    for url in (YTDLP_POT_PROVIDER_URL, FACEBOOK_PROXY_URL):
        if url:
            try:
                host = (urlparse(url).hostname or "").lower().rstrip(".")
                if host:
                    hosts.add(host)
            except Exception:
                pass
    return hosts


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().rstrip(".")


def is_youtube(url: str) -> bool:
    host = _host(url)
    return host in _YOUTUBE_HOSTS or host.endswith(".youtube.com")


def is_facebook(url: str) -> bool:
    host = _host(url)
    return host in _FACEBOOK_HOSTS or host.endswith(".facebook.com")


# --------------------------------------------------------------------------
# YouTube diagnostics helpers
#
# These report what the YouTube path will *actually* do at request time
# (cookies file present and parseable? POT provider reachable? JS runtime
# available?) so /api/health and user-facing errors reflect reality instead
# of echoing environment variables.
# --------------------------------------------------------------------------


def youtube_cookies_status() -> dict:
    """Inspect the configured cookies file without exposing its contents."""
    if not YOUTUBE_COOKIES_FILE:
        return {"configured": False, "file_found": None, "format": None}
    found = os.path.isfile(YOUTUBE_COOKIES_FILE)
    fmt: str | None = None
    if found:
        try:
            with open(YOUTUBE_COOKIES_FILE, encoding="utf-8", errors="replace") as fh:
                first_line = fh.readline(512).lstrip()
            # Browser JSON exports start with '{' or '['; Netscape files start
            # with '# Netscape ...' (or are empty).
            fmt = "json" if first_line[:1] in ("{", "[") else "netscape"
        except OSError:
            fmt = None
    return {"configured": True, "file_found": found, "format": fmt}


def pot_provider_status(timeout: float = 2.0) -> dict:
    """Ping the configured bgutil provider to see if it is actually running."""
    if not YTDLP_POT_PROVIDER_URL:
        return {"configured": False, "reachable": None}
    ping_url = YTDLP_POT_PROVIDER_URL.rstrip("/") + "/ping"
    try:
        with urllib.request.urlopen(ping_url, timeout=timeout) as resp:
            reachable = 200 <= resp.status < 300
    except Exception:
        reachable = False
    return {"configured": True, "reachable": reachable}


_js_runtime_cache: dict[str, str] = {}


def detect_js_runtime() -> str:
    """Find a JS runtime yt-dlp's EJS support can use; "" when none exists."""
    if "runtime" not in _js_runtime_cache:
        found = ""
        for name in ("node", "deno", "bun", "quickjs"):
            if shutil.which(name):
                found = name
                break
        _js_runtime_cache["runtime"] = found
    return _js_runtime_cache["runtime"]


def _sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "_", name).strip()
    name = name.rstrip(". ")
    base = name.split(".")[0].upper()
    if base in {"CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4",
                "COM5", "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2",
                "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"}:
        name = f"_{name}"
    return name[:150] if name else "download"


def _find_downloaded_file(output_dir: str, job_id: str, info: dict | None = None) -> str | None:
    """Find downloaded file by prepare_filename, metadata attributes, or directory scan fallback."""
    ignored_exts = (".part", ".ytdl", ".temp", ".tmp", ".aria2")

    if info:
        # 1. Direct filepath attributes yt-dlp may have added
        for attr in ("filepath", "_filename"):
            val = info.get(attr)
            if val and os.path.exists(val) and not val.endswith(ignored_exts):
                return val

        # 2. requested_downloads list (populated when postprocessors run, e.g. FFmpegExtractAudio)
        for req in info.get("requested_downloads") or []:
            if isinstance(req, dict):
                req_path = req.get("filepath") or req.get("_filename")
                if req_path and os.path.exists(req_path) and not req_path.endswith(ignored_exts):
                    return req_path

        # 3. prepare_filename
        try:
            with yt_dlp.YoutubeDL({"outtmpl": os.path.join(output_dir, f"{job_id}.%(ext)s")}) as ydl:
                prepared_path = ydl.prepare_filename(info)
                if os.path.exists(prepared_path) and not prepared_path.endswith(ignored_exts):
                    return prepared_path
        except Exception:
            pass

    # 4. Check common media extensions directly
    base_prefix = os.path.join(output_dir, job_id)
    for ext in (".mp4", ".mp3", ".m4a", ".webm", ".mkv", ".opus", ".ogg", ".wav", ".flac", ".aac"):
        candidate = base_prefix + ext
        if os.path.exists(candidate):
            return candidate

    # 5. Fallback: directory scan excluding temporary / partial files
    candidates = []
    expected_base = job_id + "."
    for fname in os.listdir(output_dir):
        if fname.startswith(expected_base) and not fname.endswith(ignored_exts):
            candidates.append(os.path.join(output_dir, fname))

    if not candidates:
        return None

    # Prefer exact matches over mtime heuristic
    return max(candidates, key=os.path.getmtime)


def _youtube_options(ydl_opts: dict, client: str | None = None) -> None:
    """Configure yt-dlp's current YouTube helpers for one attempt.

    bgutil is installed as the official yt-dlp plugin.  The HTTP provider is
    only needed for clients that use PO tokens (notably mweb).  Keep the
    provider available, but don't force mweb for every YouTube request: yt-dlp
    documents several clients with different requirements and limitations.

    `client` is the explicit player client for THIS attempt ("" or None
    defers to yt-dlp's own selection). Client ordering/pinning lives in
    _youtube_clients(); this function never re-reads YOUTUBE_PRIMARY_CLIENT,
    so the final defaults attempt stays genuinely unpinned.
    """
    extractor_args = ydl_opts.setdefault("extractor_args", {})

    if client:
        diagnostic_logger.info("[YOUTUBE] Using player client: %s", client)
        extractor_args["youtube"] = {"player_client": [client]}

    if YTDLP_POT_PROVIDER_URL:
        extractor_args["youtubepot-bgutilhttp"] = {
            "base_url": [YTDLP_POT_PROVIDER_URL]
        }

    # Cookies are optional and MUST be supplied as a server-side secret file.
    cookies = youtube_cookies_status()
    if cookies["configured"]:
        if not cookies["file_found"]:
            # A stale/mis-mounted secret path must not take down every
            # YouTube request: continue without cookies and say so loudly.
            diagnostic_logger.warning(
                "[COOKIES] YOUTUBE_COOKIES_FILE is configured but the file does "
                "not exist: %s. Continuing without cookies; fix the path or "
                "unset the variable.",
                YOUTUBE_COOKIES_FILE,
            )
        elif cookies["format"] == "json":
            # A JSON cookie export can never work; fail with the fix instead
            # of letting yt-dlp emit a cryptic formatting error.
            raise UnsupportedURLError(
                "YOUTUBE_COOKIES_FILE points at a JSON cookie export. yt-dlp "
                "requires Netscape-format cookies.txt (re-export with a "
                "'cookies.txt' browser extension, not the browser's JSON "
                "cookies database)."
            )
        else:
            ydl_opts["cookiefile"] = YOUTUBE_COOKIES_FILE

    # yt-dlp's current YouTube support uses EJS + a JS runtime. Deno is the
    # only runtime enabled by default; others must be explicitly enabled.
    if "js_runtimes" not in ydl_opts:
        runtime = os.getenv("YTDLP_JS_RUNTIME", "").strip() or detect_js_runtime()
        if runtime:
            ydl_opts["js_runtimes"] = {runtime: {}}
        else:
            diagnostic_logger.warning(
                "[YOUTUBE] No JavaScript runtime found (node/deno/bun/quickjs); "
                "some YouTube formats may be missing. Deploy via the Dockerfile "
                "or install a runtime."
            )
            ydl_opts["remote_components"] = ["ejs:github"]


def _is_youtube_retryable_error(error: Exception) -> bool:
    """Return True for failures where another YouTube client is worth trying."""
    text = str(error).lower().replace("’", "'")
    markers = (
        "sign in to confirm you're not a bot",
        "confirm you're not a bot",
        "not a bot",
        "login_required",
        "http error 403",
        "http error 429",
        "forbidden",
        "temporarily blocked",
        "requested format is not available",
    )
    return any(marker in text for marker in markers)


def _youtube_clients() -> list[str]:
    """Return a configurable YouTube player-client fallback chain.

    Client viability changes with every yt-dlp release (see the "Sign in to
    confirm you're not a bot" era): hard-coding a stale chain makes the app
    fail even when yt-dlp's own defaults would work.

    The default chain is tuned for datacenter IPs (Render et al.), where
    YouTube most aggressively flags automated traffic:

    1. "mweb" — yt-dlp's officially recommended client when an IP is
       flagged. It requires a GVS PO token, which the bgutil provider
       supplies (verified working end-to-end on a datacenter IP).
    2. "tv" — needs no PO token and is usually not bot-checked, at the
       cost of some DRM-protected formats without cookies.
    3. "" — defer to yt-dlp's own currently-supported client selection
       (visionos + web as of 2026.08). Kept as the final attempt so this
       app tracks yt-dlp's future client changes without a code change;
       it also handles cookies/authenticated defaults correctly.

    YOUTUBE_PRIMARY_CLIENT puts one client first without dropping the rest;
    YOUTUBE_CLIENTS forces a specific chain (e.g. "mweb,tv"). In both cases
    the entries above are only used when those variables are unset, and a
    forced chain always keeps the empty client — yt-dlp's own defaults —
    as the final attempt, so a pinned primary can never remove the escape
    hatch.
    """
    clients: list[str] = []
    primary = os.getenv("YOUTUBE_PRIMARY_CLIENT", "").strip()
    if primary:
        clients.append(primary)
    for item in os.getenv("YOUTUBE_CLIENTS", "").strip().split(","):
        client = item.strip()
        if client and client not in clients:
            clients.append(client)
    if not clients:
        clients = list(DEFAULT_YOUTUBE_CLIENTS)
    if "" not in clients:
        # "" = let yt-dlp pick its own defaults. Always the final attempt.
        clients.append("")
    return clients


def _client_label(client: str) -> str:
    """Human-readable name for a chain entry ("" = yt-dlp's own defaults)."""
    return client if client else "yt-dlp-defaults"


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
                _client_label(client),
            )

        diagnostic_logger.info(
            "[YOUTUBE] Attempt %d/%d using client=%s",
            index + 1,
            len(clients),
            _client_label(client),
        )
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=download)
            diagnostic_logger.info(
                "[YOUTUBE] Success using client=%s", _client_label(client)
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

    if MAX_FILESIZE_BYTES > 0:
        # Abort at yt-dlp level if the file is larger than the configured cap.
        opts["max_filesize"] = MAX_FILESIZE_BYTES

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
    lower = text.lower().replace("’", "'")

    if "larger than max-filesize" in lower or "max-filesize" in lower or "max_filesize" in lower:
        limit_gb = MAX_FILESIZE_BYTES / (1024 * 1024 * 1024)
        return (
            f"This file is larger than the server's size limit "
            f"({limit_gb:.1f} GB). Try a lower quality."
        )

    if "private or local network addresses are not supported" in lower:
        return "That URL points at a private or local network address and is not allowed."

    if "sign in to confirm you're not a bot" in lower or "confirm you're not a bot" in lower or "not a bot" in lower:
        # Report the actual server state instead of blaming cookies by default.
        cookies = youtube_cookies_status()
        pot = pot_provider_status()
        state = []
        if pot["configured"]:
            state.append(
                "bgutil PO-token provider is configured"
                + (" and reachable" if pot["reachable"] else " but is NOT reachable")
            )
        else:
            state.append("no PO-token provider is configured (YTDLP_POT_PROVIDER_URL)")
        if not cookies["configured"]:
            state.append("no YouTube cookies are configured (YOUTUBE_COOKIES_FILE)")
        elif not cookies["file_found"]:
            state.append(f"the configured cookies file is missing: {YOUTUBE_COOKIES_FILE}")
        elif cookies["format"] == "json":
            state.append("the configured cookies file is a JSON export, which yt-dlp cannot use")
        else:
            state.append(
                "a cookies file is configured (expired cookies are silently "
                "ignored by yt-dlp — re-export if they are old)"
            )
        diagnostic_logger.error(
            "[YOUTUBE] Bot check failed on every client attempt. Server state: %s",
            "; ".join(state),
        )
        return (
            f"[AnyDown {APP_VERSION}] YouTube flagged this server's traffic as "
            "automated. Switching clients and PO tokens often cannot clear this "
            "on datacenter IPs; an authenticated YouTube cookies file (or a "
            "cleaner server IP) usually can. Server state: " + "; ".join(state) + "."
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
    _install_ssrf_guard()
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

        # Don't offer qualities the server refuses to download.
        if _is_oversized(f):
            continue

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
    _install_ssrf_guard()
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
