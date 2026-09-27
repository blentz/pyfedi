"""The site's public RSS feed: `index_rss` in `app/main/routes.py`.

No test had ever requested it. It is served to anonymous callers, it authenticates
with a pre-issued token in the query string, and `feed_type=subscribed` reads the
caller's PRIVATE community memberships, so it is three access checks in one route.

D1356. The token was matched on its own -- `User.query.filter(User.rss_token ==
rss_token.strip()).first()` -- so an account that had been banned or deleted kept a
working feed, including the posts of the private communities it belonged to.
Measured before the repair, with `deleted = True` and then `banned = True`: the feed
was still built as that user.

`authorise_api_user` in `app/utils.py` already refuses a JWT from a remote,
unverified, banned or deleted account. Three of those four now apply here too;
`verified` deliberately does not, because an instance with email verification off
has legitimate accounts with `verified = False`.
"""
import pytest
from flask import g

from app import db
from app.models import Community, CommunityMember, Post, Site, User, utcnow


@pytest.fixture
def env(app, api_baseline):
    """A public instance -- `api_baseline`'s Site is private, and a private
    instance aborts before any of this is reached."""
    from types import SimpleNamespace

    g.admin_ids = []
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.site = site
    db.session.commit()
    return SimpleNamespace(baseline=api_baseline, site=site,
                           client=app.test_client())


def a_token(user, token='r' * 20):
    user.rss_token = token
    db.session.commit()
    return token


class TestTheFeedAnyoneCanRead:
    def test_it_answers_without_a_token(self, env):
        response = env.client.get('/index/feed')

        assert response.status_code == 200
        assert response.headers['Content-Type'].startswith('application/rss+xml')

    def test_the_feed_type_defaults_to_local(self, env):
        response = env.client.get('/index/feed')

        assert '<title>Test Site - Local</title>' in response.text

    @pytest.mark.parametrize('feed_type, shown', [('local', 'Local'),
                                                  ('popular', 'Popular'),
                                                  ('all', 'All'),
                                                  ('subscribed', 'Subscribed')])
    def test_each_feed_type_names_itself(self, env, feed_type, shown):
        response = env.client.get(f'/index/feed/{feed_type}')

        assert response.status_code == 200
        assert f'<title>Test Site - {shown}</title>' in response.text

    def test_an_unknown_feed_type_is_answered_as_all(self, env):
        """It falls off the end of the if/elif chain with `community_ids` still at
        its initial `[-1]`, which is the 'All' sentinel. Asserted rather than
        assumed: a reader guessing a feed name gets the public All feed, not an
        error and not somebody's private one."""
        response = env.client.get('/index/feed/FOO')

        assert response.status_code == 200
        assert '<title>Test Site - Foo</title>' in response.text

    def test_a_feed_type_that_looks_like_markup_is_escaped(self, env):
        """`feed_type` comes from the URL path and reaches the channel title."""
        response = env.client.get('/index/feed/%3Cb%3Ex%3C/b%3E')

        assert '<b>x</b></title>' not in response.text

    def test_the_etag_is_offered(self, env):
        response = env.client.get('/index/feed')

        assert response.headers['ETag'] == f'home_{hash(env.site.last_active)}'

    def test_a_matching_etag_gets_a_304(self, env):
        etag = env.client.get('/index/feed').headers['ETag']

        response = env.client.get('/index/feed', headers={'If-None-Match': etag})

        assert response.status_code == 304


class TestAPrivateInstance:
    """The refusal comes BEFORE the 304, which is what an earlier round repaired
    here: a caller holding an ETag from before the instance was made private --
    or one guessed, since it is `home_{hash(last_active)}` -- used to get 304
    where a fresh request got 404, and a 304 is an answer.
    """

    @pytest.fixture
    def private(self, env):
        env.site.private_instance = True
        db.session.commit()
        return env

    def test_the_feed_is_refused(self, private):
        assert private.client.get('/index/feed').status_code == 404

    def test_a_matching_etag_is_refused_too(self, env):
        etag = env.client.get('/index/feed').headers['ETag']
        env.site.private_instance = True
        db.session.commit()

        response = env.client.get('/index/feed', headers={'If-None-Match': etag})

        assert response.status_code == 404

    def test_a_token_does_not_open_a_private_instance(self, private):
        token = a_token(private.baseline.user1)

        assert private.client.get(
            f'/index/feed/subscribed?token={token}').status_code == 404


