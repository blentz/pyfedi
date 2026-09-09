"""Group D of `app/shared/tasks/maintenance.py` -- the external-service tasks.

Sub-project 29 closed Group A (no transport) in
`tests/test_shared_tasks_maintenance_cleanup.py`; sub-project 30 closed Group B
(content lifecycle) in `tests/test_shared_tasks_maintenance_lifecycle.py`.
Group D is the five that leave the database:

  `refresh_instance_chooser:975`   `add_remote_communities:1070`
  `add_remote_community_from_post:1101`
  `delete_from_s3:1121`            `clean_up_tmp:1141`

AN UNMATCHED RESPX REQUEST DOES NOT FAIL EVERY TEST IN THIS FILE, and which
tests it fails is the first thing this round established.
`respx.models.AllMockedAssertionError` descends from `AssertionError`, not from
`httpx.HTTPError`. So `add_remote_communities:1077`'s narrow
`except httpx.HTTPError` lets it through and the test fails, while
`refresh_instance_chooser:1010`'s bare `except Exception` SWALLOWS it into the
"Failed to connect" branch -- deleting the domain's InstanceChooser row and
continuing. A test there that mistypes a URL exercises the failure path while
believing it exercised the success path. That is fact 148's shape in a new
place.

GET_REQUEST SLEEPS ON RETRY. `app/utils.py:158-162` and `:173-177` each
`sleep(random.randint(3, 10))` before retrying, so a test driving `get_request`
into either handler costs 3-10 seconds. No test here does. Failure paths are
reached through the caller's own handler, or by returning a non-200 status,
which does not retry. `get_request` is imported at module scope
(`maintenance.py:19`) and called as a bare name at `:1009` and `:1072`, so
`add_remote_communities:1077`'s `except httpx.HTTPError` and
`refresh_instance_chooser:1010`'s bare `except Exception` are both reached
below by replacing `get_request` itself with a raising stand-in
(`monkeypatch.setattr('app.shared.tasks.maintenance.get_request', ...)`) --
REPLACING the real function costs nothing, unlike DRIVING it into its own
retry logic. Neither handler is left open in this file.

`random.shuffle` AT `:999` makes node order nondeterministic. No test asserts on
processing order; the oracle is the resulting set of rows.

HELPERS BELONGING TO OTHER MODULES ARE ARRANGED, NOT EXERCISED --
`search_for_community` (`app/community/util.py:34`), `find_language_or_create`
(`app/activitypub/util.py:384`) and `boto3.session.Session`. Each is replaced in
THIS module's namespace with a recorder, so the tests assert on the handover
rather than on another module's behaviour (fact 179).

NOT EVERY PATCH BELOW IS NAMESPACE-LOCAL THE WAY THE PARAGRAPH ABOVE
DESCRIBES. `'app.shared.tasks.maintenance.os.remove'`,
`'...maintenance.os.path.exists'` and `'...maintenance.random.shuffle'`
resolve through `maintenance.os` and `maintenance.random`, which ARE the
stdlib `os` and `random` modules themselves (plain `import os` / `import
random`, not a name rebound in this module) -- so those three patches set
`os.remove`, `posixpath.exists` and `random.shuffle` process-wide for the
duration of the test, not merely inside `maintenance`'s own namespace.
`monkeypatch` reverts each one and nothing else runs concurrently inside the
call, so there is no live bug; the distinction matters only if a future
target is shared more widely than this module's tests are.
"""

import os
import tempfile
import time
from types import SimpleNamespace

import httpx
import pytest

from app import db
from app.models import InstanceChooser
from app.shared.tasks.maintenance import (
    add_remote_communities, add_remote_community_from_post, clean_up_tmp,
    delete_from_s3, refresh_instance_chooser,
)


class _Recorder:
    """Replace a module-level callable and remember how it was called.

    `calls` holds one `(args, kwargs)` 2-tuple per invocation -- the full
    positional-args tuple and the keyword-args dict, not positional
    arguments alone. Callers index `[0]` first to reach the positional args
    tuple, then `[0]` again for the first positional argument specifically
    (`c[0][0]` at, e.g., `:414`, `:424`, `:514`, `:615`;
    `recorder.calls[0][0]` at `:880`, indexing the same two levels without
    the comprehension). Used for helpers belonging to other modules, so a
    test asserts on what was handed over rather than on what the callee then
    did (fact 179).
    """

    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


class _StubS3:
    """A boto3 client stand-in recording `delete_objects` and `close`.

    `boto3.session.Session().client(...)` makes no network call (fact 181), but
    `delete_objects` does. This records both calls and can be made to raise, so
    `delete_from_s3`'s failure path is reachable without an endpoint.
    """

    def __init__(self, raise_on_delete=None):
        self.deleted = []
        self.closed = False
        self.raise_on_delete = raise_on_delete

    def delete_objects(self, **kwargs):
        self.deleted.append(kwargs)
        if self.raise_on_delete:
            raise self.raise_on_delete

    def close(self):
        self.closed = True


class _StubBoto3Session:
    """Stands in for `boto3.session.Session`, returning a fixed client."""

    def __init__(self, client):
        self._client = client

    def __call__(self):
        return self

    def client(self, **kwargs):
        self.client_kwargs = kwargs
        return self._client


