"""The four admin community listings.

`admin_communities` and its three filtered siblings -- `/communities/no-topic`,
`/communities/low-quality`, `/communities/un-moderated` -- in `app/admin/routes.py`.
None of them had been requested by a test.

They are near-copies, and two differences between them are behaviour worth recording
rather than defects:

  * the main listing searches `title OR ap_id`; the three filtered ones search `title`
    only, so looking for a community by its domain works on one page and finds nothing
    on the other three;
  * every one of them computes `prev_url` with `communities.has_prev and page != 1`,
    and `has_prev` is already False on page 1, so the second test cannot change the
    answer. A mutant dropping it survives, which is why none is written for it.

The sort parameter goes through `safe_order_by` with an explicit allowlist, which has
its own tests in `tests/test_safe_order_by.py`; what this file adds is that the admin
route passes the allowlist rather than the user's string.
"""
import pytest
from flask import g

from app import db
from app.models import Community, Site
from tests.factories import make_community

LISTINGS = ['/admin/communities', '/admin/communities/no-topic',
            '/admin/communities/low-quality', '/admin/communities/un-moderated']


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = [api_baseline.user1.id]
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.site = site
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(api_baseline.user1.id)
        session['_fresh'] = True
    return SimpleNamespace(baseline=api_baseline, client=client, app=app)


def csrf(app, client):
    """A real CSRF token in the session and in the form.

    The GET tests above need none; every POST does, and without it the answer is a
    400 -- which is what six of the rename tests below first asserted their way
    around.
    """
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf

    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as session:
        session['csrf_token'] = raw
    return token


def a_community(name, **columns):
    community = make_community(name)
    for key, value in columns.items():
        setattr(community, key, value)
    db.session.commit()
    return community


class TestTheyAnswerAtAll:
    @pytest.mark.parametrize('path', LISTINGS)
    def test_an_admin_gets_the_page(self, env, path):
        assert env.client.get(path).status_code == 200

    @pytest.mark.parametrize('path', LISTINGS)
    def test_an_ordinary_user_is_refused(self, env, path):
        """`permission_required` redirects rather than aborting."""
        with env.client.session_transaction() as session:
            session['_user_id'] = str(env.baseline.user3.id)

        response = env.client.get(path)

        assert response.status_code == 302
        assert 'permission_denied' in response.headers['Location']

    @pytest.mark.parametrize('path', LISTINGS)
    def test_an_anonymous_visitor_is_refused(self, env, path):
        with env.client.session_transaction() as session:
            session.clear()

        assert env.client.get(path).status_code == 302


class TestWhichCommunitiesEachListingHolds:
    def test_the_main_listing_holds_them_all(self, env):
        a_community('ordinaryone', topic_id=None, low_quality=False,
                    un_moderated=False)

        body = env.client.get('/admin/communities').text

        assert 'ordinaryone' in body

    def test_no_topic_holds_only_those_with_none(self, env):
        from app.models import Topic

        topic = Topic(name='a topic', machine_name='a-topic', num_communities=1)
        db.session.add(topic)
        db.session.commit()
        # names chosen NOT to be substrings of one another: 'topiced' is inside
        # 'untopiced', so the first version of this test could not fail
        a_community('withouttopic', topic_id=None)
        a_community('hasatopic', topic_id=topic.id)

        body = env.client.get('/admin/communities/no-topic').text

        assert 'withouttopic' in body
        assert 'hasatopic' not in body

    def test_low_quality_holds_only_those_flagged(self, env):
        a_community('lowone', low_quality=True)
        a_community('fineone', low_quality=False)

        body = env.client.get('/admin/communities/low-quality').text

        assert 'lowone' in body
        assert 'fineone' not in body

    def test_unmoderated_holds_only_those_flagged(self, env):
        a_community('nobodywatches', un_moderated=True)
        a_community('haswatchers', un_moderated=False)

        body = env.client.get('/admin/communities/un-moderated').text

        assert 'nobodywatches' in body
        assert 'haswatchers' not in body


