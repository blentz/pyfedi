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
