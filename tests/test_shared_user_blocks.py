"""`block_another_user` and `unblock_another_user` (app/shared/user.py:20-87),
and `bot_challenge_user` (app/shared/user.py:303-339).

All three functions were at literally zero coverage before this file:
`block_another_user` 24 statements / 14 arcs, `unblock_another_user` 16 / 10
and `bot_challenge_user` 23 / 8, every one missing.

WHY bot_challenge_user IS IN THIS FILE rather than one of its own. It is the
third of the module's "one shared helper, two source arms" functions and it
shares this file's harness exactly -- `_seed_blockers`, `web_ctx`, `bearer`
and the SRC_WEB/SRC_API fork -- while having nothing to do with either the
ban pair in tests/test_shared_user_bans.py or the follow pair in
tests/test_shared_user_follows.py. Six tests cover it, at :324-461 (count
re-derived with `/usr/bin/grep -cE "^ *def test_.*bot_challenge"`):

- test_bot_challenge_user_web_sends_a_message_and_records_the_challenge
  the first-challenge path -- :313's false arm, :318's fresh uuid and
  :337-339's BotChallenge row, with the uuid asserted to appear in the
  message body :333 interpolates it into.
- test_bot_challenge_user_puts_the_recipient_in_the_conversation
  :321's `members.append(recipient)`, compared as a SET because the
  relationship's row order is a query-planner artefact.
- test_bot_challenge_user_reuses_the_uuid_of_an_unanswered_challenge
  :313's true arm with :314 false, reaching :316's uuid reuse and NOT
  minting a second BotChallenge row at :337.
- test_bot_challenge_user_refuses_someone_who_already_answered
  :314's true arm and :315's raise.
- test_bot_challenge_user_continues_for_a_confirmed_bot
  the other half of :314 -- `is_a_bot is True` is not `is False`, so a
  confirmed bot is re-challenged rather than refused. Paired with the test
  above, this is what makes :314's `is False` non-void against a plain
  truthiness test.
- test_bot_challenge_user_src_api_bare_call_crashes_on_none_current_user
  :306-307's SRC_API arm, and a PINNED DEFECT: :320 builds the Conversation
  from `user` but :322 appends `current_user`. Registered, not fixed.

THE ROLE TRAP. `block_another_user:33-35` reads
`SELECT role_id FROM "user_role" WHERE user_id = :person_id` with `.scalar()`
and compares the result against the INTEGERS ROLE_ADMIN (4) and ROLE_STAFF
(3) from app/constants.py:80-81. tests/factories.py:365's `grant_permission`
mints a fresh Role per call with a sequential id, and tests/conftest.py:131
resets every sequence between tests -- so the third or fourth
`grant_permission` call in a test would make its subject staff or admin BY
ID, refusing a block for a reason the test never intended. Every role in this
file is therefore created with an EXPLICIT id, and named something that is
neither 'Admin' nor 'Staff' so that `User.is_admin()`'s name path
(app/models.py:1263) cannot satisfy an assertion the id comparison was
supposed to carry.
"""
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app import db
from app.constants import NOTIF_USER, ROLE_ADMIN, ROLE_STAFF, SRC_API, SRC_WEB
from app.models import (BotChallenge, ChatMessage, Conversation,
                         NotificationSubscription, Role, UserBlock, user_role)
from app.shared.user import block_another_user, bot_challenge_user, unblock_another_user
from tests.factories import bearer, make_instance, make_user, web_ctx


def _seed_blockers():
    """Two local users, neither of them id 1.

    The first user minted in any test is id 1, deterministically:
    tests/conftest.py:131-132 runs `SELECT setval(c.oid, 1, false)` over every
    sequence after every test. `User.is_admin` (app/models.py:1259-1261)
    returns True for id 1 regardless of roles. Nothing in THIS file reads
    is_admin -- block_another_user compares role ids, not names -- but the
    burn is kept so that a later test added to this file cannot inherit the
    trap silently. tests/test_shared_reply_make.py:247 is the precedent.
    """
    instance = make_instance('remote.example')
    burn = make_user(instance, 'burn-the-id-1-seat', local=True)
    assert burn.id == 1, 'the id-1 admin trap moved; re-derive before trusting this'

    blocker = make_user(instance, 'blocker', local=True)
    target = make_user(instance, 'target', local=True)
    db.session.commit()
    return SimpleNamespace(blocker=blocker, target=target)


def _give_role_with_id(user, role_id):
    """Put `user` in `user_role` against a role whose id is exactly role_id.

    `user_role.role_id` is a foreign key to `role.id` (app/models.py:937), so
    the Role row has to exist. The name deliberately avoids 'Admin' and
    'Staff' -- block_another_user reads the ID, and a name that also satisfies
    User.is_admin() would let a future assertion pass for the wrong reason.
    """
    db.session.add(Role(id=role_id, name=f'role-with-id-{role_id}', weight=0))
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=user.id, role_id=role_id))
    db.session.commit()