class TestTheSearchBox:
    @pytest.mark.parametrize('path', LISTINGS)
    def test_a_search_narrows_the_list(self, env, path):
        wanted = a_community('wantedcommunity', topic_id=None, low_quality=True,
                             un_moderated=True)
        a_community('othercommunity', topic_id=None, low_quality=True,
                    un_moderated=True)

        body = env.client.get(path, query_string={'search': 'wanted'}).text

        assert 'wantedcommunity' in body
        assert 'othercommunity' not in body

    @pytest.mark.parametrize('path', LISTINGS)
    def test_an_empty_search_shows_everything(self, env, path):
        a_community('shownanyway', topic_id=None, low_quality=True,
                    un_moderated=True)

        body = env.client.get(path, query_string={'search': ''}).text

        assert 'shownanyway' in body

    @pytest.mark.parametrize('path', LISTINGS)
    def test_a_search_matching_nothing(self, env, path):
        a_community('present', topic_id=None, low_quality=True, un_moderated=True)

        body = env.client.get(path, query_string={'search': 'zzzznomatch'}).text

        assert 'present' not in body

    def test_the_search_matches_a_title_case_insensitively(self, env):
        community = a_community('casetest')
        community.title = 'MixedCase Title'
        db.session.commit()

        body = env.client.get('/admin/communities',
                              query_string={'search': 'mixedcase'}).text

        assert 'MixedCase Title' in body

    def test_the_main_listing_also_searches_the_ap_id(self, env):
        """`or_(title.ilike, ap_id.ilike)` -- searching by domain works here."""
        community = a_community('domainsearch')
        community.title = 'Nothing Like The Domain'
        community.ap_id = 'domainsearch@findme.example'
        db.session.commit()

        body = env.client.get('/admin/communities',
                              query_string={'search': 'findme.example'}).text

        assert 'Nothing Like The Domain' in body

    def test_the_filtered_listings_do_not_search_the_ap_id(self, env):
        """Observed behaviour, not a defect claim: the three filtered listings search
        the title alone, so a domain finds nothing there. Asserted so that making them
        consistent is a deliberate change rather than an accident."""
        community = a_community('domainsearch2', low_quality=True)
        community.title = 'Nothing Like The Domain Either'
        community.ap_id = 'domainsearch2@findme2.example'
        db.session.commit()

        body = env.client.get('/admin/communities/low-quality',
                              query_string={'search': 'findme2.example'}).text

        assert 'Nothing Like The Domain Either' not in body


class TestTheSortParameter:
    """Only the main listing takes one; the three siblings order by post_count."""

    @pytest.mark.parametrize('sort_by', ['title ASC', 'title DESC',
                                         'post_count DESC', 'last_active ASC',
                                         'subscriptions_count DESC'])
    def test_an_allowed_sort(self, env, sort_by):
        a_community('sortable')

        response = env.client.get('/admin/communities',
                                  query_string={'sort_by': sort_by})

        assert response.status_code == 200
        assert 'sortable' in response.text

    @pytest.mark.parametrize('sort_by', [
        'id',                                  # a real column, not allowed
        'title; DROP TABLE community',         # the injection the allowlist exists for
        'nonexistent_column DESC',
        '',
        'title SIDEWAYS',                      # an unknown direction
    ])
    def test_a_sort_the_allowlist_refuses_still_answers(self, env, sort_by):
        """`safe_order_by` falls back rather than raising, so the page still renders --
        which is why an admin cannot tell a rejected sort from an accepted one, and why
        the allowlist has to be right."""
        a_community('stillhere')

        response = env.client.get('/admin/communities',
                                  query_string={'sort_by': sort_by})

        assert response.status_code == 200
        assert 'stillhere' in response.text

    ALLOWED = ['title', 'topic_id', 'subscriptions_count', 'show_popular',
               'show_all', 'post_count', 'content_retention', 'nsfw',
               'post_reply_count', 'last_active']

    def two_communities_differing_in_everything(self, env):
        """One community low in every sortable column and one high, so ASC and DESC
        can be told apart for each field in the allowlist.

        This is what the allowlist test needs and a status assertion does not give:
        `safe_order_by` falls back to a default for a field it refuses, so a rejected
        sort renders a 200 with the same rows. Only the ORDER distinguishes them --
        the mutant that dropped `last_active` from the allowlist survived until this
        existed.
        """
        from datetime import datetime

        from app.models import Topic

        low_topic = Topic(name='low topic', machine_name='low-topic',
                          num_communities=1)
        high_topic = Topic(name='high topic', machine_name='high-topic',
                           num_communities=1)
        db.session.add_all([low_topic, high_topic])
        db.session.commit()

        low = a_community('sortlow')
        high = a_community('sorthigh')
        for community, topic, number, flag, when in (
                (low, low_topic, 1, False, datetime(2020, 1, 1)),
                (high, high_topic, 99, True, datetime(2026, 1, 1))):
            community.title = 'AAA low' if community is low else 'ZZZ high'
            community.topic_id = topic.id
            community.subscriptions_count = number
            community.post_count = number
            community.post_reply_count = number
            community.content_retention = number
            community.show_popular = flag
            community.show_all = flag
            community.nsfw = flag
            community.last_active = when
        db.session.commit()
        # every other community in the fixtures must sort between or outside these two
        for other in Community.query.filter(Community.id.notin_([low.id, high.id])):
            other.title = 'MMM other'
            other.topic_id = None
            other.subscriptions_count = 50
            other.post_count = 50
            other.post_reply_count = 50
            other.content_retention = 50
            other.last_active = datetime(2023, 1, 1)
        db.session.commit()
        return low, high

    @pytest.mark.parametrize('field', ALLOWED)
    def test_each_allowed_field_really_sorts(self, env, field):
        low, high = self.two_communities_differing_in_everything(env)

        ascending = env.client.get(
            '/admin/communities', query_string={'sort_by': f'{field} ASC'}).text
        descending = env.client.get(
            '/admin/communities', query_string={'sort_by': f'{field} DESC'}).text

        assert ascending.index('sortlow') < ascending.index('sorthigh'), field
        assert descending.index('sorthigh') < descending.index('sortlow'), field

    @pytest.mark.parametrize('sort_by', ['id DESC', 'id ASC',
                                         'nonexistent_column ASC'])
    def test_a_refused_sort_falls_back_to_the_same_order(self, env, sort_by):
        """`safe_order_by`'s else arm is `desc(...)` of the alphabetically FIRST
        allowed field -- `content_retention` for this allowlist -- and not the
        caller's field in either direction.

        `id` is a real column deliberately left out of the allowlist, so `id ASC`
        and `id DESC` must give the SAME order as each other and as a nonsense
        field: that is what "the parameter was refused" looks like from outside.
        Measured rather than assumed -- the first version of this test guessed
        ascending and failed.
        """
        low, high = self.two_communities_differing_in_everything(env)

        body = env.client.get('/admin/communities',
                              query_string={'sort_by': sort_by}).text

        assert body.index('sorthigh') < body.index('sortlow')  # content_retention DESC

    def test_an_unknown_direction_means_ascending(self, env):
        """`title SIDEWAYS` is NOT a refused sort: the FIELD is allowed and only the
        direction is unrecognised, and `safe_order_by` reads anything that is not
        'desc' as ascending. Grouping it with the refused parameters above was wrong,
        and the test said so."""
        low, high = self.two_communities_differing_in_everything(env)

        body = env.client.get('/admin/communities',
                              query_string={'sort_by': 'title SIDEWAYS'}).text

        assert body.index('sortlow') < body.index('sorthigh')  # 'AAA low' first

    def test_the_two_directions_differ(self, env):
        """Otherwise `safe_order_by`'s `desc` arm is never observed through a route."""
        first = a_community('aaasortfirst')
        first.title = 'AAA First'
        second = a_community('zzzsortlast')
        second.title = 'ZZZ Last'
        db.session.commit()

        ascending = env.client.get('/admin/communities',
                                   query_string={'sort_by': 'title ASC'}).text
        descending = env.client.get('/admin/communities',
                                    query_string={'sort_by': 'title DESC'}).text

        assert ascending.index('AAA First') < ascending.index('ZZZ Last')
        assert descending.index('ZZZ Last') < descending.index('AAA First')


