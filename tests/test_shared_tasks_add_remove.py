"""`add_object` and `remove_object` -- the twin builders of AP Add and Remove.

`app/shared/tasks/adds.py:56-100` and `app/shared/tasks/removes.py:56-100`
(extents by ast). THE TWO MODULES ARE STRUCTURALLY IDENTICAL, and the claim is
MEASURED rather than impressionistic: both are 100 lines and 51 statements with
10 branches, both finished at zero missing statements and zero missing arms, and
the ten executed arcs are the same ten in both files -- (58,59) (58,61) (63,64)
(63,66) (81,82) (81,100) (96,-56) (96,97) (97,96) (97,98). `diff adds.py
removes.py` reports exactly EIGHT hunks and every one of them is a name
substitution (`Add:`/`Remove:`, `sticky_post`/`unsticky_post`,
`add_mod`/`remove_mod`, `add_object`/`remove_object`, `add_id`/`remove_id`, the
`add`/`remove` dict variable, the `'Add'`/`'Remove'` type literal, and the
`/activities/add/`-vs-`/activities/remove/` path segment). Rewriting the token
`remove` to `add` in all of its forms -- identifier, string literal and prose
word -- makes the two files BYTE-IDENTICAL. **An earlier version of this
paragraph said "after normalising names the only textual difference is one
docstring word", which is short of the truth in both directions**: two string
literals also differ, and once the docstring word is normalised with the rest
nothing differs at all. They share even their dead imports -- `post_request` is
imported at `:2` of each and called in neither; only `send_post_request` is
called, at `:98` and `:100` of both.

That equivalence is why one file tests both, and it is asserted rather than
assumed (see `test_the_twins_are_structurally_identical` below), so a future
divergence becomes a detectable event rather than a later discovery.

EVERY TEST IS WRITTEN OUT TWICE, ONCE PER TWIN, AND NEVER PARAMETRISED ACROSS
THEM. A parametrised failure names the parameter rather than the function, and
an arm covered under only one parameter is invisible in the failure output --
the same reasoning that kept `make_post`'s and `edit_post`'s error tests
separate in sub-project 22.

THE `:74` TERNARY IS THE REASON `_seed` SETS TWO URL COLUMNS.
`community.ap_moderators_url if community_id else community.ap_featured_url`
selects between two columns that `make_community` (tests/factories.py:122)
leaves at None. With both None, the ternary's two arms produce the same value,
every `target` assertion passes under either, and the tests prove nothing.
`_seed` sets them to distinct values and the smoke tests assert they differ.

THE TWO WRAPPER LOOKUPS DIFFER, TWELVE LINES APART, IN BOTH TWINS:
`:32` is `session.query(Post).get(post_id)` and returns None for a missing id,
so the raise arrives later as `AttributeError` when `:59` reads
`object.community`; `:47` is `session.query(User).filter_by(id=mod_id).one()`
and raises `NoResultFound`. Establish each by reading, never by inheriting from
its neighbour -- this is the third module in which the campaign has met this
trap, and sub-project 21 lost a fix round to it.
"""

import json
import socket
from types import SimpleNamespace

import pytest

from app import db
from app.models import ActivityPubLog
from app.shared.tasks.adds import add_mod, add_object, sticky_post
from app.shared.tasks.removes import remove_mod, remove_object, unsticky_post
from tests.factories import (
    make_community, make_community_member, make_instance, make_post, make_user,
)

PEER_INBOX = 'https://peer.example/c/c1/inbox'
FEATURED_URL = 'https://test.piefed.local/c/c1/featured'
MODERATORS_URL = 'https://test.piefed.local/c/c1/moderators'

_REAL_GETADDRINFO = socket.getaddrinfo

# Any globally routable literal will do -- the only property
# app/utils.py:5530 reads off it is `is_global`.
_EXAMPLE_TLD_ADDRESS = '93.184.216.34'


def _seed(local_community=True, with_keys=False):
    """instance, author, community, post, mod -- committed.

    ORDER IS LOAD-BEARING. `make_community` (tests/factories.py:122) hardcodes
    `instance_id=1` and the db_session teardown resets every sequence, so the
    local instance is created FIRST; a peer built before this call would take
    id 1 and leave the community's FK pointing at it.

    `ap_featured_url` AND `ap_moderators_url` ARE SET HERE AND MUST STAY SET.
    `make_community` leaves both None, and `:74`'s ternary selects between
    exactly these two columns -- with both None the ternary's arms are
    indistinguishable on the wire.

    `local_community=False` sets `ap_id` AND `ap_profile_id` on peer.example,
    because `Community.is_local()` (app/models.py:795) is a DISJUNCTION whose
    `profile_id()` falls back to a computed default; `ap_id` alone leaves the
    community silently LOCAL and `:100` never runs (fact 112).
    """
    instance = make_instance('test.piefed.local', software='piefed')
    user = make_user(instance, 'author', local=True, with_keys=with_keys)
    community = make_community('c1')
    community.ap_featured_url = FEATURED_URL
    community.ap_moderators_url = MODERATORS_URL
    post = make_post(community, user, ap_id='https://test.piefed.local/post/1')
    mod = make_user(instance, 'themod', local=True)
    if not local_community:
        community.ap_id = 'c1@peer.example'
        community.ap_profile_id = 'https://peer.example/c/c1'
        community.ap_public_url = 'https://peer.example/c/c1'
        community.ap_followers_url = 'https://peer.example/c/c1/followers'
        community.ap_domain = 'peer.example'
    db.session.commit()
    return SimpleNamespace(instance=instance, user=user, community=community,
                           post=post, mod=mod)


