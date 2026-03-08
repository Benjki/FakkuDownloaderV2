"""Tests for placer.py — ToPlace processor."""

import zipfile
from pathlib import Path

import pytest

from config import Config
from placer import Placer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    fakku = root / 'Fakku'
    fakku.mkdir()
    toplace = root / 'ToPlace'
    toplace.mkdir()
    tfm = root / 'TO FIX MANUALLY'
    # Don't create tfm — let placer create it

    return Config(
        fakku_username='', fakku_password='', fakku_totp_secret='',
        fakku_collection_url='', smtp_host='', smtp_port=587,
        smtp_user='', smtp_password='', smtp_from='', smtp_to='',
        storage_root=str(root),
        storage_primary=str(fakku),
        to_place_dir=str(toplace),
        to_fix_manually_dir=str(tfm),
        done_file='', cookies_file='', temp_dir='',
        page_timeout=15, page_wait=15, book_wait=30,
        min_image_size_kb=50, allowed_image_dimensions=[],
        max_retry=3, chrome_offset=None,
        to_place_only=False,
    )


def _touch(directory: Path, name: str, pages: int = 10) -> Path:
    """Create a valid ZIP file with dummy image entries."""
    f = directory / name
    with zipfile.ZipFile(f, 'w') as zf:
        for i in range(pages):
            zf.writestr(f'page_{i:03d}.png', b'fake image data')
    return f


# ---------------------------------------------------------------------------
# TestFixAndRename
# ---------------------------------------------------------------------------

class TestFixAndRename:
    """Place a [Author] Title.zip file, verify it gets renamed then moved."""

    def test_bracket_author_zip_renamed_and_placed(self, tmp_path):
        cfg = _make_config(tmp_path)
        toplace = Path(cfg.to_place_dir)
        _touch(toplace, '[Circle (AuthorName)] Some Title [Extra].zip')

        placer = Placer(cfg)
        reports = placer.run()

        assert len(reports) == 1
        r = reports[0]
        assert r['error'] is None
        assert r['author'] == 'AuthorName'
        assert r['title'] == 'Some Title'
        assert r['original_filename'] == '[Circle (AuthorName)] Some Title [Extra].zip'
        # File should no longer be in ToPlace
        assert not list(toplace.iterdir())

    def test_underscore_removal(self, tmp_path):
        cfg = _make_config(tmp_path)
        toplace = Path(cfg.to_place_dir)
        _touch(toplace, '[Author]_My_Cool_Title.zip')

        placer = Placer(cfg)
        reports = placer.run()

        assert len(reports) == 1
        assert reports[0]['title'] == 'My Cool Title'
        assert reports[0]['error'] is None

    def test_already_clean_filename(self, tmp_path):
        cfg = _make_config(tmp_path)
        toplace = Path(cfg.to_place_dir)
        _touch(toplace, 'Nice Title [Author].cbz')

        placer = Placer(cfg)
        reports = placer.run()

        assert len(reports) == 1
        assert reports[0]['title'] == 'Nice Title'
        assert reports[0]['author'] == 'Author'

    def test_zip_extension_normalized_to_cbz(self, tmp_path):
        cfg = _make_config(tmp_path)
        toplace = Path(cfg.to_place_dir)
        _touch(toplace, 'My Book [Author].zip')

        placer = Placer(cfg)
        reports = placer.run()

        assert len(reports) == 1
        assert reports[0]['cbz_filename'].endswith('.cbz')


# ---------------------------------------------------------------------------
# TestCoverRouting
# ---------------------------------------------------------------------------

