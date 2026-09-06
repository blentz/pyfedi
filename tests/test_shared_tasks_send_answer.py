"""`send_answer` -- the Celery-path builder and deliverer of a ChooseAnswer.

`app/shared/tasks/notes.py:242-310` (extent by ast). The sibling of
`send_reply` (`:80-229`), which sub-project 20 took to two unreachable
statements and zero unreachable arms.

`send_answer` is a 2x2 plus a loop. `:265` (`if is_undo:`) and `:279`
(`if post_reply.community.is_local():`) are INDEPENDENT, so there are four
end-to-end paths, each with a different wire shape:

    local,  choose -> Announce(:290-298) wrapping the ChooseAnswer
    local,  undo   -> Announce wrapping the Undo wrapping the ChooseAnswer
    remote, choose -> the bare ChooseAnswer to community.ap_inbox_url (:304)
    remote, undo   -> the bare Undo to the same

THE `@context` ASYMMETRY IS THE LOAD-BEARING ASSERTION. It is deleted from
whichever object becomes an INNER object and kept on the outermost one:
`:266` strips `lock` when it is about to be nested inside `undo` at `:272`;
`:281` strips `undo` and `:284` strips `lock` when either is about to be
nested inside `announce` at `:294`. The outermost object keeps the
`@context` it was built with (`:259`, `:273`, `:295`). Every path test below
asserts presence on the outer object and absence on every nested one.
Sub-project 19 got a `@context` claim wrong by REASONING about which deletes
run; its amendment declaring an arm unreachable was false because the delete
ran on the ordinary path. Assert, do not argue.

DIFFERENCES FROM `send_reply` THAT CHANGE HOW IT IS TESTED:

1. `send_answer` takes NO `session` parameter. It opens its own at `:243`
   with `get_task_session()` (`app/utils.py:3673-3675`), which returns
   `Session(bind=db.engine)` -- an independent session, not `db.session`.
   Seeded rows must therefore be COMMITTED before the call, or the function
   will not see them. `_seed` below commits.

2. `send_answer` never calls `patch_db_session` (`app/utils.py:3679`), where
   `make_reply` (`:58`) and `edit_reply` (`:71`) both wrap their callee in
   it. Anything `send_answer` reaches that touches `db.session` therefore
   gets the request-scoped session rather than the task session it just
   opened. Registered as a finding; measured, not assumed.

3. `:303` is a conditional expression, `undo if is_undo else lock`, and it
   is the ONLY one in the function -- established by an AST walk over the
   `send_answer` FunctionDef, not by grep (fact 94). coverage.py emits no
   arc for a ternary (fact 87), so it reports zero missing arms whether or
   not both arms run; both have named tests below.
"""

import json
import socket
from types import SimpleNamespace

import pytest

from app import db
from app.models import ActivityPubLog
from app.shared.tasks.notes import choose_answer, send_answer, unchoose_answer
from tests.factories import (
    make_community, make_community_member, make_instance, make_post,
    make_post_reply, make_user,
)


def _seed(local_community=True, with_keys=False):
    """instance, author, community, post, reply -- committed.

    ORDER IS LOAD-BEARING. `make_community` (tests/factories.py:122)
    hardcodes `instance_id=1` and the db_session teardown resets every
    sequence, so the local instance must be created FIRST; a peer built
    before this call would take id 1 and leave the community's FK pointing
    at it.

    `local_community=False` sets `ap_id` AND `ap_profile_id` on peer.example,
    because `Community.is_local()` (app/models.py:795) is a DISJUNCTION --
    `ap_id is None or profile_id().startswith(SERVER_URL)` -- whose
    `profile_id()` falls back to a computed default. Setting `ap_id` alone
    leaves the community silently LOCAL, and `:279`'s false arm would never
    be taken while these tests still passed (fact 112).

    THE COMMIT IS NOT OPTIONAL: `send_answer` reads through its own session
    (see the module docstring), which cannot see this one's uncommitted rows.
    """
    instance = make_instance('test.piefed.local', software='piefed')
    user = make_user(instance, 'author', local=True, with_keys=with_keys)
    community = make_community('c1')
    post = make_post(community, user, ap_id='https://test.piefed.local/post/1')
    reply = make_post_reply(post, user, body='an answer')
    if not local_community:
        community.ap_id = 'c1@peer.example'
        community.ap_profile_id = 'https://peer.example/c/c1'
        community.ap_public_url = 'https://peer.example/c/c1'
        community.ap_followers_url = 'https://peer.example/c/c1/followers'
        community.ap_domain = 'peer.example'
    db.session.commit()
    return SimpleNamespace(instance=instance, user=user, community=community,
                           post=post, reply=reply)


