"""Integers that arrive in a query string, and the rows they name.

Sub-project 112 -- a sweep, not a module. `int(request.args.get('topic_id', 0))`
raises `ValueError` for any value that is not a number, including the empty
string a `<select>` submits when nothing is chosen, and `TypeError` when the
parameter is absent altogether. Both are 500s, from a value the caller chooses.

`grep -rn "int(request.args.get(" app/` found nine sites in three files: the
community list and the health endpoint in `app/main/routes.py` (three each) and
`app/feed/routes.py`'s add-a-community-to-a-feed (three). The community list is
a page anybody can open; `/health2` takes no login at all. They are all
`request.args.get(..., 0, type=int)` now, which answers the default rather than
raising.

Two of the ids in the feed route named rows that had to exist and were read
without asking, which is the same shape one level down: `db.session.get(Feed,
feed_id).user_id` is an `AttributeError` for an id nobody has, on the line
before the one that says 404. And `_feed_add_community` behind it deleted a
FeedItem it had not found and inserted one it already had.
"""
import pytest
from flask import g

from app import cache, db
from app.models import Community, Feed, FeedItem, Language, Site, Topic, User
from tests.factories import (make_community, make_community_member, make_user)


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    user = make_user(api_baseline.instance_local, 'querytester', local=True)
    user.verified = True
    user.private_key = 'x'
    db.session.commit()
    community = make_community('probeland')
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return SimpleNamespace(app=app, client=client, anonymous=app.test_client(),
                           user=user, community=community,
                           baseline=api_baseline)


def a_feed(owner, name='afeed', public=False):
    feed = Feed(user_id=owner.id, title=name.title(), name=name,
                machine_name=name, public=public, instance_id=1,
                ap_profile_id=f'https://test.piefed.local/f/{name}',
                ap_public_url=f'https://test.piefed.local/f/{name}',
                ap_followers_url=f'https://test.piefed.local/f/{name}/followers',
                subscriptions_count=0, num_communities=0)
    db.session.add(feed)
    db.session.commit()
    return feed


NOT_A_NUMBER = ['x', '', '1.5', 'null', '0x1', ' ', 'NaN', '-', '1,2']


class TestTheCommunityList:
    """D1311. `/communities?topic_id=` -- what a select with nothing chosen
    sends -- was a 500 on a page anybody can open."""

    @pytest.mark.parametrize('value', NOT_A_NUMBER)
    def test_a_topic_id_that_is_not_a_number(self, env, value):
        response = env.client.get(f'/communities?topic_id={value}')
        assert response.status_code == 200

    @pytest.mark.parametrize('value', NOT_A_NUMBER)
    def test_a_feed_id_that_is_not_a_number(self, env, value):
        assert env.client.get(f'/communities?feed_id={value}').status_code == 200

    @pytest.mark.parametrize('value', NOT_A_NUMBER)
    def test_a_language_id_that_is_not_a_number(self, env, value):
        assert env.client.get(
            f'/communities?language_id={value}').status_code == 200

    def test_an_anonymous_visitor_on_an_open_instance(self, env):
        """`login_required_if_private_instance` lets anyone in when the
        instance is open, so the crash was reachable without an account."""
        g.site.private_instance = False
        db.session.commit()
        assert env.anonymous.get('/communities?topic_id=x').status_code == 200

    def test_an_anonymous_visitor_on_a_private_one_is_sent_to_log_in(self, env):
        g.site.private_instance = True
        db.session.commit()
        response = env.anonymous.get('/communities?topic_id=x')
        assert response.status_code == 302
        assert '/auth/login' in response.headers['Location']

    def test_a_number_far_beyond_any_row(self, env):
        assert env.client.get(
            '/communities?topic_id=' + '9' * 20).status_code == 200

    def test_a_negative_one(self, env):
        assert env.client.get('/communities?topic_id=-5').status_code == 200

    def test_all_three_at_once(self, env):
        assert env.client.get(
            '/communities?topic_id=x&feed_id=y&language_id=z').status_code == 200

    def test_the_filter_still_filters(self, env):
        """The value that is a number does what it always did, which is what
        makes the default-on-failure a repair rather than a removal."""
        topic = Topic(name='Music', machine_name='music', num_communities=1)
        db.session.add(topic)
        db.session.commit()
        env.community.topic_id = topic.id
        other = make_community('elsewhere')
        db.session.commit()
        response = env.client.get(f'/communities?topic_id={topic.id}')
        assert response.status_code == 200
        assert b'probeland' in response.data
        assert b'elsewhere' not in response.data

    def test_a_topic_that_matches_nothing(self, env):
        response = env.client.get('/communities?topic_id=999999')
        assert response.status_code == 200
        assert b'probeland' not in response.data

    def test_a_language_filter_that_is_a_number(self, env):
        language = Language(code='en', name='English')
        db.session.add(language)
        db.session.commit()
        env.community.languages.append(language)
        db.session.commit()
        response = env.client.get(f'/communities?language_id={language.id}')
        assert response.status_code == 200
        assert b'probeland' in response.data

    def test_a_feed_filter_that_is_a_number(self, env):
        feed = a_feed(env.user, 'listfeed')
        db.session.add(FeedItem(feed_id=feed.id, community_id=env.community.id))
        make_community('elsewhere')
        db.session.commit()
        response = env.client.get(f'/communities?feed_id={feed.id}')
        assert response.status_code == 200
        assert b'probeland' in response.data
        assert b'elsewhere' not in response.data

    def test_a_search_term(self, env):
        make_community('elsewhere')
        db.session.commit()
        response = env.client.get('/communities?search=probeland')
        assert response.status_code == 200
        assert b'probeland' in response.data
        assert b'elsewhere' not in response.data

    def test_a_sort_column_that_does_not_exist(self, env):
        """`safe_order_by` decides this one, and it is asserted here because
        this route is where the parameter arrives."""
        assert env.client.get(
            '/communities?sort_by=drop%20table').status_code == 200


