"""Filename cleaning logic for imported archives.

Transforms filenames like ``[Circle (Author)] Some Title [Extra] (Tag).zip``
into ``Some Title [Author].cbz``.
"""

import re


def process_filename(filename: str) -> tuple[str, str, str]:
    """
    Process a filename according to naming rules.

    Returns (new_filename, title, author).
    - new_filename: cleaned filename with .cbz extension
    - title: extracted title
    - author: extracted author (may be empty string)
    """
    # Strip extension
    if filename.lower().endswith('.cbz'):
        name = filename[:-4]
    elif filename.lower().endswith('.zip'):
        name = filename[:-4]
    else:
        name = filename

    # Replace underscores with spaces
    name = name.replace('_', ' ')

    # Extract author from leading bracket/paren group
    author = ''
    if name.startswith('['):
        m = re.match(r'\[([^\]]*)\]\s*', name)
        if m:
            bracket_content = m.group(1).strip()
            name = name[m.end():]
            # Check for inner parens: [Circle (Author)] -> Author
            inner = re.search(r'\(([^)]+)\)', bracket_content)
            if inner:
                author = inner.group(1).strip()
            elif bracket_content:
                author = bracket_content
    elif name.startswith('('):
        m = re.match(r'\(([^)]*)\)\s*', name)
        if m:
            paren_content = m.group(1).strip()
            name = name[m.end():]
            if paren_content:
                author = paren_content

    # Remove all remaining [...] and (...) groups
    name = re.sub(r'\[[^\]]*\]', '', name)
    name = re.sub(r'\([^)]*\)', '', name)

    # Clean up whitespace
    title = ' '.join(name.split()).strip()

    # Build filename
    if author:
        new_filename = f'{title} [{author}].cbz'
    else:
        new_filename = f'{title}.cbz'

    return new_filename, title, author
