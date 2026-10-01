"""tests/test_webfinger.py"""
from app import db
from app.activitypub import routes as activitypub_routes
from app.models import utcnow
from tests.factories import make_community, make_feed, make_local_feed, make_site, make_user, seed_community_owner


def webfinger_get(app, resource=None, user_agent=None):
    """GET /.well-known/webfinger through the real route.

    Driving the route rather than calling `process_webfinger_request` directly
    is what exercises the allowlist and ban guards, and it is the only way the
    handler's status codes are observable at all: `abort()` raises an
    HTTPException that only a request context turns into a response, so a
    direct call would see an exception rather than the 400 or 404 a peer gets.

    `user_agent` is how the requesting domain is set: `requestor_domain()`
    (app/utils.py) parses the URL out of a `+`-delimited User-Agent comment and
    returns its host. No double is needed or wanted.
    """
    headers = {}
    if user_agent is not None:
        headers['User-Agent'] = user_agent
    query = f'?resource={resource}' if resource is not None else ''
    with app.test_client() as client:
        return client.get(f'/.well-known/webfinger{query}', headers=headers)


def seed_local_actors(host='peer.example'):
    """A Site row (id 1, needed by `before_request`'s `get_site_as_dict()`
    (`app/utils.py:5414-5418`, `db.session.query(Site).get(1)`), which runs on
    every request before any view function -- `webfinger`'s own `g.site`
    lookup (`app/activitypub/routes.py:59-60`) is dead code by the time the
    route body runs (D147) -- plus the Instance and User that id-1-hardcoding
    factories require.

    `seed_community_owner` rather than a bare `make_instance`: it creates BOTH
    the Instance (id 1) and a local User (id 1), and `make_community` hardcodes
    `user_id=1`/`instance_id=1` against real foreign keys. A Site-and-Instance-only
    seed would fail the FK the moment any test built a Community.

    The 'communityowner' user it creates is local (ap_id None) and so is
    webfinger-resolvable, but no test in this file queries that name.
    """
    site = make_site()
    instance = seed_community_owner(host)
    db.session.commit()
    return site, instance


def test_a_request_with_no_requesting_domain_skips_both_guards(app, db_session, monkeypatch):
    """The walrus `if requesting_domain := requestor_domain():` is falsy when the
    User-Agent carries no `+URL` comment, so neither the allowlist nor the ban
    check runs. Proved by doubling BOTH guards to refuse everything and still
    getting a non-403: if either ran, this would be 403.
    """
    seed_local_actors()
    monkeypatch.setattr(activitypub_routes, 'instance_banned', lambda domain: True)
    monkeypatch.setattr(activitypub_routes, 'instance_allowed', lambda domain: False)

    response = webfinger_get(app, resource='acct:nobody@test.piefed.local',
                             user_agent='plain-agent-without-a-url')

    assert response.status_code != 403


def test_a_banned_requesting_domain_is_refused(app, db_session, monkeypatch):
    """The `else` arm: allowlist mode off, so `instance_banned` decides.
    `Site.allowlist_mode` defaults to 0 (app/models.py:3955) but is set here
    explicitly -- the campaign forbids resting on a declared default.
    """
    site, instance = seed_local_actors()
    site.allowlist_mode = 0
    db.session.commit()
    monkeypatch.setattr(activitypub_routes, 'get_setting', lambda name, default=None: False)
    seen = {}

    def fake_banned(domain):
        seen['domain'] = domain
        return True

    monkeypatch.setattr(activitypub_routes, 'instance_banned', fake_banned)

    response = webfinger_get(app, resource='acct:nobody@test.piefed.local',
                             user_agent='Mastodon/4.2 (+https://blocked.example)')

    assert response.status_code == 403
    assert seen['domain'] == 'blocked.example'


def test_an_unbanned_requesting_domain_is_served(app, db_session, monkeypatch):
    """The same arm, other side. Pairs with the test above so the ban guard
    cannot be deleted without a failure.
    """
    site, instance = seed_local_actors()
    site.allowlist_mode = 0
    db.session.commit()
    monkeypatch.setattr(activitypub_routes, 'get_setting', lambda name, default=None: False)
    monkeypatch.setattr(activitypub_routes, 'instance_banned', lambda domain: False)

    response = webfinger_get(app, resource='acct:nobody@test.piefed.local',
                             user_agent='Mastodon/4.2 (+https://friendly.example)')

    assert response.status_code != 403


