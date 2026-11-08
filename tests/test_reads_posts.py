"""app/reads/posts.py: one query builder for post listings, with the raw-SQL fast path derived beside it.

Each test asks the builder directly and reads both what the query lists and, where the spelling has one, what the
fast path lists, so the two are seen to agree. The two spellings (OFFSET_PAGES for get_post_list, KEYSET_PAGES for
get_post_list2) keep the alpha listings' disagreements; the tests named after a divergence pin each one, so a ruling
that unifies them changes a test on purpose rather than by accident.
"""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import and_

from app import db
from app.constants import POST_TYPE_EVENT, POST_TYPE_POLL
from app.models import Community, Language, Post, PostBookmark, Topic, utcnow
from app.reads import posts as posts_module
from app.reads.posts import KEYSET_PAGES, OFFSET_PAGES, TOP_WINDOWS, Listing, fast_post_ids, post_listing
from tests.factories import (hide_post, make_community, make_community_member, make_domain, make_feed_item,
                             make_local_feed, make_post, make_post_vote, make_user, mark_post_read)

SPELLINGS = [OFFSET_PAGES, KEYSET_PAGES]
SPELLING_IDS = ['offset', 'keyset']
RESTRICTION_LISTS = ('blocked_person_ids', 'blocked_community_ids', 'blocked_instance_ids', 'blocked_domain_ids',
                     'private_community_ids', 'read_language_ids')


def restricted(**lists):
    """A viewer's restrictions as Restrictions answers them, with only `lists` set."""
    return SimpleNamespace(**{name: lists.get(name, []) for name in RESTRICTION_LISTS})


NONE = restricted()


@pytest.fixture
def world(app, api_baseline):
    """A local community on the front page with three posts a minute apart; every other community leaves it."""
    for other in Community.query.all():
        other.show_all = False
    here = make_community('here')
    here.show_all = True
    author = make_user(api_baseline.instance_local, 'writer', local=True)
    now = utcnow()
    posts = {}
    for index, title in enumerate(['first', 'second', 'third'], start=1):
        post = make_post(here, author, f'https://test.piefed.local/p/{index}', title=title)
        post.posted_at = post.last_active = now - timedelta(minutes=index)
        post.score = 10 * (4 - index)       # first 30, second 20, third 10
        post.ranking = post.ranking_scaled = 4 - index
        post.reply_count = 1
        posts[title] = post
    db.session.commit()
    return SimpleNamespace(here=here, author=author, posts=posts, reader=api_baseline.user3, base=api_baseline)


def listed(built) -> list[str]:
    """Titles in the query's order."""
    return [post.title for post in built.posts.all()]


def fast_listed(built) -> list[str]:
    titles = {post.id: post.title for post in Post.query.all()}
    return [titles[post_id] for post_id in fast_post_ids(built)]


def order_by(built) -> str:
    sql = str(built.posts.statement.compile())
    return sql.split('ORDER BY', 1)[1].strip() if 'ORDER BY' in sql else ''


def build(listing=Listing(), user=None, restrictions=NONE, spelling=OFFSET_PAGES, named=None):
    return post_listing(user, restrictions, listing, spelling, named)


# ------------------------------------------------------------------ scopes

