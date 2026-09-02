"""Coverage for the three actor-profile endpoints in app/activitypub/routes.py:
user_profile (/u/<actor>), community_profile (/c/<actor>) and feed_profile
(/f/<actor>). All three are mirrored implementations of "serve an actor as
ActivityPub JSON or as HTML, chosen by the Accept header" -- these are the
documents every federated interaction with this instance begins by fetching.

This file covers the content-negotiation switch (`is_activitypub_request()`)
that governs all three endpoints, using community_profile as the
representative because it starts the least covered of the three (16% vs.
user_profile's 52.9%, inherited from tests/test_remote_handle_resolution.py and
tests/test_request_hooks.py -- see those files' docstrings and this task's
report for exactly what they already prove). Later tasks in this sub-project
build community- and feed-specific coverage on top of the two helpers below;
their names and signatures (`profile_get`, `seed_actors`) are a contract those
tasks depend on.

`is_activitypub_request` IS DEFINED TWICE, BYTE-IDENTICALLY, AND ONE COPY IS
DEAD. app/activitypub/util.py:2200 is the one these routes call -- routes.py
imports it there. app/utils.py:1862 is a duplicate that nothing in the
application imports. Mutating the app/utils.py copy kills no test here, which
is easy to misread as weak coverage; mutating the app/activitypub/util.py copy
kills one test per disjunct. Any change or mutation of this function must
target app.activitypub.util, or patch the name as bound on activitypub_routes.
"""

from app import db
from app.activitypub import routes as activitypub_routes
from tests.factories import (make_community, make_local_feed, make_site, make_user,
                             seed_community_owner)

AP_ACCEPT = 'application/activity+json'
LD_ACCEPT = 'application/ld+json'


def profile_get(app, path, accept=None):
    """GET an actor-profile endpoint through the real route.

    `accept` is passed as a real Accept header rather than doubling
    `is_activitypub_request()`, which is a plain substring test over exactly
    two values (app/activitypub/util.py:2200 -- NOT the dead byte-identical
    twin in app/utils.py; see this module's docstring). The parse is part of
    what is under test, so a double would hide it -- the same reason
    sub-project 8 drove `requestor_domain()` with a real User-Agent.
    """
    headers = {'Accept': accept} if accept is not None else {}
    with app.test_client() as client:
        return client.get(path, headers=headers)


def seed_actors(host='peer.example'):
    """A Site row plus the Instance and User that id-1-hardcoding factories need.

    `seed_community_owner` rather than a bare `make_instance`: it creates BOTH
    the Instance (id 1) and a local User (id 1), which `make_community`
    hardcodes against real foreign keys.
    """
    site = make_site()
    instance = seed_community_owner(host)
    db.session.commit()
    return site, instance


def _double_the_renderers(monkeypatch):
    """Stop the three HTML renderers and the remote-handle resolver from running.

    `resolve_remote_handle` reaches the NETWORK; `user_profile` calls it
    whenever its lookups return None, so a test that omits this double can make
    a real request. The three `show_*` renderers pull templates and are each
    their own future slice.
    """
    calls = {}
    for name in ('show_profile', 'show_community', 'show_feed'):
        calls[name] = []
        monkeypatch.setattr(activitypub_routes, name,
                            lambda obj, _n=name: calls[_n].append(obj) or f'HTML:{_n}')
    monkeypatch.setattr(activitypub_routes, 'resolve_remote_handle', lambda actor: None)
    return calls


def test_an_activity_json_accept_header_selects_the_activitypub_document(app, db_session, monkeypatch):
    """`is_activitypub_request()` is true for 'application/activity+json'. The
    community endpoint is used because it is the least covered of the three;
    the same switch governs all three.
    """
    seed_actors()
    make_community(name='books', host='test.piefed.local')
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/c/books', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.content_type == 'application/activity+json'
    assert response.json['type'] == 'Group'


def test_an_ld_json_accept_header_also_selects_the_activitypub_document(app, db_session, monkeypatch):
    """The OTHER accepted value. `is_activitypub_request` is a two-disjunct
    substring test and this is the only test that exercises the ld+json arm --
    without it, dropping that disjunct would kill nothing.
    """
    seed_actors()
    make_community(name='books', host='test.piefed.local')
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/c/books', accept=LD_ACCEPT)

    assert response.status_code == 200
    assert response.content_type == 'application/activity+json'


def test_a_browser_accept_header_selects_the_html_renderer(app, db_session, monkeypatch):
    """The false side. A browser Accept reaches `show_community`, which is
    doubled -- so this asserts the DELEGATION happened, not what the template
    produced.
    """
    seed_actors()
    community = make_community(name='books', host='test.piefed.local')
    community_id = community.id          # captured BEFORE the request: see below
    calls = _double_the_renderers(monkeypatch)

    response = profile_get(app, '/c/books', accept='text/html')

    assert response.status_code == 200
    assert len(calls['show_community']) == 1
    assert calls['show_community'][0].id == community_id


def test_no_accept_header_at_all_selects_the_html_renderer(app, db_session, monkeypatch):
    """`request.headers.get('Accept', '')` defaults to the empty string, so a
    request with no Accept is a browser request. Distinct from the test above,
    which sends a header that is present but not an AP type.
    """
    seed_actors()
    make_community(name='books', host='test.piefed.local')
    calls = _double_the_renderers(monkeypatch)

    response = profile_get(app, '/c/books')

    assert response.status_code == 200
    assert len(calls['show_community']) == 1


