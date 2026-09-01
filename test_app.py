from __future__ import annotations

import importlib
import json
import multiprocessing
import os
import socket
import tempfile
import unittest
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import ANY, MagicMock, call, patch

from flask.testing import FlaskClient
from youtube_dl.downloader import get_suitable_downloader
from youtube_dl.downloader.http import HttpFD

import app as target
import config

YOUTUBE_URL = "https://youtu.be/jNQXAC9IVRw"
PLAYLIST_URL = "https://www.youtube.com/playlist?list=PL123"
SOUNDCLOUD_URL = "https://soundcloud.com/example/track"


def _cleanup_in_child(download_dir: str) -> None:
    target.DOWNLOAD_PATH = Path(download_dir)
    target.ACTIVE_DOWNLOAD_CLAIMS.clear()
    target.cleanup_stale_downloads()


def _try_operation_lock_in_child(download_dir: str, result_path: str) -> None:
    target.DOWNLOAD_PATH = Path(download_dir)
    handle = target._acquire_operation_lock()
    Path(result_path).write_text(
        "acquired" if handle is not None else "busy",
        encoding="utf-8",
    )
    if handle is not None:
        target._close_locked(handle)


def _close_fd(fd: int) -> None:
    with suppress(OSError):
        os.close(fd)


class AppTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.original_download_path = target.DOWNLOAD_PATH
        self.addCleanup(setattr, target, "DOWNLOAD_PATH", self.original_download_path)
        target.DOWNLOAD_PATH = Path(self.temp_dir.name)
        target.app.config.update(TESTING=True)
        with target.DOWNLOAD_ATTEMPTS_LOCK:
            target.DOWNLOAD_ATTEMPTS.clear()
            target.GLOBAL_OPERATION_ATTEMPTS.clear()
        with target.DOWNLOAD_FILES_LOCK:
            target.ACTIVE_DOWNLOAD_CLAIMS.clear()
            for handle in target.ORPHANED_OPERATION_LOCKS:
                target._close_locked(handle)
            target.ORPHANED_OPERATION_LOCKS.clear()
            for handle in target.ORPHANED_JOB_LOCKS:
                target._close_locked(handle)
            target.ORPHANED_JOB_LOCKS.clear()
            target.ORPHANED_JOB_PROCESSES.clear()
        self.client: FlaskClient = target.app.test_client()
        self.headers = {"Origin": "http://localhost"}

    def tearDown(self) -> None:
        for handles in (
            target.ORPHANED_OPERATION_LOCKS,
            target.ORPHANED_JOB_LOCKS,
        ):
            for handle in handles:
                if not handle.closed:
                    target._close_locked(handle)
            handles.clear()
        target.ORPHANED_JOB_PROCESSES.clear()

    def test_disables_plugins_before_importing_ytdlp(self) -> None:
        source = Path(target.__file__).read_text(encoding="utf-8")

        self.assertEqual(os.environ.get("YTDLP_NO_PLUGINS"), "1")
        self.assertIn('os.environ["YTDLP_NO_PLUGINS"] = "1"', source)
        self.assertLess(source.index("YTDLP_NO_PLUGINS"), source.index("yt_dlp"))

    def test_worker_watchdog_starts_before_third_party_imports(self) -> None:
        source = Path(target.__file__).read_text(encoding="utf-8")

        watchdog_start = source.index("Thread(target=_watch_parent")
        self.assertLess(watchdog_start, source.index("from flask import"))
        self.assertLess(watchdog_start, source.index("from yt_dlp import"))

    @staticmethod
    def media_info(
        *,
        title: str = "Title",
        extractor_key: str = "Youtube",
        duration: int | None = 120,
        formats: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        if formats is None:
            formats = [
                {"format_id": "audio", "acodec": "mp4a.40.2", "vcodec": "none"},
                {"format_id": "video", "acodec": "none", "vcodec": "avc1"},
            ]
        formats = [
            {
                "url": f"https://example.com/{item.get('format_id', 'media')}",
                "protocol": "https",
                **item,
            }
            for item in formats
        ]
        return {
            "id": "jNQXAC9IVRw",
            "title": title,
            "extractor_key": extractor_key,
            "duration": duration,
            "is_live": False,
            "formats": formats,
        }

    @staticmethod
    def media_response(
        outputs: list[str] | None = None,
        *,
        engine: str = "yt-dlp",
    ) -> dict[str, object]:
        return {
            "kind": "media",
            "source": "YouTube",
            "title": "Title",
            "duration_seconds": 120,
            "is_live": False,
            "outputs": outputs if outputs is not None else ["audio", "video"],
            "engine": engine,
        }

    @staticmethod
    def downloaded_file(mode: str = "audio") -> dict[str, object]:
        extension = "mp3" if mode == "audio" else "webm"
        return {
            "name": f"yt-download-title-{'a' * 32}.{extension}",
            "extension": extension,
            "mode": mode,
            "size_bytes": 5,
        }

    def test_validates_only_public_web_urls(self) -> None:
        for value in (
            "https://example.com/media",
            "https://8.8.8.8/media",
            YOUTUBE_URL,
        ):
            with self.subTest(value=value):
                self.assertEqual(target.validate_public_url(value), value)

        for value in (
            "file:///etc/passwd",
            "http://localhost/media",
            "http://intranet/media",
            "http://127.0.0.1/media",
            "http://192.168.1.2/media",
            "http://[::1]/media",
            "https://user:pass@example.com/media",
            "https://example.com:8443/media",
        ):
            with self.subTest(value=value), self.assertRaises(target.InputError):
                target.validate_public_url(value)

    def test_outbound_guard_rejects_private_dns_results(self) -> None:
        for address in ("127.0.0.1", "224.0.0.1"):
            result = [
                (
                    socket.AddressFamily.AF_INET,
                    socket.SocketKind.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    (address, 443),
                )
            ]
            with (
                self.subTest(address=address),
                patch.object(
                    target.socket,
                    "getaddrinfo",
                    return_value=result,
                ) as resolver,
                target.public_network_only(),
                self.assertRaises(OSError),
            ):
                target.socket.getaddrinfo(
                    "blocked.example",
                    443,
                    type=socket.SocketKind.SOCK_STREAM,
                )

            resolver.assert_called_once()

    def test_outbound_guard_blocks_proxy_env_and_non_web_ports(self) -> None:
        public_result = [
            (
                socket.AddressFamily.AF_INET,
                socket.SocketKind.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("8.8.8.8", 22),
            )
        ]
        with (
            patch.dict(
                os.environ,
                {"HTTPS_PROXY": "http://proxy.example:8080"},
                clear=False,
            ),
            patch.object(
                target.socket,
                "getaddrinfo",
                return_value=public_result,
            ) as resolver,
        ):
            with target.public_network_only():
                self.assertNotIn("HTTPS_PROXY", os.environ)
                with self.assertRaises(OSError):
                    target.socket.getaddrinfo(
                        "public.example",
                        22,
                        type=socket.SocketKind.SOCK_STREAM,
                    )
            self.assertEqual(
                os.environ["HTTPS_PROXY"],
                "http://proxy.example:8080",
            )

        resolver.assert_not_called()

    def test_media_subprocess_guard_covers_probe_and_every_ffmpeg_input(
        self,
    ) -> None:
        self.assertEqual(
            target.guard_media_command(["ffprobe", "file:unsafe.mp4"]),
            [
                "ffprobe",
                "-protocol_whitelist",
                "file,crypto,data",
                "file:unsafe.mp4",
            ],
        )
        self.assertEqual(
            target.guard_media_command(
                ["ffmpeg", "-i", "file:first", "-i", "file:second", "file:out"]
            ),
            [
                "ffmpeg",
                "-protocol_whitelist",
                "file,crypto,data",
                "-i",
                "file:first",
                "-protocol_whitelist",
                "file,crypto,data",
                "-i",
                "file:second",
                "file:out",
            ],
        )
        self.assertEqual(
            target.guard_media_command([b"ffprobe", b"file:unsafe.mp4"]),
            [
                b"ffprobe",
                b"-protocol_whitelist",
                b"file,crypto,data",
                b"file:unsafe.mp4",
            ],
        )
        with self.assertRaisesRegex(target.EngineError, "protocol policy"):
            target.guard_media_command(
                ["ffprobe", "-protocol_whitelist=http,file", "file:unsafe.mp4"]
            )
        with self.assertRaisesRegex(target.EngineError, "PhantomJS"):
            target.guard_media_command(["phantomjs", "extractor.js"])

    def test_outbound_guard_patches_modern_and_legacy_media_launches(self) -> None:
        modern_commands: list[object] = []
        legacy_commands: list[object] = []
        original_descriptor = target.YtDlpPopen.__dict__["run"]

        def fake_modern_run(
            _cls: type[object],
            command: object,
            *_args: object,
            **_kwargs: object,
        ) -> tuple[None, None, int]:
            modern_commands.append(command)
            return None, None, 0

        class FakeLegacyPopen:
            def __init__(
                self,
                command: object,
                *_args: object,
                **_kwargs: object,
            ) -> None:
                legacy_commands.append(command)

        setattr(  # noqa: B010
            target.YtDlpPopen,
            "run",
            classmethod(fake_modern_run),
        )
        try:
            with (
                patch.object(target.subprocess, "Popen", FakeLegacyPopen),
                target.public_network_only(),
            ):
                target.YtDlpPopen.run(["ffprobe", "file:unsafe.mp4"])
                target.subprocess.Popen(
                    ["ffmpeg", "-i", "file:unsafe.mp4", "file:out.mp3"]
                )
        finally:
            setattr(  # noqa: B010
                target.YtDlpPopen,
                "run",
                original_descriptor,
            )

        self.assertEqual(
            modern_commands,
            [
                [
                    "ffprobe",
                    "-protocol_whitelist",
                    "file,crypto,data",
                    "file:unsafe.mp4",
                ]
            ],
        )
        self.assertEqual(
            legacy_commands,
            [
                [
                    "ffmpeg",
                    "-protocol_whitelist",
                    "file,crypto,data",
                    "-i",
                    "file:unsafe.mp4",
                    "file:out.mp3",
                ]
            ],
        )

    def test_identifies_only_direct_youtube_video_urls(self) -> None:
        self.assertTrue(target.is_youtube_url(YOUTUBE_URL))
        self.assertTrue(
            target.is_youtube_url(
                "https://www.youtube.com/watch?v=jNQXAC9IVRw"
            )
        )
        self.assertFalse(target.is_youtube_url(PLAYLIST_URL))
        self.assertFalse(
            target.is_youtube_url(
                "https://youtube.com.evil.test/watch?v=jNQXAC9IVRw"
            )
        )

    def test_maps_video_and_audio_capabilities(self) -> None:
        response = target.media_response(self.media_info(), YOUTUBE_URL, "yt-dlp")

        self.assertEqual(response, self.media_response())

    def test_maps_audio_only_capability(self) -> None:
        info = self.media_info(
            extractor_key="Soundcloud",
            formats=[
                {"format_id": "audio", "acodec": "opus", "vcodec": "none"}
            ],
        )

        response = target.media_response(info, SOUNDCLOUD_URL, "yt-dlp")

        self.assertEqual(response["source"], "SoundCloud")
        self.assertEqual(response["outputs"], ["audio"])

    def test_maps_video_only_capability(self) -> None:
        info = self.media_info(
            extractor_key="Vimeo",
            formats=[
                {"format_id": "video", "acodec": "none", "vcodec": "avc1"}
            ],
        )

        response = target.media_response(
            info,
            "https://vimeo.com/123456789",
            "yt-dlp",
        )

        self.assertEqual(response["source"], "Vimeo")
        self.assertEqual(response["outputs"], ["video"])

    def test_capabilities_exclude_unsafe_transport_formats(self) -> None:
        formats: list[dict[str, object]] = [
            {
                "format_id": "audio",
                "protocol": "https",
                "url": "https://media.example/audio.m4a",
                "acodec": "aac",
                "vcodec": "none",
            },
            {
                "format_id": "video",
                "protocol": "rtmp",
                "url": "rtmp://media.example/video",
                "acodec": "none",
                "vcodec": "h264",
            },
        ]
        info = self.media_info(formats=formats)

        response = target.media_response(info, YOUTUBE_URL, "yt-dlp")

        self.assertEqual(response["outputs"], ["audio"])
        info["formats"] = [formats[1]]
        with self.assertRaises(target.UnsupportedMediaError):
            target.media_response(info, YOUTUBE_URL, "yt-dlp")
        info["formats"] = [formats[0]]
        with (
            patch.object(target, "_RESOLVE_DOWNLOADER", return_value=object),
            self.assertRaises(target.UnsupportedMediaError),
        ):
            target.media_response(info, YOUTUBE_URL, "yt-dlp")

    def test_maps_normalized_extensions_when_codecs_are_omitted(self) -> None:
        info = self.media_info(
            extractor_key="Imgur",
            formats=[
                {
                    "format_id": "gifv",
                    "video_ext": "mp4",
                    "audio_ext": "none",
                    "url": "https://i.imgur.com/A61SaA1.mp4",
                }
            ],
        )

        response = target.media_response(
            info,
            "https://imgur.com/A61SaA1",
            "yt-dlp",
        )

        self.assertEqual(response["source"], "Imgur")
        self.assertEqual(response["outputs"], ["video"])

    def test_maps_single_direct_format_without_formats_list(self) -> None:
        info = self.media_info(extractor_key="Vocaroo")
        info.pop("formats")
        info.update(
            {
                "url": "https://media.example/audio.mp3",
                "protocol": "https",
                "ext": "mp3",
                "acodec": "mp3",
                "vcodec": "none",
            }
        )

        response = target.media_response(
            info,
            "https://vocaroo.com/1de8yA3LNe77",
            "yt-dlp",
        )

        self.assertEqual(response["source"], "vocaroo.com")
        self.assertEqual(response["outputs"], ["audio"])

    def test_rejects_malformed_or_missing_formats(self) -> None:
        for formats in (
            None,
            "not-a-list",
            {"vcodec": "avc1", "acodec": "mp4a"},
            [None, {"vcodec": "none", "acodec": "none"}],
        ):
            info = self.media_info()
            info["formats"] = formats
            with (
                self.subTest(formats=formats),
                self.assertRaises(target.UnsupportedMediaError),
            ):
                target.media_response(info, YOUTUBE_URL, "yt-dlp")

    def test_rejects_spotify_before_primary_engine(self) -> None:
        with (
            patch.object(target, "inspect_with_ytdlp") as primary,
            patch.object(target, "inspect_with_legacy") as legacy,
            self.assertRaises(target.UnsupportedMediaError),
        ):
            target.inspect_media(
                "https://open.spotify.com/track/1234567890123456789012"
            )

        primary.assert_not_called()
        legacy.assert_not_called()

    def test_bounds_playlist_entries_and_uses_one_based_indexes(self) -> None:
        info: dict[str, object] = {
            "_type": "playlist",
            "title": "Playlist",
            "extractor_key": "YoutubePlaylist",
            "entries": [
                {
                    "_type": "url",
                    "id": str(index),
                    "title": f"Episode {index}",
                    "url": f"https://example.test/{index}",
                }
                for index in range(1, 52)
            ],
        }

        with patch.object(target, "MAX_PLAYLIST_ITEMS", 50):
            response = target.playlist_response(info, PLAYLIST_URL)

        self.assertEqual(response["kind"], "playlist")
        self.assertEqual(response["source"], "YouTube")
        self.assertEqual(response["title"], "Playlist")
        self.assertEqual(response["engine"], "yt-dlp")
        self.assertEqual(len(response["entries"]), 50)
        self.assertEqual(response["entries"][0], {"index": 1, "title": "Episode 1"})
        self.assertEqual(
            response["entries"][-1],
            {"index": 50, "title": "Episode 50"},
        )
        self.assertIs(response["truncated"], True)

    def test_inspects_selected_playlist_item_by_one_based_index(self) -> None:
        selected = self.media_info(title="Episode 1")
        with patch.object(
            target,
            "inspect_with_ytdlp",
            return_value=selected,
        ) as primary:
            response = target.inspect_media(PLAYLIST_URL, 1)

        primary.assert_called_once_with(PLAYLIST_URL, 1)
        self.assertEqual(response["kind"], "media")
        self.assertEqual(response["title"], "Episode 1")

    def test_primary_inspection_uses_bounded_safe_extractor_options(self) -> None:
        for playlist_index in (None, 2):
            primary = MagicMock()
            primary.__enter__.return_value = primary
            selected = self.media_info(title="Episode 2")
            primary.extract_info.return_value = (
                selected
                if playlist_index is None
                else {
                    "_type": "playlist",
                    "title": "Playlist",
                    "entries": [selected],
                }
            )
            with (
                self.subTest(playlist_index=playlist_index),
                patch.object(target, "MAX_PLAYLIST_ITEMS", 50),
                patch.object(target, "YtDlp", return_value=primary) as constructor,
                patch.object(target, "LegacyYoutubeDL") as legacy,
            ):
                result = target.inspect_with_ytdlp(PLAYLIST_URL, playlist_index)

            options = constructor.call_args.args[0]
            self.assertEqual(
                options["allowed_extractors"],
                ["default", "-generic"],
            )
            self.assertIs(options["cachedir"], False)
            self.assertIs(options["enable_file_urls"], False)
            self.assertEqual(options["external_downloader"], {"default": "native"})
            self.assertEqual(options["js_runtimes"], {"deno": {}})
            self.assertEqual(options["proxy"], "")
            self.assertEqual(options["remote_components"], [])
            self.assertIs(options["noprogress"], True)
            self.assertIs(options["hls_prefer_native"], True)
            primary.extract_info.assert_called_once_with(
                PLAYLIST_URL,
                download=False,
            )
            if playlist_index is None:
                self.assertEqual(options["extract_flat"], "in_playlist")
                self.assertEqual(options["playlistend"], 51)
                self.assertNotIn("playlist_items", options)
            else:
                self.assertIs(options["extract_flat"], False)
                self.assertEqual(options["playlist_items"], "2")
                self.assertNotIn("playlistend", options)
                self.assertEqual(result, selected)
            legacy.assert_not_called()

    def test_legacy_downloader_only_accepts_direct_format_18(self) -> None:
        downloader = object.__new__(target.LegacyYoutubeDL)
        with patch.object(target.BaseLegacyYoutubeDL, "process_info") as parent:
            downloader.process_info(
                {
                    "format_id": "18",
                    "protocol": "https",
                    "url": "https://example.com/video.mp4",
                }
            )

        parent.assert_called_once()
        invalid_infos: tuple[dict[str, object], ...] = (
            {"format_id": "22", "protocol": "https"},
            {"format_id": "18", "protocol": "m3u8_native"},
            {
                "format_id": "18",
                "protocol": "https",
                "url": "file:///tmp/video.mp4",
            },
        )
        for info in invalid_infos:
            with (
                self.subTest(info=info),
                self.assertRaisesRegex(target.LegacyDownloadError, "direct format 18"),
            ):
                downloader.process_info(info)

    def test_legacy_inspection_exposes_only_selected_direct_format_18(self) -> None:
        legacy = MagicMock()
        legacy.__enter__.return_value = legacy
        selected = self.media_info()
        selected.update(
            {
                "format_id": "18",
                "protocol": "https",
                "url": "https://example.com/video.mp4",
                "acodec": "mp4a.40.2",
                "vcodec": "avc1",
                "formats": [
                    {
                        "format_id": "22",
                        "protocol": "https",
                        "url": "https://example.com/hd.mp4",
                        "acodec": "aac",
                        "vcodec": "avc1",
                    }
                ],
            }
        )
        legacy.extract_info.return_value = selected
        with patch.object(
            target, "LegacyYoutubeDL", return_value=legacy
        ) as constructor:
            result = target.inspect_with_legacy(YOUTUBE_URL)

        options = constructor.call_args.args[0]
        self.assertIs(options["cachedir"], False)
        self.assertEqual(options["format"], "18")
        self.assertNotIn("formats", result)
        self.assertEqual(result["format_id"], "18")
        self.assertEqual(
            target.media_response(result, YOUTUBE_URL, "youtube-dl fallback")[
                "outputs"
            ],
            ["audio", "video"],
        )

        invalid_selections = (
            {"format_id": "22", "protocol": "https", "url": "https://x.test/v"},
            {
                "format_id": "18",
                "protocol": "m3u8_native",
                "url": "https://x.test/v.m3u8",
            },
        )
        for invalid in invalid_selections:
            legacy.extract_info.return_value = invalid
            with (
                self.subTest(invalid=invalid),
                patch.object(target, "LegacyYoutubeDL", return_value=legacy),
                self.assertRaises(target.EngineError),
            ):
                target.inspect_with_legacy(YOUTUBE_URL)

    def test_rejects_active_live_but_allows_ended_live(self) -> None:
        for live_status in ("is_live", "is_upcoming"):
            active = self.media_info(duration=None)
            active.update(
                {"is_live": live_status == "is_live", "live_status": live_status}
            )
            with (
                self.subTest(live_status=live_status),
                self.assertRaises(target.UnsupportedMediaError),
            ):
                target.media_response(active, YOUTUBE_URL, "yt-dlp")

        ended = self.media_info()
        ended.update({"is_live": False, "live_status": "was_live", "was_live": True})
        response = target.media_response(ended, YOUTUBE_URL, "yt-dlp")
        self.assertIs(response["is_live"], False)

    def test_rejects_known_over_duration_media(self) -> None:
        with (
            patch.object(target, "MAX_DURATION_SECONDS", 120),
            self.assertRaises(target.MediaLimitError),
        ):
            target.media_response(
                self.media_info(duration=121),
                YOUTUBE_URL,
                "yt-dlp",
            )

    def test_allows_unknown_duration_for_non_live_media(self) -> None:
        response = target.media_response(
            self.media_info(duration=None),
            YOUTUBE_URL,
            "yt-dlp",
        )

        self.assertIsNone(response["duration_seconds"])
        self.assertIs(response["is_live"], False)

    def test_inspect_requires_allowed_origin(self) -> None:
        with patch.object(target, "run_job") as run_job:
            response = self.client.post("/inspect", json={"url": YOUTUBE_URL})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(set(response.get_json()), {"error"})
        run_job.assert_not_called()

    def test_cors_preflight_allows_json_only_for_configured_origin(self) -> None:
        preflight = {
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type",
        }
        allowed = self.client.options(
            "/inspect",
            headers={**preflight, "Origin": "https://majkey25.github.io"},
        )
        denied = self.client.options(
            "/inspect",
            headers={**preflight, "Origin": "https://evil.test"},
        )

        self.assertEqual(
            allowed.headers.get("Access-Control-Allow-Origin"),
            "https://majkey25.github.io",
        )
        self.assertIn(
            "Content-Type",
            allowed.headers.get("Access-Control-Allow-Headers", ""),
        )
        self.assertNotIn("Access-Control-Allow-Origin", denied.headers)

    def test_inspect_requires_a_json_object(self) -> None:
        with patch.object(target, "run_job") as run_job:
            form_response = self.client.post(
                "/inspect",
                data={"url": YOUTUBE_URL},
                headers=self.headers,
            )
            list_response = self.client.post(
                "/inspect",
                json=[YOUTUBE_URL],
                headers=self.headers,
            )

        self.assertEqual(form_response.status_code, 400)
        self.assertEqual(set(form_response.get_json()), {"error"})
        self.assertEqual(list_response.status_code, 400)
        self.assertEqual(set(list_response.get_json()), {"error"})
        run_job.assert_not_called()

    def test_inspect_returns_media_and_passes_playlist_index(self) -> None:
        expected = self.media_response()
        with (
            patch.object(
                target,
                "run_job",
                return_value=expected,
            ) as run_job,
            patch.object(target, "cleanup_stale_downloads") as cleanup,
        ):
            response = self.client.post(
                "/inspect",
                json={"url": PLAYLIST_URL, "playlist_index": 2},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), expected)
        cleanup.assert_called_once_with()
        run_job.assert_called_once_with("inspect", PLAYLIST_URL, 2, None, ANY)

    def test_inspect_uses_shared_slot_and_maps_contract_errors(self) -> None:
        with (
            patch.object(target, "run_job") as run_job,
            patch.object(
                target.DOWNLOAD_SLOT,
                "acquire",
                return_value=False,
            ) as acquire,
            patch.object(target.DOWNLOAD_SLOT, "release") as release,
        ):
            response = self.client.post(
                "/inspect",
                json={"url": YOUTUBE_URL},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 429)
        acquire.assert_called_once_with(blocking=False)
        run_job.assert_not_called()
        release.assert_not_called()

        cases = (
            (target.InputError("bad input"), 400),
            (target.MediaLimitError("too large"), 413),
            (target.UnsupportedMediaError("unsupported"), 422),
            (target.EngineError("engine failed"), 502),
            (target.OperationTimeoutError("timed out"), 504),
        )
        for error, status in cases:
            with target.DOWNLOAD_ATTEMPTS_LOCK:
                target.DOWNLOAD_ATTEMPTS.clear()
            with (
                self.subTest(error=type(error).__name__),
                patch.object(target, "run_job", side_effect=error),
                patch.object(target, "cleanup_stale_downloads"),
                patch.object(
                    target.DOWNLOAD_SLOT,
                    "acquire",
                    return_value=True,
                ) as acquire,
                patch.object(target.DOWNLOAD_SLOT, "release") as release,
            ):
                response = self.client.post(
                    "/inspect",
                    json={"url": YOUTUBE_URL},
                    headers=self.headers,
                )

            self.assertEqual(response.status_code, status)
            self.assertEqual(set(response.get_json()), {"error"})
            acquire.assert_called_once_with(blocking=False)
            release.assert_called_once_with()

    def test_json_routes_reject_invalid_playlist_indexes_before_engine(self) -> None:
        with (
            patch.object(target, "MAX_PLAYLIST_ITEMS", 50),
            patch.object(target, "run_job") as run_job,
        ):
            for endpoint in ("/inspect", "/download"):
                for playlist_index in (True, 0, 51):
                    payload: dict[str, object] = {
                        "url": PLAYLIST_URL,
                        "playlist_index": playlist_index,
                    }
                    if endpoint == "/download":
                        payload["mode"] = "audio"
                    with self.subTest(
                        endpoint=endpoint,
                        playlist_index=playlist_index,
                    ):
                        response = self.client.post(
                            endpoint,
                            json=payload,
                            headers=self.headers,
                        )
                        self.assertEqual(response.status_code, 400)
                        self.assertEqual(set(response.get_json()), {"error"})

        run_job.assert_not_called()

    def test_inspection_falls_back_for_direct_youtube_only(self) -> None:
        legacy_info = self.media_info()
        legacy_info.update(
            {
                "format_id": "18",
                "protocol": "https",
                "url": "https://example.com/video.mp4",
                "acodec": "mp4a.40.2",
                "vcodec": "avc1",
            }
        )
        legacy_info.pop("formats")
        with (
            patch.object(
                target,
                "inspect_with_ytdlp",
                side_effect=target.EngineError("primary failed"),
            ) as primary,
            patch.object(
                target,
                "inspect_with_legacy",
                return_value=legacy_info,
            ) as legacy,
        ):
            response = target.inspect_media(YOUTUBE_URL)

        primary.assert_called_once_with(YOUTUBE_URL, None)
        legacy.assert_called_once_with(YOUTUBE_URL)
        self.assertEqual(response["engine"], "youtube-dl fallback")

        for link, playlist_index in ((SOUNDCLOUD_URL, None), (YOUTUBE_URL, 1)):
            with (
                self.subTest(link=link, playlist_index=playlist_index),
                patch.object(
                    target,
                    "inspect_with_ytdlp",
                    side_effect=target.EngineError("primary failed"),
                ),
                patch.object(target, "inspect_with_legacy") as disallowed_legacy,
                self.assertRaises(target.EngineError),
            ):
                target.inspect_media(link, playlist_index)
            disallowed_legacy.assert_not_called()

    def test_download_returns_exactly_one_selected_file(self) -> None:
        downloaded_file = self.downloaded_file()
        expected = {
            "status": "ready",
            "source": "YouTube",
            "title": "Title",
            "engine": "yt-dlp",
            "file": downloaded_file,
        }
        with (
            patch.object(
                target,
                "run_job",
                return_value=expected,
            ) as run_job,
            patch.object(target, "cleanup_stale_downloads") as cleanup,
            patch.object(target, "ensure_storage_capacity") as ensure_capacity,
        ):
            response = self.client.post(
                "/download",
                json={"url": PLAYLIST_URL, "mode": "audio", "playlist_index": 3},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            expected,
        )
        cleanup.assert_called_once_with()
        ensure_capacity.assert_called_once_with()
        run_job.assert_called_once_with(
            "download", PLAYLIST_URL, 3, "audio", ANY
        )

    def test_download_enforces_origin_json_and_shared_slot(self) -> None:
        with (
            patch.object(target, "run_job") as run_job,
            patch.object(
                target.DOWNLOAD_SLOT,
                "acquire",
                return_value=False,
            ) as acquire,
        ):
            no_origin = self.client.post(
                "/download",
                json={"url": YOUTUBE_URL, "mode": "audio"},
            )
            form = self.client.post(
                "/download",
                data={"url": YOUTUBE_URL, "mode": "audio"},
                headers=self.headers,
            )
            busy = self.client.post(
                "/download",
                json={"url": YOUTUBE_URL, "mode": "audio"},
                headers=self.headers,
            )

        self.assertEqual(no_origin.status_code, 403)
        self.assertEqual(form.status_code, 400)
        self.assertEqual(busy.status_code, 429)
        acquire.assert_called_once_with(blocking=False)
        run_job.assert_not_called()

    def test_download_maps_contract_errors_and_releases_slot(self) -> None:
        cases = (
            ("job", target.InputError("bad input"), 400),
            ("job", target.UnsupportedMediaError("unsupported"), 422),
            ("job", target.MediaLimitError("too large"), 413),
            ("job", target.EngineError("engine failed"), 502),
            ("job", target.OperationTimeoutError("timed out"), 504),
            ("storage", target.StorageLimitError("storage full"), 507),
        )
        for stage, error, status in cases:
            with target.DOWNLOAD_ATTEMPTS_LOCK:
                target.DOWNLOAD_ATTEMPTS.clear()
            run_job = MagicMock(return_value={"status": "ready"})
            ensure_capacity = MagicMock()
            if stage == "job":
                run_job.side_effect = error
            else:
                ensure_capacity.side_effect = error
            with (
                self.subTest(error=type(error).__name__),
                patch.object(target, "run_job", run_job),
                patch.object(target, "cleanup_stale_downloads"),
                patch.object(target, "ensure_storage_capacity", ensure_capacity),
                patch.object(
                    target.DOWNLOAD_SLOT,
                    "acquire",
                    return_value=True,
                ),
                patch.object(target.DOWNLOAD_SLOT, "release") as release,
            ):
                response = self.client.post(
                    "/download",
                    json={"url": YOUTUBE_URL, "mode": "audio"},
                    headers=self.headers,
                )

            self.assertEqual(response.status_code, status)
            self.assertEqual(set(response.get_json()), {"error"})
            release.assert_called_once_with()
            if stage == "job":
                run_job.assert_called_once_with(
                    "download", YOUTUBE_URL, None, "audio", ANY
                )
            else:
                run_job.assert_not_called()
            if stage == "storage":
                ensure_capacity.assert_called_once_with()

    def test_download_rejects_invalid_mode_before_inspection(self) -> None:
        for mode in ("gif", [], {}):
            with (
                self.subTest(mode=mode),
                patch.object(target, "run_job") as run_job,
            ):
                response = self.client.post(
                    "/download",
                    json={"url": YOUTUBE_URL, "mode": mode},
                    headers=self.headers,
                )

            self.assertEqual(response.status_code, 400)
            self.assertEqual(set(response.get_json()), {"error"})
            run_job.assert_not_called()

    def test_download_rejects_unsupported_output_before_engine(self) -> None:
        metadata = self.media_response(["audio"])
        with (
            patch.object(target, "inspect_media", return_value=metadata),
            patch.object(target, "download_media") as download_media,
            self.assertRaises(target.UnsupportedMediaError),
        ):
            target._execute_job(
                {
                    "operation": "download",
                    "url": SOUNDCLOUD_URL,
                    "playlist_index": None,
                    "mode": "video",
                },
                "1" * 32,
            )

        download_media.assert_not_called()

    def test_download_rejects_unknown_duration_audio_before_conversion(self) -> None:
        metadata = self.media_response()
        metadata["duration_seconds"] = None
        with (
            patch.object(target, "inspect_media", return_value=metadata),
            patch.object(target, "download_media") as download_media,
            self.assertRaises(target.MediaLimitError),
        ):
            target._execute_job(
                {
                    "operation": "download",
                    "url": YOUTUBE_URL,
                    "playlist_index": None,
                    "mode": "audio",
                },
                "2" * 32,
            )

        download_media.assert_not_called()

    def test_download_rejects_playlist_without_selected_item(self) -> None:
        playlist = {
            "kind": "playlist",
            "source": "YouTube",
            "title": "Playlist",
            "entries": [{"index": 1, "title": "Episode 1"}],
            "truncated": False,
            "engine": "yt-dlp",
        }
        with (
            patch.object(target, "inspect_media", return_value=playlist),
            patch.object(target, "download_media") as download_media,
            self.assertRaises(target.UnsupportedMediaError),
        ):
            target._execute_job(
                {
                    "operation": "download",
                    "url": PLAYLIST_URL,
                    "playlist_index": None,
                    "mode": "audio",
                },
                "3" * 32,
            )

        download_media.assert_not_called()

    def test_download_falls_back_for_direct_youtube_only(self) -> None:
        metadata = self.media_response()
        downloaded_file = self.downloaded_file()
        with (
            patch.object(
                target,
                "download_with_ytdlp",
                side_effect=target.EngineError("primary failed"),
            ) as primary,
            patch.object(
                target,
                "download_with_legacy",
                return_value=downloaded_file,
            ) as legacy,
        ):
            result = target.download_media(
                YOUTUBE_URL, "audio", None, metadata, "a" * 32
            )

        primary.assert_called_once_with(
            YOUTUBE_URL, "audio", None, metadata, "a" * 32
        )
        legacy.assert_called_once_with(YOUTUBE_URL, "audio", metadata, "a" * 32)
        self.assertEqual(result, (downloaded_file, "youtube-dl fallback"))

        for link, playlist_index in ((SOUNDCLOUD_URL, None), (YOUTUBE_URL, 1)):
            with (
                self.subTest(link=link, playlist_index=playlist_index),
                patch.object(
                    target,
                    "download_with_ytdlp",
                    side_effect=target.EngineError("primary failed"),
                ),
                patch.object(target, "download_with_legacy") as disallowed_legacy,
                self.assertRaises(target.EngineError),
            ):
                target.download_media(
                    link,
                    "audio",
                    playlist_index,
                    metadata,
                    "b" * 32,
                )
            disallowed_legacy.assert_not_called()

    def test_download_does_not_hide_non_engine_errors(self) -> None:
        metadata = self.media_response()
        with (
            patch.object(
                target,
                "download_with_ytdlp",
                side_effect=RuntimeError("bug"),
            ),
            patch.object(target, "download_with_legacy") as legacy,
            self.assertRaisesRegex(RuntimeError, "bug"),
        ):
            target.download_media(YOUTUBE_URL, "audio", None, metadata, "c" * 32)

        legacy.assert_not_called()

    def test_primary_download_creates_only_selected_safe_output(self) -> None:
        metadata = self.media_response()
        for link, mode, extension, playlist_index in (
            (YOUTUBE_URL, "audio", "mp3", None),
            (PLAYLIST_URL, "video", "webm", 2),
        ):
            primary = MagicMock()
            primary.__enter__.return_value = primary
            with (
                self.subTest(mode=mode),
                patch.object(
                    target,
                    "uuid4",
                    return_value=SimpleNamespace(hex="a" * 32),
                ),
                patch.object(target, "YtDlp", return_value=primary) as constructor,
                patch.object(target, "LegacyYoutubeDL") as legacy,
            ):

                def write_output(
                    *_args: object,
                    output_extension: str = extension,
                    **_kwargs: object,
                ) -> object:
                    options = constructor.call_args.args[0]
                    template = options["outtmpl"]
                    self.assertIsInstance(template, str)
                    assert isinstance(template, str)
                    Path(
                        template.replace("%(ext)s", output_extension)
                    ).write_bytes(b"media")
                    raise target.MaxDownloadsReached

                primary.download.side_effect = write_output
                result = target.download_with_ytdlp(
                    link,
                    cast(target.OutputMode, mode),
                    playlist_index,
                    metadata,
                    "a" * 32,
                )

            options = constructor.call_args.args[0]
            self.assertEqual(
                options["allowed_extractors"],
                ["default", "-generic"],
            )
            self.assertIs(options["cachedir"], False)
            self.assertIs(options["enable_file_urls"], False)
            self.assertEqual(options["external_downloader"], {"default": "native"})
            self.assertEqual(options["proxy"], "")
            self.assertEqual(options["remote_components"], [])
            self.assertEqual(options["max_filesize"], target.MAX_MEDIA_BYTES)
            self.assertEqual(options["max_downloads"], 1)
            self.assertIs(options["noprogress"], True)
            self.assertEqual(options["retries"], target.MAX_RETRIES)
            self.assertEqual(options["fragment_retries"], target.MAX_RETRIES)
            self.assertEqual(options["concurrent_fragment_downloads"], 1)
            self.assertEqual(
                options["socket_timeout"],
                target.SOCKET_TIMEOUT_SECONDS,
            )
            self.assertEqual(len(options["progress_hooks"]), 1)
            self.assertTrue(callable(options["progress_hooks"][0]))
            self.assertTrue(callable(options["match_filter"]))
            primary.download.assert_called_once_with([link])
            primary.extract_info.assert_not_called()
            self.assertIs(options["updatetime"], False)
            self.assertIs(options["noplaylist"], playlist_index is None)
            if playlist_index is None:
                self.assertNotIn("playlist_items", options)
            else:
                self.assertEqual(options["playlist_items"], "2")
            self.assertEqual(result["extension"], extension)
            self.assertEqual(result["mode"], mode)
            self.assertEqual(result["size_bytes"], 5)
            self.assertEqual(
                [path.name for path in target.DOWNLOAD_PATH.iterdir()],
                [result["name"]],
            )
            if mode == "audio":
                self.assertEqual(options["format"], target.AUDIO_FORMAT)
                self.assertIn(target.SAFE_PROTOCOL_FILTER, target.AUDIO_FORMAT)
                self.assertIn(
                    f"filesize<={target.MAX_MEDIA_BYTES}",
                    target.AUDIO_FORMAT,
                )
                self.assertEqual(
                    options["postprocessors"],
                    [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": "0",
                        }
                    ],
                )
            else:
                self.assertEqual(options["format"], target.VIDEO_FORMAT)
                self.assertIn(target.SAFE_PROTOCOL_FILTER, target.VIDEO_FORMAT)
                self.assertIn(
                    f"filesize<={target.VIDEO_STREAM_MAX_BYTES}",
                    target.VIDEO_FORMAT,
                )
                self.assertLessEqual(
                    target.VIDEO_STREAM_MAX_BYTES
                    + target.VIDEO_AUDIO_MAX_BYTES,
                    target.MAX_MEDIA_BYTES,
                )
                self.assertIn(
                    f"bv*[filesize<={target.MAX_MEDIA_BYTES}]",
                    target.VIDEO_FORMAT,
                )
                self.assertEqual(options["merge_output_format"], "mp4")
                self.assertNotIn("postprocessors", options)
            legacy.assert_not_called()
            for path in target.DOWNLOAD_PATH.iterdir():
                path.unlink()

    def test_primary_download_error_removes_partial_artifacts(self) -> None:
        primary = MagicMock()
        primary.__enter__.return_value = primary
        with (
            patch.object(
                target,
                "uuid4",
                return_value=SimpleNamespace(hex="b" * 32),
            ),
            patch.object(target, "YtDlp", return_value=primary) as constructor,
        ):

            def fail_download(*_args: object, **_kwargs: object) -> None:
                template = constructor.call_args.args[0]["outtmpl"]
                self.assertIsInstance(template, str)
                assert isinstance(template, str)
                Path(template.replace("%(ext)s", "mp4.part")).write_bytes(b"part")
                raise target.YtDlpDownloadError("primary failed")

            primary.download.side_effect = fail_download
            with self.assertRaises(target.EngineError):
                target.download_with_ytdlp(
                    YOUTUBE_URL,
                    "video",
                    None,
                    self.media_response(),
                    "b" * 32,
                )

        primary.download.assert_called_once_with([YOUTUBE_URL])
        self.assertEqual(list(target.DOWNLOAD_PATH.iterdir()), [])

    def test_primary_reports_no_bounded_format_as_media_limit(self) -> None:
        primary = MagicMock()
        primary.__enter__.return_value = primary
        primary.download.side_effect = target.YtDlpDownloadError(
            "ERROR: Requested format is not available"
        )
        with (
            patch.object(target, "YtDlp", return_value=primary),
            self.assertRaises(target.MediaLimitError),
        ):
            target.download_with_ytdlp(
                YOUTUBE_URL,
                "video",
                None,
                self.media_response(),
                "8" * 32,
            )

        self.assertEqual(list(target.DOWNLOAD_PATH.iterdir()), [])

    def test_primary_never_accepts_fragment_as_final_output(self) -> None:
        primary = MagicMock()
        primary.__enter__.return_value = primary
        with (
            patch.object(
                target,
                "uuid4",
                return_value=SimpleNamespace(hex="9" * 32),
            ),
            patch.object(target, "YtDlp", return_value=primary) as constructor,
            patch.object(target, "download_with_legacy") as legacy,
        ):

            def leave_fragment(*_args: object, **_kwargs: object) -> None:
                template = constructor.call_args.args[0]["outtmpl"]
                self.assertIsInstance(template, str)
                assert isinstance(template, str)
                Path(template.replace("%(ext)s", "f137.mp4")).write_bytes(b"part")
                raise target.MaxDownloadsReached

            primary.download.side_effect = leave_fragment
            with self.assertRaises(target.MediaLimitError):
                target.download_media(
                    YOUTUBE_URL,
                    "video",
                    None,
                    self.media_response(),
                    "9" * 32,
                )

        legacy.assert_not_called()
        self.assertEqual(list(target.DOWNLOAD_PATH.iterdir()), [])

    def test_download_filter_revalidates_live_and_duration_limits(self) -> None:
        audio_filter = target.make_download_match_filter("audio")
        video_filter = target.make_download_match_filter("video")
        self.assertIsNone(audio_filter(self.media_info()))
        self.assertIsNone(audio_filter(self.media_info(duration=None), incomplete=True))
        with self.assertRaises(target.MediaLimitError):
            audio_filter(self.media_info(duration=None))

        active = self.media_info(duration=None)
        active.update({"is_live": True, "live_status": "is_live"})
        with self.assertRaises(target.UnsupportedMediaError):
            video_filter(active)
        with (
            patch.object(target, "MAX_DURATION_SECONDS", 120),
            self.assertRaises(target.MediaLimitError),
        ):
            video_filter(self.media_info(duration=121))

        audio_only = self.media_info(
            formats=[
                {"format_id": "audio", "acodec": "opus", "vcodec": "none"}
            ]
        )
        video_only = self.media_info(
            formats=[
                {"format_id": "video", "acodec": "none", "vcodec": "avc1"}
            ]
        )
        with self.assertRaises(target.UnsupportedMediaError):
            video_filter(audio_only)
        with self.assertRaises(target.UnsupportedMediaError):
            audio_filter(video_only)

    def test_primary_request_director_excludes_native_and_proxy_handlers(self) -> None:
        with target.YtDlp({"quiet": True, "proxy": ""}) as client:
            runtime = cast(target.YtDlpRuntime, client)
            handlers = list(runtime._request_director.handlers.values())

        self.assertEqual(
            [type(handler).__name__ for handler in handlers],
            ["DirectRequestsRH"],
        )
        request_with_proxy = MagicMock(proxies={"all": "http://proxy.test"})
        direct_handler = cast(target.DirectRequestsRH, handlers[0])
        self.assertEqual(
            direct_handler._get_proxies(request_with_proxy),
            {"all": None},
        )

    def test_primary_blocks_unsafe_downloader_protocols(self) -> None:
        with (
            patch(
                "yt_dlp.downloader.rtmp.RtmpFD.download",
                return_value=(True, True),
            ) as unsafe_download,
            target.YtDlp({"quiet": True}) as client,
            self.assertRaises(target.YtDlpDownloadError),
        ):
            client.dl(
                "unused",
                {"url": "rtmp://example.test/media", "protocol": "rtmp"},
            )

        unsafe_download.assert_not_called()

    def test_downloader_guard_rejects_missing_url_before_resolvers(self) -> None:
        invalid_infos: tuple[dict[str, object], ...] = (
            {},
            {"url": ""},
            {"url": "   "},
            {"url": 1},
        )
        with (
            patch.object(target, "_RESOLVE_PROTOCOL") as protocol,
            patch.object(target, "_RESOLVE_DOWNLOADER") as resolver,
        ):
            for info in invalid_infos:
                with self.subTest(info=info):
                    self.assertIsNone(target._safe_downloader_type(info, {}))

        protocol.assert_not_called()
        resolver.assert_not_called()

        with target.YtDlp({"quiet": True}) as client:
            for info in invalid_infos:
                with (
                    self.subTest(info=info),
                    patch.object(
                        client,
                        "raise_no_formats",
                        side_effect=target.YtDlpDownloadError("no formats"),
                    ) as raise_no_formats,
                    patch.object(target, "_safe_downloader_type") as safe_resolver,
                    self.assertRaises(target.YtDlpDownloadError),
                ):
                    client.dl("unused", info)
                raise_no_formats.assert_called_once_with(info, True)
                safe_resolver.assert_not_called()

    def test_hls_manifest_body_is_hard_bounded(self) -> None:
        manifest = b"#EXTM3U\n#EXT-X-ENDLIST\n"
        info: dict[str, object] = {
            "id": "media",
            "ext": "mp4",
            "url": "https://example.test/media.m3u8",
            "protocol": "m3u8_native",
        }
        response = MagicMock(url="https://example.test/final.m3u8")
        response.__enter__.return_value = response
        response.get_header.return_value = None
        response.read.return_value = manifest
        with (
            target.YtDlp({"quiet": True}) as client,
            patch.object(client, "urlopen", return_value=response),
            patch.object(target, "MAX_HLS_MANIFEST_BYTES", len(manifest)),
            patch.object(target, "_HLS_CAN_DOWNLOAD", return_value=True),
            patch.object(target.HlsFD, "real_download", return_value=True) as parent,
        ):
            constructor = cast(
                Callable[
                    [object, Mapping[str, object]],
                    target.GuardedHlsFD,
                ],
                target.GuardedHlsFD,
            )
            downloader = constructor(client, dict(client.params))
            self.assertIs(downloader.real_download("unused", info), True)

        response.read.assert_called_once_with(len(manifest) + 1)
        parent.assert_called_once()

        for header in (None, "bad", "1"):
            oversized = MagicMock(url="https://example.test/final.m3u8")
            oversized.__enter__.return_value = oversized
            oversized.get_header.return_value = header
            oversized.read.return_value = b"x" * 9
            with (
                self.subTest(header=header),
                target.YtDlp({"quiet": True}) as client,
                patch.object(client, "urlopen", return_value=oversized),
                patch.object(target, "MAX_HLS_MANIFEST_BYTES", 8),
            ):
                downloader = constructor(client, dict(client.params))
                with self.assertRaisesRegex(
                    target.YtDlpDownloadError, "exceeds the size limit"
                ):
                    downloader.real_download("unused", info)
            oversized.read.assert_called_once_with(9)

        declared = MagicMock(url="https://example.test/final.m3u8")
        declared.__enter__.return_value = declared
        declared.get_header.return_value = "9"
        with (
            target.YtDlp({"quiet": True}) as client,
            patch.object(client, "urlopen", return_value=declared),
            patch.object(target, "MAX_HLS_MANIFEST_BYTES", 8),
        ):
            downloader = constructor(client, dict(client.params))
            with self.assertRaisesRegex(
                target.YtDlpDownloadError, "exceeds the size limit"
            ):
                downloader.real_download("unused", info)
        declared.read.assert_not_called()

    def test_hls_preloaded_manifest_uses_encoded_limit(self) -> None:
        info: dict[str, object] = {
            "id": "media",
            "ext": "mp4",
            "url": "https://example.test/media.m3u8",
            "protocol": "m3u8_native",
            "hls_media_playlist_data": "é" * 5,
        }
        with (
            target.YtDlp({"quiet": True}) as client,
            patch.object(target, "MAX_HLS_MANIFEST_BYTES", 8),
        ):
            constructor = cast(
                Callable[
                    [object, Mapping[str, object]],
                    target.GuardedHlsFD,
                ],
                target.GuardedHlsFD,
            )
            downloader = constructor(client, dict(client.params))
            with self.assertRaisesRegex(
                target.YtDlpDownloadError, "exceeds the size limit"
            ):
                downloader.real_download("unused", info)

        info["hls_media_playlist_data"] = "x" * 9
        with (
            target.YtDlp({"quiet": True}) as client,
            patch.object(target, "MAX_HLS_MANIFEST_BYTES", 8),
        ):
            downloader = constructor(client, dict(client.params))
            with self.assertRaises(target.YtDlpDownloadError):
                downloader.real_download("unused", info)

    def test_guarded_hls_never_falls_back_to_ffmpeg_network_download(self) -> None:
        unsupported_manifest = "\n".join(
            (
                "#EXTM3U",
                '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="https://example.test/key"',
                "#EXT-X-ENDLIST",
            )
        )
        info: dict[str, object] = {
            "id": "media",
            "ext": "mp4",
            "url": "https://example.test/media.m3u8",
            "protocol": "m3u8_native",
            "hls_media_playlist_data": unsupported_manifest,
        }
        with (
            target.YtDlp({"quiet": True}) as client,
            patch(
                "yt_dlp.downloader.hls.FFmpegFD.real_download",
                return_value=True,
            ) as ffmpeg_download,
        ):
            constructor = cast(
                Callable[
                    [object, Mapping[str, object]],
                    target.GuardedHlsFD,
                ],
                target.GuardedHlsFD,
            )
            downloader = constructor(client, dict(client.params))
            with self.assertRaises(target.YtDlpDownloadError):
                downloader.real_download("unused", info)

        ffmpeg_download.assert_not_called()

    def test_primary_format_selector_excludes_external_protocols(self) -> None:
        formats = [
            {
                "format_id": "safe",
                "ext": "m4a",
                "protocol": "https",
                "acodec": "aac",
                "vcodec": "none",
                "url": "https://example.test/audio",
            },
            {
                "format_id": "unsafe",
                "ext": "m4a",
                "protocol": "rtmp",
                "acodec": "aac",
                "vcodec": "none",
                "url": "rtmp://example.test/audio",
            },
        ]
        with target.YtDlp({"quiet": True}) as client:
            selector = client.build_format_selector(target.AUDIO_FORMAT)
            selected = list(
                selector(
                    {
                        "formats": formats,
                        "has_merged_format": False,
                        "incomplete_formats": False,
                    }
                )
            )

        self.assertEqual([item["format_id"] for item in selected], ["safe"])

    def test_primary_size_hook_error_automatically_removes_part(self) -> None:
        primary = MagicMock()
        primary.__enter__.return_value = primary
        with (
            patch.object(target, "MAX_MEDIA_BYTES", 4),
            patch.object(
                target,
                "uuid4",
                return_value=SimpleNamespace(hex="c" * 32),
            ),
            patch.object(target, "YtDlp", return_value=primary) as constructor,
        ):

            def exceed_limit(*_args: object, **_kwargs: object) -> None:
                options = constructor.call_args.args[0]
                template = options["outtmpl"]
                self.assertIsInstance(template, str)
                assert isinstance(template, str)
                Path(template.replace("%(ext)s", "mp4.part")).write_bytes(b"large")
                options["progress_hooks"][0](
                    {"status": "downloading", "downloaded_bytes": 5}
                )

            primary.download.side_effect = exceed_limit
            with self.assertRaises(target.MediaLimitError):
                target.download_with_ytdlp(
                    YOUTUBE_URL,
                    "video",
                    None,
                    self.media_response(),
                    "c" * 32,
                )

        primary.download.assert_called_once_with([YOUTUBE_URL])
        self.assertEqual(list(target.DOWNLOAD_PATH.iterdir()), [])

    def test_legacy_download_helper_outputs_one_file_and_cleans_failure(self) -> None:
        metadata = self.media_response(engine="youtube-dl fallback")
        for mode, extension in (("audio", "mp3"), ("video", "mp4")):
            legacy = MagicMock()
            legacy.__enter__.return_value = legacy
            with (
                self.subTest(mode=mode),
                patch.object(
                    target,
                    "uuid4",
                    return_value=SimpleNamespace(hex="d" * 32),
                ),
                patch.object(
                    target,
                    "LegacyYoutubeDL",
                    return_value=legacy,
                ) as constructor,
            ):

                def write_output(
                    *_args: object,
                    output_extension: str = extension,
                    **_kwargs: object,
                ) -> None:
                    template = constructor.call_args.args[0]["outtmpl"]
                    self.assertIsInstance(template, str)
                    assert isinstance(template, str)
                    Path(
                        template.replace("%(ext)s", output_extension)
                    ).write_bytes(b"media")

                legacy.download.side_effect = write_output
                result = target.download_with_legacy(
                    YOUTUBE_URL,
                    cast(target.OutputMode, mode),
                    metadata,
                    "d" * 32,
                )

            options = constructor.call_args.args[0]
            self.assertIs(options["cachedir"], False)
            self.assertEqual(options["format"], "18")
            self.assertNotIn("external_downloader", options)
            self.assertIs(options["hls_prefer_native"], True)
            self.assertEqual(options["proxy"], "")
            self.assertTrue(callable(options["match_filter"]))
            self.assertIs(
                get_suitable_downloader({"protocol": "https"}, options),
                HttpFD,
            )
            self.assertEqual(options["max_filesize"], target.MAX_MEDIA_BYTES)
            self.assertEqual(options["retries"], target.MAX_RETRIES)
            self.assertIs(options["noprogress"], True)
            self.assertEqual(
                options["socket_timeout"],
                target.SOCKET_TIMEOUT_SECONDS,
            )
            self.assertIs(options["noplaylist"], True)
            legacy.download.assert_called_once_with([YOUTUBE_URL])
            legacy.extract_info.assert_not_called()
            self.assertEqual(result["extension"], extension)
            self.assertEqual(result["mode"], mode)
            self.assertEqual(result["size_bytes"], 5)
            self.assertEqual(
                [path.name for path in target.DOWNLOAD_PATH.iterdir()],
                [result["name"]],
            )
            if mode == "audio":
                self.assertEqual(
                    options["postprocessors"],
                    [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": "0",
                        }
                    ],
                )
            else:
                self.assertNotIn("postprocessors", options)
            for path in target.DOWNLOAD_PATH.iterdir():
                path.unlink()

        failed = MagicMock()
        failed.__enter__.return_value = failed
        with (
            patch.object(
                target,
                "uuid4",
                return_value=SimpleNamespace(hex="e" * 32),
            ),
            patch.object(
                target,
                "LegacyYoutubeDL",
                return_value=failed,
            ) as constructor,
        ):

            def fail_download(*_args: object, **_kwargs: object) -> None:
                template = constructor.call_args.args[0]["outtmpl"]
                self.assertIsInstance(template, str)
                assert isinstance(template, str)
                Path(template.replace("%(ext)s", "mp4.part")).write_bytes(b"part")
                raise target.LegacyDownloadError("legacy failed")

            failed.download.side_effect = fail_download
            with self.assertRaises(target.EngineError):
                target.download_with_legacy(
                    YOUTUBE_URL,
                    "video",
                    metadata,
                    "e" * 32,
                )

        failed.download.assert_called_once_with([YOUTUBE_URL])
        self.assertEqual(list(target.DOWNLOAD_PATH.iterdir()), [])

    def test_engine_wrappers_only_translate_library_download_errors(self) -> None:
        primary = MagicMock()
        primary.__enter__.return_value = primary
        primary.extract_info.side_effect = target.YtDlpDownloadError(
            "primary failed"
        )
        with (
            patch.object(target, "YtDlp", return_value=primary),
            self.assertRaises(target.EngineError),
        ):
            target.inspect_with_ytdlp(YOUTUBE_URL, None)

        primary.extract_info.side_effect = RuntimeError("primary bug")
        with (
            patch.object(target, "YtDlp", return_value=primary),
            self.assertRaisesRegex(RuntimeError, "primary bug"),
        ):
            target.inspect_with_ytdlp(YOUTUBE_URL, None)

        primary.download.side_effect = RuntimeError("primary download bug")
        with (
            patch.object(target, "YtDlp", return_value=primary),
            self.assertRaisesRegex(RuntimeError, "primary download bug"),
        ):
            target.download_with_ytdlp(
                YOUTUBE_URL,
                "audio",
                None,
                self.media_response(),
                "f" * 32,
            )

        legacy = MagicMock()
        legacy.__enter__.return_value = legacy
        legacy.extract_info.side_effect = target.LegacyDownloadError(
            "legacy failed"
        )
        with (
            patch.object(target, "LegacyYoutubeDL", return_value=legacy),
            self.assertRaises(target.EngineError),
        ):
            target.inspect_with_legacy(YOUTUBE_URL)

        legacy.extract_info.side_effect = RuntimeError("legacy bug")
        with (
            patch.object(target, "LegacyYoutubeDL", return_value=legacy),
            self.assertRaisesRegex(RuntimeError, "legacy bug"),
        ):
            target.inspect_with_legacy(YOUTUBE_URL)

        legacy.download.side_effect = target.LegacyDownloadError("legacy failed")
        with (
            patch.object(target, "LegacyYoutubeDL", return_value=legacy),
            self.assertRaises(target.EngineError),
        ):
            target.download_with_legacy(
                YOUTUBE_URL,
                "audio",
                self.media_response(),
                "1" * 32,
            )

        legacy.download.side_effect = RuntimeError("legacy download bug")
        with (
            patch.object(target, "LegacyYoutubeDL", return_value=legacy),
            self.assertRaisesRegex(RuntimeError, "legacy download bug"),
        ):
            target.download_with_legacy(
                YOUTUBE_URL,
                "audio",
                self.media_response(),
                "2" * 32,
            )

    def test_real_job_worker_rejects_spotify_without_import_cleanup(self) -> None:
        stale = target.DOWNLOAD_PATH / f"yt-download-stale-{'4' * 32}.mp3"
        stale.write_bytes(b"stale")
        operation_lock = target._acquire_operation_lock()
        self.assertIsNotNone(operation_lock)
        assert operation_lock is not None
        try:
            with self.assertRaises(target.UnsupportedMediaError):
                target.run_job(
                    "inspect",
                    "https://open.spotify.com/track/123",
                    None,
                    None,
                    operation_lock,
                )
        finally:
            target._close_locked(operation_lock)

        self.assertTrue(stale.exists())
        self.assertEqual(list(target.DOWNLOAD_PATH.glob(".yt-job-*")), [])

    def test_job_launcher_uses_fixed_command_and_one_deadline(self) -> None:
        process = MagicMock(pid=1234, returncode=0)
        process.poll.return_value = 0
        expected = self.media_response()
        operation_lock = target._acquire_operation_lock()
        self.assertIsNotNone(operation_lock)
        assert operation_lock is not None
        with (
            patch.object(target, "monotonic", return_value=10.0),
            patch.object(target.subprocess, "Popen", return_value=process) as popen,
            patch.object(target, "_wait_for_job") as wait_for_job,
            patch.object(target, "_decode_job_result", return_value=expected),
        ):
            try:
                result = target.run_job(
                    "inspect",
                    YOUTUBE_URL,
                    None,
                    None,
                    operation_lock,
                )
            finally:
                target._close_locked(operation_lock)

        self.assertEqual(result, expected)
        command = popen.call_args.args[0]
        self.assertEqual(
            command,
            [
                target.sys.executable,
                str(Path(target.__file__).resolve()),
                "--job-worker",
            ],
        )
        self.assertIs(popen.call_args.kwargs["stdin"], target.subprocess.DEVNULL)
        self.assertIs(popen.call_args.kwargs["stdout"], target.subprocess.DEVNULL)
        self.assertIs(popen.call_args.kwargs["stderr"], target.subprocess.DEVNULL)
        wait_for_job.assert_called_once_with(
            process, 10.0 + target.JOB_TIMEOUT_SECONDS
        )

    def test_unreaped_job_retains_locks_controls_and_liveness(self) -> None:
        job_id = "1" * 32
        work = target.DOWNLOAD_PATH / f"yt-work-title-{job_id}.mp3"
        process = MagicMock(pid=1234, returncode=0)
        process.poll.return_value = 0
        operation_lock = target._acquire_operation_lock()
        self.assertIsNotNone(operation_lock)
        assert operation_lock is not None
        self.addCleanup(operation_lock.close)
        app_path = Path(target.__file__).resolve()
        pipe_fds = os.pipe()
        for fd in pipe_fds:
            self.addCleanup(_close_fd, fd)
        original_close = os.close
        path_factory = MagicMock()
        path_factory.return_value.resolve.return_value = app_path

        def fail_reap(child: object, _deadline: float) -> None:
            work.write_bytes(b"partial")
            target._terminate_process_tree(
                cast(target.subprocess.Popen[bytes], child)
            )

        with (
            patch.object(target, "Path", path_factory),
            patch.object(target.os, "name", "posix"),
            patch.object(target.os, "pipe", return_value=pipe_fds),
            patch.object(target.os, "close", wraps=original_close) as close_fd,
            patch.object(target, "_try_lock_file", return_value=True),
            patch.object(target, "_unlock_file"),
            patch.object(target, "uuid4", return_value=SimpleNamespace(hex=job_id)),
            patch.object(target.subprocess, "Popen", return_value=process),
            patch.object(target, "_wait_for_job", side_effect=fail_reap),
            patch.object(
                target,
                "_terminate_process_tree",
                side_effect=(
                    target.JobReapError("first reap failed"),
                    target.JobReapError("second reap failed"),
                ),
            ) as terminate,
            self.assertRaises(target.JobReapError),
        ):
            target.run_job(
                "inspect",
                YOUTUBE_URL,
                None,
                None,
                operation_lock,
            )

        target._close_locked(operation_lock)
        self.assertEqual(terminate.call_count, 2)
        self.assertIn(call(pipe_fds[0]), close_fd.call_args_list)
        self.assertIn(call(pipe_fds[1]), close_fd.call_args_list)
        self.assertTrue(work.exists())
        self.assertEqual(
            len(list(target.DOWNLOAD_PATH.glob(f".yt-job-{job_id}.*"))),
            3,
        )
        self.assertEqual(len(target.ORPHANED_JOB_LOCKS), 1)
        self.assertEqual(target.ORPHANED_JOB_PROCESSES, [process])
        target._close_locked(target.ORPHANED_JOB_LOCKS.pop())
        target.ORPHANED_JOB_PROCESSES.clear()

    def test_confirmed_retry_cleans_job_and_returns_regular_engine_error(self) -> None:
        job_id = "2" * 32
        work = target.DOWNLOAD_PATH / f"yt-work-title-{job_id}.mp3"
        process = MagicMock(pid=1235, returncode=0)
        process.poll.return_value = 0
        operation_lock = target._acquire_operation_lock()
        self.assertIsNotNone(operation_lock)
        assert operation_lock is not None
        self.addCleanup(operation_lock.close)
        operation_fd = operation_lock.fileno()
        app_path = Path(target.__file__).resolve()
        pipe_fds = os.pipe()
        for fd in pipe_fds:
            self.addCleanup(_close_fd, fd)
        original_close = os.close
        path_factory = MagicMock()
        path_factory.return_value.resolve.return_value = app_path

        def retry_reap(child: object, _deadline: float) -> None:
            work.write_bytes(b"partial")
            target._terminate_process_tree(
                cast(target.subprocess.Popen[bytes], child)
            )

        with (
            patch.object(target, "Path", path_factory),
            patch.object(target.os, "name", "posix"),
            patch.object(target.os, "pipe", return_value=pipe_fds),
            patch.object(target.os, "close", wraps=original_close) as close_fd,
            patch.object(target, "_try_lock_file", return_value=True),
            patch.object(target, "_unlock_file"),
            patch.object(target, "uuid4", return_value=SimpleNamespace(hex=job_id)),
            patch.object(target.subprocess, "Popen", return_value=process) as popen,
            patch.object(target, "_wait_for_job", side_effect=retry_reap),
            patch.object(
                target,
                "_terminate_process_tree",
                side_effect=(target.JobReapError("first reap failed"), None),
            ) as terminate,
            self.assertRaises(target.EngineError) as raised,
        ):
            target.run_job(
                "inspect",
                YOUTUBE_URL,
                None,
                None,
                operation_lock,
            )

        target._close_locked(operation_lock)
        self.assertNotIsInstance(raised.exception, target.JobReapError)
        self.assertEqual(terminate.call_count, 2)
        self.assertIn(call(pipe_fds[0]), close_fd.call_args_list)
        self.assertIn(call(pipe_fds[1]), close_fd.call_args_list)
        self.assertFalse(work.exists())
        self.assertEqual(
            list(target.DOWNLOAD_PATH.glob(f".yt-job-{job_id}.*")),
            [],
        )
        self.assertEqual(target.ORPHANED_JOB_LOCKS, [])
        self.assertEqual(target.ORPHANED_JOB_PROCESSES, [])
        self.assertIn(operation_fd, popen.call_args.kwargs["pass_fds"])

    def test_job_timeout_and_memory_breach_kill_before_error(self) -> None:
        timed_out = MagicMock(pid=10)
        timed_out.poll.return_value = None
        with (
            patch.object(target, "monotonic", return_value=1.0),
            patch.object(target, "_terminate_process_tree") as terminate,
            self.assertRaises(target.OperationTimeoutError),
        ):
            target._wait_for_job(timed_out, 0.0)
        terminate.assert_called_once_with(timed_out)

        memory_limited = MagicMock(pid=11)
        memory_limited.poll.return_value = None
        with (
            patch.object(
                target,
                "_process_group_rss_bytes",
                return_value=target.JOB_MEMORY_BYTES + 1,
            ),
            patch.object(target, "_terminate_process_tree") as terminate,
            self.assertRaises(target.EngineError),
        ):
            target._wait_for_job(memory_limited, target.monotonic() + 10.0)
        terminate.assert_called_once_with(memory_limited)

    def test_job_memory_monitor_tolerates_one_startup_race(self) -> None:
        process = MagicMock(pid=12, returncode=0)
        process.poll.side_effect = [None, None, None, 0]
        with (
            patch.object(target.sys, "platform", "linux"),
            patch.object(target, "_process_group_rss_bytes", side_effect=[0, 1]),
            patch.object(target, "_stored_bytes", return_value=0),
            patch.object(target, "_terminate_process_tree") as terminate,
            patch.object(target, "sleep"),
        ):
            target._wait_for_job(process, target.monotonic() + 10.0)

        terminate.assert_not_called()

    def test_job_memory_monitor_failure_kills_before_error(self) -> None:
        monitor_failed = MagicMock(pid=12)
        monitor_failed.poll.return_value = None
        with (
            patch.object(target.sys, "platform", "linux"),
            patch.object(target, "_process_group_rss_bytes", return_value=None),
            patch.object(target, "_terminate_process_tree") as terminate,
            self.assertRaisesRegex(target.EngineError, "memory monitor failed"),
        ):
            target._wait_for_job(monitor_failed, target.monotonic() + 10.0)
        terminate.assert_called_once_with(monitor_failed)

    def test_job_storage_monitor_kills_on_limit_or_scan_failure(self) -> None:
        process = MagicMock(pid=13)
        process.poll.return_value = None
        with (
            patch.object(target, "_process_group_rss_bytes", return_value=1),
            patch.object(
                target,
                "_stored_bytes",
                return_value=target.MAX_STORED_BYTES + 1,
            ),
            patch.object(target, "_terminate_process_tree") as terminate,
            self.assertRaises(target.StorageLimitError),
        ):
            target._wait_for_job(process, target.monotonic() + 10.0)
        terminate.assert_called_once_with(process)

        with (
            patch.object(target, "_process_group_rss_bytes", return_value=1),
            patch.object(target, "_stored_bytes", side_effect=OSError("disk")),
            patch.object(target, "_terminate_process_tree") as terminate,
            self.assertRaisesRegex(target.EngineError, "storage monitor failed"),
        ):
            target._wait_for_job(process, target.monotonic() + 10.0)
        terminate.assert_called_once_with(process)

    def test_parent_watchdog_and_group_kill_fail_closed(self) -> None:
        with (
            patch.object(target.os, "name", "posix"),
            patch.object(target.signal, "SIGKILL", 9, create=True),
            patch.object(target.os, "read", side_effect=OSError("closed")),
            patch.object(target.os, "close") as close,
            patch.object(target.os, "killpg", create=True) as kill_group,
        ):
            target._watch_parent(22)

        close.assert_called_once_with(22)
        kill_group.assert_called_once_with(0, 9)

        process = MagicMock(pid=23)
        with (
            patch.object(target.os, "name", "posix"),
            patch.object(target.signal, "SIGKILL", 9, create=True),
            patch.object(
                target.os,
                "killpg",
                side_effect=PermissionError,
                create=True,
            ),
            self.assertRaises(target.JobReapError),
        ):
            target._terminate_process_tree(process)

    def test_job_result_boundary_rejects_invalid_and_oversized_ipc(self) -> None:
        result_path = target.DOWNLOAD_PATH / ".result"
        job_id = "a" * 32
        invalid_payloads = (
            b"",
            b"not-json",
            json.dumps({"ok": False, "error": "Unknown", "message": "x"}).encode(),
            b"x" * (target.JOB_RESULT_MAX_BYTES + 1),
        )
        for payload in invalid_payloads:
            result_path.write_bytes(payload)
            with (
                self.subTest(size=len(payload)),
                self.assertRaises(target.EngineError),
            ):
                target._decode_job_result(result_path, "inspect", job_id)

        result_path.write_text(
            json.dumps(
                {
                    "ok": False,
                    "error": "MediaLimitError",
                    "message": "too large",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(target.MediaLimitError):
            target._decode_job_result(result_path, "inspect", job_id)

    def test_job_result_boundary_rejects_hostile_shapes_and_wrong_job(self) -> None:
        result_path = target.DOWNLOAD_PATH / ".result"
        job_id = "a" * 32
        inspection = self.media_response()
        inspection["outputs"] = [{}]
        result_path.write_text(
            json.dumps({"ok": True, "value": inspection}),
            encoding="utf-8",
        )
        with self.assertRaises(target.EngineError):
            target._decode_job_result(result_path, "inspect", job_id)

        ready = target.DOWNLOAD_PATH / f"yt-download-title-{job_id}.mp3"
        ready.write_bytes(b"media")
        invalid_mode = {
            "status": "ready",
            "source": "YouTube",
            "title": "Title",
            "engine": "yt-dlp",
            "file": {
                "name": ready.name,
                "extension": "mp3",
                "mode": {},
                "size_bytes": 5,
            },
        }
        result_path.write_text(
            json.dumps({"ok": True, "value": invalid_mode}),
            encoding="utf-8",
        )
        with self.assertRaises(target.EngineError):
            target._decode_job_result(result_path, "download", job_id)

        wrong_job_id = "b" * 32
        unrelated = target.DOWNLOAD_PATH / f"yt-download-title-{wrong_job_id}.mp3"
        unrelated.write_bytes(b"media")
        wrong_job = {
            "status": "ready",
            "source": "YouTube",
            "title": "Title",
            "engine": "yt-dlp",
            "file": {
                "name": unrelated.name,
                "extension": "mp3",
                "mode": "audio",
                "size_bytes": 5,
            },
        }
        result_path.write_text(
            json.dumps({"ok": True, "value": wrong_job}),
            encoding="utf-8",
        )
        with self.assertRaises(target.EngineError):
            target._decode_job_result(result_path, "download", job_id)
        self.assertTrue(unrelated.exists())

    def test_exact_job_cleanup_does_not_remove_unrelated_artifacts(self) -> None:
        job_id = "5" * 32
        unrelated_id = "6" * 32
        matching = (
            target.DOWNLOAD_PATH / f"yt-work-title-{job_id}.mp4.part",
            target.DOWNLOAD_PATH / f"yt-download-title-{job_id}.mp4",
        )
        unrelated = (
            target.DOWNLOAD_PATH / f"yt-work-title-{unrelated_id}.mp4.part"
        )
        for artifact in (*matching, unrelated):
            artifact.write_bytes(b"x")

        target.cleanup_job_id(job_id, include_ready=True)

        self.assertFalse(any(artifact.exists() for artifact in matching))
        self.assertTrue(unrelated.exists())

    def test_work_output_is_not_public_until_atomic_publish(self) -> None:
        job_id = "7" * 32
        work_prefix = target._job_prefix({"title": "Title"}, job_id)
        work = target.DOWNLOAD_PATH / f"{work_prefix}.mp4"
        work.write_bytes(b"complete")
        self.assertIsNone(target.resolve_download_path(work.name))

        result = target._finish_job(work_prefix, "video")

        ready = target.DOWNLOAD_PATH / result["name"]
        self.assertTrue(ready.is_file())
        self.assertFalse(work.exists())
        self.assertIsNotNone(target.resolve_download_path(ready.name))

        collision_work = target.DOWNLOAD_PATH / f"{work_prefix}.mp4"
        collision_work.write_bytes(b"new")
        original = ready.read_bytes()
        with self.assertRaises(target.EngineError):
            target._finish_job(work_prefix, "video")
        self.assertEqual(ready.read_bytes(), original)
        self.assertEqual(collision_work.read_bytes(), b"new")

    def test_progress_hook_enforces_cumulative_job_size(self) -> None:
        job_prefix = f"yt-download-test-{'f' * 32}"
        first = target.DOWNLOAD_PATH / f"{job_prefix}.f137.mp4.part"
        second = target.DOWNLOAD_PATH / f"{job_prefix}.f140.m4a.part"
        first.write_bytes(b"x" * 6)

        with patch.object(target, "MAX_MEDIA_BYTES", 10):
            hook = target.make_progress_hook(job_prefix)
            hook({"status": "downloading", "downloaded_bytes": 6})
            second.write_bytes(b"x" * 5)
            with self.assertRaises(target.MediaLimitError):
                hook({"status": "downloading", "downloaded_bytes": 5})

        target.cleanup_job(job_prefix)
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())

    def test_cleanup_uses_small_and_large_ttl(self) -> None:
        small = target.DOWNLOAD_PATH / f"yt-download-small-{'a' * 32}.mp3"
        large = target.DOWNLOAD_PATH / f"yt-download-large-{'b' * 32}.mp4"
        small.write_bytes(b"x")
        large.write_bytes(b"x" * 10)
        os.utime(small, (700.0, 700.0))
        os.utime(large, (700.0, 700.0))

        with (
            patch.object(target, "SMALL_FILE_MAX_BYTES", 10),
            patch.object(target, "SMALL_FILE_TTL_SECONDS", 200),
            patch.object(target, "LARGE_FILE_TTL_SECONDS", 400),
            patch.object(target, "time", return_value=1_000.0),
        ):
            target.cleanup_stale_downloads()

        self.assertFalse(small.exists())
        self.assertTrue(large.exists())

    def test_cleanup_recovers_crashed_claim_without_overwrite(self) -> None:
        ready = target.DOWNLOAD_PATH / f"yt-download-ready-{'8' * 32}.mp3"
        ready.write_bytes(b"media")
        original_stat = ready.stat()
        claim = target.DOWNLOAD_PATH / f"{ready.name}.{'9' * 32}.sending"
        ready.replace(claim)

        target.cleanup_stale_downloads()

        recovered_stat = ready.stat()
        self.assertFalse(claim.exists())
        self.assertEqual(ready.read_bytes(), b"media")
        self.assertEqual(recovered_stat.st_ino, original_stat.st_ino)
        self.assertEqual(recovered_stat.st_mtime_ns, original_stat.st_mtime_ns)

        collision_claim = (
            target.DOWNLOAD_PATH / f"{ready.name}.{'a' * 32}.sending"
        )
        collision_claim.write_bytes(b"other")
        target.cleanup_stale_downloads()
        self.assertEqual(ready.read_bytes(), b"media")
        self.assertEqual(collision_claim.read_bytes(), b"other")

        malformed = target.DOWNLOAD_PATH / f"{ready.name}.invalid.sending"
        malformed.write_bytes(b"invalid")
        target.cleanup_stale_downloads()
        self.assertFalse(malformed.exists())

    def test_cleanup_removes_work_and_partials_but_keeps_unrelated(self) -> None:
        work = target.DOWNLOAD_PATH / f"yt-work-title-{'b' * 32}.mp4"
        partial = target.DOWNLOAD_PATH / f"yt-download-title-{'c' * 32}.mp4.part"
        unrelated = target.DOWNLOAD_PATH / "private.part"
        for artifact in (work, partial, unrelated):
            artifact.write_bytes(b"x")

        target.cleanup_stale_downloads()

        self.assertFalse(work.exists())
        self.assertFalse(partial.exists())
        self.assertTrue(unrelated.exists())

    def test_storage_evicts_oldest_ready_and_failed_delete_stays_counted(
        self,
    ) -> None:
        oldest = target.DOWNLOAD_PATH / f"yt-download-oldest-{'1' * 32}.mp3"
        middle = target.DOWNLOAD_PATH / f"yt-download-middle-{'2' * 32}.mp3"
        newest = target.DOWNLOAD_PATH / f"yt-download-newest-{'3' * 32}.mp3"
        for age, artifact in enumerate((oldest, middle, newest), 1):
            artifact.write_bytes(b"x" * 10)
            os.utime(artifact, (float(age), float(age)))

        with (
            patch.object(target, "MAX_MEDIA_BYTES", 10),
            patch.object(target, "MAX_STORED_BYTES", 30),
            patch.object(target, "SMALL_FILE_TTL_SECONDS", 1_000),
            patch.object(target, "time", return_value=10.0),
        ):
            target.ensure_storage_capacity()

        self.assertFalse(oldest.exists())
        self.assertFalse(middle.exists())
        self.assertTrue(newest.exists())

        failed = target.DOWNLOAD_PATH / f"yt-download-failed-{'4' * 32}.mp3"
        failed.write_bytes(b"x" * 11)
        original_unlink = Path.unlink

        def fail_one(path: Path, missing_ok: bool = False) -> None:
            if path == failed:
                raise OSError("disk error")
            original_unlink(path, missing_ok=missing_ok)

        with (
            patch.object(target, "MAX_MEDIA_BYTES", 10),
            patch.object(target, "MAX_STORED_BYTES", 30),
            patch.object(target, "SMALL_FILE_TTL_SECONDS", 1_000),
            patch.object(target, "time", return_value=10.0),
            patch.object(Path, "unlink", autospec=True, side_effect=fail_one),
            self.assertRaises(target.StorageLimitError),
        ):
            target.ensure_storage_capacity()
        self.assertTrue(failed.exists())

    def test_orphan_job_controls_are_removed_but_active_controls_survive(
        self,
    ) -> None:
        orphan_id = "d" * 32
        for suffix in ("request", "result", "lock"):
            (target.DOWNLOAD_PATH / f".yt-job-{orphan_id}.{suffix}").write_bytes(
                b"1"
            )
        orphan_ready = (
            target.DOWNLOAD_PATH / f"yt-download-orphan-{orphan_id}.mp3"
        )
        orphan_work = target.DOWNLOAD_PATH / f"yt-work-orphan-{orphan_id}.mp3"
        orphan_ready.write_bytes(b"ready")
        orphan_work.write_bytes(b"work")

        unrelated = target.DOWNLOAD_PATH / f"yt-download-unrelated-{'f' * 32}.mp3"
        unrelated.write_bytes(b"unrelated")

        active_id = "e" * 32
        active_lock = target.DOWNLOAD_PATH / f".yt-job-{active_id}.lock"
        active_lock.write_bytes(b"1")
        active_request = target.DOWNLOAD_PATH / f".yt-job-{active_id}.request"
        active_request.write_bytes(b"request")
        handle = target._open_locked(active_lock)
        self.assertIsNotNone(handle)
        assert handle is not None
        try:
            target.cleanup_stale_downloads()
            self.assertTrue(active_lock.exists())
            self.assertTrue(active_request.exists())
        finally:
            target._close_locked(handle)

        self.assertEqual(
            list(target.DOWNLOAD_PATH.glob(f".yt-job-{orphan_id}.*")), []
        )
        self.assertFalse(orphan_ready.exists())
        self.assertFalse(orphan_work.exists())
        self.assertTrue(unrelated.exists())

    def test_operation_lock_and_claim_lock_work_across_processes(self) -> None:
        context = multiprocessing.get_context("spawn")
        result_path = target.DOWNLOAD_PATH / "operation-result.txt"
        handle = target._acquire_operation_lock()
        self.assertIsNotNone(handle)
        assert handle is not None
        with patch.dict(os.environ, {"DOWNLOAD_DIR": str(target.DOWNLOAD_PATH)}):
            busy = context.Process(
                target=_try_operation_lock_in_child,
                args=(str(target.DOWNLOAD_PATH), str(result_path)),
            )
            busy.start()
            busy.join(15)
        self.assertEqual(busy.exitcode, 0)
        self.assertEqual(result_path.read_text(encoding="utf-8"), "busy")
        target._close_locked(handle)

        with patch.dict(os.environ, {"DOWNLOAD_DIR": str(target.DOWNLOAD_PATH)}):
            acquired = context.Process(
                target=_try_operation_lock_in_child,
                args=(str(target.DOWNLOAD_PATH), str(result_path)),
            )
            acquired.start()
            acquired.join(15)
        self.assertEqual(acquired.exitcode, 0)
        self.assertEqual(result_path.read_text(encoding="utf-8"), "acquired")

        ready = target.DOWNLOAD_PATH / f"yt-download-locked-{'f' * 32}.mp4"
        ready.write_bytes(b"video" * 20_000)
        with target.app.test_request_context(f"/downloads/{ready.name}"):
            response = target.download_file(ready.name)
        iterator = iter(response.response)
        self.assertTrue(next(iterator))
        claim = next(target.DOWNLOAD_PATH.glob("*.sending"))
        target.ACTIVE_DOWNLOAD_CLAIMS.clear()
        with patch.dict(os.environ, {"DOWNLOAD_DIR": str(target.DOWNLOAD_PATH)}):
            cleanup = context.Process(
                target=_cleanup_in_child,
                args=(str(target.DOWNLOAD_PATH),),
            )
            cleanup.start()
            cleanup.join(15)
        self.assertEqual(cleanup.exitcode, 0)
        self.assertTrue(claim.exists())
        response.close()
        self.assertTrue(ready.exists())

    def test_generated_file_lock_uses_writable_handle(self) -> None:
        generated = target.DOWNLOAD_PATH / f"yt-download-lock-{'a' * 32}.mp4"
        generated.write_bytes(b"media")

        handle = target._open_locked(generated)

        self.assertIsNotNone(handle)
        assert handle is not None
        try:
            self.assertTrue(handle.writable())
        finally:
            target._close_locked(handle)
        self.assertEqual(generated.read_bytes(), b"media")

    def test_reserves_capacity_and_removes_abandoned_partial(self) -> None:
        for extension in ("mp3", "mp4"):
            target_file = (
                target.DOWNLOAD_PATH / f"yt-download-old-{'c' * 32}.{extension}"
            )
            target_file.write_bytes(b"x" * 100)

        with (
            patch.object(target, "MAX_MEDIA_BYTES", 100),
            patch.object(target, "MAX_STORED_BYTES", 400),
        ):
            target.ensure_storage_capacity()
            extra = target.DOWNLOAD_PATH / f"yt-download-extra-{'d' * 32}.part"
            extra.write_bytes(b"x")
            target.ensure_storage_capacity()

        self.assertFalse(extra.exists())

    def test_root_runs_cleanup_when_download_slot_is_idle(self) -> None:
        with (
            patch.object(
                target.DOWNLOAD_SLOT,
                "acquire",
                return_value=True,
            ) as acquire,
            patch.object(target.DOWNLOAD_SLOT, "release") as release,
            patch.object(target, "cleanup_stale_downloads") as cleanup,
        ):
            response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        acquire.assert_called_once_with(blocking=False)
        cleanup.assert_called_once_with()
        release.assert_called_once_with()

        with (
            patch.object(target.DOWNLOAD_SLOT, "acquire", return_value=True),
            patch.object(target.DOWNLOAD_SLOT, "release") as failed_release,
            patch.object(
                target,
                "cleanup_stale_downloads",
                side_effect=RuntimeError("cleanup failed"),
            ),
        ):
            failed_response = self.client.get("/")

        self.assertEqual(failed_response.status_code, 200)
        failed_release.assert_called_once_with()

    def test_root_skips_cleanup_when_download_slot_is_busy(self) -> None:
        with (
            patch.object(target.DOWNLOAD_SLOT, "acquire", return_value=False),
            patch.object(target.DOWNLOAD_SLOT, "release") as release,
            patch.object(target, "cleanup_stale_downloads") as cleanup,
        ):
            response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        cleanup.assert_not_called()
        release.assert_not_called()

    def test_generated_file_can_be_downloaded_only_once(self) -> None:
        generated = target.DOWNLOAD_PATH / f"yt-download-test-{'b' * 32}.mp3"
        generated.write_bytes(b"media")

        head = self.client.head(f"/downloads/{generated.name}")
        self.assertEqual(head.status_code, 200)
        head.close()
        self.assertTrue(generated.exists())

        first = self.client.get(f"/downloads/{generated.name}", buffered=True)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data, b"media")
        first.close()

        self.assertFalse(generated.exists())
        self.assertEqual(
            self.client.get(f"/downloads/{generated.name}").status_code,
            404,
        )

    def test_unstarted_file_response_restores_its_active_claim(self) -> None:
        generated = target.DOWNLOAD_PATH / f"yt-download-test-{'4' * 32}.mp3"
        generated.write_bytes(b"media")

        with target.app.test_request_context(f"/downloads/{generated.name}"):
            response = target.download_file(generated.name)

        claims = list(target.DOWNLOAD_PATH.glob("*.sending"))
        self.assertFalse(generated.exists())
        self.assertEqual(len(claims), 1)
        self.assertEqual(target.ACTIVE_DOWNLOAD_CLAIMS, {claims[0]})

        response.close()

        self.assertTrue(generated.exists())
        self.assertEqual(list(target.DOWNLOAD_PATH.glob("*.sending")), [])
        self.assertEqual(target.ACTIVE_DOWNLOAD_CLAIMS, set())

    def test_unstarted_wsgi_iterator_restores_its_active_claim(self) -> None:
        generated = target.DOWNLOAD_PATH / f"yt-download-test-{'6' * 32}.mp3"
        generated.write_bytes(b"media")

        with target.app.test_request_context(
            f"/downloads/{generated.name}"
        ) as context:
            response = target.download_file(generated.name)
            app_iterator = response.get_app_iter(context.request.environ)

        close = getattr(app_iterator, "close", None)
        self.assertTrue(callable(close))
        assert callable(close)
        close()

        self.assertTrue(generated.exists())
        self.assertEqual(list(target.DOWNLOAD_PATH.glob("*.sending")), [])
        self.assertEqual(target.ACTIVE_DOWNLOAD_CLAIMS, set())

    def test_active_claim_survives_cleanup_and_counts_toward_storage(self) -> None:
        generated = target.DOWNLOAD_PATH / f"yt-download-test-{'5' * 32}.mp4"
        generated.write_bytes(b"video" * 20_000)

        with target.app.test_request_context(f"/downloads/{generated.name}"):
            response = target.download_file(generated.name)
        iterator = iter(response.response)
        self.assertTrue(next(iterator))
        claim = next(target.DOWNLOAD_PATH.glob("*.sending"))
        os.utime(claim, (1.0, 1.0))

        with (
            patch.object(target, "SMALL_FILE_TTL_SECONDS", 1),
            patch.object(target, "LARGE_FILE_TTL_SECONDS", 1),
            patch.object(target, "time", return_value=10.0),
        ):
            target.cleanup_stale_downloads()

        self.assertTrue(claim.exists())
        with (
            patch.object(target, "MAX_MEDIA_BYTES", 10),
            patch.object(target, "MAX_STORED_BYTES", 21),
            self.assertRaises(target.StorageLimitError),
        ):
            target.ensure_storage_capacity()

        response.close()

        self.assertTrue(generated.exists())
        self.assertEqual(list(target.DOWNLOAD_PATH.glob("*.sending")), [])
        self.assertEqual(target.ACTIVE_DOWNLOAD_CLAIMS, set())

    def test_expired_download_is_deleted_before_head_or_claim(self) -> None:
        for size, age in ((9, 201), (10, 401)):
            generated = (
                target.DOWNLOAD_PATH
                / f"yt-download-expired-{size}-{'8' * 32}.mp4"
            )
            generated.write_bytes(b"x" * size)
            os.utime(generated, (1_000 - age, 1_000 - age))
            with (
                self.subTest(size=size),
                patch.object(target, "SMALL_FILE_MAX_BYTES", 10),
                patch.object(target, "SMALL_FILE_TTL_SECONDS", 200),
                patch.object(target, "LARGE_FILE_TTL_SECONDS", 400),
                patch.object(target, "time", return_value=1_000),
            ):
                response = self.client.head(f"/downloads/{generated.name}")

            self.assertEqual(response.status_code, 404)
            self.assertFalse(generated.exists())
            self.assertEqual(list(target.DOWNLOAD_PATH.glob("*.sending")), [])

    def test_interrupted_download_can_retry_and_concurrent_get_is_blocked(
        self,
    ) -> None:
        generated = target.DOWNLOAD_PATH / f"yt-download-test-{'a' * 32}.mp4"
        content = b"video" * 20000
        generated.write_bytes(content)

        interrupted = self.client.get(
            f"/downloads/{generated.name}",
            buffered=False,
        )
        first_chunk = next(iter(interrupted.response))
        self.assertTrue(first_chunk)
        self.assertFalse(generated.exists())
        with target.app.test_client() as other_client:
            self.assertEqual(
                other_client.get(f"/downloads/{generated.name}").status_code,
                404,
            )
        interrupted.close()

        self.assertTrue(generated.exists())
        self.assertEqual(list(target.DOWNLOAD_PATH.glob("*.sending")), [])
        retried = self.client.get(f"/downloads/{generated.name}", buffered=True)
        self.assertEqual(retried.status_code, 200)
        self.assertEqual(retried.data, content)
        retried.close()
        self.assertFalse(generated.exists())

    def test_serving_accepts_safe_extensions_and_rejects_unsafe_paths(self) -> None:
        unrelated = target.DOWNLOAD_PATH / "private.txt"
        unrelated.write_text("private", encoding="utf-8")

        self.assertIsNone(target.resolve_download_path(unrelated.name))
        self.assertEqual(
            self.client.get(f"/downloads/{unrelated.name}").status_code,
            400,
        )
        self.assertTrue(unrelated.exists())

        for suffix in ("html", "mp4.part"):
            unsafe = (
                target.DOWNLOAD_PATH
                / f"yt-download-test-{'c' * 32}.{suffix}"
            )
            unsafe.write_bytes(b"unsafe")
            with self.subTest(suffix=suffix):
                self.assertIsNone(target.resolve_download_path(unsafe.name))
                self.assertEqual(
                    self.client.get(f"/downloads/{unsafe.name}").status_code,
                    400,
                )

        safe = target.DOWNLOAD_PATH / f"yt-download-test-{'d' * 32}.webm"
        safe.write_bytes(b"video")
        response = self.client.get(f"/downloads/{safe.name}", buffered=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"video")
        response.close()

        with tempfile.TemporaryDirectory() as outside_dir:
            outside = Path(outside_dir) / "outside.webm"
            outside.write_bytes(b"outside")
            symlink = (
                target.DOWNLOAD_PATH
                / f"yt-download-test-{'f' * 32}.webm"
            )
            try:
                symlink.symlink_to(outside)
            except OSError:
                pass
            else:
                self.assertIsNone(target.resolve_download_path(symlink.name))

    def test_delete_is_safe_when_file_disappears_after_check(self) -> None:
        generated = target.DOWNLOAD_PATH / f"yt-download-test-{'e' * 32}.mp3"
        generated.write_bytes(b"media")
        original_unlink = Path.unlink

        def race_unlink(path: Path, missing_ok: bool = False) -> None:
            original_unlink(path, missing_ok=True)
            original_unlink(path, missing_ok=missing_ok)

        with patch.object(
            Path,
            "unlink",
            autospec=True,
            side_effect=race_unlink,
        ):
            response = self.client.post(
                "/delete",
                data={"files": generated.name},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {"status": "Files deleted", "files": [generated.name]},
        )

        first = target.DOWNLOAD_PATH / f"yt-download-first-{'1' * 32}.mp3"
        second = target.DOWNLOAD_PATH / f"yt-download-second-{'2' * 32}.mp3"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        too_many = self.client.post(
            "/delete",
            data={"files": [first.name, second.name]},
            headers=self.headers,
        )
        self.assertEqual(target.MAX_DELETE_FILES, 1)
        self.assertEqual(too_many.status_code, 400)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())

    def test_proxy_headers_are_trusted_only_by_configured_middleware(self) -> None:
        with target.app.test_request_context(
            headers={
                "X-Forwarded-For": "203.0.113.10",
                "X-Real-IP": "203.0.113.11",
            },
            environ_base={"REMOTE_ADDR": "198.51.100.9"},
        ):
            self.assertEqual(target.request_client_id(), "198.51.100.9")

    def test_rate_keys_normalize_mapped_ipv4_and_ipv6_prefixes(self) -> None:
        self.assertEqual(target._rate_limit_key("::ffff:192.0.2.4"), "192.0.2.4")
        self.assertEqual(target._rate_limit_key("192.0.2.4"), "192.0.2.4")
        self.assertEqual(
            target._rate_limit_key("2001:db8:1:2::1"),
            target._rate_limit_key("2001:db8:1:2::ffff"),
        )
        self.assertNotEqual(
            target._rate_limit_key("2001:db8:1:2::1"),
            target._rate_limit_key("2001:db8:1:3::1"),
        )

    def test_global_rate_budget_aggregates_clients_and_expires(self) -> None:
        with (
            patch.object(target, "GLOBAL_RATE_LIMIT_REQUESTS", 2),
            patch.object(target, "RATE_LIMIT_REQUESTS", 10),
            patch.object(target, "monotonic", return_value=100.0),
        ):
            self.assertFalse(target.operation_rate_limit_exceeded("192.0.2.1"))
            self.assertFalse(target.operation_rate_limit_exceeded("192.0.2.2"))
            self.assertTrue(target.operation_rate_limit_exceeded("192.0.2.3"))

        with (
            patch.object(target, "GLOBAL_RATE_LIMIT_REQUESTS", 2),
            patch.object(target, "RATE_LIMIT_REQUESTS", 10),
            patch.object(
                target,
                "monotonic",
                return_value=100.0 + target.RATE_LIMIT_WINDOW_SECONDS + 1,
            ),
        ):
            self.assertFalse(target.operation_rate_limit_exceeded("192.0.2.3"))

    def test_global_rate_rejections_do_not_grow_client_state(self) -> None:
        with (
            patch.object(target, "GLOBAL_RATE_LIMIT_REQUESTS", 1),
            patch.object(target, "RATE_LIMIT_REQUESTS", 1),
            patch.object(target, "RATE_LIMIT_MAX_CLIENTS", 3),
            patch.object(target, "monotonic", return_value=100.0),
        ):
            self.assertFalse(target.operation_rate_limit_exceeded("192.0.2.1"))
            for index in range(20):
                self.assertTrue(
                    target.operation_rate_limit_exceeded(f"198.51.100.{index}")
                )
            self.assertEqual(list(target.DOWNLOAD_ATTEMPTS), ["192.0.2.1"])
            self.assertLessEqual(
                len(target.DOWNLOAD_ATTEMPTS),
                target.RATE_LIMIT_MAX_CLIENTS,
            )

        with (
            patch.object(target, "GLOBAL_RATE_LIMIT_REQUESTS", 100),
            patch.object(target, "RATE_LIMIT_REQUESTS", 1),
            patch.object(target, "monotonic", return_value=100.0),
        ):
            self.assertTrue(target.operation_rate_limit_exceeded("192.0.2.1"))

    def test_global_rate_rejection_happens_before_operation_slot(self) -> None:
        expected = self.media_response()
        with (
            patch.object(target, "GLOBAL_RATE_LIMIT_REQUESTS", 1),
            patch.object(target, "RATE_LIMIT_REQUESTS", 10),
            patch.object(target, "run_job", return_value=expected) as run_job,
            patch.object(
                target.DOWNLOAD_SLOT,
                "acquire",
                wraps=target.DOWNLOAD_SLOT.acquire,
            ) as acquire,
        ):
            first = self.client.post(
                "/inspect",
                json={"url": YOUTUBE_URL},
                headers=self.headers,
                environ_overrides={"REMOTE_ADDR": "192.0.2.1"},
            )
            limited = self.client.post(
                "/inspect",
                json={"url": YOUTUBE_URL},
                headers=self.headers,
                environ_overrides={"REMOTE_ADDR": "192.0.2.2"},
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(limited.status_code, 429)
        acquire.assert_called_once_with(blocking=False)
        run_job.assert_called_once_with("inspect", YOUTUBE_URL, None, None, ANY)

    def test_api_and_download_responses_are_private_no_store(self) -> None:
        denied = self.client.post("/inspect", json={"url": YOUTUBE_URL})
        self.assertEqual(
            denied.headers.get("Strict-Transport-Security"),
            "max-age=31536000",
        )
        self.assertEqual(
            denied.headers.get("Cache-Control"),
            "private, no-store, max-age=0",
        )

        ready = target.DOWNLOAD_PATH / f"yt-download-cache-{'0' * 32}.mp3"
        ready.write_bytes(b"media")
        response = self.client.get(f"/downloads/{ready.name}", buffered=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers.get("Cache-Control"),
            "private, no-store, max-age=0",
        )
        response.close()

        original_wsgi_app = target.app.wsgi_app
        target.app.wsgi_app = target.ProxyFix(
            original_wsgi_app,
            x_for=1,
            x_proto=1,
            x_host=0,
        )
        forwarded_headers = {
            "Host": "media.example",
            "Origin": "https://media.example",
            "X-Forwarded-For": "203.0.113.10",
            "X-Forwarded-Proto": "https",
        }
        try:
            with (
                patch.object(target, "TRUST_X_REAL_IP", False),
                patch.object(target, "RATE_LIMIT_REQUESTS", 1),
                patch.object(
                    target,
                    "run_job",
                    return_value=self.media_response(),
                ) as run_job,
            ):
                first = self.client.post(
                    "/inspect",
                    json={"url": YOUTUBE_URL},
                    headers={**forwarded_headers, "X-Real-IP": "203.0.113.11"},
                    environ_overrides={"REMOTE_ADDR": "198.51.100.1"},
                )
                limited = self.client.post(
                    "/download",
                    json={"url": YOUTUBE_URL, "mode": "audio"},
                    headers={**forwarded_headers, "X-Real-IP": "203.0.113.12"},
                    environ_overrides={"REMOTE_ADDR": "198.51.100.2"},
                )
        finally:
            target.app.wsgi_app = original_wsgi_app

        self.assertEqual(first.status_code, 200)
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(set(limited.get_json()), {"error"})
        run_job.assert_called_once_with("inspect", YOUTUBE_URL, None, None, ANY)

    def test_trusted_alwaysdata_headers_keep_origin_and_rate_limit_per_user(
        self,
    ) -> None:
        original_wsgi_app = target.app.wsgi_app
        target.app.wsgi_app = target.ProxyFix(
            original_wsgi_app,
            x_for=0,
            x_proto=1,
            x_host=0,
        )
        alwaysdata_headers = {
            "Host": "media.example",
            "Origin": "https://media.example",
            "X-Forwarded-Proto": "https",
            "X-Real-IP": "203.0.113.20",
        }
        try:
            with (
                patch.object(target, "TRUST_PROXY_HEADERS", True),
                patch.object(target, "TRUST_X_REAL_IP", True),
                patch.object(target, "RATE_LIMIT_REQUESTS", 1),
                patch.object(
                    target,
                    "run_job",
                    return_value=self.media_response(),
                ) as run_job,
            ):
                first = self.client.post(
                    "/inspect",
                    json={"url": YOUTUBE_URL},
                    headers=alwaysdata_headers,
                    environ_overrides={"REMOTE_ADDR": "198.51.100.1"},
                )
                limited = self.client.post(
                    "/inspect",
                    json={"url": YOUTUBE_URL},
                    headers=alwaysdata_headers,
                    environ_overrides={"REMOTE_ADDR": "198.51.100.2"},
                )
        finally:
            target.app.wsgi_app = original_wsgi_app

        self.assertEqual(first.status_code, 200)
        self.assertEqual(limited.status_code, 429)
        run_job.assert_called_once_with("inspect", YOUTUBE_URL, None, None, ANY)

        with (
            patch.object(target, "TRUST_PROXY_HEADERS", True),
            patch.object(target, "TRUST_X_REAL_IP", True),
            target.app.test_request_context(
                headers={"X-Real-IP": "not-an-ip"},
                environ_base={"REMOTE_ADDR": "198.51.100.9"},
            ),
        ):
            self.assertEqual(target.request_client_id(), "198.51.100.9")

    def test_generated_names_are_bounded_by_utf8_bytes(self) -> None:
        title = "界" * 100
        safe_title = target.sanitize_title(title)
        self.assertLessEqual(len(safe_title.encode("utf-8")), 60)
        with patch.object(
            target,
            "uuid4",
            return_value=SimpleNamespace(hex="7" * 32),
        ):
            job_prefix = target._job_prefix({"title": title}, "7" * 32)
        self.assertLess(len(f"{job_prefix}.f137.mp4.part".encode()), 255)

    def test_resource_defaults_match_public_service_contract(self) -> None:
        self.assertEqual(target.MAX_MEDIA_BYTES, 216 * 1024 * 1024)
        self.assertEqual(target.MAX_STORED_BYTES, 648 * 1024 * 1024)
        self.assertEqual(target.MAX_PLAYLIST_ITEMS, 50)
        self.assertEqual(target.SOCKET_TIMEOUT_SECONDS, 15)
        self.assertEqual(target.JOB_TIMEOUT_SECONDS, 900)
        self.assertEqual(target.JOB_MEMORY_BYTES, 192 * 1024 * 1024)
        self.assertEqual(target.MAX_HLS_MANIFEST_BYTES, 8 * 1024 * 1024)
        self.assertEqual(target.GLOBAL_RATE_LIMIT_REQUESTS, 60)
        self.assertEqual(target.SMALL_FILE_MAX_BYTES, 64 * 1024 * 1024)
        self.assertEqual(target.SMALL_FILE_TTL_SECONDS, 600)
        self.assertEqual(target.LARGE_FILE_TTL_SECONDS, 1800)

    def test_config_rejects_invalid_resource_environments(self) -> None:
        for name in (
            "MAX_PLAYLIST_ITEMS",
            "SOCKET_TIMEOUT_SECONDS",
            "JOB_TIMEOUT_SECONDS",
            "JOB_MEMORY_BYTES",
            "MAX_HLS_MANIFEST_BYTES",
            "SMALL_FILE_MAX_BYTES",
            "SMALL_FILE_TTL_SECONDS",
            "LARGE_FILE_TTL_SECONDS",
            "GLOBAL_RATE_LIMIT_REQUESTS",
        ):
            try:
                with (
                    self.subTest(name=name),
                    patch.dict(os.environ, {name: "0"}),
                    self.assertRaises(ValueError),
                ):
                    importlib.reload(config)
            finally:
                importlib.reload(config)

        try:
            with (
                patch.dict(
                    os.environ,
                    {"MAX_MEDIA_BYTES": "101", "MAX_STORED_BYTES": "201"},
                ),
                self.assertRaises(ValueError),
            ):
                importlib.reload(config)
        finally:
            importlib.reload(config)

    def test_rejects_oversized_json_requests(self) -> None:
        response = self.client.post(
            "/inspect",
            json={"url": YOUTUBE_URL, "padding": "x" * 20_000},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.get_json(), {"error": "Request is too large."})


if __name__ == "__main__":
    unittest.main()