class TestScopes:
    @pytest.mark.parametrize('spelling', SPELLINGS, ids=SPELLING_IDS)
    def test_the_front_page_is_every_community_that_asks_to_be_seen(self, world, spelling):
        built = build(Listing(sort='New'), spelling=spelling)
        assert listed(built) == ['first', 'second', 'third']
        assert (built.fast is not None) == spelling.raw_sql

    def test_the_fast_path_lists_what_the_query_lists(self, world):
        built = build(Listing(sort='New'))
        assert fast_listed(built) == listed(built) == ['first', 'second', 'third']
        assert 'show_all is true' in built.fast.criteria

    def test_local_is_this_instance_only(self, world):
        remote = world.base.community1
        remote.show_all = True
        make_post(remote, world.author, 'https://remote.piefed.test/p/9', title='remote')
        assert 'remote' in listed(build(Listing(sort='New')))
        built = build(Listing(type='Local', sort='New'))
        assert listed(built) == fast_listed(built) == ['first', 'second', 'third']

    def test_popular_wants_a_score_and_a_willing_community(self, world):
        assert listed(build(Listing(type='Popular'))) == []
        world.here.show_popular = True
        world.posts['second'].score = 101
        db.session.commit()
        built = build(Listing(type='Popular'))
        assert listed(built) == fast_listed(built) == ['second']

    @pytest.mark.parametrize('type_, moderator', [('Subscribed', False), ('Moderating', True),
                                                  ('ModeratorView', True)], ids=['subscribed', 'moderating',
                                                                                 'moderator-view'])
    def test_your_own_communities(self, world, type_, moderator):
        assert listed(build(Listing(type=type_), user=world.reader)) == []
        make_community_member(world.reader, world.here, is_moderator=moderator)
        built = build(Listing(type=type_, sort='New'), user=world.reader)
        assert listed(built) == fast_listed(built) == ['first', 'second', 'third']

    def test_a_banned_membership_does_not_subscribe_you(self, world):
        make_community_member(world.reader, world.here).is_banned = True
        db.session.commit()
        assert listed(build(Listing(type='Subscribed'), user=world.reader)) == []

    def test_your_own_communities_need_a_viewer_or_are_the_front_page(self, world):
        """The alpha callers refuse these first; asked of the builder, nobody's communities are the All listing."""
        assert sorted(listed(build(Listing(type='Subscribed')))) == ['first', 'second', 'third']

    def test_one_community_by_id_or_by_name(self, world):
        by_id = build(Listing(community_id=world.here.id, sort='New'))
        named = and_(Community.name == 'here', Community.ap_domain == world.here.ap_domain)
        by_name = build(Listing(community_name='here', sort='New'), named=named)
        assert listed(by_id) == listed(by_name) == ['first', 'second', 'third']
        assert by_id.fast is None and by_name.fast is None   # a narrowed listing has no fast path
        assert listed(build(Listing(community_id=world.base.community2.id))) == []

    @pytest.mark.parametrize('children', [True, False], ids=['carries-children', 'not-children'])
    def test_a_feed(self, world, children):
        parent = make_local_feed('parent', public=True)
        parent.show_posts_in_children = children
        child = make_local_feed('child', public=True)
        child.parent_feed_id = parent.id
        make_feed_item(child, world.here)
        assert sorted(listed(build(Listing(feed_id=parent.id)))) == (['first', 'second', 'third'] if children else [])
        assert build(Listing(feed_id=child.id)).fast is None

    @pytest.mark.parametrize('feed_id', ['missing', 'private'])
    def test_a_feed_you_may_not_read_is_not_found(self, world, feed_id):
        feed = make_local_feed('mine', public=False, user=world.author)
        with pytest.raises(Exception, match='feed not found'):
            build(Listing(feed_id=999999 if feed_id == 'missing' else feed.id))

    @pytest.mark.parametrize('children', [True, False], ids=['carries-children', 'not-children'])
    def test_a_topic(self, world, children):
        parent = Topic(name='all', machine_name='all', show_posts_in_children=children)
        db.session.add(parent)
        db.session.commit()
        child = Topic(name='news', machine_name='news', parent_id=parent.id)
        db.session.add(child)
        db.session.commit()
        world.here.topic_id = child.id
        db.session.commit()
        assert sorted(listed(build(Listing(topic_id=parent.id)))) == (['first', 'second', 'third'] if children else [])
        assert sorted(listed(build(Listing(topic_id=child.id)))) == ['first', 'second', 'third']

    def test_a_topic_nobody_holds(self, world):
        with pytest.raises(Exception, match='topic not found'):
            build(Listing(topic_id=999999))

    def test_an_author_is_a_profile_listing(self, world):
        world.posts['first'].visibility = 'unlisted'    # a profile listing shows it; no other listing does
        db.session.commit()
        assert listed(build(Listing(sort='New'), user=world.reader)) == ['second', 'third']
        world.here.show_all = False                     # nor does an author's listing ask the front page
        db.session.commit()
        built = build(Listing(person_id=world.author.id, sort='New'), user=world.reader)
        assert listed(built) == ['first', 'second', 'third']
        assert built.fast is None