def test_allowlist_intense_refuses_a_domain_not_on_the_list(app, db_session, monkeypatch):
    """Both conjuncts true: `get_setting('use_allowlist')` AND
    `allowlist_mode == ALLOWLIST_INTENSE` (2, app/constants.py:142). This arm
    consults `instance_allowed`, not `instance_banned` -- `instance_banned` is
    doubled to False here so that a mutation collapsing the branch would be
    caught rather than masked.
    """
    site, instance = seed_local_actors()
    site.allowlist_mode = 2
    db.session.commit()
    monkeypatch.setattr(activitypub_routes, 'get_setting', lambda name, default=None: True)
    monkeypatch.setattr(activitypub_routes, 'instance_allowed', lambda domain: False)
    monkeypatch.setattr(activitypub_routes, 'instance_banned', lambda domain: False)

    response = webfinger_get(app, resource='acct:nobody@test.piefed.local',
                             user_agent='Mastodon/4.2 (+https://stranger.example)')

    assert response.status_code == 403


def test_allowlist_intense_serves_a_domain_on_the_list(app, db_session, monkeypatch):
    """The permitted side of the same arm."""
    site, instance = seed_local_actors()
    site.allowlist_mode = 2
    db.session.commit()
    monkeypatch.setattr(activitypub_routes, 'get_setting', lambda name, default=None: True)
    monkeypatch.setattr(activitypub_routes, 'instance_allowed', lambda domain: True)
    monkeypatch.setattr(activitypub_routes, 'instance_banned', lambda domain: True)

    response = webfinger_get(app, resource='acct:nobody@test.piefed.local',
                             user_agent='Mastodon/4.2 (+https://allowed.example)')

    assert response.status_code != 403


def test_allowlist_mode_below_intense_falls_through_to_the_ban_check(app, db_session, monkeypatch):
    """Isolates the SECOND conjunct: `use_allowlist` is on but the mode is 1
    (strong), not 2 (intense), so the `else` arm runs. `instance_allowed` is
    doubled to refuse and `instance_banned` to permit -- so the response proves
    which delegate actually decided, not merely that the request succeeded.
    """
    site, instance = seed_local_actors()
    site.allowlist_mode = 1
    db.session.commit()
    monkeypatch.setattr(activitypub_routes, 'get_setting', lambda name, default=None: True)
    monkeypatch.setattr(activitypub_routes, 'instance_allowed', lambda domain: False)
    monkeypatch.setattr(activitypub_routes, 'instance_banned', lambda domain: False)

    response = webfinger_get(app, resource='acct:nobody@test.piefed.local',
                             user_agent='Mastodon/4.2 (+https://strongmode.example)')

    assert response.status_code != 403


def test_use_allowlist_off_falls_through_to_the_ban_check_even_at_intense_mode(app, db_session, monkeypatch):
    """Isolates the FIRST conjunct: `allowlist_mode` is 2 (intense) but
    `get_setting('use_allowlist')` is doubled to `False`, so `and` short-
    circuits and the `else` arm runs. `instance_allowed` is doubled to refuse
    and `instance_banned` to permit -- so a mutation that drops
    `get_setting('use_allowlist') and` (leaving only the mode check) would
    wrongly take the `if` arm and see instance_allowed's refusal, turning
    this into a 403.
    """
    site, instance = seed_local_actors()
    site.allowlist_mode = 2
    db.session.commit()
    monkeypatch.setattr(activitypub_routes, 'get_setting', lambda name, default=None: False)
    monkeypatch.setattr(activitypub_routes, 'instance_allowed', lambda domain: False)
    monkeypatch.setattr(activitypub_routes, 'instance_banned', lambda domain: False)

    response = webfinger_get(app, resource='acct:nobody@test.piefed.local',
                             user_agent='Mastodon/4.2 (+https://offmode.example)')

    assert response.status_code != 403


def test_a_request_without_a_resource_argument_is_404(app, db_session):
    """`abort(404)` when `request.args.get('resource')` is falsy. No User-Agent,
    so the access guards are skipped and this isolates the resource check.
    """
    seed_local_actors()

    response = webfinger_get(app)

    assert response.status_code == 404


