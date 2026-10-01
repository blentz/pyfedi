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
from app.models import (Community, CommunityMember, Feed, FeedItem, FeedMember, Instance,
                        Post, User, utcnow)
from tests.factories import (make_community, make_feed, make_feed_member, make_instance,
                             make_local_feed, make_post, make_user, seed_community_owner,
                             seed_signing_site)

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


def _following_community(name='memes'):
    """A community the feed task's following loop can resolve WITHOUT a fetch.

    The loop calls `find_actor_or_create(fci, community_only=True)`, which
    resolves on `Community.ap_profile_id` -- NOT `ap_public_url` and NOT
    `ap_id`. The chain is `find_actor_by_url` -> `find_remote_actor`
    (app/activitypub/actor.py), which lowercases the url first and routes it to
    the Community query on seeing `/c/` (and no `/p/`) in the path.
    `make_community(name, host=PEER)` produces exactly that shape.

    `ap_fetched_at` MUST be set. A resolved actor is handed straight to
    `schedule_actor_refresh`, which re-refreshes anything fetched over a day
    ago OR NEVER, and `make_community` leaves the column NULL. That refresh is
    an outbound request, so a community seeded without this line fails on
    `block_outbound_http` for a reason that has nothing to do with the loop.
    `test_a_feed_owners_url_is_fetched_and_recorded` sets it on its owner for
    the same reason.

    `seed_community_owner` is deliberately NOT called here: it is not
    idempotent (`Instance.domain` is unique) and every caller has already run
    it via `_remote_feed()`. `make_community`'s hardcoded `instance_id=1` /
    `user_id=1` are satisfied by that earlier call.
    """
    community = make_community(name, host=PEER)
    community.ap_fetched_at = utcnow()
    db.session.commit()
    return community


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
    `refresh_feed_profile_task` LACKED, which used to make them crash on the
    same row. Task 10 gave both the same guard;
    `test_a_community_with_no_instance_is_skipped` and
    `test_a_feed_with_no_instance_is_skipped` are its siblings' versions of
    this test.

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
    `AllMockedAssertionError` -- not an `httpx.HTTPError`, so the task's
    `except Exception:` (the one guarding the `signed_get_request` fallback, below the
    `except httpx.HTTPError: return`) catches it, `signed_get_request` fails
    too, the inner `except Exception:` around it catches that, and the task returns
    silently. Both are cited by content rather than by line number: line
    numbers in this file went stale once already when Task 10 inserted guards
    into the two sibling tasks.
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


def test_a_failed_fetch_is_not_retried(app, db_session, http_mock, no_real_sleeping):
    """D224, fixed (owner ruling): the task used to sleep `randint(3, 10)`
    seconds inline in the worker and fetch again. `get_request` already
    retries a read error itself, so the outer sleep-and-retry is gone and one
    `httpx.HTTPError` ends the refresh quietly.

    The observable is the transport count: `get_request` makes its own two
    attempts, so a dead peer is asked twice, not the four times the task's
    second `get_request` used to add. `no_real_sleeping` covers
    `get_request`'s own sleep.
    """
    user = _remote_user()
    user.title = 'Before'
    db.session.commit()
    route = http_mock.get(user.ap_public_url).mock(side_effect=httpx.ConnectError('boom'))

    refresh_user_profile_task(user.id)

    db.session.refresh(user)
    assert route.call_count == 2
    assert user.title == 'Before'


def test_a_malformed_actor_document_counts_an_instance_failure(
        app, db_session, http_mock):
    """PINS THE CORRECT BEHAVIOUR, which was the control for two defects.

    `refresh_user_profile_task` wraps `actor_data.json()` in
    `try/except JSONDecodeError`, increments `user.instance.failures` and
    returns. `refresh_community_profile_task` and `refresh_feed_profile_task`
    called `.json()` unguarded and raised instead; Task 10 gave both the same
    handler, so all three now behave alike -- see
    `test_a_malformed_community_document_counts_an_instance_failure` and
    `test_a_malformed_feed_document_counts_an_instance_failure`.

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
    """`if new_indexable != user.indexable:` runs
    `UPDATE "post" SET indexable = :indexable WHERE user_id = :user_id` over
    every post the user has. The document omits `indexable`, which the task
    reads as True, and the user is seeded False so the branch is entered.

    A POST IS SEEDED WITH `indexable=False` AND THE ASSERTION IS ON THE POST
    ROW. That is the only assertion that can kill this branch. `user.indexable
    = new_indexable` is assigned UNCONDITIONALLY further down the
    task, outside this `if`, so `user.indexable` reads True after the refresh
    whether the branch ran or not -- asserting it alone left the whole block
    deletable with the suite green, and the raw UPDATE running against zero
    rows meant nothing observed it either. The user column is asserted too,
    but only as a guard on the seeding: it says the document was applied at
    all, and it is the post row that says this branch was what applied it.

    `Post.indexable` defaults to True (`app/models.py:1731`), so the seeded
    post's False is set explicitly after `make_post` -- a post left at the
    default would read True after the refresh either way.

    `make_post` needs a community and an author. The author is the refreshed
    user itself, because the UPDATE filters on `user_id`; a post by anyone
    else would not be touched by a working branch.
    """
    user = _remote_user()
    user.indexable = False
    db.session.commit()
    community = make_community('memes', host=PEER)
    db.session.commit()
    post = make_post(community, user, f'https://{PEER}/p/indexed')
    post.indexable = False
    db.session.commit()
    _serve(http_mock, user.ap_public_url, _person_document())

    refresh_user_profile_task(user.id)

    db.session.refresh(user)
    db.session.refresh(post)
    assert post.indexable is True
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
    """The no-fetch path, which all three tasks now share (D223).

    No route is registered, and `block_outbound_http` would raise if the task
    fetched anyway -- so the absence of a request is what this test proves.
    """
    community = _remote_community()

    refresh_community_profile_task(community.id, _group_document())

    db.session.refresh(community)
    assert community.title == 'Memes, refreshed'


def test_refreshing_a_user_applies_a_supplied_document(app, db_session, http_mock):
    """D223, fixed (owner ruling): the user task takes the same optional
    pre-fetched `activity_json` the community task does, so a caller already
    holding the document does not pay for a second fetch that may return a
    different one. No route is registered: the absence of a request is the
    observable, as for the community test above.
    """
    user = _remote_user()

    refresh_user_profile_task(user.id, _person_document(fields={'name': 'Supplied'}))

    db.session.refresh(user)
    assert user.title == 'Supplied'


def test_refreshing_a_feed_applies_a_supplied_document(app, db_session, http_mock):
    """D223, fixed (owner ruling): the feed task's twin of the user test above."""
    feed = _remote_feed()

    refresh_feed_profile_task(feed.id, _feed_document())

    db.session.refresh(feed)
    assert feed.title == 'News, refreshed'


def test_a_community_with_no_instance_is_skipped(app, db_session, http_mock):
    """`refresh_community_profile_task` now guards `community.instance_id`
    before dereferencing `community.instance`, in the position
    `refresh_user_profile_task` has always had it:
    `user and user.instance_id and user.instance.online()`.

    `instance_id` is a nullable FK (app/models.py), so a NULL one makes
    `community.instance` None. Until this guard was added,
    `community.instance.online()` raised
    `AttributeError: 'NoneType' object has no attribute 'online'`; the
    community is now left untouched instead.

    THE SEEDED TITLE IS THE OBSERVABLE, NOT "nothing raised". A bare
    "the call returned" cannot tell a guard that fired for the right reason
    from one that abandoned the document for the wrong one. The supplied
    `_group_document()` carries `name: 'Memes, refreshed'`, which the task
    would write over `'Before'` if it ran, so the assertion below fails if
    the guard stops firing AND the task somehow survives the NULL.
    """
    community = _remote_community()
    community.instance_id = None
    community.title = 'Before'
    db.session.commit()

    refresh_community_profile_task(community.id, _group_document())

    db.session.refresh(community)
    assert community.title == 'Before'


def test_a_malformed_community_document_counts_an_instance_failure(
        app, db_session, http_mock):
    """`refresh_community_profile_task` now wraps `actor_data.json()` in
    `try/except JSONDecodeError`, increments `instance.failures` and returns,
    exactly as `refresh_user_profile_task` always has. Until it did, a peer
    that answered 200 with a non-JSON body raised `json.JSONDecodeError` out
    of this task -- and it is the peer that chooses the body.

    `test_a_malformed_actor_document_counts_an_instance_failure` is the
    control showing the same shape for the user task.

    `failures` is seeded to 5 for the reason given there: it defaults to 0, so
    an assertion of `1` against a default of `0` cannot tell an increment from
    an assignment. The seeded title is asserted alongside it so that a fix
    which counted the failure but then applied a half-decoded document would
    still fail.
    """
    community = _remote_community()
    community.instance.failures = 5
    community.title = 'Before'
    db.session.commit()
    _serve(http_mock, community.ap_public_url, text='<html>not json</html>')

    refresh_community_profile_task(community.id, None)

    db.session.refresh(community)
    db.session.refresh(community.instance)
    assert community.instance.failures == 6
    assert community.title == 'Before'


def test_the_moderators_url_is_taken_from_attributed_to(app, db_session, http_mock):
    """`if 'attributedTo' in activity_json and isinstance(..., str):` -- the
    lemmy and mbin spelling.

    DEVIATION FROM THE BRIEF: the brief's assertion (`community.title ==
    'Memes, refreshed'`) only proves the task completed, not that `mods_url`
    was used for anything. Reading the task shows it does more with
    `ap_moderators_url`: after the community fields are committed, it fetches
    that URL and, for every actor in the `OrderedCollection`'s
    `orderedItems`, calls `find_actor_or_create` and upserts a
    `CommunityMember` row with `is_moderator=True` (app/activitypub/util.py).
    So this test serves that collection with one member and asserts the
    resulting `CommunityMember` row instead of the title.

    The member is a pre-existing remote user with `ap_fetched_at` set fresh:
    `find_actor_or_create` then resolves it via `find_remote_actor` (a DB
    lookup by `ap_profile_id`) rather than creating it, and
    `schedule_actor_refresh` sees a recently-fetched actor and does not
    itself queue a nested refresh -- which runs eagerly under this suite's
    Celery config and would otherwise need its own HTTP mock for the
    member's own actor document.
    """
    community = _remote_community()
    mod = make_user(community.instance, 'fauxmod')
    mod.ap_fetched_at = utcnow()
    db.session.commit()
    mods_url = f'https://{PEER}/c/memes/moderators'
    _serve(http_mock, mods_url,
           {'type': 'OrderedCollection', 'orderedItems': [mod.ap_profile_id]})

    refresh_community_profile_task(
        community.id, _group_document(fields={'attributedTo': mods_url}))

    membership = db.session.query(CommunityMember).filter_by(
        community_id=community.id, user_id=mod.id).first()
    assert membership is not None
    assert membership.is_moderator is True


