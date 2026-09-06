"""`send_reply` -- the Celery-path builder and deliverer of an ActivityPub Note.

`app/shared/tasks/notes.py:80-229`. This is the TWIN of `send_post`
(`app/shared/tasks/pages.py:88-352`), which sub-project 19 took to two
unreachable statements and three unreachable arms. The two functions share a
mention scan, early returns, a Create/Announce construction and a per-instance
fan-out -- so the DIFFERENCES between them are this sub-project's findings.

FIVE DIVERGENCES, established before any test was written:

1. `:92` scans `reply.body` with `re.finditer` and NO guard, where
   `pages.py:97` guards its scan with `if post.body:`. `PostReply.body` is
   `db.Column(db.Text)` (`app/models.py:2901`), so None is storable. Whether a
   None-bodied reply can REACH this line is measured in this file, not assumed.

2. `:143` does not test `community.private`, and `pages.py:153` does. That flag
   is commented "only members can view. no federation" (`app/models.py:611`),
   and `pages.py` is the only one of TEN senders in `app/shared/tasks/` that
   honours it. The leak is LATENT: both writers of the flag couple it to
   `local_only` in the view layer (`app/community/routes.py:103-104` and
   `:1230-1231`), so no current path produces `private=True, local_only=False`,
   and `:143`'s `local_only` test already catches every private community that
   exists.

3. `:217`'s `instance.online()` cannot be False. `:216` calls
   `community.following_instances()` with the default `include_dormant=False`,
   so `app/models.py:849-850` already filters `dormant` and `gone_forever` in
   SQL, and `Instance.online()` (`:118-119`) is exactly
   `not (dormant or gone_forever)`.

4. `:203`/`:225` repeat an `@context` del/re-add pair sub-project 19 proved
   yields an EQUIVALENT MUTANT: `post_request`
   (`app/activitypub/signature.py:100-101`) re-adds the key when a body lacks
   it, same value and same trailing position, before serialization. Tests here
   assert KEY ORDER and say so rather than claiming a kill.

5. There is NO amendment block. `send_post` rewrites its Page into a Note at
   `:309-332`, which is where its aliasing hazard lives; this function is
   already a Note. Assertions still read serialized bytes, because `:203`'s
   `del` mutates `create` in place.

ENTRY. `send_reply(reply_id, parent_id, edit=False, session=None)`. `session`
has no usable default -- `:81` dereferences it immediately. `parent_id` is a
BRANCH, not a convenience: truthy loads a PostReply as the parent (`:84`),
falsy uses `reply.post` (`:86`), and the choice changes what `:175`'s
`parent.public_url()` emits.

RECIPIENTS START SEEDED. `:90` is `recipients = [parent.author]`, where
`send_post` starts from `[]`. Then `:120` is
`if recipient.is_local() and recipient.id != parent.author.id:` -- so the
parent's author is always a delivery recipient but is deliberately excluded
from the mention notification. That interaction has no analogue in the twin.

THREE EARLY RETURNS stand between entry and the builder: `:143-144`,
`:147-148`, `:149-151`. `send_post` has a fourth only because it tests
`community.private`.
"""

import json
import socket
from types import SimpleNamespace

import pytest

from app import db
from app.constants import NOTIF_MENTION
from app.models import (
    ActivityPubLog, BannedInstances, CommunityBan, Instance, Notification,
    PostReply,
)
from app.shared.tasks.notes import send_reply
from tests.factories import (
    make_community, make_community_ban, make_instance, make_instance_block,
    make_post, make_post_reply, make_user,
)


