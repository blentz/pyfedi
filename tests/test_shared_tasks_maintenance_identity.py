"""Group C's identity phases in `app/shared/tasks/maintenance.py`.

Sub-projects 29, 30, 31 and 32 closed the rest of this module. This file
covers what was left: the two blocks of `monitor_healthy_instances` gated on
`instance.software`, plus the task's own outer handler.

  Lemmy/PieFed admin roles and custom emoji  `:637-696`
  MBIN admin roles                           `:703-739`
  the task's outer handler                   `:742-744`

Task 9 measured the whole module across all five maintenance test files and
found two further gaps: `:733-735` (the MBIN block's own `except`, never
exercised because every MBIN test above reaches its failure through a caught
crash inside the `try` rather than the request itself raising) and
`delete_old_soft_deleted_content:270`'s FALSE arm, in a task outside this
file's stated scope. Task 9's binding constraints allow writing only to this
file, so `TestMbinAdminRoleFailures` and
`TestDeleteOldSoftDeletedContentReplyRace` below close both regardless of
which task they belong to.

ENTRY IS GATED ON `software`. `:637` needs 'lemmy', 'piefed' or 'pylova';
`:703` needs 'mbin'. Sub-project 32's fixtures used `make_instance`'s
'mastodon' default, so neither block ran and both `if` statements were
covered only on their false arms. Every fixture here sets a matching value --
which means a test that sets `software` and forgets about these blocks enters
them anyway. Sub-project 32 learned that when a version test entered the
Lemmy block by accident and crashed.

THE HTTP HALF RUNS FIRST. An instance with `software` set still goes through
discovery and the fetch block above. `_quiet_http_half` neutralises them so a
test observes only the identity phases.

THE OUTER HANDLER IS REACHED THROUGH `:547`. `instance_banned` is called at
loop level, outside every `try`, so a raise there is the one failure that
reaches `:742` rather than being caught per-instance.

`get_request` RAISES, unlike `get_request_instance` which returns a synthetic
500. Task 1 probed what that meant for a test that forgot to patch it, before
Task 2's fix: an unpatched `get_request` raised inside the `try`, and because
`response` was never assigned before that point in this iteration, the
`finally`'s `if response:` raised `UnboundLocalError` before the `except
Exception` could absorb anything. The task-level `except` then re-raised that
`UnboundLocalError` to the caller -- a forgotten patch killed the task rather
than quietly redirecting it.

Task 2 seeded `response = None` before both `try` blocks (`:638`, `:704`) and
guarded the `finally`s with `is not None`, matching sub-project 32's
fetch-block idiom (`:594`). A raising `get_request` is now caught by
`:690`/`:733`'s `except` and becomes a failure increment instead of a crash --
`TestIdentityPhaseFailures` below covers that.

THE REMOVAL LOOP'S DRAIN-BEFORE-DELETE FIX (`:659-663`) stands on its own
terms: draining `session.query(InstanceRole)...` into `stale_roles` before
issuing any `.delete()` stops the loop from mutating a result set mid-
iteration and desynchronizing the session's identity map, regardless of
what else is pending in the session. It is NOT rescued by, and its
correctness does NOT depend on, pending-add visibility. `get_task_session`
(`app/utils.py:3673-3675`) returns a bare `Session(bind=db.engine)`, which
keeps SQLAlchemy's default `autoflush=True` -- the opposite of `db.session`,
which this project pins to `autoflush=False` (`app/__init__.py:81`). Under
autoflush, a pending `session.add` from `:656` is flushed to the database
before the removal query at `:661` ever executes, whether that query is
consumed directly by a `for` loop (the old shape) or drained into a list
first (the new one) -- both issue the identical SELECT at the identical
program point, so they cannot diverge on whether a pending add is visible.
A fixture built to catch that divergence (a pending add still in flight
when the removal query runs) cannot exist against this session. This was
checked empirically, not just read off the session config: reverting the
drain-before-delete fix and re-running
`test_an_admin_arrives_while_another_leaves_in_the_same_sweep` below --
whose fixture has a genuinely pending create, an admin arriving while
another leaves in the same sweep -- still passed against the reverted
code. The fix is correct because mutating a result set mid-iteration and
desynchronizing the identity map are defects on their own terms, not
because any fixture here can observe the two orderings disagreeing.
"""

from datetime import timedelta

import httpx
import pytest

from app import db
from app.models import Emoji, Instance, InstanceRole, PostReply, User, utcnow
from app.shared.tasks.maintenance import (
    delete_old_soft_deleted_content, monitor_healthy_instances)
