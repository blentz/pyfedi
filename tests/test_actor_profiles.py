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
# make_local_feed and make_feed and their distinction (ap_id None vs. always
# set) are exercised by later tasks in this sub-project, not here -- this file
# only imports make_local_feed to keep the import list identical to what those
# tasks add to, per the brief.
