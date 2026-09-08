"""`delete_object` and its eight wrappers -- the AP Delete and Undo senders.

`app/shared/tasks/deletes.py`, 318 lines. Six `@celery.task` wrappers
(`delete_reply:29`, `restore_reply:44`, `delete_post:59`, `restore_post:74`,
`delete_community:89`, `restore_community:104`) delegating to
`delete_object:118`, plus `delete_posts_with_blocked_images:231` and a PM pair
(`delete_pm:260`, `restore_pm:276`) over `delete_message:291`.

FOUR DELIVERY PATHS AND THREE SIGNING ACTORS, which is one more path and one
more signer than any module this campaign has closed:
  `:196-199` local community -- an Announce per `following_instances()` row
      passing the FOUR-conjunct guard at `:197`, signed with the COMMUNITY key.
  `:201-203` remote community -- one direct send to `ap_inbox_url`, signed with
      the USER key.
  `:212-216` the author's own followers -- a raw `Instance` join, signed with
      the USER key, skipping any domain already in `domains_sent_to`.
  `:318` private messages -- one send to `recipient.ap_inbox_url`, signed with
      the SENDER key, after `:293`'s local-recipient early return.

THE GUARD AT `:127-134` IS SPLIT ACROSS THREE STATEMENTS, which is why this
site was misread by the campaign's own register. `:127` returns for a non-post
in a `local_only` community, `:130` returns for a `local_only` community when
the user has no followers, and `:133` returns for an offline instance. Only
`Community.private` is missing, so this is D309's NARROW shape and not the wide
one -- a distinction that survives only because the guard is read as a block.

`:197` CARRIES FOUR CONJUNCTS AND COVERAGE.PY SEES ONE ARC PAIR:
`instance.inbox`, `instance.online()`, `not user.has_blocked_instance(...)` and
`not instance_banned(...)`. Branch coverage reads 100% with three of them
untested. Every one is pinned by its own test and its own mutation.
"""

import json
import socket
from types import SimpleNamespace

import pytest

from app import db
from app.constants import NOTIF_REPORT
from app.models import ActivityPubLog, Notification
from app.shared.tasks.deletes import (
    delete_community, delete_post, delete_reply, restore_community,
    restore_post, restore_reply,
)
from tests.factories import (
    make_community, make_community_member, make_instance, make_notification,
    make_post, make_user,
)

PEER_INBOX = 'https://peer.example/inbox'
OTHER_INBOX = 'https://other.example/inbox'

_REAL_GETADDRINFO = socket.getaddrinfo
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
    On a machine whose resolver hijacks NXDOMAIN into a wildcard A record,
    these tests start failing at app/utils.py:5530's `is_global` check instead.
    """
    monkeypatch.setattr(socket, 'getaddrinfo', _getaddrinfo_without_the_network)


def _seed(local_community=True, with_keys=False):
    """instance, user, community, post, reply -- committed.

    `user` both authors the content and performs the deletion, which is the
    author-delete shape; moderator deletes differ only by passing `reason`.
    `delete_object:119` loads the user and signs with their key on the remote
    and follower paths (`:202`, `:218`), and with the COMMUNITY's key on the
    local Announce path (`:198`) -- so `with_keys=True` supplies both.
    """
    instance = make_instance('test.piefed.local', software='piefed')
    user = make_user(instance, 'author', local=True, with_keys=with_keys)
    community = make_community('c1')
    if with_keys:
        community.private_key = user.private_key
        community.public_key = user.public_key
    if not local_community:
        community.ap_id = 'c1@peer.example'
        community.ap_profile_id = 'https://peer.example/c/c1'
        community.ap_public_url = 'https://peer.example/c/c1'
        community.ap_inbox_url = PEER_INBOX
        community.ap_domain = 'peer.example'
    db.session.commit()
    post = make_post(community, user, 'https://test.piefed.local/post/1')
    db.session.commit()
    return SimpleNamespace(instance=instance, user=user, community=community,
                           post=post)


def _make_deliverable(s, online=True):
    """Move the community off instance id 1 onto a real peer Instance.

    `:133` dereferences `community.instance` and returns when it is offline, so
    a community left on instance 1 -- which `make_instance` gives no inbox --
    still reaches the delivery paths, but `online=False` here is what exercises
    `:133`'s true arm. `Instance.online()` is exactly
    `not (self.dormant or self.gone_forever)` (app/models.py:118-119).
    Returns the peer.
    """
    peer = make_instance('follower-home.example', software='lemmy')
    if not online:
        peer.dormant = True
        peer.gone_forever = True
    s.community.instance_id = peer.id
    db.session.commit()
    return peer


def _sent_activity(route, index=-1):
    """The JSON body of the request `route` captured, decoded from the bytes
    that were actually sent. Defaults to the most recent call.
    """
    return json.loads(route.calls[index].request.content)


def _delivered_inboxes(*routes):
    """The SET of inboxes that received a request. Never a list, never ordered
    -- `following_instances()` ends in an unordered `.distinct().all()`."""
    return {str(r.calls[i].request.url) for r in routes for i in range(len(r.calls))}


def _follower(s, http_mock, inbox=PEER_INBOX, domain='follower.example'):
    """A remote instance `following_instances()` will actually return.

    THREE parts, and the third is the one that is easy to miss: an Instance
    row, an inbox, and a USER ON THAT INSTANCE WHO IS A MEMBER OF THE
    COMMUNITY. `Community.following_instances()` (app/models.py:842-851) joins
    `CommunityMember`; without the membership the query returns nothing,
    `:196`'s loop never runs, and every delivery assertion passes against zero
    deliveries.

    THIS IS NOT THE FIXTURE PATH 3 NEEDS. The follower fan-out at `:214-218`
    joins `UserFollower`, not `CommunityMember`, so a recipient built here is
    invisible to it and vice versa. Task 9 builds that one separately.

    Returns `(route, instance)`.
    """
    inst = make_instance(domain, software='lemmy')
    inst.inbox = inbox
    member_user = make_user(inst, f'member_{domain.split(".")[0]}')
    make_community_member(member_user, s.community)
    db.session.commit()
    return http_mock.post(inbox).respond(200, json={}), inst


def test_delete_post_announces_to_the_communitys_followers(db_session, http_mock):
    """`delete_post:59` on a LOCAL community: the guard at `:127-134`, the
    Delete envelope at `:147-156`, and the Announce loop at `:196-199`.

    The nested `@context` absence discriminates -- `:181` deletes it from the
    Delete before `:191` nests it, and `signature.py:100-101`'s reinjection
    reaches the top level only.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    delete_post(None, s.user.id, s.post.id)

    announce = _sent_activity(route)
    assert announce['type'] == 'Announce'
    assert announce['object']['type'] == 'Delete'
    assert announce['object']['object'] == s.post.public_url()
    assert '@context' not in announce['object']


