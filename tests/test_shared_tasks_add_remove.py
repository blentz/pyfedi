"""`add_object` and `remove_object` -- the twin builders of AP Add and Remove.

`app/shared/tasks/adds.py:56-100` and `app/shared/tasks/removes.py:56-100`
(extents by ast). THE TWO MODULES ARE STRUCTURALLY IDENTICAL: both are 100
lines and 51 statements, their coverage residuals are the same lines and the
same arcs function for function, and after normalising names the only textual
difference between the files is one docstring word. They share even their dead
imports -- `post_request` is imported at `:2` of each and called in neither.

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
