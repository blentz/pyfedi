"""The post API's other listing: `get_post_list2`, the keyset-paginated one
behind `/post/list2`.

Sub-project 84, slice N. Three defects of its own, all measured first, on top
of the four it shares with `get_post_list` and was fixed for in slice M:

* every one of its ELEVEN `Top*` sorts read `order_by(desc(a, desc(b)))` --
  one call to `desc()` with two arguments, which is
  `TypeError: desc() takes 1 positional argument but 2 were given`. Sorting
  this listing by score, in any window, was a 500 (D1231);
* the search was applied TWICE, and the second time by title alone: the first
  pass reads the full-text vector, the second ANDed `Post.title.ilike(...)`
  onto it, so a word that appears in a post's body and not in its title found
  nothing here while the same search through `get_post_list` found the post
  (D1232);
* `limit` was never clamped to PAGE_LENGTH, so a caller naming `limit=100000`
  had every one of those rows built into Post objects and rendered (D1233).
"""
import pytest
from flask import current_app, g

from app import db
from app.api.alpha.utils.post import get_post_list, get_post_list2
from app.constants import (NOTIF_POST, POST_STATUS_REVIEWING, POST_TYPE_EVENT,
                           POST_TYPE_POLL)
from app.models import (Community, Domain, Language, Post, Site, Topic, User,
                        read_posts, utcnow)
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
    """The same world slice M builds for `get_post_list`, so the two listings
    can be asked the same question."""
    from datetime import timedelta
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    for other in Community.query.all():
        other.show_all = False
    community = make_community('probeland')
    community.show_all = True
    author = make_user(api_baseline.instance_local, 'writer', local=True)
    make_community_member(author, community)
    posts = []
    now = utcnow()
    for index, title in enumerate(['first', 'second', 'third'], start=1):
        post = make_post(community, author,
                         ap_id=f'https://test.piefed.local/q/{index}')
        post.title = title
        post.posted_at = now - timedelta(minutes=index)
        post.last_active = now - timedelta(minutes=index)
        posts.append(post)
    db.session.commit()
    return SimpleNamespace(community=community, author=author, posts=posts,
                           reader=api_baseline.user3, baseline=api_baseline)


class TestSorts:
    """D1231. Each of these was `TypeError: desc() takes 1 positional argument
    but 2 were given` -- the whole Top family, in every window."""

    @pytest.fixture
    def spread(self, env):
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
        for index in [(n * 5) % 11 for n in range(11)]:
            post = make_post(env.community, env.author,
                             ap_id=f'https://test.piefed.local/r/{index}')
            post.title = f'age{index}'
            post.posted_at = now - ages[index]
            post.last_active = now - ages[len(ages) - 1 - index]
            post.score = index
            post.ranking = (index * 3) % 11
            post.ranking_scaled = (index * 7) % 11
            post.reply_count = 1
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
    ])
    def test_how_far_back_each_top_sort_reaches(self, spread, sort, count):
        res = get_post_list2(None, {'sort': sort, 'limit': 50})
        assert len(res['posts']) == count

    @pytest.mark.parametrize('sort, key', [
        ('Hot', lambda index: -((index * 3) % 11)),
        ('New', lambda index: index),
        ('Scaled', lambda index: -((index * 7) % 11)),
        ('Active', lambda index: -index),
        ('TopAll', lambda index: -index),
    ])
    def test_the_whole_order_of_each_sort(self, spread, sort, key):
        res = get_post_list2(None, {'sort': sort, 'limit': 50,
                                    'ignore_sticky': True})
        expected = [f'age{index}' for index in sorted(range(11), key=key)]
        assert [entry['post']['title'] for entry in res['posts']] == expected

    def test_the_active_sort_wants_a_reply(self, spread):
        for post in spread.posts:
            post.reply_count = 0
        spread.posts[0].reply_count = 2
        db.session.commit()
        res = get_post_list2(None, {'sort': 'Active', 'limit': 50})
        assert len(res['posts']) == 1

    def test_the_active_sort_wants_a_last_active(self, spread):
        """D1243. `Post.last_active` has no default of any kind, so it is the
        one column in this ORDER BY that really can be NULL -- and a keyset
        page ordered by a nullable column can drop rows without saying so.
        get_post_list's Active arm has always filtered it out."""
        for post in spread.posts:
            post.last_active = None
        spread.posts[0].last_active = utcnow()
        db.session.commit()
        res = get_post_list2(None, {'sort': 'Active', 'limit': 50})
        assert len(res['posts']) == 1

    def test_the_scaled_sort_wants_a_scaled_ranking(self, spread):
        for post in spread.posts:
            post.ranking_scaled = None
        db.session.commit()
        assert get_post_list2(None, {'sort': 'Scaled'})['posts'] == []

    def test_the_old_sort(self, spread):
        """'Old' is named only in this function's FIRST copy of the sort
        chain; the second copy has no arm for it, so the ordering it leaves
        behind is the one that runs."""
        res = get_post_list2(None, {'sort': 'Old', 'limit': 50,
                                    'ignore_sticky': True})
        assert res['posts'][0]['post']['title'] == 'age10'

    def test_an_unrecognised_top_sort_falls_back_to_a_day(self, spread):
        res = get_post_list2(None, {'sort': 'TopNonsense', 'limit': 50})
        assert len(res['posts']) == 4

    def test_the_relevance_sort_leaves_the_search_to_order_things(self, spread):
        spread.posts[0].body = 'a needle'
        spread.posts[0].indexable = True
        db.session.commit()
        res = get_post_list2(None, {'sort': 'Relevance', 'q': 'needle'})
        assert [entry['post']['title'] for entry in res['posts']] == ['age0']

    def test_a_sort_nobody_recognises_still_answers(self, spread):
        assert len(get_post_list2(None, {'sort': 'Nonsense',
                                         'limit': 50})['posts']) == 11