def _peer(domain='peer.example', software='lemmy'):
    """A remote Instance. ALWAYS call AFTER `_seed()` -- see `_seed` for why."""
    return make_instance(domain, software=software)


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
    That is what makes it dangerous rather than merely slow: on a machine whose
    resolver hijacks NXDOMAIN into a wildcard A record, these tests start
    failing at app/utils.py:5530's `is_global` check instead.
    """
    monkeypatch.setattr(socket, 'getaddrinfo', _getaddrinfo_without_the_network)


def _make_deliverable(s, inbox=PEER_INBOX):
    """Attach the community to a real peer Instance and give it an inbox.

    The database half of `_remote_inbox`. The early-return tests call this
    ALONE, because they assert the delivery never happens and `http_mock` is
    built with `assert_all_called=True` -- a registered route that never fires
    would fail them for the wrong reason.
    """
    s.community.instance_id = _peer().id
    s.community.ap_inbox_url = inbox
    db.session.commit()


def _remote_inbox(s, http_mock, inbox=PEER_INBOX):
    """THE CAPTURE MECHANISM. Returns the respx route the activity lands on.

    Three things must be true for `:100`'s call to become an observable
    request: the community must be remote (`_seed(local_community=False)`,
    closing BOTH disjuncts of `Community.is_local()`); it must have an
    `ap_inbox_url`, which `make_community` leaves None and which `post_request`
    (app/activitypub/signature.py:109-111) short-circuits on; and the user must
    have a keypair (`_seed(with_keys=True)`), because signing calls `.encode()`
    on `user.private_key`.

    WHY THE HTTP BODY AND NOT A RECORDER: `:82` mutates `add` in place with
    `del`, so a recorder holding the dict would be read back post-`del`. respx
    captures the bytes that actually left, at
    app/activitypub/signature.py:494.
    """
    _make_deliverable(s, inbox)
    return http_mock.post(inbox).respond(200, json={})


def _sent_activity(route, index=-1):
    """The JSON body of the request `route` captured, decoded from the bytes
    that were actually sent. Defaults to the most recent call."""
    return json.loads(route.calls[index].request.content)


def _key_id_of(route, index=-1):
    """The `keyId` the captured request was signed under.

    The only observable separating `:98`'s signer (the COMMUNITY) from
    `:100`'s (the USER). respx never verifies a signature, so the key material
    leaves no trace on the wire -- only the declared keyId does.
    """
    return route.calls[index].request.headers['signature'].split('"')[1]


def _community_follower(s, http_mock=None, domain='fan.example',
                        member_name='fan', with_inbox=True):
    """A remote instance `community.following_instances()` returns at `:96`,
    plus the respx route its delivery lands on.

    Returns `SimpleNamespace(instance, member, route)`; `route` is None when
    none was registered.

    THE COMMUNITY'S KEYPAIR IS SET HERE. `:98` signs with
    `community.private_key`, which `make_community` leaves None, and signing
    calls `.encode()` on it. The author's key is reused rather than a second
    generated: generation costs about a second, respx never verifies a
    signature, and the actor a delivery was signed AS is observable through
    `keyId`, not through the key material.

    `with_inbox=False` leaves `Instance.inbox` None, closing `:97`'s FIRST
    conjunct.
    """
    assert s.user.private_key is not None, '_seed(with_keys=True) is required'
    s.community.private_key = s.user.private_key
    s.community.public_key = s.user.public_key
    instance = make_instance(domain, software='lemmy')
    instance.inbox = f'https://{domain}/inbox' if with_inbox else None
    member = make_user(instance, member_name, local=False)
    make_community_member(member, s.community)
    db.session.commit()
    route = (http_mock.post(instance.inbox).respond(200, json={})
             if http_mock is not None and with_inbox else None)
    return SimpleNamespace(instance=instance, member=member, route=route)


def _recording_task_session(monkeypatch, module):
    """Make `get_task_session` hand back a GENUINE Session that records
    `rollback()` and `close()`.

    `module` is the twin whose binding to patch -- `app.shared.tasks.adds` or
    `app.shared.tasks.removes`. BOTH import `get_task_session` into their own
    namespace, so patching `app.utils.get_task_session` would miss the binding
    the wrappers actually call and leave these tests green while observing
    nothing. Two twins, two patch targets.

    The session is real and does real work; only the observation is added, by
    wrapping the two methods rather than replacing the object.

    Returns a `SimpleNamespace(calls=[])`; the wrapper appends 'rollback' and
    'close' in the order they happened, so `finally` running after `except` is
    observable rather than assumed.
    """
    from app import db as _db
    from sqlalchemy.orm import Session as _Session

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

    monkeypatch.setattr(module, 'get_task_session', _make)
    return record


def test_the_twins_are_structurally_identical(db_session):
    """The premise this whole file rests on, asserted rather than assumed.

    `adds.py` and `removes.py` are the same file with different names. If that
    ever stops being true, this test fails and the divergence becomes a
    detectable event rather than something a later sub-project discovers.

    The comparison is deliberately coarse -- function names, extents and line
    count -- because a stricter one would fail on the legitimate naming
    differences, and a looser one would not notice a function being added.
    """
    import ast

    def shape(path):
        src = open(path).read()
        return (
            len(src.splitlines()),
            [(n.lineno, n.end_lineno) for n in ast.parse(src).body
             if isinstance(n, ast.FunctionDef)],
        )

    adds_lines, adds_extents = shape('app/shared/tasks/adds.py')
    removes_lines, removes_extents = shape('app/shared/tasks/removes.py')

    assert adds_lines == removes_lines == 100
    assert adds_extents == removes_extents == [(27, 38), (42, 53), (56, 100)]


def test_a_remote_community_receives_the_bare_add(db_session, http_mock):
    """The harness itself: `:81`'s FALSE arm reaches `:100` and the bytes are
    readable. `target` is asserted non-None because the whole ternary suite
    depends on `_seed` setting the two URL columns `make_community` leaves
    empty."""
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    add_object(db.session, s.user.id, s.post)

    sent = _sent_activity(route)
    assert sent['type'] == 'Add'
    assert sent['target'] == FEATURED_URL
    assert FEATURED_URL != MODERATORS_URL


def test_a_remote_community_receives_the_bare_remove(db_session, http_mock):
    """The removes twin of the test above -- `removes.py:100`."""
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    remove_object(db.session, s.user.id, s.post)

    sent = _sent_activity(route)
    assert sent['type'] == 'Remove'
    assert sent['target'] == FEATURED_URL


def test_a_local_only_community_does_not_federate_the_add(db_session, http_mock):
    """adds.py:63's TRUE arm via `local_only`, returning at :64."""
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    s.community.local_only = True
    db.session.commit()

    add_object(db.session, s.user.id, s.post)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_local_only_community_does_not_federate_the_remove(db_session, http_mock):
    """removes.py:63's TRUE arm via `local_only`. The twin of the test above."""
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    s.community.local_only = True
    db.session.commit()

    remove_object(db.session, s.user.id, s.post)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_dormant_instance_does_not_receive_the_add(db_session, http_mock):
    """adds.py:63's TRUE arm via `not instance.online()`.

    `Instance.online()` (app/models.py:118-119) is
    `not (self.dormant or self.gone_forever)`. `_make_deliverable` reassigns
    `community.instance_id` to the peer before its commit, so this lands on
    the instance the guard actually reads.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    s.community.instance.dormant = True
    db.session.commit()

    add_object(db.session, s.user.id, s.post)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_dormant_instance_does_not_receive_the_remove(db_session, http_mock):
    """removes.py:63's TRUE arm via `not instance.online()`. The twin of the
    test above."""
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    s.community.instance.dormant = True
    db.session.commit()

    remove_object(db.session, s.user.id, s.post)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_private_community_does_not_federate_the_add(db_session, http_mock):
    """adds.py:63's `private` conjunct -- one of this sub-project's two
    production changes.

    `Community.private` (app/models.py:611) is commented "only members can
    view. no federation.", and before this conjunct landed `:63` tested only
    `local_only` and `instance.online()` -- so a private, non-local-only
    community federated its stickies and moderator adds out.

    D309's FOURTH closed site of twelve gates; sub-projects 20, 21 and 22
    closed `notes.py:143`, `notes.py:248` and `pages.py:398`. `local_only` is
    left False deliberately: with it True the test would pass on the
    pre-existing conjunct and prove nothing.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    s.community.private = True
    db.session.commit()

    add_object(db.session, s.user.id, s.post)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_private_community_does_not_federate_the_remove(db_session, http_mock):
    """removes.py:63's `private` conjunct -- the twin, and D309's FIFTH closed
    site.

    THE TWINS ARE FIXED TOGETHER DELIBERATELY. Guarding one and not the other
    would manufacture a structural divergence between two files that are
    currently identical -- precisely the defect this campaign hunts for.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    s.community.private = True
    db.session.commit()

    remove_object(db.session, s.user.id, s.post)

    assert db.session.query(ActivityPubLog).count() == 0


def test_the_add_without_a_community_id_targets_the_featured_url(
        db_session, http_mock):
    """:58's TRUE arm (:59) and :74's FALSE arm, in adds.py.

    No `community_id` means the community comes from `object.community` at
    :59, and `:74` selects `ap_featured_url`. `target` is the witness for
    both at once, and it can only discriminate because `_seed` sets the two
    URL columns to different values -- `make_community` leaves both None.
    """
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    add_object(db.session, s.user.id, s.post)

    sent = _sent_activity(route)
    assert sent['target'] == FEATURED_URL
    assert set(sent) == {'id', 'type', 'actor', 'object', 'target',
                         '@context', 'audience', 'to', 'cc'}
    assert sent['actor'] == s.user.public_url()
    assert sent['object'] == s.post.public_url()
    assert sent['audience'] == s.community.public_url()
    assert sent['to'] == ['https://www.w3.org/ns/activitystreams#Public']
    assert sent['cc'] == [s.community.public_url()]


def test_the_add_with_a_community_id_targets_the_moderators_url(
        db_session, http_mock):
    """:58's FALSE arm (:61) and :74's TRUE arm, in adds.py.

    The companion to the test above: passing `community_id` resolves the
    community by query at :61 and selects `ap_moderators_url` at :74. Asserting
    a value the other arm could not produce is what makes the pair
    discriminating.
    """
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    add_object(db.session, s.user.id, s.mod, s.community.id)

    sent = _sent_activity(route)
    assert sent['target'] == MODERATORS_URL
    assert sent['object'] == s.mod.public_url()


def test_the_remove_without_a_community_id_targets_the_featured_url(
        db_session, http_mock):
    """:58's TRUE arm and :74's FALSE arm, in removes.py -- the twin."""
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    remove_object(db.session, s.user.id, s.post)

    sent = _sent_activity(route)
    assert sent['type'] == 'Remove'
    assert sent['target'] == FEATURED_URL
    assert sent['object'] == s.post.public_url()


def test_the_remove_with_a_community_id_targets_the_moderators_url(
        db_session, http_mock):
    """:58's FALSE arm and :74's TRUE arm, in removes.py -- the twin."""
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    remove_object(db.session, s.user.id, s.mod, s.community.id)

    sent = _sent_activity(route)
    assert sent['type'] == 'Remove'
    assert sent['target'] == MODERATORS_URL
    assert sent['object'] == s.mod.public_url()


def test_the_remote_add_keeps_its_context_and_is_signed_as_the_user(
        db_session, http_mock):
    """`@context` is asserted PRESENT: nothing nests this object on this path,
    because :82's `del` runs only under `is_local()`. And :100 signs with
    `user.public_url() + '#main-key'` where :98 signs as the COMMUNITY --
    `keyId` is the only observable that separates them.
    """
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    add_object(db.session, s.user.id, s.post)

    assert '@context' in _sent_activity(route)
    assert _key_id_of(route) == s.user.public_url() + '#main-key'


def test_the_remote_remove_keeps_its_context_and_is_signed_as_the_user(
        db_session, http_mock):
    """The removes twin of the test above."""
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    remove_object(db.session, s.user.id, s.post)

    assert '@context' in _sent_activity(route)
    assert _key_id_of(route) == s.user.public_url() + '#main-key'


def test_a_local_community_announces_the_add_and_strips_its_inner_context(
        db_session, http_mock):
    """adds.py:81's TRUE arm -- :82's `del add['@context']` and the Announce
    built at :87-95.

    The Announce keeps the `@context` built at :92; the Add nested at :91 has
    had its own stripped at :82. Asserting BOTH directions is what catches a
    mutant deleting from the wrong object -- either alone would still accept a
    well-formed activity.

    `cc` is the followers collection here (:86), NOT the
    `[community.public_url()]` the inner Add carries (:68) -- :86 rebinds the
    name to a NEW list, so the inner object's `cc` still points at the old one.
    Asserting both proves the rebinding did not alias.
    """
    s = _seed(with_keys=True)
    fan = _community_follower(s, http_mock)

    add_object(db.session, s.user.id, s.post)

    sent = _sent_activity(fan.route)
    assert sent['type'] == 'Announce'
    assert '@context' in sent
    assert sent['actor'] == s.community.public_url()
    assert sent['object']['type'] == 'Add'
    assert '@context' not in sent['object']
    assert sent['cc'] == [s.community.ap_followers_url]
    assert sent['object']['cc'] == [s.community.public_url()]


def test_a_local_community_announces_the_remove_and_strips_its_inner_context(
        db_session, http_mock):
    """removes.py:81's TRUE arm -- the twin of the test above, asserting the
    same asymmetry on the Remove."""
    s = _seed(with_keys=True)
    fan = _community_follower(s, http_mock)

    remove_object(db.session, s.user.id, s.post)

    sent = _sent_activity(fan.route)
    assert sent['type'] == 'Announce'
    assert '@context' in sent
    assert sent['object']['type'] == 'Remove'
    assert '@context' not in sent['object']
    assert sent['cc'] == [s.community.ap_followers_url]
    assert sent['object']['cc'] == [s.community.public_url()]


def test_the_announced_add_is_signed_as_the_community(db_session, http_mock):
    """adds.py:98 signs with `community.private_key` and
    `community.public_url() + '#main-key'` -- the companion to Task 3's
    user-signed assertion. The community's and user's public urls differ in
    path, so a mutant swapping the signer produces a valid but wrong keyId."""
    s = _seed(with_keys=True)
    fan = _community_follower(s, http_mock)

    add_object(db.session, s.user.id, s.post)

    assert _key_id_of(fan.route) == s.community.public_url() + '#main-key'


def test_the_announced_remove_is_signed_as_the_community(db_session, http_mock):
    """removes.py:98 -- the twin of the test above."""
    s = _seed(with_keys=True)
    fan = _community_follower(s, http_mock)

    remove_object(db.session, s.user.id, s.post)

    assert _key_id_of(fan.route) == s.community.public_url() + '#main-key'


def test_a_local_community_with_no_followers_sends_no_add(db_session, http_mock):
    """adds.py:96's loop never entered -- straight past to the return.

    `following_instances()` returns empty because no CommunityMember exists on
    a remote instance. The Announce is still BUILT at :87-95; nothing delivers
    it.
    """
    s = _seed(with_keys=True)

    add_object(db.session, s.user.id, s.post)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_local_community_with_no_followers_sends_no_remove(
        db_session, http_mock):
    """removes.py:96's loop never entered -- the twin."""
    s = _seed(with_keys=True)

    remove_object(db.session, s.user.id, s.post)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_following_instance_without_an_inbox_gets_no_add(db_session, http_mock):
    """adds.py:97's FALSE arm -- the loop continuing.

    `with_inbox=False` leaves `Instance.inbox` None, closing :97's FIRST
    conjunct before any other is evaluated. No route is registered, so
    `http_mock`'s `assert_all_called=True` is not tripped, and the count of 0
    rules out a request attempted against a None inbox.
    """
    s = _seed(with_keys=True)
    _community_follower(s, http_mock, with_inbox=False)

    add_object(db.session, s.user.id, s.post)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_following_instance_without_an_inbox_gets_no_remove(
        db_session, http_mock):
    """removes.py:97's FALSE arm -- the twin."""
    s = _seed(with_keys=True)
    _community_follower(s, http_mock, with_inbox=False)

    remove_object(db.session, s.user.id, s.post)

    assert db.session.query(ActivityPubLog).count() == 0


