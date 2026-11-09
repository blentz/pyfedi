"""Writing a status from a Mastodon app (plan W1.3; ruling M4): a reply is a comment, a status naming a community is a
post there, any other a post in the person's microblogs. Edit, delete and source too."""
from itertools import count

import pytest
from cachelib import SimpleCache
from werkzeug.exceptions import TooManyRequests

import app.mastodon.compose as compose
from app import cache, db
from app.constants import COMMUNITY_KIND_MICROBLOGS, VISIBILITY_FOLLOWERS, VISIBILITY_PUBLIC
from app.mastodon import ids
from app.models import Language, Post, PostReply
from app.oauth_server import tokens
from tests.factories import make_community, make_post, make_post_reply, make_user

_SERIAL = count()


@pytest.fixture
def world(app, api_baseline):
    registered, _secret = tokens.register_app('Tusky', ['https://app.example/cb'], 'read write follow', None)
    me = make_user(None, 'composer', local=True, with_keys=True)   # a local account posts with its keys
    other = make_user(None, 'composeother', local=True)
    microblogs = make_community('microblogs')
    microblogs.kind = COMMUNITY_KIND_MICROBLOGS
    cooking = make_community('cooking')
    db.session.commit()
    access, _refresh = tokens.issue_token(registered, me, 'read write')
    return {'me': me, 'other': other, 'microblogs': microblogs, 'cooking': cooking, 'app': registered,
            'token': access, 'client': app.test_client(), 'flask': app}


def _send(world, method='post', path='/api/v1/statuses', token=None, headers=None, **kwargs):
    headers = {'Authorization': f"Bearer {token or world['token']}", **(headers or {})}
    return getattr(world['client'], method)(path, headers=headers, **kwargs)


def _made(response):
    assert response.status_code == 200, response.get_data(as_text=True)
    return ids.find_status(response.get_json()['id'])


def test_a_plain_status_is_a_microblog_post_titled_by_its_first_sentence(world):
    row = _made(_send(world, json={'status': 'Baked a pie today. It was good.'}))
    assert isinstance(row, Post) and row.community_id == world['microblogs'].id
    assert row.title == 'Baked a pie today' and row.body == 'Baked a pie today. It was good.'
    assert row.user_id == world['me'].id and row.visibility == VISIBILITY_PUBLIC


def test_a_status_naming_a_community_is_a_post_there(world):
    row = _made(_send(world, json={'status': 'Best crust tips? @cooking@test.piefed.local'}))
    assert isinstance(row, Post) and row.community_id == world['cooking'].id
    assert _made(_send(world, json={'status': 'Lemmy style !cooking@test.piefed.local works'})).community_id == \
        world['cooking'].id


def test_a_community_it_does_not_know_is_microblogs(world):
    assert _made(_send(world, json={'status': 'hello @nowhere@far.example'})).community_id == world['microblogs'].id


def test_a_reply_is_a_comment_on_the_post_or_under_the_comment(world):
    post = make_post(world['cooking'], world['other'], ap_id=f'https://test.piefed.local/post/c{next(_SERIAL)}')
    top = _made(_send(world, json={'status': 'Lovely', 'in_reply_to_id': ids.status_id(post)}))
    assert isinstance(top, PostReply) and top.post_id == post.id and top.parent_id is None
    comment = make_post_reply(post, world['other'])
    nested = _made(_send(world, data={'status': 'Agreed', 'in_reply_to_id': ids.status_id(comment)}))
    assert nested.parent_id == comment.id


def test_a_reply_to_what_the_viewer_may_not_read_is_not_found(world):
    post = make_post(world['cooking'], world['other'], ap_id=f'https://test.piefed.local/post/c{next(_SERIAL)}')
    post.visibility = VISIBILITY_FOLLOWERS
    db.session.commit()
    assert _send(world, json={'status': 'hi', 'in_reply_to_id': ids.status_id(post)}).status_code == 404


