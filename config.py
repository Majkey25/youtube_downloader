from __future__ import annotations

import os
from pathlib import Path

ROOT_DIR = Path(__file__).parent
DEFAULT_DOWNLOAD_DIR = ROOT_DIR / "downloads"

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", DEFAULT_DOWNLOAD_DIR)).resolve()
SECRET_KEY = os.getenv("SECRET_KEY", "change-me")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))
USER_AGENT = os.getenv(
    "USER_AGENT",
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
)
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
RANDOM_DELAY_MIN_SECONDS = float(os.getenv("RANDOM_DELAY_MIN_SECONDS", "0"))
RANDOM_DELAY_MAX_SECONDS = float(os.getenv("RANDOM_DELAY_MAX_SECONDS", "1.0"))