from app.utils import get_task_session as _real_get_task_session
from tests.factories import (
    make_community, make_instance, make_post, make_post_reply, make_user)


class _Recorder:
    """`calls` holds one `(args, kwargs)` tuple per invocation.

    So `c[0][0]` is the first positional argument. Same shape as the other
    maintenance test files' recorders; kept identical so a reader moving
    between them does not have to re-learn it.
    """

    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


def _response(status_code=200, payload=None):
    """A real `httpx.Response`, because production calls `.json()` and `.close()`."""
    if payload is None:
        return httpx.Response(status_code=status_code)
    return httpx.Response(status_code=status_code, json=payload)


def _seed_instance(domain, software='mastodon'):
    """Seed an instance the task will actually see.

    `monitor_healthy_instances:543` filters `Instance.id != 1`, and the
    `db_session` teardown resets every sequence with
    `SELECT setval(c.oid, 1, false)` (`tests/conftest.py:131`), so the FIRST
    instance a test seeds lands on exactly the id the task excludes.

    The reserved row absorbs that id and is excluded by STATE rather than by
    id -- dormant and gone_forever, with `start_trying_again` a year out -- so
    it stays invisible wherever it actually lands.
    """
    if db.session.query(Instance).filter_by(domain='reserved-id-one.example').first() is None:
        reserved = Instance(domain='reserved-id-one.example', software='mastodon')
        reserved.dormant = True
        reserved.gone_forever = True
        reserved.start_trying_again = utcnow() + timedelta(days=365)
        db.session.add(reserved)
        db.session.commit()
    return make_instance(domain, software=software)


def _quiet_http_half(monkeypatch):
    """Neutralise the fetch and discovery blocks above the identity phases.

    Every instance here has `software` set, so the HTTP half runs first. A 404
    from `get_request_instance` leaves the instance online -- two failure
    increments, well under `:609`'s threshold of 5 -- and drives no state the
    identity assertions read.
    """
    monkeypatch.setattr(
        'app.shared.tasks.maintenance.get_request_instance',
        lambda *args, **kwargs: _response(404))


def _site_payload(*actor_ids, emojis=None):
    """A Lemmy `/api/v3/site` body: admins, and optionally custom emoji.

    `:646` reads `admin['person']['actor_id']`; `:674` reads
    `emoji['custom_emoji']` and `:675` reads `emoji['keywords']`.
    """
    return {
        'admins': [{'person': {'actor_id': a}} for a in actor_ids],
        'custom_emojis': emojis if emojis is not None else [],
    }


def _emoji(shortcode, url='https://peer.example/e.png', category='cat', keywords=('happy',)):
    """One entry for `_site_payload`'s `custom_emojis` list."""
    return {
        'custom_emoji': {'shortcode': shortcode, 'image_url': url, 'category': category},
        'keywords': [{'keyword': k} for k in keywords],
    }


def _mbin_payload(*items):
    """An MBIN `/api/users/admins` body. `:711` reads `instance_data['items']`."""
    return {'items': list(items)}


class TestTheTaskLevelHandler:
    """`monitor_healthy_instances:742-744` -- the task's own `except`.

    `:745`'s `finally` and `:746`'s `session.close()` were already covered:
    every call reaches them. `:742-744` had never run, because every failure
    inside the loop is caught per-instance. `:547`'s `instance_banned` call is
    the exception -- it sits at loop level, outside every `try`, so a raise
    there is the one that reaches the task handler.
    """

    def test_a_raising_ban_check_rolls_back_and_re_raises(self, db_session, monkeypatch):
        """`:744`'s `raise`. The task does not swallow -- Celery must see it."""
        def _boom(domain):
            raise RuntimeError('ban check exploded')

        monkeypatch.setattr('app.shared.tasks.maintenance.instance_banned', _boom)
        _quiet_http_half(monkeypatch)
        _seed_instance('peer.example', software='lemmy')
        db.session.commit()

        with pytest.raises(RuntimeError, match='ban check exploded'):
            monitor_healthy_instances()


