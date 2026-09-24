"""The post API's listing: `get_post_list`, and the three prefetch helpers
that feed it.

Sub-project 84, slice M -- the first of the two listing functions in
`app/api/alpha/utils/post.py`. Seven defects, all measured first, and the
first of them was handing private communities to anyone who asked:

* this function builds TWO queries -- a sqlalchemy one and a raw SQL one --
  and runs whichever `use_faster_query` picks. The private-community filter
  was applied to the sqlalchemy query unconditionally but appended to the SQL
  only when the reader already belonged to a private community, which an
  anonymous caller never does. `GET /post/list` answered with every private
  community's posts on the instance (D1227);
* the same shape one branch along: a URL search filtered the sqlalchemy query
  and left the fast path on, so the SQL ran with no url condition and the
  search answered with everything (D1228);
* `minimum_upvotes` compared `up_votes - down_votes` in one query and `score`
  in the other, so the same request answered differently depending on which
  path ran (D1230);

* `has_next_page = len(post_ids) > page + 1 * limit`. `*` binds tighter than
  `+`, so the test read `len > page + limit` and stayed true long after the
  rows ran out: page 6 of a nine-row listing at two per page came back EMPTY
  and still said the next page was 7, which an infinite-scroll client follows
  forever (D1224);
* the ORDER BY clause was joined unconditionally, and two ordinary requests
  leave it empty -- an unrecognised sort with `ignore_sticky` set, and a URL
  search sorted by relevance, which is a plain
  `GET /search?type_=Url&sort=Relevance`. Both came back as
  `psycopg2.errors.SyntaxError: syntax error at or near "LIMIT"` (D1225);
* asked for 'Subscribed', 'Moderating' or 'ModeratorView' without an account,
  the listing fell through to the All arm and answered a different question
  without saying so (D1223);
* a feed or topic id nobody holds was `'NoneType' object has no attribute
  'show_posts_in_children'` (D1226).

Fixed in `get_post_list2` at the same time, which is a copy of this function:
the two share every one of these but the pagination, and slice N covers it.
"""
import pytest
from flask import current_app, g

from app import db
from app.api.alpha.utils.post import (get_post_interacted_at, get_post_list,
                                      get_post_unread_counts,
                                      get_post_votes_for_posts)
from app.constants import NOTIF_POST, POST_STATUS_REVIEWING
from app.models import (Community, Domain, Filter, Language, Post, Site, Topic,
                        User, read_posts, utcnow)
from tests.factories import (make_community, make_community_block,
                             make_community_member, make_domain,
                             make_domain_block, make_feed_item,
                             make_instance_block, make_local_feed, make_post,
                             make_post_vote, make_user, make_user_block)

MISSING = 999999


def token(user):
    return f'Bearer {user.encode_jwt_token()}'


def names(res):
    return sorted(entry['post']['title'] for entry in res['posts'])


@pytest.fixture
def env(app, api_baseline):
    """A local community listed on the front page, with three posts in it.

    `api_baseline` brings two more posts of its own; this fixture clears
    `show_all` on its communities so the front-page listing sees only what is
    made here, and a test that wants the others asks for them by community.
    """
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    # api_baseline's own communities are taken OUT of the front-page listing
    # here: Community.show_all defaults to True, so its two posts would appear
    # in every unnarrowed listing below and every count would have to carry
    # them. A test that wants them asks for them by community.
    for other in Community.query.all():
        other.show_all = False
    community = make_community('probeland')
    community.show_all = True
    # This file's own author, not api_baseline.user2: user2 wrote the baseline's
    # two posts, and a `person_id` listing ignores `show_all`, so sharing the
    # account would put those posts into every by-person count here.
    author = make_user(api_baseline.instance_local, 'writer', local=True)
    make_community_member(author, community)
    posts = []
    from datetime import timedelta
    now = utcnow()
    for index, title in enumerate(['first', 'second', 'third'], start=1):
        post = make_post(community, author,
                         ap_id=f'https://test.piefed.local/p/{index}')
        post.title = title
        # Distinct timestamps: make_post gives every post the same instant, and
        # a tie in `posted_at` leaves every date-ordered assertion below at the
        # mercy of whatever order the database happens to return.
        post.posted_at = now - timedelta(minutes=index)
        post.last_active = now - timedelta(minutes=index)
        posts.append(post)
    db.session.commit()
    return SimpleNamespace(community=community, author=author, posts=posts,
                           reader=api_baseline.user3,
                           baseline=api_baseline)


