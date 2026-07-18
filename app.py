from __future__ import annotations

import logging
import re
from collections import OrderedDict, deque
from collections.abc import Iterable, Iterator
from pathlib import Path
from threading import BoundedSemaphore, Lock
from time import monotonic, time
from typing import cast
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    render_template,
    request,
    send_from_directory,
)
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from config import (
    ALLOWED_ORIGINS,
    DOWNLOAD_DIR,
    HOST,
    MAX_DURATION_SECONDS,
    MAX_MEDIA_BYTES,
    MAX_REQUEST_BYTES,
    MAX_RETRIES,
    MAX_STORED_BYTES,
    PORT,
    RATE_LIMIT_MAX_CLIENTS,
    RATE_LIMIT_REQUESTS,
    RATE_LIMIT_WINDOW_SECONDS,
    STALE_FILE_AGE_SECONDS,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES

DOWNLOAD_PATH = DOWNLOAD_DIR
DOWNLOAD_PATH.mkdir(parents=True, exist_ok=True)
STATIC_PATH = Path(__file__).parent / "static"

YOUTUBE_HOSTS = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
)
VIDEO_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{11}")
GENERATED_FILE_PATTERN = re.compile(r"yt-download-.+-[0-9a-f]{32}\.(?:mp3|mp4)")
GENERATED_ARTIFACT_PATTERN = re.compile(r"yt-download-.+-[0-9a-f]{32}\..+")
MAX_DELETE_FILES = 2
# ponytail: one in-process job protects local disk.
# Add an external queue before adding workers.
DOWNLOAD_SLOT = BoundedSemaphore(1)
DOWNLOAD_ATTEMPTS: OrderedDict[str, deque[float]] = OrderedDict()
DOWNLOAD_ATTEMPTS_LOCK = Lock()


class MediaLimitError(Exception):
    pass


class StorageLimitError(Exception):
    pass


def sanitize_title(title: str) -> str:
    cleaned = re.sub(r"[^\w\- ]", "", title).strip()
    return cleaned[:60] or "youtube-file"


def is_youtube_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or hostname not in YOUTUBE_HOSTS:
        return False

    segments = parsed.path.strip("/").split("/") if parsed.path.strip("/") else []
    video_id: str | None = None
    if hostname == "youtu.be" and len(segments) == 1:
        video_id = segments[0]
    elif parsed.path.rstrip("/") == "/watch":
        values = parse_qs(parsed.query).get("v", [])
        video_id = values[0] if len(values) == 1 else None
    elif len(segments) == 2 and segments[0] in {"embed", "live", "shorts"}:
        video_id = segments[1]
    return video_id is not None and VIDEO_ID_PATTERN.fullmatch(video_id) is not None


def extract_title(link: str) -> str:
    with YoutubeDL(
        {
            "quiet": True,
            "max_filesize": MAX_MEDIA_BYTES,
            "noplaylist": True,
            "retries": MAX_RETRIES,
        }
    ) as client:
        info = client.extract_info(link, download=False)
    if not isinstance(info, dict):
        raise DownloadError("YouTube returned invalid video metadata")
    if info.get("_type") in {"playlist", "multi_video"}:
        raise MediaLimitError("Playlists are not supported.")
    duration = info.get("duration")
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or duration <= 0
    ):
        raise MediaLimitError("Live or unknown-length videos are not supported.")
    if duration > MAX_DURATION_SECONDS:
        raise MediaLimitError(
            f"Video exceeds the {MAX_DURATION_SECONDS // 60}-minute limit."
        )
    title = info.get("title") or info.get("id")
    return sanitize_title(title if isinstance(title, str) else uuid4().hex)


