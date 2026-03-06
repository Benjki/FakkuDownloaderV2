# ToPlace Processor — Refined Spec

## Summary

Add a post-download phase that processes `.zip` and `.cbz` files dropped into a `/ToPlace` folder, normalizes their filenames, detects series membership via title heuristics and filesystem scanning, and routes them into the existing `/Fakku/` folder structure following the same rules as the FAKKU downloader.

Also: move `TO FIX MANUALLY` from inside `/Fakku/` to the storage root (sibling of `/Fakku/` and `/ToPlace/`) for both FAKKU downloads and ToPlace processing.

---

## 1. Folder Layout

```
STORAGE_ROOT/              (new required env var)
  Fakku/                   (= STORAGE_PRIMARY, unchanged)
    A/
      %%%OneShots%%%/
      Some Series [Author]/
    B/ ...
  ToPlace/                 (input folder — user drops files here)
  TO FIX MANUALLY/         (moved from Fakku/TO FIX MANUALLY/ to root)
```

### Config Changes

| Env var | Required | Default | Notes |
|---------|----------|---------|-------|
| `STORAGE_ROOT` | **Yes** | — | Parent directory containing `Fakku/`, `ToPlace/`, `TO FIX MANUALLY/`. Fails fast if missing. |

- `STORAGE_PRIMARY` continues to point to the `Fakku/` folder.
- `ToPlace/` path is derived as `STORAGE_ROOT / ToPlace`.
- `TO FIX MANUALLY/` path is derived as `STORAGE_ROOT / TO FIX MANUALLY`.

### Migration: TO FIX MANUALLY

- `route_book()` in `organizer.py` currently returns `'TO FIX MANUALLY'` as a path relative to `storage_primary`. This must change so that both FAKKU downloads and ToPlace route to `STORAGE_ROOT/TO FIX MANUALLY/` instead of `STORAGE_PRIMARY/TO FIX MANUALLY/`.
- Existing files in `Fakku/TO FIX MANUALLY/` are **not** auto-migrated. The user moves them manually.

---

## 2. Execution Order

1. Browser authentication + FAKKU download queue (existing flow, unchanged).
2. **After** downloads complete and the success email logic runs: process `/ToPlace`.
3. ToPlace processing does **not** require browser or authentication — it is purely filesystem-based.
4. The combined email includes both sections (see Section 8).

### Dry Run

- `--dry-run` applies to ToPlace as well: log what would be renamed/placed/deleted, but touch **no** files.
- The in-place rename step (Section 4) is also skipped in dry run.

---

## 3. Input

- Scan `STORAGE_ROOT/ToPlace/` for `*.zip` and `*.cbz` files (non-recursive, top-level only).
- If the folder is empty or missing, skip silently with a log message.

---

## 4. Step 1 — Fix Names (In-Place Rename)

Physically rename each file in `/ToPlace/` before further processing. This is adapted from the existing `fixNames.py` logic.

### Algorithm

Given a filename like `[Circle (Author)] Some Title [Extra] (Tag).zip`:

1. Strip the file extension (`.zip` or `.cbz`).
2. Replace all underscores with spaces.
3. Extract author from the leading bracket/paren group:
   - `[Circle (Author)]` -> author = `Author` (inner parens preferred).
   - `[CircleName]` -> author = `CircleName` (no inner parens = use full bracket content).
   - `(CircleName)` -> author = `CircleName`.
   - No leading bracket/paren -> author = `''` (empty).
4. Remove all remaining `[...]` and `(...)` groups from the title.
5. Strip and trim the title.
6. Construct: `{Title} [{Author}].cbz` (always `.cbz` — see Section 5).

### Edge Cases

- If the file is already in `Title [Author].cbz` format (no leading brackets), it passes through with only underscore removal and extension normalization.
- If target filename already exists in `/ToPlace/`, skip with a warning log.

---

## 5. File Extension

- Accept both `.zip` and `.cbz` as input.
- **Always normalize to `.cbz`** on output (both the in-place rename and the final placement).
- The files are not re-packed or modified internally — only the extension changes.

---

## 6. Step 2 — Parse Title and Author from Fixed Filename

After renaming, parse the cleaned filename:

- Pattern: `{Title} [{Author}].cbz`
- Extract `title` and `author` from the filename.
- If no `[Author]` bracket is found, `author = ''`.

---

## 7. Step 3 — Series Detection

Two-phase detection using only the filename and the filesystem. No network requests.

### Phase A: Title Heuristic

Reuse `infer_series_from_title(title)`:
- If the title ends with a bare integer N >= 2 (e.g. "Dark Pleasure 2"), infer `series_name = "Dark Pleasure"`, `volume_number = 2`.
- If N == 1 or no trailing integer, this phase returns nothing.

### Phase B: Filesystem Scan

Scan only the relevant letter folder to find matching existing files:

1. Compute `letter = first_letter(title)`.
2. Scan `STORAGE_PRIMARY/<letter>/%%%OneShots%%%/` for `.cbz` and `.zip` files.
3. Also scan loose files directly in `STORAGE_PRIMARY/<letter>/` (not recursive into subfolders).
4. **Match criteria** (normalized, case-insensitive):
   - The existing file's title stem (filename minus `[Author]` suffix and extension) is a prefix of the new book's inferred series name.
   - The existing file's author matches the new book's author (case-insensitive, after normalization).

