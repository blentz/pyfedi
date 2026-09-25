"""The wiring that lets the suite run on several workers at once.

`run_tests.sh` passes `-n <workers> --dist loadgroup` for a whole-suite run, and
tests/conftest.py gives each worker a database and a pair of Redis databases of
its own. Everything the suite shares is otherwise global: the per-test teardown
DELETEs every row and resets every sequence to 1, so two workers against one
database would wipe each other's rows mid-test.

These are the pure parts of that wiring. The parts that are not pure -- the
`CREATE DATABASE ... TEMPLATE` each worker runs, and the distribution itself --
are exercised by every parallel run of the suite, which is the only honest test
of them.
"""
from pathlib import Path

import pytest

from tests.conftest import (MAX_XDIST_WORKERS, REDIS_DBS_PER_WORKER,
                            SHARED_STATIC_GROUP, SHARED_STATIC_MODULES,
                            is_disposable_database_url, touches_shared_static,
                            worker_database_url, worker_index, worker_redis_url)

DATABASE = 'postgresql+psycopg2://pyfedi:pyfedi@test-db:5432/pyfedi_test'
REDIS = 'redis://test-redis:6379/1'


class TestNamingAWorker:
    def test_the_first_worker(self):
        assert worker_index('gw0') == 0

    def test_a_later_one(self):
        assert worker_index('gw11') == 11

    def test_a_serial_run_has_no_index(self):
        assert worker_index(None) is None

    def test_a_name_with_no_digits_is_not_a_worker(self):
        """xdist calls the controller 'master'. It must not map onto 0:
        `build_worker_database` DROPs the database it is about to build, so a
        controller holding gw0's name would delete a running worker's database
        underneath it -- and a name with no number must not be a ValueError
        either."""
        assert worker_index('master') is None

    def test_a_process_that_is_not_a_worker_keeps_the_shared_database(self):
        assert worker_database_url(DATABASE, 'master') == DATABASE
        assert worker_redis_url(REDIS, 'master', 0) == REDIS


class TestTheTemplateIsNeverTheTarget:
    """`build_worker_database` DROPs the database it builds, so being pointed at
    the template would delete what every worker copies from."""

    def test_it_refuses_to_rebuild_the_template(self):
        from tests.conftest import build_worker_database

        with pytest.raises(RuntimeError, match='refusing to rebuild'):
            build_worker_database(DATABASE, DATABASE)

    def test_it_refuses_even_when_the_hosts_differ(self):
        from tests.conftest import build_worker_database

        with pytest.raises(RuntimeError, match='refusing to rebuild'):
            build_worker_database(
                'postgresql+psycopg2://u:p@elsewhere:5432/pyfedi_test', DATABASE)


class TestTheDatabaseEachWorkerGets:
    def test_each_worker_gets_its_own(self):
        first = worker_database_url(DATABASE, 'gw0')
        second = worker_database_url(DATABASE, 'gw1')
        assert first != second
        assert first.endswith('/pyfedi_gw0_test')
        assert second.endswith('/pyfedi_gw1_test')

    def test_the_server_is_the_same_one(self):
        assert worker_database_url(DATABASE, 'gw0').startswith(
            'postgresql+psycopg2://pyfedi:pyfedi@test-db:5432/')

    def test_a_serial_run_keeps_the_database_it_was_given(self):
        assert worker_database_url(DATABASE, None) == DATABASE

    def test_no_url_at_all(self):
        assert worker_database_url(None, 'gw0') is None

    def test_the_name_still_ends_test(self):
        """The teardown DELETEs every row in whatever database it is pointed at,
        and `is_disposable_database_url` is what stops it being pointed at
        something else. A worker database that lost the suffix would be refused
        -- which is why the index goes in the middle."""
        for worker in ('gw0', 'gw3', 'gw11'):
            url = worker_database_url(DATABASE, worker)
            assert is_disposable_database_url(url), url

    def test_a_database_whose_name_does_not_end_test_is_still_suffixed(self):
        url = worker_database_url(
            'postgresql+psycopg2://u:p@host:5432/something', 'gw2')
        assert url.endswith('/something_gw2_test')