def test_an_acct_resource_resolves_a_local_user(app, db_session):
    """The `'acct:' in query` branch. `make_user(..., local=True)` is required:
    webfinger filters `ap_id=None`, and a remote user (the factory's default)
    is invisible to it.
    """
    site, instance = seed_local_actors()
    make_user(instance, 'alice', local=True)

    response = webfinger_get(app, resource='acct:alice@test.piefed.local')

    assert response.status_code == 200
    assert response.content_type == 'application/jrd+json'
    assert response.json['subject'] == 'acct:alice@test.piefed.local'


def test_a_url_resource_resolves_by_its_last_path_segment(app, db_session):
    """The `elif 'https:' in query or 'http:' in query` branch, which takes
    `query.split('/')[-1]`. Reached only when 'acct:' is absent from the whole
    string -- the acct test above is its pair.
    """
    site, instance = seed_local_actors()
    make_user(instance, 'alice', local=True)

    response = webfinger_get(app, resource='https://test.piefed.local/u/alice')

    assert response.status_code == 200
    assert response.json['subject'] == 'acct:alice@test.piefed.local'


def test_a_plain_http_url_resource_also_resolves(app, db_session):
    """The second disjunct, `'http:' in query`. Isolated from `'https:'` by
    using a scheme that contains 'http:' but not 'https:'.
    """
    site, instance = seed_local_actors()
    make_user(instance, 'alice', local=True)

    response = webfinger_get(app, resource='http://test.piefed.local/u/alice')

    assert response.status_code == 200
    assert response.content_type == 'application/jrd+json'
    assert response.json['subject'] == 'acct:alice@test.piefed.local'


def test_a_malformed_resource_is_400(app, db_session):
    """The parse chain's `else` arm: neither 'acct:' nor a scheme appears, so
    no branch can extract an actor and the request is malformed. RFC 7033 wants
    400 for that, and 400 is also what separates it from the 404 a well-formed
    resource that names no local actor gets (test_an_unknown_actor_is_404) and
    from the 200 a successful lookup gets.

    Only the status is asserted. The old bare-string return carried the sentence
    'Webfinger regex failed to match'; it is kept as the `description=` on the
    abort, but it does not reach the wire: flask_smorest's app-wide
    HTTPException handler (registered by `rest_api = Api()`, app/__init__.py:97)
    renders `{"code": 400, "status": "Bad Request"}` from its own `e.data` and
    ignores werkzeug's `description`. Asserting the sentence's ABSENCE would
    freeze that accident in place, so this test asserts neither presence nor
    absence of a body.
    """
    seed_local_actors()

    response = webfinger_get(app, resource='alice-with-no-scheme')

    assert response.status_code == 400


def test_the_instance_actor_is_served_from_the_special_case(app, db_session):
    """`actor == current_app.config['SERVER_NAME']` short-circuits every
    database lookup and returns a fixed JRD pointing at /actor. No User, Community
    or Feed row exists in this test, which is what proves the short-circuit: any
    other path would 404 on an unknown actor (app/activitypub/routes.py,
    `if object is None: abort(404)`).
    """
    seed_local_actors()

    response = webfinger_get(app, resource='acct:test.piefed.local@test.piefed.local')

    assert response.status_code == 200
    assert response.content_type == 'application/jrd+json'
    assert response.json['subject'] == 'acct:test.piefed.local@test.piefed.local'
    assert response.json['aliases'] == ['https://test.piefed.local/actor']
    assert response.headers['Cache-Control'] == 'public, max-age=15'
    assert response.headers['Access-Control-Allow-Origin'] == '*'


def test_the_instance_actor_links_name_the_profile_page_and_the_actor(app, db_session):
    """Both entries of the special case's `links` list, which no other test
    asserts. `rel` values are the contract remote software matches on.
    """
    seed_local_actors()

    response = webfinger_get(app, resource='acct:test.piefed.local@test.piefed.local')

    assert response.status_code == 200
    rels = {link['rel']: link for link in response.json['links']}
    assert rels['http://webfinger.net/rel/profile-page']['type'] == 'text/html'
    assert rels['http://webfinger.net/rel/profile-page']['href'] == 'https://test.piefed.local/about'
    assert rels['self']['type'] == 'application/activity+json'
    assert rels['self']['href'] == 'https://test.piefed.local/actor'