def _seed(body='a reply', with_parent_reply=False, local_community=True,
          with_keys=False):
    """instance, author, community, post, reply -- and the parent the call needs.

    ORDER IS LOAD-BEARING. `make_community` hardcodes `instance_id=1` and
    tests/conftest.py:143 truncates with RESTART IDENTITY, so the local instance
    is created first; a peer built before this call would take id 1 and leave
    the community's FK pointing at it.

    `with_parent_reply=True` makes the parent a PostReply, which is `:83`'s
    true arm; the default leaves it the Post, which is `:86`. `make_post_reply`
    (tests/factories.py:453) does not set `parent_id` or `depth`, so a nested
    reply sets them here.

    `local_community=False` sets `ap_id` AND `ap_profile_id` on peer.example,
    because `Community.is_local()` (app/models.py:795-796) is a DISJUNCTION and
    `ap_id` alone leaves it local.

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
    if with_parent_reply:
        parent = make_post_reply(post, user, body='the parent reply')
        reply = make_post_reply(post, user, body=body)
        reply.parent_id = parent.id
        reply.depth = 1
    else:
        parent = post
        reply = make_post_reply(post, user, body=body)
    if not local_community:
        community.ap_id = 'c1@peer.example'
        community.ap_profile_id = 'https://peer.example/c/c1'
        community.ap_public_url = 'https://peer.example/c/c1'
        community.ap_followers_url = 'https://peer.example/c/c1/followers'
        community.ap_domain = 'peer.example'
    db.session.commit()
    return SimpleNamespace(instance=instance, user=user, community=community,
                           post=post, reply=reply, parent=parent)


def _peer(domain='peer.example', software='lemmy'):
    """A remote Instance. ALWAYS call this AFTER `_seed()` -- see `_seed`'s
    docstring for why the order matters."""
    return make_instance(domain, software=software)


def _send(s, edit=False):
    """`send_reply` takes an explicit session; there is no usable default.

    `parent_id` is passed only when the seeded parent is a PostReply. That is
    the whole of `:83`'s dispatch: a falsy `parent_id` sends the function down
    `:86` to `reply.post` instead.
    """
    parent_id = s.parent.id if isinstance(s.parent, PostReply) else None
    return send_reply(s.reply.id, parent_id, edit=edit, session=db.session)


def _reauthor_the_parent(s, name='op'):
    """Give the reply's PARENT a different local author from the reply's.

    `:90` seeds `recipients` with `parent.author`, and `:120` excludes that same
    user from notification. When the reply's author IS the parent's author --
    which every `_seed()` produces -- those two facts overlap and hide each
    other: a mention of the author is deduped at `:111` whether or not `:97`
    skipped it, and the author is un-notifiable at `:120` whether or not `:108`
    collected them. Splitting the two identities is what makes those arms
    observable at all.
    """
    op = make_user(s.instance, name, local=True)
    s.parent.user_id = op.id
    db.session.commit()
    db.session.expire(s.parent)
    return op


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


def _make_deliverable(s, inbox=PEER_INBOX):
    """Attach the community to a real peer Instance and give it an inbox.

    The half of `_remote_inbox` below that touches the database and nothing
    else. `_remote_inbox` calls this and then registers the route; the private
    gate tests call it ALONE, because they assert that the delivery never
    happens and `http_mock` is `assert_all_called=True` -- registering a route
    they expect never to fire would fail them for the wrong reason. Read
    `_remote_inbox`'s docstring for why each of these two assignments is
    required before `send_reply` can reach the transport at all.
    """
    s.community.instance_id = _peer().id
    s.community.ap_inbox_url = inbox
    db.session.commit()


def _remote_inbox(s, http_mock, inbox=PEER_INBOX):
    """THE CAPTURE MECHANISM for anything `send_reply` writes into `note` or
    `create`. Returns the respx route the outbound Create lands on; pair it
    with `_sent_activity` below.

    Everything the builder produces between :153 and :197 lives in two locals,
    `note` and `create`, and is never persisted. The only way to read them is to
    let the function actually deliver. notes.py:220-222 is the cheapest route to
    that: the `else` arm of :202, taken when the community is NOT local, calls
    `send_post_request(community.ap_inbox_url, create, ...)` with
    `create['object']` still bound to `note` (:192).

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

    WHY THE HTTP BODY AND NOT A MONKEYPATCHED RECORDER. This was ported from
    `tests/test_shared_tasks_send_post.py`, where `page` keeps being mutated
    after delivery by the :309-332 amendment block, so a recorder holding the
    dict would be read back in its post-amendment state. `send_reply` has NO
    amendment block -- divergence 5 in the module docstring -- so that exact
    hazard is milder here, but the approach is kept because :203's
    `del create['@context']` still mutates `create` in place, and because a
    serialized snapshot is what actually left the process. respx captures the
    request bytes at app/activitypub/signature.py:494.

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
    _make_deliverable(s, inbox)
    return http_mock.post(inbox).respond(200, json={})


def _sent_activity(route, index=-1):
    """The JSON body of the request `route` captured, decoded from the bytes
    that were actually sent. Defaults to the most recent call."""
    return json.loads(route.calls[index].request.content)


def _peer_instance(s):
    """The Instance `_remote_inbox` attached the community to.

    `_remote_inbox` builds its peer internally and does not hand it back, and
    calling `_peer()` a second time would insert a SECOND row for
    peer.example -- `Instance.domain` carries no unique constraint, so that
    would not fail, it would just quietly give a mentioned remote user a
    different instance row from the community's and change which domains
    :228 considers already-sent-to.
    """
    return db.session.query(Instance).get(s.community.instance_id)


# ---------------------------------------------------------------------------
# Entry and parent dispatch, :80-90
# ---------------------------------------------------------------------------


def test_a_top_level_reply_names_the_post_as_its_inReplyTo(db_session, http_mock):
    """:83's FALSE arm and :86. `parent_id` is None, so `parent = reply.post`
    and `:175`'s `inReplyTo` is the POST's public url.

    The delivered body is the witness. Asserting only that no notification
    appeared would pass against a broken dispatch, because the post's author is
    excluded at :120 either way.
    """
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    _send(s)

    assert _sent_activity(route)['object']['inReplyTo'] == s.post.public_url()


def test_a_nested_reply_names_its_parent_reply_as_its_inReplyTo(db_session, http_mock):
    """:83's TRUE arm and :84. `parent_id` is set, so the parent is a PostReply
    and `:175`'s `inReplyTo` is the PARENT REPLY's public url -- a different
    value from the test above, which is what makes the pair discriminating."""
    s = _seed(with_parent_reply=True, local_community=False, with_keys=True)
    assert isinstance(s.parent, PostReply)
    route = _remote_inbox(s, http_mock)

    _send(s)

    assert _sent_activity(route)['object']['inReplyTo'] == s.parent.public_url()
    assert s.parent.public_url() != s.post.public_url()


