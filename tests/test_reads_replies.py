"""app/reads/replies.py: a post's reply tree, and the comment listings, as a viewer may see them.

The thread is asked through `thread_replies` with each of its two spellings (WHOLE_THREAD for post_replies,
ONE_BRANCH for get_comment_branch); the listings through `comment_listing`, a flat listing or a conversation. The tests
named after a divergence pin it, so a ruling that unifies two reads changes a test on purpose rather than by accident.
"""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app import db
from app.constants import VISIBILITY_FOLLOWERS
from app.models import Language, PostReply, utcnow
from app.reads.replies import (CONVERSATION_BLOCKS, LISTING_BLOCKS, ONE_BRANCH, TOP_WINDOWS, TREE_BLOCKS,
                               WHOLE_THREAD, CommentListing, _without_blocked, comment_listing, reply_branch,
                               reply_tree, thread_replies)
from tests.factories import make_community, make_community_member, make_post, make_post_reply, make_user

RESTRICTION_LISTS = ('blocked_person_ids', 'blocked_community_ids', 'blocked_instance_ids', 'blocked_domain_ids',
                     'private_community_ids', 'read_language_ids')


def restricted(**lists):
    """A viewer's restrictions as Restrictions answers them, with only `lists` set."""
    return SimpleNamespace(**{name: lists.get(name, []) for name in RESTRICTION_LISTS})


NONE = restricted()


@pytest.fixture
def world(app, api_baseline):
    """A post with a thread: `top` (a minute old), `child` under it and `grandchild` under that, and `second` (two
    minutes old), each with the path and root PostReply.new gives it."""
    here = make_community('here')
    author = make_user(api_baseline.instance_local, 'writer', local=True)
    post = make_post(here, author, 'https://test.piefed.local/p/1')
    now = utcnow()

    def reply(body, parent=None, minutes=1, score=0):
        row = make_post_reply(post, author, body=body)
        row.posted_at = now - timedelta(minutes=minutes)
        row.score = row.up_votes = score
        row.down_votes = 0
        row.ranking = score
        if parent is None:
            row.depth, row.path, row.root_id = 0, [0, row.id], row.id
        else:
            row.parent_id, row.depth = parent.id, parent.depth + 1
            row.path, row.root_id = parent.path + [row.id], parent.root_id
        db.session.commit()
        return row

    top = reply('top', score=3)
    child = reply('child', top, minutes=1, score=2)
    grandchild = reply('grandchild', child, minutes=1, score=1)
    second = reply('second', minutes=2, score=5)
    return SimpleNamespace(here=here, author=author, post=post, top=top, child=child, grandchild=grandchild,
                           second=second, reader=api_baseline.user3, remote=api_baseline.instance_remote,
                           base=api_baseline, reply=reply)


def bodies(rows) -> list[str]:
    return [row.body for row in rows]


def tree_bodies(tree) -> list:
    return [(entry['comment'].body, tree_bodies(entry['replies'])) for entry in tree]


def thread(world, viewer=None, restrictions=NONE, sort_by='new', spelling=WHOLE_THREAD, **options):
    return bodies(thread_replies(world.post, viewer, restrictions, sort_by, spelling, **options).all())


SPELLINGS = [WHOLE_THREAD, ONE_BRANCH]
SPELLING_IDS = ['whole-thread', 'one-branch']


# ------------------------------------------------------------------ the thread

