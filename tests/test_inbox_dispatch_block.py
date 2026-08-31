"""Sub-project 5c, Task 11 -- the Block arm (app/activitypub/routes.py). The
last coverage task in this sub-project, and the one arm with no authorised
fix: everything registered below is pinned as observed behaviour, not
corrected.

**Stale citations.** The task brief cites `routes.py:1590-1671`, with
sub-citations throughout that range. Re-read against current source, the arm
now runs `:1595-1675` -- every one of the brief's line numbers is stale by a
few lines, continuing the pattern every prior task in this sub-project
reported. Corrected citations, used throughout this file instead of the
brief's:

  - arm entry / docstring:                       :1595-1611  (brief: 1590-)
  - `cc` emptied when not announced+storing:      :1612-1613  (brief: 1607-1608)
  - `blocked_ap_id = object.lower()` (the dict
    probe's crash site):                         :1617       (brief: 1612)
  - blocked user unknown -> IGNORED:              :1619-1621  (brief: 1614-1615)
  - already_banned computed:                      :1622-1623  (brief: 1617-1618)
  - `target.count('/') < 4` site/community split: :1628       (brief: 1623)
  - site-ban non-admin refusal:                   :1629-1631  (brief: 1624-1626)
  - site-ban ordinary case (banned/ban_until/
    remove_data/SUCCESS):                         :1642-1652  (brief: 1640-1642
                                                    for ban_until specifically)
  - community-ban block:                          :1653-1668  (brief: 1649-1663)
  - community permission guard:                   :1660       (brief: 1655)
  - Mastodon no-target path:                       :1669-1673  (brief: 1664-1668)

## Outcome table, derived from current source (not copied from the brief)

Two reads happen before ANY branch on 'target' exists: `blocked_ap_id =
core_activity['object'].lower()` (:1617) and the `session.query(User)`
lookup it feeds (:1618). Both run for every Block activity, site-ban,
community-ban, or Mastodon-shaped alike -- so the dict-object crash (see
PROBE below) is not specific to any one of the three sub-paths; it happens
before the code has even looked at 'target'.

| branch (lines)                                    | selects it                                                              | writes                                                                 | delegates to                                              | logs |
|-----------------------------------------------------|-----------------------------------------------------------------------------|-----------------------------------------------------------------------------|-----------------------------------------------------------------|------|
| blocked user unknown (1619-1621)                     | no `User` row matches `blocked_ap_id`                                       | nothing                                                                      | nothing                                                          | APLOG_USERBAN/APLOG_IGNORED 'Does not exist here' |
| site-ban, non-admin (1629-1631)                      | `target.count('/') < 4`; `not blocker.is_instance_admin()`                  | nothing                                                                      | nothing                                                          | APLOG_USERBAN/APLOG_FAILURE 'Does not have permission' |
| site-ban, blocked is local (1632-1636)               | site ban; blocker is admin; `blocked.is_local()`                            | whatever `ban_user` itself does (delegate, mocked in this file)             | `ban_user(blocker, blocked, None, core_activity)` -- unconditionally, regardless of `already_banned` | APLOG_USERBAN/APLOG_MONITOR 'Remote Admin in banning one of our users from their site' |
| site-ban, blocked on a third instance (1637-1640)    | site ban; blocker is admin; blocked not local; `blocked.instance_id != blocker.instance_id` | nothing                                                                      | nothing                                                          | APLOG_USERBAN/APLOG_MONITOR 'Remote Admin is banning a user of a different instance from their site' |
| site-ban, ordinary case (1642-1652)                  | site ban; blocker is admin; blocked not local; same instance as blocker     | `blocked.banned = True` and `blocked.ban_until = <peer string>` -- ONLY when `not already_banned` (see ban_until PROBE below for what that write actually does); `site_ban_remove_data` runs regardless of `already_banned` | `site_ban_remove_data(blocker.id, blocked)` only when `removeData` | APLOG_USERBAN/APLOG_SUCCESS (unconditional) |
| community-ban, unfound community (1656-1659)         | not site ban; `community` (from Announce, or resolved from `target`) is falsy | nothing                                                                    | nothing                                                          | APLOG_USERBAN/APLOG_IGNORED 'Blocked or unfound community' |
| community-ban, no permission (1660-1662)             | community found; `not community.is_moderator(blocker) and not community.is_instance_admin(blocker)` | nothing                                                       | nothing                                                          | APLOG_USERBAN/APLOG_FAILURE 'Does not have permission' |
| community-ban, success (1664-1668)                   | community found; moderator OR instance admin                                | none directly -- delegates do the writing                                   | `community_ban_remove_data(blocker.id, community.id, blocked)` only when `removeData`; `ban_user(blocker, blocked, community, core_activity)` only when `not already_banned` | APLOG_USERBAN/APLOG_SUCCESS (unconditional) |
| Mastodon, no target, new block (1670-1673)           | no 'target' key; `object` is a str; `not blocker.has_blocked_user(blocked.id)` | `UserBlock(blocker_id, blocked_id)`, commit                               | nothing                                                          | **NOTHING.** No `log_incoming_ap` call exists anywhere in this branch. |
| Mastodon, no target, already blocked (1670-1673)     | no 'target' key; `object` is a str; already blocked                          | nothing                                                                      | nothing                                                          | **NOTHING**, same as above. |

## Four findings pinned by this file, not fixed

**1. The Mastodon no-target path logs nothing, on either outcome.** Neither
the create-a-UserBlock branch nor the already-blocked no-op branch calls
`log_incoming_ap` anywhere -- confirmed by reading :1669-1673 in full: no
such call exists in the branch's source, not merely "not observed to fire".
`test_mastodon_no_target_creates_a_block_and_logs_nothing` and
`test_mastodon_no_target_skips_a_duplicate_and_logs_nothing` below both run
with `LOG_ACTIVITYPUB_TO_DB` explicitly True and assert
`ActivityPubLog.query.count() == 0`, so the zero is not a vacuous artifact
of logging being off (Task 6's shipped mistake, called out in this task's
brief).

The brief also asks to establish what happens "including when `object` is
not a string." Read literally, that scenario cannot be reached independent
of finding 2 below: `core_activity['object']` is read via `.lower()` at
:1617 UNCONDITIONALLY, for every Block activity regardless of whether
'target' is present -- so a non-string `object` never survives to reach the
Mastodon branch's own `isinstance(core_activity['object'], str)` guard at
:1670. It crashes three lines into the arm, before 'target' is even
inspected. The "no target path logs nothing on any outcome" claim is
therefore true in the strongest available sense: not just on its own two
reachable outcomes (create / skip), but also on the crash outcome finding 2
demonstrates, which happens to every no-target Block whose object is not a
string, before this branch's own code ever runs.

**2. `core_activity['object'].lower()` (:1617) reads before any `isinstance`
check.** `test_a_dict_shaped_object_crashes_before_any_isinstance_check`
below feeds a dict-shaped `object` (no 'target', matching the Mastodon
shape the brief asks about) and observes `AttributeError: 'dict' object has
no attribute 'lower'`, raised at :1617 -- three lines into the arm, before
'target' is inspected, before `blocked` is looked up, and long before the
Mastodon path's own `isinstance(core_activity['object'], str)` guard at
:1670 is ever reached. No `log_incoming_ap` call fires; the exception
propagates out of `dispatch()` uncaught by anything inside
`process_inbox_request`'s own try block (only the bare
`except Exception: session.rollback(); raise` at :1889 sees it, and
re-raises).

**3. `blocked.ban_until = core_activity['expires']` (or `['endTime']`),
:1645/:1647 -- established, not assumed.** The brief poses this as an open
question about whether SQLAlchemy coerces a raw peer-supplied string into a
`DateTime` column. Reading `app/models.py`'s `User` class in full (the
entire class body, :964-1656) surfaces a stronger answer than "no parsing
happens": **`User` has NO `ban_until` column or attribute at all.** The
real, mapped `DateTime` column on `User` is `banned_until` (:975, "null ==
permanent ban") -- one word different from what routes.py:1645/1647
actually write to. `ban_until` is a real, mapped column on a DIFFERENT
model, `CommunityBan` (:3545) -- close enough in name and shape that the
two are easy to conflate while reading the Block arm, which is presumably
how this happened.

`test_ordinary_site_ban_the_ban_until_probe` below establishes what this
means operationally:
  - `session.commit()` (:1648) succeeds without error, no matter what
    string `expires` carries (a garbage, unparseable string is used
    deliberately, to prove nothing downstream ever tries to parse or
    validate it either).
  - `sa_inspect(User).columns.keys()` does not contain `'ban_until'` --
    proof from the mapper itself, not from any one instance's runtime
    state, that no such column is ever going to exist to receive this
    write, in this process or any other.
  - Plain instance-attribute assignment on a SQLAlchemy declarative object
    for a name the mapper does not recognise is ordinary Python attribute
    assignment (`__dict__`), not something the ORM's flush machinery
    inspects at all -- so the write neither raises nor persists. It exists,
    transiently, only on the one Python object the dispatcher's own
    independent task session built for this call; that session is closed
    (`finally: session.close()`, :1889) before `dispatch()` returns, and
    the object (and the attribute on it) goes with it.
  - The REAL `banned_until` column, re-read fresh from the database after
    dispatch, is untouched -- still `None`, exactly as seeded, because
    nothing on this code path ever writes to it.

Net effect: a federated site ban's expiry is silently discarded, every
time, regardless of what a peer sends for `expires`/`endTime` -- not merely
unparsed, but never written to any column that exists. `blocked.banned`
(the real, correctly-named boolean column) IS set True correctly; only the
expiry half of a temporary ban is lost. Registered here; not fixed, per
this task's contract -- the fix belongs to whoever triages this defect
next, and is a one-word rename (`ban_until` -> `banned_until`) plus real
parsing of the peer string, which :1645/:1647 currently make no attempt at
either.

**4. Anything else found while deriving the table above.**
  - The site-ban "blocked is local" branch (:1632-1636) calls `ban_user`
    UNCONDITIONALLY -- it does not consult `already_banned` at all, unlike
    the "ordinary" remote-blocked branch a few lines below, which skips the
    banned/ban_until write specifically `if not already_banned`. A remote
    admin re-sending an already-actioned local-user ban therefore always
    re-invokes the local `ban_user` delegate, even though the arm computed
    `already_banned` specifically to avoid exactly that kind of redundant
    re-ban ("we don't want remote temp bans to over-ride our permanent
    bans", the arm's own docstring, :1601-ish). Pinned by
    `test_site_ban_of_a_local_user_reinvokes_ban_user_even_if_already_banned`
    below; not fixed.
  - `community_ban_remove_data` (:1664-1665) and `ban_user` for the
    community-ban success path (:1666-1667) are two SEPARATE `if`
    statements, not one combined condition -- `remove_data` runs regardless
    of `already_banned`, exactly mirroring the site-ban path's structure.
    Covered by `test_community_ban_already_banned_skips_ban_user_but_still_succeeds`.
  - When the activity arrived via Announce, `community` is already resolved
    (from the OUTER Announce actor, at the preamble's own
    `community_only=True` lookup, routes.py:862) before the Block arm ever
    runs, and the community-ban branch's own resolution
    (`community if community else find_actor_or_create_cached(target, ...)`,
    :1654) short-circuits entirely -- `target` is never even looked up in
    that case, so a `target` naming a nonexistent community has no effect
    when the activity is Announced. Covered by
    `test_community_ban_when_announced_short_circuits_target_resolution`.
  - `User.is_instance_admin()` (app/models.py:1268) takes NO arguments and
    checks `InstanceRole` for the CALLING user's own `instance_id` --
    used by the site-ban guard (`blocker.is_instance_admin()`). This is a
    different method from `Community.is_instance_admin(user)`
    (app/models.py:752), used by the community-ban guard
    (`community.is_instance_admin(blocker)`), which checks `InstanceRole`
    for the COMMUNITY's home instance against the given user. The two are
    easy to conflate by name; this file exercises both, on their own
    correct actors.
"""

