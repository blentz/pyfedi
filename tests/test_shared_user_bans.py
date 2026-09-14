"""`ban_user` and `unban_user` (app/shared/user.py:141-229).

Both were at literally zero coverage before this file: 51 statements / 26
arcs and 10 / 2, every one missing.

NO PERMISSION GUARD. ban_user contains no authorization check. :143's
`authorise_api_user` authenticates and does not authorize, and both callers
gate before calling -- app/api/alpha/utils/user.py:971 and
app/user/routes.py:767 each test
`user_access('ban users', ...) or user_access('manage users', ...)`. There is
therefore no "unprivileged caller is refused" test in this file, because
there is nothing to refuse it.

THE id-1 ADMIN TRAP IS LIVE HERE. `add_to_modlog` (app/utils.py:3570)
branches on `actor.is_instance_admin() or actor.is_admin() or
actor.is_staff()` to pick 'admin' or 'mod' as the action type, and
`User.is_admin` (app/models.py:1259-1261) returns True for id 1 outright.
tests/conftest.py:131-132 resets every sequence between tests, so the first
user minted is id 1 deterministically. _seed_ban_scenario burns that seat.

FOUR LINES WERE FIXED IN THIS ROUND. app/shared/user.py:161, :171, :183 and
:192 used to read `if SRC_WEB:` -- a bare imported name whose value is 1
(app/constants.py:91) -- where :93 in the same module already read
`if src == SRC_WEB:`. All four were therefore unconditionally true, so
ban_user's web-only flash messages fired on API calls too. They now read
`if src == SRC_WEB:`, matching :93. The four tests below --
test_ban_user_api_does_not_flash, test_ban_user_purging_via_the_api_does_not_warn,
test_ban_user_purging_a_local_target_via_the_api_does_not_flash and
test_ban_user_purging_a_remote_target_via_the_api_does_not_flash -- are the
inverted pins: they were written first, against the old behaviour, asserting
the flash DID fire; the fix landed in the same commit as their inversion, and
they now assert an empty flash list plus a same-mechanism assertion that the
underlying ban/purge actually ran, so the empty list is evidence the block
was skipped rather than an artefact of the harness.
"""
import contextlib
from types import SimpleNamespace

import pytest
from flask import session as flask_session

from app import db
from app.constants import SRC_API, SRC_WEB
from app.models import InstanceRole, IpBan, ModLog, User
from app.shared.user import ban_user, unban_user
from tests.factories import (bearer, grant_permission, make_instance,
                             make_user, web_ctx)


class _BanForm:
    """The web arm's `input`, which is a WTForms object rather than a dict.

    :151-155 read `input.person_id`, `input.purge.data`, `input.ip_address.data`,
    `input.reason.data` and `input.flush.data`. app/user/routes.py:782 sets
    `form.person_id = user.id` as a plain attribute on the form immediately
    before calling, so person_id is an int here and the other four are
    field-like objects with a `.data`.
    """

    class _Field:
        def __init__(self, data):
            self.data = data

    def __init__(self, person_id, purge=False, ip_address=False, reason='',
                 flush=False):
        self.person_id = person_id
        self.purge = self._Field(purge)
        self.ip_address = self._Field(ip_address)
        self.reason = self._Field(reason)
        self.flush = self._Field(flush)


def _seed_ban_scenario(target_local=True):
    """An admin who may ban, and a target to ban.

    The first user minted is id 1 and `User.is_admin` (app/models.py:1259-1261)
    calls id 1 an admin regardless of roles. `add_to_modlog` (app/utils.py:3570)
    reads exactly that to choose between the 'admin' and 'mod' action types,
    so without this burn every modlog assertion in this file would be
    satisfied by the id-1 shortcut rather than by the role the test granted.
    tests/test_shared_reply_make.py:247 is the established precedent.

    `grant_permission` is called ONCE here. It mints a Role with a sequential
    id, and app/shared/user.py does not compare role ids -- but
    block_another_user in the sibling test file does, against ROLE_STAFF (3)
    and ROLE_ADMIN (4), so the call count is kept deliberate and low.
    """
    instance = make_instance('remote.example')
    burn = make_user(instance, 'burn-the-id-1-seat', local=True)
    assert burn.id == 1, 'the id-1 admin trap moved; re-derive before trusting this'

    admin = make_user(instance, 'banner', local=True)
    target = make_user(instance, 'target', local=target_local)
    grant_permission(admin, 'ban users')
    db.session.commit()
    return SimpleNamespace(admin=admin, target=target, instance=instance)


