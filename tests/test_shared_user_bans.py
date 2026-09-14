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

FOUR TESTS IN THIS FILE PIN A DEFECT. app/shared/user.py:161, :171, :183 and
:192 read `if SRC_WEB:` -- a bare imported name whose value is 1
(app/constants.py:91) -- where :93 in the same module reads
`if src == SRC_WEB:`. All four are unconditionally true, so ban_user's
web-only flash messages fire on API calls too. Those tests assert today's
behaviour and are INVERTED by the task that fixes the four lines. Each one
says PINS A DEFECT in its docstring.
"""
import contextlib
from types import SimpleNamespace

import pytest
from flask import session as flask_session

from app import db
from app.constants import SRC_API, SRC_WEB
from app.models import IpBan, ModLog, User
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


def test_ban_user_api_flashes_anyway(app, db_session):
    """PINS A DEFECT. :192 reads `if SRC_WEB:` -- the bare constant, value 1
    (app/constants.py:91) -- where :93 in the same module reads
    `if src == SRC_WEB:`. So this web-only block runs on an API ban as well,
    and :198 writes an interface message into the API caller's session.

    A request context is pushed here ONLY so that flash() has somewhere to
    write; the call itself is SRC_API and carries a bearer token. That is the
    point: app/api/alpha/utils/user.py:975 calls ban_user with SRC_API from a
    real API request handler, so a request context is exactly what production
    has.

    THIS ASSERTION IS INVERTED by the task that fixes the four lines.
    """
    s = _seed_ban_scenario()

    with app.test_request_context('/'):
        with _recording_task_selector():
            ban_user({'person_id': s.target.id, 'purge_content': False,
                      'ban_ip_address': False, 'reason': 'spam'},
                     SRC_API, bearer(s.admin))
        flashed = [message for _category, message in
                   flask_session.get('_flashes', [])]

    assert flashed == [f'{s.target.display_name()} has been banned.']


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