class TestSearch:
    def test_a_word_in_the_body_is_found(self, env):
        """D1232. The query was applied twice, the second time by TITLE alone,
        so a full-text match in the body was ANDed away."""
        env.posts[0].body = 'a needle in here'
        env.posts[0].indexable = True
        db.session.commit()
        assert names(get_post_list2(None, {'q': 'needle'})) == ['first']
        # and the other listing agrees, which is the point
        assert names(get_post_list(None, {'q': 'needle'})) == ['first']

    def test_a_word_in_the_title_is_found(self, env):
        env.posts[0].indexable = True
        db.session.commit()
        assert names(get_post_list2(None, {'q': 'first'})) == ['first']

    def test_an_unindexable_post_is_never_found(self, env):
        env.posts[0].body = 'a needle in here'
        env.posts[0].indexable = False
        db.session.commit()
        assert get_post_list2(None, {'q': 'needle'})['posts'] == []

    def test_a_url_search(self, env):
        env.posts[0].url = 'https://example.test/thing'
        db.session.commit()
        assert names(get_post_list2(None, {'q': 'example.test'},
                                    search_type='Url')) == ['first']


class TestPaging:
    def test_the_page_cursor_walks_the_listing(self, env):
        """Keyset pagination: `next_page` is an opaque bookmark, not a
        number, and it is handed back as `page_cursor`."""
        first = get_post_list2(None, {'limit': 2})
        assert len(first['posts']) == 2
        assert first['next_page'] is not None
        second = get_post_list2(None, {'limit': 2,
                                       'page_cursor': first['next_page']})
        assert len(second['posts']) == 1
        assert second['next_page'] is None
        assert names(first) + names(second) == ['first', 'second', 'third']

    def test_the_page_key_is_read_as_a_cursor_too(self, env):
        first = get_post_list2(None, {'limit': 2})
        second = get_post_list2(None, {'limit': 2,
                                       'page': first['next_page']})
        assert len(second['posts']) == 1

    def test_a_limit_beyond_the_configured_page_length_is_clamped(
            self, env, monkeypatch):
        """D1233. This function had no clamp at all."""
        monkeypatch.setitem(current_app.config, 'PAGE_LENGTH', 2)
        assert len(get_post_list2(None, {'limit': 500})['posts']) == 2


class TestTypes:
    def test_the_default_listing(self, env):
        assert names(get_post_list2(None, {})) == ['first', 'second', 'third']

    @pytest.mark.parametrize('type_', ['Subscribed', 'Moderating',
                                       'ModeratorView'])
    def test_a_listing_of_your_own_needs_an_account(self, env, type_):
        with pytest.raises(Exception, match='incorrect login'):
            get_post_list2(None, {'type_': type_})

    def test_the_local_listing_is_only_this_instance(self, env):
        env.baseline.community1.show_all = True
        db.session.commit()
        assert 'post one' in names(get_post_list2(None, {}))
        assert 'post one' not in names(get_post_list2(None,
                                                      {'type_': 'Local'}))

    def test_the_popular_listing(self, env):
        assert get_post_list2(None, {'type_': 'Popular'})['posts'] == []
        env.community.show_popular = True
        env.posts[0].score = 101
        db.session.commit()
        assert names(get_post_list2(None, {'type_': 'Popular'})) == ['first']

    def test_the_subscribed_listing(self, env):
        assert get_post_list2(token(env.reader),
                              {'type_': 'Subscribed'})['posts'] == []
        make_community_member(env.reader, env.community)
        assert len(get_post_list2(token(env.reader),
                                  {'type_': 'Subscribed'})['posts']) == 3

    @pytest.mark.parametrize('type_', ['Moderating', 'ModeratorView'])
    def test_the_moderating_listing(self, env, type_):
        assert get_post_list2(token(env.reader),
                              {'type_': type_})['posts'] == []
        make_community_member(env.reader, env.community, is_moderator=True)
        assert len(get_post_list2(token(env.reader),
                                  {'type_': type_})['posts']) == 3


