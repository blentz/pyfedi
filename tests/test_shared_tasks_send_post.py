"""`send_post` -- the Celery-path builder and deliverer of an ActivityPub Page.

`app/shared/tasks/pages.py:88-371`. This is the second of two Page builders in
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
:154 before the builder runs at all. That is also why the false arms of :270
and :333 (`if not community.local_only:`) are UNREACHABLE: `community` is bound
once at :91 and never reassigned, so by :270 the flag is always falsy. Those two
arms are registered as unreachable rather than chased.

STOPPING BEFORE THE NETWORK. :339-341 is
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

import json
import socket
from types import SimpleNamespace

import pytest

from app import db
from app.constants import (
    NOTIF_MENTION, POST_TYPE_ARTICLE, POST_TYPE_EVENT, POST_TYPE_IMAGE,
    POST_TYPE_LINK, POST_TYPE_POLL, POST_TYPE_VIDEO,
)
from app.models import (
    ActivityPubLog, BannedInstances, CommunityBan, Event, File, Notification,
    Poll, PollChoice, User, UserFollower,
)
from app.shared.tasks.pages import send_post
from tests.factories import (
    make_community, make_community_member, make_instance, make_instance_block,
    make_post, make_user,
)


def _seed(body=None, post_type=POST_TYPE_ARTICLE, url=None, local_community=True,
          with_keys=False):
    """The local instance, a local author, a community, and a post.

    ORDER IS LOAD-BEARING. `make_community` hardcodes `instance_id=1`
    (tests/factories.py) and tests/conftest.py:143 truncates with
    RESTART IDENTITY, so whichever Instance is inserted first gets id 1. The
    local instance is created first here so the community's FK points at it. A
    peer built before this call would capture id 1 and silently make the
    community's instance the peer -- see `_peer` below.

    `local_community=False` gives the community a genuinely remote AP
    identity. `Community.is_local()` (app/models.py:795-796) is a
    DISJUNCTION -- `self.ap_id is None or self.profile_id().startswith(
    SERVER_URL)` -- and `profile_id()` (app/models.py:787-789) reads
    `ap_profile_id`, falling back to a `SERVER_URL`-based value only when
    `ap_profile_id` is unset. `make_community`'s default `host` is
    `test.piefed.local`, the test `SERVER_URL`'s host, so it always sets
    `ap_profile_id` to a `SERVER_URL`-prefixed value -- meaning the second
    disjunct is true regardless of `ap_id`. Setting `ap_id` alone therefore
    left `is_local()` returning True; :159's guard never opened. To close
    the second disjunct too, `ap_profile_id`, `ap_public_url` and
    `ap_followers_url` are overridden onto `peer.example` here, alongside
    `ap_domain` for consistency with a real remote community's row shape.

    `with_keys=True` is passed straight through to `make_user`
    (tests/factories.py:39), giving the author a real RSA keypair. Off by
    default because generation costs roughly a second; required by any test
    that reaches `_remote_inbox` below, because signing calls `.encode()` on
    `user.private_key` and a keyless author dies there before any request.
    """
    instance = make_instance('test.piefed.local', software='piefed')
    user = make_user(instance, 'author', local=True, with_keys=with_keys)
    community = make_community('c1')
    post = make_post(community, user, ap_id='https://test.piefed.local/post/1')
    post.type = post_type
    post.body = body
    post.url = url
    if not local_community:
        community.ap_id = 'c1@peer.example'
        community.ap_profile_id = 'https://peer.example/c/c1'
        community.ap_public_url = 'https://peer.example/c/c1'
        community.ap_followers_url = 'https://peer.example/c/c1/followers'
        community.ap_domain = 'peer.example'
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


PEER_INBOX = 'https://peer.example/c/c1/inbox'

_REAL_GETADDRINFO = socket.getaddrinfo

# The address a `.example` host resolves to under the stub below. Any globally
# routable literal will do -- the only property app/utils.py:5530 reads off it
# is `is_global` -- but a real one keeps the fixture honest if anyone ever
# prints it. 93.184.216.34 was example.com's address for years.
_EXAMPLE_TLD_ADDRESS = '93.184.216.34'


def _getaddrinfo_without_the_network(host, *args, **kwargs):
    """`socket.getaddrinfo`, answering for `.example` hosts without a resolver.

    Everything else is delegated to the real function unchanged, so nothing in
    the process that genuinely needs to resolve a name is affected.
    """
    if isinstance(host, str) and (host == 'example' or host.endswith('.example')):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '',
                 (_EXAMPLE_TLD_ADDRESS, 0))]
    return _REAL_GETADDRINFO(host, *args, **kwargs)


@pytest.fixture(autouse=True)
def _peer_example_resolves_without_a_resolver(monkeypatch):
    """Keep `_remote_inbox`'s delivery off the network's DNS resolver.

    THIS IS NOT OPTIONAL AND MUST NOT BE DELETED AS UNNECESSARY. Signing a
    delivery runs `is_invalid_get_request_uri` (app/utils.py:5494-5536), called
    from `HttpSignature.signed_request` at app/activitypub/signature.py:442 --
    for POST as well as GET. Its `current_app.debug` short-circuit at
    app/utils.py:5495-5496 does NOT fire here: neither config.py nor
    tests/conftest.py sets DEBUG, so `app.debug` is False. Control therefore
    reaches app/utils.py:5520, `socket.getaddrinfo(f.host, None)`, and a real
    lookup of `peer.example` goes out to whatever resolver the machine has.

    That lookup is fail-open (app/utils.py:5519-5522 returns False on
    `gaierror`/`timeout`), which is exactly why it is dangerous rather than
    merely slow: on a machine whose resolver is fast and NXDOMAINs it costs
    nothing and nobody notices, but on one that is slow, that hangs, or that
    hijacks NXDOMAIN into a wildcard A record pointing somewhere private, the
    same tests stall or start failing at app/utils.py:5530-5531's `is_global`
    check. A suite whose outcome depends on the host's resolver is not
    hermetic, and `_remote_inbox` is shared by every delivery test in this
    file.

    The stub is deliberately the NARROWEST thing that removes the lookup: it
    replaces only the resolver, for only the reserved-by-RFC-2606 `.example`
    TLD this file's peers live under, delegating every other host to the real
    `socket.getaddrinfo`. `is_invalid_get_request_uri` itself still runs in
    full -- the empty-host, `.local`, scheme and `is_global` checks all execute
    against a real answer -- so the production path is exercised, not skipped.
    The canned address is globally routable, so the verdict is False, which is
    the same verdict the fail-open path produces today; this makes that verdict
    deterministic rather than a property of the machine.

    Autouse, because `_remote_inbox` is a plain function and cannot request a
    fixture: this is how it gets the isolation without growing a parameter that
    every calling test would have to remember. It is inert for the tests that
    never deliver -- they resolve nothing -- and `monkeypatch` undoes it after
    each test, so the patch never outlives the test that needed it.
    """
    monkeypatch.setattr(socket, 'getaddrinfo', _getaddrinfo_without_the_network)


def _remote_inbox(s, http_mock, inbox=PEER_INBOX):
    """THE CAPTURE MECHANISM for anything `send_post` writes into `page` or
    `create`. Returns the respx route the outbound Create lands on; pair it
    with `_sent_activity` below.

    Everything the builder produces between :163 and :262 lives in two locals,
    `page` and `create`, and is never persisted. The only way to read them is
    to let the function actually deliver. pages.py:305-307 is the cheapest
    route to that: the `else` arm of :271, taken when the community is NOT
    local, calls `send_post_request(community.ap_inbox_url, create, ...)` with
    `create['object']` still bound to `page` (:257).

    Three things have to be true for that call to become an observable HTTP
    request, and this helper plus its caller supply all three:

      1. The community must be remote -- `_seed(local_community=False)`, which
         closes both disjuncts of `Community.is_local()` (app/models.py:
         795-796). See `_seed`'s docstring.
      2. It must have an `ap_inbox_url`. `_seed` does NOT set one, and
         `make_community` leaves it None; with None, `post_request`
         (app/activitypub/signature.py:109-111) short-circuits to an
         "empty uri" `ActivityPubLog` failure row and never reaches the
         transport. This helper sets it, and registers exactly that URL with
         `http_mock`.
      3. The author must have a keypair -- `_seed(with_keys=True)` -- because
         `HttpSignature.signed_request` (app/activitypub/signature.py:472)
         signs with `user.private_key` before issuing the request at :494.

    Delivery is synchronous here: `send_post_request`
    (app/activitypub/signature.py:82-91) calls `post_request.delay(...)` at
    :89, and tests/conftest.py:106-107 runs Celery eagerly, so the POST has
    already happened by the time `_send` returns.

    WHY THE HTTP BODY AND NOT A MONKEYPATCHED RECORDER. `page` keeps being
    mutated after delivery -- :313 deletes `name`, :315-330 rewrite `content`
    and `type`, :331 adds `inReplyTo` -- and `create['object']` is the same
    object throughout. A recorder that kept the dict would therefore be read
    back in its post-:331 state, not the state that was sent. respx captures
    the serialized request bytes at :494, so `_sent_activity` returns a true
    snapshot of what left the process.

    `http_mock` is `assert_all_called=True` (tests/conftest.py:287-295), so the
    route registered here failing to fire is itself a test failure -- a test
    using this helper cannot silently stop delivering.

    NO NETWORK IS TOUCHED, including DNS. Signing the delivery runs
    `is_invalid_get_request_uri`, which resolves the inbox host for real at
    app/utils.py:5520 and fails open at :5521-5522 -- so without help these
    tests would depend on the machine's resolver. The autouse
    `_peer_example_resolves_without_a_resolver` fixture above answers for
    `.example` hosts in-process; read its docstring before changing `inbox` to
    a host outside that TLD, which would put the live lookup back.
    """
    s.community.instance_id = _peer().id
    s.community.ap_inbox_url = inbox
    db.session.commit()
    return http_mock.post(inbox).respond(200, json={})


def _sent_activity(route, index=-1):
    """The JSON body of the request `route` captured, decoded from the bytes
    that were actually sent. Defaults to the most recent call."""
    return json.loads(route.calls[index].request.content)


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
    `community.local_only = True` believing it skipped delivery at :270; it in
    fact returns here, before the builder. That is also why :270's and :333's
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
    local -- `Community.is_local()` (app/models.py:795-796) is a disjunction,
    and `_seed(local_community=False)` now closes both of its disjuncts (see
    `_seed`'s docstring for why setting `ap_id` alone was not enough). The
    `assert s.community.is_local() is False` below is the guard-opens proof
    the fix round asked for -- it fails loudly if this ever regresses to
    closing again, rather than passing for the wrong reason.

    Uses the `make_instance_block` factory (tests/factories.py:576) rather than
    constructing `InstanceBlock` inline -- it exists for exactly this row and
    keeps the model import out of the test module.

    STRONGER WITNESS THAN THE OTHER THREE EARLY-RETURN TESTS. A mention
    notification landing only proves `send_post` was invoked and ran past
    :147 -- it does not distinguish this early return from the function
    running to completion, since completion also leaves exactly one
    notification. But completion here would additionally call
    `send_post_request(community.ap_inbox_url, ...)` at :306 (the remote,
    non-`is_local` arm of :271) with `ap_inbox_url` left unset (None) by
    `_seed`/`make_community`; `post_request` (app/activitypub/signature.py:
    109-111) logs that as an `ActivityPubLog` failure row rather than raising.
    So `ActivityPubLog.query.count() == 0` DOES distinguish the two: it is
    zero only if the function returned at :161 before reaching :306.
    """
    s = _seed(body='hello @mentioned@test.piefed.local', local_community=False)
    assert s.community.is_local() is False
    mentioned = make_user(s.instance, 'mentioned', local=True)
    peer = _peer()
    s.community.instance_id = peer.id
    db.session.commit()
    make_instance_block(s.user, peer)

    _send(s.post)

    assert Notification.query.filter_by(user_id=mentioned.id).count() == 1
    assert ActivityPubLog.query.count() == 0