def test_one_instance_is_skipped_while_another_receives_the_add(
        db_session, http_mock, monkeypatch):
    """Both of adds.py's loop arms in ONE run -- the skip, then the delivery.

    The discriminating case: with a single follower, "skipped" and "loop
    ended" both produce zero deliveries and are indistinguishable. The second,
    delivering instance is what proves iteration continued PAST the skipped
    one.

    THE SEQUENCE IS CONTROLLED, NOT OBSERVED. `Community.following_instances()`
    (app/models.py:842-851) ends in an unordered `.distinct().all()` -- no
    ORDER BY -- so which instance a live query returns first is a property of
    Postgres's query plan, not of the code. This test's subject is the loop's
    continue-past-a-skip behaviour; that ordering is incidental to it, so
    `following_instances` is patched for the duration of the call to hand back
    a known sequence (the skipped instance first, then the delivering one)
    instead of trusting the query to happen to agree. An earlier version of
    this test asserted the LIVE order matched that sequence, on the reasoning
    that a reversal would silently defeat a "skip and continue" -> "skip and
    break" mutant; that assertion was reproducibly flaky in practice --
    confirmed to fail on a first run and pass on an immediate re-run of the
    same, unmutated tree (task-7-review.md) -- because Postgres was free to
    answer either order. Controlling the sequence here is strictly more
    discriminating than that observed assertion ever was, since it no longer
    depends on the planner cooperating.
    """
    s = _seed(with_keys=True)
    mute = _community_follower(s, http_mock, domain='mute.example',
                        member_name='mute', with_inbox=False)
    good = _community_follower(s, http_mock, domain='fan.example',
                               member_name='fan')
    monkeypatch.setattr(s.community, 'following_instances',
                        lambda *a, **kw: [mute.instance, good.instance])

    add_object(db.session, s.user.id, s.post)

    assert good.route.call_count == 1
    assert db.session.query(ActivityPubLog).count() == 1


