# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

FakkuDownloader V2 is a Playwright-based headless scraper that downloads manga/doujin content from Fakku.net. It authenticates with TOTP 2FA, navigates a FAKKU collection, screenshots each page of the reader's `<canvas>` element, and packages the result as a CBZ file with ComicInfo.xml metadata.

Deployed as a Kubernetes CronJob (`k8s/cronjob.yaml`) running nightly at 02:45 UTC. Config is supplied entirely via environment variables; local development uses a `.env` file.

## Commands

```bash
# Install dependencies (Python 3.12)
pip install -r requirements.txt
playwright install chromium --with-deps   # local dev
playwright install chrome --with-deps     # production (matches Dockerfile)

# Normal run
python main.py

# Dry run — steps 1–7 only (no screenshots/CBZ/done.txt), sends [DRY RUN] email
python main.py --dry-run

# Run all tests
pytest

# Run a single test file / specific test
pytest tests/test_organizer.py
pytest tests/test_organizer.py::TestRouteBook::test_series_routing

# Docker (local dev)
docker compose run --rm downloader       # full run
docker compose run --rm dry-run          # dry run
```

## Configuration

All config is loaded by `config.py:load_config()` into a `Config` dataclass. Missing required vars cause a fast-fail with a clear message. When `TO_PLACE_ONLY=true` the FAKKU auth/queue vars are not required.

| Env var | Default | Notes |
|---|---|---|
| `FAKKU_USERNAME` | required | Account login |
| `FAKKU_PASSWORD` | required | Account password |
| `FAKKU_TOTP_SECRET` | required | Base32 TOTP seed |
| `FAKKU_COLLECTION_URL` | required | Paginated collection URL to scrape |
| `STORAGE_ROOT` | required | Parent directory (contains `ToPlace/`, `TO FIX MANUALLY/`) |
| `STORAGE_PRIMARY` | required | Fakku library root (e.g. `STORAGE_ROOT/Fakku`) |
| `DONE_FILE` | `./queue/done.txt` | One normalised URL per line |
| `COOKIES_FILE` | `./queue/cookies.pickle` | Playwright cookies pickle |
| `TEMP_DIR` | `./queue/tmp` | Per-book screenshot staging area |
| `SMTP_HOST` | required | — |
| `SMTP_PORT` | `587` | — |
| `SMTP_USER` | required | — |
| `SMTP_PASSWORD` | required | — |
| `SMTP_FROM` | required | — |
| `SMTP_TO` | required | — |
| `PAGE_TIMEOUT` | `15` | Seconds to wait for page/canvas load |
| `PAGE_WAIT` | `15` | Seconds to sleep after each page screenshot (soft-ban guard) |
| `BOOK_WAIT` | `30` | Seconds to sleep between books |
| `MIN_IMAGE_SIZE_KB` | `50` | Screenshots below this size are rejected and retried |
| `ALLOWED_IMAGE_DIMENSIONS` | `""` | Comma-separated `WxH` allowlist (e.g. `1360x1920,1337x1920`); empty = any size |
| `MAX_RETRY` | `3` | Retry attempts per page (exponential backoff: 2s, 4s, 8s) |
| `CHROME_OFFSET` | `None` | Browser chrome height in px. `None` = auto-detect once and cache. |
| `TO_PLACE_ONLY` | `false` | Skip Phase 1 (FAKKU downloads); run only Phase 2 (ToPlace) |

## Storage Layout

```
STORAGE_ROOT/
├── ToPlace/              ← drop zone; Placer scans this each run
├── TO FIX MANUALLY/      ← routing destination for problem books
└── Fakku/                ← STORAGE_PRIMARY
    ├── A/
    │   └── Series Name/
    │       └── Series Name vol.01 [Author].cbz
    ├── %%%OneShots%%%/
    │   └── A/
    │       └── Title [Author].cbz
    └── Covers/
        └── Cover Group Name/
            └── Cover Group Name #42 [Author].cbz
```

