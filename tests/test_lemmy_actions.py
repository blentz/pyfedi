"""What a member does in Lemmy's API for Jerboa (plan W3; spec 2026-11-10-lemmy-v3-design §2, ruling L2): votes,
comments, saves, read marks, hiding, following and blocking go through alpha's handlers and answer Lemmy 0.19.11's
models; a mistyped body is refused as alpha refuses it; creating a comment shares alpha's limit."""
import uuid

import pytest

from app import cache, db, limiter
from app.models import CommunityBlock, CommunityMember, PostBookmark, PostReply, PostReplyBookmark, UserBlock, \
    read_posts
from tests import lemmy_app_contract as contract
from tests import test_onion_guard_limits as limit_tests
from tests.factories import make_community, make_post, make_post_reply, make_user

limited_app = limit_tests.limited_app   # an app whose limiter counts


@pytest.fixture
def world(app, api_baseline, monkeypatch):
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    cache.clear()
    person, author = make_user(None, 'lemmyactor', local=True, with_keys=True), make_user(None, 'lemmyacted', local=True)
    person.verified = True   # a local account comments once verified and with its keys (can_create_post_reply)
    community = make_community('lemmyactions')
    post = make_post(community, author, ap_id=f'https://test.piefed.local/post/{uuid.uuid4().hex[:10]}')
    reply = make_post_reply(post, author, body='a comment')
    reply.path, reply.depth = [0, reply.id], 0
    db.session.commit()
    return {'person': person, 'author': author, 'community': community, 'post': post, 'reply': reply,
            'client': app.test_client(), 'headers': {'Authorization': f'Bearer {person.encode_jwt_token()}'}}


def _call(world, method, path, json, model, signed_in=True):
    response = getattr(world['client'], method)(path, json=json, headers=world['headers'] if signed_in else {})
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    if model:
        assert (found := contract.problems(body, model)) == [], found
    return body


def test_a_vote_on_a_post_counts_and_can_be_taken_back(world):
    up = _call(world, 'post', '/api/v3/post/like', {'post_id': world['post'].id, 'score': 1}, 'PostResponse')
    assert up['post_view']['my_vote'] == 1 and up['post_view']['counts']['upvotes'] == 1
    back = _call(world, 'post', '/api/v3/post/like', {'post_id': world['post'].id, 'score': 0}, 'PostResponse')
    assert back['post_view']['my_vote'] == 0 and back['post_view']['counts']['upvotes'] == 0


def test_a_post_is_saved_and_unsaved(world):
    saved = _call(world, 'put', '/api/v3/post/save', {'post_id': world['post'].id, 'save': True}, 'PostResponse')
    assert saved['post_view']['saved'] and PostBookmark.query.filter_by(user_id=world['person'].id).count() == 1
    _call(world, 'put', '/api/v3/post/save', {'post_id': world['post'].id, 'save': False}, 'PostResponse')
    assert PostBookmark.query.filter_by(user_id=world['person'].id).count() == 0


def test_an_own_post_is_deleted(world):
    own = make_post(world['community'], world['person'], ap_id=f'https://test.piefed.local/post/{uuid.uuid4().hex}')
    body = _call(world, 'post', '/api/v3/post/delete', {'post_id': own.id, 'deleted': True}, 'PostResponse')
    assert body['post_view']['post']['deleted']


def test_posts_are_marked_read(world):
    body = _call(world, 'post', '/api/v3/post/mark_as_read', {'post_ids': [world['post'].id], 'read': True}, None)
    assert body == {'success': True}
    assert db.session.execute(read_posts.select().where(read_posts.c.user_id == world['person'].id)).first()


def test_posts_are_hidden_several_at_once(world):
    other = make_post(world['community'], world['author'], ap_id=f'https://test.piefed.local/post/{uuid.uuid4().hex}')
    body = _call(world, 'post', '/api/v3/post/hide', {'post_ids': [world['post'].id, other.id], 'hide': True}, None)
    assert body == {'success': True}
    listed = _call(world, 'get', '/api/v3/post/list', None, 'GetPostsResponse')
    assert {view['post']['id'] for view in listed['posts']}.isdisjoint({world['post'].id, other.id})


@pytest.mark.parametrize('fields', [{}, {'post_ids': [], 'hide': True}, {'post_ids': [1], 'hide': 'yes'}])
def test_hiding_needs_posts_and_a_yes_or_no(world, fields):
    response = world['client'].post('/api/v3/post/hide', json=fields, headers=world['headers'])
    assert response.status_code == 400 and response.get_json() == {'error': 'invalid_request'}