class TestTheTokenInTheQueryString:
    """D1356. `feed_type=subscribed` is the arm the token unlocks, so it is the one
    asserted: an anonymous caller gets `community_ids = [-1]` and the All feed,
    while an authenticated one gets their own memberships.
    """

    def subscribed(self, env, token=None):
        url = '/index/feed/subscribed'
        if token is not None:
            url += f'?token={token}'
        return env.client.get(url)

    def a_post_in_a_community_they_joined(self, env):
        """`api_baseline` makes user1 a member of community1, which is REMOTE.

        That is the discriminator this route needs: the `subscribed` arm reads the
        caller's memberships, and an unauthenticated caller falls through to the
        `local` arm, which is `instance_id = 1`. So a post in a remote community
        the user belongs to is visible with the token and invisible without it,
        with no private-community plumbing to arrange.
        """
        post = Post(user_id=env.baseline.user2.id,
                    community_id=env.baseline.community1.id,
                    title='a post from a community they joined', type=0,
                    posted_at=utcnow(), last_active=utcnow(),
                    ap_id='https://remote.piefed.test/p/9001', deleted=False,
                    status=1, from_bot=False, nsfw=False, nsfl=False, sticky=False,
                    indexable=True, microblog=False, body_html='<p>x</p>')
        db.session.add(post)
        for member in CommunityMember.query.filter_by(
                user_id=env.baseline.user1.id):
            member.is_banned = False
        db.session.commit()
        return post

    def test_a_good_token_authenticates(self, env):
        token = a_token(env.baseline.user1)

        response = self.subscribed(env, token)

        assert response.status_code == 200
        assert '<title>Test Site - Subscribed</title>' in response.text

    def test_surrounding_whitespace_is_stripped(self, env):
        """Asserted on the CONTENT, not the status: an unauthenticated request for
        this feed is also a 200, so a status assertion cannot tell a stripped token
        from an ignored one."""
        token = a_token(env.baseline.user1)
        self.a_post_in_a_community_they_joined(env)

        assert self.TITLE in self.subscribed(env, f'%20{token}%20').text

    def test_a_token_nobody_holds_is_anonymous(self, env):
        a_token(env.baseline.user1)

        response = self.subscribed(env, 'z' * 20)

        assert response.status_code == 200

    TITLE = 'a post from a community they joined'

    OTHER_TITLE = 'a post from a community they never joined'

    def a_post_in_a_community_they_did_not_join(self, env):
        """`api_baseline` puts user2, not user1, in community2 -- so this post is
        the difference between the `subscribed` feed and the `all` feed, and
        without it nothing separates an arm that reads the memberships from one
        that falls through to All."""
        post = Post(user_id=env.baseline.user2.id,
                    community_id=env.baseline.community2.id,
                    title=self.OTHER_TITLE, type=0, posted_at=utcnow(),
                    last_active=utcnow(), ap_id='https://remote.piefed.test/p/9002',
                    deleted=False, status=1, from_bot=False, nsfw=False, nsfl=False,
                    sticky=False, indexable=True, microblog=False,
                    body_html='<p>y</p>')
        db.session.add(post)
        db.session.commit()
        return post

    def test_the_subscribed_feed_holds_only_their_own_communities(self, env):
        token = a_token(env.baseline.user1)
        self.a_post_in_a_community_they_joined(env)
        self.a_post_in_a_community_they_did_not_join(env)

        body = self.subscribed(env, token).text

        assert self.TITLE in body
        assert self.OTHER_TITLE not in body

    def test_the_all_feed_holds_both(self, env):
        """The other side, so the assertion above is known to be about the
        membership rather than about the post being invisible everywhere.

        It needs the token: the `local` arm's condition is `feed_type == 'local' or
        not current_user_is_authenticated`, so an ANONYMOUS request for `all` takes
        the local arm and sees neither of these remote posts. That is asserted
        separately below -- it is the route's existing behaviour and it is the
        conservative direction, so it is pinned rather than changed.
        """
        token = a_token(env.baseline.user1)
        self.a_post_in_a_community_they_joined(env)
        self.a_post_in_a_community_they_did_not_join(env)

        body = env.client.get(f'/index/feed/all?token={token}').text

        assert self.TITLE in body and self.OTHER_TITLE in body

    def test_an_anonymous_all_feed_is_the_local_one(self, env):
        """`not current_user_is_authenticated` is the second half of the `local`
        arm's condition, so every feed_type an anonymous caller asks for that is
        not `popular` is answered from local communities only."""
        self.a_post_in_a_community_they_joined(env)
        self.a_post_in_a_community_they_did_not_join(env)

        body = env.client.get('/index/feed/all').text

        assert self.TITLE not in body and self.OTHER_TITLE not in body

    def test_the_token_is_what_reaches_their_communities(self, env):
        """The control for the two tests below: without it they could not tell a
        revoked token from a feed that never showed this post anyway."""
        token = a_token(env.baseline.user1)
        self.a_post_in_a_community_they_joined(env)

        assert self.TITLE in self.subscribed(env, token).text
        assert self.TITLE not in self.subscribed(env, None).text

    def test_a_deleted_account_no_longer_authenticates(self, env):
        """The defect. A deleted account's token kept working, and with it the
        communities that account belonged to."""
        token = a_token(env.baseline.user1)
        self.a_post_in_a_community_they_joined(env)

        env.baseline.user1.deleted = True
        db.session.commit()

        assert self.TITLE not in self.subscribed(env, token).text

    def test_a_banned_account_no_longer_authenticates(self, env):
        token = a_token(env.baseline.user1)
        self.a_post_in_a_community_they_joined(env)

        env.baseline.user1.banned = True
        db.session.commit()

        assert self.TITLE not in self.subscribed(env, token).text

    def test_a_remote_account_does_not_authenticate(self, env):
        """`authorise_api_user` refuses a remote actor for the same reason: a
        credential of ours belongs to a local account."""
        user = env.baseline.user1
        token = a_token(user)
        self.a_post_in_a_community_they_joined(env)
        user.ap_id = 'someone@remote.test'
        db.session.commit()

        assert self.TITLE not in self.subscribed(env, token).text

    def test_the_refusal_is_not_a_failure(self, env):
        """A stale token falls back to the anonymous feed rather than erroring:
        a feed reader holding a revoked token should stop seeing private posts,
        not start seeing 500s."""
        token = a_token(env.baseline.user1)
        env.baseline.user1.deleted = True
        db.session.commit()

        response = self.subscribed(env, token)

        assert response.status_code == 200

    def test_an_empty_token_is_anonymous(self, env):
        a_token(env.baseline.user1)

        assert self.subscribed(env, '').status_code == 200

    def test_a_user_with_no_token_is_not_matched_by_an_empty_one(self, env):
        """`rss_token` is NULL until the front page sets one, and `'' == NULL` is
        not a match in SQL -- asserted so the walrus guard's falsy arm is known to
        be doing the work rather than the comparison."""
        env.baseline.user1.rss_token = None
        db.session.commit()

        assert self.subscribed(env, '').status_code == 200


