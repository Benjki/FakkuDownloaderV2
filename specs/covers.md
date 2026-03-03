# Spec: Cover Subfolder Grouping

## Goal
Books with ≤ 4 pages ("covers") currently land flat in `Covers/`. This spec introduces
automatic subfoldering: each cover is placed in `Covers/<group>/` where `<group>` is a
short series-like prefix extracted from the book title.

---

## New function: `extract_cover_group(title: str) -> str`

Lives in `organizer.py`. Called by `route_book()` whenever `book.is_cover` is `True`.

### Step 1 — Pre-clean the title
1. Strip a trailing `[...]` author tag: remove the last ` [...]` occurrence (the bracket
   block Fakku appends, e.g. `[Akinosora]` or `[YUG]`).
2. Strip trailing non-word decoration: remove trailing emoji, `❤`, `★`, `♥`, etc. using
   a regex that drops characters outside `[\w\s\-'.,#()]` from the right.

### Step 2 — Tokenise
Split on whitespace. **Hyphenated words are a single token** (`Key-Visual`, `Kari-YUG`
each count as 1 token).

### Step 3 — Find the first delimiter token
Scan tokens left-to-right and stop at the **first** match:

| Priority | Pattern | Examples |
|----------|---------|---------|
| 1 | A `DELIMITER_KEYWORD` (case-insensitive: `vol`, `vol.`, `part`, `no`, `no.`, `number`, `issue`) **where the immediately following token is all-digit** | `Vol. 52`, `Part 158` |
| 2 | Token starts with `#` followed by digits | `#82`, `#62` |
| 3 | Token matches a date-like pattern: 4-digit year prefix `YYYY-MM…` or `YYYYMMDD` | `2020-12`, `2020-0607` |
| 4 | Token is purely numeric (all digits) | `95`, `62`, `52` |
| 5 | Token is a standalone `-` (dash surrounded by spaces, i.e. its own token after splitting on whitespace) | `- Miu's Summer` |

The first pattern that fires ends the scan.

### Step 4 — Extract and cap the prefix
- Collect every token **before** the delimiter token.
- If the prefix has **more than 3 tokens**: keep only the first 3.
- If the prefix has 0 tokens (delimiter was the very first token): fall through to Step 5.

### Step 5 — No-delimiter / zero-prefix fallback
If no delimiter was found, **or** the prefix is empty, take the first 3 tokens of the
(pre-cleaned) title (or all tokens if fewer than 3 exist).

### Step 6 — Sanitise
Join prefix tokens with a single space, then apply `replace_illegal()` to produce a
filesystem-safe folder name.

---

## Worked examples

| Raw title | Delimiter | Prefix tokens (before cap) | Group |
|-----------|-----------|----------------------------|-------|
| `X-Eros Pinup #82 Kito Sakeru` | `#82` (rule 2) | `[X-Eros, Pinup]` | `X-Eros Pinup` |
| `X-Eros Girls Collection #62  Akinosora [Akinosora]` | `#62` (rule 2) | `[X-Eros, Girls, Collection]` | `X-Eros Girls Collection` |
| `Kari-YUG Vol. 52 [YUG]` | `Vol.` (rule 1) | `[Kari-YUG]` | `Kari-YUG` |
| `Kairakuten Heroines 2020-12 - Remu` | `2020-12` (rule 3) | `[Kairakuten, Heroines]` | `Kairakuten Heroines` |
| `Cover's Comment Part 158 NaPaTa` | `Part` (rule 1) | `[Cover's, Comment]` | `Cover's Comment` |
| `BEAST Cover Girl - Miu's Summer ❤ by Nakamachi Machi` | `-` (rule 5) | `[BEAST, Cover, Girl]` | `BEAST Cover Girl` |
| `Bavel 2020-0607 Double Cover` | `2020-0607` (rule 3) | `[Bavel]` | `Bavel` |
| `48 Sex Positions Under the Kotatsu` | none | fallback first 3 | `48 Sex Positions` |
| `Weekly Kairakuten Key-Visual Collection 95 [Aramaki Echizen]` | `95` (rule 4) | `[Weekly, Kairakuten, Key-Visual, Collection]` → cap 3 | `Weekly Kairakuten Key-Visual` |
| `Kairakuten` (single word, no delimiter) | none | fallback first 3 → 1 token | `Kairakuten` |

---

## Routing change

`route_book()` in `organizer.py` currently returns `'Covers'` for cover books.

**New behaviour:**
```python
if book.is_cover:
    group = extract_cover_group(book.title)
    return str(Path('Covers') / group)
```

Result: `Covers/X-Eros Pinup/`, `Covers/Kairakuten Heroines/`, etc.

---

## Filename
Unchanged — same format as one-shots:

```
<Book title> [Author].cbz
```

No volume numbering. The full title is used (not a short title).

---

## Existing flat files in `Covers/`
**Leave in place.** New downloads go straight into the appropriate subfolder. Existing
flat CBZ files are not touched. No retroactive move logic is added for covers.

---

## Email / notifier impact

`_book_card()` in `notifier.py` currently hardcodes the string `"Cover (≤4 pages) →
placed in Covers/"`. Replace with the actual `r['series_dir']` value so the subfolder
is visible in the report:

```
Cover (≤4 pages) → placed in Covers/X-Eros Pinup/
```

---

## Files to change

| File | Change |
|------|--------|
| `organizer.py` | Add `extract_cover_group(title)`. Update `route_book()` to call it. |
| `notifier.py` | Update `cover` routing label in `_book_card()` to show `r['series_dir']`. |
| `tests/test_organizer.py` | Add `TestExtractCoverGroup` with all worked examples above as cases. |

No changes needed to `Book`, `downloader.py`, or `main.py`.

---

## Edge cases & clarifications

| Situation | Behaviour |
|-----------|-----------|
| Leading number in title (`48 Sex Positions…`) | Number counts as token 1; not skipped |
| Hyphenated token (`Key-Visual`) | Counts as 1 token toward the 3-word cap |
| Author tag present (`[Akinosora]`) | Stripped before tokenising |
| Trailing emoji (`❤`) | Stripped before tokenising |
| Delimiter is the very first token | Zero-token prefix → fallback (first 3 tokens of full cleaned title) |
| Title is one word, no delimiter | Group = that one word |
| Keyword delimiter without a following number (e.g. `Vol` at the end) | Does **not** fire rule 1; continue scanning |
| File conflict in subfolder | Existing `book.file_conflict` path applies: re-routes to `TO FIX MANUALLY/` |
fg