class TestTheHealthEndpoint:
    """D1312. The same three reads, on a route with no login at all."""

    @pytest.mark.parametrize('name', ['topic_id', 'feed_id', 'language_id'])
    @pytest.mark.parametrize('value', ['x', '', '1.5'])
    def test_an_id_that_is_not_a_number(self, env, name, value):
        assert env.anonymous.get(f'/health2?{name}={value}').status_code == 200

    def test_the_plain_call(self, env):
        assert env.anonymous.get('/health2').status_code == 200

    def test_a_head_request(self, env):
        assert env.anonymous.head('/health2').status_code == 200

    def test_a_search_term(self, env):
        assert env.anonymous.get('/health2?search=probe').status_code == 200

    def test_a_topic_that_exists(self, env):
        topic = Topic(name='Music', machine_name='music', num_communities=1)
        db.session.add(topic)
        db.session.commit()
        env.community.topic_id = topic.id
        db.session.commit()
        assert env.anonymous.get(
            f'/health2?topic_id={topic.id}').status_code == 200

    def test_a_language_that_exists(self, env):
        language = Language(code='en', name='English')
        db.session.add(language)
        db.session.commit()
        assert env.anonymous.get(
            f'/health2?language_id={language.id}').status_code == 200

    def test_a_feed_that_exists(self, env):
        feed = a_feed(env.user, 'healthfeed', public=True)
        db.session.add(FeedItem(feed_id=feed.id, community_id=env.community.id))
        db.session.commit()
        assert env.anonymous.get(f'/health2?feed_id={feed.id}').status_code == 200

    def test_the_prompt_flash(self, env):
        assert env.anonymous.get('/health2?prompt=1').status_code == 200

    @pytest.mark.parametrize('nsfw', ['yes', 'no', 'all'])
    def test_each_nsfw_choice(self, env, nsfw):
        assert env.anonymous.get(f'/health2?nsfw={nsfw}').status_code == 200

    def test_with_nsfw_switched_off_site_wide(self, env):
        g.site.enable_nsfw = False
        db.session.commit()
        assert env.anonymous.get('/health2?nsfw=yes').status_code == 200

    def test_a_logged_in_caller_takes_the_other_branch(self, env):
        assert env.client.get('/health2').status_code == 200

    def test_a_logged_in_caller_who_hides_things(self, env):
        env.user.hide_low_quality = True
        env.user.hide_nsfw = 1
        env.user.hide_nsfl = 1
        db.session.commit()
        assert env.client.get('/health2').status_code == 200

    def test_a_logged_in_caller_who_allows_nsfw(self, env):
        env.user.hide_nsfw = 0
        db.session.commit()
        assert env.client.get('/health2?nsfw=yes').status_code == 200
        assert env.client.get('/health2?nsfw=no').status_code == 200

    def test_a_logged_in_caller_banned_from_a_community(self, env):
        from app.models import CommunityBan
        from app.utils import communities_banned_from
        db.session.add(CommunityBan(user_id=env.user.id,
                                    community_id=env.community.id))
        db.session.commit()
        cache.delete_memoized(communities_banned_from, env.user.id)
        assert env.client.get('/health2').status_code == 200

    def test_a_sort_column_that_does_not_exist(self, env):
        assert env.anonymous.get(
            '/health2?sort_by=nonsense').status_code == 200

    def test_the_first_health_endpoint_as_well(self, env):
        assert env.anonymous.get('/health').status_code == 200
        assert env.anonymous.head('/health').status_code == 200


