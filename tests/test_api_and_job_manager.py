"""Unit tests for JobManager, main.py API logic, and downloader.py SSRF/file guards.

Run: python -m unittest tests.test_api_and_job_manager -v
"""
import asyncio
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import downloader
from downloader import UnsupportedURLError
from job_manager import JobManager, JobStatus
import main


class JobManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="an-down-jm-test-")
        self.jm = JobManager(file_ttl_seconds=1)

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            import shutil
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_create_and_get_job(self):
        job = self.jm.create_job("https://example.com/video")
        self.assertIsNotNone(job.id)
        self.assertEqual(job.status, JobStatus.QUEUED)
        self.assertEqual(job.url, "https://example.com/video")
        self.assertEqual(self.jm.get(job.id), job)

    def test_update_and_get_payload(self):
        job = self.jm.create_job("https://example.com/video")
        self.jm.update(
            job.id,
            status=JobStatus.DOWNLOADING,
            progress=50.0,
            downloaded_bytes=500,
            total_bytes=1000,
            speed=250.0,
            eta=2,
        )
        payload = self.jm.get_payload(job.id)
        self.assertEqual(payload["job_id"], job.id)
        self.assertEqual(payload["status"], "downloading")
        self.assertEqual(payload["progress"], 50.0)
        self.assertEqual(payload["downloaded_bytes"], 500)
        self.assertEqual(payload["total_bytes"], 1000)
        self.assertEqual(payload["speed"], 250.0)
        self.assertEqual(payload["eta"], 2)

    def test_cleanup_expired_completed_jobs_and_files(self):
        job = self.jm.create_job("https://example.com/video")
        file_path = os.path.join(self.temp_dir, f"{job.id}.mp4")
        with open(file_path, "w") as f:
            f.write("content")
        self.jm.update(job.id, status=JobStatus.COMPLETED, filepath=file_path)

        # Backdate finished_at to simulate expiration
        job.finished_at = time.time() - 10
        self.jm.cleanup_expired(self.temp_dir)

        self.assertIsNone(self.jm.get(job.id))
        self.assertFalse(os.path.exists(file_path))

    def test_in_flight_jobs_are_not_expired(self):
        """Regression: a long-running download was evicted once it exceeded the
        TTL, which deleted the file underneath it and leaked it permanently."""
        for status in (JobStatus.QUEUED, JobStatus.DOWNLOADING):
            with self.subTest(status=status):
                job = self.jm.create_job("https://example.com/long")
                self.jm.update(job.id, status=status)
                job.updated_at = time.time() - 10_000
                self.jm.cleanup_expired(self.temp_dir)
                self.assertIsNotNone(self.jm.get(job.id))

    def test_expired_failed_job_is_dropped(self):
        job = self.jm.create_job("https://example.com/bad")
        self.jm.update(job.id, status=JobStatus.FAILED, error="boom")
        job.finished_at = time.time() - 10
        self.jm.cleanup_expired(self.temp_dir)
        self.assertIsNone(self.jm.get(job.id))

    def test_cleanup_prefix_sweep_removes_all_files_for_a_job(self):
        job = self.jm.create_job("https://example.com/multi")
        names = [f"{job.id}.mp4", f"{job.id}.part", f"{job.id}.ytdl"]
        for n in names:
            with open(os.path.join(self.temp_dir, n), "w") as f:
                f.write("x")
        self.jm.update(job.id, status=JobStatus.COMPLETED)
        job.finished_at = time.time() - 10
        self.jm.cleanup_expired(self.temp_dir)
        for n in names:
            self.assertFalse(
                os.path.exists(os.path.join(self.temp_dir, n)), f"{n} should be gone"
            )

    def test_cleanup_orphaned_temp_files(self):
        part_file = os.path.join(self.temp_dir, "orphan.part")
        with open(part_file, "w") as f:
            f.write("temp")
        # Set mtime back by 10s
        old_time = time.time() - 10
        os.utime(part_file, (old_time, old_time))

        self.jm.cleanup_expired(self.temp_dir)
        self.assertFalse(os.path.exists(part_file))