class TestCoverRouting:
    """Files with <= 4 pages are routed as covers."""

    def test_cover_routed_to_covers_folder(self, tmp_path):
        cfg = _make_config(tmp_path)
        toplace = Path(cfg.to_place_dir)
        _touch(toplace, 'X-Eros Pinup #82 Kito Sakeru [Author].cbz', pages=3)

        placer = Placer(cfg)
        reports = placer.run()

        assert len(reports) == 1
        r = reports[0]
        assert r['error'] is None
        assert r['routing'] == 'cover'
        dest = Path(r['cbz_path'])
        assert dest.exists()
        assert 'Covers' in str(dest)

    def test_4_pages_is_cover(self, tmp_path):
        cfg = _make_config(tmp_path)
        toplace = Path(cfg.to_place_dir)
        _touch(toplace, 'Some Pinup [Artist].cbz', pages=4)

        placer = Placer(cfg)
        reports = placer.run()

        assert reports[0]['routing'] == 'cover'

    def test_5_pages_is_not_cover(self, tmp_path):
        cfg = _make_config(tmp_path)
        toplace = Path(cfg.to_place_dir)
        _touch(toplace, 'Some Story [Artist].cbz', pages=5)

        placer = Placer(cfg)
        reports = placer.run()

        assert reports[0]['routing'] == 'oneshot'

    def test_cover_skips_series_detection(self, tmp_path):
        """A cover with a volume-like title should NOT be treated as a series."""
        cfg = _make_config(tmp_path)
        toplace = Path(cfg.to_place_dir)
        _touch(toplace, 'Magazine Cover 2 [Artist].cbz', pages=2)

        placer = Placer(cfg)
        reports = placer.run()

        r = reports[0]
        assert r['routing'] == 'cover'
        assert r['series_name'] is None
        assert r['volume_number'] is None


# ---------------------------------------------------------------------------
# TestOneshotRouting
# ---------------------------------------------------------------------------

class TestOneshotRouting:
    """File with no series indicator -> goes to OneShots."""

    def test_oneshot_goes_to_oneshots_folder(self, tmp_path):
        cfg = _make_config(tmp_path)
        toplace = Path(cfg.to_place_dir)
        _touch(toplace, 'Standalone Story [Author].cbz')

        placer = Placer(cfg)
        reports = placer.run()

        assert len(reports) == 1
        r = reports[0]
        assert r['routing'] == 'oneshot'
        dest = Path(r['cbz_path'])
        assert dest.exists()
        assert '%%%OneShots%%%' in str(dest)
        assert r['cbz_filename'] == 'Standalone Story [Author].cbz'

    def test_vol1_title_goes_to_oneshots(self, tmp_path):
        """A title ending in '1' should NOT trigger series detection."""
        cfg = _make_config(tmp_path)
        toplace = Path(cfg.to_place_dir)
        _touch(toplace, 'Dark Pleasure 1 [Author].cbz')

        placer = Placer(cfg)
        reports = placer.run()

        assert len(reports) == 1
        assert reports[0]['routing'] == 'oneshot'


# ---------------------------------------------------------------------------
# TestSeriesDetection
# ---------------------------------------------------------------------------

class TestSeriesDetection:
    """Vol.2 with existing vol.1 in OneShots -> series folder, vol.1 moved."""

    def test_vol2_with_existing_vol1_creates_series(self, tmp_path):
        cfg = _make_config(tmp_path)
        fakku = Path(cfg.storage_primary)
        toplace = Path(cfg.to_place_dir)

        # Place vol.1 in OneShots
        oneshots = fakku / 'D' / '%%%OneShots%%%'
        oneshots.mkdir(parents=True)
        _touch(oneshots, 'Dark Pleasure [TestAuthor].cbz')

        # Drop vol.2 in ToPlace
        _touch(toplace, 'Dark Pleasure 2 [TestAuthor].cbz')

        placer = Placer(cfg)
        reports = placer.run()

        assert len(reports) == 1
        r = reports[0]
        assert r['routing'] == 'series'
        assert r['series_name'] == 'Dark Pleasure'
        assert r['volume_number'] == 2
        # Vol.1 should have been retroactively moved
        assert r['oneshot_move'] is not None
        # Series folder should exist
        series_dir = fakku / 'D' / 'Dark Pleasure [TestAuthor]'
        assert series_dir.exists()
        # Vol.1 should be in series folder now
        assert len(list(series_dir.glob('*vol.1*'))) == 1
        # Vol.2 should also be in series folder
        assert len(list(series_dir.glob('*vol.2*'))) == 1

    def test_vol3_with_vol1_and_vol2_present(self, tmp_path):
        cfg = _make_config(tmp_path)
        fakku = Path(cfg.storage_primary)
        toplace = Path(cfg.to_place_dir)

        # Series folder with vol.1 and vol.2 already placed
        series_dir = fakku / 'M' / 'My Story [Author]'
        series_dir.mkdir(parents=True)
        _touch(series_dir, 'My Story vol.1 [Author].cbz')
        _touch(series_dir, 'My Story vol.2 [Author].cbz')

        # Drop vol.3
        _touch(toplace, 'My Story 3 [Author].cbz')

        placer = Placer(cfg)
        reports = placer.run()

        assert len(reports) == 1
        r = reports[0]
        assert r['routing'] == 'series'
        assert r['volume_number'] == 3
        assert r['missing_vol_nums'] == []


