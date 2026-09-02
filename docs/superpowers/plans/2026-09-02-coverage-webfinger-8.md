# Sub-project 8: the webfinger discovery surface — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring `webfinger` and `process_webfinger_request` — how every remote instance discovers every local actor, 53 uncovered statements — to full statement coverage, and fix the defects the spec names.

**Architecture:** Tests drive `GET /.well-known/webfinger?resource=…` through `app.test_client()`, end to end, so the route's access guard and the handler are exercised together. Status code, content-type and cache headers are asserted everywhere — two of the four defects are visible *only* as a status code. One new test file, one new factory.

**Tech Stack:** pytest, pytest-timeout, Flask test client, SQLAlchemy, podman-compose (`./run_tests.sh`).

**Spec:** `docs/superpowers/specs/2026-09-02-coverage-webfinger-8-design.md`

## Global Constraints

- Defects found **inside these two functions** are fixed test-first, each in its own commit, separate from every test-only commit, each proved by a mutation that fails a named test. Anything outside them is **registered, not fixed**.
- Findings are numbered from **D142**; **both** of the register's "Next free number" notes are updated in the same change that takes them.
- The coverage floor in `coverage_floors.ini` rises to the measured blended figure **rounded down**. It is currently 71.
- **Locate every code target by content, not by the line numbers in this plan.** They drift; every sub-project since 5c has found them stale.
- **Every test asserts `response.status_code`.** Success paths also assert `Content-Type: application/jrd+json`, `Cache-Control` and `Access-Control-Allow-Origin`. A body-only assertion cannot see two of this slice's defects.
- **No assertion may rest on a column's declared default.** `Site.allowlist_mode` defaults to `0` (`app/models.py:3955`), `User.deleted` to `False` (`:978`), `Feed.public` to `False` (`:4062`). Every test that depends on one of these sets it explicitly.
- Every guard is mutation-tested with **each conjunct dropped separately**, each killed by a distinct named test.
- A kill by `respx.models.AllMockedAssertionError` is an **infrastructure kill, not behavioural**.
- **Any docstring claim about another test must be verified true.** Sub-project 7 lost three fix rounds to this, two of them to docstrings written true and falsified later by its own fixes.
- **After inverting a defect pin, check which branch that pin used to cover.** Sub-project 7 silently lost a branch this way and the suite stayed green.
- **One pytest session at a time.** Implementers run only their own file; the controller runs the full suite and supplies all coverage figures. Stopping `run_tests.sh` on the host does not kill pytest in the container.
- **Delete nothing** the task did not create. `claude_test` and `scratch_full_cov.json` in the repository root are not ours.

---

## File structure

| File | Responsibility |
|---|---|
| `tests/test_webfinger.py` | **new** — all of `webfinger` and `process_webfinger_request` |
| `tests/factories.py` | **one new factory** — `make_local_feed` |
| `app/activitypub/routes.py` | defect fixes only, these two functions only |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` | findings from D142 |
| `tests/README.md`, `coverage_floors.ini` | harness facts, floor raise |

### The one new factory, and why the spec was wrong to say none was needed

`make_feed` **always sets `ap_id`** — its own docstring says so: *"`local=True` still sets `ap_id` (unlike `make_user(local=True)` and `make_community`, both of which leave `ap_id` `None`)"*. Webfinger's feed lookup is `Feed.query.filter_by(name=..., ap_id=None)`. **No feed `make_feed` can build is reachable by webfinger at all**, at any argument. Task 6 adds `make_local_feed`.

Do **not** change `make_feed`'s behaviour to fix this. Its `ap_id` is load-bearing for the resolver sub-project — `find_remote_actor` branches on `ap_profile_id` containing `/f/` — and `Feed.is_local()` is `ap_id is None or profile_id().startswith(SERVER_URL)`, so existing callers rely on the second disjunct. Add a sibling.

### Existing helpers — reuse, do not rebuild

- `make_site()` — the `Site` row with id 1 that the route's `g.site` lookup needs.
- `make_user(instance, name, local=False, with_keys=False)` — `local=True` leaves `ap_id` `None`, which is what webfinger requires.
- `make_community(name='microblogs', host='test.piefed.local')` — leaves `ap_id` `None` and sets `ap_profile_id` to `https://<host>/c/<name>`.
- `seed_community_owner(domain='peer.example') -> Instance` creates the Instance (id 1) **and** the local User (id 1) that `make_community`'s hardcoded `user_id=1`/`instance_id=1` require. Always use it rather than a bare `make_instance` when a Community will be built — two tasks in sub-project 5e hit that foreign key.
- `record_moderation(monkeypatch, *names)` — `tests/test_inbox_dispatch_lock_delete.py`, patches names on `app.activitypub.routes`.

