# FakkuDownloader — Automated Rewrite Spec

**Date:** 2026-03-01
**Status:** Draft v6 — Refining

---

## 1. Goal

Transform FakkuDownloader from a manually-triggered local Python script into a robust,
containerizable, fully automated download pipeline — deployable as a Kubernetes CronJob on a
k3s homeserver. The download queue is managed entirely through a personal FAKKU collection in
the browser, requiring no manual file editing.

---

## 2. Language Decision: Python + Playwright

**Chosen stack:** Python 3.12 + Playwright (sync API)

**Chromium launch flags (always applied):** `--no-sandbox`, `--disable-dev-shm-usage`.
These are required in Docker containers and have no meaningful downside when running locally.
Applied unconditionally — no container-detection logic needed.

**Rationale:**
- FAKKU's reader is a canvas-based, heavily JS-driven SPA. A real browser is non-negotiable —
  screenshotting the canvas is the only viable capture method.
- Playwright bundles its own browser binaries (`playwright install chromium`). No more
  chromedriver version mismatch — the main operational pain point of the current tool.
- Playwright has better auto-waiting, more reliable locators, and cleaner iframe handling than
  Selenium 3/4 for modern SPAs.
- `pyotp` (TOTP) + `playwright` + stdlib `email`/`smtplib` are all mature Python packages.
- Go + `chromedp` is viable for smaller containers, but browser automation on canvas-heavy,
  iframe-nested readers is significantly harder to maintain in Go.
- Container size (~500 MB with Playwright Chromium) is acceptable for a homeserver.
- Sync Playwright API is sufficient for sequential downloads. Async can be layered on later.

---

## 3. Delivery Phases

### Phase 1 — Local Rewrite
Fully working locally. Configuration via `.env`. No Docker required.

### Phase 2 — Containerization & k3s Deployment
Dockerfile + Kubernetes manifests on top of Phase 1 with minimal application changes.

---

## 4. Configuration

All settings via `.env` (Phase 1) or Kubernetes Secrets injected as env vars (Phase 2).
No hardcoded values in source code. `config.py` validates all required vars at startup — if
anything is missing, the tool exits immediately with a clear error message.

```
# Authentication
FAKKU_USERNAME=
FAKKU_PASSWORD=
FAKKU_TOTP_SECRET=           # Base32 TOTP seed — see Section 5.1

# Download queue
FAKKU_COLLECTION_URL=https://www.fakku.net/users/<username>/collections/to-dwnl

# Notifications (SMTP)
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
SMTP_FROM=
SMTP_TO=

# Storage
STORAGE_PRIMARY=./downloaded     # Phase 2: /storage/primary  (PVC mount)
# Tracking & state files
DONE_FILE=./done.txt
COOKIES_FILE=./cookies.pickle    # Phase 2: /app/queue/cookies.pickle (queue PVC)

# Temp directory for in-progress page screenshots
TEMP_DIR=./tmp               # Phase 2: /app/queue/tmp (queue PVC)

# Tuning
PAGE_TIMEOUT=15            # seconds before a page load is considered failed
PAGE_WAIT=5                # seconds between page requests (soft-ban guard)
MIN_IMAGE_SIZE_KB=50       # screenshots smaller than this are considered corrupt
MAX_RETRY=3                # retry attempts per page / per login step
CHROME_OFFSET=             # px added to canvas height for window resize (blank = auto-detect)
```

---

## 5. Authentication & TOTP Automation

### 5.1 TOTP Setup (one-time, by user)
Export the TOTP base32 seed from your Authenticator app and set it as `FAKKU_TOTP_SECRET`.
Most apps (Authy, 2FAS, Google Authenticator via Takeout export) can show or export this seed.
Alternatively: disable and re-enable 2FA on the FAKKU account and save the secret string shown
during QR setup.

### 5.2 Cookie Lifecycle
On every run:
- If `cookies.pickle` exists and `fakku_otpa` expires **more than 7 days from now** →
  use stored cookies headlessly.
- If cookies are missing, expired, or **≤ 7 days from expiry** → trigger auto-login.
- A proactive warning email is sent at run start if expiry is within 7 days (even if the
  cookies are still valid for this run).

