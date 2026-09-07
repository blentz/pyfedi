"""`send_post` -- the Celery-path builder and deliverer of an ActivityPub Page.

`app/shared/tasks/pages.py:88-352`. This is the second of two Page builders in
the codebase; the other is `post_to_page` (app/activitypub/util.py:132-217 by
`ast`; :217 is `return activity_data` and :220 opens `post_replies_for_ap`).
Its direct callers are app/activitypub/routes.py:2059 and :2184. It is NOT
reached from routes.py:2033 -- that line calls `post_to_activity`, which calls
`post_to_page` at app/activitypub/util.py:115. They are near-twins and their
disagreements are findings D298, D299 and D300. (Both citations on these two
lines were wrong: the extent read 132-219, and routes.py:2033 was named as the
caller. D305, a fourth claimed disagreement about the `attachment` key, has been
withdrawn -- util.py:147 puts `"attachment": []` in `post_to_page`'s
unconditional dict literal, so the two builders agree there.)

ENTRY is a direct call. `send_post(post_id, edit=False, session=None)` has no
usable default for `session` -- :89 is `session.query(Post).get(post_id)` -- so
every test here passes `db.session` explicitly.

FOUR EARLY RETURNS stand between entry and the builder at :163, and a test that
wants to reach the builder must clear all four. (This said :175 until the final
review of sub-project 19; :175 is `language = {...}`, a line INSIDE the builder,
and the rest of this file already says :163 -- see :199, :563 and :1192.)

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
from app.activitypub.signature import default_context
from app.models import (
    ActivityPubLog, BannedInstances, CommunityBan, Event, File, Instance,
    Language, Notification, Poll, PollChoice, User, UserFollower,
)
from app.shared.tasks.pages import edit_post, make_post as make_post_task, move_object, move_post, send_post
from tests.factories import (
    make_community, make_community_member, make_instance, make_instance_block,
    make_post, make_user,
)


def _seed(body=None, post_type=POST_TYPE_ARTICLE, url=None, local_community=True,
          with_keys=False):
    """The local instance, a local author, a community, and a post.

    ORDER IS LOAD-BEARING. `make_community` hardcodes `instance_id=1`
    (tests/factories.py) and the db_session teardown resets every
    sequence (tests/conftest.py:131-132), so whichever Instance is inserted first
    gets id 1. The
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
         `HttpSignature.signed_request` (app/activitypub/signature.py:425)
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

    `http_mock` is `assert_all_called=True` (tests/conftest.py:336-343), so the
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

    THE LOCAL ARM'S EXCEPT AT :107-108 IS UNREACHABLE FOR EVERY INPUT. For a
    bare local name
    (no `@`), `search_for_user` (app/user/utils.py:85) hits :91-92
    (`server = ''`), so :94's `if server:` is False and :98 -- the function's
    only `raise` -- can never fire on that path. A no-match local lookup
    instead falls through :103 (`if already_exists:`, False) and :105
    (`elif not allow_fetch:`, False -- `allow_fetch` defaults True) to
    :108-109's clean `return None`. A nonexistent local mention returns None
    without ever raising, so NO CHOICE OF MENTION reaches :107-108 on this arm
    -- registered here as a finding for the residual sweep rather than
    exercised. It is not dead code in the stronger sense: the clause is bare, so
    a SQLAlchemyError out of app/user/utils.py:101 would still land in it.

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
    would make `http_mock`'s `assert_all_called=True` (tests/conftest.py:336-343)
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
# `ap_datetime`: most of the other 26 (this read "28" until the final review of
# sub-project 19 -- 29 sites minus the three guarded here is 26, not 28)
# pass a non-nullable column, and making
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
    at app/utils.py:2294, from app/shared/tasks/pages.py:224 -- that number is
    PRE-FIX NUMBERING. The guard now occupies :224 and the read it protects
    moved down to :225, which is the line named at the top of this docstring.

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
    at app/utils.py:2294, from app/shared/tasks/pages.py:232 -- PRE-FIX
    NUMBERING. The read now sits at :234, guarded by :233.

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
    at app/utils.py:2294, from app/shared/tasks/pages.py:233 -- PRE-FIX
    NUMBERING. The read now sits at :236, guarded by :235.

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


# ---------------------------------------------------------------------------
# The poll options block, :226-230
# ---------------------------------------------------------------------------
#
# THREE CONDITIONAL-EXPRESSION ARMS THAT NO COVERAGE FIGURE CAN SEE. :226 is
# `poll.total_votes() if edit else 0`, :229 is `choice.num_votes if edit else 0`
# and :230 is `page['oneOf' if poll.mode == 'single' else 'anyOf'] = choices`.
# Each is one statement on one line, so both of its arms cover that line and
# coverage.py emits no arc for the choice -- tests/README.md fact 87. The
# region can therefore sit at 100% statements and 100% branches with the `edit`
# arm of :226 and :229 and the 'anyOf' arm of :230 never once executed, which
# is exactly the state this file was in before these three tests: every poll
# fixture above passes `edit=False` and `mode='single'`, and none of them
# writes a PollChoice row at all, so :229 was reached only from other test
# modules and neither of its arms was pinned here.
#
# Found by the AST walk fact 87(c) prescribes -- `ast.parse`, then every
# `IfExp` inside the `send_post` `FunctionDef` -- not by reading the residual,
# which lists none of them.


def _poll_with_choices(post, mode='single', end_poll=None):
    """A Poll for `post` plus two PollChoice rows carrying real vote counts.

    THE COUNTS ARE NON-ZERO ON PURPOSE. :226 and :229 each choose between a
    stored number and the literal `0`, so a fixture whose choices had no votes
    would make both arms compute the same value and neither test below could
    tell them apart -- fact 33 wearing fact 87's hat.

    `sort_order` is set because :228 orders by it
    (`PollChoice.query.filter_by(post_id=post.id).order_by(
    PollChoice.sort_order)`), and the tests assert the choices in order.
    """
    db.session.add(Poll(post_id=post.id, end_poll=end_poll, mode=mode))
    db.session.add(PollChoice(post_id=post.id, choice_text='yes',
                              sort_order=1, num_votes=3))
    db.session.add(PollChoice(post_id=post.id, choice_text='no',
                              sort_order=2, num_votes=4))
    db.session.commit()


def test_a_created_polls_vote_counts_are_reported_as_zero(db_session, http_mock):
    """:226 and :229 on their `not edit` arms, and :230 on its 'single' arm.

    A poll being federated for the first time reports no votes regardless of
    what the rows say, which is what `if edit else 0` encodes. The fixture
    gives the two choices 3 and 4 votes, so `Poll.total_votes()`
    (app/models.py:3814-3816, `SUM(num_votes)`) would be 7 and the two
    `totalItems` would be 3 and 4 if either ternary took its other arm.
    Asserting the zeros is therefore a real discrimination, not a restatement
    of an empty fixture.
    """
    s = _seed(post_type=POST_TYPE_POLL, local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)
    _poll_with_choices(s.post)

    _send(s.post, edit=False)

    page = _page_of(route)
    assert page['votersCount'] == 0
    assert [c['name'] for c in page['oneOf']] == ['yes', 'no']
    assert [c['replies']['totalItems'] for c in page['oneOf']] == [0, 0]


def test_an_edited_polls_vote_counts_are_reported_in_full(db_session, http_mock):
    """:226 and :229 on their `edit` arms.

    The mirror of the test above, differing only in `edit=True`. 7 is not a
    third constant: it is the sum of the 3 and 4 asserted per choice on the
    next line, so a mutant that collapsed :226 to `poll.total_votes()` and one
    that collapsed :229 to `choice.num_votes` are killed by the create test
    while this one holds them honest in the other direction.
    """
    s = _seed(post_type=POST_TYPE_POLL, local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)
    _poll_with_choices(s.post)

    _send(s.post, edit=True)

    page = _page_of(route)
    assert page['votersCount'] == 7
    assert [c['replies']['totalItems'] for c in page['oneOf']] == [3, 4]


def test_a_multiple_choice_poll_is_delivered_under_anyOf(db_session, http_mock):
    """:230's 'anyOf' arm.

    `Poll.mode` (app/models.py:3783) is a free-text column whose own comment
    documents the two values as "'single' or 'multiple'"; every other poll
    fixture in this file is 'single', so without this test the key the choices
    are delivered under is fixed by the fixtures rather than by the code.

    `'oneOf' not in page` is asserted first and separately: it is the half that
    fails on a mutant which emits both keys, which reading `page['anyOf']`
    alone would not notice.
    """
    s = _seed(post_type=POST_TYPE_POLL, local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)
    _poll_with_choices(s.post, mode='multiple')

    _send(s.post, edit=False)

    page = _page_of(route)
    assert 'oneOf' not in page
    assert [c['name'] for c in page['anyOf']] == ['yes', 'no']


# ---------------------------------------------------------------------------
# The localised-start block, :322-326
# ---------------------------------------------------------------------------
#
# :322's guard is the fourth one commit e1692167 added, and it is NOT one of
# D298's three. It exists because :325 dereferences the SAME nullable
# `Event.start` a second time --
# `post.event.start.replace(tzinfo=ZoneInfo('UTC')).astimezone(event_tz)` --
# under a block that was gated only on `post.type == POST_TYPE_EVENT`. Guarding
# :233 alone therefore MOVED the crash rather than removing it: a start-less
# event died ninety lines later instead, at :324's
# `ZoneInfo(post.event.timezone)` with `TypeError: expected str, bytes or
# os.PathLike object, not NoneType`.
#
# `timezone` is deliberately NOT part of that guard. app/shared/post.py:708 is
# `event.timezone = event_data.get('timezone', 'UTC')` -- an unconditional
# write with a default -- and app/api/alpha/schema.py:350 declares
# `timezone = fields.String(...)` with no `allow_none=True`, so marshmallow
# rejects an explicit null before `edit_post` is reached. `timezone` is
# genuinely unreachable as None through this path; `start` is not.
#
# THE TRUE ARM NEEDS ITS OWN OBSERVABLE, and it is not the one the tests above
# use. :322-326 runs at :322, which is BELOW the community delivery at :306, so
# the Page captured by `_remote_inbox` was serialized before this block ran and
# cannot see it. A mutant reading `if post.type == POST_TYPE_EVENT and False:`
# survives every test above -- measured, not assumed. `_inward_follower` below
# supplies the second delivery that does see it.