### Decision Matrix

| Title heuristic result | Filesystem match found | Action |
|------------------------|------------------------|--------|
| Series detected (vol N >= 2) | Vol.1 found in OneShots | Create series folder, retroactive move vol.1, place vol.N |
| Series detected (vol N >= 2) | Vol.1 NOT found | Route to `TO FIX MANUALLY` (missing volumes) |
| No series detected | — | Route as oneshot to `%%%OneShots%%%` |
| Vol.1 explicitly | — | Route as oneshot (normal rules). Future vol.2 triggers retroactive move. |

### What is NOT done

- No reverse merge: placing vol.1 does not scan for existing vol.2+ to consolidate.
- No scanning inside series subfolders (only `%%%OneShots%%%` and letter root).

---

## 8. Step 4 — Routing

Reuse the existing routing logic from `organizer.py` with these adjustments:

| Condition | Destination |
|-----------|-------------|
| File conflict (destination exists) | `STORAGE_ROOT/TO FIX MANUALLY/` |
| Missing preceding volumes | `STORAGE_ROOT/TO FIX MANUALLY/` |
| Series (vol N, all preceding vols present) | `STORAGE_PRIMARY/<Letter>/<Series> [<Author>]/` |
| Oneshot (no series detected) | `STORAGE_PRIMARY/<Letter>/%%%OneShots%%%/` |

### Filename Construction

Reuse `build_filename()` from `organizer.py`:
- Series: `<Series> vol.<N> - <Short Title> [<Author>].cbz` or `<Series> vol.<N> [<Author>].cbz`
- Oneshot: `<Title> [<Author>].cbz`

### Retroactive Oneshot Move

When placing vol.2+ and vol.1 is found in `%%%OneShots%%%`:
- Reuse `check_and_move_oneshot()` identically to the downloader.
- Rename vol.1 to series naming convention and move it to the new series folder.
- Report the move in the email (see Section 9).

### File Conflict Check

Before placing, check if a file with the same name (`.cbz` or `.zip`) already exists at the destination:
- If yes, route to `TO FIX MANUALLY/`.

---

## 9. Step 5 — Place and Clean Up

1. Move (not copy) the renamed `.cbz` from `/ToPlace/` to the computed destination.
2. Create destination directories as needed.
3. **Delete** the source file from `/ToPlace/` after successful placement (it was already moved, so this is implicit).

---

## 10. Email Updates

### Structure

The success email gains a second section:

```
[FAKKU Downloads]          (existing section, unchanged)
  Summary badge counts
  Book cards (green/blue/grey/red borders)

[ToPlace Processing]       (new section)
  Summary badge counts
  Book cards (same color scheme)
```

### ToPlace Book Cards

Each card includes:
- Title and author
- Routing decision (series/oneshot/to-fix-manually)
- Original filename (before rename)
- Destination path
- Retroactive move info (if vol.1 was moved)

### When no ToPlace files exist

Omit the ToPlace section entirely (don't show an empty section).

### Dry Run

Both sections get `[DRY RUN]` prefix in the subject line (existing behavior, unchanged).

---

## 11. Error Handling

- If a single ToPlace file fails (bad filename, IO error), log the error and **continue** with the next file. Do not halt.
- Failed files remain in `/ToPlace/` untouched.
- The email includes failed files with an error indicator.
- ToPlace errors do **not** trigger `send_error()` — they are reported in the success email's ToPlace section.

---

## 12. Implementation Plan (TDD)

### New Files

- `placer.py` — ToPlace processor class (mirrors `downloader.py` structure).
- `tests/test_placer.py` — unit tests for the placer.
- `tests/test_fix_names.py` — unit tests for the name-fixing logic (ported from fixNames.py).

### Modified Files

- `config.py` — add `STORAGE_ROOT` (required), derive `to_place_dir` and `to_fix_manually_dir`.
- `organizer.py` — update `route_book()` to accept/return absolute paths using `STORAGE_ROOT` for `TO FIX MANUALLY`.
- `downloader.py` — update all `TO FIX MANUALLY` path construction to use new config paths.
- `notifier.py` — add ToPlace section to `send_success()`.
- `main.py` — call placer after downloader completes.

### Test Strategy (YAGNI)

Write tests **before** implementation for each unit:

1. **Name fixing**: various bracket patterns, underscore removal, extension normalization, collision handling.
2. **Title/author parsing**: extract from cleaned filename.
3. **Series detection**: title heuristic + filesystem scan with mock directory structures.
4. **Routing**: reuse existing routing tests as templates, add ToPlace-specific cases.
5. **Retroactive move**: vol.2 placement triggers vol.1 move from OneShots.
6. **File conflict**: destination already exists -> TO FIX MANUALLY.
7. **Dry run**: verify no filesystem changes occur.
8. **End-to-end**: drop files in mock ToPlace, verify final locations.

No abstractions, no helpers beyond what's needed. Inline logic where it's used once.

---

## 13. Out of Scope

- No internal re-packing of archives (no ComicInfo.xml injection for ToPlace files).
- No recursive scanning of `/ToPlace/` subfolders.
- No reverse merge (placing vol.1 doesn't consolidate existing vol.2+).
- No scanning inside existing series subfolders during detection.
- No auto-migration of existing `Fakku/TO FIX MANUALLY/` contents.
- No cover detection for ToPlace files (no page count available without opening the archive).
