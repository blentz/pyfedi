"""Group C of `app/shared/tasks/maintenance.py` -- the instance-health tasks.

Sub-project 29 closed Group A in `tests/test_shared_tasks_maintenance_cleanup.py`,
30 closed Group B in `..._lifecycle.py`, and 31 closed Group D in
`..._external.py`. Group C is the last, and the only group no test reached at
all. This file covers its first half:

  `sync_defederation_subscriptions:409`   `check_instance_health:427`
  `monitor_healthy_instances:509`, HTTP half only

THIS FILE USES NO RESPX. Every helper these tasks call is imported at module
scope (`maintenance.py:13` and `:19`), so tests replace
`app.shared.tasks.maintenance.get_request_instance`, `.get_request`,
`.instance_banned` and `.download_defeds` directly and hand back constructed
`httpx.Response` objects. That avoids sub-project 31's central hazard and
`get_request`'s 3-to-10-second retry sleep (`app/utils.py:158-162`, `:173-177`)
in one move: no request reaches a transport, so neither can bite.

BUT THE HAZARD STILL EXISTS FOR A TEST THAT FORGETS THE PATCH.
`get_request_instance` (`app/utils.py:190-196`) catches EVERYTHING with a bare
`except:` -- including respx's `AllMockedAssertionError` from the autouse
`block_outbound_http` fixture -- and returns a synthetic
`httpx.Response(status_code=500)`. So an unpatched call does not fail the test;
it routes it into the failure path while the test believes it tested success.
Task 1 established this by observation.

THE IDENTITY HALF IS OUT OF SCOPE. `monitor_healthy_instances:606` needs
`instance.software` in {'lemmy', 'piefed', 'pylova'} and `:667` needs 'mbin'.
Every fixture here uses `make_instance`'s default, 'mastodon', so neither body
runs. Both `if` statements still evaluate, so this file covers their FALSE arms
and sub-project 33 owns the true ones.

INSTANCE 1 IS RESERVED. `:449` and `:518` both filter `Instance.id != 1`, and
the conftest fixtures seed `instance_id=1`. Seeded instances must not be it.

THE TASKS RUN ON THEIR OWN CONNECTION. `get_task_session()` returns
`Session(bind=db.engine)` (`app/utils.py:3673-3675`), so rows a test seeds must
be COMMITTED before the task runs, and an ORM attribute read afterwards is stale
unless the test calls `db.session.expire_all()` first (fact 153).
"""

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import event

from app import db
from app.models import BannedInstances, DefederationSubscription, Instance, utcnow
from app.shared.tasks.maintenance import (
    check_instance_health,
    monitor_healthy_instances,
    sync_defederation_subscriptions,
)
from tests.factories import make_instance


class _Recorder:
    """`calls` holds one `(args, kwargs)` tuple per invocation.

    So `c[0][0]` is the first positional argument and `c[1]` the keywords. Same
    shape as the Group D file's recorder; kept identical so a reader moving
    between the two files does not have to re-learn it.
    """

    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


def _response(status_code=200, payload=None):
    """A real `httpx.Response`, because production calls `.json()` and `.close()`.

    A stub would have to imitate both. Constructing the real class means the
    test exercises the same parsing production does.
    """
    if payload is None:
        return httpx.Response(status_code=status_code)
    return httpx.Response(status_code=status_code, json=payload)


NODEINFO_LINK = 'http://nodeinfo.diaspora.software/ns/schema/2.0'


class TestSyncDefederationSubscriptions:
    """`sync_defederation_subscriptions:409` -- refresh subscription-sourced bans.

    `:413` deletes every ban carrying a `subscription_id` and `:414` commits,
    then `:416` walks the subscriptions and `:417` hands each to
    `download_defeds`. `:419-421` rolls back and re-raises.

    `BannedInstances.subscription_id` is None for a ban a local admin placed
    (`app/models.py:70`), which is what `:413`'s WHERE clause distinguishes.
    """

    def test_subscription_bans_are_cleared_and_admin_bans_survive(self, db_session, monkeypatch):
        """`:413`'s WHERE clause. Deleting it would take the admin ban too."""
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.download_defeds', _Recorder())
        sub = DefederationSubscription(domain='sub.example')
        db.session.add(sub)
        db.session.commit()
        db.session.add(BannedInstances(domain='from-sub.example', subscription_id=sub.id))
        db.session.add(BannedInstances(domain='from-admin.example', subscription_id=None))
        db.session.commit()

        sync_defederation_subscriptions()

        db.session.expire_all()
        remaining = {b.domain for b in db.session.query(BannedInstances).all()}
        assert remaining == {'from-admin.example'}

    def test_every_subscription_is_handed_over_with_its_id_and_domain(self, db_session, monkeypatch):
        """`:417`'s call. The set comparison is deliberate: `:416` returns
        planner-ordered rows and this file does not assert an order over those.
        """
        recorder = _Recorder()
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.download_defeds', recorder)
        first = DefederationSubscription(domain='one.example')
        second = DefederationSubscription(domain='two.example')
        db.session.add_all([first, second])
        db.session.commit()
        expected = {(first.id, 'one.example'), (second.id, 'two.example')}

        sync_defederation_subscriptions()

        assert {(c[0][0], c[0][1]) for c in recorder.calls} == expected

    def test_a_failing_download_rolls_back_and_re_raises(self, db_session, monkeypatch):
        """`:419-421`. The task does not swallow -- Celery must see the failure.

        The raise comes from `download_defeds` at `:417`, INSIDE the `try` that
        opens at `:412`, which is what makes `:420`'s rollback reachable.
        """
        def _boom(*args, **kwargs):
            raise RuntimeError('defed download failed')

        monkeypatch.setattr('app.shared.tasks.maintenance.download_defeds', _boom)
        db.session.add(DefederationSubscription(domain='sub.example'))
        db.session.commit()

        with pytest.raises(RuntimeError, match='defed download failed'):
            sync_defederation_subscriptions()

    def test_no_subscriptions_is_not_an_error(self, db_session, monkeypatch):
        """`:416`'s loop over an empty result. The delete at `:413` still runs."""
        recorder = _Recorder()
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.download_defeds', recorder)
        db.session.add(BannedInstances(domain='stale.example', subscription_id=None))
        db.session.commit()

        sync_defederation_subscriptions()

        db.session.expire_all()
        assert recorder.calls == []
        assert db.session.query(BannedInstances).filter_by(
            domain='stale.example').first() is not None
