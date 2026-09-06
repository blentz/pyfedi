"""`send_post` -- the Celery-path builder and deliverer of an ActivityPub Page.

`app/shared/tasks/pages.py:88-368`. This is the second of two Page builders in
the codebase; the other is `post_to_page` (app/activitypub/util.py:132-219),
reached from the outbox collection view at app/activitypub/routes.py:2033. They
are near-twins and their disagreements are findings D298, D299 and D300.

ENTRY is a direct call. `send_post(post_id, edit=False, session=None)` has no
usable default for `session` -- :89 is `session.query(Post).get(post_id)` -- so
every test here passes `db.session` explicitly.

FOUR EARLY RETURNS stand between entry and the builder at :175, and a test that
wants to reach the builder must clear all four:

  :149-150  `if not community.instance.online(): return`
  :153-154  `if community.local_only or community.private: return`
  :156-158  a CommunityBan row for (user, community)
  :159-161  a remote community whose instance the user blocked, or that is banned

`Instance.online()` is `not (self.dormant or self.gone_forever)`, and both
columns default False (app/models.py:98, :100), so a factory instance is online
without help.

NOTE ON :153, because sub-project 18 relied on the opposite. Setting
`community.local_only = True` does NOT merely skip delivery -- it returns at
:154 before the builder runs at all. That is also why the false arms of :267
and :330 (`if not community.local_only:`) are UNREACHABLE: `community` is bound
once at :91 and never reassigned, so by :267 the flag is always falsy. Those two
arms are registered as unreachable rather than chased.

STOPPING BEFORE THE NETWORK. :336-338 is
`followers = ...; if not followers: return`. A post whose author has no inward
UserFollower rows ends the function there. Combined with a local community that
has no following_instances(), the entire builder runs with zero outbound
requests, and no `http_mock` is needed. Tests that DO reach delivery must give
the sender real keys (`make_user(..., with_keys=True)`), because signing calls
`.encode()` on the private key.

MENTIONS ARE SILENTLY SKIPPED. `search_for_user` (app/user/utils.py:85) is
called at :106 and :112, each inside a bare `except: pass` (:107-108, :113-114).
So a test asserting that a mention produced no notification cannot distinguish
"correctly skipped" from "crashed and swallowed" -- pin the reason, not the
absence.
"""

from types import SimpleNamespace

import pytest

from app import db
from app.constants import (
    NOTIF_MENTION, POST_TYPE_ARTICLE, POST_TYPE_EVENT, POST_TYPE_IMAGE,
    POST_TYPE_LINK, POST_TYPE_POLL, POST_TYPE_VIDEO,
)
from app.models import (
    BannedInstances, CommunityBan, Event, File, Notification, Poll,
    PollChoice, User, UserFollower,
)
from app.shared.tasks.pages import send_post
from tests.factories import (
    make_community, make_community_member, make_instance, make_instance_block,
    make_post, make_user,
)


def _seed(body=None, post_type=POST_TYPE_ARTICLE, url=None, local_community=True):
    """The local instance, a local author, a community, and a post.

    ORDER IS LOAD-BEARING. `make_community` hardcodes `instance_id=1`
    (tests/factories.py) and tests/conftest.py:143 truncates with
    RESTART IDENTITY, so whichever Instance is inserted first gets id 1. The
    local instance is created first here so the community's FK points at it. A
    peer built before this call would capture id 1 and silently make the
    community's instance the peer -- see `_peer` below.

    `local_community=False` gives the community an `ap_id`, which is what
    `Community.is_local()` tests, so :159's guard opens.
    """
    instance = make_instance('test.piefed.local', software='piefed')
    user = make_user(instance, 'author', local=True)
    community = make_community('c1')
    post = make_post(community, user, ap_id='https://test.piefed.local/post/1')
    post.type = post_type
    post.body = body
    post.url = url
    if not local_community:
        community.ap_id = 'c1@peer.example'
    db.session.commit()
    return SimpleNamespace(instance=instance, user=user, community=community,
                           post=post)


def _peer(domain='peer.example', software='lemmy'):
    """A remote Instance. ALWAYS call this AFTER `_seed()` -- see `_seed`'s
    docstring for why the order matters."""
    return make_instance(domain, software=software)