@contextlib.contextmanager
def _recording_task_selector():
    """Collect every task key ban_user and unban_user federate.

    Both call `task_selector(...)` unqualified, and app/shared/user.py:10's
    `from app.shared.tasks import task_selector` already bound the original
    into this module's globals -- so patching app.shared.tasks.task_selector
    would NOT intercept. Rebinding the name ON app.shared.user does.
    tests/test_shared_post_moderation.py:142 is the precedent.

    Restores in a finally: app.shared.user is imported once per session, so a
    leaked patch would corrupt every test that ran after this one.
    """
    import app.shared.user as user_module
    calls = []
    original = user_module.task_selector

    def recorder(task_key, **kwargs):
        calls.append((task_key, kwargs))
        return None

    user_module.task_selector = recorder
    try:
        yield calls
    finally:
        user_module.task_selector = original


def test_ban_user_api_without_purge_bans_and_logs(app, db_session):
    s = _seed_ban_scenario()

    with app.test_request_context('/'):
        with _recording_task_selector() as calls:
            ban_user({'person_id': s.target.id, 'purge_content': False,
                      'ban_ip_address': False, 'reason': 'spam'},
                     SRC_API, bearer(s.admin))

    db.session.expire_all()
    assert db.session.query(User).get(s.target.id).banned is True
    entry = db.session.query(ModLog).one()
    assert entry.action == 'ban_user'
    assert entry.reason == 'spam'
    assert [key for key, _kwargs in calls] == ['ban_from_site']


def test_ban_user_without_purge_does_not_delete_the_target(app, db_session):
    """The `else` at :188 is the no-purge branch: it logs 'ban_user', not
    'delete_user', and reaches no deletion path at all.

    Asserting the modlog action alone would not separate the branches -- both
    write a ModLog row -- so `deleted` is asserted too.
    """
    s = _seed_ban_scenario()

    with app.test_request_context('/'):
        with _recording_task_selector():
            ban_user({'person_id': s.target.id, 'purge_content': False,
                      'ban_ip_address': False, 'reason': 'spam'},
                     SRC_API, bearer(s.admin))

    db.session.expire_all()
    target = db.session.query(User).get(s.target.id)
    assert target.deleted is False
    assert db.session.query(ModLog).one().action == 'ban_user'


def test_ban_user_passes_remove_data_false_when_not_purging(app, db_session):
    """:207's `remove_data=purge_content and to_ban.is_local()`.

    The first operand. Task 5 covers the second.
    """
    s = _seed_ban_scenario()

    with app.test_request_context('/'):
        with _recording_task_selector() as calls:
            ban_user({'person_id': s.target.id, 'purge_content': False,
                      'ban_ip_address': False, 'reason': 'spam'},
                     SRC_API, bearer(s.admin))

    key, kwargs = calls[0]
    assert key == 'ban_from_site'
    assert kwargs['remove_data'] is False
    assert kwargs['user_id'] == s.target.id
    assert kwargs['mod_id'] == s.admin.id


def test_ban_user_api_does_not_flash(app, db_session):
    """The inversion of a pin. Before the fix, :192 read `if SRC_WEB:` -- the
    bare constant, value 1 -- so this web-only block ran on API bans and wrote
    an interface message into the API caller's session. It now reads
    `if src == SRC_WEB:`, matching :93.

    The request context is still pushed, and flash() would still succeed if
    the block ran: the empty list below is therefore evidence that the block
    was SKIPPED, not that flashing was impossible. That distinction is what
    makes this an assertion rather than an artefact of the harness -- see the
    web test above, which flashes under the same conditions.

    `banned is True` is kept but is not load-bearing for THIS guard: :156
    sets it unconditionally, before the `purge_content` fork and long before
    :192, so it witnesses only that ban_user's first two statements ran --
    mechanism (a), asserting on state something else sets unconditionally.
    The load-bearing check is the ModLog assertion: :189's
    `add_to_modlog('ban_user', ...)` runs only inside the no-purge `else`
    block that :192 itself lives in, immediately before the guard, so a
    'ban_user' row proves control actually reached the statement the guard
    is on.
    """
    s = _seed_ban_scenario()

    with app.test_request_context('/'):
        with _recording_task_selector():
            ban_user({'person_id': s.target.id, 'purge_content': False,
                      'ban_ip_address': False, 'reason': 'spam'},
                     SRC_API, bearer(s.admin))
        flashed = [message for _category, message in
                   flask_session.get('_flashes', [])]

    assert flashed == []
    db.session.expire_all()
    assert db.session.query(User).get(s.target.id).banned is True
    assert db.session.query(ModLog).one().action == 'ban_user'


