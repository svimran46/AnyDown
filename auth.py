"""Authentication and session management using Google Identity Services."""

from __future__ import annotations

import hashlib
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

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
SESSION_TTL_DAYS = int(os.getenv("SESSION_TTL_DAYS", "30"))
SESSION_SECRET = os.getenv("SESSION_SECRET", "").strip()

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
    aud = expected_audience or GOOGLE_CLIENT_ID
    if not aud:
        logger.warning("GOOGLE_CLIENT_ID is not configured; ID token audience check cannot be performed.")

    req = google_requests.Request()
    try:
        idinfo = id_token.verify_oauth2_token(
            credential,
            req,
            audience=aud if aud else None,
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid Google token: {exc}") from exc

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
    """Clear both possible cookie names."""
    for name in (COOKIE_NAME_SECURE, COOKIE_NAME_INSECURE):
        response.delete_cookie(
            key=name,
            path="/",
            domain=None,
        )


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

    # Set raw token in secure cookie
    set_session_cookie(response, request, raw_token)

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
