# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

FakkuDownloader is a Python Selenium-based scraper that downloads manga/doujin content from Fakku.net. It authenticates via stored cookies, navigates the JavaScript reader, and saves each page as a PNG screenshot.

## Running the Downloader

```bash
# Install dependencies
pip install -r requirements.txt

# Run with defaults (reads from urls/urls.txt, tracks completed in urls/done.txt)
python main.py

# Custom options
python main.py -u custom_urls.txt -d custom_done.txt -t 15 -w 8
```

CLI flags: `-u/--url` (URLs file), `-d/--done` (completed URLs file), `-c/--cookies` (cookies pickle), `-t/--timeout` (page load timeout, default 10s), `-w/--wait` (wait between pages, default 5s).

**First run** opens a non-headless browser for manual login; cookies are saved to `cookies.pickle` for subsequent headless runs.

## Architecture

- **`main.py`** — Entry point. Parses args, initializes `Browser` and `Downloader`, triggers authentication.
- **`browser.py`** — `Browser` class wrapping Selenium WebDriver (Chrome or Firefox). Handles cookie persistence (pickle), headless toggle, window sizing, and localStorage setup for the reader.
- **`downloader.py`** — `Downloader` class. Core loop: reads `urls.txt`, skips URLs already in `done.txt`, extracts book metadata via BeautifulSoup, then iterates pages — switching to the reader iframe, waiting for canvas elements, executing JS to remove overlays and resize the window, then taking a screenshot per page.
- **`book.py`** — `Book` dataclass holding title, author, page count, and `has_multiple` flag. `get_formatted_title()` produces a filesystem-safe directory name.
- **`helper.py`** — File/folder creation, URL list I/O, illegal character stripping from filenames, cookie expiry display.
- **`utils.py`** — All constants: URLs, file paths, timeouts, display resolution (`MAX_DISPLAY = [1440, 2560]`), driver path.

## Key Workflows

**Single book download:** URL → extract metadata (title, author, pages via BeautifulSoup) → create `downloaded/single/<title>/` → screenshot each page via `reader/<title>/page/<N>`.

**Collection download:** If URL contains `"collections"`, `handle_collection()` paginates through the collection page, extracts individual book URLs, and processes each as a single book (saved to `downloaded/multiple/`).

**Resume:** The `done.txt` file tracks completed URLs. Re-running skips already-finished downloads.

## Maintenance Notes

The website HTML structure changes frequently — most bug fixes involve updating BeautifulSoup CSS/attribute selectors in `downloader.py` for title, author, and page count extraction.

Selenium uses legacy `find_element_by_*` style calls (Selenium 3.x API); if upgrading to Selenium 4, these need updating to `find_element(By.*, ...)`.

The ChromeDriver (`chromedriver.exe`) must match the installed Chrome browser version exactly.