### Facts established before this plan — do not re-derive

- `requestor_domain()` (`app/utils.py`) reads the **User-Agent header**, pulling the URL out of a `+`-delimited comment (`Mastodon/4.2 (+https://example.social)`) and taking its host. So the route's access guard is driven with a real `User-Agent` header — **do not double `requestor_domain`** unless a test needs a value the header cannot produce.
- `ALLOWLIST_INTENSE` is `2`, from `app/constants.py:142`, reaching routes.py via `from app.constants import *`.
- `SERVER_NAME` is `test.piefed.local` under test (`.env.test`). Confirm `SERVER_URL` yourself before relying on it; the community lookup builds `f"{SERVER_URL}/c/{actor.strip().lower()}"` and must match `make_community`'s `ap_profile_id` exactly.
- Webfinger **lowercases** the actor for the community lookup (`actor.strip().lower()`) but **not** for the feed lookup (`actor.strip()`). Community names in tests should be lowercase; feed names are matched case-sensitively.
- `@cache.memoize(timeout=60)` on `process_webfinger_request` is **inert under test** — `.env.test` sets `CACHE_TYPE=NullCache`. No test needs to clear a cache.

---

### Task 1: the test file, and the route's access guard

**Files:** Create `tests/test_webfinger.py`

**Interfaces — Produces:** `webfinger_get(...)`, `seed_local_actors(...)`, used by every later task.

Covers `webfinger`: the no-requesting-domain path, the ban path, both arms of the allowlist branch, and the missing-`resource` 404.

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_webfinger.py"""
from app import db
from app.activitypub import routes as activitypub_routes
from app.models import Site
from tests.factories import (make_community, make_site, make_user,
                             seed_community_owner)


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


def test_a_request_without_a_resource_argument_is_404(app, db_session):
    """`abort(404)` when `request.args.get('resource')` is falsy. No User-Agent,
    so the access guards are skipped and this isolates the resource check.
    """
    seed_local_actors()

    response = webfinger_get(app)

    assert response.status_code == 404
```

- [ ] **Step 2: Run the file**

Run: `./run_tests.sh tests/test_webfinger.py -q`
Expected: PASS. If `get_setting`'s signature differs from `(name, default=None)`, read it and match — do not change production.

- [ ] **Step 3: Mutation-test the access guard**

Four edits, restoring after each, each killed by a distinct named test:
(a) drop `get_setting('use_allowlist') and` — `test_allowlist_mode_below_intense_falls_through_to_the_ban_check` should die;
(b) drop `and g.site.allowlist_mode == ALLOWLIST_INTENSE`;
(c) replace the whole `if requesting_domain := requestor_domain():` with `if True:`;
(d) delete the `abort(404)` arm.
Record each kill with its failure text. Verify `git diff app/activitypub/routes.py` is empty. If any cannot be killed, say so — that is a finding.

- [ ] **Step 4: Commit**

```bash
git add tests/test_webfinger.py
git commit -m "test: cover the webfinger route's access guard and 404 arm"
```

---

### Task 2: the resource parser, and the malformed-resource status

**Files:** Modify `tests/test_webfinger.py`

The three parse branches. **One test pins a defect — do not fix it.**

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run the file** — Expected: PASS. Every assertion states current behaviour.

- [ ] **Step 3: Commit**

```bash
git add tests/test_webfinger.py
git commit -m "test: cover webfinger's three parse branches and pin the malformed-resource status"
```

---

### Task 3: the instance-actor special case

**Files:** Modify `tests/test_webfinger.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_the_instance_actor_is_served_from_the_special_case(app, db_session):
    """`actor == current_app.config['SERVER_NAME']` short-circuits every
    database lookup and returns a fixed JRD pointing at /actor. No User, Community
    or Feed row exists in this test, which is what proves the short-circuit: any
    other path would return '' for an unknown actor.
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
```

- [ ] **Step 2: Run the file**

Expected: PASS. If `SERVER_URL` is not `https://test.piefed.local`, read its real value and use it — do not hardcode a guess, and record the real value in your report.

- [ ] **Step 3: Mutation-test the special case**

Change `actor == current_app.config['SERVER_NAME']` to `False` and confirm a named test fails. Restore; verify the production diff is empty; record the kill.

