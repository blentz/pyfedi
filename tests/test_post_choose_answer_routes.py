"""Marking a comment as the answer to a question, from the web.

`post_reply_choose_answer` and `post_reply_unchoose_answer` in
`app/post/routes.py`. Neither had been requested by a test.

D1357 is D1127's repair on the two siblings it missed. `post_set_ai`, thirty lines
above these, reads `db.session.get(Post, post_id) or abort(404)` for exactly this
reason; these two read `db.session.get(PostReply, post_reply_id)` and then
dereferenced it. The order of the disjuncts is what hid it: an anonymous caller is
refused by `current_user.is_authenticated` before anything is read, and a logged-in
non-moderator is refused by the reads themselves raising -- but an ADMIN or a
MODERATOR passes on the FIRST disjunct, so the None travelled into
`choose_answer`, where `post_reply.answer = True` was

    AttributeError: 'NoneType' object has no attribute 'answer'
    app/shared/reply.py:593

-- a 500 for a reply id that does not exist. Measured with `api_baseline`'s user1,
who is an admin.

The permission rule itself is left exactly as it was and is pinned below, including
the part worth questioning: `post_reply.user_id == current_user.id` lets the
REPLY'S OWN AUTHOR mark their comment as the accepted answer to somebody else's
question. `post_reply_mark_as_answer` in `app/api/alpha/utils/reply.py` applies the
same rule, so it is consistent across both entry points rather than a slip in one
of them, and changing a product policy is not this campaign's business. The tests
say what it does today so that a change to it has to be deliberate.
"""
import pytest
from flask import g

from app import db
from app.models import Community, CommunityMember, Post, PostReply, Site, User, utcnow


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.site = site
    db.session.commit()
    return SimpleNamespace(baseline=api_baseline, client=app.test_client(),
                           app=app)


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def logout(client):
    with client.session_transaction() as session:
        session.clear()


def a_question_and_answer(env, asker=None, answerer=None):
    """A post by one user with a reply by another, in a local community.

    The two must be different people: the whole question this route answers is
    whose permission it is, and a post and reply by the same user cannot show it.
    """
    baseline = env.baseline
    asker = asker or baseline.user2
    answerer = answerer or baseline.user3
    community = db.session.get(Community, baseline.community1.id)
    community.instance_id = 1
    community.question_answer = True
    post = Post(user_id=asker.id, community_id=community.id, title='a question',
                type=0, posted_at=utcnow(), last_active=utcnow(), deleted=False,
                status=1, from_bot=False, nsfw=False, nsfl=False, sticky=False,
                indexable=True, microblog=False)
    db.session.add(post)
    db.session.commit()
    reply = PostReply(user_id=answerer.id, post_id=post.id,
                      community_id=community.id, body='an answer',
                      body_html='<p>an answer</p>', posted_at=utcnow(),
                      deleted=False, answer=False)
    db.session.add(reply)
    db.session.commit()
    return post, reply


def a_moderator(env, community_id):
    user = env.baseline.user4
    db.session.add(CommunityMember(user_id=user.id, community_id=community_id,
                                   is_moderator=True, is_owner=False))
    db.session.commit()
    return user


class TestAReplyIdThatDoesNotExist:
    """D1357. The 404 has to come from the route, because by the time
    `choose_answer` has it there is nothing to check: that function assigns to
    `post_reply.answer` on its first line.
    """

    @pytest.mark.parametrize('path', ['choose_answer', 'unchoose_answer'])
    def test_an_admin_gets_a_404(self, env, path):
        """An admin passes the first disjunct, so nothing else stopped the None."""
        assert env.baseline.user1.is_admin_or_staff() is True
        login(env.client, env.baseline.user1)

        response = env.client.post(f'/post_reply/999999/{path}')

        assert response.status_code == 404

    @pytest.mark.parametrize('path', ['choose_answer', 'unchoose_answer'])
    def test_a_moderator_gets_a_404(self, env, path):
        post, reply = a_question_and_answer(env)
        moderator = a_moderator(env, reply.community_id)
        login(env.client, moderator)

        response = env.client.post(f'/post_reply/999999/{path}')

        assert response.status_code == 404

    @pytest.mark.parametrize('path', ['choose_answer', 'unchoose_answer'])
    def test_an_ordinary_user_gets_a_404_rather_than_a_500(self, env, path):
        """Before the repair this raised out of the ROUTE rather than out of
        `choose_answer`, on `post_reply.user_id`."""
        login(env.client, env.baseline.user3)

        response = env.client.post(f'/post_reply/999999/{path}')

        assert response.status_code == 404

    @pytest.mark.parametrize('path', ['choose_answer', 'unchoose_answer'])
    def test_an_anonymous_caller_gets_a_404_too(self, env, path):
        """It used to be a 403: `current_user.is_authenticated` short-circuited
        before anything was read. Now the row is looked up first, so a caller
        learns the reply does not exist -- which is not a disclosure, since a reply
        id that does not exist tells nobody anything."""
        logout(env.client)

        response = env.client.post(f'/post_reply/999999/{path}')

        assert response.status_code == 404