def test_a_local_community_is_resolved_by_its_profile_id(app, db_session, monkeypatch):
    """The local branch builds `https://<SERVER_NAME>/c/<actor.lower()>` and
    compares it to `ap_profile_id`. `make_community(host='test.piefed.local')`
    produces exactly that, and leaves `ap_id` None.
    """
    seed_actors()
    make_community(name='books', host='test.piefed.local')
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/c/books', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['preferredUsername'] == 'books'


def test_a_local_community_with_a_non_null_ap_id_is_not_found(app, db_session, monkeypatch):
    """The local lookup's `ap_id=None` clause. `make_community` never sets
    `ap_id` (it stays None), so the positive test above cannot distinguish
    filtering on `ap_id=None` from not filtering on it at all -- dropping that
    clause from the query changes nothing there. This test sets `ap_id`
    explicitly to a non-null value on an otherwise-matching community, so the
    filter is the ONLY thing standing between it and a 200.
    """
    seed_actors()
    community = make_community(name='books', host='test.piefed.local')
    community.ap_id = 'books@test.piefed.local'
    db.session.commit()
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/c/books', accept=AP_ACCEPT)

    assert response.status_code == 404


def test_a_remote_community_refuses_an_activitypub_request(app, db_session, monkeypatch):
    """`'@' in actor` plus an AP Accept aborts 400 -- the comment says "don't
    provide activitypub info for remote communities". `user_profile` has NO
    equivalent guard, which the spec registers as an asymmetry; this test is
    the community half of that comparison.
    """
    seed_actors()
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/c/books@peer.example', accept=AP_ACCEPT)

    assert response.status_code == 400


def test_a_remote_community_serves_html_to_a_browser(app, db_session, monkeypatch):
    """The same remote path WITHOUT an AP Accept skips the 400 and looks the
    community up by `ap_id`, filtered `banned=False`.
    """
    site, instance = seed_actors()
    community = make_community(name='books', host='peer.example')
    community.ap_id = 'books@peer.example'
    db.session.commit()
    calls = _double_the_renderers(monkeypatch)

    response = profile_get(app, '/c/books@peer.example', accept='text/html')

    assert response.status_code == 200
    assert len(calls['show_community']) == 1


def test_a_banned_remote_community_is_not_found(app, db_session, monkeypatch):
    """The remote lookup's `banned=False`. Seeded explicitly -- `Community.banned`
    defaults to False, so leaving it alone would assert nothing.

    Note the LOCAL lookup has no such guard; that asymmetry is registered, not
    fixed here.
    """
    site, instance = seed_actors()
    community = make_community(name='books', host='peer.example')
    community.ap_id = 'books@peer.example'
    community.banned = True
    db.session.commit()
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/c/books@peer.example', accept='text/html')

    assert response.status_code == 404


def test_an_unknown_community_returns_404_to_an_activitypub_request(app, db_session, monkeypatch):
    """The not-found path's first arm: `if is_activitypub_request(): abort(404)`,
    ahead of the two authenticated redirects.
    """
    seed_actors()
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/c/nosuch', accept=AP_ACCEPT)

    assert response.status_code == 404


def test_an_unknown_community_returns_404_to_an_anonymous_browser(app, db_session, monkeypatch):
    """The not-found path's final `else`. An anonymous browser gets 404 rather
    than either redirect, because both redirect arms require authentication.
    """
    seed_actors()
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/c/nosuch', accept='text/html')

    assert response.status_code == 404


def test_a_local_only_community_refuses_an_activitypub_request(app, db_session, monkeypatch):
    """First disjunct of `if community.local_only or community.private: abort(403)`.
    `private` is left False so this test isolates `local_only`.
    """
    seed_actors()
    community = make_community(name='books', host='test.piefed.local')
    community.local_only = True
    community.private = False
    db.session.commit()
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/c/books', accept=AP_ACCEPT)

    assert response.status_code == 403


def test_a_private_community_refuses_an_activitypub_request(app, db_session, monkeypatch):
    """Second disjunct. `local_only` is left False, so this test is the only one
    that can kill `community.private`.
    """
    seed_actors()
    community = make_community(name='books', host='test.piefed.local')
    community.local_only = False
    community.private = True
    db.session.commit()
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/c/books', accept=AP_ACCEPT)

    assert response.status_code == 403


def test_a_local_only_community_still_serves_html(app, db_session, monkeypatch):
    """The 403 guard sits INSIDE `if is_activitypub_request()`, so a browser
    still gets the page. Pins the guard's scope, not just its existence.
    """
    seed_actors()
    community = make_community(name='books', host='test.piefed.local')
    community.local_only = True
    db.session.commit()
    calls = _double_the_renderers(monkeypatch)

    response = profile_get(app, '/c/books', accept='text/html')

    assert response.status_code == 200
    assert len(calls['show_community']) == 1


