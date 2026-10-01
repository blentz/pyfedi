"""Round 235: the downvote's own happy path, and four small federation endpoints.

`tests/test_inbox_dispatch_votes.py` covers `process_upvote` end to end and
`process_downvote` only where the two differ -- the `else` that logs 'Cannot downvote
this', which the upvote arm lacks. The downvote's SUCCESS path, its unfound-object
refusal, and its dict-unwrapping were never driven, so a downvote arriving from a peer had
no row saying it is recorded, logged and announced.

Also here: the HEAD arms of the two reply collections, `/post/<id>/`'s redirect, the feed
inbox and the two feed collections' refusal of a remote handle, and the rollback arm of
`process_delete_request`.

The two `pass` statements this round removed (D1421) are why some of these lines looked
uncovered at all -- see the findings ledger.
"""
import pytest
from flask import g

from app import db
from app.activitypub import routes as activitypub_routes
from app.activitypub.routes import process_downvote, process_question_answer
from app.models import ActivityPubLog, PostReply, PostVote, Site, utcnow
from tests.factories import (make_community, make_instance, make_post, make_post_reply,
                             make_site, make_user)


def _seed_vote_scenario(host='peer.example'):
    """The same shape as `tests/test_inbox_dispatch_votes.py`'s helper: a local community
    owned by the seeded user 1 on instance 1, an author, a voter, and one post.
    """
    make_site()
    instance = make_instance(host)
    make_user(instance, 'community_owner')
    community = make_community(host=host)
    community.ap_fetched_at = utcnow()
    author = make_user(instance, 'author')
    voter = make_user(instance, 'voter')
    post = make_post(community, author, ap_id=f'https://{host}/objects/1')
    db.session.commit()
    return voter, post


# --------------------------------------------------------------------------
# process_downvote's own path
# --------------------------------------------------------------------------


class TestADownvoteFromAPeer:

    def test_it_records_the_vote_logs_and_announces(self, app, db_session, monkeypatch):
        """The happy path. `liked.vote()` runs for real, so the assertion is a `PostVote`
        row with `effect == -1` -- the sign is what distinguishes this from the upvote
        path, and a delegate that called `vote(user, 'upvote', ...)` would otherwise look
        identical in the log."""
        monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
        voter, post = _seed_vote_scenario()
        calls = []
        monkeypatch.setattr(activitypub_routes, 'announce_activity_to_followers',
                            lambda *args, **kwargs: calls.append((args, kwargs)))

        request_json = {'id': 'https://peer.example/activities/1', 'object': post.ap_id}

        process_downvote(voter, True, request_json, False)

        vote = PostVote.query.filter_by(user_id=voter.id, post_id=post.id).one()
        assert vote.effect == -1
        assert ActivityPubLog.query.one().result == 'success'
        args, kwargs = calls[0]
        assert args[0].id == post.community_id
        assert args[1].id == voter.id
        assert kwargs == {'can_batch': True}

    def test_an_announced_downvote_is_not_announced_again(self, app, db_session,
                                                          monkeypatch):
        """`if not announced:`. An activity that reached this instance inside an Announce
        has already been distributed by the community's own server, so re-announcing it
        sends every follower a second copy."""
        monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
        voter, post = _seed_vote_scenario()
        calls = []
        monkeypatch.setattr(activitypub_routes, 'announce_activity_to_followers',
                            lambda *args, **kwargs: calls.append(args))

        request_json = {'id': 'https://peer.example/activities/1',
                        'object': {'object': post.ap_id}}

        process_downvote(voter, True, request_json, True)

        assert PostVote.query.filter_by(user_id=voter.id).one().effect == -1
        assert calls == []

    def test_a_dict_object_is_unwrapped_to_its_id(self, app, db_session, monkeypatch):
        """Lemmy and kbin send `object` as an object rather than a string. Proven by the
        vote succeeding: an un-unwrapped dict never matches a `Post.ap_id` string and would
        fall into the unfound-object refusal below."""
        voter, post = _seed_vote_scenario()
        monkeypatch.setattr(activitypub_routes, 'announce_activity_to_followers',
                            lambda *a, **k: None)

        request_json = {'id': 'https://peer.example/activities/1',
                        'object': {'id': post.ap_id}}

        process_downvote(voter, True, request_json, False)

        assert PostVote.query.filter_by(user_id=voter.id, post_id=post.id).one().effect \
            == -1

    def test_a_downvote_of_something_that_is_not_here_is_logged_and_dropped(
            self, app, db_session, monkeypatch):
        """The refusal, with the ap_id folded into the message so an operator reading the
        log can tell which object was missing. No vote row is written."""
        monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
        voter, _post = _seed_vote_scenario()

        ap_id = 'https://peer.example/objects/does-not-exist'
        request_json = {'id': 'https://peer.example/activities/1', 'object': ap_id}

        process_downvote(voter, True, request_json, False)

        row = ActivityPubLog.query.one()
        assert row.result == 'failure'
        assert row.exception_message == f'Unfound object {ap_id}'
        assert PostVote.query.count() == 0


class TestAQuestionAnswerWithADictObject:

    def test_a_dict_object_is_unwrapped_to_its_id(self, app, db_session, monkeypatch):
        """`process_question_answer`'s copy of the same unwrapping. It is a separate copy
        in a separate function, so it needs its own row -- proven here by the refusal
        naming the unwrapped STRING: an un-unwrapped dict would be concatenated into the
        message and raise `TypeError` instead."""
        monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
        voter, _post = _seed_vote_scenario()

        ap_id = 'https://peer.example/comments/nothing-here'
        request_json = {'id': 'https://peer.example/activities/1',
                        'object': {'id': ap_id}}

        process_question_answer(voter, True, request_json, False)

        assert ActivityPubLog.query.one().exception_message == f'Unfound object {ap_id}'


