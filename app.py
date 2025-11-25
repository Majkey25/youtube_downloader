from __future__ import annotations

import logging
import random
import re
import signal
import sys
import time
from pathlib import Path
from uuid import uuid4

from flask import (
    Flask,
    abort,
    jsonify,
    make_response,
    render_template,
    request,
    send_from_directory,
)
from yt_dlp import DownloadError, YoutubeDL

from config import (
    DOWNLOAD_DIR,
    HOST,
    MAX_RETRIES,
    PORT,
    RANDOM_DELAY_MAX_SECONDS,
    RANDOM_DELAY_MIN_SECONDS,
    SECRET_KEY,
    USER_AGENT,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = SECRET_KEY

DOWNLOAD_PATH = DOWNLOAD_DIR
DOWNLOAD_PATH.mkdir(parents=True, exist_ok=True)
STATIC_PATH = Path(app.static_folder)

YOUTUBE_PATTERN = re.compile(
    r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/", re.IGNORECASE
)
downloaded_files: set[str] = set()


def sanitize_title(title: str) -> str:
    cleaned = re.sub(r"[^\w\- ]", "", title).strip()
    return cleaned[:60] or "youtube-file"


def wait_random_delay() -> None:
    if (
        RANDOM_DELAY_MAX_SECONDS <= 0
        or RANDOM_DELAY_MAX_SECONDS < RANDOM_DELAY_MIN_SECONDS
    ):
        return
    delay_seconds = random.uniform(
        RANDOM_DELAY_MIN_SECONDS, RANDOM_DELAY_MAX_SECONDS
    )
    logging.info("Sleeping %.2f seconds to reduce rate limits", delay_seconds)
    time.sleep(delay_seconds)


def extract_title(link: str) -> str:
    options = {
        "quiet": True,
        "skip_download": True,
        "http_headers": {"User-Agent": USER_AGENT},
    }
    with YoutubeDL(options) as client:
        info = client.extract_info(link, download=False)
    return sanitize_title(info.get("title") or info.get("id") or uuid4().hex)


def download_media(link: str) -> dict[str, str]:
    title_stem = f"{extract_title(link)}-{uuid4().hex[:6]}"
    audio_name = f"{title_stem}.mp3"
    video_name = f"{title_stem}.mp4"
    audio_path = DOWNLOAD_PATH / audio_name
    video_path = DOWNLOAD_PATH / video_name

    base_options = {
        "quiet": True,
        "retries": MAX_RETRIES,
        "outtmpl": str(DOWNLOAD_PATH / f"{title_stem}.%(ext)s"),
        "http_headers": {"User-Agent": USER_AGENT},
    }

    audio_options = {
        **base_options,
        "format": "bestaudio/best",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "0",
            }
        ],
    }

    video_options = {
        **base_options,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]",
        "merge_output_format": "mp4",
    }

    wait_random_delay()
    try:
        with YoutubeDL(audio_options) as audio_client:
            audio_client.download([link])

        with YoutubeDL(video_options) as video_client:
            video_client.download([link])
    except Exception:
        for target in (audio_path, video_path):
            if target.exists():
                target.unlink()
        raise

    if not audio_path.exists() or not video_path.exists():
        raise FileNotFoundError("Expected media files were not created")

    downloaded_files.update({audio_name, video_name})
    return {"mp3_file": audio_name, "mp4_file": video_name}


@app.route("/")
def index() -> str:
    return render_template("index.html")


@app.route("/service-worker.js")
def service_worker() -> object:
    return send_from_directory(str(STATIC_PATH), "service-worker.js")


@app.route("/download", methods=["POST"])
def download() -> object:
    youtube_link = request.form.get("youtubeLink")
    if not youtube_link:
        json_payload = request.get_json(silent=True) or {}
        youtube_link = json_payload.get("youtubeLink")
    if not youtube_link:
        return jsonify({"error": "YouTube link is required."}), 400

    if not YOUTUBE_PATTERN.search(youtube_link):
        return jsonify({"error": "Please provide a valid YouTube link."}), 400

    try:
        files = download_media(youtube_link)
        response = make_response(
            jsonify({"status": "Download complete", "files": files})
        )
        response.set_cookie("downloaded_file", files["mp3_file"])
        return response
    except DownloadError as error:
        logging.error("Download failed: %s", error)
        return jsonify({"error": "Download failed. Try again soon."}), 502
    except FileNotFoundError as error:
        logging.error("Missing file after download: %s", error)
        return jsonify({"error": "Could not create download files."}), 500
    except Exception as error:  # noqa: BLE001
        logging.exception("Unexpected error")
        return jsonify({"error": "Unexpected server error. Please try again."}), 500


@app.route("/downloads/<path:filename>")
def download_file(filename: str) -> object:
    file_path = (DOWNLOAD_PATH / filename).resolve()
    if DOWNLOAD_PATH not in file_path.parents:
        abort(400)
    if not file_path.exists():
        abort(404)
    return send_from_directory(
        str(DOWNLOAD_PATH), filename, as_attachment=True
    )


@app.route("/delete", methods=["POST"])
def delete_file() -> object:
    removed_files: list[str] = []
    for file_name in list(downloaded_files):
        target = DOWNLOAD_PATH / file_name
        if target.exists():
            target.unlink()
            removed_files.append(file_name)
        downloaded_files.discard(file_name)

    if removed_files:
        return jsonify({"status": "Files deleted", "files": removed_files})
    return jsonify({"error": "No files to delete"}), 404


@app.route("/delete_cookie", methods=["POST"])
def delete_cookie() -> object:
    response = make_response(jsonify({"status": "Cookie deleted"}))
    response.set_cookie("downloaded_file", "", expires=0)
    return response


def cleanup_downloads() -> None:
    for file_name in list(downloaded_files):
        target = DOWNLOAD_PATH / file_name
        try:
            if target.exists():
                target.unlink()
                logging.info("Deleted %s", target)
        except Exception as error:  # noqa: BLE001
            logging.error("Failed to delete %s: %s", target, error)
        finally:
            downloaded_files.discard(file_name)


def signal_handler(signal_number: int, frame) -> None:  # type: ignore[override]
    logging.info("Shutting down server")
    cleanup_downloads()
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    app.run(host=HOST, port=PORT)
