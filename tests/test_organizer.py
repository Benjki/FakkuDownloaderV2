"""Tests for organizer.py"""

import zipfile
from pathlib import Path

import pytest

from book import Book
from organizer import (
    MetadataError,
    build_filename,
    check_and_move_oneshot,
    check_ownership,
    compute_short_title,
    extract_metadata,
    pack_cbz,
    route_book,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_oneshot(**kw):
    defaults = dict(
        title='Test Book',
        author='Test Author',
        pages=10,
        tags=[],
        source_url='',
        series_name=None,
        volume_number=None,
        short_title=None,
        is_cover=False,
    )
    defaults.update(kw)
    return Book(**defaults)


def make_series(**kw):
    defaults = dict(
        title='Series Chapter 1',
        author='Author',
        pages=10,
        tags=[],
        source_url='',
        series_name='Series',
        volume_number=1,
        short_title='Chapter 1',
        is_cover=False,
    )
    defaults.update(kw)
    return Book(**defaults)


def _fake_png() -> bytes:
    """Minimal valid-ish PNG header."""
    return b'\x89PNG\r\n\x1a\n' + b'\x00' * 100


# ---------------------------------------------------------------------------
# route_book
# ---------------------------------------------------------------------------

class TestRouteBook:
    def test_cover_routing(self):
        b = make_oneshot(pages=4, is_cover=True)
        assert route_book(b) == 'Covers'

    def test_oneshot_routing(self):
        b = make_oneshot()
        assert route_book(b) == '%%%OneShots%%%'

    def test_series_routing_letter(self):
        b = make_series(series_name='Attack on Titan', author='Isayama')
        result = route_book(b).replace('\\', '/')
        assert result.startswith('A/')

    def test_series_routing_digit_first(self):
        b = make_series(series_name='3x3 Eyes', author='Author')
        result = route_book(b).replace('\\', '/')
        assert result.startswith('#/')

    def test_series_folder_contains_author(self):
        b = make_series(series_name='My Series', author='Some Author')
        result = route_book(b)
        assert 'Some Author' in result

    def test_cover_takes_priority_over_series(self):
        b = make_series(is_cover=True, pages=2)
        assert route_book(b) == 'Covers'


# ---------------------------------------------------------------------------
# build_filename
# ---------------------------------------------------------------------------

class TestBuildFilename:
    def test_oneshot_filename(self):
        b = make_oneshot(title='My Book', author='Author')
        assert build_filename(b) == 'My Book [Author].cbz'

    def test_series_filename(self):
        b = make_series(series_name='MySeries', volume_number=3, short_title='Part 3', author='Auth')
        assert build_filename(b) == 'MySeries vol.3 - Part 3 [Auth].cbz'

    def test_ends_with_cbz(self):
        assert build_filename(make_oneshot()).endswith('.cbz')

    def test_max_length_enforced(self):
        b = make_oneshot(title='A' * 300, author='B' * 10)
        fname = build_filename(b)
        assert len(fname) <= 255

    def test_illegal_chars_stripped(self):
        b = make_oneshot(title='My/Book:Title', author='Au*thor')
        fname = build_filename(b)
        for ch in r'/*:':
            assert ch not in fname


# ---------------------------------------------------------------------------
# compute_short_title
# ---------------------------------------------------------------------------

class TestComputeShortTitle:
    def test_strips_prefix(self):
        assert compute_short_title('My Series Chapter 1', 'My Series') == 'Chapter 1'

    def test_no_match_returns_full(self):
        assert compute_short_title('Some Other Title', 'My Series') == 'Some Other Title'

    def test_case_insensitive(self):
        result = compute_short_title('MY SERIES Chapter 1', 'My Series')
        assert result == 'Chapter 1'

    def test_strips_leading_separator(self):
        result = compute_short_title('My Series - Chapter 1', 'My Series')
        assert result == 'Chapter 1'


# ---------------------------------------------------------------------------
# check_and_move_oneshot
# ---------------------------------------------------------------------------

class TestCheckAndMoveOneshot:
    def test_moves_single_match(self, tmp_path):
        oneshots = tmp_path / '%%%OneShots%%%'
        oneshots.mkdir()
        cbz = oneshots / 'My Series Chapter 1 [Author].cbz'
        cbz.write_bytes(b'fake')

        series_rel = str(Path('M') / 'My Series [Author]')
        check_and_move_oneshot('My Series', 'Author', 'My Series Chapter 1', str(tmp_path), series_rel)

        dest_dir = tmp_path / series_rel
        assert dest_dir.exists()
        assert len(list(dest_dir.glob('*.cbz'))) == 1

    def test_no_match_does_not_crash(self, tmp_path):
        oneshots = tmp_path / '%%%OneShots%%%'
        oneshots.mkdir()
        # No files present — should log warning but not raise
        check_and_move_oneshot('Ghost Series', 'Author', 'Vol 1', str(tmp_path), 'G/Ghost Series [Author]')

    def test_multiple_matches_no_move(self, tmp_path):
        oneshots = tmp_path / '%%%OneShots%%%'
        oneshots.mkdir()
        (oneshots / 'My Series Chapter 1 [Author] v1.cbz').write_bytes(b'x')
        (oneshots / 'My Series Chapter 1 [Author] v2.cbz').write_bytes(b'x')

        series_rel = 'M/My Series [Author]'
        check_and_move_oneshot('My Series', 'Author', 'My Series Chapter 1', str(tmp_path), series_rel)

        dest = tmp_path / series_rel
        # Either dir doesn't exist, or it exists with 0 CBZs
        if dest.exists():
            assert len(list(dest.glob('*.cbz'))) == 0

    def test_no_oneshots_dir_is_noop(self, tmp_path):
        # %%%OneShots%%% doesn't exist — should silently return
        check_and_move_oneshot('X', 'Y', 'Z', str(tmp_path), 'X/X [Y]')


# ---------------------------------------------------------------------------
# extract_metadata
# ---------------------------------------------------------------------------

SAMPLE_HTML = """\
<html><body>
<h1 class="block col-span-full text-2xl font-bold text-brand-light text-left dark:text-white dark:link:text-white pt-0">
  Test Manga Title
</h1>
<div class="text-default-link table-cell w-full space-y-2 text-left align-top [&>a]:inline">Great Author</div>
<div class="text-default-link table-cell w-full space-y-2 text-left align-top [&>a]:inline">Some Parody</div>
<div class="text-default-link table-cell w-full space-y-2 text-left align-top [&>a]:inline">220 pages</div>
<a href="/tags/romance">Romance</a>
<a href="/genres/ecchi">Ecchi</a>
</body></html>"""


class TestExtractMetadata:
    def test_extracts_title(self):
        meta = extract_metadata(SAMPLE_HTML)
        assert meta['title'] == 'Test Manga Title'

    def test_extracts_author(self):
        meta = extract_metadata(SAMPLE_HTML)
        assert meta['author'] == 'Great Author'

    def test_extracts_pages(self):
        meta = extract_metadata(SAMPLE_HTML)
        assert meta['pages'] == 220

    def test_extracts_tags(self):
        meta = extract_metadata(SAMPLE_HTML)
        assert 'Romance' in meta['tags'] or 'Ecchi' in meta['tags']

    def test_missing_title_raises(self):
        with pytest.raises(MetadataError):
            extract_metadata('<html><body></body></html>')

    def test_missing_author_raises(self):
        html = '<html><body><h1 class="block col-span-full text-2xl font-bold text-brand-light text-left dark:text-white dark:link:text-white pt-0">Title</h1></body></html>'
        with pytest.raises(MetadataError):
            extract_metadata(html)


# ---------------------------------------------------------------------------
# check_ownership
# ---------------------------------------------------------------------------

class TestCheckOwnership:
    def test_owned_book_read_link(self):
        html = '<html><body><a href="/hentai/foo/read">Read</a></body></html>'
        assert check_ownership(html) is True

    def test_owned_book_read_text(self):
        html = '<html><body><button>Read</button></body></html>'
        assert check_ownership(html) is True

    def test_unowned_book(self):
        html = '<html><body><p>Purchase to read</p></body></html>'
        assert check_ownership(html) is False


# ---------------------------------------------------------------------------
# pack_cbz
# ---------------------------------------------------------------------------

class TestPackCbz:
    def test_creates_valid_cbz(self, tmp_path):
        temp = tmp_path / 'pages'
        temp.mkdir()
        for i in range(1, 4):
            (temp / f'{i}.png').write_bytes(_fake_png())

        dest = str(tmp_path / 'test.cbz')
        pack_cbz(str(temp), dest, ['Romance', 'Comedy'])

        assert Path(dest).exists()
        with zipfile.ZipFile(dest) as zf:
            names = zf.namelist()
        assert '001.png' in names
        assert '002.png' in names
        assert '003.png' in names
        assert 'ComicInfo.xml' in names

    def test_cbz_contains_tags_in_xml(self, tmp_path):
        temp = tmp_path / 'p'
        temp.mkdir()
        (temp / '1.png').write_bytes(_fake_png())

        dest = str(tmp_path / 'tagged.cbz')
        pack_cbz(str(temp), dest, ['Action', 'Drama'])

        with zipfile.ZipFile(dest) as zf:
            xml = zf.read('ComicInfo.xml').decode()
        assert 'Action' in xml
        assert 'Drama' in xml

    def test_raises_on_empty_dir(self, tmp_path):
        empty = tmp_path / 'empty'
        empty.mkdir()
        with pytest.raises(ValueError, match='No PNG'):
            pack_cbz(str(empty), str(tmp_path / 'out.cbz'), [])