def download_media(link: str) -> dict[str, str]:
    title_stem = f"yt-download-{extract_title(link)}-{uuid4().hex}"
    audio_name = f"{title_stem}.mp3"
    video_name = f"{title_stem}.mp4"
    audio_path = DOWNLOAD_PATH / audio_name
    video_path = DOWNLOAD_PATH / video_name

    try:
        with YoutubeDL(
            {
                "quiet": True,
                "max_filesize": MAX_MEDIA_BYTES,
                "noplaylist": True,
                "retries": MAX_RETRIES,
                "outtmpl": str(DOWNLOAD_PATH / f"{title_stem}.%(ext)s"),
                "format": "bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "0",
                    }
                ],
            }
        ) as audio_client:
            audio_client.download([link])

        validate_output_file(audio_path)

        with YoutubeDL(
            {
                "quiet": True,
                "max_filesize": MAX_MEDIA_BYTES,
                "noplaylist": True,
                "retries": MAX_RETRIES,
                "outtmpl": str(DOWNLOAD_PATH / f"{title_stem}.%(ext)s"),
                "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]",
                "merge_output_format": "mp4",
            }
        ) as video_client:
            video_client.download([link])
        validate_output_file(video_path)
    except Exception:
        for target in DOWNLOAD_PATH.glob(f"{title_stem}.*"):
            target.unlink(missing_ok=True)
        raise

    if not audio_path.exists() or not video_path.exists():
        for target in DOWNLOAD_PATH.glob(f"{title_stem}.*"):
            target.unlink(missing_ok=True)
        raise FileNotFoundError("Expected media files were not created")

    return {"mp3_file": audio_name, "mp4_file": video_name}


def validate_output_file(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"Expected media file was not created: {path.name}")
    size = path.stat().st_size
    if size <= 0:
        raise FileNotFoundError(f"Generated media file is empty: {path.name}")
    if size > MAX_MEDIA_BYTES:
        raise MediaLimitError(f"Generated file exceeds the size limit: {path.name}")


def generated_artifacts() -> list[Path]:
    return [
        target
        for target in DOWNLOAD_PATH.iterdir()
        if target.is_file() and GENERATED_ARTIFACT_PATTERN.fullmatch(target.name)
    ]


def cleanup_stale_downloads() -> None:
    cutoff = time() - STALE_FILE_AGE_SECONDS
    for target in generated_artifacts():
        try:
            if target.stat().st_mtime <= cutoff:
                target.unlink()
        except FileNotFoundError:
            continue
        except OSError as error:
            logging.error("Failed to delete stale file %s: %s", target, error)


def ensure_storage_capacity() -> None:
    stored_bytes = 0
    for target in generated_artifacts():
        try:
            stored_bytes += target.stat().st_size
        except FileNotFoundError:
            continue
    if stored_bytes > MAX_STORED_BYTES - (2 * MAX_MEDIA_BYTES):
        raise StorageLimitError("Download storage is busy. Try again later.")


cleanup_stale_downloads()


def resolve_download_path(filename: str) -> Path | None:
    if (
        not filename
        or Path(filename).name != filename
        or GENERATED_FILE_PATTERN.fullmatch(filename) is None
    ):
        return None
    target = (DOWNLOAD_PATH / filename).resolve()
    return target if target.parent == DOWNLOAD_PATH else None


def request_origin_is_allowed() -> bool:
    origin = request.headers.get("Origin")
    if not origin:
        return True
    normalized = origin.rstrip("/")
    return normalized == request.host_url.rstrip("/") or normalized in ALLOWED_ORIGINS


def request_client_id() -> str:
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        client_ip = forwarded_for.partition(",")[0].strip()
        if client_ip:
            return client_ip
    return request.remote_addr or "unknown"


def download_rate_limit_exceeded(client_id: str) -> bool:
    now = monotonic()
    cutoff = now - RATE_LIMIT_WINDOW_SECONDS
    with DOWNLOAD_ATTEMPTS_LOCK:
        attempts = DOWNLOAD_ATTEMPTS.pop(client_id, deque())
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        if len(attempts) >= RATE_LIMIT_REQUESTS:
            DOWNLOAD_ATTEMPTS[client_id] = attempts
            return True
        attempts.append(now)
        DOWNLOAD_ATTEMPTS[client_id] = attempts
        while len(DOWNLOAD_ATTEMPTS) > RATE_LIMIT_MAX_CLIENTS:
            DOWNLOAD_ATTEMPTS.popitem(last=False)
    return False