def test_a_user_is_matched_case_insensitively(app, db_session):
    """`func.lower(User.user_name) == actor.strip().lower()`. The stored name is
    lowercase and the query is mixed case, so a case-sensitive comparison would
    fail this.
    """
    site, instance = seed_local_actors()
    make_user(instance, 'alice', local=True)

    response = webfinger_get(app, resource='acct:ALICE@test.piefed.local')

    assert response.status_code == 200
    assert response.json['links'][1]['properties'][
        'https://www.w3.org/ns/activitystreams#type'] == 'Person'


def test_a_user_is_matched_by_alt_user_name(app, db_session):
    """The second disjunct of the `or_(...)`. `user_name` deliberately does NOT
    match the query, so this test is the only one that can kill that disjunct.
    """
    site, instance = seed_local_actors()
    user = make_user(instance, 'alice', local=True)
    user.alt_user_name = 'alice_alt'
    db.session.commit()

    response = webfinger_get(app, resource='acct:alice_alt@test.piefed.local')

    assert response.status_code == 200
    assert response.content_type == 'application/jrd+json'
    assert response.json['subject'] == 'acct:alice_alt@test.piefed.local'


def test_a_deleted_user_is_not_served(app, db_session):
    """`deleted=False`. Seeded explicitly to True -- `User.deleted` defaults to
    False (app/models.py:978), so leaving it alone would assert nothing.
    """
    site, instance = seed_local_actors()
    user = make_user(instance, 'alice', local=True)
    user.deleted = True
    db.session.commit()

    response = webfinger_get(app, resource='acct:alice@test.piefed.local')

    assert response.status_code == 404


def test_a_banned_user_is_not_served(app, db_session):
    """`banned=False`. `make_user` sets banned=False explicitly, so flipping it
    here is a real change of state rather than a default being restated.
    """
    site, instance = seed_local_actors()
    user = make_user(instance, 'alice', local=True)
    user.banned = True
    db.session.commit()

    response = webfinger_get(app, resource='acct:alice@test.piefed.local')

    assert response.status_code == 404


def test_a_remote_user_is_not_served(app, db_session):
    """`ap_id=None`. A remote user is `make_user`'s DEFAULT (local=False), which
    is why every positive test in this file passes local=True. This instance must
    not answer webfinger for an actor it does not host.
    """
    site, instance = seed_local_actors()
    make_user(instance, 'alice')

    response = webfinger_get(app, resource='acct:alice@test.piefed.local')

    assert response.status_code == 404


def test_a_user_response_carries_the_fep_3b86_create_template(app, db_session):
    """`isinstance(object, User)` appends a share template. The Community branch
    below appends a different one, and a Feed gets neither -- three outcomes from
    one isinstance chain.
    """
    site, instance = seed_local_actors()
    make_user(instance, 'alice', local=True)

    response = webfinger_get(app, resource='acct:alice@test.piefed.local')

    assert response.status_code == 200
    rels = {link['rel'] for link in response.json['links']}
    assert 'https://w3id.org/fep/3b86/Create' in rels


def test_a_community_is_served_when_no_user_matches(app, db_session):
    """The fallback after the User lookup returns None. `make_community` leaves
    `ap_id` None and builds `ap_profile_id` as https://<host>/c/<name>, which is
    what the lookup compares against -- so the community's host must be this
    instance's own for it to be found. No User named 'books' is seeded by this
    file, so the User lookup above this in the code returns None first and the
    Community branch is what actually resolves it.
    """
    seed_local_actors()
    make_community(name='books', host='test.piefed.local')

    response = webfinger_get(app, resource='acct:books@test.piefed.local')

    assert response.status_code == 200
    assert response.json['links'][1]['properties'][
        'https://www.w3.org/ns/activitystreams#type'] == 'Group'


def test_a_local_only_community_is_not_served(app, db_session):
    """`local_only=False`. `make_community` sets local_only=False explicitly, so
    flipping it here is a real state change.
    """
    seed_local_actors()
    community = make_community(name='books', host='test.piefed.local')
    community.local_only = True
    db.session.commit()

    response = webfinger_get(app, resource='acct:books@test.piefed.local')

    assert response.status_code == 404


def test_a_banned_community_is_not_served(app, db_session):
    """D153, fixed. `banned=False` in the Community lookup, the guard the User
    and Feed lookups beside it already carry. Community deletion bans the
    community to hide it, so without this a deleted community stayed
    resolvable. `banned` defaults to False, so it is set explicitly.
    """
    seed_local_actors()
    community = make_community(name='books', host='test.piefed.local')
    community.banned = True
    db.session.commit()

    response = webfinger_get(app, resource='acct:books@test.piefed.local')

    assert response.status_code == 404