class TestDeleteFromS3:
    """`delete_from_s3:1121` -- delete a batch of keys from object storage.

    Eight statements (measured via `--cov-report=term-missing`), ZERO branch
    points, and the only task in this module with no `except` and no session.
    `:1134`'s `try` wraps `:1135`'s `delete_objects` so `:1136`'s `finally`
    always runs `:1137`'s `s3.close()`, even when `delete_objects` raises.
    That was this round's PC3 and is now fixed by the production change these
    tests exercise.
    """

    def _patch_boto3(self, monkeypatch, client):
        """Replace `boto3` in THIS module's namespace with a stand-in.

        `:1126` reads `boto3.session.Session()`, so the stand-in needs a
        `session` attribute carrying a `Session` callable. `_StubBoto3Session`
        returns itself when called, so `Session()` yields the object whose
        `client(...)` hands back the stub.
        """
        stub = _StubBoto3Session(client)
        monkeypatch.setattr('app.shared.tasks.maintenance.boto3',
                            SimpleNamespace(session=SimpleNamespace(Session=stub)))
        return stub

    def test_the_keys_are_sent_as_a_delete_payload(self, db_session, monkeypatch):
        client = _StubS3()
        self._patch_boto3(monkeypatch, client)

        delete_from_s3(['a.png', 'b.png'])

        assert len(client.deleted) == 1
        payload = client.deleted[0]['Delete']
        assert [o['Key'] for o in payload['Objects']] == ['a.png', 'b.png']
        assert payload['Quiet'] is True

    def test_an_empty_list_still_issues_one_call(self, db_session, monkeypatch):
        """`:1123`'s comprehension over an empty list.

        The task does not guard against an empty batch, so it sends a delete
        with no objects. This test pins that as the current behaviour rather
        than asserting it is desirable -- the guard's absence is registered,
        not fixed.
        """
        client = _StubS3()
        self._patch_boto3(monkeypatch, client)

        delete_from_s3([])

        assert client.deleted[0]['Delete']['Objects'] == []

    def test_the_client_is_closed_on_the_success_path(self, db_session, monkeypatch):
        client = _StubS3()
        self._patch_boto3(monkeypatch, client)

        delete_from_s3(['a.png'])

        assert client.closed is True

    def test_the_client_is_closed_when_the_delete_raises(self, db_session, monkeypatch):
        """PC3: `:1135` raises, but `:1136`'s `finally` still runs `:1137`'s
        `s3.close()`, so the client's connection pool no longer leaks on the
        failure path.
        """
        client = _StubS3(raise_on_delete=RuntimeError('s3 is down'))
        self._patch_boto3(monkeypatch, client)

        with pytest.raises(RuntimeError, match='s3 is down'):
            delete_from_s3(['a.png'])

        assert client.closed is True

    def test_the_client_is_built_from_s3_config_and_targets_the_configured_bucket(
            self, db_session, monkeypatch, app):
        """`:1128-1132`'s five configuration reads and `:1135`'s `Bucket=`.

        Every test above patches `boto3` but never reads
        `_StubBoto3Session.client_kwargs` (`:103`) or `_patch_boto3`'s return
        value (`:129`), so `:1128-1132` and `:1135` were executed by all four
        but asserted by none -- swapping `S3_BUCKET` for `S3_REGION` at
        `:1135` survived the whole suite. It would still survive here too if
        the five `S3_*` settings shared their common empty-string test
        default, since a swap between two identical values is unobservable;
        this test gives each one a distinct value through the `app` fixture
        so a swap of any pair is caught.
        """
        monkeypatch.setitem(app.config, 'S3_REGION', 'test-region')
        monkeypatch.setitem(app.config, 'S3_ENDPOINT', 'https://s3.example.test')
        monkeypatch.setitem(app.config, 'S3_ACCESS_KEY', 'test-access-key')
        monkeypatch.setitem(app.config, 'S3_ACCESS_SECRET', 'test-access-secret')
        monkeypatch.setitem(app.config, 'S3_BUCKET', 'test-bucket')
        client = _StubS3()
        stub = self._patch_boto3(monkeypatch, client)

        delete_from_s3(['a.png'])

        assert stub.client_kwargs == {
            'service_name': 's3',
            'region_name': 'test-region',
            'endpoint_url': 'https://s3.example.test',
            'aws_access_key_id': 'test-access-key',
            'aws_secret_access_key': 'test-access-secret',
        }
        assert client.deleted[0]['Bucket'] == 'test-bucket'