class TestIdentityPhaseFailures:
    """The two identity blocks' error handling."""

    def test_a_raising_request_does_not_end_the_whole_sweep(self, db_session, monkeypatch):
        """DC1: `:694` reads `response`, which `:640` may never have bound.

        `get_request` RAISES, unlike `get_request_instance`. The `except` at
        `:690` catches the original and then the `finally` at `:693-695`
        raises `UnboundLocalError`, which that handler has already run and
        cannot catch. It escapes to `:742`, rolls back and re-raises, so one
        instance's failure ends the sweep for every other instance.

        The oracle is that BOTH instances were touched, compared as a set:
        `:540` returns planner-ordered rows.
        """
        def _raise(*args, **kwargs):
            raise httpx.HTTPError('transport down')

        monkeypatch.setattr('app.shared.tasks.maintenance.get_request', _raise)
        _quiet_http_half(monkeypatch)
        for domain in ('one.example', 'two.example'):
            _seed_instance(domain, software='lemmy')
        db.session.commit()

        monitor_healthy_instances()

        db.session.expire_all()
        touched = {i.domain for i in db.session.query(Instance).all() if i.failures > 2}
        assert touched == {'one.example', 'two.example'}


class TestLemmyAdminRoles:
    """The Lemmy/PieFed block's admin reconciliation.

    `:637` forks on `software`; `:640` on the response; `:645` walks
    `instance_data['admins']`; `:647` requires an http(s) scheme; `:649`
    resolves the actor and `:650` skips one that is already an admin;
    `:651-656` creates the role.

    `find_actor_or_create` is imported at `maintenance.py:13`, so the
    namespace idiom reaches it. `InstanceRole` has a composite primary key
    `(instance_id, user_id)` (`app/models.py:167-168`), so a duplicate add
    raises rather than silently doubling -- `:650`'s guard is what prevents
    that.
    """

    def _lemmy(self, monkeypatch, payload, actor=None):
        _quiet_http_half(monkeypatch)
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request',
            _Recorder(result=_response(200, payload)))
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.find_actor_or_create',
            lambda profile_id, **kwargs: actor)

    def test_a_listed_admin_gets_an_instance_role(self, db_session, monkeypatch):
        """`:656`'s `session.add`. Delete it and no role exists."""
        instance = _seed_instance('peer.example', software='lemmy')
        admin = make_user(instance, 'adminuser')
        self._lemmy(monkeypatch, _site_payload(admin.ap_profile_id), actor=admin)

        monitor_healthy_instances()

        db.session.expire_all()
        roles = db.session.query(InstanceRole).filter_by(instance_id=instance.id).all()
        assert {(r.user_id, r.role) for r in roles} == {(admin.id, 'admin')}

    def test_a_non_http_actor_id_is_skipped(self, db_session, monkeypatch):
        """`:647`'s false arm. An acct: URI creates no role and is never resolved.

        The oracle is that `find_actor_or_create` was NOT called -- asserting
        only that no role exists would hold if the resolver returned None too.
        """
        instance = _seed_instance('peer.example', software='lemmy')
        resolver = _Recorder(result=None)
        _quiet_http_half(monkeypatch)
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request',
            _Recorder(result=_response(200, _site_payload('acct:admin@peer.example'))))
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.find_actor_or_create', resolver)

        monitor_healthy_instances()

        assert resolver.calls == []

    def test_an_unresolvable_actor_creates_no_role(self, db_session, monkeypatch):
        """`:650`'s `user and ...` conjunct, false because the resolver returned None.

        Dropping just that conjunct does not make role count alone fail: with
        no `user`, `instance.user_is_admin(user.id)` raises `AttributeError`
        on `None`, `:690` catches it, and the block still ends with zero
        roles -- a crash swallowed into a skip looks the same as a clean one
        by that measure. `:692`'s failure increment is what tells them apart:
        the guard intact costs only the HTTP half's two; the guard missing
        costs a third from the caught crash. So the oracle checks BOTH that
        no role exists and that no failure was recorded by this block.
        """
        instance = _seed_instance('peer.example', software='lemmy')
        self._lemmy(
            monkeypatch, _site_payload('https://peer.example/users/ghost'), actor=None)
        before = instance.failures

        monitor_healthy_instances()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='peer.example').first()
        assert db.session.query(InstanceRole).filter_by(
            instance_id=instance.id).count() == 0
        assert reloaded.failures == before + 2

    def test_an_existing_admin_is_not_added_twice(self, db_session, monkeypatch):
        """`:650`'s second conjunct. Without it the composite PK collides.

        The role is seeded first, so `user_is_admin` is already true. A second
        `session.add` for the same `(instance_id, user_id)` would raise, which
        `:690`'s `except` would swallow into `:692`'s failure increment -- so
        the oracle checks BOTH that one role exists and that no failure was
        recorded by this block.
        """
        instance = _seed_instance('peer.example', software='lemmy')
        admin = make_user(instance, 'adminuser')
        db.session.add(InstanceRole(
            instance_id=instance.id, user_id=admin.id, role='admin'))
        db.session.commit()
        before = instance.failures
        self._lemmy(monkeypatch, _site_payload(admin.ap_profile_id), actor=admin)

        monitor_healthy_instances()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='peer.example').first()
        assert db.session.query(InstanceRole).filter_by(
            instance_id=instance.id).count() == 1
        assert reloaded.failures == before + 2

    def test_a_non_200_site_response_creates_no_role(self, db_session, monkeypatch):
        """`:641`'s false arm.

        Negating that guard does not make role count alone fail: with no
        payload attached to the 503, `response.json()` raises on the empty
        body, `:690` catches it, and the block still ends with zero roles --
        a crash swallowed into a skip looks the same as a clean one by that
        measure. `:692`'s failure increment is what tells them apart: the
        guard intact costs only the HTTP half's two; the guard missing costs
        a third from the caught crash. So the oracle checks BOTH that no
        role exists and that no failure was recorded by this block.
        """
        instance = _seed_instance('peer.example', software='lemmy')
        admin = make_user(instance, 'adminuser')
        _quiet_http_half(monkeypatch)
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request',
            _Recorder(result=_response(503)))
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.find_actor_or_create',
            lambda profile_id, **kwargs: admin)
        before = instance.failures

        monitor_healthy_instances()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='peer.example').first()
        assert db.session.query(InstanceRole).filter_by(
            instance_id=instance.id).count() == 0
        assert reloaded.failures == before + 2

    def test_an_admin_no_longer_listed_loses_the_role(self, db_session, monkeypatch):
        """`:669`'s `.delete()`. The departing admin's role goes."""
        instance = _seed_instance('peer.example', software='lemmy')
        staying = make_user(instance, 'staying')
        leaving = make_user(instance, 'leaving')
        for user in (staying, leaving):
            db.session.add(InstanceRole(
                instance_id=instance.id, user_id=user.id, role='admin'))
        db.session.commit()
        self._lemmy(monkeypatch, _site_payload(staying.ap_profile_id), actor=staying)

        monitor_healthy_instances()

        db.session.expire_all()
        remaining = {
            r.user_id for r in
            db.session.query(InstanceRole).filter_by(instance_id=instance.id).all()}
        assert remaining == {staying.id}

    def test_a_still_listed_admin_keeps_the_role(self, db_session, monkeypatch):
        """`:662`'s false arm -- the profile IS in the listed set, so no delete.

        This is the companion the removal test needs: without it, a mutation
        that deletes unconditionally would still satisfy the test above.
        """
        instance = _seed_instance('peer.example', software='lemmy')
        staying = make_user(instance, 'staying')
        db.session.add(InstanceRole(
            instance_id=instance.id, user_id=staying.id, role='admin'))
        db.session.commit()
        self._lemmy(monkeypatch, _site_payload(staying.ap_profile_id), actor=staying)

        monitor_healthy_instances()

        db.session.expire_all()
        assert db.session.query(InstanceRole).filter_by(
            instance_id=instance.id, user_id=staying.id).count() == 1

    def test_an_admin_arrives_while_another_leaves_in_the_same_sweep(self, db_session, monkeypatch):
        """`:656`'s `session.add` and `:669`'s `.delete()`, both firing for
        the same instance in the same pass.

        Every test above isolates one half: a genuinely new admin with no
        prior role, or an existing role with no new admin arriving. Here a
        departing admin already has a committed role, and the payload lists
        a DIFFERENT admin who has never had one, so both `:656` and the
        removal loop run in the same sweep. The oracle is that the final
        role set is exactly the arriving admin's -- neither operation may
        leave a trace of the other's target behind.
        """
        instance = _seed_instance('peer.example', software='lemmy')
        leaving = make_user(instance, 'leaving')
        arriving = make_user(instance, 'arriving')
        db.session.add(InstanceRole(
            instance_id=instance.id, user_id=leaving.id, role='admin'))
        db.session.commit()
        self._lemmy(monkeypatch, _site_payload(arriving.ap_profile_id), actor=arriving)

        monitor_healthy_instances()

        db.session.expire_all()
        remaining = {
            r.user_id for r in
            db.session.query(InstanceRole).filter_by(instance_id=instance.id).all()}
        assert remaining == {arriving.id}