def test_ban_user_web_flashes_the_plain_message(app, db_session):
    """:198's else -- a target holding no role, so :195 is false.

    Note which `else` that is: it hangs off the role check at :195, not off
    the source test at :192. A banned user WITH a role gets only the warning
    at :196 and never this message.
    """
    s = _seed_ban_scenario()

    with web_ctx(app, s.admin):
        with _recording_task_selector():
            ban_user(_BanForm(s.target.id, reason='spam'), SRC_WEB, None)
        flashed = [message for _category, message in
                   flask_session.get('_flashes', [])]

    assert flashed == [f'{s.target.display_name()} has been banned.']


def test_ban_user_warns_instead_when_the_target_holds_a_role(app, db_session):
    """:195-196 -- and the suppression at :197-198 that follows from it.

    `is_admin()` matches on the role NAME (app/models.py:1263), so the role
    granted here is named 'Admin' deliberately. The assertion is that the
    plain "has been banned" message is ABSENT: that absence is the whole
    behaviour of the else at :197.
    """
    s = _seed_ban_scenario()
    from app.models import Role, user_role
    role = Role(name='Admin', weight=0)
    db.session.add(role)
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=s.target.id,
                                                 role_id=role.id))
    db.session.commit()

    with web_ctx(app, s.admin):
        with _recording_task_selector():
            ban_user(_BanForm(s.target.id, reason='spam'), SRC_WEB, None)
        flashed = [message for _category, message in
                   flask_session.get('_flashes', [])]

    assert flashed == ['Banned user with role permissions.']


def test_ban_user_creates_an_ip_ban(app, db_session):
    """:201-205. Both operands of :201's `and` need to be true, so the target
    is given an ip_address as well as the flag."""
    s = _seed_ban_scenario()
    s.target.ip_address = '203.0.113.7'
    db.session.commit()

    with app.test_request_context('/'):
        with _recording_task_selector():
            ban_user({'person_id': s.target.id, 'purge_content': False,
                      'ban_ip_address': True, 'reason': 'spam'},
                     SRC_API, bearer(s.admin))

    ban = db.session.query(IpBan).one()
    assert ban.ip_address == '203.0.113.7'
    assert ban.notes == 'spam'


def test_ban_user_skips_the_ip_ban_when_the_target_has_no_address(app, db_session):
    """The second operand of :201. The flag is TRUE here and no IpBan is
    created, which is what separates the two operands: a version testing only
    `ban_ip_address` would insert a row with a NULL address."""
    s = _seed_ban_scenario()
    assert s.target.ip_address is None

    with app.test_request_context('/'):
        with _recording_task_selector():
            ban_user({'person_id': s.target.id, 'purge_content': False,
                      'ban_ip_address': True, 'reason': 'spam'},
                     SRC_API, bearer(s.admin))

    assert db.session.query(IpBan).count() == 0


def test_ban_user_does_not_duplicate_an_existing_ip_ban(app, db_session):
    """:203's `if not existing_ip_ban` -- the false arc."""
    s = _seed_ban_scenario()
    s.target.ip_address = '203.0.113.7'
    db.session.add(IpBan(ip_address='203.0.113.7', notes='an earlier ban'))
    db.session.commit()

    with app.test_request_context('/'):
        with _recording_task_selector():
            ban_user({'person_id': s.target.id, 'purge_content': False,
                      'ban_ip_address': True, 'reason': 'spam'},
                     SRC_API, bearer(s.admin))

    ban = db.session.query(IpBan).one()
    assert ban.notes == 'an earlier ban'


def test_unban_user_api_clears_banned_and_deleted(app, db_session):
    """:221-222 clear BOTH flags. The target is seeded with both set, so a
    version clearing only `banned` fails here."""
    s = _seed_ban_scenario()
    s.target.banned = True
    s.target.deleted = True
    db.session.commit()

    with _recording_task_selector() as calls:
        returned = unban_user({'person_id': s.target.id}, SRC_API,
                              bearer(s.admin))

    db.session.expire_all()
    target = db.session.query(User).get(s.target.id)
    assert target.banned is False
    assert target.deleted is False
    assert [key for key, _kwargs in calls] == ['unban_from_site']
    assert returned is None
    assert db.session.query(ModLog).one().action == 'unban_user'


