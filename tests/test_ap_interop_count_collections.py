"""Interop C5 (spec 2026-10-12): count-only `following` on a Group, `liked` on a Person, and inline `likes`,
`dislikes` and `shares` on local posts and comments, each also served at its own id."""
import pytest

from app import db
from app.activitypub.util import comment_model_to_json, post_to_page
from app.constants import POST_STATUS_REVIEWING
from app.models import PostBoost, PostReplyVote, PostVote
from tests.factories import (make_community, make_instance, make_instance_block, make_post, make_post_reply,
                             make_user)
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


def _voter():
    community, author, post = seed_local_post()
    alice = make_user(None, 'alice', local=True)
    return community, author, post, alice


def _vote(user, post=None, reply=None, effect=1.0, federated_like=True):
    if post is not None:
        db.session.add(PostVote(user_id=user.id, author_id=post.user_id, post_id=post.id, effect=effect,
                              federated_like=federated_like))
    else:
        db.session.add(PostReplyVote(user_id=user.id, author_id=reply.user_id, post_reply_id=reply.id,
                                     effect=effect, federated_like=federated_like))
    db.session.commit()


def test_the_person_document_names_its_liked_collection(app, db_session):
    _voter()

    assert ap_get(app, '/u/alice').json['liked'] == f'{SERVER}/u/alice/liked'


def test_liked_counts_post_and_comment_upvotes_and_lists_none(app, db_session):
    community, author, post, alice = _voter()
    _vote(alice, post=post)
    _vote(alice, reply=make_post_reply(post, author))

    response = ap_get(app, '/u/alice/liked')

    assert response.status_code == 200
    assert response.json['type'] == 'OrderedCollection'
    assert response.json['id'] == f'{SERVER}/u/alice/liked'
    assert response.json['totalItems'] == 2
    assert response.json['orderedItems'] == []


def test_liked_skips_every_vote_that_was_not_federated(app, db_session):
    community, author, post, alice = _voter()
    _vote(alice, post=make_post(community, author, None, title='down'), effect=-1.0)
    for flag in ('local_only', 'private'):
        hidden = make_community(name=f'c_{flag}')
        setattr(hidden, flag, True)
        db.session.commit()
        _vote(alice, post=make_post(hidden, author, None, title=flag))
    followers_only = make_post(community, author, None, title='fo')
    followers_only.visibility = 'followers'
    deleted = make_post(community, author, None, title='gone')
    deleted.deleted = True
    db.session.commit()
    _vote(alice, post=followers_only)
    _vote(alice, post=deleted)
    hidden_reply = make_post_reply(post, author)
    hidden_reply.visibility = 'followers'
    deleted_reply = make_post_reply(post, author)
    deleted_reply.deleted = True
    db.session.commit()
    _vote(alice, reply=hidden_reply)
    _vote(alice, reply=deleted_reply)
    _vote(alice, reply=make_post_reply(post, author), effect=-1.0)

    assert ap_get(app, '/u/alice/liked').json['totalItems'] == 0


def test_a_user_who_votes_privately_has_liked_zero(app, db_session):
    community, author, post, alice = _voter()
    _vote(alice, post=post)
    alice.vote_privately = True
    db.session.commit()

    response = ap_get(app, '/u/alice/liked')

    assert response.json['totalItems'] == 0
    assert ap_get(app, '/u/alice').json['liked'] == f'{SERVER}/u/alice/liked'


def test_liked_answers_as_the_other_person_collections_for_gone_unknown_and_browser(app, db_session):
    community, author, post, alice = _voter()
    with app.test_client() as client:
        browser = client.get('/u/alice/liked', headers={'Accept': 'text/html'})
    assert (browser.status_code, browser.headers['Location']) == (302, '/u/alice')
    assert ap_get(app, '/u/nosuch/liked').status_code == 404
    alice.deleted = True
    db.session.commit()
    assert ap_get(app, '/u/alice/liked').status_code == 410


POST_COLLECTIONS = ['likes', 'dislikes', 'shares']


@pytest.mark.parametrize('name', POST_COLLECTIONS)
def test_a_post_collection_serves_its_inline_object(app, db_session, name):
    community, author, post = seed_local_post()
    post.up_votes, post.down_votes = 4, 1
    db.session.commit()

    response = ap_get(app, f'/post/{post.id}/{name}')

    assert response.status_code == 200
    assert response.content_type == 'application/activity+json'
    body = dict(response.json)
    assert body.pop('@context')
    assert body == post_to_page(post)[name]


def _post_local_only(community, post): community.local_only = True
def _post_private(community, post): community.private = True
def _post_unpublished(community, post): post.status = POST_STATUS_REVIEWING
def _post_followers_only(community, post): post.visibility = 'followers'
def _post_deleted(community, post): post.deleted = True


@pytest.mark.parametrize('refuse', [_post_local_only, _post_private, _post_unpublished, _post_followers_only,
                                    _post_deleted])
@pytest.mark.parametrize('name', POST_COLLECTIONS)
def test_a_post_collection_is_refused_exactly_as_its_post(app, db_session, refuse, name):
    community, author, post = seed_local_post()
    refuse(community, post)
    db.session.commit()

    assert ap_get(app, f'/post/{post.id}/{name}').status_code == ap_get(app, f'/post/{post.id}').status_code