def test_a_deleted_community_is_not_served(app, db_session):
    """D153, fixed. `ap_deleted_at=None` in the Community lookup, the
    soft-delete guard the Feed lookup already carries. It has no column
    default, so it is set explicitly while `banned` stays False.
    """
    seed_local_actors()
    community = make_community(name='books', host='test.piefed.local')
    community.ap_deleted_at = utcnow()
    db.session.commit()

    response = webfinger_get(app, resource='acct:books@test.piefed.local')

    assert response.status_code == 404


def test_a_remote_community_is_not_served(app, db_session):
    """The lookup builds the profile id from OUR SERVER_URL, so a community
    published on another host cannot match it whatever its name.
    """
    seed_local_actors()
    make_community(name='books', host='peer.example')

    response = webfinger_get(app, resource='acct:books@test.piefed.local')

    assert response.status_code == 404


def test_a_community_with_a_non_null_ap_id_is_not_served(app, db_session):
    """`ap_id=None`. `make_community` leaves `ap_id` unset (None) by default, so
    every other community test in this file is blind to this filter -- flipping
    it here to a non-None value (as a remote-cached copy of a community would
    carry) is the one state change needed to prove the filter does something.
    """
    seed_local_actors()
    community = make_community(name='books', host='test.piefed.local')
    community.ap_id = 'books@test.piefed.local'
    db.session.commit()

    response = webfinger_get(app, resource='acct:books@test.piefed.local')

    assert response.status_code == 404


def test_a_community_response_carries_the_fep_3b86_follow_template(app, db_session):
    """`elif isinstance(object, Community)` -- a different template from the
    User branch asserted in the previous test.
    """
    seed_local_actors()
    make_community(name='books', host='test.piefed.local')

    response = webfinger_get(app, resource='acct:books@test.piefed.local')

    assert response.status_code == 200
    rels = {link['rel'] for link in response.json['links']}
    assert 'https://w3id.org/fep/3b86/Follow' in rels
    assert 'https://w3id.org/fep/3b86/Create' not in rels


def test_a_feed_is_served_when_no_user_or_community_matches(app, db_session):
    """The third fallback in the chain. Requires `make_local_feed`: see its
    docstring for why `make_feed` cannot reach this branch.
    """
    seed_local_actors()
    make_local_feed('news', public=True)

    response = webfinger_get(app, resource='acct:news@test.piefed.local')

    assert response.status_code == 200
    assert response.json['links'][1]['properties'][
        'https://www.w3.org/ns/activitystreams#type'] == 'Feed'


def test_a_tilde_resource_resolves_a_feed_directly(app, db_session):
    """The `feed = True` path: a leading `~` skips the User and Community
    lookups entirely. Proved by seeding a USER with the same name -- the tilde
    branch must return the Feed, which the non-tilde chain would never reach
    because the user matches first.
    """
    site, instance = seed_local_actors()
    make_user(instance, 'news', local=True)
    make_local_feed('news', public=True)

    response = webfinger_get(app, resource='acct:~news@test.piefed.local')

    assert response.status_code == 200
    assert response.json['links'][1]['properties'][
        'https://www.w3.org/ns/activitystreams#type'] == 'Feed'


def test_a_private_feed_is_not_served(app, db_session):
    """`public=True` in the non-tilde chain's Feed lookup. A feed its owner has
    not published must not have its existence and URL advertised to any
    instance that asks, exactly as the User lookup's `deleted=False`/
    `banned=False` and the Community lookup's `local_only=False` keep those
    actors off the wire.

    `Feed.public` is passed False explicitly here rather than left to the
    column's own default (app/models.py:4062), so the test states its premise.
    """
    seed_local_actors()
    make_local_feed('secret', public=False)

    response = webfinger_get(app, resource='acct:secret@test.piefed.local')

    assert response.status_code == 404


def test_a_remote_feed_is_not_served(app, db_session):
    """The `ap_id=None` conjunct of the non-tilde chain's feed lookup, isolated
    from the `public`/`banned`/`ap_deleted_at` guards beside it by a feed that
    is public, unbanned and undeleted and differs only in being remote. This is
    also the test that would fail if a later change made `make_local_feed` set
    `ap_id` the way `make_feed` does.
    """
    site, instance = seed_local_actors()
    make_feed(instance, name='news', public=True)

    response = webfinger_get(app, resource='acct:news@test.piefed.local')

    assert response.status_code == 404