def test_one_instance_is_skipped_while_another_receives_the_remove(
        db_session, http_mock, monkeypatch):
    """Both of removes.py's loop arms in one run -- the twin of the test
    above, controlling the sequence for the same reason."""
    s = _seed(with_keys=True)
    mute = _community_follower(s, http_mock, domain='mute.example',
                        member_name='mute', with_inbox=False)
    good = _community_follower(s, http_mock, domain='fan.example',
                               member_name='fan')
    monkeypatch.setattr(s.community, 'following_instances',
                        lambda *a, **kw: [mute.instance, good.instance])

    remove_object(db.session, s.user.id, s.post)

    assert good.route.call_count == 1
    assert db.session.query(ActivityPubLog).count() == 1


def test_sticky_post_delivers_an_add_targeting_featured(db_session, http_mock):
    """`sticky_post` (:27-38) calls `add_object` with NO community_id, so it
    drives :58's true arm and :74's false arm. `send_async` is accepted and
    ignored; None is passed to prove it is not read."""
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    sticky_post(None, s.user.id, s.post.id)

    sent = _sent_activity(route)
    assert sent['type'] == 'Add'
    assert sent['target'] == FEATURED_URL


def test_unsticky_post_delivers_a_remove_targeting_featured(
        db_session, http_mock):
    """`unsticky_post` (:27-38 of removes.py) -- the twin."""
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    unsticky_post(None, s.user.id, s.post.id)

    sent = _sent_activity(route)
    assert sent['type'] == 'Remove'
    assert sent['target'] == FEATURED_URL