FOLLOWER_INBOX = 'https://follower.example/u/fan/inbox'


def _inward_follower(s, http_mock=None, domain='follower.example',
                     inbox=FOLLOWER_INBOX, software='mastodon',
                     with_inbox=True, instance=None):
    """A remote follower of the author, and the inbox their AMENDED copy lands
    in. Returns the respx route, or None when no route was registered (no
    `http_mock`, or `with_inbox=False`); pair a route with `_sent_activity`.

    THIS IS THE ONLY WAY TO OBSERVE :314-330. `_remote_inbox` captures the
    Page as it was at :306; everything from :313 onward -- the `name` delete,
    the `content` reset, the Event->Note type change, the localised start, the
    `contentMap` -- happens afterwards, to the same dict. The second send at
    :352 is the first one that carries those mutations.

    Reaching :352 means clearing, in order:

      :339-341  `followers = session.query(UserFollower).filter_by(
                local_user_id=post.user_id, is_inward=True).all()`, then
                `if not followers: return`. Hence the UserFollower row, which
                must be `is_inward=True`.
      :349      `user.following_instances()` (app/models.py:1667-1676) joins
                Instance -> User -> UserFollower on the SAME inward filter, so
                the follower must be a REMOTE user whose instance is neither
                dormant nor gone_forever.
      :350      `instance.domain not in domains_sent_to and instance.id != 1
                and instance.software != 'piefed'`. `domains_sent_to` already
                holds SERVER_NAME (:264) and the community's domain (:307), so
                this follower lives on a THIRD domain. `make_instance`'s
                default software is 'mastodon', which is what this path is for.
      :351      `instance.inbox` must be set -- `make_instance` leaves it None
                -- and the instance online and unblocked.

    The domain stays inside `.example` so the autouse
    `_peer_example_resolves_without_a_resolver` fixture still answers for it.

    THE KNOBS BELOW WERE ADDED BY TASK 7 and are all defaulted to the original
    behaviour, so the Task 5 caller above is untouched. They exist because
    :350's and :351's conjuncts are each closed by a DIFFERENT column of this
    same row set, and building a second follower factory to close them would be
    the duplication this helper was written to avoid. They mirror
    `_community_follower`'s knobs one for one:

      `software`     closes :350's third conjunct ('piefed').
      `with_inbox`   leaves `Instance.inbox` at `make_instance`'s None, which
                     closes :351's first conjunct.
      `instance`     reuses an EXISTING Instance instead of making one, so the
                     follower can be planted on a domain `domains_sent_to`
                     already holds -- the only way to close :350's first
                     conjunct, since the one other domain in that list is
                     SERVER_NAME's, whose Instance is id 1 and is filtered out
                     of `following_instances` in SQL (app/models.py:1673).
      `http_mock`    now optional. A test whose follower is SUPPOSED to receive
                     nothing must not register a route for it: `http_mock` is
                     `assert_all_called=True` (tests/conftest.py:336-343), so an
                     unfired route would fail the test for the wrong reason.
                     Those tests count `ActivityPubLog` rows instead -- see
                     `test_a_follower_instance_with_no_inbox_gets_no_amended_copy`
                     for why that witness and not "no HTTP happened".
    """
    if instance is None:
        instance = make_instance(domain, software=software)
    instance.inbox = inbox if with_inbox else None
    fan = make_user(instance, 'fan')
    db.session.add(UserFollower(local_user_id=s.user.id, remote_user_id=fan.id,
                                is_inward=True))
    db.session.commit()
    if http_mock is None or not with_inbox:
        return None
    return http_mock.post(instance.inbox).respond(200, json={})


def test_an_event_note_carries_its_start_localised_to_the_event_timezone(db_session, http_mock):
    """:322's TRUE arm, and the only test that reaches :324-326 at all.

    Without it, `if post.type == POST_TYPE_EVENT and False:` survives the whole
    file: the localised-start block could be deleted outright and nothing would
    notice, because every other event test reads the Page as it was at :306,
    before this block runs.

    The timezone is deliberately NOT 'UTC'. 2030-06-01T13:00 UTC is
    2030-06-01T09:00 in America/New_York (EDT, UTC-4, in June), so the asserted
    string also pins :325's conversion -- a mutant that dropped the
    `.astimezone(event_tz)` would emit 13:00 and be caught. 'UTC' would make
    input and output identical and hide that.

    `content` is asserted whole rather than by substring, so the composition is
    pinned too: :315 resets it to '', :320's `elif post.type !=
    POST_TYPE_POLL:` writes the title paragraph, and :326 APPENDS to that with
    `+=`. A mutant swapping :326's `+=` for `=` would lose the title and is
    caught here. `post.body` is None, so :327's `if post_body_html:` is false
    and nothing further is appended.
    """
    from datetime import datetime
    s = _seed(post_type=POST_TYPE_EVENT, local_community=False, with_keys=True)
    _remote_inbox(s, http_mock)
    follower_route = _inward_follower(s, http_mock)
    db.session.add(Event(post_id=s.post.id, start=datetime(2030, 6, 1, 13, 0),
                         end=None, timezone='America/New_York'))
    db.session.commit()

    _send(s.post)

    note = _sent_activity(follower_route)['object']
    assert note['type'] == 'Note'
    assert note['content'] == ('<p>a post</p>'
                               '<p>2030-06-01T09:00:00 (America/New_York)</p>')


# ---------------------------------------------------------------------------
# The type dispatch and the tag/cc appends, :163-174
# ---------------------------------------------------------------------------
#
# These are the three arms Tasks 1 and 2 deferred (see the ':163-174 --
# deferred to Task 6' comment block above, and the note in
# `test_a_remote_recipient_gets_no_local_notification`). All three write only
# to locals -- `type`, `tag`, `cc` -- read into `page`/`create` at :185 and
# :188-189 and never persisted, so the delivered body is the only observable.
# `_remote_inbox` / `_sent_activity` supply it.


def _key_id_of(route, index=-1):
    """The `keyId` the captured request was signed under.

    `HttpSignature.compile_signature` (app/activitypub/signature.py:353-359)
    emits `keyId="<id>",headers="..."`, so the value is the first
    double-quoted field. This is the ONLY observable that separates the two
    signing actors inside :294's loop: :298 and :302 sign as the community
    (`community.public_url() + '#main-key'`) while :300 signs as the author
    (`user.public_url() + '#main-key'`), and respx never verifies a signature,
    so the private key itself leaves no trace on the wire.
    """
    return route.calls[index].request.headers['signature'].split('"')[1]


def test_a_remote_mention_resolves_and_reaches_the_delivered_activity(db_session, http_mock):
    """:109-114, the REMOTE arm's SUCCESS path -- deferred from Task 1.

    Task 1 could only reach `except: pass` at :113-114
    (`test_a_banned_remote_host_mention_is_skipped_via_the_remote_except`).
    The success path -- `search_for_user` returning a real remote User at :112
    and :115's `if recipient:` opening -- had no witness there, because
    `User.is_local()` is False for a remote recipient so :127 skips the
    notification either way: a resolved remote mention and an unresolved one
    both leave `Notification.query.count() == 0`.

    The resolution IS visible in the delivered activity. A recipient that
    reached `recipients` is appended to `tag` and `cc` at :172-174, which are
    read into the body at :188-189 and :259. A mutant making :112 return None
    -- or deleting :115-123 -- empties both, and this test fails.

    The mentioned user is put on the community's own instance so that
    :307's `domains_sent_to.append(community.instance.domain)` suppresses a
    second, redundant delivery at :335; `s.community.instance` is the peer
    `_remote_inbox` created, so no second `make_instance('peer.example')` is
    attempted (`Instance.domain` is unique).
    """
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)
    remote = make_user(s.community.instance, 'remoteuser', local=False)
    assert remote.is_local() is False
    s.post.body = 'hello @remoteuser@peer.example'
    db.session.commit()

    _send(s.post)

    activity = _sent_activity(route)
    assert activity['cc'] == [remote.public_url()]
    assert {'href': remote.public_url(), 'name': remote.mention_tag(),
            'type': 'Mention'} in activity['object']['tag']
    # The proof that this is the resolution path and not the notification
    # path: no Notification was written, yet the recipient still landed.
    assert Notification.query.filter_by(user_id=remote.id).count() == 0


def test_a_mentioned_recipient_lands_in_both_tag_and_cc(db_session, http_mock):
    """:172-174 -- deferred from Task 2. `tag.append(...)` at :173 and
    `cc.append(...)` at :174 are separate statements, so both are asserted:
    a mutant dropping either one survives an assertion on the other.

    `cc` is ONE list object shared by `page['cc']` (:188) and `create['cc']`
    (:259) -- :170 binds it once and :174 mutates it in place -- so both are
    checked, which also pins that neither key was rebuilt from something else.
    `tag` starts as `post.tags_for_activitypub()` (:171); this post carries no
    tags, so the single element is the Mention and `len(tag) == 1` holds.

    A LOCAL mentioned user, deliberately: it keeps this test distinct from
    `test_a_remote_mention_resolves_and_reaches_the_delivered_activity` above,
    which takes :109-114's remote arm to reach the same two appends.
    """
    s = _seed(body='hello @mentioned@test.piefed.local', local_community=False,
              with_keys=True)
    mentioned = make_user(s.instance, 'mentioned', local=True)
    route = _remote_inbox(s, http_mock)

    _send(s.post)

    activity = _sent_activity(route)
    expected_tag = {'href': mentioned.public_url(),
                    'name': mentioned.mention_tag(), 'type': 'Mention'}
    assert activity['object']['tag'] == [expected_tag]
    assert activity['object']['cc'] == [mentioned.public_url()]
    assert activity['cc'] == [mentioned.public_url()]