@pytest.mark.parametrize('name', POST_COLLECTIONS)
def test_a_post_collection_is_401_when_the_author_blocked_the_requester(app, db_session, name):
    community, author, post = seed_local_post()
    make_instance_block(author, make_instance('blocked.example'))

    response = ap_get(app, f'/post/{post.id}/{name}', user_agent='Test (+https://blocked.example)')

    assert response.status_code == 401


@pytest.mark.parametrize('name', POST_COLLECTIONS)
def test_an_unknown_or_remote_posts_collection_is_404(app, db_session, name):
    community, author, post = seed_local_post()
    remote = make_post(community, make_user(make_instance('remote.example'), 'far'),
                       'https://remote.example/p/1')

    assert ap_get(app, f'/post/999999/{name}').status_code == 404
    assert ap_get(app, f'/post/{remote.id}/{name}').status_code == 404


@pytest.mark.parametrize('name', POST_COLLECTIONS)
def test_a_browser_asking_for_a_post_collection_is_sent_to_the_post(app, db_session, name):
    community, author, post = seed_local_post()
    with app.test_client() as client:
        response = client.get(f'/post/{post.id}/{name}', headers={'Accept': 'text/html'})

    assert (response.status_code, response.headers['Location']) == (302, f'/post/{post.id}')


def test_liked_counts_only_votes_marked_federated(app, db_session):
    community, author, post, alice = _voter()
    _vote(alice, post=post)
    _vote(alice, post=make_post(community, author, None, title='a'), federated_like=None)
    _vote(alice, post=make_post(community, author, None, title='b'), federated_like=False)
    reply = make_post_reply(post, author)
    _vote(alice, reply=reply)
    _vote(alice, reply=make_post_reply(post, author), federated_like=None)
    _vote(alice, reply=make_post_reply(post, author), federated_like=False)

    assert ap_get(app, '/u/alice/liked').json['totalItems'] == 2


def test_a_downvote_flipped_privately_to_an_upvote_is_not_liked(app, db_session):
    community, author, post, alice = _voter()
    _vote(alice, post=post, effect=-1.0, federated_like=False)  # its Dislike was sent
    row = PostVote.query.filter_by(user_id=alice.id, post_id=post.id).one()
    row.effect = 1.0  # flipped to an upvote with no send_vote (private)
    db.session.commit()

    assert ap_get(app, '/u/alice/liked').json['totalItems'] == 0


COMMENT_COLLECTIONS = ['likes', 'dislikes']


@pytest.mark.parametrize('name', COMMENT_COLLECTIONS)
def test_a_comment_collection_serves_its_inline_object(app, db_session, name):
    community, author, post = seed_local_post()
    reply = make_post_reply(post, author)
    reply.up_votes, reply.down_votes = 2, 7
    db.session.commit()

    response = ap_get(app, f'/comment/{reply.id}/{name}')

    assert response.status_code == 200
    body = dict(response.json)
    assert body.pop('@context')
    assert body == comment_model_to_json(reply)[name]


def test_a_comment_has_no_served_shares(app, db_session):
    community, author, post = seed_local_post()
    reply = make_post_reply(post, author)

    assert ap_get(app, f'/comment/{reply.id}/shares').status_code == 404


def _reply_local_only(community, reply): community.local_only = True
def _reply_private(community, reply): community.private = True
def _reply_followers_only(community, reply): reply.visibility = 'followers'
def _reply_deleted(community, reply): reply.deleted = True


@pytest.mark.parametrize('refuse', [_reply_local_only, _reply_private, _reply_followers_only, _reply_deleted])
@pytest.mark.parametrize('name', COMMENT_COLLECTIONS)
def test_a_comment_collection_is_refused_exactly_as_its_comment(app, db_session, refuse, name):
    community, author, post = seed_local_post()
    reply = make_post_reply(post, author)
    refuse(community, reply)
    db.session.commit()

    assert (ap_get(app, f'/comment/{reply.id}/{name}').status_code
            == ap_get(app, f'/comment/{reply.id}').status_code)


@pytest.mark.parametrize('name', COMMENT_COLLECTIONS)
def test_a_comment_collection_is_401_when_the_author_blocked_the_requester(app, db_session, name):
    community, author, post = seed_local_post()
    make_instance_block(author, make_instance('blocked.example'))
    reply = make_post_reply(post, author)

    response = ap_get(app, f'/comment/{reply.id}/{name}', user_agent='Test (+https://blocked.example)')

    assert response.status_code == 401


@pytest.mark.parametrize('name', COMMENT_COLLECTIONS)
def test_an_unknown_or_remote_comments_collection_is_404(app, db_session, name):
    community, author, post = seed_local_post()
    remote = make_post_reply(post, make_user(make_instance('remote.example'), 'far'))
    remote.ap_id = 'https://remote.example/comment/1'
    db.session.commit()

    assert ap_get(app, f'/comment/999999/{name}').status_code == 404
    assert ap_get(app, f'/comment/{remote.id}/{name}').status_code == 404


@pytest.mark.parametrize('name', COMMENT_COLLECTIONS)
def test_a_browser_asking_for_a_comment_collection_is_sent_to_the_comment(app, db_session, name):
    community, author, post = seed_local_post()
    reply = make_post_reply(post, author)
    with app.test_client() as client:
        response = client.get(f'/comment/{reply.id}/{name}', headers={'Accept': 'text/html'})

    assert (response.status_code, response.headers['Location']) == (302, f'/comment/{reply.id}')
