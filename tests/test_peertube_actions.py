"""Rating and commenting in PeerTube's API (plan W2.2a; spec 2026-11-09-peertube-login-design §3): a like is an
upvote and a dislike a downvote, set as a state; a comment and a reply go through the shared reply code and answer
200 {comment}, as the official app requires; a person deletes only their own."""
import uuid

import pytest
from werkzeug.exceptions import Forbidden

from app import db
from app.constants import POST_TYPE_VIDEO
from app.models import PostReply, PostVote
from app.oauth_server import tokens
from app.shared.media import with_media
from tests import peertube_app_contract as contract
from tests.factories import make_community, make_post, make_post_reply, make_user


@pytest.fixture
def world(app, api_baseline):
    person = make_user(None, 'ptact', local=True, with_keys=True)   # a local account comments with its keys
    author = make_user(None, 'ptactauthor', local=True)
    community = make_community('ptact')
    community.show_all = True
    server = app.config['SERVER_URL']
    post = make_post(community, author, ap_id=f'{server}/post/pta{uuid.uuid4().hex[:8]}', title='Act on me')
    post.type = POST_TYPE_VIDEO
    post.url = f'{server}/static/media/posts/ab/cd/abcd{uuid.uuid4().hex[:11]}.mp4'
    with_media(post, status='ready', uuid=str(uuid.uuid4()), mime='video/mp4', bytes=1, duration_s=3, width=640,
               height=360, fps=30, codec='h264')
    db.session.commit()
    local, _secret = tokens.local_client()
    access, _refresh = tokens.issue_token(local, person, tokens.LOCAL_CLIENT_SCOPE)
    return {'person': person, 'author': author, 'post': post, 'local': local, 'client': app.test_client(),
            'headers': {'Authorization': f'Bearer {access}'}}


def _call(world, method, path, **kwargs):
    return getattr(world['client'], method)(path, headers=world['headers'], **kwargs)


def _rating(world):
    return _call(world, 'get', f"/api/v1/users/me/videos/{world['post'].id}/rating").get_json()['rating']


def _effects(world):
    return [vote.effect for vote in PostVote.query.filter_by(user_id=world['person'].id, post_id=world['post'].id)]


def test_rating_is_a_state_like_dislike_and_none(world):
    path = f"/api/v1/videos/{world['post'].id}/rate"
    assert _rating(world) == 'none'
    assert _call(world, 'put', path, json={'rating': 'like'}).status_code == 204
    assert _rating(world) == 'like' and [effect > 0 for effect in _effects(world)] == [True]
    assert _call(world, 'put', path, json={'rating': 'like'}).status_code == 204   # not toggled off
    assert _rating(world) == 'like'
    assert _call(world, 'put', path, data={'rating': 'dislike'}).status_code == 204   # TubeLab sends a form
    assert _rating(world) == 'dislike' and [effect < 0 for effect in _effects(world)] == [True]
    assert _call(world, 'put', path, json={'rating': 'none'}).status_code == 204
    assert _rating(world) == 'none' and _effects(world) == []
    assert _call(world, 'put', path, json={'rating': 'none'}).status_code == 204


def test_a_rating_that_is_none_of_the_three_is_refused(world):
    response = _call(world, 'put', f"/api/v1/videos/{world['post'].id}/rate", json={'rating': 'love'})
    assert response.status_code == 400 and response.get_json()['code'] == 'invalid_request'


@pytest.mark.parametrize('method, path', [('get', '/api/v1/users/me/videos/999999/rating'),
                                          ('put', '/api/v1/videos/999999/rate'),
                                          ('post', '/api/v1/videos/999999/comment-threads'),
                                          ('post', '/api/v1/videos/999999/comments/1'),
                                          ('delete', '/api/v1/videos/999999/comments/1')])
def test_no_such_video_is_a_404(world, method, path):
    assert _call(world, method, path, json={'rating': 'like', 'text': 'hi'}).status_code == 404


def test_a_shared_refusal_is_a_400_with_its_reason(world, monkeypatch):
    def refuse(*args, **kwargs):
        raise Exception('You have voted too much')
    monkeypatch.setattr('app.shared.post.vote_for_post', refuse)
    response = _call(world, 'put', f"/api/v1/videos/{world['post'].id}/rate", json={'rating': 'like'})
    assert response.status_code == 400 and 'too much' in response.get_json()['detail']


def test_a_comment_answers_200_with_the_comment_as_the_app_decodes_it(world):
    response = _call(world, 'post', f"/api/v1/videos/{world['post'].id}/comment-threads",
                     json={'text': 'A *fine* video'})
    assert response.status_code == 200, response.get_data(as_text=True)
    made = response.get_json()['comment']
    assert contract.problems(made, 'VideoCommentThread') == []
    assert made['threadId'] == made['id'] and made['videoId'] == world['post'].id and '<em>fine</em>' in made['text']
    reply = db.session.get(PostReply, made['id'])
    assert reply.user_id == world['person'].id and reply.parent_id is None
    threads = world['client'].get(f"/api/v1/videos/{world['post'].id}/comment-threads").get_json()['data']
    assert made['id'] in [thread['id'] for thread in threads]


