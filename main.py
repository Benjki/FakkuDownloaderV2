"""Entry point for FakkuDownloader."""

import argparse
import logging
import re
import sys
import traceback
from pathlib import Path

import requests
from bs4 import BeautifulSoup

import notifier as notifier_module
from auth import ensure_authenticated, load_cookies
from book import Book
from browser import Browser
from config import load_config
from downloader import Downloader
from helper import load_done_file, normalise_url
from organizer import (
    build_filename,
    check_ownership,
    compute_short_title,
    detect_series,
    extract_metadata,
    route_book,
)

BASE_URL = 'https://www.fakku.net'


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='FakkuDownloader — headless manga scraper powered by Playwright',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Print download plan without downloading anything',
    )
    return parser.parse_args()


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )


def run_dry_run(config) -> None:
    """
    Fetch the collection + book info pages using requests (no browser needed).
    Print a plan line for each URL: [STATUS] url -> dest_path
    No files are written.
    """
    logger = logging.getLogger('dry-run')

    cookies = load_cookies(config.cookies_file)
    if not cookies:
        logger.error(
            'No cookies.pickle found. Run without --dry-run first to authenticate.'
        )
        sys.exit(1)

    session = requests.Session()
    for c in cookies:
        session.cookies.set(c['name'], c['value'], domain=c.get('domain', ''))

    done = load_done_file(config.done_file)

    # Paginate the collection
    resp = session.get(config.fakku_collection_url, timeout=20)
    soup = BeautifulSoup(resp.text, 'lxml')

    page_count = 1
    last_page_a = soup.find('a', title='Last Page')
    if last_page_a:
        m = re.search(r'/page/(\d+)', last_page_a.get('href', ''))
        if m:
            page_count = int(m.group(1))
    else:
        page_nums = [
            int(m.group(1))
            for a in soup.find_all('a', title=re.compile(r'^Page \d+$'))
            if (m := re.search(r'^Page (\d+)$', a.get('title', '')))
        ]
        if page_nums:
            page_count = max(page_nums)

    all_urls: list[str] = []
    for pg in range(1, page_count + 1):
        if pg > 1:
            resp = session.get(
                f'{config.fakku_collection_url}/page/{pg}', timeout=20
            )
            soup = BeautifulSoup(resp.text, 'lxml')
        for div in soup.find_all(
            'div', class_=lambda c: c and 'flex' in c and 'mt-3' in c
        ):
            a = div.find('a', href=True)
            if a:
                all_urls.append(normalise_url(BASE_URL + a['href']))

    for url in all_urls:
        norm = normalise_url(url)
        if norm in done:
            print(f'[SKIP/DONE]    {url}')
            continue

        try:
            resp = session.get(url, timeout=20)
            html = resp.text

            if not check_ownership(html):
                print(f'[SKIP/UNOWNED] {url}')
                continue

            meta = extract_metadata(html)
            title = meta['title']
            author = meta['author']
            pages = meta['pages']
            tags = meta['tags']

            is_cover = pages <= 4
            series_name, volume_number, short_title = None, None, None
            if not is_cover:
                series_name, volume_number, _ = detect_series(html, url, session)
                if series_name:
                    short_title = compute_short_title(title, series_name)

            book = Book(
                title=title,
                author=author,
                pages=pages,
                tags=tags,
                source_url=norm,
                series_name=series_name,
                volume_number=volume_number,
                short_title=short_title,
                is_cover=is_cover,
            )

            rel_dir = route_book(book)
            filename = build_filename(book)
            dest = str(Path(config.storage_primary) / rel_dir / filename)
            label = 'COVER' if is_cover else ('SERIES' if series_name else 'ONESHOT')
            print(f'[{label:<7}]    {url} -> {dest}')

        except Exception as e:
            print(f'[ERROR]        {url} — {e}')


def main():
    args = parse_args()
    setup_logging()
    config = load_config()

    if args.dry_run:
        run_dry_run(config)
        return

    browser = Browser(config)
    browser.start()

    try:
        ensure_authenticated(browser, config, notifier_module)
        downloader = Downloader(browser, config)
        downloader.run()
    except KeyboardInterrupt:
        logging.getLogger('main').info('Interrupted by user.')
    except Exception as e:
        tb = traceback.format_exc()
        logging.getLogger('main').error('Fatal error: %s\n%s', e, tb)
        try:
            notifier_module.send_error(config, '', None, str(e), tb)
        except Exception:
            pass
    finally:
        browser.close()


if __name__ == '__main__':
    main()
