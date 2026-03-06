"""Tests for fix_names.py"""

from fix_names import process_filename


class TestAuthorExtraction:
    def test_bracket_with_inner_parens(self):
        result = process_filename('[Circle (Author)] Title.zip')
        assert result == ('Title [Author].cbz', 'Title', 'Author')

    def test_bracket_no_inner_parens(self):
        result = process_filename('[CircleName] Title.zip')
        assert result == ('Title [CircleName].cbz', 'Title', 'CircleName')

    def test_paren_group(self):
        result = process_filename('(CircleName) Title.zip')
        assert result == ('Title [CircleName].cbz', 'Title', 'CircleName')

    def test_no_leading_group(self):
        result = process_filename('Title.zip')
        assert result == ('Title.cbz', 'Title', '')

    def test_empty_brackets(self):
        result = process_filename('[] Title.zip')
        assert result == ('Title.cbz', 'Title', '')


class TestExtensionHandling:
    def test_zip_becomes_cbz(self):
        new_name, _, _ = process_filename('[Author] Title.zip')
        assert new_name.endswith('.cbz')

    def test_cbz_stays_cbz(self):
        new_name, _, _ = process_filename('[Author] Title.cbz')
        assert new_name.endswith('.cbz')

    def test_cbz_input_full(self):
        result = process_filename('[Circle (Author)] Title.cbz')
        assert result == ('Title [Author].cbz', 'Title', 'Author')


class TestUnderscoreReplacement:
    def test_underscores_become_spaces(self):
        result = process_filename('[Author] Title_With_Underscores.zip')
        assert result == ('Title With Underscores [Author].cbz', 'Title With Underscores', 'Author')


class TestExtraTagRemoval:
    def test_removes_bracket_and_paren_tags(self):
        result = process_filename('[Circle (Author)] Title [Extra Tag] (Another Tag).zip')
        assert result == ('Title [Author].cbz', 'Title', 'Author')

    def test_multiple_extra_groups(self):
        result = process_filename('[Author] Title [Tag1] [Tag2] (Tag3).zip')
        assert result == ('Title [Author].cbz', 'Title', 'Author')


class TestAlreadyClean:
    def test_no_author_already_clean_cbz(self):
        result = process_filename('Title.cbz')
        assert result == ('Title.cbz', 'Title', '')

    def test_no_author_already_clean_zip(self):
        """Extension changes but nothing else."""
        result = process_filename('Title.zip')
        assert result == ('Title.cbz', 'Title', '')

    def test_non_leading_bracket_is_stripped(self):
        """[Author] not at start is treated as an extra tag and removed."""
        result = process_filename('Title [Author].zip')
        assert result == ('Title.cbz', 'Title', '')


class TestEdgeCases:
    def test_whitespace_trimming(self):
        """Extra spaces in the title should be collapsed."""
        result = process_filename('[Author]   Title   .zip')
        assert result == ('Title [Author].cbz', 'Title', 'Author')

    def test_title_with_spaces_between_removed_groups(self):
        result = process_filename('[Author] Some Title [Extra].zip')
        assert result == ('Some Title [Author].cbz', 'Some Title', 'Author')