def test_a_comment_is_made_edited_and_deleted_under_lemmys_names(world):
    made = _call(world, 'post', '/api/v3/comment', {'content': 'Hello there', 'post_id': world['post'].id,
                                                    'parent_id': world['reply'].id}, 'CommentResponse')
    comment = made['comment_view']['comment']
    assert comment['content'] == 'Hello there' and comment['path'].endswith(f"{world['reply'].id}.{comment['id']}")
    edited = _call(world, 'put', '/api/v3/comment', {'comment_id': comment['id'], 'content': 'Hello again'},
                   'CommentResponse')
    assert edited['comment_view']['comment']['content'] == 'Hello again'
    deleted = _call(world, 'post', '/api/v3/comment/delete', {'comment_id': comment['id'], 'deleted': True},
                    'CommentResponse')
    assert deleted['comment_view']['comment']['deleted'] and db.session.get(PostReply, comment['id']).deleted


def test_a_comment_is_voted_on_and_saved(world):
    voted = _call(world, 'post', '/api/v3/comment/like', {'comment_id': world['reply'].id, 'score': -1},
                  'CommentResponse')
    assert voted['comment_view']['my_vote'] == -1 and voted['comment_view']['counts']['downvotes'] == 1
    saved = _call(world, 'put', '/api/v3/comment/save', {'comment_id': world['reply'].id, 'save': True},
                  'CommentResponse')
    assert saved['comment_view']['saved'] and PostReplyBookmark.query.filter_by(user_id=world['person'].id).count() == 1


def test_a_community_is_followed_and_left(world):
    joined = _call(world, 'post', '/api/v3/community/follow', {'community_id': world['community'].id, 'follow': True},
                   'CommunityResponse')
    assert joined['community_view']['subscribed'] == 'Subscribed'
    assert CommunityMember.query.filter_by(user_id=world['person'].id, community_id=world['community'].id).count() == 1
    left = _call(world, 'post', '/api/v3/community/follow', {'community_id': world['community'].id, 'follow': False},
                 'CommunityResponse')
    assert left['community_view']['subscribed'] == 'NotSubscribed'


def test_a_community_and_a_person_are_blocked(world):
    body = _call(world, 'post', '/api/v3/community/block', {'community_id': world['community'].id, 'block': True},
                 'BlockCommunityResponse')
    assert body['blocked'] and CommunityBlock.query.filter_by(user_id=world['person'].id).count() == 1
    body = _call(world, 'post', '/api/v3/user/block', {'person_id': world['author'].id, 'block': True},
                 'BlockPersonResponse')
    assert body['blocked'] and body['person_view']['person']['name'] == 'lemmyacted'
    assert UserBlock.query.filter_by(blocker_id=world['person'].id).count() == 1


@pytest.mark.parametrize('path,fields', [('/api/v3/post/like', {'post_id': 'x', 'score': 1}),
                                         ('/api/v3/comment', {'post_id': 1}),
                                         ('/api/v3/community/follow', None)])
def test_a_mistyped_body_is_refused_in_lemmys_shape(world, path, fields):
    response = world['client'].post(path, json=fields, headers=world['headers'])
    assert response.status_code == 400 and set(response.get_json()) == {'error'}


def test_a_write_without_a_login_is_refused(world):
    response = world['client'].post('/api/v3/post/like', json={'post_id': world['post'].id, 'score': 1})
    assert response.status_code == 400 and response.get_json() == {'error': 'incorrect_login'}


def test_making_comments_shares_alphas_limit(limited_app, api_baseline, monkeypatch):
    monkeypatch.setitem(limited_app.config, 'ENABLE_ALPHA_API', 'true')
    with limited_app.app_context():
        person = make_user(None, 'lemmylimited', local=True, with_keys=True)
        author, person.verified = make_user(None, 'lemmylimitedauthor', local=True), True
        post = make_post(make_community('lemmylimited'), author, ap_id=f'https://test.piefed.local/post/{uuid.uuid4()}')
        headers, away = {'Authorization': f'Bearer {person.encode_jwt_token()}'}, {'REMOTE_ADDR': '203.0.113.7'}
        client = limited_app.test_client()
        limiter.reset()
        limiter.enabled = True
        try:
            codes = [client.post('/api/alpha/comment', json={'body': f'alpha {n}', 'post_id': post.id},
                                 headers=headers, environ_base=away).status_code for n in range(2)]
            codes += [client.post('/api/v3/comment', json={'content': f'lemmy {n}', 'post_id': post.id},
                                  headers=headers, environ_base=away).status_code for n in range(2)]
        finally:
            limiter.reset()
            limiter.enabled = False
    assert codes == [200, 200, 200, 429]
