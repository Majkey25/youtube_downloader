from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as target


class AppTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        target.DOWNLOAD_PATH = Path(self.temp_dir.name)
        target.app.config.update(TESTING=True)
        self.client = target.app.test_client()

    def test_accepts_direct_video_urls_only(self) -> None:
        self.assertTrue(target.is_youtube_url("https://youtu.be/jNQXAC9IVRw"))
        self.assertTrue(
            target.is_youtube_url("https://www.youtube.com/watch?v=jNQXAC9IVRw")
        )
        self.assertFalse(
            target.is_youtube_url("https://www.youtube.com/playlist?list=PL123")
        )
        self.assertFalse(
            target.is_youtube_url("https://youtube.com.evil.test/watch?v=jNQXAC9IVRw")
        )

    def test_rejects_cross_origin_and_oversized_requests(self) -> None:
        forbidden = self.client.post(
            "/download",
            data={"youtubeLink": "https://youtu.be/jNQXAC9IVRw"},
            headers={"Origin": "https://evil.test"},
        )
        self.assertEqual(forbidden.status_code, 403)

        oversized = self.client.post(
            "/download",
            data={
                "youtubeLink": "https://youtu.be/jNQXAC9IVRw",
                "padding": "x" * 20_000,
            },
        )
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(oversized.get_json(), {"error": "Request is too large."})

    def test_generated_file_is_deleted_after_delivery(self) -> None:
        unrelated = target.DOWNLOAD_PATH / "private.txt"
        unrelated.write_text("private", encoding="utf-8")
        self.assertIsNone(target.resolve_download_path(unrelated.name))
        self.assertEqual(
            self.client.get(f"/downloads/{unrelated.name}").status_code,
            400,
        )

        artifact = target.DOWNLOAD_PATH / f"yt-download-test-{'a' * 32}.mp4.part"
        artifact.write_bytes(b"partial")
        os.utime(artifact, (0, 0))
        with patch.object(target, "STALE_FILE_AGE_SECONDS", 1):
            target.cleanup_stale_downloads()
        self.assertFalse(artifact.exists())
        self.assertTrue(unrelated.exists())

        generated = target.DOWNLOAD_PATH / f"yt-download-test-{'b' * 32}.mp4"
        generated.write_bytes(b"media")
        response = self.client.get(f"/downloads/{generated.name}", buffered=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"media")
        response.close()
        self.assertFalse(generated.exists())


if __name__ == "__main__":
    unittest.main()
