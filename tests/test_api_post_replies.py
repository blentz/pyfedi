"""The post API's last two functions: `get_post`, and `get_post_replies`,
which serves a post's comment tree.

Sub-project 84, slice O -- the slice that closes
`app/api/alpha/utils/post.py`. Four defects, all measured first:

* `if max_depth:` where `if max_depth is not None:` was meant. A `max_depth`
  of 0 means "the top level and nothing under it" -- and 0 is falsy, so the
  one depth a caller is most likely to ask for was the one silently ignored:
  the whole tree came back (D1237);
* asked for the replies to no post at all, the endpoint left `post_id` None,
  handed it to `db.session.get(Post, None)` and died inside `post_replies`
  with `'NoneType' object has no attribute 'archived'`. A post id nobody
  holds took the same path, and a `parent_id` nobody holds died one line
  earlier on `parent.post_id` (D1238);
* `get_post` answered a bare `NoResultFound: ()` for an id nobody holds --
  `post_view` looks the row up with `.one()` (D1239);
* and `invalid literal for int() with base 10: 'abc'` for an id that is not a
  number (D1240).
"""
import pytest
from flask import current_app, g

from app import db
from app.api.alpha.utils.post import get_post, get_post_replies
from app.models import Post, PostReply, Site
from tests.factories import (make_community, make_community_member, make_post,
                             make_post_reply, make_post_reply_bookmark,
                             make_post_reply_vote, make_user)

MISSING = 999999


def token(user):
    return f'Bearer {user.encode_jwt_token()}'


def flatten(tree):
    """Every comment in a nested reply tree, depth first."""
    found = []
    for item in tree:
        found.append(item['comment']['body'])
        found.extend(flatten(item['replies']))
    return found


@pytest.fixture
def env(app, api_baseline):
    """One post with a three-deep thread in it: top -> child -> grand.

    The factory does not set `path`, `depth` or `root_id` -- `PostReply.new`
    does, and nothing else does (fact 561) -- so this fixture sets them, which
    is what the tree walk reads.
    """
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('probeland')
    author = make_user(api_baseline.instance_local, 'writer', local=True)
    make_community_member(author, community)
    post = make_post(community, author, ap_id='https://test.piefed.local/t/1')
    top = make_post_reply(post, author, body='top')
    top.path = [0, top.id]
    top.depth = 0
    child = make_post_reply(post, author, body='child')
    child.parent_id = top.id
    child.root_id = top.id
    child.path = [0, top.id, child.id]
    child.depth = 1
    grand = make_post_reply(post, author, body='grand')
    grand.parent_id = child.id
    grand.root_id = top.id
    grand.path = [0, top.id, child.id, grand.id]
    grand.depth = 2
    db.session.commit()
    return SimpleNamespace(community=community, author=author, post=post,
                           top=top, child=child, grand=grand,
                           reader=api_baseline.user3, baseline=api_baseline)


class TestOnePost:
    def test_naming_no_post_is_refused(self, env):
        with pytest.raises(Exception, match='missing parameters for post'):
            get_post(None, {})

    def test_naming_nothing_at_all_is_refused(self, env):
        with pytest.raises(Exception, match='missing parameters for post'):
            get_post(None, None)

    def test_an_id_that_is_not_a_number_is_refused_by_name(self, env):
        """D1240."""
        with pytest.raises(Exception, match='id must be a number'):
            get_post(None, {'id': 'abc'})

    def test_a_post_nobody_holds_is_refused_by_name(self, env):
        """D1239. `post_view` looks the row up with `.one()`, so this was a
        bare `NoResultFound: ()`."""
        with pytest.raises(Exception, match='post not found'):
            get_post(None, {'id': MISSING})

    def test_a_post(self, env):
        res = get_post(None, {'id': env.post.id})
        assert res['post_view']['post']['id'] == env.post.id

    def test_a_post_read_by_an_account(self, env):
        res = get_post(token(env.reader), {'id': env.post.id})
        assert res['post_view']['post']['id'] == env.post.id


class TestWhatIsRefused:
    def test_naming_neither_a_post_nor_a_parent(self, env):
        """D1238. `db.session.get(Post, None)` answers None after warning that
        a fully NULL primary key identity cannot load any object, and the None
        reached `post_replies`."""
        with pytest.raises(Exception, match='post_id or parent_id required'):
            get_post_replies(None, {})

    def test_a_post_nobody_holds(self, env):
        """D1238."""
        with pytest.raises(Exception, match='post not found'):
            get_post_replies(None, {'post_id': MISSING})

    def test_a_comment_nobody_holds(self, env):
        """D1238."""
        with pytest.raises(Exception, match='comment not found'):
            get_post_replies(None, {'parent_id': MISSING})