def _send(post, edit=False):
    """`send_post` takes an explicit session; there is no usable default."""
    return send_post(post.id, edit=edit, session=db.session)


# ---------------------------------------------------------------------------
# Mention extraction, :96-123
# ---------------------------------------------------------------------------


def test_a_body_with_no_mentions_skips_the_scanner_entirely(db_session):
    """:97, false arm -- `if post.body:` with a body that is falsy.

    The witness is that no Notification exists: the scanner never runs, so
    :126's loop has nothing to iterate.
    """
    s = _seed(body=None)
    _send(s.post)

    assert Notification.query.count() == 0


def test_a_local_mention_resolves_and_is_notified(db_session):
    """:98-108 and :115-123. The local arm of :102's host comparison.

    `current_app.config['SERVER_NAME']` is 'test.piefed.local'
    (tests/conftest.py:69), which is what `_seed` gives the local instance, so
    `@mentioned@test.piefed.local` takes :103-108.
    """
    s = _seed(body='hello @mentioned@test.piefed.local')
    mentioned = make_user(s.instance, 'mentioned', local=True)
    assert len({s.user.id, mentioned.id}) == 2

    _send(s.post)

    notifications = Notification.query.filter_by(user_id=mentioned.id).all()
    assert len(notifications) == 1
    assert notifications[0].notif_type == NOTIF_MENTION
    assert notifications[0].subtype == 'post_mention'


def test_an_author_mentioning_themselves_is_not_notified(db_session):
    """:104, false arm -- `if user_name != user.user_name:`.

    The author is 'author', so `@author@test.piefed.local` is skipped without
    ever calling `search_for_user`.
    """
    s = _seed(body='hello @author@test.piefed.local')
    _send(s.post)

    assert Notification.query.count() == 0


def test_a_banned_remote_host_mention_is_skipped_via_the_remote_except(db_session):
    """:112-114 -- the only reachable bare `except: pass` in mention
    resolution. This test replaces `test_an_unresolvable_local_mention_is_
    skipped_silently`, whose premise (that :107-108's local-arm except is
    reachable) was wrong.

    THE LOCAL ARM'S EXCEPT AT :107-108 IS UNREACHABLE. For a bare local name
    (no `@`), `search_for_user` (app/user/utils.py:85) hits :91-92
    (`server = ''`), so :94's `if server:` is False and :98 -- the function's
    only `raise` -- can never fire on that path. A no-match local lookup
    instead falls through :103 (`if already_exists:`, False) and :105
    (`elif not allow_fetch:`, False -- `allow_fetch` defaults True) to
    :108-109's clean `return None`. A nonexistent local mention returns None
    without ever raising, so :107-108 is dead code on this arm -- registered
    here as a finding for the residual sweep rather than exercised.

    THE REMOTE ARM CAN RAISE. `search_for_user` for `name@host` takes
    app/user/utils.py:94's true branch, and :98 raises when the host has a
    `BannedInstances` row. That reaches pages.py:112's call inside the
    try/except, and :113-114 swallows it. If `_send` propagated that
    exception uncaught, this test would fail -- passing is the witness that
    the except actually fires.
    """
    s = _seed()
    db.session.add(BannedInstances(domain='peer.example', reason='test'))
    db.session.commit()
    s.post.body = 'hello @someone@peer.example'
    db.session.commit()

    _send(s.post)

    assert Notification.query.count() == 0


def test_the_same_local_user_mentioned_twice_is_added_once(db_session):
    """:116-123, the dedup. :118's first disjunct -- a local recipient has
    `ap_id` None, so the comparison falls to `user_name`."""
    s = _seed(body='@mentioned@test.piefed.local and again @mentioned@test.piefed.local')
    mentioned = make_user(s.instance, 'mentioned', local=True)
    assert mentioned.ap_id is None

    _send(s.post)

    assert Notification.query.filter_by(user_id=mentioned.id).count() == 1


def test_two_different_local_users_are_both_added(db_session):
    """:122-123, the true arm of `if add_recipient:` on the second pass --
    the dedup must NOT suppress a genuinely different recipient."""
    s = _seed(body='@alpha@test.piefed.local and @beta@test.piefed.local')
    alpha = make_user(s.instance, 'alpha', local=True)
    beta = make_user(s.instance, 'beta', local=True)
    assert len({s.user.id, alpha.id, beta.id}) == 3

    _send(s.post)

    assert Notification.query.filter_by(user_id=alpha.id).count() == 1
    assert Notification.query.filter_by(user_id=beta.id).count() == 1