def test_unban_user_web_takes_the_same_dict_shaped_input(app, db_session):
    """:214-219's fork is real but its two arms are near-identical: both read
    `input['person_id']`, so unban_user takes a DICT even on the web path --
    unlike ban_user, whose web arm reads form attributes. app/user/routes.py:817
    passes `{'person_id': user.id}` accordingly. The arms differ only in where
    the actor comes from, which is why `mod_id` is asserted here.
    """
    s = _seed_ban_scenario()
    s.target.banned = True
    db.session.commit()

    with web_ctx(app, s.admin):
        with _recording_task_selector() as calls:
            unban_user({'person_id': s.target.id}, SRC_WEB, None)

    db.session.expire_all()
    assert db.session.query(User).get(s.target.id).banned is False
    _key, kwargs = calls[0]
    assert kwargs['mod_id'] == s.admin.id


@pytest.fixture
def no_real_purge(monkeypatch):
    """Record purge_user_then_delete instead of running it.

    app/shared/user.py:11 does `from app.user.utils import
    purge_user_then_delete`, which bound the original into this module's
    globals at import time -- so patching app.user.utils would not intercept
    :170's unqualified call. The real function dispatches a Celery task, and
    tests/conftest.py:105-110 sets task_always_eager with eager_propagates,
    so leaving it unpatched runs the entire purge inline against a separate
    task session.
    """
    calls = []
    monkeypatch.setattr('app.shared.user.purge_user_then_delete',
                        lambda user_id, flush=True: calls.append((user_id, flush)))
    return calls


@pytest.fixture
def redis_lock_only_double(monkeypatch):
    """A `app.redis_client` double covering only `.lock(...)`.

    This environment's fakeredis has no Lua scripting, and redis-py's
    Lock.release() needs EVALSHA -- so the redis_double fixture raises
    `redis.exceptions.ResponseError: unknown command 'evalsha'` on __exit__
    of any `with redis_client.lock(...)` block, which app/shared/user.py:178
    is. See tests/test_inbox_dispatch_votes.py:145-159 and "The fakeredis lock
    limitation" in tests/README.md.

    app/shared/user.py:177 imports redis_client INSIDE the function body, so
    patching the single module attribute reaches it.
    """
    class _LockOnly:
        def lock(self, *args, **kwargs):
            return contextlib.nullcontext()

    monkeypatch.setattr('app.redis_client', _LockOnly())


def test_ban_user_purging_a_local_target_deletes_it_through_the_purge_task(
        app, db_session, no_real_purge):
    """:168's true arm. A local target reaches :169-170: deleted_by is
    stamped and purge_user_then_delete is dispatched with the flush flag the
    caller supplied.

    flush_cdn is False on the API path (:148 sets it unconditionally), which
    is what the second element of the recorded call asserts.

    NO REQUEST CONTEXT, deliberately. This test used to push one, and its
    docstring used to say why: before commit fd5b9bcd, :171 read
    `if SRC_WEB:` -- the bare imported constant, value 1 -- so the flash at
    :172-173 fired on this SRC_API call and needed somewhere to write to.
    :171 now reads `if src == SRC_WEB:` and this call passes SRC_API, so
    nothing flashes and the wrapper became vestigial; it is dropped rather
    than left standing with a false explanation attached. Nothing else on
    ban_user's SRC_API path reads the request: :143's authorise_api_user
    decodes a JWT out of the `auth` argument alone (tests/test_shared_user_
    follows.py:224 calls it with no context at all), add_to_modlog at :186
    reads only the ORM and get_setting, and purge_user_then_delete at :170
    is replaced by the no_real_purge fixture. If :171 were ever reverted to
    the bare constant, this test would now fail with RuntimeError rather
    than pass silently -- a crash-shaped signal, which is why the four
    dedicated inverted pins below keep their contexts and assert on an empty
    flash list instead.
    """
    s = _seed_ban_scenario(target_local=True)

    with _recording_task_selector():
        ban_user({'person_id': s.target.id, 'purge_content': True,
                  'ban_ip_address': False, 'reason': 'spam'},
                 SRC_API, bearer(s.admin))

    db.session.expire_all()
    assert no_real_purge == [(s.target.id, False)]
    assert db.session.query(User).get(s.target.id).deleted_by == s.admin.id
    assert db.session.query(ModLog).one().action == 'delete_user'


