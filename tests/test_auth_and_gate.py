"""Comprehensive tests for Google Authentication, Session Management,
and Quality Access Gating in AnyDown.

Run: python -m unittest tests.test_auth_and_gate -v
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import auth
import authorization
import database
import main
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response


class AuthAndGateBaseTestCase(unittest.TestCase):
    """Sets up an isolated temporary SQLite database for each test run."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="anydown-auth-test-")
        self.db_path = os.path.join(self.temp_dir, "test_anydown.db")
        os.environ["SQLITE_DB_PATH"] = self.db_path
        os.environ["GOOGLE_CLIENT_ID"] = "test-client-id.apps.googleusercontent.com"
        os.environ["GUEST_MAX_HEIGHT"] = "720"
        auth.GOOGLE_CLIENT_ID = "test-client-id.apps.googleusercontent.com"
        authorization.GUEST_MAX_HEIGHT = 720

        # Initialize tables
        database.init_db()

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_mock_request(
        self,
        cookies: dict[str, str] | None = None,
        is_https: bool = False,
        client_ip: str = "203.0.113.1",
    ) -> MagicMock:
        mock_req = MagicMock(spec=Request)
        mock_req.cookies = cookies or {}
        mock_req.client = MagicMock()
        mock_req.client.host = client_ip
        mock_req.headers = {
            "x-forwarded-proto": "https" if is_https else "http",
        }
        mock_req.url = MagicMock()
        mock_req.url.scheme = "https" if is_https else "http"
        return mock_req


class GoogleTokenVerificationTests(AuthAndGateBaseTestCase):
    """Test verification of Google ID tokens and claim validation."""

    @patch("google.oauth2.id_token.verify_oauth2_token")
    def test_verify_valid_google_token(self, mock_verify):
        mock_verify.return_value = {
            "iss": "accounts.google.com",
            "sub": "google-sub-12345",
            "email": "user@example.com",
            "email_verified": True,
            "name": "Test User",
            "picture": "https://example.com/avatar.jpg",
        }

        claims = auth.verify_google_credential("valid-credential")
        self.assertEqual(claims["sub"], "google-sub-12345")
        self.assertEqual(claims["email"], "user@example.com")
        self.assertEqual(claims["name"], "Test User")

    @patch("google.oauth2.id_token.verify_oauth2_token", side_effect=ValueError("Token expired"))
    def test_verify_expired_google_token_raises_401(self, mock_verify):
        with self.assertRaises(HTTPException) as ctx:
            auth.verify_google_credential("expired-credential")
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn("Invalid Google token", ctx.exception.detail)

    @patch("google.oauth2.id_token.verify_oauth2_token")
    def test_verify_invalid_issuer_raises_401(self, mock_verify):
        mock_verify.return_value = {
            "iss": "https://malicious-issuer.com",
            "sub": "sub-123",
            "email": "user@example.com",
        }
        with self.assertRaises(HTTPException) as ctx:
            auth.verify_google_credential("malicious-issuer-credential")
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn("Invalid Google token issuer", ctx.exception.detail)

    @patch("google.oauth2.id_token.verify_oauth2_token")
    def test_verify_missing_sub_raises_401(self, mock_verify):
        mock_verify.return_value = {
            "iss": "accounts.google.com",
            "email": "user@example.com",
        }
        with self.assertRaises(HTTPException) as ctx:
            auth.verify_google_credential("no-sub-credential")
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn("missing subject", ctx.exception.detail)

    @patch("google.oauth2.id_token.verify_oauth2_token")
    def test_verify_missing_email_raises_401(self, mock_verify):
        mock_verify.return_value = {
            "iss": "accounts.google.com",
            "sub": "sub-123",
        }
        with self.assertRaises(HTTPException) as ctx:
            auth.verify_google_credential("no-email-credential")
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn("missing email", ctx.exception.detail)