class SSRFAndFileGuardTests(unittest.TestCase):
    def setUp(self):
        self._orig_pot = downloader.YTDLP_POT_PROVIDER_URL
        self._orig_proxy = downloader.FACEBOOK_PROXY_URL

    def tearDown(self):
        downloader.YTDLP_POT_PROVIDER_URL = self._orig_pot
        downloader.FACEBOOK_PROXY_URL = self._orig_proxy

    def test_assert_routable_blocks_private_and_loopback(self):
        import ipaddress
        blocked_ips = [
            "127.0.0.1", "127.0.0.2", "10.0.0.1", "172.16.0.1",
            "192.168.1.1", "169.254.169.254", "0.0.0.0", "::1"
        ]
        for ip_str in blocked_ips:
            ip = ipaddress.ip_address(ip_str)
            with self.assertRaises(downloader.BlockedAddressError):
                downloader._assert_routable(ip)

    def test_assert_routable_allows_public_ips(self):
        import ipaddress
        public_ips = ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"]
        for ip_str in public_ips:
            ip = ipaddress.ip_address(ip_str)
            try:
                downloader._assert_routable(ip)
            except downloader.BlockedAddressError:
                self.fail(f"Public IP {ip_str} was unexpectedly blocked")

    def test_is_exempt_target_only_allows_configured_port(self):
        downloader.YTDLP_POT_PROVIDER_URL = "http://127.0.0.1:4416"
        # Configured port on loopback is exempt
        self.assertTrue(downloader._is_exempt_target("127.0.0.1", 4416))
        self.assertTrue(downloader._is_exempt_target("localhost", 4416))

        # Different ports on loopback are NOT exempt (prevents SSRF to internal services)
        self.assertFalse(downloader._is_exempt_target("127.0.0.1", 8000))
        self.assertFalse(downloader._is_exempt_target("127.0.0.1", 22))
        self.assertFalse(downloader._is_exempt_target("127.0.0.1", 6379))
        # Other loopback addresses are not covered by the alias list.
        self.assertFalse(downloader._is_exempt_target("127.0.0.2", 4416))
        # An unknown port is never exempt: "any port" would turn one
        # configured peer into an open internal port range.
        self.assertFalse(downloader._is_exempt_target("127.0.0.1", None))

    def test_is_exempt_target_denies_all_when_unconfigured(self):
        downloader.YTDLP_POT_PROVIDER_URL = ""
        downloader.FACEBOOK_PROXY_URL = ""
        # 127.0.0.1:4416 stays exempt: yt-dlp's bgutil plugin targets it even
        # when unconfigured, and blocking it silently disabled the PO-token
        # provider (see test_pot_provider_default_endpoint_is_exempt).
        self.assertFalse(downloader._is_exempt_target("localhost", 80))
        self.assertFalse(downloader._is_exempt_target("127.0.0.1", 8000))
        self.assertFalse(downloader._is_exempt_target("127.0.0.1", None))

    def test_pot_provider_default_endpoint_is_exempt(self):
        """Regression: the guard blocked yt-dlp's default PO-token endpoint.

        The bgutil plugin targets http://127.0.0.1:4416 regardless of
        YTDLP_POT_PROVIDER_URL. With the variable unset the guard rejected it
        as loopback, so the provider was unreachable, mweb had no GVS token,
        and downloads degraded to a single 360p format.
        """
        saved_pot = downloader.YTDLP_POT_PROVIDER_URL
        try:
            downloader.YTDLP_POT_PROVIDER_URL = ""
            for host in ("127.0.0.1", "localhost", "::1", "localhost.localdomain"):
                self.assertTrue(
                    downloader._is_exempt_target(host, 4416),
                    f"{host}:4416 must reach the PO-token provider",
                )
            # No other loopback port is opened by this exemption.
            for port in (8000, 22, 6379, 5432, 9999):
                self.assertFalse(downloader._is_exempt_target("127.0.0.1", port))
            # A non-loopback host on the same port is unaffected.
            self.assertFalse(downloader._is_exempt_target("10.0.0.5", 4416))
        finally:
            downloader.YTDLP_POT_PROVIDER_URL = saved_pot

    def test_youtube_proxy_is_actually_applied(self):
        """Regression: YOUTUBE_PROXY_URL was set only in _apply_platform_options,
        which YouTube URLs never reach, so it was dead config."""
        saved = downloader.YOUTUBE_PROXY_URL
        try:
            downloader.YOUTUBE_PROXY_URL = "http://proxy.internal:3128"
            opts: dict = {}
            downloader._youtube_options(opts, client="tv")
            self.assertEqual(opts.get("proxy"), "http://proxy.internal:3128")
        finally:
            downloader.YOUTUBE_PROXY_URL = saved

    def test_find_downloaded_file_survives_concurrent_deletion(self):
        """Regression: max(..., key=os.path.getmtime) raised FileNotFoundError
        when cleanup deleted the file between listdir and getmtime."""
        with tempfile.TemporaryDirectory() as temp_dir:
            job_id = "race-job"
            a = os.path.join(temp_dir, f"{job_id}.mp4")
            b = os.path.join(temp_dir, f"{job_id}.mkv")
            for p in (a, b):
                with open(p, "w") as f:
                    f.write("x")

            real_getmtime = os.path.getmtime
            state = {"n": 0}

            def flaky_getmtime(path):
                state["n"] += 1
                # Vanish on the second candidate, mid-iteration.
                if state["n"] == 2 and os.path.exists(b):
                    os.unlink(b)
                return real_getmtime(path)

            with patch("os.path.getmtime", side_effect=flaky_getmtime):
                found = downloader._find_downloaded_file(temp_dir, job_id)
            self.assertIsNotNone(found)

    def test_sanitize_filename_windows_reserved(self):
        for reserved in ("CON", "PRN", "AUX", "NUL", "COM1", "LPT1"):
            sanitized = downloader._sanitize_filename(reserved)
            self.assertEqual(sanitized, f"_{reserved}")

    def test_sanitize_filename_strips_trailing_dots(self):
        sanitized = downloader._sanitize_filename("my_video....")
        self.assertEqual(sanitized, "my_video")

    def test_find_downloaded_file_ignores_temp_extensions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            job_id = "test-job-99"
            part_file = os.path.join(temp_dir, f"{job_id}.part")
            ytdl_file = os.path.join(temp_dir, f"{job_id}.ytdl")
            real_file = os.path.join(temp_dir, f"{job_id}.mp4")

            with open(part_file, "w") as f:
                f.write("part")
            with open(ytdl_file, "w") as f:
                f.write("ytdl")
            with open(real_file, "w") as f:
                f.write("real")

            found = downloader._find_downloaded_file(temp_dir, job_id)
            self.assertEqual(found, real_file)