def test_api_post_list(app, api_baseline):
    """The row this file started as, kept as it was written.

    It predates the rest of this file -- a smoke test that an authenticated
    caller asking for one community's posts gets some -- and the classes below
    cover what it covers several times over. It stays because deleting a
    passing test to replace it with one's own is not a trade the reader can
    check.
    """
    from sqlalchemy import desc

    from app.api.alpha.utils.post import get_post_list

    user_id = api_baseline.user1.id
    user = db.session.get(User, user_id)
    assert user is not None and hasattr(user, 'id')
    jwt = user.encode_jwt_token()
    assert jwt is not None
    auth = f'Bearer {jwt}'

    high_post_community = Community.query.filter(Community.instance_id != 1).order_by(
        desc(Community.post_count)).first()
    assert high_post_community is not None and hasattr(high_post_community, 'id')

    # post list should be more than 0
    g.admin_ids = [1]
    data = {"community_id": high_post_community.id}
    response = get_post_list(auth, data)
    assert 'posts' in response and len(response['posts']) > 0


# --------------------------------------------------------------- the type arms

class TestTypes:
    def test_the_default_listing_is_every_community_that_asks_to_be_seen(
            self, env):
        assert names(get_post_list(None, {})) == ['first', 'second', 'third']

    @pytest.mark.parametrize('type_', ['Subscribed', 'Moderating',
                                       'ModeratorView'])
    def test_a_listing_of_your_own_needs_an_account(self, env, type_):
        """D1223."""
        with pytest.raises(Exception, match='incorrect login'):
            get_post_list(None, {'type_': type_})

    def test_the_local_listing_is_only_this_instance(self, env):
        # community1 is REMOTE (it has an ap_id) and asks to be seen, so it is
        # in the All listing and must not be in the Local one.
        env.baseline.community1.show_all = True
        db.session.commit()
        assert 'post one' in names(get_post_list(None, {}))
        listed = names(get_post_list(None, {'type_': 'Local'}))
        assert 'first' in listed
        assert 'post one' not in listed

    def test_the_popular_listing_wants_a_score_and_a_willing_community(
            self, env):
        assert get_post_list(None, {'type_': 'Popular'})['posts'] == []
        env.community.show_popular = True
        env.posts[0].score = 101
        db.session.commit()
        assert names(get_post_list(None, {'type_': 'Popular'})) == ['first']

    def test_the_subscribed_listing_is_what_you_joined(self, env):
        assert get_post_list(token(env.reader),
                             {'type_': 'Subscribed'})['posts'] == []
        make_community_member(env.reader, env.community)
        assert names(get_post_list(token(env.reader),
                                   {'type_': 'Subscribed'})) == \
            ['first', 'second', 'third']

    def test_a_banned_membership_does_not_subscribe_you(self, env):
        membership = make_community_member(env.reader, env.community)
        membership.is_banned = True
        db.session.commit()
        assert get_post_list(token(env.reader),
                             {'type_': 'Subscribed'})['posts'] == []

    @pytest.mark.parametrize('type_', ['Moderating', 'ModeratorView'])
    def test_the_moderating_listing_is_what_you_moderate(self, env, type_):
        assert get_post_list(token(env.reader), {'type_': type_})['posts'] == []
        make_community_member(env.reader, env.community, is_moderator=True)
        assert names(get_post_list(token(env.reader),
                                   {'type_': type_})) == \
            ['first', 'second', 'third']


