"""File organization — routing rules, naming, series detection, and CBZ packing."""

import re
import logging
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from book import Book
from helper import replace_illegal, first_letter, normalise_url

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cover group extraction — module-level constants
# ---------------------------------------------------------------------------

_COVER_DELIMITER_KEYWORDS = frozenset({'vol', 'vol.', 'part', 'no', 'no.', 'number', 'issue'})
_COVER_DATE_RE = re.compile(r'^\d{4}[-/]\d{2,}')  # YYYY-MM, YYYY-MMDD, YYYY-MM-DD …
_COVER_YYYYMMDD_RE = re.compile(r'^\d{8}$')        # 20200607

_TITLE_VOLUME_RE = re.compile(r'^(.+?)\s+(\d+)$')

TO_FIX_MANUALLY = 'TO FIX MANUALLY'


class MetadataError(Exception):
    pass


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------

def extract_metadata(html: str) -> dict:
    """
    Extract title, author, page_count, tags from a book info page.
    Raises MetadataError if title, author, or page_count cannot be found.
    Returns dict with keys: title, author, pages, tags.
    """
    soup = BeautifulSoup(html, 'lxml')

    # Title
    title_el = soup.find(
        'h1',
        class_=lambda c: c and 'text-2xl' in c and 'font-bold' in c,
    )
    if not title_el:
        raise MetadataError('Cannot find book title — CSS selector may need updating')
    title = title_el.get_text(strip=True)

    # Author (first metadata value div)
    other_divs = soup.find_all(
        'div',
        class_=lambda c: c and 'table-cell' in c and 'space-y-2' in c,
    )
    if not other_divs:
        logger.warning('Cannot find author — continuing without artist name')
        author = ''
    else:
        author = other_divs[0].get_text(strip=True)

    # Page count
    page_divs = [d for d in other_divs if 'pages' in d.get_text()]
    if not page_divs:
        pages = 1
    else:
        page_text = page_divs[-1].get_text(strip=True)
        m = re.search(r'(\d+)', page_text)
        pages = int(m.group(1)) if m else 1

    # Tags — require data-attribute-count to exclude <template> anchors that
    # share the same /tags/ href pattern but contain prefixed text like "tags: Foo".
    tag_links = (
        soup.select('a[href*="/tags/"][data-attribute-count]')
        or soup.select('a[href*="/genres/"][data-attribute-count]')
    )
    _seen: set[str] = set()
    tags = []
    for a in tag_links:
        text = a.get_text(strip=True)
        if text and text not in _seen:
            _seen.add(text)
            tags.append(text)

    return {'title': title, 'author': author, 'pages': pages, 'tags': tags}


# ---------------------------------------------------------------------------
# Ownership check
# ---------------------------------------------------------------------------

def check_ownership(html: str) -> bool:
    """Return True if the book info page contains a 'Read' button."""
    soup = BeautifulSoup(html, 'lxml')
    read_btn = (
        soup.find('a', href=lambda h: h and '/read' in h)
        or soup.find(string=re.compile(r'\bRead\b'))
    )
    return read_btn is not None


# ---------------------------------------------------------------------------
# Series detection
# ---------------------------------------------------------------------------

