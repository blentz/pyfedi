"""Round 266: a boost from a banned account, and the remaining refresh arms.

`process_microblog_announce` ingests a boost of somebody else's post. Its trust gate is the only
thing between a peer's Announce and a post appearing in a local reader's feed, and two of its
refusals had no rows:

    a banned announcer   a STALE-CACHE BACKSTOP. `find_actor_or_create_cached` rejects a banned
                         actor upstream, but only for an actor it looks up -- an id cached before
                         the ban bypasses that check, and this arm is what catches it.
    an object that is not a post   the boosted uri resolved to something else, so there is nothing
                         to show.

`create_post_reply`'s "could not find parent post" is the same kind of refusal one class over.

The rest of the round is the refresh machinery: the `current_app.debug` dispatch arms that decide
whether a refresh runs inline or through celery, kbin's `moderators` spelling of a feed's owner
collection, the `nsfl` flag a feed's own server sets, the promotion of an existing member to owner,
and `actor_json_to_model`'s `KeyError` arm -- which exists because the document is a peer's and a
missing key must not take the caller down with it.
"""
from unittest.mock import patch

import httpx
import pytest

from app import db
from app.activitypub import util as ap_util
from app.activitypub.util import (actor_json_to_model, banned_user_agents, create_post_reply,
                                  process_microblog_announce, refresh_community_profile,
                                  refresh_feed_profile)
from app.models import Feed, FeedMember, Post, User
from tests.factories import (make_community, make_feed, make_feed_member, make_follow,
                             make_instance, make_post, make_site, make_user)

PEER = 'm.example'


@pytest.fixture
def followed_booster(db_session):
    """A remote account a local reader follows, which is what `announcer_is_followed` looks for."""
    make_site()
    instance = make_instance(PEER)
    booster = make_user(instance, 'booster')
    local = make_user(None, 'localreader', local=True)
    make_follow(local, booster)
    db.session.commit()
    return booster


@pytest.fixture
def log_spy(monkeypatch):
    """`LOG_ACTIVITYPUB_TO_DB` is False by default, so `log_incoming_ap` is a no-op under test.

    Intercepting it is what makes each refusal's REASON observable -- and a distinct reason per exit
    is the only way an operator reading the log can tell which gate refused an Announce.
    """
    calls = []

    def fake_log(id, aplog_type, aplog_result, saved_json, message=None, session=None):
        calls.append((aplog_type, aplog_result, message))

    monkeypatch.setattr('app.activitypub.util.log_incoming_ap', fake_log)
    return calls


def announce(actor_uri, object_uri):
    return {
        'id': f'{actor_uri}/statuses/1/activity',
        'type': 'Announce',
        'actor': actor_uri,
        'object': object_uri,
    }


# --------------------------------------------------------------------------
# A boost this instance will not ingest
# --------------------------------------------------------------------------