def test_a_remote_community_delete_is_sent_direct(db_session, http_mock):
    """`:201-203`. `community.is_local()` is False, so no Announce is built and
    the Delete goes straight to `community.ap_inbox_url`.

    NO `@context` ASSERTION BELONGS ON THIS ACTIVITY. `:181`'s
    `del delete['@context']` sits inside the `is_local()` branch, so the remote
    path ships whatever `:152` set -- and the Delete is the top-level object
    here, exactly where `signature.py:100-101` reinjects. Both a presence and
    an absence assertion would be non-discriminating.
    """
    s = _seed(local_community=False, with_keys=True)
    _make_deliverable(s)
    route = http_mock.post(PEER_INBOX).respond(200, json={})

    delete_post(None, s.user.id, s.post.id)

    delete = _sent_activity(route)
    assert delete['type'] == 'Delete'
    assert delete['actor'] == s.user.public_url()
    assert delete['audience'] == s.community.public_url()


def test_a_moderator_delete_still_clears_notifications(db_session, http_mock):
    """`:221-228`'s cleanup, which `:205-206`'s early return used to skip.

    A moderator delete passes `reason`; an author delete does not. Before this
    commit the `reason` return at `:205` fired first, so a moderated removal
    left its notifications pointing at content that no longer exists -- the
    asymmetry running the wrong way, since moderated removals are exactly where
    a stale notification matters.

    THE ROW COUNT IS OVER `Notification`, NOT `ActivityPubLog`. The delivery
    assertion here would pass either way: `:205` returns after both the
    Announce loop and the direct send have already run.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)
    make_notification(s.user, s.post)

    delete_post(None, s.user.id, s.post.id, reason='spam')

    assert db.session.query(Notification).count() == 0
    assert len(route.calls) == 1


def test_an_author_delete_clears_notifications_too(db_session, http_mock):
    """The control. Without it, the test above cannot distinguish "the reason
    return no longer blocks the cleanup" from "the cleanup runs
    unconditionally and always did"."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)
    make_notification(s.user, s.post)

    delete_post(None, s.user.id, s.post.id)

    assert db.session.query(Notification).count() == 0
    assert len(route.calls) == 1


def test_a_report_notification_survives_the_delete(db_session, http_mock):
    """`:225-226`'s `continue`, the one arm of the cleanup loop that keeps a
    row. Two notifications on the same post, one of them a report: the report
    survives and the other does not, so a mutation removing the `continue`
    fails on the count rather than on which row happens to remain."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)
    make_notification(s.user, s.post)
    make_notification(s.user, s.post, notif_type=NOTIF_REPORT)

    delete_post(None, s.user.id, s.post.id)

    assert db.session.query(Notification).count() == 1
    assert db.session.query(Notification).one().notif_type == NOTIF_REPORT
    assert len(route.calls) == 1