def detect_series(
    html: str,
    book_url: str,
    session: requests.Session,
) -> tuple[str | None, int | None, str | None]:
    """
    Returns (series_name, volume_number, short_title) or (None, None, None).

    Parses the embedded collection block from the book info page:
      <div>This chapter is part of <em><a href="/collections/...">Series Name</a></em>.</div>
      <ul>
        <li><strong><a href="/hentai/vol1-slug">Vol 1 Title</a></strong> ...</li>
        <li><strong><a href="/hentai/vol2-slug">Vol 2 Title</a></strong> ...</li>
      </ul>

    The volume list is already on the page — no extra HTTP request needed.
    short_title is returned as None; caller computes it via compute_short_title().
    """
    soup = BeautifulSoup(html, 'lxml')

    # Locate ALL "This chapter is part of" text nodes.
    # A book can belong to multiple FAKKU collections simultaneously (e.g. a direct
    # 2-book series AND a large anniversary collab umbrella).  Picking the wrong one
    # would assign a wrong volume number and corrupt the retroactive move logic.
    # Safe strategy: if more than one collection block is present, skip series
    # detection entirely and treat the book as a one-shot.
    part_of_nodes = soup.find_all(
        string=re.compile(r'This chapter is part of', re.IGNORECASE),
    )
    if not part_of_nodes:
        return None, None, None
    if len(part_of_nodes) > 1:
        names = []
        for node in part_of_nodes:
            em = node.parent.find('em') if node.parent else None
            a = em.find('a') if em else None
            if a:
                names.append(a.get_text(strip=True))
        logger.warning(
            'Book belongs to %d collections (%s) — routing to "TO FIX MANUALLY".',
            len(part_of_nodes),
            ', '.join(f'"{n}"' for n in names),
        )
        # Sentinel: caller uses this to set Book.multi_collection = True
        return '__multi_collection__', None, None

    part_of_node = part_of_nodes[0]

    # Series name lives in <em><a>Series Name</a></em> inside that block
    block = part_of_node.parent
    while block and block.name not in ('div', 'p', 'section', 'article'):
        block = block.parent
    if not block:
        return None, None, None

    em = block.find('em')
    series_link = em.find('a') if em else None
    if not series_link:
        return None, None, None

    series_name = series_link.get_text(strip=True)
    if not series_name:
        return None, None, None

    logger.debug('Series detected: %s', series_name)

    # The ordered volume list is a sibling <ul> of the "part of" block.
    # Use find_next_sibling to stay within the same parent container and avoid
    # accidentally matching site-nav or tag <ul> elements elsewhere in the DOM.
    ul = block.find_next_sibling('ul')
    if not ul:
        # Fall back to parent's first <ul> in case the block is nested differently
        ul = block.parent.find('ul') if block.parent else None
    if not ul:
        logger.warning('Series "%s": volume list not found on page — defaulting to vol 1.', series_name)
        return series_name, 1, None

    # Each <li> contains:
    #   <p>N</p>  ← explicit volume number written by FAKKU
    #   <p><strong><a href="/hentai/...">Title</a></strong></p>
    #   <p><a href="...">Start Reading</a></p>  ← always points to current book, ignore
    # Read the number directly from the <p> rather than inferring from list position.
    book_path = '/' + book_url.split('fakku.net/')[-1].rstrip('/')
    volume_number = None
    for li in ul.find_all('li'):
        # Chapter link is wrapped in <b><a>, not <strong><a>
        b_tag = li.find('b')
        if not b_tag:
            continue
        a = b_tag.find('a', href=re.compile(r'/hentai/'))
        if not a:
            continue
        if a['href'].rstrip('/') != book_path:
            continue
        # Found the matching chapter — read volume number from the first <div>
        # Structure: <div class="flex-none text-right text-sm">N</div>
        first_div = li.find('div')
        if first_div and first_div.get_text(strip=True).isdigit():
            volume_number = int(first_div.get_text(strip=True))
        else:
            volume_number = 1
        break

    if volume_number is None:
        logger.warning(
            'Book not found in series "%s" volume list — defaulting to vol 1.', series_name,
        )
        volume_number = 1

    return series_name, volume_number, None


def _strip_series_prefix(title: str, series_name: str) -> str:
    """
    Word-level prefix strip that ignores punctuation differences between
    series_name and title (e.g. comma vs no-comma, hyphen variants).

    Extracts alphanumeric word tokens from both strings and checks whether
    the title starts with the same word sequence as the series name.
    If so, scans the original title character-by-character to find the exact
    position where the series prefix ends, then returns the trimmed remainder.
    Returns '' if no match.
    """
    series_words = re.findall(r'[a-z0-9]+', series_name.lower())
    if not series_words:
        return ''
    title_words = re.findall(r'[a-z0-9]+', title.lower())
    if title_words[:len(series_words)] != series_words:
        return ''
    # Walk the original title, consuming exactly len(series_words) word tokens
    words_matched = 0
    i = 0
    while i < len(title) and words_matched < len(series_words):
        if title[i].isalnum():
            while i < len(title) and title[i].isalnum():
                i += 1
            words_matched += 1
        else:
            i += 1
    return title[i:].lstrip(' ,-:').strip()


def compute_short_title(title: str, series_name: str) -> str:
    """Strip series_name prefix from title (case-insensitive). Returns full title if no match."""
    if title.lower().startswith(series_name.lower()):
        short = title[len(series_name):].lstrip(' -:').strip()
        return short if short else title
    # Fallback: word-level match ignoring punctuation differences
    short = _strip_series_prefix(title, series_name)
    return short if short else title


def infer_series_from_title(title: str) -> tuple[str, int] | None:
    """
    Heuristic fallback for when Fakku reports no series.
    If title ends with a bare integer >= 2 (e.g. "Dark Pleasure 2"),
    infer series name and volume number from the title itself.
    Returns (series_name, volume_number) or None.
    False positives land in TO FIX MANUALLY via the missing-volumes check.
    """
    m = _TITLE_VOLUME_RE.match(title)
    if not m:
        return None
    volume = int(m.group(2))
    if volume < 2:
        return None
    return m.group(1), volume