class TestNarrowing:
    def test_one_community_by_id(self, env):
        other = make_community('elsewhere')
        other.show_all = True
        make_post(other, env.author, ap_id='https://test.piefed.local/q/9')
        db.session.commit()
        assert names(get_post_list2(None,
                                    {'community_id': env.community.id})) == \
            ['first', 'second', 'third']

    def test_one_community_by_bare_name(self, env):
        # A second community that also asks to be seen: without it, a
        # community_name that was ignored altogether would fall through to the
        # front-page listing and answer with the same three posts.
        other = make_community('elsewhere')
        other.show_all = True
        other_post = make_post(other, env.author,
                               ap_id='https://test.piefed.local/q/8')
        other_post.title = 'theirs'
        env.community.ap_domain = current_app.config['SERVER_NAME']
        db.session.commit()
        assert names(get_post_list2(None,
                                    {'community_name': 'probeland'})) == \
            ['first', 'second', 'third']

    def test_one_community_by_full_handle(self, env):
        other = make_community('elsewhere')
        other.show_all = True
        other_post = make_post(other, env.author,
                               ap_id='https://test.piefed.local/q/8')
        other_post.title = 'theirs'
        db.session.commit()
        assert names(get_post_list2(
            None, {'community_name': 'probeland@test.piefed.local'})) == \
            ['first', 'second', 'third']

    def test_one_person(self, env):
        stranger = make_user(env.baseline.instance_local, 'stranger',
                             local=True)
        make_post(env.community, stranger,
                  ap_id='https://test.piefed.local/q/9')
        db.session.commit()
        assert names(get_post_list2(None,
                                    {'person_id': env.author.id})) == \
            ['first', 'second', 'third']

    def test_one_feed(self, env):
        feed = make_local_feed('newsfeed')
        make_feed_item(feed, env.community)
        db.session.commit()
        assert names(get_post_list2(None, {'feed_id': feed.id})) == \
            ['first', 'second', 'third']

    def test_a_feed_that_carries_its_children(self, env):
        parent = make_local_feed('parent')
        parent.show_posts_in_children = True
        child = make_local_feed('child')
        child.parent_feed_id = parent.id
        make_feed_item(child, env.community)
        db.session.commit()
        assert len(get_post_list2(None, {'feed_id': parent.id})['posts']) == 3

    def test_a_feed_that_does_not_carry_its_children(self, env):
        parent = make_local_feed('parent')
        parent.show_posts_in_children = False
        child = make_local_feed('child')
        child.parent_feed_id = parent.id
        make_feed_item(child, env.community)
        db.session.commit()
        assert get_post_list2(None, {'feed_id': parent.id})['posts'] == []

    def test_a_feed_nobody_holds_is_refused_by_name(self, env):
        with pytest.raises(Exception, match='feed not found'):
            get_post_list2(None, {'feed_id': MISSING})

    def test_an_instance_sticky_does_not_lead_a_feed_listing(self, env):
        """A feed is not the front page. Every other narrowing branch turns
        `segregate_instance_stickies` off, and the feed branch had that line
        only in an unreachable duplicate of itself."""
        feed = make_local_feed('newsfeed')
        make_feed_item(feed, env.community)
        env.posts[2].instance_sticky = True   # 'third', the oldest
        db.session.commit()
        res = get_post_list2(None, {'feed_id': feed.id, 'sort': 'New'})
        assert res['posts'][0]['post']['title'] == 'first'

    def test_one_topic(self, env):
        topic = Topic(name='news', machine_name='news')
        db.session.add(topic)
        db.session.commit()
        env.community.topic_id = topic.id
        db.session.commit()
        assert len(get_post_list2(None, {'topic_id': topic.id})['posts']) == 3

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
        assert len(get_post_list2(None, {'topic_id': parent.id})['posts']) == 3

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
        assert get_post_list2(None, {'topic_id': parent.id})['posts'] == []

    def test_a_topic_nobody_holds_is_refused_by_name(self, env):
        with pytest.raises(Exception, match='topic not found'):
            get_post_list2(None, {'topic_id': MISSING})