def test_a_malformed_moderator_entry_is_skipped(app, db_session, http_mock):
    """D219, fixed (owner ruling): a moderators entry that is an object with
    no `id`, or neither a string nor an object, used to raise -- `KeyError`
    in `find_actor_or_create` or the removal loop, `AttributeError` on
    `.strip()`/`.lower()` -- and abort the whole refresh. It is now skipped:
    the well-formed entry after it is still a moderator, and the removal pass
    does not drop them for the bad entries beside them.
    """
    community = _remote_community()
    mod = make_user(community.instance, 'fauxmod')
    mod.ap_fetched_at = utcnow()
    db.session.add(CommunityMember(community_id=community.id, user_id=mod.id, is_moderator=True))
    db.session.commit()
    mods_url = f'https://{PEER}/c/memes/moderators'
    _serve(http_mock, mods_url, {'type': 'OrderedCollection',
                                 'orderedItems': [{'type': 'Person'}, 42, mod.ap_profile_id]})

    refresh_community_profile_task(
        community.id, _group_document(fields={'attributedTo': mods_url}))

    db.session.refresh(community)
    membership = db.session.query(CommunityMember).filter_by(
        community_id=community.id, user_id=mod.id).one()
    assert membership.is_moderator is True
    assert community.title == 'Memes, refreshed'


def test_a_typeless_moderators_document_is_skipped(app, db_session, http_mock):
    """`'type' in mods_data`, checked before `mods_data['type']` is read --
    the same missing conjunct as the followers guard, in the same function.

    THE ORDER IS MODERATORS, THEN FOLLOWERS, THEN FEATURED, and this is the
    FIRST of the three: the moderators guard is `app/activitypub/util.py:947`,
    the followers guard `:984`, and the correct featured guard `:993`, which
    sits BELOW both rather than between them. `test_a_typeless_followers_
    document_is_skipped` states the same relationship from the middle guard's
    side ("the featured guard nine lines below this one", 993 - 984 = 9) and
    is the model this docstring now follows.

    THE DOCUMENT DIFFERS FROM `test_the_moderators_url_is_taken_from_attributed_to`'s
    IN EXACTLY ONE KEY: `type` is absent and the same usable `orderedItems`
    list remains. Well-formed JSON at 200, so the status check and decode both
    pass and only this conjunct can stop it.

    The observable is that NO `CommunityMember` row exists. That is what
    separates this from a vacuous "nothing raised": with the guard removed the
    identical document creates a moderator membership, exactly as the
    happy-path test above proves it does when `type` is present. `mod` is
    seeded and resolvable for that reason -- an unresolvable entry would make
    the count zero either way.

    The title is asserted too: this guard must skip only the collection.
    """
    community = _remote_community()
    mod = make_user(community.instance, 'fauxmod')
    mod.ap_fetched_at = utcnow()
    community.title = 'Before'
    db.session.commit()
    mods_url = f'https://{PEER}/c/memes/moderators'
    _serve(http_mock, mods_url, {'orderedItems': [mod.ap_profile_id]})

    refresh_community_profile_task(
        community.id, _group_document(fields={'attributedTo': mods_url}))

    db.session.refresh(community)
    assert community.title == 'Memes, refreshed'
    assert db.session.query(CommunityMember).filter_by(
        community_id=community.id).count() == 0


def test_the_moderators_url_falls_back_to_the_kbin_spelling(app, db_session, http_mock):
    """`elif 'moderators' in activity_json:` -- kbin's spelling. Reached only
    when `attributedTo` is absent or not a string, so the document carries
    `moderators` alone.

    Same deviation and reasoning as
    `test_the_moderators_url_is_taken_from_attributed_to` above: the
    `CommunityMember` row is the observable, not the title.
    """
    community = _remote_community()
    mod = make_user(community.instance, 'otherfauxmod')
    mod.ap_fetched_at = utcnow()
    db.session.commit()
    mods_url = f'https://{PEER}/c/memes/moderators'
    _serve(http_mock, mods_url,
           {'type': 'OrderedCollection', 'orderedItems': [mod.ap_profile_id]})

    refresh_community_profile_task(
        community.id, _group_document(fields={'moderators': mods_url}))

    membership = db.session.query(CommunityMember).filter_by(
        community_id=community.id, user_id=mod.id).first()
    assert membership is not None
    assert membership.is_moderator is True


def test_a_moderators_object_is_not_stored_as_the_moderators_url(app, db_session, http_mock):
    """D233, fixed. The `attributedTo` arm checked `isinstance(..., str)` and
    the kbin `moderators` arm below it did not, so a peer sending a collection
    OBJECT there put a dict into the String column and the commit failed,
    losing the whole refresh. A non-string now takes the `else` arm."""
    community = _remote_community()

    refresh_community_profile_task(community.id, _group_document(
        fields={'moderators': {'type': 'OrderedCollection', 'id': f'https://{PEER}/c/memes/moderators'}}))

    db.session.refresh(community)
    assert community.title == 'Memes, refreshed'
    assert community.ap_moderators_url is None


def test_the_sensitive_flag_sets_nsfw(app, db_session, http_mock):
    """`community.nsfw = activity_json['sensitive'] if 'sensitive' in ... else False`
    -- note the else, which means an absent key RESETS nsfw rather than
    leaving it. The community is seeded nsfw=True so the reset is observable,
    and its twin below asserts the set.
    """
    community = _remote_community()
    community.nsfw = True
    db.session.commit()

    refresh_community_profile_task(community.id, _group_document())

    db.session.refresh(community)
    assert community.nsfw is False


def test_a_sensitive_document_sets_nsfw(app, db_session, http_mock):
    """The truthy side of the same expression."""
    community = _remote_community()
    community.nsfw = False
    db.session.commit()

    refresh_community_profile_task(
        community.id, _group_document(fields={'sensitive': True}))

    db.session.refresh(community)
    assert community.nsfw is True


def test_a_followers_url_is_fetched_and_counted(app, db_session, http_mock):
    """The followers collection is fetched only when `ap_followers_url` is set.
    `_remote_community` clears it, so this test sets it back -- which is the
    contrast that makes the guard killable.

    DEVIATION FROM THE BRIEF: the brief names the target column
    `Community.subscriptions_count` (app/models.py:568, "Local subscribers").
    Reading `refresh_community_profile_task` (app/activitypub/util.py) shows
    it writes `community.total_subscriptions_count` (app/models.py:569,
    "Local AND remote") with the fetched `totalItems` instead --
    `subscriptions_count` is never touched by this fetch. This test asserts
    the column the task actually writes.
    """
    community = _remote_community()
    followers_url = f'https://{PEER}/c/memes/followers'
    community.ap_followers_url = followers_url
    db.session.commit()
    _serve(http_mock, followers_url,
           {'type': 'Collection', 'totalItems': 42})

    refresh_community_profile_task(community.id, _group_document())

    db.session.refresh(community)
    assert community.total_subscriptions_count == 42


def test_a_typeless_followers_document_is_skipped(app, db_session, http_mock):
    """`'type' in followers_data`, checked before `followers_data['type']` is
    read. The featured guard nine lines below this one has always checked
    membership first; this guard did not, so a peer answering with a JSON
    object carrying no `type` raised `KeyError: 'type'` out of the task.

    THE DOCUMENT DIFFERS FROM THE HAPPY PATH IN EXACTLY ONE KEY. It is
    well-formed JSON at status 200 carrying a usable `totalItems`, so the
    status check and the decode above it both pass and only this guard can
    stop it. A malformed or non-200 body would die at one of those instead and
    prove nothing about this conjunct -- the same trap the following-collection
    pins had to avoid.

    `total_subscriptions_count` is SEEDED TO 7, a non-default value (the column
    default is 0), and asserted unchanged: `totalItems` is 42 in the served
    document, so a guard that stopped firing would write 42 over it. The title
    is asserted alongside because this guard must skip the collection only --
    the actor document was applied before it and must stay applied.
    """
    community = _remote_community()
    followers_url = f'https://{PEER}/c/memes/followers'
    community.ap_followers_url = followers_url
    community.total_subscriptions_count = 7
    community.title = 'Before'
    db.session.commit()
    _serve(http_mock, followers_url, {'totalItems': 42})

    refresh_community_profile_task(community.id, _group_document())

    db.session.refresh(community)
    assert community.title == 'Memes, refreshed'
    assert community.total_subscriptions_count == 7


def test_no_followers_url_means_no_followers_fetch(app, db_session, http_mock):
    """The absent side. No route is registered for the followers collection,
    and `block_outbound_http` raises if the task fetches one -- so this test
    proves the guard short-circuits rather than merely that a count stayed
    put. `total_subscriptions_count` is seeded to a non-default value (the
    column default is 0, app/models.py:569) so "unchanged" is distinguishable
    from "never set".
    """
    community = _remote_community()
    community.total_subscriptions_count = 7
    db.session.commit()

    refresh_community_profile_task(community.id, _group_document())

    db.session.refresh(community)
    assert community.total_subscriptions_count == 7