class TestDepth:
    def test_the_whole_thread(self, env):
        res = get_post_replies(None, {'post_id': env.post.id})
        assert flatten(res['comments']) == ['top', 'child', 'grand']

    def test_the_top_level_only(self, env):
        """D1237. `max_depth=0` is falsy, so `if max_depth:` skipped the
        filter and answered with the whole thread."""
        res = get_post_replies(None, {'post_id': env.post.id, 'max_depth': 0})
        assert flatten(res['comments']) == ['top']

    def test_one_level_down(self, env):
        res = get_post_replies(None, {'post_id': env.post.id, 'max_depth': 1})
        assert flatten(res['comments']) == ['top', 'child']

    def test_two_levels_down(self, env):
        res = get_post_replies(None, {'post_id': env.post.id, 'max_depth': 2})
        assert flatten(res['comments']) == ['top', 'child', 'grand']

    def test_deeper_than_the_thread_goes(self, env):
        res = get_post_replies(None, {'post_id': env.post.id, 'max_depth': 9})
        assert flatten(res['comments']) == ['top', 'child', 'grand']


class TestBranches:
    def test_a_branch_from_one_comment(self, env):
        res = get_post_replies(None, {'parent_id': env.child.id})
        assert flatten(res['comments']) == ['child', 'grand']

    def test_a_branch_whose_post_is_named_too(self, env):
        res = get_post_replies(None, {'parent_id': env.child.id,
                                      'post_id': env.post.id})
        assert flatten(res['comments']) == ['child', 'grand']

    def test_a_branch_cut_at_its_own_root(self, env):
        """D1237, on the branch side: inside a branch the depth is counted
        from the parent, not from the post."""
        res = get_post_replies(None, {'parent_id': env.top.id,
                                      'max_depth': 0})
        assert flatten(res['comments']) == ['top']

    def test_a_branch_counts_depth_from_its_own_root(self, env):
        """Inside a branch the filter counts from the parent, not from the
        post: `child` sits at `comment.depth` 1, and a branch rooted there
        with max_depth 0 must still answer with `child` itself."""
        res = get_post_replies(None, {'parent_id': env.child.id,
                                      'max_depth': 0})
        assert flatten(res['comments']) == ['child']

    def test_a_branch_cut_one_level_down(self, env):
        res = get_post_replies(None, {'parent_id': env.top.id,
                                      'max_depth': 1})
        assert flatten(res['comments']) == ['top', 'child']


class TestPaging:
    @pytest.fixture
    def two_threads(self, env):
        second = make_post_reply(env.post, env.author, body='second top')
        second.path = [0, second.id]
        second.depth = 0
        db.session.commit()
        env.second = second
        return env

    def test_a_branch_is_never_split_across_pages(self, two_threads):
        """The limit counts whole branches: the first thread is three
        comments and the limit is one, and it comes back whole."""
        # sort='Old': the default is 'New', which puts the second thread
        # first, and these tests are about which BRANCH a page carries rather
        # than which order the branches come in.
        res = get_post_replies(None, {'post_id': two_threads.post.id,
                                      'limit': 1, 'sort': 'Old'})
        assert flatten(res['comments']) == ['top', 'child', 'grand']
        assert res['next_page'] == str(two_threads.second.id)

    def test_the_cursor_carries_on_from_there(self, two_threads):
        first = get_post_replies(None, {'post_id': two_threads.post.id,
                                        'limit': 1, 'sort': 'Old'})
        second = get_post_replies(None, {'post_id': two_threads.post.id,
                                         'limit': 1, 'sort': 'Old',
                                         'page': first['next_page']})
        assert flatten(second['comments']) == ['second top']
        assert second['next_page'] is None

    def test_a_cursor_nobody_holds_is_past_the_end(self, two_threads):
        res = get_post_replies(None, {'post_id': two_threads.post.id,
                                      'page': MISSING})
        assert res['comments'] == []
        assert res['next_page'] is None

    def test_a_cursor_that_is_not_a_number_starts_at_the_beginning(
            self, two_threads):
        res = get_post_replies(None, {'post_id': two_threads.post.id,
                                      'page': 'abc', 'sort': 'Old'})
        assert flatten(res['comments']) == ['top', 'child', 'grand',
                                            'second top']

    def test_a_limit_that_fits_both_threads(self, two_threads):
        res = get_post_replies(None, {'post_id': two_threads.post.id,
                                      'limit': 10, 'sort': 'Old'})
        assert flatten(res['comments']) == ['top', 'child', 'grand',
                                            'second top']
        assert res['next_page'] is None

    def test_a_limit_beyond_the_configured_page_length_is_clamped(
            self, two_threads, monkeypatch):
        monkeypatch.setitem(current_app.config, 'PAGE_LENGTH', 1)
        res = get_post_replies(None, {'post_id': two_threads.post.id,
                                      'limit': 500, 'sort': 'Old'})
        # one branch, whole, and a cursor to the next
        assert flatten(res['comments']) == ['top', 'child', 'grand']
        assert res['next_page'] == str(two_threads.second.id)

    def test_a_post_with_no_comments_at_all(self, env):
        quiet = make_post(env.community, env.author,
                          ap_id='https://test.piefed.local/t/2')
        db.session.commit()
        res = get_post_replies(None, {'post_id': quiet.id})
        assert res['comments'] == []
        assert res['next_page'] is None