@pytest.mark.parametrize('post_type,expected', [
    (POST_TYPE_POLL, 'Question'),
    (POST_TYPE_EVENT, 'Event'),
    (POST_TYPE_ARTICLE, 'Page'),
])
def test_the_delivered_object_type_follows_the_post_type(db_session, http_mock,
                                                         post_type, expected):
    """:163-168, all three arms -- deferred from Task 2.

    `type` is a local, consumed at :185 as `page['type']`. Without a delivered
    body there is nothing to read it off, which is why Task 2's version of
    this test could only assert that `_send` did not raise -- true for every
    arm and therefore not a test of the dispatch at all.

    The Poll and Event rows exist because :222-248 dereference them
    (`poll.mode` at :230, `event.timezone` at :237); they carry no bearing on
    :163-168 itself, which branches on `post.type` alone.
    """
    s = _seed(post_type=post_type, local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)
    if post_type == POST_TYPE_POLL:
        db.session.add(Poll(post_id=s.post.id, end_poll=None, mode='single'))
    elif post_type == POST_TYPE_EVENT:
        db.session.add(Event(post_id=s.post.id, start=None, end=None,
                             timezone='UTC'))
    db.session.commit()

    _send(s.post)

    assert _sent_activity(route)['object']['type'] == expected


# ---------------------------------------------------------------------------
# FOUR UNREACHABLE ITEMS, and why no test here chases them.
#
# Three are branch arms; the fourth is a pair of STATEMENTS. Together they are
# the whole of what `send_post` (:88-352) leaves unmeasured -- coverage reports
# exactly the lines 107-108 and exactly the arcs (270, 310), (312, 314) and
# (333, 339) as missing inside the function, and nothing else.
#
# (1) AND (2): THE FALSE ARMS OF :270 AND :333, both `if not
# community.local_only:` -- the arcs (270, 310) and (333, 339). They will stay
# missing.
#
# `community` is bound once, at :91 (`community = post.community`), and is
# never reassigned anywhere in :88-352. And :153-154 is
# `if community.local_only or community.private: return`. So a community with
# `local_only` set returns at :154, long before :270; by the time control
# reaches :270, `community.local_only` is necessarily falsy and
# `not community.local_only` is necessarily True.
#
# tests/README.md fact 75, cause 4(b) -- a condition falsified by an invariant
# established before the guard runs. Worth recording that 4(b) names a CALLER
# or an ENCLOSING GUARD as the establisher and neither is what happens here:
# the establisher is an EARLIER RETURN in the same function.
#
# CORRECTED by the final review of sub-project 19. This block used to read:
# "Sub-project 18 set `community.local_only = True` in ten of its tests
# believing it skipped delivery at :270." That is FALSE and it named ten
# innocent tests. All ten sites -- tests/test_shared_post_edit.py:534, :562,
# :580, :630, :665, :680, :866, :901, :1075, :1456 -- document the CORRECT
# mechanism in their docstrings: app/shared/post.py:736 sets `federate = False`
# so :743 never dispatches, and send_post is never entered. Neither :270 nor
# :153-154 is their mechanism, because neither line runs for them.
#
# What DID say :270 is this sub-project's own plan
# (docs/superpowers/plans/2026-09-06-coverage-send-post-19.md:535) and design
# (docs/superpowers/specs/2026-09-06-coverage-send-post-19-design.md:177), and
# they are about DIRECT send_post tests -- this file's kind. For a direct test
# the trap is real: local_only returns at :154 and the builder never runs.
#
# (3): THE FALSE ARM OF :312, arc (312, 314). :312 is `if 'name' in page:`.
# :196 sets `'name': post.title` unconditionally, inside the dict literal that
# builds `page`, and nothing removes the key before :312 -- :313 is the only
# `del` and it sits inside the true arm -- so the false arm can never run.
# :209-210 re-assigns the same key for non-polls, which is a redundant
# statement rather than a second writer: :196 has already set it for every
# type. Cause 4(b) again, with a third kind of establisher: an unconditional
# assignment rather than a return.
#
# (4): THE STATEMENTS :107-108, the bare `except: pass` on the LOCAL arm of the
# mention scanner. This is the only item of the four scoped to statements, and
# fact 75's only statement-level cause -- 6, the redundant statement -- does
# NOT fit it: the handler is not redundant, it is unreachable FOR EVERY INPUT,
# because `search_for_user` (app/user/utils.py:85-158) has no raising path for a
# bare local name. Read the callee in order for the argument :106 passes it:
# :88's `if '@' in address` is false for a name with no host, so :91-92 set
# `server = ''`; :94's `if server:` is then false, so the function's sole
# `raise` (:98, the blocked-instance check -- confirmed sole by an AST walk for
# `ast.Raise` inside the `FunctionDef`) is skipped; the hit path returns a User
# at :104 and the miss path ends at :108-109, `if not server: return None`.
# The call therefore returns a User or None and never raises OF ITS OWN
# ACCORD.
#
# THE LIMIT OF THAT PROOF, added by the final review of sub-project 19. The
# clause is BARE -- `except:`, not `except Exception:` -- so it also catches
# what the callee's machinery raises. On this arm :101 is
# `db.session.query(User).filter_by(user_name=name, ap_id=None).first()`, and a
# SQLAlchemyError from a dropped connection lands in :107-108 and is swallowed.
# What is proved is "no INPUT reaches a raise", which is exactly enough to
# explain why no test here chases the lines and why coverage will always report
# them missing. It is NOT "these lines can never run", and it would not justify
# deleting them.
#
# Contrast :112, the REMOTE arm: its address always contains '@', so :94 opens
# and :98 can fire. Its `except` at :113-114 IS reached, by
# test_a_banned_remote_host_mention_is_skipped_via_the_remote_except above.
# The two handlers are the same three tokens and only one of them is dead.
#
# OFFERED TO THE NEXT README TASK AS A NEW CAUSE FOR FACT 75: an UNREACHABLE
# HANDLER -- a bare `except` whose callee has no raising path for the argument
# shape this call site can produce. It is proved the way cause 7 is proved, by
# reading the callee's statements rather than by counting failures, but it is
# scoped to a statement, so neither 1-5 (clause) nor 7 (expression arm) covers
# it, and 6 asserts redundancy this shape does not have.
#
# A FIFTH ITEM WAS LISTED HERE AND IS WITHDRAWN. The plan claimed the TRUE arm
# of :310 (`if '@context' not in create:`) was unreachable because :260 always
# puts `@context` into `create`. It does -- and :272 then DELETES it, on every
# local community, which is `_seed()`'s default. So :311 runs on the ordinary
# path. Cover it; do not register it. Disproved in Task 5 round 1 by reading
# :270-272, after renumbering exposed the claim to a re-check.
#
# AND THREE REDUNDANT CONJUNCTS THE COVERAGE FIGURES CANNOT SHOW AT ALL.
# :295's `instance.online()`, :351's `instance.online()` and :350's
# `instance.id != 1` are all unreachable-False. `Instance.online()` is
# `not (self.dormant or self.gone_forever)` (app/models.py:118-119), and both
# loops draw their `instance` from a query that has already applied those same
# predicates in SQL: `Community.following_instances` (app/models.py:842-851)
# filters `Instance.dormant == False` at :849 and `Instance.id != 1,
# Instance.gone_forever == False` at :850, and `User.following_instances`
# (app/models.py:1667-1676) does the same at :1672 and :1673. No row either
# loop can see fails any of the three conjuncts.
#
# THEY DO NOT APPEAR IN THE RESIDUAL ABOVE, AND THAT IS THE POINT. Coverage
# records a branch arc at the `if` level, not per conjunct, so a conjunct that
# never varies is invisible to the branch figure for the same structural reason
# fact 87 gives for a conditional expression. Mutation found these; no number
# could have. They are cause 4(b) once more, with the establisher being THE
# QUERY THAT PRODUCED THE LOOP VARIABLE -- a fourth kind of establisher, and
# the reason this block names the establisher every time instead of just citing
# the cause.
#
# The tests below still exercise :295 and :351 as whole guards -- a dormant
# instance cannot be produced, but an INBOXLESS one can, and
# test_an_inboxless_follower_instance_is_skipped and
# test_a_follower_instance_with_no_inbox_gets_no_amended_copy close each `if`
# on its first conjunct.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Create/Announce construction and per-instance delivery, :250-307
# ---------------------------------------------------------------------------
#
# :294-304 is a DIFFERENT arm of :271 from the one `_remote_inbox` serves.
# `_remote_inbox` forces `community.is_local()` False to reach :306; the
# fan-out loop needs it True, which is what `_seed()`'s default gives. Hence
# `_community_follower` below rather than a change to `_remote_inbox`, which
# four other tasks depend on.
#
# WHAT MAKES AN INSTANCE A FOLLOWER, read off the query rather than assumed.
# `Community.following_instances` (app/models.py:842-851) joins
# Instance -> User -> CommunityMember and filters
# `CommunityMember.community_id == self.id`, `CommunityMember.is_banned ==
# False`, `Instance.id != 1`, `Instance.dormant == False` and
# `Instance.gone_forever == False`. So the row shape is: a remote Instance
# (id != 1 -- the local instance is id 1, see `_seed`), a User on it, and a
# CommunityMember joining that user to the community. `make_community_member`
# (tests/factories.py:382-392) sets `is_banned=False` already.
#
# The other half of :294's set, `user.following_instances(software='piefed')`
# (app/models.py:1667-1676), needs inward `UserFollower` rows AND
# `software == 'piefed'`; none of these tests create either, so it contributes
# nothing and the set is exactly the community's followers.
#
# FINDING -- :295's `instance.online()` is UNREACHABLE-FALSE. `online()` is
# `not (self.dormant or self.gone_forever)` (app/models.py:118-119), and BOTH
# `following_instances` implementations already filter `Instance.dormant ==
# False` and `Instance.gone_forever == False` in SQL. An offline instance can
# therefore never enter the loop, so the second conjunct is a redundant
# re-check that no test can drive to False.
# `test_a_dormant_follower_instance_is_skipped` below pins the behaviour that
# IS real -- an offline follower receives nothing -- and says so in its
# docstring rather than claiming an arm it cannot take.


