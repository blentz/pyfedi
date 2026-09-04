"""tests/test_ap_refresh_profiles.py

The three refresh-profile tasks, driven directly.

They are celery tasks, but every caller invokes them inline under
`current_app.debug` and the `app` fixture puts celery in eager mode, so
calling the undecorated function is the same code path a worker runs, with no
mock anywhere. `tests/test_ap_refresh_community_profile.py` established this
for the community task and covers its legacy flair loop; this file covers the
actor re-fetch that file scopes out, for all three tasks.

`PEER` must not end in `.local`: `get_request()` rejects those via
`is_invalid_get_request_uri()` before respx ever sees the request, so a
`.local` fixture fails for a reason unrelated to the code under test.
"""
import httpx
import pytest

from app import db
from app.activitypub.util import (refresh_community_profile_task,
                                  refresh_feed_profile_task,
                                  refresh_user_profile_task)
from app.models import Community, Feed, Instance, User
from tests.factories import (make_community, make_feed, make_instance, make_user,
                             seed_community_owner)

PEER = 'peer.example'


def _remote_user(name='wakko'):
    """A remote user on an online instance, ready to refresh.

    `ap_public_url` is what the task fetches, so it is set explicitly rather
    than left to the factory -- the task would otherwise request `None` and
    fail inside httpx rather than in the code under test.
    """
    instance = seed_community_owner(PEER)
    user = make_user(instance, name)
    user.ap_public_url = f'https://{PEER}/u/{name}'
    user.ap_profile_id = f'https://{PEER}/u/{name}'
    db.session.commit()
    return user


def _remote_community(name='memes'):
    """A remote community with nothing the task would fetch beyond the actor.

    `make_community` sets `ap_followers_url`; it is cleared so the followers
    collection is never fetched. `http_mock`'s `assert_all_called=True` would
    not catch that omission -- `block_outbound_http` would, by raising.
    """
    seed_community_owner(PEER)
    community = make_community(name, host=PEER)
    community.ap_public_url = f'https://{PEER}/c/{name}'
    community.ap_followers_url = None
    db.session.commit()
    return community


def _remote_feed(name='news'):
    """A REMOTE feed -- `make_local_feed` would give a local one, and
    `refresh_feed_profile_task` guards `not feed.is_local()`, so a local feed
    returns before fetching anything.
    """
    instance = seed_community_owner(PEER)
    feed = make_feed(instance, name)
    feed.ap_public_url = f'https://{PEER}/f/{name}'
    feed.ap_followers_url = None
    db.session.commit()
    return feed


def _person_document(name='wakko', fields=None):
    """The peer's Person document, holding only the keys the task reads
    unconditionally. Every other key is opted into via `fields`, so the absent
    side of each guard is what the baseline already gives.
    """
    document = {
        'type': 'Person',
        'id': f'https://{PEER}/u/{name}',
        'preferredUsername': name,
        'publicKey': {'publicKeyPem': '-----BEGIN PUBLIC KEY-----refreshed'},
    }
    if fields:
        document.update(fields)
    return document


def _group_document(name='memes', fields=None):
    """The peer's Group document. `name` becomes the community title.

    `tag` is deliberately absent and must stay absent unless a test means to
    reach the legacy flair loop, which `tests/test_ap_refresh_community_profile.py`
    already covers and this file does not re-test.
    """
    document = {
        'type': 'Group',
        'id': f'https://{PEER}/c/{name}',
        'preferredUsername': name,
        'name': 'Memes, refreshed',
        'publicKey': {'publicKeyPem': '-----BEGIN PUBLIC KEY-----refreshed'},
    }
    if fields:
        document.update(fields)
    return document


def _feed_document(name='news', fields=None):
    """The peer's Feed document."""
    document = {
        'type': 'Feed',
        'id': f'https://{PEER}/f/{name}',
        'preferredUsername': name,
        'name': 'News, refreshed',
        'publicKey': {'publicKeyPem': '-----BEGIN PUBLIC KEY-----refreshed'},
    }
    if fields:
        document.update(fields)
    return document


def _serve(http_mock, url, document=None, status=200, text=None):
    """Register one respx route.

    `text` serves a raw body instead of JSON, which is how the malformed-JSON
    tests reach the `.json()` call with something that cannot be decoded.

    `http_mock` is created with `assert_all_called=True`, so every route
    registered here MUST be exercised by the test that registers it.
    """
    if text is not None:
        return http_mock.get(url).mock(
            return_value=httpx.Response(status, text=text))
    return http_mock.get(url).mock(
        return_value=httpx.Response(status, json=document))


def test_refreshing_a_user_applies_the_peers_document(app, db_session, http_mock):
    """`refresh_user_profile_task`'s ordinary path: fetch the actor document
    and apply it.

    The assertion is on the User row's own columns, not on the absence of an
    exception. The task commits the refreshed profile before doing anything
    else, so "nothing raised" would not distinguish a working apply from one
    that abandoned the document.

    `_remote_user`'s default name matches the document's `preferredUsername`
    ('wakko' on both sides), so `user.user_name` is seeded to a contrary
    value ('stale') before the call -- without that, the assertion would
    pass against a no-op just as readily as against a genuine apply.
    """
    user = _remote_user()
    user.user_name = 'stale'
    db.session.commit()
    _serve(http_mock, user.ap_public_url,
           _person_document(fields={'name': 'Wakko Warner'}))

    refresh_user_profile_task(user.id)

    db.session.refresh(user)
    assert user.user_name == 'wakko'
    assert user.title == 'Wakko Warner'