def test_a_featured_url_is_fetched_and_restickied(app, db_session, http_mock):
    """The featured collection is fetched only when `ap_featured_url` is set,
    exactly like the followers gate above. This test also carries the whole
    item walk in one pass: a stale sticky post that the collection does NOT
    name is un-stickied by the `UPDATE post SET sticky = false` line, a
    non-sticky post the collection DOES name is stickied, and a third
    collection entry naming no post PyFedi has ever heard of takes the
    `if post:` False branch without raising -- covering both arms of the
    walk in one test.

    `ap_featured_url` is seeded directly on the community rather than via the
    document's `featured` key -- `_group_document()` carries no such key by
    default (verified by reading it), and direct seeding mirrors the shape
    `test_a_followers_url_is_fetched_and_counted` already uses for
    `ap_followers_url`. Either approach reaches the same gate; this one
    keeps the document identical to the rest of this file's happy-path calls.

    `stale_sticky.sticky is False` is the load-bearing assertion: it is the
    only thing that distinguishes "the UPDATE actually ran" from "nothing
    happened, and the collection's own resticky merely left `newly_featured`
    alone."
    """
    community = _remote_community()
    featured_url = f'https://{PEER}/c/memes/featured'
    community.ap_featured_url = featured_url
    db.session.commit()
    poster = make_user(community.instance, 'poster')
    db.session.commit()
    stale_sticky = make_post(community, poster, f'https://{PEER}/p/stale')
    stale_sticky.sticky = True
    newly_featured = make_post(community, poster, f'https://{PEER}/p/new')
    newly_featured.sticky = False
    db.session.commit()
    _serve(http_mock, featured_url, {
        'type': 'OrderedCollection',
        'orderedItems': [
            {'id': newly_featured.ap_id},
            {'id': f'https://{PEER}/p/unknown'},
        ],
    })

    refresh_community_profile_task(community.id, _group_document())

    db.session.refresh(stale_sticky)
    db.session.refresh(newly_featured)
    assert stale_sticky.sticky is False
    assert newly_featured.sticky is True


def test_a_malformed_featured_item_is_skipped_and_the_rest_restickied(
        app, db_session, http_mock):
    """D219, fixed (owner ruling): `item['id']` on an entry with no id -- or a
    bare string, or a non-string id -- used to raise out of the walk AFTER the
    `UPDATE post SET sticky = false` had committed, so one bad entry left the
    community with no stickies at all. A malformed entry is now skipped and
    the entries after it are still restickied.
    """
    community = _remote_community()
    featured_url = f'https://{PEER}/c/memes/featured'
    community.ap_featured_url = featured_url
    db.session.commit()
    poster = make_user(community.instance, 'poster')
    db.session.commit()
    featured = make_post(community, poster, f'https://{PEER}/p/new')
    featured.sticky = True
    db.session.commit()
    _serve(http_mock, featured_url, {
        'type': 'OrderedCollection',
        'orderedItems': [{'type': 'Page'}, f'https://{PEER}/p/bare', {'id': 42},
                         {'id': featured.ap_id}],
    })

    refresh_community_profile_task(community.id, _group_document())

    db.session.refresh(featured)
    assert featured.sticky is True


def test_no_featured_url_means_no_featured_fetch(app, db_session, http_mock):
    """The absent side. No route is registered for the featured collection,
    and `block_outbound_http` raises if the task fetches one -- so this test
    proves the guard short-circuits. A pre-existing sticky post is seeded
    first so "still sticky" is distinguishable from "no post was ever
    stickied to begin with."
    """
    community = _remote_community()
    poster = make_user(community.instance, 'poster')
    db.session.commit()
    post = make_post(community, poster, f'https://{PEER}/p/stale')
    post.sticky = True
    db.session.commit()

    refresh_community_profile_task(community.id, _group_document())

    db.session.refresh(post)
    assert post.sticky is True


def test_a_non_200_featured_response_does_nothing(app, db_session, http_mock):
    """`if featured_request.status_code == 200:` -- the second gate, after the
    URL check. A non-200 response leaves the sticky post untouched, the same
    contrast as the URL-absent test above, and the route IS registered and
    IS called (satisfying `assert_all_called=True`) -- it is the status code,
    not the absence of a request, that stops the task here.
    """
    community = _remote_community()
    featured_url = f'https://{PEER}/c/memes/featured'
    community.ap_featured_url = featured_url
    db.session.commit()
    poster = make_user(community.instance, 'poster')
    db.session.commit()
    post = make_post(community, poster, f'https://{PEER}/p/stale')
    post.sticky = True
    db.session.commit()
    _serve(http_mock, featured_url,
           {'type': 'OrderedCollection', 'orderedItems': []}, status=500)

    refresh_community_profile_task(community.id, _group_document())

    db.session.refresh(post)
    assert post.sticky is True


def test_a_null_featured_document_does_nothing(app, db_session, http_mock):
    """The first conjunct of `if featured_data and 'type' in featured_data and
    featured_data['type'] == 'OrderedCollection' and 'orderedItems' in
    featured_data:` -- `featured_data` itself must be truthy. A body of JSON
    `null` decodes to `None`, which is falsy, so the guard short-circuits
    here without evaluating any of the three checks after it, and without
    raising. That silence is what sets this conjunct's kill apart from the
    two by-crash ones below it in a different way than the third: removing
    THIS conjunct lets `'type' in None` execute next, which raises
    `TypeError` (a `None` is not a subscriptable/iterable container) instead
    of returning quietly -- so this test kills its mutant by crash, same
    mechanism as the second and fourth conjuncts, not by an assertion flip.
    """
    community = _remote_community()
    featured_url = f'https://{PEER}/c/memes/featured'
    community.ap_featured_url = featured_url
    db.session.commit()
    poster = make_user(community.instance, 'poster')
    db.session.commit()
    post = make_post(community, poster, f'https://{PEER}/p/stale')
    post.sticky = True
    db.session.commit()
    _serve(http_mock, featured_url, text='null')

    refresh_community_profile_task(community.id, _group_document())

    db.session.refresh(post)
    assert post.sticky is True


def test_a_typeless_featured_document_does_nothing(app, db_session, http_mock):
    """The second conjunct: `'type' in featured_data`. The document is a
    non-empty, truthy dict (satisfying the first conjunct) that carries no
    `type` key at all, so this is the only conjunct the short-circuit stops
    at. Removing it lets the next conjunct, `featured_data['type']`, run
    straight into a `KeyError('type')` -- this test kills that mutant by
    crash, not by a changed assertion.
    """
    community = _remote_community()
    featured_url = f'https://{PEER}/c/memes/featured'
    community.ap_featured_url = featured_url
    db.session.commit()
    poster = make_user(community.instance, 'poster')
    db.session.commit()
    post = make_post(community, poster, f'https://{PEER}/p/stale')
    post.sticky = True
    db.session.commit()
    _serve(http_mock, featured_url, {'orderedItems': []})

    refresh_community_profile_task(community.id, _group_document())

    db.session.refresh(post)
    assert post.sticky is True


def test_a_wrong_typed_featured_document_does_nothing(app, db_session, http_mock):
    """The third conjunct: `featured_data['type'] == 'OrderedCollection'`.
    Both `type` and `orderedItems` are present here, so `type` holding
    anything other than `'OrderedCollection'` is what the short-circuit
    stops on, and only that. This is the one conjunct of the four whose
    removal does NOT crash: drop it and the guard reads `featured_data and
    'type' in featured_data and 'orderedItems' in featured_data`, which this
    document satisfies, so the mutant falls through into the UPDATE and
    re-stickies from `orderedItems` (empty here, so the seeded post simply
    goes non-sticky and stays that way). That flip -- not an exception --
    is what kills this mutant, the "drops through and restickies" case
    the other three conjuncts don't share.
    """
    community = _remote_community()
    featured_url = f'https://{PEER}/c/memes/featured'
    community.ap_featured_url = featured_url
    db.session.commit()
    poster = make_user(community.instance, 'poster')
    db.session.commit()
    post = make_post(community, poster, f'https://{PEER}/p/stale')
    post.sticky = True
    db.session.commit()
    _serve(http_mock, featured_url,
           {'type': 'Collection', 'orderedItems': []})

    refresh_community_profile_task(community.id, _group_document())

    db.session.refresh(post)
    assert post.sticky is True


def test_an_itemless_featured_document_does_nothing(app, db_session, http_mock):
    """The fourth conjunct: `'orderedItems' in featured_data`. `type` is
    correctly `'OrderedCollection'` here, so the missing `orderedItems` key
    is the only false conjunct the short-circuit stops at. Removing it lets
    the guard pass with no `orderedItems` key present at all: the mutant
    runs the UPDATE (un-stickying the seeded post) and then crashes on
    `featured_data['orderedItems']` in the `for` loop -- killed by crash,
    the same mechanism as the first and second conjuncts.
    """
    community = _remote_community()
    featured_url = f'https://{PEER}/c/memes/featured'
    community.ap_featured_url = featured_url
    db.session.commit()
    poster = make_user(community.instance, 'poster')
    db.session.commit()
    post = make_post(community, poster, f'https://{PEER}/p/stale')
    post.sticky = True
    db.session.commit()
    _serve(http_mock, featured_url, {'type': 'OrderedCollection'})

    refresh_community_profile_task(community.id, _group_document())

    db.session.refresh(post)
    assert post.sticky is True


def test_refreshing_a_feed_applies_the_peers_document(app, db_session, http_mock):
    """`refresh_feed_profile_task` always fetches -- it takes a feed id alone,
    with no `activity_json` parameter, unlike the community task.

    After applying the actor document the task fetches the feed's `/following`
    collection, now gated on `feed.ap_following_url` being set (Task 10; it
    was ungated, and `_remote_feed()` leaves the column at its default of
    `None`, so this test used to crash on `get_request(None)` before reaching
    its own assertion).

    `ap_following_url` IS SET AND MOCKED DELIBERATELY, not as leftover
    workaround: the happy path is a feed whose peer sent a `following` key, so
    driving the gate's PRESENT side is what makes this the happy path rather
    than a second copy of `test_a_feed_with_no_following_url_is_skipped`,
    which drives the absent side. The collection is served empty so the
    feed-item loop stays out of a test about applying the actor document; that
    loop is covered by `test_a_following_collection_entry_becomes_a_feed_item`
    and `test_a_following_entry_that_resolves_to_nothing_is_skipped`.
    """
    feed = _remote_feed()
    feed.ap_following_url = f'https://{PEER}/f/news/following'
    db.session.commit()
    _serve(http_mock, feed.ap_public_url, _feed_document())
    _serve(http_mock, feed.ap_following_url, {'type': 'Collection', 'items': []})

    refresh_feed_profile_task(feed.id)

    db.session.refresh(feed)
    assert feed.title == 'News, refreshed'