import json

import pytest
from sqlalchemy import inspect as sa_inspect

from app import db
from app.models import ActivityPubLog, InstanceRole, User, UserBlock, utcnow
from tests.factories import inbox_activity, make_community, make_community_member, make_instance, \
    make_user, make_user_block, seed_community_owner
from tests.test_inbox_dispatch_lock_delete import record_moderation
from tests.test_inbox_dispatch_preamble import dispatch


# --- Pre-target checks: routes.py:1612-1623, reached by every Block shape ---


def test_cc_is_emptied_when_not_announced_and_storing_json_and_unknown_blocked_is_ignored(
        app, db_session, monkeypatch):
    """routes.py:1612-1613 and :1619-1621, covered together: the cheapest
    outcome that both reaches a `log_incoming_ap(..., saved_json, ...)` call
    and lets us inspect what got stored. This Block is not wrapped in an
    Announce (`announced` is False), so `core_activity` IS `request_json`
    IS `saved_json` -- the same dict, per routes.py:931-932 (`announced =
    False; core_activity = request_json`) and :853 (`saved_json =
    request_json if store_ap_json else None`). Mutating `core_activity['cc']`
    in place therefore mutates the exact object that gets `json.dumps`-ed
    into `ActivityPubLog.activity_json` (app/activitypub/util.py:4531-4532).

    A long `cc` list is seeded (as a real federated Block would carry --
    the arm's own docstring calls this "very long list of instances") and
    the blocked actor is a URL no seeded User matches, so the arm reaches
    :1619-1621's `if not blocked:` refusal -- the cheapest reachable log
    call, before 'target' is even inspected.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    blocker = make_user(instance, 'admin')
    blocker.ap_fetched_at = utcnow()
    db.session.commit()

    activity = inbox_activity(blocker, activity_type='Block',
                              object_uri='https://peer.example/users/nobody',
                              cc=['https://a.example/inbox', 'https://b.example/inbox',
                                  'https://c.example/inbox'])

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.result == 'ignored'
    assert log.exception_message == 'Does not exist here'

    stored = json.loads(log.activity_json)
    assert stored['cc'] == []


def test_a_dict_shaped_object_crashes_before_any_isinstance_check(app, db_session, monkeypatch):
    """routes.py:1617 -- `core_activity['object'].lower()` runs
    unconditionally, before 'target' is inspected and long before the
    Mastodon path's own `isinstance(core_activity['object'], str)` guard at
    :1670. A dict has no `.lower()` method.

    No 'target' key is set, matching the Mastodon shape the brief's Step 7
    describes -- but per this file's module docstring (finding 2), the
    crash happens identically whether or not 'target' is present, since
    :1617 runs before either code path is chosen.
    """
    instance = make_instance('peer.example')
    blocker = make_user(instance, 'admin')
    blocker.ap_fetched_at = utcnow()
    db.session.commit()

    activity = inbox_activity(blocker, activity_type='Block',
                              object={'id': 'https://peer.example/users/victim', 'type': 'Person'})

    with pytest.raises(AttributeError, match=r"'dict' object has no attribute 'lower'"):
        dispatch(activity)

    assert ActivityPubLog.query.count() == 0


# --- The site-ban path: routes.py:1628-1652 ---


def _seed_site_ban_blocker(instance_domain='peer.example', grant_admin=True):
    """A remote User who sends the Block, on their own instance. Admin
    status is granted via an InstanceRole on the BLOCKER's OWN instance_id
    -- User.is_instance_admin() (app/models.py:1268) takes no arguments and
    checks InstanceRole against `self.instance_id`, unlike
    Community.is_instance_admin(user) (app/models.py:752), which checks the
    COMMUNITY's instance_id instead. Returns (instance, blocker).
    """
    instance = make_instance(instance_domain)
    blocker = make_user(instance, 'admin')
    blocker.ap_fetched_at = utcnow()
    if grant_admin:
        db.session.add(InstanceRole(instance_id=instance.id, user_id=blocker.id, role='admin'))
    db.session.commit()
    return instance, blocker


def _stamp_local_ap_profile_id(app, user):
    """make_user(None, name, local=True) leaves `ap_profile_id` None, unlike
    a REAL local user: app/utils.py's finalize_user_setup (:2896-2900) sets
    `ap_profile_id` at registration time even for local accounts -- only
    `ap_id` (the column is_local() actually keys off) stays None to mark
    "local". The Block arm's blocked-user lookup (routes.py:1618) filters
    directly on the `ap_profile_id` COLUMN, so a local blocked user needs
    this real shape to be reachable by an incoming Block's `object` field at
    all -- left at the bare factory's None, no incoming Block could ever
    name this user, and routes.py:1632-1636's `blocked.is_local()` branch
    would be untestable (and, per the same column-filter reasoning,
    genuinely unreachable in production for a user this factory shape
    describes).
    """
    user.ap_profile_id = f"{app.config['SERVER_URL']}/u/{user.user_name}".lower()
    db.session.commit()
    return user.ap_profile_id


def test_site_ban_by_a_non_admin_is_refused(app, db_session, monkeypatch):
    """routes.py:1629-1631. `blocker.is_instance_admin()` is False (no
    InstanceRole row at all), so the site-ban guard refuses before `blocked`
    is even consulted for locality.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, blocker = _seed_site_ban_blocker(grant_admin=False)
    victim = make_user(instance, 'victim')
    db.session.commit()

    activity = inbox_activity(blocker, activity_type='Block',
                              object_uri=victim.ap_profile_id,
                              target=f'https://{instance.domain}')

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Does not have permission'