def test_add_mod_delivers_an_add_targeting_moderators(db_session, http_mock):
    """`add_mod` (:42-53) passes community_id, driving :58's false arm and
    :74's true arm. The `target` difference from `sticky_post` is the whole
    witness that the two wrappers take opposite arms."""
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    add_mod(None, s.user.id, s.mod.id, s.community.id)

    sent = _sent_activity(route)
    assert sent['type'] == 'Add'
    assert sent['target'] == MODERATORS_URL
    assert sent['object'] == s.mod.public_url()


def test_remove_mod_delivers_a_remove_targeting_moderators(
        db_session, http_mock):
    """`remove_mod` (:42-53 of removes.py) -- the twin."""
    s = _seed(local_community=False, with_keys=True)
    route = _remote_inbox(s, http_mock)

    remove_mod(None, s.user.id, s.mod.id, s.community.id)

    sent = _sent_activity(route)
    assert sent['type'] == 'Remove'
    assert sent['target'] == MODERATORS_URL


def test_sticky_post_rolls_back_and_closes_on_a_missing_post(
        db_session, monkeypatch):
    """adds.py:34-36's except arm and :37-38's finally, reached by a NATURAL
    raise.

    `:32` uses `.get()`, which returns None for an absent id, and `:59`'s
    `object.community` then raises AttributeError -- NOT NoResultFound, which
    is what `:47`'s `.one()` raises twelve lines away in the same file.

    The recorded call ORDER is the assertion that `finally` ran after
    `except`, which a bare "was close called" check could not distinguish from
    a wrapper that closed instead of rolling back.
    """
    import app.shared.tasks.adds as adds_module
    s = _seed()
    record = _recording_task_session(monkeypatch, adds_module)

    with pytest.raises(AttributeError):
        sticky_post(None, s.user.id, s.post.id + 1000)

    assert record.calls == ['rollback', 'close']


