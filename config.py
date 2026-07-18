from __future__ import annotations

import os
from pathlib import Path

ROOT_DIR = Path(__file__).parent
DEFAULT_DOWNLOAD_DIR = ROOT_DIR / "downloads"
DEFAULT_ALLOWED_ORIGINS = "https://majkey25.github.io"

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", DEFAULT_DOWNLOAD_DIR)).resolve()
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
MAX_DURATION_SECONDS = int(os.getenv("MAX_DURATION_SECONDS", "7200"))
MAX_MEDIA_BYTES = int(os.getenv("MAX_MEDIA_BYTES", str(512 * 1024 * 1024)))
MAX_STORED_BYTES = int(os.getenv("MAX_STORED_BYTES", str(2 * 1024 * 1024 * 1024)))
STALE_FILE_AGE_SECONDS = int(os.getenv("STALE_FILE_AGE_SECONDS", "3600"))
MAX_REQUEST_BYTES = int(os.getenv("MAX_REQUEST_BYTES", "16384"))
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "5"))
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "3600"))
RATE_LIMIT_MAX_CLIENTS = int(os.getenv("RATE_LIMIT_MAX_CLIENTS", "2048"))
_origins = os.getenv("ALLOWED_ORIGINS", DEFAULT_ALLOWED_ORIGINS).split(",")
ALLOWED_ORIGINS = frozenset(
    origin.strip().rstrip("/") for origin in _origins if origin.strip()
)

if MAX_RETRIES < 0:
    raise ValueError("MAX_RETRIES must be zero or greater")
if (
    min(
        MAX_DURATION_SECONDS,
        MAX_MEDIA_BYTES,
        MAX_STORED_BYTES,
        STALE_FILE_AGE_SECONDS,
        MAX_REQUEST_BYTES,
        RATE_LIMIT_REQUESTS,
        RATE_LIMIT_WINDOW_SECONDS,
        RATE_LIMIT_MAX_CLIENTS,
    )
    <= 0
):
    raise ValueError("Resource limits must be greater than zero")
if MAX_STORED_BYTES < 2 * MAX_MEDIA_BYTES:
    raise ValueError("MAX_STORED_BYTES must fit one MP3 and MP4 job")