class TestLemmyCustomEmoji:
    """`:672-688` -- refresh the instance's custom emoji.

    `:672` skips the whole block for a banned domain; `:673` walks
    `custom_emojis`; `:678` forks on whether a row with that token already
    exists for this instance.

    `Emoji` has NO unique constraint on `(instance_id, token)`
    (`app/models.py:4378-4384`), so `:676-677`'s lookup is the only thing
    preventing duplicates.
    """

    def _lemmy(self, monkeypatch, payload, actor=None):
        _quiet_http_half(monkeypatch)
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request',
            _Recorder(result=_response(200, payload)))
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.find_actor_or_create',
            lambda profile_id, **kwargs: actor)

    def test_a_new_emoji_is_created(self, db_session, monkeypatch):
        """`:683-687`'s create arm."""
        instance = _seed_instance('peer.example', software='lemmy')
        self._lemmy(monkeypatch, _site_payload(emojis=[
            _emoji('blobcat', url='https://peer.example/blob.png',
                   category='blobs', keywords=('happy', 'cat'))]))

        monitor_healthy_instances()

        db.session.expire_all()
        rows = db.session.query(Emoji).filter_by(instance_id=instance.id).all()
        assert len(rows) == 1
        assert rows[0].token == ':blobcat:'
        assert rows[0].url == 'https://peer.example/blob.png'
        assert rows[0].category == 'blobs'
        assert rows[0].aliases == 'happy cat'

    def test_an_existing_emoji_is_updated_not_duplicated(self, db_session, monkeypatch):
        """`:678`'s true arm and `:679-681`. The token has no unique constraint,
        so a broken lookup would duplicate rather than raise.
        """
        instance = _seed_instance('peer.example', software='lemmy')
        db.session.add(Emoji(
            instance_id=instance.id, token=':blobcat:',
            url='https://peer.example/old.png', category='old', aliases='stale'))
        db.session.commit()
        self._lemmy(monkeypatch, _site_payload(emojis=[
            _emoji('blobcat', url='https://peer.example/new.png',
                   category='blobs', keywords=('happy',))]))

        monitor_healthy_instances()

        db.session.expire_all()
        rows = db.session.query(Emoji).filter_by(instance_id=instance.id).all()
        assert len(rows) == 1
        assert rows[0].url == 'https://peer.example/new.png'
        assert rows[0].category == 'blobs'
        assert rows[0].aliases == 'happy'

    def test_a_banned_domain_skips_the_emoji_refresh(self, db_session, monkeypatch):
        """`:672`'s false arm. Admin roles are still reconciled above it --
        only the emoji block is skipped -- so the oracle is the absence of an
        Emoji row, not the absence of all work.

        `instance_banned` is also called at `:547`, loop level, before this
        instance's identity blocks run at all -- and both call sites see the
        SAME `instance.domain` value for a single seeded instance (confirmed
        empirically: a blanket `True` patch left `get_request` uncalled, i.e.
        `:547` had already `continue`d). A domain-keyed patch therefore cannot
        tell the two call sites apart either -- both would need to agree on
        one verdict for 'peer.example'. What distinguishes them is ORDER:
        `:547` is always the first call for a given instance, `:672` the
        second, so a call-counter lets `:547` see `False` (falls through to
        run the admin-role phase and reach the emoji block) while `:672`
        sees `True` (skips only the emoji refresh). Because the admin phase
        still runs first, this also confirms the test isn't vacuously passing
        on a sweep that never started -- the mock is exercised at least once
        before the block under test is reached.
        """
        instance = _seed_instance('peer.example', software='lemmy')
        self._lemmy(monkeypatch, _site_payload(emojis=[_emoji('blobcat')]))
        calls = {'count': 0}

        def _banned_after_loop_gate(domain):
            calls['count'] += 1
            return calls['count'] > 1

        monkeypatch.setattr(
            'app.shared.tasks.maintenance.instance_banned', _banned_after_loop_gate)

        monitor_healthy_instances()

        assert calls['count'] >= 2
        db.session.expire_all()
        assert db.session.query(Emoji).filter_by(instance_id=instance.id).count() == 0

    def test_a_non_200_does_not_invalidate_the_emoji_cache(self, db_session, monkeypatch):
        """DC2: `cache.delete_memoized` sits outside `:641`'s guard.

        A 404 from one Lemmy instance discards the whole site's emoji
        replacements. The oracle records the call rather than observing the
        cache, because `CACHE_TYPE` is `NullCache` under test
        (`tests/conftest.py:68`) and an invalidation there is unobservable.
        """
        recorder = _Recorder()
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.cache.delete_memoized', recorder)
        _quiet_http_half(monkeypatch)
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request',
            _Recorder(result=_response(404)))
        _seed_instance('peer.example', software='lemmy')
        db.session.commit()

        monitor_healthy_instances()

        assert recorder.calls == []

    def test_a_200_does_invalidate_the_emoji_cache(self, db_session, monkeypatch):
        """The other side of DC2. Without this, deleting the call entirely
        would satisfy the test above.
        """
        recorder = _Recorder()
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.cache.delete_memoized', recorder)
        self._lemmy(monkeypatch, _site_payload(emojis=[_emoji('blobcat')]))
        _seed_instance('peer.example', software='lemmy')
        db.session.commit()

        monitor_healthy_instances()

        assert len(recorder.calls) == 1