class TestNarrowing:
    def test_one_community_by_id(self, env):
        other = make_community('elsewhere')
        other.show_all = True
        make_post(other, env.author, ap_id='https://test.piefed.local/p/9')
        db.session.commit()
        assert names(get_post_list(None,
                                   {'community_id': env.community.id})) == \
            ['first', 'second', 'third']

    def test_one_community_by_bare_name(self, env):
        env.community.ap_domain = current_app.config['SERVER_NAME']
        db.session.commit()
        assert names(get_post_list(None,
                                   {'community_name': 'probeland'})) == \
            ['first', 'second', 'third']

    def test_one_community_by_full_handle(self, env):
        assert names(get_post_list(
            None, {'community_name': 'probeland@test.piefed.local'})) == \
            ['first', 'second', 'third']

    def test_a_community_nobody_holds_lists_nothing(self, env):
        assert get_post_list(None, {'community_id': MISSING})['posts'] == []
        assert get_post_list(None, {'community_name': 'nowhere'})['posts'] == []

    def test_one_person(self, env):
        stranger = make_user(env.baseline.instance_local, 'stranger',
                             local=True)
        make_post(env.community, stranger,
                  ap_id='https://test.piefed.local/p/9')
        db.session.commit()
        assert names(get_post_list(None,
                                   {'person_id': env.author.id})) == \
            ['first', 'second', 'third']

    def test_one_feed(self, env):
        feed = make_local_feed('newsfeed')
        make_feed_item(feed, env.community)
        db.session.commit()
        assert names(get_post_list(None, {'feed_id': feed.id})) == \
            ['first', 'second', 'third']

    def test_a_feed_that_carries_its_children(self, env):
        parent = make_local_feed('parent')
        parent.show_posts_in_children = True
        child = make_local_feed('child')
        child.parent_feed_id = parent.id
        make_feed_item(child, env.community)
        db.session.commit()
        assert names(get_post_list(None, {'feed_id': parent.id})) == \
            ['first', 'second', 'third']

    def test_a_feed_that_does_not_carry_its_children(self, env):
        parent = make_local_feed('parent')
        parent.show_posts_in_children = False
        child = make_local_feed('child')
        child.parent_feed_id = parent.id
        make_feed_item(child, env.community)
        db.session.commit()
        assert get_post_list(None, {'feed_id': parent.id})['posts'] == []

    def test_a_feed_nobody_holds_is_refused_by_name(self, env):
        """D1226."""
        with pytest.raises(Exception, match='feed not found'):
            get_post_list(None, {'feed_id': MISSING})

    def test_one_topic(self, env):
        topic = Topic(name='news', machine_name='news')
        db.session.add(topic)
        db.session.commit()
        env.community.topic_id = topic.id
        db.session.commit()
        assert names(get_post_list(None, {'topic_id': topic.id})) == \
            ['first', 'second', 'third']

    def test_a_topic_that_carries_its_children(self, env):
        parent = Topic(name='all', machine_name='all',
                       show_posts_in_children=True)
        db.session.add(parent)
        db.session.commit()
        child = Topic(name='news', machine_name='news', parent_id=parent.id)
        db.session.add(child)
        db.session.commit()
        env.community.topic_id = child.id
        db.session.commit()
        assert names(get_post_list(None, {'topic_id': parent.id})) == \
            ['first', 'second', 'third']

    def test_a_topic_that_does_not_carry_its_children(self, env):
        parent = Topic(name='all', machine_name='all',
                       show_posts_in_children=False)
        db.session.add(parent)
        db.session.commit()
        child = Topic(name='news', machine_name='news', parent_id=parent.id)
        db.session.add(child)
        db.session.commit()
        env.community.topic_id = child.id
        db.session.commit()
        assert get_post_list(None, {'topic_id': parent.id})['posts'] == []

    def test_a_topic_nobody_holds_is_refused_by_name(self, env):
        """D1226."""
        with pytest.raises(Exception, match='topic not found'):
            get_post_list(None, {'topic_id': MISSING})


class TestPrivateCommunities:
    @pytest.fixture
    def private(self, env):
        env.community.private = True
        db.session.commit()
        return env.community

    def test_an_anonymous_reader_is_refused(self, env, private):
        with pytest.raises(Exception, match='authentication required'):
            get_post_list(None, {'community_id': private.id})

    def test_a_reader_who_is_not_a_member_is_refused(self, env, private):
        with pytest.raises(Exception, match='membership required'):
            get_post_list(token(env.reader), {'community_id': private.id})

    def test_a_member_is_answered(self, env, private):
        make_community_member(env.reader, private)
        assert len(get_post_list(token(env.reader),
                                 {'community_id': private.id})['posts']) == 3

    def test_the_same_by_name(self, env, private):
        with pytest.raises(Exception, match='authentication required'):
            get_post_list(None, {'community_name': 'probeland@test.piefed.local'})

    def test_a_private_community_is_absent_from_the_open_listing(self, env,
                                                                 private):
        assert get_post_list(None, {})['posts'] == []

    def test_a_member_still_sees_it_in_the_open_listing(self, env, private):
        make_community_member(env.reader, private)
        assert len(get_post_list(token(env.reader), {})['posts']) == 3

    def test_a_private_community_is_absent_from_a_feed_listing(self, env,
                                                               private):
        """The other query path: narrowing by feed turns the raw SQL off, and
        the sqlalchemy filter is what has to keep the private community out."""
        feed = make_local_feed('newsfeed')
        make_feed_item(feed, private)
        db.session.commit()
        assert get_post_list(None, {'feed_id': feed.id})['posts'] == []

    def test_a_member_sees_it_in_a_feed_listing(self, env, private):
        feed = make_local_feed('newsfeed')
        make_feed_item(feed, private)
        make_community_member(env.reader, private)
        db.session.commit()
        assert len(get_post_list(token(env.reader),
                                 {'feed_id': feed.id})['posts']) == 3


