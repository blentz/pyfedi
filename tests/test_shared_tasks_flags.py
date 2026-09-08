"""`report_post` and `report_reply` -- the AP Flag senders.

`app/shared/tasks/flags.py`, 78 lines, three functions: two `@celery.task`
wrappers (`report_reply:25`, `report_post:40`) delegating to `report_object:54`.
This is the SIXTH module in which the campaign has met this shape, after
`notes.py`, `pages.py`, `adds.py` and `removes.py`, and the harness below is
transferred from `tests/test_shared_tasks_add_remove.py` rather than rebuilt.

THE TWO WRAPPER LOOKUPS DIFFER, FIFTEEN LINES APART, IN ONE FILE:
`:30` is `session.query(PostReply).filter_by(id=reply_id).one()` and raises
`NoResultFound` for a missing id; `:45` is `session.query(Post).get(post_id)`
and returns None, so the raise arrives later as `AttributeError` when `:56`
reads `object.community`. Establish each by READING, never by inheriting from
its neighbour -- this is the fourth module in which the campaign has met this
trap, and sub-project 21 lost a fix round to it.

WHAT DOES NOT APPLY HERE. `@context` is set at `:67`, inside the `flag` dict,
and `flag` IS the top-level posted object -- there is no Announce wrapper in
this module. The nested-`@context`-absence assertions that carried sub-projects
20-23 have no subject here, and asserting the absence would be asserting a
property of a structure that does not exist.

ORDER IS NOT GUARANTEED AND IS NEVER ASSERTED. `:73` runs
`session.query(Instance).filter(Instance.id.in_(instance_ids))` through the
TASK session, so the rows are neither the test's objects nor in any promised
order. Every assertion over delivered inboxes in this file is set-based.
Sub-project 23 spent a ruling removing the last two ordered assertions from
this suite; this file does not add a third.
"""

import json
import socket
from types import SimpleNamespace

import pytest
from sqlalchemy.orm.exc import NoResultFound

from app import db
from app.models import ActivityPubLog
from app.shared.tasks.flags import report_post, report_reply
from tests.factories import (
    make_community, make_instance, make_post, make_post_reply, make_user,
)

PEER_INBOX = 'https://peer.example/inbox'
OTHER_INBOX = 'https://other.example/inbox'

_REAL_GETADDRINFO = socket.getaddrinfo

# Any globally routable literal will do -- the only property
# app/utils.py:5530 reads off it is `is_global`.
_EXAMPLE_TLD_ADDRESS = '93.184.216.34'