### 5.3 Auto-Login Flow
Runs fully headless (works inside a container — no interactive browser needed).

1. Launch Chromium via Playwright.
2. Navigate to `https://www.fakku.net/login/`.
3. Fill `username` and `password` from config.
4. Submit the login form.
5. Wait for the TOTP input field.
6. Generate 6-digit code: `pyotp.TOTP(FAKKU_TOTP_SECRET).now()`.
7. Fill the TOTP field and submit.
8. Wait for successful login (redirect away from `/login/`).
9. Save all cookies to `cookies.pickle`.
10. Continue the download run in the same browser session.
11. Any step failure → halt immediately + send failure email.

**First-run bootstrap (k3s):** If `cookies.pickle` is absent from the PVC on the very first
CronJob execution, Steps 1–10 run automatically inside the container. No manual intervention.

---

## 6. Download Queue

### 6.1 Source: FAKKU Collection
The user maintains a personal FAKKU collection named "to-dwnl" (or equivalent). The tool reads
that collection page to build its work queue. There is no local `urls.txt` to manage — the
user adds and removes books in the FAKKU UI, and the tool picks up the queue on each run.

### 6.2 Queue Resolution at Run Start
1. Fetch `FAKKU_COLLECTION_URL`. Paginate through all pages:
   - Try the "Last Page" link selector to determine total page count.
   - If the "Last Page" selector returns nothing and the page has next-page indicators →
     **halt immediately**: pagination failure is critical (silent truncation would silently drop
     URLs from the queue). Email: "Collection pagination selector broken — queue incomplete."
   - Fallback within normal operation: if the collection fits on one page, no pagination needed.
2. Extract all book URLs from the collected pages.
3. **Normalise** each URL: strip trailing slash, lowercase scheme and host.
4. Load `DONE_FILE` and normalise its entries the same way.
5. Filter out any URL already in `DONE_FILE`.
6. If the resulting queue is empty → exit silently with code 0.
7. Process remaining URLs in order.

### 6.3 On Successful Download
Append the normalised URL to `DONE_FILE`. The FAKKU collection is not modified — it grows as
a permanent record. `DONE_FILE` is the sole source of truth for what has been downloaded.

---

## 7. Core Download Pipeline

### 7.1 Per-Book Flow
```
1.  Navigate to the book's info page.
2.  Check for ownership: look for the 'Read' button (or equivalent ownership indicator).
    If absent → halt and notify: "Book not owned or inaccessible: <url>".
3.  Extract metadata: title, author, stated page count, genre tags, series membership.
4.  Determine output path (Section 8). Create folders if needed (incl. retroactive move check).
5.  Check for a partial temp download folder (Section 7.4).
6.  For each page not yet downloaded (1 → stated_page_count):
    a. Navigate to: <book_url>/read/page/<N>
    b. Verify current URL contains /read/page/ (soft-ban check — Section 7.6).
    c. Switch to the FAKKU Reader iframe.
    d. Wait for canvas element (retry with backoff).
    e. Remove overlay layers via JS.
    f. Resize window to exact canvas dimensions + chrome offset (Section 7.5).
    g. Screenshot → save as temp PNG in TEMP_DIR/<sanitised-title>/.
    h. Validate: file size >= MIN_IMAGE_SIZE_KB. Retry if too small.
    i. Log: "<title> page N/total — <size> KB — <elapsed>ms"
7.  Validate actual page count == stated count (Section 7.3).
8.  Pack temp PNGs + ComicInfo.xml into a .cbz (Section 9).
9.  Copy .cbz to STORAGE_PRIMARY.
10. Delete temp PNG folder.
11. Append normalised URL to DONE_FILE.
```

### 7.2 Retry Logic
Wraps every page load, canvas wait, and screenshot:
- Attempt 1: immediate
- Attempt 2: wait 2 s
- Attempt 3: wait 4 s
- After MAX_RETRY failures → halt the run (Section 7.7).

### 7.3 Page Count Validation
After all pages are downloaded, verify the actual page count matches the stated count from the
book info page:
- If actual downloaded pages < stated count → halt and notify.
  Email subject: `[FakkuDL] ERROR: Page count mismatch — <book title>`
  Body: stated count, actual count, book URL.