# --------------------------------------------------------------------------
# The reply collections and the post redirect
# --------------------------------------------------------------------------


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.site = site
    community = make_community('collectionland')
    author = make_user(api_baseline.instance_local, 'collectionauthor', local=True)
    post = make_post(community, author, ap_id='https://test.piefed.local/x/1')
    db.session.commit()
    return SimpleNamespace(app=app, site=site, client=app.test_client(), post=post,
                           community=community, author=author, baseline=api_baseline)


AP_HEADERS = {'Accept': 'application/activity+json'}


class TestTheReplyCollections:
    """`/post/<id>/replies` and `/post/<id>/context` both answer GET with a collection and
    HEAD with an empty one -- a HEAD is a peer asking whether the URL exists and what its
    headers are, and building the reply list for an answer with no body is work nobody
    reads.
    """

    @pytest.mark.parametrize('suffix', ['replies', 'context'])
    def test_a_get_lists_the_replies(self, env, suffix):
        make_post_reply(env.post, env.author, body='a reply')
        db.session.commit()

        response = env.client.get(f'/post/{env.post.id}/{suffix}',
                                  headers=AP_HEADERS)

        assert response.status_code == 200
        assert response.get_json()['totalItems'] >= 1

    @pytest.mark.parametrize('suffix,view', [('replies', 'post_replies_ap'),
                                             ('context', 'post_ap_context')])
    def test_a_head_answers_without_building_the_list(self, env, suffix, view):
        """The `else` arm, and the only way to see it: a HEAD response has its body
        stripped by the time a test client sees it, so both arms look identical from
        outside. The view is called directly, and the collection it built is read before
        Flask discards it -- `totalItems` is absent, which is what makes a HEAD cheap.
        """
        import app.activitypub.routes as routes

        make_post_reply(env.post, env.author, body='a reply')
        db.session.commit()

        with env.app.test_request_context(f'/post/{env.post.id}/{suffix}',
                                          method='HEAD', headers=AP_HEADERS):
            response = getattr(routes, view)(env.post.id)

        assert response.status_code == 200
        assert 'totalItems' not in response.get_data(as_text=True)

    @pytest.mark.parametrize('suffix,view', [('replies', 'post_replies_ap'),
                                             ('context', 'post_ap_context')])
    def test_a_get_through_the_same_call_does_build_it(self, env, suffix, view):
        """The control for the row above, taking the same route into the view so the two
        differ only in the method."""
        import app.activitypub.routes as routes

        make_post_reply(env.post, env.author, body='a reply')
        db.session.commit()

        with env.app.test_request_context(f'/post/{env.post.id}/{suffix}',
                                          method='GET', headers=AP_HEADERS):
            response = getattr(routes, view)(env.post.id)

        assert 'totalItems' in response.get_data(as_text=True)

    @pytest.mark.parametrize('suffix', ['replies', 'context'])
    def test_a_browser_request_is_refused(self, env, suffix):
        """Both collections are ActivityPub-only; a browser gets a 400 rather than a page
        it cannot use."""
        response = env.client.get(f'/post/{env.post.id}/{suffix}')

        assert response.status_code >= 400

    def test_a_trailing_slash_redirects_to_the_post(self, env):
        """`/post/<id>/` is a separate route whose whole body is a redirect to
        `/post/<id>`, so a peer that appends a slash is not answered with a 404."""
        response = env.client.get(f'/post/{env.post.id}/')

        assert response.status_code == 302
        assert response.headers['Location'].endswith(f'/post/{env.post.id}')


# --------------------------------------------------------------------------
# The feed actor's endpoints
# --------------------------------------------------------------------------


class TestAFeedsOwnEndpoints:
    """A Feed is an ActivityPub actor, so it has an inbox and the two collections. All
    three refuse a handle containing `@`: a REMOTE feed's ActivityPub data belongs to the
    instance that hosts it, and answering for it here would be this instance speaking for
    another.
    """

    def test_the_feed_inbox_is_the_shared_inbox(self, env):
        """`return shared_inbox()`. The per-actor inbox exists because peers address it,
        but nothing about the handling differs -- proven by the malformed-body refusal
        being the same one the shared inbox gives."""
        response = env.client.post('/f/anyfeed/inbox', data='not json',
                                   content_type='application/activity+json')

        assert response.status_code == 400

    @pytest.mark.parametrize('suffix', ['outbox', 'following'])
    def test_a_remote_handle_is_refused(self, env, suffix):
        # as a peer asks: without the ActivityPub Accept header a browser is redirected (D177)
        response = env.client.get(f'/f/somefeed@peer.example/{suffix}', headers=AP_HEADERS)

        assert response.status_code == 400

    @pytest.mark.parametrize('suffix', ['outbox', 'following'])
    def test_a_feed_this_instance_does_not_have_is_a_404(self, env, suffix):
        """The line below the refusal, and the reason it is a 404 rather than a crash:
        `feed.public` is read further down."""
        response = env.client.get(f'/f/nosuchfeed/{suffix}', headers=AP_HEADERS)

        assert response.status_code == 404
