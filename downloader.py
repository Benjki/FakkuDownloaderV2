"""Queue resolution and per-book download orchestration."""

import logging
import random
import re
import shutil
import struct
import time
import traceback
from pathlib import Path

import requests
from bs4 import BeautifulSoup

import notifier as notifier_module
from book import Book
from browser import Browser
from config import Config
from helper import load_done_file, append_done, create_folder_if_missing, normalise_url, replace_illegal, first_letter
from organizer import (
    MetadataError,
    TO_FIX_MANUALLY,
    check_and_move_oneshot,
    check_ownership,
    compute_short_title,
    detect_series,
    extract_metadata,
    build_filename,
    infer_series_from_title,
    pack_cbz,
    route_book,
)

logger = logging.getLogger(__name__)

BASE_URL = 'https://www.fakku.net'


class PaginationError(Exception):
    pass


class SessionError(Exception):
    pass


class EndOfBook(Exception):
    """Raised when the reader redirects to a page earlier than requested — book is shorter than metadata claims."""
    def __init__(self, requested: int, actual: int):
        super().__init__(f'Requested page {requested} but reader shows page {actual} — book ends at page {actual}')
        self.actual_page = actual


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
        # Collection items are injected by JavaScript after the initial HTML loads.
        # Wait for at least one book div to appear before reading page content.
        try:
            page.wait_for_selector('div.flex.mt-3', timeout=20000)
        except Exception:
            pass  # Empty collection or slow load — proceed with whatever is available
        time.sleep(1)

        html = page.content()
        actual_url = page.url
        if actual_url.rstrip('/') != config.fakku_collection_url.rstrip('/'):
            logger.warning('Collection page redirected: %s -> %s', config.fakku_collection_url, actual_url)
        logger.info('Collection page loaded (url=%s html_len=%d)', actual_url, len(html))
        soup = BeautifulSoup(html, 'lxml')

        # Determine total page count
        page_count = 1
        last_page_a = soup.find('a', title='Last Page')
        if last_page_a:
            m = re.search(r'/page/(\d+)', last_page_a.get('href', ''))
            if m:
                page_count = int(m.group(1))
        else:
            # New pagination style: individual "Page N" links, no "Last Page" link
            page_nums = [
                int(m.group(1))
                for a in soup.find_all('a', title=re.compile(r'^Page \d+$'))
                if (m := re.search(r'^Page (\d+)$', a.get('title', '')))
            ]
            if page_nums:
                page_count = max(page_nums)
            elif soup.find('a', title='Next Page') or soup.find('a', rel='next'):
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
                try:
                    page.wait_for_selector('div.flex.mt-3', timeout=20000)
                except Exception:
                    pass
                time.sleep(1)
                html = page.content()
                soup = BeautifulSoup(html, 'lxml')

            divs = soup.find_all(
                'div', class_=lambda c: c and 'flex' in c and 'mt-3' in c
            )
            logger.info('Page %d: found %d book divs.', pg, len(divs))
            if pg == 1 and len(divs) == 0:
                # id="collection-slug" is injected by the server only for authenticated
                # collection owners. Its absence means the page is a stripped/guest render.
                if 'id="collection-slug"' not in html:
                    raise SessionError(
                        'Collection page loaded but missing ownership marker — '
                        'session is likely invalid.'
                    )
            for div in divs:
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

    def download_book(self, url: str, idx: int = 0, total: int = 0) -> dict:
        """
        Full per-book flow (steps 1-13 from spec Section 7.1).
        Returns a report dict for the success summary email.
        Raises on unrecoverable error — caller catches and halts.
        """
        config = self._config
        page = self._browser.page

        # 1. Fetch book info page
        page.goto(url, wait_until='domcontentloaded')
        time.sleep(5)
        html = page.content()

        # 2. Ownership check
        if not check_ownership(html):
            logger.warning('Not owned — skipping (not added to done.txt): %s', url)
            return {'display_name': url, 'skipped': True, 'skip_reason': 'not owned', 'url': url}

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
        multi_collection = False
        if not is_cover:
            session = self._make_requests_session()
            raw_series, volume_number, _ = detect_series(html, url, session)
            if raw_series == '__multi_collection__':
                multi_collection = True
                volume_number = None
            elif raw_series:
                series_name = raw_series
                # Fakku's volume list is sometimes incomplete — the book isn't listed
                # in its own series page, so detect_series() defaults to vol.1.
                # If the title heuristic agrees on the series name but suggests a
                # higher volume, trust the title over the broken Fakku list.
                inferred = infer_series_from_title(title)
                if (
                    inferred
                    and inferred[0].lower() == series_name.lower()
                    and inferred[1] > volume_number
                ):
                    logger.info(
                        'Title heuristic overrides Fakku vol.%d -> vol.%d for series "%s"',
                        volume_number, inferred[1], series_name,
                    )
                    volume_number = inferred[1]
                short_title = compute_short_title(title, series_name)
            else:
                inferred = infer_series_from_title(title)
                if inferred:
                    series_name, volume_number = inferred
                    short_title = compute_short_title(title, series_name)
                    logger.info(
                        'Title heuristic: inferred series "%s" vol.%d from title "%s"',
                        series_name, volume_number, title,
                    )

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
            multi_collection=multi_collection,
        )

        progress = f' - <{idx}/{total}>' if total else ''
        logger.info('Downloading: %s (%d pages)%s', book.display_name(), pages, progress)

        # 7. Retroactive one-shot → series move, then missing-volume check.
        rel_dir = route_book(book)
        series_dir_abs = self._resolve_dir(rel_dir)
        series_dir_created = not series_dir_abs.exists()
        oneshot_move = None
        missing_vol_nums: list[int] = []

        if book.is_series() and book.volume_number and book.volume_number >= 2:
            series_safe = replace_illegal(series_name).lower()

            # Try to rescue vol.1 from %%%OneShots%%% if it's not yet in the series dir.
            vol1_present = series_dir_abs.exists() and any(
                f.name.lower().startswith(series_safe + ' vol.1')
                for f in list(series_dir_abs.glob('*.cbz')) + list(series_dir_abs.glob('*.zip'))
            )
            if not vol1_present:
                oneshot_move = check_and_move_oneshot(
                    series_name, author, series_name, config.storage_primary, str(rel_dir)
                )

            # After the rescue attempt, verify every preceding volume is in the series dir.
            # series_dir_abs may now exist (created by the move above), so re-check.
            for k in range(1, book.volume_number):
                k_present = series_dir_abs.exists() and any(
                    f.name.lower().startswith(f'{series_safe} vol.{k}')
                    for f in list(series_dir_abs.glob('*.cbz')) + list(series_dir_abs.glob('*.zip'))
                )
                if not k_present:
                    missing_vol_nums.append(k)

            if missing_vol_nums:
                logger.warning(
                    '"%s": vol.%s not found — routing to "TO FIX MANUALLY".',
                    series_name,
                    ', '.join(str(k) for k in missing_vol_nums),
                )
                book.missing_volumes = True
                rel_dir = route_book(book)  # re-routes to TO FIX MANUALLY
                series_dir_abs = self._resolve_dir(rel_dir)
                series_dir_created = not series_dir_abs.exists()

        # 8. Create temp directory for this book's pages
        temp_dir = str(Path(config.temp_dir) / _safe_dirname(title))
        create_folder_if_missing(temp_dir)

        # 9. Screenshot each page (skip pages already captured in a previous run)
        page_retries = 0
        for page_num in range(1, pages + 1):
            dest = str(Path(temp_dir) / f'{page_num}.png')
            if self._page_already_done(dest):
                logger.info('Page %d/%d (cached)', page_num, pages)
                continue
            logger.info('Page %d/%d', page_num, pages)
            try:
                page_retries += self.download_page(url, dest, page_num)
            except EndOfBook as e:
                logger.warning(
                    '"%s": metadata claimed %d pages but reader stopped at page %d — adjusting.',
                    title, pages, e.actual_page,
                )
                pages = e.actual_page
                book.pages = pages
                break

        # 10. Page count validation (all pages must be present)
        actual = len(list(Path(temp_dir).glob('*.png')))
        if actual < pages:
            raise ValueError(
                f'Page count mismatch for "{title}": expected {pages}, got {actual}'
            )

        # 11. Pack CBZ
        filename = build_filename(book)
        create_folder_if_missing(series_dir_abs)
        cbz_path = str(Path(series_dir_abs) / filename)

        conflicting_path = None
        zip_alt = Path(cbz_path).with_suffix('.zip')
        if Path(cbz_path).exists():
            conflicting_path = cbz_path
        elif zip_alt.exists():
            conflicting_path = str(zip_alt)
        if conflicting_path:
            logger.warning(
                'File already exists at "%s" — routing to "TO FIX MANUALLY".', conflicting_path
            )
            book.file_conflict = True
            rel_dir = route_book(book)
            series_dir_abs = self._resolve_dir(rel_dir)
            cbz_path = str(Path(series_dir_abs) / filename)

        pack_cbz(temp_dir, cbz_path, book)

        # 12. Clean up temp directory
        shutil.rmtree(temp_dir, ignore_errors=True)

        # 13. Mark done
        append_done(config.done_file, url)
        self._done.add(normalise_url(url))

        if page_retries:
            logger.info('Page retries for "%s": %d', title, page_retries)
        logger.info('Done: %s -> %s', book.display_name(), cbz_path)
        if multi_collection:
            routing = 'multi_collection'
        elif book.missing_volumes:
            routing = 'missing_volumes'
        elif book.file_conflict:
            routing = 'file_conflict'
        elif is_cover:
            routing = 'cover'
        elif series_name:
            routing = 'series'
        else:
            routing = 'oneshot'

        return {
            'display_name': book.display_name(),
            'skipped': False,
            'url': normalise_url(url),
            'title': title,
            'author': author,
            'pages': pages,
            'routing': routing,
            'series_name': series_name,
            'volume_number': volume_number,
            'missing_vol_nums': missing_vol_nums,
            'series_dir': str(rel_dir),
            'series_dir_created': series_dir_created if series_name else None,
            'cbz_filename': filename,
            'cbz_path': cbz_path,
            'oneshot_move': oneshot_move,
            'conflicting_path': conflicting_path,
            'page_retries': page_retries,
        }

    def download_page(self, book_url: str, dest: str, page_num: int) -> int:
        """
        Screenshot a single reader page with retry logic.
        Attempts up to config.max_retry times with exponential backoff.
        Returns the number of failed attempts before success (0 = first try worked).
        """
        config = self._config
        page = self._browser.page
        delays = [2, 4, 8]
        failed = 0

        for attempt in range(config.max_retry):
            try:
                reader_url = f'{book_url}/read/page/{page_num}'
                page.goto(reader_url, wait_until='domcontentloaded')

                # Soft-ban check — detect redirect away from this book's reader entirely.
                # The SPA may normalize /read/page/1 → /read, so allow both forms;
                # only flag a redirect that leaves the book's URL namespace.
                book_base = book_url.rstrip('/')
                if f'{book_base}/read' not in page.url:
                    raise RuntimeError(
                        f'Unexpected redirect to {page.url} — possible soft ban'
                    )

                # End-of-book check — FAKKU redirects out-of-range pages to /read/page/end.
                if page.url.rstrip('/').endswith('/read/page/end'):
                    raise EndOfBook(page_num, page_num - 1)

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

                # Resize the viewport to the canvas dimensions before sleeping.
                # V1 did this on every page; the changing window size looks like
                # real browser behaviour and avoids a static-viewport fingerprint.
                canvas_idx = max(0, (layers or 1) - 2)

                # End-of-book check — if the reader rendered no canvases after PageView
                # appeared, the metadata page count is wrong and we've passed the real end.
                canvas_count = page.evaluate(
                    """() => {
                        const iframe = document.querySelector('iframe[title="FAKKU Reader"]');
                        if (!iframe || !iframe.contentDocument) return -1;
                        return iframe.contentDocument.getElementsByTagName('canvas').length;
                    }"""
                )
                if canvas_count == 0:
                    raise EndOfBook(page_num, page_num - 1)
                canvas_dims = page.evaluate(
                    f"""() => {{
                        const iframe = document.querySelector('iframe[title="FAKKU Reader"]');
                        if (!iframe || !iframe.contentDocument) return null;
                        const c = iframe.contentDocument.getElementsByTagName('canvas')[{canvas_idx}];
                        return c ? {{width: c.width, height: c.height}} : null;
                    }}"""
                )
                if canvas_dims and canvas_dims.get('width') and canvas_dims.get('height'):
                    offset = self._browser.get_chrome_offset()
                    page.set_viewport_size({
                        'width': canvas_dims['width'],
                        'height': canvas_dims['height'] + offset,
                    })

                # Wait for the page to fully render before capturing.
                # Jitter avoids perfectly mechanical timing that rate limiters flag.
                time.sleep(config.page_wait + random.uniform(0, 3))

                # Screenshot the canvas element directly — this captures only the
                # manga page pixels with no reader UI padding or surrounding whitespace.
                canvas_locator = frame_locator.locator('canvas').nth(canvas_idx)
                canvas_locator.screenshot(path=dest)

                # Validate file size
                size_kb = Path(dest).stat().st_size / 1024
                if size_kb < config.min_image_size_kb:
                    raise ValueError(
                        f'Screenshot too small ({size_kb:.1f} KB < {config.min_image_size_kb} KB)'
                    )

                # Validate image dimensions against allowlist (if configured)
                if config.allowed_image_dimensions:
                    w, h = _png_dimensions(dest)
                    if (w, h) not in config.allowed_image_dimensions:
                        allowed = ', '.join(f'{aw}x{ah}' for aw, ah in config.allowed_image_dimensions)
                        raise ValueError(
                            f'Unexpected screenshot dimensions {w}x{h} '
                            f'(allowed: {allowed})'
                        )

                return failed

            except EndOfBook:
                raise
            except Exception as e:
                failed += 1
                logger.warning('Page %d attempt %d/%d failed: %s', page_num, attempt + 1, config.max_retry, e)
                if attempt < len(delays):
                    time.sleep(delays[attempt])
                if attempt == config.max_retry - 1:
                    raise

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_dir(self, rel_dir: str) -> Path:
        """Resolve a route_book() result to an absolute directory path."""
        if rel_dir == TO_FIX_MANUALLY:
            return Path(self._config.to_fix_manually_dir)
        return Path(self._config.storage_primary) / rel_dir

    def _page_already_done(self, path: str) -> bool:
        """Return True if path exists, meets the min size, and (if configured) has valid dimensions."""
        config = self._config
        p = Path(path)
        if not p.exists():
            return False
        if p.stat().st_size / 1024 < config.min_image_size_kb:
            return False
        if config.allowed_image_dimensions:
            try:
                w, h = _png_dimensions(path)
                if (w, h) not in config.allowed_image_dimensions:
                    return False
            except Exception:
                return False
        return True

    def _make_requests_session(self) -> requests.Session:
        """Build a requests.Session with current browser cookies."""
        session = requests.Session()
        for c in self._browser.get_cookies():
            session.cookies.set(c['name'], c['value'], domain=c.get('domain', ''))
        return session

    def dry_run_book(self, url: str, idx: int = 0, total: int = 0) -> dict:
        """
        Simulate the full per-book flow without downloading or writing any files.
        Performs steps 1-7 (fetch, ownership, metadata, series detection, routing,
        oneshot-move check, missing-volume check, file-conflict check) identically
        to download_book, but skips screenshots, CBZ packing, and done.txt updates.
        """
        config = self._config
        page = self._browser.page

        # 1. Fetch book info page
        page.goto(url, wait_until='domcontentloaded')
        time.sleep(5)
        html = page.content()

        # 2. Ownership check
        if not check_ownership(html):
            logger.info('[DRY RUN] Not owned — would skip: %s', url)
            return {'display_name': url, 'skipped': True, 'skip_reason': 'not owned', 'url': url}

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
        multi_collection = False
        if not is_cover:
            session = self._make_requests_session()
            raw_series, volume_number, _ = detect_series(html, url, session)
            if raw_series == '__multi_collection__':
                multi_collection = True
                volume_number = None
            elif raw_series:
                series_name = raw_series
                inferred = infer_series_from_title(title)
                if (
                    inferred
                    and inferred[0].lower() == series_name.lower()
                    and inferred[1] > volume_number
                ):
                    logger.info(
                        '[DRY RUN] Title heuristic overrides Fakku vol.%d -> vol.%d for series "%s"',
                        volume_number, inferred[1], series_name,
                    )
                    volume_number = inferred[1]
                short_title = compute_short_title(title, series_name)
            else:
                inferred = infer_series_from_title(title)
                if inferred:
                    series_name, volume_number = inferred
                    short_title = compute_short_title(title, series_name)
                    logger.info(
                        '[DRY RUN] Title heuristic: inferred series "%s" vol.%d from title "%s"',
                        series_name, volume_number, title,
                    )

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
            multi_collection=multi_collection,
        )

        progress = f' - <{idx}/{total}>' if total else ''
        logger.info('[DRY RUN] Processing: %s (%d pages)%s', book.display_name(), pages, progress)

        # 7. Retroactive oneshot move check + missing-volume check (read-only)
        rel_dir = route_book(book)
        series_dir_abs = self._resolve_dir(rel_dir)
        series_dir_created = not series_dir_abs.exists()
        oneshot_move = None
        missing_vol_nums: list[int] = []

        if book.is_series() and book.volume_number and book.volume_number >= 2:
            series_safe = replace_illegal(series_name).lower()

            vol1_present = series_dir_abs.exists() and any(
                f.name.lower().startswith(series_safe + ' vol.1')
                for f in list(series_dir_abs.glob('*.cbz')) + list(series_dir_abs.glob('*.zip'))
            )
            if not vol1_present:
                oneshot_move = check_and_move_oneshot(
                    series_name, author, series_name, config.storage_primary, str(rel_dir),
                    dry_run=True,
                )

            for k in range(1, book.volume_number):
                k_present = series_dir_abs.exists() and any(
                    f.name.lower().startswith(f'{series_safe} vol.{k}')
                    for f in list(series_dir_abs.glob('*.cbz')) + list(series_dir_abs.glob('*.zip'))
                )
                if not k_present:
                    missing_vol_nums.append(k)

            if missing_vol_nums:
                book.missing_volumes = True
                rel_dir = route_book(book)
                series_dir_abs = self._resolve_dir(rel_dir)
                series_dir_created = not series_dir_abs.exists()

        # File conflict check
        filename = build_filename(book)
        cbz_path = str(Path(series_dir_abs) / filename)
        conflicting_path = None
        zip_alt = Path(cbz_path).with_suffix('.zip')
        if Path(cbz_path).exists():
            conflicting_path = cbz_path
        elif zip_alt.exists():
            conflicting_path = str(zip_alt)
        if conflicting_path:
            book.file_conflict = True
            rel_dir = route_book(book)
            series_dir_abs = self._resolve_dir(rel_dir)
            cbz_path = str(Path(series_dir_abs) / filename)

        # Steps 8-13 skipped (no screenshots, no CBZ, no done.txt update)
        # Create an empty placeholder so folder structure is visible for inspection.
        # Not added to done.txt — delete these manually after reviewing.
        placeholder = Path(cbz_path)
        if not placeholder.exists():
            placeholder.parent.mkdir(parents=True, exist_ok=True)
            placeholder.write_bytes(b'')

        if multi_collection:
            routing = 'multi_collection'
        elif book.missing_volumes:
            routing = 'missing_volumes'
        elif book.file_conflict:
            routing = 'file_conflict'
        elif is_cover:
            routing = 'cover'
        elif series_name:
            routing = 'series'
        else:
            routing = 'oneshot'

        label = routing.upper().replace('_', ' ')
        logger.info('[DRY RUN] [%-18s] %s -> %s', label, book.display_name(), cbz_path)
        if missing_vol_nums:
            vols = ', '.join(f'vol.{k}' for k in missing_vol_nums)
            logger.warning('[DRY RUN]   Missing preceding volumes: %s', vols)
        if conflicting_path:
            logger.warning('[DRY RUN]   File conflict with: %s', conflicting_path)
        if oneshot_move:
            logger.info('[DRY RUN]   Would move vol.1: %s -> %s',
                        oneshot_move['from'], oneshot_move['to'])

        return {
            'display_name': book.display_name(),
            'skipped': False,
            'url': normalise_url(url),
            'title': title,
            'author': author,
            'pages': pages,
            'routing': routing,
            'series_name': series_name,
            'volume_number': volume_number,
            'missing_vol_nums': missing_vol_nums,
            'series_dir': str(rel_dir),
            'series_dir_created': series_dir_created if series_name else None,
            'cbz_filename': filename,
            'cbz_path': cbz_path,
            'oneshot_move': oneshot_move,
            'conflicting_path': conflicting_path,
        }

    def _reconcile_missing_volumes(self, reports: list[dict], dry_run: bool = False) -> int:
        """
        One reconciliation pass over reports where routing == 'missing_volumes'.
        For each such book, checks whether every missing preceding volume is now
        present in the correct series folder (on disk in normal mode; via this
        run's reports in dry-run mode).  If all are present, moves the CBZ from
        TO FIX MANUALLY/ to the series folder and updates the report in-place.

        Returns the number of files moved (or would-be moved in dry-run).
        The caller should loop until the return value is 0.
        """
        config = self._config
        moved = 0

        for report in reports:
            if report.get('routing') != 'missing_volumes':
                continue

            series_name = report.get('series_name')
            author = report.get('author', '')
            missing_vol_nums = report.get('missing_vol_nums', [])
            cbz_filename = report.get('cbz_filename')

            if not series_name or not cbz_filename or not missing_vol_nums:
                continue

            # Correct series folder: what route_book would return with no flags set
            letter = first_letter(series_name)
            author_tag = f' [{author}]' if author else ''
            correct_rel_dir = str(Path(letter) / replace_illegal(f'{series_name}{author_tag}'))
            series_dir_abs = Path(config.storage_primary) / correct_rel_dir
            series_safe = replace_illegal(series_name).lower()

            def vol_present(k: int) -> bool:
                # Check filesystem — covers both previous runs and current run (normal mode)
                if series_dir_abs.exists() and any(
                    f.name.lower().startswith(f'{series_safe} vol.{k}')
                    for f in series_dir_abs.glob('*.cbz')
                ):
                    return True
                # In dry-run no files are written; check this run's reports instead.
                # A volume reconciled in an earlier pass of the loop will already
                # have routing == 'series', so it counts here too.
                if dry_run:
                    return any(
                        r.get('series_name') == series_name
                        and r.get('volume_number') == k
                        and r.get('routing') == 'series'
                        for r in reports
                    )
                return False

            if not all(vol_present(k) for k in missing_vol_nums):
                continue

            src = Path(config.to_fix_manually_dir) / cbz_filename
            dest = series_dir_abs / cbz_filename

            if src.exists():
                series_dir_abs.mkdir(parents=True, exist_ok=True)
                src.rename(dest)
            elif not dry_run:
                logger.warning('Reconciliation: file not found at expected path: %s', src)
                continue

            prefix = '[DRY RUN] ' if dry_run else ''
            logger.info('%sReconciliation: moved "%s" -> %s', prefix, cbz_filename, correct_rel_dir)

            report['routing'] = 'series'
            report['series_dir'] = correct_rel_dir
            report['cbz_path'] = str(dest)
            moved += 1

        return moved

    def run_dry_run(self) -> tuple[list[dict], str] | None:
        """Simulate a full run: fetch queue, check each book, print plan. Nothing is written."""
        start = time.time()

        try:
            queue = self.fetch_queue()
        except SessionError:
            raise
        except PaginationError as e:
            logger.error('[DRY RUN] PaginationError: %s', e)
            return

        if not queue:
            logger.info('[DRY RUN] Nothing to download.')
            return

        logger.info('[DRY RUN] %d book(s) in queue.', len(queue))
        reports: list[dict] = []

        for i, url in enumerate(queue):
            try:
                report = self.dry_run_book(url, idx=i + 1, total=len(queue))
                reports.append(report)
            except Exception as e:
                tb = traceback.format_exc()
                logger.error('[DRY RUN] Error processing %s: %s\n%s', url, e, tb)

            if i < len(queue) - 1:
                time.sleep(self._config.book_wait)

        reconciled = 0
        while True:
            n = self._reconcile_missing_volumes(reports, dry_run=True)
            reconciled += n
            if n == 0:
                break
        if reconciled:
            logger.info('[DRY RUN] Reconciliation: would move %d book(s) out of TO FIX MANUALLY.', reconciled)

        downloaded = [r for r in reports if not r.get('skipped')]
        skipped = [r for r in reports if r.get('skipped')]
        needs_attention = sum(
            1 for r in downloaded
            if r['routing'] in ('multi_collection', 'missing_volumes', 'file_conflict')
        )
        elapsed = time.strftime('%H:%M:%S', time.gmtime(time.time() - start))
        logger.info(
            '[DRY RUN] Done. %d would download (%d need attention), %d skipped. Elapsed: %s',
            len(downloaded), needs_attention, len(skipped), elapsed,
        )
        return reports, elapsed

    def run(self) -> tuple[list[dict], str] | None:
        """Main loop: fetch_queue() → download each book in order."""
        start = time.time()
        reports: list[dict] = []

        try:
            queue = self.fetch_queue()
        except SessionError:
            raise
        except PaginationError as e:
            logger.error('PaginationError: %s', e)
            notifier_module.send_error(
                self._config, self._config.fakku_collection_url, None, str(e), ''
            )
            return

        if not queue:
            logger.info('Nothing to download.')
            return

        for i, url in enumerate(queue):
            try:
                report = self.download_book(url, idx=i + 1, total=len(queue))
                reports.append(report)
            except Exception as e:
                tb = traceback.format_exc()
                logger.error('Error downloading %s: %s\n%s', url, e, tb)
                notifier_module.send_error(self._config, url, None, str(e), tb, reports=reports)
                return  # halt on first failure

            if i < len(queue) - 1:
                logger.info('Waiting %gs before next book...', self._config.book_wait)
                time.sleep(self._config.book_wait + random.uniform(0, 10))

        reconciled = 0
        while True:
            n = self._reconcile_missing_volumes(reports)
            reconciled += n
            if n == 0:
                break
        if reconciled:
            logger.info('Reconciliation: moved %d book(s) out of TO FIX MANUALLY.', reconciled)

        elapsed = time.strftime('%H:%M:%S', time.gmtime(time.time() - start))
        logger.info('Run complete. %d book(s). Elapsed: %s', len(reports), elapsed)
        return reports, elapsed


def _safe_dirname(name: str) -> str:
    """Sanitize a title string for use as a temp directory name."""
    return re.sub(r'[\\/*?:"<>|]', '_', name)[:100]


def _png_dimensions(path: str) -> tuple[int, int]:
    """Read width and height from a PNG file header (no extra dependencies)."""
    with open(path, 'rb') as f:
        f.read(8)   # PNG signature
        f.read(4)   # IHDR chunk length
        f.read(4)   # 'IHDR'
        width = struct.unpack('>I', f.read(4))[0]
        height = struct.unpack('>I', f.read(4))[0]
    return width, height