# ---------------------------------------------------------------------------
# Mention scan, :91-116
# ---------------------------------------------------------------------------


def test_a_body_with_no_mentions_never_enters_the_scan_loop(db_session):
    """:93, the zero-iteration case -- `re.finditer` yields nothing, so the
    loop body at :94-116 never runs and `recipients` stays exactly `:90`'s
    `[parent.author]`.

    The witness is that no Notification exists. That is only meaningful as half
    of a pair: `test_a_local_mention_resolves_and_is_notified` below runs the
    same seed WITH an `@name@host` in the body and gets a row, so the difference
    between the two isolates the scan. Note that :92 has no `if reply.body:`
    guard (divergence 1), so this test proves the loop is empty, not that the
    scan was skipped.
    """
    s = _seed(body='a reply with no mentions in it at all')

    _send(s)

    assert Notification.query.count() == 0


def test_a_local_mention_resolves_and_is_notified(db_session):
    """:95's TRUE arm (:96-101) plus :108's true arm and :115-116.

    `current_app.config['SERVER_NAME']` is 'test.piefed.local'
    (tests/conftest.py:69), which is what `_seed` gives the local instance, so
    `@mentioned@test.piefed.local` compares equal at :95 and takes the local
    branch. `subtype` is 'comment_mention' here, where the twin writes
    'post_mention'.
    """
    s = _seed(body='hello @mentioned@test.piefed.local')
    mentioned = make_user(s.instance, 'mentioned', local=True)
    assert len({s.user.id, mentioned.id}) == 2

    _send(s)

    notifications = Notification.query.filter_by(user_id=mentioned.id).all()
    assert len(notifications) == 1
    assert notifications[0].notif_type == NOTIF_MENTION
    assert notifications[0].subtype == 'comment_mention'


def test_a_remote_mention_resolves_and_reaches_the_delivered_tags(db_session, http_mock):
    """:95's FALSE arm -- :102-107 builds `name@host` and resolves it -- with
    :108's true arm and :115-116 appending the result.

    THE DELIVERED `tag` IS THE WITNESS, not a Notification. A remote recipient
    never gets one (:120 tests `recipient.is_local()`), so an absence assertion
    could not tell a resolved remote mention from an unresolved one. `:157`
    writes one `tag`/`cc` entry per recipient, so the remote user's
    `public_url()` appearing there is proof the else arm resolved them and
    :116 appended them.

    The mentioned user is put on the SAME peer the community lives on, so
    :228's `domains_sent_to` check suppresses a second delivery and the route
    below sees exactly one call.
    """
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)
    remote = make_user(_peer_instance(s), 'remoteuser', local=False)
    s.reply.body = 'hello @remoteuser@peer.example'
    db.session.commit()

    _send(s)

    note = _sent_activity(route)['object']
    assert [t['href'] for t in note['tag']] == [s.user.public_url(),
                                                remote.public_url()]
    assert remote.public_url() in note['cc']
    assert Notification.query.filter_by(user_id=remote.id).count() == 0


def test_the_author_mentioning_themselves_is_never_looked_up(db_session):
    """:97's FALSE arm -- `if user_name != user.user_name:`.

    THIS TEST ONLY WORKS BECAUSE THE PARENT HAS A DIFFERENT AUTHOR. With
    `_seed`'s default the reply's author IS `parent.author`, so a self-mention
    that slipped past :97 would be deduped at :111 against `:90`'s seeded
    entry and excluded at :120 for being `parent.author` -- the guard would be
    unobservable. `_reauthor_the_parent` hands the post to 'op', so if :97
    stopped skipping, `search_for_user('author')` would resolve, :111 would
    NOT match 'op', :116 would append the author, and :120 would notify them
    because their id differs from `parent.author.id`. Zero notifications is
    therefore a real witness for the guard.
    """
    s = _seed(body='hello @author@test.piefed.local')
    op = _reauthor_the_parent(s)
    assert op.id != s.user.id

    _send(s)

    assert Notification.query.count() == 0


def test_a_banned_remote_host_mention_is_swallowed_by_the_remote_except(db_session):
    """:104-107 -- the only REACHABLE bare `except: pass` in this scan.

    `search_for_user` for `name@host` takes app/user/utils.py:94's true branch,
    and :98 raises when the host has a `BannedInstances` row. That reaches
    notes.py:105's call inside the try, and :106-107 swallows it. If `_send`
    propagated the exception, this test would error -- passing is the witness
    that the except fires.

    THE LOCAL ARM'S EXCEPT AT :100-101 IS UNREACHABLE FOR EVERY MENTION, and no
    test here claims otherwise. For a bare local name app/user/utils.py:88
    finds no '@', so :91-92 sets `server = ''`, :94 is False and the function's
    only `raise` at :98 is skipped; a nonexistent local name then falls through
    :103 and :105 to :108-109's clean `return None`. Sub-project 19 established
    this against the same function. It is not dead in the stronger sense -- the
    clause is bare, so a SQLAlchemyError out of app/user/utils.py:101 would
    still land in it -- and it is registered for the residual sweep rather than
    chased.
    """
    s = _seed()
    db.session.add(BannedInstances(domain='peer.example', reason='test'))
    s.reply.body = 'hello @someone@peer.example'
    db.session.commit()

    _send(s)

    assert Notification.query.count() == 0