# ------------------------------------------------------------- restrictions

class TestRestrictions:
    @pytest.mark.parametrize('lists', ['person', 'community', 'instance', 'domain'])
    def test_each_block_holds_on_both_paths(self, world, lists):
        domain = make_domain('blocked.example')
        world.posts['first'].domain_id = domain.id
        db.session.commit()
        ids = {'person': {'blocked_person_ids': [world.author.id]},
               'community': {'blocked_community_ids': [world.here.id]},
               'instance': {'blocked_instance_ids': [world.author.instance_id]},
               'domain': {'blocked_domain_ids': [domain.id]}}[lists]
        built = build(Listing(sort='New'), restrictions=restricted(**ids))
        expected = ['second', 'third'] if lists == 'domain' else []
        assert listed(built) == fast_listed(built) == expected

    @pytest.mark.parametrize('type_', ['Moderating', 'ModeratorView'])
    def test_a_moderator_sees_past_their_domain_blocks(self, world, type_):
        domain = make_domain('blocked.example')
        world.posts['first'].domain_id = domain.id
        make_community_member(world.reader, world.here, is_moderator=True)
        built = build(Listing(type=type_, sort='New'), user=world.reader,
                      restrictions=restricted(blocked_domain_ids=[domain.id]))
        assert listed(built) == fast_listed(built) == ['first', 'second', 'third']
        assert not any('blocked_domain_ids' in criterion for criterion in built.fast.criteria)

    def test_a_private_community_is_for_its_members(self, world):
        world.here.private = True
        db.session.commit()
        assert listed(build(Listing())) == fast_listed(build(Listing())) == []
        member = build(Listing(sort='New'), restrictions=restricted(private_community_ids=[world.here.id]))
        assert listed(member) == fast_listed(member) == ['first', 'second', 'third']

    @pytest.mark.parametrize('spelling', SPELLINGS, ids=SPELLING_IDS)
    def test_divergence_read_languages_in_the_query(self, world, spelling):
        """get_post_list filters the viewer's languages only on its fast path; get_post_list2 in its query."""
        english = Language.query.filter_by(code='en').first() or Language(code='en', name='English')
        other = Language(code='xx', name='Other')
        db.session.add_all([english, other])
        db.session.commit()
        world.posts['first'].language_id = other.id
        db.session.commit()
        built = build(Listing(sort='New'), restrictions=restricted(read_language_ids=[english.id]),
                      spelling=spelling)
        assert listed(built) == (['first', 'second', 'third'] if spelling is OFFSET_PAGES else ['second', 'third'])
        if built.fast:
            assert fast_listed(built) == ['second', 'third']

    def test_an_anonymous_reader_is_not_shown_local_only_posts(self, world):
        world.posts['first'].local_only = True
        db.session.commit()
        anonymous = build(Listing(sort='New'))
        assert listed(anonymous) == fast_listed(anonymous) == ['second', 'third']
        signed_in = build(Listing(sort='New'), user=world.reader)
        assert listed(signed_in) == fast_listed(signed_in) == ['first', 'second', 'third']


# ------------------------------------------------------------------ search

class TestSearch:
    def test_a_url_search_keeps_the_fast_path(self, world):
        world.posts['second'].url = 'https://example.test/x'
        db.session.commit()
        built = build(Listing(query='example.test', search_type='Url'))
        assert listed(built) == fast_listed(built) == ['second']
        assert 'instance_sticky DESC' not in built.fast.order_by   # a search is not the front page

    @pytest.mark.parametrize('sort', ['Relevance', 'New'])
    def test_a_full_text_search_has_no_fast_path(self, world, sort):
        world.posts['first'].body = 'a needle in here'
        world.posts['first'].indexable = True
        db.session.commit()
        built = build(Listing(query='needle', sort=sort))
        assert listed(built) == ['first']
        assert built.fast is None


