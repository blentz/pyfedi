"""Mutes (owner ruling 2026-11-08: build mutes; plan W1.3): a muted account leaves the muter's timelines, and with its
notifications their notifications, until it expires or is undone. Through Mastodon's mute, unmute, mutes and
relationships; a profile and a thread still show the muted, as Mastodon's do."""
from datetime import timedelta
from itertools import count

import pytest

from app import db
from app.constants import NOTIF_FOLLOW, SRC_API
from app.mastodon import ids
from app.models import Notification, UserMute, utcnow
from app.oauth_server import tokens
from app.shared import user as shared_user
from app.utils import Authorised
from tests.factories import make_community, make_follow, make_post, make_user

_SERIAL = count()


@pytest.fixture
def world(app, api_baseline):
    registered, _secret = tokens.register_app('Tusky', ['https://app.example/cb'], 'read write follow', None)
    me = make_user(None, 'muter', local=True)
    loud, quiet = make_user(None, 'loudone', local=True), make_user(None, 'quietone', local=True)
    make_follow(me, loud)
    make_follow(me, quiet)
    community = make_community('mutes')
    community.show_all = True
    db.session.commit()
    access, _refresh = tokens.issue_token(registered, me, 'read write follow')
    return {'me': me, 'loud': loud, 'quiet': quiet, 'community': community, 'token': access,
            'client': app.test_client()}


def _call(world, method, path, **kwargs):
    return getattr(world['client'], method)(path, headers={'Authorization': f"Bearer {world['token']}"}, **kwargs)


def _post(world, author):
    return make_post(world['community'], author, ap_id=f'https://test.piefed.local/post/mu{next(_SERIAL)}')


def _ids(response):
    return [status['id'] for status in response.get_json()]


def test_a_muted_accounts_posts_leave_the_timelines_but_not_their_profile(world):
    loud_post, quiet_post = _post(world, world['loud']), _post(world, world['quiet'])
    muted = _call(world, 'post', f"/api/v1/accounts/{world['loud'].id}/mute")
    assert muted.status_code == 200 and muted.get_json()['muting'] is True
    assert muted.get_json()['muting_notifications'] is True
    for path in ('/api/v1/timelines/home', '/api/v1/timelines/public', '/api/v1/timelines/tag/none'):
        assert ids.status_id(loud_post) not in _ids(_call(world, 'get', path))
    assert ids.status_id(quiet_post) in _ids(_call(world, 'get', '/api/v1/timelines/home'))
    profile = _call(world, 'get', f"/api/v1/accounts/{world['loud'].id}/statuses")
    assert ids.status_id(loud_post) in _ids(profile)
    assert _call(world, 'post', f"/api/v1/accounts/{world['loud'].id}/unmute").get_json()['muting'] is False
    assert ids.status_id(loud_post) in _ids(_call(world, 'get', '/api/v1/timelines/home'))


def test_notifications_go_only_with_a_mute_that_says_so(world):
    def notify(author):
        db.session.add(Notification(title='t', url='/x', user_id=world['me'].id, author_id=author.id,
                                    notif_type=NOTIF_FOLLOW, targets={}))
        db.session.commit()
    notify(world['loud'])
    notify(world['quiet'])
    _call(world, 'post', f"/api/v1/accounts/{world['loud'].id}/mute", json={'notifications': False})
    _call(world, 'post', f"/api/v1/accounts/{world['quiet'].id}/mute", data={'notifications': 'true'})
    authors = [item['account']['username'] for item in _call(world, 'get', '/api/v1/notifications').get_json()]
    assert authors == ['loudone']
    relationship = _call(world, 'get', '/api/v1/accounts/relationships',
                         query_string={'id[]': str(world['loud'].id)}).get_json()[0]
    assert relationship['muting'] is True and relationship['muting_notifications'] is False


def test_a_mute_with_a_duration_ends_by_itself(world):
    _call(world, 'post', f"/api/v1/accounts/{world['loud'].id}/mute", json={'duration': 60})
    mute = UserMute.query.filter_by(muter_id=world['me'].id).one()
    assert mute.expires_at is not None and mute.expires_at > utcnow()
    mute.expires_at = utcnow() - timedelta(seconds=1)
    db.session.commit()
    assert shared_user.muted_ids(world['me'].id) == []
    assert _call(world, 'get', '/api/v1/mutes').get_json() == []


def test_muting_again_replaces_the_mute_and_mutes_lists_them(world):
    _call(world, 'post', f"/api/v1/accounts/{world['loud'].id}/mute", json={'duration': 'soon'})   # no end
    _call(world, 'post', f"/api/v1/accounts/{world['loud'].id}/mute", json={'notifications': False})
    mute = UserMute.query.filter_by(muter_id=world['me'].id).one()
    assert mute.expires_at is None and mute.notifications is False
    assert [account['username'] for account in _call(world, 'get', '/api/v1/mutes').get_json()] == ['loudone']


def test_oneself_and_nobody_cannot_be_muted(world):
    assert _call(world, 'post', f"/api/v1/accounts/{world['me'].id}/mute").status_code == 422
    assert _call(world, 'post', '/api/v1/accounts/99999999/mute').status_code == 404
    with pytest.raises(Exception, match='user not found'):   # the shared function, called directly
        shared_user.mute_user(2 ** 31 - 1, True, 0, SRC_API, Authorised(world['me']))