def test_an_unresolvable_local_mention_adds_no_recipient(db_session):
    """:108's FALSE arm -- `search_for_user` returned None, so :109-116 is
    skipped entirely and nothing is appended.

    'nobody' matches no User row, and app/user/utils.py:108-109 returns None
    without raising (see the previous test's docstring), so `recipient` is
    falsy at :108. The parent is re-authored so that an appended recipient
    WOULD have produced a notification -- without that, :120's exclusion of
    `parent.author` would mask the difference.
    """
    s = _seed(body='hello @nobody@test.piefed.local')
    _reauthor_the_parent(s)

    _send(s)

    assert Notification.query.count() == 0


def test_a_mention_of_the_parent_author_is_deduped_against_the_seeded_recipient(
        db_session, http_mock):
    """:110-114 with `add_recipient` going FALSE on the FIRST iteration, via
    :111's FIRST disjunct (`not recipient.ap_id and user_name ==`).

    THIS IS THE DEDUP CASE THE TWIN CANNOT HAVE. `send_post` starts from `[]`,
    so its first mention always appends; `:90` seeds `[parent.author]`, so the
    very first mention is already compared against something. Here the mention
    IS the parent's author, so the existing recipient it collides with is
    `:90`'s seed -- not an earlier mention. The companion test below is the
    other case.

    The delivered `tag` is the witness. Both users are local and
    `parent.author` is excluded at :120, so notification counts are identical
    whether or not the dedup fired; `:157` appends one tag per recipient, so a
    failed dedup would show up as a second, duplicate entry.
    """
    s = _seed(local_community=False, with_keys=True)
    op = _reauthor_the_parent(s)
    s.reply.body = 'hello @op@test.piefed.local'
    db.session.commit()
    route = _remote_inbox(s, http_mock)

    _send(s)

    note = _sent_activity(route)['object']
    assert [t['href'] for t in note['tag']] == [op.public_url()]


def test_the_same_local_user_mentioned_twice_is_added_once(db_session):
    """:110-114 with `add_recipient` going FALSE on the SECOND pass, via
    :111's FIRST disjunct -- and the existing recipient it matches is an
    EARLIER MENTION, not `:90`'s seed.

    The first pass compares 'mentioned' against `parent.author` and does not
    match, so :116 appends. The second pass walks `[parent.author, mentioned]`
    and matches on the second element. A local user has `ap_id` None, so the
    comparison falls to `user_name`. Two notifications would appear if the
    dedup failed: :121's `edit` is False, so :125's `existing_notification` is
    None on both passes and :134 would write a second row.
    """
    s = _seed(body='@mentioned@test.piefed.local and again @mentioned@test.piefed.local')
    mentioned = make_user(s.instance, 'mentioned', local=True)
    assert mentioned.ap_id is None

    _send(s)

    assert Notification.query.filter_by(user_id=mentioned.id).count() == 1


def test_the_same_remote_user_mentioned_twice_is_added_once(db_session, http_mock):
    """:110-114 via :111's SECOND disjunct (`recipient.ap_id and ap_id ==`),
    matching an EARLIER MENTION.

    A remote user has a non-None `ap_id`, so the first disjunct's
    `not recipient.ap_id` is False and only the second can fire. The delivered
    `tag` is the witness for the same reason as the parent-author case: a
    remote recipient is never notified, so a duplicate would only ever show up
    in the outbound body.
    """
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)
    remote = make_user(_peer_instance(s), 'remoteuser', local=False)
    assert remote.ap_id == 'remoteuser@peer.example'
    s.reply.body = '@remoteuser@peer.example and again @remoteuser@peer.example'
    db.session.commit()

    _send(s)

    note = _sent_activity(route)['object']
    assert [t['href'] for t in note['tag']] == [s.user.public_url(),
                                                remote.public_url()]


def test_two_different_local_users_are_both_added(db_session):
    """:115-116, the TRUE arm of `if add_recipient:` on the second pass -- the
    dedup must not suppress a genuinely different recipient."""
    s = _seed(body='@alpha@test.piefed.local and @beta@test.piefed.local')
    alpha = make_user(s.instance, 'alpha', local=True)
    beta = make_user(s.instance, 'beta', local=True)
    assert len({s.user.id, alpha.id, beta.id}) == 3

    _send(s)

    assert Notification.query.filter_by(user_id=alpha.id).count() == 1
    assert Notification.query.filter_by(user_id=beta.id).count() == 1


# ---------------------------------------------------------------------------
# The unguarded mention scan, :92 -- a LATENT crash, measured not assumed
# ---------------------------------------------------------------------------