# ---------------------------------------------------------------------------
# Cover group extraction
# ---------------------------------------------------------------------------

def extract_cover_group(title: str) -> str:
    """
    Derive a short grouping label from a cover title for use as a subfolder
    name under Covers/.  See specs/covers.md for the full algorithm.

    Examples:
        'X-Eros Pinup #82 Kito Sakeru'            -> 'X-Eros Pinup'
        'Kari-YUG Vol. 52 [YUG]'                  -> 'Kari-YUG'
        'Kairakuten Heroines 2020-12 - Remu'       -> 'Kairakuten Heroines'
        "Cover's Comment Part 158 NaPaTa"          -> "Cover's Comment"
        '48 Sex Positions Under the Kotatsu'       -> '48 Sex Positions'
    """
    # Step 1: strip trailing [Author] tag
    cleaned = re.sub(r'\s*\[[^\]]*\]\s*$', '', title).strip()
    # Step 1b: strip trailing non-word decoration (emoji, ❤, ★ …)
    cleaned = re.sub(r"[^\w\s\-'.,#()]+$", '', cleaned, flags=re.UNICODE).strip()

    tokens = cleaned.split()
    if not tokens:
        return replace_illegal(title[:50].strip())

    # Step 3: find the index of the first delimiter token
    delimiter_idx: int | None = None
    for i, tok in enumerate(tokens):
        tl = tok.lower()
        # Priority 1: keyword (Vol, Part, No, Issue …) followed by a numeric token
        if tl in _COVER_DELIMITER_KEYWORDS:
            if i + 1 < len(tokens) and tokens[i + 1].isdigit():
                delimiter_idx = i
                break
        # Priority 2: #N  (e.g. #82, #62)
        if re.match(r'^#\d+', tok):
            delimiter_idx = i
            break
        # Priority 3: date-like  YYYY-MM…  or  YYYYMMDD
        if _COVER_DATE_RE.match(tok) or _COVER_YYYYMMDD_RE.match(tok):
            delimiter_idx = i
            break
        # Priority 4: purely numeric token
        if tok.isdigit():
            delimiter_idx = i
            break
        # Priority 5: standalone dash separator  ( … - Subtitle )
        if tok == '-':
            delimiter_idx = i
            break

    # Steps 4 + 5: build prefix, cap at 3 tokens
    if delimiter_idx is not None and delimiter_idx > 0:
        prefix = tokens[:delimiter_idx]
    else:
        # No delimiter found, OR delimiter at index 0 (empty prefix) → first 3 tokens
        prefix = tokens

    return replace_illegal(' '.join(prefix[:3]))


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def route_book(book: Book) -> str:
    """
    Return destination directory path relative to storage_primary.
    Priority:
    1. multi_collection or missing_volumes -> 'TO FIX MANUALLY'
    2. is_cover (pages <= 4) -> 'Covers/<group>'
    3. series_name is not None -> '<Letter>/<Series> [Author]'
    4. default (one-shot) -> '<Letter>/%%%OneShots%%%'
    """
    if book.multi_collection or book.missing_volumes or book.file_conflict:
        return TO_FIX_MANUALLY
    if book.is_cover:
        group = extract_cover_group(book.title)
        return str(Path('Covers') / group)
    if book.is_series():
        letter = first_letter(book.series_name)
        author_tag = f' [{book.author}]' if book.author else ''
        folder_name = replace_illegal(f'{book.series_name}{author_tag}')
        return str(Path(letter) / folder_name)
    letter = first_letter(book.title)
    return str(Path(letter) / '%%%OneShots%%%')


# ---------------------------------------------------------------------------
# Filename generation
# ---------------------------------------------------------------------------

def build_filename(book: Book) -> str:
    """
    Returns filename WITH .cbz extension.
    Series with meaningful subtitle: '<Series> vol.<N> - <Subtitle> [<Author>].cbz'
    Series with no meaningful subtitle: '<Series> vol.<N> [<Author>].cbz'
    One-shot/Cover: '<Title> [<Author>].cbz'
    255-char limit on stem before adding extension.

    Subtitle is omitted when it is empty, a bare integer, or identical to the
    series name (the compute_short_title fallback when title == series name).
    """
    author_tag = f' [{book.author}]' if book.author else ''
    if book.is_series():
        short = book.short_title or ''
        if short and not short.isdigit() and short != book.series_name:
            stem = f'{book.series_name} vol.{book.volume_number} - {short}{author_tag}'
        else:
            stem = f'{book.series_name} vol.{book.volume_number}{author_tag}'
    else:
        stem = f'{book.title}{author_tag}'
    stem = replace_illegal(stem, max_length=251)  # 251 + 4 (.cbz) = 255
    return stem + '.cbz'