def _seed(local_community=True, with_keys=False):
    """instance, user, author, community, post, reply -- committed, in that
    binding order (see the `SimpleNamespace(...)` call at the end of this
    function).

    `user` IS THE REPORTING USER, bound from a local variable named
    `reporter` -- the returned object has NO field named `reporter`, only
    `user`. `author` is the user who wrote `post` and `reply`. A caller
    passes `s.user.id` as `report_post`/`report_reply`'s `user_id` argument.

    ORDER IS LOAD-BEARING. `make_community` (tests/factories.py:122) hardcodes
    `instance_id=1` and the db_session teardown resets every sequence, so the
    local instance is created FIRST; a peer built before this call would take
    id 1 and leave the community's FK pointing at it.

    `local_community=False` sets `ap_id` AND `ap_profile_id` on peer.example,
    because `Community.is_local()` (app/models.py:795) is a DISJUNCTION whose
    `profile_id()` falls back to a computed default; `ap_id` alone leaves the
    community silently LOCAL (fact 112).
    """
    instance = make_instance('test.piefed.local', software='piefed')
    reporter = make_user(instance, 'reporter', local=True, with_keys=with_keys)
    author = make_user(instance, 'author', local=True)
    community = make_community('c1')
    post = make_post(community, author, ap_id='https://test.piefed.local/post/1')
    reply = make_post_reply(post, author, body='a reply')
    if not local_community:
        community.ap_id = 'c1@peer.example'
        community.ap_profile_id = 'https://peer.example/c/c1'
        community.ap_public_url = 'https://peer.example/c/c1'
        community.ap_domain = 'peer.example'
    db.session.commit()
    return SimpleNamespace(instance=instance, user=reporter, author=author,
                           community=community, post=post, reply=reply)


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
    On a machine whose resolver hijacks NXDOMAIN into a wildcard A record,
    these tests start failing at app/utils.py:5530's `is_global` check instead.
    """
    monkeypatch.setattr(socket, 'getaddrinfo', _getaddrinfo_without_the_network)


def _make_deliverable(s, online=True):
    """Put the community on a real peer Instance so `:57`'s gate passes.

    The database half of `_reporting_instance`. The early-return tests call
    this ALONE, because they assert the delivery never happens and `http_mock`
    is built with `assert_all_called=True` -- a registered route that never
    fires would fail them for the wrong reason.

    `Instance.online()` (app/models.py:118-119) is exactly
    `not (self.dormant or self.gone_forever)`, so `online=False` sets both.
    """
    peer = _peer()
    if not online:
        peer.dormant = True
        peer.gone_forever = True
    s.community.instance_id = peer.id
    db.session.commit()
    return peer


def _reporting_instance(s, http_mock, inbox=PEER_INBOX, domain='recipient.example'):
    """THE CAPTURE MECHANISM. Returns (route, instance_id).

    `report_object` does NOT deliver to the community's instance -- it delivers
    to the instances named in `instance_ids`, queried at `:73`. So the
    community's instance controls only the `:57` gate; the RECIPIENT is a
    separate Instance row created here.

    Three things must be true for `:76`'s call to become an observable request:
    `:57`'s gate must pass (`_make_deliverable`), the recipient Instance must
    have an `inbox`, and the reporter must have a keypair
    (`_seed(with_keys=True)`), because signing calls `.encode()` on
    `user.private_key`.

    WHY THE INBOX MATTERS IS NOT THAT `post_request` "SHORT-CIRCUITS" ON A
    MISSING ONE -- that verb would make this file's early-return assertions
    look vacuous. `post_request` builds and `session.add`s its
    `ActivityPubLog` row UNCONDITIONALLY at app/activitypub/signature.py:105,
    BEFORE the transport and before the uri check at :109-111, which does not
    return early either: it marks the already-written row `failure` /
    `empty uri`. So a row is written for ANY attempted delivery, including one
    to a None inbox. That is exactly what makes
    `assert db.session.query(ActivityPubLog).count() == 0` a real observation
    -- it distinguishes "the guard returned before `:73`" from "the loop ran
    and delivered nowhere", which a short-circuit reading would say it cannot.

    NOTE THE LINE NUMBER. Sub-project 23's copy of this docstring cited `:102`
    for the `session.add`; `:102` is an unrelated `type = ...` statement and
    the add is at `:105`. Corrected here.

    WHY THE HTTP BODY AND NOT A RECORDER: respx captures the bytes that
    actually left, handed to the transport as `data=body_bytes` at
    app/activitypub/signature.py:498. A recorder holding the dict would be
    read back after any in-place mutation.
    """
    recipient = make_instance(domain, software='lemmy')
    recipient.inbox = inbox
    db.session.commit()
    return http_mock.post(inbox).respond(200, json={}), recipient.id


def _sent_activity(route, index=-1):
    """The JSON body of the request `route` captured, decoded from the bytes
    that were actually sent. Defaults to the most recent call."""
    return json.loads(route.calls[index].request.content)


def _delivered_inboxes(*routes):
    """The SET of inboxes that received a request. Never a list, never ordered
    -- see the module docstring."""
    return {str(r.calls[i].request.url) for r in routes for i in range(len(r.calls))}


def test_a_local_only_community_sends_no_flag(db_session, http_mock):
    """`:57`'s first disjunct. `local_only` returns before the envelope is
    built, so nothing is sent AND no ActivityPubLog row is written.

    NO ROUTE IS REGISTERED. `http_mock` is built with
    `assert_all_called=True`, so a route registered here and never called
    would fail this test for the wrong reason.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    recipient = make_instance('recipient.example', software='lemmy')
    recipient.inbox = PEER_INBOX
    s.community.local_only = True
    db.session.commit()

    report_post(None, s.user.id, s.post.id, 'spam', [recipient.id])

    assert db.session.query(ActivityPubLog).count() == 0


