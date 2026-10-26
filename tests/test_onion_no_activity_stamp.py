"""Onion traffic leaves no activity stamp (onion unlinkability spec 2026-10-26, goal 3).

An onion request, or an onion login, never moves a clearnet account's `last_seen` or `email_unread_sent`. A clearnet
request still does. The clock is frozen, so "unchanged" cannot be a coincidence of timing.
"""
from datetime import datetime
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.api.alpha.utils.user import put_user_save_user_settings
from app.constants import SRC_API
from app.models import User
from app.onion import stamp_activity
from app.shared.post import delete_post, mark_post_read, restore_post
from app.utils import finalize_user_setup
from tests.factories import bearer, make_post_reply
from tests.test_onion_marker import ONION, PROXY, onion_context, proxy_ip
from tests.test_onion_unlinkable_ip import (CLEARNET_IP, WORLD, a_post, a_reply, account, api_login, oauth_login,
                                            refreshed, web_login, world)  # noqa: F401  (world is a fixture)
from tests.two_domains import default_request, two_domains  # noqa: F401  (a fixture)

LONG_AGO = datetime(2020, 1, 1, 12, 0, 0)
FROZEN = datetime(2026, 10, 26, 3, 4, 5)


@pytest.fixture(autouse=True)
def frozen_clock():
    with patch('app.onion.utcnow', return_value=FROZEN):
        yield


def sleepy_account(world, domain=None, name='sleeper'):
    user = account(world, domain or world.domains[0], name)
    user.last_seen = LONG_AGO
    user.email_unread_sent = True
    db.session.commit()
    return user


def vote_on_post(user):
    WORLD['post'].vote(user, 'upvote', None)


def vote_on_reply(user):
    WORLD['reply'].vote(user, 'upvote', None)


def own_post_deleted_then_restored(user):
    own = WORLD['own_post']
    own.user_id = user.id
    db.session.commit()
    delete_post(own.id, False, SRC_API, bearer(user))
    restore_post(own.id, SRC_API, bearer(user))


def reading(user):
    mark_post_read([WORLD['post'].id], True, user.id)
    mark_post_read([WORLD['post'].id], False, user.id)


def settings(user):
    with patch('app.api.alpha.utils.user.cache'):
        put_user_save_user_settings(bearer(user), {'default_sort_type': 'Hot'})


def verified(user):
    finalize_user_setup(user)
    db.session.commit()


ACTIONS = [web_login, oauth_login, api_login, a_post, a_reply, vote_on_post, vote_on_reply,
           own_post_deleted_then_restored, reading, settings, verified]


@pytest.fixture
def stampable(world):
    from tests.factories import make_post as make_post_row
    WORLD['reply'] = make_post_reply(WORLD['post'], world.local_user('replied', world.domains[0]))
    WORLD['own_post'] = make_post_row(WORLD['post'].community, world.local_user('poster', world.domains[0]),
                                      'https://elsewhere.example/p/2')
    return world


def stamps(user):
    user = refreshed(user)
    return user.last_seen, user.email_unread_sent


@pytest.mark.parametrize('action', ACTIONS, ids=lambda action: action.__name__)
def test_onion_page_view_vote_and_api_call_leave_last_seen_unchanged(app, stampable, action):
    user = sleepy_account(stampable)
    with proxy_ip(app), onion_context(app):
        action(user)
    assert stamps(user) == (LONG_AGO, True), action.__name__


@pytest.mark.parametrize('action', ACTIONS, ids=lambda action: action.__name__)
def test_clearnet_request_still_stamps_last_seen(app, stampable, action):
    user = sleepy_account(stampable)
    with default_request(app), patch('app.shared.post.ip_address', return_value=CLEARNET_IP), \
            patch('app.shared.reply.ip_address', return_value=CLEARNET_IP):
        action(user)
    last_seen, unread_sent = stamps(user)
    assert last_seen > LONG_AGO, action.__name__


def test_onion_login_leaves_last_seen_unchanged(app, stampable):
    user = sleepy_account(stampable)
    for login in (web_login, oauth_login, api_login):
        with proxy_ip(app), onion_context(app):
            login(user)
    assert stamps(user) == (LONG_AGO, True)


def test_a_logged_in_onion_page_view_leaves_both_stamps_unchanged(app, stampable):
    user = sleepy_account(stampable)
    client = app.test_client()
    with proxy_ip(app):
        with client.session_transaction(base_url='http://abc.onion') as session:
            session['_user_id'] = str(user.id)
            session['_fresh'] = True
        for _ in range(3):   # a cookieless onion request is sent out and back for its session cookie
            response = client.get('/', base_url='http://abc.onion', headers={**ONION, 'X-Forwarded-For': '10.89.0.7'},
                                  environ_base={'REMOTE_ADDR': PROXY})
            if response.status_code != 303:
                break
    assert response.status_code == 200
    assert stamps(user) == (LONG_AGO, True)


def test_a_logged_in_clearnet_page_view_stamps_both(app, stampable):
    user = sleepy_account(stampable)
    client = app.test_client()
    with client.session_transaction(base_url='https://test.piefed.local') as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    assert client.get('/', base_url='https://test.piefed.local').status_code == 200
    assert stamps(user) == (FROZEN, False)


def test_onion_request_leaves_email_unread_sent_unchanged(app, stampable):
    user = sleepy_account(stampable)
    with proxy_ip(app), onion_context(app):
        stamp_activity(user)
    db.session.commit()
    assert stamps(user) == (LONG_AGO, True)


def test_clearnet_stamp_sets_both(app, stampable):
    user = sleepy_account(stampable)
    with default_request(app):
        stamp_activity(user)
    db.session.commit()
    assert stamps(user) == (FROZEN, False)


def test_work_outside_a_request_still_stamps(app, stampable):
    user = sleepy_account(stampable)
    stamp_activity(user)
    db.session.commit()
    assert stamps(user) == (FROZEN, False)