class TestTheReadersOwnState:
    def test_an_anonymous_reader_is_told_nothing_personal(self, env):
        res = get_post_replies(None, {'post_id': env.post.id})
        top = res['comments'][0]
        assert top['my_vote'] == 0
        assert top['banned_from_community'] is False

    def test_your_own_vote_comes_back_with_the_comment(self, env):
        make_post_reply_vote(env.reader, env.top, 1)
        make_post_reply_vote(env.reader, env.child, -1)
        res = get_post_replies(token(env.reader), {'post_id': env.post.id})
        assert res['comments'][0]['my_vote'] == 1
        assert res['comments'][0]['replies'][0]['my_vote'] == -1

    def test_your_bookmark_comes_back_with_the_comment(self, env):
        make_post_reply_bookmark(env.reader, env.top)
        res = get_post_replies(token(env.reader), {'post_id': env.post.id})
        assert res['comments'][0]['saved'] is True
        assert res['comments'][0]['replies'][0]['saved'] is False

    def test_a_reader_banned_from_the_community_is_told_so(self, env):
        from tests.factories import make_community_ban
        make_community_ban(env.reader, env.community, banned_by=env.author)
        res = get_post_replies(token(env.reader), {'post_id': env.post.id})
        assert res['comments'][0]['banned_from_community'] is True

    def test_a_comment_by_someone_banned_from_the_community(self, env):
        from tests.factories import make_community_ban
        make_community_ban(env.author, env.community, banned_by=env.reader)
        res = get_post_replies(token(env.reader), {'post_id': env.post.id})
        assert res['comments'][0]['creator_banned_from_community'] is True

    def test_a_comment_by_a_moderator_says_so(self, env):
        from app.models import CommunityMember
        CommunityMember.query.filter_by(
            user_id=env.author.id,
            community_id=env.community.id).one().is_moderator = True
        db.session.commit()
        res = get_post_replies(token(env.reader), {'post_id': env.post.id})
        assert res['comments'][0]['creator_is_moderator'] is True

    def test_only_the_top_level_carries_the_post_and_community(self, env):
        res = get_post_replies(None, {'post_id': env.post.id})
        top = res['comments'][0]
        assert top['post']['id'] == env.post.id
        assert top['community']['id'] == env.community.id
        assert 'post' not in top['replies'][0]
        assert 'community' not in top['replies'][0]

    def test_two_top_level_comments_share_one_post_view(self, env):
        second = make_post_reply(env.post, env.author, body='second top')
        second.path = [0, second.id]
        second.depth = 0
        db.session.commit()
        res = get_post_replies(None, {'post_id': env.post.id, 'limit': 10,
                                      'sort': 'Old'})
        assert res['comments'][0]['post'] is res['comments'][1]['post']


class TestSorts:
    @pytest.mark.parametrize('sort', ['New', 'Old', 'Top', 'Hot', 'Nonsense'])
    def test_every_sort_answers(self, env, sort):
        res = get_post_replies(None, {'post_id': env.post.id, 'sort': sort})
        assert flatten(res['comments']) == ['top', 'child', 'grand']

    def test_the_new_sort_puts_the_newest_thread_first(self, env):
        from datetime import timedelta

        from app.models import utcnow
        second = make_post_reply(env.post, env.author, body='second top')
        second.path = [0, second.id]
        second.depth = 0
        second.posted_at = utcnow() + timedelta(minutes=5)
        db.session.commit()
        res = get_post_replies(None, {'post_id': env.post.id, 'sort': 'New',
                                      'limit': 10})
        assert res['comments'][0]['comment']['body'] == 'second top'

    def test_the_old_sort_puts_the_oldest_thread_first(self, env):
        from datetime import timedelta

        from app.models import utcnow
        second = make_post_reply(env.post, env.author, body='second top')
        second.path = [0, second.id]
        second.depth = 0
        second.posted_at = utcnow() - timedelta(minutes=5)
        db.session.commit()
        res = get_post_replies(None, {'post_id': env.post.id, 'sort': 'Old',
                                      'limit': 10})
        assert res['comments'][0]['comment']['body'] == 'second top'