class TestTheRedisDatabasesEachWorkerGets:
    def test_the_two_a_worker_takes_are_its_own(self):
        broker = worker_redis_url(REDIS, 'gw0', 0)
        cache = worker_redis_url(REDIS, 'gw0', 1)
        assert broker.endswith('/2')
        assert cache.endswith('/3')

    def test_no_two_workers_share_one(self):
        seen = set()
        for index in range(MAX_XDIST_WORKERS):
            for offset in range(REDIS_DBS_PER_WORKER):
                seen.add(worker_redis_url(REDIS, f'gw{index}', offset))
        assert len(seen) == MAX_XDIST_WORKERS * REDIS_DBS_PER_WORKER

    def test_none_of_them_is_one_a_serial_run_uses(self):
        """.env.test uses 0 for Celery and 1 for the cache, and a parallel run
        must not write into either -- a serial run afterwards would read what it
        left."""
        for index in range(MAX_XDIST_WORKERS):
            for offset in range(REDIS_DBS_PER_WORKER):
                assert worker_redis_url(REDIS, f'gw{index}', offset)[-2:] not in \
                    ('/0', '/1')

    def test_the_last_worker_still_fits_in_redis(self):
        """Redis ships with 16 databases, numbered 0-15, and run_tests.sh caps
        the worker count on exactly this arithmetic."""
        last = worker_redis_url(REDIS, f'gw{MAX_XDIST_WORKERS - 1}',
                                REDIS_DBS_PER_WORKER - 1)
        assert int(last.rpartition('/')[2]) <= 15

    def test_the_server_is_the_same_one(self):
        assert worker_redis_url(REDIS, 'gw2', 0).startswith('redis://test-redis:6379/')

    def test_a_serial_run_keeps_the_database_it_was_given(self):
        assert worker_redis_url(REDIS, None, 0) == REDIS

    def test_no_url_at_all(self):
        assert worker_redis_url(None, 'gw0', 0) is None


class TestWhatHasToStayOnOneWorker:
    """`app/static/media`, `/tmp` and `/posts` are one directory each for the
    whole container, so the modules that name them share a group."""

    def test_a_module_that_writes_an_upload(self):
        assert touches_shared_static('tests/test_admin_upload_forms.py')

    def test_and_the_module_that_asserts_that_directory_is_clean(self):
        """The pair from fact 650: on two workers they would see each other's
        files."""
        assert touches_shared_static('tests/test_admin_federation.py')

    def test_a_module_that_touches_nothing_shared(self):
        assert not touches_shared_static('tests/test_safe_order_by.py')

    def test_this_module_is_in_the_group_itself(self):
        """Because it names the directory in its own prose. The detection is
        textual and deliberately conservative: a module that only talks about
        `app/static/` costs one worker's parallelism and needs no judgement
        about whether it writes there."""
        assert touches_shared_static('tests/test_parallel_workers.py')

    def test_a_module_that_does_not_exist(self):
        assert not touches_shared_static('tests/test_no_such_module.py')

    def test_a_module_that_writes_there_through_app_code(self):
        """Named in SHARED_STATIC_MODULES, because it names no path itself --
        which is exactly why reading the sources could not find it."""
        assert touches_shared_static('tests/test_shared_user_bans.py')
        assert 'app/static/' not in \
            Path('tests/test_shared_user_bans.py').read_text(encoding='utf8')

    def test_every_module_in_that_list_still_exists(self):
        """The list is measured, so it goes stale when a module is renamed --
        and a stale entry is a module running in parallel that should not be."""
        for module in sorted(SHARED_STATIC_MODULES):
            assert Path(module).is_file(), module

    def test_every_module_naming_that_directory_is_found(self):
        """The property. The grouping is derived from the source rather than
        listed, so a new test that writes there joins without anybody
        remembering -- and this is what says the derivation still works."""
        missed = [str(path) for path in sorted(Path('tests').glob('test_*.py'))
                  if 'app/static/' in path.read_text(encoding='utf8')
                  and not touches_shared_static(str(path))]
        assert missed == []

    def test_there_are_modules_in_the_group_at_all(self):
        """A detection that found nothing would pass the property above in
        silence."""
        grouped = [path for path in sorted(Path('tests').glob('test_*.py'))
                   if touches_shared_static(str(path))]
        assert len(grouped) > 10


