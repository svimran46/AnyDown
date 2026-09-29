"""Coverage for the non-YouTube paths, which had no tests at all.

Facebook detection and proxying, the audio-only FFmpeg branch, filename
sanitisation, the oversized-format cap, and URL validation were all unverified
while every existing test targeted YouTube.
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.getcwd())

import downloader  # noqa: E402
import main  # noqa: E402


class FacebookDetectionTests(unittest.TestCase):
    def test_known_hosts(self):
        for url in (
            "https://facebook.com/watch?v=1",
            "https://www.facebook.com/watch?v=1",
            "https://m.facebook.com/watch?v=1",
            "https://fb.watch/abc/",
            "https://www.fb.watch/abc/",
        ):
            self.assertTrue(downloader.is_facebook(url), url)

    def test_subdomains_match(self):
        self.assertTrue(downloader.is_facebook("https://web.facebook.com/x"))
        self.assertTrue(downloader.is_facebook("https://mbasic.facebook.com/x"))

    def test_lookalikes_rejected(self):
        for url in (
            "https://notfacebook.com/x",
            "https://facebook.com.evil.io/x",
            "https://evilfacebook.com/x",
            "https://fb.watch.evil.io/x",
            "https://youtube.com/watch?v=1",
        ):
            self.assertFalse(downloader.is_facebook(url), url)

    def test_case_and_trailing_dot_normalised(self):
        self.assertTrue(downloader.is_facebook("https://FACEBOOK.COM/x"))
        self.assertTrue(downloader.is_facebook("https://www.facebook.com./x"))


class FacebookProxyTests(unittest.TestCase):
    def _opts(self, url, proxy):
        saved = downloader.FACEBOOK_PROXY_URL
        try:
            downloader.FACEBOOK_PROXY_URL = proxy
            opts: dict = {}
            downloader._apply_platform_options(url, opts)
            return opts
        finally:
            downloader.FACEBOOK_PROXY_URL = saved

    def test_proxy_applied_for_facebook(self):
        opts = self._opts("https://www.facebook.com/watch?v=1",
                          "http://fbproxy.internal:8080")
        self.assertEqual(opts.get("proxy"), "http://fbproxy.internal:8080")

    def test_no_proxy_leaves_opts_untouched(self):
        opts = self._opts("https://www.facebook.com/watch?v=1", "")
        self.assertIsNone(opts.get("proxy"))

    def test_youtube_proxy_not_applied_here(self):
        """Regression: YouTube must not pick up the Facebook proxy.

        _apply_platform_options is only reached for non-YouTube URLs, so a
        Facebook proxy configured here must never leak into a YouTube request.
        """
        opts = self._opts("https://www.facebook.com/watch?v=1",
                          "http://fbproxy.internal:8080")
        self.assertNotIn("extractor_args", opts)


class AudioOnlyOptionTests(unittest.TestCase):
    def _build(self, audio_only, max_height=None, format_id=None):
        captured = {}

        def fake_to_thread(fn, *a, **k):  # not used; download_media is sync
            raise AssertionError("unexpected")

        with patch.object(downloader, "_base_options", return_value={}):
            with patch.object(
                downloader, "_install_ssrf_guard", lambda: None
            ):
                with patch.object(
                    downloader.yt_dlp, "YoutubeDL",
                    side_effect=lambda opts, **k: captured.setdefault("opts", opts),
                ):
                    try:
                        downloader.download_media(
                            "https://example.com/v", tempfile.gettempdir(),
                            "audio-job", format_id, False, audio_only, None,
                            max_height,
                        )
                    except Exception:
                        pass
        return captured.get("opts", {})

    def test_audio_only_requests_audio_and_extracts_mp3(self):
        opts = self._build(audio_only=True)
        self.assertEqual(opts.get("format"), "bestaudio/best")
        pps = opts.get("postprocessors") or []
        self.assertEqual(len(pps), 1)
        self.assertEqual(pps[0]["key"], "FFmpegExtractAudio")
        self.assertEqual(pps[0]["preferredcodec"], "mp3")

    def test_audio_only_ignores_resolution_ceiling(self):
        """Audio has no height, so a ceiling must not constrain it."""
        opts = self._build(audio_only=True, max_height=720)
        self.assertNotIn("height", opts.get("format", ""))


class SanitizeFilenameTests(unittest.TestCase):
    def test_path_separators_removed(self):
        for bad in ("a/b", "a\\b", "a:b", "a*b", "a?b", 'a"b', "a<b", "a>b", "a|b"):
            self.assertNotIn("/", downloader._sanitize_filename(bad))

    def test_control_characters_stripped(self):
        out = downloader._sanitize_filename("bad\x00name\x1fname")
        self.assertNotIn("\x00", out)
        self.assertNotIn("\x1f", out)

    def test_windows_reserved_names_prefixed(self):
        for reserved in ("CON", "con", "NUL", "COM1", "LPT9"):
            self.assertTrue(
                downloader._sanitize_filename(reserved).startswith("_"),
                reserved,
            )

    def test_trailing_dots_and_spaces_removed(self):
        self.assertFalse(downloader._sanitize_filename("name...  ").endswith((".", " ")))

    def test_length_capped(self):
        self.assertLessEqual(len(downloader._sanitize_filename("x" * 400)), 150)

    def test_empty_falls_back(self):
        self.assertEqual(downloader._sanitize_filename(""), "download")
        self.assertEqual(downloader._sanitize_filename("   "), "download")


class OversizedFormatTests(unittest.TestCase):
    def setUp(self):
        self.saved = downloader.MAX_FILESIZE_BYTES

    def tearDown(self):
        downloader.MAX_FILESIZE_BYTES = self.saved

    def test_zero_disables_cap(self):
        downloader.MAX_FILESIZE_BYTES = 0
        self.assertFalse(downloader._is_oversized({"filesize": 10**12}))

    def test_unknown_size_never_blocked(self):
        downloader.MAX_FILESIZE_BYTES = 1000
        self.assertFalse(downloader._is_oversized({}))
        self.assertFalse(downloader._is_oversized({"filesize": None}))

    def test_approximate_size_honoured(self):
        downloader.MAX_FILESIZE_BYTES = 1000
        self.assertTrue(downloader._is_oversized({"filesize_approx": 5000}))
        self.assertFalse(downloader._is_oversized({"filesize_approx": 500}))

    def test_actual_size_preferred(self):
        downloader.MAX_FILESIZE_BYTES = 1000
        self.assertTrue(
            downloader._is_oversized({"filesize": 5000, "filesize_approx": 10})
        )


class UrlValidationTests(unittest.TestCase):
    def test_rejects_non_http(self):
        for url in ("file:///etc/passwd", "ftp://x/y", "gopher://x", "javascript:alert(1)"):
            with self.assertRaises(Exception):
                main._validate_url_syntax(url)

    def test_rejects_loopback_and_private_literals(self):
        for url in (
            "http://127.0.0.1/x", "http://[::1]/x", "http://10.0.0.1/x",
            "http://192.168.1.1/x", "http://169.254.169.254/x",
        ):
            with self.assertRaises(Exception):
                main._validate_url_syntax(url)

    def test_rejects_localhost_by_name(self):
        for url in ("http://localhost/x", "http://localhost.localdomain/x"):
            with self.assertRaises(Exception):
                main._validate_url_syntax(url)

    def test_rejects_embedded_credentials(self):
        with self.assertRaises(Exception):
            main._validate_url_syntax("http://user:pass@example.com/x")

    def test_accepts_public_https(self):
        self.assertEqual(
            main._validate_url_syntax("https://www.youtube.com/watch?v=abc"),
            "www.youtube.com",
        )

    def test_cloud_metadata_endpoint_blocked(self):
        """169.254.169.254 is the cloud metadata service."""
        with self.assertRaises(Exception):
            main._validate_url_syntax("http://169.254.169.254/latest/meta-data/")


if __name__ == "__main__":
    unittest.main(verbosity=2)
