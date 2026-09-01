from __future__ import annotations

import ipaddress
import json
import logging
import math
import os
import re
import signal
import socket
import subprocess
import sys
from collections import OrderedDict, deque
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from threading import BoundedSemaphore, Lock, Thread
from time import monotonic, sleep, time
from typing import Any, BinaryIO, Literal, Protocol, TypedDict, cast
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

JOB_WORKER_MODE = len(sys.argv) == 2 and sys.argv[1] == "--job-worker"


def _watch_parent(liveness_fd: int) -> None:
    try:
        while os.read(liveness_fd, 1):
            pass
    except OSError:
        pass
    finally:
        with suppress(OSError):
            os.close(liveness_fd)
    if os.name == "posix":
        with suppress(OSError):
            os.killpg(0, signal.SIGKILL)


if JOB_WORKER_MODE and os.name == "posix":
    try:
        _worker_liveness_fd = int(os.environ["YTDL_JOB_LIVENESS_FD"])
    except (KeyError, ValueError) as error:
        raise SystemExit(1) from error
    Thread(target=_watch_parent, args=(_worker_liveness_fd,), daemon=True).start()

os.environ["YTDLP_NO_PLUGINS"] = "1"

from flask import (  # noqa: E402
    Flask,
    Response,
    abort,
    jsonify,
    render_template,
    request,
    send_file,
    send_from_directory,
)
from werkzeug.middleware.proxy_fix import ProxyFix  # noqa: E402
from youtube_dl import YoutubeDL as BaseLegacyYoutubeDL  # noqa: E402
from youtube_dl.utils import DownloadError as LegacyDownloadError  # noqa: E402
from yt_dlp import YoutubeDL as BaseYtDlp  # noqa: E402
from yt_dlp.dependencies import Cryptodome  # noqa: E402
from yt_dlp.downloader import get_suitable_downloader  # noqa: E402
from yt_dlp.downloader.dash import DashSegmentsFD  # noqa: E402
from yt_dlp.downloader.hls import HlsFD  # noqa: E402
from yt_dlp.downloader.http import HttpFD  # noqa: E402
from yt_dlp.networking._requests import RequestsRH  # noqa: E402
from yt_dlp.networking.common import RequestDirector  # noqa: E402
from yt_dlp.utils import (  # noqa: E402
    DownloadError as YtDlpDownloadError,
)
from yt_dlp.utils import (  # noqa: E402
    MaxDownloadsReached,
    determine_protocol,
)
from yt_dlp.utils import (  # noqa: E402
    Popen as YtDlpPopen,
)

from config import (  # noqa: E402
    ALLOWED_ORIGINS,
    DOWNLOAD_DIR,
    GLOBAL_RATE_LIMIT_REQUESTS,
    HOST,
    JOB_MEMORY_BYTES,
    JOB_TIMEOUT_SECONDS,
    LARGE_FILE_TTL_SECONDS,
    MAX_DURATION_SECONDS,
    MAX_HLS_MANIFEST_BYTES,
    MAX_MEDIA_BYTES,
    MAX_PLAYLIST_ITEMS,
    MAX_REQUEST_BYTES,
    MAX_RETRIES,
    MAX_STORED_BYTES,
    MAX_URL_LENGTH,
    PORT,
    RATE_LIMIT_MAX_CLIENTS,
    RATE_LIMIT_REQUESTS,
    RATE_LIMIT_WINDOW_SECONDS,
    SMALL_FILE_MAX_BYTES,
    SMALL_FILE_TTL_SECONDS,
    SOCKET_TIMEOUT_SECONDS,
    TRUST_PROXY_HEADERS,
    TRUST_X_REAL_IP,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

app = Flask(__name__, static_folder="static", template_folder="templates")
if TRUST_PROXY_HEADERS:
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=0 if TRUST_X_REAL_IP else 1,
        x_proto=1,
        x_host=0,
    )
app.config.update(
    MAX_CONTENT_LENGTH=MAX_REQUEST_BYTES,
    MAX_FORM_MEMORY_SIZE=MAX_REQUEST_BYTES,
    MAX_FORM_PARTS=8,
)

DOWNLOAD_PATH = DOWNLOAD_DIR
DOWNLOAD_PATH.mkdir(parents=True, exist_ok=True)
STATIC_PATH = Path(__file__).parent / "static"

OutputMode = Literal["audio", "video"]


class DownloadedFile(TypedDict):
    name: str
    extension: str
    mode: OutputMode
    size_bytes: int


class MediaResponse(TypedDict):
    kind: Literal["media"]
    source: str
    title: str
    duration_seconds: int | float | None
    is_live: Literal[False]
    outputs: list[OutputMode]
    engine: str


class PlaylistEntry(TypedDict):
    index: int
    title: str


class PlaylistResponse(TypedDict):
    kind: Literal["playlist"]
    source: str
    title: str
    entries: list[PlaylistEntry]
    truncated: bool
    engine: str


InspectResponse = MediaResponse | PlaylistResponse


class DownloadMatchFilter(Protocol):
    def __call__(
        self,
        info: Mapping[str, object],
        *,
        incomplete: bool = False,
    ) -> str | None: ...


ProgressHook = Callable[[Mapping[str, object]], None]


class NoFormatsRaiser(Protocol):
    def __call__(
        self,
        info: Mapping[str, object],
        forced: bool = False,
    ) -> None: ...


class SuitableDownloaderResolver(Protocol):
    def __call__(
        self,
        info: Mapping[str, object],
        params: Mapping[str, object],
        *,
        to_stdout: bool = False,
    ) -> type[object] | None: ...


class MediaDownloader(Protocol):
    def add_progress_hook(self, hook: ProgressHook) -> None: ...

    def download(
        self,
        name: str,
        info: Mapping[str, object],
        subtitle: bool = False,
    ) -> tuple[bool | None, bool]: ...


class UrlResponse(Protocol):
    url: str

    def get_header(self, name: str, default: str | None = None) -> str | None: ...

    def read(self, amount: int | None = None) -> bytes: ...

    def __enter__(self) -> UrlResponse: ...

    def __exit__(
        self,
        exception_type: object,
        exception: object,
        traceback: object,
    ) -> bool | None: ...


class UrlOpener(Protocol):
    def urlopen(self, request: object) -> UrlResponse: ...


class HlsRuntime(Protocol):
    ydl: UrlOpener

    def _prepare_url(
        self,
        info: Mapping[str, object],
        url: str,
    ) -> object: ...


class YtDlpRuntime(Protocol):
    _request_director: RequestDirector
    _progress_hooks: Iterable[ProgressHook]

    def _copy_infodict(
        self,
        info: Mapping[str, object],
    ) -> dict[str, object]: ...

    def _calc_headers(self, info: Mapping[str, object]) -> object: ...


class YtDlpFactory(Protocol):
    def __call__(
        self,
        params: Mapping[str, object] | None = None,
    ) -> GuardedYtDlp: ...


ProtocolResolver = Callable[[Mapping[str, object]], object]
DownloaderConstructor = Callable[
    [object, Mapping[str, object]],
    MediaDownloader,
]
HlsCapabilityChecker = Callable[[str, Mapping[str, object], bool], bool]
HlsDownload = Callable[[str, Mapping[str, object]], bool | None]
RequestDirectorBuilder = Callable[
    [Collection[type[RequestsRH]], Collection[object] | None],
    RequestDirector,
]

_RESOLVE_PROTOCOL = cast(ProtocolResolver, determine_protocol)
_RESOLVE_DOWNLOADER = cast(SuitableDownloaderResolver, get_suitable_downloader)
_HLS_CAN_DOWNLOAD = cast(HlsCapabilityChecker, HlsFD.can_download)

