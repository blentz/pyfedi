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
   `:309-330`, which is where its aliasing hazard lives; this function is
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
from app.models import BannedInstances, Instance, Notification, PostReply
from app.shared.tasks.notes import send_reply
from tests.factories import (
    make_community, make_instance, make_post, make_post_reply, make_user,
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
    s.community.instance_id = _peer().id
    s.community.ap_inbox_url = inbox
    db.session.commit()
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