def test_a_feed_moderators_object_is_not_stored_as_the_moderators_url(app, db_session, http_mock):
    """D233, fixed, the feed copy: a `moderators` object used to go into
    `Feed.ap_moderators_url`, a String column, and fail the commit."""
    feed = _remote_feed()
    _serve(http_mock, feed.ap_public_url, _feed_document(
        fields={'moderators': {'type': 'OrderedCollection', 'id': f'https://{PEER}/f/news/moderators'}}))

    refresh_feed_profile_task(feed.id)

    db.session.refresh(feed)
    assert feed.title == 'News, refreshed'
    assert feed.ap_moderators_url is None


def test_refreshing_a_local_feed_does_nothing(
        app, db_session, no_real_sleeping, monkeypatch):
    """`not feed.is_local()` -- the third conjunct, which NEITHER sibling has.

    THE OBSERVABLE IS A SPY ON `get_request`, NOT `block_outbound_http`, and
    the difference is the whole point of this test. `make_local_feed` builds a
    feed whose host is `test.piefed.local`, and `is_invalid_get_request_uri`
    (app/utils.py) rejects any host ending in `.local` -- so `get_request`
    raises `httpx.HTTPError` from its own first line, BEFORE httpx is
    reached and therefore before respx or `block_outbound_http` can see
    anything. Drop `and not feed.is_local()` and the task takes the
    `except httpx.HTTPError:` path and returns quietly. Outbound-HTTP blocking
    cannot distinguish the guard firing from the guard gone, which is exactly
    how this test previously passed with no assertions at all while its
    conjunct survived deletion. Spying on `ap_util.get_request` sees the
    attempt regardless of whether it ever becomes a request -- the same
    pattern the three user-guard tests above use, and for the same class of
    reason.

    The spy returns a real `httpx.Response` so a dropped conjunct runs the
    task to completion and the kill lands on the assertions here rather than
    on an incidental crash. `_feed_document()` carries a `name` and a
    `publicKey` and no `attributedTo`/`moderators`, and `make_local_feed`
    leaves `ap_following_url` NULL, so the mutant fetches the actor document
    once, applies it, and stops -- overwriting the seeded title and the NULL
    public key, which the two column assertions below catch.

    `no_real_sleeping` is belt-and-braces: it keeps any sleep a regression
    reintroduces fast instead of slow.

    `make_local_feed` gives a local feed directly; using it rather than
    mutating a remote one keeps the fixture honest about what it represents.
    It leaves `ap_id` NULL, which satisfies `Feed.is_local()`'s first
    disjunct (`app/models.py:4237-4238`).
    """
    seed_community_owner(PEER)
    feed = make_local_feed('localnews')
    feed.title = 'Before'
    db.session.commit()
    calls = []

    def _spy(*a, **kw):
        calls.append(a)
        return httpx.Response(200, json=_feed_document())

    monkeypatch.setattr(ap_util, 'get_request', _spy)

    refresh_feed_profile_task(feed.id)

    db.session.refresh(feed)
    assert calls == []
    assert feed.title == 'Before'
    assert feed.public_key is None
    assert feed.ap_fetched_at is None


def test_a_feed_with_no_instance_is_skipped(app, db_session, http_mock):
    """`refresh_feed_profile_task` now guards `feed.instance_id` before
    dereferencing `feed.instance`, the same fix as the community task's and
    in the same position the user task has always had it. Until it was added,
    `feed.instance.online()` on a NULL FK raised
    `AttributeError: 'NoneType' object has no attribute 'online'`.

    THE SEEDED TITLE IS THE OBSERVABLE, NOT "nothing raised": the feed task
    always fetches, and `_feed_document()` would set the title to
    `'News, refreshed'`, so `'Before'` surviving proves the task stopped at
    the guard rather than merely finishing.
    """
    feed = _remote_feed()
    feed.instance_id = None
    feed.title = 'Before'
    db.session.commit()

    refresh_feed_profile_task(feed.id)

    db.session.refresh(feed)
    assert feed.title == 'Before'


def test_a_malformed_feed_document_counts_an_instance_failure(
        app, db_session, http_mock):
    """`actor_data.json()` is now guarded in `refresh_feed_profile_task` too,
    with the same `try/except JSONDecodeError` -> count -> return that the
    user task has and the community task gained alongside this one. Until it
    was, a non-JSON 200 raised `json.JSONDecodeError` out of the task.

    `failures` is seeded to 5 so the assertion distinguishes an increment from
    an assignment, and the seeded title is asserted so a fix that counted the
    failure but carried on refreshing would still fail.
    """
    feed = _remote_feed()
    feed.instance.failures = 5
    feed.title = 'Before'
    db.session.commit()
    _serve(http_mock, feed.ap_public_url, text='<html>not json</html>')

    refresh_feed_profile_task(feed.id)

    db.session.refresh(feed)
    db.session.refresh(feed.instance)
    assert feed.instance.failures == 6
    assert feed.title == 'Before'


def test_a_feed_with_no_following_url_is_skipped(app, db_session, http_mock):
    """`refresh_feed_profile_task`'s fetch of `feed.ap_following_url` is now
    gated on the column being set -- `if feed.ap_following_url:` -- which is
    the shape every other collection fetch in the trio already had:
    `community.ap_moderators_url`, `community.ap_followers_url`,
    `community.ap_featured_url` and this task's own `feed.ap_moderators_url`.
    It was the one of the five with no gate.

    `Feed.ap_following_url` has no default, so a feed that arrives without a
    `following` key -- which `make_feed` reproduces -- used to reach
    `get_request(None)` and raise
    `httpx.HTTPError: invalid uri` (via `is_invalid_get_request_uri`). The
    peer chooses whether to send that key, so this was remotely triggerable.

    THE ASSERTIONS ARE ON THE FEED'S OWN COLUMNS, NOT "nothing raised",
    because this fix must skip only the FETCH and not the refresh. A gate that
    returned early, or one placed above the document application, would also
    raise nothing -- and would silently stop remote feeds refreshing at all.
    The seeded `'Before'` title must therefore have been REPLACED by the
    document's, not preserved.

    No route is registered for a following collection, so `block_outbound_http`
    raises if the task fetches one anyway.
    """
    feed = _remote_feed()
    feed.ap_following_url = None
    feed.title = 'Before'
    db.session.commit()
    _serve(http_mock, feed.ap_public_url, _feed_document())

    refresh_feed_profile_task(feed.id)

    db.session.refresh(feed)
    assert feed.title == 'News, refreshed'
    assert feed.public_key == '-----BEGIN PUBLIC KEY-----refreshed'


def test_a_non_200_following_response_creates_no_feed_items(app, db_session, http_mock):
    """`if res.status_code == 200:` around the following-collection body --
    the check all five sibling collection fetches make and this one did not.
    A peer answering 502 at its `/following` used to have its body decoded and
    acted on regardless.

    THE 502 BODY IS A VALID, ACTIONABLE COLLECTION, and that is the whole
    design of this test. Serving garbage at 502 would make dropping the status
    check kill this test by `JSONDecodeError` -- the same way dropping the
    DECODE guard kills
    `test_a_malformed_following_collection_creates_no_feed_items` -- and the
    two guards would then be indistinguishable under mutation. With a decodable
    body naming a resolvable community, dropping the status check instead
    CREATES a `FeedItem`, and the count assertion below flips. Two guards, two
    different kills.

    502 rather than 404 because a peer's collection endpoint failing is the
    realistic shape; nothing in the task branches on which non-200 it is.

    The title is asserted alongside the count because this guard must skip only
    the collection: the actor document was fetched and applied before it, and a
    fix that abandoned the refresh would also create no feed items.
    """
    feed = _remote_feed()
    feed.ap_following_url = f'https://{PEER}/f/news/following'
    feed.title = 'Before'
    db.session.commit()
    community = _following_community()
    _serve(http_mock, feed.ap_public_url, _feed_document())
    _serve(http_mock, feed.ap_following_url,
           {'type': 'Collection', 'items': [community.ap_profile_id]}, status=502)

    refresh_feed_profile_task(feed.id)

    db.session.refresh(feed)
    assert feed.title == 'News, refreshed'
    assert db.session.query(FeedItem).filter_by(feed_id=feed.id).count() == 0


def test_a_malformed_following_collection_creates_no_feed_items(
        app, db_session, http_mock):
    """`try/except JSONDecodeError` around the following collection's
    `.json()`. A peer answering 200 with a non-JSON body used to raise
    `json.JSONDecodeError` straight out of the task, past
    `except Exception: session.rollback(); raise` -- and past a refresh that
    had already been committed.

    THE PLAIN-RETURN SPELLING IS DELIBERATE, not the `instance.failures += 1`
    of Fix B. `Instance.failures` tracks whether an instance is answering at
    all, and by the time this fetch runs the SAME instance has already served
    a well-formed actor document, been decoded and been applied. Counting a
    failure here would report an instance as unhealthy on the evidence of one
    malformed sub-collection. `verify_object_from_source`
    (app/activitypub/util.py, `def` at `:4458`) holds the file's existing
    plain-return decode guards -- two of them, one on the unsigned object
    fetch and one on the signed retry, each `except JSONDecodeError:
    object_request.close(); return ...` with no `failures` increment -- and
    that is the shape matched. NOT `resolve_remote_post`, which an earlier
    revision of this docstring named: that function contains no
    `JSONDecodeError` handler at all, and the register's D216 was corrected
    for the same misattribution.

    `failures` is therefore SEEDED TO 5 AND ASSERTED STILL 5. That is the
    assertion that pins the choice: it fails if anyone converts this guard to
    the counting spelling, and it is not the vacuous `== 0` that the column
    default would give.
    """
    feed = _remote_feed()
    feed.ap_following_url = f'https://{PEER}/f/news/following'
    feed.title = 'Before'
    feed.instance.failures = 5
    db.session.commit()
    _serve(http_mock, feed.ap_public_url, _feed_document())
    _serve(http_mock, feed.ap_following_url, text='<html>not json</html>')

    refresh_feed_profile_task(feed.id)

    db.session.refresh(feed)
    db.session.refresh(feed.instance)
    assert feed.title == 'News, refreshed'
    assert db.session.query(FeedItem).filter_by(feed_id=feed.id).count() == 0
    assert feed.instance.failures == 5