def test_an_offline_community_instance_sends_no_flag(db_session, http_mock):
    """`:57`'s third disjunct. `Instance.online()` (app/models.py:118-119) is
    `not (self.dormant or self.gone_forever)`, so a dormant-and-gone instance
    returns.

    THE CONTROL FOR THIS TEST IS Task 1's SMOKE TEST, which runs the same path
    with `online=True` and DOES deliver. Without that pairing, an assertion of
    zero rows passes against any breakage that stops delivery for any reason.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s, online=False)
    recipient = make_instance('recipient.example', software='lemmy')
    recipient.inbox = PEER_INBOX
    db.session.commit()

    report_post(None, s.user.id, s.post.id, 'spam', [recipient.id])

    assert db.session.query(ActivityPubLog).count() == 0


def test_report_post_delivers_a_flag_to_the_named_instance(
        db_session, http_mock):
    """`report_post:40` end to end: `.get()` at `:45`, the gate at `:57`
    passing, the envelope at `:60-71`, and the loop at `:73-76` delivering.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, recipient_id = _reporting_instance(s, http_mock)

    report_post(None, s.user.id, s.post.id, 'spam', [recipient_id])

    flag = _sent_activity(route)
    assert flag['type'] == 'Flag'
    assert flag['summary'] == 'spam'
    assert flag['object'] == s.post.public_url()
    assert flag['actor'] == s.user.public_url()


def test_a_private_community_sends_no_flag(db_session, http_mock):
    """D309's site in this module. Before this commit, `:57` gated on
    `local_only` and `instance.online()` but not on `Community.private`, so
    a report about content in a private community federated out. This test
    pins down that `community.private` is now part of the guard.

    This test FAILS without the `private` conjunct -- a real ActivityPubLog
    row is written and a real request is attempted -- and passes with it.

    THE ORDER OF `:57`'s DISJUNCTS IS LOAD-BEARING, NOT ARBITRARY. `private`
    sits BEFORE `not community.instance.online()`, and `or` short-circuits
    left to right, so a private community with no instance row
    (`Community.instance_id`, app/models.py:575, is a nullable FK) returns
    at the `private` check instead of reaching `community.instance.online()`
    and raising `AttributeError` on `None.online()`. That is a side effect
    of this test's fix, not something it asserts directly -- reordering the
    disjuncts would reopen the crash without failing this test, since this
    test's community always has an instance.

    NO ROUTE IS REGISTERED. `http_mock` is built with
    `assert_all_called=True`, so a route registered here and never called
    would fail this test for the wrong reason.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    recipient = make_instance('recipient.example', software='lemmy')
    recipient.inbox = PEER_INBOX
    s.community.private = True
    db.session.commit()

    report_post(None, s.user.id, s.post.id, 'spam', [recipient.id])

    assert db.session.query(ActivityPubLog).count() == 0


def test_report_reply_delivers_a_flag_naming_the_reply(db_session, http_mock):
    """`report_reply:25` end to end. Written out separately from
    `report_post`'s test rather than parametrised: a parametrised failure
    names the parameter rather than the function, and an arm covered under
    only one parameter is invisible in the failure output.

    The assertion that earns this test its place is `flag['object']` -- it is
    the REPLY's url, not the post's, which is what distinguishes `:30`'s
    lookup from `:45`'s reaching the same handler.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, recipient_id = _reporting_instance(s, http_mock)

    report_reply(None, s.user.id, s.reply.id, 'abuse', [recipient_id])

    flag = _sent_activity(route)
    assert flag['type'] == 'Flag'
    assert flag['object'] == s.reply.public_url()
    assert flag['object'] != s.post.public_url()


