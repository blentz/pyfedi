"""Group C of `app/shared/tasks/maintenance.py` -- the instance-health tasks.

Sub-project 29 closed Group A in `tests/test_shared_tasks_maintenance_cleanup.py`,
30 closed Group B in `..._lifecycle.py`, and 31 closed Group D in
`..._external.py`. Group C is the last, and the only group no test reached at
all. This file covers its first half:

  `sync_defederation_subscriptions:409`   `check_instance_health:427`
  `monitor_healthy_instances:534`, HTTP half only

THIS FILE USES NO RESPX. Every helper these tasks call is imported at module
scope (`maintenance.py:19`), so tests replace
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

THE IDENTITY HALF IS MOSTLY OUT OF SCOPE. `monitor_healthy_instances:637` needs
`instance.software` in {'lemmy', 'piefed', 'pylova'} and `:698` needs 'mbin'.
Every fixture but one uses `make_instance`'s default, 'mastodon', so neither
body runs for those. Both `if` statements still evaluate, so this file covers
their FALSE arms and sub-project 33 owns the true ones. The one exception is
DC4's `test_a_lemmy_point_release_above_nine_is_rediscovered`, which seeds
`software='lemmy'` and therefore DOES enter `:637`'s block -- `get_request` is
patched there to a harmless 404 (see that test's docstring) purely so an
otherwise-unmocked call does not reach a transport; it exercises no assertion
of its own.

INSTANCE 1 IS RESERVED. `:449` and `:543` both filter `Instance.id != 1`, and
the `db_session` teardown resets every sequence with
`SELECT setval(c.oid, 1, false)` (`tests/conftest.py:131`), so the FIRST
instance a test seeds lands on exactly the id both tasks exclude. `_seed_instance`
below plants a row that absorbs that id and is excluded by STATE, not id, so it
stays invisible to both tasks no matter which id it actually receives.

THE TASKS RUN ON THEIR OWN CONNECTION. `get_task_session()` returns
`Session(bind=db.engine)` (`app/utils.py:3673-3675`), so rows a test seeds must
be COMMITTED before the task runs, and an ORM attribute read afterwards is stale
unless the test calls `db.session.expire_all()` first (fact 153).
"""

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from app import db
from app.models import BannedInstances, DefederationSubscription, Instance, utcnow
from app.shared.tasks.maintenance import (
    _version_at_least,
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


def _seed_instance(domain, software='mastodon'):
    """Seed an instance the tasks will actually see.

    `check_instance_health:449` and `monitor_healthy_instances:543` both filter
    `Instance.id != 1`, and the `db_session` teardown resets every sequence with
    `SELECT setval(c.oid, 1, false)` (`tests/conftest.py:131`), so the FIRST
    instance a test seeds lands on exactly the id both tasks exclude. A test
    that seeds one instance and asserts the task changed it would pass only
    because the task processed nothing.

    The reserved row absorbs that id. It is excluded by STATE rather than by id
    -- dormant and gone_forever, with `start_trying_again` a year out -- so it
    stays invisible to both of `check_instance_health`'s loops and to
    `monitor_healthy_instances` no matter which id it actually receives.
    """
    if db.session.query(Instance).filter_by(domain='reserved-id-one.example').first() is None:
        reserved = Instance(domain='reserved-id-one.example', software='mastodon')
        reserved.dormant = True
        reserved.gone_forever = True
        reserved.start_trying_again = utcnow() + timedelta(days=365)
        db.session.add(reserved)
        db.session.commit()
    return make_instance(domain, software=software)


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

        This proves the exception propagates rather than being swallowed. It
        does NOT discriminate a mutant that drops `:420`'s `session.rollback()`
        alone: by the time the loop at `:416` runs, `:414`'s commit has already
        landed, so the rollback here only ever acts on a transaction holding
        nothing but the read at `:416`. No observable state depends on whether
        that rollback runs, so no assertion here can tell the two apart.
        """
        def _boom(*args, **kwargs):
            raise RuntimeError('defed download failed')

        monkeypatch.setattr('app.shared.tasks.maintenance.download_defeds', _boom)
        db.session.add(DefederationSubscription(domain='sub.example'))
        db.session.commit()

        with pytest.raises(RuntimeError, match='defed download failed'):
            sync_defederation_subscriptions()

    def test_no_subscriptions_is_not_an_error(self, db_session, monkeypatch):
        """Covers the zero-iteration arm of `:416`'s loop, nothing more.

        In isolation this cannot distinguish correct empty-subscription
        handling from a task that does nothing at all -- it leans on the other
        three tests in this class to establish that the task does something.
        It also cannot be strengthened by adding a subscription-sourced ban for
        `:413` to delete: `BannedInstances.subscription_id` is a foreign key to
        `defederation_subscription.id` (`app/models.py:70`), so with zero
        subscription rows no subscription-sourced ban can exist for the DELETE
        to remove.
        """
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


class TestMonitorHealthyInstances:
    """`monitor_healthy_instances:534` -- HTTP half only (see module docstring).

    DC1 asked whether the task's missing `patch_db_session` wrapper is an
    observable defect. `get_request_instance` (`app/utils.py:189-196`)
    mutates and commits its `instance` argument through Flask-SQLAlchemy's
    `db.session`, not through the task's own `session` (`:536`).
    `check_instance_health` wraps its body in `patch_db_session(session)` so
    that helper's writes land on the task's connection; `monitor_healthy_instances`
    does not.

    NO PRODUCTION CHANGE WAS MADE. Two discriminator designs were tried
    (task-2-report.md has the full record):

    1. Counting distinct `id(conn.connection)` values across every
       `before_cursor_execute` event whose statement mentions "instance" (the
       brief's own design). This was NOISY, not a stable signal: the same
       unmodified code produced 1 distinct connection in one harness and 2 in
       another (the difference was an unrelated extra SELECT in `_seed_instance`
       changing what was already idle in the pool at that point), and adding
       the `patch_db_session` wrapper experimentally did NOT collapse the
       count back to 1 in the harness that showed 2. Connection-pool checkout
       identity depends on incidental pool state (what else recently checked
       in and out), not on which Session object issued a given statement, so
       it cannot discriminate this defect.
    2. Reading back the actual persisted `Instance` row after the task
       completes. This is deterministic and directly answers the question
       that matters: does the value survive? It does, identically, whether or
       not `patch_db_session` wraps the task. The reason is structural, not
       incidental: `instance` in this test is loaded via the task's own
       `session.query(Instance)` and is therefore only ever attached to that
       session's identity map. `get_request_instance`'s `db.session.commit()`
       -- even when `db.session` is Flask-SQLAlchemy's own, separate scoped
       session -- has nothing of `instance`'s to flush, because `instance`
       was never added to `db.session`'s identity map. The `failures += 1`
       and `update_dormant_gone()` calls mutate the same Python object the
       task already holds; that mutation is picked up and persisted by the
       task's own later `session.commit()` regardless of which session
       object's `.commit()` was called in between.

    So the two writers are not racing over `instance`'s data: there is only
    ever one attached session for the object either code path can mutate.
    This is the same shape sub-project 31's PC2 investigated and correctly
    left alone. The test below is design 2, kept as a standing check on this
    invariant.
    """

    def test_the_failure_bookkeeping_survives_without_patch_db_session(self, db_session, monkeypatch):
        """DC1 discriminator (design 2 of 2; see class docstring for design 1
        and why it could not discriminate).

        Forces `get_request_instance`'s exception path (`app/utils.py:190-196`)
        so its `instance.failures += 1` / `update_dormant_gone()` /
        `db.session.commit()` actually run, uncommitted through the task's own
        session. Then reads the row back through an unrelated, freshly-expired
        handle and checks the write actually landed -- the concrete
        consequence a lost update would have.

        Result: PASSES against unmodified code (no `patch_db_session` wrapper).
        `failures` ends at 3, not lost or halved: `not nodeinfo_href` is true,
        so the nodeinfo-discovery block runs, `get_request_instance` raises
        internally and takes its except branch (+1, in-memory, on the same
        object the task holds), the caller's `elif nodeinfo.status_code >= 300`
        arm adds a second +1 (its status is the helper's synthetic
        `httpx.Response(status_code=500)`), and because `instance.nodeinfo_href`
        is still empty afterward, the second `if instance.nodeinfo_href` block's
        `else` arm adds a third +1 before the task's own `session.commit()`
        flushes the total. Neither threshold (5, then 12) is crossed, so
        `dormant` and `gone_forever` stay `False`. This does not prove no
        wrapper is ever needed elsewhere -- only that this call site's specific
        writes are not lost -- but it is what "the defect matters" would have
        to mean here, and it does not hold.
        """
        def _raise(*args, **kwargs):
            raise httpx.HTTPError('transport down')

        monkeypatch.setattr('app.utils.get_request', _raise)
        instance = _seed_instance('peer.example')
        instance.nodeinfo_href = None
        db.session.commit()
        instance_id = instance.id

        monitor_healthy_instances()

        db.session.expire_all()
        fresh = db.session.query(Instance).filter_by(id=instance_id).first()
        assert fresh.failures == 3
        assert fresh.dormant is False
        assert fresh.gone_forever is False

    def test_a_raising_helper_does_not_end_the_whole_sweep(self, db_session, monkeypatch):
        """DC2: a raising `get_request_instance` no longer ends the sweep.

        Before the guard, `:590` closed `nodeinfo`, which `:559` might never
        have bound: if `get_request_instance` raised, the `except` at `:585`
        caught it and then the `finally` at `:588-590` raised
        `UnboundLocalError` -- which that handler had already run and could
        not catch. It escaped to `:736`, rolled back and re-raised, so one
        instance's failure ended the sweep for every other instance. The
        `nodeinfo = None` / `node = None` bindings ahead of each `try` and the
        `is not None` guards on `.close()` fix that: the `except` arm's own
        `instance.failures += 1` runs instead, and the loop continues to the
        next instance.

        The oracle is that BOTH instances were touched, compared as a set:
        `:540` returns planner-ordered rows and this file asserts no order over
        those.
        """
        def _raise(*args, **kwargs):
            raise RuntimeError('helper exploded')

        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance', _raise)
        for domain in ('one.example', 'two.example'):
            instance = _seed_instance(domain)
            instance.nodeinfo_href = None
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        touched = {i.domain for i in db.session.query(Instance).all() if i.failures > 0}
        assert touched == {'one.example', 'two.example'}

    def test_discovery_assigns_the_matching_href(self, db_session, monkeypatch):
        """`:574-577`. A rel in the recognised set supplies the href and clears
        the failure state.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(200, {'links': [
                {'rel': NODEINFO_LINK, 'href': 'https://peer.example/nodeinfo/2.0'}]})))
        instance = _seed_instance('peer.example')
        instance.nodeinfo_href = None
        instance.failures = 3
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='peer.example').first()
        assert reloaded.nodeinfo_href == 'https://peer.example/nodeinfo/2.0'
        assert reloaded.failures == 0

    def test_a_non_dict_link_before_a_match_does_not_abort_discovery(self, db_session, monkeypatch):
        """`:569`'s `isinstance` guard, taking its false arm on a bare string
        that precedes a matching entry.

        A single non-dict string with no entry after it cannot tell the
        guard's presence from its absence: `isinstance(links, dict)`
        short-circuits `'rel' in links` for any plain string that does not
        itself contain the substring `'rel'`, so removing the guard changes
        nothing observable for that shape alone. Here the leading entry IS the
        string `'rel'` (so `'rel' in links` is True on a bare string once the
        `isinstance` short-circuit is gone) and a genuine match follows it.
        With the guard intact, `isinstance('rel', dict)` is False, the whole
        condition short-circuits before `links['rel']` is ever evaluated, so
        nothing happens for that entry, and the loop moves on to match the
        second entry. Without the guard, `'rel' in links` is True and
        `links['rel']` is evaluated on a plain string, raising `TypeError`
        (string indices must be integers) -- which escapes the `for` loop
        entirely, so the second, matching entry is never reached and
        `nodeinfo_href` stays unset. Verified by hand-negating `:569` (dropping
        `isinstance(links, dict) and `): the loop then raises on the first
        entry, the outer `except` at `:585-587` swallows it, and
        `nodeinfo_href` stays `None` instead of being set to the second
        entry's href.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(200, {'links': [
                'rel',
                {'rel': NODEINFO_LINK, 'href': 'https://odd.example/nodeinfo/2.0'},
            ]})))
        instance = _seed_instance('odd.example')
        instance.nodeinfo_href = None
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        assert db.session.query(Instance).filter_by(
            domain='odd.example').first().nodeinfo_href == 'https://odd.example/nodeinfo/2.0'

    def test_a_non_200_discovery_counts_a_failure(self, db_session, monkeypatch):
        """`:582`'s `elif` and its `:584` increment. A 404 is logged and
        counted, not retried.

        `failures` ends at 2, not merely nonzero: `:584` counts the non-200
        discovery response, and because `nodeinfo_href` is still unset
        afterward, `:593`'s `else` arm at `:627` counts a second failure
        before the task's own commit. Asserting only `failures > 0` would not
        discriminate `:584`'s increment from `:627`'s -- removing `:584` alone
        still leaves `failures == 1 > 0`, so the loose assertion cannot fail on
        that regression. The exact count of 2 is what ties this assertion to
        `:584` specifically.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(404)))
        instance = _seed_instance('missing.example')
        instance.nodeinfo_href = None
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        assert db.session.query(Instance).filter_by(
            domain='missing.example').first().failures == 2

    def test_a_genuine_3xx_discovery_response_counts_a_failure(self, db_session, monkeypatch):
        """`:582`'s `elif`, TRUE arm, with a status actually inside `[300, 400)`
        rather than `test_a_non_200_discovery_counts_a_failure`'s 404.

        Task 11's mutation testing found `:582`'s `>= 300` survives being
        mutated to `>= 400` under every existing test in this file: the only
        discovery statuses exercised are 200 (`< 300`) and 404 (`>= 400`), so
        nothing here distinguished the boundary actually written from one
        drawn 100 higher. A 304 sits strictly between the two and is real --
        redirects and not-modified responses are exactly what a `>= 300`
        catch-all is for.

        The oracle mirrors the 404 test's: 2, not merely nonzero. `:584`
        counts the 304 itself, and because `nodeinfo_href` stays unset,
        `:593`'s `else` arm at `:627` counts a second failure. Regression this
        catches: narrowing `:582` to `>= 400` (or any bound above 304) leaves
        a 304 unmatched by `==200` or the narrowed `elif`, so `:584` never
        fires and only `:627`'s single increment lands -- `failures` ends at
        1 instead of 2.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(304)))
        instance = _seed_instance('redirected.example')
        instance.nodeinfo_href = None
        instance.failures = 0
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        assert db.session.query(Instance).filter_by(
            domain='redirected.example').first().failures == 2

    def test_an_unmatched_document_counts_one_failure_not_one_per_link(self, db_session, monkeypatch):
        """DC3: before the fix, the increment sat INSIDE the per-link loop
        (`:568-579`), firing once per unrelated link instead of once per
        document.

        Three unrelated links would have recorded three failures under that
        defect, so a document's shape -- not the instance's reachability --
        would have driven `update_dormant_gone`'s thresholds
        (`app/models.py:146-150`: dormant above 2, gone above 7). The
        `matched` flag (`:567`, set `:578`) fixed it: the increment now runs
        at `:580-581`, after the loop, at most once regardless of how many
        links a document lists.

        Two increments are expected in total: one for the unmatched document,
        and one from the no-href arm below the fetch block.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(200, {'links': [
                {'rel': 'https://example.invalid/a', 'href': 'https://x.example/a'},
                {'rel': 'https://example.invalid/b', 'href': 'https://x.example/b'},
                {'rel': 'https://example.invalid/c', 'href': 'https://x.example/c'},
            ]})))
        instance = _seed_instance('noisy.example')
        instance.nodeinfo_href = None
        instance.failures = 0
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        assert db.session.query(Instance).filter_by(
            domain='noisy.example').first().failures == 2

    def test_a_lemmy_point_release_above_nine_is_rediscovered(self, db_session, monkeypatch):
        """DC4: the version check compares strings.

        `'0.19.10' >= '0.19.4'` is False lexically, so exactly the newer Lemmy
        instances this check exists to catch keep their stale
        `nodeinfo/2.0.json` href instead of rediscovering it.

        The oracle is the rewritten href. Asserting the instance is merely
        'healthy' would hold on both sides of the fix.

        A seeded lemmy instance is online (`dormant`/`gone_forever` default
        False) by construction, so `:637`'s admin-role block IS entered
        regardless of this defect -- observed directly, contrary to an
        earlier assumption that a lemmy fixture here would not reach it.
        `get_request` (not `get_request_instance`) is patched to a harmless
        404 purely so that unrelated, already-mocked-away block does not
        attempt a real HTTP call; it has no bearing on the DC4 assertion
        below, which depends only on `get_request_instance`'s recorder.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(200, {'links': [
                {'rel': NODEINFO_LINK, 'href': 'https://lemmy.example/nodeinfo/2.1'}]})))
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request', _Recorder(result=_response(404)))
        instance = _seed_instance('lemmy.example', software='lemmy')
        instance.version = '0.19.10'
        instance.nodeinfo_href = 'https://lemmy.example/nodeinfo/2.0.json'
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        assert db.session.query(Instance).filter_by(
            domain='lemmy.example').first().nodeinfo_href == 'https://lemmy.example/nodeinfo/2.1'

    def test_a_healthy_node_document_clears_the_failure_state(self, db_session, monkeypatch):
        """The fetch block's 200 arm (`:597-604`): software, version and the
        three flags.

        `get_request` (not `get_request_instance`) is patched to a harmless
        404 because this arm sets `instance.software = 'piefed'` (`:600`) and
        clears `dormant` (`:603`), so `instance.online()` is True and `:637`'s
        admin-role guard IS entered once `software` becomes 'piefed'. An
        unpatched call there would reach a live transport and, on the
        pre-existing unbound-`response` bug at `:689`, raise instead of
        merely logging a failure -- unrelated to what this test checks.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(200, {'software': {'name': 'PieFed', 'version': '1.2.3'}})))
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request', _Recorder(result=_response(404)))
        instance = _seed_instance('healthy.example')
        instance.nodeinfo_href = 'https://healthy.example/nodeinfo/2.0'
        instance.failures = 4
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='healthy.example').first()
        assert reloaded.software == 'piefed'
        assert reloaded.version == '1.2.3'
        assert reloaded.failures == 0
        assert reloaded.dormant is False

    def test_a_non_200_node_response_drops_the_href_and_counts_a_failure(self, db_session, monkeypatch):
        """The `elif ... >= 300` arm (`:605-611`): the href is cleared so
        discovery re-runs next sweep, and `most_recent_attempt` is stamped.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(503)))
        instance = _seed_instance('flaky.example')
        instance.nodeinfo_href = 'https://flaky.example/nodeinfo/2.0'
        instance.failures = 0
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='flaky.example').first()
        assert reloaded.nodeinfo_href is None
        assert reloaded.failures == 1
        assert reloaded.most_recent_attempt is not None

    def test_the_sixth_failure_turns_an_instance_dormant(self, db_session, monkeypatch):
        """The BOUNDARY in the `elif ... >= 300` arm (`:609`): `> 5`, so five
        failures is not enough and six is.

        Seeded at 5, the sweep's own increment makes 6.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(503)))
        instance = _seed_instance('sixth.example')
        instance.nodeinfo_href = 'https://sixth.example/nodeinfo/2.0'
        instance.failures = 5
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='sixth.example').first()
        assert reloaded.failures == 6
        assert reloaded.dormant is True
        assert reloaded.start_trying_again is not None

    def test_the_fifth_failure_does_not(self, db_session, monkeypatch):
        """The other side of the same boundary (`:609`). Seeded at 4, ending at 5."""
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(503)))
        instance = _seed_instance('fifth.example')
        instance.nodeinfo_href = 'https://fifth.example/nodeinfo/2.0'
        instance.failures = 4
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='fifth.example').first()
        assert reloaded.failures == 5
        assert reloaded.dormant is False

    def test_an_instance_with_no_href_after_discovery_escalates(self, db_session, monkeypatch):
        """The else arm below the fetch block (`:626-634`), reached when
        discovery found nothing. This is a DIFFERENT path from the fetch
        block's failure arm and has its own threshold checks.

        Seeded at 12: discovery's own `matched=False` increment (`:581`)
        takes it to 13, then this else arm's increment (`:627`) takes it to
        14 before `:632`'s `> 12` check fires -- comfortably past the
        boundary, not pinned to it, so this proves the else arm's
        `gone_forever` assignment (`:633`) fires at all but not exactly where.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(200, {'links': []})))
        instance = _seed_instance('nohref.example')
        instance.nodeinfo_href = None
        instance.failures = 12
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='nohref.example').first()
        assert reloaded.gone_forever is True

    def test_the_sixth_failure_in_the_no_href_else_arm_turns_dormant(self, db_session, monkeypatch):
        """The BOUNDARY in the no-href `else` arm (`:629`): `> 5`, mirroring
        `test_the_sixth_failure_turns_an_instance_dormant` for the fetch
        block's `elif` arm but for the path that never has a
        `nodeinfo_href` at all.

        Task 11's mutation testing found `:629` survives being mutated to
        `>= 5`: `test_an_instance_with_no_href_after_discovery_escalates`
        seeds this same arm at 12 and ends at 14, comfortably past the
        boundary on both sides of `> 5` vs `>= 5`, so it cannot tell them
        apart. A 404 discovery response takes the discovery block's own
        `elif >= 300` arm (`:582-584`), counting one failure without ever
        setting `nodeinfo_href`; because it is still unset, `:593`'s `else`
        arm runs too and counts a second. Seeded at 4: discovery's
        increment makes 5, this arm's own increment (`:627`) makes 6 --
        past the boundary.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(404)))
        instance = _seed_instance('nohref-sixth.example')
        instance.nodeinfo_href = None
        instance.failures = 4
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='nohref-sixth.example').first()
        assert reloaded.failures == 6
        assert reloaded.dormant is True
        assert reloaded.start_trying_again is not None

    def test_the_fifth_failure_in_the_no_href_else_arm_does_not(self, db_session, monkeypatch):
        """The other side of the same boundary (`:629`). Seeded at 3:
        discovery's increment (`:584`) makes 4, this arm's own increment
        (`:627`) makes 5 -- not `> 5`.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(404)))
        instance = _seed_instance('nohref-fifth.example')
        instance.nodeinfo_href = None
        instance.failures = 3
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='nohref-fifth.example').first()
        assert reloaded.failures == 5
        assert reloaded.dormant is False

    def test_the_thirteenth_failure_in_the_no_href_else_arm_ends_gone_forever(self, db_session, monkeypatch):
        """The BOUNDARY in the no-href `else` arm (`:632`): `> 12`, the same
        shape as `test_the_sixth_failure_in_the_no_href_else_arm_turns_dormant`
        above but for the `gone_forever` threshold.

        Task 11's mutation testing found `:632` survives being mutated to
        `>= 12` for the same reason as `:629`:
        `test_an_instance_with_no_href_after_discovery_escalates` ends at 14,
        past the boundary on both sides. Seeded at 11: discovery's own
        increment makes 12, this arm's own increment (`:627`) makes 13 --
        past the boundary.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(404)))
        instance = _seed_instance('nohref-thirteenth.example')
        instance.nodeinfo_href = None
        instance.failures = 11
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='nohref-thirteenth.example').first()
        assert reloaded.failures == 13
        assert reloaded.gone_forever is True

    def test_the_twelfth_failure_in_the_no_href_else_arm_does_not_end_gone_forever(self, db_session, monkeypatch):
        """The other side of the same boundary (`:632`). Seeded at 10:
        discovery's increment makes 11, this arm's own increment makes 12 --
        not `> 12`. `dormant` is set regardless (12 > 5).
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(404)))
        instance = _seed_instance('nohref-twelfth.example')
        instance.nodeinfo_href = None
        instance.failures = 10
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='nohref-twelfth.example').first()
        assert reloaded.failures == 12
        assert reloaded.dormant is True
        assert reloaded.gone_forever is False

    def test_a_banned_domain_is_skipped_before_any_request(self, db_session, monkeypatch):
        """`:547`'s true arm. The oracle is that no request was made."""
        recorder = _Recorder(result=_response(200, {'software': {'name': 'PieFed', 'version': '1.0'}}))
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance', recorder)
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.instance_banned', lambda domain: True)
        instance = _seed_instance('banned.example')
        instance.nodeinfo_href = 'https://banned.example/nodeinfo/2.0'
        db.session.commit()

        monitor_healthy_instances()

        assert recorder.calls == []

    def test_a_discovery_response_between_200_and_300_counts_no_failure_here(self, db_session, monkeypatch):
        """`:582`'s `elif`, the FALSE arm. A status code that is neither 200
        (`:565`) nor >= 300 (`:582`) falls through the whole if/elif chain
        untouched and lands straight on the `finally` at `:588-590`. Neither
        existing discovery test reaches this: the 200 tests take `:565`'s
        true arm, and `test_a_non_200_discovery_counts_a_failure` uses a 404,
        which takes `:582`'s TRUE arm.

        Regression this catches: widening `:582` so it also matches a 250
        (e.g. changing `>= 300` to `>= 200`) would add a second failure here
        -- `:584`'s increment on top of the no-href `else` arm's `:627` -- so
        `failures` would end at 2 instead of 1.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(250)))
        instance = _seed_instance('inbetween.example')
        instance.nodeinfo_href = None
        instance.failures = 0
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='inbetween.example').first()
        assert reloaded.failures == 1
        assert reloaded.nodeinfo_href is None

    def test_a_node_response_between_200_and_300_leaves_state_untouched(self, db_session, monkeypatch):
        """`:605`'s `elif`, the FALSE arm -- the second fetch block's
        counterpart to the discovery test above, reached when a
        `nodeinfo_href` is already on file. A 250 satisfies neither `:597`'s
        `== 200` nor `:605`'s `>= 300`, so nothing in the if/elif body runs
        and control falls straight to the `finally` at `:621-623`.

        Regression this catches: widening `:605`'s `>= 300` to also match
        250 would clear `nodeinfo_href` and add a failure; narrowing `:597`'s
        `== 200` so a 250 is treated as success would instead reset
        `failures` to 0 and set `software`/`version`. Either mutation
        changes one of this test's unchanged-state assertions.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(250)))
        instance = _seed_instance('node-inbetween.example')
        instance.nodeinfo_href = 'https://node-inbetween.example/nodeinfo/2.0'
        instance.failures = 3
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='node-inbetween.example').first()
        assert reloaded.failures == 3
        assert reloaded.nodeinfo_href == 'https://node-inbetween.example/nodeinfo/2.0'
        assert reloaded.software == 'mastodon'
        assert reloaded.version is None

    def test_a_raising_node_fetch_below_both_thresholds_counts_one_failure(self, db_session, monkeypatch):
        """The `except` arm below the second fetch block (`:612-620`), never
        reached by `:605-611`'s own boundary tests
        (`test_the_sixth_failure_turns_an_instance_dormant` and
        `test_the_fifth_failure_does_not`) because those return a non-2xx
        response rather than raising.

        Seeded at 4, ending at 5: not `> 5` (`:616`), so `dormant` stays
        `False` and lines `:617-618` do not run -- the FALSE side of that
        boundary in an arm none of the existing tests reach. `node` is never
        bound (the raise happens inside `get_request_instance` itself), so
        `:622`'s `is not None` guard also takes its FALSE arm here.
        """
        def _raise(*args, **kwargs):
            raise RuntimeError('node fetch exploded')

        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance', _raise)
        instance = _seed_instance('excepting-below.example')
        instance.nodeinfo_href = 'https://excepting-below.example/nodeinfo/2.0'
        instance.failures = 4
        instance.most_recent_attempt = None
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='excepting-below.example').first()
        assert reloaded.failures == 5
        assert reloaded.dormant is False
        assert reloaded.gone_forever is False
        assert reloaded.most_recent_attempt is not None

    def test_the_sixth_failure_in_the_except_arm_turns_dormant(self, db_session, monkeypatch):
        """The BOUNDARY in the `except` arm (`:616`): `> 5`, mirroring
        `test_the_sixth_failure_turns_an_instance_dormant` for the `elif`
        arm but for the path that raises instead of returning a non-2xx
        response. Seeded at 5, the except handler's own increment (`:614`)
        makes 6.
        """
        def _raise(*args, **kwargs):
            raise RuntimeError('node fetch exploded')

        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance', _raise)
        instance = _seed_instance('excepting-sixth.example')
        instance.nodeinfo_href = 'https://excepting-sixth.example/nodeinfo/2.0'
        instance.failures = 5
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='excepting-sixth.example').first()
        assert reloaded.failures == 6
        assert reloaded.dormant is True
        assert reloaded.gone_forever is False
        assert reloaded.start_trying_again is not None

    def test_the_thirteenth_failure_in_the_except_arm_ends_gone_forever(self, db_session, monkeypatch):
        """The BOUNDARY in the `except` arm (`:619`): `> 12`, the one none of
        this module's existing tests pin exactly -- `test_an_instance_with_no_href_after_discovery_escalates`
        seeds its (different, `else`-arm) path high enough to end at 14, past
        the boundary rather than on it. Seeded at 12, the except handler's
        own increment (`:614`) makes 13.
        """
        def _raise(*args, **kwargs):
            raise RuntimeError('node fetch exploded')

        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance', _raise)
        instance = _seed_instance('excepting-thirteenth.example')
        instance.nodeinfo_href = 'https://excepting-thirteenth.example/nodeinfo/2.0'
        instance.failures = 12
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='excepting-thirteenth.example').first()
        assert reloaded.failures == 13
        assert reloaded.dormant is True
        assert reloaded.gone_forever is True

    def test_the_twelfth_failure_in_the_except_arm_does_not_end_gone_forever(self, db_session, monkeypatch):
        """The other side of the same boundary (`:619`). Seeded at 11, ending
        at 12: `dormant` is set (12 > 5) but `gone_forever` is not (12 is not
        > 12).
        """
        def _raise(*args, **kwargs):
            raise RuntimeError('node fetch exploded')

        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance', _raise)
        instance = _seed_instance('excepting-twelfth.example')
        instance.nodeinfo_href = 'https://excepting-twelfth.example/nodeinfo/2.0'
        instance.failures = 11
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='excepting-twelfth.example').first()
        assert reloaded.failures == 12
        assert reloaded.dormant is True
        assert reloaded.gone_forever is False