class TestMbinAdminRoles:
    """`:703-739` -- MBIN admin reconciliation.

    `:703` forks on `software == 'mbin'`; `:707` on the response; `:711` walks
    `instance_data['items']`; `:712` reads the username defensively; `:713` is
    a COMPOUND -- `username and (isAdmin or isGlobalModerator)` -- which
    coverage sees as one arc pair, so each conjunct needs its own test;
    `:714` looks the user up locally and `:715` skips one that is not known;
    `:717` skips one already an admin; `:726-732` removes stale roles.

    Unlike the Lemmy block this never creates a User: the API response does
    not carry enough to build one.
    """

    def _mbin(self, monkeypatch, payload):
        _quiet_http_half(monkeypatch)
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request',
            _Recorder(result=_response(200, payload)))

    def test_a_listed_admin_gets_an_instance_role(self, db_session, monkeypatch):
        """`:723`'s `session.add`."""
        instance = _seed_instance('peer.example', software='mbin')
        admin = make_user(instance, 'adminuser')
        self._mbin(monkeypatch, _mbin_payload(
            {'username': 'adminuser', 'isAdmin': True}))

        monitor_healthy_instances()

        db.session.expire_all()
        roles = db.session.query(InstanceRole).filter_by(instance_id=instance.id).all()
        assert {r.user_id for r in roles} == {admin.id}

    def test_a_global_moderator_also_gets_the_role(self, db_session, monkeypatch):
        """`:713`'s second disjunct, alone. `isAdmin` is absent."""
        instance = _seed_instance('peer.example', software='mbin')
        admin = make_user(instance, 'moduser')
        self._mbin(monkeypatch, _mbin_payload(
            {'username': 'moduser', 'isGlobalModerator': True}))

        monitor_healthy_instances()

        db.session.expire_all()
        assert db.session.query(InstanceRole).filter_by(
            instance_id=instance.id, user_id=admin.id).count() == 1

    def test_a_plain_user_gets_no_role(self, db_session, monkeypatch):
        """`:713`'s false arm -- neither flag set."""
        instance = _seed_instance('peer.example', software='mbin')
        make_user(instance, 'plainuser')
        self._mbin(monkeypatch, _mbin_payload({'username': 'plainuser'}))

        monitor_healthy_instances()

        db.session.expire_all()
        assert db.session.query(InstanceRole).filter_by(
            instance_id=instance.id).count() == 0

    def test_an_item_without_a_username_is_skipped(self, db_session, monkeypatch):
        """`:712`'s else -- the key is absent, so `username` is None and
        `:713`'s first conjunct is false. Without `:712`'s guard this raises
        `KeyError`, caught by `:733`.
        """
        instance = _seed_instance('peer.example', software='mbin')
        before = instance.failures
        self._mbin(monkeypatch, _mbin_payload({'isAdmin': True}))

        monitor_healthy_instances()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='peer.example').first()
        assert db.session.query(InstanceRole).filter_by(
            instance_id=instance.id).count() == 0
        assert reloaded.failures == before + 2

    def test_an_unknown_username_gets_no_role(self, db_session, monkeypatch):
        """`:715`'s false arm -- listed as admin but not in our database.

        Dropping that guard does not make role count alone fail: with `user`
        None, `admin_user_ids.append(user.id)` raises `AttributeError`,
        `:733` catches it, and the block still ends with zero roles -- a
        crash swallowed into a skip looks the same as a clean one by that
        measure. `:735`'s failure increment is what tells them apart: the
        guard intact costs only the HTTP half's two; the guard missing costs
        a third from the caught crash. So the oracle checks BOTH that no
        role exists and that no failure was recorded by this block.
        """
        instance = _seed_instance('peer.example', software='mbin')
        self._mbin(monkeypatch, _mbin_payload(
            {'username': 'stranger', 'isAdmin': True}))
        before = instance.failures

        monitor_healthy_instances()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='peer.example').first()
        assert db.session.query(InstanceRole).filter_by(
            instance_id=instance.id).count() == 0
        assert reloaded.failures == before + 2

    def test_an_admin_no_longer_listed_loses_the_role(self, db_session, monkeypatch):
        """`:732`'s `.delete()`."""
        instance = _seed_instance('peer.example', software='mbin')
        staying = make_user(instance, 'staying')
        leaving = make_user(instance, 'leaving')
        for user in (staying, leaving):
            db.session.add(InstanceRole(
                instance_id=instance.id, user_id=user.id, role='admin'))
        db.session.commit()
        self._mbin(monkeypatch, _mbin_payload(
            {'username': 'staying', 'isAdmin': True}))

        monitor_healthy_instances()

        db.session.expire_all()
        remaining = {
            r.user_id for r in
            db.session.query(InstanceRole).filter_by(instance_id=instance.id).all()}
        assert remaining == {staying.id}

    def test_a_still_listed_admin_keeps_the_role(self, db_session, monkeypatch):
        """`:727`'s false arm -- the user IS in `admin_user_ids`, so no delete.

        This is the companion the removal test needs: without it, a mutation
        that deletes unconditionally (dropping the `not in` negation at
        `:727`) would still satisfy the test above.
        """
        instance = _seed_instance('peer.example', software='mbin')
        staying = make_user(instance, 'staying')
        db.session.add(InstanceRole(
            instance_id=instance.id, user_id=staying.id, role='admin'))
        db.session.commit()
        self._mbin(monkeypatch, _mbin_payload(
            {'username': 'staying', 'isAdmin': True}))

        monitor_healthy_instances()

        db.session.expire_all()
        assert db.session.query(InstanceRole).filter_by(
            instance_id=instance.id, user_id=staying.id).count() == 1

    def test_a_non_200_admins_response_creates_no_role(self, db_session, monkeypatch):
        """`:707`'s false arm.

        Negating that guard does not make role count alone fail: entering
        the block with a 503 and no payload attached, `response.json()`
        raises on the empty body, `:733` catches it, and the block still
        ends with zero roles -- a crash swallowed into a skip looks the same
        as a clean one by that measure. `:735`'s failure increment is what
        tells them apart: the guard intact costs only the HTTP half's two;
        the guard missing costs a third from the caught crash. So the oracle
        checks BOTH that no role exists and that no failure was recorded by
        this block.
        """
        instance = _seed_instance('peer.example', software='mbin')
        make_user(instance, 'adminuser')
        _quiet_http_half(monkeypatch)
        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_request',
            _Recorder(result=_response(503)))
        before = instance.failures

        monitor_healthy_instances()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='peer.example').first()
        assert db.session.query(InstanceRole).filter_by(
            instance_id=instance.id).count() == 0
        assert reloaded.failures == before + 2