class TestABoostFromAnAccountThatIsBanned:

    def test_a_banned_announcer_is_refused(self, followed_booster, log_spy):
        """`if announcer.banned`, defence in depth. Since D59 `find_actor_or_create_cached` refuses a
        banned actor on a cache hit as well as a miss, so only a stubbed lookup reaches this arm.
        """
        followed_booster.banned = True
        db.session.commit()
        fetched = []

        # `find_actor_or_create_cached` refuses a banned actor, so the backstop is reached only by
        # stubbing the lookup to hand one back.
        with patch.object(ap_util, 'find_actor_or_create_cached',
                          return_value=followed_booster), \
                patch.object(ap_util, 'remote_object_to_json',
                             side_effect=lambda uri: fetched.append(uri)):
            result = process_microblog_announce(
                announce(followed_booster.ap_profile_id, f'https://{PEER}/statuses/1'),
                'https://m.example/activities/1', False)

        assert result is None
        # Above every network call, which is what the gate's own comment requires.
        assert fetched == []
        assert log_spy[-1][2] == f'{followed_booster.ap_id} is banned'

    def test_an_unbanned_announcer_gets_past_that_gate(self, followed_booster, log_spy):
        """The control: the row above must be refusing for the BAN rather than for something the
        fixture does. This one reaches the fetch and fails later, on the fetch answering nothing.
        """
        fetched = []

        with patch.object(ap_util, 'remote_object_to_json',
                          side_effect=lambda uri: fetched.append(uri)):
            result = process_microblog_announce(
                announce(followed_booster.ap_profile_id, f'https://{PEER}/statuses/1'),
                'https://m.example/activities/1', False)

        assert result is None
        assert fetched == [f'https://{PEER}/statuses/1']
        assert log_spy[-1][2] != f'{followed_booster.ap_id} is banned'

    def test_a_boost_of_something_that_is_not_a_post_is_refused(self, followed_booster, log_spy):
        """`if not isinstance(resolved, Post)`. `create_resolved_object` answers with whatever the
        uri turned out to be -- a reply, or None -- and a boost of one of those has nothing to show
        in a feed. The reason string is its own, so the log distinguishes it from the fetch
        failing."""
        document = {'id': f'https://{PEER}/statuses/1', 'type': 'Note',
                    'attributedTo': followed_booster.ap_profile_id,
                    'content': '<p>a boosted note</p>',
                    'to': ['https://www.w3.org/ns/activitystreams#Public']}

        with patch.object(ap_util, 'remote_object_to_json', return_value=document), \
                patch.object(ap_util, 'create_resolved_object', return_value=None):
            result = process_microblog_announce(
                announce(followed_booster.ap_profile_id, f'https://{PEER}/statuses/1'),
                'https://m.example/activities/1', False)

        assert result is None
        assert log_spy[-1][2] == 'Boosted object did not resolve to a post'

    def test_a_boost_that_does_resolve_to_a_post_is_kept(self, followed_booster, log_spy):
        """The True side of the same test, so the row above is not passing because nothing ever
        resolves. `record_boost` is what puts the post in the followers' feeds.

        The boosted uri must NOT already be a post here. `Post.get_by_ap_id(uri)` eleven lines
        above returns the already-ingested post and records the boost there, so a post seeded under
        the announced uri never reaches the resolve path at all -- which is what the next row
        covers instead.
        """
        community = make_community('microblogland')
        db.session.commit()
        post = make_post(community, followed_booster, ap_id=f'https://{PEER}/statuses/already')
        db.session.commit()
        uri = f'https://{PEER}/statuses/fresh'
        document = {'id': uri, 'type': 'Note',
                    'attributedTo': followed_booster.ap_profile_id,
                    'content': '<p>a boosted note</p>',
                    'to': ['https://www.w3.org/ns/activitystreams#Public']}
        boosted = []

        with patch.object(ap_util, 'remote_object_to_json', return_value=document), \
                patch.object(ap_util, 'create_resolved_object', return_value=post), \
                patch.object(ap_util, 'record_boost',
                             side_effect=lambda p, a: boosted.append((p.id, a.id))):
            result = process_microblog_announce(
                announce(followed_booster.ap_profile_id, uri),
                'https://m.example/activities/1', False)

        assert result is post
        assert boosted == [(post.id, followed_booster.id)]

    def test_a_boost_of_a_post_already_here_is_recorded_without_fetching(self, followed_booster,
                                                                       log_spy):
        """`post = Post.get_by_ap_id(uri)` / `if post:`. The comment above it says the uri may be a
        LOCAL post's, because local ap_ids are guessable -- so this arm exists to record the boost
        without a fetch, and the private/local_only refusal below it is the gate that a guessed uri
        runs into."""
        community = make_community('microblogland')
        db.session.commit()
        post = make_post(community, followed_booster, ap_id=f'https://{PEER}/statuses/here')
        db.session.commit()
        fetched = []
        boosted = []

        with patch.object(ap_util, 'remote_object_to_json',
                          side_effect=lambda uri: fetched.append(uri)), \
                patch.object(ap_util, 'record_boost',
                             side_effect=lambda p, a: boosted.append((p.id, a.id))):
            result = process_microblog_announce(
                announce(followed_booster.ap_profile_id, f'https://{PEER}/statuses/here'),
                'https://m.example/activities/1', False)

        assert result is post
        assert fetched == []
        assert boosted == [(post.id, followed_booster.id)]

    def test_a_boost_of_a_post_in_a_private_community_is_refused(self, followed_booster, log_spy):
        """The gate that arm exists for. A remote actor one local user follows could otherwise
        Announce a guessed `https://<server>/post/<id>` and have it recorded as a boost -- including
        a post in an invite-only or local-only community it was never a member of."""
        community = make_community('privateland')
        community.private = True
        db.session.commit()
        post = make_post(community, followed_booster, ap_id=f'https://{PEER}/statuses/secret')
        db.session.commit()
        boosted = []

        with patch.object(ap_util, 'record_boost',
                          side_effect=lambda p, a: boosted.append(p.id)):
            result = process_microblog_announce(
                announce(followed_booster.ap_profile_id, f'https://{PEER}/statuses/secret'),
                'https://m.example/activities/1', False)

        assert result is None
        assert boosted == []
        assert log_spy[-1][2] == \
            'Boosted post belongs to a private or local_only community'


