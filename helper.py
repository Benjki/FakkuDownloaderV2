import re
from pathlib import Path
from urllib.parse import urlparse, urlunparse


def file_exists(path: str) -> bool:
    urls_path = Path(path)
    return urls_path.is_file() and not urls_path.stat().st_size == 0


def folder_exists(path: str) -> bool:
    urls_path = Path(path)
    return urls_path.is_dir()


def create_file_if_missing(path: str) -> bool:
    if not file_exists(path):
        Path(path).touch()
        return True
    return False


def create_folder_if_missing(path: str) -> bool:
    if not folder_exists(path):
        Path(path).mkdir(parents=True, exist_ok=True)
        return True
    return False


def replace_illegal(value: str, max_length: int = 255) -> str:
    replaced = re.sub(r'[\\/*?:"<>|]', "", value)
    replaced = replaced.strip()
    if replaced.endswith("."):
        replaced = _rreplace(replaced, '.', '', 1)
    return replaced[:max_length]


def _rreplace(value: str, old: str, new: str, occurrence: int) -> str:
    li = value.rsplit(old, occurrence)
    return new.join(li)


def normalise_url(url: str) -> str:
    """Strip trailing slash and lowercase scheme+host for consistent comparison."""
    url = url.strip().rstrip('/')
    p = urlparse(url)
    return urlunparse(p._replace(scheme=p.scheme.lower(), netloc=p.netloc.lower()))


def load_done_file(path: str) -> set[str]:
    """Load done.txt into a set of normalised URLs. Returns empty set if file missing."""
    done = set()
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    done.add(normalise_url(line))
    except FileNotFoundError:
        pass
    return done


def append_done(path: str, url: str) -> None:
    """Append a normalised URL to done.txt."""
    with open(path, 'a', encoding='utf-8') as f:
        f.write(normalise_url(url) + '\n')


def first_letter(name: str) -> str:
    """Return uppercase first alphanumeric char, or '#' for digit-first titles."""
    for ch in name:
        if ch.isalpha():
            return ch.upper()
        if ch.isdigit():
            return '#'
    return '#'