- The incomplete temp folder is left on disk so the next run can attempt to resume.

### 7.4 Partial Download Resume
If a previous run halted mid-book, a temp PNG folder for that book exists on disk.

On the next run, when reaching that book:
1. Detect the existing temp folder.
2. Scan existing PNGs — keep those with size ≥ MIN_IMAGE_SIZE_KB.
3. Resume from the first missing or invalid page number.
4. Proceed to CBZ packing once all pages are present and valid.

Temp folder: `<TEMP_DIR>/<sanitised-book-title>/`
In Phase 2 (k3s): temp dir lives on the **queue PVC** so it survives pod restarts and
mid-book crashes. Resume picks up exactly where it left off on the next CronJob run.

### 7.5 Chrome Offset (Window Resize)

### 7.5 Chrome Offset (Window Resize)
After reading canvas dimensions via JS, the browser window must be resized to fit exactly.
The offset accounts for the browser chrome (toolbar, tab bar, etc.) above the content area.

- If `CHROME_OFFSET` is set in config: use that value (px) directly.
- If `CHROME_OFFSET` is blank or absent: auto-detect at runtime by comparing
  `window.outerHeight - window.innerHeight` via `page.evaluate()` once per run, before the
  first screenshot. Cache the result for the remainder of that run.

### 7.6 Soft-Ban / Redirect Detection
After navigating to any reader page, check that the current URL still contains `/read/page/`.
If the browser has been redirected to `/login/` or any other URL (CAPTCHA, account wall,
rate-limit page), treat it as a soft-ban:
- Log the redirect destination.
- Halt immediately.
- Send a notification email: "Soft-ban detected — redirected to `<url>` on page N of `<book>`."

The failed URL is not added to `DONE_FILE`. On the next run, the user can retry after waiting.

### 7.7 Error Handling Policy — Halt and Notify
**Any** unrecoverable error halts the entire run immediately and sends an email. No book is
skipped silently. Unrecoverable conditions:

- Login failure at any step
- Missing or invalid config values at startup
- Collection page fetch failure
- Metadata extraction failure (CSS selectors return nothing)
- Page load timeout after MAX_RETRY attempts
- Screenshot size below MIN_IMAGE_SIZE_KB after MAX_RETRY attempts
- CBZ creation or validation failure
- File system errors (disk full, permission denied)
- Destination CBZ already exists (suspicious — do not overwrite silently)
- Soft-ban / unexpected redirect detected mid-download (Section 7.6)

The URL that caused the halt is **not** added to `DONE_FILE`. The next run resumes at that URL.
Notification email includes: book URL, page number if applicable, error type, full traceback.

---

## 8. File Organization

### 8.1 PRIMARY Storage Routing Rules (in priority order)

| Condition | Destination in PRIMARY |
|-----------|------------------------|
| Page count ≤ 4 | `/Covers/` |
| "This chapter is part of X" detected (multi-volume) | `/<FirstLetter>/<Series> [Author]/` |
| No series detected (one-shot) | `/%%%OneShots%%%/` |

First-letter derivation: first alphanumeric char of series name or book title. Digits → `#`.

The organizer is designed to be **extensible**: routing rules are defined as an ordered list
of condition→destination mappings, so new rules can be added without restructuring the code.

### 8.2 PRIMARY Folder Structure Example
```
STORAGE_ROOT/
├── #/
├── Covers/
│   └── Some Cover Title [Author].cbz
├── A/
│   └── Another Series [Author]/
│       ├── Another Series vol.1 - First Title [Author].cbz
│       └── Another Series vol.2 - Second Title [Author].cbz
├── R/
│   └── Relaxation [Katsura Airi]/
│       ├── Relaxation vol.1 - River Play [Katsura Airi].cbz
│       └── Relaxation vol.2 - Connection ~Summer Days~ [Katsura Airi].cbz
└── %%%OneShots%%%/
    └── My OneShot Title [Author].cbz
```

### 8.4 First-Letter Rule
Use the first alphanumeric character of the series name (multi-volume) or book title (one-shot
and covers). Skip leading punctuation, quotes, parentheses. Letters → uppercase. Digits → `#`.