`TEMP_DIR` holds per-book subdirectories during download (e.g. `tmp/Book Title/1.png`). If a run is interrupted, the temp directory is left on disk and the next run **resumes automatically** from the last successfully captured page (`_page_already_done` checks for the file's existence before re-shooting). Temp dirs are only deleted after a successful CBZ pack.

## Architecture

### Entry & configuration
- **`main.py`** — two-phase execution:
  1. **Phase 1** (FAKKU downloads): Browser → auth → `Downloader.run()` / `.run_dry_run()`. Skipped when `TO_PLACE_ONLY=true`.
  2. **Phase 2** (ToPlace processing): `Placer.run()` — no browser needed. Always runs.
  Combined email sent at end if either phase produced reports.
- **`config.py`** — `load_config()` reads all env vars and returns a `Config` dataclass.

### Browser layer
- **`browser.py`** — `Browser` wraps the Playwright context:
  - Must set `user_agent` explicitly to a non-headless Chrome UA — FAKKU's server detects `HeadlessChrome` and returns a stripped page (~33KB, no books) instead of the full SSR page (~30KB with books).
  - `_setup_localstorage()` sets `fakku-scrollWheelPageChange=false` after context creation.
  - `get_chrome_offset()` auto-detects browser chrome height once per run and caches it; used to set viewport to exactly the canvas dimensions on each reader page.

### Authentication
- **`auth.py`** — `ensure_authenticated()` loads cookies from pickle, checks `fakku_otpa` expiry, then validates the session by navigating to `/account`. If anything fails it runs a full TOTP login (`pyotp`) and saves fresh cookies.

### Queue & download orchestration
- **`downloader.py`** — `Downloader` class:
  - `fetch_queue()` paginates the collection page and returns URLs not already in `done.txt`. Waits for `div.flex.mt-3` before reading `page.content()`. Logs `html_len` — expected ~30KB for a valid session.
  - `download_book()` — 13-step per-book flow (see below).
  - `dry_run_book()` mirrors steps 1–7 (no screenshots/CBZ/done.txt write).
  - `_make_requests_session()` — builds a `requests.Session` carrying the current browser cookies; passed to `detect_series()` for possible future HTTP requests (currently unused there).

### Organisation & file naming
- **`organizer.py`** — all routing, naming, and file-system logic:
  - `check_ownership(html)` — looks for a `/read` link; books not purchased are skipped.
  - `extract_metadata(html)` — title, author, page count, tags via BeautifulSoup/lxml.
  - `detect_series(html, url, session)` — parses the embedded "This chapter is part of" block on the info page (no extra HTTP request needed). Returns `(series_name, volume_number, short_title)` or the `'__multi_collection__'` sentinel when a book belongs to more than one FAKKU collection simultaneously.
  - `infer_series_from_title(title)` — heuristic fallback when FAKKU reports no series. Matches: `Title Part/Vol/Ch N` (N≥2), `Title #N` (N≥2), `Title N` bare integer (N≥2). False positives land in `TO FIX MANUALLY` via the missing-volumes check.
  - `route_book(book)` → relative path string (see Routing Rules below).
  - `extract_cover_group(title)` — extracts a publisher/series prefix from cover titles for use as a `Covers/` subfolder. See `specs/covers.md` for the full algorithm.
  - `build_filename(book)` — produces the final CBZ filename.
  - `pack_cbz(temp_dir, dest, book)` — zips PNGs + writes ComicInfo.xml.
  - `check_and_move_oneshot(...)` — when a series vol.≥2 arrives, retroactively moves vol.1 out of `%%%OneShots%%%`. Accepts `dry_run=True`.
- **`book.py`** — `Book` dataclass:
  ```
  title, author, pages, tags, source_url
  series_name, volume_number, short_title   # all None if one-shot
  is_cover        # set by downloader: pages <= 4
  multi_collection, missing_volumes, file_conflict  # routing flags, set before route_book()
  ```

### ToPlace pipeline
- **`placer.py`** — `Placer` class. Scans `ToPlace/` for `.zip`/`.cbz` archives. Per-file flow:
  1. Fix filename via `process_filename()`, rename in-place.
  2. Count image files inside the archive (png/jpg/jpeg/webp/gif/bmp).
  3. Set `is_cover = (pages <= 4)`.
  4. Run `infer_series_from_title()` for series detection (no HTML/browser available).
  5. Build `Book`, call `check_and_move_oneshot()`, check missing volumes, call `route_book()`.
  6. Check for file conflict at destination.
  7. Move file to destination (skipped in dry-run).
- **`fix_names.py`** — `process_filename(filename)` → `(new_filename, title, author)`. Parses the convention `[Circle (Author)] Title [Extra] (Tag).zip` → `Title [Author].cbz`. Algorithm: strip extension → replace `_` with space → extract author from leading `[Group (Author)]` inner parens or leading `[Author]`/`(Author)` → remove all remaining `[…]`/`(…)` groups → output `Title [Author].cbz`. Non-leading brackets are treated as extra tags and stripped.

### Notifications
- **`notifier.py`** — HTML email (`MIMEMultipart('alternative')` with plain-text fallback).
  - `send_success(config, reports, elapsed, dry_run=False)` — summary badges + one card per book.
  - `send_error(config, url, page, error, trace, reports=None)` — red header + traceback.
  - Card border colour: green = series, blue = oneshot, grey = cover, red = needs-attention (`TO FIX MANUALLY`).

## Per-Book Download Flow (`download_book`, 13 steps)

1. Fetch info page HTML via `page.goto(url)`.
2. Ownership check — skip if no `/read` link.
3. Extract metadata (title, author, pages, tags).
4. Set `is_cover = (pages <= 4)`.
5. Series detection: `detect_series()` on the info page HTML, then `infer_series_from_title()` as fallback. `infer_series_from_title` wins on volume number if it produces a higher value.
6. Build `Book` dataclass.
7. Route: `route_book()`, then check missing preceding volumes (routes to `TO FIX MANUALLY` if gaps found), retroactively move vol.1 from OneShots if vol.≥2 just arrived.
8. Create `TEMP_DIR/<sanitized_title>/`.
9. Screenshot pages 1–N. Each call to `download_page()`:
   - Navigates to `{book_url}/read/page/{N}`, checks for soft-ban redirect.
   - Raises `EndOfBook` only on URL redirect to `/read/page/end` (authoritative server signal). Canvas-count failures raise retryable `ValueError` instead.
   - Removes top overlay layer via JS, resizes viewport to exact canvas dimensions (anti-fingerprint), sleeps `PAGE_WAIT + jitter`, screenshots the canvas element directly.
   - `canvas_idx = max(0, layers - 2)` where `layers` is the `.layer` element count before removal (0 or 1 → idx=0 single page; 2 → idx=0; 3 → idx=1 spread).
   - Validates file size ≥ `MIN_IMAGE_SIZE_KB` and optionally dimensions against `ALLOWED_IMAGE_DIMENSIONS`.
   - Already-captured pages (file exists) are skipped — **partial runs resume automatically**.
10. Validate that all expected PNGs are present.
11. Pack CBZ: zip PNGs + write `ComicInfo.xml`.
12. Delete temp directory.
13. Append URL to `done.txt`.

## Routing Rules (`route_book`)

Priority order (first match wins):

| Condition | Destination |
|---|---|
| `multi_collection` or `missing_volumes` or `file_conflict` | `TO FIX MANUALLY/<series or title>/` |
| `is_cover` (pages ≤ 4) | `Covers/<extract_cover_group(title)>/` |
| `series_name` is set | `<first_letter>/<series_name>/` |
| one-shot | `%%%OneShots%%%/<first_letter>/` |

All paths are relative to `STORAGE_PRIMARY`. `TO FIX MANUALLY` is relative to `STORAGE_ROOT`.

## Key Maintenance Points

**Selector drift** — FAKKU's HTML changes frequently. Most fragile selectors: `extract_metadata` in `organizer.py` (title/author/pages/tags), `fetch_queue` in `downloader.py` (`div.flex.mt-3` and the book URL pattern). When books stop being found or metadata is wrong, check these first.

**Collection page size diagnostic** — `fetch_queue` logs `html_len`. ~30KB = valid session. ~33KB = broken session or wrong UA (FAKKU serves a stripped page).

**User-Agent is critical** — Never remove the explicit `user_agent` from `Browser.start()`. Without it Playwright reports `HeadlessChrome` and FAKKU serves the stripped page.

**`channel='chrome'`** — Production uses `channel='chrome'` (real Chrome binary). The Dockerfile installs it via `playwright install chrome --with-deps`. Chrome needs a writable `$HOME` for crashpad — solved by `ENV HOME=/tmp`. The k8s pod runs as `runAsUser: 1000`.

**`detect_series` multi-collection sentinel** — When a book belongs to more than one FAKKU collection simultaneously, `detect_series` returns `'__multi_collection__'` as `series_name`. The downloader sets `book.multi_collection = True` and re-routes to `TO FIX MANUALLY`. Do not change this sentinel string without updating `downloader.py`.

**`infer_series_from_title` false positives** — Any book whose title ends with a number ≥ 2 will be treated as vol.N of a series. The missing-volumes check then catches false positives and routes them to `TO FIX MANUALLY` for manual review.

**Timing** — All sleeps use `random.uniform` jitter. `PAGE_WAIT` and `BOOK_WAIT` in `k8s/cronjob.yaml` override code defaults — keep them in sync when changing defaults.

**Cookie format** — Cookies are pickled in Playwright format (`expires` key). Do not mix with Selenium-format cookies (`expiry` key).

**`TO_PLACE_ONLY` mode** — Skips Phase 1 entirely. Only `STORAGE_ROOT`, `STORAGE_PRIMARY`, and SMTP vars are required.

**`requests.Session` in `detect_series`** — The session is built from browser cookies and passed through for potential future HTTP requests, but `detect_series` currently parses only the HTML already fetched by the browser (the volume list is embedded on the info page).
