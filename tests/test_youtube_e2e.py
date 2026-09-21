"""Exercise the real downloader.py code path against the previously failing YouTube URL.

Run: python3 tests/test_youtube_e2e.py
Requires: pip install -r requirements.txt and outbound internet access.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from downloader import download_media, fetch_info  # noqa: E402

URL = "https://www.youtube.com/watch?v=EBs3jouCN0Q"
OUT = "/tmp/an-down-test"

info = fetch_info(URL)
print("== fetch_info OK ==")
print("title:", info["title"])
print("uploader:", info.get("uploader"))
print("duration:", info.get("duration"))
print("formats offered:", len(info["formats"]))
for f in info["formats"][:6]:
    print("  ", f["format_id"], f.get("resolution") or f.get("ext"),
          "video" if f["has_video"] else "audio", f.get("filesize"))

print()
print("== download_media (audio_only) ==")
path, name = download_media(URL, OUT, "job-test-1", audio_only=True)
size = os.path.getsize(path)
print("file:", path)
print("display name:", name)
print("bytes:", size)
assert size > 100_000, "downloaded file suspiciously small"
print("== ALL OK ==")