def test_site_ban_of_a_local_user_delegates_and_returns_without_remove_data(
        app, db_session, monkeypatch):
    """routes.py:1632-1636. `blocked.is_local()` is True (a local user, one
    a remote admin has no authority to ban directly) -- delegates entirely
    to `ban_user(blocker, blocked, None, core_activity)` and returns
    IMMEDIATELY, before `remove_data` is even read: `removeData=True` is
    set on the activity here specifically to prove `site_ban_remove_data`
    is never reached on this branch, since the `return` at :1636 precedes
    the `if remove_data:` check at :1650 entirely.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, blocker = _seed_site_ban_blocker()
    victim = make_user(None, 'victim', local=True)
    victim_ap_profile_id = _stamp_local_ap_profile_id(app, victim)

    calls = record_moderation(monkeypatch, 'ban_user', 'site_ban_remove_data')

    activity = inbox_activity(blocker, activity_type='Block',
                              object_uri=victim_ap_profile_id,
                              target=f'https://{instance.domain}', removeData=True)

    dispatch(activity)

    assert len(calls['ban_user']) == 1
    args, kwargs = calls['ban_user'][0]
    assert kwargs == {}
    blocker_arg, blocked_arg, community_arg, core_activity_arg = args
    assert sa_inspect(blocker_arg).identity[0] == blocker.id
    assert sa_inspect(blocked_arg).identity[0] == victim.id
    assert community_arg is None
    assert core_activity_arg is activity

    assert calls['site_ban_remove_data'] == []

    log = ActivityPubLog.query.one()
    assert log.result == 'Debug this'
    assert log.exception_message == 'Remote Admin in banning one of our users from their site'


def test_site_ban_of_a_local_user_reinvokes_ban_user_even_if_already_banned(
        app, db_session, monkeypatch):
    """routes.py:1632-1636 does not consult `already_banned` at all --
    unlike the "ordinary" remote-blocked branch a few lines below, which
    explicitly skips its write `if not already_banned`. Registered as
    finding 4 in this file's module docstring: a remote admin re-sending an
    already-actioned local-user ban always re-invokes `ban_user` here,
    where the ordinary branch would have skipped the equivalent work.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, blocker = _seed_site_ban_blocker()
    victim = make_user(None, 'victim', local=True)
    victim.banned = True
    victim_ap_profile_id = _stamp_local_ap_profile_id(app, victim)

    calls = record_moderation(monkeypatch, 'ban_user')

    activity = inbox_activity(blocker, activity_type='Block',
                              object_uri=victim_ap_profile_id,
                              target=f'https://{instance.domain}')

    dispatch(activity)

    assert len(calls['ban_user']) == 1

    log = ActivityPubLog.query.one()
    assert log.result == 'Debug this'