YOUTUBE_HOSTS = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
)
VIDEO_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{11}")
SAFE_MEDIA_EXTENSIONS = frozenset(
    {
        "3gp",
        "aac",
        "flac",
        "m4a",
        "mka",
        "mkv",
        "mov",
        "mp3",
        "mp4",
        "ogg",
        "opus",
        "wav",
        "webm",
    }
)
SAFE_EXTENSION_PATTERN = "|".join(sorted(SAFE_MEDIA_EXTENSIONS))
SAFE_PROTOCOL_FILTER = (
    "[protocol~='^(?:https?|m3u8|m3u8_native|http_dash_segments)$']"
)
SAFE_DOWNLOAD_PROTOCOLS = frozenset(
    {"http", "https", "m3u8", "m3u8_native", "http_dash_segments"}
)
PROXY_ENVIRONMENT_KEYS = (
    "ALL_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "all_proxy",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)
WEB_PORTS = frozenset({80, 443, "80", "443", "http", "https", b"80", b"443"})
NETWORK_FREE_PROTOCOLS = "file,crypto,data"
FFMPEG_EXECUTABLES = frozenset({"ffmpeg", "ffmpeg.exe", "avconv", "avconv.exe"})
FFPROBE_EXECUTABLES = frozenset(
    {"ffprobe", "ffprobe.exe", "avprobe", "avprobe.exe"}
)
PHANTOMJS_EXECUTABLES = frozenset({"phantomjs", "phantomjs.exe"})
GENERATED_FILE_PATTERN = re.compile(
    rf"yt-download-.+-[0-9a-f]{{32}}\.(?:{SAFE_EXTENSION_PATTERN})"
)
JOB_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
WORK_ARTIFACT_PATTERN = re.compile(
    r"yt-work-.+-(?P<job_id>[0-9a-f]{32})\..+"
)
DOWNLOAD_ARTIFACT_PATTERN = re.compile(
    r"yt-download-.+-(?P<job_id>[0-9a-f]{32})\..+"
)
CLAIM_PATTERN = re.compile(
    rf"(?P<ready>yt-download-.+-[0-9a-f]{{32}}\."
    rf"(?:{SAFE_EXTENSION_PATTERN}))\.(?P<claim>[0-9a-f]{{32}})\.sending"
)
JOB_CONTROL_PATTERN = re.compile(
    r"\.yt-job-(?P<job_id>[0-9a-f]{32})\.(?:request|result|lock)"
)
JOB_RESULT_MAX_BYTES = 64 * 1024
JOB_REAP_SECONDS = 5.0
JOB_POLL_SECONDS = 0.05
MAX_DELETE_FILES = 1
VIDEO_STREAM_MAX_BYTES = MAX_MEDIA_BYTES * 60 // 100
VIDEO_AUDIO_MAX_BYTES = MAX_MEDIA_BYTES * 35 // 100
AUDIO_FORMAT = "/".join(
    (
        f"ba[acodec^=mp3][filesize<={MAX_MEDIA_BYTES}]{SAFE_PROTOCOL_FILTER}",
        f"ba[acodec^=mp3][filesize_approx<={MAX_MEDIA_BYTES}]"
        f"{SAFE_PROTOCOL_FILTER}",
        f"ba[filesize<={MAX_MEDIA_BYTES}]{SAFE_PROTOCOL_FILTER}",
        f"ba[filesize_approx<={MAX_MEDIA_BYTES}]{SAFE_PROTOCOL_FILTER}",
        f"b[filesize<={MAX_MEDIA_BYTES}]{SAFE_PROTOCOL_FILTER}",
        f"b[filesize_approx<={MAX_MEDIA_BYTES}]{SAFE_PROTOCOL_FILTER}",
        f"ba{SAFE_PROTOCOL_FILTER}",
        f"b{SAFE_PROTOCOL_FILTER}",
    )
)
VIDEO_FORMAT = "/".join(
    (
        f"bv*[ext=mp4][filesize<={VIDEO_STREAM_MAX_BYTES}]"
        f"{SAFE_PROTOCOL_FILTER}+ba[ext=m4a][filesize<={VIDEO_AUDIO_MAX_BYTES}]"
        f"{SAFE_PROTOCOL_FILTER}",
        f"bv*[ext=mp4][filesize_approx<={VIDEO_STREAM_MAX_BYTES}]"
        f"{SAFE_PROTOCOL_FILTER}+ba[ext=m4a]"
        f"[filesize_approx<={VIDEO_AUDIO_MAX_BYTES}]{SAFE_PROTOCOL_FILTER}",
        f"b[ext=mp4][filesize<={MAX_MEDIA_BYTES}]{SAFE_PROTOCOL_FILTER}",
        f"b[ext=mp4][filesize_approx<={MAX_MEDIA_BYTES}]"
        f"{SAFE_PROTOCOL_FILTER}",
        f"bv*[ext=mp4][filesize<={MAX_MEDIA_BYTES}]{SAFE_PROTOCOL_FILTER}",
        f"bv*[ext=mp4][filesize_approx<={MAX_MEDIA_BYTES}]"
        f"{SAFE_PROTOCOL_FILTER}",
        f"bv*[filesize<={VIDEO_STREAM_MAX_BYTES}]{SAFE_PROTOCOL_FILTER}"
        f"+ba[filesize<={VIDEO_AUDIO_MAX_BYTES}]{SAFE_PROTOCOL_FILTER}",
        f"bv*[filesize_approx<={VIDEO_STREAM_MAX_BYTES}]{SAFE_PROTOCOL_FILTER}"
        f"+ba[filesize_approx<={VIDEO_AUDIO_MAX_BYTES}]{SAFE_PROTOCOL_FILTER}",
        f"b[filesize<={MAX_MEDIA_BYTES}]{SAFE_PROTOCOL_FILTER}",
        f"b[filesize_approx<={MAX_MEDIA_BYTES}]{SAFE_PROTOCOL_FILTER}",
        f"bv*[filesize<={MAX_MEDIA_BYTES}]{SAFE_PROTOCOL_FILTER}",
        f"bv*[filesize_approx<={MAX_MEDIA_BYTES}]{SAFE_PROTOCOL_FILTER}",
        f"bv*[ext=mp4]{SAFE_PROTOCOL_FILTER}+ba[ext=m4a]{SAFE_PROTOCOL_FILTER}",
        f"b[ext=mp4]{SAFE_PROTOCOL_FILTER}",
        f"bv*[ext=mp4]{SAFE_PROTOCOL_FILTER}",
        f"bv*{SAFE_PROTOCOL_FILTER}+ba{SAFE_PROTOCOL_FILTER}",
        f"b{SAFE_PROTOCOL_FILTER}",
        f"bv*{SAFE_PROTOCOL_FILTER}",
    )
)
LEGACY_FORMAT = "18"
NO_FORMAT_ERROR = "Requested format is not available"

# ponytail: one process-local slot matches the deployed one-worker service.
DOWNLOAD_SLOT = BoundedSemaphore(1)
DOWNLOAD_ATTEMPTS: OrderedDict[str, deque[float]] = OrderedDict()
GLOBAL_OPERATION_ATTEMPTS: deque[float] = deque()
DOWNLOAD_ATTEMPTS_LOCK = Lock()
DOWNLOAD_FILES_LOCK = Lock()
ACTIVE_DOWNLOAD_CLAIMS: set[Path] = set()
ORPHANED_OPERATION_LOCKS: list[BinaryIO] = []
ORPHANED_JOB_LOCKS: list[BinaryIO] = []
ORPHANED_JOB_PROCESSES: list[subprocess.Popen[bytes]] = []


class InputError(Exception):
    pass


class UnsupportedMediaError(Exception):
    pass


class MediaLimitError(Exception):
    pass


class StorageLimitError(Exception):
    pass


class EngineError(Exception):
    pass


class OperationTimeoutError(Exception):
    pass


class JobReapError(EngineError):
    pass


def _is_direct_legacy_format(info: Mapping[str, object]) -> bool:
    url = info.get("url")
    protocol = info.get("protocol")
    if (
        info.get("format_id") != LEGACY_FORMAT
        or protocol not in {"http", "https"}
        or not isinstance(url, str)
        or not url.strip()
    ):
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return parsed.scheme == protocol and parsed.hostname is not None


class LegacyYoutubeDL(BaseLegacyYoutubeDL):
    def process_info(self, info_dict: dict[str, object]) -> None:
        if not _is_direct_legacy_format(info_dict):
            raise LegacyDownloadError(
                "The fallback engine accepts only direct format 18."
            )
        super().process_info(info_dict)


class DirectRequestsRH(RequestsRH):
    def _get_proxies(self, request: object) -> dict[str, None]:
        return {"all": None}


def _safe_downloader_type(
    info: Mapping[str, object],
    params: Mapping[str, object],
    *,
    to_stdout: bool = False,
) -> type[HlsFD] | type[HttpFD] | type[DashSegmentsFD] | None:
    url = info.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    safe_info = dict(info)
    try:
        protocol = _RESOLVE_PROTOCOL(safe_info)
    except (KeyError, TypeError):
        return None
    if not isinstance(protocol, str) or not protocol:
        return None
    if any(part not in SAFE_DOWNLOAD_PROTOCOLS for part in protocol.split("+")):
        return None
    downloader_type = _RESOLVE_DOWNLOADER(
        safe_info,
        params,
        to_stdout=to_stdout,
    )
    if downloader_type is HlsFD:
        return HlsFD
    if downloader_type is HttpFD:
        return HttpFD
    if downloader_type is DashSegmentsFD:
        return DashSegmentsFD
    return None


class GuardedHlsFD(HlsFD):
    def real_download(
        self,
        filename: str,
        info_dict: Mapping[str, object],
    ) -> bool | None:
        safe_info = dict(info_dict)
        manifest = safe_info.get("hls_media_playlist_data")
        if isinstance(manifest, str):
            if len(manifest) > MAX_HLS_MANIFEST_BYTES:
                raise YtDlpDownloadError("The HLS manifest exceeds the size limit.")
            try:
                manifest_size = len(manifest.encode("utf-8"))
            except UnicodeEncodeError as error:
                raise YtDlpDownloadError("The HLS manifest is invalid.") from error
            if manifest_size > MAX_HLS_MANIFEST_BYTES:
                raise YtDlpDownloadError("The HLS manifest exceeds the size limit.")
        else:
            manifest_url = safe_info.get("url")
            if not isinstance(manifest_url, str):
                raise YtDlpDownloadError("The HLS manifest URL is invalid.")
            runtime = cast(HlsRuntime, self)
            with runtime.ydl.urlopen(
                runtime._prepare_url(safe_info, manifest_url)
            ) as response:
                content_length = response.get_header("Content-Length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError:
                        pass
                    else:
                        if declared_length > MAX_HLS_MANIFEST_BYTES:
                            raise YtDlpDownloadError(
                                "The HLS manifest exceeds the size limit."
                            )
                manifest_url = response.url
                manifest_bytes = response.read(MAX_HLS_MANIFEST_BYTES + 1)
            if not isinstance(manifest_bytes, bytes):
                raise YtDlpDownloadError("The HLS manifest is invalid.")
            if len(manifest_bytes) > MAX_HLS_MANIFEST_BYTES:
                raise YtDlpDownloadError("The HLS manifest exceeds the size limit.")
            manifest = manifest_bytes.decode("utf-8", "ignore")
            safe_info["url"] = manifest_url
            safe_info["hls_media_playlist_data"] = manifest

        if not any(
            line.strip() == "#EXT-X-ENDLIST" for line in manifest.splitlines()
        ):
            raise YtDlpDownloadError("Live or incomplete HLS media is blocked.")
        if not _HLS_CAN_DOWNLOAD(manifest, safe_info, False):
            raise YtDlpDownloadError("Unsupported or protected HLS media is blocked.")
        if "#EXT-X-KEY:METHOD=AES-128" in manifest and not Cryptodome.AES:
            raise YtDlpDownloadError("AES-128 HLS requires native decryption support.")
        real_download = cast(HlsDownload, super().real_download)
        return real_download(filename, safe_info)


class GuardedYtDlp(BaseYtDlp):
    def build_request_director(
        self,
        handlers: object,
        preferences: Collection[object] | None = None,
    ) -> RequestDirector:
        builder = cast(RequestDirectorBuilder, super().build_request_director)
        return builder((DirectRequestsRH,), preferences)

    # yt-dlp has no published types. Pyright infers bool here even though the
    # pinned FileDownloader.download runtime returns (success, attempted).
    def dl(  # type: ignore[override]
        self,
        name: str,
        info: Mapping[str, object],
        subtitle: bool = False,
        test: bool = False,
    ) -> tuple[bool | None, bool]:
        url = info.get("url")
        if not isinstance(url, str) or not url.strip():
            raise_no_formats = cast(NoFormatsRaiser, self.raise_no_formats)
            raise_no_formats(info, True)
        if test:
            verbose = self.params.get("verbose")
            quiet = self.params.get("quiet") or not verbose
            params = {
                "test": True,
                "quiet": quiet,
                "verbose": verbose,
                "noprogress": quiet,
                "nopart": True,
                "skip_unavailable_fragments": False,
                "keep_fragments": False,
                "overwrites": True,
                "_no_ytdl_file": True,
            }
        else:
            params = self.params

        downloader_type = _safe_downloader_type(
            info,
            params,
            to_stdout=name == "-",
        )
        if downloader_type is None:
            raise YtDlpDownloadError(
                "The media transport or downloader is not allowed."
            )
        if downloader_type is HlsFD:
            guarded_downloader_type = GuardedHlsFD
        elif downloader_type is HttpFD:
            guarded_downloader_type = HttpFD
        elif downloader_type is DashSegmentsFD:
            guarded_downloader_type = DashSegmentsFD
        else:
            raise YtDlpDownloadError("The media downloader is not allowed.")
        downloader_factory = cast(DownloaderConstructor, guarded_downloader_type)
        downloader = downloader_factory(self, params)
        runtime = cast(YtDlpRuntime, self)
        if not test:
            for progress_hook in runtime._progress_hooks:
                downloader.add_progress_hook(progress_hook)

        safe_info = runtime._copy_infodict(info)
        if safe_info.get("http_headers") is None:
            safe_info["http_headers"] = runtime._calc_headers(safe_info)
        return downloader.download(name, safe_info, subtitle)


YtDlp = cast(YtDlpFactory, GuardedYtDlp)


def guard_media_command(command: object) -> object:
    if not isinstance(command, Sequence) or isinstance(command, str | bytes):
        return command
    if not command or not isinstance(command[0], str | bytes | os.PathLike):
        return command
    executable = os.fspath(command[0])
    name = os.path.basename(os.fsdecode(executable)).casefold()
    if name in PHANTOMJS_EXECUTABLES:
        raise EngineError("PhantomJS network access is blocked.")
    if name not in FFMPEG_EXECUTABLES | FFPROBE_EXECUTABLES:
        return command
    for argument in command[1:]:
        if (
            isinstance(argument, str)
            and (
                argument == "-protocol_whitelist"
                or argument.startswith("-protocol_whitelist=")
            )
        ) or (
            isinstance(argument, bytes)
            and (
                argument == b"-protocol_whitelist"
                or argument.startswith(b"-protocol_whitelist=")
            )
        ):
            raise EngineError("The media processor protocol policy was overridden.")

    encoded = isinstance(executable, bytes)
    flag = b"-protocol_whitelist" if encoded else "-protocol_whitelist"
    protocols = (
        NETWORK_FREE_PROTOCOLS.encode() if encoded else NETWORK_FREE_PROTOCOLS
    )
    if name in FFPROBE_EXECUTABLES:
        return [command[0], flag, protocols, *command[1:]]

    guarded: list[object] = [command[0]]
    for argument in command[1:]:
        if isinstance(argument, str | bytes) and os.fsdecode(argument) == "-i":
            guarded.extend((flag, protocols))
        guarded.append(argument)
    return guarded


@contextmanager
def public_network_only() -> Iterator[None]:
    original_getaddrinfo = socket.getaddrinfo
    original_ytdlp_descriptor = YtDlpPopen.__dict__["run"]
    original_ytdlp_run = YtDlpPopen.run
    original_popen = subprocess.Popen
    original_proxy_environment = {
        key: os.environ.pop(key, None) for key in PROXY_ENVIRONMENT_KEYS
    }

    # Variadic wrappers mirror third-party subprocess call signatures.
    def guarded_ytdlp_run(
        _cls: type[YtDlpPopen],
        command: object,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return original_ytdlp_run(guard_media_command(command), *args, **kwargs)

    class GuardedLegacyPopen(original_popen):
        def __init__(
            self,
            command: object,
            *args: Any,
            **kwargs: Any,
        ) -> None:
            # Typeshed cannot express this third-party variadic command wrapper.
            super().__init__(cast(Any, guard_media_command(command)), *args, **kwargs)

    def guarded_getaddrinfo(
        host: str | bytes | None,
        port: str | bytes | int | None,
        family: int = 0,
        type: int = 0,
        proto: int = 0,
        flags: int = 0,
    ) -> list[
        tuple[
            socket.AddressFamily,
            socket.SocketKind,
            int,
            str,
            tuple[str, int] | tuple[str, int, int, int] | tuple[int, bytes],
        ]
    ]:
        if port not in WEB_PORTS:
            raise OSError("Only standard web ports are allowed.")
        addresses = original_getaddrinfo(host, port, family, type, proto, flags)
        if not addresses:
            raise OSError("The destination hostname did not resolve.")
        for _family, _type, _proto, _name, address in addresses:
            destination = ipaddress.ip_address(address[0])
            if not destination.is_global or destination.is_multicast:
                raise OSError("Private network destinations are blocked.")
        return addresses

    # ponytail: process-wide guard is safe with one outbound slot; use an
    # egress proxy before adding concurrent workers.
    socket.getaddrinfo = guarded_getaddrinfo
    # setattr preserves the third-party descriptor for exact restoration.
    setattr(YtDlpPopen, "run", classmethod(guarded_ytdlp_run))  # noqa: B010
    setattr(subprocess, "Popen", GuardedLegacyPopen)  # noqa: B010
    try:
        yield
    finally:
        socket.getaddrinfo = original_getaddrinfo
        setattr(YtDlpPopen, "run", original_ytdlp_descriptor)  # noqa: B010
        setattr(subprocess, "Popen", original_popen)  # noqa: B010
        for key in PROXY_ENVIRONMENT_KEYS:
            os.environ.pop(key, None)
        for key, value in original_proxy_environment.items():
            if value is not None:
                os.environ[key] = value


def sanitize_title(title: str) -> str:
    cleaned = re.sub(r"[^\w\- ]", "", title).strip()
    bounded = cleaned.encode()[:60].decode("utf-8", "ignore").strip()
    return bounded or "media-file"


def validate_public_url(value: str) -> str:
    if not isinstance(value, str):
        raise InputError("A public media URL is required.")
    link = value.strip()
    if not link or len(link) > MAX_URL_LENGTH:
        raise InputError("A valid public media URL is required.")
    try:
        parsed = urlsplit(link)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise InputError("The media URL is invalid.") from error
    if parsed.scheme.casefold() not in {"http", "https"} or not hostname:
        raise InputError("Only public HTTP and HTTPS URLs are supported.")
    if (
        parsed.username is not None
        or parsed.password is not None
        or "\\" in parsed.netloc
    ):
        raise InputError("Credentials are not allowed in media URLs.")
    if port is not None and port not in {80, 443}:
        raise InputError("Only standard web ports are supported.")

    host = hostname.rstrip(".").casefold()
    if not host or host == "localhost" or host.endswith((".localhost", ".local")):
        raise InputError("Local network URLs are not supported.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or all(character in "0123456789." for character in host):
            raise InputError("A public hostname is required.") from None
    else:
        if not address.is_global:
            raise InputError("Local or special-purpose IP addresses are not supported.")
    return link


def is_youtube_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError:
        return False
    if not hostname:
        return False
    host = hostname.rstrip(".").casefold()
    if parsed.scheme.casefold() not in {"http", "https"} or host not in YOUTUBE_HOSTS:
        return False
    query = parse_qs(parsed.query)
    if "list" in query:
        return False

    segments = parsed.path.strip("/").split("/") if parsed.path.strip("/") else []
    video_id: str | None = None
    if host == "youtu.be" and len(segments) == 1:
        video_id = segments[0]
    elif parsed.path.rstrip("/") == "/watch":
        values = query.get("v", [])
        video_id = values[0] if len(values) == 1 else None
    elif len(segments) == 2 and segments[0] in {"embed", "live", "shorts"}:
        video_id = segments[1]
    return video_id is not None and VIDEO_ID_PATTERN.fullmatch(video_id) is not None


def _is_spotify_url(link: str) -> bool:
    hostname = urlsplit(link).hostname
    if not hostname:
        return False
    host = hostname.rstrip(".").casefold()
    return host == "spotify.com" or host.endswith(".spotify.com")


def source_name(info: Mapping[str, object], link: str) -> str:
    extractor = info.get("extractor_key") or info.get("extractor")
    key = extractor.casefold() if isinstance(extractor, str) else ""
    sources = (
        ("youtube", "YouTube"),
        ("soundcloud", "SoundCloud"),
        ("vimeo", "Vimeo"),
        ("twitch", "Twitch"),
        ("instagram", "Instagram"),
        ("tiktok", "TikTok"),
        ("facebook", "Facebook"),
        ("twitter", "X / Twitter"),
        ("reddit", "Reddit"),
        ("imgur", "Imgur"),
        ("dailymotion", "Dailymotion"),
        ("bandcamp", "Bandcamp"),
        ("mixcloud", "Mixcloud"),
        ("bilibili", "Bilibili"),
        ("kick", "Kick"),
        ("rumble", "Rumble"),
        ("streamable", "Streamable"),
        ("bbc", "BBC"),
        ("itv", "ITV"),
        ("ard", "ARD"),
        ("zdf", "ZDF"),
    )
    for prefix, label in sources:
        if key.startswith(prefix):
            return label
    hostname = urlsplit(link).hostname or "Unknown source"
    return hostname.removeprefix("www.")


def _media_title(info: Mapping[str, object]) -> str:
    for field in ("title", "id"):
        value = info.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()[:300]
    return "Untitled media"


def _duration(info: Mapping[str, object]) -> int | float | None:
    value = info.get("duration")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    if value > MAX_DURATION_SECONDS:
        raise MediaLimitError(
            f"Media exceeds the {MAX_DURATION_SECONDS // 60}-minute limit."
        )
    return value


def _validated_media_duration(
    info: Mapping[str, object],
    *,
    require_known: bool,
) -> int | float | None:
    live_status = info.get("live_status")
    if info.get("is_live") is True or live_status in {
        "is_live",
        "is_upcoming",
        "post_live",
    }:
        raise UnsupportedMediaError(
            "Active and upcoming livestreams are not supported."
        )
    duration = _duration(info)
    if require_known and duration is None:
        raise MediaLimitError("Audio conversion requires a known positive duration.")
    return duration


def _has_codec(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value.casefold() != "none"


def media_response(
    info: Mapping[str, object],
    link: str,
    engine: str,
) -> MediaResponse:
    duration = _validated_media_duration(info, require_known=False)

    formats = info.get("formats")
    if isinstance(formats, list):
        format_items: Iterable[object] = formats
    elif formats is None and isinstance(info.get("url"), str):
        format_items = (info,)
    else:
        raise UnsupportedMediaError("No downloadable public formats were found.")
    has_audio = False
    has_video = False
    downloader_params = _primary_options()
    for item in format_items:
        if not isinstance(item, Mapping) or item.get("has_drm") is True:
            continue
        download_info = dict(info)
        download_info.pop("formats", None)
        download_info.update(item)
        if _safe_downloader_type(download_info, downloader_params) is None:
            continue
        has_audio = has_audio or _has_codec(item.get("acodec")) or _has_codec(
            item.get("audio_ext")
        )
        has_video = has_video or _has_codec(item.get("vcodec")) or _has_codec(
            item.get("video_ext")
        )
    outputs: list[OutputMode] = []
    if has_audio:
        outputs.append("audio")
    if has_video:
        outputs.append("video")
    if not outputs:
        raise UnsupportedMediaError("No downloadable public formats were found.")

    return {
        "kind": "media",
        "source": source_name(info, link),
        "title": _media_title(info),
        "duration_seconds": duration,
        "is_live": False,
        "outputs": outputs,
        "engine": engine,
    }


def playlist_response(
    info: Mapping[str, object],
    link: str,
) -> PlaylistResponse:
    raw_entries = info.get("entries")
    if isinstance(raw_entries, str | bytes | Mapping) or not isinstance(
        raw_entries, Iterable
    ):
        raise UnsupportedMediaError("No selectable playlist items were found.")

    entries: list[PlaylistEntry] = []
    truncated = False
    for index, raw_entry in enumerate(raw_entries, 1):
        if index > MAX_PLAYLIST_ITEMS:
            truncated = True
            break
        if not isinstance(raw_entry, Mapping):
            continue
        title = raw_entry.get("title") or raw_entry.get("id")
        if isinstance(title, str) and title.strip():
            entries.append({"index": index, "title": title.strip()[:300]})
    if not entries:
        raise UnsupportedMediaError("No selectable playlist items were found.")
    return {
        "kind": "playlist",
        "source": source_name(info, link),
        "title": _media_title(info),
        "entries": entries,
        "truncated": truncated,
        "engine": "yt-dlp",
    }


def _selected_entry(info: Mapping[str, object]) -> Mapping[str, object]:
    if info.get("_type") not in {"playlist", "multi_video"}:
        return info
    entries = info.get("entries")
    if not isinstance(entries, Iterable) or isinstance(
        entries, str | bytes | Mapping
    ):
        raise UnsupportedMediaError("The selected playlist item is unavailable.")
    for entry in entries:
        if isinstance(entry, Mapping):
            return entry
    raise UnsupportedMediaError("The selected playlist item is unavailable.")


def _primary_options() -> dict[str, object]:
    return {
        "cachedir": False,
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "allowed_extractors": ["default", "-generic"],
        "enable_file_urls": False,
        "external_downloader": {"default": "native"},
        "hls_prefer_native": True,
        "js_runtimes": {"deno": {}},
        "proxy": "",
        "remote_components": [],
        "socket_timeout": SOCKET_TIMEOUT_SECONDS,
        "retries": MAX_RETRIES,
        "fragment_retries": MAX_RETRIES,
        "extractor_retries": MAX_RETRIES,
        "file_access_retries": 1,
        "concurrent_fragment_downloads": 1,
    }


def inspect_with_ytdlp(
    link: str,
    playlist_index: int | None,
) -> Mapping[str, object]:
    options = _primary_options()
    options.update({"noplaylist": False})
    if playlist_index is None:
        options.update(
            {
                "extract_flat": "in_playlist",
                "playlistend": MAX_PLAYLIST_ITEMS + 1,
            }
        )
    else:
        options.update({"extract_flat": False, "playlist_items": str(playlist_index)})
    try:
        with public_network_only(), YtDlp(options) as client:
            result = client.extract_info(link, download=False)
    except YtDlpDownloadError as error:
        raise EngineError(
            "The primary media engine could not inspect this URL."
        ) from error
    if not isinstance(result, Mapping):
        raise EngineError("The primary media engine returned invalid metadata.")
    return _selected_entry(result) if playlist_index is not None else result


def inspect_with_legacy(link: str) -> Mapping[str, object]:
    options: dict[str, object] = {
        "cachedir": False,
        "format": LEGACY_FORMAT,
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "max_filesize": MAX_MEDIA_BYTES,
        "hls_prefer_native": True,
        "noplaylist": True,
        "proxy": "",
        "retries": MAX_RETRIES,
        "socket_timeout": SOCKET_TIMEOUT_SECONDS,
        "youtube_player_js_variant": "actual",
    }
    try:
        with public_network_only(), LegacyYoutubeDL(options) as client:
            result = client.extract_info(link, download=False)
    except LegacyDownloadError as error:
        raise EngineError(
            "The fallback media engine could not inspect this URL."
        ) from error
    if not isinstance(result, Mapping):
        raise EngineError("The fallback media engine returned invalid metadata.")
    if not _is_direct_legacy_format(result):
        raise EngineError("The fallback engine found no direct format 18.")
    selected = dict(result)
    selected.pop("formats", None)
    selected.pop("requested_formats", None)
    return selected


def inspect_media(link: str, playlist_index: int | None = None) -> InspectResponse:
    validated_link = validate_public_url(link)
    if _is_spotify_url(validated_link):
        raise UnsupportedMediaError(
            "Spotify protected tracks are not downloadable by this service."
        )
    try:
        info = inspect_with_ytdlp(validated_link, playlist_index)
    except EngineError:
        if playlist_index is not None or not is_youtube_url(validated_link):
            raise
        legacy_info = inspect_with_legacy(validated_link)
        return media_response(legacy_info, validated_link, "youtube-dl fallback")

    if playlist_index is None and info.get("_type") in {"playlist", "multi_video"}:
        return playlist_response(info, validated_link)
    return media_response(info, validated_link, "yt-dlp")


def _validate_job_id(job_id: str) -> str:
    if JOB_ID_PATTERN.fullmatch(job_id) is None:
        raise EngineError("The media job identifier is invalid.")
    return job_id


def _is_service_artifact(target: Path) -> bool:
    return target.name.startswith(("yt-download-", "yt-work-")) and (
        target.is_file() or target.is_symlink()
    )


def generated_artifacts() -> list[Path]:
    return [
        target for target in DOWNLOAD_PATH.iterdir() if _is_service_artifact(target)
    ]


def _job_artifacts(job_prefix: str) -> list[Path]:
    return [
        target
        for target in DOWNLOAD_PATH.glob(f"{job_prefix}.*")
        if target.is_file()
    ]


def cleanup_job(job_prefix: str) -> None:
    for target in DOWNLOAD_PATH.glob(f"{job_prefix}.*"):
        try:
            if target.is_file() or target.is_symlink():
                target.unlink(missing_ok=True)
        except OSError as error:
            logging.error("Failed to remove job artifact %s: %s", target, error)


def cleanup_job_id(job_id: str, *, include_ready: bool) -> None:
    validated_job_id = _validate_job_id(job_id)
    for target in DOWNLOAD_PATH.iterdir():
        work_match = WORK_ARTIFACT_PATTERN.fullmatch(target.name)
        ready_match = DOWNLOAD_ARTIFACT_PATTERN.fullmatch(target.name)
        belongs_to_job = bool(
            work_match and work_match.group("job_id") == validated_job_id
        ) or bool(
            include_ready
            and ready_match
            and ready_match.group("job_id") == validated_job_id
        )
        if not belongs_to_job:
            continue
        try:
            if target.is_file() or target.is_symlink():
                target.unlink(missing_ok=True)
        except OSError as error:
            logging.error("Failed to remove job artifact %s: %s", target, error)


def make_progress_hook(
    job_prefix: str,
) -> Callable[[Mapping[str, object]], None]:
    def check_size(progress: Mapping[str, object]) -> None:
        total = 0
        for target in _job_artifacts(job_prefix):
            try:
                total += target.stat().st_size
            except FileNotFoundError:
                continue
        downloaded_bytes = progress.get("downloaded_bytes")
        if (
            total == 0
            and type(downloaded_bytes) is int
            and downloaded_bytes > 0
        ):
            total = downloaded_bytes
        if total > MAX_MEDIA_BYTES:
            raise MediaLimitError("The generated media exceeds the size limit.")

    return check_size


def make_download_match_filter(mode: OutputMode) -> DownloadMatchFilter:
    def validate(
        info: Mapping[str, object],
        *,
        incomplete: bool = False,
    ) -> str | None:
        if not incomplete:
            response = media_response(info, "", "yt-dlp")
            if mode not in response["outputs"]:
                raise UnsupportedMediaError(
                    f"The selected {mode} output is no longer available."
                )
            if mode == "audio" and response["duration_seconds"] is None:
                raise MediaLimitError(
                    "Audio conversion requires a known positive duration."
                )
        return None

    return validate


def validate_output_file(path: Path) -> None:
    if (
        not path.is_file()
        or path.is_symlink()
        or path.resolve().parent != DOWNLOAD_PATH
    ):
        raise EngineError("The media engine did not create a safe output file.")
    size = path.stat().st_size
    if size <= 0:
        raise EngineError("The media engine created an empty output file.")
    if size > MAX_MEDIA_BYTES:
        raise MediaLimitError("The generated media exceeds the size limit.")


def _finish_job(job_prefix: str, mode: OutputMode) -> DownloadedFile:
    candidates = [
        target
        for target in _job_artifacts(job_prefix)
        if target.stem == job_prefix
        if target.suffix.removeprefix(".").casefold() in SAFE_MEDIA_EXTENSIONS
        and not target.name.endswith(".part")
        and not target.is_symlink()
        and target.resolve().parent == DOWNLOAD_PATH
    ]
    if mode == "audio":
        candidates = [
            target for target in candidates if target.suffix.casefold() == ".mp3"
        ]
    if not candidates:
        raise MediaLimitError("The media engine did not create a complete output file.")
    if len(candidates) != 1:
        raise EngineError("The media engine did not create exactly one output file.")
    output = candidates[0]
    validate_output_file(output)
    for artifact in _job_artifacts(job_prefix):
        if artifact != output:
            artifact.unlink(missing_ok=True)
    extension = output.suffix.removeprefix(".").casefold()
    ready_prefix = f"yt-download-{job_prefix.removeprefix('yt-work-')}"
    ready = DOWNLOAD_PATH / f"{ready_prefix}.{extension}"
    if GENERATED_FILE_PATTERN.fullmatch(ready.name) is None:
        raise EngineError("The media engine produced an invalid output name.")
    size = output.stat().st_size
    try:
        os.link(output, ready)
    except FileExistsError as error:
        raise EngineError("The completed media file already exists.") from error
    except OSError as error:
        raise EngineError("The completed media file could not be published.") from error
    try:
        output.unlink()
    except OSError as error:
        logging.error("Failed to remove published work artifact %s: %s", output, error)
    return {
        "name": ready.name,
        "extension": extension,
        "mode": mode,
        "size_bytes": size,
    }


def _job_prefix(metadata: Mapping[str, object], job_id: str) -> str:
    title = metadata.get("title")
    safe_title = sanitize_title(title if isinstance(title, str) else "media-file")
    return f"yt-work-{safe_title}-{_validate_job_id(job_id)}"


def download_with_ytdlp(
    link: str,
    mode: OutputMode,
    playlist_index: int | None,
    metadata: Mapping[str, object],
    job_id: str,
) -> DownloadedFile:
    job_prefix = _job_prefix(metadata, job_id)
    options = _primary_options()
    options.update(
        {
            "format": AUDIO_FORMAT if mode == "audio" else VIDEO_FORMAT,
            "max_filesize": MAX_MEDIA_BYTES,
            "max_downloads": 1,
            "match_filter": make_download_match_filter(mode),
            "noplaylist": playlist_index is None,
            "outtmpl": str(DOWNLOAD_PATH / f"{job_prefix}.%(ext)s"),
            "overwrites": False,
            "progress_hooks": [make_progress_hook(job_prefix)],
            "updatetime": False,
        }
    )
    if playlist_index is not None:
        options["playlist_items"] = str(playlist_index)
    if mode == "audio":
        options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "0",
            }
        ]
    else:
        options["merge_output_format"] = "mp4"
    try:
        with (
            public_network_only(),
            YtDlp(options) as client,
            suppress(MaxDownloadsReached),
        ):
            client.download([link])
        try:
            return _finish_job(job_prefix, mode)
        except EngineError as error:
            if not _job_artifacts(job_prefix):
                raise MediaLimitError(
                    "No downloadable format fits the media size limit."
                ) from error
            raise
    except YtDlpDownloadError as error:
        cleanup_job(job_prefix)
        if NO_FORMAT_ERROR in str(error):
            raise MediaLimitError(
                "No downloadable format fits the media size limit."
            ) from error
        raise EngineError(
            "The primary media engine could not download this item."
        ) from error
    except Exception:
        cleanup_job(job_prefix)
        raise


def download_with_legacy(
    link: str,
    mode: OutputMode,
    metadata: Mapping[str, object],
    job_id: str,
) -> DownloadedFile:
    job_prefix = _job_prefix(metadata, job_id)
    options: dict[str, object] = {
        "cachedir": False,
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "format": LEGACY_FORMAT,
        "hls_prefer_native": True,
        "max_filesize": MAX_MEDIA_BYTES,
        "match_filter": make_download_match_filter(mode),
        "noplaylist": True,
        "outtmpl": str(DOWNLOAD_PATH / f"{job_prefix}.%(ext)s"),
        "progress_hooks": [make_progress_hook(job_prefix)],
        "proxy": "",
        "retries": MAX_RETRIES,
        "socket_timeout": SOCKET_TIMEOUT_SECONDS,
        "updatetime": False,
        "youtube_player_js_variant": "actual",
    }
    if mode == "audio":
        options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "0",
            }
        ]
    try:
        with public_network_only(), LegacyYoutubeDL(options) as client:
            client.download([link])
        try:
            return _finish_job(job_prefix, mode)
        except EngineError as error:
            if not _job_artifacts(job_prefix):
                raise MediaLimitError(
                    "No fallback format fits the media size limit."
                ) from error
            raise
    except LegacyDownloadError as error:
        cleanup_job(job_prefix)
        raise EngineError(
            "The fallback media engine could not download this item."
        ) from error
    except Exception:
        cleanup_job(job_prefix)
        raise


def download_media(
    link: str,
    mode: OutputMode,
    playlist_index: int | None,
    metadata: Mapping[str, object],
    job_id: str,
) -> tuple[DownloadedFile, str]:
    try:
        return (
            download_with_ytdlp(link, mode, playlist_index, metadata, job_id),
            "yt-dlp",
        )
    except EngineError:
        if playlist_index is not None or not is_youtube_url(link):
            raise
        return (
            download_with_legacy(link, mode, metadata, job_id),
            "youtube-dl fallback",
        )


def _try_lock_file(handle: BinaryIO) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, ValueError):
        return False
    return True