class TestCheckInstanceHealthGoneForever:
    """`check_instance_health:427` -- first loop, `:435-443`.

    `:435` computes the cutoff, `:436-439` selects dormant instances whose
    `start_trying_again` is already past it, `:441-442` marks each
    `gone_forever`, and `:443` commits.

    The whole body sits inside `patch_db_session(session)` at `:431`, unlike
    `monitor_healthy_instances`. That is DC1's subject and is why this task's
    tests need no session gymnastics.
    """

    def _dormant(self, domain, days_ago):
        instance = _seed_instance(domain)
        instance.dormant = True
        instance.gone_forever = False
        instance.start_trying_again = utcnow() - timedelta(days=days_ago)
        return instance

    def test_a_long_dormant_instance_is_marked_gone(self, db_session, monkeypatch):
        """`:442`'s assignment. The recheck loop is neutralised so this test
        observes only the first loop: a raising helper would otherwise route
        into `:494-497` and change `failures`.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(500)))
        self._dormant('gone.example', days_ago=6)
        db.session.commit()

        check_instance_health()

        db.session.expire_all()
        assert db.session.query(Instance).filter_by(
            domain='gone.example').first().gone_forever is True

    def test_a_recently_dormant_instance_is_not_marked_gone(self, db_session, monkeypatch):
        """The BOUNDARY at `:438`: `start_trying_again` must be older than the
        five-day cutoff, not merely set.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(500)))
        self._dormant('recent.example', days_ago=4)
        db.session.commit()

        check_instance_health()

        db.session.expire_all()
        assert db.session.query(Instance).filter_by(
            domain='recent.example').first().gone_forever is False

    def test_a_live_instance_is_untouched_by_the_sweep(self, db_session, monkeypatch):
        """`:437`'s `dormant == True` filter, and `:447`'s.

        The instance's `start_trying_again` is 99 days past, so it satisfies
        `:438`'s cutoff on its own and only `:437` keeps it out of the first
        loop -- drop that filter and `gone_forever` is wrongly set.

        The helper RAISES rather than returning a non-200, and that choice is
        what makes the second assertion bind. `failures` is incremented only in
        the recheck loop's `except` arm at `:494-497`, never on a non-2xx
        branch, so a helper that merely returns a 500 would leave `failures` at
        0 whether or not `:447` selected the row. Raising means selection is
        observable.
        """
        def _raise(*args, **kwargs):
            raise RuntimeError('the live instance should never be rechecked')

        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance', _raise)
        instance = _seed_instance('live.example')
        instance.dormant = False
        instance.start_trying_again = utcnow() - timedelta(days=99)
        db.session.commit()

        check_instance_health()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='live.example').first()
        assert reloaded.gone_forever is False
        assert reloaded.failures == 0