def _community_follower(s, http_mock=None, domain='fan.example',
                        software='lemmy', member_name='fan', with_inbox=True,
                        dormant=False):
    """A remote instance that `community.following_instances()` returns at
    :294, plus the respx route its delivery lands on.

    Returns `SimpleNamespace(instance, member, route)`; `route` is None when
    no route was registered (no `http_mock`, or `with_inbox=False`).

    THE COMMUNITY'S KEYPAIR IS SET HERE. :298 and :302 sign with
    `community.private_key`, which `make_community` (tests/factories.py:
    122-151) leaves None, and `HttpSignature.signed_request` calls `.encode()`
    on it -- a keyless community dies exactly as a keyless author does. The
    author's key is reused rather than a second one generated: generation
    costs about a second per keypair, respx never verifies a signature, and
    the actor a delivery was signed AS is observable through `keyId`
    (`_key_id_of`), not through the key material. `_seed(with_keys=True)` is
    therefore a precondition, asserted below.

    `with_inbox=False` leaves `Instance.inbox` at `make_instance`'s None,
    which is what closes :295's FIRST conjunct. `dormant=True` sets the column
    `Community.following_instances` filters on -- see the FINDING above for
    why that is a query-level exclusion and not :295's second conjunct.

    The domain stays inside `.example` so the autouse
    `_peer_example_resolves_without_a_resolver` fixture still answers for it.
    """
    assert s.user.private_key is not None, '_seed(with_keys=True) is required'
    s.community.private_key = s.user.private_key
    s.community.public_key = s.user.public_key
    instance = make_instance(domain, software=software)
    instance.inbox = f'https://{domain}/inbox' if with_inbox else None
    instance.dormant = dormant
    member = make_user(instance, member_name, local=False)
    make_community_member(member, s.community)
    db.session.commit()
    route = (http_mock.post(instance.inbox).respond(200, json={})
             if http_mock is not None and with_inbox else None)
    return SimpleNamespace(instance=instance, member=member, route=route)


def test_a_microblog_instance_gets_the_announce_on_create(db_session, http_mock):
    """:296 TRUE and :297 TRUE -> :298, the `microblog_announce`.

    'mastodon' is taken verbatim from `MICROBLOG_APPS`
    (app/constants.py:89 -- `["mastodon", "misskey", "akkoma", "iceshrimp",
    "pleroma", "fedibird"]`) rather than guessed.

    The body separates all three delivery statements at once. :298 sends
    `microblog_announce`, whose `object` is `post.ap_id`, a STRING (:289);
    :302 would send `group_announce`, whose `object` is the `create` DICT
    (:280); :300 would send `create` itself, type 'Create'. Asserting the type
    is 'Announce' AND the object is the bare ap_id string admits only :298.
    `keyId` additionally pins the community as the signing actor, which
    separates :298/:302 from :300.
    """
    s = _seed(with_keys=True)
    assert s.community.is_local() is True
    fan = _community_follower(s, http_mock, software='mastodon')

    _send(s.post, edit=False)

    announce = _sent_activity(fan.route)
    assert announce['type'] == 'Announce'
    assert announce['object'] == s.post.ap_id
    assert announce['actor'] == s.community.public_url()
    assert _key_id_of(fan.route) == s.community.public_url() + '#main-key'


def test_a_microblog_instance_gets_the_create_directly_on_edit(db_session, http_mock):
    """:296 TRUE and :297 FALSE -> :300, the direct `create`.

    Identical setup to the test above except `edit=True`, which makes
    `activity` 'update' at :250 and closes :297's `if activity == 'create'`.
    :300 sends the Create/Update envelope itself -- not an Announce -- and
    signs it as the AUTHOR, so both the body type and the `keyId` flip
    relative to the create case. Asserting only the type would leave a mutant
    that swapped :300's signing actor alive.
    """
    s = _seed(with_keys=True)
    fan = _community_follower(s, http_mock, software='mastodon')

    _send(s.post, edit=True)

    activity = _sent_activity(fan.route)
    assert activity['type'] == 'Update'
    assert activity['object']['id'] == s.post.public_url()
    assert _key_id_of(fan.route) == s.user.public_url() + '#main-key'


def test_a_lemmy_instance_gets_the_group_announce(db_session, http_mock):
    """:296 FALSE -> :302, the `group_announce`.

    'lemmy' is not in `MICROBLOG_APPS` (app/constants.py:89), so :296's
    membership test fails and the `else` at :301-302 runs for BOTH create and
    edit. `group_announce`'s `object` is the whole `create` dict (:280), which
    is what distinguishes it from :298's string-object announce.

    `cc` is asserted because :275 REBINDS the local `cc` to
    `[community.ap_followers_url]` after :259 has already bound the original
    recipient list into `create['cc']`. The nested Create therefore keeps the
    empty recipient `cc` while the Announce carries the followers URL -- a
    mutant that moved :275 above :253 would collapse the two.
    """
    s = _seed(with_keys=True)
    fan = _community_follower(s, http_mock, software='lemmy')

    _send(s.post, edit=False)

    announce = _sent_activity(fan.route)
    assert announce['type'] == 'Announce'
    assert announce['object']['type'] == 'Create'
    assert announce['object']['object']['id'] == s.post.public_url()
    assert announce['cc'] == [s.community.ap_followers_url]
    assert announce['object']['cc'] == []
    assert _key_id_of(fan.route) == s.community.public_url() + '#main-key'


def test_a_poll_does_not_mark_the_domain_as_sent_to(db_session, http_mock):
    """:303 FALSE -- `if post.type != POST_TYPE_POLL:` -- so :304 is skipped.

    `domains_sent_to` is a local; the only place its contents are read is
    :335, `if recipient.instance.domain not in domains_sent_to`. So the
    witness for :303's false arm is a SECOND delivery to the same inbox: the
    mention fan-out at :336 is not suppressed, because the fan's domain was
    never appended.

    The mentioned user IS the community member, so one Instance serves both
    roles and both sends target the same URL -- `route.calls` counts them.
    Call 0 is the Announce from :302, call 1 the amended Create from :336.

    Pairs with `test_a_non_poll_marks_the_domain_as_sent_to` below, whose
    setup differs ONLY in `post_type`. Neither test alone distinguishes :303;
    together they do.
    """
    s = _seed(post_type=POST_TYPE_POLL, body='hello @fan@fan.example',
              with_keys=True)
    fan = _community_follower(s, http_mock, software='lemmy')
    db.session.add(Poll(post_id=s.post.id, end_poll=None, mode='single'))
    db.session.commit()

    _send(s.post)

    assert len(fan.route.calls) == 2
    assert _sent_activity(fan.route, index=0)['type'] == 'Announce'
    second = _sent_activity(fan.route, index=1)
    assert second['type'] == 'Create'
    assert second['object']['type'] == 'Question'


def test_a_non_poll_marks_the_domain_as_sent_to(db_session, http_mock):
    """:303 TRUE -> :304, `domains_sent_to.append(instance.domain)`.

    The mirror of the poll test above: same community member, same mention,
    same Announce -- but an article appends 'fan.example' to
    `domains_sent_to`, so :335's guard closes and :336 never fires. Exactly
    one request reaches the inbox.

    A mutant deleting :304 makes this test see two calls; a mutant that made
    :303 unconditional makes the poll test see one. The pair pins the branch
    in both directions.
    """
    s = _seed(post_type=POST_TYPE_ARTICLE, body='hello @fan@fan.example',
              with_keys=True)
    fan = _community_follower(s, http_mock, software='lemmy')

    _send(s.post)

    assert len(fan.route.calls) == 1
    assert _sent_activity(fan.route)['type'] == 'Announce'


def test_a_remote_community_receives_the_create_at_its_inbox(db_session, http_mock):
    """:271 FALSE -> :306, the remote-community arm.

    No Announce is built at all on this arm: the Create goes straight to
    `community.ap_inbox_url`, signed by the AUTHOR. Asserting the request URL,
    the body type and the `keyId` together separates :306 from every statement
    in the `is_local()` branch above it, all of which POST to an
    `Instance.inbox` instead.

    `assert s.community.is_local() is False` is the guard-opens proof --
    `Community.is_local()` (app/models.py:795-796) is a disjunction and
    `ap_id` alone does not close it; see `_seed`'s docstring.
    """
    s = _seed(local_community=False, with_keys=True)
    assert s.community.is_local() is False
    route = _remote_inbox(s, http_mock)

    _send(s.post)

    assert len(route.calls) == 1
    assert str(route.calls[0].request.url) == PEER_INBOX
    activity = _sent_activity(route)
    assert activity['type'] == 'Create'
    assert activity['object']['type'] == 'Page'
    assert _key_id_of(route) == s.user.public_url() + '#main-key'


