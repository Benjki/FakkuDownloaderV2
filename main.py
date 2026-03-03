"""Entry point for FakkuDownloader."""

import argparse
import logging
import traceback

import notifier as notifier_module
from auth import ensure_authenticated
from browser import Browser
from config import load_config
from downloader import Downloader


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

    browser = Browser(config)
    browser.start()

    try:
        ensure_authenticated(browser, config, notifier_module)
        downloader = Downloader(browser, config)
        if args.dry_run:
            downloader.run_dry_run()
        else:
            downloader.run()
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


if __name__ == '__main__':
    main()