class TestPrivateCommunities:
    @pytest.fixture
    def private(self, env):
        env.community.private = True
        db.session.commit()
        return env.community

    def test_an_anonymous_reader_is_refused(self, env, private):
        with pytest.raises(Exception, match='authentication required'):
            get_post_list2(None, {'community_id': private.id})

    def test_a_reader_who_is_not_a_member_is_refused(self, env, private):
        with pytest.raises(Exception, match='membership required'):
            get_post_list2(token(env.reader), {'community_id': private.id})

    def test_a_member_is_answered(self, env, private):
        make_community_member(env.reader, private)
        assert len(get_post_list2(token(env.reader),
                                  {'community_id': private.id})['posts']) == 3

    def test_a_private_community_is_absent_from_the_open_listing(self, env,
                                                                 private):
        assert get_post_list2(None, {})['posts'] == []

    def test_a_member_still_sees_it_in_the_open_listing(self, env, private):
        make_community_member(env.reader, private)
        assert len(get_post_list2(token(env.reader), {})['posts']) == 3


class TestBlocks:
    def test_a_person_you_blocked_is_left_out(self, env):
        make_user_block(env.reader, env.author)
        assert get_post_list2(token(env.reader), {})['posts'] == []

    def test_a_community_you_blocked_is_left_out(self, env):
        make_community_block(env.reader, env.community)
        assert get_post_list2(token(env.reader), {})['posts'] == []

    def test_an_instance_you_blocked_is_left_out(self, env):
        env.community.instance_id = env.baseline.instance_remote.id
        db.session.commit()
        make_instance_block(env.reader, env.baseline.instance_remote)
        assert get_post_list2(token(env.reader), {})['posts'] == []

    def test_a_domain_you_blocked_is_left_out(self, env):
        domain = make_domain('example.test')
        env.posts[0].domain_id = domain.id
        db.session.commit()
        make_domain_block(env.reader, domain)
        assert names(get_post_list2(token(env.reader), {})) == \
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
        listed = names(get_post_list2(token(env.reader), {}))
        assert 'first' not in listed
        assert 'second' in listed
        assert 'third' in listed

    def test_a_community_your_keyword_filter_matches_is_left_out(self, env):
        env.reader.community_keyword_filter = 'probe'
        db.session.commit()
        assert get_post_list2(token(env.reader), {})['posts'] == []


class TestContentSettings:
    def test_a_bot_is_left_out_when_you_hide_bots(self, env):
        env.posts[0].from_bot = True
        env.reader.ignore_bots = 1
        db.session.commit()
        assert names(get_post_list2(token(env.reader), {})) == \
            ['second', 'third']

    def test_nsfl_is_left_out_when_you_hide_it(self, env):
        env.posts[0].nsfl = True
        env.reader.hide_nsfl = 1
        db.session.commit()
        assert names(get_post_list2(token(env.reader), {})) == \
            ['second', 'third']

    def test_nsfw_is_left_out_when_you_hide_it(self, env):
        env.posts[0].nsfw = True
        env.reader.hide_nsfw = 1
        db.session.commit()
        assert names(get_post_list2(token(env.reader), {})) == \
            ['second', 'third']

    @pytest.mark.parametrize('asked, expected', [
        ('Include', ['first', 'second', 'third']),
        ('Only', ['first']),
        ('Exclude', ['second', 'third']),
    ])
    def test_what_the_request_can_say_about_nsfw(self, env, asked, expected):
        env.posts[0].nsfw = True
        env.reader.hide_nsfw = 1
        db.session.commit()
        assert names(get_post_list2(token(env.reader),
                                    {'nsfw': asked})) == expected

    @pytest.mark.parametrize('asked, expected', [
        ('', ['second', 'third']),
        ('Exclude', ['second', 'third']),
        ('Only', ['first']),
        ('Include', ['first', 'second', 'third']),
    ])
    def test_the_same_asked_anonymously(self, env, asked, expected):
        env.posts[0].nsfw = True
        db.session.commit()
        assert names(get_post_list2(None, {'nsfw': asked})) == expected

    def test_ai_generated_is_left_out_when_you_hide_it(self, env):
        env.posts[0].ai_generated = True
        env.reader.hide_gen_ai = 1
        db.session.commit()
        assert names(get_post_list2(token(env.reader), {})) == \
            ['second', 'third']

    def test_nsfl_is_listed_when_you_have_not_hidden_it(self, env):
        env.posts[0].nsfl = True
        env.reader.hide_nsfl = 0
        db.session.commit()
        assert 'first' in names(get_post_list2(token(env.reader), {}))

    def test_ai_generated_is_listed_when_you_have_not_hidden_it(self, env):
        env.posts[0].ai_generated = True
        env.reader.hide_gen_ai = 0
        db.session.commit()
        assert 'first' in names(get_post_list2(token(env.reader), {}))

    def test_an_nsfw_value_nobody_recognises_filters_nothing(self, env):
        env.posts[0].nsfw = True
        env.reader.hide_nsfw = 0
        db.session.commit()
        assert names(get_post_list2(token(env.reader),
                                    {'nsfw': 'Nonsense'})) == \
            ['first', 'second', 'third']

    def test_an_anonymous_reader_and_an_nsfw_value_nobody_recognises(self, env):
        env.posts[0].nsfw = True
        db.session.commit()
        assert names(get_post_list2(None, {'nsfw': 'Nonsense'})) == \
            ['first', 'second', 'third']

    def test_a_post_you_have_read_is_left_out_when_you_hide_those(self, env):
        env.reader.hide_read_posts = True
        db.session.execute(read_posts.insert().values(
            user_id=env.reader.id, read_post_id=env.posts[0].id,
            interacted_at=utcnow()))
        db.session.commit()
        assert names(get_post_list2(token(env.reader), {})) == \
            ['second', 'third']