class TestCleanUpTmp:
    """`clean_up_tmp:1141` -- delete stale media from the tmp directory.

    `:1149` returns early when the directory is absent; `:1152` walks it;
    `:1154` skips non-files; `:1156` filters by extension; `:1158` filters by
    age; `:1161`'s bare `except` swallows a failed unlink.

    THE DIRECTORY IS A PARAMETER BECAUSE OF THIS ROUND. It was a hardcoded
    RELATIVE path, `'app/static/tmp'`, resolved against the process working
    directory -- which under the test container is `/app`, the bind-mounted
    repository. Covering this function meant creating real files inside the
    working tree. The default now resolves to the same absolute path production
    already used, which this round verified rather than assumed.
    """

    def _stale(self, directory, name, age_seconds):
        path = os.path.join(directory, name)
        with open(path, 'w') as fh:
            fh.write('x')
        old = time.time() - age_seconds
        os.utime(path, (old, old))
        return path

    def test_a_stale_image_is_removed(self, db_session):
        directory = tempfile.mkdtemp()
        path = self._stale(directory, 'old.jpg', 25 * 60 * 60)

        clean_up_tmp(directory)

        assert not os.path.exists(path)

    def test_a_recent_image_survives(self, db_session):
        """The BOUNDARY at `:1158` -- older than one day, not merely old."""
        directory = tempfile.mkdtemp()
        path = self._stale(directory, 'recent.jpg', 23 * 60 * 60)

        clean_up_tmp(directory)

        assert os.path.exists(path)

    def test_a_file_with_an_undeletable_extension_survives(self, db_session):
        """`:1156`'s extension filter. A .txt is not in DELETABLE_EXTENSIONS."""
        directory = tempfile.mkdtemp()
        path = self._stale(directory, 'old.txt', 25 * 60 * 60)

        clean_up_tmp(directory)

        assert os.path.exists(path)

    def test_the_extension_check_is_case_insensitive(self, db_session):
        """The extension fold makes `.JPG` match, but not attributably to
        either single line. `:1155` lowercases the whole filename before
        `os.path.splitext`, and `:1156` lowercases `ext` again before the
        `DELETABLE_EXTENSIONS` membership test -- by the time `:1156` runs,
        `ext` is already lowercase because of `:1155`, so mutation proved
        (D370) that dropping EITHER single `.lower()` alone survives every
        test in this file: the other line's fold still makes `ext` lowercase
        either way. This test cannot attribute the case-insensitivity to one
        line over the other; it only pins that the fold happens somewhere in
        `:1152-1156`.
        """
        directory = tempfile.mkdtemp()
        path = self._stale(directory, 'OLD.JPG', 25 * 60 * 60)

        clean_up_tmp(directory)

        assert not os.path.exists(path)

    def test_a_subdirectory_is_skipped(self, db_session, monkeypatch):
        """`:1154`'s `os.path.isfile` guard, taking its false arm.

        The directory is backdated past the age window and named with a
        deletable extension, so `:1154` is the only thing standing between it
        and `:1160`'s `os.remove`. Asserting that it survives would prove
        nothing: `os.remove` on a directory raises `IsADirectoryError`, which
        `:1161`'s bare `except` swallows, so it survives with the guard
        deleted too. The test records the removal attempts instead.
        """
        directory = tempfile.mkdtemp()
        nested = os.path.join(directory, 'sub.jpg')
        os.mkdir(nested)
        old = time.time() - 25 * 60 * 60
        os.utime(nested, (old, old))

        removed = []
        real_remove = os.remove

        def _record(path):
            removed.append(path)
            return real_remove(path)

        monkeypatch.setattr('app.shared.tasks.maintenance.os.remove', _record)

        clean_up_tmp(directory)

        assert removed == []
        assert os.path.isdir(nested)

    def test_a_missing_directory_returns_early(self, db_session):
        """`:1149`'s true arm. The task must not raise on a path that is gone."""
        directory = tempfile.mkdtemp()
        os.rmdir(directory)

        clean_up_tmp(directory)

    def test_a_failed_unlink_is_swallowed(self, db_session, monkeypatch):
        """`:1159-1162`'s bare `except Exception: pass`.

        The task continues rather than aborting the sweep when one file cannot
        be removed. This asserts the swallow, not that swallowing is right --
        a file the sweep cannot delete is registered, not fixed.
        """
        directory = tempfile.mkdtemp()
        path = self._stale(directory, 'locked.jpg', 25 * 60 * 60)

        def _refuse(_):
            raise PermissionError('nope')

        monkeypatch.setattr('app.shared.tasks.maintenance.os.remove', _refuse)

        clean_up_tmp(directory)

        assert os.path.exists(path)

    def test_the_default_directory_resolves_under_the_app_root(
            self, db_session, app, monkeypatch):
        """`:1146-1147`'s `if directory is None` branch -- the parameter's
        default, exercised by calling with no argument at all.

        This module's docstring records that the directory used to be a
        hardcoded RELATIVE path, `'app/static/tmp'`. `os.path.exists` is
        patched to record the path it is asked about and return False, so
        `:1149` takes its early-return arm before any real directory is
        touched -- this proves what `:1147` resolved to without depending on,
        or disturbing, whatever is actually in the real tmp directory.

        Reverting `:1147` to the old hardcoded relative string is a one-line
        regression this catches directly: `seen` would hold
        `'app/static/tmp'` instead of the absolute path built from
        `app.root_path`.
        """
        seen = []

        def _exists(path):
            seen.append(path)
            return False

        monkeypatch.setattr('app.shared.tasks.maintenance.os.path.exists', _exists)

        clean_up_tmp()

        assert seen == [os.path.join(app.root_path, 'static', 'tmp')]