def test_site_ban_of_a_user_on_a_different_instance_is_only_monitored(app, db_session, monkeypatch):
    """routes.py:1637-1640. `blocked.is_local()` is False (a genuine remote
    user) but `blocked.instance_id != blocker.instance_id` -- the case the
    arm's own docstring calls out as one PieFed does not currently expect
    to receive directly (:1608-1610). No delegate runs; only a MONITOR log.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, blocker = _seed_site_ban_blocker()
    other_instance = make_instance('victim-instance.example')
    victim = make_user(other_instance, 'victim')
    db.session.commit()

    calls = record_moderation(monkeypatch, 'ban_user', 'site_ban_remove_data')

    activity = inbox_activity(blocker, activity_type='Block',
                              object_uri=victim.ap_profile_id,
                              target=f'https://{instance.domain}')

    dispatch(activity)

    assert calls['ban_user'] == []
    assert calls['site_ban_remove_data'] == []

    log = ActivityPubLog.query.one()
    assert log.result == 'Debug this'
    assert log.exception_message == 'Remote Admin is banning a user of a different instance from their site'


def _seed_ordinary_site_ban(instance_domain='peer.example', banned_baseline=False):
    """blocker and blocked share ONE instance and blocked is remote (not
    local) -- the only combination that reaches the "ordinary" write at
    routes.py:1642-1652, since :1632's is_local() check and :1637's
    cross-instance check both precede it and both `return` before it.
    """
    instance, blocker = _seed_site_ban_blocker(instance_domain)
    victim = make_user(instance, 'victim')
    victim.banned = banned_baseline
    db.session.commit()
    return instance, blocker, victim


def test_ordinary_site_ban_the_ban_until_probe(app, db_session, monkeypatch):
    """routes.py:1642-1652 -- the "ordinary" site-ban write, and finding 3
    from this file's module docstring: what a peer-supplied `expires`
    string actually does to `blocked.ban_until`.

    `victim.banned` starts False (an explicit baseline, not the bare model
    default -- see this task's global constraints on default-backed
    assertions) so `victim.banned is True` after dispatch is real evidence
    of the write at :1643, not a default sitting there unexamined.

    `expires` is a deliberately UNPARSEABLE string ('not-a-real-date'), to
    prove nothing downstream ever attempts to parse or validate it -- if
    anything did, this string would be exactly the input to make it fail.

    OBSERVED: `session.commit()` (:1648) succeeds regardless. `User` has NO
    `ban_until` column at all (confirmed via the mapper itself, not any one
    instance's state) -- the arm's own `blocked.ban_until = ...` write sets
    a plain, un-mapped Python attribute that SQLAlchemy's flush machinery
    never inspects. It exists only on the dispatcher's own object, in its
    own independent task session, which is closed before dispatch()
    returns. The REAL DateTime column on User, `banned_until`, is read back
    fresh from the database after dispatch and is untouched -- still None,
    exactly as seeded.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, blocker, victim = _seed_ordinary_site_ban()
    victim_id = victim.id

    assert 'ban_until' not in sa_inspect(User).columns.keys()

    activity = inbox_activity(blocker, activity_type='Block',
                              object_uri=victim.ap_profile_id,
                              target=f'https://{instance.domain}',
                              expires='not-a-real-date')

    dispatch(activity)

    db.session.expire_all()
    fresh = db.session.get(User, victim_id)
    assert fresh.banned is True
    assert fresh.banned_until is None  # the REAL column: untouched
    assert getattr(fresh, 'ban_until', '<no such attribute>') == '<no such attribute>'

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_ordinary_site_ban_reads_endTime_when_expires_is_absent(app, db_session, monkeypatch):
    """routes.py:1646-1647 -- the `elif 'endTime' in core_activity:` half of
    the same assignment, reached only when 'expires' is absent. Same
    conclusion as the probe above (no real column exists to write to);
    this test exists only to prove the elif branch itself is reachable and
    does not crash, not to re-derive the ban_until finding a second time.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, blocker, victim = _seed_ordinary_site_ban()

    activity = inbox_activity(blocker, activity_type='Block',
                              object_uri=victim.ap_profile_id,
                              target=f'https://{instance.domain}',
                              endTime='also-not-a-real-date')

    dispatch(activity)

    db.session.expire_all()
    assert victim.banned is True
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_ordinary_site_ban_calls_remove_data_when_removeData_is_set(app, db_session, monkeypatch):
    """routes.py:1650-1651. `site_ban_remove_data(blocker.id, blocked)` runs
    only when `removeData` is truthy -- proven here by setting it, and by
    the sibling test below proving the opposite when it is absent.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, blocker, victim = _seed_ordinary_site_ban()

    calls = record_moderation(monkeypatch, 'site_ban_remove_data')

    activity = inbox_activity(blocker, activity_type='Block',
                              object_uri=victim.ap_profile_id,
                              target=f'https://{instance.domain}', removeData=True)

    dispatch(activity)

    assert len(calls['site_ban_remove_data']) == 1
    args, kwargs = calls['site_ban_remove_data'][0]
    assert kwargs == {}
    blocker_id_arg, blocked_arg = args
    assert blocker_id_arg == blocker.id
    assert sa_inspect(blocked_arg).identity[0] == victim.id

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_ordinary_site_ban_skips_remove_data_when_removeData_is_absent(app, db_session, monkeypatch):
    """routes.py:1650. `removeData` defaults False when the key is absent
    (:1625) -- `site_ban_remove_data` must not run.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, blocker, victim = _seed_ordinary_site_ban()

    calls = record_moderation(monkeypatch, 'site_ban_remove_data')

    activity = inbox_activity(blocker, activity_type='Block',
                              object_uri=victim.ap_profile_id,
                              target=f'https://{instance.domain}')

    dispatch(activity)

    assert calls['site_ban_remove_data'] == []
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_site_ban_already_banned_skips_rebanning_but_still_succeeds(app, db_session, monkeypatch):
    """routes.py:1642. `already_banned` is True (`victim.banned` seeded
    True), so the `blocked.banned = True` / `ban_until` write at
    :1643-1647 is skipped entirely -- proven by seeding a real
    `banned_until` baseline (a REAL column) and showing it survives
    UNCHANGED, which the arm's own docstring explains is deliberate: "we
    don't want remote temp bans to over-ride our permanent bans" (:1601).
    `remove_data` still runs (it is a separate, unconditional `if`,
    :1650-1651) -- proven here with `removeData=True` in the same test,
    since that half of the branch does not depend on `already_banned`.
    SUCCESS is still logged either way (:1652 is unconditional).
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, blocker, victim = _seed_ordinary_site_ban(banned_baseline=True)
    baseline_until = utcnow()
    victim.banned_until = baseline_until
    db.session.commit()
    victim_id = victim.id

    calls = record_moderation(monkeypatch, 'site_ban_remove_data')

    activity = inbox_activity(blocker, activity_type='Block',
                              object_uri=victim.ap_profile_id,
                              target=f'https://{instance.domain}',
                              expires='2099-01-01T00:00:00Z', removeData=True)

    dispatch(activity)

    db.session.expire_all()
    fresh = db.session.get(User, victim_id)
    assert fresh.banned is True
    assert fresh.banned_until == baseline_until  # untouched -- the already_banned skip

    assert len(calls['site_ban_remove_data']) == 1

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


# --- The community-ban path: routes.py:1653-1668 ---


def _seed_bannable_community(host='peer.example', name='microblogs'):
    """A resolvable Community (blocker and blocked are seeded separately by
    each test, since which of the two guard halves is under test varies).
    `ap_fetched_at` stamped so find_actor_or_create_cached's
    schedule_actor_refresh does not fire a real fetch under eager Celery.
    """
    seed_community_owner(host)  # instance id 1 + local owner user id 1
    community = make_community(name, host=host)
    community.ap_fetched_at = utcnow()
    db.session.commit()
    return community


def _seed_community_ban_blocker(domain='blocker.example', name='blockermod'):
    instance = make_instance(domain)
    blocker = make_user(instance, name)
    blocker.ap_fetched_at = utcnow()
    db.session.commit()
    return blocker


def _seed_community_ban_victim(domain='victim.example'):
    instance = make_instance(domain)
    victim = make_user(instance, 'victim')
    db.session.commit()
    return victim


def test_community_ban_of_an_unfound_community_is_ignored(app, db_session, monkeypatch):
    """routes.py:1656-1659. `target` names a community that does not exist
    -- `find_actor_or_create_cached(target, create_if_not_found=False,
    community_only=True)` returns None, and no HTTP fetch is attempted
    (create_if_not_found=False short-circuits find_actor_or_create before
    any remote fetch is even considered).
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    blocker = _seed_community_ban_blocker()
    victim = _seed_community_ban_victim()

    activity = inbox_activity(blocker, activity_type='Block',
                              object_uri=victim.ap_profile_id,
                              target='https://peer.example/c/does-not-exist')

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.result == 'ignored'
    assert log.exception_message == 'Blocked or unfound community'


def test_community_ban_neither_moderator_nor_instance_admin_is_refused(app, db_session, monkeypatch):
    """routes.py:1660-1662. `blocker` is neither a CommunityMember with
    is_moderator=True NOR named in an InstanceRole for the community's home
    instance -- both halves of the guard's `and` are True, so it refuses.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    community = _seed_bannable_community()
    blocker = _seed_community_ban_blocker()
    victim = _seed_community_ban_victim()
    # blocker is deliberately neither a moderator nor an instance admin here.

    calls = record_moderation(monkeypatch, 'ban_user', 'community_ban_remove_data')

    activity = inbox_activity(blocker, activity_type='Block',
                              object_uri=victim.ap_profile_id,
                              target=community.ap_profile_id)

    dispatch(activity)

    assert calls['ban_user'] == []
    assert calls['community_ban_remove_data'] == []

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Does not have permission'


def test_community_ban_by_a_moderator_who_is_not_an_instance_admin_succeeds(app, db_session, monkeypatch):
    """routes.py:1660's `not community.is_moderator(blocker) and not
    community.is_instance_admin(blocker)` -- this fixture makes
    `is_moderator` True and `is_instance_admin` False (no InstanceRole row
    exists for this blocker at all, by construction). That is the DISTINCT
    killer for dropping the guard's SECOND conjunct (leaving only `if not
    community.is_moderator(blocker):`): with only `is_moderator` in play,
    dropping the OTHER conjunct is invisible here (this blocker already
    passes on is_moderator alone) -- it is
    test_community_ban_by_an_instance_admin_who_is_not_a_moderator_succeeds
    below, whose blocker is a moderator=False / instance-admin=True mirror,
    that kills the FIRST conjunct's removal instead. Each fixture leaves
    the OTHER condition verifiably false: this one seeds no InstanceRole
    row whatsoever, so `is_instance_admin` cannot accidentally be True.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    community = _seed_bannable_community()
    blocker = _seed_community_ban_blocker()
    victim = _seed_community_ban_victim()
    make_community_member(blocker, community, is_moderator=True)
    assert community.is_moderator(blocker) is True
    assert community.is_instance_admin(blocker) is False

    calls = record_moderation(monkeypatch, 'ban_user')

    activity = inbox_activity(blocker, activity_type='Block',
                              object_uri=victim.ap_profile_id,
                              target=community.ap_profile_id)

    dispatch(activity)

    assert len(calls['ban_user']) == 1
    args, kwargs = calls['ban_user'][0]
    blocker_arg, blocked_arg, community_arg, core_activity_arg = args
    assert sa_inspect(blocker_arg).identity[0] == blocker.id
    assert sa_inspect(blocked_arg).identity[0] == victim.id
    assert sa_inspect(community_arg).identity[0] == community.id
    assert core_activity_arg is activity

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_community_ban_by_an_instance_admin_who_is_not_a_moderator_succeeds(app, db_session, monkeypatch):
    """routes.py:1660's other half -- this fixture makes `is_instance_admin`
    True (an InstanceRole naming the COMMUNITY's home instance, per
    Community.is_instance_admin's own instance_id, app/models.py:752-757 --
    NOT the blocker's own instance_id) and `is_moderator` False (no
    CommunityMember row at all, by construction). This is the DISTINCT
    killer for dropping the guard's FIRST conjunct (leaving only `if not
    community.is_instance_admin(blocker):`): the sibling test above cannot
    kill this one, since its blocker is not an instance admin at all.

    Also covers `community_ban_remove_data` (routes.py:1664-1665,
    `removeData=True` here), so this file need not build a fourth near-
    identical success fixture solely for that assertion.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    community = _seed_bannable_community()
    blocker = _seed_community_ban_blocker()
    victim = _seed_community_ban_victim()
    db.session.add(InstanceRole(instance_id=community.instance_id, user_id=blocker.id, role='admin'))
    db.session.commit()
    assert community.is_instance_admin(blocker) is True
    assert community.is_moderator(blocker) is False

    calls = record_moderation(monkeypatch, 'ban_user', 'community_ban_remove_data')

    activity = inbox_activity(blocker, activity_type='Block',
                              object_uri=victim.ap_profile_id,
                              target=community.ap_profile_id, removeData=True)

    dispatch(activity)

    assert len(calls['ban_user']) == 1
    assert len(calls['community_ban_remove_data']) == 1
    rd_args, rd_kwargs = calls['community_ban_remove_data'][0]
    blocker_id_arg, community_id_arg, blocked_arg = rd_args
    assert blocker_id_arg == blocker.id
    assert community_id_arg == community.id
    assert sa_inspect(blocked_arg).identity[0] == victim.id

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_community_ban_already_banned_skips_ban_user_but_still_succeeds(app, db_session, monkeypatch):
    """routes.py:1666-1667's `if not already_banned:` guards ONLY the
    `ban_user` call -- `community_ban_remove_data` (:1664-1665) is a
    separate, unconditional-on-already_banned `if`, mirroring the site-ban
    path's identical structure. `already_banned` is forced True by seeding
    `victim.banned = True` before dispatch.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    community = _seed_bannable_community()
    blocker = _seed_community_ban_blocker()
    victim = _seed_community_ban_victim()
    victim.banned = True
    db.session.commit()
    make_community_member(blocker, community, is_moderator=True)

    calls = record_moderation(monkeypatch, 'ban_user', 'community_ban_remove_data')

    activity = inbox_activity(blocker, activity_type='Block',
                              object_uri=victim.ap_profile_id,
                              target=community.ap_profile_id, removeData=True)

    dispatch(activity)

    assert calls['ban_user'] == []
    assert len(calls['community_ban_remove_data']) == 1

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_community_ban_when_announced_short_circuits_target_resolution(app, db_session, monkeypatch):
    """routes.py:1654's `community = community if community else
    find_actor_or_create_cached(target, ...)`. When the Block arrives
    wrapped in an Announce, `community` is ALREADY the outer Announce
    actor -- resolved by the preamble's own community_only lookup
    (routes.py:862) before the Block arm ever runs -- so this line's
    `find_actor_or_create_cached` call is never reached at all. Proven by
    naming a `target` that resolves to NOTHING (a community that does not
    exist): if the arm re-resolved from `target` instead of trusting the
    already-set `community`, this would fall into the "unfound community"
    IGNORED branch instead of succeeding.

    The inner Block's `actor` (not the outer Announce's actor) is what
    becomes `blocker` -- routes.py:915's `user = find_actor_or_create_cached
    (request_json['object']['actor'])`, per the preamble's own Announce
    handling -- so the permission guard is evaluated against the INNER
    actor, made a moderator of the announcing community here.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    community = _seed_bannable_community(host='announcer.example')
    blocker = _seed_community_ban_blocker()
    blocker.ap_fetched_at = utcnow()
    victim = _seed_community_ban_victim()
    make_community_member(blocker, community, is_moderator=True)
    db.session.commit()

    calls = record_moderation(monkeypatch, 'ban_user')

    inner_block = {
        'id': f'https://announcer.example/activities/block-{victim.id}',
        'type': 'Block',
        'actor': blocker.ap_profile_id,
        'object': victim.ap_profile_id,
        'target': 'https://announcer.example/c/this-community-does-not-exist',
    }
    activity = inbox_activity(community, activity_type='Announce', object=inner_block)

    dispatch(activity)

    assert len(calls['ban_user']) == 1
    args, kwargs = calls['ban_user'][0]
    blocker_arg, blocked_arg, community_arg, core_activity_arg = args
    assert sa_inspect(community_arg).identity[0] == community.id
    assert core_activity_arg is inner_block


# --- The Mastodon no-target path: routes.py:1669-1673 ---


def test_mastodon_no_target_creates_a_block_and_logs_nothing(app, db_session, monkeypatch):
    """routes.py:1670-1673. No 'target' key at all -- Mastodon's own Block
    shape. `blocker.has_blocked_user(blocked.id)` is False (no prior
    UserBlock row), so a new one is created and committed. Per this file's
    module docstring (finding 1), no `log_incoming_ap` call exists anywhere
    in this branch's source -- LOG_ACTIVITYPUB_TO_DB is explicitly True
    here so the zero-count assertion is not a vacuous artifact of logging
    being off.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    blocker = make_user(instance, 'blocker')
    blocker.ap_fetched_at = utcnow()
    victim = make_user(instance, 'victim')
    db.session.commit()

    activity = inbox_activity(blocker, activity_type='Block', object_uri=victim.ap_profile_id)
    assert 'target' not in activity

    dispatch(activity)

    row = UserBlock.query.filter_by(blocker_id=blocker.id, blocked_id=victim.id).one()
    assert row is not None

    assert ActivityPubLog.query.count() == 0


def test_mastodon_no_target_skips_a_duplicate_and_logs_nothing(app, db_session, monkeypatch):
    """routes.py:1671's `if not blocker.has_blocked_user(blocked.id):` --
    the OTHER outcome of the same branch: a UserBlock row already exists,
    so nothing new is created. Still logs nothing, matching the "on any
    outcome" claim in this file's module docstring finding 1.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    blocker = make_user(instance, 'blocker')
    blocker.ap_fetched_at = utcnow()
    victim = make_user(instance, 'victim')
    db.session.commit()
    make_user_block(blocker, victim)

    activity = inbox_activity(blocker, activity_type='Block', object_uri=victim.ap_profile_id)

    dispatch(activity)

    assert UserBlock.query.filter_by(blocker_id=blocker.id, blocked_id=victim.id).count() == 1
    assert ActivityPubLog.query.count() == 0
