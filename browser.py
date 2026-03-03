"""Playwright browser wrapper — manages browser lifecycle, cookies, and chrome offset."""

from playwright.sync_api import sync_playwright, Page, BrowserContext
from config import Config


class Browser:
    def __init__(self, config: Config):
        self._config = config
        self._playwright = None
        self._browser = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None
        self._chrome_offset: int | None = None

    def start(self) -> None:
        """Launch Chromium with required flags. Must be called before any other method."""
        self._playwright = sync_playwright().start()
        # Use the system-installed Chrome binary instead of Playwright's bundled
        # Chromium. Real Chrome has the correct TLS fingerprint, real plugin list,
        # and a user-agent that automatically matches the installed version —
        # eliminating the most reliable binary-level bot-detection signals.
        self._browser = self._playwright.chromium.launch(
            channel='chrome',
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled',
            ],
        )
        self._context = self._browser.new_context(
            viewport={'width': 1440, 'height': 2560},
            locale='en-US',   # sets Accept-Language header + navigator.language/languages
            # No user_agent override — let Chrome report its real installed version
        )
        # Mask the three properties most commonly checked by bot-detection scripts.
        # This init script runs before any page or frame script on every navigation.
        self._context.add_init_script("""
            // Playwright sets navigator.webdriver = true even on real Chrome
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            // Ensure plugins is non-empty (headless can still zero it out)
            if (!navigator.plugins || navigator.plugins.length === 0)
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
            // Ensure window.chrome.runtime exists (absent in some headless contexts)
            if (!window.chrome) window.chrome = {};
            if (!window.chrome.runtime) window.chrome.runtime = {};
        """)
        self.page = self._context.new_page()
        self._setup_localstorage()

    def _setup_localstorage(self) -> None:
        """Set FAKKU reader preferences via localStorage."""
        self.page.goto('https://www.fakku.net', wait_until='domcontentloaded')
        self.page.evaluate(
            "window.localStorage.setItem('fakku-scrollWheelPageChange', 'false')"
        )

    def load_cookies(self, cookies: list[dict]) -> None:
        """Inject cookies into the browser context."""
        self._context.add_cookies(cookies)

    def get_cookies(self) -> list[dict]:
        """Return all cookies from the current browser context."""
        return self._context.cookies()

    def get_chrome_offset(self) -> int:
        """Return browser chrome height offset (px added to canvas height for window resize).

        Uses the configured value if set; otherwise auto-detects once via JS and caches.
        """
        if self._config.chrome_offset is not None:
            return self._config.chrome_offset
        if self._chrome_offset is None:
            outer = self.page.evaluate('window.outerHeight')
            inner = self.page.evaluate('window.innerHeight')
            self._chrome_offset = outer - inner
        return self._chrome_offset

    def close(self) -> None:
        """Shut down the browser and Playwright."""
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()