class TestThread:
    @pytest.mark.parametrize('sort_by, expected', [
        ('new', ['top', 'child', 'grandchild', 'second']),
        ('old', ['second', 'grandchild', 'child', 'top']),
        ('top', ['second', 'top', 'child', 'grandchild']),
        ('hot', ['second', 'top', 'child', 'grandchild']),
    ], ids=['new', 'old', 'top', 'hot'])
    def test_its_sorts(self, world, sort_by, expected):
        for minutes, row in enumerate([world.top, world.child, world.grandchild], start=1):
            row.posted_at = utcnow() - timedelta(seconds=minutes)
        db.session.commit()
        assert thread(world, sort_by=sort_by) == expected

    def test_an_unknown_sort_orders_nothing(self, world):
        assert 'ORDER BY' not in str(thread_replies(world.post, None, NONE, 'sideways', WHOLE_THREAD).statement)

    @pytest.mark.parametrize('spelling, limited', [(WHOLE_THREAD, True), (ONE_BRANCH, False)], ids=SPELLING_IDS)
    def test_the_whole_thread_reads_at_most_2000(self, world, spelling, limited):
        built = thread_replies(world.post, None, NONE, 'new', spelling)
        assert (built._limit_clause is not None) == limited

    def test_an_anonymous_reader_loses_the_silenced_instances(self, world):
        world.top.instance_id = world.remote.id
        world.second.instance_id = None
        db.session.commit()
        assert sorted(thread(world, silenced_instance_ids=[world.remote.id])) == ['child', 'grandchild', 'second']
        assert len(thread(world)) == 4

    @pytest.mark.parametrize('leave_out', [True, False], ids=['left-out', 'kept'])
    def test_a_reply_hidden_here(self, world, leave_out):
        world.second.local_only = True
        db.session.commit()
        assert ('second' in thread(world, leave_out_hidden_here=leave_out)) is not leave_out

    def test_a_viewers_blocks_spare_a_reply_with_no_instance(self, world):
        world.top.instance_id = world.remote.id
        world.second.instance_id = None
        world.child.user_id = world.reader.id
        db.session.commit()
        blocks = restricted(blocked_instance_ids=[world.remote.id], blocked_person_ids=[world.reader.id])
        assert thread(world, viewer=world.base.user1, restrictions=blocks) == ['grandchild', 'second']

    @pytest.mark.parametrize('setting', ['ignore_bots', 'ignore_reposters'], ids=['bots', 'reposters'])
    def test_a_viewer_who_ignores_bots_loses_their_replies(self, world, setting):
        world.second.from_bot = True
        setattr(world.reader, setting, 1)
        db.session.commit()
        assert 'second' not in thread(world, viewer=world.reader)

    @pytest.mark.parametrize('spelling, spared', [(WHOLE_THREAD, True), (ONE_BRANCH, False)], ids=SPELLING_IDS)
    def test_the_hide_threshold(self, world, spelling, spared):
        """Divergence: only the whole thread spares a reply that is not collapsible."""
        world.reader.reply_hide_threshold = 1
        world.grandchild.collapsible = False
        db.session.commit()
        shown = thread(world, viewer=world.reader, spelling=spelling)
        assert sorted(shown) == sorted(['top', 'child', 'second'] + (['grandchild'] if spared else []))

    def test_a_moderator_sees_under_the_hide_threshold(self, world):
        world.reader.reply_hide_threshold = 100
        make_community_member(world.reader, world.here, is_moderator=True)
        db.session.commit()
        assert len(thread(world, viewer=world.reader)) == 4

    def test_a_viewer_reads_their_languages(self, world):
        read, unread = Language(code='xr', name='Read'), Language(code='xu', name='Unread')
        db.session.add_all([read, unread])
        db.session.commit()
        world.second.language_id = unread.id
        world.top.language_id = read.id
        world.reader.read_language_ids = [read.id]
        db.session.commit()
        assert sorted(thread(world, viewer=world.reader)) == ['child', 'grandchild', 'top']

    def test_a_viewer_with_no_settings_sees_everything(self, world):
        world.reader.reply_hide_threshold = None
        world.reader.read_language_ids = []
        db.session.commit()
        assert len(thread(world, viewer=world.reader)) == 4


class TestTree:
    def test_the_tree_places_each_reply_under_its_parent(self, world):
        rows = thread_replies(world.post, None, NONE, 'new', WHOLE_THREAD).all()
        assert tree_bodies(reply_tree(rows)) == [('top', [('child', [('grandchild', [])])]), ('second', [])]

    def test_a_reply_whose_parent_is_not_read_is_not_placed(self, world):
        rows = [world.top, world.grandchild, world.second]
        assert tree_bodies(reply_tree(rows)) == [('top', []), ('second', [])]

    def test_a_branch_is_its_root_and_what_is_under_it(self, world):
        rows = thread_replies(world.post, None, NONE, 'new', ONE_BRANCH).all()
        assert tree_bodies(reply_branch(rows, world.child.id)) == [('child', [('grandchild', [])])]
        assert reply_branch(rows, 999999) == []


# ------------------------------------------------------------------ blocks