# ---------------------------------------------------------------------------
# TestMissingVolumes
# ---------------------------------------------------------------------------

class TestMissingVolumes:
    """Vol.3 with no vol.1 or vol.2 -> TO FIX MANUALLY."""

    def test_missing_volumes_routes_to_fix_manually(self, tmp_path):
        cfg = _make_config(tmp_path)
        toplace = Path(cfg.to_place_dir)

        # Drop vol.3 with no prior volumes anywhere
        _touch(toplace, 'Ghost Series 3 [Author].cbz')

        placer = Placer(cfg)
        reports = placer.run()

        assert len(reports) == 1
        r = reports[0]
        assert r['routing'] == 'missing_volumes'
        dest = Path(r['cbz_path'])
        assert dest.exists()
        assert 'TO FIX MANUALLY' in str(dest)

    def test_vol3_missing_vol2_only(self, tmp_path):
        """Vol.1 exists but vol.2 is missing -> still TO FIX MANUALLY."""
        cfg = _make_config(tmp_path)
        fakku = Path(cfg.storage_primary)
        toplace = Path(cfg.to_place_dir)

        # Vol.1 in OneShots
        oneshots = fakku / 'S' / '%%%OneShots%%%'
        oneshots.mkdir(parents=True)
        _touch(oneshots, 'Some Series [Author].cbz')

        # Drop vol.3 (vol.2 missing)
        _touch(toplace, 'Some Series 3 [Author].cbz')

        placer = Placer(cfg)
        reports = placer.run()

        assert len(reports) == 1
        r = reports[0]
        assert r['routing'] == 'missing_volumes'


# ---------------------------------------------------------------------------
# TestFileConflict
# ---------------------------------------------------------------------------

class TestFileConflict:
    """Destination already exists -> TO FIX MANUALLY."""

    def test_file_conflict_routes_to_fix_manually(self, tmp_path):
        cfg = _make_config(tmp_path)
        fakku = Path(cfg.storage_primary)
        toplace = Path(cfg.to_place_dir)

        # Pre-existing file at the destination
        oneshots = fakku / 'M' / '%%%OneShots%%%'
        oneshots.mkdir(parents=True)
        _touch(oneshots, 'My Book [Author].cbz')

        # Drop same-named file in ToPlace
        _touch(toplace, 'My Book [Author].cbz')

        placer = Placer(cfg)
        reports = placer.run()

        assert len(reports) == 1
        r = reports[0]
        assert r['routing'] == 'file_conflict'
        dest = Path(r['cbz_path'])
        assert 'TO FIX MANUALLY' in str(dest)


# ---------------------------------------------------------------------------
# TestDryRun
# ---------------------------------------------------------------------------