def _unlock_file(handle: BinaryIO) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, ValueError):
        pass


def _open_locked(target: Path) -> BinaryIO | None:
    try:
        handle = target.open("r+b")
    except OSError:
        return None
    if not _try_lock_file(handle):
        handle.close()
        return None
    return handle


def _close_locked(handle: BinaryIO) -> None:
    _unlock_file(handle)
    handle.close()


def _acquire_operation_lock() -> BinaryIO | None:
    lock_path = DOWNLOAD_PATH / ".yt-operation.lock"
    try:
        handle = lock_path.open("a+b")
        if handle.tell() == 0:
            handle.write(b"1")
            handle.flush()
    except OSError:
        return None
    if not _try_lock_file(handle):
        handle.close()
        return None
    return handle


def _claim_original(claim: Path) -> Path | None:
    match = CLAIM_PATTERN.fullmatch(claim.name)
    if match is None or claim.parent != DOWNLOAD_PATH:
        return None
    original = DOWNLOAD_PATH / match.group("ready")
    return original if original.parent == DOWNLOAD_PATH else None


def _same_file(first: Path, second: Path) -> bool:
    try:
        return os.path.samestat(first.stat(), second.stat())
    except OSError:
        return False


def _restore_claim(claim: Path, original: Path) -> bool:
    try:
        os.link(claim, original)
    except FileExistsError:
        if _same_file(claim, original):
            claim.unlink(missing_ok=True)
            return True
        return False
    claim.unlink(missing_ok=True)
    return True