class TestBlocks:
    def test_a_person_you_blocked_is_left_out(self, env):
        make_user_block(env.reader, env.author)
        assert get_post_list(token(env.reader), {})['posts'] == []

    def test_a_community_you_blocked_is_left_out(self, env):
        make_community_block(env.reader, env.community)
        assert get_post_list(token(env.reader), {})['posts'] == []

    def test_an_instance_you_blocked_is_left_out(self, env):
        env.community.instance_id = env.baseline.instance_remote.id
        db.session.commit()
        make_instance_block(env.reader, env.baseline.instance_remote)
        assert get_post_list(token(env.reader), {})['posts'] == []

    def test_a_domain_you_blocked_is_left_out(self, env):
        domain = make_domain('example.test')
        env.posts[0].domain_id = domain.id
        db.session.commit()
        make_domain_block(env.reader, domain)
        assert names(get_post_list(token(env.reader), {})) == \
            ['second', 'third']

    def test_a_language_you_do_not_read_is_left_out(self, env):
        english = Language(code='en', name='English')
        french = Language(code='fr', name='French')
        db.session.add_all([english, french])
        db.session.commit()
        env.posts[0].language_id = french.id
        env.posts[1].language_id = english.id
        db.session.commit()
        env.reader.read_language_ids = [english.id]
        db.session.commit()
        listed = names(get_post_list(token(env.reader), {}))
        assert 'second' in listed
        assert 'third' in listed    # language_id is null -- always kept
        assert 'first' not in listed

    def test_your_own_posts_are_not_filtered_against_your_own_blocks(self, env):
        """`user_id != person_id` -- looking at your own posts skips the block
        lists entirely."""
        make_user_block(env.author, env.author)
        assert len(get_post_list(token(env.author),
                                 {'person_id': env.author.id})['posts']) == 3

    def test_a_community_your_keyword_filter_matches_is_left_out(self, env):
        env.reader.community_keyword_filter = 'probe'
        db.session.commit()
        assert get_post_list(token(env.reader), {})['posts'] == []


class TestContentSettings:
    def test_a_bot_is_left_out_when_you_hide_bots(self, env):
        env.posts[0].from_bot = True
        env.reader.ignore_bots = 1
        db.session.commit()
        assert names(get_post_list(token(env.reader), {})) == \
            ['second', 'third']

    def test_nsfl_is_left_out_when_you_hide_it(self, env):
        env.posts[0].nsfl = True
        env.reader.hide_nsfl = 1
        db.session.commit()
        assert names(get_post_list(token(env.reader), {})) == \
            ['second', 'third']

    def test_nsfw_is_left_out_when_you_hide_it(self, env):
        env.posts[0].nsfw = True
        env.reader.hide_nsfw = 1
        db.session.commit()
        assert names(get_post_list(token(env.reader), {})) == \
            ['second', 'third']

    def test_asking_for_nsfw_overrides_the_setting(self, env):
        env.posts[0].nsfw = True
        env.reader.hide_nsfw = 1
        db.session.commit()
        assert names(get_post_list(token(env.reader),
                                   {'nsfw': 'Include'})) == \
            ['first', 'second', 'third']

    def test_asking_for_only_nsfw(self, env):
        env.posts[0].nsfw = True
        db.session.commit()
        assert names(get_post_list(token(env.reader), {'nsfw': 'Only'})) == \
            ['first']

    def test_asking_to_exclude_nsfw(self, env):
        env.posts[0].nsfw = True
        db.session.commit()
        assert names(get_post_list(token(env.reader),
                                   {'nsfw': 'Exclude'})) == \
            ['second', 'third']

    def test_an_anonymous_reader_gets_no_nsfw_by_default(self, env):
        env.posts[0].nsfw = True
        db.session.commit()
        assert names(get_post_list(None, {})) == ['second', 'third']

    def test_an_anonymous_reader_can_ask_for_only_nsfw(self, env):
        env.posts[0].nsfw = True
        db.session.commit()
        assert names(get_post_list(None, {'nsfw': 'Only'})) == ['first']

    def test_an_anonymous_reader_can_ask_to_include_nsfw(self, env):
        env.posts[0].nsfw = True
        db.session.commit()
        assert names(get_post_list(None, {'nsfw': 'Include'})) == \
            ['first', 'second', 'third']

    def test_ai_generated_is_left_out_when_you_hide_it(self, env):
        env.posts[0].ai_generated = True
        env.reader.hide_gen_ai = 1
        db.session.commit()
        assert names(get_post_list(token(env.reader), {})) == \
            ['second', 'third']

    def test_a_post_you_have_read_is_left_out_when_you_hide_those(self, env):
        env.reader.hide_read_posts = True
        db.session.execute(read_posts.insert().values(
            user_id=env.reader.id, read_post_id=env.posts[0].id,
            interacted_at=utcnow()))
        db.session.commit()
        assert names(get_post_list(token(env.reader), {})) == \
            ['second', 'third']

    def test_hiding_read_posts_does_not_apply_to_a_search(self, env):
        env.reader.hide_read_posts = True
        env.posts[0].indexable = True
        db.session.execute(read_posts.insert().values(
            user_id=env.reader.id, read_post_id=env.posts[0].id,
            interacted_at=utcnow()))
        db.session.commit()
        assert 'first' in names(get_post_list(token(env.reader),
                                              {'q': 'first'}))

    def test_nsfl_is_listed_when_you_have_not_hidden_it(self, env):
        env.posts[0].nsfl = True
        env.reader.hide_nsfl = 0
        db.session.commit()
        assert 'first' in names(get_post_list(token(env.reader), {}))

    def test_ai_generated_is_listed_when_you_have_not_hidden_it(self, env):
        env.posts[0].ai_generated = True
        env.reader.hide_gen_ai = 0
        db.session.commit()
        assert 'first' in names(get_post_list(token(env.reader), {}))

    def test_an_nsfw_value_nobody_recognises_filters_nothing(self, env):
        env.posts[0].nsfw = True
        env.reader.hide_nsfw = 0
        db.session.commit()
        assert names(get_post_list(token(env.reader),
                                   {'nsfw': 'Nonsense'})) == \
            ['first', 'second', 'third']

    def test_an_anonymous_reader_and_an_nsfw_value_nobody_recognises(self, env):
        env.posts[0].nsfw = True
        db.session.commit()
        assert names(get_post_list(None, {'nsfw': 'Nonsense'})) == \
            ['first', 'second', 'third']

    def test_hiding_read_posts_when_you_have_read_none(self, env):
        env.reader.hide_read_posts = True
        db.session.commit()
        assert names(get_post_list(token(env.reader), {})) == \
            ['first', 'second', 'third']

    def test_a_home_filter_is_passed_to_the_view(self, env):
        db.session.add(Filter(user_id=env.reader.id, title='t',
                              keywords='second', filter_home=True,
                              hide_type=0))
        db.session.commit()
        assert len(get_post_list(token(env.reader), {})['posts']) == 3


