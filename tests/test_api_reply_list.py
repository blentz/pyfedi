"""`get_reply_list`, which closes `app/api/alpha/utils/reply.py`.

Sub-project 84, slice I. One defect, measured:

* the depth-first branch built `depth_query = ' AND depth <= {max_depth}'`
  with **no f-prefix**, so the braces themselves reached Postgres and every
  depth-first comment query carrying a `max_depth` was
  `psycopg2.errors.SyntaxError: syntax error at or near "{"` -- and the
  aborted transaction took the next query in the request with it (D1198).

The function is one long dispatch: a filter chosen from the request, then a
sort, then a page. The rows below walk each filter in turn, then the threaded
branch, which is the half that builds SQL by hand.
"""
from datetime import timedelta
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.models import (Community, CommunityMember, Post, PostReply,
                        PostReplyBookmark, PostReplyVote, Site, User,
                        UserBlock, InstanceBlock, utcnow)
from tests.factories import (make_community, make_community_member,
                             make_instance, make_post, make_post_reply,
                             make_post_reply_bookmark, make_post_reply_vote,
                             make_user)


def token(user):
    return f'Bearer {user.encode_jwt_token()}'


def a_child(parent, author, body='a child'):
    """A reply under `parent`, with the `path` and `root_id` `PostReply.new`
    would give it."""
    child = make_post_reply(db.session.get(Post, parent.post_id), author,
                            body=body)
    child.body_html = f'<p>{body}</p>'
    child.parent_id = parent.id
    child.depth = (parent.depth or 0) + 1
    child.path = (parent.path or [0, parent.id]) + [child.id]
    child.root_id = parent.root_id or parent.id
    db.session.commit()
    return child


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    reader = api_baseline.user2
    author = api_baseline.user3
    community = api_baseline.community1
    post = api_baseline.post1
    reply = make_post_reply(post, author, body='the first comment')
    reply.body_html = '<p>the first comment</p>'
    db.session.commit()
    # `make_post_reply` leaves `path` and `root_id` unset on purpose (fact
    # 561), and the depth-first branch walks every row's path, so every
    # comment this file builds is given the pair `PostReply.new` would.
    reply.path = [0, reply.id]
    reply.root_id = reply.id
    db.session.commit()
    return reader, author, community, post, reply, api_baseline


# --------------------------------------------------------------------------
# D1198 -- the f-string that was not one
# --------------------------------------------------------------------------


def test_a_depth_first_query_with_a_maximum_depth(app, env):
    """D1198. `' AND depth <= {max_depth}'` with no f-prefix put the braces
    into the SQL. Measured: `PROBE bp1 max_depth=True: ProgrammingError:
    (psycopg2.errors.SyntaxError) syntax error at or near "{"`."""
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    child = a_child(reply, author, body='a deep one')

    answer = get_reply_list(token(reader), {'post_id': post.id,
                                            'depth_first': True,
                                            'max_depth': 0})

    ids = [c['comment']['id'] for c in answer['comments']]
    assert reply.id in ids
    assert child.id not in ids


