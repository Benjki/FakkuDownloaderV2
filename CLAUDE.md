# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

FakkuDownloader V2 is a Playwright-based headless scraper that downloads manga/doujin content from Fakku.net. It authenticates with TOTP 2FA, navigates a FAKKU collection, screenshots each page of the reader's `<canvas>` element, and packages the result as a CBZ file with ComicInfo.xml metadata.

Deployed as a Kubernetes CronJob (`k8s/cronjob.yaml`) running nightly. Config is supplied entirely via environment variables; local development uses a `.env` file.

## Commands

```bash
# Install dependencies (Python 3.12)
pip install -r requirements.txt
playwright install chromium --with-deps   # or chrome --with-deps for production

# Normal run
python main.py

# Dry run — does everything except screenshots/CBZ/done.txt, sends [DRY RUN] email
python main.py --dry-run

# Run all tests
pytest

# Run a single test file / specific test
pytest tests/test_organizer.py
pytest tests/test_organizer.py::TestRouteBook::test_series_routing
```

## Architecture

### Entry & configuration
- **`main.py`** — parses `--dry-run` flag, calls `ensure_authenticated`, then `downloader.run()` or `downloader.run_dry_run()`.
- **`config.py`** — `load_config()` reads all env vars and returns a `Config` dataclass. Validates required vars at startup; fails fast with a clear message.

### Browser layer
- **`browser.py`** — `Browser` wraps the Playwright context. Key behaviours:
  - Must set `user_agent` explicitly to a non-headless Chrome UA — FAKKU's server detects `HeadlessChrome` and returns a stripped page (33KB, no books) instead of the full SSR page (47KB with books).
  - `_setup_localstorage()` runs after the context is created; sets `fakku-scrollWheelPageChange=false` which controls reader page-turn behaviour.
  - `get_chrome_offset()` auto-detects the browser chrome height once and caches it; used to size the viewport to exactly the canvas dimensions on each reader page.

### Authentication
- **`auth.py`** — `ensure_authenticated()` loads cookies from pickle, checks `fakku_otpa` expiry, then validates the session by navigating to `/account`. If anything fails it runs a full TOTP login (`pyotp`) and saves fresh cookies. Called once per run before any scraping.

### Queue & download orchestration
- **`downloader.py`** — `Downloader` class.
  - `fetch_queue()` paginates the collection page and returns URLs not already in `done.txt`. Uses `page.wait_for_selector('div.flex.mt-3')` to wait for the JavaScript-injected book list before reading `page.content()`. Logs `html_len` and div count — expected ~47KB for a valid session.
  - `download_book()` is the 13-step per-book flow: fetch info page → ownership check → extract metadata → detect series → route → screenshot every page → validate → pack CBZ → mark done.
  - `dry_run_book()` mirrors steps 1–7 (no screenshots/CBZ/done.txt write).
  - `download_page()` resizes the viewport to the canvas dimensions on each page (mirrors human behaviour, avoids static-viewport fingerprinting).

### Organisation & file naming
- **`organizer.py`** — all routing, naming, and file-system logic:
  - `check_ownership(html)` — looks for a `/read` link; books not purchased are skipped.
  - `extract_metadata(html)` — title, author, page count, tags via BeautifulSoup/lxml selectors.
  - `detect_series(html, url, session)` — checks the book's series page; returns `(series_name, volume_number, short_title)` or `'__multi_collection__'` sentinel.
  - `route_book(book)` → relative path string. Rules in priority order: `multi_collection` / `missing_volumes` / `file_conflict` → `TO FIX MANUALLY/<series>/`; `is_cover` (≤4 pages) → `Covers/<group>/`; series → `<First letter>/<Series name>/`; oneshot → `%%%OneShots%%%/<First letter>/`.
  - `extract_cover_group(title)` — extracts a publisher/series prefix from cover titles using delimiter keywords, date patterns, `#N` issue numbers, and bare integers.
  - `build_filename(book)` — produces the final CBZ filename.
  - `pack_cbz(temp_dir, dest, book)` — zips PNGs + writes ComicInfo.xml.
  - `check_and_move_oneshot(...)` — when a new series vol.≥2 arrives, retroactively moves vol.1 out of `%%%OneShots%%%`. Accepts `dry_run=True` to simulate without moving.
- **`book.py`** — `Book` dataclass. Routing flags (`is_cover`, `multi_collection`, `missing_volumes`, `file_conflict`) are set by `downloader.py` before calling `route_book()`.

### Notifications
- **`notifier.py`** — HTML email (inline CSS, `MIMEMultipart('alternative')` with plain-text fallback).
  - `send_success(config, reports, elapsed, dry_run=False)` — summary banner with coloured badge counts + one card per book.
  - `send_error(config, url, page, error, trace, reports=None)` — red header + traceback + "Completed before error" book cards when `reports` is non-empty.
  - Book card border colour indicates routing: green=series, blue=oneshot, grey=cover, red=needs-attention.

## Key maintenance points

**Selector drift** — FAKKU's HTML changes frequently. The most fragile selectors are in `organizer.py` (`extract_metadata`) and `downloader.py` (`fetch_queue`). When books stop being found or metadata is wrong, check these first.

**Collection page rendering** — The book list is JavaScript-injected. `fetch_queue` waits for `div.flex.mt-3` to appear before reading the page. If the HTML length logged is ~33KB (instead of ~47KB), the session is broken or the UA is wrong.

**User-Agent is critical** — Never remove the explicit `user_agent` from `Browser.start()`. Without it, Playwright reports `HeadlessChrome` and FAKKU serves a stripped page.

**`channel='chrome'`** — production uses the real Chrome binary (`playwright install chrome --with-deps` in the Dockerfile) for better TLS fingerprinting. Local dev also requires Chrome installed.

**Timing** — All sleeps use jitter (`random.uniform`). `PAGE_WAIT`, `BOOK_WAIT` in the k8s cronjob override the code defaults — keep them in sync when changing defaults.

**Cookie format** — Cookies are pickled in Playwright format (`expires` key, not Selenium's `expiry`). Do not mix V1 cookie files with V2.
