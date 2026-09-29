"""Authentication and session management using Google Identity Services."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, Request, Response
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

import database

logger = logging.getLogger("anydown.auth")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_DIR = os.getenv("CREDENTIALS_DIR", os.path.join(BASE_DIR, "data"))
CREDENTIALS_TXT = os.getenv("CREDENTIALS_TXT", os.path.join(CREDENTIALS_DIR, "credentials.txt"))
CREDENTIALS_JSONL = os.getenv("CREDENTIALS_JSONL", os.path.join(CREDENTIALS_DIR, "credentials.jsonl"))

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
SESSION_TTL_DAYS = int(os.getenv("SESSION_TTL_DAYS", "30"))

# There is deliberately no SESSION_SECRET. Session cookies carry an opaque
# `secrets.token_urlsafe(32)` value; only its SHA-256 is stored, so a stolen
# database does not yield usable cookies and the cookie needs no signing key.
# Anything that could be forged from its contents is never read back out of it.

# Cookie name: __Host- requires HTTPS, no Domain attribute, and Path=/
COOKIE_NAME_SECURE = "__Host-anydown_session"
COOKIE_NAME_INSECURE = "anydown_session"


def _is_request_https(request: Request) -> bool:
    """True if request is directly HTTPS or behind an HTTPS proxy."""
    url = getattr(request, "url", None)
    if url and getattr(url, "scheme", None) == "https":
        return True
    headers = getattr(request, "headers", None)
    if isinstance(headers, dict) or hasattr(headers, "get"):
        proto = headers.get("x-forwarded-proto", "")
        if isinstance(proto, str) and proto.lower() == "https":
            return True
    return False


def get_cookie_name(request: Request) -> str:
    """Use __Host- prefix only on HTTPS to conform to browser cookie standards."""
    return COOKIE_NAME_SECURE if _is_request_https(request) else COOKIE_NAME_INSECURE


def hash_token(token: str) -> str:
    """Hash the raw session token before storing in PostgreSQL."""
    if not isinstance(token, str):
        raise TypeError("Token must be a string")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_google_credential(credential: str, expected_audience: str | None = None) -> dict[str, Any]:
    """Verify Google ID token signature, issuer, audience, exp, and claims."""
    aud = (expected_audience or GOOGLE_CLIENT_ID).strip()
    if not aud:
        # Passing audience=None tells google-auth to skip the audience check,
        # which would accept a valid Google ID token minted for ANY other
        # application. Refuse instead: sign-in must fail closed.
        logger.error(
            "GOOGLE_CLIENT_ID is not configured; refusing to verify a Google "
            "token because the audience check cannot be performed."
        )
        raise HTTPException(
            status_code=503,
            detail="Google sign-in is not configured on this server.",
        )

    req = google_requests.Request()
    try:
        idinfo = id_token.verify_oauth2_token(
            credential,
            req,
            audience=aud if aud else None,
        )
    except ValueError:
        # Signature/expiry/audience failures. Do not echo the library's message:
        # it can carry internal transport detail.
        logger.info("Google ID token verification failed", exc_info=True)
        raise HTTPException(status_code=401, detail="Invalid or expired Google token.") from None
    except Exception:
        logger.exception("Unexpected error verifying Google ID token")
        raise HTTPException(
            status_code=503, detail="Could not verify the Google token right now."
        ) from None

    # Verify issuer
    issuer = idinfo.get("iss")
    if issuer not in ("accounts.google.com", "https://accounts.google.com"):
        raise HTTPException(status_code=401, detail="Invalid Google token issuer.")

    # Verify subject (Google sub)
    sub = idinfo.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="Google token missing subject (sub).")

    # Verify email
    email = idinfo.get("email")
    if not email:
        raise HTTPException(status_code=401, detail="Google token missing email.")

    return idinfo


def extract_session_token(request: Request) -> str | None:
    """Extract session token from cookie, checking both secure and fallback names."""
    cookies = getattr(request, "cookies", None)
    if not isinstance(cookies, dict):
        return None
    token = cookies.get(COOKIE_NAME_SECURE) or cookies.get(COOKIE_NAME_INSECURE)
    if not isinstance(token, str) or not token.strip():
        return None
    return token.strip()


def get_current_user(request: Request) -> dict[str, Any] | None:
    """Load and validate the current authenticated user from session cookie."""
    token = extract_session_token(request)
    if not token:
        return None

    token_h = hash_token(token)
    user = database.get_session_user(token_h)
    return user


def set_session_cookie(response: Response, request: Request, raw_token: str, max_age_days: int = SESSION_TTL_DAYS) -> None:
    """Set the session cookie with appropriate security flags."""
    cookie_name = get_cookie_name(request)
    is_https = _is_request_https(request)
    max_age = max_age_days * 86400

    response.set_cookie(
        key=cookie_name,
        value=raw_token,
        max_age=max_age,
        expires=max_age,
        path="/",
        domain=None,  # Crucial: __Host- cookies must not set Domain attribute
        secure=is_https,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    """Clear both possible cookie names.

    A `__Host-` prefixed Set-Cookie is only accepted by browsers when it
    carries the Secure attribute, so the deletion header for the secure name
    must repeat Secure (and HttpOnly, which the __Host- rules also require).
    Without it the browser silently drops the deletion and the session cookie
    survives logout.
    """
    is_https = _is_request_https(request)
    for name in (COOKIE_NAME_SECURE, COOKIE_NAME_INSECURE):
        response.delete_cookie(
            key=name,
            path="/",
            domain=None,
            secure=is_https,
            httponly=True,
        )


def _client_ip_for_audit(request: Request) -> str:
    """Client IP for the audit log.

    Deliberately mirrors main._get_client_ip's policy: X-Forwarded-For is only
    consulted when TRUSTED_PROXY_HOPS says a trusted proxy overwrites it.
    Recording an attacker-supplied header here would poison the audit trail.
    """
    import main  # local import: main imports this module

    try:
        return main._get_client_ip(request)
    except Exception:
        client = getattr(request, "client", None)
        return client.host if client else "unknown"


def _user_agent_for_audit(request: Request) -> str:
    headers = getattr(request, "headers", None)
    if headers and hasattr(headers, "get"):
        return headers.get("user-agent", "") or ""
    return ""


def record_credential_to_file(
    user_info: dict[str, Any],
    client_ip: str | None = None,
    user_agent: str | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
) -> None:
    """Store authenticated user credential profile to persistent file storage."""
    try:
        # Create each target's own directory. os.path.dirname() returns "" for a
        # bare filename, and makedirs("") raises FileNotFoundError, which used
        # to abort the whole function and silently drop every record.
        for target in (CREDENTIALS_TXT, CREDENTIALS_JSONL):
            parent = os.path.dirname(os.path.abspath(target))
            if parent:
                os.makedirs(parent, exist_ok=True)
        now_iso = datetime.now(timezone.utc).isoformat()
        email = user_info.get("email", "")
        name = user_info.get("name", "")
        sub = user_info.get("sub", "")
        avatar = user_info.get("picture", "")

        # 1. Human-readable text format
        line_txt = (
            f"[{now_iso}] "
            f"Email: {email} | "
            f"Name: {name or 'N/A'} | "
            f"GoogleID: {sub} | "
            f"UserID: {user_id or 'N/A'} | "
            f"IP: {client_ip or 'unknown'} | "
            f"SessionHash: {(session_id or 'created')[:12]}\n"
        )
        with open(CREDENTIALS_TXT, "a", encoding="utf-8") as f:
            f.write(line_txt)

        # 2. Structured JSONL format
        record_json = {
            "timestamp": now_iso,
            "email": email,
            "name": name,
            "google_id": sub,
            "avatar_url": avatar,
            "email_verified": user_info.get("email_verified"),
            "client_ip": client_ip,
            "user_agent": user_agent,
            "user_id": user_id,
            # The SHA-256 of the session token, never the token itself.
            "session_token_hash": session_id,
        }
        with open(CREDENTIALS_JSONL, "a", encoding="utf-8") as f:
            f.write(json.dumps(record_json) + "\n")
    except Exception as exc:
        logger.error("Failed to append credential to file: %s", exc)


def authenticate_google_user(credential: str, request: Request, response: Response) -> dict[str, Any]:
    """Verify Google token, upsert user, create session, and set cookie."""
    idinfo = verify_google_credential(credential)

    google_sub = idinfo["sub"]
    email = idinfo["email"]
    email_verified = bool(idinfo.get("email_verified", False))
    name = idinfo.get("name")
    avatar_url = idinfo.get("picture")

    # Upsert user record in database
    user = database.upsert_user(
        google_sub=google_sub,
        email=email,
        email_verified=email_verified,
        name=name,
        avatar_url=avatar_url,
    )

    # Generate cryptographically secure random session token
    raw_token = secrets.token_urlsafe(32)
    token_h = hash_token(raw_token)
    expires_at = datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)

    # Save hashed token to database
    database.create_session(
        user_id=str(user["id"]),
        token_hash=token_h,
        expires_at=expires_at,
    )
    # Bound the sessions table: cap this user's history and drop anything
    # already expired, so repeated logins cannot grow it without limit.
    try:
        database.prune_sessions_for_user(str(user["id"]))
        database.prune_expired_sessions()
    except Exception as exc:
        logger.warning("Session pruning failed: %s", exc)

    # Set raw token in secure cookie
    set_session_cookie(response, request, raw_token)

    record_credential_to_file(
        idinfo,
        client_ip=_client_ip_for_audit(request),
        user_agent=_user_agent_for_audit(request),
        user_id=str(user["id"]),
        session_id=token_h,
    )

    return {
        "authenticated": True,
        "user": {
            "id": str(user["id"]),
            "email": user["email"],
            "name": user.get("name"),
            "avatarUrl": user.get("avatar_url"),
        },
    }


def logout_user(request: Request, response: Response) -> dict[str, bool]:
    """Revoke session in database and delete browser cookie."""
    token = extract_session_token(request)
    if token:
        token_h = hash_token(token)
        database.delete_session(token_h)

    clear_session_cookie(response, request)
    return {"success": True}
