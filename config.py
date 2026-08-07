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
MAX_MEDIA_BYTES = int(os.getenv("MAX_MEDIA_BYTES", str(216 * 1024 * 1024)))
MAX_STORED_BYTES = int(os.getenv("MAX_STORED_BYTES", str(648 * 1024 * 1024)))
MAX_PLAYLIST_ITEMS = int(os.getenv("MAX_PLAYLIST_ITEMS", "50"))
SOCKET_TIMEOUT_SECONDS = int(os.getenv("SOCKET_TIMEOUT_SECONDS", "15"))
JOB_TIMEOUT_SECONDS = int(os.getenv("JOB_TIMEOUT_SECONDS", "900"))
JOB_MEMORY_BYTES = int(os.getenv("JOB_MEMORY_BYTES", str(192 * 1024 * 1024)))
MAX_HLS_MANIFEST_BYTES = int(
    os.getenv("MAX_HLS_MANIFEST_BYTES", str(8 * 1024 * 1024))
)
SMALL_FILE_MAX_BYTES = int(
    os.getenv("SMALL_FILE_MAX_BYTES", str(64 * 1024 * 1024))
)
SMALL_FILE_TTL_SECONDS = int(os.getenv("SMALL_FILE_TTL_SECONDS", "600"))
LARGE_FILE_TTL_SECONDS = int(os.getenv("LARGE_FILE_TTL_SECONDS", "1800"))
MAX_URL_LENGTH = int(os.getenv("MAX_URL_LENGTH", "2048"))
MAX_REQUEST_BYTES = int(os.getenv("MAX_REQUEST_BYTES", "16384"))
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "5"))
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "3600"))
RATE_LIMIT_MAX_CLIENTS = int(os.getenv("RATE_LIMIT_MAX_CLIENTS", "2048"))
GLOBAL_RATE_LIMIT_REQUESTS = int(os.getenv("GLOBAL_RATE_LIMIT_REQUESTS", "60"))
TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "0") == "1"
TRUST_X_REAL_IP = os.getenv("TRUST_X_REAL_IP", "0") == "1"
if TRUST_X_REAL_IP and not TRUST_PROXY_HEADERS:
    raise ValueError("TRUST_X_REAL_IP requires TRUST_PROXY_HEADERS=1")
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
        MAX_PLAYLIST_ITEMS,
        SOCKET_TIMEOUT_SECONDS,
        JOB_TIMEOUT_SECONDS,
        JOB_MEMORY_BYTES,
        MAX_HLS_MANIFEST_BYTES,
        SMALL_FILE_MAX_BYTES,
        SMALL_FILE_TTL_SECONDS,
        LARGE_FILE_TTL_SECONDS,
        MAX_URL_LENGTH,
        MAX_REQUEST_BYTES,
        RATE_LIMIT_REQUESTS,
        RATE_LIMIT_WINDOW_SECONDS,
        RATE_LIMIT_MAX_CLIENTS,
        GLOBAL_RATE_LIMIT_REQUESTS,
    )
    <= 0
):
    raise ValueError("Resource limits must be greater than zero")
if MAX_STORED_BYTES < 3 * MAX_MEDIA_BYTES:
    raise ValueError("MAX_STORED_BYTES must be at least three times MAX_MEDIA_BYTES")
