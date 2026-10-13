"""Interop C5 (spec 2026-10-12): count-only `following` on a Group, `liked` on a Person, and inline `likes`,
`dislikes` and `shares` on local posts and comments, each also served at its own id."""
import pytest

from app import db
from app.activitypub.util import comment_model_to_json, post_to_page
from app.models import PostBoost
from tests.factories import make_instance, make_post_reply, make_user
from tests.test_ap_content_objects import ap_get, seed_local_post

SERVER = 'https://test.piefed.local'


def test_a_post_embeds_its_likes_dislikes_and_shares_counts(app, db_session):
    community, author, post = seed_local_post()
    post.up_votes, post.down_votes = 5, 2
    remote = make_instance('remote.example')
    for name in ('b1', 'b2', 'b3'):
        db.session.add(PostBoost(user_id=make_user(remote, name).id, post_id=post.id))
    db.session.commit()

    page = post_to_page(post)

    assert page['likes'] == {'id': f'{SERVER}/post/{post.id}/likes', 'type': 'Collection', 'totalItems': 5}
    assert page['dislikes'] == {'id': f'{SERVER}/post/{post.id}/dislikes', 'type': 'Collection', 'totalItems': 2}
    assert page['shares'] == {'id': f'{SERVER}/post/{post.id}/shares', 'type': 'Collection', 'totalItems': 3}


def test_a_post_with_null_cached_counts_embeds_zero(app, db_session):
    community, author, post = seed_local_post()
    post.up_votes, post.down_votes = None, None
    db.session.commit()

    page = post_to_page(post)

    assert (page['likes']['totalItems'], page['dislikes']['totalItems'], page['shares']['totalItems']) == (0, 0, 0)


def test_a_comment_embeds_likes_and_dislikes_but_no_shares(app, db_session):
    community, author, post = seed_local_post()
    reply = make_post_reply(post, author)
    reply.up_votes, reply.down_votes = 3, None
    db.session.commit()

    note = comment_model_to_json(reply)

    assert note['likes'] == {'id': f'{SERVER}/comment/{reply.id}/likes', 'type': 'Collection', 'totalItems': 3}
    assert note['dislikes'] == {'id': f'{SERVER}/comment/{reply.id}/dislikes', 'type': 'Collection',
                                'totalItems': 0}
    assert 'shares' not in note


def test_no_inline_collection_carries_items(app, db_session):
    community, author, post = seed_local_post()
    reply = make_post_reply(post, author)

    for collection in [*post_count_collections_of(post), *comment_count_collections_of(reply)]:
        assert set(collection) == {'id', 'type', 'totalItems'}


def post_count_collections_of(post):
    page = post_to_page(post)
    return [page['likes'], page['dislikes'], page['shares']]


def comment_count_collections_of(reply):
    note = comment_model_to_json(reply)
    return [note['likes'], note['dislikes']]