- [ ] **Step 4: Commit**

```bash
git add tests/test_webfinger.py
git commit -m "test: cover webfinger's instance-actor special case"
```

---

### Task 4: user resolution and its three filters

**Files:** Modify `tests/test_webfinger.py`

`User` by `user_name` or `alt_user_name`, filtered `deleted=False, banned=False, ap_id=None`.

- [ ] **Step 1: Write the failing tests**

```python
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


def test_a_deleted_user_is_not_served(app, db_session):
    """`deleted=False`. Seeded explicitly to True -- `User.deleted` defaults to
    False (app/models.py:978), so leaving it alone would assert nothing.
    """
    site, instance = seed_local_actors()
    user = make_user(instance, 'alice', local=True)
    user.deleted = True
    db.session.commit()

    response = webfinger_get(app, resource='acct:alice@test.piefed.local')

    assert response.get_data(as_text=True) == ''


def test_a_banned_user_is_not_served(app, db_session):
    """`banned=False`. `make_user` sets banned=False explicitly, so flipping it
    here is a real change of state rather than a default being restated.
    """
    site, instance = seed_local_actors()
    user = make_user(instance, 'alice', local=True)
    user.banned = True
    db.session.commit()

    response = webfinger_get(app, resource='acct:alice@test.piefed.local')

    assert response.get_data(as_text=True) == ''


def test_a_remote_user_is_not_served(app, db_session):
    """`ap_id=None`. A remote user is `make_user`'s DEFAULT (local=False), which
    is why every positive test in this file passes local=True. This instance must
    not answer webfinger for an actor it does not host.
    """
    site, instance = seed_local_actors()
    make_user(instance, 'alice')

    response = webfinger_get(app, resource='acct:alice@test.piefed.local')

    assert response.get_data(as_text=True) == ''


def test_a_user_response_carries_the_fep_3b86_create_template(app, db_session):
    """`isinstance(object, User)` appends a share template. The Community branch
    below appends a different one, and a Feed gets neither -- three outcomes from
    one isinstance chain.
    """
    site, instance = seed_local_actors()
    make_user(instance, 'alice', local=True)

    response = webfinger_get(app, resource='acct:alice@test.piefed.local')

    rels = {link['rel'] for link in response.json['links']}
    assert 'https://w3id.org/fep/3b86/Create' in rels
```

- [ ] **Step 2: Run the file** — Expected: PASS

- [ ] **Step 3: Mutation-test the user filters**

Drop each of `deleted=False`, `banned=False`, `ap_id=None` separately, and each disjunct of the `or_(...)`, confirming a distinct named test dies for each. Restore after each; verify the production diff is empty; record all five.

- [ ] **Step 4: Commit**

```bash
git add tests/test_webfinger.py
git commit -m "test: cover webfinger's user resolution and all three of its filters"
```

---

### Task 5: community resolution

**Files:** Modify `tests/test_webfinger.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_a_community_is_served_when_no_user_matches(app, db_session):
    """The fallback after the User lookup returns None. `make_community` leaves
    `ap_id` None and builds `ap_profile_id` as https://<host>/c/<name>, which is
    what the lookup compares against -- so the community's host must be this
    instance's own for it to be found.
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

    assert response.get_data(as_text=True) == ''


def test_a_remote_community_is_not_served(app, db_session):
    """The lookup builds the profile id from OUR SERVER_URL, so a community
    published on another host cannot match it whatever its name.
    """
    seed_local_actors()
    make_community(name='books', host='peer.example')

    response = webfinger_get(app, resource='acct:books@test.piefed.local')

    assert response.get_data(as_text=True) == ''


def test_a_community_response_carries_the_fep_3b86_follow_template(app, db_session):
    """`elif isinstance(object, Community)` -- a different template from the
    User branch asserted in the previous task.
    """
    seed_local_actors()
    make_community(name='books', host='test.piefed.local')

    response = webfinger_get(app, resource='acct:books@test.piefed.local')

    rels = {link['rel'] for link in response.json['links']}
    assert 'https://w3id.org/fep/3b86/Follow' in rels
    assert 'https://w3id.org/fep/3b86/Create' not in rels
```

- [ ] **Step 2: Run the file**

Expected: PASS. If the community is not found, check `SERVER_URL` against `ap_profile_id` — the lookup lowercases the actor but the factory does not lowercase the name.

- [ ] **Step 3: Mutation-test the community filters**