# ----------------------------------------------------------- viewer filters

class TestViewer:
    def test_liked_only_is_what_you_upvoted_not_your_own(self, world):
        make_post_vote(world.reader, world.posts['first'], 1)
        built = build(Listing(liked_only=True), user=world.reader)
        assert listed(built) == ['first']
        assert built.fast is None and built.read_post_ids == ()

    def test_saved_only_is_what_you_saved(self, world):
        db.session.add(PostBookmark(user_id=world.reader.id, post_id=world.posts['second'].id))
        db.session.commit()
        built = build(Listing(saved_only=True), user=world.reader)
        assert listed(built) == ['second']
        assert built.fast is None and built.sticky_columns == ()

    @pytest.mark.parametrize('setting, column', [('ignore_bots', 'from_bot'), ('ignore_reposters', 'from_reposter'),
                                                 ('hide_nsfl', 'nsfl'), ('hide_nsfw', 'nsfw'),
                                                 ('hide_gen_ai', 'ai_generated')],
                             ids=['bots', 'reposters', 'nsfl', 'nsfw', 'gen-ai'])
    def test_a_setting_hides_its_posts_on_both_paths(self, world, setting, column):
        setattr(world.posts['first'], column, True)
        setattr(world.reader, setting, 0)
        db.session.commit()
        assert 'first' in listed(build(Listing(), user=world.reader))
        setattr(world.reader, setting, 1)
        db.session.commit()
        built = build(Listing(sort='New'), user=world.reader)
        assert listed(built) == fast_listed(built) == ['second', 'third']

    @pytest.mark.parametrize('nsfw, expected', [('', ['first', 'second', 'third']), ('Include', ['first', 'second',
                                                                                                'third']),
                                                ('Exclude', ['second', 'third']), ('Only', ['first'])],
                             ids=['unasked', 'include', 'exclude', 'only'])
    def test_a_viewer_asking_about_nsfw(self, world, nsfw, expected):
        world.posts['first'].nsfw = True
        world.reader.hide_nsfw = 1 if nsfw else 0     # the request's own choice wins over the setting
        db.session.commit()
        built = build(Listing(sort='New', nsfw=nsfw), user=world.reader)
        assert listed(built) == fast_listed(built) == expected

    @pytest.mark.parametrize('nsfw, expected', [('', ['second', 'third']), ('Exclude', ['second', 'third']),
                                                ('Only', ['first']), ('Include', ['first', 'second', 'third'])],
                             ids=['unasked', 'exclude', 'only', 'include'])
    def test_nobody_signed_in_asking_about_nsfw(self, world, nsfw, expected):
        world.posts['first'].nsfw = True
        db.session.commit()
        built = build(Listing(sort='New', nsfw=nsfw))
        assert listed(built) == fast_listed(built) == expected

    def test_hide_read_on_both_paths(self, world):
        world.reader.hide_read_posts = True
        db.session.commit()
        nothing_read = build(Listing(sort='New'), user=world.reader)
        assert nothing_read.read_post_ids == ()
        assert not any('read_post_ids' in criterion for criterion in nothing_read.fast.criteria)
        mark_post_read(world.reader, world.posts['first'])
        built = build(Listing(sort='New'), user=world.reader)
        assert built.read_post_ids == (world.posts['first'].id,)
        assert listed(built) == fast_listed(built) == ['second', 'third']

    @pytest.mark.parametrize('spelling', SPELLINGS, ids=SPELLING_IDS)
    def test_divergence_hide_read_while_searching(self, world, spelling):
        """get_post_list shows read posts in a search; get_post_list2 hides them there too."""
        world.reader.hide_read_posts = True
        world.posts['first'].url = 'https://example.test/x'
        db.session.commit()
        mark_post_read(world.reader, world.posts['first'])
        built = build(Listing(query='example.test', search_type='Url'), user=world.reader, spelling=spelling)
        assert listed(built) == ([] if spelling.hides_read_when_searching else ['first'])

    @pytest.mark.parametrize('spelling', SPELLINGS, ids=SPELLING_IDS)
    def test_divergence_hidden_posts(self, world, spelling):
        """get_post_list leaves out the posts you hid (not in a search); get_post_list2 lists them."""
        world.posts['first'].url = 'https://example.test/x'
        db.session.commit()
        assert 'first' in listed(build(Listing(), user=world.reader, spelling=spelling))   # nothing hidden yet
        hide_post(world.reader, world.posts['first'])
        built = build(Listing(sort='New'), user=world.reader, spelling=spelling)
        assert listed(built) == (['second', 'third'] if spelling.hides_hidden_posts else ['first', 'second', 'third'])
        if built.fast:
            assert fast_listed(built) == ['second', 'third']
        searched = build(Listing(query='example.test', search_type='Url'), user=world.reader, spelling=spelling)
        assert listed(searched) == ['first']

    def test_a_community_keyword_filter(self, world):
        world.reader.community_keyword_filter = 'here'
        db.session.commit()
        built = build(Listing(), user=world.reader)
        assert listed(built) == fast_listed(built) == []

    def test_minimum_upvotes_reads_the_score(self, world):
        built = build(Listing(sort='New', minimum_upvotes=20))
        assert listed(built) == fast_listed(built) == ['first', 'second']