class TestBlocks:
    def query(self):
        return PostReply.query.filter(PostReply.body.in_(['top', 'child', 'grandchild', 'second']))

    @pytest.mark.parametrize('spelling, expected', [
        (TREE_BLOCKS, ['child', 'grandchild']),
        (LISTING_BLOCKS, ['child', 'grandchild']),
        (CONVERSATION_BLOCKS, []),
    ], ids=['tree', 'listing', 'conversation'])
    def test_a_blocked_person(self, world, spelling, expected):
        """Only a conversation takes a blocked reply's own replies with it in the query."""
        world.top.user_id = world.second.user_id = world.reader.id
        db.session.commit()
        rows = _without_blocked(self.query(), restricted(blocked_person_ids=[world.reader.id]), spelling)
        assert sorted(bodies(rows.all())) == expected

    @pytest.mark.parametrize('spelling, expected', [
        (TREE_BLOCKS, ['child', 'grandchild', 'second']),
        (LISTING_BLOCKS, ['child', 'grandchild']),
        (CONVERSATION_BLOCKS, []),
    ], ids=['tree', 'listing', 'conversation'])
    def test_a_blocked_instance(self, world, spelling, expected):
        """Divergence: the tree spares a reply with no instance; the listings do not."""
        world.top.instance_id = world.remote.id
        world.second.instance_id = None
        db.session.commit()
        rows = _without_blocked(self.query(), restricted(blocked_instance_ids=[world.remote.id]), spelling)
        assert sorted(bodies(rows.all())) == expected

    def test_no_blocks_leave_the_query_alone(self, world):
        query = self.query()
        assert _without_blocked(query, NONE, CONVERSATION_BLOCKS) is query


# ------------------------------------------------------------------ comment listings

def listing(world, user_id=None, restrictions=NONE, **fields):
    return comment_listing(user_id, restrictions, CommentListing(**fields))


def listed(rows) -> list[str]:
    return bodies(rows.replies.all())


class TestListings:
    def test_everything_when_nothing_narrows_it(self, world):
        rows = listing(world)
        assert not rows.conversation and not rows.one_page and rows.next_page is None
        assert {'top', 'child', 'grandchild', 'second'} <= set(listed(rows))

    def test_an_anonymous_reader_may_not_page_deep_into_everything(self, world):
        with pytest.raises(Exception, match='^unknown$'):
            listing(world, page=1001, limit=10)
        assert listing(world, user_id=world.reader.id, page=1001, limit=10).replies is not None

    @pytest.mark.parametrize('field', ['liked_only', 'saved_only'], ids=['liked', 'saved'])
    def test_liked_and_saved_want_a_viewer(self, world, field):
        with pytest.raises(Exception, match=f'^Login required for {field} query$'):
            listing(world, **{field: True})

    def test_liked_lists_what_you_upvoted_but_not_your_own(self, world):
        world.child.user_id = world.reader.id
        db.session.commit()
        rows = comment_listing(world.reader.id, NONE, CommentListing(liked_only=True),
                               liked_reply_ids=[world.top.id, world.child.id])
        assert listed(rows) == ['top']

    def test_saved_lists_what_you_bookmarked(self, world):
        rows = comment_listing(world.reader.id, NONE, CommentListing(saved_only=True),
                               saved_reply_ids=[world.second.id])
        assert listed(rows) == ['second']

    def test_saved_narrows_liked(self, world):
        rows = comment_listing(world.reader.id, NONE, CommentListing(liked_only=True, saved_only=True),
                               liked_reply_ids=[world.top.id, world.second.id], saved_reply_ids=[world.second.id])
        assert listed(rows) == ['second']

    @pytest.mark.parametrize('narrowed', [False, True], ids=['alone', 'narrowing-liked'])
    def test_a_search(self, world, narrowed):
        rows = comment_listing(world.reader.id, NONE, CommentListing(query='second', liked_only=narrowed,
                                                                     sort='Relevance'),
                               liked_reply_ids=[world.second.id])
        assert listed(rows) in ([], ['second'])   # the search vector is the database's to fill
        assert 'indexable' in str(rows.replies.statement)

    @pytest.mark.parametrize('narrowed', [False, True], ids=['alone', 'narrowing-saved'])
    def test_an_authors_comments(self, world, narrowed):
        world.second.user_id = world.reader.id
        db.session.commit()
        rows = comment_listing(world.base.user1.id, NONE, CommentListing(person_id=world.reader.id,
                                                                         saved_only=narrowed),
                               saved_reply_ids=[world.second.id])
        assert listed(rows) == ['second']

    @pytest.mark.parametrize('narrowed', [False, True], ids=['alone', 'narrowing-an-author'])
    def test_a_communitys_comments(self, world, narrowed):
        rows = listing(world, community_id=world.here.id, person_id=world.author.id if narrowed else None)
        assert sorted(listed(rows)) == ['child', 'grandchild', 'second', 'top']

    def test_a_post_narrows_a_listing(self, world):
        other = make_post(world.here, world.author, 'https://test.piefed.local/p/2')
        make_post_reply(other, world.author, body='elsewhere')
        rows = listing(world, community_id=world.here.id, post_id=world.post.id)
        assert not rows.conversation
        assert 'elsewhere' not in listed(rows)

    def test_a_listings_depth_is_below_max_depth(self, world):
        assert sorted(listed(listing(world, community_id=world.here.id, max_depth=1))) == ['second', 'top']

    def test_a_listing_drops_what_the_viewer_blocked(self, world):
        world.second.user_id = world.reader.id
        db.session.commit()
        rows = listing(world, world.base.user1.id, restricted(blocked_person_ids=[world.reader.id]),
                       community_id=world.here.id)
        assert 'second' not in listed(rows)

    @pytest.mark.parametrize('profile', [False, True], ids=['listing', 'profile'])
    def test_a_followers_only_reply(self, world, profile):
        """A listing omits it; a profile listing shows it to whoever may see it (here its author)."""
        world.second.visibility = VISIBILITY_FOLLOWERS
        db.session.commit()
        rows = listing(world, world.author.id, community_id=world.here.id,
                       person_id=world.author.id if profile else None)
        assert ('second' in listed(rows)) is profile

    def test_a_local_only_reply_is_not_for_an_anonymous_reader(self, world):
        world.second.local_only = True
        db.session.commit()
        assert 'second' not in listed(listing(world, community_id=world.here.id))
        assert 'second' in listed(listing(world, world.reader.id, community_id=world.here.id))


