"""Tests for organizer.py"""

import zipfile
from pathlib import Path

import pytest

from book import Book
from organizer import (
    MetadataError,
    TO_FIX_MANUALLY,
    build_filename,
    check_and_move_oneshot,
    check_ownership,
    compute_short_title,
    detect_series,
    extract_cover_group,
    extract_metadata,
    infer_series_from_title,
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
        multi_collection=False,
        missing_volumes=False,
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
        multi_collection=False,
        missing_volumes=False,
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
        b = make_oneshot(pages=4, is_cover=True, title='X-Eros Pinup #82 Kito Sakeru')
        result = route_book(b).replace('\\', '/')
        assert result == 'Covers/X-Eros Pinup'

    def test_oneshot_routing(self):
        b = make_oneshot()  # title='Test Book' → letter 'T'
        result = route_book(b).replace('\\', '/')
        assert result == 'T/%%%OneShots%%%'

    def test_series_routing_letter(self):
        b = make_series(series_name='Attack on Titan', author='Isayama')
        result = route_book(b).replace('\\', '/')
        assert result.startswith('A/')

    def test_series_routing_digit_first(self):
        b = make_series(series_name='3x3 Eyes', author='Author')
        result = route_book(b).replace('\\', '/')
        assert result.startswith('0-9/')

    def test_series_folder_contains_author(self):
        b = make_series(series_name='My Series', author='Some Author')
        result = route_book(b)
        assert 'Some Author' in result

    def test_cover_takes_priority_over_series(self):
        b = make_series(is_cover=True, pages=2)
        result = route_book(b).replace('\\', '/')
        assert result.startswith('Covers/')

    def test_multi_collection_routes_to_fix_manually(self):
        b = make_oneshot(multi_collection=True)
        assert route_book(b) == TO_FIX_MANUALLY

    def test_multi_collection_takes_priority_over_cover(self):
        b = make_oneshot(pages=2, is_cover=True, multi_collection=True)
        assert route_book(b) == TO_FIX_MANUALLY

    def test_missing_volumes_routes_to_fix_manually(self):
        b = make_series(missing_volumes=True)
        assert route_book(b) == TO_FIX_MANUALLY

    def test_missing_volumes_takes_priority_over_series(self):
        b = make_series(series_name='My Series', volume_number=3, missing_volumes=True)
        assert route_book(b) == TO_FIX_MANUALLY


# ---------------------------------------------------------------------------
# build_filename
# ---------------------------------------------------------------------------

class TestBuildFilename:
    def test_oneshot_filename(self):
        b = make_oneshot(title='My Book', author='Author')
        assert build_filename(b) == 'My Book [Author].cbz'

    def test_series_filename_with_subtitle(self):
        b = make_series(series_name='MySeries', volume_number=3, short_title='Part 3', author='Auth')
        assert build_filename(b) == 'MySeries vol.3 - Part 3 [Auth].cbz'

    def test_series_filename_empty_short_title(self):
        # short_title equals series_name (compute_short_title fallback) — omit subtitle
        b = make_series(series_name='Tropical Remnants', volume_number=1,
                        short_title='Tropical Remnants', author='Auth')
        assert build_filename(b) == 'Tropical Remnants vol.1 [Auth].cbz'

    def test_series_filename_bare_number_short_title(self):
        # short_title is a bare integer — omit subtitle
        b = make_series(series_name='Dark Pleasure', volume_number=2,
                        short_title='2', author='Auth')
        assert build_filename(b) == 'Dark Pleasure vol.2 [Auth].cbz'

    def test_series_filename_none_short_title(self):
        # short_title is None — omit subtitle
        b = make_series(series_name='My Series', volume_number=1,
                        short_title=None, author='Auth')
        assert build_filename(b) == 'My Series vol.1 [Auth].cbz'

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

    def test_title_equals_series_returns_title(self):
        # short is empty after strip → falls back to full title
        result = compute_short_title('Wonderful Long Distance!', 'Wonderful Long Distance!')
        assert result == 'Wonderful Long Distance!'

    def test_word_level_fallback_comma(self):
        # comma in title missing from series name → exact startswith fails → word match
        result = compute_short_title(
            'Akari-chan, Be My Onahole ~Part 1~',
            'Akari-chan Be My Onahole',
        )
        assert result == '~Part 1~'

    def test_word_level_fallback_part2(self):
        result = compute_short_title(
            'Akari-chan, Be My Onahole ~Part 2~',
            'Akari-chan Be My Onahole',
        )
        assert result == '~Part 2~'

    def test_word_level_no_match_returns_full(self):
        # Completely different title — word-level also fails → full title
        result = compute_short_title('Something Else Entirely', 'Akari-chan Be My Onahole')
        assert result == 'Something Else Entirely'


# ---------------------------------------------------------------------------
# check_and_move_oneshot
# ---------------------------------------------------------------------------

class TestCheckAndMoveOneshot:
    def test_moves_single_match(self, tmp_path):
        oneshots = tmp_path / 'M' / '%%%OneShots%%%'  # vol1_title='My Series Chapter 1' → M
        oneshots.mkdir(parents=True)
        cbz = oneshots / 'My Series Chapter 1 [Author].cbz'
        cbz.write_bytes(b'fake')

        series_rel = str(Path('M') / 'My Series [Author]')
        check_and_move_oneshot('My Series', 'Author', 'My Series Chapter 1', str(tmp_path), series_rel)

        dest_dir = tmp_path / series_rel
        assert dest_dir.exists()
        assert len(list(dest_dir.glob('*.cbz'))) == 1

    def test_no_match_does_not_crash(self, tmp_path):
        oneshots = tmp_path / 'V' / '%%%OneShots%%%'  # vol1_title='Vol 1' → V
        oneshots.mkdir(parents=True)
        # No files present — should log warning but not raise
        check_and_move_oneshot('Ghost Series', 'Author', 'Vol 1', str(tmp_path), 'G/Ghost Series [Author]')

    def test_multiple_matches_no_move(self, tmp_path):
        oneshots = tmp_path / 'M' / '%%%OneShots%%%'  # vol1_title='My Series Chapter 1' → M
        oneshots.mkdir(parents=True)
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
<a data-attribute-count="100" href="/tags/romance">Romance</a>
<a data-attribute-count="200" href="/genres/ecchi">Ecchi</a>
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

    def test_missing_author_returns_empty_string(self):
        html = '<html><body><h1 class="block col-span-full text-2xl font-bold text-brand-light text-left dark:text-white dark:link:text-white pt-0">Title</h1></body></html>'
        meta = extract_metadata(html)
        assert meta['author'] == ''


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

        book = Book(title='Test', author='Author', pages=3, tags=['Romance', 'Comedy'])
        dest = str(tmp_path / 'test.cbz')
        pack_cbz(str(temp), dest, book)

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

        book = Book(title='Tagged', author='Author', pages=1, tags=['Action', 'Drama'],
                    source_url='https://www.fakku.net/hentai/tagged')
        dest = str(tmp_path / 'tagged.cbz')
        pack_cbz(str(temp), dest, book)

        with zipfile.ZipFile(dest) as zf:
            xml = zf.read('ComicInfo.xml').decode()
        assert 'Action' in xml
        assert 'Drama' in xml
        assert 'Tagged' in xml
        assert 'Author' in xml
        assert 'fakku.net' in xml

    def test_raises_on_empty_dir(self, tmp_path):
        empty = tmp_path / 'empty'
        empty.mkdir()
        with pytest.raises(ValueError, match='No PNG'):
            pack_cbz(str(empty), str(tmp_path / 'out.cbz'), Book())


# ---------------------------------------------------------------------------
# detect_series
# ---------------------------------------------------------------------------

# Mirrors the actual Playwright-rendered HTML structure from FAKKU (March 2026).
# Volume list uses <b><a> (NOT <strong><a>) and <div> (NOT <p>) for the number.
_SERIES_HTML_TEMPLATE = """\
<html><body>
<div class="col-span-full w-full text-left text-sm">
  <h2>This chapter is part of <em><a href="/collections/my-test-series">My Test Series</a></em>.</h2>
</div>
<ul class="relative col-span-full w-full space-y-2">
  <li class="chapter">
    <div class="flex-none text-right text-sm">1</div>
    <div class="w-full flex-1 text-left">
      <b><a href="/hentai/my-test-series-chapter-1">Chapter 1 Title</a></b>
    </div>
    <div class="hidden"><a href="/hentai/my-test-series-chapter-1">Start Reading</a></div>
  </li>
  <li class="chapter active">
    <div class="flex-none text-right text-sm">2</div>
    <div class="w-full flex-1 text-left">
      <b><a href="/hentai/my-test-series-chapter-2">Chapter 2 Title</a></b>
    </div>
    <div class="hidden"><a href="/hentai/my-test-series-chapter-2">Start Reading</a></div>
  </li>
</ul>
</body></html>"""


class TestDetectSeries:
    def test_detects_series_name(self):
        name, vol, _ = detect_series(
            _SERIES_HTML_TEMPLATE,
            'https://www.fakku.net/hentai/my-test-series-chapter-2',
            None,
        )
        assert name == 'My Test Series'

    def test_detects_volume_number_vol2(self):
        _, vol, _ = detect_series(
            _SERIES_HTML_TEMPLATE,
            'https://www.fakku.net/hentai/my-test-series-chapter-2',
            None,
        )
        assert vol == 2

    def test_detects_volume_number_vol1(self):
        _, vol, _ = detect_series(
            _SERIES_HTML_TEMPLATE,
            'https://www.fakku.net/hentai/my-test-series-chapter-1',
            None,
        )
        assert vol == 1

    def test_no_series_returns_none_triple(self):
        name, vol, short = detect_series(
            '<html><body><p>No collection here</p></body></html>',
            'https://www.fakku.net/hentai/standalone',
            None,
        )
        assert (name, vol, short) == (None, None, None)

    def test_multiple_collections_returns_sentinel(self):
        """Books in multiple collections return the multi_collection sentinel."""
        html = """\
<html><body>
<div>
  <h2>This chapter is part of <em><a href="/collections/series-a">Series A</a></em>.</h2>
</div>
<ul>
  <li>
    <div>1</div>
    <div><b><a href="/hentai/some-book">Some Book</a></b></div>
  </li>
</ul>
<div>
  <h2>This chapter is part of <em><a href="/collections/umbrella-collab">Big Collab</a></em>.</h2>
</div>
<ul>
  <li>
    <div>5</div>
    <div><b><a href="/hentai/some-book">Some Book</a></b></div>
  </li>
</ul>
</body></html>"""
        name, vol, short = detect_series(
            html,
            'https://www.fakku.net/hentai/some-book',
            None,
        )
        assert name == '__multi_collection__'
        assert vol is None
        assert short is None

    def test_book_not_in_list_gets_next_volume(self):
        """Book in series but not in the volume list gets last listed + 1."""
        _, vol, _ = detect_series(
            _SERIES_HTML_TEMPLATE,
            'https://www.fakku.net/hentai/my-test-series-chapter-3-unlisted',
            None,
        )
        # List has vol 1 and 2, so unlisted book should get vol 3
        assert vol == 3

    def test_short_title_is_none(self):
        """detect_series always returns None for short_title; caller computes it."""
        _, _, short = detect_series(
            _SERIES_HTML_TEMPLATE,
            'https://www.fakku.net/hentai/my-test-series-chapter-2',
            None,
        )
        assert short is None


# ---------------------------------------------------------------------------
# infer_series_from_title
# ---------------------------------------------------------------------------

class TestInferSeriesFromTitle:
    def test_basic_vol2(self):
        assert infer_series_from_title('Dark Pleasure 2') == ('Dark Pleasure', 2)

    def test_higher_volume(self):
        assert infer_series_from_title('Something Else 5') == ('Something Else', 5)

    def test_multi_word_series(self):
        assert infer_series_from_title('My Long Series Title 3') == ('My Long Series Title', 3)

    def test_vol1_returns_none(self):
        # Vol 1 is intentionally excluded — it goes to oneshots and gets rescued later
        assert infer_series_from_title('Dark Pleasure 1') is None

    def test_no_trailing_number_returns_none(self):
        assert infer_series_from_title('Dark Pleasure') is None

    def test_non_numeric_suffix_returns_none(self):
        assert infer_series_from_title('Dark Pleasure II') is None

    def test_number_mid_title_returns_none(self):
        # Number is not at the end
        assert infer_series_from_title('3D Something') is None

    def test_single_word_with_number(self):
        assert infer_series_from_title('Series 2') == ('Series', 2)


# ---------------------------------------------------------------------------
# extract_cover_group
# ---------------------------------------------------------------------------

class TestExtractCoverGroup:
    @pytest.mark.parametrize('title, expected', [
        # Spec examples — delimiter: #N
        ('X-Eros Pinup #82 Kito Sakeru',                                           'X-Eros Pinup'),
        ('X-Eros Girls Collection #62  Akinosora [Akinosora]',                     'X-Eros Girls Collection'),
        # Spec examples — delimiter: keyword + number
        ('Kari-YUG Vol. 52 [YUG]',                                                 'Kari-YUG'),
        ("Cover's Comment Part 158 NaPaTa",                                        "Cover's Comment"),
        # Spec examples — delimiter: date-like
        ('Kairakuten Heroines 2020-12 - Remu',                                     'Kairakuten Heroines'),
        ('Bavel 2020-0607 Double Cover',                                            'Bavel'),
        # Spec examples — delimiter: standalone dash
        ("BEAST Cover Girl - Miu's Summer \u2764 by Nakamachi Machi [Nakamachi Machi]", 'BEAST Cover Girl'),
        # Spec examples — no delimiter, 3-word fallback
        ('48 Sex Positions Under the Kotatsu',                                     '48 Sex Positions'),
        # Spec examples — delimiter: bare integer, prefix > 3 tokens → cap
        ('Weekly Kairakuten Key-Visual Collection 95 [Aramaki Echizen]',           'Weekly Kairakuten Key-Visual'),
        # Single word, no delimiter
        ('Kairakuten',                                                              'Kairakuten'),
    ])
    def test_spec_examples(self, title, expected):
        assert extract_cover_group(title) == expected

    def test_keyword_without_following_number_does_not_fire(self):
        # 'Vol.' at end has no following numeric token → rule 1 skipped → fallback first 3
        # replace_illegal strips trailing period, so result loses the '.'
        assert extract_cover_group('My Book Vol.') == 'My Book Vol'

    def test_author_tag_stripped_before_processing(self):
        result_bare   = extract_cover_group('X-Eros Pinup #82 Kito Sakeru')
        result_tagged = extract_cover_group('X-Eros Pinup #82 Kito Sakeru [Kito Sakeru]')
        assert result_bare == result_tagged

    def test_delimiter_at_index_zero_uses_fallback(self):
        # First token is a bare integer → empty prefix → fallback gives first 3 tokens
        assert extract_cover_group('48 Sex Positions Under the Kotatsu') == '48 Sex Positions'

    def test_two_word_title_no_delimiter(self):
        assert extract_cover_group('Bavel Special') == 'Bavel Special'

    def test_hyphenated_token_counts_as_one(self):
        # 'Key-Visual' is one token; result must be exactly 3 tokens
        result = extract_cover_group('Weekly Kairakuten Key-Visual Collection 95')
        assert result == 'Weekly Kairakuten Key-Visual'