# ---------------------------------------------------------------------------
# :163-174 -- deferred to Task 6
# ---------------------------------------------------------------------------
#
# pages.py:163-168 (the type dispatch: 'Question' / 'Event' / 'Page') and
# :172-174 (appending each recipient to `tag` and `cc`) all write only to
# locals -- `type`, `tag`, `cc` -- that are read into the `page`/`create`
# dicts at :185, :188-189 and never persisted or otherwise exposed. With the
# communities and posts this file's tests build, the builder's own outbound
# calls at :294-307 and :333-337 are never entered either: no peer instance
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
#
# UPDATE, Task 3: that capture now exists -- `_remote_inbox` / `_sent_activity`
# in the prelude above. Task 6 should reuse it rather than build a second one.


# ---------------------------------------------------------------------------
# The Page builder's image-url fallback, :213-221
# ---------------------------------------------------------------------------
#
# :213-221 is
#
#   213:     if post.image_id:
#   214:         image_url = ''
#   215:         if post.image.source_url:
#   216:             image_url = post.image.source_url
#   217:         elif post.image.file_path:
#   218:             image_url = post.image.file_path.replace('app/static/', ...)
#   219:         elif post.image.thumbnail_path:
#   220:             image_url = post.image.thumbnail_path.replace('app/static/', ...)
#   221:         page['image'] = {'type': 'Image', 'url': image_url}
#
# The rewrite at :218 and :220 substitutes `current_app.config['SERVER_URL']`
# + '/static/' for the leading 'app/static/'. SERVER_URL is built at
# app/__init__.py:132-135 from HTTP_PROTOCOL (default 'https', config.py:52)
# and SERVER_NAME, which tests/conftest.py:69 pins to 'test.piefed.local' --
# so the expected strings below are spelled out in full rather than rebuilt
# from the same config value the production code reads.
#
# `page` is a local, so each of these four asserts on the delivered Create's
# `object.image` instead; see `_remote_inbox`.


