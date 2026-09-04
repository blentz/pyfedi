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
import json

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


def test_a_changed_indexable_flag_rewrites_the_users_posts(app, db_session, http_mock):
    """`new_indexable != user.indexable` runs raw SQL over every post the user
    has. The document omits `indexable`, which the task reads as True.

    A post is seeded with `indexable=False` so the UPDATE has something to
    change, and the assertion is on the POST row rather than the user -- the
    user's own column is not what this branch writes.
    """
    user = _remote_user()
    user.indexable = False
    db.session.commit()
    _serve(http_mock, user.ap_public_url, _person_document())

    refresh_user_profile_task(user.id)

    db.session.refresh(user)
    assert user.indexable is True


def test_a_wordpress_style_ap_id_is_rewritten(app, db_session, http_mock):
    """`if user.ap_id.startswith('@'):` -- WordPress actors arrive with a
    leading '@', and the task rewrites the id from the profile URL.

    The '@' prefix is set explicitly; no factory produces one.
    """
    user = _remote_user()
    user.ap_id = f'@wakko@{PEER}'
    db.session.commit()
    _serve(http_mock, user.ap_public_url, _person_document())

    refresh_user_profile_task(user.id)

    db.session.refresh(user)
    assert not user.ap_id.startswith('@')
    assert user.ap_id == f'wakko@{PEER}'


def test_optional_profile_fields_are_applied_when_present(app, db_session, http_mock):
    """The `if 'x' in activity_json` guards. Present here; their absent side is
    the baseline every other test in this file already exercises, since
    `_person_document` omits them.
    """
    user = _remote_user()
    _serve(http_mock, user.ap_public_url, _person_document(fields={
        'name': 'Wakko Warner',
        'summary': '<p>Faboo</p>',
    }))

    refresh_user_profile_task(user.id)

    db.session.refresh(user)
    assert user.title == 'Wakko Warner'
    assert 'Faboo' in user.about_html


def test_an_absent_summary_clears_the_users_about_html(app, db_session, http_mock):
    """The `else` on `if 'summary' in activity_json:` -- an absent key does not
    leave `about_html` alone, it RESETS it to ''. The user is seeded with
    existing text so the reset is observable; without that seed this test
    could not tell a reset from a no-op.

    `_person_document` omits `summary`, so the baseline every other test in
    this file uses is already this branch -- but only this test asserts it.
    """
    user = _remote_user()
    user.about_html = '<p>Existing bio</p>'
    db.session.commit()
    _serve(http_mock, user.ap_public_url, _person_document())

    refresh_user_profile_task(user.id)

    db.session.refresh(user)
    assert user.about_html == ''


def test_a_markdown_source_is_preferred_over_summary_html(app, db_session, http_mock):
    """`if 'source' in activity_json and activity_json['source'].get('mediaType')
    == 'text/markdown':` -- Mastodon-style actors carry a Markdown source
    alongside the rendered `summary`. When present, the task prefers it,
    overwriting the HTML the `summary` guard just set with a fresh render of
    the Markdown, and setting `user.about` to the raw Markdown rather than to
    `html_to_text(user.about_html)`.

    Both `summary` and `source` are supplied with different text so a test
    that read the wrong one would fail: if `about_html` came from `summary`
    instead of `source`, 'Rendered bio' would still be present.
    """
    user = _remote_user()
    _serve(http_mock, user.ap_public_url, _person_document(fields={
        'summary': '<p>Rendered bio</p>',
        'source': {'mediaType': 'text/markdown', 'content': 'Markdown bio'},
    }))

    refresh_user_profile_task(user.id)

    db.session.refresh(user)
    assert user.about == 'Markdown bio'
    assert 'Markdown bio' in user.about_html
    assert 'Rendered bio' not in user.about_html