class TestMbinAdminRoleFailures:
    """`:733-735` -- the MBIN block's own `except`, and `:737`'s FALSE arm.

    Every failure test in `TestMbinAdminRoles` above reaches `:733` through a
    crash CAUGHT INSIDE the `try` body -- a `KeyError` from a missing
    `'username'`, an `AttributeError` from a `None` user -- so `response`
    (200, with a malformed or absent payload) is never `None` by the time
    `:733` runs, and `:737`'s `if response is not None:` always takes its
    TRUE arm there. `:733-735` themselves -- `session.rollback()` and
    `instance.failures += 1` -- had never executed, and `:737`'s FALSE arm
    had never executed either.

    This test makes `get_request` itself raise, the twin of
    `TestIdentityPhaseFailures.test_a_raising_request_does_not_end_the_whole_sweep`
    but for the MBIN block. `response` is seeded `None` at `:704` and the
    raise happens during the assignment at `:706`, so `response` is still
    `None` when `:733`'s `except` runs and when `:737` evaluates it --
    exercising the FALSE arm this file was still missing.
    """

    def test_a_raising_request_is_caught_as_a_failure(self, db_session, monkeypatch):
        """Deleting `:735`'s `instance.failures += 1` is caught here: without
        it `failures` would land on `before + 2` (the HTTP half's two,
        `_quiet_http_half`'s 404) instead of `before + 3`. Deleting the whole
        `:733-735` except block instead lets the `httpx.HTTPError` escape
        uncaught to `:742`'s task-level handler, which rolls back and
        re-raises -- `monitor_healthy_instances()` would then raise instead
        of returning, which this test's bare call (no `pytest.raises`) would
        also catch.
        """
        def _raise(*args, **kwargs):
            raise httpx.HTTPError('transport down')

        _quiet_http_half(monkeypatch)
        monkeypatch.setattr('app.shared.tasks.maintenance.get_request', _raise)
        instance = _seed_instance('peer.example', software='mbin')
        db.session.commit()
        before = instance.failures

        monitor_healthy_instances()

        db.session.expire_all()
        reloaded = db.session.query(Instance).filter_by(domain='peer.example').first()
        assert reloaded.failures == before + 3