def test_the_community_document_carries_its_federation_contract(app, db_session, monkeypatch):
    """The fields a remote instance actually needs: the id it will store, the
    inbox it will deliver to, the shared inbox, and the public key it will
    verify signatures against. Asserted together because a document missing any
    one of them is unusable, and nothing else in this file asserts them.
    """
    seed_actors()
    community = make_community(name='books', host='test.piefed.local')
    community.public_key = 'PUBKEY'
    db.session.commit()
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/c/books', accept=AP_ACCEPT)

    assert response.status_code == 200
    data = response.json
    assert data['id'] == 'https://test.piefed.local/c/books'
    assert data['inbox'] == 'https://test.piefed.local/c/books/inbox'
    assert data['outbox'] == 'https://test.piefed.local/c/books/outbox'
    assert data['endpoints']['sharedInbox'] == 'https://test.piefed.local/inbox'
    assert data['publicKey']['id'] == 'https://test.piefed.local/c/books#main-key'
    assert data['publicKey']['publicKeyPem'] == 'PUBKEY'


def test_the_community_response_headers_are_set(app, db_session, monkeypatch):
    """Cache-Control, Vary and Link. `Vary: Accept` matters most: the body
    depends on the Accept header, so a shared cache that does not vary on it
    may serve this JSON to a browser. `feed_profile` omits it -- registered as
    a defect, and this test is the community half of the comparison.

    The route sets `Vary: Accept` (routes.py), but Flask-Compress runs as an
    `after_request` registered ahead of the route and appends 'Accept-Encoding'
    to whatever Vary is already on the response (see the comment on
    `register_request_hooks(app)` in app/__init__.py and
    test_request_hooks.py::test_after_request_runs_before_flask_compress, which
    pins the same ordering for HTML responses). So the header actually observed
    here is 'Accept, Accept-Encoding', not the bare 'Accept' the route sets.
    Asserted as the exact, deterministic two-token string rather
    than a substring check, since a bare substring match on 'Accept' would
    also match 'Accept-Encoding' alone and so would not catch the route
    dropping its own 'Accept' token.
    """
    seed_actors()
    make_community(name='books', host='test.piefed.local')
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/c/books', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'public, max-age=30'
    assert response.headers['Vary'] == 'Accept, Accept-Encoding'
    assert 'rel="alternate"' in response.headers['Link']


def test_a_community_description_adds_summary_and_source(app, db_session, monkeypatch):
    """`if community.description_html:` adds two keys. Both asserted -- a test
    checking only `summary` would survive deleting the `source` line.
    """
    seed_actors()
    community = make_community(name='books', host='test.piefed.local')
    community.description_html = '<p>About books</p>'
    community.description = 'About books'
    db.session.commit()
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/c/books', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['summary'] == '<p>About books</p>'
    assert response.json['source'] == {'content': 'About books', 'mediaType': 'text/markdown'}


def test_a_community_without_a_description_omits_both_keys(app, db_session, monkeypatch):
    """The false side. Asserting ABSENCE is what makes the guard killable: a
    mutation making the block unconditional would still satisfy the test above.
    """
    seed_actors()
    make_community(name='books', host='test.piefed.local')
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/c/books', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert 'summary' not in response.json
    assert 'source' not in response.json


def test_a_community_theme_is_included(app, db_session, monkeypatch):
    """`if community.theme:` -- the true side. There is no false-side test
    needed beyond the baseline: the many earlier tests that build the document
    with `theme` left at its declared default of '' (app/models.py:548 --
    `db.Column(db.String(20), default='')`, NOT None) never assert on the
    'theme' key, so this single test carries the whole guard; a false side is
    added below to make the absence explicit and mutation-resistant.
    """
    seed_actors()
    community = make_community(name='books', host='test.piefed.local')
    community.theme = 'dark'
    db.session.commit()
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/c/books', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['theme'] == 'dark'


def test_a_community_without_a_theme_omits_the_key(app, db_session, monkeypatch):
    """The false side of the theme guard, paired with the test above per the
    absence discipline this task follows for every optional field.

    `theme` is set to None EXPLICITLY rather than left alone. Its declared
    default is '' (app/models.py:548), which is falsy, so this test would pass
    without the assignment -- but only by resting on that default, which the
    campaign forbids. Setting it states the premise the test depends on.
    """
    seed_actors()
    community = make_community(name='books', host='test.piefed.local')
    community.theme = None
    db.session.commit()
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/c/books', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert 'theme' not in response.json