def test_a_reply_lands_under_its_comment_in_its_thread(world):
    top = _call(world, 'post', f"/api/v1/videos/{world['post'].id}/comment-threads",
                json={'text': 'First'}).get_json()['comment']['id']
    response = _call(world, 'post', f"/api/v1/videos/{world['post'].id}/comments/{top}", data={'text': 'Agreed'})
    assert response.status_code == 200, response.get_data(as_text=True)
    made = response.get_json()['comment']
    assert made['threadId'] == top and made['inReplyToCommentId'] == top
    assert db.session.get(PostReply, made['id']).parent_id == top
    deeper = _call(world, 'post', f"/api/v1/videos/{world['post'].id}/comments/{made['id']}", json={'text': 'Deeper'})
    assert deeper.get_json()['comment']['threadId'] == top


@pytest.mark.parametrize('text', ['', '   ', None, 'x' * 5001, 7], ids=['empty', 'blank', 'none', 'long', 'number'])
def test_a_comment_needs_text_within_the_limit(world, text):
    response = _call(world, 'post', f"/api/v1/videos/{world['post'].id}/comment-threads", json={'text': text})
    assert response.status_code == 400 and response.get_json()['code'] == 'invalid_request'


def test_a_reply_to_a_comment_of_another_video_is_a_404(world):
    other = make_post(world['post'].community, world['author'], ap_id=f'https://x.example/{uuid.uuid4().hex}')
    elsewhere = make_post_reply(other, world['author'])
    response = _call(world, 'post', f"/api/v1/videos/{world['post'].id}/comments/{elsewhere.id}", json={'text': 'x'})
    assert response.status_code == 404


def test_a_person_deletes_their_own_comment_and_not_anothers(world):
    made = _call(world, 'post', f"/api/v1/videos/{world['post'].id}/comment-threads",
                 json={'text': 'Mine'}).get_json()['comment']
    theirs = make_post_reply(world['post'], world['author'])
    refused = _call(world, 'delete', f"/api/v1/videos/{world['post'].id}/comments/{theirs.id}")
    assert refused.status_code == 403 and not db.session.get(PostReply, theirs.id).deleted
    assert _call(world, 'delete', f"/api/v1/videos/{world['post'].id}/comments/{made['id']}").status_code == 204
    db.session.expire_all()
    assert db.session.get(PostReply, made['id']).deleted


def test_a_shared_reply_refusal_is_a_400(world, monkeypatch):
    def refuse(*args, **kwargs):
        raise Exception('rate_limited')
    monkeypatch.setattr('app.shared.reply.make_reply', refuse)
    response = _call(world, 'post', f"/api/v1/videos/{world['post'].id}/comment-threads", json={'text': 'hi'})
    assert response.status_code == 400 and response.get_json()['detail'] == 'rate_limited'


def test_acting_needs_a_token_that_may_write(world):
    reader, _refresh = tokens.issue_token(world['local'], world['person'], 'read')
    response = world['client'].put(f"/api/v1/videos/{world['post'].id}/rate", json={'rating': 'like'},
                                   headers={'Authorization': f'Bearer {reader}'})
    assert response.status_code == 403 and _effects(world) == []


@pytest.mark.parametrize('raised, status, code', [(Forbidden(), 403, 'forbidden'),
                                                  (ValueError('bug'), 500, 'internal_error')], ids=['abort', 'bug'])
def test_a_shared_writes_abort_is_itself_and_a_bug_says_nothing(world, monkeypatch, raised, status, code):
    def fail(*args, **kwargs):
        raise raised
    monkeypatch.setattr('app.shared.post.vote_for_post', fail)
    response = _call(world, 'put', f"/api/v1/videos/{world['post'].id}/rate", json={'rating': 'like'})
    assert response.status_code == status and response.get_json()['code'] == code
    assert 'bug' not in response.get_data(as_text=True)


def test_a_refused_delete_is_a_400(world, monkeypatch):
    made = _call(world, 'post', f"/api/v1/videos/{world['post'].id}/comment-threads",
                 json={'text': 'Mine'}).get_json()['comment']
    def refuse(*args, **kwargs):
        raise Exception('This comment cannot be deleted')
    monkeypatch.setattr('app.shared.reply.delete_reply', refuse)
    response = _call(world, 'delete', f"/api/v1/videos/{world['post'].id}/comments/{made['id']}")
    assert response.status_code == 400 and 'cannot be deleted' in response.get_json()['detail']