def _recording_task_session(monkeypatch):
    """Make `get_task_session` hand back a GENUINE Session that records
    `rollback()` and `close()`.

    One of FIVE copies, not a third: `grep -rn "^def _recording_task_session"
    tests/` finds this one plus `tests/test_shared_tasks_send_reply.py:1637`,
    `tests/test_shared_tasks_send_answer.py:567`,
    `tests/test_shared_tasks_send_post.py:2355` (pre-dating this sub-project) and
    `tests/test_shared_tasks_add_remove.py:244` (also pre-dating this
    sub-project, and carrying an extra `module` parameter since it patches
    either twin). This module's own docstring undercounted at "a third copy" /
    "THREE COPIES" when it was written; the true population of five is
    registered as **D324** in
    `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`.
    Duplicated rather than imported: this campaign keeps its test modules
    independent so a helper can be edited for one function's needs without
    silently changing another's assertions -- that design choice is unchanged
    by the corrected count.

    The session is real and does real work -- only the observation is added, by
    wrapping the two methods rather than replacing the object. A fake session
    would prove the wrapper calls methods on a mock; this proves it calls them
    on the session the function actually used.

    THE PATCH TARGET IS THE FLAGS MODULE, NOT `app.utils`.
    `app/shared/tasks/flags.py:4` imports `get_task_session` into the flags
    namespace, and both wrappers resolve it there (`:27`, `:42`). Patching
    `app.utils.get_task_session` would apply cleanly, observe nothing, and
    leave the assertion trivially true against an empty list -- the silent
    failure mode this docstring exists to prevent.

    Returns a `SimpleNamespace(calls=[])`; the wrapper appends 'rollback' and
    'close' in the order they happened, so `finally` running after `except` is
    observable rather than assumed.
    """
    from app import db as _db
    from sqlalchemy.orm import Session as _Session
    import app.shared.tasks.flags as flags_module

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

    monkeypatch.setattr(flags_module, 'get_task_session', _make)
    return record


def test_a_missing_reply_raises_NoResultFound_and_rolls_back(
        db_session, monkeypatch):
    """`:30`'s `.filter_by(id=reply_id).one()` against an absent id, plus
    `:32-34`'s except arm and `:35-36`'s finally.

    `.one()` raises `NoResultFound` AT THE LOOKUP -- the handler is never
    entered. Contrast the post wrapper's test below, which reaches `:56`.

    Asserting `['rollback', 'close']` rather than merely `raises` is what
    makes this a test of the wrapper: a `raises`-only test cannot tell a
    rollback from its absence. The ORDER also distinguishes `finally` running
    after `except` from a wrapper that closed instead of rolling back.
    """
    s = _seed()
    record = _recording_task_session(monkeypatch)

    with pytest.raises(NoResultFound):
        report_reply(None, s.user.id, s.reply.id + 1000, 'spam', [])

    assert record.calls == ['rollback', 'close']


def test_a_missing_post_raises_AttributeError_and_rolls_back(
        db_session, monkeypatch):
    """`:45`'s `.get(post_id)` against an absent id, plus `:47-49` and
    `:50-51`.

    THE DIFFERENT EXCEPTION TYPE IS THE POINT. `.get()` returns None rather
    than raising, so the failure arrives eleven lines later at `:56`
    (`object.community` on None) as `AttributeError`. Two lookup styles, one
    file. Asserting the type is what records the asymmetry.
    """
    s = _seed()
    record = _recording_task_session(monkeypatch)

    with pytest.raises(AttributeError):
        report_post(None, s.user.id, s.post.id + 1000, 'spam', [])

    assert record.calls == ['rollback', 'close']


def test_report_reply_closes_the_session_on_the_happy_path(
        db_session, http_mock, monkeypatch):
    """`:35-36`'s finally on the SUCCESS path -- `close` with no `rollback`.

    The control for the error tests above: without it, `finally` running is
    only ever observed alongside an exception, and a wrapper that closed only
    in the `except` arm would pass every other test in this file.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, recipient_id = _reporting_instance(s, http_mock)
    record = _recording_task_session(monkeypatch)

    report_reply(None, s.user.id, s.reply.id, 'spam', [recipient_id])

    assert record.calls == ['close']


def test_report_post_closes_the_session_on_the_happy_path(
        db_session, http_mock, monkeypatch):
    """`:50-51`'s finally on the SUCCESS path. Written out separately from the
    reply wrapper's rather than parametrised -- the two `finally` blocks are
    different lines in different functions, and a parametrised pass would
    cover one of them under a name that does not say which.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, recipient_id = _reporting_instance(s, http_mock)
    record = _recording_task_session(monkeypatch)

    report_post(None, s.user.id, s.post.id, 'spam', [recipient_id])

    assert record.calls == ['close']