def _artifact_expired(target: Path, now: float) -> bool:
    stat = target.stat()
    ttl = (
        SMALL_FILE_TTL_SECONDS
        if stat.st_size < SMALL_FILE_MAX_BYTES
        else LARGE_FILE_TTL_SECONDS
    )
    return stat.st_mtime <= now - ttl


def _job_lock_path(job_id: str) -> Path:
    return DOWNLOAD_PATH / f".yt-job-{_validate_job_id(job_id)}.lock"


def _job_is_active(job_id: str) -> bool:
    lock_path = _job_lock_path(job_id)
    if not lock_path.is_file():
        return False
    handle = _open_locked(lock_path)
    if handle is None:
        return True
    _close_locked(handle)
    return False


def _unlink_locked(target: Path, handle: BinaryIO) -> bool:
    try:
        target.unlink(missing_ok=True)
    except PermissionError:
        _close_locked(handle)
        try:
            target.unlink(missing_ok=True)
        except OSError as error:
            logging.error("Failed to delete file %s: %s", target, error)
            return False
        return True
    except OSError as error:
        logging.error("Failed to delete file %s: %s", target, error)
        return False
    finally:
        if not handle.closed:
            _close_locked(handle)
    return True


def _cleanup_downloads_locked(now: float) -> None:
    control_job_ids = {
        match.group("job_id")
        for target in DOWNLOAD_PATH.iterdir()
        if (match := JOB_CONTROL_PATTERN.fullmatch(target.name)) is not None
    }
    for job_id in control_job_ids:
        if _job_is_active(job_id):
            continue
        cleanup_job_id(job_id, include_ready=True)
        for suffix in ("request", "result", "lock"):
            control_path = DOWNLOAD_PATH / f".yt-job-{job_id}.{suffix}"
            try:
                control_path.unlink(missing_ok=True)
            except OSError as error:
                logging.error(
                    "Failed to remove orphan job control file %s: %s",
                    control_path,
                    error,
                )
    for target in generated_artifacts():
        if target in ACTIVE_DOWNLOAD_CLAIMS:
            continue
        try:
            if target.is_symlink():
                target.unlink(missing_ok=True)
                continue
            ready = GENERATED_FILE_PATTERN.fullmatch(target.name)
            claim = (
                _claim_original(target) if target.name.endswith(".sending") else None
            )
            work = WORK_ARTIFACT_PATTERN.fullmatch(target.name)
            if work and _job_is_active(work.group("job_id")):
                continue
            handle = _open_locked(target)
            if handle is None:
                continue
            if ready:
                if _artifact_expired(target, now):
                    _unlink_locked(target, handle)
                else:
                    _close_locked(handle)
                continue
            if target.name.endswith(".sending"):
                if _artifact_expired(target, now) or claim is None:
                    _unlink_locked(target, handle)
                else:
                    if os.name == "nt":
                        _close_locked(handle)
                    try:
                        _restore_claim(target, claim)
                    except OSError as error:
                        logging.error(
                            "Failed to recover download claim %s: %s", target, error
                        )
                    finally:
                        if not handle.closed:
                            _close_locked(handle)
                continue
            _unlink_locked(target, handle)
        except FileNotFoundError:
            continue
        except OSError as error:
            logging.error("Failed to clean download artifact %s: %s", target, error)