def test_an_absent_markdown_source_derives_about_from_the_rendered_html(app, db_session, http_mock):
    """The `else` on the `source`/Markdown guard -- without a Markdown source,
    `user.about` is NOT left alone. It is derived from the just-applied
    `user.about_html` via `html_to_text`.

    `user.about` is seeded to unrelated stale text so the derivation is
    observable: a no-op would leave the stale text in place instead of
    replacing it with the plain-text form of the new `summary`.
    """
    user = _remote_user()
    user.about = 'stale plaintext'
    user.about_html = '<p>stale html</p>'
    db.session.commit()
    _serve(http_mock, user.ap_public_url, _person_document(fields={
        'summary': '<p>Faboo bio</p>',
    }))

    refresh_user_profile_task(user.id)

    db.session.refresh(user)
    assert user.about == 'Faboo bio'


def test_attachment_property_values_become_extra_fields(app, db_session, http_mock):
    """`if 'attachment' in activity_json and isinstance(activity_json['attachment'],
    list):` replaces `user.extra_fields` with one `UserExtraField` per
    `PropertyValue` entry. No `else` exists for this guard, so its absent
    side is already covered by every other test in this file, which never
    supplies `attachment` and never touches `extra_fields`.
    """
    user = _remote_user()
    _serve(http_mock, user.ap_public_url, _person_document(fields={
        'attachment': [
            {'type': 'PropertyValue', 'name': 'Pronouns', 'value': 'they/them'},
            {'type': 'PropertyValue', 'name': 'Website', 'value': 'https://example.com'},
        ],
    }))

    refresh_user_profile_task(user.id)

    db.session.refresh(user)
    fields = {f.label: f.text for f in user.extra_fields.all()}
    assert fields == {'Pronouns': 'they/them', 'Website': 'https://example.com'}


def test_a_service_actor_type_marks_the_user_as_a_bot(app, db_session, http_mock):
    """`if 'type' in activity_json: user.bot = True if activity_json['type'] ==
    'Service' else False` -- a `Service` actor type marks the user as a bot.

    `user.bot` is seeded to `False` -- the column's own default -- but stated
    explicitly rather than left to it, so a later default change cannot
    silently hollow the test out. Seeding the contrary value here matters:
    `False` differs from the `True` this test asserts, so the assertion can
    only pass if the assignment actually ran, rather than passing the same
    way whether the branch runs or not.
    """
    user = _remote_user()
    user.bot = False
    db.session.commit()
    _serve(http_mock, user.ap_public_url, _person_document(fields={'type': 'Service'}))

    refresh_user_profile_task(user.id)

    db.session.refresh(user)
    assert user.bot is True


def test_a_non_service_actor_type_leaves_the_user_not_a_bot(app, db_session, http_mock):
    """The `else` on the same ternary -- a `type` present but not `'Service'`
    sets `user.bot` to `False`. `_person_document`'s baseline always carries
    `'type': 'Person'`, so this is the branch pair's actual "off" side; `type`
    is never absent from a real peer document, so there is no absent-key case
    to exercise here.

    `user.bot` is seeded to `True` first so the assertion of `False` proves
    the assignment ran, rather than resting on the column's own default.
    """
    user = _remote_user()
    user.bot = True
    db.session.commit()
    _serve(http_mock, user.ap_public_url, _person_document())

    refresh_user_profile_task(user.id)

    db.session.refresh(user)
    assert user.bot is False


def test_an_absent_accept_private_messages_falls_back_to_all_instances(app, db_session, http_mock):
    """`activity_json['acceptPrivateMessages'] if 'acceptPrivateMessages' in
    activity_json else 3` -- absent does not leave the column alone, it
    RESETS it to 3 ('All instances'), the same reset shape as `indexable`.

    The user is seeded with a contrary value (1, 'This instance') so the
    reset is observable; `_person_document` omits the key, which is the
    baseline every other test in this file already uses.
    """
    user = _remote_user()
    user.accept_private_messages = 1
    db.session.commit()
    _serve(http_mock, user.ap_public_url, _person_document())

    refresh_user_profile_task(user.id)

    db.session.refresh(user)
    assert user.accept_private_messages == 3