class TestAReplyWhoseParentIsNotHere:

    def test_a_reply_with_no_findable_parent_post_is_refused(self, db_session, log_spy):
        """`if post_id is None`, which is NOT the same refusal as finding no parent at all.

        `find_reply_parent` answers with `(post_id, parent_comment_id, root_id)`. All three absent
        takes the outer `else` ("Unable to find parent post/comment"); this arm is the case where a
        ROOT was identified and the post it belongs to was not -- a reply chain whose ancestor rows
        exist but whose post has been deleted. Storing the reply would be a comment on nothing,
        reachable from no page, and the distinct reason string is what tells the two apart in the
        log.
        """
        make_site()
        instance = make_instance(PEER)
        author = make_user(instance, 'replier')
        community = make_community('replyland')
        db.session.commit()

        with patch.object(ap_util, 'find_reply_parent', return_value=(None, None, 42)):
            result = create_post_reply(False, community, f'https://{PEER}/statuses/999', {
                'id': f'https://{PEER}/activities/r1',
                'type': 'Create',
                'to': ['https://www.w3.org/ns/activitystreams#Public'],
                'object': {'id': f'https://{PEER}/statuses/r1',
                           'type': 'Note',
                           'inReplyTo': f'https://{PEER}/statuses/999',
                           'content': '<p>a reply to nothing</p>',
                           'to': ['https://www.w3.org/ns/activitystreams#Public'],
                           'attributedTo': author.ap_profile_id}}, author)

        assert result is None
        assert log_spy[-1][2] == 'Could not find parent post'


# --------------------------------------------------------------------------
# How a refresh is dispatched
# --------------------------------------------------------------------------


class TestHowARefreshIsDispatched:
    """`if current_app.debug: <task>(...) else: <task>.apply_async(..., countdown=randint(1, 10))`

    The countdown is the point of the else: a refresh is triggered by rendering a page that mentions
    the actor, so a popular remote community would otherwise have every worker fetch its profile at
    the same instant.
    """

    def test_a_community_refresh_runs_inline_under_debug(self, db_session, monkeypatch):
        monkeypatch.setattr(ap_util.current_app, 'debug', True, raising=False)
        called = []

        with patch.object(ap_util, 'refresh_community_profile_task',
                          side_effect=lambda *args: called.append(args)):
            refresh_community_profile(7, {'type': 'Group'})

        assert called == [(7, {'type': 'Group'})]

    def test_a_community_refresh_is_queued_with_a_countdown_otherwise(self, db_session,
                                                                     monkeypatch):
        monkeypatch.setattr(ap_util.current_app, 'debug', False, raising=False)
        queued = []

        class Task:
            @staticmethod
            def apply_async(args=None, countdown=None):
                queued.append((args, countdown))

        with patch.object(ap_util, 'refresh_community_profile_task', Task):
            refresh_community_profile(7, {'type': 'Group'})

        assert len(queued) == 1
        args, countdown = queued[0]
        assert args == (7, {'type': 'Group'})
        assert 1 <= countdown <= 10

    def test_a_feed_refresh_runs_inline_under_debug(self, db_session, monkeypatch):
        monkeypatch.setattr(ap_util.current_app, 'debug', True, raising=False)
        called = []

        with patch.object(ap_util, 'refresh_feed_profile_task',
                          side_effect=lambda *args: called.append(args)):
            refresh_feed_profile(9)

        assert called == [(9,)]

    def test_a_feed_refresh_is_queued_with_a_countdown_otherwise(self, db_session, monkeypatch):
        monkeypatch.setattr(ap_util.current_app, 'debug', False, raising=False)
        queued = []

        class Task:
            @staticmethod
            def apply_async(args=None, countdown=None):
                queued.append((args, countdown))

        with patch.object(ap_util, 'refresh_feed_profile_task', Task):
            refresh_feed_profile(9)

        assert queued[0][0] == (9,)
        assert 1 <= queued[0][1] <= 10


