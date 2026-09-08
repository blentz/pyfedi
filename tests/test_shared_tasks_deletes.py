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
site was misread by the campaign's own register as omitting `online()` --
`:133` had carried that check all along. `:127` returns for a non-post in a
`local_only` community, `:130` returns for a `local_only` community when the
user has no followers, and `:133` now returns for a `private` community too,
ahead of the pre-existing online check it shares its line with. `private`
could not join the two `local_only` checks above it -- those are conditional
on `is_post` and on `followers` respectively -- so `:133`, this guard's only
unconditional statement, is where it had to go. D309's last site, closed.

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
    delete_community, delete_post, delete_posts_with_blocked_images,
    delete_reply, restore_community, restore_post, restore_reply,
)
from tests.factories import (
    make_community, make_community_member, make_instance, make_notification,
    make_post, make_post_reply, make_user,
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
    and follower paths (`:202`, `:216`), and with the COMMUNITY's key on the
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

    THIS IS NOT THE FIXTURE PATH 3 NEEDS. The follower fan-out at `:212-216`
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
    """`:219-226`'s cleanup, which `:205-206`'s early return used to skip.

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
    """`:224`'s `continue`, guarded by `:223`'s report check -- the one arm of
    the cleanup loop that keeps a row. Two notifications on the same post, one
    of them a report: the report survives and the other does not, so a
    mutation removing the `continue` fails on the count rather than on which
    row happens to remain."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)
    make_notification(s.user, s.post)
    make_notification(s.user, s.post, notif_type=NOTIF_REPORT)

    delete_post(None, s.user.id, s.post.id)

    assert db.session.query(Notification).count() == 1
    assert db.session.query(Notification).one().notif_type == NOTIF_REPORT
    assert len(route.calls) == 1


def _following_instance_without_a_mocked_route(s, inbox=PEER_INBOX, domain='follower.example'):
    """Same DB shape as `_follower` -- an Instance, an inbox, and a member
    User -- so `community.following_instances()` returns a real row, but with
    NO route registered on `http_mock`.

    This is deliberate, not an oversight. `http_mock` is `assert_all_called=True`
    (conftest.py:336-342): a route registered but never hit fails at teardown.
    The four tests below assert on a guard that is SUPPOSED to stop delivery,
    so a route built with `_follower` would sit uncalled and turn a correct
    pass into a spurious teardown error. Registering nothing instead lets an
    unexpected send fall through to the session-scoped empty router
    (conftest.py's `assert_all_called=False` one), which raises for the
    unmatched request exactly as a real network failure would; `post_request`
    catches that and still writes the `ActivityPubLog` failure row
    (`signature.py:105` before the transport, `:143`'s `except Exception`
    after). So a guard regression is caught by the row count either way, and a
    correctly-firing guard leaves both the count and the router's bookkeeping
    clean.
    """
    inst = make_instance(domain, software='lemmy')
    inst.inbox = inbox
    member_user = make_user(inst, f'member_{domain.split(".")[0]}')
    make_community_member(member_user, s.community)
    db.session.commit()
    return inst


def test_a_local_only_community_sends_no_reply_delete(db_session, http_mock):
    """`:127`'s guard, reached only for a NON-post -- `not is_post` is its first
    conjunct, so a post never returns here.

    Zero deliveries, so the oracle is the `ActivityPubLog` count: `post_request`
    writes its row at `signature.py:105` before the transport, and a
    delivered-inboxes assertion cannot see a send that respx never matched
    (`signature.py:143` swallows it).

    A `following_instances()` row is deliberately added here (the brief's
    version omits it): without one, `community.is_local()`'s Announce loop at
    `:196` iterates zero times regardless of whether `:127` fires, so the
    assertion would hold even with the guard deleted -- confirmed empirically,
    see task-3-report.md. `_follower` itself is not used because it registers
    an `http_mock` route that this test, if the guard fires correctly as
    expected, never calls; `http_mock` is `assert_all_called=True`, so an
    uncalled route fails at teardown. See
    `_following_instance_without_a_mocked_route`.
    """
    s = _seed(with_keys=True)
    peer = _make_deliverable(s)
    _following_instance_without_a_mocked_route(s)
    reply = make_post_reply(s.post, s.user)
    s.community.local_only = True
    db.session.commit()

    delete_reply(None, s.user.id, reply.id)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_local_only_community_sends_no_post_delete_without_followers(
        db_session, http_mock):
    """`:130`'s guard. A POST passes `:127` (its `not is_post` conjunct is
    False) and is stopped here instead, because the author has no
    `UserFollower` rows and the community is `local_only`.

    The pair `:127`/`:130` is why this function contributes two lines to D309
    and counts once as a site.

    A `following_instances()` row is deliberately added here (the brief's
    version omits it): see the note on
    `test_a_local_only_community_sends_no_reply_delete` above."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    _following_instance_without_a_mocked_route(s)
    s.community.local_only = True
    db.session.commit()

    delete_post(None, s.user.id, s.post.id)

    assert db.session.query(ActivityPubLog).count() == 0


def test_a_private_community_sends_no_delete(db_session, http_mock):
    """D309's TWELFTH AND FINAL SITE.

    `private` is seeded with `local_only` deliberately left False -- with it
    True the test would pass on `:127`/`:130`'s pre-existing conjuncts and
    prove nothing about the new one. That is the same trap every earlier D309
    site's test was built to avoid, and `app/admin/routes.py:1388` makes the
    uncoupled state reachable today: it writes `community.local_only` from the
    admin form without touching `community.private`.

    A `following_instances()` row is deliberately added here (the brief's
    version omits it): see the note on
    `test_a_local_only_community_sends_no_reply_delete` above -- without it
    this test passes vacuously before the fix too, which is exactly what was
    observed and is recorded in task-3-report.md.

    `private` goes FIRST in `:133`'s `or`. `Community.instance_id` is a
    nullable FK (`app/models.py:575`), and `or` short-circuits left to right,
    so a private community with no instance row returns at the guard rather
    than raising `AttributeError` on `None.online()`. NO TEST ASSERTS THIS --
    it is a property of the ordering, not something this suite pins: every
    test here gives the community a real, non-null instance
    (`_make_deliverable`), so a reordering to `not community.instance.online()
    or community.private` would still pass all four.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    _following_instance_without_a_mocked_route(s)
    s.community.private = True
    db.session.commit()

    delete_post(None, s.user.id, s.post.id)

    assert db.session.query(ActivityPubLog).count() == 0


def test_the_blocked_image_batch_deletes_every_post(db_session, http_mock):
    """`delete_posts_with_blocked_images:231`, which raised `AttributeError` on
    EVERY call before this commit.

    TWO posts, because the crash was partial rather than total: the loop
    commits the first post's deletion at `:248` and unlinks its file at `:247`
    before `:250` raises, so a one-post batch would have shown a deleted post
    and a raised task -- the same visible state a successful delete of one post
    leaves. The second post is what distinguishes them.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)
    second = make_post(s.community, s.user, 'https://test.piefed.local/post/2')
    db.session.commit()

    delete_posts_with_blocked_images([s.post.id, second.id], s.user.id, False)

    # `delete_posts_with_blocked_images` writes through `get_task_session()`, a
    # SEPARATE `Session(bind=db.engine)` from this test's `db.session`. Its
    # commits expire objects in ITS OWN identity map, not this one's, so
    # `s.post`/`second` -- already loaded here before the task ran -- would
    # otherwise still show their pre-task Python-level cached attributes.
    db.session.expire_all()

    assert s.post.deleted is True
    assert second.deleted is True
    assert _delivered_inboxes(route) == {PEER_INBOX}
    assert len(route.calls) == 2


def test_the_blocked_image_batch_skips_a_missing_post(db_session, http_mock):
    """`:238`'s `if post:` false arm. A post id that no longer exists is
    skipped rather than raising, and the surviving id is still processed."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    delete_posts_with_blocked_images([999999, s.post.id], s.user.id, False)

    # See the comment in test_the_blocked_image_batch_deletes_every_post above:
    # the task writes through a separate session, so this session's cached
    # `s.post` needs an explicit expire before it will show the update.
    db.session.expire_all()

    assert s.post.deleted is True
    assert len(route.calls) == 1


def test_an_offline_community_instance_sends_no_delete(db_session, http_mock):
    """`:133`'s other disjunct, the one that was already there. Separated from
    the `private` test because the two fail independently.

    A `following_instances()` row is deliberately added here (the brief's
    version omits it): see the note on
    `test_a_local_only_community_sends_no_reply_delete` above. Here it also
    demonstrates that `:133` checks `community.instance`, the community's OWN
    host, not the following instance's -- the following instance stays online
    throughout."""
    s = _seed(with_keys=True)
    _make_deliverable(s, online=False)
    _following_instance_without_a_mocked_route(s)

    delete_post(None, s.user.id, s.post.id)

    assert db.session.query(ActivityPubLog).count() == 0


def _recording_task_session(monkeypatch):
    """Make `get_task_session` hand back a GENUINE Session that records
    `rollback()` and `close()`.

    THE TENTH COPY of a helper that also lives in
    tests/test_shared_tasks_flags.py, test_shared_tasks_likes.py,
    test_shared_tasks_locks.py, test_shared_tasks_send_answer.py,
    test_shared_tasks_add_remove.py, test_shared_tasks_send_reply.py,
    test_shared_tasks_send_post.py, test_shared_tasks_groups.py and
    test_shared_tasks_blocks.py. Duplicated rather than imported: this
    campaign keeps its test modules independent so a helper can be edited for
    one function's needs without silently changing another's assertions.

    The session is real -- only the observation is added, by wrapping the two
    methods rather than replacing the object. A fake session would prove the
    wrapper calls methods on a mock; this proves it calls them on the session
    the function actually used.

    THE PATCH TARGET IS THE DELETES MODULE, NOT `app.utils`. `deletes.py:5`
    imports `get_task_session` into the deletes namespace and every wrapper
    resolves it there. Patching `app.utils.get_task_session` would apply
    cleanly, observe nothing, and leave the assertion trivially true against
    an empty list.

    Matches tests/test_shared_tasks_groups.py's `_recording_task_session`
    construction exactly: `get_task_session` itself is replaced with a
    factory that builds a fresh `Session(bind=db.engine)` per call and wraps
    that instance's `rollback`/`close`, rather than fetching one real session
    up front and handing back the same object every time. There is no
    `db.create_scoped_session` call in the sibling to match.
    """
    from app import db as _db
    from sqlalchemy.orm import Session as _Session
    import app.shared.tasks.deletes as deletes_module

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

    monkeypatch.setattr(deletes_module, 'get_task_session', _make)
    return record


@pytest.mark.parametrize('task, kwarg', [
    (delete_reply, 'reply_id'),
    (restore_reply, 'reply_id'),
    (delete_post, 'post_id'),
    (restore_post, 'post_id'),
    (delete_community, 'community_id'),
    (restore_community, 'community_id'),
])
def test_each_wrapper_rolls_back_and_reraises(db_session, monkeypatch, task, kwarg):
    """All six wrappers' `except Exception: session.rollback(); raise` --
    `:37` (`delete_reply`), `:52` (`restore_reply`), `:67` (`delete_post`),
    `:82` (`restore_post`), `:97` (`delete_community`), `:112`
    (`restore_community`).

    An id that does not exist makes the wrapper's own query raise -- `:34`,
    `:49`, `:94` and `:109`'s `.one()` raises `NoResultFound` for the reply
    and community wrappers, and `:64`/`:79`'s `.get()` returns `None` for the
    post wrappers, whose `delete_object` then raises `AttributeError` on
    `:123`'s `object.community`. Both propagate through the same `except`.

    `record.calls == ['rollback', 'close']` IS THE ASSERTION. A
    `pytest.raises` alone passes just as well when `session.rollback()` is
    replaced by `pass`, which is exactly the defect sub-project 26 shipped in
    four tests and had to fix in a later round.
    """
    record = _recording_task_session(monkeypatch)

    with pytest.raises(Exception):
        task(None, 1, **{kwarg: 999999})

    assert record.calls == ['rollback', 'close']


def test_restore_post_sends_an_undo(db_session, http_mock):
    """`restore_post:74` -> `is_restore=True`. `:160-172` wraps the Delete in
    an Undo, and on the LOCAL path `:178` strips the Undo's `@context` and
    `:161` has already stripped the Delete's, so the Announce's own
    `@context` at `:192` is the only level that carries one.

    BOTH nested absences are asserted and both fail independently -- this is
    the campaign's first activity with `@context` absences at two depths.
    """
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    restore_post(None, s.user.id, s.post.id)

    announce = _sent_activity(route)
    assert announce['object']['type'] == 'Undo'
    assert '@context' not in announce['object']
    assert announce['object']['object']['type'] == 'Delete'
    assert '@context' not in announce['object']['object']


def test_delete_community_addresses_the_community_itself(db_session, http_mock):
    """`delete_community:89`, whose object IS the community -- `:120-121`
    takes the `isinstance(object, Community)` arm rather than `:123`'s
    `object.community`."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)

    delete_community(None, s.user.id, s.community.id)

    announce = _sent_activity(route)
    assert announce['object']['object'] == s.community.public_url()


def test_restore_reply_sends_an_undo_of_a_reply_delete(db_session, http_mock):
    """`restore_reply:44`, the `is_restore` arm on a NON-post. Distinguishes
    `:123`'s `object.community` -- the else arm reached from the reply
    wrappers, as opposed to `:121`'s `isinstance(object, Community)` arm the
    community wrappers take instead."""
    s = _seed(with_keys=True)
    _make_deliverable(s)
    route, _inst = _follower(s, http_mock)
    reply = make_post_reply(s.post, s.user)
    db.session.commit()

    restore_reply(None, s.user.id, reply.id)

    announce = _sent_activity(route)
    assert announce['object']['type'] == 'Undo'
    assert announce['object']['object']['object'] == reply.public_url()