def test_an_absolute_community_icon_url_is_used_as_is(app, db_session, monkeypatch):
    """`if icon_image.startswith('http')` -- the true side. `icon_image()` is
    doubled rather than relying on the real one, because this test is about
    the URL branch, not about image storage. `community.icon_id` has a real
    foreign key to `file.id`, so a real (otherwise-empty) File row is seeded
    and its id assigned -- an arbitrary integer like 1 is rejected by the FK
    constraint unless a File with that id exists. The guard above the block
    is `if community.icon_id is not None:`, and the method double alone does
    not enter it.
    """
    seed_actors()
    from app.models import File
    icon_file = File()
    db.session.add(icon_file)
    db.session.commit()
    community = make_community(name='books', host='test.piefed.local')
    community.icon_id = icon_file.id
    db.session.commit()
    _double_the_renderers(monkeypatch)
    monkeypatch.setattr(type(community), 'icon_image',
                        lambda self, size='default': 'https://cdn.example/icon.png')

    response = profile_get(app, '/c/books', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['icon'] == {'type': 'Image', 'url': 'https://cdn.example/icon.png'}


def test_a_relative_community_icon_url_is_prefixed_with_the_server_url(app, db_session, monkeypatch):
    """The false side of the same branch: a stored path is made absolute."""
    seed_actors()
    from app.models import File
    icon_file = File()
    db.session.add(icon_file)
    db.session.commit()
    community = make_community(name='books', host='test.piefed.local')
    community.icon_id = icon_file.id
    db.session.commit()
    _double_the_renderers(monkeypatch)
    monkeypatch.setattr(type(community), 'icon_image',
                        lambda self, size='default': '/static/icon.png')

    response = profile_get(app, '/c/books', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['icon']['url'] == 'https://test.piefed.local/static/icon.png'


def test_a_community_with_no_icon_omits_the_key(app, db_session, monkeypatch):
    """The guard above the whole icon block: `if community.icon_id is not
    None:`. `icon_id` is left unset (None, per `make_community`), so this is
    the absence side for the guard itself, distinct from the absolute/relative
    split inside it.
    """
    seed_actors()
    make_community(name='books', host='test.piefed.local')
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/c/books', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert 'icon' not in response.json


def test_an_absolute_community_header_url_is_used_as_is(app, db_session, monkeypatch):
    """The image block mirrors the icon block exactly; both need covering
    because they are separate code, not a shared helper. `image_id` needs a
    real File row for the same FK reason as `icon_id` above.
    """
    seed_actors()
    from app.models import File
    header_file = File()
    db.session.add(header_file)
    db.session.commit()
    community = make_community(name='books', host='test.piefed.local')
    community.image_id = header_file.id
    db.session.commit()
    _double_the_renderers(monkeypatch)
    monkeypatch.setattr(type(community), 'header_image',
                        lambda self: 'https://cdn.example/header.png')

    response = profile_get(app, '/c/books', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['image'] == {'type': 'Image', 'url': 'https://cdn.example/header.png'}


def test_a_relative_community_header_url_is_prefixed(app, db_session, monkeypatch):
    seed_actors()
    from app.models import File
    header_file = File()
    db.session.add(header_file)
    db.session.commit()
    community = make_community(name='books', host='test.piefed.local')
    community.image_id = header_file.id
    db.session.commit()
    _double_the_renderers(monkeypatch)
    monkeypatch.setattr(type(community), 'header_image', lambda self: '/static/header.png')

    response = profile_get(app, '/c/books', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['image']['url'] == 'https://test.piefed.local/static/header.png'


def test_a_community_with_no_header_image_omits_the_key(app, db_session, monkeypatch):
    """The guard above the whole image block: `if community.image_id is not
    None:`. `image_id` is left unset, mirroring the icon absence test above.
    """
    seed_actors()
    make_community(name='books', host='test.piefed.local')
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/c/books', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert 'image' not in response.json


def test_community_languages_are_listed(app, db_session, monkeypatch):
    """The `for language in community.languages` loop. A community with no
    languages yields an empty list, so this test seeds one to enter the loop
    body -- otherwise the append line is never executed. `Community.languages`
    is a `lazy='dynamic'` relationship (an AppenderQuery), which -- unlike a
    plain dynamic query -- supports `.append()` directly; no `.all()` or list
    conversion is needed to mutate it.
    """
    seed_actors()
    community = make_community(name='books', host='test.piefed.local')
    from app.models import Language
    language = Language(code='en', name='English')
    db.session.add(language)
    db.session.commit()
    community.languages.append(language)
    db.session.commit()
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/c/books', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert {'identifier': 'en', 'name': 'English'} in response.json['language']


def test_a_community_with_no_languages_gets_an_empty_language_list(app, db_session, monkeypatch):
    """The unconditional `actor_data['language'] = []` line runs regardless of
    the loop, so a community with no attached languages still gets the key --
    just with an empty list rather than the key being absent. This is not an
    absence test paired with the one above; it pins that 'language' is always
    present, unlike every other optional field in this file.
    """
    seed_actors()
    make_community(name='books', host='test.piefed.local')
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/c/books', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['language'] == []


def test_a_local_feed_is_resolved_by_name(app, db_session, monkeypatch):
    """The local branch looks up `name=actor.lower(), ap_id=None`.
    `make_local_feed` is required: `make_feed` sets `ap_id` unconditionally, so
    no feed it builds is reachable here (tests/README.md, sub-project 8).
    """
    seed_actors()
    make_local_feed('news', public=True)
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/f/news', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['type'] == 'Feed'


def test_a_two_segment_feed_path_joins_the_owner_into_the_name(app, db_session, monkeypatch):
    """The second route, `/f/<actor>/<feed_owner>`, concatenates the two
    segments with a '/' BEFORE the lookup -- so the feed's stored name must
    contain the slash. Nothing else in this file exercises that route.
    """
    seed_actors()
    make_local_feed('news/alice', public=True)
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/f/news/alice', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['type'] == 'Feed'


def test_a_remote_feed_refuses_an_activitypub_request(app, db_session, monkeypatch):
    """Mirrors community_profile's 400. Both have this guard; user_profile does
    not -- the asymmetry the spec registers.
    """
    seed_actors()
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/f/news@peer.example', accept=AP_ACCEPT)

    assert response.status_code == 400


def test_a_remote_feed_serves_html_to_a_browser(app, db_session, monkeypatch):
    """The same remote path WITHOUT an AP Accept skips the 400 and looks the
    feed up by `ap_id`, filtered `banned=False`. Mirrors
    test_a_remote_community_serves_html_to_a_browser.

    `make_local_feed` leaves `ap_id` None, so it is set explicitly here to the
    exact string the remote lookup compares against (`ap_id=actor.lower()`,
    where `actor` is the full `news@peer.example` path segment) -- the same
    way the community factory's remote tests set `ap_id` explicitly.
    """
    seed_actors()
    feed = make_local_feed('news', public=True)
    feed.ap_id = 'news@peer.example'
    db.session.commit()
    calls = _double_the_renderers(monkeypatch)

    response = profile_get(app, '/f/news@peer.example', accept='text/html')

    assert response.status_code == 200
    assert len(calls['show_feed']) == 1


def test_a_banned_remote_feed_is_not_found(app, db_session, monkeypatch):
    """The remote lookup's `banned=False`. Seeded explicitly -- `Feed.banned`
    defaults to False, so leaving it alone would assert nothing. Mirrors
    test_a_banned_remote_community_is_not_found.

    Differs from test_a_remote_feed_serves_html_to_a_browser in EXACTLY one
    respect, `banned`, so this test's kill of the `banned=False` clause is not
    confounded with the host, the ap_id, or anything else.

    Note the LOCAL lookup has no such guard; that asymmetry is registered for
    community_profile above and holds here too, not fixed in either place.
    """
    seed_actors()
    feed = make_local_feed('news', public=True)
    feed.ap_id = 'news@peer.example'
    feed.banned = True
    db.session.commit()
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/f/news@peer.example', accept='text/html')

    assert response.status_code == 404


def test_a_non_public_feed_refuses_an_activitypub_request(app, db_session, monkeypatch):
    """`if not feed.public: abort(403)`. `public=False` is passed EXPLICITLY --
    it is also the column default (app/models.py), so relying on the default
    would make the test's premise invisible.
    """
    seed_actors()
    make_local_feed('news', public=False)
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/f/news', accept=AP_ACCEPT)

    assert response.status_code == 403


def test_a_non_public_feed_still_serves_html(app, db_session, monkeypatch):
    """The 403 sits inside the AP branch, so a browser still gets the page --
    the same scoping community_profile has for local_only.
    """
    seed_actors()
    make_local_feed('news', public=False)
    calls = _double_the_renderers(monkeypatch)

    response = profile_get(app, '/f/news', accept='text/html')

    assert response.status_code == 200
    assert len(calls['show_feed']) == 1


def test_an_unknown_feed_is_404(app, db_session, monkeypatch):
    seed_actors()
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/f/nosuch', accept=AP_ACCEPT)

    assert response.status_code == 404


def test_the_feed_document_carries_its_federation_contract(app, db_session, monkeypatch):
    seed_actors()
    feed = make_local_feed('news', public=True)
    feed.public_key = 'FEEDKEY'
    db.session.commit()
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/f/news', accept=AP_ACCEPT)

    assert response.status_code == 200
    data = response.json
    assert data['id'] == 'https://test.piefed.local/f/news'
    assert data['inbox'] == 'https://test.piefed.local/f/news/inbox'
    assert data['following'] == 'https://test.piefed.local/f/news/following'
    assert data['endpoints']['sharedInbox'] == 'https://test.piefed.local/inbox'
    assert data['publicKey']['publicKeyPem'] == 'FEEDKEY'


def test_the_feed_response_omits_the_vary_header(app, db_session, monkeypatch):
    """PINS a defect. `community_profile` and `user_profile` both set
    `Vary: Accept`; `feed_profile` does not.

    The response body depends entirely on the Accept header -- this same URL
    returns ActivityPub JSON or an HTML page. Without `Vary`, any shared cache
    between this instance and its peers may store one and serve it for the
    other: a browser gets the JSON, or a remote instance gets the HTML and
    fails to parse an actor it needs to federate with.

    Cache-Control and Link ARE set, so this is an omission in an otherwise
    complete header block, not a block nobody wrote.

    NOTE THE ASSERTION SHAPE. A `Vary` header is always present, because
    Flask-Compress registers an `after_request` that appends `Accept-Encoding`
    to every response (app/__init__.py -- `compress.init_app(app)`, and the
    comment there explains the ordering). So the defect is NOT a missing Vary
    header; it is that `Accept` is missing FROM it. `community_profile`, which
    does set it, yields 'Accept, Accept-Encoding'; this endpoint yields
    'Accept-Encoding' alone. Asserting `'Vary' not in response.headers` would
    fail against a real response and prove nothing about the defect.
    """
    seed_actors()
    make_local_feed('news', public=True)
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/f/news', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'public, max-age=5'
    assert 'rel="alternate"' in response.headers['Link']
    assert response.headers['Vary'] == 'Accept-Encoding'
    assert 'Accept,' not in response.headers['Vary']


def test_a_local_feed_with_a_non_null_ap_id_is_not_found(app, db_session, monkeypatch):
    """The local lookup's `ap_id=None` clause, isolated the same way
    `test_a_local_community_with_a_non_null_ap_id_is_not_found` isolates
    community_profile's equivalent clause. `make_local_feed` never sets
    `ap_id` (it stays None), so none of the positive tests above can
    distinguish filtering on `ap_id=None` from not filtering on it at all --
    dropping that clause from the query changes nothing there. This test sets
    `ap_id` explicitly to a non-null value on an otherwise-matching feed, so
    the filter is the ONLY thing standing between it and a 200.

    Added during Step 3 of this task: dropping `ap_id=None` from the LOCAL
    lookup in feed_profile is unkillable by every other test in this file,
    because `make_local_feed` leaves `ap_id` None by construction -- the same
    pattern already recorded for community_profile and elsewhere in this
    campaign (this task's report has the count). This test is the fix: it
    supplies the contrary case the mutation needs to be observable.
    """
    seed_actors()
    feed = make_local_feed('news', public=True)
    feed.ap_id = 'news@test.piefed.local'
    db.session.commit()
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/f/news', accept=AP_ACCEPT)

    assert response.status_code == 404


# NOT TESTED HERE (already covered elsewhere -- see this task's report):
#
# - user_profile's AP-JSON happy path for a local user found by bare username,
#   its Accept-driven content_type, and the suppressed session cookie:
#   tests/test_request_hooks.py::test_activity_json_response_does_not_set_a_session_cookie
# - user_profile's two lookup branches (handle-with-@ and bare-username-then-
#   ap_profile_id-fallback) returning None, gated three ways (anonymous,
#   AP-Accept, banned-instance-exception-not-500):
#   tests/test_remote_handle_resolution.py (all four tests)
#
def _seed_file():
    """A bare File row, for an icon_id/image_id foreign key.

    `icon_id` and `image_id` are real foreign keys to `file.id` (both Feed's
    and Community's) -- an arbitrary integer like 1 is rejected by the FK
    constraint unless a File with that id exists. Task 4 seeded this inline,
    once per icon/header test, with `from app.models import File; f =
    File(); db.session.add(f); db.session.commit()`; this helper is the same
    four lines, factored out for Task 6's four callers. It did not already
    exist under this name despite the brief listing it as an interface
    already in this file -- see this task's report.
    """
    from app.models import File
    file = File()
    db.session.add(file)
    db.session.commit()
    return file


def test_an_absolute_feed_icon_url_is_used_as_is(app, db_session, monkeypatch):
    """`if icon_image.startswith('http')` -- the true side. `icon_image()` is
    doubled rather than seeding a real File row's path: this test is about the
    URL branch, not about image storage. `feed.icon_id` has a real foreign key
    to `file.id`, so a real (otherwise-empty) File row is seeded and its id
    assigned. The guard above the block is `if feed.icon_id is not None:`, and
    the method double alone does not enter it.
    """
    seed_actors()
    icon_file = _seed_file()
    feed = make_local_feed('news', public=True)
    feed.icon_id = icon_file.id
    db.session.commit()
    _double_the_renderers(monkeypatch)
    monkeypatch.setattr(type(feed), 'icon_image',
                        lambda self, size='default': 'https://cdn.example/icon.png')

    response = profile_get(app, '/f/news', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['icon'] == {'type': 'Image', 'url': 'https://cdn.example/icon.png'}


def test_a_relative_feed_icon_url_is_prefixed_with_the_server_url(app, db_session, monkeypatch):
    """The false side of the same branch: a stored path is made absolute."""
    seed_actors()
    icon_file = _seed_file()
    feed = make_local_feed('news', public=True)
    feed.icon_id = icon_file.id
    db.session.commit()
    _double_the_renderers(monkeypatch)
    monkeypatch.setattr(type(feed), 'icon_image',
                        lambda self, size='default': '/static/icon.png')

    response = profile_get(app, '/f/news', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['icon']['url'] == 'https://test.piefed.local/static/icon.png'


def test_an_absolute_feed_header_url_is_used_as_is(app, db_session, monkeypatch):
    """The image block is separate code from the icon block, not a shared
    helper, so both need covering. `Feed.header_image` takes no `size`
    argument (unlike `icon_image`), confirmed by reading app/models.py before
    writing this double.
    """
    seed_actors()
    header_file = _seed_file()
    feed = make_local_feed('news', public=True)
    feed.image_id = header_file.id
    db.session.commit()
    _double_the_renderers(monkeypatch)
    monkeypatch.setattr(type(feed), 'header_image', lambda self: 'https://cdn.example/h.png')

    response = profile_get(app, '/f/news', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['image'] == {'type': 'Image', 'url': 'https://cdn.example/h.png'}


def test_a_relative_feed_header_url_is_prefixed(app, db_session, monkeypatch):
    """The false side of the header-image branch, mirroring the icon pair."""
    seed_actors()
    header_file = _seed_file()
    feed = make_local_feed('news', public=True)
    feed.image_id = header_file.id
    db.session.commit()
    _double_the_renderers(monkeypatch)
    monkeypatch.setattr(type(feed), 'header_image', lambda self: '/static/h.png')

    response = profile_get(app, '/f/news', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['image']['url'] == 'https://test.piefed.local/static/h.png'


def test_a_feed_with_no_icon_omits_the_key(app, db_session, monkeypatch):
    """The outer guard `if feed.icon_id is not None:` (routes.py:2697),
    distinct from the `startswith('http')` split inside it. `icon_id` has no
    declared default (app/models.py:4057, a bare `db.Column(db.Integer,
    db.ForeignKey('file.id'))`), so leaving it unset on `make_local_feed` is
    None-by-construction, not a default the test happens to rest on.

    This matters because `Feed.icon_image()` (app/models.py:4097) itself
    guards on `self.icon_id is not None` and falls through to
    '/static/images/1px.gif' rather than raising when it is None -- so a
    mutation deleting the outer guard in `feed_profile` would still call
    `icon_image()` successfully and add a harmless-looking `icon` key with
    that placeholder URL, undetected by the four tests above (all of which
    set `icon_id`). This test is what makes that mutation observable.
    """
    seed_actors()
    make_local_feed('news', public=True)
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/f/news', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert 'icon' not in response.json


def test_a_feed_with_no_header_image_omits_the_key(app, db_session, monkeypatch):
    """The outer guard `if feed.image_id is not None:` (routes.py:2709),
    mirroring the icon guard above. `image_id` likewise has no declared
    default (app/models.py:4058), so this rests on None-by-construction.

    `Feed.header_image()` (app/models.py:4124) guards on `self.image_id is
    not None` internally and falls through to `''` rather than raising, so
    -- exactly as with the icon guard -- deleting this outer guard would add
    an `image` key built from that empty-string placeholder to every
    response, undetected by the header tests above (all of which set
    `image_id`).
    """
    seed_actors()
    make_local_feed('news', public=True)
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/f/news', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert 'image' not in response.json


def test_a_feed_description_adds_summary_and_source(app, db_session, monkeypatch):
    """`if feed.description_html:` adds two keys. Both asserted -- a test
    checking only `summary` would survive deleting the `source` line, the same
    reasoning as the community equivalent above.
    """
    seed_actors()
    feed = make_local_feed('news', public=True)
    feed.description_html = '<p>News</p>'
    feed.description = 'News'
    db.session.commit()
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/f/news', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['summary'] == '<p>News</p>'
    assert response.json['source'] == {'content': 'News', 'mediaType': 'text/markdown'}


def test_a_feed_without_a_description_omits_both_keys(app, db_session, monkeypatch):
    """The false side. `description_html` has no declared default (a bare
    `db.Column(db.Text)`, no `default=`), so it is left alone -- None -- and
    the absence rests on that None, not on any falsy declared default.
    """
    seed_actors()
    make_local_feed('news', public=True)
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/f/news', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert 'summary' not in response.json
    assert 'source' not in response.json


def test_a_feeds_child_feeds_are_listed_by_profile_id(app, db_session, monkeypatch):
    """The `for child_feed in feed.children.all()` loop. A childless feed yields
    an empty list, so a child must be seeded to execute the append.

    `Feed.children` (app/models.py:4091) is the backref of `Feed.parent`:
    `parent = db.relationship('Feed', remote_side=[id],
    backref=db.backref('children', lazy='dynamic'))`. The FK it walks is the
    plain `parent_feed_id` column (app/models.py:4064,
    `db.Column(db.Integer, db.ForeignKey('feed.id'), index=True)`) -- there is
    no association table. Setting `child.parent_feed_id = parent.id` directly
    and committing is therefore sufficient to attach it; `feed.children` is
    `lazy='dynamic'`, matching `Community.languages`, so `.all()` in the route
    (not `.append()` here -- that is on the child's own FK, not the parent's
    collection) is what the route already calls.
    """
    seed_actors()
    parent = make_local_feed('news', public=True)
    child = make_local_feed('sports', public=True)
    child.parent_feed_id = parent.id
    db.session.commit()
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/f/news', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert child.ap_profile_id in response.json['childFeeds']


def test_a_childless_feed_lists_no_child_feeds(app, db_session, monkeypatch):
    """The loop's zero-iteration side: the key exists and is empty, because
    `actor_data['childFeeds'] = []` runs unconditionally before the loop.
    """
    seed_actors()
    make_local_feed('news', public=True)
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/f/news', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['childFeeds'] == []


# feed_profile's remote branch's `banned=False` filter and its two-segment
# route's slash-joined name are covered above (Task 5). icon/image, description,
# and childFeeds are covered above (Task 6). Any remaining feed_profile fields
# (languages) and feed_outbox/feed_following are left to later tasks in this
# sub-project.
#
# ---------------------------------------------------------------------------
# user_profile (Task 7): its HEAD branch, its ap_profile_id fallback lookup,
# its HTML delegation, and its bot/Service type branch.
#
# NOT TESTED HERE (already covered elsewhere -- see this task's report):
# - the AP-JSON happy path for a local user found by bare username, its
#   Accept-driven content_type, and the suppressed session cookie:
#   tests/test_request_hooks.py::test_activity_json_response_does_not_set_a_session_cookie
# - both lookup branches (handle-with-@ and bare-username) returning None,
#   gated three ways (anonymous, AP-Accept, banned-instance-exception-not-500),
#   and the resulting resolve_remote_handle/search_for_user call:
#   tests/test_remote_handle_resolution.py (all four tests)
# ---------------------------------------------------------------------------

def test_a_head_request_for_an_activitypub_client_returns_an_empty_json_body(app, db_session, monkeypatch):
    """`user_profile` is the ONLY one of the three whose route accepts HEAD and
    the only one with an explicit HEAD branch -- an asymmetry the spec registers.
    The branch returns `jsonify('')` with the AP content type and no actor
    document at all -- critically, no Cache-Control, Vary or Link headers, which
    the real AP-JSON branch below (routes.py) sets explicitly via
    `resp.headers.set(...)`.

    That header absence is what actually proves the explicit branch ran, rather
    than the GET path running and Werkzeug silently emptying the body for a HEAD
    request. `Response.get_app_iter()` (werkzeug/wrappers/response.py) forces an
    empty body whenever `environ['REQUEST_METHOD'] == 'HEAD'`, REGARDLESS of
    what the view returned -- so if this route's own HEAD branch were deleted,
    a HEAD request would fall through to the exact same `is_activitypub_request()`
    branch a GET takes, build the full actor_data JSON, set Cache-Control/Vary/
    Link on it, and Werkzeug would still empty the body before it reached the
    test client. `response.content_type` and `response.data` are identical
    (application/activity+json, b'') in both the genuine-branch and
    Werkzeug-stripped-GET cases -- only the extra headers differ. Verified
    empirically in Step 3: deleting the branch does NOT change status_code or
    content_type, only the Cache-Control assertion below dies (see this task's
    report).
    """
    site, instance = seed_actors()
    make_user(instance, 'alice', local=True)
    _double_the_renderers(monkeypatch)

    with app.test_client() as client:
        response = client.head('/u/alice', headers={'Accept': AP_ACCEPT})

    assert response.status_code == 200
    assert response.content_type == 'application/activity+json'
    assert 'Cache-Control' not in response.headers


def test_a_head_request_from_a_browser_returns_an_empty_string(app, db_session, monkeypatch):
    """The HEAD branch's else: no content type is set, and `show_profile` is
    never reached -- asserted through the double's call list, which is what
    distinguishes this from the browser GET path. Unlike the AP-Accept HEAD
    test above, this one does not need a header-absence trick: if the branch
    were deleted, a HEAD request with a browser Accept would fall to
    `is_activitypub_request()`'s false arm and call `show_profile(user)`,
    which the double records -- so `calls['show_profile'] == []` directly
    proves the branch, not Werkzeug's HEAD body-stripping, produced this
    response.
    """
    site, instance = seed_actors()
    make_user(instance, 'alice', local=True)
    calls = _double_the_renderers(monkeypatch)

    with app.test_client() as client:
        response = client.head('/u/alice', headers={'Accept': 'text/html'})

    assert response.status_code == 200
    assert calls['show_profile'] == []


def test_a_user_is_resolved_by_ap_profile_id_when_the_name_does_not_match(app, db_session, monkeypatch):
    """The second local lookup, reached only when the `user_name` query returns
    None. The user's `user_name` deliberately differs from the path segment, so
    only the `ap_profile_id` fallback can find them.

    Asserts `id` (built from `user.public_url()`, which falls back to
    `.../u/{user.user_name}` -- i.e. 'alice', not the requested path segment
    'bob') rather than `type`, deviating from this task's brief. The brief's
    literal assertion was `response.json['type'] == 'Person'`, which rests on
    `User.bot`'s declared default (False, app/models.py:1007) without setting
    it explicitly -- exactly the pattern this campaign's global constraints
    forbid ("No assertion may rest on a column's declared default... User.bot
    in particular: check it") and the reason Task 4 was rejected. Asserting on
    `id` instead proves the SAME thing the brief's assertion was reaching for
    (the fallback found the right row, not just any row) without resting on
    that default, and more strongly: 'Person' could results from a coincidence
    of the ternary's default, whereas 'alice' appearing in the id can only come
    from the actual matched User object.
    """
    site, instance = seed_actors()
    user = make_user(instance, 'alice', local=True)
    user.ap_profile_id = 'https://test.piefed.local/u/bob'
    db.session.commit()
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/u/bob', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['id'] == 'https://test.piefed.local/u/alice'


def test_a_browser_request_for_a_user_reaches_show_profile(app, db_session, monkeypatch):
    """The HTML delegation, which nothing else in this file or the two existing
    files asserts. `show_profile` is doubled, so this asserts the delegation
    happened and with which user -- not what the template rendered.
    """
    site, instance = seed_actors()
    user = make_user(instance, 'alice', local=True)
    user_id = user.id                    # captured BEFORE the request: see Task 1
    calls = _double_the_renderers(monkeypatch)

    response = profile_get(app, '/u/alice', accept='text/html')

    assert response.status_code == 200
    assert len(calls['show_profile']) == 1
    assert calls['show_profile'][0].id == user_id


def test_a_bot_user_is_typed_as_a_service(app, db_session, monkeypatch):
    """`"type": "Person" if not user.bot else "Service"`. Nothing else covers
    the Service side; `User.bot` is set explicitly rather than left to default
    (its declared default is False, app/models.py:1007 -- see this task's
    report for why that matters).
    """
    site, instance = seed_actors()
    user = make_user(instance, 'alice', local=True)
    user.bot = True
    db.session.commit()
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/u/alice', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['type'] == 'Service'


def test_a_non_bot_user_is_typed_as_a_person(app, db_session, monkeypatch):
    """The false side of the same ternary, added beyond this task's brief (which
    specified only the Service side above). Without this test, a mutation
    collapsing the ternary to an unconditional 'Service' would still satisfy
    test_a_bot_user_is_typed_as_a_service and survive undetected -- the same
    absence-testing discipline this file already applies to every other
    optional/branching field (theme, description, icon, image, ...).

    `user.bot` is set to False EXPLICITLY rather than left unset. Its declared
    default is also False (app/models.py:1007), so leaving it alone would rest
    the assertion on that default -- forbidden by this campaign's constraints
    ("User.bot in particular: check it"). Setting it states the premise this
    test depends on, the same way test_a_community_without_a_theme_omits_the_key
    sets `theme = None` explicitly despite '' already being falsy.
    """
    site, instance = seed_actors()
    user = make_user(instance, 'alice', local=True)
    user.bot = False
    db.session.commit()
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/u/alice', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['type'] == 'Person'
