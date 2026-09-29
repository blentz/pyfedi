"""Round 232: the vote-ring finder, and what the front page defaults to.

Two clusters left in `app/main/routes.py`.

`/find_voters` is an admin tool for spotting coordinated downvoting: it walks the 5000
most recently seen accounts, keeps the ones with more than ten recent downvotes, and
reports the accounts whose downvote lists are IDENTICAL. Nothing exercised it, which for a
moderation tool means nobody had checked that it groups the right accounts -- an answer
that named the wrong people would be acted on.

The front page's defaults are the other cluster: three lines that decide what a visitor
with no preferences sees, including the one that stops an anonymous visitor being given
the `subscribed` filter, which they cannot use.
"""
from types import SimpleNamespace

import pytest
from flask import g

from app import db
from app.models import Site
from tests.factories import (grant_permission, make_community, make_community_member,
                             make_post, make_post_vote, make_user)


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.site = site
    return SimpleNamespace(app=app, site=site, baseline=api_baseline,
                           anonymous=app.test_client())


def signed_in_client(app, user):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return client


# --------------------------------------------------------------------------
# /find_voters
# --------------------------------------------------------------------------


class TestTheVoteRingFinder:

    @pytest.fixture
    def seeded(self, env):
        admin = make_user(env.baseline.instance_local, 'voteadmin', local=True)
        admin.verified = True
        admin.private_key = 'x'
        grant_permission(admin, 'change instance settings')
        community = make_community('votingland')
        author = make_user(env.baseline.instance_local, 'voteauthor', local=True)
        make_community_member(author, community)
        db.session.commit()
        posts = [make_post(community, author, ap_id=f'https://test.piefed.local/v/{n}',
                           title=f'post {n}') for n in range(12)]
        db.session.commit()
        env.admin = admin
        env.client = signed_in_client(env.app, admin)
        env.posts = posts
        env.community = community
        return env

    def downvoter(self, env, name, posts):
        user = make_user(env.baseline.instance_local, name, local=True)
        db.session.commit()
        for post in posts:
            make_post_vote(user, post, -1.0)
        return user

    def test_two_accounts_downvoting_the_same_posts_are_reported_together(self, seeded):
        """What the tool is for. Eleven shared downvotes clears the `> 10` threshold for
        both accounts, and identical lists put them under one key -- so the answer names
        both ids, which is the claim a moderator would act on."""
        first = self.downvoter(seeded, 'ringmember1', seeded.posts[:11])
        second = self.downvoter(seeded, 'ringmember2', seeded.posts[:11])

        body = seeded.client.get('/find_voters').get_data(as_text=True)

        assert str(first.id) in body
        assert str(second.id) in body

    def test_accounts_downvoting_different_posts_are_not_reported(self, seeded):
        """`len(keys) > 1` is the filter that makes this a report about COINCIDENCE rather
        than a list of everyone who downvotes a lot. Both accounts below are over the
        threshold; only their overlap is missing."""
        lonely = self.downvoter(seeded, 'lonelyvoter', seeded.posts[:11])
        other = self.downvoter(seeded, 'othervoter', seeded.posts[1:12])

        body = seeded.client.get('/find_voters').get_data(as_text=True)

        assert str(lonely.id) not in body
        assert str(other.id) not in body

    def test_an_account_with_ten_downvotes_is_below_the_threshold(self, seeded):
        """`> 10`, asserted from both sides in one row: two accounts with ten identical
        downvotes are not reported, and the same two with eleven are."""
        first = self.downvoter(seeded, 'tenvoter1', seeded.posts[:10])
        second = self.downvoter(seeded, 'tenvoter2', seeded.posts[:10])

        below = seeded.client.get('/find_voters').get_data(as_text=True)

        make_post_vote(first, seeded.posts[10], -1.0)
        make_post_vote(second, seeded.posts[10], -1.0)

        above = seeded.client.get('/find_voters').get_data(as_text=True)

        assert str(first.id) not in below
        assert str(first.id) in above
        assert str(second.id) in above

    def test_upvotes_are_not_counted(self, seeded):
        """`recently_downvoted_posts` filters `effect < 0`. A pair of accounts that upvote
        the same eleven posts is a pair of people who like the same things, and reporting
        them as a ring is the mistake that costs somebody their account."""
        first = make_user(seeded.baseline.instance_local, 'fanclub1', local=True)
        second = make_user(seeded.baseline.instance_local, 'fanclub2', local=True)
        db.session.commit()
        for post in seeded.posts[:11]:
            make_post_vote(first, post, 1.0)
            make_post_vote(second, post, 1.0)

        body = seeded.client.get('/find_voters').get_data(as_text=True)

        assert str(first.id) not in body
        assert str(second.id) not in body

    def test_three_accounts_with_one_list_are_reported_as_one_group(self, seeded):
        """The `append` arm of `find_duplicate_values`, which is only reached by the THIRD
        key sharing a value -- the first creates the list and the second replaces the
        `if`."""
        ids = [self.downvoter(seeded, f'trio{n}', seeded.posts[:11]).id for n in range(3)]

        body = seeded.client.get('/find_voters').get_data(as_text=True)

        for user_id in ids:
            assert str(user_id) in body

    def test_an_ordinary_account_is_refused(self, seeded):
        """`@permission_required('change instance settings')`, which redirects to
        `auth.permission_denied` rather than aborting 403. The answer lists account ids and
        their downvoting behaviour, so it is a report about other people."""
        first = self.downvoter(seeded, 'ringmember1', seeded.posts[:11])
        second = self.downvoter(seeded, 'ringmember2', seeded.posts[:11])
        ordinary = make_user(seeded.baseline.instance_local, 'nosytester', local=True)
        ordinary.verified = True
        db.session.commit()

        response = signed_in_client(seeded.app, ordinary).get('/find_voters')

        assert response.status_code == 302
        assert 'permission_denied' in response.headers['Location']
        # The refusal is what withholds the report, so the row checks the report is not in
        # the answer either -- a redirect that still carried the body would pass on status
        # alone.
        assert str(first.id) not in response.get_data(as_text=True)
        assert str(second.id) not in response.get_data(as_text=True)

    def test_a_signed_out_visitor_is_sent_to_the_login_page(self, seeded):
        response = seeded.anonymous.get('/find_voters')

        assert response.status_code == 302
        assert '/auth/login' in response.headers['Location']


