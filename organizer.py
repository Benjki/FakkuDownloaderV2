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
    1. multi_collection or missing_volumes -> 'TO FIX MANUALLY'
    2. is_cover (pages <= 4) -> 'Covers'
    3. series_name is not None -> '<Letter>/<Series> [Author]'
    4. default (one-shot) -> '<Letter>/%%%OneShots%%%'
    """
    if book.multi_collection or book.missing_volumes:
        return 'TO FIX MANUALLY'
    if book.is_cover:
        return 'Covers'
    if book.is_series():
        letter = first_letter(book.series_name)
        folder_name = replace_illegal(f'{book.series_name} [{book.author}]')
        return str(Path(letter) / folder_name)
    letter = first_letter(book.title)
    return str(Path(letter) / '%%%OneShots%%%')


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
) -> dict | None:
    """
    When vol.N (N>=2) is detected and series_dir doesn't exist yet,
    check if vol.1 is stranded in %%%OneShots%%% and move it.

    Returns a dict {'from': old_name, 'to': new_name} if a file was moved,
    or None otherwise.
    """
    oneshots_dir = Path(storage_primary) / first_letter(vol1_title) / '%%%OneShots%%%'
    if not oneshots_dir.exists():
        return None

    vol1_stem = replace_illegal(vol1_title).lower()
    author_term = replace_illegal(author).lower()

    candidates = [
        f for f in oneshots_dir.glob('*.cbz')
        if vol1_stem in f.stem.lower() and author_term in f.stem.lower()
    ]

    if len(candidates) == 1:
        candidate = candidates[0]
        target_dir = Path(storage_primary) / series_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        # Derive the short title from the matched file's own stem rather than
        # from vol1_title (which is the series name and would produce a wrong filename).
        # Stem is like "Series Name - Short Title [Author]" — strip [Author] then
        # strip the series name prefix.
        bare = re.sub(r'\s*\[.*?\]\s*$', '', candidate.stem)
        short = compute_short_title(bare, series_name)
        new_name = replace_illegal(
            f'{series_name} vol.1 - {short} [{author}]',
            max_length=251,
        ) + '.cbz'
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