### 8.5 Series Detection
1. Look for "This chapter is part of X" on the book info page.
2. If found: extract series name from that text.
3. Fetch `<book_url>/collections` to get the ordered volume list.
4. Find current book's position → volume number (1-based).
5. Compute **short title**: strip series name prefix from book title (case-insensitive).
   If the title doesn't start with the series name, keep the full title as-is.

### 8.6 Retroactive One-Shot → Series Move (PRIMARY only)

When downloading a multi-volume book (vol.N, N ≥ 2) and the series folder does not yet exist
in PRIMARY, the tool checks whether earlier volumes are stranded in `%%%OneShots%%%`:

**Algorithm:**
1. From the series collection list, get the vol.1 entry: its title and author.
2. Sanitize the vol.1 title and author using the same rules as filename generation.
3. Search `PRIMARY/%%%OneShots%%%/` for a `.cbz` file whose name contains both the sanitized
   vol.1 title and the author string.
4. If exactly one match is found:
   - Create the series folder (`PRIMARY/<Letter>/<Series> [Author]/`).
   - Rename and move the matched CBZ to: `<Series> vol.1 - <Short Title> [<Author>].cbz`
     inside the series folder.
   - Log: "Moved <old path> → <new path> (retroactive series assignment)".
5. If no match is found: create the series folder anyway and place the current book. Log a
   warning that vol.1 was not found in `%%%OneShots%%%` (may never have been downloaded, or
   may have been filed under a slightly different name — no automatic action).
6. If multiple matches are found: do not move anything. Log a warning listing the ambiguous
   candidates. Place the current book in the series folder normally.

This check runs only in PRIMARY.

### 8.7 Filename Format

**Multi-volume:**
```
<Series Name> vol.<N> - <Short Title> [<Author>].cbz
```
Examples:
```
Relaxation vol.1 - River Play [Katsura Airi].cbz
Relaxation vol.2 - Connection ~Summer Days~ [Katsura Airi].cbz
My Series vol.3 - Side Story [Author].cbz
```

**One-shot / Cover:**
```
<Book Title> [<Author>].cbz
```

**Author rule:** Always include `[Author]` suffix — no truncation.

### 8.8 Filename Sanitization
Applied to every path component (letter folder, series folder, filename):
- Strip: `\ / * ? : " < > |`
- Trim leading/trailing whitespace
- Remove trailing `.` from any component
- Truncate to **255 characters maximum** (applied after all transformations, before appending
  the `.cbz` extension for filenames)

---

## 9. CBZ Generation (Komga-compatible)

Target reader: **Komga** — pages must be at the root level of the ZIP (no subfolder).

1. Collect all temp PNGs for the book.
2. Sort by page number.
3. Rename to zero-padded filenames: `001.png`, `002.png`, …, `NNN.png`.
4. Generate `ComicInfo.xml` (see Section 9.1) and include it at the root level of the ZIP.
5. Create a ZIP archive with all PNGs + `ComicInfo.xml` at root level.
6. Write with `.cbz` extension.
7. Validate: open the ZIP and confirm central directory is readable and non-empty.
8. Delete the temp PNG folder.

Implementation: Python stdlib `zipfile` + `xml.etree.ElementTree`. No external dependencies.

### 9.1 ComicInfo.xml (Tags Only)

The `ComicInfo.xml` is embedded to expose genre tags to Komga's filtering and search. Only
the `<Tags>` field is populated from FAKKU metadata — Komga infers title, series, volume, and
author from the folder structure and filename automatically.

```xml
<?xml version="1.0" encoding="utf-8"?>
<ComicInfo xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
           xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <Tags>Romance, Comedy, Drama</Tags>
</ComicInfo>
```

Tags are extracted from the book info page (the genre/tag section) using BeautifulSoup during
metadata extraction. Comma-separated, in the order they appear on the page.

---

## 10. Notifications (Email / SMTP)