class APITests(unittest.TestCase):
    """Isolated per-test temp DB: the modules under test open a SQLite
    connection on every request, and without this they would write
    anydown.db into the repository."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="an-down-api-test-")
        self._saved_db = os.environ.get("SQLITE_DB_PATH")
        os.environ["SQLITE_DB_PATH"] = os.path.join(self._tmp, "test.db")
        import database

        database.init_db()

    def tearDown(self):
        if self._saved_db is None:
            os.environ.pop("SQLITE_DB_PATH", None)
        else:
            os.environ["SQLITE_DB_PATH"] = self._saved_db
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)
        # Drop jobs created by tests so they do not leak into other modules.
        for job_id in list(getattr(main.job_manager, "_jobs", {})):
            main.job_manager._jobs.pop(job_id, None)

    def test_validate_url_syntax_rejects_disallowed(self):
        from fastapi import HTTPException
        invalid_urls = [
            "ftp://example.com/file",
            "file:///etc/passwd",
            "http://localhost/test",
            "http://127.0.0.1:8000/api",
            "http://192.168.1.1/router",
            "http://user:pass@example.com/video",
            "not-a-url",
        ]
        for url in invalid_urls:
            with self.assertRaises(HTTPException):
                main._validate_url_syntax(url)

    def test_validate_url_syntax_accepts_valid(self):
        valid = [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "http://example.com/video.mp4",
        ]
        for url in valid:
            host = main._validate_url_syntax(url)
            self.assertTrue(len(host) > 0)

    def test_throttle_mechanism(self):
        from fastapi import HTTPException
        table = {}
        ip = "192.0.2.1"
        # First call succeeds
        main._throttle(ip, table, 2.0)
        # Immediate second call is throttled
        with self.assertRaises(HTTPException) as ctx:
            main._throttle(ip, table, 2.0)
        self.assertEqual(ctx.exception.status_code, 429)

    def test_get_status_404_on_unknown(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            main.get_status("nonexistent-job-id")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_get_file_409_when_not_ready(self):
        from fastapi import HTTPException
        job = main.job_manager.create_job("https://example.com/video")
        with self.assertRaises(HTTPException) as ctx:
            main.get_file(job.id)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_start_download_returns_immediately_async(self):
        # Verify that start_download returns job_id immediately with status 'queued'
        payload = main.DownloadRequest(
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            audio_only=True,
        )
        mock_request = MagicMock()
        mock_request.client.host = "203.0.113.50"
        mock_request.headers = {}

        # start_download now verifies the media before queueing, so the
        # extraction must be stubbed: this test is about the async hand-off,
        # not about reaching YouTube.
        with patch.object(main, "_validate_public_url", return_value=None):
            with patch.object(main, "_get_or_fetch_info", return_value={"formats": []}):
                with patch.object(main, "_execute_download_job") as mock_exec:
                    res = asyncio.run(main.start_download(payload, mock_request))
                    self.assertIn("job_id", res)
                    self.assertEqual(res["status"], "queued")
                    job = main.job_manager.get(res["job_id"])
                    self.assertIsNotNone(job)

    def test_get_userscript_endpoint(self):
        # Verify /anydown.user.js returns the userscript file with application/javascript
        response = main.get_userscript()
        self.assertEqual(response.media_type, "application/javascript")
        # attachment (not inline) so the link saves the script to disk instead
        # of rendering the JavaScript source as a readable page.
        self.assertEqual(
            response.headers.get("content-disposition"),
            'attachment; filename="anydown-assistant.user.js"',
        )
        self.assertTrue(os.path.exists(response.path))


class InfoCoalescingTests(unittest.TestCase):
    """Regression: a cancelled leader must not poison its URL forever.

    _get_or_fetch_info settles the shared future and removes the _info_inflight
    entry. When that cleanup ran on two separate code paths, a leader cancelled
    between the fetch returning and the cache write left a pending future in
    _info_inflight. Every later request for that URL awaited a future that
    could never complete and never attempted a fetch again, wedging the URL for
    the lifetime of the process.
    """

    URL = "https://example.invalid/video"

    def setUp(self):
        self._orig_fetch = main.fetch_info
        self._orig_cache = dict(main._info_cache)
        main._info_cache.clear()
        main._info_inflight.clear()

    def tearDown(self):
        main.fetch_info = self._orig_fetch
        main._info_cache.clear()
        main._info_cache.update(self._orig_cache)
        main._info_inflight.clear()

    def test_cancelled_leader_does_not_wedge_the_url(self):
        async def scenario():
            calls = []

            def slow_fetch(url):
                calls.append(url)
                time.sleep(0.5)
                return {"formats": []}

            main.fetch_info = slow_fetch

            leader = asyncio.create_task(main._get_or_fetch_info(self.URL))
            await asyncio.sleep(0.1)  # leader has registered its future
            self.assertIn(self.URL, main._info_inflight)

            # Cancel while the leader is between the fetch and the cache write.
            await main._info_cache_lock.acquire()
            await asyncio.sleep(0.6)
            leader.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await leader
            main._info_cache_lock.release()

            # The in-flight entry must be gone, or the URL stays poisoned.
            self.assertNotIn(self.URL, main._info_inflight)

            # A fresh request must still be able to fetch.
            main.fetch_info = lambda url: (calls.append(url), {"formats": []})[1]
            info = await asyncio.wait_for(main._get_or_fetch_info(self.URL), timeout=3.0)
            self.assertEqual(info, {"formats": []})
            self.assertEqual(len(calls), 2)

        asyncio.run(scenario())

    def test_failed_leader_propagates_to_waiters(self):
        async def scenario():
            def boom(url):
                raise UnsupportedURLError("nope")

            main.fetch_info = boom
            with self.assertRaises(UnsupportedURLError):
                await main._get_or_fetch_info(self.URL)
            self.assertNotIn(self.URL, main._info_inflight)

            # A later request must retry rather than reuse the dead future.
            main.fetch_info = lambda url: {"formats": [{"format_id": "18"}]}
            info = await asyncio.wait_for(main._get_or_fetch_info(self.URL), timeout=3.0)
            self.assertEqual(info["formats"][0]["format_id"], "18")

        asyncio.run(scenario())

    def test_successful_fetch_populates_cache_and_clears_inflight(self):
        async def scenario():
            main.fetch_info = lambda url: {"formats": [{"format_id": "137"}]}
            info = await main._get_or_fetch_info(self.URL)
            self.assertEqual(info["formats"][0]["format_id"], "137")
            self.assertIn(self.URL, main._info_cache)
            self.assertNotIn(self.URL, main._info_inflight)

            # Second call is served from cache without re-fetching.
            calls = []
            main.fetch_info = lambda url: calls.append(url)
            again = await main._get_or_fetch_info(self.URL)
            self.assertEqual(again["formats"][0]["format_id"], "137")
            self.assertEqual(calls, [])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main(verbosity=2)