def _peer(domain='peer.example', software='lemmy'):
    """A remote Instance. ALWAYS call AFTER `_seed()` -- see `_seed` for why."""
    return make_instance(domain, software=software)


def _send(s, is_undo=False):
    """`send_answer(post_reply_id, user_id, is_undo)` -- no session parameter."""
    return send_answer(s.reply.id, s.user.id, is_undo)


PEER_INBOX = 'https://peer.example/c/c1/inbox'

_REAL_GETADDRINFO = socket.getaddrinfo

# Any globally routable literal will do -- the only property
# app/utils.py:5530 reads off it is `is_global`.
_EXAMPLE_TLD_ADDRESS = '93.184.216.34'


def _getaddrinfo_without_the_network(host, *args, **kwargs):
    """`socket.getaddrinfo`, answering for `.example` hosts without a resolver.

    Everything else is delegated to the real function unchanged.
    """
    if isinstance(host, str) and (host == 'example' or host.endswith('.example')):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '',
                 (_EXAMPLE_TLD_ADDRESS, 0))]
    return _REAL_GETADDRINFO(host, *args, **kwargs)


@pytest.fixture(autouse=True)
def _peer_example_resolves_without_a_resolver(monkeypatch):
    """Keep delivery off the machine's DNS resolver.

    THIS IS NOT OPTIONAL AND MUST NOT BE DELETED AS UNNECESSARY. Signing runs
    `is_invalid_get_request_uri`, which reaches `socket.getaddrinfo` at
    app/utils.py:5520 for POST as well as GET, and FAILS OPEN at :5521-5522.
    That is what makes it dangerous rather than merely slow: on a machine
    whose resolver hijacks NXDOMAIN into a wildcard A record, these tests
    start failing at app/utils.py:5530's `is_global` check instead. The stub
    is the narrowest thing that removes the lookup -- only the resolver, only
    for the RFC-2606 `.example` TLD -- so `is_invalid_get_request_uri` still
    runs in full against a real answer.

    Autouse because `_remote_inbox` is a plain function and cannot request a
    fixture.
    """
    monkeypatch.setattr(socket, 'getaddrinfo', _getaddrinfo_without_the_network)


def _make_deliverable(s, inbox=PEER_INBOX):
    """Attach the community to a real peer Instance and give it an inbox.

    The half of `_remote_inbox` that touches the database and nothing else.
    The early-return tests call this ALONE, because they assert the delivery
    never happens and `http_mock` is built with `assert_all_called=True` --
    registering a route they expect never to fire would fail them for the
    wrong reason.
    """
    s.community.instance_id = _peer().id
    s.community.ap_inbox_url = inbox
    db.session.commit()