# ---------------------------------------------------------------------------
# Retroactive one-shot -> series move
# ---------------------------------------------------------------------------

def check_and_move_oneshot(
    series_name: str,
    author: str,
    vol1_title: str,
    storage_primary: str,
    series_dir: str,
    dry_run: bool = False,
) -> dict | None:
    """
    When vol.N (N>=2) is detected and series_dir doesn't exist yet,
    check if vol.1 is stranded in %%%OneShots%%% and move it.

    Returns a dict {'from': old_name, 'to': new_name} if a file was moved
    (or would be moved in dry_run mode), or None otherwise.
    When dry_run=True the filesystem check is performed but no file is renamed.
    """
    oneshots_dir = Path(storage_primary) / first_letter(vol1_title) / '%%%OneShots%%%'
    if not oneshots_dir.exists():
        return None

    vol1_stem = replace_illegal(vol1_title).lower()
    author_term = replace_illegal(author).lower()

    candidates = [
        f for f in list(oneshots_dir.glob('*.cbz')) + list(oneshots_dir.glob('*.zip'))
        if vol1_stem in f.stem.lower() and author_term in f.stem.lower()
    ]

    if len(candidates) == 1:
        candidate = candidates[0]
        bare = re.sub(r'\s*\[.*?\]\s*$', '', candidate.stem)
        short = compute_short_title(bare, series_name)
        if short and not short.isdigit() and short != series_name:
            new_name = replace_illegal(
                f'{series_name} vol.1 - {short} [{author}]',
                max_length=251,
            ) + '.cbz'
        else:
            new_name = replace_illegal(
                f'{series_name} vol.1 [{author}]',
                max_length=251,
            ) + '.cbz'
        if dry_run:
            logger.info('Retroactive move (dry run): %s -> %s', candidate.name, new_name)
            return {'from': candidate.name, 'to': new_name}
        target_dir = Path(storage_primary) / series_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        # Derive the short title from the matched file's own stem rather than
        # from vol1_title (which is the series name and would produce a wrong filename).
        # Stem is like "Series Name - Short Title [Author]" — strip [Author] then
        # strip the series name prefix.
        dest = target_dir / new_name
        candidate.rename(dest)
        logger.info('Retroactive move: %s -> %s', candidate.name, dest)
        return {'from': candidate.name, 'to': new_name}
    elif len(candidates) == 0:
        logger.warning(
            f'Vol.1 of "{series_name}" not found in %%%OneShots%%% — '
            f'may not have been downloaded yet.',
        )
    else:
        logger.warning(
            f'Ambiguous retroactive match for "{series_name}" vol.1 — '
            f'{len(candidates)} candidates found. No files moved. '
            f'Candidates: {[c.name for c in candidates]}',
        )
    return None


# ---------------------------------------------------------------------------
# CBZ generation
# ---------------------------------------------------------------------------

def _build_comic_info_xml(book: 'Book') -> str:
    root = ET.Element('ComicInfo')
    root.set('xmlns:xsi', 'http://www.w3.org/2001/XMLSchema-instance')
    root.set('xmlns:xsd', 'http://www.w3.org/2001/XMLSchema')

    def _add(tag: str, value: str | int | None) -> None:
        if value is not None and value != '':
            ET.SubElement(root, tag).text = str(value)

    _add('Title', book.title)
    _add('Writer', book.author)
    _add('PageCount', book.pages if book.pages else None)
    if book.series_name:
        _add('Series', book.series_name)
        _add('Number', book.volume_number)
    _add('Web', book.source_url)
    _add('Tags', ', '.join(book.tags) if book.tags else None)

    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding='unicode')


def pack_cbz(temp_dir: str, dest_path: str, book: 'Book') -> None:
    """
    Pack all PNGs from temp_dir into a Komga-compatible CBZ.
    Pages at root level, includes ComicInfo.xml.
    Validates the resulting ZIP before returning.
    """
    temp = Path(temp_dir)
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    pngs = sorted(temp.glob('*.png'), key=lambda p: int(p.stem))
    if not pngs:
        raise ValueError(f'No PNG files found in {temp_dir}')

    with zipfile.ZipFile(dest, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        for i, png in enumerate(pngs, start=1):
            zf.write(png, arcname=f'{i:03d}.png')
        zf.writestr('ComicInfo.xml', _build_comic_info_xml(book))

    # Validate
    with zipfile.ZipFile(dest, 'r') as zf:
        if not zf.namelist():
            raise ValueError(f'CBZ validation failed — archive is empty: {dest}')

    logger.info(f'CBZ packed: {dest} ({len(pngs)} pages)')