def test_the_remote_communitys_domain_is_marked_as_sent_to(db_session, http_mock):
    """:307, `domains_sent_to.append(community.instance.domain)`.

    Same shape of witness as :304's: the append is only readable through
    :335's membership test. A user mentioned on the community's OWN instance
    would otherwise be delivered to a second time at :336 -- so the peer's
    `Instance.inbox` is pointed at the same URL as the community's
    `ap_inbox_url`, and the assertion is that the route fired ONCE.

    Without :307 this test sees two calls. `make_instance` leaves `inbox`
    None, so setting it here is what gives the counter-factual an observable
    destination at all -- with None, :336 would fail into an 'empty uri'
    `ActivityPubLog` row (app/activitypub/signature.py:109-111) and the call
    count would stay 1 either way.
    """
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)
    s.community.instance.inbox = PEER_INBOX
    make_user(s.community.instance, 'remoteuser', local=False)
    s.post.body = 'hello @remoteuser@peer.example'
    db.session.commit()

    _send(s.post)

    assert len(route.calls) == 1
    assert _sent_activity(route)['type'] == 'Create'


def test_a_dormant_follower_instance_is_skipped(db_session):
    """An offline follower instance receives nothing.

    READ THE DOCSTRING, NOT THE NAME, FOR WHICH ARM THIS IS. It is NOT :295's
    second conjunct. `Instance.online()` is
    `not (self.dormant or self.gone_forever)` (app/models.py:118-119), and
    `Community.following_instances` (app/models.py:848-850) already filters
    `Instance.dormant == False` and `Instance.gone_forever == False` in SQL --
    as does `User.following_instances` (app/models.py:1671-1673). Every
    instance :294 can yield is therefore already online, so
    `instance.online()` at :295 is a redundant re-check whose False arm no
    test can reach. Registered as a finding; not chased.

    The two `following_instances` assertions are what make this test
    distinguish anything: `include_dormant=True` returning the instance proves
    the CommunityMember/User/Instance rows are shaped correctly and that the
    exclusion is the dormancy and nothing else (a mis-seeded row would make
    both calls return `[]` and the test would pass for the wrong reason).

    No `http_mock`: nothing should be sent. The session-scoped empty respx
    router (tests/conftest.py:331) raises on any unmatched request, which
    `post_request` records as an `ActivityPubLog` failure rather than
    re-raising -- so `ActivityPubLog.query.count() == 0` is the proof that no
    delivery was even attempted.
    """
    s = _seed(with_keys=True)
    fan = _community_follower(s, dormant=True)

    assert s.community.following_instances() == []
    assert s.community.following_instances(include_dormant=True) == [fan.instance]

    _send(s.post)

    assert ActivityPubLog.query.count() == 0


def test_an_inboxless_follower_instance_is_skipped(db_session):
    """:295 FIRST conjunct FALSE -- `if instance.inbox and ...`.

    Unlike the dormant case above, this arm IS reachable: `Instance.inbox` is
    nullable and `make_instance` leaves it None, and no `following_instances`
    filter mentions it. The instance genuinely enters :294's loop and is
    rejected at :295 -- which the first assertion proves by showing the query
    returns it.

    THE WITNESS IS THE ABSENCE OF AN ActivityPubLog ROW, not the absence of an
    HTTP request. Deleting `instance.inbox and` from :295 would call
    `send_post_request(None, group_announce, ...)`, and `post_request`
    (app/activitypub/signature.py:109-111) turns a None uri into a
    `result='failure'`/`exception_message='empty uri'` log row without ever
    reaching the transport. A test asserting only 'no HTTP happened' would
    therefore pass against that mutant; counting log rows does not.
    """
    s = _seed(with_keys=True)
    fan = _community_follower(s, with_inbox=False)

    assert s.community.following_instances() == [fan.instance]
    assert fan.instance.inbox is None

    _send(s.post)

    assert ActivityPubLog.query.count() == 0


# ---------------------------------------------------------------------------
# The Note amendment, :309-332
# ---------------------------------------------------------------------------
#
# WHAT IS BEING AMENDED, AND WHY THE OBSERVABLE MOVES. :257 binds
# `'object': page` and :314 binds `note = page`: one dict under two names. So
# :313's `del`, :315's content reset and :317's type change all rewrite what
# `create['object']` points at, IN PLACE, AFTER the community send at :306 has
# already serialized it. Every assertion below therefore reads the FOLLOWER
# delivery at :352 (`_inward_follower`), which is the first send that carries
# the amendment; where a test needs to show that a key was present before the
# amendment removed it, it reads the community delivery at :306
# (`_remote_inbox`) in the same test and compares.
#
# The brief for this task asked for these assertions at :336, the mention
# fan-out, instead. :352 is used because it needs no `search_for_user` round
# trip to set up and because Task 5 already built `_inward_follower` for
# exactly this; the amended `create` is the same object on both paths, so
# nothing is lost. :336's own arms stay covered by Task 6's
# `test_a_poll_does_not_mark_the_domain_as_sent_to` /
# `test_a_non_poll_marks_the_domain_as_sent_to` pair.
#
# :333's FALSE arm, arc (333, 339), is unreachable and is not chased here.
# `community` is bound once at :91 and never rebound anywhere in :88-352, and
# :153-154 is `if community.local_only or community.private: return` -- so
# `not community.local_only` is necessarily true by the time :333 is evaluated.
# `test_a_local_only_or_private_community_stops_before_the_builder` above pins
# the return that makes it so.
#
# :312's FALSE arm, arc (312, 314), is unreachable for the same kind of reason:
# :196 puts `name` into the `page` dict literal unconditionally, :209-210
# merely re-assigns the same key for a non-poll, and nothing between :196 and
# :312 deletes it -- :313 IS the delete, inside :312's true arm.


def test_a_local_communitys_amended_copy_carries_the_context_key_last(db_session, http_mock):
    """:310 TRUE -> :311, `create['@context'] = default_context()`.

    THE TRUE ARM IS THE ORDINARY PATH, NOT A CORNER CASE. :271 is
    `if community.is_local():` and :272 is `del create['@context']`, so every
    local community -- `_seed()`'s default -- arrives at :310 with the key
    already gone.

    THE NAME IS THE KEY'S POSITION, NOT ITS RESTORATION, and that is
    deliberate: position is what this test can actually pin. An earlier name
    ('...regains_the_context_key') claimed the restoration, which the finding
    below shows nothing in this file can distinguish.
    `post_request` (app/activitypub/signature.py:100-101) opens with
    `if '@context' not in body: body['@context'] = default_context()`, so a
    mutant that deleted :310-311 outright would have the key put back, with the
    same value and in the same trailing position, before the body was
    serialized at :456. The key's PRESENCE therefore cannot separate :311 from
    its own deletion, and no assertion in this file can; registered as a
    finding rather than claimed as a kill. MEASURED, NOT ASSUMED: `if True:` at
    :310 and a `pass` at :311 each leave every test in this file green.

    What the assertions DO pin is that this test is genuinely standing on
    :310's true arm. `create` is built at :253-262 with `@context` seventh and
    `audience` eighth and last; a run that never deleted it keeps that order
    (see the remote-community test below, which asserts exactly that). Seeing
    `@context` LAST here is the proof that :272 ran and the key was re-added
    afterwards -- a Python dict re-append moves the key to the end, and
    `json.dumps` preserves insertion order.

    No `_remote_inbox` here: the community is local, so :271's true arm runs
    and :294's loop has nothing to iterate (no CommunityMember rows, and the
    follower is 'mastodon' so `user.following_instances(software='piefed')` is
    empty too). The follower delivery at :352 is the only request made.
    """
    s = _seed(with_keys=True)
    assert s.community.is_local() is True
    route = _inward_follower(s, http_mock)

    _send(s.post)

    activity = _sent_activity(route)
    assert activity['@context'] == default_context()
    assert list(activity)[-1] == '@context'


def test_a_remote_communitys_amended_copy_keeps_the_context_it_started_with(db_session, http_mock):
    """:310 FALSE -- the guard finds `@context` still in place, so :311 is
    skipped.

    A remote community takes :271's `else` at :305-307, which never reaches
    :272's `del`, so the key survives from the :253-262 literal untouched.

    THE WITNESS IS THE KEY ORDER, not the key's presence: presence is true on
    both arms (see the local-community test above for why nothing on the wire
    can separate :311 from its absence). In the literal, `@context` is the
    seventh key and `audience` the eighth and last. Asserting that layout on
    the follower's copy proves the key was never removed and re-appended --
    i.e. that this test really is on :310's FALSE arm and not silently on the
    true one. The community delivery at :306 is asserted to carry the same
    layout, which is what shows the amendment left it alone.
    """
    s = _seed(local_community=False, with_keys=True)
    assert s.community.is_local() is False
    community_route = _remote_inbox(s, http_mock)
    follower_route = _inward_follower(s, http_mock)

    _send(s.post)

    for route in (community_route, follower_route):
        activity = _sent_activity(route)
        assert activity['@context'] == default_context()
        assert list(activity)[6] == '@context'
        assert list(activity)[-1] == 'audience'


def test_the_amended_note_drops_the_name_the_page_carried(db_session, http_mock):
    """:312 TRUE -> :313, `del page['name']`.

    Mastodon has no use for a Note with a `name`, and :196 always supplies one.
    The pair of deliveries in this one test is what makes the delete
    observable: the community's copy, serialized at :306 BEFORE the amendment,
    still carries `name`; the follower's copy, serialized at :352 after it,
    does not. Asserting only the absence would pass against a mutant that
    stopped :196 writing the key in the first place.

    :312's false arm is unreachable -- see the block comment above.
    """
    s = _seed(local_community=False, with_keys=True)
    community_route = _remote_inbox(s, http_mock)
    follower_route = _inward_follower(s, http_mock)

    _send(s.post)

    assert _page_of(community_route)['name'] == 'a post'
    assert 'name' not in _sent_activity(follower_route)['object']


