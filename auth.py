"""Authentication — cookie lifecycle management and TOTP auto-login."""

import logging
import pickle
from datetime import datetime, timezone

import pyotp

from browser import Browser
from config import Config

logger = logging.getLogger(__name__)

LOGIN_URL = 'https://www.fakku.net/login'


class AuthError(Exception):
    pass


def load_cookies(path: str) -> list[dict] | None:
    """Load cookies from pickle. Returns None if file missing or corrupt."""
    try:
        with open(path, 'rb') as f:
            return pickle.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning(f'Failed to load cookies from {path}: {e}')
        return None


def save_cookies(path: str, cookies: list[dict]) -> None:
    """Persist cookies to pickle file."""
    with open(path, 'wb') as f:
        pickle.dump(cookies, f)


def cookies_are_valid(cookies: list[dict], threshold_days: int = 7) -> bool:
    """Return True if the fakku_otpa cookie expires more than threshold_days from now."""
    now = datetime.now(tz=timezone.utc).timestamp()
    for c in cookies:
        if c.get('name') == 'fakku_otpa':
            exp = c.get('expires', 0)
            if exp and (exp - now) > threshold_days * 86400:
                return True
    return False


def login(browser: Browser, config: Config) -> list[dict]:
    """
    Full TOTP login flow. Returns browser cookies on success.
    Raises AuthError on any step failure.
    """
    page = browser.page
    logger.info('Navigating to login page...')
    page.goto(LOGIN_URL, wait_until='networkidle')

    # Wait for the login form to render (React/Next.js app — DOM ready isn't enough)
    try:
        page.wait_for_selector('input[type="email"]', state='visible', timeout=30000)
    except Exception as e:
        raise AuthError(f'Login form did not appear — page may have changed: {e}') from e

    # Fill email + password
    try:
        page.fill('input[type="email"]', config.fakku_username)
        page.fill('input[name="password"]', config.fakku_password)
        page.click('button[type="submit"]')
    except Exception as e:
        raise AuthError(f'Failed to fill login form: {e}') from e

    # Wait for TOTP field
    try:
        page.wait_for_selector('input[name="totp"]', state='visible', timeout=30000)
    except Exception as e:
        raise AuthError(f'TOTP field did not appear after login: {e}') from e

    # Generate TOTP code and submit
    totp_code = pyotp.TOTP(config.fakku_totp_secret).now()
    logger.info('Submitting TOTP code...')
    try:
        page.fill('input[name="totp"]', totp_code)
        page.click('button[type="submit"]')
    except Exception as e:
        raise AuthError(f'Failed to submit TOTP: {e}') from e

    # Verify we've left the login page
    try:
        page.wait_for_url(lambda url: '/login' not in url, timeout=15000)
    except Exception as e:
        raise AuthError(f'Login did not redirect away from /login/: {e}') from e

    logger.info('Login successful.')
    return browser.get_cookies()


def ensure_authenticated(browser: Browser, config: Config, notifier) -> None:
    """
    Called at the start of every run.
    - If valid cookies exist (>7 days until expiry): inject them, done.
    - If near-expiry (<=14 days): inject AND send a warning email.
    - If missing/expired: run TOTP login, save new cookies.
    """
    cookies = load_cookies(config.cookies_file)

    if cookies is not None:
        if cookies_are_valid(cookies, threshold_days=7):
            browser.load_cookies(cookies)
            # Warn if expiring within 14 days
            if not cookies_are_valid(cookies, threshold_days=14):
                notifier.send_warning(
                    config,
                    subject='Cookies expiring soon',
                    body=(
                        'FAKKU session cookies will expire within 14 days. '
                        'A fresh login will occur automatically on the next expiry.'
                    ),
                )
            logger.info('Cookies loaded from file — login skipped.')
            return

    # Missing, corrupt, or expired → full TOTP login
    logger.info('Cookies missing or expired — running TOTP login...')
    new_cookies = login(browser, config)
    save_cookies(config.cookies_file, new_cookies)
    browser.load_cookies(new_cookies)
    logger.info('New cookies saved to %s.', config.cookies_file)