# ---------------------------------------------------------------------------
# Mention notification, :125-147
# ---------------------------------------------------------------------------


def test_a_remote_recipient_gets_no_local_notification(db_session):
    """:127, false arm -- `if recipient.is_local():`.

    `User.is_local()` tests `ap_id`, which `make_user(local=False)` sets. The
    recipient is still collected into `recipients` (and so still reaches :172's
    tag loop), but no Notification row is written for them.
    """
    s = _seed()
    peer = _peer()
    s.post.body = 'hello @remoteuser@peer.example'
    db.session.commit()
    remote = make_user(peer, 'remoteuser', local=False)

    _send(s.post)

    assert Notification.query.filter_by(user_id=remote.id).count() == 0


def test_an_edit_reuses_the_existing_mention_notification(db_session):
    """:128-129 and :132's false arm. On an edit, an existing Notification with
    the same url suppresses a second one -- so the count stays 1 across a
    create followed by an edit."""
    s = _seed(body='hello @mentioned@test.piefed.local')
    mentioned = make_user(s.instance, 'mentioned', local=True)

    _send(s.post, edit=False)
    assert Notification.query.filter_by(user_id=mentioned.id).count() == 1

    _send(s.post, edit=True)
    assert Notification.query.filter_by(user_id=mentioned.id).count() == 1


def test_an_edit_with_no_prior_notification_creates_one(db_session):
    """:128-129 with the query returning None, so :132's TRUE arm still runs.
    This is the arm that distinguishes "edit suppresses" from "edit never
    notifies"."""
    s = _seed(body='hello @mentioned@test.piefed.local')
    mentioned = make_user(s.instance, 'mentioned', local=True)

    _send(s.post, edit=True)

    assert Notification.query.filter_by(user_id=mentioned.id).count() == 1


def test_the_mention_notification_carries_its_targets_and_bumps_the_unread_count(db_session):
    """:133-147. The targets dict at :133-138 and the counter at :145.

    `author_user_name` at :137 is a conditional expression; coverage.py emits no
    arc for one (tests/README.md fact 87), so both arms need named tests. This
    takes the `user_name` arm -- a local author has `ap_id` None.
    """
    s = _seed(body='hello @mentioned@test.piefed.local')
    mentioned = make_user(s.instance, 'mentioned', local=True)
    before = mentioned.unread_notifications
    assert s.user.ap_id is None

    _send(s.post)

    notification = Notification.query.filter_by(user_id=mentioned.id).one()
    assert notification.targets['gen'] == '0'
    assert notification.targets['post_id'] == s.post.id
    assert notification.targets['author_user_name'] == 'author'
    db.session.expire(mentioned)
    assert mentioned.unread_notifications == before + 1


def test_the_mention_notification_uses_ap_id_when_the_author_has_one(db_session):
    """:137, the OTHER arm of the conditional expression."""
    s = _seed(body='hello @mentioned@test.piefed.local')
    s.user.ap_id = 'author@peer.example'
    db.session.commit()
    mentioned = make_user(s.instance, 'mentioned', local=True)

    _send(s.post)

    notification = Notification.query.filter_by(user_id=mentioned.id).one()
    assert notification.targets['author_user_name'] == 'author@peer.example'


# ---------------------------------------------------------------------------
# The four early returns, :149-161
# ---------------------------------------------------------------------------


def test_a_dormant_community_instance_stops_before_the_builder(db_session):
    """:149-150. `Instance.online()` is `not (self.dormant or self.gone_forever)`
    (app/models.py:118-119), and both columns default False (app/models.py:98,
    :100), so this must be set explicitly.

    The witness is that the mention notification from :125-147 DID land. That
    is enough to distinguish an early return here from `send_post` never having
    been called at all -- a never-called function would leave zero Notification
    rows. It does NOT, on its own, distinguish an early return here from the
    function running all the way to completion (nothing later in this test's
    setup would raise if it did) -- only that narrower claim is made.
    """
    s = _seed(body='hello @mentioned@test.piefed.local')
    mentioned = make_user(s.instance, 'mentioned', local=True)
    s.instance.dormant = True
    db.session.commit()

    _send(s.post)

    assert Notification.query.filter_by(user_id=mentioned.id).count() == 1