@app.after_request
def add_cors_headers(response: Response) -> Response:
    origin = request.headers.get("Origin", "").rstrip("/")
    if origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Vary"] = "Origin"
    return response


@app.route("/")
def index() -> str:
    return render_template("index.html")


@app.route("/service-worker.js")
def service_worker() -> Response:
    return send_from_directory(str(STATIC_PATH), "service-worker.js")


@app.route("/download", methods=["POST"])
def download() -> Response | tuple[Response, int]:
    if not request_origin_is_allowed():
        return jsonify({"error": "Origin is not allowed."}), 403
    youtube_link = request.form.get("youtubeLink")
    if not youtube_link:
        json_payload = request.get_json(silent=True)
        if isinstance(json_payload, dict):
            json_link = json_payload.get("youtubeLink")
            youtube_link = json_link if isinstance(json_link, str) else None
    if not isinstance(youtube_link, str) or not youtube_link.strip():
        return jsonify({"error": "YouTube link is required."}), 400
    youtube_link = youtube_link.strip()

    if not is_youtube_url(youtube_link):
        return jsonify({"error": "Please provide a direct YouTube video link."}), 400

    if download_rate_limit_exceeded(request_client_id()):
        return jsonify({"error": "Download limit reached. Try again later."}), 429

    if not DOWNLOAD_SLOT.acquire(blocking=False):
        return jsonify({"error": "Another download is running. Try again soon."}), 429

    try:
        cleanup_stale_downloads()
        ensure_storage_capacity()
        files = download_media(youtube_link)
        return jsonify({"status": "Download complete", "files": files})
    except MediaLimitError as error:
        logging.info("Download rejected: %s", error)
        return jsonify({"error": str(error)}), 413
    except StorageLimitError as error:
        logging.warning("Download storage limit reached: %s", error)
        return jsonify({"error": str(error)}), 507
    except DownloadError as error:
        logging.error("Download failed: %s", error)
        return jsonify({"error": "Download failed. Try again soon."}), 502
    except FileNotFoundError as error:
        logging.error("Missing file after download: %s", error)
        return jsonify({"error": "Could not create download files."}), 500
    except Exception:  # noqa: BLE001
        logging.exception("Unexpected error")
        return jsonify({"error": "Unexpected server error. Please try again."}), 500
    finally:
        DOWNLOAD_SLOT.release()


@app.errorhandler(413)
def request_too_large(_error: Exception) -> tuple[Response, int]:
    return jsonify({"error": "Request is too large."}), 413


@app.route("/downloads/<path:filename>")
def download_file(filename: str) -> Response:
    file_path = resolve_download_path(filename)
    if file_path is None:
        abort(400)
    assert file_path is not None
    if not file_path.is_file():
        abort(404)
    response = send_from_directory(str(DOWNLOAD_PATH), filename, as_attachment=True)
    if response.status_code == 200 and request.method == "GET":
        source = cast(Iterable[bytes], response.response)

        def stream_and_delete() -> Iterator[bytes]:
            try:
                yield from source
            finally:
                close = getattr(source, "close", None)
                if callable(close):
                    close()
                file_path.unlink(missing_ok=True)

        response.response = stream_and_delete()
    return response


@app.route("/delete", methods=["POST"])
def delete_file() -> Response | tuple[Response, int]:
    if not request_origin_is_allowed():
        return jsonify({"error": "Origin is not allowed."}), 403
    requested_files = request.form.getlist("files")
    if len(requested_files) > MAX_DELETE_FILES:
        return jsonify({"error": "Too many files requested."}), 400

    removed_files: list[str] = []
    for file_name in requested_files:
        target = resolve_download_path(file_name)
        if target is not None and target.is_file():
            target.unlink()
            removed_files.append(file_name)

    if removed_files:
        return jsonify({"status": "Files deleted", "files": removed_files})
    return jsonify({"error": "No files to delete"}), 404


if __name__ == "__main__":
    app.run(host=HOST, port=PORT)