def test_a_page_is_retyped_as_a_note(db_session, http_mock):
    """:316's FIRST disjunct -- `note['type'] == 'Page'` -- and :317.

    :168 gives an article the type 'Page'; :317 rewrites it to 'Note'. Both
    deliveries are asserted because the rewrite happens between them: without
    the community copy, a mutant that made :185 emit 'Note' from the start
    would pass.

    :316's SECOND disjunct, `note['type'] == 'Event'`, is taken by
    `test_an_event_note_carries_its_start_localised_to_the_event_timezone`
    above, which asserts the follower's object is a 'Note' for an Event post.
    Not duplicated here. :316's FALSE arm is
    `test_a_poll_note_keeps_its_question_type_and_stays_empty` below.
    """
    s = _seed(local_community=False, with_keys=True)
    community_route = _remote_inbox(s, http_mock)
    follower_route = _inward_follower(s, http_mock)

    _send(s.post)

    assert _page_of(community_route)['type'] == 'Page'
    assert _sent_activity(follower_route)['object']['type'] == 'Note'


def test_a_poll_note_keeps_its_question_type_and_stays_empty(db_session, http_mock):
    """Three false arms in one post shape, because one post shape is what
    produces all three:

      :316 FALSE  -- :164 types a poll 'Question', which is neither 'Page' nor
                     'Event', so :317 does not run and the type survives the
                     amendment.
      :318 FALSE  -- a poll is neither POST_TYPE_LINK nor POST_TYPE_VIDEO.
      :320 FALSE  -- `elif post.type != POST_TYPE_POLL` is exactly the poll
                     exclusion, so no title paragraph is written either.

    They are separately killed despite sharing a test: a mutant on :316 changes
    the asserted `type`, and a mutant on either :318 or :320 changes the
    asserted `content` (to the anchor paragraph or to the title paragraph
    respectively). The empty string is the whole point -- :191 gave the Page a
    real body for a poll and :315 threw it away, so `content == ''` also pins
    :315 for the one type where nothing writes it back.

    `post.body` is None, so :327 is false and nothing is appended afterwards.
    """
    s = _seed(post_type=POST_TYPE_POLL, local_community=False, with_keys=True)
    community_route = _remote_inbox(s, http_mock)
    follower_route = _inward_follower(s, http_mock)
    db.session.add(Poll(post_id=s.post.id, end_poll=None, mode='single'))
    db.session.commit()

    _send(s.post)

    assert _page_of(community_route)['content'] == '<p>a post</p>'
    note = _sent_activity(follower_route)['object']
    assert note['type'] == 'Question'
    assert note['content'] == ''


@pytest.mark.parametrize('post_type', [POST_TYPE_LINK, POST_TYPE_VIDEO])
def test_a_link_or_video_note_gets_an_anchor_to_its_url(db_session, http_mock,
                                                        post_type):
    """:318 TRUE -> :319, once per disjunct.

    Parametrized rather than written twice because the two disjuncts of
    `post.type == POST_TYPE_LINK or post.type == POST_TYPE_VIDEO` differ only
    in the constant: each parameter closes the other disjunct, so dropping
    either one from :318 fails exactly one case.

    The expected string is spelled out in full, unquoted `href` included, so it
    pins :319's concatenation verbatim -- production really does emit
    `<a href=...>` with no quotes. It also pins that :319 APPENDS to the ''
    that :315 just wrote (a mutant swapping `+=` for `=` is invisible here, but
    a mutant deleting :315 would leave the Page's own body in front of the
    anchor and fail).
    """
    s = _seed(post_type=post_type, url='https://example.com/x',
              local_community=False, with_keys=True)
    _remote_inbox(s, http_mock)
    follower_route = _inward_follower(s, http_mock)

    _send(s.post)

    note = _sent_activity(follower_route)['object']
    assert note['content'] == '<p><a href=https://example.com/x>a post</a></p>'


def test_an_article_note_gets_its_title_as_a_paragraph(db_session, http_mock):
    """:320 TRUE -> :321, and :331.

    An article is neither a link/video (so :318 is false and control reaches
    the `elif`) nor a poll (so :320 opens), and :321 writes the title
    paragraph.

    Three further arms are pinned by asserting `content` WHOLE rather than by
    substring, and by naming the two keys:

      :322 first conjunct FALSE -- an article is not an event, so no localised
           start is appended. Its TRUE arm is
           `test_an_event_note_carries_its_start_localised_to_the_event_timezone`
           and its second conjunct's false arm is
           `test_an_event_with_no_start_does_not_crash`; both are above and
           neither is duplicated here.
      :327 FALSE -- `post.body_html` is None, so :93 makes `post_body_html` ''
           and nothing is appended. READ THE NEXT PARAGRAPH BEFORE CREDITING
           THIS WITH A KILL.
      :329 FALSE -- `make_post` never sets `language_id`, so no `contentMap` is
           emitted. A mutant making :329 unconditional would emit
           `{'en': ...}`, because `Post.language_code` (app/models.py:2645-2649)
           falls back to 'en'; the absent key is what refuses it. Paired with
           `test_a_note_with_a_language_carries_a_content_map` below.

    :327's TRUE-direction mutant survives, and no test in this file can kill
    it. `post_body_html` is '' on this arm (:93), so forcing :327 open runs
    `note['content'] = note['content'] + ''`, which is the identity. The guard
    is an optimisation, not a behaviour, in the false direction; `if True:`
    there is an EQUIVALENT MUTANT. Measured, not assumed. What the pair with
    `test_a_post_body_is_appended_to_the_amended_note` below does kill is the
    other direction, `if False:`.

    :331 sets `inReplyTo` to None explicitly. `is None` is asserted rather than
    `not in`, because the key being PRESENT and null is the behaviour -- the
    Page never had the key, so deleting :331 removes it entirely.
    """
    s = _seed(local_community=False, with_keys=True)
    _remote_inbox(s, http_mock)
    follower_route = _inward_follower(s, http_mock)

    _send(s.post)

    note = _sent_activity(follower_route)['object']
    assert note['content'] == '<p>a post</p>'
    assert 'contentMap' not in note
    assert note['inReplyTo'] is None


def test_a_post_body_is_appended_to_the_amended_note(db_session, http_mock):
    """:327 TRUE -> :328, `note['content'] = note['content'] + post_body_html`.

    The mirror of the article test above, differing only in `body_html`. The
    whole-string assertion pins the CONCATENATION and its order: :321's title
    paragraph first, the rendered body after. A mutant replacing :328's
    concatenation with a plain assignment loses the title and fails here, while
    the article test above stays green.

    This is also the only test that can close :327 in the direction that has an
    observable: `if False:` there drops the body and fails here. The other
    direction is an equivalent mutant -- see the article test's docstring.
    """
    s = _seed(local_community=False, with_keys=True)
    _remote_inbox(s, http_mock)
    follower_route = _inward_follower(s, http_mock)
    s.post.body_html = '<p>the body</p>'
    db.session.commit()

    _send(s.post)

    note = _sent_activity(follower_route)['object']
    assert note['content'] == '<p>a post</p><p>the body</p>'


def test_a_note_with_a_language_carries_a_content_map(db_session, http_mock):
    """:329 TRUE -> :330, `note['contentMap'] = {post.language_code(): ...}`.

    The language is deliberately NOT English. `Post.language_code`
    (app/models.py:2645-2649) returns 'en' whenever `language_id` is unset, so
    a mutant that made :329 unconditional would still emit a `contentMap` --
    keyed 'en'. Asserting the key is 'fr' is what separates "the guard opened
    because a language is set" from "the guard was removed".

    The mapped value is asserted too, so :330 cannot be reduced to an empty or
    stale string: it must be the content as it stands AFTER :321 and :328.
    """
    s = _seed(local_community=False, with_keys=True)
    _remote_inbox(s, http_mock)
    follower_route = _inward_follower(s, http_mock)
    language = Language(code='fr', name='French')
    db.session.add(language)
    db.session.commit()
    s.post.language_id = language.id
    db.session.commit()

    _send(s.post)

    note = _sent_activity(follower_route)['object']
    assert note['contentMap'] == {'fr': '<p>a post</p>'}


# ---------------------------------------------------------------------------
# The follower fan-out, :339-352
# ---------------------------------------------------------------------------
#
# :340-341's EARLY RETURN IS ALREADY EXERCISED and is not given a test of its
# own. Every test in this file that never builds a UserFollower row takes it --
# `test_a_remote_community_receives_the_create_at_its_inbox` above is the
# clearest, since it asserts the community route fired exactly once and so
# would notice anything sent afterwards. Adding a test for it would also be
# adding a test that cannot fail: deleting :340-341 changes nothing, because
# `followers` is then empty, :344's loop has nothing to iterate and
# `user.following_instances()` -- which joins the same UserFollower rows
# (app/models.py:1668-1670) -- returns []. The arc is covered; the statement
# has no observable of its own.
#
# TWO CONJUNCTS IN THIS BLOCK ARE UNREACHABLE, both for the reason Task 6
# recorded at :295: `User.following_instances` (app/models.py:1667-1676)
# already applies the same filters in SQL, so the Python re-check can only ever
# see the value it filtered for.
#
#   :350's `instance.id != 1` -- app/models.py:1673 is
#   `instances.filter(Instance.id != 1, Instance.gone_forever == False)`,
#   unconditionally. The local instance can never be yielded, so the conjunct
#   is never False.
#
#   :351's `instance.online()` -- `Instance.online()` is
#   `not (self.dormant or self.gone_forever)` (app/models.py:118-119), and
#   app/models.py:1672-1673 filters `Instance.dormant == False` and
#   `Instance.gone_forever == False`. Every instance the loop can see is
#   already online.
#
# Registered as findings; not chased. MEASURED, NOT ASSUMED: deleting either
# conjunct from its `if` leaves every test in this file green, while deleting
# any of the other five fails exactly the test named for it.
#
# The remaining five conjuncts each get a test below.