class UserUpsertAndSessionTests(AuthAndGateBaseTestCase):
    """Test first-time user creation, returning user logins, and session lifecycle."""

    @patch("google.oauth2.id_token.verify_oauth2_token")
    def test_first_time_user_creation(self, mock_verify):
        mock_verify.return_value = {
            "iss": "accounts.google.com",
            "sub": "first-time-user-sub",
            "email": "firsttime@example.com",
            "email_verified": True,
            "name": "First Timerson",
            "picture": "https://example.com/first.png",
        }
        request = self._create_mock_request(is_https=True)
        response = Response()

        result = auth.authenticate_google_user("first-time-cred", request, response)

        self.assertTrue(result["authenticated"])
        self.assertEqual(result["user"]["email"], "firsttime@example.com")
        self.assertEqual(result["user"]["name"], "First Timerson")

        # Database checks
        conn = database._get_connection()
        with conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM users WHERE google_sub = ?", ("first-time-user-sub",))
            user_row = cur.fetchone()
            self.assertIsNotNone(user_row)
            self.assertEqual(user_row["email"], "firsttime@example.com")

            # Check that raw token was NOT stored, only hash
            cur.execute("SELECT * FROM sessions WHERE user_id = ?", (user_row["id"],))
            session_rows = cur.fetchall()
            self.assertEqual(len(session_rows), 1)
            token_hash = session_rows[0]["token_hash"]
            self.assertEqual(len(token_hash), 64)  # SHA-256 hex is 64 chars

        # Check response cookie
        set_cookie_header = response.headers.get("set-cookie")
        self.assertIsNotNone(set_cookie_header)
        self.assertIn(auth.COOKIE_NAME_SECURE, set_cookie_header)
        self.assertIn("HttpOnly", set_cookie_header)
        self.assertIn("Secure", set_cookie_header)
        self.assertIn("SameSite=lax", set_cookie_header)

    @patch("google.oauth2.id_token.verify_oauth2_token")
    def test_returning_user_login_updates_timestamp(self, mock_verify):
        mock_verify.return_value = {
            "iss": "accounts.google.com",
            "sub": "returning-user-sub",
            "email": "returning@example.com",
            "email_verified": True,
            "name": "Original Name",
        }
        request = self._create_mock_request(is_https=False)
        response1 = Response()
        auth.authenticate_google_user("first-login", request, response1)

        # Login again with updated name
        mock_verify.return_value["name"] = "Updated Name"
        response2 = Response()
        result2 = auth.authenticate_google_user("second-login", request, response2)

        self.assertEqual(result2["user"]["name"], "Updated Name")

        conn = database._get_connection()
        with conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) AS c FROM users WHERE google_sub = ?", ("returning-user-sub",))
            count = cur.fetchone()["c"]
            self.assertEqual(count, 1)  # No duplicate rows created

    def test_expired_session_returns_none(self):
        user = database.upsert_user("sub-exp", "exp@example.com", True, "Expired User", None)
        raw_token = "raw-expired-session-token"
        token_h = auth.hash_token(raw_token)
        past_time = datetime.now(timezone.utc) - timedelta(days=2)

        database.create_session(str(user["id"]), token_h, past_time)

        request = self._create_mock_request(cookies={auth.COOKIE_NAME_INSECURE: raw_token})
        current_user = auth.get_current_user(request)
        self.assertIsNone(current_user)

    def test_tampered_or_invalid_session_returns_none(self):
        request = self._create_mock_request(cookies={auth.COOKIE_NAME_INSECURE: "fake-random-token"})
        current_user = auth.get_current_user(request)
        self.assertIsNone(current_user)

    def test_logout_revokes_session_and_clears_cookie(self):
        user = database.upsert_user("sub-logout", "logout@example.com", True, "Logout User", None)
        raw_token = "raw-logout-token"
        token_h = auth.hash_token(raw_token)
        future_time = datetime.now(timezone.utc) + timedelta(days=1)

        database.create_session(str(user["id"]), token_h, future_time)

        request = self._create_mock_request(cookies={auth.COOKIE_NAME_INSECURE: raw_token})
        response = Response()

        result = auth.logout_user(request, response)
        self.assertTrue(result["success"])

        # DB session should be removed
        conn = database._get_connection()
        with conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM sessions WHERE token_hash = ?", (token_h,))
            self.assertIsNone(cur.fetchone())

        # Response should have cleared cookie
        set_cookie_header = response.headers.get("set-cookie", "")
        self.assertIn('max-age=0', set_cookie_header.lower())