class TestDryRun:
    """Verify no files are moved/renamed when dry_run=True."""

    def test_dry_run_no_files_moved(self, tmp_path):
        cfg = _make_config(tmp_path)
        toplace = Path(cfg.to_place_dir)
        original = _touch(toplace, '[Author] My Title.zip')

        placer = Placer(cfg)
        reports = placer.run(dry_run=True)

        assert len(reports) == 1
        r = reports[0]
        assert r['error'] is None
        # Original file should still be in ToPlace, untouched
        assert original.exists()
        # No file should exist at the destination
        fakku = Path(cfg.storage_primary)
        oneshots_files = list(fakku.rglob('*.cbz'))
        assert len(oneshots_files) == 0

    def test_dry_run_no_rename(self, tmp_path):
        cfg = _make_config(tmp_path)
        toplace = Path(cfg.to_place_dir)
        original = _touch(toplace, '[Author] My Title.zip')

        placer = Placer(cfg)
        placer.run(dry_run=True)

        # Original filename should still exist (not renamed)
        assert original.exists()
        # Renamed file should NOT exist
        assert not (toplace / 'My Title [Author].cbz').exists()


# ---------------------------------------------------------------------------
# TestEmptyToPlace
# ---------------------------------------------------------------------------

class TestEmptyToPlace:
    """Empty folder -> empty reports list."""

    def test_empty_folder_returns_empty(self, tmp_path):
        cfg = _make_config(tmp_path)
        placer = Placer(cfg)
        reports = placer.run()
        assert reports == []

    def test_missing_folder_returns_empty(self, tmp_path):
        cfg = _make_config(tmp_path)
        # Remove the ToPlace dir
        import shutil
        shutil.rmtree(cfg.to_place_dir)

        placer = Placer(cfg)
        reports = placer.run()
        assert reports == []


# ---------------------------------------------------------------------------
# TestCleanup
# ---------------------------------------------------------------------------

class TestCleanup:
    """Source file deleted from ToPlace after successful placement."""

    def test_source_removed_after_placement(self, tmp_path):
        cfg = _make_config(tmp_path)
        toplace = Path(cfg.to_place_dir)
        src = _touch(toplace, 'Clean Me Up [Author].cbz')

        placer = Placer(cfg)
        reports = placer.run()

        assert len(reports) == 1
        assert reports[0]['error'] is None
        # Source should be gone from ToPlace
        assert not src.exists()
        assert not list(toplace.iterdir())

    def test_failed_file_stays_in_toplace(self, tmp_path):
        """If processing fails, the file should remain in ToPlace."""
        cfg = _make_config(tmp_path)
        toplace = Path(cfg.to_place_dir)
        # Create a file that will fail — empty filename after cleaning
        src = _touch(toplace, '[].cbz')

        placer = Placer(cfg)
        reports = placer.run()

        assert len(reports) == 1
        assert reports[0]['error'] is not None
        # File should still be in ToPlace
        assert src.exists()


# ---------------------------------------------------------------------------
# TestMultipleFiles
# ---------------------------------------------------------------------------

class TestMultipleFiles:
    """Multiple files processed in a single run."""

    def test_multiple_files_all_processed(self, tmp_path):
        cfg = _make_config(tmp_path)
        toplace = Path(cfg.to_place_dir)
        _touch(toplace, 'Book A [Author1].cbz')
        _touch(toplace, 'Book B [Author2].cbz')
        _touch(toplace, 'Book C [Author3].zip')

        placer = Placer(cfg)
        reports = placer.run()

        assert len(reports) == 3
        assert all(r['error'] is None for r in reports)
        # ToPlace should be empty
        assert not list(toplace.iterdir())

    def test_one_failure_does_not_stop_others(self, tmp_path):
        cfg = _make_config(tmp_path)
        toplace = Path(cfg.to_place_dir)
        _touch(toplace, '[].cbz')  # will fail
        _touch(toplace, 'Good Book [Author].cbz')  # should succeed

        placer = Placer(cfg)
        reports = placer.run()

        assert len(reports) == 2
        errors = [r for r in reports if r['error'] is not None]
        successes = [r for r in reports if r['error'] is None]
        assert len(errors) == 1
        assert len(successes) == 1