def test_each_follower_is_appended_to_the_activitys_cc(db_session, http_mock):
    """:346 TRUE -> :347, `create['cc'].append(user_details.public_url())`.

    THE MENTION IS LOAD-BEARING, not scenery. With no mention, `create['cc']`
    would be `[]` at :347 and a mutant replacing the `.append` with an
    assignment would produce the identical one-element list. Seeding cc with a
    mentioned recipient at :174 first means the assertion pins BOTH that the
    follower was added AND that what was already there survived, in order.

    The mentioned user lives on the community's own instance, so :335 refuses a
    second delivery to them (its domain went into `domains_sent_to` at :307) --
    which is why no route is registered for that instance. That behaviour is
    `test_the_remote_communitys_domain_is_marked_as_sent_to`'s subject, not
    this test's; it is relied on here only to keep the request count at two.
    """
    s = _seed(local_community=False, with_keys=True)
    _remote_inbox(s, http_mock)
    follower_route = _inward_follower(s, http_mock)
    mentioned = make_user(s.community.instance, 'remoteuser', local=False)
    s.post.body = 'hello @remoteuser@peer.example'
    db.session.commit()

    _send(s.post)

    assert _sent_activity(follower_route)['cc'] == [
        mentioned.public_url(), 'https://follower.example/users/fan']


def test_a_follower_row_with_no_user_is_skipped(db_session, http_mock):
    """:346 FALSE -- `session.query(User).get(follower.remote_user_id)` returns
    None, so :347 is skipped for that row.

    THE ONLY SHAPE THE DATABASE ALLOWS IS A NULL remote_user_id. The brief
    suggested a row whose `remote_user_id` "matches no User"; Postgres refuses
    that outright -- `UserFollower.remote_user_id` is
    `db.ForeignKey('user.id')` (app/models.py:3568) and inserting a dangling
    integer raises IntegrityError, measured. The column is nullable, though, so
    a NULL is insertable, and `Query.get(None)` returns None (with a SAWarning
    that a fully NULL identity cannot load an object). That is the arm.

    A SECOND, REAL FOLLOWER IS REQUIRED for the test to observe anything: the
    NULL row is invisible to `user.following_instances()` at :349, which joins
    `UserFollower.remote_user_id == User.id` (app/models.py:1669), so on its
    own it produces no delivery and no body to assert against.

    The discriminator is twofold. Removing :346 makes :347 call
    `.public_url()` on None, and `send_post` is called directly by `_send`, so
    the AttributeError propagates and the test errors rather than fails
    quietly. The `cc` assertion additionally refuses a mutant that skipped the
    row but appended something else in its place.
    """
    s = _seed(with_keys=True)
    route = _inward_follower(s, http_mock)
    db.session.add(UserFollower(local_user_id=s.user.id, remote_user_id=None,
                                is_inward=True))
    db.session.commit()

    _send(s.post)

    assert _sent_activity(route)['cc'] == ['https://follower.example/users/fan']


def test_a_follower_on_an_already_delivered_domain_is_skipped(db_session, http_mock):
    """:350 FIRST conjunct FALSE -- `instance.domain not in domains_sent_to`.

    The follower is planted on the community's OWN instance, whose domain :307
    has already appended to `domains_sent_to`, and that instance's `inbox` is
    pointed at the same URL the community's `ap_inbox_url` uses. So if the
    conjunct were removed the same route would be hit a second time, and the
    call count -- not the presence or absence of an HTTP mock -- is the
    witness.

    `ActivityPubLog` is counted as well, because a mutant that reached :352
    with a DIFFERENT destination would miss the registered route, and
    `post_request` (app/activitypub/signature.py:103-105) writes its log row
    before the request is attempted and swallows the transport error at
    :143-145. One row means one send was attempted, full stop.

    No route is registered for the follower: `http_mock` is
    `assert_all_called=True`, so a route this test expects never to fire would
    fail it for the wrong reason.
    """
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)
    _inward_follower(s, instance=s.community.instance, inbox=PEER_INBOX)

    _send(s.post)

    assert len(route.calls) == 1
    assert ActivityPubLog.query.count() == 1


def test_a_piefed_follower_instance_is_skipped(db_session, http_mock):
    """:350 THIRD conjunct FALSE -- `instance.software != 'piefed'`.

    A PieFed peer gets the post through the community's own machinery, so the
    Mastodon-shaped amended copy is not sent to it a second time. The instance
    is otherwise fully qualified -- a third domain, an inbox, online,
    unblocked, unbanned -- so this test closes that conjunct and nothing else.

    Reached only because the community here is REMOTE: on a local community
    :294 would iterate `user.following_instances(software='piefed')` and send
    this same instance an Announce, which would put a second `ActivityPubLog`
    row in the way of the witness.

    THE WITNESS IS THE LOG ROW COUNT, not the absence of an HTTP request:
    dropping the conjunct sends to `https://piefed.example/inbox`, which no
    route serves, and `post_request` turns that into a `result='failure'` row
    rather than re-raising. Counting rows sees it; "no HTTP happened" would
    not. One row is the community delivery at :306.
    """
    s = _seed(local_community=False, with_keys=True)
    _remote_inbox(s, http_mock)
    _inward_follower(s, domain='piefed.example', software='piefed',
                     inbox='https://piefed.example/inbox')

    _send(s.post)

    assert ActivityPubLog.query.count() == 1


def test_a_follower_instance_with_no_inbox_gets_no_amended_copy(db_session, http_mock):
    """:351 FIRST conjunct FALSE -- `if instance.inbox and ...`.

    Distinct from `test_an_inboxless_follower_instance_is_skipped` above, which
    is the same column read at :295 on the community Announce path; this is the
    user fan-out at :351.

    `Instance.inbox` is nullable, `make_instance` leaves it None, and no filter
    in `User.following_instances` mentions it, so the instance genuinely
    reaches :351 and is refused there.

    THE WITNESS IS THE ActivityPubLog COUNT. Dropping `instance.inbox and`
    calls `send_post_request(None, create, ...)`, and `post_request`
    (app/activitypub/signature.py:109-111) turns a None uri into an
    'empty uri' failure row without touching the transport -- so a test
    asserting only that no HTTP request happened would pass against that
    mutant. The single row this asserts is the community delivery at :306.
    """
    s = _seed(local_community=False, with_keys=True)
    _remote_inbox(s, http_mock)
    _inward_follower(s, with_inbox=False)

    _send(s.post)

    assert Instance.query.filter_by(domain='follower.example').one().inbox is None
    assert ActivityPubLog.query.count() == 1


def test_a_follower_instance_the_author_has_blocked_is_skipped(db_session, http_mock):
    """:351 THIRD conjunct FALSE -- `not user.has_blocked_instance(instance.id)`.

    `User.has_blocked_instance` (app/models.py:1467-1471) is an
    `InstanceBlock` lookup on (user_id, instance_id), which is exactly what
    `make_instance_block` writes. The follower's instance is otherwise fully
    qualified, so the block is the only thing closing the guard.

    The first assertion is the guard-opens proof: without it, a mis-seeded
    block row would leave this test passing for the wrong reason -- and it
    would still pass if the follower simply never reached :351 at all.
    """
    s = _seed(local_community=False, with_keys=True)
    _remote_inbox(s, http_mock)
    _inward_follower(s)
    follower_instance = Instance.query.filter_by(domain='follower.example').one()
    make_instance_block(s.user, follower_instance)

    assert s.user.following_instances() == [follower_instance]

    _send(s.post)

    assert ActivityPubLog.query.count() == 1


def test_a_defederated_follower_instance_is_skipped(db_session, http_mock):
    """:351 FOURTH conjunct FALSE -- `not instance_banned(instance.domain)`.

    `instance_banned` (app/utils.py:2335-2364 by `ast`; this cited 2334-2359
    until the final review of sub-project 19) is a `BannedInstances` lookup on
    `domain`. Its `@cache.memoize` decorator is inert here: tests/conftest.py:68
    sets `CACHE_TYPE = 'NullCache'`, so no verdict leaks between tests.

    The ban is on the FOLLOWER's domain, not the community's -- :160's
    `instance_banned(community.instance.domain)` guard is a different call site
    and would have returned before the builder, which would make this test pass
    for the wrong reason. The first assertion pins that separation by showing
    the community delivery still happened.
    """
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)
    _inward_follower(s)
    db.session.add(BannedInstances(domain='follower.example'))
    db.session.commit()

    _send(s.post)

    assert len(route.calls) == 1
    assert ActivityPubLog.query.count() == 1


def _make_deliverable(s, inbox=PEER_INBOX):
    """Attach the community to a real peer Instance and give it an inbox.

    The half of `_remote_inbox` that touches the database and nothing else.
    The early-return tests call this ALONE, because they assert the delivery
    never happens and `http_mock` is built with `assert_all_called=True` --
    registering a route they expect never to fire would fail them for the
    wrong reason.

    Defined here at the end of the file rather than beside `_remote_inbox`
    because 11 committed citations point into this file and a mid-file
    insertion would shift every one below it.
    """
    s.community.instance_id = _peer().id
    s.community.ap_inbox_url = inbox
    db.session.commit()


def _recording_task_session(monkeypatch):
    """Make `get_task_session` hand back a GENUINE Session that records
    `rollback()` and `close()`.

    Ported from tests/test_shared_tasks_send_reply.py -- copied rather than
    imported across test modules, which is this campaign's deliberate pattern.

    The session is real and does real work; only the observation is added, by
    wrapping the two methods rather than replacing the object. A fake session
    would prove the wrapper calls methods on a mock; this proves it calls them
    on the session the function actually used.

    THE PATCH TARGET IS THE `pages` MODULE. `app/shared/tasks/pages.py`
    imports `get_task_session` into its own namespace, so patching
    `app.utils.get_task_session` would miss the binding `make_post`,
    `edit_post` and `move_post` actually call -- and the tests would pass
    while observing nothing.

    Returns a `SimpleNamespace(calls=[])`; the wrapper appends 'rollback' and
    'close' in the order they happened, so `finally` running after `except` is
    observable rather than assumed.
    """
    from app import db as _db
    from sqlalchemy.orm import Session as _Session
    import app.shared.tasks.pages as pages_module

    record = SimpleNamespace(calls=[])

    def _make():
        session = _Session(bind=_db.engine)
        real_rollback, real_close = session.rollback, session.close

        def rollback():
            record.calls.append('rollback')
            return real_rollback()

        def close():
            record.calls.append('close')
            return real_close()

        session.rollback = rollback
        session.close = close
        return session

    monkeypatch.setattr(pages_module, 'get_task_session', _make)
    return record