def _remote_inbox(s, http_mock, inbox=PEER_INBOX):
    """THE CAPTURE MECHANISM. Returns the respx route the outbound activity
    lands on; pair it with `_sent_activity`.

    Everything `send_answer` builds lives in locals (`lock`, `undo`,
    `announce`) and is never persisted, so the only way to read it is to let
    the function deliver. Three things must be true for `:304`'s call to
    become an observable request, and this helper plus `_seed` supply all
    three:

      1. The community must be remote -- `_seed(local_community=False)`,
         which closes BOTH disjuncts of `Community.is_local()`.
      2. It must have an `ap_inbox_url`; `make_community` leaves it None, and
         `post_request` (app/activitypub/signature.py:109-111) short-circuits
         to an "empty uri" failure row without reaching the transport.
      3. The user must have a keypair -- `_seed(with_keys=True)` -- because
         signing calls `.encode()` on `user.private_key`.

    Delivery is synchronous: `send_post_request`
    (app/activitypub/signature.py:82-91) calls `post_request.delay(...)` and
    conftest runs Celery eagerly, so the POST has happened by the time
    `_send` returns.

    WHY THE HTTP BODY AND NOT A RECORDER: `:266`, `:281` and `:284` mutate
    `lock` and `undo` in place with `del`, so a recorder holding the dict
    would be read back in its post-`del` state. respx captures the bytes that
    actually left, at app/activitypub/signature.py:494.
    """
    _make_deliverable(s, inbox)
    return http_mock.post(inbox).respond(200, json={})


def _sent_activity(route, index=-1):
    """The JSON body of the request `route` captured, decoded from the bytes
    that were actually sent. Defaults to the most recent call."""
    return json.loads(route.calls[index].request.content)


def _key_id_of(route, index=-1):
    """The `keyId` the captured request was signed under.

    The only observable that separates `:301`'s signer from `:304`'s: the
    Announce signs as the COMMUNITY (`community.public_url() + '#main-key'`),
    the direct post signs as the USER. respx never verifies a signature, so
    the key material leaves no trace on the wire -- only the declared keyId
    does.
    """
    return route.calls[index].request.headers['signature'].split('"')[1]


def _community_follower(s, http_mock=None, domain='fan.example',
                        member_name='fan', with_inbox=True, dormant=False):
    """A remote instance `community.following_instances()` returns at `:299`,
    plus the respx route its delivery lands on.

    Returns `SimpleNamespace(instance, member, route)`; `route` is None when
    none was registered.

    THE COMMUNITY'S KEYPAIR IS SET HERE. `:301` signs with
    `community.private_key`, which `make_community` leaves None, and signing
    calls `.encode()` on it. The author's key is reused rather than a second
    generated: generation costs about a second, respx never verifies a
    signature, and the actor a delivery was signed AS is observable through
    `keyId`, not through the key material.

    `with_inbox=False` leaves `Instance.inbox` None, which closes `:300`'s
    FIRST conjunct. `dormant=True` sets the column
    `Community.following_instances` already filters on in SQL
    (app/models.py:849) -- registered as unreachable-False, not exercised as
    a live branch.
    """
    assert s.user.private_key is not None, '_seed(with_keys=True) is required'
    s.community.private_key = s.user.private_key
    s.community.public_key = s.user.public_key
    instance = make_instance(domain, software='lemmy')
    instance.inbox = f'https://{domain}/inbox' if with_inbox else None
    instance.dormant = dormant
    member = make_user(instance, member_name, local=False)
    make_community_member(member, s.community)
    db.session.commit()
    route = (http_mock.post(instance.inbox).respond(200, json={})
             if http_mock is not None and with_inbox else None)
    return SimpleNamespace(instance=instance, member=member, route=route)


def test_a_remote_community_receives_the_bare_choose_answer(db_session, http_mock):
    """The harness itself: `:279`'s FALSE arm reaches `:304` and the bytes
    are readable. Every later test in this file depends on this working."""
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    _send(s)

    assert route.called
    assert _sent_activity(route)['type'] == 'ChooseAnswer'