class TestListingScopes:
    def test_local_is_this_instance_only(self, world):
        world.second.ap_id = 'https://remote.piefed.test/comment/9'
        db.session.commit()
        shown = listed(listing(world, community_id=world.here.id, type='Local'))
        assert 'second' not in shown and 'top' in shown

    @pytest.mark.parametrize('type_', ['Moderating', 'ModeratorView', 'Subscribed'],
                             ids=['moderating', 'moderator-view', 'subscribed'])
    def test_your_own_communities_want_a_viewer(self, world, type_):
        with pytest.raises(Exception, match='^incorrect login$'):
            listing(world, community_id=world.here.id, type=type_)

    @pytest.mark.parametrize('type_, moderator', [('Moderating', True), ('ModeratorView', True),
                                                  ('Subscribed', False)],
                             ids=['moderating', 'moderator-view', 'subscribed'])
    def test_your_own_communities(self, world, type_, moderator):
        assert listed(listing(world, world.reader.id, community_id=world.here.id, type=type_)) == []
        make_community_member(world.reader, world.here, is_moderator=moderator)
        assert len(listed(listing(world, world.reader.id, community_id=world.here.id, type=type_))) == 4


class TestConversations:
    def test_a_posts_thread(self, world):
        rows = listing(world, post_id=world.post.id)
        assert rows.conversation and not rows.one_page
        assert sorted(listed(rows)) == ['child', 'grandchild', 'second', 'top']

    def test_a_posts_thread_to_a_depth(self, world):
        assert sorted(listed(listing(world, post_id=world.post.id, max_depth=1))) == ['child', 'second', 'top']

    def test_a_parent_nobody_holds(self, world):
        with pytest.raises(Exception, match=r'^Comment with parent_id not found\.$'):
            listing(world, parent_id=999999)

    @pytest.mark.parametrize('parent, max_depth, expected', [
        ('top', None, ['child', 'grandchild', 'top']),
        ('top', 1, ['child', 'top']),
        ('child', None, ['child', 'grandchild']),
        ('child', 0, ['child']),
    ], ids=['root', 'root-to-a-depth', 'deeper', 'deeper-to-a-depth'])
    def test_a_parents_thread(self, world, parent, max_depth, expected):
        rows = listing(world, parent_id=getattr(world, parent).id, max_depth=max_depth)
        assert rows.conversation
        assert sorted(listed(rows)) == expected

    def test_a_conversation_drops_a_blocked_reply_and_its_replies(self, world):
        world.child.user_id = world.reader.id
        db.session.commit()
        rows = listing(world, world.base.user1.id, restricted(blocked_person_ids=[world.reader.id]),
                       post_id=world.post.id)
        assert sorted(listed(rows)) == ['second', 'top']