def test_warning_sensitivity_language_and_visibility(world):
    db.session.add(Language(code='fr', name='French'))
    db.session.commit()
    row = _made(_send(world, json={'status': 'Spiders in the pantry.', 'spoiler_text': ' spiders ',
                                   'sensitive': True, 'language': 'FR', 'visibility': 'private'}))
    assert row.content_warning == 'spiders' and row.nsfw is True
    assert row.language.code == 'fr' and row.visibility == VISIBILITY_FOLLOWERS
    reply = _made(_send(world, json={'status': 'eek', 'spoiler_text': 'more spiders',
                                     'in_reply_to_id': ids.status_id(row)}))
    assert reply.content_warning == 'more spiders'


@pytest.mark.parametrize('body', [
    {'status': ''}, {'status': '   '}, {'status': 'x' * 5001}, {'status': 5}, {'status': 'hi', 'media_ids': ['1']},
    {'status': 'hi', 'visibility': 'direct'}, {'status': 'hi', 'visibility': 'secret'},
    {'status': 'only for followers @cooking@test.piefed.local', 'visibility': 'private'}, ['status'],
], ids=['empty', 'blank', 'too-long', 'not-text', 'media', 'direct', 'unknown-visibility',
        'followers-only-in-a-community', 'not-an-object'])
def test_what_cannot_be_posted_is_refused(world, body):
    response = _send(world, json=body)
    assert response.status_code == 422 and 'error' in response.get_json()
    assert Post.query.filter_by(user_id=world['me'].id).count() == 0


def test_a_repeat_with_the_same_idempotency_key_makes_one_status(world, monkeypatch):
    monkeypatch.setitem(world['flask'].extensions['cache'], cache, SimpleCache())
    headers = {'Idempotency-Key': 'abc-123'}
    first = _send(world, json={'status': 'Once only.'}, headers=headers).get_json()
    again = _send(world, json={'status': 'Once only.'}, headers=headers).get_json()
    assert first['id'] == again['id'] and Post.query.filter_by(user_id=world['me'].id).count() == 1
    other_key = _send(world, json={'status': 'Once only.'}, headers={'Idempotency-Key': 'other'}).get_json()
    assert other_key['id'] != first['id']
    _send(world, method='delete', path=f"/api/v1/statuses/{first['id']}")
    remade = _send(world, json={'status': 'Once only.'}, headers=headers).get_json()   # its status is gone: anew
    assert remade['id'] not in (first['id'], other_key['id'])


def test_writing_needs_write_statuses(world):
    reader, _refresh = tokens.issue_token(world['app'], world['me'], 'read')
    assert _send(world, token=reader, json={'status': 'hi'}).status_code == 403


def test_a_shared_writes_refusal_is_a_422_with_its_reason(world):
    world['me'].banned = False
    world['me'].ban_posts = False
    world['cooking'].restricted_to_mods = True
    db.session.commit()
    response = _send(world, json={'status': 'hi @cooking@test.piefed.local'})
    assert response.status_code == 422 and 'not permitted' in response.get_json()['error']


def test_deleting_ones_own_status_answers_it_with_its_text(world):
    row = _made(_send(world, json={'status': 'Gone soon.'}))
    status = ids.status_id(row)
    response = _send(world, method='delete', path=f'/api/v1/statuses/{status}')
    assert response.status_code == 200 and response.get_json()['text'] == 'Gone soon.'
    db.session.expire_all()
    assert db.session.get(Post, row.id).deleted is True
    assert _send(world, method='delete', path=f'/api/v1/statuses/{status}').status_code == 404


def test_deleting_ones_own_comment(world):
    post = make_post(world['cooking'], world['other'], ap_id=f'https://test.piefed.local/post/c{next(_SERIAL)}')
    comment = _made(_send(world, json={'status': 'oops', 'in_reply_to_id': ids.status_id(post)}))
    assert _send(world, method='delete', path=f'/api/v1/statuses/{ids.status_id(comment)}').status_code == 200
    db.session.expire_all()
    assert db.session.get(PostReply, comment.id).deleted is True


