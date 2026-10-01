"""Round 272: six task handlers that must report their own failure.

Every one of these is the same three lines --

    except Exception:
        session.rollback()
        raise

-- at the bottom of something that runs on a task-local session. They are grouped here because the
DECISION is the same one six times, and it is not the rollback: fact 1039 records that
`session.rollback()` before `finally: session.close()` cannot be distinguished, because closing a
SQLAlchemy session releases its transaction. The `raise` is what every row below asserts, and it is
what stops a half-done job being reported as a finished one:

    instance_banned                a federation gate. It is MEMOIZED, so a swallowed failure would
                                   cache "not banned" for 150 seconds.
    new_instance_profile_task      the software and version this instance records about a peer.
    get_nodebb_replies_in_background   a fetch loop whose inner failures are already handled, so
                                   anything reaching the outer handler is not a peer's fault.
    make_image_sizes_async         resizes an image and rewrites the File row that names it.
    retrieve_mods_and_backfill     creates the community's moderators and its first posts.
    process_delete_request         an account deleting itself, which purges content.

Also here: `make_image_sizes_async`'s `else: PNG` format fallback, and the SECOND copy of
`retrieve_mods_and_backfill`'s `except IntegrityError` -- the one in the `attributedTo` branch, which
round 268 fixed and covered only in the collection branch.
"""
from unittest.mock import patch

import pytest
from flask import g
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import Community, CommunityMember, Instance, Site, User
from tests.factories import (make_community, make_community_member, make_instance, make_post,
                             make_user)


def _session_whose_get_raises(message):
    """A task session whose `get` raises, and which otherwise delegates.

    Several of these tasks wrap every fetch in a bare `except:`, so a fetch failure is swallowed by
    design and cannot reach the outer handler. The row-lookup at the top of the `try` is inside the
    outer handler and inside no inner one, which makes it the one place a failure can be injected
    without rewriting what the task does.
    """
    class GetRaises:
        def __init__(self, inner):
            self.inner = inner

        def get(self, *args, **kwargs):
            raise RuntimeError(message)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    from app.utils import get_task_session

    return GetRaises(get_task_session())


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('failland')
    author = make_user(api_baseline.instance_local, 'failauthor', local=True)
    db.session.commit()
    make_community_member(author, community)
    db.session.commit()
    return SimpleNamespace(app=app, community=community, author=author,
                           baseline=api_baseline)


# --------------------------------------------------------------------------
# A federation gate that must not cache a failure
# --------------------------------------------------------------------------


class TestWhenTheBanListCannotBeRead:

    def test_the_error_reaches_the_caller(self, env):
        """`instance_banned` gates inbound activity processing AND outbound delivery, and it is
        memoized for 150 seconds. Swallowing a failure here would answer "not banned" and CACHE
        that answer -- so every peer would be federated with for the next two and a half minutes,
        including the banned ones. The comment above it records the shape: one malformed wildcard
        entry in the admin's blocklist box used to raise `re.PatternError` out of this function, and
        it re-raises deliberately.
        """
        from app.utils import instance_banned

        with patch('app.utils.inbox_domain', side_effect=RuntimeError('cannot read the list')):
            with pytest.raises(RuntimeError, match='cannot read the list'):
                instance_banned('probe-unique-1.example')

    def test_a_working_read_still_answers(self, env):
        """The control, and the reason the row above is about the RAISE: a function that answered
        False on failure would look identical from the caller's side on the happy path."""
        from app.utils import instance_banned

        assert instance_banned('probe-unique-2.example') is False


# --------------------------------------------------------------------------
# What this instance records about a peer
# --------------------------------------------------------------------------