class TestLikedAndSaved:
    def test_only_what_you_upvoted(self, env):
        make_post_vote(env.reader, env.posts[0], 1)
        assert names(get_post_list2(token(env.reader),
                                    {'liked_only': True})) == ['first']

    def test_your_own_post_is_not_in_your_liked_listing(self, env):
        make_post_vote(env.author, env.posts[0], 1)
        assert get_post_list2(token(env.author),
                              {'liked_only': True})['posts'] == []

    def test_only_what_you_saved(self, env):
        from app.models import PostBookmark
        db.session.add(PostBookmark(user_id=env.reader.id,
                                    post_id=env.posts[1].id))
        db.session.commit()
        assert names(get_post_list2(token(env.reader),
                                    {'saved_only': True})) == ['second']


class TestWhatIsLeftOut:
    def test_a_poll_is_not_listed(self, env):
        env.posts[0].type = POST_TYPE_POLL
        db.session.commit()
        assert names(get_post_list2(None, {})) == ['second', 'third']

    def test_an_event_is_not_listed(self, env):
        env.posts[0].type = POST_TYPE_EVENT
        db.session.commit()
        assert names(get_post_list2(None, {})) == ['second', 'third']

    def test_a_deleted_post_is_not_listed(self, env):
        env.posts[0].deleted = True
        db.session.commit()
        assert names(get_post_list2(None, {})) == ['second', 'third']

    def test_a_post_still_in_review_is_not_listed(self, env):
        env.posts[0].status = POST_STATUS_REVIEWING
        db.session.commit()
        assert names(get_post_list2(None, {})) == ['second', 'third']

    def test_a_minimum_score(self, env):
        env.posts[0].score = 9
        db.session.commit()
        assert names(get_post_list2(None, {'minimum_upvotes': 5})) == ['first']


class TestReaderState:
    def test_your_own_vote_comes_back_with_the_post(self, env):
        make_post_vote(env.reader, env.posts[0], 1)
        res = get_post_list2(token(env.reader), {})
        votes = {entry['post']['title']: entry['my_vote']
                 for entry in res['posts']}
        assert votes['first'] == 1
        assert votes['second'] == 0

    def test_a_bookmark_comes_back_with_the_post(self, env):
        from app.models import PostBookmark
        db.session.add(PostBookmark(user_id=env.reader.id,
                                    post_id=env.posts[0].id))
        db.session.commit()
        res = get_post_list2(token(env.reader), {})
        saved = {entry['post']['title']: entry['saved']
                 for entry in res['posts']}
        assert saved['first'] is True
        assert saved['second'] is False

    def test_a_subscription_comes_back_with_the_post(self, env):
        from tests.factories import make_notification_subscription
        make_notification_subscription(env.reader, env.posts[0].id, NOTIF_POST)
        res = get_post_list2(token(env.reader), {})
        alerts = {entry['post']['title']: entry['activity_alert']
                  for entry in res['posts']}
        assert alerts['first'] is True
        assert alerts['second'] is False