# --------------------------------------------------------------------------
# What a feed's own server says about it
# --------------------------------------------------------------------------


class TestRefreshingAFeedsProfile:

    @pytest.fixture
    def remote_feed(self, db_session):
        make_site()
        instance = make_instance(PEER)
        owner = make_user(instance, 'feedowner')
        feed = make_feed(instance, 'news')
        feed.ap_public_url = f'https://{PEER}/f/news'
        feed.ap_followers_url = None
        feed.ap_moderators_url = None
        db.session.commit()
        from types import SimpleNamespace
        return SimpleNamespace(feed=feed, owner=owner, instance=instance)

    def _document(self, **fields):
        document = {
            'type': 'Feed',
            'id': f'https://{PEER}/f/news',
            'preferredUsername': 'news',
            'name': 'News, refreshed',
            'publicKey': {'publicKeyPem': '-----BEGIN PUBLIC KEY-----refreshed'},
        }
        document.update(fields)
        return document

    def _refresh(self, remote_feed, document):
        with patch.object(ap_util, 'get_request',
                          return_value=httpx.Response(200, json=document)):
            ap_util.refresh_feed_profile_task(remote_feed.feed.id)
        db.session.expire_all()
        return db.session.get(Feed, remote_feed.feed.id)

    def test_the_lemmy_spelling_of_the_owner_collection_is_read(self, remote_feed):
        """`attributedTo`, which Lemmy, mbin and PieFed's own feeds send."""
        refreshed = self._refresh(remote_feed, self._document(
            attributedTo=f'https://{PEER}/f/news/moderators'))

        assert refreshed.ap_moderators_url == f'https://{PEER}/f/news/moderators'

    def test_the_kbin_spelling_is_read_too(self, remote_feed):
        """`elif 'moderators' in activity_json`. A peer sending only this key would otherwise have
        its feed's owners forgotten on every refresh -- the column is overwritten
        unconditionally two lines down."""
        refreshed = self._refresh(remote_feed, self._document(
            moderators=f'https://{PEER}/f/news/mods'))

        assert refreshed.ap_moderators_url == f'https://{PEER}/f/news/mods'

    def test_a_document_with_neither_clears_the_column(self, remote_feed):
        """`else: owners_url = None`, and the assignment below is unconditional -- so a feed whose
        server stopped publishing an owner collection has the stale url removed rather than kept
        and fetched for ever."""
        remote_feed.feed.ap_moderators_url = f'https://{PEER}/f/news/old-mods'
        db.session.commit()

        refreshed = self._refresh(remote_feed, self._document())

        assert refreshed.ap_moderators_url is None

    def test_the_nsfl_flag_is_taken_from_the_document(self, remote_feed):
        """`if 'nsfl' in activity_json and activity_json['nsfl']`. It is a CONTENT WARNING set by
        the feed's own server, and a reader who filtered nsfl out relies on it arriving."""
        refreshed = self._refresh(remote_feed, self._document(nsfl=True))

        assert refreshed.nsfl is True

    def test_a_false_nsfl_does_not_set_the_flag(self, remote_feed):
        """The `and activity_json['nsfl']` half. Unlike `sensitive` above it, this one is only ever
        turned ON by a refresh -- recorded because the asymmetry is easy to read as a bug."""
        remote_feed.feed.nsfl = True
        db.session.commit()

        refreshed = self._refresh(remote_feed, self._document(nsfl=False))

        assert refreshed.nsfl is True

    def test_an_existing_member_is_promoted_to_owner(self, remote_feed):
        """The owners loop's `if existing_membership:` arm. Somebody who had already subscribed to
        the feed and is named in its owner collection must be UPDATED rather than have a second
        FeedMember row created, which the unique constraint would refuse."""
        member = make_feed_member(remote_feed.owner, remote_feed.feed)
        member.is_owner = False
        db.session.commit()

        # One `get_request` serves both fetches -- the actor document, then the owner collection
        # it named -- so the sequence is scripted rather than a single answer.
        with patch.object(ap_util, 'get_request', side_effect=[
                httpx.Response(200,
                               json=self._document(attributedTo=f'https://{PEER}/f/news/mods')),
                httpx.Response(200, json={'type': 'OrderedCollection', 'totalItems': 1,
                                          'orderedItems': [remote_feed.owner.ap_profile_id]})]), \
                patch.object(ap_util, 'find_actor_or_create',
                             return_value=remote_feed.owner), \
                patch.object(ap_util.time, 'sleep'):
            ap_util.refresh_feed_profile_task(remote_feed.feed.id)

        db.session.expire_all()
        rows = FeedMember.query.filter_by(feed_id=remote_feed.feed.id,
                                         user_id=remote_feed.owner.id).all()
        assert len(rows) == 1
        assert rows[0].is_owner is True