def test_a_feed_response_carries_neither_fep_3b86_template(app, db_session):
    """The isinstance chain's implicit third outcome: a Feed is neither a User
    nor a Community, so no template is appended. Nothing else in this file
    asserts that absence.
    """
    seed_local_actors()
    make_local_feed('news', public=True)

    response = webfinger_get(app, resource='acct:news@test.piefed.local')

    assert response.status_code == 200
    rels = {link['rel'] for link in response.json['links']}
    assert 'https://w3id.org/fep/3b86/Create' not in rels
    assert 'https://w3id.org/fep/3b86/Follow' not in rels


def test_a_banned_feed_is_not_served(app, db_session):
    """`banned=False` in the non-tilde chain's Feed lookup, the same guard the
    User lookup already carries. `Feed.banned` (app/models.py:4081) is set
    explicitly to True -- it defaults to False, so leaving it alone would
    assert nothing -- while `public` stays True, which is what isolates this
    guard from the `public` one proved above.
    """
    seed_local_actors()
    feed = make_local_feed('news', public=True)
    feed.banned = True
    db.session.commit()

    response = webfinger_get(app, resource='acct:news@test.piefed.local')

    assert response.status_code == 404


def test_a_deleted_feed_is_not_served(app, db_session):
    """`ap_deleted_at=None` in the non-tilde chain's Feed lookup. `Feed` has no
    `deleted` boolean: `ap_deleted_at` (app/models.py:4076) is its soft-delete
    marker, so it is the column that plays the part `User.deleted` plays in the
    User lookup. It has no column default, so it is None until set, and is set
    explicitly here. `public` is True and `banned` untouched, so this isolates
    the third guard from the other two.
    """
    seed_local_actors()
    feed = make_local_feed('news', public=True)
    feed.ap_deleted_at = utcnow()
    db.session.commit()

    response = webfinger_get(app, resource='acct:news@test.piefed.local')

    assert response.status_code == 404


def test_a_feed_name_lookup_is_case_sensitive(app, db_session):
    """`Feed.query.filter_by(name=actor.strip(), ...)` -- no `.lower()`, unlike
    the Community lookup's `actor.strip().lower()`. A mixed-case query against
    a lowercase-named feed must NOT match.
    """
    seed_local_actors()
    make_local_feed('news', public=True)

    response = webfinger_get(app, resource='acct:NEWS@test.piefed.local')

    assert response.status_code == 404


def test_a_tilde_resource_for_a_nonexistent_feed_is_404(app, db_session):
    """The `feed = True` branch's own miss path: no Feed named 'ghost' exists,
    so the tilde branch's Feed query (a separate `Feed.query.filter_by(...)`
    call from the one in the non-tilde chain) also falls through to `None` and
    reaches the shared `abort(404)`.
    """
    seed_local_actors()

    response = webfinger_get(app, resource='acct:~ghost@test.piefed.local')

    assert response.status_code == 404


def test_a_tilde_resource_bypasses_the_community_lookup_too(app, db_session):
    """The tilde branch skips the Community lookup exactly as it skips the User
    lookup. Proved by seeding a COMMUNITY with the same name as the feed --
    the tilde branch must still return the Feed, not the Group.
    """
    seed_local_actors()
    make_community(name='news', host='test.piefed.local')
    make_local_feed('news', public=True)

    response = webfinger_get(app, resource='acct:~news@test.piefed.local')

    assert response.status_code == 200
    assert response.json['links'][1]['properties'][
        'https://www.w3.org/ns/activitystreams#type'] == 'Feed'


def test_a_community_with_the_same_name_precedes_a_feed_in_the_non_tilde_chain(app, db_session):
    """The other side of the ordering: in the NON-tilde chain, the Community
    lookup runs before the Feed lookup, so a Community and Feed sharing a name
    resolve to the Community.
    """
    seed_local_actors()
    make_community(name='books', host='test.piefed.local')
    make_local_feed('books', public=True)

    response = webfinger_get(app, resource='acct:books@test.piefed.local')

    assert response.status_code == 200
    assert response.json['links'][1]['properties'][
        'https://www.w3.org/ns/activitystreams#type'] == 'Group'