def cleanup_stale_downloads() -> None:
    with DOWNLOAD_FILES_LOCK:
        _cleanup_downloads_locked(time())


def _stored_bytes() -> int:
    total = 0
    for target in generated_artifacts():
        try:
            if not target.is_symlink():
                total += target.stat().st_size
        except FileNotFoundError:
            continue
    return total


def ensure_storage_capacity() -> None:
    reserve_limit = MAX_STORED_BYTES - (2 * MAX_MEDIA_BYTES)
    with DOWNLOAD_FILES_LOCK:
        _cleanup_downloads_locked(time())
        if _stored_bytes() <= reserve_limit:
            return
        protected_ready: set[Path] = set()
        for artifact in generated_artifacts():
            original = _claim_original(artifact)
            if (
                original is not None
                and original.is_file()
                and not _same_file(artifact, original)
            ):
                protected_ready.add(original)
        candidates: list[tuple[float, str, Path]] = []
        for target in generated_artifacts():
            if (
                GENERATED_FILE_PATTERN.fullmatch(target.name) is None
                or target in protected_ready
            ):
                continue
            try:
                candidates.append((target.stat().st_mtime, target.name, target))
            except FileNotFoundError:
                continue
        for _mtime, _name, target in sorted(candidates):
            handle = _open_locked(target)
            if handle is not None:
                _unlink_locked(target, handle)
            if _stored_bytes() <= reserve_limit:
                return
    raise StorageLimitError("Download storage is busy. Try again later.")


