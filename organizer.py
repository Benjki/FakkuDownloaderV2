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
        raise MetadataError('Cannot find author — CSS selector may need updating')
    author = other_divs[0].get_text(strip=True)

    # Page count
    page_divs = [d for d in other_divs if 'pages' in d.get_text()]
    if not page_divs:
        pages = 1
    else:
        page_text = page_divs[-1].get_text(strip=True)
        m = re.search(r'(\d+)', page_text)
        pages = int(m.group(1)) if m else 1

    # Tags
    tag_links = soup.select('a[href*="/tags/"]') or soup.select('a[href*="/genres/"]')
    tags = [a.get_text(strip=True) for a in tag_links if a.get_text(strip=True)]

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

    1. Parse 'This chapter is part of X' text from the book info page.
    2. If found, fetch <book_url>/collections and find the book's position.
    3. short_title is returned as None here — caller computes it via compute_short_title().
    """
    soup = BeautifulSoup(html, 'lxml')

    part_of_text = soup.find(
        string=re.compile(r'This chapter is part of', re.IGNORECASE),
    )
    if not part_of_text:
        return None, None, None

    series_name = re.sub(
        r'This chapter is part of\s*', '', str(part_of_text), flags=re.IGNORECASE,
    ).strip().strip('.')
    if not series_name:
        return None, None, None

    logger.debug(f'Series detected: {series_name}')

    # Fetch the collections page for ordered volume list
    collections_url = normalise_url(book_url) + '/collections'
    try:
        resp = session.get(collections_url, timeout=15)
        coll_soup = BeautifulSoup(resp.text, 'lxml')
    except Exception as e:
        logger.warning(f'Failed to fetch collections page: {e}. Treating as one-shot.')
        return None, None, None

    # Extract ordered book entries
    entries = coll_soup.find_all('a', href=re.compile(r'/hentai/'))
    seen = set()
    ordered_entries = []
    for a in entries:
        href = a.get('href', '')
        if href and href not in seen:
            seen.add(href)
            ordered_entries.append(href)

    # Find position (1-based)
    book_path = '/' + book_url.split('fakku.net/')[-1].rstrip('/')
    volume_number = None
    for i, href in enumerate(ordered_entries, start=1):
        if href.rstrip('/') == book_path.rstrip('/'):
            volume_number = i
            break

    if volume_number is None:
        logger.warning(
            f'Book not found in collection list for series "{series_name}". '
            f'Defaulting to position 1.',
        )
        volume_number = 1

    return series_name, volume_number, None


def compute_short_title(title: str, series_name: str) -> str:
    """Strip series_name prefix from title (case-insensitive). Returns full title if no match."""
    if title.lower().startswith(series_name.lower()):
        short = title[len(series_name):].lstrip(' -:').strip()
        return short if short else title
    return title


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def route_book(book: Book) -> str:
    """
    Return destination directory path relative to storage_primary.
    Priority:
    1. is_cover (pages <= 4) -> 'Covers'
    2. series_name is not None -> '<Letter>/<Series> [Author]'
    3. default (one-shot) -> '%%%OneShots%%%'
    """
    if book.is_cover:
        return 'Covers'
    if book.is_series():
        letter = first_letter(book.series_name)
        folder_name = replace_illegal(f'{book.series_name} [{book.author}]')
        return str(Path(letter) / folder_name)
    return '%%%OneShots%%%'


# ---------------------------------------------------------------------------
# Filename generation
# ---------------------------------------------------------------------------

def build_filename(book: Book) -> str:
    """
    Returns filename WITH .cbz extension.
    Multi-volume: '<Series> vol.<N> - <Short Title> [<Author>].cbz'
    One-shot/Cover: '<Title> [<Author>].cbz'
    255-char limit on stem before adding extension.
    """
    if book.is_series():
        stem = f'{book.series_name} vol.{book.volume_number} - {book.short_title} [{book.author}]'
    else:
        stem = f'{book.title} [{book.author}]'
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
) -> None:
    """
    When vol.N (N>=2) is detected and series_dir doesn't exist yet,
    check if vol.1 is stranded in %%%OneShots%%% and move it.
    """
    oneshots_dir = Path(storage_primary) / '%%%OneShots%%%'
    if not oneshots_dir.exists():
        return

    vol1_stem = replace_illegal(vol1_title).lower()
    author_term = replace_illegal(author).lower()

    candidates = [
        f for f in oneshots_dir.glob('*.cbz')
        if vol1_stem in f.stem.lower() and author_term in f.stem.lower()
    ]

    if len(candidates) == 1:
        target_dir = Path(storage_primary) / series_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        new_name = replace_illegal(
            f'{series_name} vol.1 - {vol1_title} [{author}]',
            max_length=251,
        ) + '.cbz'
        dest = target_dir / new_name
        candidates[0].rename(dest)
        logger.info(f'Retroactive move: {candidates[0].name} -> {dest}')
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


# ---------------------------------------------------------------------------
# CBZ generation
# ---------------------------------------------------------------------------

def _build_comic_info_xml(tags: list[str]) -> str:
    root = ET.Element('ComicInfo')
    root.set('xmlns:xsi', 'http://www.w3.org/2001/XMLSchema-instance')
    root.set('xmlns:xsd', 'http://www.w3.org/2001/XMLSchema')
    tags_el = ET.SubElement(root, 'Tags')
    tags_el.text = ', '.join(tags) if tags else ''
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding='unicode')


def pack_cbz(temp_dir: str, dest_path: str, tags: list[str]) -> None:
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
        zf.writestr('ComicInfo.xml', _build_comic_info_xml(tags))

    # Validate
    with zipfile.ZipFile(dest, 'r') as zf:
        if not zf.namelist():
            raise ValueError(f'CBZ validation failed — archive is empty: {dest}')

    logger.info(f'CBZ packed: {dest} ({len(pngs)} pages)')