class TestAddRemoteCommunityFromPost:
    """`add_remote_community_from_post:1101` -- turn a post into a lookup.

    `:1102` forks on whether the post carries a url: with one, `:1104` extracts
    the domain and actor and builds a single `!name@server`; without, `:1108`
    regex-scans the body for the same shape. `:1113` skips anything on this
    instance, and `:1116-1117`'s bare `except Exception: pass` swallows
    whatever `search_for_community` raises.

    `search_for_community` IS IMPORTED INSIDE THE FUNCTION at `:1111`, so it
    never enters this module's namespace and `app.shared.tasks.maintenance.
    search_for_community` does not exist to patch. These tests patch
    `app.community.util.search_for_community` at its source -- the one helper
    in this round the namespace idiom cannot reach.
    """

    def test_a_post_with_a_url_yields_one_lookup(self, db_session, monkeypatch):
        recorder = _Recorder()
        monkeypatch.setattr('app.community.util.search_for_community', recorder)

        add_remote_community_from_post({'url': 'https://peer.example/c/books'})

        assert [c[0][0] for c in recorder.calls] == ['!books@peer.example']

    def test_a_post_without_a_url_scans_the_body(self, db_session, monkeypatch):
        """`:1102`'s false arm and `:1108`'s regex."""
        recorder = _Recorder()
        monkeypatch.setattr('app.community.util.search_for_community', recorder)

        add_remote_community_from_post(
            {'body': 'try !books@peer.example and !film@other.example'})

        assert {c[0][0] for c in recorder.calls} == {
            '!books@peer.example', '!film@other.example'}

    def test_a_body_with_no_match_looks_up_nothing(self, db_session, monkeypatch):
        """`:1107`'s regex, guarded against a false-positive match.

        This takes `:1110`'s false arm for coverage, but it CANNOT discriminate
        that guard: invert it and control enters the branch, reaches `:1112`'s
        `for cl in set(community_lookup):`, and iterates zero times over the
        empty list, so no lookup happens either way. `:1110` is a cheap skip
        over `:1111`'s import, not a correctness guard, and nothing here kills
        a mutation of it.
        """
        recorder = _Recorder()
        monkeypatch.setattr('app.community.util.search_for_community', recorder)

        add_remote_community_from_post({'body': 'nothing here'})

        assert recorder.calls == []

    def test_a_community_on_this_instance_is_skipped(self, db_session, monkeypatch, app):
        """`:1113`'s guard against looking up our own communities."""
        recorder = _Recorder()
        monkeypatch.setattr('app.community.util.search_for_community', recorder)
        local = f"!books@{app.config['SERVER_NAME']}"

        add_remote_community_from_post({'body': f'see {local}'})

        assert recorder.calls == []

    def test_a_failing_lookup_is_swallowed(self, db_session, monkeypatch):
        """`:1116-1117`'s bare `except Exception: pass`.

        This is fact 111's shape, which the campaign has reasoned about twice in
        `notes.py` and `pages.py`. The test asserts the swallow -- the task
        returns rather than propagating -- and does not claim the swallow is
        correct.

        The `called` list is what makes the swallow observable. Without it the
        test would pass identically if the patch never took effect and
        `search_for_community` was never reached, which would exercise no
        handler at all.
        """
        called = []

        def _raise(*args, **kwargs):
            called.append(args[0])
            raise RuntimeError('lookup exploded')

        monkeypatch.setattr('app.community.util.search_for_community', _raise)

        add_remote_community_from_post({'body': '!books@peer.example'})

        assert called == ['!books@peer.example']


class TestAddRemoteCommunities:
    """`add_remote_communities:1070` -- import new communities from a feed.

    `:1072` fetches lemmy.world's newcommunities listing; `:1080` proceeds only
    on 200; `:1087` walks the posts oldest-first; `:1089` skips stickied posts
    and `:1092` skips ids already imported; `:1095` hands each survivor to
    `add_remote_community_from_post` and `:1098` records the high-water mark.

    THIS FUNCTION HAS NO SESSION. `:1085`'s `get_setting` and `:1098`'s
    `set_setting` both go through `db.session` (`app/utils.py:203-222`), with no
    `get_task_session()` and no `patch_db_session` -- alone among this module's
    tasks. That is this round's PC2 and is NOT fixed by these tests.

    An unmatched respx request DOES fail a test here: `:1077` catches only
    `httpx.HTTPError`, and respx raises an `AssertionError`.
    `test_a_transport_failure_is_swallowed` below reaches `:1077`'s `except`
    directly, by replacing `get_request` itself rather than via respx.
    """

    LISTING = 'https://lemmy.world/api/v3/post/list'

    def _posts(self, *posts):
        return {'posts': [{'post': p} for p in posts]}

    def test_a_new_post_is_handed_over_and_recorded(self, db_session, monkeypatch, http_mock):
        recorder = _Recorder()
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.add_remote_community_from_post', recorder)
        http_mock.get(url__startswith=self.LISTING).respond(
            200, json=self._posts({'id': 7, 'featured_community': False,
                                   'url': 'https://peer.example/c/books'}))

        add_remote_communities()

        assert [c[0][0]['id'] for c in recorder.calls] == [7]
        from app.utils import get_setting
        assert get_setting('last_successful_import', 0) == 7

    def test_a_stickied_post_is_skipped(self, db_session, monkeypatch, http_mock):
        """`:1089`'s `featured_community` guard."""
        recorder = _Recorder()
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.add_remote_community_from_post', recorder)
        http_mock.get(url__startswith=self.LISTING).respond(
            200, json=self._posts({'id': 8, 'featured_community': True,
                                   'url': 'https://peer.example/c/books'}))

        add_remote_communities()

        assert recorder.calls == []

    def test_an_already_imported_post_is_skipped(self, db_session, monkeypatch, http_mock):
        """`:1092`'s high-water mark.

        `set_setting` is called directly to establish the mark, because the
        task only writes it after a successful hand-over and this test needs it
        set beforehand.
        """
        from app.utils import set_setting
        set_setting('last_successful_import', 20)
        recorder = _Recorder()
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.add_remote_community_from_post', recorder)
        http_mock.get(url__startswith=self.LISTING).respond(
            200, json=self._posts({'id': 15, 'featured_community': False,
                                   'url': 'https://peer.example/c/books'}))

        add_remote_communities()

        assert recorder.calls == []

    def test_a_non_200_response_does_nothing(self, db_session, monkeypatch, http_mock):
        """`:1080`'s false arm.

        A non-200 status returns without retrying -- unlike a transport error,
        which would send `get_request` into `app/utils.py:173-177`'s handler and
        cost a 3-to-10-second sleep.
        """
        recorder = _Recorder()
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.add_remote_community_from_post', recorder)
        http_mock.get(url__startswith=self.LISTING).respond(503)

        add_remote_communities()

        assert recorder.calls == []

    def test_a_transport_failure_is_swallowed(self, db_session, monkeypatch):
        """`:1077`'s `except httpx.HTTPError: return` (`:1078`).

        No respx and no real transport are involved. `get_request` is
        imported at module scope (`maintenance.py:19`) and called as a bare
        name at `:1072`, so `monkeypatch.setattr(
        'app.shared.tasks.maintenance.get_request', ...)` reaches this arm
        directly, at no runtime cost -- there is no need to drive the real
        `get_request` into its retry handlers (`app/utils.py:158-177`, each a
        3-to-10-second `sleep`) to reach a caller's OWN exception handler.

        The stand-in raises `httpx.HTTPError` itself, so this discriminates
        the `except` clause rather than anything inside `get_request`.
        `add_remote_community_from_post` is patched with a recorder so the
        test can assert the task handed nothing over and never reached
        `:1085`'s `get_setting` read.
        """
        def _raise(*args, **kwargs):
            raise httpx.HTTPError('simulated transport failure')

        monkeypatch.setattr('app.shared.tasks.maintenance.get_request', _raise)
        recorder = _Recorder()
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.add_remote_community_from_post', recorder)

        add_remote_communities()

        assert recorder.calls == []

    def test_posts_are_processed_oldest_first(self, db_session, monkeypatch, http_mock):
        """`:1087`'s `reversed(...)`.

        The listing is sorted newest-first, so the task reverses it to walk
        forward in time -- otherwise the high-water mark at `:1098` would be set
        to the newest id on the first iteration and every older post would then
        be skipped by `:1092`. This is call order, not planner order, so
        comparing a list is legitimate here.
        """
        recorder = _Recorder()
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.add_remote_community_from_post', recorder)
        http_mock.get(url__startswith=self.LISTING).respond(
            200, json=self._posts(
                {'id': 9, 'featured_community': False, 'url': 'https://peer.example/c/b'},
                {'id': 8, 'featured_community': False, 'url': 'https://peer.example/c/a'}))

        add_remote_communities()

        assert [c[0][0]['id'] for c in recorder.calls] == [8, 9]


