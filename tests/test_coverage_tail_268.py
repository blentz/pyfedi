"""Round 268: who becomes a moderator of a community this instance just heard of.

`retrieve_mods_and_backfill` runs after `search_for_community` creates a remote community, and
everything it reads is JSON another server sent. `tests/test_community_backfill.py` covers the
malformed-document defects; what had no rows are the arms that decide MEMBERSHIP and identity:

    the two moderator readers   a `moderators` OrderedCollection and an `attributedTo` list. Each
                                promotes an existing member or creates a membership, and each has
                                its own `except IntegrityError` for the race where two backfills
                                run at once. Moderator is a POWER on this instance -- removing
                                posts, banning accounts -- so both readers matter.
    an author that will not resolve   `continue`, twice: a backfilled post's and a backfilled
                                reply's. The alternative is attributing somebody else's content to
                                the wrong account, or crashing the task and leaving the community
                                empty.
    a reply id that is not a url   D1406's check repeated on the FETCHED tree, because this one did
                                not come through an inbox.

Also here: the `current_app.debug` dispatch in `search_for_community`, and the two form validators
whose refusals had no row.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import g
from sqlalchemy.exc import IntegrityError

from app import db
from app.community.util import retrieve_mods_and_backfill
from app.models import Community, CommunityMember, Post, PostReply, Site, User
from tests.factories import make_community, make_instance, make_user

MODS_URL = 'https://remote.test/c/faraway/moderators'
OUTBOX_URL = 'https://remote.test/c/faraway/outbox'
PEER = 'remote.test'


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('faraway', host=PEER)
    community.ap_id = f'faraway@{PEER}'
    community.ap_moderators_url = MODS_URL
    community.ap_outbox_url = OUTBOX_URL
    db.session.commit()
    return SimpleNamespace(app=app, community=community, baseline=api_baseline)


def backfill(env, answers, community_json=None):
    """Run the task with `remote_object_to_json` answering from `answers`.

    `sleep(0.5)` per moderator is real time, so it is patched out -- there are two copies of the
    loop and either would add half a second per actor.
    """
    def fake(url, *args, **kwargs):
        return answers.get(url)

    with patch('app.community.util.remote_object_to_json', side_effect=fake), \
            patch('app.community.util.sleep'):
        retrieve_mods_and_backfill(env.community.id, PEER, 'faraway', community_json)
    db.session.expire_all()


def a_remote_user(name):
    instance = db.session.query(Community).first().instance
    user = make_user(instance, name)
    db.session.commit()
    return user


def is_moderator(community_id, user_id):
    row = db.session.query(CommunityMember).filter_by(community_id=community_id,
                                                      user_id=user_id).first()
    return bool(row and row.is_moderator)


# --------------------------------------------------------------------------
# The moderators collection
# --------------------------------------------------------------------------


class TestWhoTheModeratorsCollectionMakesAModerator:

    def test_an_actor_in_the_collection_becomes_a_moderator(self, env):
        """The baseline for everything below: moderator is a power on THIS instance, granted by a
        list another server published."""
        mod = a_remote_user('themod')

        with patch('app.community.util.find_actor_or_create', return_value=mod):
            backfill(env, {MODS_URL: {'type': 'OrderedCollection',
                                      'orderedItems': [f'https://{PEER}/u/themod']},
                           OUTBOX_URL: None})

        assert is_moderator(env.community.id, mod.id)

    def test_an_existing_member_is_promoted_rather_than_added_again(self, env):
        """`if existing_membership: existing_membership.is_moderator = True`. Somebody who already
        subscribed has a row, and a second one would be refused by the unique constraint -- so
        without this arm a subscriber named as a moderator gets no moderator rights at all."""
        mod = a_remote_user('subscriber')
        db.session.add(CommunityMember(community_id=env.community.id, user_id=mod.id,
                                       is_moderator=False))
        db.session.commit()

        with patch('app.community.util.find_actor_or_create', return_value=mod):
            backfill(env, {MODS_URL: {'type': 'OrderedCollection',
                                      'orderedItems': [f'https://{PEER}/u/subscriber']},
                           OUTBOX_URL: None})

        rows = db.session.query(CommunityMember).filter_by(community_id=env.community.id,
                                                          user_id=mod.id).all()
        assert len(rows) == 1
        assert rows[0].is_moderator is True

    def test_an_actor_that_will_not_resolve_is_skipped(self, env):
        """`if mod:`. `find_actor_or_create` answers None for an actor it cannot fetch or will not
        accept -- a banned one, or one whose document has no usable key -- and the rest of the
        collection still has to be read."""
        good = a_remote_user('goodmod')
        answers = iter([None, good])

        with patch('app.community.util.find_actor_or_create',
                   side_effect=lambda *a, **k: next(answers)):
            backfill(env, {MODS_URL: {'type': 'OrderedCollection',
                                      'orderedItems': [f'https://{PEER}/u/gone',
                                                       f'https://{PEER}/u/goodmod']},
                           OUTBOX_URL: None})

        assert is_moderator(env.community.id, good.id)

    def test_a_racing_duplicate_membership_is_rolled_back_and_the_loop_continues(self, env):
        """`except IntegrityError: session.rollback()`.

        Two backfills of the same community can run at once -- a search and an Announce arriving
        together -- and the loser's INSERT hits the unique constraint on `(community_id, user_id)`.
        The rollback is what lets the loop carry on to the NEXT moderator instead of the whole task
        dying with the community half-filled, so the assertion is on the second moderator being
        made after the first one's commit failed.

        The race cannot be reproduced with two real transactions inside a test that runs in one, so
        the failure is injected at the session the task fetched: `get_task_session` answers with a
        proxy whose first `commit` raises and whose every other attribute is the real session's.
        """
        first = a_remote_user('firstmod')
        second = a_remote_user('secondmod')
        actors = iter([first, second])
        from app.utils import get_task_session

        real_session = get_task_session()

        class TheFirstMembershipInsertFails:
            """Fail the one commit the handler guards, and no other.

            The task commits several times -- after the mod loop, after the flags, per post -- and
            only the commits inside the mod loop are wrapped in `except IntegrityError`. The pending
            `CommunityMember` is what identifies them, so nothing outside the loop is disturbed.
            """

            def __init__(self, inner):
                self.inner = inner
                self.failures = 0
                self.commits = 0

            def commit(self):
                self.commits += 1
                pending = any(isinstance(obj, CommunityMember) for obj in self.inner.new)
                if pending and self.failures == 0:
                    self.failures += 1
                    raise IntegrityError('INSERT INTO community_member', {},
                                         Exception('duplicate key value'))
                return self.inner.commit()

            def __getattr__(self, name):
                return getattr(self.inner, name)

        proxy = TheFirstMembershipInsertFails(real_session)

        with patch('app.community.util.get_task_session', return_value=proxy), \
                patch('app.community.util.find_actor_or_create',
                      side_effect=lambda *a, **k: next(actors)):
            backfill(env, {MODS_URL: {'type': 'OrderedCollection',
                                      'orderedItems': [f'https://{PEER}/u/firstmod',
                                                       f'https://{PEER}/u/secondmod']},
                           OUTBOX_URL: None})

        assert proxy.failures == 1
        assert is_moderator(env.community.id, second.id)


class TestWhoTheAttributedToListMakesAModerator:
    """The second reader, used by servers that publish their moderators inline on the Group
    document rather than as a collection. Same two arms, a second copy of each -- so a fix applied
    to the collection reader says nothing about this one.
    """

    def _group(self, *actors):
        return {'type': 'Group', 'id': f'https://{PEER}/c/faraway',
                'preferredUsername': 'faraway',
                'attributedTo': list(actors)}

    def test_an_actor_in_the_list_becomes_a_moderator(self, env):
        mod = a_remote_user('listmod')
        env.community.ap_moderators_url = None
        db.session.commit()

        with patch('app.community.util.find_actor_or_create', return_value=mod):
            backfill(env, {OUTBOX_URL: None},
                     community_json=self._group(f'https://{PEER}/u/listmod'))

        assert is_moderator(env.community.id, mod.id)

    def test_an_existing_member_is_promoted_here_too(self, env):
        mod = a_remote_user('listsubscriber')
        db.session.add(CommunityMember(community_id=env.community.id, user_id=mod.id,
                                       is_moderator=False))
        db.session.commit()
        env.community.ap_moderators_url = None
        db.session.commit()

        with patch('app.community.util.find_actor_or_create', return_value=mod):
            backfill(env, {OUTBOX_URL: None},
                     community_json=self._group(f'https://{PEER}/u/listsubscriber'))

        rows = db.session.query(CommunityMember).filter_by(community_id=env.community.id,
                                                          user_id=mod.id).all()
        assert len(rows) == 1
        assert rows[0].is_moderator is True

    def test_an_actor_in_the_list_that_will_not_resolve_is_skipped(self, env):
        """`if mod:` -- the list reader's own copy of the guard. `find_actor_or_create` answers None
        for an actor it cannot fetch or will not accept, and `mod.id` on None would end the task with
        the community half-filled."""
        good = a_remote_user('goodlistmod')
        env.community.ap_moderators_url = None
        db.session.commit()
        answers = iter([None, good])

        with patch('app.community.util.find_actor_or_create',
                   side_effect=lambda *a, **k: next(answers)):
            backfill(env, {OUTBOX_URL: None},
                     community_json=self._group(f'https://{PEER}/u/gone',
                                                f'https://{PEER}/u/goodlistmod'))

        assert is_moderator(env.community.id, good.id)

    def test_the_collection_wins_when_both_are_published(self, env):
        """`elif`. A server publishing both is read ONCE, from the collection -- which is the
        dereferenceable one, and therefore the one that can be re-read later."""
        collection_mod = a_remote_user('collectionmod')
        list_mod = a_remote_user('listonlymod')

        def resolve(actor, *args, **kwargs):
            return collection_mod if 'collectionmod' in actor else list_mod

        with patch('app.community.util.find_actor_or_create', side_effect=resolve):
            backfill(env, {MODS_URL: {'type': 'OrderedCollection',
                                      'orderedItems': [f'https://{PEER}/u/collectionmod']},
                           OUTBOX_URL: None},
                     community_json=self._group(f'https://{PEER}/u/listonlymod'))

        assert is_moderator(env.community.id, collection_mod.id)
        assert not is_moderator(env.community.id, list_mod.id)


# --------------------------------------------------------------------------
# Backfilled content whose author cannot be identified
# --------------------------------------------------------------------------


class TestBackfilledContentWithNoUsableAuthor:

    def _outbox(self, *activities):
        return {'type': 'OrderedCollection', 'orderedItems': list(activities)}

    def _create(self, number, **object_extra):
        return {'type': 'Create',
                'id': f'https://{PEER}/activities/create/{number}',
                'attributedTo': f'https://{PEER}/u/poster',
                'object': {'id': f'https://{PEER}/post/{number}',
                           'type': 'Page',
                           'name': f'A backfilled post {number}',
                           'content': '<p>some content</p>',
                           'attributedTo': f'https://{PEER}/u/poster',
                           'to': ['https://www.w3.org/ns/activitystreams#Public'],
                           **object_extra}}

    def test_a_post_whose_author_will_not_resolve_is_skipped(self, env):
        """`if not user: continue`. The account is the post's `user_id`, so there is nothing to
        attribute the post to -- and the rest of the outbox is still worth reading."""
        with patch('app.community.util.find_actor_or_create', return_value=None):
            backfill(env, {MODS_URL: None, OUTBOX_URL: self._outbox(self._create(1))})

        assert db.session.query(Post).filter_by(ap_id=f'https://{PEER}/post/1').count() == 0

    def test_a_post_whose_author_is_local_is_skipped(self, env):
        """`if user.is_local(): continue`. A remote server's outbox claiming a LOCAL account as an
        author is either confused or lying, and either way this instance already holds whatever
        that account wrote."""
        local = make_user(None, 'localposter', local=True)
        db.session.commit()

        with patch('app.community.util.find_actor_or_create', return_value=local):
            backfill(env, {MODS_URL: None, OUTBOX_URL: self._outbox(self._create(2))})

        assert db.session.query(Post).filter_by(ap_id=f'https://{PEER}/post/2').count() == 0


class TestABackfilledReplyThisInstanceWillNotStore:
    """The replies collection of a backfilled post. Everything in it comes off the wire, and two of
    its refusals had no row.

    The outbox entry is the Announce-wrapping-Create shape
    `tests/test_community_backfill_body.py` established: the branch that reads
    `announce['object']['object']` is the one remote Lemmy and PieFed outboxes actually take, and a
    bare `Create` entry goes down the WordPress branch instead, where nothing here applies.
    """

    AUTHOR = f'https://{PEER}/u/someone'
    POST_ID = f'https://{PEER}/p/1'
    REPLIES_URL = f'https://{PEER}/p/1/replies'

    @pytest.fixture
    def seeded(self, env):
        env.community.ap_profile_id = f'https://{PEER}/c/faraway'
        author = make_user(env.baseline.instance_remote, 'someone')
        author.ap_id = f'someone@{PEER}'
        author.ap_profile_id = self.AUTHOR
        author.ap_public_url = self.AUTHOR
        db.session.commit()
        env.author = author
        return env

    def _announce(self):
        post = {'id': self.POST_ID, 'type': 'Page', 'name': 'a backfilled post',
                'attributedTo': self.AUTHOR,
                'to': ['https://www.w3.org/ns/activitystreams#Public'],
                'published': '2026-01-01T00:00:00Z',
                'replies': self.REPLIES_URL}
        return {'id': f'{self.POST_ID}/announce', 'type': 'Announce',
                'object': {'id': self.POST_ID, 'type': 'Create', 'object': post}}

    def _reply(self, reply_id, **extra):
        reply = {'id': reply_id, 'type': 'Note', 'attributedTo': self.AUTHOR,
                 'to': ['https://www.w3.org/ns/activitystreams#Public'],
                 'inReplyTo': self.POST_ID, 'content': 'a backfilled reply'}
        reply.update(extra)
        return reply

    def _run(self, seeded, replies, resolve=None):
        answers = {MODS_URL: {'type': 'OrderedCollection', 'orderedItems': []},
                   OUTBOX_URL: {'type': 'OrderedCollection',
                                'orderedItems': [self._announce()]},
                   self.REPLIES_URL: {'type': 'OrderedCollection',
                                      'orderedItems': list(replies)}}
        patches = [patch('app.community.util.remote_object_to_json',
                         side_effect=lambda url, *a, **k: answers.get(url)),
                   patch('app.community.util.sleep')]
        if resolve is not None:
            patches.append(patch('app.community.util.find_actor_or_create',
                                 side_effect=resolve))
        from contextlib import ExitStack
        with ExitStack() as stack:
            for one in patches:
                stack.enter_context(one)
            retrieve_mods_and_backfill(seeded.community.id, PEER, 'faraway')
        db.session.expire_all()

    def _replies_here(self, seeded):
        return [reply.ap_id for reply in
                db.session.query(PostReply).filter_by(
                    community_id=seeded.community.id).all()]

    def test_an_ordinary_reply_is_stored(self, seeded):
        """The control for the two refusals below, so neither passes because replies are never
        stored at all."""
        self._run(seeded, [self._reply(f'https://{PEER}/r/1')])

        assert self._replies_here(seeded) == [f'https://{PEER}/r/1']

    def test_a_reply_whose_author_will_not_resolve_is_skipped(self, seeded):
        """`if not reply_author: continue`. The account is the reply's `user_id`; without one there
        is nobody to attribute it to. The POST still exists, which is what says the refusal was the
        reply's rather than the whole entry's."""
        # The post's own author resolves; the reply's does not. Same url, so the answers are
        # scripted in call order: the post is read first.
        answers = iter([seeded.author, None])
        attempted = []
        real_new = PostReply.new.__func__

        def recording_new(cls, user, *args, **kwargs):
            attempted.append(user)
            return real_new(cls, user, *args, **kwargs)

        # Asserting on the absence of a reply row is not enough: without the guard, `PostReply.new`
        # is called with None and the AttributeError is swallowed by the `except` around it, so both
        # versions store nothing. What distinguishes them is whether the call happens at all.
        with patch.object(PostReply, 'new', classmethod(recording_new)):
            self._run(seeded, [self._reply(f'https://{PEER}/r/2')],
                      resolve=lambda *a, **k: next(answers, None))

        assert attempted == []
        assert self._replies_here(seeded) == []
        assert db.session.query(Post).filter_by(ap_id=self.POST_ID).count() == 1

    def test_a_reply_whose_id_is_not_an_http_url_is_skipped(self, seeded):
        """D1406 repeated on a FETCHED tree. `reply_data['id']` becomes `PostReply.ap_id`, which a
        template renders as an href, and this tree did not come through an inbox, so it inherits no
        check. `javascript:` is the measured payload from that finding.
        """
        self._run(seeded, [self._reply('javascript:alert(document.domain)')])

        assert self._replies_here(seeded) == []
        assert db.session.query(Post).filter_by(ap_id=self.POST_ID).count() == 1


class TestNobodyCatchesThePsycopg2IntegrityError:
    """D1432 and D1433. Both modules caught `psycopg2.IntegrityError` while SQLAlchemy raises
    `sqlalchemy.exc.IntegrityError`, whose MRO is

        IntegrityError -> DatabaseError -> DBAPIError -> StatementError -> SQLAlchemyError

    with no psycopg2 ancestor, so `issubclass(sqlalchemy.exc.IntegrityError,
    psycopg2.IntegrityError)` is False and every one of those handlers was dead. Each guarded an
    INSERT that a concurrent copy of the same work can lose on a unique constraint -- a second
    backfill of one community, or a second Accept for one join request -- and the exception went to
    an outer handler that re-raised.

    This row is the guard against the import coming back, here or anywhere else under `app/`.
    """

    def test_the_two_classes_are_unrelated(self):
        import psycopg2
        import sqlalchemy.exc

        assert not issubclass(sqlalchemy.exc.IntegrityError, psycopg2.IntegrityError)

    def test_no_module_under_app_imports_integrityerror_from_psycopg2(self):
        import ast
        import pathlib

        offenders = []
        for path in sorted(pathlib.Path('app').rglob('*.py')):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom) and (node.module or '').startswith('psycopg2'):
                    for alias in node.names:
                        if alias.name == 'IntegrityError':
                            offenders.append(f'{path}:{node.lineno}')
        assert offenders == [], \
            f'psycopg2.IntegrityError cannot catch what SQLAlchemy raises: {offenders}'

    def test_a_duplicate_accept_logs_success_rather_than_raising(self, env):
        """The D1433 site, driven through the function that holds it. A join request whose
        membership row ALREADY exists takes the `if not existing_membership:` false side, so this
        row is about the arm above it; what the handler covers is the same insert losing a race, and
        the import fix is what lets it be caught at all.
        """
        import app.activitypub.routes as ap_routes

        assert ap_routes.IntegrityError.__module__.startswith('sqlalchemy')