def test_ban_user_purging_passes_remove_data_true_for_a_local_target(
        app, db_session, no_real_purge):
    """:207's `remove_data=purge_content and to_ban.is_local()` -- the
    second operand, whose first operand Task 4 covered.

    NO REQUEST CONTEXT, for the same reason as the test above: :171 reads
    `if src == SRC_WEB:` since commit fd5b9bcd and this is an SRC_API call,
    so nothing flashes. The wrapper this test used to carry was justified by
    the pre-fix always-true `if SRC_WEB:`, and that justification no longer
    holds.
    """
    s = _seed_ban_scenario(target_local=True)

    with _recording_task_selector() as calls:
        ban_user({'person_id': s.target.id, 'purge_content': True,
                  'ban_ip_address': False, 'reason': 'spam'},
                 SRC_API, bearer(s.admin))

    _key, kwargs = calls[0]
    assert kwargs['remove_data'] is True


def test_ban_user_purging_a_remote_target_takes_the_local_deletion_path(
        app, db_session, no_real_purge, redis_lock_only_double):
    """:174-184's else arm. A remote target is NOT dispatched to the purge
    task: its content is removed inline and the row is marked deleted under a
    redis lock.

    `no_real_purge` is requested even though nothing should reach it -- an
    empty recorder is the assertion that :170 was not taken, and without the
    patch a wrong branch would run the real Celery purge instead of failing
    visibly.

    make_user leaves a remote user's ap_id set (tests/factories.py:60), and
    User.is_local() (app/models.py:1252) is
    `self.ap_id is None or self.ap_profile_id.startswith(SERVER_URL)` -- so a
    user built against remote.example is not local on either operand.

    NO REQUEST CONTEXT. This test used to push one because :183 read the
    bare `if SRC_WEB:` before commit fd5b9bcd and its flash at :184 fired on
    this SRC_API call. :183 now reads `if src == SRC_WEB:`, so nothing
    flashes and the wrapper is dropped. The remote arm reaches no other
    request-dependent code either: :175-176's delete_dependencies and
    purge_content work through the ORM, and :178's redis lock is served by
    the redis_lock_only_double fixture.
    """
    s = _seed_ban_scenario(target_local=False)

    with _recording_task_selector():
        ban_user({'person_id': s.target.id, 'purge_content': True,
                  'ban_ip_address': False, 'reason': 'spam'},
                 SRC_API, bearer(s.admin))

    db.session.expire_all()
    assert no_real_purge == []
    target = db.session.query(User).get(s.target.id)
    assert target.deleted is True
    assert target.deleted_by == s.admin.id
    assert db.session.query(ModLog).one().action == 'delete_user'


def test_ban_user_purging_passes_remove_data_false_for_a_remote_target(
        app, db_session, no_real_purge, redis_lock_only_double):
    """The other half of :207's `and`: purge_content is True but the target
    is remote, so remove_data is False. Paired with the local test above,
    this is what makes the `and` non-void.

    NO REQUEST CONTEXT, for the same reason as the test above: :183 reads
    `if src == SRC_WEB:` since commit fd5b9bcd and this is an SRC_API call,
    so the flash at :184 does not fire and the wrapper this test used to
    carry no longer has a reason to exist.
    """
    s = _seed_ban_scenario(target_local=False)

    with _recording_task_selector() as calls:
        ban_user({'person_id': s.target.id, 'purge_content': True,
                  'ban_ip_address': False, 'reason': 'spam'},
                 SRC_API, bearer(s.admin))

    _key, kwargs = calls[0]
    assert kwargs['remove_data'] is False


def test_ban_user_purging_a_local_target_flashes_on_the_web(
        app, db_session, no_real_purge):
    """:171-173. The web form path also carries flush_cdn from
    `input.flush.data` (:155), which the API path hardcodes False -- so
    _BanForm sets flush=True and the recorded call asserts it."""
    s = _seed_ban_scenario(target_local=True)

    with web_ctx(app, s.admin):
        with _recording_task_selector():
            ban_user(_BanForm(s.target.id, purge=True, reason='spam',
                              flush=True), SRC_WEB, None)
        flashed = [message for _category, message in
                   flask_session.get('_flashes', [])]

    assert no_real_purge == [(s.target.id, True)]
    assert flashed == [
        f'{s.target.display_name()} has been banned, deleted and all their '
        f'content deleted. This might take a few minutes.']


