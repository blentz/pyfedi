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
which does not retry.

`random.shuffle` AT `:999` makes node order nondeterministic. No test asserts on
processing order; the oracle is the resulting set of rows.

HELPERS BELONGING TO OTHER MODULES ARE ARRANGED, NOT EXERCISED --
`search_for_community` (`app/community/util.py:34`), `find_language_or_create`
(`app/activitypub/util.py:384`) and `boto3.session.Session`. Each is replaced in
THIS module's namespace with a recorder, so the tests assert on the handover
rather than on another module's behaviour (fact 179).
"""

import os
import tempfile
import time
from types import SimpleNamespace

import pytest

from app import db
from app.models import InstanceChooser
from app.shared.tasks.maintenance import (
    add_remote_communities, add_remote_community_from_post, clean_up_tmp,
    delete_from_s3, refresh_instance_chooser,
)


class _Recorder:
    """Replace a module-level callable and remember how it was called.

    `calls` holds one tuple of positional arguments per invocation. Used for
    helpers belonging to other modules, so a test asserts on what was handed
    over rather than on what the callee then did (fact 179).
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
        """`:1155` lowercases the filename before splitting, so .JPG matches."""
        directory = tempfile.mkdtemp()
        path = self._stale(directory, 'OLD.JPG', 25 * 60 * 60)

        clean_up_tmp(directory)

        assert not os.path.exists(path)

    def test_a_subdirectory_is_skipped(self, db_session):
        """`:1154`'s `os.path.isfile` guard, taking its false arm."""
        directory = tempfile.mkdtemp()
        nested = os.path.join(directory, 'sub.jpg')
        os.mkdir(nested)

        clean_up_tmp(directory)

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
