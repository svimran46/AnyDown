"""Comprehensive tests for Google Authentication, Session Management,
and Quality Access Gating in AnyDown.

Run: python -m unittest tests.test_auth_and_gate -v
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import auth
import authorization
import database
import downloader
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
        # The library's own message is not echoed back to the client.
        self.assertIn("Invalid", ctx.exception.detail)
        self.assertNotIn("Token expired", ctx.exception.detail)

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
        self.assertTrue(authorization.can_download_format(None, None, audio_only=True))

        # Video at or below 720p is allowed
        self.assertTrue(authorization.can_download_format(None, 360, audio_only=False))
        self.assertTrue(authorization.can_download_format(None, 480, audio_only=False))
        self.assertTrue(authorization.can_download_format(None, 720, audio_only=False))

        # Video above 720p is locked for guests
        self.assertFalse(authorization.can_download_format(None, 1080, audio_only=False))
        self.assertFalse(authorization.can_download_format(None, 1440, audio_only=False))
        self.assertFalse(authorization.can_download_format(None, 2160, audio_only=False))

    def test_unknown_video_height_is_denied(self):
        """A video with an unverifiable height must fail closed.

        Regression: the gate used to return True for height=None, which is what
        made "omit format_id" a complete bypass.
        """
        self.assertFalse(authorization.can_download_format(None, None, audio_only=False))
        self.assertFalse(authorization.can_download_format({"id": "u"}, None, audio_only=False))

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

    def test_audio_formats_are_not_locked_for_guests(self):
        """Audio-only formats have no height but must stay unlocked."""
        ladder = [
            {"format_id": "140", "height": None, "has_video": False, "has_audio": True},
            {"format_id": "251", "height": None, "has_video": False, "has_audio": True},
        ]
        annotated = authorization.annotate_formats_with_locks(ladder, user=None)
        self.assertFalse(annotated[0]["locked"])
        self.assertFalse(annotated[1]["locked"])

    def test_raw_vcodec_formats_are_recognised_as_audio(self):
        ladder = [
            {"format_id": "140", "height": None, "vcodec": "none", "acodec": "mp4a.40.5"},
            {"format_id": "137", "height": 1080, "vcodec": "avc1.640028", "acodec": "none"},
        ]
        annotated = authorization.annotate_formats_with_locks(ladder, user=None)
        self.assertFalse(annotated[0]["locked"])
        self.assertTrue(annotated[1]["locked"])

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

    _LADDER = {
        "formats": [
            {"format_id": "18", "height": 360, "has_video": True, "has_audio": True},
            {"format_id": "22", "height": 720, "has_video": True, "has_audio": True},
            {"format_id": "137", "height": 1080, "has_video": True, "has_audio": False},
            {"format_id": "313", "height": 2160, "has_video": True, "has_audio": False},
            {"format_id": "140", "height": None, "has_video": False, "has_audio": True},
        ]
    }

    def _run_download(self, payload, info=None, request=None):
        # `if request is None`, not `request or ...`: MagicMock(spec=Request)
        # is falsy, so `or` would silently swap an authenticated request for a
        # fresh guest one.
        if request is None:
            request = self._create_mock_request()
        with patch.object(main, "_throttle", return_value=None), \
             patch.object(main, "_validate_public_url", return_value=None), \
             patch.object(
                 main, "_get_or_fetch_info",
                 return_value=self._LADDER if info is None else info,
             ), \
             patch.object(main, "_execute_download_job") as run:
            res = asyncio.run(main.start_download(payload, request))
        return res, run

    def test_guest_downloading_720p_allowed(self):
        payload = main.DownloadRequest(
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            format_id="22",
            height=720,
            audio_only=False,
        )
        res, _ = self._run_download(payload)
        self.assertIn("job_id", res)
        self.assertEqual(res["status"], "queued")

    def test_guest_downloading_1080p_rejected_with_401_login_required(self):
        payload = main.DownloadRequest(
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            format_id="137",
            height=1080,
            audio_only=False,
        )
        res, _ = self._run_download(payload)
        self.assertEqual(res.status_code, 401)
        detail = json.loads(res.body.decode("utf-8"))
        self.assertEqual(detail["error"], "LOGIN_REQUIRED")
        self.assertEqual(detail["requiredHeight"], 1080)
        self.assertIn("Sign in with Google", detail["message"])

    def test_client_fake_height_bypass_rejected(self):
        # Client maliciously sends height=720, but format_id is a 1080p stream
        payload = main.DownloadRequest(
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            format_id="137",
            height=720,  # Claiming 720p
            audio_only=False,
        )
        res, _ = self._run_download(payload)
        self.assertEqual(res.status_code, 401)
        detail = json.loads(res.body.decode("utf-8"))
        self.assertEqual(detail["error"], "LOGIN_REQUIRED")
        self.assertEqual(detail["requiredHeight"], 1080)

    def test_omitting_format_id_is_rejected(self):
        # Regression: a guest used to be able to POST no format_id at all. That
        # left actual_height at the client-supplied height (None), and
        # can_download_format(None, None) returned True -- after which yt-dlp
        # downloaded `bestvideo+bestaudio/best`, i.e. the highest quality.
        payload = main.DownloadRequest(
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            format_id=None,
            height=None,
            audio_only=False,
        )
        res, run = self._run_download(payload)
        self.assertEqual(res.status_code, 400)
        detail = json.loads(res.body.decode("utf-8"))
        self.assertIn("format", detail["message"].lower())
        run.assert_not_called()

    def test_nonexistent_format_id_is_rejected(self):
        # Regression: a bogus format_id used to leave actual_height at the
        # client-supplied value, and yt-dlp's `f"{format_id}+bestaudio/..."`
        # selector silently fell through to `bestvideo+bestaudio/best`.
        for claimed_height in (None, 0, 360, 720):
            with self.subTest(claimed_height=claimed_height):
                payload = main.DownloadRequest(
                    url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    format_id="99999",
                    height=claimed_height,
                    audio_only=False,
                )
                res, run = self._run_download(payload)
                self.assertEqual(res.status_code, 400)
                run.assert_not_called()

    def test_guest_cannot_get_4k_by_claiming_low_height(self):
        payload = main.DownloadRequest(
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            format_id="313",  # 2160p
            height=144,
            audio_only=False,
        )
        res, _ = self._run_download(payload)
        self.assertEqual(res.status_code, 401)
        detail = json.loads(res.body.decode("utf-8"))
        self.assertEqual(detail["requiredHeight"], 2160)

    def test_verified_height_is_passed_to_downloader_as_ceiling(self):
        payload = main.DownloadRequest(
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            format_id="22",
            height=720,
            audio_only=False,
        )
        res, run = self._run_download(payload)
        self.assertIn("job_id", res)
        # The job is scheduled with the verified ceiling so the downloader
        # cannot resolve a higher rendition.
        self.assertEqual(run.call_args.args[2], 720)

    def test_audio_only_allowed_for_guests(self):
        payload = main.DownloadRequest(
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            format_id=None,
            audio_only=True,
        )
        res, _ = self._run_download(payload)
        self.assertIn("job_id", res)
        self.assertEqual(res["status"], "queued")

    def test_audio_only_format_id_treated_as_audio(self):
        # Requesting the raw audio format (140) with audio_only=False must not
        # be treated as a video request with an unknown height.
        payload = main.DownloadRequest(
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            format_id="140",
            height=None,
            audio_only=False,
        )
        res, run = self._run_download(payload)
        self.assertIn("job_id", res)
        self.assertIsNone(run.call_args.args[2])

    def test_info_fetch_failure_does_not_open_the_gate(self):
        # Regression: `except Exception: pass` around the format lookup used to
        # leave the gate trusting the client's height when extraction failed.
        payload = main.DownloadRequest(
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            format_id="137",
            height=720,
            audio_only=False,
        )
        request = self._create_mock_request()
        with patch.object(main, "_throttle", return_value=None), \
             patch.object(main, "_validate_public_url", return_value=None), \
             patch.object(
                 main, "_get_or_fetch_info",
                 side_effect=downloader.UnsupportedURLError("extraction failed"),
             ), \
             patch.object(main, "_execute_download_job") as run:
            res = asyncio.run(main.start_download(payload, request))
        self.assertEqual(res.status_code, 400)
        run.assert_not_called()

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

        res, _ = self._run_download(payload, request=request)
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


class CredentialFileStorageTests(unittest.TestCase):
    """Test saving user credentials to persistent files."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.orig_txt = auth.CREDENTIALS_TXT
        self.orig_jsonl = auth.CREDENTIALS_JSONL
        auth.CREDENTIALS_TXT = os.path.join(self.test_dir, "credentials.txt")
        auth.CREDENTIALS_JSONL = os.path.join(self.test_dir, "credentials.jsonl")

    def tearDown(self):
        auth.CREDENTIALS_TXT = self.orig_txt
        auth.CREDENTIALS_JSONL = self.orig_jsonl
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_record_credential_to_file(self):
        user_info = {
            "email": "stored_user@example.com",
            "name": "Stored User",
            "sub": "google-sub-9999",
            "picture": "https://avatar.example.com/p.jpg",
            "email_verified": True,
        }
        auth.record_credential_to_file(
            user_info,
            client_ip="198.51.100.42",
            user_agent="Mozilla/5.0 TestBrowser",
            user_id="user-uuid-abc",
            session_id="a" * 64,  # SHA-256 hex of the session token
        )

        self.assertTrue(os.path.exists(auth.CREDENTIALS_TXT))
        self.assertTrue(os.path.exists(auth.CREDENTIALS_JSONL))

    def test_record_credential_to_file_contents(self):
        user_info = {
            "email": "stored_user@example.com",
            "name": "Stored User",
            "sub": "google-sub-9999",
            "picture": "https://avatar.example.com/p.jpg",
            "email_verified": True,
        }
        auth.record_credential_to_file(
            user_info,
            client_ip="198.51.100.42",
            user_agent="Mozilla/5.0 TestBrowser",
            user_id="user-uuid-abc",
            session_id="a" * 64,
        )
        with open(auth.CREDENTIALS_TXT, "r", encoding="utf-8") as f:
            txt_content = f.read()
        self.assertIn("Email: stored_user@example.com", txt_content)
        self.assertIn("Name: Stored User", txt_content)
        self.assertIn("GoogleID: google-sub-9999", txt_content)
        self.assertIn("UserID: user-uuid-abc", txt_content)
        self.assertIn("IP: 198.51.100.42", txt_content)
        # The full token hash is not written in the human-readable log.
        self.assertIn("SessionHash: " + "a" * 12, txt_content)
        self.assertNotIn("a" * 64, txt_content)

        with open(auth.CREDENTIALS_JSONL, "r", encoding="utf-8") as f:
            data = json.loads(f.readline())
        self.assertEqual(data["email"], "stored_user@example.com")
        self.assertEqual(data["name"], "Stored User")
        self.assertEqual(data["google_id"], "google-sub-9999")
        self.assertEqual(data["client_ip"], "198.51.100.42")
        self.assertEqual(data["user_id"], "user-uuid-abc")
        self.assertEqual(data["session_token_hash"], "a" * 64)