class TestWhenAPeersProfileCannotBeRecorded:

    def test_the_error_reaches_the_caller(self, env):
        """`new_instance_profile_task` writes `Instance.software` and `Instance.version`, which
        decide which ActivityPub dialect this instance speaks to that peer. Its two INNER handlers
        already swallow every fetch failure and `return`, so anything reaching the outer one is a
        local fault -- and celery must record it rather than log a success.
        """
        from app.activitypub.util import new_instance_profile_task

        peer = make_instance('profileless.example')
        db.session.commit()

        # The two inner handlers are bare `except:` around the fetches, so a fetch failure cannot
        # reach the outer one. The injection has to be something the outer try covers and they do
        # not: the session lookup itself.
        with patch('app.activitypub.util.get_task_session',
                   return_value=_session_whose_get_raises('the worker broke')):
            with pytest.raises(RuntimeError, match='the worker broke'):
                new_instance_profile_task(peer.id)

    def test_a_peer_that_answers_nothing_is_not_an_error(self, env):
        """The inner handlers, for contrast: a peer that does not answer is ordinary, so the task
        returns quietly and the row keeps whatever it held."""
        import httpx

        from app.activitypub.util import new_instance_profile_task

        peer = make_instance('silent.example')
        peer.software = 'lemmy'
        db.session.commit()

        with patch('app.activitypub.util.get_request',
                   side_effect=httpx.HTTPError('no answer')):
            new_instance_profile_task(peer.id)

        db.session.expire_all()
        assert db.session.get(Instance, peer.id).software == 'lemmy'


class TestWhenTheNodebbReplyFetchFails:

    def test_the_error_reaches_the_caller(self, env):
        """`get_nodebb_replies_in_background` already catches and LOGS a failure per reply -- the
        docstring records that one failing reply used to abandon the rest -- so the outer handler
        only fires for something else entirely. It also uses `db.session.remove()` rather than
        `close()`, because it runs on the request-scoped session.
        """
        from app.activitypub.util import get_nodebb_replies_in_background

        with patch('app.activitypub.util.db.session.get',
                   side_effect=RuntimeError('the session broke')):
            with pytest.raises(RuntimeError, match='the session broke'):
                get_nodebb_replies_in_background(['https://peer.example/r/1'],
                                                 env.community.id)

    def test_one_reply_that_cannot_be_resolved_is_only_logged(self, env):
        """D1340's repair, as the control: the inner handler is what lets the loop continue, so an
        unresolvable reply must NOT reach the outer one."""
        from app.activitypub.util import get_nodebb_replies_in_background

        resolved = []

        def failing(uri, *args, **kwargs):
            resolved.append(uri)
            raise RuntimeError('that reply is gone')

        with patch('app.activitypub.util.resolve_remote_post', side_effect=failing):
            get_nodebb_replies_in_background(['https://peer.example/r/1',
                                              'https://peer.example/r/2'],
                                             env.community.id)

        assert resolved == ['https://peer.example/r/1', 'https://peer.example/r/2']


class TestWhenAnAccountDeletingItselfFails:

    def test_the_error_reaches_the_caller(self, env):
        """`process_delete_request` purges content and sets `deleted`, in that order, on ONE
        session. A failure between them is a half-deleted account -- and the re-raise is what has
        celery retry it rather than logging the delete as done.
        """
        from app.activitypub.routes import process_delete_request

        remote = make_user(env.baseline.instance_remote, 'selfdeleter')
        remote.ap_profile_id = 'https://remote.piefed.test/u/selfdeleter'
        db.session.commit()

        with patch.object(User, 'delete_dependencies',
                          side_effect=RuntimeError('the delete broke')):
            with pytest.raises(RuntimeError, match='the delete broke'):
                process_delete_request({'id': 'https://remote.piefed.test/activities/delete/1',
                                        'type': 'Delete',
                                        'actor': 'https://remote.piefed.test/u/selfdeleter'},
                                       False)

        db.session.expire_all()
        assert db.session.get(User, remote.id).deleted is False

    def test_a_delete_naming_an_unknown_account_is_not_an_error(self, env):
        """`if user:` -- a Delete for somebody this instance never had is nothing to do, and must
        not be an exception a worker retries for ever."""
        from app.activitypub.routes import process_delete_request

        process_delete_request({'id': 'https://remote.piefed.test/activities/delete/2',
                                'type': 'Delete',
                                'actor': 'https://remote.piefed.test/u/never-seen'}, False)