def test_a_feed_owners_url_is_fetched_and_recorded(app, db_session, http_mock):
    """The feed task's equivalent of the community followers pair above, for
    the first of the feed task's TWO gated collections: `feed.ap_moderators_url`
    (`app/activitypub/util.py:1115`) guards a fetch of the feed's owners,
    exactly as `community.ap_moderators_url` guards the community's moderators
    fetch. `feed.ap_following_url` (`:1155`) is the other, gated by this
    slice's own Fix C and pinned by
    `test_a_feed_with_no_following_url_is_skipped` -- an earlier revision of
    this docstring called the owners fetch "the one feed-side collection that
    is genuinely gated", which was written before that fix landed and was
    already stale when it did. Unlike the community task's
    moderators fetch -- already covered by
    `test_the_moderators_url_is_taken_from_attributed_to` and its kbin
    sibling -- no existing test in this file drives the feed's owners fetch at
    all, so this is new coverage rather than a restatement.

    `ap_following_url` is set and mocked to an empty collection for the same
    reason as `test_refreshing_a_feed_applies_the_peers_document`: the feed
    this test describes is an ordinary remote one whose peer sent a
    `following` key, so the tail's gate is satisfied and its collection is
    served empty to keep the feed-item loop out of a test about owners. Before
    Task 10 gated that fetch the assignment was mandatory rather than
    descriptive -- without it the task crashed on `get_request(None)` before
    reaching this test's assertion. The unset column is now covered on its own
    by `test_a_feed_with_no_following_url_is_skipped`.
    """
    feed = _remote_feed()
    feed.ap_following_url = f'https://{PEER}/f/news/following'
    db.session.commit()
    owner = make_user(feed.instance, 'fauxowner')
    owner.ap_fetched_at = utcnow()
    db.session.commit()
    owners_url = f'https://{PEER}/f/news/owners'
    _serve(http_mock, feed.ap_public_url,
           _feed_document(fields={'attributedTo': owners_url}))
    _serve(http_mock, owners_url,
           {'type': 'OrderedCollection', 'orderedItems': [owner.ap_profile_id]})
    _serve(http_mock, feed.ap_following_url, {'type': 'Collection', 'items': []})

    refresh_feed_profile_task(feed.id)

    membership = db.session.query(FeedMember).filter_by(
        feed_id=feed.id, user_id=owner.id).first()
    assert membership is not None
    assert membership.is_owner is True


def test_a_malformed_feed_owner_entry_is_skipped(app, db_session, http_mock):
    """D219 residue, fixed (owner ruling): the feed refresh's owners loop
    skips an entry with no string id and processes the rest, as e32fec1bf made
    the community moderators loop do. An object with no `id`, or a number,
    used to raise out of the task -- in `find_actor_or_create` or in the
    removal pass's `actor['id']` / `.lower()` -- and abort the refresh."""
    feed = _remote_feed()
    feed.ap_following_url = f'https://{PEER}/f/news/following'
    db.session.commit()
    owner = make_user(feed.instance, 'fauxowner')
    owner.ap_fetched_at = utcnow()
    db.session.commit()
    db.session.add(FeedMember(feed_id=feed.id, user_id=owner.id, is_owner=True))
    db.session.commit()
    owners_url = f'https://{PEER}/f/news/owners'
    _serve(http_mock, feed.ap_public_url,
           _feed_document(fields={'attributedTo': owners_url}))
    _serve(http_mock, owners_url,
           {'type': 'OrderedCollection', 'orderedItems': [{'type': 'Person'}, 42, owner.ap_profile_id]})
    _serve(http_mock, feed.ap_following_url, {'type': 'Collection', 'items': []})

    refresh_feed_profile_task(feed.id)

    membership = db.session.query(FeedMember).filter_by(
        feed_id=feed.id, user_id=owner.id).one()
    assert membership.is_owner is True
    db.session.refresh(feed)
    assert feed.title == 'News, refreshed'


def test_a_typeless_owners_document_is_skipped(app, db_session, http_mock):
    """`'type' in owners_data`, checked before `owners_data['type']` is read.

    A FOURTH SITE, NOT IN ROUND 2'S BRIEF, which named the community
    moderators guard, the community followers guard and the following loop.
    `refresh_feed_profile_task`'s owners guard is character-for-character the
    community moderators guard with `owners_data` for `mods_data`, and carries
    the identical defect. Fixing three of four would have left exactly the
    one-clause-several-sites gap this campaign keeps hitting, so it is fixed
    and pinned with the rest.

    The document differs from `test_a_feed_owners_url_is_fetched_and_recorded`'s
    in exactly one key -- `type` absent, the same usable `orderedItems`
    remaining -- and is well-formed JSON at 200, so only this conjunct can
    stop it.

    `ap_following_url` is set and served empty for the usual reason (see
    `test_refreshing_a_feed_applies_the_peers_document`): this is an ordinary
    remote feed, and the following collection is not what is under test.
    """
    feed = _remote_feed()
    feed.ap_following_url = f'https://{PEER}/f/news/following'
    feed.title = 'Before'
    db.session.commit()
    owner = make_user(feed.instance, 'fauxowner')
    owner.ap_fetched_at = utcnow()
    db.session.commit()
    owners_url = f'https://{PEER}/f/news/owners'
    _serve(http_mock, feed.ap_public_url,
           _feed_document(fields={'attributedTo': owners_url}))
    _serve(http_mock, owners_url, {'orderedItems': [owner.ap_profile_id]})
    _serve(http_mock, feed.ap_following_url, {'type': 'Collection', 'items': []})

    refresh_feed_profile_task(feed.id)

    db.session.refresh(feed)
    assert feed.title == 'News, refreshed'
    assert db.session.query(FeedMember).filter_by(feed_id=feed.id).count() == 0


def test_no_feed_owners_url_means_no_owners_fetch(app, db_session, http_mock):
    """The absent side. No route is registered for the owners collection, and
    `block_outbound_http` raises if the task fetches one anyway. An existing
    membership is seeded first with `is_owner=True` so "unchanged" is
    distinguishable from "never created" -- the same contrast the community
    followers pair draws with a non-default count.

    `ap_following_url` is set and served empty for the reason given in
    `test_refreshing_a_feed_applies_the_peers_document`: it keeps this feed an
    ordinary remote one, so the only absent collection is the owners one this
    test is about. Before Task 10 gated the following fetch the assignment was
    load-bearing rather than descriptive.
    """
    feed = _remote_feed()
    feed.ap_following_url = f'https://{PEER}/f/news/following'
    db.session.commit()
    filler = make_user(feed.instance, 'fillerowner')
    db.session.commit()
    existing_membership = make_feed_member(filler, feed, is_owner=True)
    _serve(http_mock, feed.ap_public_url, _feed_document())
    _serve(http_mock, feed.ap_following_url, {'type': 'Collection', 'items': []})

    refresh_feed_profile_task(feed.id)

    db.session.refresh(existing_membership)
    assert existing_membership.is_owner is True
    assert db.session.query(FeedMember).filter_by(feed_id=feed.id).count() == 1


def test_object_shaped_owners_entries_are_unwrapped_before_comparison(
        app, db_session, http_mock):
    """OBJECT ENTRIES IN THE OWNERS COLLECTION -- D219(c).

    `refresh_feed_profile_task` reads `orderedItems` twice: a membership loop
    that hands each entry to `find_actor_or_create`, and a removal loop below
    it that compares each entry with `actor.lower()`. The two disagree about
    entry shape. `find_actor_or_create` opens with
    `if isinstance(actor, dict): actor = actor['id']`, so the membership loop
    accepts an object entry -- by the helper's doing, not its own -- while the
    removal loop's `.lower()` raises `AttributeError: 'dict' object has no
    attribute 'lower'` on the same entry, out of the task through its
    `except Exception: session.rollback(); raise`.

    THE CORRECT SPELLING WAS ALREADY IN THIS FILE, in the loop this one was
    copied from: `refresh_community_profile_task`'s moderators REMOVAL loop
    unwraps inline (`if isinstance(actor, dict): actor = actor['id']`) before
    its own `actor.lower()`. Its membership loop above it does not, for the
    same reason the feed's does not -- the helper covers it. So of the four
    loops the two tasks run over these collections, the feed's removal loop
    was the only one an object entry could not survive.

    `{'type': 'Person', 'id': ...}` is what Lemmy sends in an
    `OrderedCollection` of actors, and it is the shape the sibling loop was
    written to accept.

    TWO MEMBERSHIPS, ONE IN THE COLLECTION AND ONE NOT, so the removal loop
    has to do more than not crash. `stale` is seeded as an owner and left out
    of the served collection, so the pin fails if the unwrap is dropped
    (`AttributeError`), and equally if the loop is made to swallow the
    mismatch and remove nobody, or to remove everybody. Without a seeded
    `FeedMember` the removal loop would have nothing to iterate.

    No following collection is involved: `make_feed` leaves
    `ap_following_url` NULL and Task 10's gate skips that fetch, so the task
    ends after the owners pass.
    """
    feed = _remote_feed()
    owner = make_user(feed.instance, 'fauxowner')
    owner.ap_fetched_at = utcnow()
    stale = make_user(feed.instance, 'staleowner')
    db.session.commit()
    make_feed_member(stale, feed, is_owner=True)
    owners_url = f'https://{PEER}/f/news/owners'
    _serve(http_mock, feed.ap_public_url,
           _feed_document(fields={'attributedTo': owners_url}))
    _serve(http_mock, owners_url,
           {'type': 'OrderedCollection',
            'orderedItems': [{'type': 'Person', 'id': owner.ap_profile_id}]})

    refresh_feed_profile_task(feed.id)

    kept = db.session.query(FeedMember).filter_by(
        feed_id=feed.id, user_id=owner.id).one()
    assert kept.is_owner is True
    assert db.session.query(FeedMember).filter_by(
        feed_id=feed.id, user_id=stale.id).count() == 0