class TestCheckInstanceHealthRecheck:
    """`check_instance_health`'s second loop, `:446-497`.

    `:446-450` selects dormant instances that are not yet gone and are not
    instance 1. `:453` skips banned domains and the flipboard.com literal.
    `:458` forks on whether a `nodeinfo_href` is already known: with one,
    `:459` fetches it and `:463-467` revives the instance; without, `:473`
    discovers one and `:481-491` walks the links. `:494-497` catches whatever
    either path raises, rolls back and counts a failure.

    `:469-470` and `:492-493` are `finally` blocks that close the response.
    Unlike `monitor_healthy_instances`, both names are bound before the `try`
    they belong to, so DC2's defect does not exist here.
    """

    def _dormant(self, domain, href=None):
        instance = _seed_instance(domain)
        instance.dormant = True
        instance.gone_forever = False
        instance.start_trying_again = utcnow() + timedelta(days=1)
        instance.nodeinfo_href = href
        return instance

    def test_a_known_href_returning_software_revives_the_instance(self, db_session, monkeypatch):
        """`:464-467`. Deleting `:467` leaves the instance dormant."""
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(200, {'software': {'name': 'PieFed', 'version': '1.2.3'}})))
        self._dormant('back.example', href='https://back.example/nodeinfo/2.0')
        db.session.commit()

        check_instance_health()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='back.example').first()
        assert reloaded.dormant is False
        assert reloaded.software == 'piefed'
        assert reloaded.version == '1.2.3'
        assert reloaded.failures == 0

    def test_a_document_without_software_leaves_the_instance_dormant(self, db_session, monkeypatch):
        """`:463`'s false arm. A 200 alone is not enough to revive.

        `failures == 0` is what actually binds this to `:463`: with the guard
        intact, `'software' in node_json` is False and nothing past `:463`
        runs, so `failures` stays 0. Negate the guard and `node_json['software']`
        raises `KeyError` (confirmed: the payload has no `'software'` key), the
        outer `except` at `:494` catches it, and `:496` makes `failures` 1 --
        while `dormant` alone stays `True` either way, since `:467` is never
        reached on either path. Checked by hand-negating `:463` (see task-5
        report/commit for the verbatim failure) and confirming `dormant is True`
        does NOT fail but `failures == 0` DOES.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(200, {'unexpected': True})))
        self._dormant('quiet.example', href='https://quiet.example/nodeinfo/2.0')
        db.session.commit()

        check_instance_health()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='quiet.example').first()
        assert reloaded.dormant is True
        assert reloaded.failures == 0

    def test_a_non_200_leaves_the_instance_dormant(self, db_session, monkeypatch):
        """`:460`'s false arm.

        The 503 carries a valid `{'software': ...}` body on purpose: with
        `:460` intact, the body is never parsed and `dormant` stays `True`.
        Negate `:460` (treat 503 as if it were 200) and the same valid body
        parses cleanly, `:463`'s guard is satisfied, and `:467` sets
        `dormant = False` -- no crash intervenes, because this body (unlike an
        empty 503) is exactly the shape `:464-467` expects. That is what makes
        this bind to `:460` specifically, rather than only proving the row was
        selected.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(503, {'software': {'name': 'PieFed', 'version': '1.2.3'}})))
        self._dormant('down.example', href='https://down.example/nodeinfo/2.0')
        db.session.commit()

        check_instance_health()

        db.session.expire_all()
        assert db.session.query(Instance).filter_by(
            domain='down.example').first().dormant is True

    def test_discovery_finds_an_href_and_revives_the_instance(self, db_session, monkeypatch):
        """`:471`'s else arm and `:487-490`. The instance has no known href, so
        `:473` asks well-known/nodeinfo and `:482`'s rel match supplies one.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(200, {'links': [
                {'rel': NODEINFO_LINK, 'href': 'https://found.example/nodeinfo/2.0'}]})))
        self._dormant('found.example', href=None)
        db.session.commit()

        check_instance_health()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='found.example').first()
        assert reloaded.nodeinfo_href == 'https://found.example/nodeinfo/2.0'
        assert reloaded.dormant is False

    def test_discovery_matches_the_https_schema_2_0_variant(self, db_session, monkeypatch):
        """`:484`, the `https://` variant of the 2.0 schema rel -- one of the
        three URLs `:482`'s `in` check accepts.

        Task 11's mutation testing found dropping `:484` alone survives
        every existing test in this file: `NODEINFO_LINK` (the module
        constant every other discovery test here uses) is the `:483`
        `http://` variant, so nothing exercised `:484` or `:485` before this.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(200, {'links': [
                {'rel': 'https://nodeinfo.diaspora.software/ns/schema/2.0',
                 'href': 'https://https-variant.example/nodeinfo/2.0'}]})))
        self._dormant('https-variant.example', href=None)
        db.session.commit()

        check_instance_health()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='https-variant.example').first()
        assert reloaded.nodeinfo_href == 'https://https-variant.example/nodeinfo/2.0'
        assert reloaded.dormant is False

    def test_discovery_matches_the_2_1_schema_variant(self, db_session, monkeypatch):
        """`:485`, the 2.1 schema rel -- the third of the three URLs `:482`'s
        `in` check accepts. See `test_discovery_matches_the_https_schema_2_0_variant`
        above for why this line was previously unexercised.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(200, {'links': [
                {'rel': 'http://nodeinfo.diaspora.software/ns/schema/2.1',
                 'href': 'https://schema21.example/nodeinfo/2.1'}]})))
        self._dormant('schema21.example', href=None)
        db.session.commit()

        check_instance_health()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='schema21.example').first()
        assert reloaded.nodeinfo_href == 'https://schema21.example/nodeinfo/2.1'
        assert reloaded.dormant is False

    def test_a_link_list_with_no_match_leaves_the_instance_dormant(self, db_session, monkeypatch):
        """`:482`'s false arm, taken for every link. `:481`'s loop ends without
        a break and nothing is assigned.
        """
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance',
            _Recorder(result=_response(200, {'links': [
                {'rel': 'https://example.invalid/other', 'href': 'https://x.example/y'}]})))
        self._dormant('nomatch.example', href=None)
        db.session.commit()

        check_instance_health()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='nomatch.example').first()
        assert reloaded.nodeinfo_href is None
        assert reloaded.dormant is True

    def test_a_banned_domain_is_skipped_before_any_request(self, db_session, monkeypatch):
        """`:453`'s true arm. The oracle is that no request was made at all --
        asserting only that the instance stayed dormant would hold anyway.
        """
        recorder = _Recorder(result=_response(200, {'software': {'name': 'PieFed', 'version': '1.0'}}))
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance', recorder)
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.instance_banned', lambda domain: True)
        self._dormant('banned.example', href='https://banned.example/nodeinfo/2.0')
        db.session.commit()

        check_instance_health()

        assert recorder.calls == []

    def test_flipboard_is_skipped_before_any_request(self, db_session, monkeypatch):
        """`:453`'s second guard, the `flipboard.com` literal. `instance_banned`
        is left real (not mocked to `True`) so this test cannot pass merely
        because the domain happens to be banned in the database -- there is
        no `BannedInstances` row for it, so `instance_banned('flipboard.com')`
        returns `False` and only the literal comparison can skip this row.

        Task 11's mutation testing found dropping ` or instance.domain ==
        'flipboard.com'` from `:453` survives every existing test in this
        file: none of them seeds that domain.
        """
        recorder = _Recorder(result=_response(200, {'software': {'name': 'PieFed', 'version': '1.0'}}))
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance', recorder)
        self._dormant('flipboard.com', href='https://flipboard.com/nodeinfo/2.0')
        db.session.commit()

        check_instance_health()

        assert recorder.calls == []

    def test_a_raising_request_counts_a_failure(self, db_session, monkeypatch):
        """`:496`'s increment, on the single-instance path where it survives.

        One instance only. With two, `:495`'s rollback discards the first
        instance's uncommitted increment before the second reaches `:499`'s
        commit -- see the sweep test below, and the register entry for the
        batched commit.
        """
        def _raise(*args, **kwargs):
            raise RuntimeError('recheck exploded')

        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance', _raise)
        self._dormant('bad-one.example', href='https://bad-one.example/nodeinfo/2.0')
        db.session.commit()

        check_instance_health()

        db.session.expire_all()
        assert db.session.query(Instance).filter_by(
            domain='bad-one.example').first().failures == 1

    def test_a_raising_request_does_not_end_the_sweep(self, db_session, monkeypatch):
        """`:494`'s handler lets the loop continue to the next instance.

        The oracle is which domains were ATTEMPTED, not what was persisted.
        `:499` commits once after the whole loop and `:495` rolls back inside
        it, so with two raisers only the last one's `:496` increment survives
        -- a real defect, registered by this round rather than fixed. Asserting
        over `failures` here would lock that behaviour in as though it were the
        contract.

        `get_request_instance(uri, instance: Instance, params=None,
        headers=None)` (`app/utils.py:189`) and the call site at `:459` passes
        `instance` by keyword (`get_request_instance(instance.nodeinfo_href,
        headers=HEADERS, instance=instance)`), so it never lands in `args`
        past index 0 -- the recorder reads `kwargs['instance']`.

        A set comparison: `:446` returns planner-ordered rows.
        """
        attempted = []

        def _raise(*args, **kwargs):
            attempted.append(args[1].domain if len(args) > 1 else kwargs['instance'].domain)
            raise RuntimeError('recheck exploded')

        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request_instance', _raise)
        self._dormant('bad-one.example', href='https://bad-one.example/nodeinfo/2.0')
        self._dormant('bad-two.example', href='https://bad-two.example/nodeinfo/2.0')
        db.session.commit()

        check_instance_health()

        assert set(attempted) == {'bad-one.example', 'bad-two.example'}

    def test_a_raising_banned_check_rolls_back_and_reraises(self, db_session, monkeypatch):
        """`:501-503`, the function's OUTER handler -- distinct from the inner
        one at `:494-497` the rest of this class documents. `instance_banned`
        is called at `:453`, BEFORE the inner `try` opens at `:456`, so an
        exception raised there escapes the inner handler untouched and is
        caught only by the outer `try` wrapping the whole function body
        (`:430-503`).

        Two oracles, because `pytest.raises` alone cannot tell `:501-503`
        apart from having no handler at all: with no `except` clause, the
        same `RuntimeError` would still propagate out of the function
        unchanged, and `pytest.raises` would still pass.

          1. `Session.rollback` is spied at the class level (the task's
             `session` is a plain `Session(bind=db.engine)` from
             `get_task_session`, `app/utils.py:3673-3675`, not a name this
             test can reach directly) and the spy list is cleared
             immediately before the call, so it can only record activity
             from this one invocation. This is what pins `:502`
             specifically -- deleting just that line changes nothing else
             this test checks.
          2. `pytest.raises(RuntimeError, match=...)` pins `:503`'s `raise`
             (a bare `except: pass` would swallow the error instead) and,
             together with the rollback spy firing at all, pins `:501`'s
             `except Exception:` actually catching it (an absent handler
             would still propagate the same exception, but the rollback spy
             would then record nothing).
        """
        def _raise(domain):
            raise RuntimeError('banned check exploded')

        monkeypatch.setattr('app.shared.tasks.maintenance.instance_banned', _raise)
        self._dormant('outer-handler.example', href='https://outer-handler.example/nodeinfo/2.0')
        db.session.commit()

        rollback_calls = []
        original_rollback = Session.rollback

        def _spy_rollback(self_session, *args, **kwargs):
            rollback_calls.append(self_session)
            return original_rollback(self_session, *args, **kwargs)

        monkeypatch.setattr(Session, 'rollback', _spy_rollback)
        rollback_calls.clear()

        with pytest.raises(RuntimeError, match='banned check exploded'):
            check_instance_health()

        assert rollback_calls, "session.rollback() at :502 did not run"


class TestVersionAtLeast:
    """`_version_at_least` -- the comparison DC4 introduced."""

    def test_a_double_digit_patch_outranks_a_single_digit_one(self):
        """The defect that motivated the helper: lexically '0.19.10' < '0.19.4'."""
        assert _version_at_least('0.19.10', '0.19.4') is True

    def test_an_older_release_does_not_qualify(self):
        assert _version_at_least('0.18.9', '0.19.4') is False

    def test_an_exact_match_qualifies(self):
        assert _version_at_least('0.19.4', '0.19.4') is True

    def test_a_missing_segment_is_treated_as_zero(self):
        assert _version_at_least('1', '1.0.0') is True

    def test_a_non_numeric_suffix_does_not_raise(self):
        assert _version_at_least('1.2.3-rc1', '1.2.3') is True