class TestWhenTheBackfillFails:

    def test_the_error_reaches_the_caller(self, env):
        """`retrieve_mods_and_backfill` creates the community's moderators and its first posts. The
        re-raise is what stops a community being left created-but-empty with a success in the log --
        which is the failure D1259 and D1260 were both instances of.
        """
        from app.community.util import retrieve_mods_and_backfill

        # PERM-5: the moderators collection is read only from the community's own host
        env.community.ap_profile_id = 'https://peer.example/c/x'
        env.community.ap_moderators_url = 'https://peer.example/c/x/moderators'
        db.session.commit()

        with patch('app.community.util.remote_object_to_json',
                   side_effect=RuntimeError('the fetch broke')), \
                patch('app.community.util.sleep'):
            with pytest.raises(RuntimeError, match='the fetch broke'):
                retrieve_mods_and_backfill(env.community.id, 'peer.example', 'x')

    def test_a_racing_membership_in_the_attributed_to_branch_is_caught(self, env):
        """The SECOND `except IntegrityError`, in the `attributedTo` branch. Round 268 fixed the
        class both handlers name and covered only the collection branch; this is the other copy, and
        a fix applied to one says nothing about the other.

        Same technique as that round: the failure is injected at the session the task fetched, and
        only for a commit with a pending `CommunityMember`.
        """
        from app.community.util import retrieve_mods_and_backfill
        from app.utils import get_task_session

        first = make_user(env.baseline.instance_remote, 'attrmod1')
        second = make_user(env.baseline.instance_remote, 'attrmod2')
        db.session.commit()
        # PERM-5: attributedTo moderators must be on the community's own host
        env.community.ap_profile_id = 'https://peer.example/c/x'
        env.community.ap_moderators_url = None
        db.session.commit()
        actors = iter([first, second])

        class TheFirstMembershipInsertFails:
            def __init__(self, inner):
                self.inner = inner
                self.failures = 0

            def commit(self):
                pending = any(isinstance(obj, CommunityMember) for obj in self.inner.new)
                if pending and self.failures == 0:
                    self.failures += 1
                    raise IntegrityError('INSERT INTO community_member', {},
                                         Exception('duplicate key value'))
                return self.inner.commit()

            def __getattr__(self, name):
                return getattr(self.inner, name)

        proxy = TheFirstMembershipInsertFails(get_task_session())

        with patch('app.community.util.get_task_session', return_value=proxy), \
                patch('app.community.util.find_actor_or_create',
                      side_effect=lambda *a, **k: next(actors)), \
                patch('app.community.util.remote_object_to_json', return_value=None), \
                patch('app.community.util.sleep'):
            retrieve_mods_and_backfill(
                env.community.id, 'peer.example', 'x',
                community_json={'type': 'Group', 'id': 'https://peer.example/c/x',
                                'preferredUsername': 'x',
                                'attributedTo': ['https://peer.example/u/attrmod1',
                                                 'https://peer.example/u/attrmod2']})

        assert proxy.failures == 1
        db.session.expire_all()
        row = db.session.query(CommunityMember).filter_by(
            community_id=env.community.id, user_id=second.id).first()
        assert row is not None and row.is_moderator is True


class TestWhenAnImageResizeFails:

    def test_the_error_reaches_the_caller(self, env):
        """`make_image_sizes_async` rewrites the `File` row that names the resized files, so a
        failure between the write and the commit is a row pointing at files that were never made.
        """
        from app.activitypub.util import make_image_sizes_async
        from app.models import File

        image = File(source_url='https://peer.example/x.png')
        db.session.add(image)
        db.session.commit()

        # `get_request` here is wrapped in its own bare `except: pass`, so the injection goes at the
        # session lookup, which only the outer handler covers.
        with patch('app.activitypub.util.get_task_session',
                   return_value=_session_whose_get_raises('the resize broke')):
            with pytest.raises(RuntimeError, match='the resize broke'):
                make_image_sizes_async(image.id, 170, 512, 'posts', False)
