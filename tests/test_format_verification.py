"""Tests for server-side format verification and download-request enforcement.

Run: python -m unittest tests.test_format_verification -v

There is no account system and no quality gate. What is enforced here is that
the caller gets exactly the format they asked for: the client-supplied
`height` is ignored, a `format_id` that does not exist is rejected rather than
silently swapped for "best", and extraction failure never degrades into an
unchecked request.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import authorization
import database
import downloader
import main
from starlette.requests import Request


class BaseTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="anydown-verify-test-")
        self.db_path = os.path.join(self.temp_dir, "test_anydown.db")
        os.environ["SQLITE_DB_PATH"] = self.db_path
        database.init_db()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _mock_request(self, client_ip: str = "203.0.113.1") -> MagicMock:
        req = MagicMock(spec=Request)
        req.cookies = {}
        req.client = MagicMock()
        req.client.host = client_ip
        req.headers = {}
        req.url = MagicMock()
        req.url.scheme = "https"
        return req


class FormatPolicyTests(unittest.TestCase):
    def test_no_quality_ceiling(self):
        """Every resolution is servable; there is no gate."""
        for height in (144, 360, 720, 1080, 1440, 2160, 4320):
            self.assertTrue(
                authorization.can_download_format(height),
                f"{height}p should be allowed",
            )

    def test_unknown_height_denied(self):
        """Verification, not permission: an unverifiable height is refused."""
        self.assertFalse(authorization.can_download_format(None))

    def test_audio_only_always_allowed(self):
        self.assertTrue(authorization.can_download_format(None, audio_only=True))

    def test_annotate_never_locks(self):
        formats = [
            {"format_id": "18", "height": 360, "has_video": True},
            {"format_id": "313", "height": 2160, "has_video": True},
            {"format_id": "140", "height": None, "has_video": False},
        ]
        annotated = authorization.annotate_formats(formats)
        self.assertTrue(all(not f["locked"] for f in annotated))
        # Originals untouched.
        self.assertNotIn("locked", formats[0])


class ResolveFormatTests(unittest.TestCase):
    _FORMATS = [
        {"format_id": "18", "height": 360, "has_video": True, "has_audio": True},
        {"format_id": "137", "height": 1080, "has_video": True, "has_audio": False},
        {"format_id": "140", "height": None, "has_video": False, "has_audio": True},
    ]

    def test_known_video_format(self):
        v = authorization.resolve_format(self._FORMATS, "137")
        self.assertTrue(v.known)
        self.assertFalse(v.audio_only)
        self.assertEqual(v.height, 1080)
        self.assertFalse(v.has_audio)

    def test_unknown_format_id(self):
        self.assertFalse(authorization.resolve_format(self._FORMATS, "99999").known)

    def test_missing_format_id(self):
        self.assertFalse(authorization.resolve_format(self._FORMATS, None).known)

    def test_audio_format_detected(self):
        v = authorization.resolve_format(self._FORMATS, "140")
        self.assertTrue(v.known)
        self.assertTrue(v.audio_only)
        self.assertIsNone(v.height)

    def test_audio_only_flag_short_circuits(self):
        v = authorization.resolve_format(self._FORMATS, None, audio_only=True)
        self.assertTrue(v.known)
        self.assertTrue(v.audio_only)


class DownloadRequestEnforcementTests(BaseTestCase):
    """start_download must hand the caller exactly what they asked for."""

    _LADDER = {
        "formats": [
            {"format_id": "18", "height": 360, "has_video": True, "has_audio": True},
            {"format_id": "22", "height": 720, "has_video": True, "has_audio": True},
            {"format_id": "137", "height": 1080, "has_video": True, "has_audio": False},
            {"format_id": "313", "height": 2160, "has_video": True, "has_audio": False},
            {"format_id": "140", "height": None, "has_video": False, "has_audio": True},
        ]
    }

    def _run(self, payload, info=None, request=None):
        if request is None:
            request = self._mock_request()
        with patch.object(main, "_throttle", return_value=None), \
             patch.object(main, "_validate_public_url", return_value=None), \
             patch.object(
                 main, "_get_or_fetch_info",
                 return_value=self._LADDER if info is None else info,
             ), \
             patch.object(main, "_execute_download_job") as run:
            res = asyncio.run(main.start_download(payload, request))
        return res, run

    def _payload(self, **kw):
        base = {
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "format_id": None,
            "height": None,
            "audio_only": False,
        }
        base.update(kw)
        return main.DownloadRequest(**base)

    def test_all_qualities_now_allowed(self):
        """Regression: 1080p and 4K used to return 401 LOGIN_REQUIRED."""
        for fid, expected_height in (("22", 720), ("137", 1080), ("313", 2160)):
            with self.subTest(format_id=fid):
                res, run = self._run(self._payload(format_id=fid))
                self.assertIn("job_id", res)
                self.assertEqual(res["status"], "queued")
                self.assertEqual(run.call_args.args[2], expected_height)

    def test_no_login_required_response_anywhere(self):
        """The 401 LOGIN_REQUIRED contract must be gone entirely."""
        res, _ = self._run(self._payload(format_id="313"))
        self.assertIn("job_id", res)
        self.assertNotEqual(getattr(res, "status_code", 200), 401)

    def test_client_supplied_height_is_ignored(self):
        """A lying `height` must not change what the server downloads."""
        res, run = self._run(self._payload(format_id="313", height=144))
        self.assertIn("job_id", res)
        self.assertEqual(run.call_args.args[2], 2160)

    def test_verified_height_is_the_ceiling(self):
        res, run = self._run(self._payload(format_id="137", height=360))
        self.assertIn("job_id", res)
        self.assertEqual(run.call_args.args[2], 1080)

    def test_omitting_format_id_rejected(self):
        """Regression: with no format_id, yt-dlp would resolve to `best`."""
        res, run = self._run(self._payload())
        self.assertEqual(res.status_code, 400)
        self.assertIn("format", json.loads(res.body.decode())["message"].lower())
        run.assert_not_called()

    def test_nonexistent_format_id_rejected(self):
        """Regression: a bogus id used to fall through to bestvideo+bestaudio."""
        for claimed in (None, 0, 360, 720):
            with self.subTest(height=claimed):
                res, run = self._run(
                    self._payload(format_id="99999", height=claimed)
                )
                self.assertEqual(res.status_code, 400)
                run.assert_not_called()

    def test_audio_only_accepted(self):
        res, _ = self._run(self._payload(audio_only=True))
        self.assertIn("job_id", res)

    def test_audio_format_id_treated_as_audio(self):
        res, run = self._run(self._payload(format_id="140"))
        self.assertIn("job_id", res)
        self.assertIsNone(run.call_args.args[2])

    def test_extraction_failure_does_not_allow_request(self):
        """Regression: a swallowed extraction error used to let the request through."""
        payload = self._payload(format_id="137", height=720)
        request = self._mock_request()
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

    def test_video_without_height_rejected(self):
        """A video format with no verifiable height cannot be served."""
        info = {"formats": [
            {"format_id": "odd", "height": None, "has_video": True, "has_audio": True},
        ]}
        res, run = self._run(self._payload(format_id="odd"), info=info)
        self.assertEqual(res.status_code, 400)
        run.assert_not_called()


class PublicApiSurfaceTests(BaseTestCase):
    def test_no_auth_routes_remain(self):
        paths = {r.path for r in main.app.routes if hasattr(r, "path")}
        for gone in ("/api/auth/google", "/api/auth/me",
                     "/api/auth/logout", "/api/admin/credentials"):
            self.assertNotIn(gone, paths)

    def test_config_has_no_google_or_gate_fields(self):
        cfg = main.get_config()
        for gone in ("google_client_id", "googleClientId",
                     "guest_max_height", "guestMaxHeight"):
            self.assertNotIn(gone, cfg)
        self.assertIn("app_base_url", cfg)

    def test_database_has_no_user_or_session_api(self):
        for gone in ("upsert_user", "create_session", "get_session_user",
                     "delete_session", "get_user_by_google_sub",
                     "prune_sessions_for_user", "prune_expired_sessions"):
            self.assertFalse(
                hasattr(database, gone), f"database.{gone} should be removed"
            )

    def test_no_auth_module(self):
        self.assertFalse(
            os.path.exists(
                os.path.join(os.path.dirname(__file__), "..", "auth.py")
            ),
            "auth.py should be deleted",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