if not JOB_WORKER_MODE:
    startup_operation_lock = _acquire_operation_lock()
    if startup_operation_lock is not None:
        try:
            cleanup_stale_downloads()
        finally:
            _close_locked(startup_operation_lock)


def _execute_job(payload: Mapping[str, object], job_id: str) -> dict[str, object]:
    if set(payload) != {"operation", "url", "playlist_index", "mode"}:
        raise InputError("The media job request is invalid.")
    operation = payload.get("operation")
    link_value = payload.get("url")
    link = validate_public_url(link_value if isinstance(link_value, str) else "")
    raw_index = payload.get("playlist_index")
    if raw_index is None:
        playlist_index = None
    elif type(raw_index) is int and 1 <= raw_index <= MAX_PLAYLIST_ITEMS:
        playlist_index = raw_index
    else:
        raise InputError("The media job playlist index is invalid.")
    raw_mode = payload.get("mode")
    if operation == "inspect" and raw_mode is None:
        return dict(inspect_media(link, playlist_index))
    if operation != "download" or raw_mode not in {"audio", "video"}:
        raise InputError("The media job operation is invalid.")
    mode = cast(OutputMode, raw_mode)
    metadata = inspect_media(link, playlist_index)
    if metadata["kind"] != "media":
        raise UnsupportedMediaError("Choose one playlist item before downloading.")
    if mode not in metadata["outputs"]:
        raise UnsupportedMediaError(
            f"{mode.capitalize()} is not available for this media."
        )
    if mode == "audio" and metadata["duration_seconds"] is None:
        raise MediaLimitError("Audio conversion requires a known positive duration.")
    output, engine = download_media(link, mode, playlist_index, metadata, job_id)
    return {
        "status": "ready",
        "source": metadata["source"],
        "title": metadata["title"],
        "engine": engine,
        "file": output,
    }


def _job_control_path(job_id: str, suffix: str) -> Path:
    validated_job_id = _validate_job_id(job_id)
    if suffix not in {"request", "result", "lock"}:
        raise EngineError("The media job control path is invalid.")
    return DOWNLOAD_PATH / f".yt-job-{validated_job_id}.{suffix}"


def _write_job_result(result_path: Path, payload: Mapping[str, object]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > JOB_RESULT_MAX_BYTES:
        encoded = json.dumps(
            {
                "ok": False,
                "error": "EngineError",
                "message": "The media engine returned too much result data.",
            },
            separators=(",", ":"),
        ).encode()
    with result_path.open("wb") as result_file:
        result_file.write(encoded)


def _job_worker_main() -> int:
    job_id = os.environ.get("YTDL_JOB_ID", "")
    try:
        request_path = _job_control_path(job_id, "request")
        result_path = _job_control_path(job_id, "result")
    except EngineError:
        return 1
    try:
        with request_path.open("rb") as request_file:
            encoded = request_file.read(MAX_REQUEST_BYTES + 1)
        if not encoded or len(encoded) > MAX_REQUEST_BYTES:
            raise InputError("The media job request is invalid.")
        decoded = json.loads(encoded)
        if not isinstance(decoded, dict):
            raise InputError("The media job request is invalid.")
        value = _execute_job(decoded, job_id)
        result: dict[str, object] = {"ok": True, "value": value}
    except (
        InputError,
        UnsupportedMediaError,
        MediaLimitError,
        StorageLimitError,
        EngineError,
    ) as error:
        result = {
            "ok": False,
            "error": type(error).__name__,
            "message": str(error)[:2000],
        }
    except Exception:  # noqa: BLE001
        result = {
            "ok": False,
            "error": "EngineError",
            "message": "The media engine failed unexpectedly.",
        }
    try:
        _write_job_result(result_path, result)
    except OSError:
        return 1
    return 0


def _process_group_rss_bytes(process_group_id: int) -> int | None:
    proc_path = Path("/proc")
    if os.name != "posix" or not proc_path.is_dir():
        return None
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError):
        return None
    total = 0
    try:
        processes = proc_path.iterdir()
    except OSError:
        return None
    for process_path in processes:
        if not process_path.name.isdecimal():
            continue
        try:
            stat = (process_path / "stat").read_text(encoding="utf-8")
            after_name = stat[stat.rindex(")") + 2 :].split()
            if len(after_name) < 3 or int(after_name[2]) != process_group_id:
                continue
            statm = (process_path / "statm").read_text(encoding="ascii").split()
            total += int(statm[1]) * page_size
        except (FileNotFoundError, OSError, ValueError, IndexError):
            continue
    return total