class TestLikedAndSaved:
    def test_only_what_you_upvoted(self, env):
        make_post_vote(env.reader, env.posts[0], 1)
        assert names(get_post_list(token(env.reader),
                                   {'liked_only': True})) == ['first']

    def test_your_own_post_is_not_in_your_liked_listing(self, env):
        make_post_vote(env.author, env.posts[0], 1)
        assert get_post_list(token(env.author),
                             {'liked_only': True})['posts'] == []

    def test_only_what_you_saved(self, env):
        from app.models import PostBookmark
        db.session.add(PostBookmark(user_id=env.reader.id,
                                    post_id=env.posts[1].id))
        db.session.commit()
        assert names(get_post_list(token(env.reader),
                                   {'saved_only': True})) == ['second']

    def test_liked_wins_over_saved(self, env):
        from app.models import PostBookmark
        make_post_vote(env.reader, env.posts[0], 1)
        db.session.add(PostBookmark(user_id=env.reader.id,
                                    post_id=env.posts[1].id))
        db.session.commit()
        assert names(get_post_list(token(env.reader),
                                   {'liked_only': True,
                                    'saved_only': True})) == ['first']

    def test_an_anonymous_reader_asking_for_liked_only_is_ignored(self, env):
        assert len(get_post_list(None, {'liked_only': True})['posts']) == 3


