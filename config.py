"""Configuration loader — validates environment variables at startup."""

from dataclasses import dataclass
from dotenv import load_dotenv
import os
import sys


@dataclass
class Config:
    # Auth
    fakku_username: str
    fakku_password: str
    fakku_totp_secret: str
    # Queue
    fakku_collection_url: str
    # SMTP
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_from: str
    smtp_to: str
    # Storage
    storage_primary: str
    # State files
    done_file: str
    cookies_file: str
    temp_dir: str
    # Tuning
    page_timeout: float
    page_wait: float
    min_image_size_kb: int
    max_retry: int
    chrome_offset: int | None  # None means auto-detect at runtime


def load_config() -> Config:
    load_dotenv()

    required = [
        'FAKKU_USERNAME', 'FAKKU_PASSWORD', 'FAKKU_TOTP_SECRET',
        'FAKKU_COLLECTION_URL', 'SMTP_HOST', 'SMTP_USER', 'SMTP_PASSWORD',
        'SMTP_FROM', 'SMTP_TO', 'STORAGE_PRIMARY',
    ]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        sys.exit(f"[config] Missing required env vars: {', '.join(missing)}")

    chrome_offset_raw = os.getenv('CHROME_OFFSET', '').strip()

    return Config(
        fakku_username=os.getenv('FAKKU_USERNAME'),
        fakku_password=os.getenv('FAKKU_PASSWORD'),
        fakku_totp_secret=os.getenv('FAKKU_TOTP_SECRET'),
        fakku_collection_url=os.getenv('FAKKU_COLLECTION_URL'),
        smtp_host=os.getenv('SMTP_HOST'),
        smtp_port=int(os.getenv('SMTP_PORT', '587')),
        smtp_user=os.getenv('SMTP_USER'),
        smtp_password=os.getenv('SMTP_PASSWORD'),
        smtp_from=os.getenv('SMTP_FROM'),
        smtp_to=os.getenv('SMTP_TO'),
        storage_primary=os.getenv('STORAGE_PRIMARY', './downloaded'),
        done_file=os.getenv('DONE_FILE', './done.txt'),
        cookies_file=os.getenv('COOKIES_FILE', './cookies.pickle'),
        temp_dir=os.getenv('TEMP_DIR', './tmp'),
        page_timeout=float(os.getenv('PAGE_TIMEOUT', '15')),
        page_wait=float(os.getenv('PAGE_WAIT', '5')),
        min_image_size_kb=int(os.getenv('MIN_IMAGE_SIZE_KB', '50')),
        max_retry=int(os.getenv('MAX_RETRY', '3')),
        chrome_offset=int(chrome_offset_raw) if chrome_offset_raw else None,
    )
