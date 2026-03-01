"""Tests for helper.py"""

from helper import (
    append_done,
    first_letter,
    load_done_file,
    normalise_url,
    replace_illegal,
)


class TestNormaliseUrl:
    def test_strips_trailing_slash(self):
        assert normalise_url('https://www.fakku.net/hentai/foo/') == 'https://www.fakku.net/hentai/foo'

    def test_strips_whitespace(self):
        assert normalise_url('  https://www.fakku.net/hentai/foo  ') == 'https://www.fakku.net/hentai/foo'

    def test_idempotent(self):
        url = 'https://www.fakku.net/hentai/foo'
        assert normalise_url(url) == normalise_url(normalise_url(url))

    def test_preserves_path_case(self):
        url = 'https://www.fakku.net/hentai/FooBar'
        result = normalise_url(url)
        assert result.startswith('https://www.fakku.net')
        assert 'FooBar' in result

    def test_bare_string_unchanged(self):
        assert normalise_url('not-a-url') == 'not-a-url'

    def test_multiple_trailing_slashes(self):
        # rstrip('/') removes all trailing slashes
        result = normalise_url('https://www.fakku.net/hentai/foo///')
        assert not result.endswith('/')


class TestReplaceIllegal:
    def test_removes_illegal_chars(self):
        for ch in r'\/*?:"<>|':
            assert ch not in replace_illegal(f'foo{ch}bar')

    def test_strips_trailing_dot(self):
        assert not replace_illegal('foo.').endswith('.')

    def test_max_length(self):
        assert len(replace_illegal('a' * 300, max_length=255)) <= 255

    def test_default_max_length_255(self):
        assert len(replace_illegal('b' * 300)) <= 255

    def test_normal_string_unchanged(self):
        assert replace_illegal('hello world') == 'hello world'

    def test_empty_string(self):
        assert replace_illegal('') == ''


class TestLoadDoneFile:
    def test_returns_empty_set_if_missing(self, tmp_path):
        result = load_done_file(str(tmp_path / 'nonexistent.txt'))
        assert result == set()

    def test_loads_and_normalises(self, tmp_path):
        f = tmp_path / 'done.txt'
        f.write_text(
            'https://www.fakku.net/hentai/foo/\nhttps://www.fakku.net/hentai/bar\n'
        )
        result = load_done_file(str(f))
        assert 'https://www.fakku.net/hentai/foo' in result
        assert 'https://www.fakku.net/hentai/bar' in result

    def test_skips_blank_lines(self, tmp_path):
        f = tmp_path / 'done.txt'
        f.write_text('\nhttps://www.fakku.net/hentai/foo\n\n')
        result = load_done_file(str(f))
        assert len(result) == 1

    def test_deduplicates(self, tmp_path):
        f = tmp_path / 'done.txt'
        f.write_text(
            'https://www.fakku.net/hentai/foo/\nhttps://www.fakku.net/hentai/foo\n'
        )
        result = load_done_file(str(f))
        assert len(result) == 1


class TestAppendDone:
    def test_appends_normalised_url(self, tmp_path):
        f = tmp_path / 'done.txt'
        f.write_text('')
        append_done(str(f), 'https://www.fakku.net/hentai/foo/')
        lines = f.read_text().strip().splitlines()
        assert lines == ['https://www.fakku.net/hentai/foo']

    def test_appends_multiple(self, tmp_path):
        f = tmp_path / 'done.txt'
        f.write_text('')
        append_done(str(f), 'https://www.fakku.net/hentai/foo/')
        append_done(str(f), 'https://www.fakku.net/hentai/bar/')
        lines = f.read_text().strip().splitlines()
        assert len(lines) == 2


class TestFirstLetter:
    def test_returns_upper_alpha(self):
        assert first_letter('Attack on Titan') == 'A'

    def test_digit_first_returns_hash(self):
        assert first_letter('3D Custom Girl') == '#'

    def test_empty_returns_hash(self):
        assert first_letter('') == '#'

    def test_symbol_prefix_skips_to_alpha(self):
        assert first_letter('!Zoey') == 'Z'