class TestTheCollectionHook:
    def test_every_test_carries_a_group(self, pytestconfig):
        """Asserted against this module's own items rather than the suite's,
        because collecting the suite inside a test would run it."""
        import pytest as pytest_module

        from tests.conftest import pytest_collection_modifyitems

        class Item:
            def __init__(self, nodeid):
                self.nodeid = nodeid
                self.markers = []

            def add_marker(self, marker):
                self.markers.append(marker)

        items = [Item('tests/test_safe_order_by.py::TestX::test_y'),
                 Item('tests/test_admin_federation.py::test_z')]
        pytest_collection_modifyitems(pytestconfig, items)
        groups = [marker.kwargs.get('name') or marker.args[0]
                  for item in items for marker in item.markers]
        assert groups == ['tests/test_safe_order_by.py', SHARED_STATIC_GROUP]

    def test_two_tests_in_one_module_get_the_same_group(self, pytestconfig):
        from tests.conftest import pytest_collection_modifyitems

        class Item:
            def __init__(self, nodeid):
                self.nodeid = nodeid
                self.markers = []

            def add_marker(self, marker):
                self.markers.append(marker)

        items = [Item('tests/test_safe_order_by.py::TestA::test_one'),
                 Item('tests/test_safe_order_by.py::TestB::test_two')]
        pytest_collection_modifyitems(pytestconfig, items)
        first, second = (item.markers[0].args[0] for item in items)
        assert first == second


class TestNothingLeaksConfigIntoLaterTests:
    """`app` is session-scoped, so `app.config['X'] = y` outlives the test.

    It cost two parallel-only failures: six bare writes of `PAGE_LENGTH` left 20
    behind, and `test_the_page_length_ladder` expects the site's 100. Serially it
    passed because of the order the modules happened to run in -- which is not a
    property of the tests, it is luck. Those six are `monkeypatch.setitem` now.

    The rest are pinned here by count rather than converted in one go: most set a
    value and restore it by hand in the same test, which is fragile (an
    exception on the way skips the restore) but not a leak in the happy path.
    The list can only shrink -- a new bare write, in any file, fails this.
    """

    KNOWN = {
        'test_activitypub_signature.py': 2, 'test_ap_create_reply.py': 2,
        'test_ap_notify_post.py': 2, 'test_app_factory.py': 1,
        'test_client_ip.py': 2, 'test_community_show.py': 4,
        'test_community_syndication.py': 3, 'test_error_handlers.py': 2,
        'test_feed_display_preferences.py': 4,
        'test_form_validate_guards_super.py': 2, 'test_instance_stickies.py': 2,
        'test_redirect_policy.py': 2, 'test_safe_redirect_target.py': 4,
        'test_shared_community_membership.py': 2,
        'test_shared_tasks_maintenance_cleanup.py': 8,
        'test_shared_tasks_maintenance_lifecycle.py': 33,
        'test_shared_upload.py': 12, 'test_user_misc.py': 4,
        'test_user_settings.py': 2, 'test_utils_request_context.py': 6,
    }

    # This module is skipped: it QUOTES the pattern in its own assertions below,
    # so scanning itself counts the guard as an offender.
    SELF = 'test_parallel_workers.py'

    def bare_writes(self):
        import collections
        import re

        pattern = re.compile(r"(app|current_app)\.config\['[A-Z_]+'\]\s*=\s")
        counts = collections.Counter()
        for path in sorted(Path('tests').glob('test_*.py')):
            if path.name == self.SELF:
                continue
            for line in path.read_text(encoding='utf8').splitlines():
                if pattern.search(line) and 'monkeypatch' not in line:
                    counts[path.name] += 1
        return counts

    def test_no_file_has_more_of_them_than_it_did(self):
        counts = self.bare_writes()
        grew = {name: count for name, count in counts.items()
                if count > self.KNOWN.get(name, 0)}
        assert grew == {}, \
            'use monkeypatch.setitem(app.config, ...) instead: ' + repr(grew)

    def test_the_page_length_writes_are_all_scoped(self):
        """The six that caused the failure, named so they cannot come back."""
        for name in ('test_topic_routes.py', 'test_feed_reading_routes.py'):
            text = (Path('tests') / name).read_text(encoding='utf8')
            assert "app.config['PAGE_LENGTH'] = " not in text, name
            assert "monkeypatch.setitem(app.config, 'PAGE_LENGTH'" in text, name

    def test_the_list_names_files_that_exist(self):
        for name in sorted(self.KNOWN):
            assert (Path('tests') / name).is_file(), name