class TestScoreAndStickies:
    def test_a_minimum_score(self, env):
        env.posts[0].up_votes = 10
        env.posts[0].down_votes = 1
        env.posts[0].score = 9
        db.session.commit()
        assert names(get_post_list(None, {'minimum_upvotes': 5})) == ['first']

    def test_the_minimum_reads_the_score_on_both_paths(self, env):
        """The raw SQL compares `score`, so the sqlalchemy filter has to as
        well: a post whose vote counts and stored score disagree must not be
        answered differently depending on which path ran. Narrowing by
        community is what switches the slow path on."""
        env.posts[0].up_votes = 10
        env.posts[0].down_votes = 0
        env.posts[0].score = 0
        db.session.commit()
        assert get_post_list(None, {'minimum_upvotes': 5})['posts'] == []
        assert get_post_list(None, {'minimum_upvotes': 5,
                                    'community_id': env.community.id})[
            'posts'] == []

    def test_a_sticky_post_leads_its_community(self, env):
        env.posts[2].sticky = True
        db.session.commit()
        res = get_post_list(None, {'community_id': env.community.id})
        assert res['posts'][0]['post']['title'] == 'third'

    def test_a_sticky_post_can_be_ignored(self, env):
        env.posts[2].sticky = True
        db.session.commit()
        res = get_post_list(None, {'community_id': env.community.id,
                                   'ignore_sticky': True, 'sort': 'New'})
        assert res['posts'][0]['post']['title'] != 'third'

    def test_an_instance_sticky_is_listed(self, env):
        """Not `leads`: the fast path asks the database for
        `ORDER BY instance_sticky DESC, ...`, but the page it then returns is
        re-sorted by `post_ids_to_models(post_ids, sort)`, which knows nothing
        about stickies -- so on the front page the sticky is in the answer and
        not at the top of it. Recorded in the findings as a divergence between
        the two query paths; the community-narrowed path above does order by
        sticky, and that is pinned."""
        env.posts[2].instance_sticky = True
        db.session.commit()
        res = get_post_list(None, {'sort': 'New'})
        assert 'third' in [entry['post']['title'] for entry in res['posts']]


class TestSorts:
    """Both query paths, because they order by different means.

    The front page runs the raw SQL, whose ORDER BY the sort arms build, and
    then re-sorts the page it got with `post_ids_to_models`. Narrowing by
    community turns that path off and leaves the sqlalchemy `order_by` in
    charge. A sort is only pinned when both are asked.
    """

    @pytest.fixture
    def spread(self, env):
        """One post per time window, so that every Top* arm answers something
        its neighbours -- and the one-day fallback the chain ends in -- do
        not."""
        from datetime import timedelta
        now = utcnow()
        ages = [timedelta(minutes=30), timedelta(hours=3), timedelta(hours=9),
                timedelta(hours=18), timedelta(days=3), timedelta(days=14),
                timedelta(days=45), timedelta(days=100), timedelta(days=200),
                timedelta(days=300), timedelta(days=400)]
        for post in env.posts:
            db.session.delete(post)
        db.session.commit()
        posts = []
        # Five DIFFERENT orderings, so that no sort can be mistaken for its
        # neighbour and no sort agrees with the order the database happens to
        # return: `posted_at` runs one way, `last_active` the other, and
        # `ranking` and `ranking_scaled` are two different permutations of the
        # same eleven values.
        # Built in a THIRD order, so the row order the database returns when a
        # sort is skipped altogether matches neither 'New' nor 'Old': with the
        # posts created oldest-last, an unordered query came back in exactly
        # the order 'New' asks for and the mutant that removes 'New' survived.
        for index in [(n * 5) % 11 for n in range(11)]:
            age = ages[index]
            post = make_post(env.community, env.author,
                             ap_id=f'https://test.piefed.local/s/{index}')
            post.title = f'age{index}'
            post.posted_at = now - age
            post.last_active = now - ages[len(ages) - 1 - index]
            post.score = index
            post.ranking = (index * 3) % 11
            post.ranking_scaled = (index * 7) % 11
            posts.append(post)
        db.session.commit()
        env.posts = posts
        return env

    @pytest.mark.parametrize('sort, count', [
        ('TopHour', 1),
        ('TopSixHour', 2),
        ('TopTwelveHour', 3),
        ('TopDay', 4),
        ('Top', 4),
        ('TopWeek', 5),
        ('TopMonth', 6),
        ('TopThreeMonths', 7),
        ('TopSixMonths', 8),
        ('TopNineMonths', 9),
        ('TopYear', 10),
        ('TopAll', 11),
        ('TopNonsense', 4),      # the chain ends in a one-day window
        ('Hot', 11),
        ('New', 11),
        ('Old', 11),
        ('Active', 11),
        ('Scaled', 11),
        ('Nonsense', 11),
    ])
    def test_how_far_back_each_sort_reaches(self, spread, sort, count):
        assert len(get_post_list(None, {'sort': sort})['posts']) == count

    @pytest.mark.parametrize('sort, key', [
        ('Hot', lambda index: -((index * 3) % 11)),
        ('New', lambda index: index),
        ('Old', lambda index: -index),
        ('Active', lambda index: -index),
        ('Scaled', lambda index: -((index * 7) % 11)),
        ('TopAll', lambda index: -index),
    ])
    def test_the_whole_order_of_each_sort(self, spread, sort, key):
        """Narrowed by community, so the sqlalchemy ordering is what runs --
        and the WHOLE sequence, because a sort whose arm is skipped falls
        through to no ordering at all, which the first row alone cannot
        distinguish."""
        res = get_post_list(None, {'sort': sort,
                                   'community_id': spread.community.id,
                                   'ignore_sticky': True, 'limit': 50})
        expected = [f'age{index}' for index in sorted(range(11), key=key)]
        assert [entry['post']['title'] for entry in res['posts']] == expected

    def test_a_sort_nobody_recognises_still_answers(self, spread):
        assert len(get_post_list(None, {'sort': 'Nonsense'})['posts']) == 11

    @pytest.mark.parametrize('sort', ['Nonsense', 'Relevance'])
    def test_a_sort_that_orders_nothing_with_the_stickies_off(self, spread,
                                                             sort):
        """D1225. `' ORDER BY ' + ', '.join([])` is `ORDER BY `, and the LIMIT
        that followed it made the whole statement a syntax error. These two
        sorts add no clause of their own, and `ignore_sticky` removes the only
        other contributors."""
        res = get_post_list(None, {'sort': sort, 'ignore_sticky': True})
        assert len(res['posts']) == 11

    def test_a_url_search_sorted_by_relevance(self, env):
        """D1225 again, by the shortest route: a plain
        GET /search?type_=Url&sort=Relevance."""
        env.posts[0].url = 'https://example.test/thing'
        db.session.commit()
        res = get_post_list(None, {'q': 'example.test', 'sort': 'Relevance'},
                            search_type='Url')
        assert names(res) == ['first']

    def test_a_url_search(self, env):
        """D1228. The fast path ran with no url condition at all, so this
        answered with every post on the instance."""
        env.posts[0].url = 'https://example.test/thing'
        db.session.commit()
        assert names(get_post_list(None, {'q': 'example.test'},
                                   search_type='Url')) == ['first']

    def test_a_url_search_narrowed_to_one_community(self, env):
        """The same filter on the other query path."""
        env.posts[0].url = 'https://example.test/thing'
        db.session.commit()
        assert names(get_post_list(None, {'q': 'example.test',
                                          'community_id': env.community.id},
                                   search_type='Url')) == ['first']