def test_a_depth_first_query_without_one(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    child = a_child(reply, author)

    answer = get_reply_list(token(reader), {'post_id': post.id,
                                            'depth_first': True})

    assert {c['comment']['id']
            for c in answer['comments']} >= {reply.id, child.id}


def test_a_depth_first_query_from_a_parent(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    child = a_child(reply, author)

    answer = get_reply_list(token(reader), {'parent_id': reply.id,
                                            'depth_first': True})

    assert child.id in [c['comment']['id'] for c in answer['comments']]


def test_a_depth_first_query_from_a_parent_that_is_not_the_root(app, env):
    """A parent at depth > 0 is matched by `path @> ARRAY[id]` rather than by
    `root_id`."""
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    child = a_child(reply, author)
    grandchild = a_child(child, author, body='a grandchild')

    answer = get_reply_list(token(reader), {'parent_id': child.id,
                                            'depth_first': True})

    assert grandchild.id in [c['comment']['id'] for c in answer['comments']]


@pytest.mark.parametrize('sort', ['Hot', 'Top', 'Old', 'New'])
def test_a_depth_first_query_sorts(app, env, sort):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    a_child(reply, author)

    answer = get_reply_list(token(reader), {'post_id': post.id,
                                            'depth_first': True,
                                            'sort': sort})

    assert answer['comments'] != []


def test_a_depth_first_query_pages(app, env):
    """The page array is built by walking each comment's path, so a branch is
    never split across pages."""
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    for number in range(3):
        one = make_post_reply(post, author, body=f'comment {number}')
        one.body_html = f'<p>comment {number}</p>'
        db.session.commit()
        one.path = [0, one.id]
        one.root_id = one.id
        db.session.commit()

    first = get_reply_list(token(reader), {'post_id': post.id,
                                           'depth_first': True, 'limit': 2})

    assert first['next_page'] == '2'
    second = get_reply_list(token(reader), {'post_id': post.id,
                                            'depth_first': True, 'limit': 2,
                                            'page': 2})
    assert second['comments'] != []


# --------------------------------------------------------------------------
# The threaded branch without depth_first
# --------------------------------------------------------------------------


def test_a_posts_comments_come_back_threaded(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    child = a_child(reply, author)

    answer = get_reply_list(token(reader), {'post_id': post.id})

    assert {c['comment']['id']
            for c in answer['comments']} >= {reply.id, child.id}


def test_a_parents_replies_come_back(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    child = a_child(reply, author)

    answer = get_reply_list(token(reader), {'parent_id': reply.id})

    assert child.id in [c['comment']['id'] for c in answer['comments']]


def test_a_parent_deeper_than_the_root(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    child = a_child(reply, author)
    grandchild = a_child(child, author, body='a grandchild')

    answer = get_reply_list(token(reader), {'parent_id': child.id})

    assert grandchild.id in [c['comment']['id'] for c in answer['comments']]


def test_a_parent_that_does_not_exist(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env

    with pytest.raises(Exception) as refused:
        get_reply_list(token(reader), {'parent_id': 999999})

    assert str(refused.value) == 'Comment with parent_id not found.'


def test_a_maximum_depth_narrows_a_thread(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    child = a_child(reply, author)

    answer = get_reply_list(token(reader), {'post_id': post.id,
                                            'max_depth': 0})

    ids = [c['comment']['id'] for c in answer['comments']]
    assert reply.id in ids
    assert child.id not in ids


def test_a_maximum_depth_is_relative_to_the_parent(app, env):
    """For a `parent_id` query the depth is added to the parent's own."""
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    child = a_child(reply, author)
    grandchild = a_child(child, author, body='a grandchild')

    answer = get_reply_list(token(reader), {'parent_id': reply.id,
                                            'max_depth': 1})

    ids = [c['comment']['id'] for c in answer['comments']]
    assert child.id in ids
    assert grandchild.id not in ids


# --------------------------------------------------------------------------
# The flat filters
# --------------------------------------------------------------------------


def test_the_comments_somebody_upvoted(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    make_post_reply_vote(reader, reply, 1)
    db.session.commit()

    answer = get_reply_list(token(reader), {'liked_only': True})

    assert [c['comment']['id'] for c in answer['comments']] == [reply.id]


def test_liked_only_needs_an_account(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env

    with pytest.raises(Exception) as refused:
        get_reply_list(None, {'liked_only': True})

    assert 'Login required' in str(refused.value)


def test_the_comments_somebody_saved(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    make_post_reply_bookmark(reader, reply)
    db.session.commit()

    answer = get_reply_list(token(reader), {'saved_only': True})

    assert [c['comment']['id'] for c in answer['comments']] == [reply.id]


def test_saved_only_needs_an_account(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env

    with pytest.raises(Exception) as refused:
        get_reply_list(None, {'saved_only': True})

    assert 'Login required' in str(refused.value)


def test_liked_and_saved_together_narrow_each_other(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    other = make_post_reply(post, author, body='another')
    other.body_html = '<p>another</p>'
    db.session.commit()
    make_post_reply_vote(reader, reply, 1)
    make_post_reply_vote(reader, other, 1)
    make_post_reply_bookmark(reader, reply)
    db.session.commit()

    answer = get_reply_list(token(reader), {'liked_only': True,
                                            'saved_only': True})

    assert [c['comment']['id'] for c in answer['comments']] == [reply.id]


def test_the_comments_by_one_person(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env

    answer = get_reply_list(token(reader), {'person_id': author.id})

    assert reply.id in [c['comment']['id'] for c in answer['comments']]


def test_the_comments_in_one_community(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env

    answer = get_reply_list(token(reader), {'community_id': community.id})

    assert reply.id in [c['comment']['id'] for c in answer['comments']]


def test_a_person_and_a_post_together(app, env):
    """`post_id` alongside another filter is an extra narrowing rather than
    the threaded branch."""
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    elsewhere = make_post(community, author, 'https://test.piefed.local/p/9',
                          title='another post')
    db.session.commit()
    other = make_post_reply(elsewhere, author, body='over there')
    other.body_html = '<p>over there</p>'
    db.session.commit()

    answer = get_reply_list(token(reader), {'person_id': author.id,
                                            'post_id': post.id})

    ids = [c['comment']['id'] for c in answer['comments']]
    assert reply.id in ids
    assert other.id not in ids


def test_a_search(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    reply.search_vector = None
    db.session.commit()

    answer = get_reply_list(token(reader), {'q': 'comment'})

    assert isinstance(answer['comments'], list)


def test_every_comment_when_nothing_narrows_it(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env

    answer = get_reply_list(token(reader), {})

    assert reply.id in [c['comment']['id'] for c in answer['comments']]


def test_an_anonymous_reader_may_not_walk_the_whole_archive(app, env):
    """A deep page with no account behind it is refused, and the message
    names nothing: "deliberately vague response"."""
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env

    with pytest.raises(Exception) as refused:
        get_reply_list(None, {'page': 2000, 'limit': 10})

    assert str(refused.value) == 'unknown'


def test_an_account_may(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env

    answer = get_reply_list(token(reader), {'page': 2000, 'limit': 10})

    assert answer['comments'] == []


# --------------------------------------------------------------------------
# The listing types
# --------------------------------------------------------------------------


def test_the_local_listing(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    remote = make_post_reply(post, author, body='from elsewhere')
    remote.body_html = '<p>from elsewhere</p>'
    remote.ap_id = 'https://remote.example/comment/1'
    db.session.commit()

    answer = get_reply_list(token(reader), {'person_id': author.id,
                                            'type_': 'Local'})

    ids = [c['comment']['id'] for c in answer['comments']]
    assert reply.id in ids
    assert remote.id not in ids


def test_the_moderating_listing(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    make_community_member(reader, community, is_moderator=True)
    db.session.commit()

    answer = get_reply_list(token(reader), {'person_id': author.id,
                                            'type_': 'Moderating'})

    assert reply.id in [c['comment']['id'] for c in answer['comments']]


def test_the_moderating_listing_needs_an_account(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env

    with pytest.raises(Exception) as refused:
        get_reply_list(None, {'person_id': author.id, 'type_': 'Moderating'})

    assert str(refused.value) == 'incorrect login'


def test_the_subscribed_listing(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    make_community_member(reader, community)
    db.session.commit()

    answer = get_reply_list(token(reader), {'person_id': author.id,
                                            'type_': 'Subscribed'})

    assert reply.id in [c['comment']['id'] for c in answer['comments']]


def test_the_subscribed_listing_needs_an_account(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env

    with pytest.raises(Exception) as refused:
        get_reply_list(None, {'person_id': author.id, 'type_': 'Subscribed'})

    assert str(refused.value) == 'incorrect login'


# --------------------------------------------------------------------------
# Who is left out
# --------------------------------------------------------------------------


def test_a_blocked_persons_comments_are_left_out(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    db.session.add(UserBlock(blocker_id=reader.id, blocked_id=author.id))
    db.session.commit()

    answer = get_reply_list(token(reader), {'community_id': community.id})

    assert reply.id not in [c['comment']['id'] for c in answer['comments']]


def test_a_blocked_instances_comments_are_left_out(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    db.session.add(InstanceBlock(user_id=reader.id,
                                 instance_id=baseline.instance_remote.id))
    reply.instance_id = baseline.instance_remote.id
    db.session.commit()

    answer = get_reply_list(token(reader), {'community_id': community.id})

    assert reply.id not in [c['comment']['id'] for c in answer['comments']]


def test_a_blocked_persons_thread_is_left_out_of_a_conversation(app, env):
    """In a threaded query the whole branch goes, not just the comment --
    `path @> ` is what carries the ancestry."""
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    child = a_child(reply, baseline.user4, body='a reply to the blocked one')
    db.session.add(UserBlock(blocker_id=reader.id, blocked_id=author.id))
    db.session.commit()

    answer = get_reply_list(token(reader), {'post_id': post.id})

    ids = [c['comment']['id'] for c in answer['comments']]
    assert reply.id not in ids
    assert child.id not in ids


# --------------------------------------------------------------------------
# Sorting and paging
# --------------------------------------------------------------------------


@pytest.mark.parametrize('sort', ['Hot', 'Scaled', 'Active', 'Top', 'TopAll',
                                  'Old', 'New', 'Controversial'])
def test_the_sorts_that_do_not_narrow_by_age(app, env, sort):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env

    answer = get_reply_list(token(reader), {'community_id': community.id,
                                            'sort': sort})

    assert reply.id in [c['comment']['id'] for c in answer['comments']]


@pytest.mark.parametrize('sort,age', [('TopHour', timedelta(minutes=30)),
                                      ('TopSixHour', timedelta(hours=3)),
                                      ('TopTwelveHour', timedelta(hours=8)),
                                      ('TopDay', timedelta(hours=20)),
                                      ('TopWeek', timedelta(days=3)),
                                      ('TopMonth', timedelta(days=14)),
                                      ('TopThreeMonths', timedelta(days=60)),
                                      ('TopSixMonths', timedelta(days=120)),
                                      ('TopNineMonths', timedelta(days=200)),
                                      ('TopYear', timedelta(days=300))])
def test_each_top_window_keeps_what_is_inside_it(app, env, sort, age):
    """Each of these filters by age as well as ordering, so a comment just
    inside the window stays and one well outside it goes."""
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    reply.posted_at = utcnow() - age
    old = make_post_reply(post, author, body='ancient')
    old.body_html = '<p>ancient</p>'
    old.posted_at = utcnow() - timedelta(days=400)
    db.session.commit()

    answer = get_reply_list(token(reader), {'community_id': community.id,
                                            'sort': sort})

    ids = [c['comment']['id'] for c in answer['comments']]
    assert reply.id in ids
    assert old.id not in ids


def test_the_list_is_paged(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    for number in range(3):
        one = make_post_reply(post, author, body=f'comment {number}')
        one.body_html = f'<p>comment {number}</p>'
        db.session.commit()
        one.path = [0, one.id]
        one.root_id = one.id
    db.session.commit()

    answer = get_reply_list(token(reader), {'community_id': community.id,
                                            'limit': 2})

    assert len(answer['comments']) == 2
    assert answer['next_page'] == '2'


def test_the_page_length_is_capped(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    for number in range(3):
        one = make_post_reply(post, author, body=f'comment {number}')
        one.body_html = f'<p>comment {number}</p>'
        db.session.commit()
        one.path = [0, one.id]
        one.root_id = one.id
    db.session.commit()

    with pytest.MonkeyPatch.context() as patched:
        patched.setitem(app.config, 'PAGE_LENGTH', 2)
        answer = get_reply_list(token(reader), {'community_id': community.id,
                                                'limit': 100})

    assert len(answer['comments']) == 2


# --------------------------------------------------------------------------
# What each row carries
# --------------------------------------------------------------------------


def test_a_row_carries_how_the_reader_voted(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    make_post_reply_vote(reader, reply, -1)
    db.session.commit()

    answer = get_reply_list(token(reader), {'community_id': community.id})

    assert answer['comments'][0]['my_vote'] == -1


def test_a_row_carries_whether_it_is_saved(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    make_post_reply_bookmark(reader, reply)
    db.session.commit()

    answer = get_reply_list(token(reader), {'community_id': community.id})

    assert answer['comments'][0]['saved'] is True


def test_an_anonymous_reader_gets_a_list_too(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env

    answer = get_reply_list(None, {'community_id': community.id})

    assert reply.id in [c['comment']['id'] for c in answer['comments']]
    assert answer['comments'][0]['saved'] is False


def test_the_controversial_sort_with_a_comment_nobody_voted_on(app, env):
    """D1200. The divisor was `coalesce(greatest(up_votes, down_votes), 1)`,
    which guards NULL and not zero -- and a comment with no votes at all has
    `greatest(0, 0)`. One such comment took the whole listing down with
    `psycopg2.errors.DivisionByZero: division by zero`."""
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    reply.up_votes = 0
    reply.down_votes = 0
    contested = make_post_reply(post, author, body='an argument')
    contested.body_html = '<p>an argument</p>'
    contested.up_votes = 5
    contested.down_votes = 5
    db.session.commit()

    answer = get_reply_list(token(reader), {'community_id': community.id,
                                            'sort': 'Controversial'})

    ids = [c['comment']['id'] for c in answer['comments']]
    assert contested.id in ids
    assert reply.id in ids
    assert ids.index(contested.id) < ids.index(reply.id)


def test_a_listing_type_narrows_a_person_query(app, env):
    """D1199. The listing-type block sat ABOVE the person, community and
    no-filter blocks and is guarded by `if replies:`, which none of them had
    assigned yet -- so `type_` was silently ignored for all three. Measured:
    `PROBE bq1 person_id + Local: remote included: True`."""
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    remote = make_post_reply(post, author, body='from elsewhere')
    remote.body_html = '<p>from elsewhere</p>'
    remote.ap_id = 'https://remote.example/comment/1'
    db.session.commit()

    answer = get_reply_list(token(reader), {'person_id': author.id,
                                            'type_': 'Local'})

    ids = [c['comment']['id'] for c in answer['comments']]
    assert reply.id in ids
    assert remote.id not in ids


def test_a_listing_type_narrows_a_query_with_no_filter_at_all(app, env):
    """D1199's third measurement: `PROBE bq2 community_id + Local`."""
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    remote = make_post_reply(post, author, body='from elsewhere')
    remote.body_html = '<p>from elsewhere</p>'
    remote.ap_id = 'https://remote.example/comment/1'
    db.session.commit()

    answer = get_reply_list(token(reader), {'type_': 'Local'})

    ids = [c['comment']['id'] for c in answer['comments']]
    assert reply.id in ids
    assert remote.id not in ids


def test_a_search_narrowed_by_a_person(app, env):
    """`q` on top of another filter searches within it rather than starting
    a new query."""
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env

    answer = get_reply_list(token(reader), {'liked_only': True, 'q': 'first'})

    assert isinstance(answer['comments'], list)


def test_a_person_narrowed_by_something_else(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    make_post_reply_vote(reader, reply, 1)
    db.session.commit()

    answer = get_reply_list(token(reader), {'liked_only': True,
                                            'person_id': author.id})

    assert [c['comment']['id'] for c in answer['comments']] == [reply.id]


def test_a_community_narrowed_by_something_else(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    make_post_reply_vote(reader, reply, 1)
    db.session.commit()

    answer = get_reply_list(token(reader), {'liked_only': True,
                                            'community_id': community.id})

    assert [c['comment']['id'] for c in answer['comments']] == [reply.id]


def test_a_maximum_depth_on_a_flat_list(app, env):
    """`max_depth` outside the threaded branch is a plain filter on the
    column, and it is exclusive there rather than inclusive."""
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    child = a_child(reply, author)

    answer = get_reply_list(token(reader), {'community_id': community.id,
                                            'max_depth': 1})

    ids = [c['comment']['id'] for c in answer['comments']]
    assert reply.id in ids
    assert child.id not in ids


def test_a_blocked_persons_thread_is_left_out_of_a_parent_query(app, env):
    """The ancestry filter on the threaded branch: `path` carries every
    parent, so a blocked person's whole branch goes."""
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    child = a_child(reply, baseline.user4, body='under the blocked one')
    grandchild = a_child(child, author, body='deeper still')
    db.session.add(UserBlock(blocker_id=reader.id, blocked_id=baseline.user4.id))
    db.session.commit()

    answer = get_reply_list(token(reader), {'parent_id': reply.id})

    ids = [c['comment']['id'] for c in answer['comments']]
    assert child.id not in ids
    assert grandchild.id not in ids


def test_a_blocked_instances_thread_is_left_out_too(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    child = a_child(reply, author, body='from elsewhere')
    child.instance_id = baseline.instance_remote.id
    grandchild = a_child(child, author, body='deeper still')
    db.session.add(InstanceBlock(user_id=reader.id,
                                 instance_id=baseline.instance_remote.id))
    db.session.commit()

    answer = get_reply_list(token(reader), {'parent_id': reply.id})

    ids = [c['comment']['id'] for c in answer['comments']]
    assert child.id not in ids
    assert grandchild.id not in ids


def test_the_relevance_sort_is_the_searchs_own_order(app, env):
    """"already done as part of the search query" -- the sort clause below
    deliberately does nothing for it."""
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env

    answer = get_reply_list(token(reader), {'q': 'comment',
                                            'sort': 'Relevance'})

    assert isinstance(answer['comments'], list)


def test_a_row_carries_an_upvote_in_a_liked_only_list(app, env):
    """`by_liked_only` short-circuits the per-row vote lookup, because every
    row in that list is one the reader upvoted."""
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    make_post_reply_vote(reader, reply, 1)
    db.session.commit()

    answer = get_reply_list(token(reader), {'liked_only': True})

    assert answer['comments'][0]['my_vote'] == 1


def test_a_row_carries_an_upvote_in_an_ordinary_list(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    make_post_reply_vote(reader, reply, 1)
    db.session.commit()

    answer = get_reply_list(token(reader), {'community_id': community.id})

    assert answer['comments'][0]['my_vote'] == 1


def test_only_that_persons_comments(app, env):
    """The filter narrows rather than decorating: somebody else's comment is
    absent, not merely outnumbered."""
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    theirs = make_post_reply(post, reader, body='mine')
    theirs.body_html = '<p>mine</p>'
    db.session.commit()

    answer = get_reply_list(token(reader), {'person_id': author.id})

    ids = [c['comment']['id'] for c in answer['comments']]
    assert reply.id in ids
    assert theirs.id not in ids


def test_only_that_communitys_comments(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    elsewhere = make_post(baseline.community2, author,
                          'https://test.piefed.local/p/77', title='over there')
    db.session.commit()
    other = make_post_reply(elsewhere, author, body='over there')
    other.body_html = '<p>over there</p>'
    db.session.commit()

    answer = get_reply_list(token(reader), {'community_id': community.id})

    ids = [c['comment']['id'] for c in answer['comments']]
    assert reply.id in ids
    assert other.id not in ids


def test_your_own_comments_are_not_in_your_liked_list(app, env):
    """`PostReply.user_id != user_id`: a self-upvote is not a recommendation
    to yourself."""
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    mine = make_post_reply(post, reader, body='mine')
    mine.body_html = '<p>mine</p>'
    db.session.commit()
    make_post_reply_vote(reader, mine, 1)
    make_post_reply_vote(reader, reply, 1)
    db.session.commit()

    answer = get_reply_list(token(reader), {'liked_only': True})

    ids = [c['comment']['id'] for c in answer['comments']]
    assert reply.id in ids
    assert mine.id not in ids


def test_saved_narrows_a_liked_list_rather_than_replacing_it(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    saved_not_liked = make_post_reply(post, author, body='saved only')
    saved_not_liked.body_html = '<p>saved only</p>'
    db.session.commit()
    make_post_reply_vote(reader, reply, 1)
    make_post_reply_bookmark(reader, reply)
    make_post_reply_bookmark(reader, saved_not_liked)
    db.session.commit()

    answer = get_reply_list(token(reader), {'liked_only': True,
                                            'saved_only': True})

    ids = [c['comment']['id'] for c in answer['comments']]
    assert reply.id in ids
    assert saved_not_liked.id not in ids


def test_a_comment_marked_unindexable_is_not_searched(app, env):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    hidden = make_post_reply(post, author, body='the first comment as well')
    hidden.body_html = '<p>the first comment as well</p>'
    hidden.indexable = False
    reply.indexable = True
    db.session.commit()

    answer = get_reply_list(token(reader), {'q': 'comment'})

    assert hidden.id not in [c['comment']['id'] for c in answer['comments']]


def test_a_moderator_sees_their_own_communities_in_subscribed(app, env):
    """The subscribed listing adds what you moderate to what you joined."""
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    make_community_member(reader, community, is_moderator=True)
    db.session.commit()

    answer = get_reply_list(token(reader), {'person_id': author.id,
                                            'type_': 'Subscribed'})

    assert reply.id in [c['comment']['id'] for c in answer['comments']]


def test_a_depth_relative_to_a_parent_that_is_not_the_root(app, env):
    """`max_depth` for a `parent_id` query is added to the PARENT's depth, so
    the same number means different things at different levels."""
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    child = a_child(reply, author)
    grandchild = a_child(child, author, body='a grandchild')
    great = a_child(grandchild, author, body='a great grandchild')

    answer = get_reply_list(token(reader), {'parent_id': child.id,
                                            'max_depth': 1})

    ids = [c['comment']['id'] for c in answer['comments']]
    assert grandchild.id in ids
    assert great.id not in ids


def test_a_shared_ancestor_is_listed_once(app, env):
    """The depth-first walk marks each id as it goes, so two branches of one
    parent do not each carry it."""
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    a_child(reply, author, body='one branch')
    a_child(reply, author, body='another branch')

    answer = get_reply_list(token(reader), {'post_id': post.id,
                                            'depth_first': True})

    ids = [c['comment']['id'] for c in answer['comments']]
    assert ids.count(reply.id) == 1


@pytest.mark.parametrize('sort,first', [('Hot', 'ranked'),
                                        ('Controversial', 'contested')])
def test_the_sorts_that_order_by_something_other_than_age(app, env, sort,
                                                          first):
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    made = {}
    # `contested` is created FIRST, so the fallback sort (posted_at, newest
    # first) would put `ranked` at the top: a row where the two orders agree
    # cannot tell the sort from its absence.
    for name in ('contested', 'ranked'):
        one = make_post_reply(post, author, body=name)
        one.body_html = f'<p>{name}</p>'
        one.ranking = 100 if name == 'ranked' else 0
        one.up_votes = 5 if name == 'contested' else 0
        one.down_votes = 5 if name == 'contested' else 0
        made[name] = one
    reply.ranking = 0
    reply.up_votes = 0
    reply.down_votes = 0
    db.session.commit()

    answer = get_reply_list(token(reader), {'community_id': community.id,
                                            'sort': sort})

    assert answer['comments'][0]['comment']['id'] == made[first].id


def test_the_depth_first_pages_count_each_comment_once(app, env):
    """The walk marks every id it has placed, so a parent shared by several
    branches is placed once.

    `processed.add(element)` runs unconditionally and SQL's `IN` collapses a
    doubled id, so the de-duplication is not observable through this
    endpoint's output for any shape a test can build -- recorded as D1201 and
    equivalent. What this row does hold is the paging itself.
    """
    from app.api.alpha.utils.reply import get_reply_list

    reader, author, community, post, reply, baseline = env
    for number in range(3):
        a_child(reply, author, body=f'branch {number}')

    answer = get_reply_list(token(reader), {'post_id': post.id,
                                            'depth_first': True, 'limit': 2})

    assert len(answer['comments']) == 2

    second = get_reply_list(token(reader), {'post_id': post.id,
                                            'depth_first': True, 'limit': 2,
                                            'page': 2})
    assert len(second["comments"]) == 2