def _attach_image(post, **columns):
    """A File on `post`, with only the columns the caller names set.

    :215-220 is a three-step fallback over `source_url`, `file_path` and
    `thumbnail_path` (app/models.py:373, :368 and :374), so each test must
    leave the earlier columns unset for its own arm to be reached.
    """
    f = File(**columns)
    db.session.add(f)
    db.session.commit()
    post.image_id = f.id
    db.session.commit()
    return f


def _image_of(route):
    """The `image` member of the delivered Create's Page object, :221."""
    return _sent_activity(route)['object']['image']


def test_the_image_url_prefers_the_source_url(db_session, http_mock):
    """:215-216, the first arm. `source_url` wins over the other two, which
    are both set here precisely so a mutation that reordered the chain or
    dropped :215's guard would deliver one of their values instead."""
    s = _seed(post_type=POST_TYPE_LINK, url='https://example.com/a',
              local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)
    _attach_image(s.post, source_url='https://example.com/pic.png',
                  file_path='app/static/posts/x.png',
                  thumbnail_path='app/static/posts/x_thumb.png')

    _send(s.post)

    assert _image_of(route) == {'type': 'Image',
                                'url': 'https://example.com/pic.png'}


def test_the_image_url_falls_back_to_the_file_path(db_session, http_mock):
    """:217-218, reached only when `source_url` is falsy. The stored path is
    rewritten from 'app/static/' to the server's static URL, so the delivered
    value is NOT the column value -- asserting the rewritten string is what
    distinguishes this arm from one that emitted `file_path` verbatim."""
    s = _seed(post_type=POST_TYPE_LINK, url='https://example.com/a',
              local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)
    _attach_image(s.post, source_url=None, file_path='app/static/posts/x.png')

    _send(s.post)

    assert _image_of(route) == {
        'type': 'Image',
        'url': 'https://test.piefed.local/static/posts/x.png'}