class SessionPruningTests(AuthAndGateBaseTestCase):
    """Regression: every login inserted a session row and nothing ever
    removed them, so the table grew without bound."""

    def test_repeated_logins_do_not_grow_sessions_unbounded(self):
        user = database.upsert_user("sub-many", "many@example.com", True, "Many", None)
        user_id = str(user["id"])
        for i in range(15):
            database.create_session(
                user_id,
                auth.hash_token(f"tok-{i}"),
                datetime.now(timezone.utc) + timedelta(days=1),
            )
        database.prune_sessions_for_user(user_id, keep=10)
        conn = database._get_connection()
        with conn:
            rows = conn.execute(
                "SELECT COUNT(*) AS c FROM sessions WHERE user_id = ?", (user_id,)
            ).fetchone()
        conn.close()
        self.assertLessEqual(rows["c"], 10)

    def test_prune_expired_sessions_removes_only_expired(self):
        user = database.upsert_user("sub-exp2", "exp2@example.com", True, "Exp", None)
        user_id = str(user["id"])
        now = datetime.now(timezone.utc)
        database.create_session(user_id, auth.hash_token("old"), now - timedelta(days=1))
        database.create_session(user_id, auth.hash_token("new"), now + timedelta(days=1))
        database.prune_expired_sessions()
        conn = database._get_connection()
        with conn:
            rows = conn.execute(
                "SELECT token_hash FROM sessions WHERE user_id = ?", (user_id,)
            ).fetchall()
        conn.close()
        hashes = {r["token_hash"] for r in rows}
        self.assertNotIn(auth.hash_token("old"), hashes)
        self.assertIn(auth.hash_token("new"), hashes)

    def test_bare_filenames_do_not_lose_records(self):
        """Regression: makedirs(os.path.dirname("x.txt")) raised FileNotFoundError,
        which was swallowed, silently discarding every credential record."""
        orig_txt = auth.CREDENTIALS_TXT
        orig_jsonl = auth.CREDENTIALS_JSONL
        cwd = os.getcwd()
        try:
            work = tempfile.mkdtemp()
            os.chdir(work)
            # Bare filenames, with no directory component at all.
            auth.CREDENTIALS_TXT = "creds.txt"
            auth.CREDENTIALS_JSONL = "creds.jsonl"
            auth.record_credential_to_file(
                {"email": "bare@example.com", "name": "Bare", "sub": "s1"},
                client_ip="1.2.3.4",
            )
            self.assertTrue(os.path.exists(os.path.join(work, "creds.txt")))
            self.assertTrue(os.path.exists(os.path.join(work, "creds.jsonl")))
        finally:
            os.chdir(cwd)
            auth.CREDENTIALS_TXT = orig_txt
            auth.CREDENTIALS_JSONL = orig_jsonl
            shutil.rmtree(work, ignore_errors=True)