def test_unsticky_post_rolls_back_and_closes_on_a_missing_post(
        db_session, monkeypatch):
    """removes.py's copy of the same handler -- a SEPARATE function body and
    so a separate pair of arcs. Written out rather than parametrised so each
    twin's arms are attributable to a named test."""
    import app.shared.tasks.removes as removes_module
    s = _seed()
    record = _recording_task_session(monkeypatch, removes_module)

    with pytest.raises(AttributeError):
        unsticky_post(None, s.user.id, s.post.id + 1000)

    assert record.calls == ['rollback', 'close']


def test_add_mod_rolls_back_and_closes_on_a_missing_mod(
        db_session, monkeypatch):
    """adds.py:49-51's except arm, reached by the OTHER lookup style.

    `:47` uses `.filter_by(id=mod_id).one()`, which raises NoResultFound for an
    absent id -- a different exception from the sibling wrapper twelve lines
    above, in the same file. This is why each wrapper's mechanic is established
    by reading rather than inherited.
    """
    from sqlalchemy.exc import NoResultFound
    import app.shared.tasks.adds as adds_module
    s = _seed()
    record = _recording_task_session(monkeypatch, adds_module)

    with pytest.raises(NoResultFound):
        add_mod(None, s.user.id, s.mod.id + 1000, s.community.id)

    assert record.calls == ['rollback', 'close']


def test_remove_mod_rolls_back_and_closes_on_a_missing_mod(
        db_session, monkeypatch):
    """removes.py's copy of the `.one()` handler -- the twin."""
    from sqlalchemy.exc import NoResultFound
    import app.shared.tasks.removes as removes_module
    s = _seed()
    record = _recording_task_session(monkeypatch, removes_module)

    with pytest.raises(NoResultFound):
        remove_mod(None, s.user.id, s.mod.id + 1000, s.community.id)

    assert record.calls == ['rollback', 'close']


def test_sticky_post_closes_the_session_on_the_happy_path(
        db_session, http_mock, monkeypatch):
    """adds.py's finally on the SUCCESS path -- `close` with no `rollback`.

    The control for the error tests: without it, `finally` running is only ever
    observed alongside an exception, and a wrapper that closed only in the
    except arm would pass everything else in this file.
    """
    import app.shared.tasks.adds as adds_module
    s = _seed(local_community=False, with_keys=True)
    _remote_inbox(s, http_mock)
    record = _recording_task_session(monkeypatch, adds_module)

    sticky_post(None, s.user.id, s.post.id)

    assert record.calls == ['close']


def test_unsticky_post_closes_the_session_on_the_happy_path(
        db_session, http_mock, monkeypatch):
    """removes.py's happy-path control -- the twin."""
    import app.shared.tasks.removes as removes_module
    s = _seed(local_community=False, with_keys=True)
    _remote_inbox(s, http_mock)
    record = _recording_task_session(monkeypatch, removes_module)

    unsticky_post(None, s.user.id, s.post.id)

    assert record.calls == ['close']