class TestPatchingAProxyDoesNotGiveAnAsyncMock:
    """`patch('...current_app')` hands back an AsyncMock, not a MagicMock.

    `unittest.mock` picks AsyncMock when `_is_async_obj(original)` is true, and
    that asks `inspect.isawaitable`, which is satisfied by anything with
    `__await__` -- which werkzeug's LocalProxy defines so it can proxy an async
    object. `asyncio.iscoroutinefunction(current_app)` is False, so the usual
    check does not explain it; measured, the mock is an AsyncMock and so is every
    attribute of it.

    Calling `current_app.logger.exception(...)` on one therefore builds a
    coroutine nobody awaits: `RuntimeWarning: coroutine
    'AsyncMockMixin._execute_mock_call' was never awaited`, the suite's last
    three warnings. `new_callable=MagicMock` is the fix.
    """

    def test_the_proxy_is_what_mock_reads_as_async(self, app):
        """`unittest.mock._is_async_obj` is what decides, and it is satisfied by
        LocalProxy while `asyncio.iscoroutinefunction` is not -- which is why the
        usual check does not explain the AsyncMock below."""
        import asyncio
        from unittest.mock import _is_async_obj

        import app.api.alpha as alpha

        assert asyncio.iscoroutinefunction(alpha.current_app) is False
        assert _is_async_obj(alpha.current_app) is True

    def test_a_bare_patch_of_it_is_an_async_mock(self, app):
        """The behaviour being worked around, asserted so the workaround has a
        reason a reader can check."""
        from unittest.mock import AsyncMock, patch

        with patch('app.api.alpha.current_app') as mock:
            assert isinstance(mock, AsyncMock)

    def test_new_callable_gives_a_plain_mock(self, app):
        from unittest.mock import AsyncMock, MagicMock, patch

        with patch('app.api.alpha.current_app', new_callable=MagicMock) as mock:
            assert isinstance(mock, MagicMock)
            assert not isinstance(mock, AsyncMock)
            assert not isinstance(mock.logger.exception, AsyncMock)

    def test_no_test_patches_a_current_app_proxy_bare(self):
        """The property. A bare patch of any `current_app` leaves a coroutine
        behind the moment the code under test calls a method on it."""
        import re

        offenders = []
        pattern = re.compile(r"patch\((['\"])app\.[a-z_.]*current_app\1\)")
        for path in sorted(Path('tests').glob('test_*.py')):
            if path.name == 'test_parallel_workers.py':
                continue
            for number, line in enumerate(path.read_text(encoding='utf8').splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f'{path.name}:{number}')
        assert offenders == []