class TestAddingACommunityToAFeed:
    """D1313/D1314/D1315. Three ids read with `int()` and two of them used to
    reach a row without asking whether it is there."""

    def add(self, env, **params):
        query = '&'.join(f'{key}={value}' for key, value in params.items())
        return env.client.get(f'/feed/add_community?{query}')

    def test_no_parameters_at_all(self, env):
        """`int(None)` -- the parameter absent, not merely wrong."""
        assert env.client.get('/feed/add_community').status_code == 404

    def test_a_missing_new_feed_id(self, env):
        assert self.add(env, current_feed_id=0,
                        community_id=env.community.id).status_code == 404

    @pytest.mark.parametrize('value', ['x', '', '1.5'])
    def test_a_feed_id_that_is_not_a_number(self, env, value):
        assert self.add(env, new_feed_id=value, current_feed_id=0,
                        community_id=env.community.id).status_code == 404

    def test_a_community_id_that_is_not_a_number(self, env):
        feed = a_feed(env.user)
        assert self.add(env, new_feed_id=feed.id, current_feed_id=0,
                        community_id='x').status_code == 404

    def test_a_current_feed_id_that_is_not_a_number(self, env):
        """Not a number means 0, and 0 means 'not moving it out of anything',
        so this one is added rather than refused."""
        feed = a_feed(env.user)
        assert self.add(env, new_feed_id=feed.id, current_feed_id='x',
                        community_id=env.community.id).status_code == 302
        assert FeedItem.query.filter_by(feed_id=feed.id,
                                        community_id=env.community.id).count() == 1

    def test_a_feed_nobody_has(self, env):
        assert self.add(env, new_feed_id=999999, current_feed_id=0,
                        community_id=env.community.id).status_code == 404

    def test_a_current_feed_nobody_has(self, env):
        feed = a_feed(env.user)
        assert self.add(env, new_feed_id=feed.id, current_feed_id=999999,
                        community_id=env.community.id).status_code == 404

    def test_a_community_nobody_has(self, env):
        """D1315. The insert was a ForeignKeyViolation -- a FeedItem naming a
        community that does not exist."""
        feed = a_feed(env.user)
        assert self.add(env, new_feed_id=feed.id, current_feed_id=0,
                        community_id=999999).status_code == 404
        assert FeedItem.query.count() == 0

    def test_somebody_else_s_feed(self, env):
        other = make_user(env.baseline.instance_local, 'otherowner', local=True)
        db.session.commit()
        feed = a_feed(other)
        assert self.add(env, new_feed_id=feed.id, current_feed_id=0,
                        community_id=env.community.id).status_code == 404
        assert FeedItem.query.count() == 0

    def test_moving_a_community_out_of_somebody_else_s_feed(self, env):
        other = make_user(env.baseline.instance_local, 'otherowner', local=True)
        db.session.commit()
        mine = a_feed(env.user, 'mine')
        theirs = a_feed(other, 'theirs')
        db.session.add(FeedItem(feed_id=theirs.id,
                                community_id=env.community.id))
        db.session.commit()
        assert self.add(env, new_feed_id=mine.id, current_feed_id=theirs.id,
                        community_id=env.community.id).status_code == 404
        assert FeedItem.query.filter_by(feed_id=theirs.id).count() == 1

    def test_a_community_added_to_a_feed(self, env):
        feed = a_feed(env.user)
        assert self.add(env, new_feed_id=feed.id, current_feed_id=0,
                        community_id=env.community.id).status_code == 302
        assert FeedItem.query.filter_by(feed_id=feed.id,
                                        community_id=env.community.id).count() == 1
        db.session.expire_all()
        assert db.session.get(Feed, feed.id).num_communities == 1

    def test_adding_it_twice_changes_nothing(self, env):
        """D1317. The second request made a second FeedItem and counted it
        again: the route is a GET, so a reload was enough, and the count
        overstated the feed from then on."""
        feed = a_feed(env.user)
        for _attempt in range(3):
            self.add(env, new_feed_id=feed.id, current_feed_id=0,
                     community_id=env.community.id)
        assert FeedItem.query.filter_by(feed_id=feed.id,
                                        community_id=env.community.id).count() == 1
        db.session.expire_all()
        assert db.session.get(Feed, feed.id).num_communities == 1

    def test_a_second_community_still_joins_the_same_feed(self, env):
        """The idempotence above is per pair, not per feed: a feed that already
        holds one community takes another."""
        feed = a_feed(env.user)
        second = make_community('secondland')
        db.session.commit()
        self.add(env, new_feed_id=feed.id, current_feed_id=0,
                 community_id=env.community.id)
        assert self.add(env, new_feed_id=feed.id, current_feed_id=0,
                        community_id=second.id).status_code == 302
        assert FeedItem.query.filter_by(feed_id=feed.id).count() == 2
        db.session.expire_all()
        assert db.session.get(Feed, feed.id).num_communities == 2

    def test_moving_one_between_two_feeds(self, env):
        first = a_feed(env.user, 'first')
        second = a_feed(env.user, 'second')
        self.add(env, new_feed_id=first.id, current_feed_id=0,
                 community_id=env.community.id)
        assert self.add(env, new_feed_id=second.id, current_feed_id=first.id,
                        community_id=env.community.id).status_code == 302
        assert FeedItem.query.filter_by(feed_id=first.id).count() == 0
        assert FeedItem.query.filter_by(feed_id=second.id).count() == 1
        db.session.expire_all()
        assert db.session.get(Feed, first.id).num_communities == 0
        assert db.session.get(Feed, second.id).num_communities == 1

    def test_moving_one_out_of_a_feed_it_is_not_in(self, env):
        """D1316. `db.session.delete(None)` -- `UnmappedInstanceError: Class
        'builtins.NoneType' is not mapped` -- because the FeedItem looked for
        was not there. Both feeds are the caller's, so the ownership checks
        above pass and the helper is reached."""
        first = a_feed(env.user, 'first')
        second = a_feed(env.user, 'second')
        assert self.add(env, new_feed_id=second.id, current_feed_id=first.id,
                        community_id=env.community.id).status_code == 302
        assert FeedItem.query.filter_by(feed_id=second.id).count() == 1

    def test_and_the_feed_it_was_not_in_is_not_counted_down(self, env):
        first = a_feed(env.user, 'first')
        second = a_feed(env.user, 'second')
        self.add(env, new_feed_id=second.id, current_feed_id=first.id,
                 community_id=env.community.id)
        db.session.expire_all()
        assert db.session.get(Feed, first.id).num_communities == 0
        assert db.session.get(Feed, second.id).num_communities == 1

    def test_an_anonymous_caller_is_sent_to_log_in(self, env):
        feed = a_feed(env.user)
        response = env.anonymous.get(
            f'/feed/add_community?new_feed_id={feed.id}&current_feed_id=0'
            f'&community_id={env.community.id}')
        assert response.status_code == 302
        assert '/auth/login' in response.headers['Location']
        assert FeedItem.query.count() == 0


class TestTheShapeIsGoneEverywhere:
    """The property, so the tenth site is caught when it is written."""

    FILES = ['app/main/routes.py', 'app/feed/routes.py']

    def test_no_route_wraps_a_query_argument_in_int(self, env):
        from pathlib import Path
        offenders = []
        for name in self.FILES:
            for number, line in enumerate(Path(name).read_text().splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith('#'):
                    continue
                if 'int(request.args.get(' in stripped:
                    offenders.append(f'{name}:{number}: {stripped}')
        assert offenders == []

    def test_the_sweep_looks_at_files_that_read_query_arguments(self, env):
        from pathlib import Path
        for name in self.FILES:
            assert 'request.args.get' in Path(name).read_text(), name