def test_the_image_url_falls_back_to_the_thumbnail_path(db_session, http_mock):
    """:219-220, reached only when both `source_url` and `file_path` are
    falsy. Same 'app/static/' rewrite as :218, over the thumbnail column."""
    s = _seed(post_type=POST_TYPE_LINK, url='https://example.com/a',
              local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)
    _attach_image(s.post, source_url=None, file_path=None,
                  thumbnail_path='app/static/posts/x_thumb.png')

    _send(s.post)

    assert _image_of(route) == {
        'type': 'Image',
        'url': 'https://test.piefed.local/static/posts/x_thumb.png'}


def test_the_image_url_stays_empty_when_the_file_has_no_paths(db_session, http_mock):
    """:219's false arm -- all three columns falsy, so `image_url` keeps the
    `''` assigned at :214 and :221 emits it. The key is that :221 runs at all:
    an empty `url` is still a delivered `image` member, which is what
    separates this from a post with no `image_id` (:213 false), where the
    `image` key is absent entirely."""
    s = _seed(post_type=POST_TYPE_LINK, url='https://example.com/a',
              local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)
    _attach_image(s.post, source_url=None, file_path=None, thumbnail_path=None)

    _send(s.post)

    assert _image_of(route) == {'type': 'Image', 'url': ''}


