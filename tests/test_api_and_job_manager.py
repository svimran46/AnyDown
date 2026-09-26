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

    def test_cleanup_stuck_jobs(self):
        job = self.jm.create_job("https://example.com/stuck")
        job.updated_at = time.time() - 10  # Exceeded TTL while still queued
        self.jm.cleanup_expired(self.temp_dir)
        self.assertIsNone(self.jm.get(job.id))

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

    def test_is_exempt_target_denies_all_when_unconfigured(self):
        downloader.YTDLP_POT_PROVIDER_URL = ""
        downloader.FACEBOOK_PROXY_URL = ""
        self.assertFalse(downloader._is_exempt_target("127.0.0.1", 4416))
        self.assertFalse(downloader._is_exempt_target("localhost", 80))

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

        with patch.object(main, "_validate_public_url", return_value=None):
            with patch.object(main, "_execute_download_job") as mock_exec:
                res = asyncio.run(main.start_download(payload, mock_request))
                self.assertIn("job_id", res)
                self.assertEqual(res["status"], "queued")
                job = main.job_manager.get(res["job_id"])
                self.assertIsNotNone(job)


if __name__ == "__main__":
    unittest.main(verbosity=2)