| Event | Subject | Body |
|-------|---------|------|
| Run complete | `[FakkuDL] Run complete: N books` | Book list, titles, page counts, elapsed time |
| Run halted | `[FakkuDL] ERROR: Run halted` | Book URL, page number, error type, traceback |
| Cookie expiry ≤ 7 days | `[FakkuDL] Warning: cookies expire in N days` | Expiry timestamp |
| Series folder auto-created for vol.N > 1 | `[FakkuDL] Warning: series folder created` | Series name, URL |

---

## 11. Logging

- Python `logging` module with two handlers:
  - `stdout` at INFO level (captured by Kubernetes log aggregation)
  - `./logs/downloader.log` rotating file at DEBUG level (5 MB per file, 3 backups)
- Per-page log entry: book title, page N/total, file size KB, elapsed time.
- Structured text now; JSON formatter can be added in Phase 2.

---

## 12. Testing

### 12.1 Unit Tests (pytest)
Cover all logic that does not require a browser:
- Series detection from HTML fixtures
- Volume number extraction from collection list
- Short title computation (series prefix stripping, edge cases)
- Routing rules (Covers ≤4 pages, multi-volume, one-shot, digit-first)
- Filename formatting (all three formats + author truncation)
- Filename sanitization (illegal chars, trailing dot, 255-char truncation)
- URL normalisation (trailing slash, casing)
- Cookie expiry check logic

Test data: small HTML snippets saved as fixtures (no live HTTP calls in unit tests).

### 12.2 Dry-Run Mode (`--dry-run` flag)
Fetches the FAKKU collection page (using Playwright + cookies for auth), then fetches each
book's metadata page via `requests` + BeautifulSoup. Authentication for the `requests` session
is bootstrapped by loading `COOKIES_FILE` (cookies.pickle) and injecting the cookies into a
`requests.Session` — no second browser launch needed.

Prints the plan without downloading anything:

```
[SKIP]     https://www.fakku.net/hentai/foo  (already in done.txt)
[COVER]    Some Cover [Author] → /Covers/Some Cover [Author].cbz  (4 pages)
[SERIES]   Relaxation vol.2 → /R/Relaxation [Katsura Airi]/Relaxation vol.2 - Connection ~Summer Days~ [Katsura Airi].cbz
[ONESHOT]  My Title [Author] → /%%%OneShots%%%/My Title [Author].cbz
```

Dry-run does not modify `DONE_FILE`, does not create folders, does not write any files.

---

## 13. Project Structure (Post-Rewrite)

```
FakkuDownloader/
├── .env                  ← gitignored; all credentials and tuning
├── .env.example          ← committed; all keys, no values
├── main.py               ← entry point: load config, init components, run
├── config.py             ← loads .env, validates required vars, Config dataclass
├── auth.py               ← cookie check, TOTP auto-login, cookie persistence
├── browser.py            ← Playwright browser/context/page wrapper
├── downloader.py         ← main loop: queue resolution, per-book orchestration
├── organizer.py          ← routing rules, series detection, path building, CBZ packing
├── notifier.py           ← SMTP email notifications
├── book.py               ← Book dataclass (series, volume, short title, author, pages)
├── helper.py             ← file/folder utilities, URL normalisation
├── done.txt              ← completed URLs (gitignored)
├── tmp/                  ← temp PNGs during download (gitignored)
├── downloaded/           ← STORAGE_PRIMARY for local dev
├── logs/
│   └── downloader.log
├── tests/
│   ├── fixtures/         ← HTML snippets for unit tests
│   └── test_organizer.py
│   └── test_helper.py
├── specs/
│   └── automation-rewrite.md
└── requirements.txt
```

---

## 14. Phase 2: Containerization

### 14.1 Dockerfile
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium --with-deps
COPY . .
CMD ["python", "main.py"]
```

### 14.2 Kubernetes CronJob
```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: fakku-downloader
spec:
  schedule: "0 2 * * *"        # 2am daily
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: Never
          containers:
            - name: downloader
              image: fakku-downloader:latest
              envFrom:
                - secretRef:
                    name: fakku-secrets
              volumeMounts:
                - name: primary-storage
                  mountPath: /storage/primary
                - name: queue
                  mountPath: /app/queue      # done.txt + temp/ live here
          volumes:
            - name: primary-storage
              persistentVolumeClaim:
                claimName: fakku-primary-pvc
            - name: queue
              persistentVolumeClaim:
                claimName: fakku-queue-pvc   # cookies.pickle, done.txt, tmp/ all here