class QualityAccessGatePolicyTests(unittest.TestCase):
    """Test centralized authorization rules for guest vs authenticated users."""

    def test_can_download_format_guest(self):
        # Audio-only is always allowed
        self.assertTrue(authorization.can_download_format(None, 0, audio_only=True))

        # Video at or below 720p is allowed
        self.assertTrue(authorization.can_download_format(None, 360, audio_only=False))
        self.assertTrue(authorization.can_download_format(None, 480, audio_only=False))
        self.assertTrue(authorization.can_download_format(None, 720, audio_only=False))

        # Video above 720p is locked for guests
        self.assertFalse(authorization.can_download_format(None, 1080, audio_only=False))
        self.assertFalse(authorization.can_download_format(None, 1440, audio_only=False))
        self.assertFalse(authorization.can_download_format(None, 2160, audio_only=False))

    def test_can_download_format_authenticated(self):
        mock_user = {"id": "user-uuid-1", "email": "test@example.com"}

        # Authenticated users can download any resolution
        self.assertTrue(authorization.can_download_format(mock_user, 360, audio_only=False))
        self.assertTrue(authorization.can_download_format(mock_user, 720, audio_only=False))
        self.assertTrue(authorization.can_download_format(mock_user, 1080, audio_only=False))
        self.assertTrue(authorization.can_download_format(mock_user, 1440, audio_only=False))
        self.assertTrue(authorization.can_download_format(mock_user, 2160, audio_only=False))

    def test_format_lock_annotations_for_guest(self):
        ladder = [
            {"height": 360, "format_id": "18"},
            {"height": 720, "format_id": "22"},
            {"height": 1080, "format_id": "137"},
            {"height": 2160, "format_id": "313"},
        ]
        annotated = authorization.annotate_formats_with_locks(ladder, user=None)
        self.assertFalse(annotated[0]["locked"])
        self.assertFalse(annotated[1]["locked"])
        self.assertTrue(annotated[2]["locked"])
        self.assertTrue(annotated[3]["locked"])

    def test_format_lock_annotations_for_authenticated(self):
        ladder = [
            {"height": 360, "format_id": "18"},
            {"height": 720, "format_id": "22"},
            {"height": 1080, "format_id": "137"},
            {"height": 2160, "format_id": "313"},
        ]
        mock_user = {"id": "user-uuid-1", "email": "test@example.com"}
        annotated = authorization.annotate_formats_with_locks(ladder, user=mock_user)
        self.assertFalse(annotated[0]["locked"])
        self.assertFalse(annotated[1]["locked"])
        self.assertFalse(annotated[2]["locked"])
        self.assertFalse(annotated[3]["locked"])