def test_a_none_body_crashes_the_unguarded_mention_scan(db_session):
    """:92 scans `reply.body` with no guard, where `pages.py:97` guards its
    equivalent with `if post.body:`. `PostReply.body` is nullable
    (`app/models.py:2901`), so the column permits the state this test seeds.

    THE SEVERITY CLAIM HERE IS WRITTEN AFTER THE MEASUREMENT. Every writer that
    can put a reply in front of `send_reply` writes a `str`:

      * `app/shared/reply.py:182` (make_reply), `app/post/routes.py:917` (the
        inline reply route) and `app/shared/reply.py:219` (edit_reply) each
        assign `piefed_markdown_to_lemmy_markdown(content)`, which is one
        `re.sub` (`app/utils.py:1233-1237`). `re.sub` RAISES this same
        TypeError on None, so a None `content` dies in the writer before any
        row is committed -- it cannot produce a None-bodied row.
      * Those three sites are also the ONLY dispatchers of the `make_reply` /
        `edit_reply` tasks that call this function (`app/shared/reply.py:194`,
        `:232`, `app/post/routes.py:928`), and all three are gated on a local
        author: `:1897` compares `post_reply.user_id == current_user.id` and
        the API path passes `id_match=reply.user_id` to `authorise_api_user`
        (`app/shared/reply.py:204`).
      * The two writers that CAN store a None body --
        `app/activitypub/util.py:3020`, and `PostReply.new`'s `body=` from
        `app/community/util.py:272` -- are inbound/import paths for REMOTE
        replies and dispatch neither task, so nothing they write reaches `:92`.

    So the crash is LATENT: no production path currently reaches `:92` with a
    None body. This test pins the behaviour rather than asserting the absence
    of a crash, because a test that asserted "does not crash" would be a
    permanently failing test, and one that skipped the state entirely would
    leave the divergence from `pages.py:97` unrecorded. An empty-string body is
    NOT the same state -- `re.finditer` accepts `''` -- so `''` is not a
    substitute witness.

    If a future writer ever stores a None body on a locally-authored reply,
    this test starts describing a live crash and `:92` needs `pages.py:97`'s
    guard. Adding that guard is expected to fail this test; that failure is the
    signal to re-register the finding, not a reason to delete the test.
    """
    s = _seed()
    s.reply.body = None
    db.session.commit()

    with pytest.raises(TypeError,
                       match="expected string or bytes-like object, got 'NoneType'"):
        _send(s)


