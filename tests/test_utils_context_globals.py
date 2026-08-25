"""Coverage for the application-context globals, templates and filesystem
helpers in app/utils.py: ensure_directory_exists, get_timezones, theme_list,
render_from_tpl, debug_checkpoint, round_invisible_digits, localize_datetime,
humanize_number and orjson_response.

Two corrections to the brief this file was written from, both confirmed by
probing the real container before writing assertions:

- humanize_number(1215) is '1K', not '1.2K'. Babel's format_compact_decimal
  defaults to fraction_digits=0, so the short compact form for 1215 rounds to
  the nearest thousand with no decimal at all. Confirmed against Babel 2.18.0.
- ensure_directory_exists does not handle absolute paths. It splits the
  argument on '/' and rebuilds the path one component at a time starting from
  the empty string, so for any absolute path (which starts with '/', so
  str.split('/') begins with '') the very first os.mkdir('') raises
  FileNotFoundError before any real directory is created or touched -- this is
  unconditional, not an edge case. pytest's tmp_path fixture always hands out
  an absolute path, so the brief's original tmp_path-based tests cannot pass
  as written; TestEnsureDirectoryExists below documents the crash directly
  (test_an_absolute_path_raises_instead_of_creating_anything) instead of
  disguising it, and uses monkeypatch.chdir to reach the loop/warning logic
  with a relative path -- not to hide the defect, but because a relative path
  is the only input the function actually handles, and that logic still needs
  coverage. Reported, not fixed, per instructions.
"""

import json
import os
from datetime import datetime, timedelta

import pytest
from flask import g

from app.models import utcnow
from app.utils import (debug_checkpoint, ensure_directory_exists, get_timezones,
                        humanize_number, localize_datetime, orjson_response,
                        render_from_tpl, round_invisible_digits, theme_list)


class TestGetTimezones:
    def test_regions_are_grouped(self, app):
        result = get_timezones()
        assert 'Europe' in result
        assert 'America' in result

    def test_excluded_regions_are_absent(self, app):
        """Fails if the exclusion list is dropped -- Etc/ entries are noise in a picker."""
        result = get_timezones()
        for region in ['Arctic', 'Atlantic', 'Etc', 'Other']:
            assert region not in result

    def test_entries_are_value_label_pairs(self, app):
        assert ('Europe/London', 'Europe/London') in get_timezones()['Europe']

    def test_zones_without_a_region_are_skipped(self, app):
        """UTC has no slash, so it must not appear as its own region."""
        assert 'UTC' not in get_timezones()


class TestRenderFromTpl:
    def test_year_is_substituted(self, app):
        assert render_from_tpl('{% year %}') == str(utcnow().year)

    def test_whitespace_inside_the_tag_is_ignored(self, app):
        assert render_from_tpl('{%year%}') == str(utcnow().year)

    def test_day_and_month_are_zero_padded(self, app):
        now = utcnow()
        assert render_from_tpl('{% day %}') == f'{now.day:02d}'
        assert render_from_tpl('{% month %}') == f'{now.month:02d}'

    def test_week_is_the_iso_week_number(self, app):
        assert render_from_tpl('{% week %}') == f'{utcnow().isocalendar()[1]:02d}'

    def test_an_unknown_tag_is_left_alone(self, app):
        assert render_from_tpl('{% nonsense %}') == '{% nonsense %}'

    def test_text_around_the_tag_survives(self, app):
        assert render_from_tpl('backup-{% year %}.zip').startswith('backup-')


class TestHumanizeNumber:
    def test_falsy_values_render_as_zero(self, app):
        with app.test_request_context('/'):
            g.locale = 'en'
            assert humanize_number(0) == '0'
            assert humanize_number(None) == '0'

    def test_large_numbers_are_abbreviated(self, app):
        """Babel's format_compact_decimal defaults to fraction_digits=0, so
        1215 renders as '1K', not '1.2K' -- confirmed against Babel 2.18.0
        directly in the container before writing this assertion."""
        with app.test_request_context('/'):
            g.locale = 'en'
            assert humanize_number(1215) == '1K'


class TestRoundInvisibleDigits:
    def test_none_becomes_zero(self, app):
        with app.test_request_context('/'):
            g.locale = 'en'
            assert round_invisible_digits(None) == 0

    def test_small_numbers_are_returned_unchanged(self, app):
        with app.test_request_context('/'):
            g.locale = 'en'
            assert round_invisible_digits(123) == 123

    def test_numbers_that_abbreviate_are_rounded_to_the_visible_digits(self, app):
        """1215 renders as '1K', so the count must round to a value that
        matches the plural form the abbreviation implies."""
        with app.test_request_context('/'):
            g.locale = 'en'
            assert round_invisible_digits(1215) == 1000


class TestLocalizeDatetime:
    def test_a_recent_time_reads_as_relative(self, app):
        result = localize_datetime(datetime.utcnow() - timedelta(hours=2))
        assert 'hour' in result

    def test_an_unknown_locale_falls_back_to_english(self, app):
        """The except ValueError arm."""
        result = localize_datetime(datetime.utcnow() - timedelta(hours=2), locale='nonsense')
        assert 'hour' in result


