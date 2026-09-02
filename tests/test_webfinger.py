"""tests/test_webfinger.py"""
from app import db
from app.activitypub import routes as activitypub_routes
from app.models import Site
from tests.factories import make_site, make_user, seed_community_owner


def webfinger_get(app, resource=None, user_agent=None):
    """GET /.well-known/webfinger through the real route.

    Driving the route rather than calling `process_webfinger_request` directly
    is what exercises the allowlist and ban guards, and it is the only way the
    handler's status codes are observable at all -- Flask turns its bare-string
    returns into 200s, which a direct call would hide.

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
    """A Site row (id 1, which the route's `g.site` lookup needs) plus the
    Instance and User that id-1-hardcoding factories require.

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


def test_a_malformed_resource_returns_a_bare_string_with_status_200(app, db_session):
    """PINS a defect. Neither 'acct:' nor a scheme appears, so the function
    returns the bare string 'Webfinger regex failed to match'. Flask turns that
    into HTTP 200 with a text/html content type.

    RFC 7033 wants 400 for a malformed request. A remote instance cannot tell
    this apart from a successful lookup by status alone, and a client that
    checks only the status will try to parse an English sentence as JRD.
    """
    seed_local_actors()

    response = webfinger_get(app, resource='alice-with-no-scheme')

    assert response.status_code == 200
    assert response.get_data(as_text=True) == 'Webfinger regex failed to match'
    assert 'text/html' in response.content_type


def test_the_instance_actor_is_served_from_the_special_case(app, db_session):
    """`actor == current_app.config['SERVER_NAME']` short-circuits every
    database lookup and returns a fixed JRD pointing at /actor. No User, Community
    or Feed row exists in this test, which is what proves the short-circuit: any
    other path would return '' for an unknown actor (app/activitypub/routes.py,
    `if object is None: return ''`).
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

    rels = {link['rel']: link for link in response.json['links']}
    assert rels['http://webfinger.net/rel/profile-page']['type'] == 'text/html'
    assert rels['http://webfinger.net/rel/profile-page']['href'] == 'https://test.piefed.local/about'
    assert rels['self']['type'] == 'application/activity+json'
    assert rels['self']['href'] == 'https://test.piefed.local/actor'