def test_ban_user_purging_a_remote_target_flashes_the_shorter_message(
        app, db_session, no_real_purge, redis_lock_only_double):
    """:183-184. The remote message omits "This might take a few minutes."
    because nothing was queued -- the deletion already happened inline. The
    two messages are compared in full so that a swapped pair fails."""
    s = _seed_ban_scenario(target_local=False)

    with web_ctx(app, s.admin):
        with _recording_task_selector():
            ban_user(_BanForm(s.target.id, purge=True, reason='spam'),
                     SRC_WEB, None)
        flashed = [message for _category, message in
                   flask_session.get('_flashes', [])]

    assert flashed == [
        f'{s.target.display_name()} has been banned, deleted and all their '
        f'content deleted.']


def test_ban_user_purging_an_instance_admin_warns_first(
        app, db_session, no_real_purge, redis_lock_only_double):
    """:162-163. `is_instance_admin` (app/models.py:1277-1284) needs an
    InstanceRole row with role 'admin' on the user's own instance -- it is
    NOT the same thing as the 'Admin' Role that is_admin() matches by name,
    and the two warnings at :163 and :165 are independent `if`s rather than a
    chain.

    The target is remote so that :174's arm is the one taken, keeping this
    test about the warning rather than about the purge task.

    The flash list is compared in FULL rather than with `in`. The remote
    purge path flashes exactly twice -- :163's warning, then :184's shorter
    "banned, deleted" message -- and `is_admin()`/`is_staff()` are both false
    for this target, so :165 does not fire. Full equality therefore states
    the whole outcome and additionally catches a duplicated or extra warning
    that an `in` check would pass. The pattern is
    test_ban_user_purging_a_remote_target_flashes_the_shorter_message above,
    including the `display_name()` evaluated after the call: :180 sets
    `deleted = True` before :184 reads it, so both the flash and this
    expectation resolve to User.display_name's '[deleted]' arm
    (app/models.py:1183-1184).
    """
    s = _seed_ban_scenario(target_local=False)
    db.session.add(InstanceRole(instance_id=s.target.instance_id,
                                user_id=s.target.id, role='admin'))
    db.session.commit()

    with web_ctx(app, s.admin):
        with _recording_task_selector():
            ban_user(_BanForm(s.target.id, purge=True, reason='spam'),
                     SRC_WEB, None)
        flashed = [message for _category, message in
                   flask_session.get('_flashes', [])]

    assert flashed == [
        'Purged user was a remote instance admin.',
        f'{s.target.display_name()} has been banned, deleted and all their '
        f'content deleted.']


def test_ban_user_purging_a_role_holder_warns_about_permissions(
        app, db_session, no_real_purge, redis_lock_only_double):
    """:164-165. Named 'Admin' because is_admin() matches the role NAME
    (app/models.py:1263). Independent of the instance-admin warning above:
    this target has no InstanceRole, so only one of the two warnings fires,
    which is what separates :162 from :164."""
    s = _seed_ban_scenario(target_local=False)
    from app.models import Role, user_role
    role = Role(name='Admin', weight=0)
    db.session.add(role)
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=s.target.id,
                                                 role_id=role.id))
    db.session.commit()

    with web_ctx(app, s.admin):
        with _recording_task_selector():
            ban_user(_BanForm(s.target.id, purge=True, reason='spam'),
                     SRC_WEB, None)
        flashed = [message for _category, message in
                   flask_session.get('_flashes', [])]

    assert 'Purged user with role permissions.' in flashed
    assert 'Purged user was a remote instance admin.' not in flashed


def test_ban_user_purging_via_the_api_does_not_warn(
        app, db_session, no_real_purge, redis_lock_only_double):
    """The inversion of a pin. Before the fix, :161 read `if SRC_WEB:` -- the
    bare constant, value 1 -- so this warning block ran on an API ban too. It
    now reads `if src == SRC_WEB:`, matching :93.

    The empty flash list alone would be mechanism (c) of the five false
    witnesses -- emptiness with no same-mechanism positive control -- so the
    ModLog assertion below proves the purge itself still ran; the surviving
    web-arm test above is the positive control proving the warning is
    reachable in this harness at all.
    """
    s = _seed_ban_scenario(target_local=False)
    db.session.add(InstanceRole(instance_id=s.target.instance_id,
                                user_id=s.target.id, role='admin'))
    db.session.commit()

    with app.test_request_context('/'):
        with _recording_task_selector():
            ban_user({'person_id': s.target.id, 'purge_content': True,
                      'ban_ip_address': False, 'reason': 'spam'},
                     SRC_API, bearer(s.admin))
        flashed = [message for _category, message in
                   flask_session.get('_flashes', [])]

    assert flashed == []
    assert db.session.query(ModLog).one().action == 'delete_user'