def test_a_local_only_community_does_not_federate_the_answer(db_session, http_mock):
    """:248's TRUE arm via `local_only`, returning at :249."""
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    s.community.local_only = True
    db.session.commit()

    _send(s)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_dormant_instance_does_not_receive_the_answer(db_session, http_mock):
    """:248's TRUE arm via `not instance.online()`.

    `Instance.online()` (app/models.py:118-119) is
    `not (self.dormant or self.gone_forever)`, so setting `dormant` on the
    community's OWN instance closes it. This is the community's instance, not
    a follower's -- the follower filter at :299 is a different mechanism
    entirely, covered in Task 5.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    s.community.instance.dormant = True
    db.session.commit()

    _send(s)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_private_community_does_not_federate_the_answer(db_session, http_mock):
    """:248's `private` conjunct -- THE ONE PRODUCTION CHANGE THIS
    SUB-PROJECT LANDS.

    `Community.private` (app/models.py:611) is commented "only members can
    view. no federation.", and before this conjunct landed `:248` tested only
    `local_only` and `instance.online()` -- so a private, non-local-only
    community federated its chosen answers out.

    This is the SECOND site of D309 the campaign has closed; sub-project 20
    closed `:143` in the same file for the same finding. `local_only` is left
    False deliberately: with it True the test would pass on the pre-existing
    conjunct and prove nothing.

    THE UNCOUPLED STATE THIS SEEDS HAS NO CURRENT UI PATH -- the two form
    fields are tied at app/community/routes.py:103-104 and :1230-1231, and
    the three write statements are :122, :1234 and :1237 -- so the severity
    is latent and the guard is defence in depth.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    s.community.private = True
    db.session.commit()

    _send(s)

    assert db.session.query(ActivityPubLog).count() == 0


def test_the_remote_choose_carries_its_full_key_set_and_keeps_its_context(
        db_session, http_mock):
    """:303's FALSE arm -- `undo if is_undo else lock` selecting `lock` --
    delivered by :304.

    `type` is what separates the two arms: the ChooseAnswer could not be
    produced by the true arm, which sends an Undo. `@context` is asserted
    PRESENT because nothing nests this object on this path -- :266 runs only
    under `is_undo` and :284 only under `is_local`, and neither is taken
    here, so the `@context` built at :259 survives to the wire.
    """
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    _send(s, is_undo=False)

    sent = _sent_activity(route)
    assert sent['type'] == 'ChooseAnswer'
    assert set(sent) == {'id', 'type', 'actor', 'object', '@context',
                         'audience', 'to', 'cc'}
    assert sent['actor'] == s.user.public_url()
    assert sent['object'] == s.reply.public_url()
    assert sent['audience'] == s.community.public_url()
    assert sent['to'] == ['https://www.w3.org/ns/activitystreams#Public']
    assert sent['cc'] == [s.community.public_url()]


def test_the_remote_undo_wraps_the_choose_and_strips_its_inner_context(
        db_session, http_mock):
    """:303's TRUE arm -- selecting `undo` -- and :266's `del`.

    The companion to the test above: `type` is `Undo` here, which the false
    arm could not produce. The nesting is the point -- `undo['object']` is
    the ChooseAnswer, and :266 stripped ITS `@context` before :272 nested it,
    while the Undo itself keeps the one built at :273. That asymmetry is what
    a mutant deleting the wrong object's `@context` would break.
    """
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    _send(s, is_undo=True)

    sent = _sent_activity(route)
    assert sent['type'] == 'Undo'
    assert '@context' in sent
    assert sent['object']['type'] == 'ChooseAnswer'
    assert '@context' not in sent['object']
    assert sent['actor'] == s.user.public_url()
    assert sent['object']['object'] == s.reply.public_url()


def test_the_remote_delivery_is_signed_as_the_user(db_session, http_mock):
    """:304 signs with `user.public_url() + '#main-key'`, where :301 signs as
    the COMMUNITY. `keyId` is the only observable that separates them, and
    the user's and community's public urls differ in path (`/u/author` vs
    `/c/c1`), so a mutant swapping the signer produces a valid but wrong
    keyId and this fails rather than merely not-noticing.
    """
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    _send(s)

    assert _key_id_of(route) == s.user.public_url() + '#main-key'