```

### 14.3 Credentials & Bootstrap in k3s
- All `.env` keys → entries in the `fakku-secrets` K8s Secret.
- `cookies.pickle` and `done.txt` persist on `fakku-queue-pvc`.
- `tmp/` (partial downloads) also lives on `fakku-queue-pvc` — survives pod restarts, enabling
  exact page-level resume on the next CronJob run.
- **First run:** if `cookies.pickle` is absent, the auto-login flow runs fully headless.

---

## 15. Resolved Decisions

| Decision | Choice |
|----------|--------|
| Language | Python 3.12 + Playwright (sync API) |
| Auth | Zero-touch: username + password + TOTP (pyotp) |
| Queue source | FAKKU collection URL — no local urls.txt |
| Collection URL | Configurable via `FAKKU_COLLECTION_URL` env var |
| Queue cleanup after download | Leave FAKKU collection alone; track via done.txt |
| URL normalisation | Strip trailing slash, lowercase before comparing |
| Empty queue / empty collection | Exit silently, code 0 |
| Error policy | Halt and notify on any unrecoverable error at any level |
| Partial download | Resume from last valid page on next run |
| Output format | .cbz — Komga-compatible, pages at ZIP root |
| Covers (≤4 pages) | Routed to `/Covers/` folder |
| One-shots | Routed to `/%%%OneShots%%%/` folder |
| Digit-first titles | Routed to `/#/` folder |
| Organizer extensibility | Ordered list of routing rules; easy to add new ones |
| Storage | STORAGE_PRIMARY, configurable via env var |
| Driver management | Playwright manages Chromium internally |
| Schedule | Kubernetes CronJob, 2am daily (`0 2 * * *`) |
| Temp dir (k3s) | Carved from queue PVC (survives pod restart for exact resume) |
| Notifications | SMTP email |
| Max filename length | 255 characters |
| Migration of old files | None — leave existing downloads as-is |
| Testing | pytest unit tests + `--dry-run` mode |
| Screenshot format | PNG (lossless) |
| Chrome offset | Configurable (`CHROME_OFFSET` env var); auto-detected if blank |
| Soft-ban detection | Redirect check after each page nav; halt + notify |
| Multi-collection ambiguity | Assume one series per book (non-issue in practice) |
| done.txt format | Plain URLs only, one per line |
| Log level | Fixed: DEBUG to file, INFO to stdout; no config needed |
| Dry-run auth | Playwright for collection fetch; requests+BS4 for book metadata |
| Progress display | Log lines only — no tqdm |
| Page count mismatch | Halt and notify; leave temp folder for resume |
| ComicInfo.xml | Embed in every CBZ — genre tags only |
| Retroactive one-shot move | Auto-move silently using vol.1 title+author match in %%%OneShots%%% |
| Dry-run cookies | cookies.pickle loaded into requests.Session |
| Collection pagination failure | Halt and notify — silent truncation is unacceptable |
| Inaccessible / not-owned book | Halt and notify (same as all errors) |
| Ownership pre-check | Check for 'Read' button on info page before opening reader |
| Skip flag | None — manually add URL to done.txt |
| Docker Chromium flags | Always apply --no-sandbox + --disable-dev-shm-usage |
| Python version | 3.12 |
| Dependency pinning | Exact versions (==) in requirements.txt |
| COOKIES_FILE path | Configurable via env var |
| TEMP_DIR path | Configurable via env var |
| Author truncation | Removed — always include [Author] |
| Email format | Plain text |

---

## 16. Open Items

1. **TOTP secret export (user action):** One-time manual step — export base32 seed from
   Authenticator before first run. Document clearly in README. No code work.

2. **Additional routing rules:** The organizer is extensible. New rules can be added during
   implementation if further categories are identified (e.g. anthologies, magazines).

3. **`done.txt` location in k3s:** Config path is `DONE_FILE`. In Phase 2, set
   `DONE_FILE=/app/queue/done.txt` to keep it on the queue PVC alongside `cookies.pickle`.
