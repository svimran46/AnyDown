"""Exercise the real downloader.py code path against a real YouTube URL.

Run: python -m unittest tests/test_youtube_e2e.py -v
Or:  RUN_E2E_TESTS=1 python -m unittest discover -s tests -v
Requires: pip install -r requirements.txt and outbound internet access.
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from downloader import download_media, fetch_info  # noqa: E402

URL = "https://www.youtube.com/watch?v=EBs3jouCN0Q"


def _safe_print(*args) -> None:
    text = " ".join(str(a) for a in args)
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="replace").decode("ascii"))


class YouTubeE2ETests(unittest.TestCase):
    def setUp(self):
        self.out_dir = tempfile.mkdtemp(prefix="an-down-test-")

    def tearDown(self):
        if os.path.exists(self.out_dir):
            shutil.rmtree(self.out_dir, ignore_errors=True)

    @unittest.skipUnless(
        os.getenv("RUN_E2E_TESTS") == "1" or sys.argv[0].endswith("test_youtube_e2e.py"),
        "Skipping real network E2E test; run directly or set RUN_E2E_TESTS=1",
    )
    def test_youtube_fetch_and_download(self):
        info = fetch_info(URL)
        _safe_print("== fetch_info OK ==")
        _safe_print("title:", info.get("title"))
        _safe_print("uploader:", info.get("uploader"))
        _safe_print("duration:", info.get("duration"))
        _safe_print("formats offered:", len(info.get("formats", [])))
        self.assertIn("title", info)
        self.assertTrue(len(info.get("formats", [])) > 0)

        path, name = download_media(URL, self.out_dir, "job-test-1", audio_only=True)
        self.assertTrue(os.path.exists(path), f"File {path} does not exist")
        size = os.path.getsize(path)
        _safe_print("file:", path)
        _safe_print("display name:", name)
        _safe_print("bytes:", size)
        self.assertGreater(size, 100_000, "Downloaded file suspiciously small")


if __name__ == "__main__":
    os.environ["RUN_E2E_TESTS"] = "1"
    unittest.main(verbosity=2)