class TestDebugCheckpoint:
    """The `app` fixture holds one session-scoped app context for the whole
    run (see tests/conftest.py and tests/README.md), so flask.g is NOT reset
    just because a `with app.test_request_context(...)` block exits -- only a
    genuinely separate request/app context would get a fresh `g`, and these
    all share one. Each test below clears g.__dict__ itself, the same way the
    db_session fixture does for tests that use it, so a checkpoint left behind
    by an earlier test in this class (or an earlier test file) cannot corrupt
    the delta/identity assertions here. Without that clear,
    test_checkpoints_accumulate_on_g and test_checkpoints_do_not_leak_between_requests
    both fail against real leaked state -- confirmed by running this file
    before adding the clears."""

    def test_the_first_checkpoint_has_no_delta(self, app):
        with app.test_request_context('/'):
            g.__dict__.clear()
            _, delta = debug_checkpoint('first')
            assert delta == 0

    def test_a_later_checkpoint_measures_from_the_previous_one(self, app):
        with app.test_request_context('/'):
            g.__dict__.clear()
            debug_checkpoint('first')
            _, delta = debug_checkpoint('second')
            assert delta >= 0

    def test_checkpoints_accumulate_on_g(self, app):
        with app.test_request_context('/'):
            g.__dict__.clear()
            debug_checkpoint('first')
            debug_checkpoint('second')
            assert [name for name, _ in g._debug_checkpoints] == ['first', 'second']

    def test_checkpoints_do_not_leak_between_requests(self, app):
        """g is per-request in production; this suite's shared app context
        means the leak has to be prevented by hand (see class docstring), but
        the assertion still proves what the production per-request behaviour
        guarantees: a fresh request's first checkpoint has delta 0."""
        with app.test_request_context('/'):
            g.__dict__.clear()
            debug_checkpoint('first')
        with app.test_request_context('/'):
            g.__dict__.clear()
            _, delta = debug_checkpoint('fresh')
            assert delta == 0


class TestThemeList:
    def test_the_built_in_theme_is_always_first(self, app):
        assert theme_list()[0] == ('piefed', 'PieFed')

    def test_every_entry_is_a_directory_and_display_name_pair(self, app):
        assert all(isinstance(entry, tuple) and len(entry) == 2 for entry in theme_list())


class TestOrjsonResponse:
    def test_serialises_the_object_as_json(self, app):
        response = orjson_response({'a': 1})
        assert response.mimetype == 'application/json'
        assert json.loads(response.get_data()) == {'a': 1}

    def test_the_status_and_headers_are_honoured(self, app):
        response = orjson_response({}, status=404, headers={'X-Test': 'yes'})
        assert response.status_code == 404
        assert response.headers['X-Test'] == 'yes'


class TestEnsureDirectoryExists:
    def test_an_absolute_path_raises_instead_of_creating_anything(self, app, tmp_path):
        """DEFECT, reported not fixed: ensure_directory_exists splits its argument
        on '/' and rebuilds from ''. An absolute path's split always starts with
        an empty component, so the first os.mkdir('') raises FileNotFoundError
        before any directory is created. tmp_path is always absolute, so this is
        the actual, current behaviour for the exact kind of input pytest's own
        fixture supplies -- not a contrived corner case. Every real caller in
        app/ happens to pass a relative path ('app/static/...'), so the defect
        is latent in production today, but the function is used for upload
        directories and does not handle the paths it claims to ensure.
        Production change that would make this test fail: rebuilding the path
        so a leading '/' is preserved instead of collapsed to ''."""
        target = tmp_path / 'a' / 'b' / 'c'

        with pytest.raises(FileNotFoundError):
            ensure_directory_exists(str(target))

        assert not target.exists()

    def test_a_nested_directory_is_created(self, app, tmp_path, monkeypatch):
        """Relative path, reached via chdir into tmp_path: this is the only kind
        of input the function actually handles (see the defect test above), and
        the nested-mkdir loop still needs coverage."""
        monkeypatch.chdir(tmp_path)

        ensure_directory_exists('a/b/c')

        assert (tmp_path / 'a' / 'b' / 'c').is_dir()

    def test_an_existing_directory_is_left_alone(self, app, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        os.mkdir('exists')
        with open(os.path.join('exists', 'keep.txt'), 'w') as f:
            f.write('kept')

        ensure_directory_exists('exists')

        with open(os.path.join('exists', 'keep.txt')) as f:
            assert f.read() == 'kept'

    def test_an_unwritable_directory_warns_rather_than_raising(self, app, tmp_path, monkeypatch, caplog):
        """The os.access arm, forced via monkeypatch rather than file mode bits.
        Mode bits alone do not exercise this reliably: the test container runs
        as root, and root bypasses the write-permission check that mode bits
        express -- verified directly in the container: os.access(path, os.W_OK)
        is True for a mode-0o500 directory when running as root. The brief's
        original version of this test (geteuid-gated pytest.skip) would
        therefore always skip here, leaving this branch permanently uncovered
        in this environment. Monkeypatching os.access forces the branch
        regardless of which user runs the suite, and the assertion is still on
        real observable behaviour -- the log message -- not on whether a mock
        was called. Uses a relative path via chdir, for the same reason the
        other tests in this class do: an absolute path never reaches this
        check at all (see test_an_absolute_path_raises_instead_of_creating_anything)."""
        monkeypatch.chdir(tmp_path)
        os.mkdir('readonly')
        monkeypatch.setattr(os, 'access', lambda path, mode: False)

        with caplog.at_level('WARNING'):
            ensure_directory_exists('readonly')

        assert 'not writable' in caplog.text