class DownloadGateEndpointEnforcementTests(AuthAndGateBaseTestCase):
    """Test start_download enforcement of actual height and bypass rejection."""

    def test_guest_downloading_720p_allowed(self):
        payload = main.DownloadRequest(
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            height=720,
            audio_only=False,
        )
        request = self._create_mock_request()

        with patch.object(main, "_throttle", return_value=None):
            with patch.object(main, "_validate_public_url", return_value=None):
                with patch.object(main, "_get_or_fetch_info", return_value={"formats": [{"format_id": "22", "height": 720}]}):
                    with patch.object(main, "_execute_download_job"):
                        res = asyncio.run(main.start_download(payload, request))
                        self.assertIn("job_id", res)
                        self.assertEqual(res["status"], "queued")

    def test_guest_downloading_1080p_rejected_with_401_login_required(self):
        import json
        payload = main.DownloadRequest(
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            height=1080,
            audio_only=False,
        )
        request = self._create_mock_request()

        with patch.object(main, "_throttle", return_value=None):
            with patch.object(main, "_validate_public_url", return_value=None):
                res = asyncio.run(main.start_download(payload, request))
                self.assertEqual(res.status_code, 401)
                detail = json.loads(res.body.decode("utf-8"))
                self.assertEqual(detail["error"], "LOGIN_REQUIRED")
                self.assertEqual(detail["requiredHeight"], 1080)
                self.assertIn("Sign in with Google", detail["message"])

    def test_client_fake_height_bypass_rejected(self):
        import json
        # Client maliciously sends height=720 or height=None, but format_id is a 1080p stream
        payload = main.DownloadRequest(
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            format_id="137",
            height=720,  # Claiming 720p
            audio_only=False,
        )
        request = self._create_mock_request()

        # Mock media info where format 137 has height 1080
        fake_info = {
            "formats": [
                {"format_id": "18", "height": 360},
                {"format_id": "22", "height": 720},
                {"format_id": "137", "height": 1080},
            ]
        }

        with patch.object(main, "_throttle", return_value=None):
            with patch.object(main, "_validate_public_url", return_value=None):
                with patch.object(main, "_get_or_fetch_info", return_value=fake_info):
                    res = asyncio.run(main.start_download(payload, request))
                    self.assertEqual(res.status_code, 401)
                    detail = json.loads(res.body.decode("utf-8"))
                    self.assertEqual(detail["error"], "LOGIN_REQUIRED")
                    self.assertEqual(detail["requiredHeight"], 1080)

    def test_authenticated_user_downloading_1080p_allowed(self):
        # Create an authenticated user and valid session
        user = database.upsert_user("sub-premium", "premium@example.com", True, "VIP User", None)
        raw_token = "vip-session-token"
        token_h = auth.hash_token(raw_token)
        future_time = datetime.now(timezone.utc) + timedelta(days=1)
        database.create_session(str(user["id"]), token_h, future_time)

        payload = main.DownloadRequest(
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            height=1080,
            format_id="137",
            audio_only=False,
        )
        request = self._create_mock_request(cookies={auth.COOKIE_NAME_INSECURE: raw_token})

        fake_info = {
            "formats": [
                {"format_id": "137", "height": 1080},
            ]
        }

        with patch.object(main, "_throttle", return_value=None):
            with patch.object(main, "_validate_public_url", return_value=None):
                with patch.object(main, "_get_or_fetch_info", return_value=fake_info):
                    with patch.object(main, "_execute_download_job"):
                        res = asyncio.run(main.start_download(payload, request))
                        self.assertIn("job_id", res)
                        self.assertEqual(res["status"], "queued")


class AuthEndpointsDirectTests(AuthAndGateBaseTestCase):
    """Test auth API endpoints (/api/config, /api/auth/me, /api/auth/google, /api/auth/logout)."""

    def test_config_endpoint(self):
        cfg = main.get_config()
        self.assertEqual(cfg["googleClientId"], "test-client-id.apps.googleusercontent.com")
        self.assertEqual(cfg["guestMaxHeight"], 720)

    def test_auth_me_unauthenticated(self):
        request = self._create_mock_request()
        res = main.auth_me(request)
        self.assertFalse(res["authenticated"])
        self.assertIsNone(res["user"])

    def test_auth_me_authenticated(self):
        user = database.upsert_user("sub-me", "me@example.com", True, "Me User", "https://avatar.png")
        raw_token = "me-session-token"
        token_h = auth.hash_token(raw_token)
        future_time = datetime.now(timezone.utc) + timedelta(days=1)
        database.create_session(str(user["id"]), token_h, future_time)

        request = self._create_mock_request(cookies={auth.COOKIE_NAME_INSECURE: raw_token})
        res = main.auth_me(request)
        self.assertTrue(res["authenticated"])
        self.assertEqual(res["user"]["email"], "me@example.com")
        self.assertEqual(res["user"]["name"], "Me User")

    @patch("google.oauth2.id_token.verify_oauth2_token")
    def test_auth_google_endpoint(self, mock_verify):
        mock_verify.return_value = {
            "iss": "accounts.google.com",
            "sub": "sub-google-route",
            "email": "google-route@example.com",
            "email_verified": True,
            "name": "Google Route User",
            "picture": "https://example.com/photo.jpg",
        }
        request = self._create_mock_request()
        response = Response()
        body = main.GoogleAuthRequest(credential="mock-jwt-credential")

        res = asyncio.run(main.auth_google(body, request, response))
        self.assertTrue(res["authenticated"])
        self.assertEqual(res["user"]["email"], "google-route@example.com")


if __name__ == "__main__":
    unittest.main(verbosity=2)