class _GhostReplySession:
    """Wraps a real task session so `.query(PostReply).get(ghost_id)` returns
    `None` for one chosen id, standing in for that row having been deleted by
    ANOTHER session between the raw `SELECT` at
    `delete_old_soft_deleted_content:261-266` and the ORM lookup at `:269`.
    Every other call -- `.query(Post)`, `.query(PostReply)` for any other id,
    `.execute`, `.delete`, `.commit`, `.rollback`, `.close` -- forwards to the
    real session untouched.
    """

    def __init__(self, real_session, ghost_id):
        self._real = real_session
        self._ghost_id = ghost_id

    def __getattr__(self, name):
        return getattr(self._real, name)

    def query(self, *entities, **kwargs):
        real_query = self._real.query(*entities, **kwargs)
        if entities and entities[0] is PostReply:
            return _GhostQuery(real_query, self._ghost_id)
        return real_query


class _GhostQuery:
    """Forwards everything to the wrapped `Query` except `.get(ghost_id)`."""

    def __init__(self, real_query, ghost_id):
        self._real = real_query
        self._ghost_id = ghost_id

    def __getattr__(self, name):
        return getattr(self._real, name)

    def get(self, ident):
        if ident == self._ghost_id:
            return None
        return self._real.get(ident)


class TestDeleteOldSoftDeletedContentReplyRace:
    """`delete_old_soft_deleted_content:270`'s missing branch -- `if
    post_reply:` taking its FALSE arm.

    This function is outside the scope this file's docstring states --
    sub-project 33 is `monitor_healthy_instances`'s identity phases and the
    task-level `except`. Task 9's measurement command
    (`--cov=app.shared.tasks.maintenance` across all five maintenance test
    files) found this branch missing for the WHOLE module, and Task 9's
    binding constraints permit writing only to this file, so it is closed
    here rather than left silently absorbed into the raised floor.

    `:261-266` selects `post_reply` ids matching the retention criteria with
    one raw SQL `SELECT`, a snapshot at that instant. `:269` then re-fetches
    each id through the ORM, one at a time, in a separate round trip. Between
    those two steps another session can delete the row -- a concurrent
    request, another worker -- and `:270`'s `if post_reply:` guard exists for
    exactly that case, per its own comment ("Check if still exists"). A
    single-process test cannot make a genuine second connection race the
    first, so `_GhostReplySession` stands in for the race by wrapping the
    task's own session and making `.query(PostReply).get(ghost_id)` return
    `None` for the one id under test. `get_task_session` is imported into
    `app.shared.tasks.maintenance`'s namespace (`:19`), the same kind of seam
    this file already patches for `get_request` and `find_actor_or_create`.

    Deleting or inverting `:270`'s guard makes `post_reply.delete_dependencies()`
    run on `None`, raising `AttributeError` that escapes `delete_old_soft_deleted_content`'s
    own `try` to `:278`'s `raise` -- turning a row already gone into a crash
    for the whole task instead of a silent skip. The oracle is therefore
    BOTH that the task returns normally (a bare call, no `pytest.raises`)
    AND that the row -- untouched by this pass, since the ghost lookup never
    reaches `delete_dependencies` or `session.delete` -- still exists
    afterward; the guard's absence would fail on the first, a mutant that
    kept the guard but deleted anyway would fail on the second.
    """

    def test_a_reply_gone_before_the_get_is_left_alone(self, db_session, monkeypatch):
        instance = make_instance('peer.example')
        user = make_user(instance, 'reader', local=True)
        community = make_community()
        post = make_post(community, user, 'https://peer.example/p/1')
        reply = make_post_reply(post, user)
        reply.deleted = True
        reply.posted_at = utcnow() - timedelta(days=8)
        db.session.commit()
        reply_id = reply.id

        def _ghost_get_task_session():
            return _GhostReplySession(_real_get_task_session(), reply_id)

        monkeypatch.setattr(
            'app.shared.tasks.maintenance.get_task_session', _ghost_get_task_session)

        delete_old_soft_deleted_content()

        db.session.expire_all()
        assert db.session.query(PostReply).filter_by(id=reply_id).first() is not None