def test_a_failed_community_fetch_is_not_retried(
        app, db_session, http_mock, no_real_sleeping):
    """D224, fixed (owner ruling): one `httpx.HTTPError` returns quietly with
    no sleep and no second fetch, as for the user task. The community keeps its
    pre-refresh title, which is the observable -- the task returns normally,
    so "nothing raised" would not distinguish this from a successful refresh.
    """
    community = _remote_community()
    community.title = 'Before'
    db.session.commit()
    route = http_mock.get(community.ap_public_url).mock(side_effect=httpx.ConnectError('boom'))

    refresh_community_profile_task(community.id, None)

    db.session.refresh(community)
    assert route.call_count == 2  # get_request's own two attempts, not four
    assert community.title == 'Before'


def test_a_failed_feed_fetch_is_not_retried(
        app, db_session, http_mock, no_real_sleeping):
    """D224, fixed (owner ruling): the feed task's twin of the two above."""
    feed = _remote_feed()
    feed.title = 'Before'
    db.session.commit()
    route = http_mock.get(feed.ap_public_url).mock(side_effect=httpx.ConnectError('boom'))

    refresh_feed_profile_task(feed.id)

    db.session.refresh(feed)
    assert route.call_count == 2  # get_request's own two attempts, not four
    assert feed.title == 'Before'


def test_a_user_fetch_failing_outside_httpx_falls_back_to_a_signed_get(
        app, db_session, http_mock, no_real_sleeping, monkeypatch):
    """`refresh_user_profile_task`'s `except Exception:` retries with
    `signed_get_request` against the site's private key, so a peer requiring
    HTTP signatures to serve its actor document is still refreshable. The
    community and feed tasks now have the same path (D221, tests below).

    It was a bare `except:` until D220, which also caught SystemExit;
    test_a_worker_shutdown_during_a_user_fetch_propagates pins the fix.
    """
    site = seed_signing_site()
    user = _remote_user()
    calls = []

    def exploding_get_request(uri, params=None, headers=None):
        raise RuntimeError('not an httpx error')

    def fake_signed_get(uri, private_key, key_id, **kwargs):
        calls.append((uri, private_key))
        return httpx.Response(200, json=_person_document(fields={'name': 'Signed'}))

    monkeypatch.setattr(ap_util, 'get_request', exploding_get_request)
    monkeypatch.setattr(ap_util, 'signed_get_request', fake_signed_get)

    refresh_user_profile_task(user.id)

    db.session.refresh(user)
    assert calls and calls[0][0] == user.ap_public_url
    assert calls[0][1] == site.private_key
    assert user.title == 'Signed'


def test_a_worker_shutdown_during_a_user_fetch_propagates(
        app, db_session, http_mock, monkeypatch):
    """D220, fixed: the user task's two fallback handlers were bare `except:`,
    so a SystemExit from a worker being stopped mid-fetch was swallowed into
    the signed-GET fallback and the task returned as if it had refreshed. They
    are now `except Exception:`, like the sibling tasks', so it propagates."""
    seed_signing_site()
    user = _remote_user()
    calls = []

    def stopping_get_request(uri, params=None, headers=None):
        raise SystemExit(1)

    def fake_signed_get(uri, private_key, key_id, **kwargs):
        calls.append(uri)
        return httpx.Response(200, json=_person_document())

    monkeypatch.setattr(ap_util, 'get_request', stopping_get_request)
    monkeypatch.setattr(ap_util, 'signed_get_request', fake_signed_get)

    with pytest.raises(SystemExit):
        refresh_user_profile_task(user.id)

    assert calls == []


def test_a_community_fetch_failing_outside_httpx_falls_back_to_a_signed_get(
        app, db_session, http_mock, monkeypatch):
    """D221, fixed (owner ruling): the community task used to let a non-httpx
    fetch failure propagate, so a peer requiring HTTP signatures on actor
    fetches left its communities' title, icon, moderators and key frozen at
    first ingest. It now falls back to a signed GET as the user task does.

    `RuntimeError` rather than `httpx.HTTPError`, which returns quietly
    (`test_a_failed_community_fetch_is_not_retried`).
    """
    site = seed_signing_site()
    community = _remote_community()
    calls = []

    def exploding_get_request(uri, params=None, headers=None):
        raise RuntimeError('not an httpx error')

    def fake_signed_get(uri, private_key, key_id, **kwargs):
        calls.append((uri, private_key))
        return httpx.Response(200, json=_group_document())

    monkeypatch.setattr(ap_util, 'get_request', exploding_get_request)
    monkeypatch.setattr(ap_util, 'signed_get_request', fake_signed_get)

    refresh_community_profile_task(community.id, None)

    db.session.refresh(community)
    assert calls == [(community.ap_public_url, site.private_key)]
    assert community.title == 'Memes, refreshed'


def test_a_feed_fetch_failing_outside_httpx_falls_back_to_a_signed_get(
        app, db_session, http_mock, monkeypatch):
    """D221, fixed (owner ruling): the feed task's twin of the community test
    above."""
    site = seed_signing_site()
    feed = _remote_feed()
    calls = []

    def exploding_get_request(uri, params=None, headers=None):
        raise RuntimeError('not an httpx error')

    def fake_signed_get(uri, private_key, key_id, **kwargs):
        calls.append((uri, private_key))
        return httpx.Response(200, json=_feed_document())

    monkeypatch.setattr(ap_util, 'get_request', exploding_get_request)
    monkeypatch.setattr(ap_util, 'signed_get_request', fake_signed_get)

    refresh_feed_profile_task(feed.id)

    db.session.refresh(feed)
    assert calls == [(feed.ap_public_url, site.private_key)]
    assert feed.title == 'News, refreshed'


def _fail_midway(*args, **kwargs):
    raise RuntimeError('midway')


def test_a_community_refresh_failing_midway_propagates(
        app, db_session, http_mock, monkeypatch):
    """What keeps `refresh_community_profile_task`'s `except Exception:
    session.rollback(); raise` covered now that a non-httpx fetch failure falls
    back to a signed GET (D221) instead of reaching it. The seeded title is
    asserted after the raise because the rollback must not leave a
    half-applied document behind; as before, the rollback itself is not
    independently observable here, so this kills the `raise`.
    """
    community = _remote_community()
    community.title = 'Before'
    db.session.commit()
    _serve(http_mock, community.ap_public_url, _group_document())
    monkeypatch.setattr(ap_util, 'public_key_pem', _fail_midway)

    with pytest.raises(RuntimeError, match='midway'):
        refresh_community_profile_task(community.id, None)

    db.session.refresh(community)
    assert community.title == 'Before'


def test_a_feed_refresh_failing_midway_propagates(
        app, db_session, http_mock, monkeypatch):
    """The feed task's twin of the community test above."""
    feed = _remote_feed()
    feed.title = 'Before'
    db.session.commit()
    _serve(http_mock, feed.ap_public_url, _feed_document())
    monkeypatch.setattr(ap_util, 'public_key_pem', _fail_midway)

    with pytest.raises(RuntimeError, match='midway'):
        refresh_feed_profile_task(feed.id)

    db.session.refresh(feed)
    assert feed.title == 'Before'


def test_a_following_collection_entry_becomes_a_feed_item(app, db_session, http_mock):
    """THE FOLLOWING LOOP'S BODY, which no earlier test reached: every feed
    test before this one served `{'items': []}` to get past the following
    fetch, so the loop body -- resolve the entry, build a `FeedItem`, commit
    -- had never executed.

    NOT "the last statement-coverage gap in this sub-project", which an
    earlier revision of this docstring claimed. It was false when written and
    is measurably false now: 124 statements across the three tasks are still
    uncovered, most of them in the document-application bodies (icon, image,
    description, language), and the residual is registered as D235 with the
    per-function breakdown.

    ENTRIES ARE BARE STRINGS HERE. `community_ap_id = fci` takes the item
    itself, where the community task's moderators handling unwraps objects
    (`if isinstance(actor, dict): actor = actor['id']`,
    `app/activitypub/util.py:967-968`) and the featured collection is read as
    `item['id']`. Note the unwrap is in the moderators REMOVAL loop, not the
    membership loop above it -- the membership loop (`:948-950`) passes the
    raw entry to `find_actor_or_create` exactly as this one does, so the two
    moderators loops disagree with each other about entry shape. Three
    collections in the same slice, three different entry shapes assumed. A
    dict entry would
    in fact survive here, because `find_actor_or_create` unwraps one itself --
    but only by accident of that helper, not because this loop handles it, so
    the string form is what is pinned.

    `_following_community()` explains why the entry resolves with no outbound
    request; `block_outbound_http` would raise otherwise, and no route is
    registered for the community.

    The assertion is on the persisted `FeedItem` row and BOTH its foreign
    keys. `.one()` rather than `.first()` so a loop that created two rows for
    one entry fails here rather than passing on the first.
    """
    feed = _remote_feed()
    feed.ap_following_url = f'https://{PEER}/f/news/following'
    db.session.commit()
    community = _following_community()
    _serve(http_mock, feed.ap_public_url, _feed_document())
    _serve(http_mock, feed.ap_following_url,
           {'type': 'Collection', 'items': [community.ap_profile_id]})

    refresh_feed_profile_task(feed.id)

    feed_item = db.session.query(FeedItem).one()
    assert feed_item.feed_id == feed.id
    assert feed_item.community_id == community.id