def test_only_the_named_instances_receive_the_flag(db_session, http_mock):
    """`:73`'s `Instance.id.in_(instance_ids)` filter. An instance that exists
    and has an inbox but is NOT named receives nothing.

    Its route is deliberately not registered -- but posting to it would NOT
    surface as an unmatched request failing the suite: `post_request`'s
    `except Exception as e:` (`app/activitypub/signature.py:143`) catches
    respx's unmatched-request assertion exactly as it would a real transport
    error, and records an `ActivityPubLog` failure row instead of
    propagating. `_delivered_inboxes` reads only the route this test itself
    registered, so it cannot see a spurious send to `unnamed`'s inbox either.
    The row count is the oracle: dropping `:73`'s filter entirely would still
    query only two rows here (a bare `session.query(Instance)` returns every
    row in the table, and `_seed`'s local instance and `_make_deliverable`'s
    peer both have `inbox is None`), so it would deliver to BOTH `named_id`
    and `unnamed` -- one real send and one that dies as an unmatched request
    caught into a second failure row -- for a count of 2 where the correct,
    filtered path produces 1.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, named_id = _reporting_instance(s, http_mock)
    unnamed = make_instance('unnamed.example', software='lemmy')
    unnamed.inbox = OTHER_INBOX
    db.session.commit()

    report_post(None, s.user.id, s.post.id, 'spam', [named_id])

    assert _delivered_inboxes(route) == {PEER_INBOX}
    assert db.session.query(ActivityPubLog).count() == 1


def test_an_instance_without_an_inbox_is_skipped(db_session, http_mock):
    """`:75`'s `instance.inbox is not None` guard, alone.

    NO ROUTE IS REGISTERED, and the ActivityPubLog count is what proves the
    skip. Per `_reporting_instance`'s docstring, `post_request` writes its row
    unconditionally at app/activitypub/signature.py:105 -- so a count of 0
    means `:76` was never reached, not merely that delivery failed.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    inboxless = make_instance('inboxless.example', software='lemmy')
    inboxless.inbox = None
    db.session.commit()

    report_post(None, s.user.id, s.post.id, 'spam', [inboxless.id])

    assert db.session.query(ActivityPubLog).count() == 0