def test_block_another_user_api_creates_the_block_and_returns_the_blocker(app, db_session):
    s = _seed_blockers()

    returned = block_another_user(s.target.id, SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(UserBlock).filter_by(
        blocker_id=s.blocker.id, blocked_id=s.target.id).count() == 1


def test_block_another_user_web_creates_the_block_and_returns_none(app, db_session):
    """The web arm returns None and leaves the flash to its caller (:58).

    Both the return value and the row are asserted: a function that created
    nothing would also return None.
    """
    s = _seed_blockers()

    with web_ctx(app, s.blocker):
        returned = block_another_user(s.target.id, SRC_WEB)

    assert returned is None
    assert db.session.query(UserBlock).filter_by(
        blocker_id=s.blocker.id, blocked_id=s.target.id).count() == 1


def test_block_another_user_deletes_the_targets_subscription_to_the_blocker(app, db_session):
    """:46-48's raw DELETE, which is easy to mistake for symmetry.

    Read the parameters: `entity_id = :current_user AND user_id = :user_id`
    with current_user bound to the BLOCKER's id and user_id to the TARGET's.
    So the row removed is the one where the TARGET subscribes to the BLOCKER
    -- not the blocker's own subscription to the target. Two subscriptions are
    seeded, in both directions, and only one may survive. With a single
    subscription this test would pass under a swapped binding.
    """
    s = _seed_blockers()
    db.session.add(NotificationSubscription(
        name='target follows blocker', user_id=s.target.id,
        entity_id=s.blocker.id, type=NOTIF_USER))
    db.session.add(NotificationSubscription(
        name='blocker follows target', user_id=s.blocker.id,
        entity_id=s.target.id, type=NOTIF_USER))
    db.session.commit()

    block_another_user(s.target.id, SRC_API, bearer(s.blocker))
    db.session.expire_all()

    survivors = {(n.user_id, n.entity_id)
                 for n in db.session.query(NotificationSubscription).all()}
    assert survivors == {(s.blocker.id, s.target.id)}


def test_block_another_user_is_idempotent(app, db_session):
    """:43's `if not existing_block` -- the false arc.

    The second call must not add a second row, and must still return the
    blocker's id from :56 rather than falling out of the function early.
    """
    s = _seed_blockers()
    block_another_user(s.target.id, SRC_API, bearer(s.blocker))

    returned = block_another_user(s.target.id, SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(UserBlock).filter_by(
        blocker_id=s.blocker.id, blocked_id=s.target.id).count() == 1


def test_block_another_user_api_refuses_self(app, db_session):
    s = _seed_blockers()

    with pytest.raises(Exception, match='cannot_block_self'):
        block_another_user(s.blocker.id, SRC_API, bearer(s.blocker))

    assert db.session.query(UserBlock).count() == 0


def test_block_another_user_web_refuses_self_without_raising(app, db_session):
    """The web arm flashes and returns (:30-31) where the API arm raises.

    The row count is the assertion that matters: a `return` that skipped the
    flash would also produce no rows, so the flash is checked too, through the
    session's `_flashes`.
    """
    s = _seed_blockers()

    with web_ctx(app, s.blocker):
        from flask import session
        returned = block_another_user(s.blocker.id, SRC_WEB)
        flashed = [message for _category, message in session.get('_flashes', [])]

    assert returned is None
    assert flashed == ['You cannot block yourself.']
    assert db.session.query(UserBlock).count() == 0


def test_block_another_user_api_refuses_an_admin_by_role_id(app, db_session):
    """ROLE_ADMIN is 4 and the comparison at :35 is against that INTEGER.

    The role is named 'role-with-id-4', not 'Admin', so nothing here can be
    satisfied by User.is_admin()'s name path.
    """
    s = _seed_blockers()
    _give_role_with_id(s.target, ROLE_ADMIN)

    with pytest.raises(Exception, match='cannot_block_admin_or_staff'):
        block_another_user(s.target.id, SRC_API, bearer(s.blocker))

    assert db.session.query(UserBlock).count() == 0


def test_block_another_user_api_refuses_staff_by_role_id(app, db_session):
    """The second operand of :35's `or`. Without this test that operand could
    be deleted outright and every other test in this file would stay green."""
    s = _seed_blockers()
    _give_role_with_id(s.target, ROLE_STAFF)

    with pytest.raises(Exception, match='cannot_block_admin_or_staff'):
        block_another_user(s.target.id, SRC_API, bearer(s.blocker))

    assert db.session.query(UserBlock).count() == 0


def test_block_another_user_allows_a_target_holding_an_unprivileged_role(app, db_session):
    """The positive control for the role check.

    Without this, every role-check test above would also pass against a
    version of :35 that refused EVERY user holding any role at all.
    """
    s = _seed_blockers()
    _give_role_with_id(s.target, 2)

    returned = block_another_user(s.target.id, SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(UserBlock).count() == 1


def test_block_another_user_web_refuses_an_admin_without_raising(app, db_session):
    s = _seed_blockers()
    _give_role_with_id(s.target, ROLE_ADMIN)

    with web_ctx(app, s.blocker):
        from flask import session
        returned = block_another_user(s.target.id, SRC_WEB)
        flashed = [message for _category, message in session.get('_flashes', [])]

    assert returned is None
    assert flashed == ['You cannot block admin or staff.']
    assert db.session.query(UserBlock).count() == 0


def test_unblock_another_user_api_removes_the_block(app, db_session):
    s = _seed_blockers()
    db.session.add(UserBlock(blocker_id=s.blocker.id, blocked_id=s.target.id))
    db.session.commit()

    returned = unblock_another_user(s.target.id, SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(UserBlock).count() == 0


def test_unblock_another_user_web_removes_the_block_and_returns_none(app, db_session):
    s = _seed_blockers()
    db.session.add(UserBlock(blocker_id=s.blocker.id, blocked_id=s.target.id))
    db.session.commit()

    with web_ctx(app, s.blocker):
        returned = unblock_another_user(s.target.id, SRC_WEB)

    assert returned is None
    assert db.session.query(UserBlock).count() == 0


def test_unblock_another_user_leaves_someone_elses_block_alone(app, db_session):
    """:74's filter is on BOTH blocker_id and blocked_id.

    A third user's block of the same target is seeded so that a filter
    narrowed to blocked_id alone would delete a row it must not touch. With
    only one block row present, that fault is invisible.
    """
    s = _seed_blockers()
    bystander = make_user(make_instance('other.example'), 'bystander', local=True)
    db.session.add(UserBlock(blocker_id=s.blocker.id, blocked_id=s.target.id))
    db.session.add(UserBlock(blocker_id=bystander.id, blocked_id=s.target.id))
    db.session.commit()

    unblock_another_user(s.target.id, SRC_API, bearer(s.blocker))
    db.session.expire_all()

    remaining = {(b.blocker_id, b.blocked_id)
                 for b in db.session.query(UserBlock).all()}
    assert remaining == {(bystander.id, s.target.id)}


def test_unblock_another_user_is_idempotent(app, db_session):
    """:75's `if existing_block` -- the false arc. No block exists, so the
    body is skipped and :84 still returns the caller's id."""
    s = _seed_blockers()

    returned = unblock_another_user(s.target.id, SRC_API, bearer(s.blocker))

    assert returned == s.blocker.id
    assert db.session.query(UserBlock).count() == 0


def test_unblock_another_user_api_refuses_self(app, db_session):
    s = _seed_blockers()

    with pytest.raises(Exception, match='cannot_unblock_self'):
        unblock_another_user(s.blocker.id, SRC_API, bearer(s.blocker))


def test_unblock_another_user_web_refuses_self_without_raising(app, db_session):
    s = _seed_blockers()

    with web_ctx(app, s.blocker):
        from flask import session
        returned = unblock_another_user(s.blocker.id, SRC_WEB)
        flashed = [message for _category, message in session.get('_flashes', [])]

    assert returned is None
    assert flashed == ['You cannot unblock yourself.']


def test_bot_challenge_user_web_sends_a_message_and_records_the_challenge(app, db_session):
    """The first-challenge path: no existing BotChallenge, so :318 mints a
    fresh uuid and :337-339 records it.

    The uuid is asserted to appear IN the message body rather than merely to
    exist, because :333 interpolates it into the link the recipient has to
    visit -- a challenge row whose uuid never reached the message is useless.
    """
    s = _seed_blockers()

    with web_ctx(app, s.blocker):
        bot_challenge_user(s.target.id, SRC_WEB)

    challenge = db.session.query(BotChallenge).filter_by(user_id=s.target.id).one()
    assert challenge.sent_by == s.blocker.id
    assert len(challenge.uuid) == 49

    message = db.session.query(ChatMessage).one()
    assert f'/bot_challenge/{challenge.uuid}' in message.body


def test_bot_challenge_user_puts_the_recipient_in_the_conversation(app, db_session):
    """:321's `members.append(recipient)`.

    The membership is compared as a SET: `conversation.members` comes back
    through a relationship whose row order no test may depend on.
    """
    s = _seed_blockers()

    with web_ctx(app, s.blocker):
        bot_challenge_user(s.target.id, SRC_WEB)

    conversation = db.session.query(Conversation).one()
    assert {m.id for m in conversation.members} == {s.blocker.id, s.target.id}
    assert conversation.user_id == s.blocker.id


def test_bot_challenge_user_reuses_the_uuid_of_an_unanswered_challenge(app, db_session):
    """:313-316 -- an existing challenge with is_a_bot left NULL.

    The second call must reuse the stored uuid and must NOT insert a second
    BotChallenge row (:337's `is None` is false). The uuid is set to a
    recognisable constant so that "reused" is distinguishable from "minted a
    new one that happens to be 49 characters".
    """
    s = _seed_blockers()
    db.session.add(BotChallenge(user_id=s.target.id, sent_by=s.blocker.id,
                                uuid='reused-uuid-from-the-first-challenge-0000000000000'[:49]))
    db.session.commit()

    with web_ctx(app, s.blocker):
        bot_challenge_user(s.target.id, SRC_WEB)

    assert db.session.query(BotChallenge).filter_by(user_id=s.target.id).count() == 1
    message = db.session.query(ChatMessage).one()
    stored = db.session.query(BotChallenge).filter_by(user_id=s.target.id).one().uuid
    assert f'/bot_challenge/{stored}' in message.body


def test_bot_challenge_user_refuses_someone_who_already_answered(app, db_session):
    """:314's `is_a_bot is False` -- identity, not truthiness.

    is_a_bot is set to False explicitly. The previous test leaves it NULL and
    proceeds, which is the positive control that distinguishes `is False`
    from a plain falsiness check: NULL is falsy too, and a `not
    existing_challenge.is_a_bot` would refuse both.
    """
    s = _seed_blockers()
    db.session.add(BotChallenge(user_id=s.target.id, sent_by=s.blocker.id,
                                uuid='x' * 49, is_a_bot=False))
    db.session.commit()

    with web_ctx(app, s.blocker):
        with pytest.raises(Exception, match='already responded to the challenge'):
            bot_challenge_user(s.target.id, SRC_WEB)

    assert db.session.query(ChatMessage).count() == 0
    assert db.session.query(Conversation).count() == 0


def test_bot_challenge_user_continues_for_a_confirmed_bot(app, db_session):
    """is_a_bot True reaches :316, not :315.

    Without this arm, `is False` at :314 could be replaced by `is not None`
    and every other test here would stay green.
    """
    s = _seed_blockers()
    db.session.add(BotChallenge(user_id=s.target.id, sent_by=s.blocker.id,
                                uuid='y' * 49, is_a_bot=True))
    db.session.commit()

    with web_ctx(app, s.blocker):
        bot_challenge_user(s.target.id, SRC_WEB)

    assert db.session.query(ChatMessage).count() == 1


def test_bot_challenge_user_src_api_bare_call_crashes_on_none_current_user(app, db_session):
    """PINS A DEFECT, under a condition this codebase never actually creates.

    `:320` builds the Conversation from `user` -- the API-authorised caller
    -- but `:322` appends `current_user` instead of the caller. That
    disagreement is real regardless of caller: `grep -rn bot_challenge_user
    app/` shows exactly one call site, `app/user/routes.py:2115`, and it
    hardcodes `src=SRC_WEB`. Nothing in the current tree ever invokes this
    function with `SRC_API`, so this test calls it bare -- directly, with no
    Flask request context -- to exercise the branch at all. This round's
    production budget is the backfill migration (Task 1) and ban_user's four
    `if SRC_WEB:` lines (Task 6), so `:322` is registered rather than fixed
    here.

    OBSERVED, not predicted: outside a request context, flask_login's
    `current_user` resolves to plain `None` here (not an anonymous-user
    proxy), so `conversation.members.append(current_user)` at `:322` appends
    None into the relationship, and the AttributeError below fires during the
    `db.session.commit()` at `:324` -- before `send_message` (`:335`) is ever
    reached. The brief's hypothesis was that the failure would surface inside
    `send_message`'s `user: User = current_user` default; that is not what
    happens: the flush at `:324` fails first. See this task's report for the
    exact probe transcript.

    What this test does NOT show: behaviour under a real request context. If
    a future route wired `SRC_API` to this function, that call would run
    inside an actual Flask request, where flask_login's `_load_user()` sets
    `current_user` to an `AnonymousUserMixin` instance rather than `None` --
    an object that plausibly satisfies `.id` access differently (or fails
    differently) than this bare call's plain `None` does. This test pins the
    bare-call crash it can actually produce; it does not establish what an
    API caller with no session would experience in production, because no
    such caller exists.
    """
    s = _seed_blockers()

    with pytest.raises(AttributeError, match="'NoneType' object has no attribute '_sa_instance_state'"):
        bot_challenge_user(s.target.id, SRC_API, bearer(s.blocker))

    assert db.session.query(BotChallenge).count() == 0
    assert db.session.query(ChatMessage).count() == 0
