"""ToPlace processor — scans ToPlace/ for archives, fixes names, and routes them."""

import logging
import re
from pathlib import Path

from book import Book
from config import Config
from fix_names import process_filename
from helper import first_letter, replace_illegal
from organizer import (
    TO_FIX_MANUALLY,
    build_filename,
    check_and_move_oneshot,
    compute_short_title,
    infer_series_from_title,
    route_book,
)

logger = logging.getLogger(__name__)


class Placer:
    def __init__(self, config: Config):
        self._config = config

    def run(self, dry_run: bool = False) -> list[dict]:
        """Scan ToPlace/ for archives and route each one. Returns list of report dicts."""
        toplace = Path(self._config.to_place_dir)
        if not toplace.exists():
            logger.info('ToPlace directory does not exist: %s — skipping.', toplace)
            return []

        files = sorted(
            [f for f in toplace.iterdir() if f.is_file() and f.suffix.lower() in ('.zip', '.cbz')]
        )
        if not files:
            logger.info('No .zip or .cbz files in ToPlace — nothing to process.')
            return []

        logger.info('ToPlace: found %d file(s) to process.', len(files))
        reports = []
        for filepath in files:
            try:
                report = self.place_book(filepath, dry_run=dry_run)
                reports.append(report)
            except Exception as e:
                logger.error('Failed to process %s: %s', filepath.name, e, exc_info=True)
                reports.append({
                    'original_filename': filepath.name,
                    'error': str(e),
                    'source': 'toplace',
                })
        return reports

    def place_book(self, filepath: Path, dry_run: bool = False) -> dict:
        """Process a single archive file from ToPlace/."""
        original_name = filepath.name
        prefix = '[DRY RUN] ' if dry_run else ''
        logger.info('%sProcessing: %s', prefix, original_name)

        # Step 1: Fix name
        new_filename, title, author = process_filename(filepath.name)

        # Step 1b: process_filename strips trailing [Author] from "Title [Author].cbz"
        # as a remaining bracket group. If no author was found from a leading bracket,
        # try to recover the author from a trailing [Author] in the original filename.
        if not author:
            orig_stem = filepath.stem.replace('_', ' ')
            m = re.search(r'\[([^\]]+)\]\s*$', orig_stem)
            if m:
                author = m.group(1).strip()
                new_filename = f'{title} [{author}].cbz'

        if not title:
            raise ValueError(f'Empty title after processing filename: {original_name}')

        # Step 2: Rename in-place (skip if dry_run)
        if new_filename != original_name:
            logger.info('%sRename: %s -> %s', prefix, original_name, new_filename)
        if not dry_run:
            new_path = filepath.parent / new_filename
            if new_path != filepath:
                if new_path.exists():
                    logger.warning('Target filename already exists in ToPlace, skipping rename: %s', new_filename)
                else:
                    filepath.rename(new_path)
                    filepath = new_path

        # Step 3: Series detection
        series_name = None
        volume_number = None
        short_title = None

        # Phase A: title heuristic
        inferred = infer_series_from_title(title)
        if inferred:
            series_name, volume_number = inferred
            short_title = compute_short_title(title, series_name)
            logger.info('%sTitle heuristic: series="%s" vol.%d', prefix, series_name, volume_number)

        # Phase B: filesystem scan (only if series detected from title)
        found_existing = False
        if series_name and volume_number and volume_number >= 2:
            found_existing = self._scan_for_existing_volumes(series_name, author, first_letter(title))
            logger.info('%sFilesystem scan for existing volumes: %s', prefix, 'found' if found_existing else 'not found')

        # Step 4: Build Book dataclass
        book = Book(
            title=title,
            author=author,
            pages=0,
            tags=[],
            source_url='',
            series_name=series_name,
            volume_number=volume_number,
            short_title=short_title,
            is_cover=False,
            multi_collection=False,
            missing_volumes=False,
            file_conflict=False,
        )

        # Step 5-6: Route
        oneshot_move = None
        missing_vol_nums = []

        if book.is_series() and volume_number >= 2:
            # Step 7: Retroactive move
            rel_route = route_book(book)
            oneshot_move = check_and_move_oneshot(
                series_name, author, series_name,
                self._config.storage_primary, rel_route,
                dry_run=dry_run,
            )

            # Step 8: Missing volume check
            missing_vol_nums = self._check_missing_volumes(book, rel_route)
            if missing_vol_nums:
                book.missing_volumes = True

        # Compute route (may have changed due to missing_volumes flag)
        rel_route = route_book(book)

        # Step 9: File conflict check
        if rel_route == TO_FIX_MANUALLY:
            dest_dir = Path(self._config.to_fix_manually_dir)
        else:
            dest_dir = Path(self._config.storage_primary) / rel_route

        filename = build_filename(book)
        dest_path = dest_dir / filename

        if not book.missing_volumes and not book.file_conflict:
            # Check for conflict at destination
            if dest_path.exists() or dest_path.with_suffix('.zip').exists():
                book.file_conflict = True
                rel_route = route_book(book)
                dest_dir = Path(self._config.to_fix_manually_dir)
                dest_path = dest_dir / filename

        # Determine routing label
        if book.file_conflict:
            routing = 'file_conflict'
        elif book.missing_volumes:
            routing = 'missing_volumes'
        elif book.is_series():
            routing = 'series'
        else:
            routing = 'oneshot'

        # Step 10-11: Move file
        label = routing.upper().replace('_', ' ')
        logger.info('%s[%-18s] %s -> %s', prefix, label, book.display_name(), dest_path)
        if missing_vol_nums:
            logger.warning('%s  Missing preceding volumes: %s', prefix,
                           ', '.join(f'vol.{k}' for k in missing_vol_nums))
        if oneshot_move:
            logger.info('%s  Moved vol.1: %s -> %s', prefix, oneshot_move['from'], oneshot_move['to'])
        if not dry_run:
            dest_dir.mkdir(parents=True, exist_ok=True)
            filepath.rename(dest_path)
            logger.info('Placed: %s', dest_path)

        return {
            'display_name': book.display_name(),
            'title': title,
            'author': author,
            'routing': routing,
            'original_filename': original_name,
            'cbz_filename': filename,
            'cbz_path': str(dest_path),
            'series_name': series_name,
            'volume_number': volume_number,
            'oneshot_move': oneshot_move,
            'missing_vol_nums': missing_vol_nums,
            'series_dir': str(rel_route),
            'error': None,
            'source': 'toplace',
        }

    def _scan_for_existing_volumes(self, title: str, author: str, letter: str) -> bool:
        """Check if a matching file exists in OneShots or letter root."""
        storage = Path(self._config.storage_primary)
        oneshots_dir = storage / letter / '%%%OneShots%%%'
        letter_dir = storage / letter

        title_norm = title.lower().strip()
        author_norm = author.lower().strip()

        for search_dir in [oneshots_dir, letter_dir]:
            if not search_dir.exists():
                continue
            for f in list(search_dir.glob('*.cbz')) + list(search_dir.glob('*.zip')):
                if not f.is_file():
                    continue
                stem = f.stem
                # Extract file's title and author from "Title [Author]" pattern
                file_author = ''
                m = re.match(r'^(.+?)\s*\[([^\]]+)\]\s*$', stem)
                if m:
                    file_title = m.group(1).strip().lower()
                    file_author = m.group(2).strip().lower()
                else:
                    file_title = stem.strip().lower()

                if file_author == author_norm and title_norm.startswith(file_title):
                    return True
        return False

    def _check_missing_volumes(self, book: Book, series_dir_rel: str) -> list[int]:
        """Check if all preceding volumes exist. Returns list of missing volume numbers."""
        if not book.is_series() or book.volume_number <= 1:
            return []

        series_dir = Path(self._config.storage_primary) / series_dir_rel
        oneshots_dir = Path(self._config.storage_primary) / first_letter(book.series_name) / '%%%OneShots%%%'
        letter_dir = Path(self._config.storage_primary) / first_letter(book.series_name)

        missing = []
        for vol in range(1, book.volume_number):
            found = False
            # Check series dir
            for search_dir in [series_dir, oneshots_dir, letter_dir]:
                if not search_dir.exists():
                    continue
                for f in list(search_dir.glob('*.cbz')) + list(search_dir.glob('*.zip')):
                    if not f.is_file():
                        continue
                    name_lower = f.stem.lower()
                    series_lower = book.series_name.lower()
                    # Match "Series vol.N" or "Series" (for vol.1 as oneshot)
                    if vol == 1:
                        # Vol.1 might be stored as oneshot (just title) or as "Series vol.1"
                        if f'vol.{vol}' in name_lower and series_lower in name_lower:
                            found = True
                            break
                        # Check if it's the oneshot form: "Series [Author]"
                        m = re.match(r'^(.+?)\s*\[([^\]]+)\]\s*$', f.stem)
                        if m:
                            file_title = m.group(1).strip().lower()
                            file_author = m.group(2).strip().lower()
                        else:
                            file_title = f.stem.strip().lower()
                            file_author = ''
                        if file_title == series_lower and file_author == book.author.lower():
                            found = True
                            break
                    else:
                        if f'vol.{vol}' in name_lower and series_lower in name_lower:
                            found = True
                            break
                if found:
                    break
            if not found:
                missing.append(vol)
        return missing