class TestWhatTheEntriesCarry:
    def a_local_post(self, env, title='a local post'):
        community = db.session.get(Community, env.baseline.community1.id)
        community.instance_id = 1
        community.private = False
        post = Post(user_id=env.baseline.user2.id, community_id=community.id,
                    title=title, type=0, posted_at=utcnow(), last_active=utcnow(),
                    ap_id=None, deleted=False, status=1, from_bot=False,
                    nsfw=False, nsfl=False, sticky=False, indexable=True,
                    microblog=False, body_html='<p>the body</p>')
        db.session.add(post)
        db.session.commit()
        return post

    def test_a_local_post_appears(self, env):
        self.a_local_post(env)

        response = env.client.get('/index/feed/local')

        assert 'a local post' in response.text

    def test_the_entry_carries_the_body(self, env):
        """Not the author name: `fe.author(name=...)` is set, and feedgen's RSS
        serialiser writes `<author>` only from an email address, so the name does
        not appear in the document. Measured, not assumed."""
        post = self.a_local_post(env)

        response = env.client.get('/index/feed/local')

        assert 'the body' in response.text

    def test_the_entry_links_to_the_post(self, env):
        post = self.a_local_post(env)

        response = env.client.get('/index/feed/local')

        assert f'/post/{post.id}' in response.text or post.slug in response.text
