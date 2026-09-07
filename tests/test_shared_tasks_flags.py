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
    """instance, reporter, community, post, reply -- committed.

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
    actually left, at app/activitypub/signature.py:494. A recorder holding the
    dict would be read back after any in-place mutation.
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