Drop `ap_id=None` and `local_only=False` separately; confirm a distinct named test dies for each. Restore; verify the production diff is empty; record both.

- [ ] **Step 4: Commit**

```bash
git add tests/test_webfinger.py
git commit -m "test: cover webfinger's community resolution and its two filters"
```

---

### Task 6: the local-feed factory, feed resolution, and the missing guards

**Files:** Modify `tests/factories.py`, `tests/test_webfinger.py`

**Interfaces — Produces:** `make_local_feed(name, public=False)`.

**This task pins a defect. Do not fix it.**

- [ ] **Step 1: Add the factory**

```python
def make_local_feed(name: str = 'localfeed', public: bool = False) -> Feed:
    """A Feed webfinger can actually resolve: `ap_id` is None.

    `make_feed` cannot be used here. It sets `ap_id` unconditionally -- even at
    `local=True`, as its own docstring records -- and webfinger's lookup is
    `Feed.query.filter_by(name=..., ap_id=None)`, so no feed `make_feed` builds
    is reachable by webfinger at any argument. `make_feed`'s `ap_id` is
    load-bearing elsewhere (find_remote_actor branches on `/f/` in
    `ap_profile_id`), so this is a sibling rather than a change to it.

    `public` defaults to False to match Feed.public's own column default
    (app/models.py:4062); callers pass it explicitly either way, because an
    assertion resting on a declared default proves nothing.
    """
    feed = Feed(name=name, title=name, instance_id=1, public=public,
                ap_profile_id=f"https://test.piefed.local/f/{name}",
                ap_public_url=f"https://test.piefed.local/f/{name}")
    db.session.add(feed)
    db.session.commit()
    return feed
```

Read the `Feed` model first and add whatever other non-nullable columns it requires. Do **not** set `ap_id`.

- [ ] **Step 2: Write the failing tests**

```python
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


def test_a_private_feed_is_served_anyway(app, db_session):
    """PINS a defect. The User lookup excludes deleted and banned accounts and
    the Community lookup excludes local_only communities, but the Feed lookup
    filters on `ap_id=None` and NOTHING ELSE.

    `Feed.public` is passed False explicitly here rather than left to the
    column's own default (app/models.py:4062), so the test states its premise.
    A private feed's existence and URL are published to any instance that asks.
    """
    seed_local_actors()
    make_local_feed('secret', public=False)

    response = webfinger_get(app, resource='acct:secret@test.piefed.local')

    assert response.status_code == 200
    assert response.json['subject'] == 'acct:secret@test.piefed.local'


def test_a_remote_feed_is_not_served(app, db_session):
    """`ap_id=None` is the one filter the feed lookup DOES apply. This is also
    the test that would fail if a later change made `make_local_feed` set
    `ap_id` the way `make_feed` does.
    """
    site, instance = seed_local_actors()
    from tests.factories import make_feed
    make_feed(instance, name='news', public=True)

    response = webfinger_get(app, resource='acct:news@test.piefed.local')

    assert response.get_data(as_text=True) == ''


def test_a_feed_response_carries_neither_fep_3b86_template(app, db_session):
    """The isinstance chain's implicit third outcome: a Feed is neither a User
    nor a Community, so no template is appended. Nothing else in this file
    asserts that absence.
    """
    seed_local_actors()
    make_local_feed('news', public=True)

    response = webfinger_get(app, resource='acct:news@test.piefed.local')

    rels = {link['rel'] for link in response.json['links']}
    assert 'https://w3id.org/fep/3b86/Create' not in rels
    assert 'https://w3id.org/fep/3b86/Follow' not in rels
```

- [ ] **Step 3: Run the file** — Expected: PASS

- [ ] **Step 4: Mutation-test the feed branch**

Change `if not feed:` to `if True:` and confirm `test_a_tilde_resource_resolves_a_feed_directly` fails. Restore; verify the production diff is empty; record the kill.

- [ ] **Step 5: Commit**

```bash
git add tests/factories.py tests/test_webfinger.py
git commit -m "test: cover webfinger's feed resolution and pin its missing guards"
```

---

### Task 7: the not-found status, and the discarded domain

**Files:** Modify `tests/test_webfinger.py`

**Both tests pin defects. Fix neither.**

- [ ] **Step 1: Write the failing tests**