def test_a_following_entry_that_resolves_to_nothing_is_skipped(app, db_session, http_mock):
    """The False arm of `if community and isinstance(community, Community):`.

    The collection carries TWO entries, one unresolvable and one resolvable,
    and the resolvable one is LAST. A lone bad entry would prove only that
    nothing was created; this ordering also proves the loop CONTINUED past the
    bad one rather than aborting, which is the difference between a skip and a
    silent truncation of the feed.

    The unresolvable entry is the ActivityStreams Public URI, which
    `validate_remote_actor` (app/activitypub/actor.py) refuses by exact match
    on its very first line, so `find_actor_or_create` returns None having
    touched neither the database nor the network. That matters: the obvious
    alternative -- a community URL that simply is not in the database --
    reaches `create_actor_from_remote` instead, because the loop calls
    `find_actor_or_create` with `create_if_not_found` at its default of True,
    and that FETCHES. `block_outbound_http` would raise and the test would be
    measuring the fixture rather than the guard. Public is also a shape a real
    peer can emit into a collection.

    Removing the guard makes `community.id` a `None.id` and kills this test by
    `AttributeError`; keeping it but dropping the `continue`-equivalent skip
    would create a second row and fail the count.
    """
    feed = _remote_feed()
    feed.ap_following_url = f'https://{PEER}/f/news/following'
    db.session.commit()
    community = _following_community()
    _serve(http_mock, feed.ap_public_url, _feed_document())
    _serve(http_mock, feed.ap_following_url,
           {'type': 'Collection', 'items': ['https://www.w3.org/ns/activitystreams#Public',
                      community.ap_profile_id]})

    refresh_feed_profile_task(feed.id)

    assert db.session.query(FeedItem).count() == 1
    assert db.session.query(FeedItem).one().community_id == community.id


def test_a_following_collection_with_no_items_key_is_skipped(app, db_session, http_mock):
    """`'items' in following_collection` -- since D234 `isinstance(
    following_collection.get('items'), list)` -- checked before the loop
    subscripts it. The following loop had no guard of any kind: `following_collection`
    could be `None` (a body of JSON `null`) or an object with no `items`, and
    either raised out of the task.

    The `type` check its four sibling guards make was added later (D228,
    `test_a_typeless_following_collection_is_skipped`); this test serves a
    `Collection` so only the missing `items` stops it.

    WHAT NOW TAKES THE SKIP PATH INSTEAD OF CRASHING: an empty collection
    serialised without the key -- `{"type": "Collection", "totalItems": 0}` is
    the common ActivityPub spelling -- a paged collection that offers `first`
    instead of inline items, and a collection using `orderedItems` (which this
    loop has never read). All three are ordinary peer output, not malformed
    JSON.

    THE DOCUMENT IS TRUTHY AND LACKS ONLY `items`. `{}` would be stopped by
    the `following_collection and` conjunct instead, conflating the two halves
    of the new guard; a truthy object isolates the membership check.
    """
    feed = _remote_feed()
    feed.ap_following_url = f'https://{PEER}/f/news/following'
    feed.title = 'Before'
    db.session.commit()
    _serve(http_mock, feed.ap_public_url, _feed_document())
    _serve(http_mock, feed.ap_following_url,
           {'type': 'Collection', 'totalItems': 0})

    refresh_feed_profile_task(feed.id)

    db.session.refresh(feed)
    assert feed.title == 'News, refreshed'
    assert feed.public_key == '-----BEGIN PUBLIC KEY-----refreshed'
    assert db.session.query(FeedItem).count() == 0


def test_a_typeless_following_collection_is_skipped(app, db_session, http_mock):
    """D228, fixed (owner ruling): the following loop's guard now makes the
    `type == 'Collection'` check its four sibling guards make, so a document
    that is not a Collection is not read for feed items -- the feed's own
    refresh still applies. `/f/<name>/following` publishes `"type":
    "Collection"`, which every other feed test here now serves.
    """
    feed = _remote_feed()
    community = _following_community()
    feed.ap_following_url = f'https://{PEER}/f/news/following'
    db.session.commit()
    _serve(http_mock, feed.ap_public_url, _feed_document())
    _serve(http_mock, feed.ap_following_url, {'items': [community.ap_profile_id]})

    refresh_feed_profile_task(feed.id)

    db.session.refresh(feed)
    assert feed.title == 'News, refreshed'
    assert db.session.query(FeedItem).count() == 0


def test_a_null_following_collection_is_skipped(app, db_session, http_mock):
    """The FIRST conjunct of `if following_collection and 'items' in
    following_collection:` -- `following_collection` itself must be truthy.
    Since D234 that conjunct is `isinstance(following_collection, dict)`,
    which None fails just the same; the mutant analysis below predates it.
    Mirrors `test_a_null_featured_document_does_nothing`, which pins the same
    conjunct in the featured guard.

    A body of JSON `null` decodes to `None`. `None` is falsy, so the guard
    short-circuits here without evaluating the membership check after it and
    without raising; delete THIS conjunct alone and `'items' in None` runs
    next, raising `TypeError` (a `None` is not a container). So this test
    kills its mutant by crash, where
    `test_a_following_collection_with_no_items_key_is_skipped` kills the
    SECOND conjunct by `KeyError`. Two conjuncts, two attributable kills.

    THIS TEST EXISTS BECAUSE FIX E SHIPPED WITHOUT IT. Round 2 mutated the
    whole guard to `if True:`, which deletes both conjuncts at once and so
    proves the site rather than either conjunct. Deleting `following_collection
    and` on its own left the entire suite green: every other feed test serves a
    truthy dict, and `text='null'` appeared exactly once in this file, at the
    featured guard. A fix that ADDS a conjunct needs a mutation that deletes
    THAT conjunct alone.

    Note `null` is well-formed JSON, so it passes the status check and the
    decode guard above and only this conjunct can stop it -- the same
    isolation requirement every Fix D and Fix E pin had to meet.

    The feed's own columns are asserted alongside the absent `FeedItem`
    because this guard must skip only the collection: `null` at the following
    URL must not cost the feed its refresh.
    """
    feed = _remote_feed()
    feed.ap_following_url = f'https://{PEER}/f/news/following'
    feed.title = 'Before'
    db.session.commit()
    _serve(http_mock, feed.ap_public_url, _feed_document())
    _serve(http_mock, feed.ap_following_url, text='null')

    refresh_feed_profile_task(feed.id)

    db.session.refresh(feed)
    assert feed.title == 'News, refreshed'
    assert feed.public_key == '-----BEGIN PUBLIC KEY-----refreshed'
    assert db.session.query(FeedItem).count() == 0


def test_the_following_collection_fetch_asks_for_activity_json(app, db_session, http_mock):
    """The `Accept` header on the following-collection fetch -- D232.

    `get_request` (`app/utils.py:131-139`) builds a headers dict holding only
    a `User-Agent` when its `headers` argument is None, so a call site that
    passes nothing expresses no content preference at all and a peer that
    content-negotiates is free to answer an ActivityPub URL with HTML. The
    other two `get_request` calls in `refresh_feed_profile_task` -- the
    actor fetch and the owners-collection fetch -- each pass
    `headers={'Accept': 'application/activity+json'}`.

    WHAT THE PEER SAW BEFORE THE FIX WAS `*/*`, not nothing: httpx's own
    client defaults supply an `Accept` when the caller sets none, and `*/*`
    is the one header value that positively invites a content-negotiating
    peer to send its HTML representation. So the assertion is an equality
    against the intended value rather than a presence check -- a presence
    check would have passed unfixed.

    THE ASSERTION IS ON THE RECORDED REQUEST, NOT ON THE OUTCOME, and it has
    to be. respx serves whatever the route was given no matter what the
    request asked for, so a test that checked only the resulting `FeedItem`
    rows would pass identically with and without the header. `_serve` returns
    the route it registers, and respx records every matched call on that
    route, so what the task actually sent is readable directly.

    WHY THE MISSING HEADER IS WORSE THAN IT LOOKS: the following fetch's
    `except JSONDecodeError: res.close(); return` guard means a peer that
    answers with HTML no longer raises out of the task. It returns instead,
    leaving the feed's `FeedItem` set unsynced with no exception, no log line
    and no `instance.failures` increment -- pinned as correct handling of a
    malformed body by `test_a_malformed_following_collection_creates_no_feed_items`,
    and turned into a silent functional failure only because this one call
    site asked for nothing in particular.
    """
    feed = _remote_feed()
    feed.ap_following_url = f'https://{PEER}/f/news/following'
    db.session.commit()
    community = _following_community()
    _serve(http_mock, feed.ap_public_url, _feed_document())
    following_route = _serve(http_mock, feed.ap_following_url,
                             {'type': 'Collection', 'items': [community.ap_profile_id]})

    refresh_feed_profile_task(feed.id)

    assert following_route.call_count == 1
    sent = following_route.calls.last.request
    assert sent.headers['accept'] == 'application/activity+json'
    assert db.session.query(FeedItem).one().community_id == community.id


def _spy_on_actor_lookups(monkeypatch):
    """Record each collection entry the loops resolve and whether they were
    allowed to create it, resolving nothing so no fetch or row follows."""
    calls = []

    def spy(actor, create_if_not_found=True, **kwargs):
        calls.append((actor, create_if_not_found, kwargs.get('retry')))
        return None

    monkeypatch.setattr(ap_util, 'find_actor_or_create', spy)
    return calls


def test_moderators_on_another_host_are_looked_up_but_not_created(app, db_session, http_mock, monkeypatch):
    """D226, fixed (owner ruling 2026-09-30). Every entry in a peer-supplied
    moderators collection used to be created if unseen, so a peer chose how many
    actors, on which hosts, this instance fetched. Unseen actors are now created
    only on the community's own host; other hosts are lookup-only. The housekeeping
    `retry=True` (D775) is kept.
    """
    community = _remote_community()
    mods_url = f'https://{PEER}/c/memes/moderators'
    _serve(http_mock, mods_url, {'type': 'OrderedCollection', 'orderedItems': [
        f'https://{PEER}/u/samehost', 'https://elsewhere.example/u/otherhost']})
    calls = _spy_on_actor_lookups(monkeypatch)

    refresh_community_profile_task(
        community.id, _group_document(fields={'attributedTo': mods_url}))

    assert calls == [(f'https://{PEER}/u/samehost', True, True),
                     ('https://elsewhere.example/u/otherhost', False, True)]