def test_anothers_status_is_not_ones_to_change(world):
    post = make_post(world['cooking'], world['other'], ap_id=f'https://test.piefed.local/post/c{next(_SERIAL)}')
    path = f'/api/v1/statuses/{ids.status_id(post)}'
    assert _send(world, method='delete', path=path).status_code == 403
    assert _send(world, method='put', path=path, json={'status': 'mine now'}).status_code == 403
    assert _send(world, method='get', path=path + '/source').status_code == 403


def test_editing_a_microblog_post_retitles_it_and_a_community_post_keeps_its_title(world):
    micro = _made(_send(world, json={'status': 'First words here. More.'}))
    edited = _send(world, method='put', path=f'/api/v1/statuses/{ids.status_id(micro)}',
                   json={'status': 'New words now. Yes.', 'spoiler_text': 'cw'})
    assert edited.status_code == 200, edited.get_data(as_text=True)
    db.session.expire_all()
    micro = db.session.get(Post, micro.id)
    assert micro.title == 'New words now' and micro.body == 'New words now. Yes.' and micro.content_warning == 'cw'
    titled = _made(_send(world, json={'status': 'Tips please. @cooking@test.piefed.local'}))
    _send(world, method='put', path=f'/api/v1/statuses/{ids.status_id(titled)}',
          json={'status': 'Changed body @cooking@test.piefed.local', 'language': 'xx'})
    db.session.expire_all()
    assert db.session.get(Post, titled.id).title == 'Tips please'


def test_editing_a_comment_and_reading_its_source(world):
    post = make_post(world['cooking'], world['other'], ap_id=f'https://test.piefed.local/post/c{next(_SERIAL)}')
    comment = _made(_send(world, json={'status': 'tpyo', 'in_reply_to_id': ids.status_id(post)}))
    path = f'/api/v1/statuses/{ids.status_id(comment)}'
    assert _send(world, method='put', path=path, data={'status': 'typo', 'spoiler_text': 'fixed'}).status_code == 200
    source = _send(world, method='get', path=path + '/source').get_json()
    assert source == {'id': ids.status_id(comment), 'text': 'typo', 'spoiler_text': 'fixed'}
    assert _send(world, method='put', path=path, json={'status': ''}).status_code == 422
    assert _send(world, method='put', path=path, json={'status': 'x' * 5001}).status_code == 422


def test_a_shared_writes_abort_keeps_its_status(world, monkeypatch):
    def refuse(*args, **kwargs):
        raise TooManyRequests()
    monkeypatch.setattr(compose.shared_post, 'make_post', refuse)
    response = _send(world, json={'status': 'Too fast.'})
    assert response.status_code == 429 and 'error' in response.get_json()


def test_a_fault_is_logged_and_its_text_never_sent(world, monkeypatch):
    """Only a shared function's deliberate refusal (a bare Exception) is answered with its reason."""
    def fail(*args, **kwargs):
        raise RuntimeError('relation "post" does not exist at 10.0.0.5')
    monkeypatch.setattr(compose.shared_post, 'make_post', fail)
    response = _send(world, json={'status': 'Breaks.'})
    assert response.status_code == 500 and response.get_json() == {'error': 'Something went wrong'}


def test_a_duplicate_reply_is_refused_with_its_reason_not_a_fault(world):
    """PostReply.new's own refusal (PostReplyValidationError) is a refusal, as the alpha API answers it."""
    post = make_post(world['cooking'], world['other'], ap_id=f'https://test.piefed.local/post/c{next(_SERIAL)}')
    body = {'status': 'Same words', 'in_reply_to_id': ids.status_id(post)}
    assert _send(world, json=body).status_code == 200
    again = _send(world, json=body)
    assert again.status_code == 422 and again.get_json() == {'error': 'Duplicate reply'}
