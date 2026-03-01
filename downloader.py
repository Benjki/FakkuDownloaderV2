"""Queue resolution and per-book download orchestration."""

import logging
import re
import shutil
import time
import traceback
from pathlib import Path

import requests
from bs4 import BeautifulSoup

import notifier as notifier_module
from book import Book
from browser import Browser
from config import Config
from helper import load_done_file, append_done, create_folder_if_missing, normalise_url
from organizer import (
    MetadataError,
    check_and_move_oneshot,
    check_ownership,
    compute_short_title,
    detect_series,
    extract_metadata,
    build_filename,
    pack_cbz,
    route_book,
)

logger = logging.getLogger(__name__)

BASE_URL = 'https://www.fakku.net'


class PaginationError(Exception):
    pass


class Downloader:
    def __init__(self, browser: Browser, config: Config):
        self._browser = browser
        self._config = config
        self._done: set[str] = load_done_file(config.done_file)

    # ------------------------------------------------------------------
    # Queue resolution
    # ------------------------------------------------------------------

    def fetch_queue(self) -> list[str]:
        """
        Fetch the FAKKU collection page (paginated) and return a list of
        normalised book URLs not already in done.txt.
        Raises PaginationError if pagination cannot be determined.
        """
        config = self._config
        page = self._browser.page

        page.goto(config.fakku_collection_url, wait_until='domcontentloaded')
        time.sleep(3)

        html = page.content()
        soup = BeautifulSoup(html, 'lxml')

        # Determine total page count
        page_count = 1
        last_page_a = soup.find('a', title='Last Page')
        if last_page_a:
            m = re.search(r'/page/(\d+)', last_page_a.get('href', ''))
            if m:
                page_count = int(m.group(1))
        else:
            next_btn = soup.find('a', title='Next Page') or soup.find('a', rel='next')
            if next_btn:
                raise PaginationError(
                    '"Last Page" selector not found but next-page indicator exists. '
                    'The collection CSS selector may need updating.'
                )

        all_urls: list[str] = []
        for pg in range(1, page_count + 1):
            if pg > 1:
                page.goto(
                    f'{config.fakku_collection_url}/page/{pg}',
                    wait_until='domcontentloaded',
                )
                time.sleep(2)
                html = page.content()
                soup = BeautifulSoup(html, 'lxml')

            for div in soup.find_all(
                'div', class_=lambda c: c and 'flex' in c and 'mt-3' in c
            ):
                a = div.find('a', href=True)
                if a:
                    all_urls.append(normalise_url(BASE_URL + a['href']))

        # Deduplicate, preserve order, skip already-done
        seen: set[str] = set()
        queue: list[str] = []
        for u in all_urls:
            if u not in seen and u not in self._done:
                seen.add(u)
                queue.append(u)

        logger.info(
            'Queue: %d book(s) pending (%d total in collection).', len(queue), len(all_urls)
        )
        return queue

    # ------------------------------------------------------------------
    # Per-book orchestration
    # ------------------------------------------------------------------

    def download_book(self, url: str) -> str:
        """
        Full per-book flow (steps 1-13 from spec Section 7.1).
        Returns a display name for the success summary.
        Raises on unrecoverable error — caller catches and halts.
        """
        config = self._config
        page = self._browser.page

        # 1. Fetch book info page
        page.goto(url, wait_until='domcontentloaded')
        time.sleep(2)
        html = page.content()

        # 2. Ownership check
        if not check_ownership(html):
            logger.warning('Not owned — skipping: %s', url)
            append_done(config.done_file, url)
            self._done.add(normalise_url(url))
            return f'[SKIP/NOT_OWNED] {url}'

        # 3. Extract metadata
        meta = extract_metadata(html)
        title = meta['title']
        author = meta['author']
        pages = meta['pages']
        tags = meta['tags']

        # 4. Routing flags
        is_cover = pages <= 4

        # 5. Series detection
        series_name, volume_number, short_title = None, None, None
        if not is_cover:
            session = self._make_requests_session()
            series_name, volume_number, _ = detect_series(html, url, session)
            if series_name:
                short_title = compute_short_title(title, series_name)

        # 6. Build Book dataclass
        book = Book(
            title=title,
            author=author,
            pages=pages,
            tags=tags,
            source_url=normalise_url(url),
            series_name=series_name,
            volume_number=volume_number,
            short_title=short_title,
            is_cover=is_cover,
        )

        logger.info('Downloading: %s (%d pages)', book.display_name(), pages)

        # 7. Retroactive one-shot → series move (vol.N >= 2 only)
        rel_dir = route_book(book)
        series_dir_abs = str(Path(config.storage_primary) / rel_dir)
        if book.is_series() and book.volume_number and book.volume_number >= 2:
            if not Path(series_dir_abs).exists():
                check_and_move_oneshot(
                    series_name, author, series_name, config.storage_primary, rel_dir
                )

        # 8. Create temp directory for this book's pages
        temp_dir = str(Path(config.temp_dir) / _safe_dirname(title))
        create_folder_if_missing(temp_dir)

        # 9. Screenshot each page
        for page_num in range(1, pages + 1):
            dest = str(Path(temp_dir) / f'{page_num}.png')
            self.download_page(url, dest, page_num)

        # 10. Page count validation (at least 80% present)
        actual = len(list(Path(temp_dir).glob('*.png')))
        if actual < pages * 0.8:
            raise ValueError(
                f'Page count mismatch for "{title}": expected {pages}, got {actual}'
            )

        # 11. Pack CBZ
        filename = build_filename(book)
        create_folder_if_missing(series_dir_abs)
        cbz_path = str(Path(series_dir_abs) / filename)
        pack_cbz(temp_dir, cbz_path, tags)

        # 12. Clean up temp directory
        shutil.rmtree(temp_dir, ignore_errors=True)

        # 13. Mark done
        append_done(config.done_file, url)
        self._done.add(normalise_url(url))

        logger.info('Done: %s -> %s', book.display_name(), cbz_path)
        return book.display_name()

    def download_page(self, book_url: str, dest: str, page_num: int) -> None:
        """
        Screenshot a single reader page with retry logic.
        Attempts up to config.max_retry times with exponential backoff.
        """
        config = self._config
        page = self._browser.page
        delays = [2, 4, 8]

        for attempt in range(config.max_retry):
            try:
                reader_url = f'{book_url}/read/page/{page_num}'
                page.goto(reader_url, wait_until='domcontentloaded')

                # Soft-ban check — unexpected redirect means possible rate-limit
                if '/read/page/' not in page.url:
                    raise RuntimeError(
                        f'Unexpected redirect to {page.url} — possible soft ban'
                    )

                # Wait for the reader iframe and page view element
                frame_locator = page.frame_locator('iframe[title="FAKKU Reader"]')
                frame_locator.locator('div[data-name="PageView"]').wait_for(
                    timeout=int(config.page_timeout * 1000)
                )

                # Remove top overlay layer via JS on the iframe document
                layers = page.evaluate(
                    """() => {
                        const iframe = document.querySelector('iframe[title="FAKKU Reader"]');
                        if (!iframe || !iframe.contentDocument) return 0;
                        return iframe.contentDocument.getElementsByClassName('layer').length;
                    }"""
                )
                if layers and layers > 0:
                    page.evaluate(
                        f"""() => {{
                            const iframe = document.querySelector('iframe[title="FAKKU Reader"]');
                            if (!iframe || !iframe.contentDocument) return;
                            const layers = iframe.contentDocument.getElementsByClassName('layer');
                            if (layers.length > 0) layers[layers.length - 1].remove();
                        }}"""
                    )

                # Get canvas dimensions
                canvas_idx = max(0, (layers or 1) - 2)
                width = page.evaluate(
                    f"""() => {{
                        const iframe = document.querySelector('iframe[title="FAKKU Reader"]');
                        if (!iframe || !iframe.contentDocument) return 1440;
                        const canvases = iframe.contentDocument.getElementsByTagName('canvas');
                        return canvases[{canvas_idx}] ? canvases[{canvas_idx}].width : 1440;
                    }}"""
                )
                height = page.evaluate(
                    f"""() => {{
                        const iframe = document.querySelector('iframe[title="FAKKU Reader"]');
                        if (!iframe || !iframe.contentDocument) return 2560;
                        const canvases = iframe.contentDocument.getElementsByTagName('canvas');
                        return canvases[{canvas_idx}] ? canvases[{canvas_idx}].height : 2560;
                    }}"""
                )

                page.set_viewport_size({'width': int(width), 'height': int(height)})
                time.sleep(config.page_wait)

                # Take screenshot
                page.screenshot(path=dest, full_page=False)

                # Validate file size
                size_kb = Path(dest).stat().st_size / 1024
                if size_kb < config.min_image_size_kb:
                    raise ValueError(
                        f'Screenshot too small ({size_kb:.1f} KB < {config.min_image_size_kb} KB)'
                    )

                # Restore viewport for next navigation
                page.set_viewport_size({'width': 1440, 'height': 2560})
                return

            except Exception as e:
                logger.warning('Page %d attempt %d/%d failed: %s', page_num, attempt + 1, config.max_retry, e)
                if attempt < len(delays):
                    time.sleep(delays[attempt])
                if attempt == config.max_retry - 1:
                    raise

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_requests_session(self) -> requests.Session:
        """Build a requests.Session with current browser cookies."""
        session = requests.Session()
        for c in self._browser.get_cookies():
            session.cookies.set(c['name'], c['value'], domain=c.get('domain', ''))
        return session

    def run(self) -> None:
        """Main loop: fetch_queue() → download each book in order."""
        start = time.time()
        downloaded: list[str] = []

        try:
            queue = self.fetch_queue()
        except PaginationError as e:
            logger.error('Pagination error: %s', e)
            notifier_module.send_error(
                self._config, self._config.fakku_collection_url, None, str(e), ''
            )
            return

        if not queue:
            logger.info('Nothing to download.')
            elapsed = time.strftime('%H:%M:%S', time.gmtime(time.time() - start))
            notifier_module.send_success(self._config, [], elapsed)
            return

        for url in queue:
            try:
                display = self.download_book(url)
                downloaded.append(display)
            except Exception as e:
                tb = traceback.format_exc()
                logger.error('Error downloading %s: %s\n%s', url, e, tb)
                notifier_module.send_error(self._config, url, None, str(e), tb)
                return  # halt on first failure

        elapsed = time.strftime('%H:%M:%S', time.gmtime(time.time() - start))
        notifier_module.send_success(self._config, downloaded, elapsed)
        logger.info('Run complete. %d book(s). Elapsed: %s', len(downloaded), elapsed)


def _safe_dirname(name: str) -> str:
    """Sanitize a title string for use as a temp directory name."""
    return re.sub(r'[\\/*?:"<>|]', '_', name)[:100]