def test_the_loop_continues_past_an_instance_without_an_inbox(
        db_session, http_mock):
    """`:74`'s loop CONTINUES after `:75` skips one row.

    This is the test the `:75` guard's coverage needs and the one above cannot
    give: with a single inboxless instance, a loop that ABORTED on the skip and
    a loop that CONTINUED past it are indistinguishable. Two rows, one skipped
    and one delivered, separate them.

    CREATION ORDER IS LOAD-BEARING, AND THAT IS A WEAKNESS THIS TEST CANNOT
    REMOVE, ONLY DOCUMENT. The inboxless instance is created FIRST, on
    purpose, so it holds the lower id: `db_session`'s teardown resets the id
    sequence per test, and `:73`'s `Instance.id.in_(instance_ids)` carries no
    `ORDER BY`. On a two-row table the planner has no reason to do anything
    but a physical (ascending-id) scan, so the inboxless row is expected
    FIRST. If the good instance were created first instead (lower id), an
    aborting loop -- `return` in place of `:74`'s implicit `continue` -- would
    deliver to it before ever reaching the inboxless row and returning, and
    both assertions below would pass identically to a correct, continuing
    loop. That is the bug this ordering avoids: creation order here is chosen
    so a `return`-on-skip mutant hits the skip on its FIRST iteration and
    delivers nothing, which is what makes the assertions below fail against
    it.

    THIS IS AN ASSUMPTION ABOUT THE QUERY PLAN, NOT A GUARANTEE, AND NO
    ORDERED ASSERTION IS ADDED TO PAPER OVER THAT. `:73`'s rows come from the
    TASK session, not from any object this test holds, so nothing here
    controls the order Postgres actually returns them in -- creation order
    only makes ascending-id delivery likely, not certain. If that assumption
    ever breaks (a planner choosing a different scan, a changed id sequence),
    this test degrades to LAX: it would pass under an aborting loop too,
    exactly like the reasoning above but with the two rows swapped. It does
    NOT degrade to flaky -- there is no direction in which a correct,
    continuing loop starts failing this test, only a direction in which an
    aborting loop stops being caught. A rigorous proof would require
    controlling the order `:73` returns rows in, rather than arranging ids to
    influence it; this file does not do that, because the loop iterates rows
    fetched by the task session, not rows the test can order itself.

    The delivered-inbox assertion is still set-based, per the module
    docstring's ordering discipline -- that discipline is about not asserting
    WHICH inbox comes first among several delivered ones, which is a
    different question from the one this docstring is about (whether the
    loop reaches the second row at all).
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    inboxless = make_instance('inboxless.example', software='lemmy')
    inboxless.inbox = None
    db.session.commit()
    route, good_id = _reporting_instance(s, http_mock)

    report_post(None, s.user.id, s.post.id, 'spam', [inboxless.id, good_id])

    assert _delivered_inboxes(route) == {PEER_INBOX}
    assert len(route.calls) == 1


def test_two_instances_with_inboxes_both_receive_the_flag(
        db_session, http_mock):
    """`:74`'s loop delivering more than once -- the arc back to the top.

    Set-based, for the reason the module docstring gives.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    first, first_id = _reporting_instance(s, http_mock)
    second, second_id = _reporting_instance(
        s, http_mock, inbox=OTHER_INBOX, domain='second.example')

    report_post(None, s.user.id, s.post.id, 'spam', [first_id, second_id])

    assert _delivered_inboxes(first, second) == {PEER_INBOX, OTHER_INBOX}
    assert len(first.calls) == 1
    assert len(second.calls) == 1


def test_the_flag_carries_a_top_level_context_and_the_community_audience(
        db_session, http_mock):
    """`:60-71`'s envelope, asserted on the bytes that actually left.

    `@context` is at `:67`, INSIDE the flag dict -- and here the flag IS the
    top-level posted object, because this module has no Announce wrapper.

    NO ASSERTION ABOUT `@context` APPEARS HERE, AND NONE CAN REPLACE IT.
    `app/activitypub/signature.py:100-101` is
    `if '@context' not in body: body['@context'] = default_context()`,
    which runs on `flag` itself before it is signed or sent -- and
    `_sent_activity` reads the bytes that actually left, i.e. AFTER that
    reinjection. Since `flag` has no Announce wrapper, `:100-101`'s
    "top-level only" reinjection reaches exactly the level `:67` writes to,
    with exactly the same value (`default_context()` in both places). So
    `@context` is present, and identical, on the wire whether or not `:67`
    ever runs -- there is no observable difference on the wire for any
    assertion, presence or value, to discriminate on.

    THIS IS THE MIRROR OF SUB-PROJECTS 20-23's NESTED-ABSENCE ASSERTIONS, NOT
    AN EXCEPTION TO THEIR REASONING. Those modules wrap the object in an
    Announce, so `:100-101`'s reinjection lands on the Announce's top level
    and never reaches the nested inner object -- which is exactly what makes
    "no nested `@context`" a meaningful, discriminating assertion there. Here
    there is no wrapper, so the same reinjection mechanism lands on `flag`
    itself, and that is exactly what makes any `@context` assertion on
    `flag` non-discriminating. One mechanism, opposite consequences,
    decided entirely by whether the activity is Announce-wrapped.

    The three assertions below have no such fallback anywhere in the send
    path: nothing between `:60-71` and the wire sets or reinjects
    `audience`, `to`, or `id`, so they remain real coverage of `:60-61` and
    `:68-69` even with the `@context` assertion gone.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, recipient_id = _reporting_instance(s, http_mock)

    report_post(None, s.user.id, s.post.id, 'spam', [recipient_id])

    flag = _sent_activity(route)
    assert flag['audience'] == s.community.public_url()
    assert flag['to'] == [s.community.public_url()]
    assert flag['id'].startswith('https://test.piefed.local/activities/flag/')