def _process_group_exists(process_group_id: int) -> bool:
    if os.name != "posix":
        return False
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_process_group_exit(process_group_id: int) -> bool:
    deadline = monotonic() + JOB_REAP_SECONDS
    while _process_group_exists(process_group_id):
        if monotonic() >= deadline:
            return False
        sleep(JOB_POLL_SECONDS)
    return True


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    tree_uncertain = False
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as error:
            raise JobReapError(
                "The media job process group could not be stopped."
            ) from error
    else:
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=JOB_REAP_SECONDS,
            )
            tree_uncertain = result.returncode != 0
        except (OSError, subprocess.TimeoutExpired):
            tree_uncertain = True
            with suppress(OSError):
                process.kill()
        if tree_uncertain and process.poll() is None:
            with suppress(OSError):
                process.kill()
    try:
        process.wait(timeout=JOB_REAP_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise JobReapError("The media job could not be stopped safely.") from error
    if os.name != "posix" and tree_uncertain:
        raise JobReapError("The media job process tree could not be confirmed stopped.")
    if os.name == "posix" and not _wait_for_process_group_exit(process.pid):
        raise JobReapError("The media job process group could not be reaped.")


def _wait_for_job(process: subprocess.Popen[bytes], deadline: float) -> None:
    first_memory_sample = True
    while process.poll() is None:
        if monotonic() >= deadline:
            _terminate_process_tree(process)
            raise OperationTimeoutError("The media operation exceeded its time limit.")
        rss_bytes = _process_group_rss_bytes(process.pid)
        if (
            sys.platform.startswith("linux")
            and (rss_bytes is None or rss_bytes <= 0)
        ):
            if process.poll() is not None:
                continue
            if first_memory_sample:
                first_memory_sample = False
                sleep(JOB_POLL_SECONDS)
                continue
            _terminate_process_tree(process)
            raise EngineError("The media operation memory monitor failed.")
        first_memory_sample = False
        if rss_bytes is not None and rss_bytes > JOB_MEMORY_BYTES:
            _terminate_process_tree(process)
            raise EngineError("The media operation exceeded its memory limit.")
        try:
            stored_bytes = _stored_bytes()
        except OSError as error:
            _terminate_process_tree(process)
            raise EngineError("The media operation storage monitor failed.") from error
        if stored_bytes > MAX_STORED_BYTES:
            _terminate_process_tree(process)
            raise StorageLimitError("The media operation exceeded its storage limit.")
        sleep(JOB_POLL_SECONDS)
    if os.name == "posix" and _process_group_exists(process.pid):
        _terminate_process_tree(process)
        raise EngineError("The media operation left child processes running.")
    if process.returncode != 0:
        raise EngineError("The media job process failed.")


def _valid_inspection_result(value: Mapping[str, object]) -> bool:
    kind = value.get("kind")
    common = value.get("source"), value.get("title"), value.get("engine")
    if any(not isinstance(item, str) or not item for item in common):
        return False
    if kind == "media":
        if set(value) != {
            "kind",
            "source",
            "title",
            "duration_seconds",
            "is_live",
            "outputs",
            "engine",
        } or value.get("is_live") is not False:
            return False
        duration = value.get("duration_seconds")
        if duration is not None and (
            isinstance(duration, bool)
            or not isinstance(duration, int | float)
            or not math.isfinite(duration)
            or duration <= 0
            or duration > MAX_DURATION_SECONDS
        ):
            return False
        outputs = value.get("outputs")
        return outputs in (["audio"], ["video"], ["audio", "video"])
    if kind != "playlist" or set(value) != {
        "kind",
        "source",
        "title",
        "entries",
        "truncated",
        "engine",
    }:
        return False
    entries = value.get("entries")
    if (
        not isinstance(entries, list)
        or not entries
        or len(entries) > MAX_PLAYLIST_ITEMS
    ):
        return False
    if type(value.get("truncated")) is not bool:
        return False
    return all(
        isinstance(entry, dict)
        and set(entry) == {"index", "title"}
        and type(entry.get("index")) is int
        and 1 <= cast(int, entry["index"]) <= MAX_PLAYLIST_ITEMS
        and isinstance(entry.get("title"), str)
        and bool(entry["title"])
        for entry in entries
    )


def _valid_download_result(value: Mapping[str, object], job_id: str) -> bool:
    if set(value) != {"status", "source", "title", "engine", "file"}:
        return False
    if value.get("status") != "ready" or any(
        not isinstance(value.get(key), str) or not value.get(key)
        for key in ("source", "title", "engine")
    ):
        return False
    file_value = value.get("file")
    if not isinstance(file_value, dict) or set(file_value) != {
        "name",
        "extension",
        "mode",
        "size_bytes",
    }:
        return False
    name = file_value.get("name")
    extension = file_value.get("extension")
    mode = file_value.get("mode")
    size = file_value.get("size_bytes")
    if (
        not isinstance(name, str)
        or not isinstance(extension, str)
        or extension not in SAFE_MEDIA_EXTENSIONS
        or not isinstance(mode, str)
        or mode not in {"audio", "video"}
        or type(size) is not int
        or not 0 < size <= MAX_MEDIA_BYTES
    ):
        return False
    name_match = DOWNLOAD_ARTIFACT_PATTERN.fullmatch(name)
    output = resolve_download_path(name)
    if (
        name_match is None
        or name_match.group("job_id") != job_id
        or output is None
        or output.suffix.casefold() != f".{extension}"
    ):
        return False
    try:
        return (
            output.is_file()
            and not output.is_symlink()
            and output.stat().st_size == size
        )
    except OSError:
        return False


def _decode_job_result(
    result_path: Path,
    operation: str,
    job_id: str,
) -> dict[str, object]:
    try:
        with result_path.open("rb") as result_file:
            encoded = result_file.read(JOB_RESULT_MAX_BYTES + 1)
    except OSError as error:
        raise EngineError("The media job returned no result.") from error
    if not encoded or len(encoded) > JOB_RESULT_MAX_BYTES:
        raise EngineError("The media job returned an invalid result.")
    try:
        decoded = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EngineError("The media job returned an invalid result.") from error
    if not isinstance(decoded, dict) or type(decoded.get("ok")) is not bool:
        raise EngineError("The media job returned an invalid result.")
    if decoded["ok"] is False:
        if set(decoded) != {"ok", "error", "message"}:
            raise EngineError("The media job returned an invalid error.")
        error_name = decoded.get("error")
        message = decoded.get("message")
        error_types: dict[str, type[Exception]] = {
            "InputError": InputError,
            "UnsupportedMediaError": UnsupportedMediaError,
            "MediaLimitError": MediaLimitError,
            "StorageLimitError": StorageLimitError,
            "EngineError": EngineError,
        }
        if (
            not isinstance(error_name, str)
            or error_name not in error_types
            or not isinstance(message, str)
            or not message
            or len(message) > 2000
        ):
            raise EngineError("The media job returned an invalid error.")
        raise error_types[error_name](message)
    if set(decoded) != {"ok", "value"} or not isinstance(decoded.get("value"), dict):
        raise EngineError("The media job returned an invalid result.")
    value = cast(dict[str, object], decoded["value"])
    if operation == "inspect":
        if not _valid_inspection_result(value):
            raise EngineError("The media job returned invalid inspection data.")
    elif operation == "download":
        if not _valid_download_result(value, job_id):
            raise EngineError("The media job returned an invalid download file.")
    else:
        raise EngineError("The media job operation is invalid.")
    return value


def run_job(
    operation: Literal["inspect", "download"],
    link: str,
    playlist_index: int | None,
    mode: OutputMode | None,
    operation_lock: BinaryIO,
) -> dict[str, object]:
    deadline = monotonic() + JOB_TIMEOUT_SECONDS
    job_id = uuid4().hex
    request_path = _job_control_path(job_id, "request")
    result_path = _job_control_path(job_id, "result")
    lock_path = _job_control_path(job_id, "lock")
    request_payload = {
        "operation": operation,
        "url": link,
        "playlist_index": playlist_index,
        "mode": mode,
    }
    encoded_request = json.dumps(request_payload, separators=(",", ":")).encode()
    if len(encoded_request) > MAX_REQUEST_BYTES:
        raise InputError("The media job request is too large.")
    lock_handle: BinaryIO | None = None
    process: subprocess.Popen[bytes] | None = None
    liveness_read: int | None = None
    liveness_write: int | None = None
    succeeded = False
    caught_reap_error: JobReapError | None = None
    try:
        lock_handle = lock_path.open("xb+")
        lock_handle.write(b"1")
        lock_handle.flush()
        if not _try_lock_file(lock_handle):
            raise EngineError("The media job lock could not be acquired.")
        with request_path.open("xb") as request_file:
            request_file.write(encoded_request)
        result_path.touch(exist_ok=False)
        environment = os.environ.copy()
        environment.update(
            {
                "DOWNLOAD_DIR": str(DOWNLOAD_PATH),
                "YTDL_JOB_ID": job_id,
            }
        )
        command = [sys.executable, str(Path(__file__).resolve()), "--job-worker"]
        if os.name == "posix":
            liveness_read, liveness_write = os.pipe()
            environment["YTDL_JOB_LIVENESS_FD"] = str(liveness_read)
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                start_new_session=True,
                pass_fds=(liveness_read, lock_handle.fileno())
                + (operation_lock.fileno(),),
            )
            os.close(liveness_read)
            liveness_read = None
        else:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        _wait_for_job(process, deadline)
        value = _decode_job_result(result_path, operation, job_id)
        cleanup_job_id(job_id, include_ready=False)
        succeeded = True
        return value
    except (InputError, UnsupportedMediaError, MediaLimitError, StorageLimitError):
        raise
    except JobReapError as error:
        caught_reap_error = error
    except (OperationTimeoutError, EngineError):
        raise
    except OSError as error:
        raise EngineError("The media job could not be started.") from error
    finally:
        reap_error = caught_reap_error
        if (
            reap_error is None
            and process is not None
            and process.poll() is None
        ):
            try:
                _terminate_process_tree(process)
            except JobReapError as error:
                reap_error = error
        if liveness_read is not None:
            with suppress(OSError):
                os.close(liveness_read)
        if liveness_write is not None:
            with suppress(OSError):
                os.close(liveness_write)
        if reap_error is not None and process is not None and (
            os.name == "posix" or process.poll() is None
        ):
            try:
                _terminate_process_tree(process)
            except JobReapError as error:
                reap_error = error
            else:
                reap_error = None
        if reap_error is not None:
            if lock_handle is not None:
                ORPHANED_JOB_LOCKS.append(lock_handle)
                lock_handle = None
            if process is not None:
                ORPHANED_JOB_PROCESSES.append(process)
        else:
            if not succeeded:
                cleanup_job_id(job_id, include_ready=True)
            if lock_handle is not None:
                _close_locked(lock_handle)
            for control_path in (request_path, result_path, lock_path):
                try:
                    control_path.unlink(missing_ok=True)
                except OSError as error:
                    logging.error(
                        "Failed to remove job control file %s: %s",
                        control_path,
                        error,
                    )
        if reap_error is not None:
            raise reap_error
    if caught_reap_error is not None:
        raise EngineError(str(caught_reap_error)) from caught_reap_error
    raise EngineError("The media job ended without a result.")


def resolve_download_path(filename: str) -> Path | None:
    if (
        not filename
        or Path(filename).name != filename
        or GENERATED_FILE_PATTERN.fullmatch(filename) is None
    ):
        return None
    target = DOWNLOAD_PATH / filename
    resolved = target.resolve()
    return resolved if resolved.parent == DOWNLOAD_PATH else None


def request_origin_is_allowed() -> bool:
    origin = request.headers.get("Origin", "").rstrip("/")
    return bool(origin) and (
        origin == request.host_url.rstrip("/") or origin in ALLOWED_ORIGINS
    )


def request_client_id() -> str:
    if TRUST_X_REAL_IP:
        real_ip = request.headers.get("X-Real-IP", "").strip()
        normalized = _rate_limit_key(real_ip)
        if normalized != "unknown":
            return normalized
    return _rate_limit_key(request.remote_addr or "")