class TestPaging:
    """`per_page=1000`, so the paging arms need more than a thousand rows to reach --
    which no test should create. What is asserted is the page-1 answer: no next url and
    no prev url, which is the arm every real request takes."""

    @pytest.mark.parametrize('path', LISTINGS)
    def test_page_one_offers_no_previous(self, env, path):
        a_community('onepager', topic_id=None, low_quality=True, un_moderated=True)

        body = env.client.get(path, query_string={'page': 1}).text

        assert 'page=0' not in body

    @pytest.mark.parametrize('path', LISTINGS)
    def test_a_page_beyond_the_end_is_empty_rather_than_an_error(self, env, path):
        """`error_out=False` on the paginate call."""
        a_community('beyond', topic_id=None, low_quality=True, un_moderated=True)

        response = env.client.get(path, query_string={'page': 99})

        assert response.status_code == 200
        assert 'beyond' not in response.text


class TestRenamingALocalCommunity:
    """D1370. `admin_community_edit` writes `community.name` from the form's `url`
    field, which is an editable input on the page.

    `community.name` is half of a local community's ActivityPub identity, and
    `admin_community_move` -- linked from this very page as "Convert to local
    community" -- rewrites six URLs and `ap_domain` when it changes the name. This
    route wrote the name alone, so a rename here left `ap_profile_id`,
    `ap_public_url`, `ap_followers_url`, `ap_featured_url` and `ap_moderators_url`
    pointing at `/c/<oldname>`, while `community.link()` and every page in the UI moved
    to the new one. Outbound activities kept citing a URL that no longer resolved.

    `-1` is the form's "no topic" choice, not 0, and `default_layout` accepts only
    '', 'masonry' or 'masonry_wide' -- both learned by reading `form.errors`, since a
    failed validation re-renders the page and looks like a refusal.
    """

    def edit(self, env, community, **overrides):
        data = {'csrf_token': csrf(env.app, env.client),
                'url': community.name, 'title': community.title or 'A Title',
                'description': '', 'rules': '', 'content_retention': -1,
                'topic': -1, 'default_layout': '', 'posting_warning': '',
                'downvote_accept_mode': 0}
        data.update(overrides)
        return env.client.post(f'/admin/community/{community.id}/edit', data=data)

    @pytest.fixture(autouse=True)
    def undetermined_language(self, env):
        """Every path that saves a community appends the `und` Language so that posts
        with no language are accepted, and all four sites in the codebase assume the
        row exists -- two of them dereference `.id` on it. It is created by the
        database seed, so a test that saves a community has to have it too; without
        it the append is `FlushError: Can't flush None value found in collection
        Community.languages`.
        """
        from app.models import Language

        existing = Language.query.filter(Language.code == 'und').first()
        if existing is None:
            existing = Language(code='und', name='Undetermined')
            db.session.add(existing)
            db.session.commit()
        return existing

    def a_local_community(self, env, name='beforerename'):
        community = a_community(name)
        community.ap_id = None
        community.instance_id = 1
        community.ap_featured_url = f'https://test.piefed.local/c/{name}/featured'
        community.ap_moderators_url = f'https://test.piefed.local/c/{name}/moderators'
        db.session.commit()
        assert community.is_local()
        return community

    def test_the_name_changes(self, env):
        community = self.a_local_community(env)

        response = self.edit(env, community, url='afterrename')

        assert response.status_code == 302
        db.session.refresh(community)
        assert community.name == 'afterrename'

    def test_every_activitypub_url_follows_the_name(self, env):
        community = self.a_local_community(env)

        self.edit(env, community, url='afterrename')

        db.session.refresh(community)
        base = 'https://test.piefed.local/c/afterrename'
        assert community.ap_profile_id == base
        assert community.ap_public_url == base
        assert community.ap_followers_url == f'{base}/followers'
        assert community.ap_featured_url == f'{base}/featured'
        assert community.ap_moderators_url == f'{base}/moderators'

    def test_the_old_name_is_not_left_anywhere(self, env):
        """The defect in one assertion: no column may still name the old URL."""
        community = self.a_local_community(env)

        self.edit(env, community, url='afterrename')

        db.session.refresh(community)
        for column in ('ap_profile_id', 'ap_public_url', 'ap_followers_url',
                       'ap_featured_url', 'ap_moderators_url'):
            assert 'beforerename' not in (getattr(community, column) or ''), column

    def test_the_new_name_resolves_and_the_old_one_does_not(self, env):
        community = self.a_local_community(env)

        self.edit(env, community, url='afterrename')

        assert env.client.get('/c/afterrename').status_code == 200
        # 302, not 404: `/c/<actor>` redirects when it cannot resolve the name rather
        # than refusing outright. What matters is that the old name no longer RENDERS
        # the community -- measured, not assumed.
        assert env.client.get('/c/beforerename').status_code != 200

    def test_saving_without_changing_the_name_leaves_the_urls_alone(self, env):
        """The guard is `form.url.data != community.name`, so an ordinary save must not
        rewrite anything -- including for a community whose stored URLs do not follow
        the scheme this route would generate."""
        community = self.a_local_community(env)
        community.ap_profile_id = 'https://test.piefed.local/c/beforerename?legacy=1'
        db.session.commit()

        self.edit(env, community, title='A New Title')

        db.session.refresh(community)
        assert community.title == 'A New Title'
        assert community.ap_profile_id == \
            'https://test.piefed.local/c/beforerename?legacy=1'

    def test_a_remote_communitys_urls_are_never_rewritten(self, env):
        """A remote community's URLs belong to the server that publishes it, and the
        template already warns its settings are overwritten from there. Renaming it
        locally must not claim its identity for this instance."""
        community = a_community('remoteone')
        community.ap_id = 'remoteone@peer.example'
        community.ap_profile_id = 'https://peer.example/c/remoteone'
        community.ap_public_url = 'https://peer.example/c/remoteone'
        community.ap_domain = 'peer.example'
        community.instance_id = env.baseline.instance_remote.id
        db.session.commit()
        assert not community.is_local()

        self.edit(env, community, url='renamedremote')

        db.session.refresh(community)
        assert community.ap_profile_id == 'https://peer.example/c/remoteone'
        assert community.ap_public_url == 'https://peer.example/c/remoteone'
        assert community.ap_domain == 'peer.example'

    def test_the_domain_is_set_for_a_local_rename(self, env):
        community = self.a_local_community(env)
        community.ap_domain = 'stale.example'
        db.session.commit()

        self.edit(env, community, url='afterrename')

        db.session.refresh(community)
        assert community.ap_domain == 'test.piefed.local'