def test_a_local_community_announces_the_choose_to_a_following_instance(
        db_session, http_mock):
    """:279's TRUE arm with :280 FALSE -- :284's `del lock['@context']` and
    the Announce built at :290-298.

    The Announce keeps the `@context` built at :295; the ChooseAnswer nested
    at :294 has had its own stripped at :284. Asserting both directions is
    what catches a mutant that deletes from the wrong object.

    `cc` is the community's followers collection here (:289), NOT the
    `[community.public_url()]` the inner object carries (:253) -- :289
    rebinds the name to a NEW list, so the inner object's `cc` still points
    at the old one. Asserting both proves the rebinding did not alias.
    """
    s = _seed(with_keys=True)
    fan = _community_follower(s, http_mock)

    _send(s, is_undo=False)

    sent = _sent_activity(fan.route)
    assert sent['type'] == 'Announce'
    assert '@context' in sent
    assert sent['actor'] == s.community.public_url()
    assert sent['object']['type'] == 'ChooseAnswer'
    assert '@context' not in sent['object']
    assert sent['cc'] == [s.community.ap_followers_url]
    assert sent['object']['cc'] == [s.community.public_url()]


def test_a_local_community_announces_the_undo_and_strips_two_contexts(
        db_session, http_mock):
    """:279's TRUE arm with :280 TRUE -- :281's `del undo['@context']`, on
    top of :266's `del lock['@context']` which already ran.

    THIS IS THE THREE-LEVEL PATH and the only one where two deletes fire.
    The Announce keeps its `@context`; the Undo nested inside it lost its at
    :281; the ChooseAnswer nested inside THAT lost its at :266. All three
    levels are asserted, because a mutant that skipped either delete would
    still produce a well-formed activity and only this shape would catch it.
    """
    s = _seed(with_keys=True)
    fan = _community_follower(s, http_mock)

    _send(s, is_undo=True)

    sent = _sent_activity(fan.route)
    assert sent['type'] == 'Announce'
    assert '@context' in sent
    assert sent['object']['type'] == 'Undo'
    assert '@context' not in sent['object']
    assert sent['object']['object']['type'] == 'ChooseAnswer'
    assert '@context' not in sent['object']['object']


def test_the_announce_is_signed_as_the_community(db_session, http_mock):
    """:301 signs with `community.private_key` and
    `community.public_url() + '#main-key'` -- the companion to Task 3's
    user-signed assertion. See `_key_id_of`."""
    s = _seed(with_keys=True)
    fan = _community_follower(s, http_mock)

    _send(s)

    assert _key_id_of(fan.route) == s.community.public_url() + '#main-key'


def test_a_local_community_with_no_followers_sends_nothing(db_session, http_mock):
    """:299's loop never entered -- arc (299, 310), straight to the `finally`.

    `following_instances()` returns empty because no CommunityMember exists
    on a remote instance. The Announce is still BUILT at :290-298; nothing
    delivers it. `ActivityPubLog.query.count() == 0` is the witness, since
    `post_request` would have written a row for any attempt.
    """
    s = _seed(with_keys=True)

    _send(s)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_following_instance_without_an_inbox_is_skipped(db_session, http_mock):
    """:300's FALSE arm -- arc (300, 299), the loop continuing.

    `with_inbox=False` leaves `Instance.inbox` None, closing :300's FIRST
    conjunct before any of the other three is evaluated. No route is
    registered, so `http_mock`'s `assert_all_called=True` is not tripped, and
    the `ActivityPubLog` count of 0 rules out a request having been attempted
    against a None inbox -- which would have produced an "empty uri" failure
    row rather than nothing at all.
    """
    s = _seed(with_keys=True)
    _community_follower(s, http_mock, with_inbox=False)

    _send(s)

    assert db.session.query(ActivityPubLog).count() == 0


def test_one_following_instance_is_skipped_while_another_receives(
        db_session, http_mock):
    """Both loop arms in ONE run -- (300, 299) then (300, 301).

    The discriminating case: a single-instance test cannot show that the
    guard skips an instance WITHOUT also stopping the loop, because with one
    member "skipped" and "loop ended" look identical. With two, the delivered
    one proves iteration continued past the skipped one.
    """
    s = _seed(with_keys=True)
    _community_follower(s, http_mock, domain='mute.example',
                        member_name='mute', with_inbox=False)
    good = _community_follower(s, http_mock, domain='fan.example',
                               member_name='fan')

    _send(s)

    assert good.route.call_count == 1
    assert db.session.query(ActivityPubLog).count() == 1
