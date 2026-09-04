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
from app.activitypub import util as ap_util
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


def test_refreshing_an_unknown_user_id_does_nothing(app, db_session, monkeypatch):
    """First conjunct: `user` is None when the id resolves to no row.

    THE OBSERVABLE IS A SPY ON `get_request`, NOT `block_outbound_http`. A
    dropped conjunct here reaches `user.instance_id` on `None` and crashes
    with `AttributeError` before any fetch, so an outbound-HTTP observable
    would still attribute this one correctly -- but is switched to a spy for
    consistency with the other two conjuncts, where the swallow described
    below applies.
    """
    _remote_user()
    calls = []

    def _spy(*a, **kw):
        calls.append(a)
        return httpx.Response(200, json=_person_document())

    monkeypatch.setattr(ap_util, 'get_request', _spy)

    refresh_user_profile_task(999999)

    assert calls == []


def test_refreshing_a_user_with_no_instance_does_nothing(app, db_session, monkeypatch):
    """Second conjunct: `user.instance_id`. NO FACTORY produces a NULL
    instance_id, so it is set explicitly here -- a test resting on the factory
    would never reach this branch.

    This conjunct is the one `refresh_community_profile_task` and
    `refresh_feed_profile_task` LACK, which is what makes them crash on the
    same row. Their pins are in Tasks 5 and 7.

    THE OBSERVABLE IS A SPY ON `get_request`, NOT `block_outbound_http`. See
    `test_refreshing_a_user_on_a_dormant_instance_does_nothing` for why: the
    same swallow applies to any dropped conjunct that lets the task reach the
    fetch, not only the third.
    """
    user = _remote_user()
    user.instance_id = None
    db.session.commit()
    calls = []

    def _spy(*a, **kw):
        calls.append(a)
        return httpx.Response(200, json=_person_document())

    monkeypatch.setattr(ap_util, 'get_request', _spy)

    refresh_user_profile_task(user.id)

    assert calls == []


def test_refreshing_a_user_on_a_dormant_instance_does_nothing(app, db_session, monkeypatch):
    """Third conjunct: `user.instance.online()`, which is
    `not (dormant or gone_forever)` (app/models.py). `dormant` is set
    explicitly -- it defaults to False, so a test resting on the default
    would assert the wrong side of the branch.

    THE OBSERVABLE IS A SPY ON `get_request`, NOT `block_outbound_http`.
    Dropping this conjunct lets the task reach the fetch, where respx raises
    `AllMockedAssertionError` -- not an `httpx.HTTPError`, so the task's bare
    `except:` at util.py:669 catches it, `signed_get_request` fails too, the
    inner bare `except:` at :674 catches that, and the task returns silently.
    The guard firing and not firing are then indistinguishable via outbound
    HTTP. Asserting the fetch was never attempted survives that swallow.

    The spy returns a real `httpx.Response` (rather than `None`) so that, if
    this conjunct is dropped, the task runs to completion -- fetches,
    applies the document, and returns normally -- and the kill lands on
    `assert calls == []` itself, not on an incidental crash one line past the
    spy.
    """
    user = _remote_user()
    user.instance.dormant = True
    db.session.commit()
    calls = []

    def _spy(*a, **kw):
        calls.append(a)
        return httpx.Response(200, json=_person_document())

    monkeypatch.setattr(ap_util, 'get_request', _spy)

    refresh_user_profile_task(user.id)

    assert calls == []


def test_a_failed_fetch_is_retried_once(app, db_session, http_mock, no_real_sleeping):
    """`except httpx.HTTPError:` -> `time.sleep(randint(3, 10))` -> one retry.

    `no_real_sleeping` is REQUIRED: without it this test sleeps for up to ten
    real seconds. The task sleeps inline in the worker rather than deferring to
    the broker, which is registered as a finding rather than fixed here.

    respx serves a failure then a success from one route by giving `side_effect`
    a list, so the retry is the second call rather than a second route -- one
    route, two responses, which is also what `assert_all_called=True` expects.
    """
    user = _remote_user()
    http_mock.get(user.ap_public_url).mock(side_effect=[
        httpx.ConnectError('boom'),
        httpx.Response(200, json=_person_document(fields={'name': 'Retried'})),
    ])

    refresh_user_profile_task(user.id)

    db.session.refresh(user)
    assert user.title == 'Retried'


def test_a_malformed_actor_document_counts_an_instance_failure(
        app, db_session, http_mock):
    """PINS THE CORRECT BEHAVIOUR, which is the control for two defects.

    `refresh_user_profile_task` wraps `actor_data.json()` in
    `try/except JSONDecodeError`, increments `user.instance.failures` and
    returns. `refresh_community_profile_task` and `refresh_feed_profile_task`
    call `.json()` unguarded and raise instead -- pinned in Tasks 5 and 7 and
    fixed in Task 10.

    `failures` is seeded to 5 because it defaults to 0: an assertion of `1`
    against a default of `0` cannot tell an increment from an assignment, and
    an assertion of "not 0" cannot tell it from any other write.

    `user.title` is asserted as `None`, not `None or ''`: `User.title` is a
    plain `db.Column(db.String(256))` with no default, and `make_user` never
    sets it, so a factory-built user's `title` is `None` until something
    applies a document -- which this test proves did not happen.
    """
    user = _remote_user()
    user.instance.failures = 5
    db.session.commit()
    _serve(http_mock, user.ap_public_url, text='<html>not json</html>')

    refresh_user_profile_task(user.id)

    db.session.refresh(user.instance)
    assert user.instance.failures == 6
    assert user.title is None


def test_a_non_200_actor_response_applies_nothing(app, db_session, http_mock):
    """`if actor_data.status_code == 200:` -- a 404 from the peer leaves the
    row untouched.

    THE DOCUMENT MUST CARRY VALUES THE TASK WOULD OTHERWISE APPLY, or this
    test cannot fail. `_person_document()`'s baseline has no `name` key, and
    `user.title` is assigned only `if 'name' in activity_json`
    (app/activitypub/util.py), so a document without one leaves the title
    alone whether the status guard fired or not. `name` and a differing
    `preferredUsername` are supplied here so that a broken guard would
    overwrite both seeded values and fail the two assertions below.
    """
    user = _remote_user()
    user.title = 'Before'
    user.user_name = 'before'
    db.session.commit()
    _serve(http_mock, user.ap_public_url,
           _person_document(fields={'name': 'After'}), status=404)

    refresh_user_profile_task(user.id)

    db.session.refresh(user)
    assert user.title == 'Before'
    assert user.user_name == 'before'