# --------------------------------------------------------------------------
# The front page's defaults
# --------------------------------------------------------------------------


class TestWhatTheFrontPageDefaultsTo:

    def test_an_anonymous_visitor_is_never_given_the_subscribed_filter(self, env):
        """`:70`. An instance whose `default_filter` is `subscribed` would otherwise hand
        an anonymous visitor a filter meaning "communities I have joined", which they have
        none of. Without the coercion the request does not fail -- it falls through to
        `elif view_filter == 'all' or current_user.is_anonymous:` at the end of the chain
        and quietly becomes the ALL feed, so the difference is which posts appear, not
        whether the page loads.

        `unpopular` has `show_popular` false, so it is in All and not in Popular: that is
        the assertion that tells the two apart.
        """
        env.site.default_filter = 'subscribed'
        author = make_user(env.baseline.instance_local, 'filterauthor', local=True)
        popular = make_community('everyoneshere')
        popular.show_popular = True
        unpopular = make_community('nobodyshere')
        unpopular.show_popular = False
        make_community_member(author, popular)
        db.session.commit()
        make_post(popular, author, ap_id='https://test.piefed.local/pf/1',
                  title='a popular post')
        make_post(unpopular, author, ap_id='https://test.piefed.local/pf/2',
                  title='an unpopular post')
        db.session.commit()

        response = env.anonymous.get('/')

        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert 'a popular post' in body
        assert 'an unpopular post' not in body

    def test_a_site_with_no_default_filter_falls_back_by_viewer(self, env):
        """`:72`. `Site.default_filter` is nullable, so a fresh instance has none -- the
        fallback is `popular` for a visitor and `subscribed` for an account."""
        env.site.default_filter = None
        db.session.commit()
        reader = make_user(env.baseline.instance_local, 'defaultreader', local=True)
        reader.verified = True
        reader.default_filter = None
        db.session.commit()

        anonymous = env.anonymous.get('/')
        signed_in = signed_in_client(env.app, reader).get('/')

        assert anonymous.status_code == 200
        assert signed_in.status_code == 200

    @pytest.fixture
    def a_long_list(self, env):
        author = make_user(env.baseline.instance_local, 'listauthor', local=True)
        community = make_community('longlist')
        make_community_member(author, community)
        db.session.commit()
        for n in range(8):
            make_post(community, author, ap_id=f'https://test.piefed.local/l/{n}',
                      title=f'listed post {n}')
        db.session.commit()
        return env

    def _shown(self, body):
        return [n for n in range(8) if f'listed post {n}' in body]

    def test_a_shorter_page_length_preference_is_honoured(self, a_long_list):
        """`:94`. The feed is asked for explicitly -- `/home/hot/all` -- because an
        account's default filter is `subscribed`, and a reader who has joined nothing sees
        no posts at all, which would satisfy any assertion about a SHORTER page."""
        reader = make_user(a_long_list.baseline.instance_local, 'shortpages', local=True)
        reader.verified = True
        reader.page_length = 5
        db.session.commit()

        body = signed_in_client(a_long_list.app, reader).get(
            '/home/hot/all').get_data(as_text=True)

        assert len(self._shown(body)) == 5

    def test_the_preference_cannot_lengthen_the_page(self, a_long_list, monkeypatch):
        """The `<` half. The preference is a ceiling the READER lowers, never one they
        raise: with the instance set to three posts a page, an account asking for five
        still gets three."""
        monkeypatch.setitem(a_long_list.app.config, 'PAGE_LENGTH', 3)
        reader = make_user(a_long_list.baseline.instance_local, 'greedypages', local=True)
        reader.verified = True
        reader.page_length = 5
        db.session.commit()

        body = signed_in_client(a_long_list.app, reader).get(
            '/home/hot/all').get_data(as_text=True)

        assert len(self._shown(body)) == 3

    def test_an_account_with_no_preference_gets_the_instance_page_length(self,
                                                                        a_long_list,
                                                                        monkeypatch):
        """The `current_user.page_length and` half: the column is nullable, and `None < 20`
        is a TypeError, so the guard is what keeps a fresh account off a 500."""
        monkeypatch.setitem(a_long_list.app.config, 'PAGE_LENGTH', 4)
        reader = make_user(a_long_list.baseline.instance_local, 'nopreference', local=True)
        reader.verified = True
        reader.page_length = None
        db.session.commit()

        body = signed_in_client(a_long_list.app, reader).get(
            '/home/hot/all').get_data(as_text=True)

        assert len(self._shown(body)) == 4

    def test_the_fragment_view_returns_only_the_posts(self, env):
        """`:199`. `?fragment=` is what the infinite-scroll script asks for, so it must be
        the post list WITHOUT the page around it -- a fragment that carried the whole
        document would be appended to the page the reader is already on."""
        community = make_community('fragmentland')
        author = make_user(env.baseline.instance_local, 'fragmentauthor', local=True)
        make_community_member(author, community)
        db.session.commit()
        make_post(community, author, ap_id='https://test.piefed.local/f/1',
                  title='a fragment post')
        db.session.commit()

        full = env.anonymous.get('/')
        fragment = env.anonymous.get('/?fragment=1')

        assert fragment.status_code == 200
        assert 'a fragment post' in fragment.get_data(as_text=True)
        assert '<!doctype html>' not in fragment.get_data(as_text=True).lower()
        assert '<!doctype html>' in full.get_data(as_text=True).lower()


class TestTheVerificationWarning:
    """`verification_warning()` runs at the top of the front page and the community list.
    It is the only thing that tells an account its email is still unconfirmed, and it is
    driven by `verified is False` -- an identity test, so a NULL `verified` says nothing.
    """

    def test_an_unverified_account_is_told_to_check_its_inbox(self, env):
        reader = make_user(env.baseline.instance_local, 'unverified', local=True)
        reader.verified = False
        db.session.commit()

        body = signed_in_client(env.app, reader).get('/communities').get_data(as_text=True)

        assert 'verify your account' in body

    def test_a_verified_account_is_not(self, env):
        reader = make_user(env.baseline.instance_local, 'verified', local=True)
        reader.verified = True
        db.session.commit()

        body = signed_in_client(env.app, reader).get('/communities').get_data(as_text=True)

        assert 'verify your account' not in body

    def test_an_anonymous_visitor_is_not_either(self, env):
        """`hasattr(current_user, 'verified')` is what keeps this off the anonymous page:
        `AnonymousUserMixin` has no such attribute, so the warning cannot be shown to
        somebody with no account to verify."""
        body = env.anonymous.get('/communities').get_data(as_text=True)

        assert 'verify your account' not in body
