"""Configuration loader — validates environment variables at startup."""

from dataclasses import dataclass
from dotenv import load_dotenv
import os
from pathlib import Path
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
    storage_root: str
    storage_primary: str
    to_place_dir: str
    to_fix_manually_dir: str
    # State files
    done_file: str
    cookies_file: str
    temp_dir: str
    # Tuning
    page_timeout: float
    page_wait: float
    book_wait: float           # seconds to pause between books
    min_image_size_kb: int
    # List of acceptable WxH dimensions; empty = accept any size
    allowed_image_dimensions: list[tuple[int, int]]
    max_retry: int
    chrome_offset: int | None  # None means auto-detect at runtime
    # Mode
    to_place_only: bool        # True = skip FAKKU downloads, only process ToPlace


def _parse_dimensions(raw: str) -> list[tuple[int, int]]:
    """Parse 'WxH,WxH,...' into a list of (width, height) tuples. Empty string → []."""
    result = []
    for part in raw.split(','):
        part = part.strip()
        if not part:
            continue
        try:
            w, h = part.lower().split('x')
            result.append((int(w), int(h)))
        except ValueError:
            sys.exit(f"[config] Invalid dimension '{part}' in ALLOWED_IMAGE_DIMENSIONS — expected WxH (e.g. 1360x1920)")
    return result


def load_config() -> Config:
    load_dotenv()

    to_place_only = os.getenv('TO_PLACE_ONLY', 'false').lower() in ('true', '1', 'yes')

    if to_place_only:
        required = [
            'SMTP_HOST', 'SMTP_USER', 'SMTP_PASSWORD',
            'SMTP_FROM', 'SMTP_TO', 'STORAGE_ROOT', 'STORAGE_PRIMARY',
        ]
    else:
        required = [
            'FAKKU_USERNAME', 'FAKKU_PASSWORD', 'FAKKU_TOTP_SECRET',
            'FAKKU_COLLECTION_URL', 'SMTP_HOST', 'SMTP_USER', 'SMTP_PASSWORD',
            'SMTP_FROM', 'SMTP_TO', 'STORAGE_ROOT', 'STORAGE_PRIMARY',
        ]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        sys.exit(f"[config] Missing required env vars: {', '.join(missing)}")

    chrome_offset_raw = os.getenv('CHROME_OFFSET', '').split('#')[0].strip()

    storage_root = os.getenv('STORAGE_ROOT')
    to_place_dir = str(Path(storage_root) / 'ToPlace')
    to_fix_manually_dir = str(Path(storage_root) / 'TO FIX MANUALLY')

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
        storage_root=storage_root,
        storage_primary=os.getenv('STORAGE_PRIMARY', './downloaded'),
        to_place_dir=to_place_dir,
        to_fix_manually_dir=to_fix_manually_dir,
        done_file=os.getenv('DONE_FILE', './done.txt'),
        cookies_file=os.getenv('COOKIES_FILE', './cookies.pickle'),
        temp_dir=os.getenv('TEMP_DIR', './tmp'),
        page_timeout=float(os.getenv('PAGE_TIMEOUT', '15')),
        page_wait=float(os.getenv('PAGE_WAIT', '15')),
        book_wait=float(os.getenv('BOOK_WAIT', '30')),
        min_image_size_kb=int(os.getenv('MIN_IMAGE_SIZE_KB', '50')),
        allowed_image_dimensions=_parse_dimensions(os.getenv('ALLOWED_IMAGE_DIMENSIONS', '')),
        max_retry=int(os.getenv('MAX_RETRY', '3')),
        chrome_offset=int(chrome_offset_raw) if chrome_offset_raw else None,
        to_place_only=to_place_only,
    )
