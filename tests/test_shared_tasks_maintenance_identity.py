"""Group C's identity phases in `app/shared/tasks/maintenance.py`.

Sub-projects 29, 30, 31 and 32 closed the rest of this module. This file
covers what was left: the two blocks of `monitor_healthy_instances` gated on
`instance.software`, plus the task's own outer handler.

  Lemmy/PieFed admin roles and custom emoji  `:637-696`
  MBIN admin roles                           `:703-739`
  the task's outer handler                   `:742-744`

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
"""

from datetime import timedelta

import httpx
import pytest

from app import db
from app.models import Emoji, Instance, InstanceRole, User, utcnow
from app.shared.tasks.maintenance import monitor_healthy_instances
from tests.factories import make_instance, make_user


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