```python
def test_an_unknown_actor_returns_an_empty_body_with_status_200(app, db_session):
    """PINS a defect. `if object is None: return ''` -- Flask makes that HTTP
    200 with an empty body.

    RFC 7033 wants 404. As it stands a remote instance cannot distinguish "no
    such actor here" from "this endpoint is broken", and neither from a
    successful lookup, by status alone. The content type is asserted too: it is
    text/html, not the application/jrd+json every real answer carries.
    """
    seed_local_actors()

    response = webfinger_get(app, resource='acct:nobody@test.piefed.local')

    assert response.status_code == 200
    assert response.get_data(as_text=True) == ''
    assert 'text/html' in response.content_type


def test_a_query_for_another_domain_is_answered_with_our_own_user(app, db_session):
    """PINS a defect. The resource's domain is split off and discarded:

        actor = query.split(':')[1].split('@')[0]

    so `acct:alice@evil.example` is answered with THIS instance's `alice`, and
    the reply asserts the subject is alice@test.piefed.local -- a handle the
    query never asked about.

    RFC 7033 has a server answer only for resources it is authoritative for.
    The assertion below is on the subject rather than merely on the status,
    because the status is 200 either way: what makes this a defect is the
    identity the response claims.
    """
    site, instance = seed_local_actors()
    make_user(instance, 'alice', local=True)

    response = webfinger_get(app, resource='acct:alice@evil.example')

    assert response.status_code == 200
    assert response.json['subject'] == 'acct:alice@test.piefed.local'
```

- [ ] **Step 2: Run the file** — Expected: PASS. If either assertion fails, the defect is not as described: REPORT that rather than adjusting the test.

- [ ] **Step 3: Commit**

```bash
git add tests/test_webfinger.py
git commit -m "test: pin webfinger's not-found status and its discarded query domain"
```

---

### Task 8: fix the defects

**Files:** Modify `app/activitypub/routes.py` (these two functions only); modify `tests/test_webfinger.py`

Invert each pinning test, watch it fail, then fix. **Report the actual failure output** — that is the proof. **Each fix is its own commit.**

**Fix A — the feed lookup's missing guards.** Both feed lookups (the `~` branch and the fallback) must exclude non-public and deleted feeds, matching what the User and Community lookups already do. Decide from the model whether `ap_deleted_at is None` belongs alongside `public=True` and justify the choice. Invert `test_a_private_feed_is_served_anyway`.

**Fix B — the not-found status.** An unknown actor must return 404, not an empty 200. Invert `test_an_unknown_actor_returns_an_empty_body_with_status_200`.

**Fix C — the malformed-resource status.** A resource matching no parse branch must return 400, not a 200 with an English sentence. Invert `test_a_malformed_resource_returns_a_bare_string_with_status_200`.

**Do NOT fix** the discarded query domain. Rejecting a foreign domain changes who this instance will answer for, which can break federation with software that queries loosely, and the right behaviour (404 vs ignore) is a policy decision outside this slice's authorisation. Task 9 registers it. The pinning test stays as it is.

For each fix, in order, four steps:

- [ ] **Fix A — Step 1: Invert the test.** Rename `test_a_private_feed_is_served_anyway` to `test_a_private_feed_is_not_served`, assert the body is `''` (or 404 if Fix B lands first — do Fix A first and adjust in Fix B's commit if needed). Update the docstring: it no longer pins a defect.
- [ ] **Fix A — Step 2: Run it and watch it FAIL.** Quote the failure in your report.
- [ ] **Fix A — Step 3: Apply the fix; run; expect PASS.**
- [ ] **Fix A — Step 4: Mutation-test it.** Revert just the added filter, confirm the named test fails, restore. Then commit:

```bash
git add app/activitypub/routes.py tests/test_webfinger.py
git commit -m "fix: stop webfinger advertising private and deleted feeds"
```

**Fix B is the widest of the three — read this before starting it.** Every test in the file that currently asserts `response.get_data(as_text=True) == ''` is asserting the not-found behaviour Fix B changes. At the time of writing that is at least: `test_a_deleted_user_is_not_served`, `test_a_banned_user_is_not_served`, `test_a_remote_user_is_not_served`, `test_a_local_only_community_is_not_served`, `test_a_remote_community_is_not_served`, `test_a_remote_feed_is_not_served`, and — if Fix A landed first — `test_a_private_feed_is_not_served`. **Grep for the assertion rather than trusting that list**, convert them all in this same commit, and say in your report how many you found and changed.

- [ ] **Fix B — Step 5: Invert the pinning test.** Rename `test_an_unknown_actor_returns_an_empty_body_with_status_200` to `test_an_unknown_actor_is_404`, assert `response.status_code == 404`, and rewrite its docstring: it now proves the endpoint distinguishes "not hosted here" from a successful lookup.
- [ ] **Fix B — Step 6: Run it and watch it FAIL.** Quote the failure text in your report.
- [ ] **Fix B — Step 7: Apply the fix.** Replace `return ''` with a 404. Choose the mechanism from what the route already uses — `abort(404)` is the idiom two lines up in `webfinger` — and justify the choice. Convert every other `== ''` assertion found by the grep above. Run; expect PASS.
- [ ] **Fix B — Step 8: Mutation-test it.** Restore `return ''`, confirm `test_an_unknown_actor_is_404` fails, restore the fix. Verify `git diff` shows only the intended change. Then commit:

```bash
git add app/activitypub/routes.py tests/test_webfinger.py
git commit -m "fix: return 404 for a webfinger resource this instance does not host"
```

- [ ] **Fix C — Step 9: Invert the pinning test.** Rename `test_a_malformed_resource_returns_a_bare_string_with_status_200` to `test_a_malformed_resource_is_400`, assert `response.status_code == 400`, and drop the assertions on the English sentence and the text/html content type.
- [ ] **Fix C — Step 10: Run it and watch it FAIL.** Quote the failure text.
- [ ] **Fix C — Step 11: Apply the fix.** Replace `return 'Webfinger regex failed to match'` with a 400, using the same mechanism you chose for Fix B. Say in your report whether the string is preserved anywhere — losing an operator-facing diagnostic is a real cost, and if you drop it, say why that is acceptable. Run; expect PASS.
- [ ] **Fix C — Step 12: Mutation-test it.** Restore the bare-string return, confirm the named test fails, restore the fix. Then commit:

```bash
git add app/activitypub/routes.py tests/test_webfinger.py
git commit -m "fix: return 400 for a malformed webfinger resource"
```

- [ ] **Step 13: After all three fixes, check for a vacated branch.** Each inversion may have removed the only test reaching some branch — sub-project 7 lost one this way with the suite still green. For each inverted test, name the branch it used to cover and the test that covers it now. If none does, add one and mutation-prove it.

---

### Task 9: the register, the README, and the floor

**Files:** Modify the findings register, `tests/README.md`, `coverage_floors.ini`

- [ ] **Step 1: Register the findings**

Add a `## Sub-project 8` section to `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`, in the shape sub-project 7's section uses (**read it first**). Number from **D142**; update **both** "Next free number" notes in the same change.

**Number by distinct defect, not by commit.** Every row: location, defect, impact, and how established — `measured` or `reading-level`. **Verify every line number against the file before writing it.**

Register at minimum:

- Task 8's three fixes, with commits.
- **The discarded query domain**, not fixed, with the reason: rejecting a foreign domain changes who this instance answers for and is a policy decision beyond this slice.
- **`'acct:' in query` is a substring test, not a prefix test**, checked before the URL branch. Say what the tests established about reachability, or that they established nothing.
- **`@cache.memoize(timeout=60)`** means a just-banned user stays advertised for up to 60 seconds. Reading-level *by necessity* — `.env.test` sets `CACHE_TYPE=NullCache`, so no test can demonstrate it.
- **`object` and `type` shadow builtins** throughout the function.
- **The instance-actor special case runs after the `~` marker is stripped**, so `acct:~<SERVER_NAME>@<SERVER_NAME>` returns the instance actor rather than a feed.

- [ ] **Step 2: Record the harness facts**

Append to `tests/README.md`'s numbered list, continuing its numbering. At minimum:

- `make_feed` always sets `ap_id`, so no feed it builds is reachable by any lookup filtering `ap_id=None`; `make_local_feed` exists for that.
- `requestor_domain()` reads the User-Agent's `+URL` comment, so a route guarded by it is driven with a header rather than a double.
- `CACHE_TYPE=NullCache` under test makes every `@cache.memoize`/`@cache.cached` decorator inert — which is why no test clears a cache, and why cache-staleness defects can only be registered.
- Whatever `SERVER_URL`'s real value turned out to be, and that the community lookup lowercases the actor while the feed lookup does not.

- [ ] **Step 3: Raise the floor**

The controller supplies the measured blended figure. Set `app/activitypub/routes.py` in `coverage_floors.ini` to that figure **rounded down**. It is currently 71. **Do not run the full suite.**

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md tests/README.md coverage_floors.ini
git commit -m "docs: register sub-project 8's findings and raise the routes.py floor"
```