def test_feed_owners_on_another_host_are_looked_up_but_not_created(app, db_session, http_mock, monkeypatch):
    """D226, fixed: the feed owners loop applies the same host rule, against the
    feed's own host. The following collection is served empty to keep its loop
    out of this test."""
    feed = _remote_feed()
    feed.ap_following_url = f'https://{PEER}/f/news/following'
    db.session.commit()
    owners_url = f'https://{PEER}/f/news/owners'
    _serve(http_mock, feed.ap_public_url,
           _feed_document(fields={'attributedTo': owners_url}))
    _serve(http_mock, owners_url, {'type': 'OrderedCollection', 'orderedItems': [
        f'https://{PEER}/u/samehost', 'https://elsewhere.example/u/otherhost']})
    _serve(http_mock, feed.ap_following_url, {'type': 'Collection', 'items': []})
    calls = _spy_on_actor_lookups(monkeypatch)

    refresh_feed_profile_task(feed.id)

    assert calls == [(f'https://{PEER}/u/samehost', True, True),
                     ('https://elsewhere.example/u/otherhost', False, True)]


def test_followed_communities_on_another_host_are_looked_up_but_not_created(
        app, db_session, http_mock, monkeypatch):
    """D226, fixed: the feed's following loop applies the same host rule. A feed
    following a community on another host only gains it once this instance
    already knows that community."""
    feed = _remote_feed()
    feed.ap_following_url = f'https://{PEER}/f/news/following'
    db.session.commit()
    _serve(http_mock, feed.ap_public_url, _feed_document())
    _serve(http_mock, feed.ap_following_url, {'type': 'Collection', 'items': [
        f'https://{PEER}/c/samehost', 'https://elsewhere.example/c/otherhost']})
    calls = _spy_on_actor_lookups(monkeypatch)

    refresh_feed_profile_task(feed.id)

    assert calls == [(f'https://{PEER}/c/samehost', True, True),
                     ('https://elsewhere.example/c/otherhost', False, True)]


# D234, fixed: the moderators, featured, owners and following guards checked
# that the collection's key was present, not that its value was a list, so a
# string was iterated one character at a time. A non-list value now skips the
# whole collection, which leaves existing moderators, owners and stickies alone.

def _spy_on_find_actor_or_create(monkeypatch):
    seen = []
    monkeypatch.setattr(ap_util, 'find_actor_or_create', lambda actor, *a, **kw: seen.append(actor))
    return seen


def test_a_string_moderators_collection_is_skipped(app, db_session, http_mock, monkeypatch, no_real_sleeping):
    """D234, fixed: a string orderedItems used to reach find_actor_or_create
    per character, and then the removal walk matched no character to the
    existing moderator and removed them."""
    community = _remote_community()
    mod = make_user(community.instance, 'fauxmod')
    db.session.add(CommunityMember(user_id=mod.id, community_id=community.id, is_moderator=True))
    db.session.commit()
    mods_url = f'https://{PEER}/c/memes/moderators'
    _serve(http_mock, mods_url, {'type': 'OrderedCollection', 'orderedItems': mod.ap_profile_id})
    seen = _spy_on_find_actor_or_create(monkeypatch)

    refresh_community_profile_task(community.id, _group_document(fields={'attributedTo': mods_url}))

    assert seen == []
    assert db.session.query(CommunityMember).filter_by(
        community_id=community.id, user_id=mod.id, is_moderator=True).count() == 1


def test_a_string_featured_collection_is_skipped(app, db_session, http_mock):
    """D234, fixed: a string orderedItems used to clear every sticky and then
    raise TypeError at `item['id']` on the first character."""
    community = _remote_community()
    featured_url = f'https://{PEER}/c/memes/featured'
    community.ap_featured_url = featured_url
    poster = make_user(community.instance, 'poster')
    db.session.commit()
    sticky = make_post(community, poster, f'https://{PEER}/p/sticky')
    sticky.sticky = True
    db.session.commit()
    _serve(http_mock, featured_url, {'type': 'OrderedCollection', 'orderedItems': sticky.ap_id})

    refresh_community_profile_task(community.id, _group_document())

    db.session.refresh(sticky)
    assert sticky.sticky is True


def test_a_string_owners_collection_is_skipped(app, db_session, http_mock, monkeypatch, no_real_sleeping):
    """D234, fixed, on the feed task's owners guard."""
    feed = _remote_feed()
    owner = make_user(feed.instance, 'fauxowner')
    db.session.commit()
    make_feed_member(owner, feed, is_owner=True)
    owners_url = f'https://{PEER}/f/news/owners'
    _serve(http_mock, feed.ap_public_url, _feed_document(fields={'attributedTo': owners_url}))
    _serve(http_mock, owners_url, {'type': 'OrderedCollection', 'orderedItems': owner.ap_profile_id})
    seen = _spy_on_find_actor_or_create(monkeypatch)

    refresh_feed_profile_task(feed.id)

    assert seen == []
    assert db.session.query(FeedMember).filter_by(feed_id=feed.id, user_id=owner.id, is_owner=True).count() == 1


def test_a_string_following_collection_is_skipped(app, db_session, http_mock, monkeypatch):
    """D234, fixed, on the feed task's following guard."""
    feed = _remote_feed()
    feed.ap_following_url = f'https://{PEER}/f/news/following'
    db.session.commit()
    _serve(http_mock, feed.ap_public_url, _feed_document())
    _serve(http_mock, feed.ap_following_url, {'items': f'https://{PEER}/c/memes'})
    seen = _spy_on_find_actor_or_create(monkeypatch)

    refresh_feed_profile_task(feed.id)

    assert seen == []
    assert db.session.query(FeedItem).count() == 0


# D225, fixed (owner ruling): the moderators, featured, owners and following
# loops slept 0.5s inline in the worker before every entry, and the peer chose
# how many entries there were. The sleep is gone and each loop acts on at most
# 50 entries.

def _sixty(path):
    return [f'https://{PEER}/{path}/{n}' for n in range(60)]


def _record_sleeps(monkeypatch):
    slept = []
    monkeypatch.setattr(ap_util.time, 'sleep', lambda seconds: slept.append(seconds))
    return slept


def test_the_moderators_loop_acts_on_fifty_entries_without_sleeping(app, db_session, http_mock, monkeypatch):
    community = _remote_community()
    mods_url = f'https://{PEER}/c/memes/moderators'
    _serve(http_mock, mods_url, {'type': 'OrderedCollection', 'orderedItems': _sixty('u')})
    calls = _spy_on_actor_lookups(monkeypatch)
    slept = _record_sleeps(monkeypatch)

    refresh_community_profile_task(
        community.id, _group_document(fields={'attributedTo': mods_url}))

    assert [actor for actor, _, _ in calls] == _sixty('u')[:50]
    assert slept == []


def test_the_featured_loop_acts_on_fifty_entries(app, db_session, http_mock):
    community = _remote_community()
    featured_url = f'https://{PEER}/c/memes/featured'
    community.ap_featured_url = featured_url
    db.session.commit()
    poster = make_user(community.instance, 'poster')
    db.session.commit()
    fiftieth = make_post(community, poster, _sixty('p')[49])
    fifty_first = make_post(community, poster, _sixty('p')[50])
    db.session.commit()
    _serve(http_mock, featured_url, {'type': 'OrderedCollection',
                                     'orderedItems': [{'id': url} for url in _sixty('p')]})

    refresh_community_profile_task(community.id, _group_document())

    db.session.refresh(fiftieth)
    db.session.refresh(fifty_first)
    assert fiftieth.sticky is True
    assert fifty_first.sticky is False


def test_the_feed_owners_and_following_loops_act_on_fifty_entries_without_sleeping(
        app, db_session, http_mock, monkeypatch):
    feed = _remote_feed()
    feed.ap_following_url = f'https://{PEER}/f/news/following'
    db.session.commit()
    owners_url = f'https://{PEER}/f/news/owners'
    _serve(http_mock, feed.ap_public_url, _feed_document(fields={'attributedTo': owners_url}))
    _serve(http_mock, owners_url, {'type': 'OrderedCollection', 'orderedItems': _sixty('u')})
    _serve(http_mock, feed.ap_following_url, {'type': 'Collection', 'items': _sixty('c')})
    calls = _spy_on_actor_lookups(monkeypatch)
    slept = _record_sleeps(monkeypatch)

    refresh_feed_profile_task(feed.id)

    assert [actor for actor, _, _ in calls] == _sixty('u')[:50] + _sixty('c')[:50]
    assert slept == []


# D225 residue: only the per-entry fetch/write loop is capped. The removal pass
# compares the existing moderators and owners against the peer's full list, so
# one listed after entry 50 keeps the role. Already correct; these prove it.

def test_a_moderator_listed_after_the_fiftieth_entry_is_not_removed(app, db_session, http_mock, monkeypatch):
    community = _remote_community()
    mod = make_user(community.instance, 'latemod')
    mod.ap_profile_id = _sixty('u')[55]
    db.session.add(CommunityMember(user_id=mod.id, community_id=community.id, is_moderator=True))
    db.session.commit()
    mods_url = f'https://{PEER}/c/memes/moderators'
    _serve(http_mock, mods_url, {'type': 'OrderedCollection', 'orderedItems': _sixty('u')})
    _spy_on_actor_lookups(monkeypatch)

    refresh_community_profile_task(community.id, _group_document(fields={'attributedTo': mods_url}))

    assert db.session.query(CommunityMember).filter_by(
        community_id=community.id, user_id=mod.id, is_moderator=True).count() == 1


def test_a_feed_owner_listed_after_the_fiftieth_entry_is_not_removed(app, db_session, http_mock, monkeypatch):
    feed = _remote_feed()
    owner = make_user(feed.instance, 'lateowner')
    owner.ap_profile_id = _sixty('u')[55]
    db.session.add(FeedMember(user_id=owner.id, feed_id=feed.id, is_owner=True))
    db.session.commit()
    owners_url = f'https://{PEER}/f/news/owners'
    _serve(http_mock, feed.ap_public_url, _feed_document(fields={'attributedTo': owners_url}))
    _serve(http_mock, owners_url, {'type': 'OrderedCollection', 'orderedItems': _sixty('u')})
    _spy_on_actor_lookups(monkeypatch)

    refresh_feed_profile_task(feed.id)

    assert db.session.query(FeedMember).filter_by(feed_id=feed.id, user_id=owner.id, is_owner=True).count() == 1