class TestDepthFirst:
    @pytest.mark.parametrize('parent, max_depth, expected', [
        (None, None, ['child', 'grandchild', 'second', 'top']),
        (None, 0, ['second', 'top']),
        ('top', None, ['child', 'grandchild', 'top']),
        ('child', None, ['child', 'grandchild', 'top']),   # a page carries every reply's parents
    ], ids=['post', 'post-to-a-depth', 'root', 'deeper'])
    def test_whole_branches(self, world, parent, max_depth, expected):
        fields = {'parent_id': getattr(world, parent).id} if parent else {'post_id': world.post.id}
        rows = listing(world, depth_first=True, max_depth=max_depth, limit=10, **fields)
        assert rows.conversation and rows.one_page and rows.next_page is None
        assert sorted(listed(rows)) == expected

    @pytest.mark.parametrize('sort, first', [('Hot', 'second'), ('Top', 'second'), ('Old', 'second'),
                                             ('New', 'top')], ids=['hot', 'top', 'old', 'new'])
    def test_its_sorts_choose_the_first_branch(self, world, sort, first):
        """A page holds whole branches; with a limit of one, the first page is the sort's first branch."""
        world.second.up_votes = 10
        db.session.commit()
        rows = listing(world, post_id=world.post.id, depth_first=True, limit=1, sort=sort)
        assert first in listed(rows)
        assert rows.next_page == '2'

    def test_the_pages_end_with_one_naming_no_next(self, world):
        seen, page = set(), 1
        while page:
            rows = listing(world, post_id=world.post.id, depth_first=True, limit=1, page=page)
            seen.update(listed(rows))
            page = int(rows.next_page) if rows.next_page else None
        assert seen == {'top', 'child', 'grandchild', 'second'}


class TestListingSorts:
    def order_by(self, rows) -> str:
        sql = str(rows.replies.statement.compile())
        return sql.split('ORDER BY', 1)[1].strip() if 'ORDER BY' in sql else ''

    @pytest.mark.parametrize('sort', ['Hot', 'Scaled'], ids=['hot', 'scaled'])
    def test_hot(self, world, sort):
        assert self.order_by(listing(world, community_id=world.here.id, sort=sort)) == \
            'post_reply.ranking DESC, post_reply.posted_at DESC'

    @pytest.mark.parametrize('sort', ['Top', 'TopAll'], ids=['top', 'top-all'])
    def test_top_looks_back_for_ever(self, world, sort):
        rows = listing(world, community_id=world.here.id, sort=sort)
        assert listed(rows)[0] == 'second'
        assert 'posted_at >' not in str(rows.replies.statement)

    @pytest.mark.parametrize('sort', sorted(TOP_WINDOWS), ids=sorted(TOP_WINDOWS))
    def test_a_top_window(self, world, sort):
        world.second.posted_at = utcnow() - TOP_WINDOWS[sort] - timedelta(minutes=5)
        world.top.posted_at = world.child.posted_at = world.grandchild.posted_at = utcnow()
        db.session.commit()
        assert listed(listing(world, community_id=world.here.id, sort=sort)) == ['top', 'child', 'grandchild']

    def test_old(self, world):
        assert listed(listing(world, community_id=world.here.id, sort='Old'))[0] == 'second'

    def test_relevance_is_the_searchs_own_order(self, world):
        assert self.order_by(listing(world, community_id=world.here.id, sort='Relevance')) == ''

    def test_controversial_divides_by_one_at_least(self, world):
        rows = listing(world, community_id=world.here.id, sort='Controversial')
        assert len(listed(rows)) == 4
        assert 'greatest' in self.order_by(rows)

    @pytest.mark.parametrize('sort', ['New', 'Active', 'Sideways'], ids=['new', 'active', 'unknown'])
    def test_anything_else_is_new(self, world, sort):
        assert self.order_by(listing(world, community_id=world.here.id, sort=sort)) == 'post_reply.posted_at DESC'