class ThrottleAndErrorDetailTests(AuthAndGateBaseTestCase):
    """Test detailed actionable error messages and rate limit feedback."""

    def test_logout_clears_secure_cookie_with_secure_attribute(self):
        """Regression: a __Host- Set-Cookie without Secure is rejected by the
        browser, so the deletion was silently ignored on HTTPS."""
        user = database.upsert_user("sub-c", "c@example.com", True, "C", None)
        raw = "raw-cookie-token"
        database.create_session(
            str(user["id"]),
            auth.hash_token(raw),
            datetime.now(timezone.utc) + timedelta(days=1),
        )
        request = self._create_mock_request(
            cookies={auth.COOKIE_NAME_SECURE: raw}, is_https=True
        )
        response = Response()
        auth.logout_user(request, response)

        cookies = [
            v for k, v in response.raw_headers if k == b"set-cookie"
        ]
        secure_deletion = [
            c.decode("latin-1") for c in cookies
            if c.decode("latin-1").startswith(auth.COOKIE_NAME_SECURE)
        ]
        self.assertEqual(len(secure_deletion), 1)
        header = secure_deletion[0]
        self.assertIn("Max-Age=0", header)
        self.assertIn("Secure", header)
        self.assertIn("HttpOnly", header)
        self.assertNotIn("Domain=", header)
        self.assertIn("Path=/", header)

    def test_google_verification_fails_closed_without_client_id(self):
        """Regression: audience=None skipped the audience check, accepting a
        Google ID token minted for any other application."""
        saved = auth.GOOGLE_CLIENT_ID
        try:
            auth.GOOGLE_CLIENT_ID = ""
            with patch("google.oauth2.id_token.verify_oauth2_token") as mock_verify:
                with self.assertRaises(HTTPException) as ctx:
                    auth.verify_google_credential("some-credential")
                self.assertEqual(ctx.exception.status_code, 503)
                mock_verify.assert_not_called()
        finally:
            auth.GOOGLE_CLIENT_ID = saved

    def test_admin_credentials_requires_admin_key(self):
        """Regression: with ADMIN_KEY unset the endpoint served every sign-in's
        email, IP and user agent to anyone."""
        request = MagicMock(spec=Request)
        request.headers = {}
        saved = os.environ.get("ADMIN_KEY")
        try:
            os.environ["ADMIN_KEY"] = ""
            with self.assertRaises(HTTPException) as ctx:
                main.get_credentials_file(request, "txt")
            self.assertEqual(ctx.exception.status_code, 503)
        finally:
            if saved is None:
                os.environ.pop("ADMIN_KEY", None)
            else:
                os.environ["ADMIN_KEY"] = saved

    def test_admin_credentials_rejects_wrong_key(self):
        request = MagicMock(spec=Request)
        request.headers = {"x-admin-key": "wrong"}
        saved = os.environ.get("ADMIN_KEY")
        try:
            os.environ["ADMIN_KEY"] = "correct-key"
            with self.assertRaises(HTTPException) as ctx:
                main.get_credentials_file(request, "txt")
            self.assertEqual(ctx.exception.status_code, 403)
        finally:
            if saved is None:
                os.environ.pop("ADMIN_KEY", None)
            else:
                os.environ["ADMIN_KEY"] = saved

    def test_client_ip_ignores_spoofed_forwarded_for_by_default(self):
        """Regression: throttling keyed on a header the client controls."""
        request = MagicMock(spec=Request)
        request.client = MagicMock()
        request.client.host = "198.51.100.7"
        request.headers = {"x-forwarded-for": "1.2.3.4"}
        saved = main.TRUSTED_PROXY_HOPS
        try:
            main.TRUSTED_PROXY_HOPS = 0
            self.assertEqual(main._get_client_ip(request), "198.51.100.7")
            main.TRUSTED_PROXY_HOPS = 1
            self.assertEqual(main._get_client_ip(request), "1.2.3.4")
        finally:
            main.TRUSTED_PROXY_HOPS = saved

    def test_throttle_reports_seconds(self):
        table = {"127.0.0.1": time.time()}
        with self.assertRaises(main.HTTPException) as cm:
            main._throttle("127.0.0.1", table, delay_seconds=5.0)
        self.assertEqual(cm.exception.status_code, 429)
        self.assertIn("please wait", str(cm.exception.detail))
        self.assertIn("before trying again", str(cm.exception.detail))


if __name__ == "__main__":
    unittest.main(verbosity=2)