def test_a_deleted_user_with_the_same_name_falls_through_to_the_feed(app, db_session):
    """Natural fallthrough, not the tilde shortcut: the User lookup's own
    `deleted=False` filter excludes this row, so `object` stays `None` after
    the User stage and the chain proceeds to Community (no match) and then
    Feed (match) -- all without a `~` in the resource.
    """
    site, instance = seed_local_actors()
    user = make_user(instance, 'news', local=True)
    user.deleted = True
    db.session.commit()
    make_local_feed('news', public=True)

    response = webfinger_get(app, resource='acct:news@test.piefed.local')

    assert response.status_code == 200
    assert response.json['links'][1]['properties'][
        'https://www.w3.org/ns/activitystreams#type'] == 'Feed'


def test_a_banned_user_with_the_same_name_falls_through_to_the_feed(app, db_session):
    """Same fallthrough, isolating the User lookup's `banned=False` conjunct
    instead of `deleted=False`.
    """
    site, instance = seed_local_actors()
    user = make_user(instance, 'news', local=True)
    user.banned = True
    db.session.commit()
    make_local_feed('news', public=True)

    response = webfinger_get(app, resource='acct:news@test.piefed.local')

    assert response.status_code == 200
    assert response.json['links'][1]['properties'][
        'https://www.w3.org/ns/activitystreams#type'] == 'Feed'


def test_a_remote_user_with_the_same_name_falls_through_to_the_feed(app, db_session):
    """Same fallthrough, isolating the User lookup's `ap_id=None` conjunct: a
    remote user (`make_user`'s default, `local=False`) is invisible to the
    User lookup, so the chain proceeds past it to the Feed.
    """
    site, instance = seed_local_actors()
    make_user(instance, 'news')
    make_local_feed('news', public=True)

    response = webfinger_get(app, resource='acct:news@test.piefed.local')

    assert response.status_code == 200
    assert response.json['links'][1]['properties'][
        'https://www.w3.org/ns/activitystreams#type'] == 'Feed'


def test_a_local_only_community_with_the_same_name_falls_through_to_the_feed(app, db_session):
    """The Community-stage equivalent of the fallthrough tests above: the
    Community lookup's own `local_only=False` filter excludes this row, so the
    chain proceeds to the Feed instead of stopping at the Community.
    """
    seed_local_actors()
    community = make_community(name='news', host='test.piefed.local')
    community.local_only = True
    db.session.commit()
    make_local_feed('news', public=True)

    response = webfinger_get(app, resource='acct:news@test.piefed.local')

    assert response.status_code == 200
    assert response.json['links'][1]['properties'][
        'https://www.w3.org/ns/activitystreams#type'] == 'Feed'


def test_a_tilde_prefix_is_not_recognized_in_url_form(app, db_session):
    """The `actor.startswith('~')` check lives inside the `'acct:' in query`
    branch only. A URL resource whose last path segment happens to start with
    `~` never reaches that check, so `feed` stays False and the literal actor
    `~news` (tilde included) is looked up -- which matches nothing, even
    though a feed named plain 'news' exists.
    """
    seed_local_actors()
    make_local_feed('news', public=True)

    response = webfinger_get(app, resource='https://test.piefed.local/f/~news')

    assert response.status_code == 404


def test_a_feed_alias_and_self_link_use_the_feed_public_url(app, db_session):
    """`object.public_url()` is called for `aliases[0]` and both links' `href`
    exactly as it is for User and Community. Asserted here against the Feed's
    own `ap_public_url` so a mutation swapping in some other URL-producing
    method would be caught.
    """
    seed_local_actors()
    make_local_feed('news', public=True)

    response = webfinger_get(app, resource='acct:news@test.piefed.local')

    assert response.status_code == 200
    expected_url = 'https://test.piefed.local/f/news'
    assert response.json['aliases'] == [expected_url]
    assert response.json['links'][0]['href'] == expected_url
    assert response.json['links'][1]['href'] == expected_url


def test_a_feed_actor_with_surrounding_whitespace_is_matched_after_stripping(app, db_session):
    """`.strip()` in the Feed lookup's own filter clause (`Feed.query.filter_by(
    name=actor.strip(), ...)`), isolated from the `.strip()` calls in the User
    and Community lookups above it. `%20` decodes to a literal space before
    `acct:` is split, landing in `actor` where only `.strip()` removes it.
    """
    seed_local_actors()
    make_local_feed('news', public=True)

    response = webfinger_get(app, resource='acct:%20news@test.piefed.local')

    assert response.status_code == 200
    assert response.json['links'][1]['properties'][
        'https://www.w3.org/ns/activitystreams#type'] == 'Feed'