def _move(s, target=None):
    """`move_object(session, user_id, object, origin, target)`.

    `origin` is always the seeded community; `target` defaults to a SECOND
    community so the two differ, which is what a real move means. Both must be
    `Community` instances or `:393`'s guard raises.
    """
    if target is None:
        target = make_community('c2')
        db.session.commit()
    return move_object(db.session, s.user.id, s.post, origin=s.community,
                       target=target)


def test_a_remote_community_receives_the_bare_move(db_session, http_mock):
    """The harness itself: `:416`'s FALSE arm reaches `:435` and the bytes are
    readable. Every later move test depends on this working."""
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    _move(s)

    assert route.called
    assert _sent_activity(route)['type'] == 'Move'


def test_a_local_only_community_does_not_federate_the_move(db_session, http_mock):
    """:398's TRUE arm via `local_only`, returning at :399."""
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    s.community.local_only = True
    db.session.commit()

    _move(s)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_dormant_instance_does_not_receive_the_move(db_session, http_mock):
    """:398's TRUE arm via `not instance.online()`.

    `Instance.online()` (app/models.py:118-119) is
    `not (self.dormant or self.gone_forever)`, so setting `dormant` on the
    community's OWN instance closes it. `_make_deliverable` reassigns
    `community.instance_id` to the peer before its commit, so this lands on
    the instance the guard actually reads.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    s.community.instance.dormant = True
    db.session.commit()

    _move(s)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_private_community_does_not_federate_the_move(db_session, http_mock):
    """:398's `private` conjunct -- the first of this sub-project's two
    production changes.

    `Community.private` (app/models.py:611) is commented "only members can
    view. no federation.", and before this conjunct landed `:398` tested only
    `local_only` and `instance.online()` -- so a private, non-local-only
    community federated its moves out.

    D309's THIRD closed site of ten; sub-projects 20 and 21 closed
    `notes.py:143` and `notes.py:248`. `local_only` is left False
    deliberately: with it True the test would pass on the pre-existing
    conjunct and prove nothing.

    NEITHER SIBLING GUARD IS A TEMPLATE. `pages.py:153` is
    `local_only or private` with NO `online()` check; `notes.py:248` is
    `local_only or private or not instance.online()`. This one matches :248,
    because :398 already tests `online()`.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    s.community.private = True
    db.session.commit()

    _move(s)

    assert db.session.query(ActivityPubLog).count() == 0


# Imported here rather than in the import block at the top of this file:
# 11 committed citations point into this file, and a new line at :72 would
# shift every one of them by one. Appending costs nothing.
from app.utils import TaskError


def test_a_non_community_origin_raises_task_error(db_session):
    """:393's FALSE arm via `origin`, raising at :396 -- the second of this
    sub-project's two production changes.

    `move_object` requires BOTH `origin` and `target` to be `Community`. This
    test closes the guard on `origin`; its companion below closes it on
    `target`. TWO tests are needed, not one: a mutant changing `and` to `or`
    is killed by neither alone, because either single non-Community argument
    still leaves the other's isinstance True.

    Raising `TaskError` rather than a bare `Exception` is the change. A caller
    can now catch this failure without catching everything -- `move_post`'s
    handler at :383 is `except Exception:` and so is unaffected today.
    """
    s = _seed(with_keys=True)
    target = make_community('c2')
    db.session.commit()

    with pytest.raises(TaskError):
        move_object(db.session, s.user.id, s.post, origin=s.post, target=target)


def test_a_non_community_target_raises_task_error(db_session):
    """:393's FALSE arm via `target` -- the companion to the test above, and
    the half that makes an `and`->`or` mutant die."""
    s = _seed(with_keys=True)

    with pytest.raises(TaskError):
        move_object(db.session, s.user.id, s.post, origin=s.community,
                    target=s.post)


def test_the_remote_move_carries_its_full_key_set_and_keeps_its_context(
        db_session, http_mock):
    """:416's FALSE arm, delivered by :435.

    `@context` is asserted PRESENT because nothing nests this object on this
    path -- :417's `del` runs only under `is_local()`, which is not taken
    here, so the `@context` built at :409 survives to the wire.

    `origin` and `target` are the two fields that distinguish a Move from
    every other activity this module sends, and they must differ: asserting
    both is what catches a mutant that passed the same community twice.
    """
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)
    target = make_community('c2')
    db.session.commit()

    _move(s, target=target)

    sent = _sent_activity(route)
    assert sent['type'] == 'Move'
    assert set(sent) == {'id', 'type', 'actor', 'object', '@context',
                         'origin', 'target', 'to', 'cc'}
    assert sent['actor'] == s.user.public_url()
    assert sent['object'] == s.post.public_url()
    assert sent['origin'] == s.community.public_url()
    assert sent['target'] == target.public_url()
    assert sent['origin'] != sent['target']
    assert sent['to'] == ['https://www.w3.org/ns/activitystreams#Public']
    assert sent['cc'] == [s.community.public_url()]


def test_the_remote_move_is_signed_as_the_user(db_session, http_mock):
    """:435 signs with `user.public_url() + '#main-key'`, where :433 signs as
    the COMMUNITY. `keyId` is the only observable that separates them, and the
    user's and community's public urls differ in path, so a mutant swapping
    the signer produces a valid but wrong keyId and this fails rather than
    merely not-noticing.
    """
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    _move(s)

    assert _key_id_of(route) == s.user.public_url() + '#main-key'


def test_a_local_community_announces_the_move_and_strips_its_inner_context(
        db_session, http_mock):
    """:416's TRUE arm -- :417's `del move['@context']` and the Announce built
    at :422-430.

    The Announce keeps the `@context` built at :427; the Move nested at :426
    has had its own stripped at :417. Asserting BOTH directions is what
    catches a mutant that deletes from the wrong object -- either alone would
    still accept a well-formed activity.

    `cc` is the community's followers collection here (:421), NOT the
    `[community.public_url()]` the inner Move carries (:403) -- :421 rebinds
    the name to a NEW list, so the inner object's `cc` still points at the
    old one. Asserting both proves the rebinding did not alias.
    """
    s = _seed(with_keys=True)
    fan = _community_follower(s, http_mock)

    _move(s)

    sent = _sent_activity(fan.route)
    assert sent['type'] == 'Announce'
    assert '@context' in sent
    assert sent['actor'] == s.community.public_url()
    assert sent['object']['type'] == 'Move'
    assert '@context' not in sent['object']
    assert sent['cc'] == [s.community.ap_followers_url]
    assert sent['object']['cc'] == [s.community.public_url()]


def test_the_announced_move_is_signed_as_the_community(db_session, http_mock):
    """:433 signs with `community.private_key` and
    `community.public_url() + '#main-key'` -- the companion to Task 4's
    user-signed assertion."""
    s = _seed(with_keys=True)
    fan = _community_follower(s, http_mock)

    _move(s)

    assert _key_id_of(fan.route) == s.community.public_url() + '#main-key'


def test_a_local_community_with_no_followers_sends_no_move(db_session, http_mock):
    """:431's loop never entered -- the arc straight past the loop.

    `following_instances()` returns empty because no CommunityMember exists on
    a remote instance. The Announce is still BUILT at :422-430; nothing
    delivers it.
    """
    s = _seed(with_keys=True)

    _move(s)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_following_instance_without_an_inbox_gets_no_move(db_session, http_mock):
    """:432's FALSE arm -- the loop continuing.

    `with_inbox=False` leaves `Instance.inbox` None, closing :432's FIRST
    conjunct before any of the other three is evaluated. No route is
    registered, so `http_mock`'s `assert_all_called=True` is not tripped, and
    the count of 0 rules out a request attempted against a None inbox.
    """
    s = _seed(with_keys=True)
    _community_follower(s, http_mock, with_inbox=False)

    _move(s)

    assert db.session.query(ActivityPubLog).count() == 0


def test_one_following_instance_is_skipped_while_another_receives_the_move(
        db_session, http_mock):
    """Both loop arms in ONE run -- the skip, then the delivery.

    The discriminating case: a single-instance test cannot show that the guard
    skips an instance WITHOUT also stopping the loop, because with one member
    "skipped" and "loop ended" look identical. With two, the delivered one
    proves iteration continued past the skipped one.

    THE ORDER ASSERTION IS LOAD-BEARING AND NOT DECORATION.
    `Community.following_instances()` (app/models.py:842-851) ends in an
    unordered `.distinct().all()` -- no ORDER BY -- so the order is a property
    of Postgres's query plan, not of the code. If it ever reverses, a mutant
    turning "skip and continue" into "skip and break" would still leave the
    delivered route called once, and this test would pass while no longer
    proving what it claims.
    """
    s = _seed(with_keys=True)
    _community_follower(s, http_mock, domain='mute.example',
                        member_name='mute', with_inbox=False)
    good = _community_follower(s, http_mock, domain='fan.example',
                               member_name='fan')

    ordered = [i.domain for i in s.community.following_instances()]
    assert ordered == ['mute.example', 'fan.example'], (
        f'this test proves the loop CONTINUES past a skip, which requires the '
        f'skipped instance first; got {ordered}')

    _move(s)

    assert good.route.call_count == 1
    assert db.session.query(ActivityPubLog).count() == 1