def test_ban_user_purging_a_local_target_via_the_api_does_not_flash(
        app, db_session, no_real_purge):
    """The inversion of a pin. Before the fix, :171 read `if SRC_WEB:` -- the
    same bare-constant fault as :161. It now reads `if src == SRC_WEB:`,
    matching :93.

    `no_real_purge`'s recorded call is the same-mechanism positive control:
    it proves purge_user_then_delete actually ran, so the empty flash list is
    evidence the block was skipped rather than an artefact of the harness --
    see the web test above, which flashes under the same conditions.
    """
    s = _seed_ban_scenario(target_local=True)

    with app.test_request_context('/'):
        with _recording_task_selector():
            ban_user({'person_id': s.target.id, 'purge_content': True,
                      'ban_ip_address': False, 'reason': 'spam'},
                     SRC_API, bearer(s.admin))
        flashed = [message for _category, message in
                   flask_session.get('_flashes', [])]

    assert flashed == []
    assert no_real_purge == [(s.target.id, False)]


def test_ban_user_purging_a_remote_target_via_the_api_does_not_flash(
        app, db_session, no_real_purge, redis_lock_only_double):
    """The inversion of a pin. Before the fix, :183 read `if SRC_WEB:` -- the
    same bare-constant fault. It now reads `if src == SRC_WEB:`, matching
    :93.

    The ModLog assertion is the same-mechanism positive control proving the
    remote purge path actually ran, so the empty flash list is evidence the
    block was skipped rather than an artefact of the harness -- see the web
    test above, which flashes under the same conditions.
    """
    s = _seed_ban_scenario(target_local=False)

    with app.test_request_context('/'):
        with _recording_task_selector():
            ban_user({'person_id': s.target.id, 'purge_content': True,
                      'ban_ip_address': False, 'reason': 'spam'},
                     SRC_API, bearer(s.admin))
        flashed = [message for _category, message in
                   flask_session.get('_flashes', [])]

    assert flashed == []
    assert db.session.query(ModLog).one().action == 'delete_user'


def test_ban_user_without_purge_warns_about_an_instance_admin_too(
        app, db_session):
    """:193-194 -- the no-purge sibling of
    test_ban_user_purging_an_instance_admin_warns_first's :162-163. This gap
    was left open by the plan's Task 4 brief, which wrote tests for
    :195-198 (the role-holder warning and the plain-message else) but never
    seeded an InstanceRole to exercise :193's true arm, so :194 was never
    executed.

    :193 and :195 are independent `if`s, not a chain: this target carries an
    InstanceRole but no Role, so :195 is false and :197's else also fires.
    Both messages are expected, and the full flash list is asserted rather
    than membership of one string -- a membership-only assertion would still
    pass if :197's else had stopped firing.

    is_instance_admin (app/models.py:1277-1284) needs an InstanceRole row
    with role='admin' matching the user's own instance_id, and is False
    outright when instance_id is falsy -- it is NOT the same thing as the
    'Admin'-named Role that is_admin() matches, which is why this target
    holds an InstanceRole and no Role.

    Driven through the web arm (web_ctx) rather than SRC_API: purge_content
    is False here, so nothing needs app.test_request_context('/') beyond
    what web_ctx already provides for flash()/session.
    """
    s = _seed_ban_scenario()
    db.session.add(InstanceRole(instance_id=s.target.instance_id,
                                user_id=s.target.id, role='admin'))
    db.session.commit()

    with web_ctx(app, s.admin):
        with _recording_task_selector():
            ban_user(_BanForm(s.target.id, reason='spam'), SRC_WEB, None)
        flashed = [message for _category, message in
                   flask_session.get('_flashes', [])]

    assert flashed == [
        'Banned user was a remote instance admin.',
        f'{s.target.display_name()} has been banned.']
