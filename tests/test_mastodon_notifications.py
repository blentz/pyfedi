"""Mastodon's notifications, v1 (plan W1.2): PieFed's that have a Mastodon form, about what the viewer may still read,
paged by notification id with a Link header."""
import re
from itertools import count

import pytest

from app import db
from app.constants import NOTIF_BAN, NOTIF_COMMUNITY, NOTIF_FOLLOW, NOTIF_FOLLOW_REQUEST, NOTIF_MENTION, \
    NOTIF_REPLY, NOTIF_USER, VISIBILITY_FOLLOWERS
from app.mastodon import ids
from app.models import Notification
from app.oauth_server import tokens
from tests.factories import make_community, make_post, make_post_reply, make_user

_SERIAL = count()


@pytest.fixture
def world(app, api_baseline):
    registered, _secret = tokens.register_app('Tusky', ['https://app.example/cb'], 'read write follow', None)
    me = make_user(None, 'notifme', local=True)
    other = make_user(None, 'notifother', local=True)
    community = make_community('notifs')
    access, _refresh = tokens.issue_token(registered, me, 'read')
    return {'me': me, 'other': other, 'community': community, 'app': registered, 'token': access,
            'client': app.test_client()}


def _post(world, **columns):
    post = make_post(world['community'], world['other'], ap_id=f'https://test.piefed.local/post/n{next(_SERIAL)}')
    for name, value in columns.items():
        setattr(post, name, value)
    db.session.commit()
    return post


def _notify(world, kind, subtype='', author=None, **targets):
    notification = Notification(title='t', url='/x', user_id=world['me'].id, author_id=(author or world['other']).id,
                                notif_type=kind, subtype=subtype, targets=targets)
    db.session.add(notification)
    db.session.commit()
    return notification


def _get(world, token=True, **query):
    headers = {'Authorization': f"Bearer {world['token']}"} if token else {}
    return world['client'].get('/api/v1/notifications', headers=headers, query_string=query)


def _answer(world, **query):
    response = _get(world, **query)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def test_each_kind_has_its_mastodon_form(world):
    post = _post(world)
    comment = make_post_reply(post, world['other'])
    new_post = _notify(world, NOTIF_USER, 'new_post_from_followed_user', post_id=post.id)
    in_community = _notify(world, NOTIF_COMMUNITY, 'new_post_in_followed_community', post_id=post.id,
                           community_id=world['community'].id)
    reply = _notify(world, NOTIF_REPLY, 'new_reply_on_followed_comment', post_id=post.id, comment_id=comment.id)
    post_mention = _notify(world, NOTIF_MENTION, 'post_mention', post_id=post.id)
    comment_mention = _notify(world, NOTIF_MENTION, 'comment_mention', comment_id=comment.id)
    follow = _notify(world, NOTIF_FOLLOW)
    request = _notify(world, NOTIF_FOLLOW_REQUEST)
    _notify(world, NOTIF_BAN)   # no Mastodon form
    answer = {item['id']: item for item in _answer(world)}
    assert {key: item['type'] for key, item in answer.items()} == {
        str(new_post.id): 'status', str(in_community.id): 'status', str(reply.id): 'mention',
        str(post_mention.id): 'mention', str(comment_mention.id): 'mention', str(follow.id): 'follow',
        str(request.id): 'follow_request'}
    assert answer[str(reply.id)]['status']['id'] == ids.status_id(comment)
    assert answer[str(post_mention.id)]['status']['id'] == ids.status_id(post)
    assert 'status' not in answer[str(follow.id)]
    assert answer[str(follow.id)]['account']['username'] == 'notifother'
    assert answer[str(follow.id)]['created_at'].endswith('Z')


def test_newest_first_paged_by_id_with_a_link_header(world):
    made = [_notify(world, NOTIF_FOLLOW) for _ in range(5)]
    first = _get(world, limit=2)
    assert [item['id'] for item in first.get_json()] == [str(made[4].id), str(made[3].id)]
    assert f'max_id={made[3].id}' in first.headers['Link'] and f'min_id={made[4].id}' in first.headers['Link']
    assert [item['id'] for item in _answer(world, limit=2, max_id=made[3].id)] == [str(made[2].id), str(made[1].id)]
    assert [item['id'] for item in _answer(world, limit=2, min_id=made[0].id)] == [str(made[2].id), str(made[1].id)]
    assert [item['id'] for item in _answer(world, limit=2, since_id=made[0].id)] == [str(made[4].id), str(made[3].id)]


def test_types_and_exclude_types_narrow_it(world):
    post = _post(world)
    follow, status = _notify(world, NOTIF_FOLLOW), _notify(world, NOTIF_USER, post_id=post.id)
    assert [item['id'] for item in _answer(world, **{'types[]': 'follow'})] == [str(follow.id)]
    assert [item['id'] for item in _answer(world, **{'exclude_types[]': 'follow'})] == [str(status.id)]


def test_one_about_what_the_viewer_may_not_read_is_left_out(world):
    hidden = _post(world, visibility=VISIBILITY_FOLLOWERS)
    gone = _post(world, deleted=True)
    _notify(world, NOTIF_USER, post_id=hidden.id)
    _notify(world, NOTIF_USER, post_id=gone.id)
    _notify(world, NOTIF_USER, post_id=2 ** 31 - 1)
    _notify(world, NOTIF_REPLY, post_id=hidden.id)   # no comment_id
    deleted_author = make_user(None, 'notifgone', local=True)
    deleted_author.deleted = True
    db.session.commit()
    _notify(world, NOTIF_FOLLOW, author=deleted_author)
    response = _get(world)
    assert response.get_json() == [] and 'max_id=' in response.headers['Link']   # paging leads on past them


def test_only_the_viewers_own_and_never_without_a_token(world):
    theirs = Notification(title='t', url='/x', user_id=world['other'].id, author_id=world['me'].id,
                          notif_type=NOTIF_FOLLOW, targets={})
    db.session.add(theirs)
    db.session.commit()
    assert _answer(world) == []
    assert _get(world, token=False).status_code == 401
    assert not re.search(r'max_id', _get(world).headers.get('Link', ''))


@pytest.mark.parametrize('value', ['x', '0', '99999999999'], ids=['letters', 'zero', 'huge'])
def test_a_bound_that_is_no_id_is_no_bound(world, value):
    made = _notify(world, NOTIF_FOLLOW)
    assert [item['id'] for item in _answer(world, max_id=value, limit='x')] == [str(made.id)]
