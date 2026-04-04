"""ToPlace processor — scans ToPlace/ for archives, fixes names, and routes them."""

import logging
import re
import zipfile
from pathlib import Path

from book import Book
from config import Config
from fix_names import process_filename
from helper import first_letter
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

    @staticmethod
    def _count_pages(filepath: Path) -> int:
        """Count image files inside a ZIP/CBZ archive without extracting."""
        image_exts = ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp')
        with zipfile.ZipFile(filepath, 'r') as zf:
            return sum(1 for n in zf.namelist() if n.lower().endswith(image_exts))

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

        # Step 3: Count pages and detect covers
        pages = self._count_pages(filepath)
        is_cover = pages <= 4
        logger.info('%sPages: %d — %s', prefix, pages, 'cover' if is_cover else 'normal')

        # Step 4: Series detection (skip for covers, same as downloader)
        series_name = None
        volume_number = None
        short_title = None

        if not is_cover:
            # Phase A: title heuristic
            inferred = infer_series_from_title(title)
            if inferred:
                series_name, volume_number = inferred
                short_title = compute_short_title(title, series_name)
                logger.info('%sTitle heuristic: series="%s" vol.%d', prefix, series_name, volume_number)

        # Step 5: Build Book dataclass
        book = Book(
            title=title,
            author=author,
            pages=pages,
            tags=[],
            source_url='',
            series_name=series_name,
            volume_number=volume_number,
            short_title=short_title,
            is_cover=is_cover,
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
        elif book.is_cover:
            routing = 'cover'
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

                    # Check for volume markers: "vol.N", "Part N", "#N", "＃N", or bare "N"
                    def _has_volume(n_lower: str, s_lower: str, v: int) -> bool:
                        if s_lower not in n_lower:
                            return False
                        vol_patterns = [
                            f'vol.{v}', f'vol {v}', f'part {v}', f'ch.{v}', f'ch {v}',
                            f'#{v}', f'\uff03{v}',
                        ]
                        for pat in vol_patterns:
                            if pat in n_lower:
                                return True
                        # Bare number: "Series N [Author]" — extract title part and check
                        tm = re.match(r'^(.+?)\s*\[', f.stem)
                        raw_title = (tm.group(1).strip() if tm else f.stem.strip()).lower()
                        if raw_title.endswith(f' {v}') and raw_title[:-len(f' {v}')] == s_lower:
                            return True
                        return False

                    if vol == 1:
                        # Vol.1 might be stored with a volume marker
                        if _has_volume(name_lower, series_lower, vol):
                            found = True
                            break
                        # Or as oneshot (just title): "Series [Author]"
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
                        if _has_volume(name_lower, series_lower, vol):
                            found = True
                            break
                if found:
                    break
            if not found:
                missing.append(vol)
        return missing