# ---------------------------------------------------------------------------
# D299 -- the image attachment, :180-181
# ---------------------------------------------------------------------------


def test_an_image_post_with_no_image_row_does_not_crash(db_session):
    """D299. :181 dereferences `post.image` under `elif post.type ==
    POST_TYPE_IMAGE` with NO image_id check.

    Before the fix this raises `AttributeError: 'NoneType' object has no
    attribute 'source_url'`. The state is ordinary, not contrived:
    `edit_post` produces a POST_TYPE_IMAGE post with `image_id` None whenever
    the image path at app/shared/post.py:601-608 is not taken.

    :213 in this same function, thirty-two lines below, already guards the same
    dereference with `if post.image_id:`, and
    app/activitypub/util.py:172 guards it in the sibling builder.

    NO DELIVERY, AND NO `http_mock`, DELIBERATELY. The builder at :177-181 runs
    long before the outbound calls at :294-337, so the crash this test names
    happens with a local community and zero requests. Adding a registered route
    would make `http_mock`'s `assert_all_called=True` (tests/conftest.py:288-295)
    raise its own teardown failure alongside the AttributeError, obscuring the
    very failure text that proves the test reaches :181.
    """
    s = _seed(post_type=POST_TYPE_IMAGE)
    assert s.post.image_id is None

    _send(s.post)


def _attachment_of(route):
    """The `attachment` member of the delivered Create's Page object, :197."""
    return _sent_activity(route)['object']['attachment']


def test_an_image_post_with_an_image_row_still_gets_its_attachment(db_session, http_mock):
    """:180-181, the true arm -- the guard must not suppress a real image.

    Without this, `elif False:` would pass the test above and lose the feature,
    so the assertion is on the delivered `attachment`, not on completing
    without raising. `attachment` is a local read into `page` at :197 and never
    persisted, so this reads it back off the wire via `_remote_inbox` /
    `_sent_activity` -- the same capture the :213-221 tests above use.
    """
    s = _seed(post_type=POST_TYPE_IMAGE, local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)
    _attach_image(s.post, source_url='https://example.com/pic.png',
                  alt_text='a picture')

    _send(s.post)

    assert _attachment_of(route) == [{'type': 'Image',
                                      'url': 'https://example.com/pic.png',
                                      'name': 'a picture'}]


