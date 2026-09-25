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

    def test_a_name_with_no_digits_counts_as_the_first(self):
        """xdist calls the controller 'master'; nothing asks it for a database,
        but a name that arrives without a number must not be a ValueError."""
        assert worker_index('master') == 0


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