def _rate_limit_key(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return "unknown"
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


def operation_rate_limit_exceeded(client_id: str) -> bool:
    now = monotonic()
    cutoff = now - RATE_LIMIT_WINDOW_SECONDS
    with DOWNLOAD_ATTEMPTS_LOCK:
        while GLOBAL_OPERATION_ATTEMPTS and GLOBAL_OPERATION_ATTEMPTS[0] <= cutoff:
            GLOBAL_OPERATION_ATTEMPTS.popleft()
        attempts = DOWNLOAD_ATTEMPTS.pop(client_id, deque())
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        limited = (
            len(attempts) >= RATE_LIMIT_REQUESTS
            or len(GLOBAL_OPERATION_ATTEMPTS) >= GLOBAL_RATE_LIMIT_REQUESTS
        )
        if not limited:
            attempts.append(now)
            GLOBAL_OPERATION_ATTEMPTS.append(now)
        if attempts:
            DOWNLOAD_ATTEMPTS[client_id] = attempts
        while len(DOWNLOAD_ATTEMPTS) > RATE_LIMIT_MAX_CLIENTS:
            DOWNLOAD_ATTEMPTS.popitem(last=False)
    return limited


def _request_payload(*, download: bool) -> tuple[str, int | None, OutputMode | None]:
    if not request.is_json:
        raise InputError("A JSON request body is required.")
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise InputError("A JSON object is required.")
    allowed_keys = {"url", "playlist_index"}
    if download:
        allowed_keys.add("mode")
    if set(payload) - allowed_keys:
        raise InputError("The request contains unsupported fields.")
    raw_url = payload.get("url")
    if not isinstance(raw_url, str):
        raise InputError("A public media URL is required.")
    link = validate_public_url(raw_url)

    raw_index = payload.get("playlist_index")
    if raw_index is None:
        playlist_index = None
    elif type(raw_index) is int and 1 <= raw_index <= MAX_PLAYLIST_ITEMS:
        playlist_index = raw_index
    else:
        raise InputError("playlist_index must select one of the first 50 items.")

    mode: OutputMode | None = None
    if download:
        raw_mode = payload.get("mode")
        if not isinstance(raw_mode, str) or raw_mode not in {"audio", "video"}:
            raise InputError("mode must be audio or video.")
        mode = cast(OutputMode, raw_mode)
    return link, playlist_index, mode


def _error_response(error: Exception) -> tuple[Response, int]:
    if isinstance(error, InputError):
        status = 400
    elif isinstance(error, UnsupportedMediaError):
        status = 422
    elif isinstance(error, MediaLimitError):
        status = 413
    elif isinstance(error, StorageLimitError):
        status = 507
    elif isinstance(error, OperationTimeoutError):
        status = 504
    else:
        status = 502
    return jsonify({"error": str(error)}), status


@app.after_request
def add_response_headers(response: Response) -> Response:
    origin = request.headers.get("Origin", "").rstrip("/")
    if origin and (
        origin == request.host_url.rstrip("/") or origin in ALLOWED_ORIGINS
    ):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers.add("Vary", "Origin")
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self' https://majkey25-ytdl.alwaysdata.net; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )
    response.headers["Strict-Transport-Security"] = "max-age=31536000"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = (
        "camera=(), geolocation=(), microphone=(), payment=()"
    )
    if request.path in {"/inspect", "/download", "/delete"} or request.path.startswith(
        "/downloads/"
    ):
        response.headers["Cache-Control"] = "private, no-store, max-age=0"
    return response


@app.route("/")
def index() -> str:
    if DOWNLOAD_SLOT.acquire(blocking=False):
        operation_lock = _acquire_operation_lock()
        try:
            if operation_lock is not None:
                try:
                    cleanup_stale_downloads()
                except Exception:  # noqa: BLE001
                    logging.exception("Idle cleanup failed")
        finally:
            if operation_lock is not None:
                _close_locked(operation_lock)
            DOWNLOAD_SLOT.release()
    return render_template("index.html")


@app.route("/service-worker.js")
def service_worker() -> Response:
    return send_from_directory(str(STATIC_PATH), "service-worker.js")


@app.route("/inspect", methods=["POST"])
def inspect() -> Response | tuple[Response, int]:
    if not request_origin_is_allowed():
        return jsonify({"error": "Origin is not allowed."}), 403
    try:
        link, playlist_index, _mode = _request_payload(download=False)
    except InputError as error:
        return _error_response(error)
    if operation_rate_limit_exceeded(request_client_id()):
        return jsonify({"error": "Request limit reached. Try again later."}), 429
    if not DOWNLOAD_SLOT.acquire(blocking=False):
        return jsonify({"error": "Another operation is running. Try again soon."}), 429
    operation_lock = _acquire_operation_lock()
    if operation_lock is None:
        DOWNLOAD_SLOT.release()
        return jsonify({"error": "Another operation is running. Try again soon."}), 429
    release_slot = True
    try:
        cleanup_stale_downloads()
        return jsonify(run_job("inspect", link, playlist_index, None, operation_lock))
    except JobReapError as error:
        release_slot = False
        return _error_response(error)
    except (
        InputError,
        UnsupportedMediaError,
        MediaLimitError,
        StorageLimitError,
        OperationTimeoutError,
        EngineError,
    ) as error:
        return _error_response(error)
    except Exception:  # noqa: BLE001
        logging.exception("Unexpected inspection error")
        return jsonify({"error": "Unexpected server error. Please try again."}), 500
    finally:
        if release_slot:
            _close_locked(operation_lock)
            DOWNLOAD_SLOT.release()
        else:
            ORPHANED_OPERATION_LOCKS.append(operation_lock)


@app.route("/download", methods=["POST"])
def download() -> Response | tuple[Response, int]:
    if not request_origin_is_allowed():
        return jsonify({"error": "Origin is not allowed."}), 403
    try:
        link, playlist_index, mode = _request_payload(download=True)
    except InputError as error:
        return _error_response(error)
    assert mode is not None
    if operation_rate_limit_exceeded(request_client_id()):
        return jsonify({"error": "Request limit reached. Try again later."}), 429
    if not DOWNLOAD_SLOT.acquire(blocking=False):
        return jsonify({"error": "Another operation is running. Try again soon."}), 429
    operation_lock = _acquire_operation_lock()
    if operation_lock is None:
        DOWNLOAD_SLOT.release()
        return jsonify({"error": "Another operation is running. Try again soon."}), 429
    release_slot = True
    try:
        cleanup_stale_downloads()
        ensure_storage_capacity()
        return jsonify(
            run_job("download", link, playlist_index, mode, operation_lock)
        )
    except JobReapError as error:
        release_slot = False
        return _error_response(error)
    except (
        InputError,
        UnsupportedMediaError,
        MediaLimitError,
        StorageLimitError,
        OperationTimeoutError,
        EngineError,
    ) as error:
        return _error_response(error)
    except Exception:  # noqa: BLE001
        logging.exception("Unexpected download error")
        return jsonify({"error": "Unexpected server error. Please try again."}), 500
    finally:
        if release_slot:
            _close_locked(operation_lock)
            DOWNLOAD_SLOT.release()
        else:
            ORPHANED_OPERATION_LOCKS.append(operation_lock)


@app.errorhandler(413)
def request_too_large(_error: Exception) -> tuple[Response, int]:
    return jsonify({"error": "Request is too large."}), 413


@app.route("/downloads/<path:filename>")
def download_file(filename: str) -> Response:
    file_path = resolve_download_path(filename)
    if file_path is None:
        abort(400)
    assert file_path is not None
    claimed_path = DOWNLOAD_PATH / f"{filename}.{uuid4().hex}.sending"
    claim_handle: BinaryIO | None = None
    with DOWNLOAD_FILES_LOCK:
        if not file_path.is_file():
            abort(404)
        claim_handle = _open_locked(file_path)
        if claim_handle is None:
            abort(404)
        try:
            file_stat = file_path.stat()
        except FileNotFoundError:
            _close_locked(claim_handle)
            abort(404)
        else:
            ttl = (
                SMALL_FILE_TTL_SECONDS
                if file_stat.st_size < SMALL_FILE_MAX_BYTES
                else LARGE_FILE_TTL_SECONDS
            )
        if file_stat.st_mtime <= time() - ttl:
            _unlink_locked(file_path, claim_handle)
            abort(404)
        if request.method == "HEAD":
            _close_locked(claim_handle)
            return send_from_directory(
                str(DOWNLOAD_PATH),
                filename,
                as_attachment=True,
                conditional=False,
            )
        try:
            file_path.replace(claimed_path)
        except FileNotFoundError:
            _close_locked(claim_handle)
            abort(404)
        except PermissionError:
            _close_locked(claim_handle)
            try:
                file_path.replace(claimed_path)
                claim_handle = _open_locked(claimed_path)
            except OSError:
                abort(404)
            if claim_handle is None:
                with suppress(OSError):
                    _restore_claim(claimed_path, file_path)
                abort(404)
        ACTIVE_DOWNLOAD_CLAIMS.add(claimed_path)

    source: Iterable[bytes] | None = None
    completed = False
    finalized = False

    def finalize() -> None:
        nonlocal finalized
        with DOWNLOAD_FILES_LOCK:
            if finalized:
                return
            finalized = True
            try:
                if completed:
                    claimed_path.unlink(missing_ok=True)
                else:
                    _restore_claim(claimed_path, file_path)
            except PermissionError:
                if claim_handle is not None and not claim_handle.closed:
                    _close_locked(claim_handle)
                try:
                    if completed:
                        claimed_path.unlink(missing_ok=True)
                    else:
                        _restore_claim(claimed_path, file_path)
                except OSError as error:
                    logging.error("Failed to finalize claimed download: %s", error)
            except OSError as error:
                logging.error("Failed to finalize claimed download: %s", error)
            finally:
                if claim_handle is not None and not claim_handle.closed:
                    _close_locked(claim_handle)
                if source is not None:
                    close = getattr(source, "close", None)
                    if callable(close):
                        try:
                            close()
                        except Exception:  # noqa: BLE001
                            logging.exception("Failed to close download stream")
                ACTIVE_DOWNLOAD_CLAIMS.discard(claimed_path)

    try:
        assert claim_handle is not None
        response = send_file(
            claim_handle,
            as_attachment=True,
            download_name=filename,
            conditional=False,
        )
    except Exception:
        finalize()
        raise
    source = cast(Iterable[bytes], response.response)
    if response.status_code != 200:
        finalize()
        return response

    def stream_and_finalize() -> Iterator[bytes]:
        nonlocal completed
        try:
            yield from source
            completed = True
        finally:
            finalize()

    response.response = stream_and_finalize()
    response.direct_passthrough = False
    response.call_on_close(finalize)
    return response


@app.route("/delete", methods=["POST"])
def delete_file() -> Response | tuple[Response, int]:
    if not request_origin_is_allowed():
        return jsonify({"error": "Origin is not allowed."}), 403
    requested_files = request.form.getlist("files")
    if len(requested_files) > MAX_DELETE_FILES:
        return jsonify({"error": "Too many files requested."}), 400
    if len(requested_files) != 1:
        return jsonify({"error": "No file to delete."}), 404

    filename = requested_files[0]
    target = resolve_download_path(filename)
    with DOWNLOAD_FILES_LOCK:
        if target is None or not target.is_file():
            return jsonify({"error": "No file to delete."}), 404
        assert target is not None
        handle = _open_locked(target)
        if handle is None:
            return jsonify({"error": "No file to delete."}), 404
        if not _unlink_locked(target, handle):
            return jsonify({"error": "File could not be deleted."}), 500
    return jsonify({"status": "Files deleted", "files": [filename]})


if __name__ == "__main__":
    if JOB_WORKER_MODE:
        raise SystemExit(_job_worker_main())
    app.run(host=HOST, port=PORT)