def test_a_tilde_resource_does_not_resolve_a_remote_feed(app, db_session):
    """The tilde branch's `ap_id=None` filter (app/activitypub/routes.py's
    second, textually-separate `Feed.query.filter_by(...)` call, reached only
    when `feed = True`) is its own mutation target distinct from the non-tilde
    chain's copy proven by test_a_remote_feed_is_not_served. The feed is
    public, unbanned and undeleted, so only `ap_id` can produce the miss.
    """
    site, instance = seed_local_actors()
    make_feed(instance, name='news', public=True)

    response = webfinger_get(app, resource='acct:~news@test.piefed.local')

    assert response.status_code == 404


def test_a_tilde_resource_for_a_private_feed_is_not_served(app, db_session):
    """The tilde branch's Feed query is a second, textually separate copy of the
    non-tilde chain's, so every guard has to be present twice. This is the only
    test that can kill `public=True` in the tilde copy --
    test_a_private_feed_is_not_served proves it in the other one.
    """
    seed_local_actors()
    make_local_feed('secret', public=False)

    response = webfinger_get(app, resource='acct:~secret@test.piefed.local')

    assert response.status_code == 404


def test_a_tilde_resource_for_a_banned_feed_is_not_served(app, db_session):
    """`banned=False` in the tilde copy of the Feed lookup, the counterpart of
    test_a_banned_feed_is_not_served. `public` is True so the miss can only be
    the `banned` guard.
    """
    seed_local_actors()
    feed = make_local_feed('news', public=True)
    feed.banned = True
    db.session.commit()

    response = webfinger_get(app, resource='acct:~news@test.piefed.local')

    assert response.status_code == 404


def test_a_tilde_resource_for_a_deleted_feed_is_not_served(app, db_session):
    """`ap_deleted_at=None` in the tilde copy of the Feed lookup, the
    counterpart of test_a_deleted_feed_is_not_served. `public` is True and
    `banned` untouched, so only the soft-delete guard can produce the miss.
    """
    seed_local_actors()
    feed = make_local_feed('news', public=True)
    feed.ap_deleted_at = utcnow()
    db.session.commit()

    response = webfinger_get(app, resource='acct:~news@test.piefed.local')

    assert response.status_code == 404


def test_an_unknown_actor_is_404(app, db_session):
    """`if object is None: abort(404)`. This is the test that proves the
    endpoint distinguishes "this instance does not host that actor" from a
    successful lookup: a remote instance reading only the status can now tell
    the two apart, which RFC 7033 requires and which the previous empty 200
    made impossible.
    """
    seed_local_actors()

    response = webfinger_get(app, resource='acct:nobody@test.piefed.local')

    assert response.status_code == 404


def test_a_query_for_another_domain_is_404(app, db_session):
    """D145, fixed (owner ruling 2026-09-30). The resource's domain used to be
    split off and discarded, so `acct:alice@evil.example` was answered with THIS
    instance's `alice`, asserting a handle the query never asked about. RFC 7033
    has a server answer only for resources it is authoritative for, so a
    domain other than SERVER_NAME is now 404 even though a local alice exists.
    """
    site, instance = seed_local_actors()
    make_user(instance, 'alice', local=True)

    response = webfinger_get(app, resource='acct:alice@evil.example')

    assert response.status_code == 404


def test_a_url_resource_on_another_domain_is_404(app, db_session):
    """D145, fixed: the URL form's host is held to the same rule as the acct
    form's domain.
    """
    site, instance = seed_local_actors()
    make_user(instance, 'alice', local=True)

    response = webfinger_get(app, resource='https://evil.example/u/alice')

    assert response.status_code == 404


def test_the_domain_comparison_is_case_insensitive(app, db_session):
    """D145, fixed: domains are case-insensitive, so an upper-cased
    SERVER_NAME is still ours.
    """
    site, instance = seed_local_actors()
    make_user(instance, 'alice', local=True)

    response = webfinger_get(app, resource='acct:alice@TEST.PieFed.local')

    assert response.status_code == 200
    assert response.json['subject'] == 'acct:alice@test.piefed.local'