class TestRefreshInstanceChooser:
    """`refresh_instance_chooser:975` -- rebuild the instance-chooser table.

    `:984` asks fediverse.observer for PieFed nodes; `:986` and `:991` bail on a
    bad status or shape; `:999` shuffles; `:1002` walks the nodes, asking each
    for its own chooser document; `:1026` creates or updates a row; `:1040`'s
    else and `:1010`'s handler delete one; `:1056`'s `for` head walks the
    existing rows and `:1057-1058` prunes those for domains the observer no
    longer lists.

    A TEST HERE THAT FORGETS A ROUTE DOES NOT FAIL -- IT SILENTLY TESTS THE
    FAILURE PATH. `:1010`'s bare `except Exception` catches respx's
    `AllMockedAssertionError` (an `AssertionError`, not an `httpx.HTTPError`),
    logs "Failed to connect", deletes the row and continues. Every success-path
    test below registers both routes for that reason.

    `:999`'s `random.shuffle` makes processing order nondeterministic; no test
    asserts on it. The oracle is the resulting set of rows.

    `:1010`'s inner handler IS tested below
    (`test_a_connection_failure_deletes_an_existing_row_and_continues`), by
    replacing `get_request` itself at module scope rather than by forgetting
    a respx route or driving the real function into its 3-10-second retry.
    `get_request` is imported at module scope (`maintenance.py:19`) and
    called as a bare name at `:1009`, so
    `monkeypatch.setattr('app.shared.tasks.maintenance.get_request', ...)`
    reaches it directly and at no runtime cost. A deliberate replacement is
    NOT indistinguishable from a forgotten route: the raising stand-in below
    uses a dedicated exception class, and the second, healthy domain in that
    same test is fetched through a genuine respx route bound by
    `assert_all_called=True`, so a route that really was forgotten would
    ERROR at teardown rather than pass silently. `:1046`'s OUTER per-domain
    handler is a different arm: it sits outside the inner `try`/`except`, so
    it is reached by something in the `:1018-1044` body raising, not by a
    missing route. `find_language_or_create` (patched at module scope,
    `:1032`) makes a convenient raise site for that, and is exercised below
    for both arms of `:1050`'s existing-row guard.
    """

    OBSERVER = 'https://api.fediverse.observer/'

    def _nodes(self, *domains):
        return {'data': {'nodes': [
            {'domain': d, 'uptime_alltime': 99, 'monthsmonitored': 12}
            for d in domains]}}

    def _chooser(self):
        """`newbie_friendly` is `False` deliberately, NOT the more obvious
        `True`: `InstanceChooser.newbie_friendly`'s column default is `True`
        (`app/models.py:4365`), identical to `:1035`'s `chooser_data.get(
        'newbie_friendly', True)` fallback -- so a document that also says
        `True` leaves `:1035` covered but unobserved, indistinguishable from
        deleting the line entirely or reading the wrong key. Returning
        `False` here makes `:1035` discriminated wherever a test checks the
        stored value against the column default.
        """
        return {'nsfw': False, 'newbie_friendly': False, 'name': 'Peer'}

    def test_a_listed_domain_gets_a_row(self, db_session, http_mock):
        http_mock.post(self.OBSERVER).respond(200, json=self._nodes('peer.example'))
        http_mock.get('https://peer.example/api/alpha/site/instance_chooser').respond(
            200, json=self._chooser())

        refresh_instance_chooser()

        db.session.expire_all()
        rows = db.session.query(InstanceChooser).all()
        assert {r.domain for r in rows} == {'peer.example'}

    def test_the_uptime_and_months_are_folded_into_the_stored_data(self, db_session, http_mock):
        """`:1021-1022` copy two fields off the observer node into the chooser
        document before `:1038` stores the whole thing.
        """
        http_mock.post(self.OBSERVER).respond(200, json=self._nodes('peer.example'))
        http_mock.get('https://peer.example/api/alpha/site/instance_chooser').respond(
            200, json=self._chooser())

        refresh_instance_chooser()

        db.session.expire_all()
        row = db.session.query(InstanceChooser).filter_by(domain='peer.example').first()
        assert row.data['uptime'] == 99
        assert row.data['monthsmonitored'] == 12

    def test_an_existing_row_is_updated_rather_than_duplicated(self, db_session, http_mock):
        """`:1026`'s false arm -- the row already exists.

        Also discriminates `:1034`'s `nsfw` and `:1035`'s `newbie_friendly`
        writes: both are seeded opposite to what `_chooser()`'s document
        supplies, so a row that merely keeps its pre-existing values, or
        that reads `:1035`'s update from the wrong key, would leave either
        assertion below failing.
        """
        db.session.add(InstanceChooser(domain='peer.example', nsfw=True, newbie_friendly=True))
        db.session.commit()
        http_mock.post(self.OBSERVER).respond(200, json=self._nodes('peer.example'))
        http_mock.get('https://peer.example/api/alpha/site/instance_chooser').respond(
            200, json=self._chooser())

        refresh_instance_chooser()

        db.session.expire_all()
        rows = db.session.query(InstanceChooser).filter_by(domain='peer.example').all()
        assert len(rows) == 1
        assert rows[0].nsfw is False
        assert rows[0].newbie_friendly is False

    def test_a_chooser_404_removes_an_existing_row(self, db_session, http_mock):
        """`:1040`'s else arm and `:1043`'s guard."""
        db.session.add(InstanceChooser(domain='peer.example'))
        db.session.commit()
        http_mock.post(self.OBSERVER).respond(200, json=self._nodes('peer.example'))
        http_mock.get('https://peer.example/api/alpha/site/instance_chooser').respond(404)

        refresh_instance_chooser()

        db.session.expire_all()
        assert db.session.query(InstanceChooser).filter_by(
            domain='peer.example').first() is None

    def test_a_chooser_404_with_no_existing_row_logs_nothing(
            self, db_session, http_mock, monkeypatch, app):
        """`:1043`'s guard, false arm -- a 404 for a domain with no existing
        row.

        DATABASE STATE AND EXCEPTION PROPAGATION CANNOT DISCRIMINATE THIS
        GUARD. If `:1043`'s `if existing:` were deleted so `:1044`'s
        `session.delete(existing)` always ran, `session.delete(None)` raises
        `UnmappedInstanceError` from inside the `:1018-1044` body -- but
        `:1046`'s OUTER handler catches exactly that, performs the identical
        guarded query-and-delete itself, finds nothing (there was never a
        row), and continues. The end state -- no row for the domain -- and
        the fact that nothing propagates out of `refresh_instance_chooser()`
        are the same whether `:1043`'s guard exists or not, so neither is a
        valid oracle for this branch.

        THE LOG IS A DIFFERENT ORACLE. `:1046`'s handler only runs, and only
        logs its "Error processing domain" warning, when it actually catches
        something. The guarded original takes `:1043`'s false arm cleanly
        and never enters that handler, so nothing is logged. The mutated
        version enters it via the manufactured `UnmappedInstanceError`, and
        logs.

        `recorder == []` is the discriminator for `:1043` itself -- it is
        what the guard-removal mutation above flips to non-empty.

        The row assertion below is a CONSISTENCY CHECK, not proof the 404
        path ran: no row is ever seeded for `peer.example`, and only the
        200-status branch (`:1024-1028`) creates one, so
        `.filter_by(domain='peer.example').first() is None` would hold
        trivially even if `refresh_instance_chooser` never touched this
        domain at all. It rules out a mutation that wrongly creates a row on
        this path, but it cannot by itself show the path executed.

        What actually guarantees the domain was processed is `http_mock`'s
        `assert_all_called=True` (`tests/conftest.py:339-342`): this test
        registers the 404 GET route for `peer.example`, and if
        `refresh_instance_chooser` never made that request, respx raises at
        fixture teardown and the test ERRORS rather than passing vacuously.
        That is the mechanism that rules out "the 404 route was never
        reached at all" -- not the empty-recorder assertion, which would
        stay empty either way if the route went unhit.
        """
        recorder = []
        monkeypatch.setattr(
            app.logger, 'warning', lambda *a, **kw: recorder.append((a, kw)))
        http_mock.post(self.OBSERVER).respond(200, json=self._nodes('peer.example'))
        http_mock.get('https://peer.example/api/alpha/site/instance_chooser').respond(404)

        refresh_instance_chooser()

        db.session.expire_all()
        assert db.session.query(InstanceChooser).filter_by(
            domain='peer.example').first() is None
        assert recorder == []

    def test_a_domain_the_observer_dropped_is_pruned(self, db_session, http_mock):
        """`:1056-1058` -- rows for domains absent from the observer response."""
        db.session.add(InstanceChooser(domain='gone.example'))
        db.session.commit()
        http_mock.post(self.OBSERVER).respond(200, json=self._nodes('peer.example'))
        http_mock.get('https://peer.example/api/alpha/site/instance_chooser').respond(
            200, json=self._chooser())

        refresh_instance_chooser()

        db.session.expire_all()
        assert {r.domain for r in db.session.query(InstanceChooser).all()} == {'peer.example'}

    def test_a_connection_failure_deletes_an_existing_row_and_continues(
            self, db_session, http_mock, monkeypatch):
        """`:1010`'s inner `except Exception as e:` (`:1010-1016`), both arms
        of `:1014`'s `if existing:` guard.

        No respx and no real transport are involved. `get_request` is
        imported at module scope (`maintenance.py:19`) and called as a bare
        name at `:1009`, so replacing it in `maintenance`'s own namespace
        reaches this handler directly -- there is no need to drive the real
        `get_request` into `app/utils.py:158-177`'s retry logic, which sleeps
        3-10 seconds per call.

        `:1010` catches a bare `Exception`, so any exception type reaches
        it. `_SimulatedOutage` is a dedicated class raised only for URLs
        containing `down`, which makes this deliberately-raised failure
        impossible to confuse with a test that merely forgot to register a
        respx route: `peer.example` goes through the REAL `get_request`
        (captured before patching) and a genuine respx route bound by
        `assert_all_called=True` (`tests/conftest.py:339-342`), so a
        forgotten route there would ERROR at teardown rather than pass.

        Three domains distinguish every arm the block has:
        `down.example` has an existing row and fails -- `:1014`'s true arm,
        the row is deleted; `newly-down.example` has no row and fails --
        `:1014`'s false arm, `:1015`'s delete is skipped and nothing raises
        on the `None` lookup; `peer.example` succeeds, proving `:1016`'s
        `continue` returns control to the loop rather than aborting the run.
        """
        class _SimulatedOutage(Exception):
            pass

        from app.utils import get_request as _real_get_request

        def _get_request(uri, *args, **kwargs):
            if 'down' in uri:
                raise _SimulatedOutage('simulated-instance-chooser-outage')
            return _real_get_request(uri, *args, **kwargs)

        monkeypatch.setattr('app.shared.tasks.maintenance.get_request', _get_request)
        db.session.add(InstanceChooser(domain='down.example'))
        db.session.commit()
        http_mock.post(self.OBSERVER).respond(
            200, json=self._nodes('down.example', 'newly-down.example', 'peer.example'))
        http_mock.get('https://peer.example/api/alpha/site/instance_chooser').respond(
            200, json=self._chooser())

        refresh_instance_chooser()

        db.session.expire_all()
        assert db.session.query(InstanceChooser).filter_by(
            domain='down.example').first() is None
        assert db.session.query(InstanceChooser).filter_by(
            domain='newly-down.example').first() is None
        assert {r.domain for r in db.session.query(InstanceChooser).all()} == {'peer.example'}

    def test_a_language_in_the_document_is_resolved(self, db_session, http_mock, monkeypatch):
        """`:1031`'s true arm. `find_language_or_create` is imported at module
        scope (`maintenance.py:13`), so the namespace idiom reaches it.
        """
        recorder = _Recorder(result=type('L', (), {'id': 42})())
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.find_language_or_create', recorder)
        doc = self._chooser()
        doc['language'] = {'id': 1, 'code': 'en', 'name': 'English'}
        http_mock.post(self.OBSERVER).respond(200, json=self._nodes('peer.example'))
        http_mock.get('https://peer.example/api/alpha/site/instance_chooser').respond(
            200, json=doc)

        refresh_instance_chooser()

        db.session.expire_all()
        row = db.session.query(InstanceChooser).filter_by(domain='peer.example').first()
        assert row.language_id == 42
        assert recorder.calls[0][0] == ('en', 'English')

    def test_a_language_without_an_id_is_not_resolved(
            self, db_session, http_mock, monkeypatch):
        """`:1031`'s SECOND conjunct, `'id' in chooser_data['language']`.

        The test above sets a `language` dict that carries `id`, `code` and
        `name` together, so it cannot discriminate a mutation that drops the
        second conjunct: both the guard and the mutant take the same branch
        for that document. This test's `language` dict has `code` and `name`
        but no `id` -- the guard's true arm requires both conjuncts, so the
        unmutated guard must take its FALSE arm and never call
        `find_language_or_create`, leaving `language_id` at its column
        default of `None`. Dropping the second conjunct would let a
        `language`-shaped-but-`id`-less document through, calling
        `find_language_or_create('en', 'English')` and setting `language_id`
        to the patched return value's `id` -- both assertions below would
        then fail.
        """
        recorder = _Recorder(result=type('L', (), {'id': 42})())
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.find_language_or_create', recorder)
        doc = self._chooser()
        doc['language'] = {'code': 'en', 'name': 'English'}
        http_mock.post(self.OBSERVER).respond(200, json=self._nodes('peer.example'))
        http_mock.get('https://peer.example/api/alpha/site/instance_chooser').respond(
            200, json=doc)

        refresh_instance_chooser()

        db.session.expire_all()
        row = db.session.query(InstanceChooser).filter_by(domain='peer.example').first()
        assert row.language_id is None
        assert recorder.calls == []

    def test_an_observer_non_200_returns_early(self, db_session, http_mock):
        """`:986`'s true arm. Deleting the check does not fall through
        harmlessly: `:990`'s `response.json()` on the empty 503 body raises
        `json.decoder.JSONDecodeError`, which is not caught until `:1062`'s
        outer handler re-raises it, failing the test. That is what actually
        binds this test to `:986` -- not a registered-route mismatch, since
        no chooser route is registered on this path either way.
        """
        db.session.add(InstanceChooser(domain='kept.example'))
        db.session.commit()
        http_mock.post(self.OBSERVER).respond(503)

        refresh_instance_chooser()

        db.session.expire_all()
        assert db.session.query(InstanceChooser).filter_by(
            domain='kept.example').first() is not None

    def test_a_malformed_observer_response_returns_early(self, db_session, http_mock):
        """`:991`'s shape check -- 200 with no `data.nodes`."""
        db.session.add(InstanceChooser(domain='kept.example'))
        db.session.commit()
        http_mock.post(self.OBSERVER).respond(200, json={'unexpected': True})

        refresh_instance_chooser()

        db.session.expire_all()
        assert db.session.query(InstanceChooser).filter_by(
            domain='kept.example').first() is not None

    def test_a_response_with_data_but_no_nodes_returns_early(self, db_session, http_mock):
        """`:991`'s THIRD disjunct, `'nodes' not in response_data['data']`.

        The test above sends a body with no `data` key at all, which already
        trips the guard's SECOND disjunct (`'data' not in response_data`) --
        it cannot discriminate a mutation that deletes the third disjunct,
        because with `data` absent entirely the second disjunct catches it
        regardless of whether the third exists. This test supplies a `data`
        key with a different shape, so only the third disjunct can catch it.

        If the third disjunct were deleted, control would fall through past
        `:991`'s early return to `:998`'s
        `response_data['data']['nodes']`, which raises `KeyError` -- there is
        no `nodes` key here. Nothing inside `refresh_instance_chooser` catches
        a `KeyError` raised before the per-domain loop starts; it propagates
        out of the function, which this call (made with no `pytest.raises`)
        would then fail on.
        """
        db.session.add(InstanceChooser(domain='kept.example'))
        db.session.commit()
        http_mock.post(self.OBSERVER).respond(200, json={'data': {'unexpected': True}})

        refresh_instance_chooser()

        db.session.expire_all()
        assert db.session.query(InstanceChooser).filter_by(
            domain='kept.example').first() is not None

    def test_a_failure_after_a_200_response_deletes_an_existing_row(
            self, db_session, http_mock, monkeypatch):
        """`:1046-1051`'s outer per-domain handler, true arm of `:1050`.

        Unlike `:1010`'s inner handler (arm 2, deliberately left open -- see
        the sub-project's report), this one sits OUTSIDE the inner
        `try`/`except`, so only something raising from the `:1018-1044` body
        reaches it. `find_language_or_create` is patched to raise once the
        chooser document already carries a `language` key, so the response is
        a normal 200 and the raise comes from inside that body, not from
        `get_request`.

        Narrowing `:1046` from `except Exception` to `except ValueError` is a
        one-line regression this catches directly: the patched raise is a
        `RuntimeError`, which the narrowed clause would no longer catch, so it
        would propagate out of `refresh_instance_chooser()` and this call --
        made with no `pytest.raises` -- would fail the test. (Verified: the
        same narrowing does NOT fail the false-arm test below, since that
        one's `json.decoder.JSONDecodeError` trigger is a `ValueError`
        subclass -- each test's regression proof is specific to its own
        trigger.)
        """
        db.session.add(InstanceChooser(domain='peer.example'))
        db.session.commit()

        def _boom(*args, **kwargs):
            raise RuntimeError('language lookup exploded')

        monkeypatch.setattr(
            'app.shared.tasks.maintenance.find_language_or_create', _boom)
        doc = self._chooser()
        doc['language'] = {'id': 1, 'code': 'en', 'name': 'English'}
        http_mock.post(self.OBSERVER).respond(200, json=self._nodes('peer.example'))
        http_mock.get('https://peer.example/api/alpha/site/instance_chooser').respond(
            200, json=doc)

        refresh_instance_chooser()

        db.session.expire_all()
        assert db.session.query(InstanceChooser).filter_by(
            domain='peer.example').first() is None

    def test_a_failure_after_a_200_response_with_no_existing_row_is_a_no_op(
            self, db_session, http_mock):
        """`:1046-1051`'s outer per-domain handler, false arm of `:1050`.

        This CANNOT reuse the test above's `find_language_or_create` trigger.
        That raise happens at `:1032`, after `:1025`'s query and `:1026-1028`'s
        create-if-absent have already run -- so by the time the outer handler
        queries at `:1049`, a new row for the domain is already staged in the
        session and visible to the query (SQLAlchemy autoflushes before it),
        making `existing` truthy even with no row committed beforehand.
        Verified by instrumenting the handler directly: with no pre-existing
        row, `existing` still came back as the just-created `InstanceChooser`.

        A malformed JSON body makes `:1019`'s `chooser_response.json()` raise
        instead, before `:1025` ever runs, so `:1049`'s query legitimately
        finds nothing and `:1050`'s guard must take its false arm to skip
        `:1051`'s delete.

        If that guard were removed so `:1051` always ran, `session.delete(None)`
        raises `UnmappedInstanceError` inside the handler itself; nothing
        inside the loop catches that, so it reaches `:1062`'s outer handler,
        which re-raises -- the same propagation path the `:986` test above
        documents -- and fails this call, made with no `pytest.raises`.
        """
        http_mock.post(self.OBSERVER).respond(200, json=self._nodes('peer.example'))
        http_mock.get('https://peer.example/api/alpha/site/instance_chooser').respond(
            200, content=b'not valid json')

        refresh_instance_chooser()

        db.session.expire_all()
        assert db.session.query(InstanceChooser).filter_by(
            domain='peer.example').first() is None

    def test_a_failure_inside_the_task_propagates(self, db_session, monkeypatch, http_mock):
        """`:1062-1064`'s handler.

        Reached by making `random.shuffle` raise -- it is called at `:999`,
        inside `:977`'s `try` and before the per-domain loop, so the outer
        handler is the one that catches it. Patching a symbol used inside the
        loop would instead be swallowed by `:1046`.
        """
        def _boom(*args, **kwargs):
            raise RuntimeError('the task itself failed')

        monkeypatch.setattr('app.shared.tasks.maintenance.random.shuffle', _boom)
        http_mock.post(self.OBSERVER).respond(200, json=self._nodes('peer.example'))

        with pytest.raises(RuntimeError, match='the task itself failed'):
            refresh_instance_chooser()