# --------------------------------------------------------------------------
# A document that is missing a key
# --------------------------------------------------------------------------


class TestADocumentThatCannotBeRead:

    def test_a_missing_key_answers_none_rather_than_raising(self, db_session):
        """`except KeyError: log; return None`.

        `actor_json_to_model` is handed a document a PEER wrote, and the constructor above reads
        several keys outright. Answering None is what lets the caller refuse the actor; a KeyError
        would come out of whichever inbox or page triggered the lookup.
        """
        make_site()
        make_instance(PEER)
        db.session.commit()

        # `endpoints` present without `sharedInbox` is the KeyError this `try` covers: every other
        # key the constructor reads is either guarded with an `in` test or read above the `try`.
        result = actor_json_to_model({
            'type': 'Person',
            'id': f'https://{PEER}/u/nokeys',
            'preferredUsername': 'nokeys',
            'publicKey': {'publicKeyPem': '-----BEGIN PUBLIC KEY-----x'},
            'endpoints': {},
        }, 'nokeys', PEER)

        assert result is None
        assert db.session.query(User).filter_by(user_name='nokeys').count() == 0

    def test_a_document_with_the_keys_produces_a_row(self, db_session):
        """The control. The `try` covers a constructor call, so a row here says the KeyError arm
        above is reached for the missing key rather than for the shape of the call."""
        make_site()
        make_instance(PEER)
        db.session.commit()

        result = actor_json_to_model({
            'type': 'Person',
            'id': f'https://{PEER}/u/haskeys',
            'preferredUsername': 'haskeys',
            'inbox': f'https://{PEER}/u/haskeys/inbox',
            'outbox': f'https://{PEER}/u/haskeys/outbox',
            'publicKey': {'publicKeyPem': '-----BEGIN PUBLIC KEY-----x'},
        }, 'haskeys', PEER)

        assert result is not None
        assert result.user_name == 'haskeys'


class TestTheUserAgentBlocklistStub:

    def test_it_blocks_nothing_yet(self):
        """`return []  # todo: finish this function`.

        Recorded as OBSERVED, and deliberately: it is a stub, and its callers treat an empty list as
        "block nobody". The row is here so that filling it in is a visible change rather than a
        silent one -- a list of user agents is a blocklist, and a blocklist that quietly starts
        matching is a federation outage.
        """
        assert banned_user_agents() == []
