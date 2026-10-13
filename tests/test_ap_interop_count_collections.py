"""Interop C5 (spec 2026-10-12): count-only `following` on a Group, `liked` on a Person, and inline `likes`,
`dislikes` and `shares` on local posts and comments, each also served at its own id."""
import pytest

from app import db
from app.activitypub.util import comment_model_to_json, post_to_page
from app.models import PostBoost
from tests.factories import make_community, make_instance, make_post_reply, make_user
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


def test_the_group_document_names_its_following_collection(app, db_session):
    seed_local_post()   # seeds the site, instance and the local community 'books'

    assert ap_get(app, '/c/books').json['following'] == f'{SERVER}/c/books/following'


def test_a_group_follows_nothing(app, db_session):
    seed_local_post()

    response = ap_get(app, '/c/books/following')

    assert response.status_code == 200
    assert response.json['type'] == 'OrderedCollection'
    assert response.json['id'] == f'{SERVER}/c/books/following'
    assert response.json['totalItems'] == 0
    assert response.json['orderedItems'] == []


@pytest.mark.parametrize('flag', ['local_only', 'private'])
def test_a_local_only_or_private_groups_following_is_403(app, db_session, flag):
    community, author, post = seed_local_post()
    setattr(community, flag, True)
    db.session.commit()

    assert ap_get(app, '/c/books/following').status_code == 403


def test_an_unknown_or_remote_groups_following_is_404(app, db_session):
    seed_local_post()
    remote = make_community(name='films', host='remote.example')
    remote.ap_id = 'https://remote.example/c/films'
    db.session.commit()

    assert ap_get(app, '/c/nosuch/following').status_code == 404
    assert ap_get(app, '/c/films/following').status_code == 404


def test_a_browser_asking_for_a_groups_following_is_sent_to_the_group(app, db_session):
    seed_local_post()
    with app.test_client() as client:
        response = client.get('/c/books/following', headers={'Accept': 'text/html'})

    assert (response.status_code, response.headers['Location']) == (302, '/c/books')