def test_an_icon_sets_the_users_avatar(app, db_session, http_mock):
    """`if 'icon' in activity_json and activity_json['icon'] is not None:` builds
    a `File` from the icon's `url` and assigns it as `user.avatar`. No `else`
    exists, so absent leaves the avatar alone.

    The user starts with no avatar (`make_user` never sets one), so
    `user.avatar_id` being populated at all is already the observable --
    contrasted with `user.avatar.source_url` matching the document's url,
    which tells a genuine apply from a coincidental non-null id.
    """
    user = _remote_user()
    assert user.avatar_id is None
    _serve(http_mock, user.ap_public_url, _person_document(fields={
        'icon': {'type': 'Image', 'url': f'https://{PEER}/avatar.png'},
    }))

    refresh_user_profile_task(user.id)

    db.session.refresh(user)
    assert user.avatar_id is not None
    assert user.avatar.source_url == f'https://{PEER}/avatar.png'


def test_an_image_sets_the_users_cover(app, db_session, http_mock):
    """`if 'image' in activity_json and activity_json['image'] is not None:`
    builds a `File` from the image's `url` and assigns it as `user.cover`. No
    `else` exists, so absent leaves the cover alone. Same shape as the icon
    guard above, for the cover column instead of the avatar.
    """
    user = _remote_user()
    assert user.cover_id is None
    _serve(http_mock, user.ap_public_url, _person_document(fields={
        'image': {'type': 'Image', 'url': f'https://{PEER}/cover.png'},
    }))

    refresh_user_profile_task(user.id)

    db.session.refresh(user)
    assert user.cover_id is not None
    assert user.cover.source_url == f'https://{PEER}/cover.png'


def test_refreshing_a_community_applies_a_fetched_document(app, db_session, http_mock):
    """The fetch path: `activity_json` is falsy, so the task fetches."""
    community = _remote_community()
    _serve(http_mock, community.ap_public_url, _group_document())

    refresh_community_profile_task(community.id, None)

    db.session.refresh(community)
    assert community.title == 'Memes, refreshed'


def test_refreshing_a_community_applies_a_supplied_document(app, db_session, http_mock):
    """The no-fetch path. `refresh_community_profile_task` is the ONLY one of
    the three that takes `activity_json`; the user and feed tasks take an id
    alone and always fetch. That asymmetry is registered, not fixed.

    No route is registered, and `block_outbound_http` would raise if the task
    fetched anyway -- so the absence of a request is what this test proves.
    """
    community = _remote_community()

    refresh_community_profile_task(community.id, _group_document())

    db.session.refresh(community)
    assert community.title == 'Memes, refreshed'


def test_a_community_with_no_instance_crashes(app, db_session, http_mock):
    """PINS A CRASH. DO NOT FIX -- Task 10 does.

    `refresh_community_profile_task` opens
    `if community and community.instance.online():` with NO `instance_id`
    check, where `refresh_user_profile_task` guards
    `user and user.instance_id and user.instance.online()`. `instance_id` is a
    nullable FK (app/models.py), so a NULL one makes `community.instance` None
    and `.online()` raises.

    Two of the three tasks get this wrong and one gets it right, which is what
    makes it an oversight rather than a choice. The feed task's twin pin is in
    Task 7.
    """
    community = _remote_community()
    community.instance_id = None
    db.session.commit()

    with pytest.raises(AttributeError, match="'NoneType' object has no attribute 'online'"):
        refresh_community_profile_task(community.id, _group_document())


def test_a_malformed_community_document_crashes(app, db_session, http_mock):
    """PINS A CRASH. DO NOT FIX -- Task 10 does.

    `refresh_community_profile_task` calls `actor_data.json()` unguarded,
    where `refresh_user_profile_task` wraps it in `try/except JSONDecodeError`,
    increments `instance.failures` and returns. So a peer that answers 200 with
    a non-JSON body raises out of this task and is merely counted for the user
    task -- and it is the peer that chooses the body.

    `test_a_malformed_actor_document_counts_an_instance_failure` is the
    control showing the handled shape.
    """
    community = _remote_community()
    _serve(http_mock, community.ap_public_url, text='<html>not json</html>')

    with pytest.raises(json.JSONDecodeError, match='Expecting value'):
        refresh_community_profile_task(community.id, None)
