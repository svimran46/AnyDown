"""Unit tests for downloader.py YouTube fallback/diagnostic behavior.

Run: python3 -m unittest tests.test_youtube_fallback -v
No network access required.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import downloader  # noqa: E402


class ClientChainTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("YOUTUBE_CLIENTS", None)
        os.environ.pop("YOUTUBE_PRIMARY_CLIENT", None)

    def test_default_chain_is_datacenter_tuned(self):
        # mweb (PO token via bgutil) first, then tv (no PO token), then
        # yt-dlp's own defaults as the final escape hatch.
        self.assertEqual(downloader._youtube_clients(), ["mweb", "tv", ""])

    def test_default_chain_constant_ends_with_ytdlp_defaults(self):
        self.assertEqual(downloader.DEFAULT_YOUTUBE_CLIENTS[-1], "")

    def test_primary_client_keeps_defaults_as_last_resort(self):
        os.environ["YOUTUBE_PRIMARY_CLIENT"] = "web"
        self.assertEqual(downloader._youtube_clients(), ["web", ""])

    def test_forced_chain_keeps_defaults_as_last_resort(self):
        os.environ["YOUTUBE_CLIENTS"] = "mweb, tv"
        self.assertEqual(downloader._youtube_clients(), ["mweb", "tv", ""])

    def test_primary_plus_forced_chain_dedupes(self):
        os.environ["YOUTUBE_PRIMARY_CLIENT"] = "mweb"
        os.environ["YOUTUBE_CLIENTS"] = "mweb,tv"
        self.assertEqual(downloader._youtube_clients(), ["mweb", "tv", ""])

    def test_defaults_attempt_does_not_pin_player_client(self):
        # The final attempt must defer to yt-dlp's own client selection,
        # even though the default *chain* starts with mweb.
        opts: dict = {}
        downloader._youtube_options(opts, client="")
        self.assertNotIn("youtube", opts.get("extractor_args", {}))

    def test_client_label_names_defaults_attempt(self):
        self.assertEqual(downloader._client_label(""), "yt-dlp-defaults")
        self.assertEqual(downloader._client_label("tv"), "tv")


class CookieOptionTests(unittest.TestCase):
    def setUp(self):
        self._orig = downloader.YOUTUBE_COOKIES_FILE

    def tearDown(self):
        downloader.YOUTUBE_COOKIES_FILE = self._orig

    def test_unconfigured(self):
        downloader.YOUTUBE_COOKIES_FILE = ""
        self.assertEqual(
            downloader.youtube_cookies_status(),
            {"configured": False, "file_found": None, "format": None},
        )

    def test_stale_path_warns_and_continues_without_cookies(self):
        downloader.YOUTUBE_COOKIES_FILE = "/nonexistent/cookies.txt"
        opts: dict = {}
        # Must not raise: a stale secret path must not take down every request.
        downloader._youtube_options(opts, client="")
        self.assertNotIn("cookiefile", opts)

    def test_valid_netscape_file_is_used(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("# Netscape HTTP Cookie File\n")
            path = fh.name
        try:
            downloader.YOUTUBE_COOKIES_FILE = path
            status = downloader.youtube_cookies_status()
            self.assertTrue(status["configured"] and status["file_found"])
            self.assertEqual(status["format"], "netscape")
            opts: dict = {}
            downloader._youtube_options(opts, client="")
            self.assertEqual(opts.get("cookiefile"), path)
        finally:
            os.unlink(path)

    def test_json_export_is_rejected_with_clear_error(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write('[{"name":"VISITOR_INFO1_LIVE"}]')
            path = fh.name
        try:
            downloader.YOUTUBE_COOKIES_FILE = path
            with self.assertRaises(downloader.UnsupportedURLError) as ctx:
                downloader._youtube_options({}, client="")
            self.assertIn("JSON", str(ctx.exception))
        finally:
            os.unlink(path)


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self._orig_pot = downloader.YTDLP_POT_PROVIDER_URL
        self._orig_cookies = downloader.YOUTUBE_COOKIES_FILE

    def tearDown(self):
        downloader.YTDLP_POT_PROVIDER_URL = self._orig_pot
        downloader.YOUTUBE_COOKIES_FILE = self._orig_cookies

    def test_pot_unreachable_is_detected(self):
        downloader.YTDLP_POT_PROVIDER_URL = "http://127.0.0.1:9"
        status = downloader.pot_provider_status(timeout=1.0)
        self.assertEqual(status, {"configured": True, "reachable": False})

    def test_pot_unconfigured(self):
        downloader.YTDLP_POT_PROVIDER_URL = ""
        self.assertEqual(
            downloader.pot_provider_status(),
            {"configured": False, "reachable": None},
        )

    def test_bot_check_error_reports_real_state(self):
        downloader.YTDLP_POT_PROVIDER_URL = "http://127.0.0.1:9"
        downloader.YOUTUBE_COOKIES_FILE = ""
        msg = downloader._friendly_error(
            downloader.UnsupportedURLError("Sign in to confirm you're not a bot")
        )
        self.assertIn("NOT reachable", msg)
        self.assertIn("no YouTube cookies are configured", msg)

    def test_bot_check_error_names_missing_cookie_file(self):
        downloader.YTDLP_POT_PROVIDER_URL = ""
        downloader.YOUTUBE_COOKIES_FILE = "/nonexistent/cookies.txt"
        msg = downloader._friendly_error(
            downloader.UnsupportedURLError("confirm you're not a bot")
        )
        self.assertIn("/nonexistent/cookies.txt", msg)


class VersionMarkerTests(unittest.TestCase):
    """The app version must be visible in health output and bot-check errors
    so a stale deployment is identifiable from the error text alone."""

    def test_app_version_is_nonempty_semverish(self):
        self.assertRegex(downloader.APP_VERSION, r"^\d+\.\d+\.\d+$")

    def test_bot_check_error_includes_app_version(self):
        msg = downloader._friendly_error(
            downloader.UnsupportedURLError("confirm you're not a bot")
        )
        self.assertIn(f"[AnyDown {downloader.APP_VERSION}]", msg)

    def test_health_payload_includes_app_version(self):
        # health() is a plain sync function; calling it directly avoids the
        # optional httpx2 dependency needed by fastapi's TestClient.
        import main
        payload = main.health()
        self.assertEqual(payload["app_version"], downloader.APP_VERSION)
        self.assertIn("yt_dlp", payload)


class JsRuntimeTests(unittest.TestCase):
    def test_detect_returns_string(self):
        # In a bare environment "" is acceptable (warning + remote components);
        # with node/deno/bun installed it must find one of them.
        self.assertIsInstance(downloader.detect_js_runtime(), str)

    def test_unset_runtime_falls_back_to_detection(self):
        saved = os.environ.pop("YTDLP_JS_RUNTIME", None)
        try:
            opts: dict = {}
            downloader._youtube_options(opts, client="")
            runtime = downloader.detect_js_runtime()
            if runtime:
                self.assertEqual(opts.get("js_runtimes"), {runtime: {}})
            else:
                self.assertEqual(opts.get("remote_components"), ["ejs:github"])
        finally:
            if saved is not None:
                os.environ["YTDLP_JS_RUNTIME"] = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