class TestWhoMayChooseAnAnswer:
    """The rule as it stands, pinned rather than changed."""

    def test_an_anonymous_caller_is_refused(self, env):
        post, reply = a_question_and_answer(env)
        logout(env.client)

        response = env.client.post(f'/post_reply/{reply.id}/choose_answer')

        assert response.status_code == 403
        db.session.refresh(reply)
        assert reply.answer is False

    def test_an_unrelated_user_is_refused(self, env):
        post, reply = a_question_and_answer(env)
        login(env.client, env.baseline.user4)

        response = env.client.post(f'/post_reply/{reply.id}/choose_answer')

        assert response.status_code == 403
        db.session.refresh(reply)
        assert reply.answer is False

    def test_an_admin_may(self, env):
        post, reply = a_question_and_answer(env)
        login(env.client, env.baseline.user1)

        response = env.client.post(f'/post_reply/{reply.id}/choose_answer')

        assert response.status_code == 200
        db.session.refresh(reply)
        assert reply.answer is True

    def test_a_moderator_of_the_community_may(self, env):
        post, reply = a_question_and_answer(env)
        moderator = a_moderator(env, reply.community_id)
        login(env.client, moderator)

        response = env.client.post(f'/post_reply/{reply.id}/choose_answer')

        assert response.status_code == 200
        db.session.refresh(reply)
        assert reply.answer is True

    def test_the_replys_own_author_may(self, env):
        """Worth reading twice: the person who WROTE the comment may mark it as
        the accepted answer to somebody else's question, and the notification
        `choose_answer` sends says "Your answer was chosen as an answer to ...".

        `post_reply_mark_as_answer` in the API applies the same rule, so this is
        the product's policy in two places rather than a slip in one. Asserted as
        it is; a change to it should be somebody's decision, not a side effect.
        """
        post, reply = a_question_and_answer(env)
        assert reply.user_id != post.user_id
        login(env.client, db.session.get(User, reply.user_id))

        response = env.client.post(f'/post_reply/{reply.id}/choose_answer')

        assert response.status_code == 200
        db.session.refresh(reply)
        assert reply.answer is True

    def test_the_asker_is_refused_unless_they_moderate(self, env):
        """The other half of the same surprise: the person whose question it is
        has no say, unless they happen to be a moderator or an admin."""
        post, reply = a_question_and_answer(env)
        login(env.client, db.session.get(User, post.user_id))

        response = env.client.post(f'/post_reply/{reply.id}/choose_answer')

        assert response.status_code == 403
        db.session.refresh(reply)
        assert reply.answer is False


class TestUnchoosing:
    def test_an_admin_may_unchoose(self, env):
        post, reply = a_question_and_answer(env)
        reply.answer = True
        db.session.commit()
        login(env.client, env.baseline.user1)

        response = env.client.post(f'/post_reply/{reply.id}/unchoose_answer')

        assert response.status_code == 200
        db.session.refresh(reply)
        assert reply.answer is False

    def test_an_unrelated_user_may_not(self, env):
        post, reply = a_question_and_answer(env)
        reply.answer = True
        db.session.commit()
        login(env.client, env.baseline.user4)

        response = env.client.post(f'/post_reply/{reply.id}/unchoose_answer')

        assert response.status_code == 403
        db.session.refresh(reply)
        assert reply.answer is True

    def test_unchoosing_something_that_was_never_chosen(self, env):
        """Idempotent, and asserted because nothing checks the current value."""
        post, reply = a_question_and_answer(env)
        login(env.client, env.baseline.user1)

        response = env.client.post(f'/post_reply/{reply.id}/unchoose_answer')

        assert response.status_code == 200
        db.session.refresh(reply)
        assert reply.answer is False


class TestWhatChoosingSends:
    def test_the_replys_author_is_notified(self, env):
        from app.models import Notification

        post, reply = a_question_and_answer(env)
        login(env.client, env.baseline.user1)

        env.client.post(f'/post_reply/{reply.id}/choose_answer')

        notification = Notification.query.filter_by(user_id=reply.user_id).order_by(
            Notification.id.desc()).first()
        assert notification is not None
        assert 'a question' in notification.title

    def test_their_unread_count_goes_up(self, env):
        post, reply = a_question_and_answer(env)
        author = db.session.get(User, reply.user_id)
        before = author.unread_notifications
        login(env.client, env.baseline.user1)

        env.client.post(f'/post_reply/{reply.id}/choose_answer')

        db.session.refresh(author)
        assert author.unread_notifications == before + 1