def test_a_non_image_post_with_an_image_row_gets_no_image_attachment(db_session, http_mock):
    """:180's TYPE test, isolated from its image_id test.

    The post is an ARTICLE that nonetheless carries an `image_id`, which is the
    only shape that separates `elif post.type == POST_TYPE_IMAGE and
    post.image_id:` from a mutant that kept only `elif post.image_id:`. The
    four :213-221 tests above all attach an image to a LINK post, and a LINK is
    caught by :178 before :180 is ever evaluated, so none of them can see that
    mutation; an ARTICLE reaches :180 and does.

    `page['image']` is still emitted here (:213 is true), and is asserted
    alongside the empty `attachment` so the test cannot pass by the image
    simply having failed to attach.
    """
    s = _seed(post_type=POST_TYPE_ARTICLE, local_community=False,
              with_keys=True)
    route = _remote_inbox(s, http_mock)
    _attach_image(s.post, source_url='https://example.com/pic.png',
                  alt_text='a picture')

    _send(s.post)

    assert _attachment_of(route) == []
    assert _image_of(route) == {'type': 'Image',
                                'url': 'https://example.com/pic.png'}


# ---------------------------------------------------------------------------
# D298 -- ap_datetime on nullable timestamps, :224-225, :233-236
# ---------------------------------------------------------------------------
#
# `ap_datetime` (app/utils.py:2293-2294) is one statement,
# `return date_time.isoformat() + '+00:00'`, with no None guard, and it has 29
# call sites. The fix is therefore at the three callers here, not in
# `ap_datetime`: most of the other 28 pass a non-nullable column, and making
# `ap_datetime` return None would put `"endTime": null` on the wire, which a
# peer cannot tell apart from a missing value without knowing our schema. An
# ABSENT key is unambiguous, and it is what the sibling builder already does --
# app/activitypub/util.py:168 guards its own `updated` key by omitting it.
#
# That is why every crash test below also asserts the key is ABSENT. Omission
# is the whole of the arbitration, and nothing else in this file defends it: a
# guard that emitted None instead would pass a test that only checked for the
# absence of a traceback.
#
# THE SAME THREE READS EXIST UNFIXED IN THE OUTBOX BUILDER, at
# app/activitypub/util.py:195, :200 and :201. They are reached from the outbox
# collection view (app/activitypub/routes.py:2033), not from this Celery task,
# and stay registered as D298.


def _page_of(route):
    """The Page object of the delivered Create, as it left the process.

    Read it off the wire rather than off `page`: :314-331 keep mutating that
    same dict after delivery. See `_remote_inbox`'s docstring.
    """
    return _sent_activity(route)['object']


def test_a_poll_with_no_end_time_does_not_crash(db_session, http_mock):
    """D298 at :225. `Poll.end_poll` (app/models.py:3782) is nullable, and
    `edit_post` writes it only under `if 'end_poll' in poll_data and
    poll_data['end_poll']` (app/shared/post.py:687), so a poll created or
    edited with no end time reaches this call with None.

    Before the fix this raised, verbatim:
    `AttributeError: 'NoneType' object has no attribute 'isoformat'`
    at app/utils.py:2294, from app/shared/tasks/pages.py:224.

    The `endTime` assertion is the load-bearing half. Completing without
    raising only proves the read was skipped; it does not prove the key was
    omitted rather than emitted as null, which is the choice being made here.
    """
    s = _seed(post_type=POST_TYPE_POLL, local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)
    db.session.add(Poll(post_id=s.post.id, end_poll=None, mode='single'))
    db.session.commit()

    _send(s.post)

    page = _page_of(route)
    assert 'endTime' not in page
    assert page['type'] == 'Question'


