"""Entry point for FakkuDownloader."""

import argparse
import logging
import traceback

import notifier as notifier_module
from auth import ensure_authenticated
from browser import Browser
from config import load_config
from downloader import Downloader
from placer import Placer


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='FakkuDownloader — headless manga scraper powered by Playwright',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Print download plan without downloading anything',
    )
    return parser.parse_args()


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )



def main():
    args = parse_args()
    setup_logging()
    config = load_config()

    # Phase 1: FAKKU downloads (skip when TO_PLACE_ONLY=true)
    dl_result = None
    if not config.to_place_only:
        browser = Browser(config)
        browser.start()
        try:
            ensure_authenticated(browser, config, notifier_module)
            downloader = Downloader(browser, config)
            if args.dry_run:
                dl_result = downloader.run_dry_run()
            else:
                dl_result = downloader.run()
        except KeyboardInterrupt:
            logging.getLogger('main').info('Interrupted by user.')
        except Exception as e:
            tb = traceback.format_exc()
            logging.getLogger('main').error('Fatal error: %s\n%s', e, tb)
            try:
                notifier_module.send_error(config, '', None, str(e), tb)
            except Exception:
                pass
        finally:
            browser.close()

    # Phase 2: Process ToPlace folder (no browser needed)
    placer = Placer(config)
    toplace_reports = placer.run(dry_run=args.dry_run)

    # Send combined email
    if dl_result or toplace_reports:
        reports, elapsed = dl_result or ([], '00:00:00')
        notifier_module.send_success(
            config, reports, elapsed,
            dry_run=args.dry_run,
            toplace_reports=toplace_reports or None,
        )


if __name__ == '__main__':
    main()