class TestSearch:
    def test_a_search_matches_the_body(self, env):
        env.posts[0].body = 'a needle in here'
        env.posts[0].indexable = True
        db.session.commit()
        assert 'first' in names(get_post_list(None, {'q': 'needle'}))

    def test_an_unindexable_post_is_never_found(self, env):
        env.posts[0].body = 'a needle in here'
        env.posts[0].indexable = False
        db.session.commit()
        assert 'first' not in names(get_post_list(None, {'q': 'needle'}))


class TestPaging:
    def test_the_pages_and_where_they_stop(self, env):
        """D1224. `page + 1 * limit` is `page + limit`, so the last page kept
        pointing at another one."""
        first = get_post_list(None, {'limit': 2})
        assert len(first['posts']) == 2
        assert first['next_page'] == '2'
        second = get_post_list(None, {'limit': 2, 'page': 2})
        assert len(second['posts']) == 1
        assert second['next_page'] is None

    def test_a_page_past_the_end_says_so(self, env):
        """D1224. This page came back empty AND named a next one."""
        res = get_post_list(None, {'limit': 2, 'page': 3})
        assert res['posts'] == []
        assert res['next_page'] is None

    def test_the_cursor_is_read_in_place_of_the_page(self, env):
        res = get_post_list(None, {'limit': 2, 'page_cursor': '2'})
        assert len(res['posts']) == 1

    def test_the_pages_of_a_single_community(self, env):
        """The other pagination: narrowing by community turns off the raw-SQL
        path, so this is Flask-SQLAlchemy's paginate rather than the branch
        above."""
        first = get_post_list(None, {'community_id': env.community.id,
                                     'limit': 2})
        assert len(first['posts']) == 2
        assert first['next_page'] == '2'
        second = get_post_list(None, {'community_id': env.community.id,
                                      'limit': 2, 'page': 2})
        assert len(second['posts']) == 1
        assert second['next_page'] is None

    def test_a_limit_beyond_the_configured_page_length_is_clamped(
            self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'PAGE_LENGTH', 2)
        assert len(get_post_list(None, {'limit': 500})['posts']) == 2


class TestWhatIsLeftOut:
    def test_a_deleted_post_is_not_listed(self, env):
        env.posts[0].deleted = True
        db.session.commit()
        assert names(get_post_list(None, {})) == ['second', 'third']

    def test_a_post_still_in_review_is_not_listed(self, env):
        env.posts[0].status = POST_STATUS_REVIEWING
        db.session.commit()
        assert names(get_post_list(None, {})) == ['second', 'third']

    def test_a_banned_community_is_not_listed(self, env):
        env.community.banned = True
        db.session.commit()
        assert get_post_list(None, {})['posts'] == []


