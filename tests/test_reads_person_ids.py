"""The post and comment listings by a set of authors (plan W1.2: a Mastodon home timeline is the posts and comments of
the people the viewer follows, ruling M2). Each author's posts are listed as their profile lists them: followers-only
ones to their followers only."""
from itertools import count

import pytest

from app import db
from app.constants import VISIBILITY_FOLLOWERS
from app.reads.posts import KEYSET_PAGES, Listing, post_listing
from app.reads.replies import CommentListing, comment_listing
from app.reads.viewer import Restrictions
from tests.factories import make_community, make_follow, make_post, make_post_reply, make_user

_SERIAL = count()


@pytest.fixture
def world(app, api_baseline):
    viewer = make_user(None, 'setviewer', local=True)
    followed, other = make_user(None, 'setfollowed', local=True), make_user(None, 'setother', local=True)
    make_follow(viewer, followed)
    community = make_community('setofauthors')
    return {'viewer': viewer, 'followed': followed, 'other': other, 'community': community}


def _post(world, author, **columns):
    post = make_post(world['community'], author, ap_id=f'https://test.piefed.local/post/set{next(_SERIAL)}')
    for name, value in columns.items():
        setattr(post, name, value)
    db.session.commit()
    return post


def _posts(world, person_ids, viewer=None):
    viewer = viewer if viewer is not None else world['viewer']
    listing = post_listing(viewer, Restrictions(viewer.id), Listing(sort='', person_ids=person_ids,
                                                                    ignore_sticky=True), KEYSET_PAGES)
    return set(listing.posts.all())


def _comments(world, person_ids, viewer=None):
    viewer = viewer if viewer is not None else world['viewer']
    return set(comment_listing(viewer.id, Restrictions(viewer.id),
                               CommentListing(person_ids=person_ids)).replies.all())


def test_posts_by_any_of_the_authors_and_no_one_else(world):
    mine, theirs = _post(world, world['followed']), _post(world, world['other'])
    assert _posts(world, (world['followed'].id,)) == {mine}
    assert _posts(world, (world['followed'].id, world['other'].id)) == {mine, theirs}
    assert _posts(world, ()) == set()


def test_a_followers_only_post_is_listed_to_a_follower_alone(world):
    hidden = _post(world, world['followed'], visibility=VISIBILITY_FOLLOWERS)
    assert _posts(world, (world['followed'].id,)) == {hidden}
    assert _posts(world, (world['followed'].id,), viewer=world['other']) == set()


def test_comments_by_any_of_the_authors_and_no_one_else(world):
    post = _post(world, world['other'])
    mine, theirs = make_post_reply(post, world['followed']), make_post_reply(post, world['other'])
    assert _comments(world, (world['followed'].id,)) == {mine}
    assert _comments(world, (world['followed'].id, world['other'].id)) == {mine, theirs}
    assert _comments(world, ()) == set()


def test_a_followers_only_comment_is_listed_to_a_follower_alone(world):
    """On a third person's post: the author a reply answers may see it too (R-REPLYVIEW)."""
    post = _post(world, make_user(None, 'setthird', local=True))
    hidden = make_post_reply(post, world['followed'])
    hidden.visibility = VISIBILITY_FOLLOWERS
    db.session.commit()
    assert _comments(world, (world['followed'].id,)) == {hidden}
    assert _comments(world, (world['followed'].id,), viewer=world['other']) == set()


def test_set_of_authors_narrows_a_liked_listing_too(world):
    """person_ids applies on top of another narrowing, as person_id does."""
    post = _post(world, world['other'])
    reply = make_post_reply(post, world['followed'])
    rows = comment_listing(world['viewer'].id, Restrictions(world['viewer'].id),
                           CommentListing(saved_only=True, person_ids=(world['followed'].id,)),
                           saved_reply_ids=[reply.id]).replies.all()
    assert rows == [reply]