# ---------------------------------------------------------------------------
# Mutation-testing record (sub-project 23, Task 7)
#
# Both twins (app/shared/tasks/adds.py, app/shared/tasks/removes.py) were
# mutated one line at a time, one file at a time, and run against this file
# alone (35 tests collected as of this commit). Each mutation was applied
# with a single targeted `sed -i` to one file, tested, then reverted with
# `git checkout -- app/` before the next mutation; `git diff -- app/` was
# confirmed empty and both files confirmed at 100 lines after every restore.
#
# Columns: line = pre-mutation source line; sed = the exact substitution
# applied; result = kill/survivor/equivalent/no-op, sole (one failing test)
# or multi (several), and assertion-kill (AssertionError) vs crash-kill
# (uncaught exception, reported by pytest as ERROR).
#
# M1  :58  invert the `community_id` test
#     sed -i '58s/if not community_id:/if community_id:/'
#     -> produced: "    if community_id:"
#     adds.py:    KILL, multi (16 failed / 19 passed, mixes assertion and
#                 crash kills -- 10 of the 16 also errored in teardown/setup)
#     removes.py: KILL, multi (16 failed / 19 passed, same mix) -- symmetric
#
# M2  :63  drop the `private` conjunct
#     sed -i '63s/ or community.private//'
#     -> produced: "    if community.local_only or not community.instance.online():"
#     adds.py:    KILL, sole assertion-kill (1 failed / 34 passed)
#                 test_a_private_community_does_not_federate_the_add
#     removes.py: KILL, sole assertion-kill (1 failed / 34 passed)
#                 test_a_private_community_does_not_federate_the_remove
#     Matches the previously-recorded twin-specific result from Tasks 2-6
#     (each twin's reversion is caught only by that twin's own test).
#
# M3  :63  drop the `local_only` conjunct
#     sed -i '63s/community.local_only or //'
#     -> produced: "    if community.private or not community.instance.online():"
#     adds.py:    KILL, sole assertion-kill (1 failed / 34 passed)
#                 test_a_local_only_community_does_not_federate_the_add
#     removes.py: KILL, sole assertion-kill (1 failed / 34 passed)
#                 test_a_local_only_community_does_not_federate_the_remove
#     Re-dry-run against the post-Task-2 (three-conjunct) line as required;
#     not a no-op here -- symmetric across twins.
#
# M4  :63  drop the `online()` conjunct
#     sed -i '63s/ or not community.instance.online()//'
#     -> produced: "    if community.local_only or community.private: "
#     adds.py:    KILL, sole assertion-kill (1 failed / 34 passed)
#                 test_a_dormant_instance_does_not_receive_the_add
#     removes.py: KILL, sole assertion-kill (1 failed / 34 passed)
#                 test_a_dormant_instance_does_not_receive_the_remove
#     Re-dry-run against the post-Task-2 line as required; not a no-op here
#     -- symmetric across twins.
#
# M5  :74  swap the ternary's arms
#     sed -i '74s/ap_moderators_url if community_id else community.ap_featured_url/ap_featured_url if community_id else community.ap_moderators_url/'
#     -> produced: "      'target': community.ap_featured_url if community_id else community.ap_moderators_url,"
#     adds.py:    KILL, multi assertion-kill, no crashes (5 failed / 30 passed)
#                 test_a_remote_community_receives_the_bare_add
#                 test_the_add_without_a_community_id_targets_the_featured_url
#                 test_the_add_with_a_community_id_targets_the_moderators_url
#                 test_sticky_post_delivers_an_add_targeting_featured
#                 test_add_mod_delivers_an_add_targeting_moderators
#     removes.py: KILL, multi assertion-kill, no crashes (5 failed / 30 passed)
#                 test_a_remote_community_receives_the_bare_remove
#                 test_the_remove_without_a_community_id_targets_the_featured_url
#                 test_the_remove_with_a_community_id_targets_the_moderators_url
#                 test_unsticky_post_delivers_a_remove_targeting_featured
#                 test_remove_mod_delivers_a_remove_targeting_moderators
#     This is the mutation that matters most: it is the only check that the
#     :74 ternary's two arms are genuinely discriminated by the seeded data
#     (coverage alone cannot see this). Both twins killed it cleanly.
#
# M6  :81  invert `is_local()`
#     sed -i '81s/if community.is_local():/if not community.is_local():/'
#     -> produced: "    if not community.is_local():"
#     adds.py:    KILL, multi, mixing assertion and crash kills
#                 (11 failed / 24 passed, 10 errors; 12 unique tests touched)
#     removes.py: KILL, multi, mixing assertion and crash kills
#                 (11 failed / 24 passed, 10 errors; 12 unique tests touched)
#     Expected shape per the plan: flipping the local/remote branch for
#     every test at once produces both failed assertions and tests that
#     crash trying to reach following_instances()/ap_inbox_url on the wrong
#     kind of community. Symmetric across twins.
#
# M7  :82  comment out the inner `del`
#     sed -i '82s|^|#|'
#     -> produced: "#        del add['@context']"  (adds.py)
#                  "#        del remove['@context']"  (removes.py)
#     adds.py:    KILL, sole assertion-kill (1 failed / 34 passed)
#                 test_a_local_community_announces_the_add_and_strips_its_inner_context
#     removes.py: KILL, sole assertion-kill (1 failed / 34 passed)
#                 test_a_local_community_announces_the_remove_and_strips_its_inner_context
#     Note: the first run against removes.py under this mutation reported an
#     extra, unrelated failure/error on an *adds* test
#     (test_one_instance_is_skipped_while_another_receives_the_add), which
#     cannot be caused by a removes.py-only change. Re-running the identical
#     mutation reproduced only the expected sole kill above, confirming the
#     first result was a transient flake (not a mutation effect, not a real
#     twin asymmetry) rather than a genuine divergence.
#
# M8  :97  drop the `inbox` conjunct
#     sed -i '97s/instance.inbox and //'
#     -> produced: "            if instance.online() and not user.has_blocked_instance(instance.id) and not instance_banned(instance.domain):"
#     adds.py:    KILL, multi assertion-kill, no crashes (2 failed / 33 passed)
#                 test_a_following_instance_without_an_inbox_gets_no_add
#                 test_one_instance_is_skipped_while_another_receives_the_add
#     removes.py: KILL, multi assertion-kill, no crashes (2 failed / 33 passed)
#                 test_a_following_instance_without_an_inbox_gets_no_remove
#                 test_one_instance_is_skipped_while_another_receives_the_remove
#
# Summary: 8/8 mutations x 2 twins = 16/16 runs, all KILLS. Zero survivors,
# zero equivalent mutants, zero no-op substitutions (M2/M3/M4 were re-dry-run
# against the post-Task-2 :63 line, as required, and all three mutated it
# correctly this time). No twin asymmetry was found: every mutation killed
# both twins with the same shape (same kill count, same kill type, mirrored
# test names). Counts above are as-of this commit, against the 35-test file;
# a later re-run against a larger file should read higher pass counts as
# growth, not as a discrepancy with this record.
#
# Tree state after every mutation cycle: `git diff -- app/` empty and both
# app/shared/tasks/adds.py and app/shared/tasks/removes.py at 100 lines,
# verified before starting the next mutation.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Unreachability and ternary reconciliation (sub-project 23, Task 8)
#
# Measured at commit 3359cb53 over the full suite (3976 passed, 3 skipped,
# 6 subtests, exit 0). BOTH TWINS FINISHED AT 100.0000%: 51 statements with
# 0 missing and 10 branches with 0 partial, in each file. Every one of the six
# functions -- sticky_post/unsticky_post (:27-38), add_mod/remove_mod (:42-53)
# and add_object/remove_object (:56-100), extents by ast.parse and
# FunctionDef.end_lineno -- is at zero missing statements and zero missing arms.
#
# THIS BLOCK THEREFORE HAS NO UNCOVERED ARM TO ARGUE ABOUT, AND THAT IS THE
# RESULT RATHER THAN AN OMISSION. The plan's step "write a test for each
# remaining reachable arm" had nothing to do by the time it was reached: Tasks
# 2-6 had already closed every arm in both files. Sub-projects 21 and 22 closed
# their modules to 99.09% and 98.65%, each carrying proved-unreachable
# residuals; these two carry none, and they are the campaign's first modules to
# reach 100.
#
# The ten executed arcs, identical in both files:
#   (58, 59)  (58, 61)  (63, 64)  (63, 66)  (81, 82)  (81, 100)
#   (96, -56) (96, 97)  (97, 96)  (97, 98)
#
# ONE ITEM IS STILL UNREACHABLE, AND IT IS A REDUNDANCY FINDING RATHER THAN A
# COVERAGE GAP.
#
#   :97's `instance.online()` conjunct, in BOTH twins, is unreachable-False.
#   ESTABLISHER: the SQL query that produced the loop variable.
#   `instance` comes from `community.following_instances()` at :96, which is
#   `Community.following_instances` (app/models.py:842-851) called with the
#   default `include_dormant=False`; :849 filters `Instance.dormant == False`
#   and :850 filters `Instance.id != 1, Instance.gone_forever == False`, and
#   `Instance.online()` (app/models.py:118-119) is exactly
#   `not (self.dormant or self.gone_forever)`. Both columns the method reads
#   are already pinned in SQL, so the conjunct is True for every row the loop
#   can yield. This is fact 75's cause 4(b) with the query as the establisher,
#   the same shape registered as D302 -- of which these two sites are the
#   seventh and eighth.
#
#   IT APPEARS IN NO RESIDUAL, AND MUST NOT BE DESCRIBED AS AN UNCOVERED ARM.
#   coverage.py records a branch arc at the `if` level, not per conjunct, so a
#   short-circuited conjunct gets no arc of its own. Both of :97's arcs --
#   (97, 98) taken and (97, 96) skipped -- are executed above, and the skip is
#   produced by the LIVE `instance.inbox` conjunct
#   (test_a_following_instance_without_an_inbox_gets_no_add and its removes
#   twin). Zero missing arms is what a redundant conjunct looks like; it is not
#   evidence that every conjunct has both values.
#
#   NOT CHASED, DELIBERATELY. A test seeding `dormant=True` on a follower would
#   be green and worthless: the query would not return the row at all, so the
#   conjunct would never be evaluated on it. Dropping `online()` from :97 is an
#   EQUIVALENT MUTANT BY CONSTRUCTION (fact 120's second class), which is why
#   the 16-run sweep above mutates the live `instance.inbox` conjunct at the
#   same line (M8, a double assertion-kill in each twin) and not this one.
#
# TERNARY RECONCILIATION -- the ast walk's RAW OUTPUT, not a confirmation of an
# expectation:
#
#   $ python3 - <<'PY'
#     import ast
#     for path in ('app/shared/tasks/adds.py', 'app/shared/tasks/removes.py'):
#         src = open(path).read()
#         print(path)
#         for node in ast.walk(ast.parse(src)):
#             if isinstance(node, ast.FunctionDef):
#                 print('  ', node.name, node.lineno, node.end_lineno)
#                 for sub in ast.walk(node):
#                     if isinstance(sub, ast.IfExp):
#                         print('     TERNARY', sub.lineno,
#                               ast.get_source_segment(src, sub))
#     PY
#   app/shared/tasks/adds.py
#      sticky_post 27 38
#      add_mod 42 53
#      add_object 56 100
#        TERNARY 74 community.ap_moderators_url if community_id else community.ap_featured_url
#   app/shared/tasks/removes.py
#      unsticky_post 27 38
#      remove_mod 42 53
#      remove_object 56 100
#        TERNARY 74 community.ap_moderators_url if community_id else community.ap_featured_url
#
#   EXACTLY ONE TERNARY PER TWIN, both at :74, and each arm has a named test:
#
#   adds.py:74    TRUE  (ap_moderators_url)
#                 test_the_add_with_a_community_id_targets_the_moderators_url
#                 test_add_mod_delivers_an_add_targeting_moderators
#   adds.py:74    FALSE (ap_featured_url)
#                 test_the_add_without_a_community_id_targets_the_featured_url
#                 test_sticky_post_delivers_an_add_targeting_featured
#   removes.py:74 TRUE  (ap_moderators_url)
#                 test_the_remove_with_a_community_id_targets_the_moderators_url
#                 test_remove_mod_delivers_a_remove_targeting_moderators
#   removes.py:74 FALSE (ap_featured_url)
#                 test_the_remove_without_a_community_id_targets_the_featured_url
#                 test_unsticky_post_delivers_a_remove_targeting_featured
#
#   THE COVERAGE FIGURES CANNOT SEE ANY OF THAT (fact 87): a ternary is one
#   statement on one line, both arms execute it, and no arc is recorded for the
#   choice. The arms are proved discriminating by MUTATION instead -- M5 in the
#   record above swaps them and is a five-test assertion-kill in each twin --
#   and by `_seed` setting `ap_featured_url` and `ap_moderators_url` to
#   DISTINCT non-None values. make_community (tests/factories.py:122) leaves
#   both at None, and with both None the two arms return the same value, every
#   `target` assertion passes under either, and the eight tests above prove
#   nothing while still reading as fully covered.
# ---------------------------------------------------------------------------