class TestReaderState:
    def test_your_own_vote_comes_back_with_the_post(self, env):
        make_post_vote(env.reader, env.posts[0], 1)
        res = get_post_list(token(env.reader), {})
        votes = {entry['post']['title']: entry['my_vote']
                 for entry in res['posts']}
        assert votes['first'] == 1
        assert votes['second'] == 0

    def test_the_unread_comment_count_comes_back_with_the_post(self, env):
        from tests.factories import make_post_reply
        make_post_reply(env.posts[0], env.author, body='a reply')
        env.posts[0].reply_count = 1
        db.session.commit()
        res = get_post_list(token(env.reader), {})
        unread = {entry['post']['title']: entry['unread_comments']
                  for entry in res['posts']}
        assert unread['first'] == 1
        assert unread['second'] == 0

    def test_a_bookmark_comes_back_with_the_post(self, env):
        from app.models import PostBookmark
        db.session.add(PostBookmark(user_id=env.reader.id,
                                    post_id=env.posts[0].id))
        db.session.commit()
        res = get_post_list(token(env.reader), {})
        saved = {entry['post']['title']: entry['saved']
                 for entry in res['posts']}
        assert saved['first'] is True
        assert saved['second'] is False

    def test_a_subscription_comes_back_with_the_post(self, env):
        from tests.factories import make_notification_subscription
        make_notification_subscription(env.reader, env.posts[0].id, NOTIF_POST)
        res = get_post_list(token(env.reader), {})
        # `activity_alert`, not `subscribed`: on a post view `subscribed`
        # reports membership of the COMMUNITY, and the post's own subscription
        # is what activity_alert carries.
        alerts = {entry['post']['title']: entry['activity_alert']
                  for entry in res['posts']}
        assert alerts['first'] is True
        assert alerts['second'] is False

    def test_a_note_about_an_author_comes_back_with_their_post(self, env):
        db.session.execute(
            db.text('INSERT INTO user_note (user_id, target_id, body) '
                    'VALUES (:user_id, :target_id, :body)'),
            {'user_id': env.reader.id, 'target_id': env.author.id,
             'body': 'a note'})
        db.session.commit()
        res = get_post_list(token(env.reader), {})
        assert res['posts'][0]['creator']['note'] == 'a note'


class TestPrefetchHelpers:
    def test_the_votes_of_several_posts_at_once(self, env):
        make_post_vote(env.reader, env.posts[0], 1)
        make_post_vote(env.reader, env.posts[1], -1)
        votes = get_post_votes_for_posts(
            env.reader.id, [p.id for p in env.posts])
        assert votes == {env.posts[0].id: 1, env.posts[1].id: -1}

    def test_no_reader_and_no_posts_means_no_query(self, env):
        assert get_post_votes_for_posts(None, [1]) == {}
        assert get_post_votes_for_posts(env.reader.id, []) == {}
        assert get_post_unread_counts(None, [1]) == {}
        assert get_post_unread_counts(env.reader.id, []) == {}
        assert get_post_interacted_at(None, [1]) == {}
        assert get_post_interacted_at(env.reader.id, []) == {}

    def test_the_unread_count_of_a_post_never_opened(self, env):
        from tests.factories import make_post_reply
        make_post_reply(env.posts[0], env.author, body='a reply')
        env.posts[0].reply_count = 1
        db.session.commit()
        counts = get_post_unread_counts(env.reader.id, [env.posts[0].id])
        assert counts[env.posts[0].id] == 1

    def test_the_unread_count_of_a_post_already_read(self, env):
        from datetime import timedelta

        from tests.factories import make_post_reply
        old = make_post_reply(env.posts[0], env.author, body='old')
        old.posted_at = utcnow() - timedelta(days=2)
        db.session.execute(read_posts.insert().values(
            user_id=env.reader.id, read_post_id=env.posts[0].id,
            interacted_at=utcnow() - timedelta(days=1)))
        db.session.commit()
        counts = get_post_unread_counts(env.reader.id, [env.posts[0].id])
        assert counts[env.posts[0].id] == 0

    def test_when_a_post_was_last_interacted_with(self, env):
        when = utcnow()
        db.session.execute(read_posts.insert().values(
            user_id=env.reader.id, read_post_id=env.posts[0].id,
            interacted_at=when))
        db.session.commit()
        interacted = get_post_interacted_at(env.reader.id,
                                            [env.posts[0].id])
        assert interacted[env.posts[0].id] == when