def test_an_event_with_no_start_does_not_crash(db_session, http_mock):
    """D298 at :234. `Event.start` (app/models.py:3841) is nullable, and
    `edit_post` writes it only under `if 'start' in event_data`
    (app/shared/post.py:703-704).

    Before the fix this raised, verbatim:
    `AttributeError: 'NoneType' object has no attribute 'isoformat'`
    at app/utils.py:2294, from app/shared/tasks/pages.py:232.

    `timezone` is set here because `edit_post` always writes one --
    app/shared/post.py:708 is `event_data.get('timezone', 'UTC')`, with a
    default -- so a start-less event on the reachable path still has a
    timezone. That matters: it is what makes :322 a genuine `start`-only
    guard rather than one covering for a second missing column.
    """
    s = _seed(post_type=POST_TYPE_EVENT, local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)
    db.session.add(Event(post_id=s.post.id, start=None, end=None,
                         timezone='UTC'))
    db.session.commit()

    _send(s.post)

    page = _page_of(route)
    assert 'startTime' not in page
    assert 'endTime' not in page
    assert page['type'] == 'Event'


def test_an_event_with_a_start_but_no_end_does_not_crash(db_session, http_mock):
    """D298 at :236, reachable only once :233 is guarded. `Event.end`
    (app/models.py:3842) is nullable and `edit_post` writes it only under
    `if 'end' in event_data and event_data['end']` (app/shared/post.py:705).

    Before the fix this raised, verbatim:
    `AttributeError: 'NoneType' object has no attribute 'isoformat'`
    at app/utils.py:2294, from app/shared/tasks/pages.py:233.

    SEPARATE FROM THE TEST ABOVE BECAUSE THE TWO GUARDS ARE SEPARATELY
    LOAD-BEARING: guarding `start` alone leaves this shape crashing on the
    next statement. `startTime` is asserted present here as well, so the test
    cannot pass by both guards having closed.
    """
    from datetime import datetime
    s = _seed(post_type=POST_TYPE_EVENT, local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)
    db.session.add(Event(post_id=s.post.id, start=datetime(2030, 6, 1, 9, 0),
                         end=None, timezone='UTC'))
    db.session.commit()

    _send(s.post)

    page = _page_of(route)
    assert page['startTime'] == '2030-06-01T09:00:00+00:00'
    assert 'endTime' not in page


def test_a_poll_with_an_end_time_still_emits_endTime(db_session, http_mock):
    """:224's true arm. Without this, `if False:` would pass the crash test
    above and silently drop `endTime` for every poll that has one."""
    from datetime import datetime
    s = _seed(post_type=POST_TYPE_POLL, local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)
    db.session.add(Poll(post_id=s.post.id, end_poll=datetime(2030, 6, 1, 12, 0),
                        mode='single'))
    db.session.commit()

    _send(s.post)

    assert _page_of(route)['endTime'] == '2030-06-01T12:00:00+00:00'


def test_an_event_with_both_times_still_emits_both_keys(db_session, http_mock):
    """:233 and :235, both true arms.

    Both keys are asserted in one test because a single Event row carries
    both columns; they are still separately killed, because `if False:` on
    either guard drops only that guard's key and this asserts each by name.
    """
    from datetime import datetime
    s = _seed(post_type=POST_TYPE_EVENT, local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)
    db.session.add(Event(post_id=s.post.id, start=datetime(2030, 6, 1, 9, 0),
                         end=datetime(2030, 6, 1, 10, 0), timezone='UTC'))
    db.session.commit()

    _send(s.post)

    page = _page_of(route)
    assert page['startTime'] == '2030-06-01T09:00:00+00:00'
    assert page['endTime'] == '2030-06-01T10:00:00+00:00'