def test_a_gone_forever_instance_also_stops(db_session):
    """:149-150 via the second disjunct of `online()`. The two columns are
    separately load-bearing. See the previous test's docstring for what the
    Notification-count witness does and does not establish."""
    s = _seed(body='hello @mentioned@test.piefed.local')
    mentioned = make_user(s.instance, 'mentioned', local=True)
    s.instance.gone_forever = True
    db.session.commit()

    _send(s.post)

    assert Notification.query.filter_by(user_id=mentioned.id).count() == 1


@pytest.mark.parametrize('flag', ['local_only', 'private'])
def test_a_local_only_or_private_community_stops_before_the_builder(db_session, flag):
    """:153-154, both disjuncts.

    THIS IS THE RETURN SUB-PROJECT 18 WAS ACTUALLY USING. Its tests set
    `community.local_only = True` believing it skipped delivery at :267; it in
    fact returns here, before the builder. That is also why :267's and :330's
    false arms are unreachable -- see this file's module docstring.
    """
    s = _seed(body='hello @mentioned@test.piefed.local')
    mentioned = make_user(s.instance, 'mentioned', local=True)
    setattr(s.community, flag, True)
    db.session.commit()

    _send(s.post)

    assert Notification.query.filter_by(user_id=mentioned.id).count() == 1


def test_a_banned_author_stops_before_the_builder(db_session):
    """:156-158. A CommunityBan row for (author, community)."""
    s = _seed(body='hello @mentioned@test.piefed.local')
    mentioned = make_user(s.instance, 'mentioned', local=True)
    db.session.add(CommunityBan(user_id=s.user.id, community_id=s.community.id))
    db.session.commit()

    _send(s.post)

    assert Notification.query.filter_by(user_id=mentioned.id).count() == 1


def test_a_remote_community_on_a_blocked_instance_stops(db_session):
    """:159-161, first disjunct. :159 opens only for a community that is NOT
    local -- `Community.is_local()` (app/models.py:795) tests `ap_id`, which
    `_seed` sets under `local_community=False`.

    Uses the `make_instance_block` factory (tests/factories.py:576) rather than
    constructing `InstanceBlock` inline -- it exists for exactly this row and
    keeps the model import out of the test module.
    """
    s = _seed(body='hello @mentioned@test.piefed.local', local_community=False)
    mentioned = make_user(s.instance, 'mentioned', local=True)
    peer = _peer()
    s.community.instance_id = peer.id
    db.session.commit()
    make_instance_block(s.user, peer)

    _send(s.post)

    assert Notification.query.filter_by(user_id=mentioned.id).count() == 1


# ---------------------------------------------------------------------------
# :163-174 -- deferred to Task 6
# ---------------------------------------------------------------------------
#
# pages.py:163-168 (the type dispatch: 'Question' / 'Event' / 'Page') and
# :172-174 (appending each recipient to `tag` and `cc`) all write only to
# locals -- `type`, `tag`, `cc` -- that are read into the `page`/`create`
# dicts at :185, :188-189 and never persisted or otherwise exposed. With the
# communities and posts this file's tests build, the builder's own outbound
# calls at :291-304 and :330-334 are never entered either: no peer instance
# follows the local community and no `UserFollower` row exists, so
# `send_post_request` is never invoked, leaving nothing -- mocked or real --
# to inspect for `type`, `tag` or `cc`.
#
# The brief's `test_the_activity_type_follows_the_post_type` and
# `test_a_mentioned_recipient_lands_in_both_tag_and_cc` would therefore only
# have been able to assert that `_send` completed without raising for each
# post type / for a mentioned recipient -- an assertion that cannot
# distinguish the behaviour it names from any other code path that also
# completes without raising. Per this project's rubric that is a defect, so
# both are dropped here rather than kept with a softened docstring. Task 1
# already established this as the recorded route for an unobservable arm
# (pages.py:109-114's success path); Task 6, which builds the
# outbound-delivery capture that makes the Create body's `type`, `tag` and
# `cc` visible, is where these two arms belong.