# ------------------------------------------------------------- stickies

class TestStickies:
    def test_a_community_listing_puts_its_stickies_first(self, world):
        world.posts['third'].sticky = True
        db.session.commit()
        built = build(Listing(community_id=world.here.id, sort='New'))
        assert listed(built) == ['third', 'first', 'second']
        assert built.sticky_columns == (Post.sticky,)
        ignored = build(Listing(community_id=world.here.id, sort='New', ignore_sticky=True))
        assert listed(ignored) == ['first', 'second', 'third']
        assert ignored.sticky_columns == ()

    def test_the_front_page_puts_instance_stickies_first(self, world):
        world.posts['third'].instance_sticky = True
        db.session.commit()
        built = build(Listing(sort='New'))
        assert listed(built) == fast_listed(built) == ['third', 'first', 'second']
        assert built.sticky_columns == (Post.instance_sticky,)
        ignored = build(Listing(sort='New', ignore_sticky=True))
        assert listed(ignored) == fast_listed(ignored) == ['first', 'second', 'third']


# ------------------------------------------------------------------ sorts

class TestSorts:
    @pytest.mark.parametrize('spelling', SPELLINGS, ids=SPELLING_IDS)
    @pytest.mark.parametrize('sort, rank, tail', [('Hot', Post.ranking, (Post.posted_at,)),
                                                  ('Scaled', Post.ranking_scaled, (Post.ranking, Post.posted_at))],
                             ids=['hot', 'scaled'])
    def test_a_boosted_sort_is_left_to_the_caller(self, world, spelling, sort, rank, tail):
        built = build(Listing(sort=sort), spelling=spelling)
        assert built.boosted == (rank, tail)
        assert sorted(listed(built)) == ['first', 'second', 'third']
        if built.fast:      # the fast path reads the boosted tiers itself
            assert fast_listed(built) == ['first', 'second', 'third']

    def test_scaled_leaves_out_bots(self, world):
        world.posts['first'].from_bot = True
        db.session.commit()
        built = build(Listing(sort='Scaled'))
        assert sorted(listed(built)) == sorted(fast_listed(built)) == ['second', 'third']

    @pytest.mark.parametrize('spelling', SPELLINGS, ids=SPELLING_IDS)
    @pytest.mark.parametrize('sort', sorted(TOP_WINDOWS) + ['TopUnknown'])
    def test_top_orders_by_score_within_its_window(self, world, spelling, sort):
        world.posts['first'].posted_at = utcnow() - timedelta(days=400)
        db.session.commit()
        built = build(Listing(sort=sort), spelling=spelling)
        # every window holds the two recent posts; only all time reaches back 400 days
        expected = ['first', 'second', 'third'] if sort == 'TopAll' else ['second', 'third']
        assert listed(built) == expected
        if built.fast:
            assert fast_listed(built) == expected
        tiebreak = spelling.keyset_tiebreaks and sort in TOP_WINDOWS
        assert order_by(built).endswith('post.score DESC, post.score DESC, post.id DESC') == tiebreak

    @pytest.mark.parametrize('spelling', SPELLINGS, ids=SPELLING_IDS)
    def test_new_and_its_keyset_tiebreak(self, world, spelling):
        built = build(Listing(sort='New', ignore_sticky=True), spelling=spelling)
        assert listed(built) == ['first', 'second', 'third']
        assert order_by(built) == ('post.posted_at DESC, post.posted_at DESC, post.id DESC'
                                   if spelling.keyset_tiebreaks else 'post.posted_at DESC')

    @pytest.mark.parametrize('spelling', SPELLINGS, ids=SPELLING_IDS)
    def test_divergence_a_sort_starting_new(self, world, spelling):
        """get_post_list orders any 'New...' sort as New; get_post_list2 only 'New' itself."""
        built = build(Listing(sort='NewComments', ignore_sticky=True), spelling=spelling)
        assert order_by(built) == ('post.posted_at DESC' if spelling.new_sorts_by_prefix else '')

    @pytest.mark.parametrize('spelling', SPELLINGS, ids=SPELLING_IDS)
    def test_divergence_active_needs_a_reply(self, world, spelling):
        """Both order Active by last_active; get_post_list2 also lists only posts with a reply."""
        world.posts['first'].reply_count = 0
        world.posts['third'].last_active = utcnow()
        db.session.commit()
        built = build(Listing(sort='Active', ignore_sticky=True), spelling=spelling)
        assert listed(built) == (['third', 'second'] if spelling.active_needs_replies else ['third', 'first', 'second'])
        if built.fast:
            assert fast_listed(built) == listed(built)
        assert order_by(built).endswith('post.id DESC') == spelling.keyset_tiebreaks

    @pytest.mark.parametrize('spelling', SPELLINGS, ids=SPELLING_IDS)
    def test_old_is_oldest_first_with_no_tiebreak(self, world, spelling):
        built = build(Listing(sort='Old', ignore_sticky=True), spelling=spelling)
        assert listed(built) == ['third', 'second', 'first']
        assert order_by(built) == 'post.posted_at ASC'

    @pytest.mark.parametrize('sort', ['Relevance', 'Controversial'])
    def test_an_unordered_sort_orders_nothing_and_the_fast_path_still_reads(self, world, sort):
        """D1225: with the stickies ignored there is no ORDER BY at all, and the raw SQL must not write one."""
        built = build(Listing(sort=sort, ignore_sticky=True))
        assert built.boosted is None and built.fast.order_by == ()
        assert sorted(fast_listed(built)) == ['first', 'second', 'third']


@pytest.mark.parametrize('spelling', SPELLINGS, ids=SPELLING_IDS)
def test_divergence_polls_and_events(world, spelling):
    """get_post_list lists polls and events; get_post_list2 leaves them out until it supports them."""
    world.posts['first'].type = POST_TYPE_POLL
    world.posts['second'].type = POST_TYPE_EVENT
    db.session.commit()
    built = build(Listing(sort='New'), spelling=spelling)
    assert listed(built) == (['third'] if spelling.hides_polls_and_events else ['first', 'second', 'third'])


def test_content_filters_come_with_the_scope(world, monkeypatch):
    """The front page carries the viewer's home filters; a narrowed listing their post filters; nobody has none."""
    monkeypatch.setattr(posts_module, 'user_filters_home', lambda user_id: {'home': user_id})
    monkeypatch.setattr(posts_module, 'user_filters_posts', lambda user_id: {'posts': user_id})
    reader = world.reader
    assert build(Listing(), user=reader).content_filters == {'home': reader.id}
    assert build(Listing(community_id=world.here.id), user=reader).content_filters == {'posts': reader.id}
    assert build(Listing()).content_filters == {}
    assert build(Listing(person_id=world.author.id), user=reader).content_filters == {}