def test_an_empty_body_scans_cleanly_and_still_delivers(db_session, http_mock):
    """The falsy body `:92` DOES survive, which is what makes the None case
    above the only witness for the missing guard. `pages.py:97`'s `if post.body:`
    would skip both; `:92` skips neither and only one of them raises."""
    s = _seed(body='', local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    _send(s)

    assert _sent_activity(route)['object']['source']['content'] == ''


# ---------------------------------------------------------------------------
# The private gate, :143
# ---------------------------------------------------------------------------


def test_a_private_community_does_not_federate_the_reply(db_session):
    """:143's NEW `community.private` conjunct. `Community.private` is
    commented "only members can view. no federation." (`app/models.py:611`),
    and before this conjunct landed `notes.py:143` tested only `local_only` and
    `instance.online()` -- so a private, non-local-only community federated its
    replies out.

    THE UNCOUPLED STATE THIS SEEDS HAS NO CURRENT UI PATH. Both writers of the
    flag set `local_only` alongside it in the view layer
    (`app/community/routes.py:103-104` and `:1230-1231`), so `private=True,
    local_only=False` is producible at the model and database level but not
    through the site. The conjunct is defence in depth on the footing the
    design spec's 2.2 describes, matching `pages.py:153`, which is the only one
    of the ten senders in `app/shared/tasks/` that tests the flag. The other
    eight remain unguarded and stay registered.

    THE ASSERTION IS AN ABSENCE OF DELIVERY, and it is positive rather than
    vacuous: `post_request` (`app/activitypub/signature.py:103-105`) inserts an
    `ActivityPubLog` row for every outbound attempt BEFORE it touches the
    transport, and swallows the transport failure at `:143-148` -- so an
    ungated send leaves a row behind whether or not a route was registered for
    it. Zero rows therefore means `send_reply` returned at `:144` without
    reaching `:221`. The companion test below seeds the identical community
    with `private=False` and asserts a row DOES appear, which is what makes
    this pair discriminating rather than a count of nothing.

    `http_mock` is deliberately NOT requested: it is `assert_all_called=True`
    (`tests/conftest.py:294`), so registering the peer inbox this test wants
    never to be called would itself fail the test. The session-wide respx mock
    (`tests/conftest.py:283`) still intercepts, so no network is touched.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    s.community.private = True
    s.community.local_only = False
    db.session.commit()

    _send(s)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_non_private_community_of_the_same_shape_does_federate(db_session):
    """:143's FALSE arm for the same conjunct -- the control for the test
    above. Identical seed but `private=False`, so `:143` falls through and
    `:221` delivers, leaving the `ActivityPubLog` row `post_request` writes at
    `app/activitypub/signature.py:103-105`. Without this, the zero-row
    assertion above would pass against a `send_reply` that never delivered
    anything at all."""
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    s.community.private = False
    s.community.local_only = False
    db.session.commit()

    _send(s)

    assert db.session.query(ActivityPubLog).count() == 1


# ---------------------------------------------------------------------------
# The mention-notification block, :118-141
# ---------------------------------------------------------------------------


def test_a_remote_recipient_is_never_notified(db_session):
    """:120's FIRST conjunct -- `recipient.is_local()`.

    The seeded remote recipient has `id != parent.author.id` (:120's second
    conjunct is True for them, same as for any third party), so this isolates
    the first conjunct specifically: were `is_local()` dropped from the
    condition, `remote`'s id-inequality alone would satisfy it and this test
    would fail. The community stays local (the default), so nothing here
    depends on delivery -- `remote` is resolved purely through the mention
    scan's `already_exists` lookup at `app/user/utils.py:99`, with no network
    reached.
    """
    s = _seed(body='hello @remoteuser@peer.example')
    peer = _peer()
    remote = make_user(peer, 'remoteuser', local=False)
    assert remote.ap_id == 'remoteuser@peer.example'

    _send(s)

    assert Notification.query.filter_by(user_id=remote.id).count() == 0


def test_a_mention_of_the_parents_author_is_excluded_but_a_third_party_is_notified(
        db_session):
    """:120's SECOND conjunct -- `recipient.id != parent.author.id` -- THE
    DIVERGENCE FROM `send_post`.

    `send_post`'s `recipients` starts `[]`; `:90` here seeds it with
    `[parent.author]`, so the parent's author is always a delivery recipient
    (divergence in this file's module docstring) but this conjunct excludes
    them from ever being NOTIFIED of their own reply. `send_post` has no
    equivalent conjunct because it has nothing seeded to exclude.

    `_reauthor_the_parent` gives the parent a local author, 'op', distinct
    from the reply's author -- without that split every `_seed()` reply's
    author IS `parent.author`, and the two facts described in that helper's
    docstring would hide each other. 'op' is then mentioned BY NAME so they
    are resolved and re-collide with :90's seeded entry at :111 (deduped, not
    re-appended -- `test_a_mention_of_the_parent_author_is_deduped_against_
    the_seeded_recipient` above covers that dedup on its own), leaving
    `recipients` as `[op, third]`. The loop at :119 then reaches op first:
    `op.is_local()` is True and `op.id == parent.author.id`, so the second
    conjunct is what stops the notification -- if it were dropped, op's
    `is_local()` alone would satisfy the guard and they would be notified.
    `third` has no such collision, so a real notification for them is proof
    the guard is not simply skipping every recipient.
    """
    s = _seed()
    op = _reauthor_the_parent(s)
    third = make_user(s.instance, 'third', local=True)
    s.reply.body = 'hello @op@test.piefed.local and @third@test.piefed.local'
    db.session.commit()

    _send(s)

    assert Notification.query.filter_by(user_id=op.id).count() == 0
    assert Notification.query.filter_by(user_id=third.id).count() == 1


def test_edit_true_creates_once_then_skips_a_duplicate_on_the_second_call(
        db_session):
    """:121's TRUE arm (`if edit:`) across two calls, exercising both results
    `:122`'s query can produce and both arms of `:125`.

    The first `edit=True` call finds no matching row at `:122` --
    `existing_notification` is None -- so `:125`'s `not existing_notification`
    is True and `:126-141` creates one. The second call, same reply and
    therefore the same `:122` URL, now finds the row the first call wrote, so
    `existing_notification` is truthy and `:125` is False: `:126-141` does not
    run again. One notification surviving two calls is the witness; the
    companion test below makes `edit=False` the control that shows this is
    not simply because "the second mention was ignored."
    """
    s = _seed(body='hello @mentioned@test.piefed.local')
    mentioned = make_user(s.instance, 'mentioned', local=True)

    _send(s, edit=True)
    assert Notification.query.filter_by(user_id=mentioned.id).count() == 1

    _send(s, edit=True)
    assert Notification.query.filter_by(user_id=mentioned.id).count() == 1


def test_edit_false_never_checks_for_an_existing_notification_and_duplicates(
        db_session):
    """:121's FALSE arm -- `:124` sets `existing_notification = None`
    unconditionally, without running `:122`'s query at all.

    The first call uses `edit=True` to write a real notification row, so a
    matching row genuinely exists in the database. The second call, on the
    same reply, uses `edit=False`: if `:124` behaved like `:122`'s query, it
    would find that row and skip. It does not consult the row at all, so
    `:125` is True again and a SECOND notification is written. Two rows after
    the second call is the only way to observe that `:124` never looks --
    the test above showed `edit=True` skipping when a match exists; this one
    shows `edit=False` does not, against the identical existing row.
    """
    s = _seed(body='hello @mentioned@test.piefed.local')
    mentioned = make_user(s.instance, 'mentioned', local=True)

    _send(s, edit=True)
    assert Notification.query.filter_by(user_id=mentioned.id).count() == 1

    _send(s, edit=False)
    assert Notification.query.filter_by(user_id=mentioned.id).count() == 2


def test_targets_data_records_post_comment_and_falls_back_to_username(
        db_session):
    """:127-132's `targets_data` dict, and :129's FALSE arm -- `author.ap_id
    if author.ap_id else author.user_name` -- taken because a locally-authored
    reply's `ap_id` is None (`tests/factories.py:58`).

    coverage.py emits no arc for a conditional expression (fact 87 per the
    brief), so only an assertion that would fail under the other arm's value
    can show which one ran: `author_user_name` here is asserted to equal
    `s.user.user_name`, a value the TRUE arm could not produce, since the
    TRUE arm reads `ap_id` and this author's `ap_id` is None.
    """
    s = _seed(body='hello @mentioned@test.piefed.local')
    mentioned = make_user(s.instance, 'mentioned', local=True)
    assert s.user.ap_id is None

    _send(s)

    notification = Notification.query.filter_by(user_id=mentioned.id).one()
    assert notification.targets == {
        'gen': '0',
        'post_id': s.post.id,
        'author_user_name': s.user.user_name,
        'comment_id': s.reply.id,
        'comment_body': s.reply.body,
    }


def test_targets_data_uses_the_authors_ap_id_when_present(db_session):
    """:129's TRUE arm -- the companion to the test above. Giving the reply's
    author an `ap_id` (locally-authored replies never have one on their own,
    per the previous test) makes `author.ap_id` truthy, so `targets_data`
    records that value instead of `user_name`. The two tests together are
    what `:129`'s missing coverage.py arc requires: neither value alone would
    distinguish the branch that produced it.

    `_reauthor_the_parent` is used here for a reason UNRELATED to its usual
    purpose: with the default seed the reply's author is also `:90`'s seeded
    `parent.author`, the first entry `:119`'s loop reaches, so `:120` would
    call `is_local()` on this same user BEFORE the exclusion is decided.
    `is_local()` (`app/models.py:1251-1252`) reads `ap_profile_id` whenever
    `ap_id` is set, and a plain local user has no `ap_profile_id` -- setting
    only `ap_id` on that user crashes the very first loop iteration.
    Reauthoring the parent moves `parent.author` to 'op', so the reply's
    author is never iterated over and its `is_local()` is never called.
    """
    s = _seed(body='hello @mentioned@test.piefed.local')
    _reauthor_the_parent(s)
    mentioned = make_user(s.instance, 'mentioned', local=True)
    s.user.ap_id = 'author@elsewhere.example'
    db.session.commit()

    _send(s)

    notification = Notification.query.filter_by(user_id=mentioned.id).one()
    assert notification.targets['author_user_name'] == 'author@elsewhere.example'


def test_a_notified_recipients_unread_count_is_incremented(db_session):
    """:139 -- `recipient.unread_notifications += 1`, run only on the
    `:125` create path. Asserted as a delta across the call rather than a
    fixed value, so the test does not depend on `User`'s column default.
    """
    s = _seed(body='hello @mentioned@test.piefed.local')
    mentioned = make_user(s.instance, 'mentioned', local=True)
    before = mentioned.unread_notifications

    _send(s)

    db.session.refresh(mentioned)
    assert mentioned.unread_notifications == before + 1


# ---------------------------------------------------------------------------
# The three early returns, :143-151
# ---------------------------------------------------------------------------
#
# `:143` is `if community.local_only or community.private or not
# community.instance.online():` -- THREE disjuncts, not two. The middle one,
# `community.private`, is already covered above by
# `test_a_private_community_does_not_federate_the_reply` /
# `test_a_non_private_community_of_the_same_shape_does_federate`; it is not
# repeated here. This block covers the FIRST disjunct (`local_only`), the
# THIRD (`not community.instance.online()`, via both `dormant` and
# `gone_forever`), `:147-148`'s `CommunityBan` return, and `:149-151`'s
# not-local-community guard in both its disjuncts and its false arm.
#
# THE WITNESS IS `ActivityPubLog.query.count()`, not the mention
# notification at `:118-141`. That block runs BEFORE `:143`, so "a
# notification landed" is true whether the function returns at `:144`/
# `:148`/`:151` or runs to completion -- it cannot tell the two apart and is
# not used here. `post_request` (`app/activitypub/signature.py:103-105`,
# see `test_a_private_community_does_not_federate_the_reply` above) inserts
# an `ActivityPubLog` row for every outbound attempt before it ever touches
# the transport, so a seed that WOULD deliver if nothing stopped it -- proven
# by the existing `test_a_non_private_community_of_the_same_shape_does_
# federate` baseline of exactly one row for the identical remote+deliverable
# shape -- gives zero rows only if the return actually fired.


def test_a_local_only_community_does_not_federate_the_reply(db_session):
    """:143's FIRST disjunct -- `community.local_only`.

    Same remote+deliverable shape as `test_a_non_private_community_of_the_
    same_shape_does_federate` (that test's `local_only=False` is this test's
    control), with `local_only=True` instead. `private` stays at its column
    default of `False` and the instance stays online, isolating this one
    disjunct.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    s.community.local_only = True
    db.session.commit()

    _send(s)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_dormant_instance_does_not_federate_the_reply(db_session):
    """:143's THIRD disjunct -- `not community.instance.online()` -- via the
    `dormant` column. `Instance.online()` (`app/models.py:118-119`) is
    `not (self.dormant or self.gone_forever)`, so setting `dormant=True`
    alone is enough to flip it. `local_only` and `private` stay at their
    `False` defaults.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    s.community.instance.dormant = True
    db.session.commit()

    _send(s)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_gone_forever_instance_does_not_federate_the_reply(db_session):
    """:143's THIRD disjunct again, the OTHER column -- `gone_forever`. Kept
    as a separate test from the `dormant` one above because `Instance.
    online()` reads both columns independently and either alone must flip
    the guard; a single test setting both would not tell them apart.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    s.community.instance.gone_forever = True
    db.session.commit()

    _send(s)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_banned_author_does_not_federate_the_reply(db_session):
    """:147-148 -- `CommunityBan` for `(user_id, community_id)`. `:143`
    stays false here (the community is not local_only, not private, and its
    instance is online), so this isolates the second return: without it,
    the remote+deliverable shape below is the same one `test_a_non_private_
    community_of_the_same_shape_does_federate` shows produces exactly one
    `ActivityPubLog` row.

    `make_community_ban` (`tests/factories.py:414-436`) defaults `banned_by`
    to `community.user_id`, which `make_community` (`tests/factories.py:139`)
    hardcodes to `1` -- the same id `s.user` gets as the first `User` row
    this test creates, so the ban is self-authored. `CommunityBan` places no
    constraint against that; `:146`'s query only cares about `(user_id,
    community_id)`.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    make_community_ban(s.user, s.community)

    _send(s)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_blocked_remote_instance_does_not_federate_the_reply(db_session):
    """:149-151's FIRST disjunct -- `user.has_blocked_instance(community.
    instance.id)`.

    `assert s.community.is_local() is False` is not ceremony: `Community.
    is_local()` (`app/models.py:795-796`) is `self.ap_id is None or
    self.profile_id().startswith(SERVER_URL)`, a DISJUNCTION, and
    `profile_id()` falls back to a computed default when `ap_profile_id` is
    unset -- so a seed that set only `ap_id` would leave this returning
    `True` and `:149`'s `if not community.is_local():` would never open,
    silently skipping the very block this test targets. `_seed(local_
    community=False)` sets both `ap_id` and `ap_profile_id` (see its
    docstring), and this assertion is what would catch it if that ever
    regressed.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    assert s.community.is_local() is False
    make_instance_block(s.user, s.community.instance)

    _send(s)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_banned_remote_instance_does_not_federate_the_reply(db_session):
    """:149-151's SECOND disjunct -- `instance_banned(community.instance.
    domain)`.

    Same `is_local()` hazard as the test above, so the same assertion is
    made immediately after seeding. `BannedInstances` is the table
    `instance_banned` (`app/utils.py:2335-2356`) queries; `_make_deliverable`
    (called first) puts the community's instance on `peer.example`, so the
    row is added for that domain. `instance_banned` is `@cache.memoize`d, but
    `tests/conftest.py:68` sets `CACHE_TYPE = 'NullCache'` for the whole
    suite, so there is no stale-verdict hazard from the earlier `BannedInstances`
    row `test_a_banned_remote_host_mention_is_swallowed_by_the_remote_except`
    adds for the same domain in a different test.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    assert s.community.is_local() is False
    db.session.add(BannedInstances(domain=s.community.instance.domain, reason='test'))
    db.session.commit()

    _send(s)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_local_community_skips_the_remote_instance_checks_entirely(db_session):
    """:149's FALSE arm -- `not community.is_local()` is `False` for the
    default (local) community, so `:150-151`'s block is never even
    evaluated, let alone returned from.

    A WEAKER TEST WOULD JUST SEND A REPLY AND CHECK IT COMPLETED -- several
    tests above already show that. What makes this one discriminating
    against the specific mutation of dropping or flipping the `not` is that
    `user.has_blocked_instance(community.instance.id)` and `instance_banned
    (community.instance.domain)` are made TRUE for the community's own
    (local) instance before sending: an `InstanceBlock` row for `(s.user,
    s.instance)` and a `BannedInstances` row for `s.instance.domain`. If
    `:149` incorrectly entered that block for a local community, either
    condition alone would trigger `:151`'s return. Because the community is
    local, `:149` is skipped regardless, and the reply still reaches the
    final "send copy of the Create to anyone else Mentioned" loop
    (`:227-229`): a remote mentioned user on a different domain from the
    community's own is never in `domains_sent_to` (`:199`, just
    `[SERVER_NAME]` here, since the local community has no following
    instances configured), so `post_request` (`app/activitypub/
    signature.py:103-105`) fires for them and leaves exactly one
    `ActivityPubLog` row -- the same unconditional-log-row reasoning the
    tests above rely on, run here as evidence of full completion rather than
    an early return.
    """
    s = _seed(body='hello @remoteuser@peer.example', with_keys=True)
    assert s.community.is_local() is True
    make_instance_block(s.user, s.instance)
    db.session.add(BannedInstances(domain=s.instance.domain, reason='test'))
    peer = _peer()
    make_user(peer, 'remoteuser', local=False)
    db.session.commit()

    _send(s)

    assert db.session.query(ActivityPubLog).count() == 1
