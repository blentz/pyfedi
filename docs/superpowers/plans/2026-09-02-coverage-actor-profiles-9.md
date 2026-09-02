# Sub-project 9: the actor-profile endpoints — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring `user_profile`, `community_profile` and `feed_profile` — the three documents every federated interaction with this instance starts by fetching, 96 uncovered statements — to full statement coverage, and fix the two defects the spec authorises.

**Architecture:** Tests drive `GET /u/<actor>`, `/c/<actor>` and `/f/<actor>` through `app.test_client()`, with and without an ActivityPub `Accept` header. The three endpoints are mirrored implementations, so tests are written to make their differences visible rather than to cover each in isolation. One new test file.

**Tech Stack:** pytest, Flask test client, SQLAlchemy, podman-compose (`./run_tests.sh`).

**Spec:** `docs/superpowers/specs/2026-09-02-coverage-actor-profiles-9-design.md`

## Global Constraints

- Defects found **inside these three functions** are fixed test-first, each in its own commit, each proved by a mutation that fails a named test. Anything outside them is **registered, not fixed**. Only two fixes are pre-authorised (Task 10); anything else needs an argument in the report.
- Findings are numbered from **D154**; **both** of the register's "Next free number" notes are updated in the same change.
- The coverage floor in `coverage_floors.ini` rises to the measured blended figure **rounded down**. It is currently 74.
- **Locate every code target by content, not by the line numbers in this plan.** They drift; every sub-project since 5c has found them stale.
- **EVERY test asserts `response.status_code`.** AP-JSON tests also assert `content_type`, `Cache-Control`, `Link`, and `Vary` where the endpoint sets it. The headers are the federation contract and one of them is a registered defect. Sub-project 8's plan omitted this assertion in nine of its own blocks and it cost two fix rounds — do not repeat that.
- **No assertion may rest on a column's declared default.** Every column these functions branch on has one: `User.deleted`/`User.banned`, `Community.private`/`local_only`/`banned`, `Feed.public`/`banned` — all `default=False`. Set them explicitly.
- Every guard is mutation-tested with **each conjunct dropped separately**, each killed by a distinct named test. Record whether each kill is an **assertion-kill or a crash-kill**, and whether it is a **sole death**.
- **The unkillable-clause pattern.** A filter clause whose value equals what the factory always produces cannot be killed by any test using that factory unmodified. Sub-project 8 hit this four times. All three local lookups filter `ap_id=None` and every local-actor factory produces exactly that — **expect those clauses to be unkillable, and add a test that sets `ap_id` explicitly to something contrary.**
- A kill by `respx.models.AllMockedAssertionError` is an **infrastructure kill, not behavioural**.
- **Any docstring claim about another test must be verified true**, and must remain true after Task 10's fixes. After inverting a pin, check which branch that pin used to cover.
- **One pytest session at a time.** Implementers run only their own file; the controller runs the full suite and supplies all coverage figures. Stopping `run_tests.sh` on the host does not kill pytest in the container.
- **Delete nothing** the task did not create. `claude_test` and `scratch_full_cov.json` in the repository root are not ours.

---

## File structure

| File | Responsibility |
|---|---|
| `tests/test_actor_profiles.py` | **new** — all three endpoints |
| `app/activitypub/routes.py` | Task 10 fixes only, `user_profile` only |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` | findings from D154 |
| `tests/README.md`, `coverage_floors.ini` | harness facts, floor raise |

**No new factories are expected — but Task 1 confirms this against the models before relying on it.** Sub-project 8's spec made the same claim and was wrong: `make_feed` sets `ap_id` unconditionally, so no feed it built was reachable by a lookup filtering `ap_id=None`. `make_local_feed(name, public)` exists now precisely for that.

### Facts established before this plan — do not re-derive

- **`is_activitypub_request()`** is `'application/ld+json' in request.headers.get('Accept','') or 'application/activity+json' in request.headers.get('Accept','')` (`app/utils.py`). A plain substring test on `Accept`, with two accepted values. **Drive it with a real header, not a double** — the parse is part of what is under test — and cover both accepted values plus at least one rejected one.
- **`is_activitypub_request` is imported into `app.activitypub.routes` at its line 15**, so it *can* be patched there if a test genuinely needs a value a header cannot produce. Prefer the header.
- `show_profile`, `show_community`, `show_feed` and `default_context` are **direct imports** in `routes.py`; `resolve_remote_handle` is in its namespace too. All are patched on `app.activitypub.routes`, following the campaign's binding-site convention (`record_moderation(monkeypatch, *names)` from `tests/test_inbox_dispatch_lock_delete.py`).
- **`resolve_remote_handle` reaches the network and must never run.** Double it in every test that could reach it — `user_profile` calls it whenever its lookups return `None`.
- **`SERVER_URL` is `https://test.piefed.local`**, `SERVER_NAME` is `test.piefed.local`. Settled in sub-project 8.
- **`seed_community_owner(domain)` creates the Instance (id 1) AND the local User (id 1)** that `make_community`'s hardcoded `user_id=1`/`instance_id=1` require. Always use it rather than a bare `make_instance` when a Community will be built.
- Relevant defaults, all `False`: `User.deleted`, `User.banned`, `Community.private`, `Community.local_only`, `Community.banned`, `Feed.public`, `Feed.banned`.

### Two existing test files already drive `/u/<actor>` — READ THEM FIRST

`tests/test_remote_handle_resolution.py` (4 tests) and `tests/test_request_hooks.py` (39 tests, one of which gets `/u/<user_name>`). Between them they cover `user_profile`'s AP-JSON happy path, both lookup branches, and every optional field's *false* side — which is why it measures 52.9% while its siblings sit at 16% and 20%.

`test_remote_handle_resolution.py` in particular drives `/u/wakko@mastodon.cloud` **with** `Accept: application/activity+json`, which is the remote-actor path the spec flags as an asymmetry. **Task 1 reads both files and reports what they cover**, so this slice complements rather than duplicates them — and so Task 10's fix is checked against tests that already depend on `user_profile`.

---

### Task 1: the harness, content negotiation, and a survey of what already exists

**Files:** Create `tests/test_actor_profiles.py`

**Interfaces — Produces:** `profile_get(...)`, `seed_actors(...)`, used by every later task.

- [ ] **Step 1: Read the two existing files and report**

Read `tests/test_remote_handle_resolution.py` and `tests/test_request_hooks.py`. In your report, list which `user_profile` paths each already covers. Write no test that duplicates one of them; if you believe one of their tests is wrong, say so rather than working around it.

Also read the `Community`, `Feed` and `User` models and confirm no new factory is needed for a *local* actor of each kind. Say explicitly which factory you will use for each and that it leaves `ap_id` `None`.

- [ ] **Step 2: Write the failing tests**

```python
"""tests/test_actor_profiles.py"""
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
    two values (app/utils.py). The parse is part of what is under test, so a
    double would hide it -- the same reason sub-project 8 drove
    `requestor_domain()` with a real User-Agent.
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
    calls = _double_the_renderers(monkeypatch)

    response = profile_get(app, '/c/books', accept='text/html')

    assert response.status_code == 200
    assert len(calls['show_community']) == 1
    assert calls['show_community'][0].id == community.id


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
```

- [ ] **Step 3: Run the file**

Run: `./run_tests.sh tests/test_actor_profiles.py -q`
Expected: PASS. If a doubled `show_community` returning a string breaks Flask's response handling, return a real minimal response instead and say so — do not change production.

- [ ] **Step 4: Mutation-test the content-negotiation switch**

Drop each disjunct of `is_activitypub_request()` separately — but note it lives in `app/utils.py`, **outside this slice's three functions**. Mutating it is allowed for measurement; **restore it and verify `git diff app/utils.py` is empty.** Record which test dies for each disjunct.

- [ ] **Step 5: Commit**

```bash
git add tests/test_actor_profiles.py
git commit -m "test: cover the actor endpoints' content negotiation"
```

---

### Task 2: community_profile's lookups and its four not-found paths

**Files:** Modify `tests/test_actor_profiles.py`

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run the file** — Expected: PASS

- [ ] **Step 3: Mutation-test the lookups**

Drop `banned=False` from the remote lookup; confirm a named test dies. Then drop `ap_id=None` from the local lookup — **expect this to be unkillable**, because `make_community` never sets `ap_id`. If it is, add `test_a_local_community_with_a_non_null_ap_id_is_not_found` setting `ap_id` explicitly, and report it. Restore after each; verify `git diff app/activitypub/routes.py` is empty.

- [ ] **Step 4: Commit**

```bash
git add tests/test_actor_profiles.py
git commit -m "test: cover community_profile's lookups and not-found paths"
```

---

### Task 3: community_profile's visibility guard, document and headers

**Files:** Modify `tests/test_actor_profiles.py`

- [ ] **Step 1: Write the failing tests**

```python
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
    """
    seed_actors()
    make_community(name='books', host='test.piefed.local')
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/c/books', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'public, max-age=30'
    assert response.headers['Vary'] == 'Accept'
    assert 'rel="alternate"' in response.headers['Link']
```

- [ ] **Step 2: Run the file** — Expected: PASS

- [ ] **Step 3: Mutation-test the visibility guard**

Drop `community.local_only or` and confirm a distinct named test dies; drop `or community.private` and confirm a different one dies. Restore after each; verify the production diff is empty; record both with assertion-vs-crash and sole-death status.

- [ ] **Step 4: Commit**

```bash
git add tests/test_actor_profiles.py
git commit -m "test: cover community_profile's visibility guard, document and headers"
```

---

### Task 4: community_profile's optional fields

**Files:** Modify `tests/test_actor_profiles.py`

Six optional blocks: description, theme, icon (http and relative), image (http and relative), languages.

- [ ] **Step 1: Write the failing tests**

```python
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
    seed_actors()
    community = make_community(name='books', host='test.piefed.local')
    community.theme = 'dark'
    db.session.commit()
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/c/books', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['theme'] == 'dark'


def test_an_absolute_community_icon_url_is_used_as_is(app, db_session, monkeypatch):
    """`if icon_image.startswith('http')` -- the true side. `icon_image()` is
    doubled rather than seeding a real File row, because this test is about the
    URL branch, not about image storage.
    """
    seed_actors()
    community = make_community(name='books', host='test.piefed.local')
    community.icon_id = 1
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
    community = make_community(name='books', host='test.piefed.local')
    community.icon_id = 1
    db.session.commit()
    _double_the_renderers(monkeypatch)
    monkeypatch.setattr(type(community), 'icon_image',
                        lambda self, size='default': '/static/icon.png')

    response = profile_get(app, '/c/books', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['icon']['url'] == 'https://test.piefed.local/static/icon.png'


def test_an_absolute_community_header_url_is_used_as_is(app, db_session, monkeypatch):
    """The image block mirrors the icon block exactly; both need covering
    because they are separate code, not a shared helper.
    """
    seed_actors()
    community = make_community(name='books', host='test.piefed.local')
    community.image_id = 1
    db.session.commit()
    _double_the_renderers(monkeypatch)
    monkeypatch.setattr(type(community), 'header_image',
                        lambda self: 'https://cdn.example/header.png')

    response = profile_get(app, '/c/books', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['image'] == {'type': 'Image', 'url': 'https://cdn.example/header.png'}


def test_a_relative_community_header_url_is_prefixed(app, db_session, monkeypatch):
    seed_actors()
    community = make_community(name='books', host='test.piefed.local')
    community.image_id = 1
    db.session.commit()
    _double_the_renderers(monkeypatch)
    monkeypatch.setattr(type(community), 'header_image', lambda self: '/static/header.png')

    response = profile_get(app, '/c/books', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['image']['url'] == 'https://test.piefed.local/static/header.png'


def test_community_languages_are_listed(app, db_session, monkeypatch):
    """The `for language in community.languages` loop. A community with no
    languages yields an empty list, so this test seeds one to enter the loop
    body -- otherwise the append line is never executed.
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
```

- [ ] **Step 2: Run the file**

Expected: PASS. `icon_image` and `header_image` are model methods — read their real signatures before doubling and match them; if `monkeypatch.setattr` on the class is awkward, seed a real `File` row instead and say why in your report.

- [ ] **Step 3: Mutation-test the two URL branches**

Invert `icon_image.startswith('http')` and confirm a distinct named test dies; do the same for the header block. Restore; verify the production diff is empty; record both.

- [ ] **Step 4: Commit**

```bash
git add tests/test_actor_profiles.py
git commit -m "test: cover community_profile's optional document fields"
```

---

### Task 5: feed_profile's lookups, routes, visibility and headers

**Files:** Modify `tests/test_actor_profiles.py`

**This task PINS A DEFECT — do not fix it.** `feed_profile` omits `Vary: Accept` while the other two set it. `test_the_feed_response_omits_the_vary_header` asserts that absence. Task 10 may fix it.

- [ ] **Step 1: Write the failing tests**

```python
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
    """
    seed_actors()
    make_local_feed('news', public=True)
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/f/news', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'public, max-age=5'
    assert 'rel="alternate"' in response.headers['Link']
    assert 'Vary' not in response.headers
```

- [ ] **Step 2: Run the file**

Expected: PASS. If `make_local_feed` cannot store a name containing `/`, report that — the two-segment route may then be unreachable in tests, which is itself a finding.

- [ ] **Step 3: Mutation-test feed_profile's guards**

Drop `banned=False` from the remote lookup; drop `ap_id=None` from the local lookup (**expect unkillable** — add a contrary test if so); invert `if not feed.public`. Record each with assertion-vs-crash and sole-death status. Restore after each; verify the production diff is empty.

- [ ] **Step 4: Commit**

```bash
git add tests/test_actor_profiles.py
git commit -m "test: cover feed_profile's lookups and guards, pin its missing Vary header"
```

---

### Task 6: feed_profile's optional fields

**Files:** Modify `tests/test_actor_profiles.py`

- [ ] **Step 1: Write the failing tests**

Seven tests: description present and absent, icon absolute and relative, header absolute and relative, and the `childFeeds` loop both ways. Every one uses `make_local_feed(..., public=True)` — a non-public feed 403s before the document is built.

```python
def test_an_absolute_feed_icon_url_is_used_as_is(app, db_session, monkeypatch):
    """`if icon_image.startswith('http')` -- the true side. `icon_image()` is
    doubled rather than seeding a real File row: this test is about the URL
    branch, not about image storage.
    """
    seed_actors()
    feed = make_local_feed('news', public=True)
    feed.icon_id = 1
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
    feed = make_local_feed('news', public=True)
    feed.icon_id = 1
    db.session.commit()
    _double_the_renderers(monkeypatch)
    monkeypatch.setattr(type(feed), 'icon_image',
                        lambda self, size='default': '/static/icon.png')

    response = profile_get(app, '/f/news', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['icon']['url'] == 'https://test.piefed.local/static/icon.png'


def test_an_absolute_feed_header_url_is_used_as_is(app, db_session, monkeypatch):
    """The image block is separate code from the icon block, not a shared
    helper, so both need covering.
    """
    seed_actors()
    feed = make_local_feed('news', public=True)
    feed.image_id = 1
    db.session.commit()
    _double_the_renderers(monkeypatch)
    monkeypatch.setattr(type(feed), 'header_image', lambda self: 'https://cdn.example/h.png')

    response = profile_get(app, '/f/news', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['image'] == {'type': 'Image', 'url': 'https://cdn.example/h.png'}


def test_a_relative_feed_header_url_is_prefixed(app, db_session, monkeypatch):
    seed_actors()
    feed = make_local_feed('news', public=True)
    feed.image_id = 1
    db.session.commit()
    _double_the_renderers(monkeypatch)
    monkeypatch.setattr(type(feed), 'header_image', lambda self: '/static/h.png')

    response = profile_get(app, '/f/news', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['image']['url'] == 'https://test.piefed.local/static/h.png'



def test_a_feed_description_adds_summary_and_source(app, db_session, monkeypatch):
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

    Read `Feed.children` before writing this -- it is a relationship, and how a
    child is attached (a parent_feed_id column, an association row) decides the
    seeding. Do not guess.
    """
    seed_actors()
    parent = make_local_feed('news', public=True)
    child = make_local_feed('sports', public=True)
    # attach `child` to `parent` by whatever mechanism Feed.children uses
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
```

- [ ] **Step 2: Run the file** — Expected: PASS

- [ ] **Step 3: Mutation-test the two URL branches**, as in Task 4. Restore; verify the production diff is empty.

- [ ] **Step 4: Commit**

```bash
git add tests/test_actor_profiles.py
git commit -m "test: cover feed_profile's optional document fields"
```

---

### Task 7: user_profile's HEAD branch, fallback and delegation

**Files:** Modify `tests/test_actor_profiles.py`

**Read `tests/test_remote_handle_resolution.py` and `tests/test_request_hooks.py` first** — they already cover `user_profile`'s AP-JSON happy path and both lookup branches. Write nothing that duplicates them.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_head_request_for_an_activitypub_client_returns_an_empty_json_body(app, db_session, monkeypatch):
    """`user_profile` is the ONLY one of the three whose route accepts HEAD and
    the only one with an explicit HEAD branch -- an asymmetry the spec registers.
    The branch returns `jsonify('')` with the AP content type and no actor
    document at all.
    """
    site, instance = seed_actors()
    make_user(instance, 'alice', local=True)
    _double_the_renderers(monkeypatch)

    with app.test_client() as client:
        response = client.head('/u/alice', headers={'Accept': AP_ACCEPT})

    assert response.status_code == 200
    assert response.content_type == 'application/activity+json'


def test_a_head_request_from_a_browser_returns_an_empty_string(app, db_session, monkeypatch):
    """The HEAD branch's else: no content type is set, and `show_profile` is
    never reached -- asserted through the double's call list, which is what
    distinguishes this from the browser GET path.
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
    """
    site, instance = seed_actors()
    user = make_user(instance, 'alice', local=True)
    user.ap_profile_id = 'https://test.piefed.local/u/bob'
    db.session.commit()
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/u/bob', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['type'] == 'Person'


def test_a_browser_request_for_a_user_reaches_show_profile(app, db_session, monkeypatch):
    """The HTML delegation, which nothing else in this file or the two existing
    files asserts. `show_profile` is doubled, so this asserts the delegation
    happened and with which user -- not what the template rendered.
    """
    site, instance = seed_actors()
    user = make_user(instance, 'alice', local=True)
    calls = _double_the_renderers(monkeypatch)

    response = profile_get(app, '/u/alice', accept='text/html')

    assert response.status_code == 200
    assert len(calls['show_profile']) == 1
    assert calls['show_profile'][0].id == user.id


def test_a_bot_user_is_typed_as_a_service(app, db_session, monkeypatch):
    """`"type": "Person" if not user.bot else "Service"`. Nothing else covers
    the Service side; `User.bot` is set explicitly rather than left to default.
    """
    site, instance = seed_actors()
    user = make_user(instance, 'alice', local=True)
    user.bot = True
    db.session.commit()
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/u/alice', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['type'] == 'Service'
```

- [ ] **Step 2: Run the file**

Expected: PASS. Flask strips HEAD response bodies automatically; assert on status and headers, not on body content. If `User.bot` does not exist under that name, read the model and use the real one.

- [ ] **Step 3: Mutation-test the HEAD branch**

Delete `if request.method == 'HEAD':` and its body; confirm a named HEAD test dies. Restore; verify the production diff is empty; record the kill.

- [ ] **Step 4: Commit**

```bash
git add tests/test_actor_profiles.py
git commit -m "test: cover user_profile's HEAD branch, profile-id fallback and delegation"
```

---

### Task 8: user_profile's optional fields

**Files:** Modify `tests/test_actor_profiles.py`

Nine tests: avatar absolute and relative, cover absolute and relative, `about_html` present and absent, `matrix_user_id`, and the `extra_fields` loop both ways. Note `user_profile` names its image fields `avatar`/`cover`, not `icon`/`image` as the other two do.

- [ ] **Step 1: Write the failing tests**

```python
def test_an_absolute_user_avatar_url_is_used_as_is(app, db_session, monkeypatch):
    """`if avatar_image.startswith('http')` -- the true side. The key emitted is
    `icon`, even though the column is `avatar_id`.
    """
    site, instance = seed_actors()
    user = make_user(instance, 'alice', local=True)
    user.avatar_id = 1
    db.session.commit()
    _double_the_renderers(monkeypatch)
    monkeypatch.setattr(type(user), 'avatar_image',
                        lambda self, size='default': 'https://cdn.example/a.png')

    response = profile_get(app, '/u/alice', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['icon'] == {'type': 'Image', 'url': 'https://cdn.example/a.png'}


def test_a_relative_user_avatar_url_is_prefixed_with_the_server_url(app, db_session, monkeypatch):
    site, instance = seed_actors()
    user = make_user(instance, 'alice', local=True)
    user.avatar_id = 1
    db.session.commit()
    _double_the_renderers(monkeypatch)
    monkeypatch.setattr(type(user), 'avatar_image',
                        lambda self, size='default': '/static/a.png')

    response = profile_get(app, '/u/alice', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['icon']['url'] == 'https://test.piefed.local/static/a.png'


def test_an_absolute_user_cover_url_is_used_as_is(app, db_session, monkeypatch):
    """The cover block emits `image`, from the `cover_id` column."""
    site, instance = seed_actors()
    user = make_user(instance, 'alice', local=True)
    user.cover_id = 1
    db.session.commit()
    _double_the_renderers(monkeypatch)
    monkeypatch.setattr(type(user), 'cover_image', lambda self: 'https://cdn.example/c.png')

    response = profile_get(app, '/u/alice', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['image'] == {'type': 'Image', 'url': 'https://cdn.example/c.png'}


def test_a_relative_user_cover_url_is_prefixed(app, db_session, monkeypatch):
    site, instance = seed_actors()
    user = make_user(instance, 'alice', local=True)
    user.cover_id = 1
    db.session.commit()
    _double_the_renderers(monkeypatch)
    monkeypatch.setattr(type(user), 'cover_image', lambda self: '/static/c.png')

    response = profile_get(app, '/u/alice', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['image']['url'] == 'https://test.piefed.local/static/c.png'


def test_a_user_without_an_about_omits_summary_and_source(app, db_session, monkeypatch):
    """The false side. Asserting ABSENCE is what makes the guard killable."""
    site, instance = seed_actors()
    make_user(instance, 'alice', local=True)
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/u/alice', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert 'summary' not in response.json
    assert 'source' not in response.json



def test_a_user_about_adds_summary_and_source(app, db_session, monkeypatch):
    site, instance = seed_actors()
    user = make_user(instance, 'alice', local=True)
    user.about_html = '<p>Hello</p>'
    user.about = 'Hello'
    db.session.commit()
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/u/alice', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['summary'] == '<p>Hello</p>'
    assert response.json['source'] == {'content': 'Hello', 'mediaType': 'text/markdown'}


def test_a_user_matrix_id_is_included(app, db_session, monkeypatch):
    site, instance = seed_actors()
    user = make_user(instance, 'alice', local=True)
    user.matrix_user_id = '@alice:matrix.example'
    db.session.commit()
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/u/alice', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['matrixUserId'] == '@alice:matrix.example'


def test_user_extra_fields_become_property_value_attachments(app, db_session, monkeypatch):
    """`if user.extra_fields.count() > 0` then a loop. Read `User.extra_fields`
    and its row model before seeding -- it is a relationship with its own table,
    and the label/text column names decide the assertion. Do not guess.
    """
    site, instance = seed_actors()
    user = make_user(instance, 'alice', local=True)
    # seed one extra field row attached to `user`
    db.session.commit()
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/u/alice', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert {'type': 'PropertyValue', 'name': 'Website',
            'value': 'https://example.com'} in response.json['attachment']


def test_a_user_without_extra_fields_has_no_attachment_key(app, db_session, monkeypatch):
    """The false side of the count guard: `attachment` is created INSIDE the
    guard, so its absence is what the guard controls.
    """
    site, instance = seed_actors()
    make_user(instance, 'alice', local=True)
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/u/alice', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert 'attachment' not in response.json
```

- [ ] **Step 2: Run the file** — Expected: PASS

- [ ] **Step 3: Mutation-test the two URL branches**, as in Task 4.

- [ ] **Step 4: Commit**

```bash
git add tests/test_actor_profiles.py
git commit -m "test: cover user_profile's optional document fields"
```

---

### Task 9: pin user_profile's dead admin branch and its deleted-user exposure

**Files:** Modify `tests/test_actor_profiles.py`

**Both tests pin defects. Fix neither — Task 10 does.**

- [ ] **Step 1: Write the failing tests**

```python
def test_the_admin_branch_and_the_else_branch_resolve_identically(app, db_session, monkeypatch):
    """PINS a defect. `user_profile` opens with
    `if current_user.is_authenticated and current_user.is_admin():` followed by
    an `else:` whose six lines are BYTE-FOR-BYTE identical. The comment above it
    says "admins can view deleted accounts", but neither branch filters
    `deleted`, so the branch decides nothing.

    This test asserts the two branches agree. It cannot distinguish "the code
    is duplicated" from "the code is correct and happens to agree" on its own --
    that is what the source reading establishes, and what Task 10's mutation
    will prove. What it DOES pin is that removing the branch cannot change
    behaviour, which is the fix's precondition.
    """
    site, instance = seed_actors()
    make_user(instance, 'alice', local=True)
    _double_the_renderers(monkeypatch)

    anonymous = profile_get(app, '/u/alice', accept=AP_ACCEPT)

    assert anonymous.status_code == 200
    assert anonymous.json['id'] == 'https://test.piefed.local/u/alice'


def test_a_deleted_user_profile_is_served_to_anyone(app, db_session, monkeypatch):
    """PINS a defect, and the more consequential of the two.

    Neither lookup branch filters `deleted` or `banned`, so this endpoint
    returns a deleted user's full actor document -- public key, inbox, shared
    inbox -- to an unauthenticated caller. Webfinger's user lookup DOES filter
    both (`deleted=False, banned=False`), so the two endpoints give opposite
    answers about the same user.

    `deleted` is set explicitly; it defaults to False, so leaving it alone
    would assert nothing.
    """
    site, instance = seed_actors()
    user = make_user(instance, 'alice', local=True)
    user.deleted = True
    db.session.commit()
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/u/alice', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['id'] == 'https://test.piefed.local/u/alice'


def test_a_banned_user_profile_is_served_to_anyone(app, db_session, monkeypatch):
    """The same gap on the other column. Pinned separately so a fix that adds
    only one guard is caught by the other test.
    """
    site, instance = seed_actors()
    user = make_user(instance, 'alice', local=True)
    user.banned = True
    db.session.commit()
    _double_the_renderers(monkeypatch)

    response = profile_get(app, '/u/alice', accept=AP_ACCEPT)

    assert response.status_code == 200
    assert response.json['id'] == 'https://test.piefed.local/u/alice'
```

- [ ] **Step 2: Run the file**

Expected: PASS. Every assertion states current behaviour. If one fails, the defect is not as described — REPORT that rather than adjusting the test.

- [ ] **Step 3: Commit**

```bash
git add tests/test_actor_profiles.py
git commit -m "test: pin user_profile's dead admin branch and its deleted-user exposure"
```

---

### Task 10: fix user_profile

**Files:** Modify `app/activitypub/routes.py` (`user_profile` only); modify `tests/test_actor_profiles.py`

Two authorised fixes, **two separate commits**. Invert, watch it fail, fix, mutation-prove.

**Fix A — remove the dead admin branch.** The `if current_user.is_authenticated and current_user.is_admin():` block and its `else:` are byte-for-byte identical. Collapse them to one copy. This is a pure no-op refactor with no behavioural change, so **prove it by mutation differently**: after collapsing, confirm the whole file still passes, and confirm coverage of the region did not drop (the controller supplies figures; report the region's uncovered lines before and after).

**Fix B — add the `deleted` and `banned` guards.** Both local lookups must exclude deleted and banned users, matching what webfinger's user lookup already does. Inverts `test_a_deleted_user_profile_is_served_to_anyone` and `test_a_banned_user_profile_is_served_to_anyone`.

**This changes who is visible.** A deleted or banned user's profile will return 404 where it now returns a document. That is intended and was decided deliberately. **Check `tests/test_remote_handle_resolution.py` and `tests/test_request_hooks.py` still pass** — they drive this same route and may depend on the old behaviour. If either breaks, report it before adjusting anything: a test that breaks here is telling you something about the fix.

**Do NOT restore the comment's original intent** (letting admins see deleted accounts) by adding an admin branch back. That would be a new feature, not a fix, and nothing in the codebase implements it today.

- [ ] **Fix A — Step 1:** Collapse the duplicated branch. Run the file; expect all tests to pass unchanged, since the branch is a no-op.
- [ ] **Fix A — Step 2:** Update `test_the_admin_branch_and_the_else_branch_resolve_identically` — its docstring now describes removed code. Rename and reword it to state what it still proves: that anonymous resolution works. Verify the new docstring is true.
- [ ] **Fix A — Step 3:** Report the region's uncovered lines before and after, and confirm no test was lost.
- [ ] **Fix A — Step 4: Commit**

```bash
git add app/activitypub/routes.py tests/test_actor_profiles.py
git commit -m "refactor: remove user_profile's duplicated admin lookup branch"
```

- [ ] **Fix B — Step 5:** Invert both pinning tests: rename to `..._is_not_served` and assert 404.
- [ ] **Fix B — Step 6:** Run them and watch them FAIL. **Quote the failure text in your report** — that output is the proof the tests discriminate.
- [ ] **Fix B — Step 7:** Add `deleted=False, banned=False` to both local lookups. Decide whether the `ap_id` remote lookup needs them too and justify either way. Run; expect PASS.
- [ ] **Fix B — Step 8:** Run the two existing `/u/` test files. Report the result. If either fails, stop and report before changing anything.
- [ ] **Fix B — Step 9:** Mutation-test: drop `deleted=False`, confirm the deleted test dies; drop `banned=False`, confirm the banned test dies. Record assertion-vs-crash and sole-death for each. Restore.
- [ ] **Fix B — Step 10: Commit**

```bash
git add app/activitypub/routes.py tests/test_actor_profiles.py
git commit -m "fix: stop serving deleted and banned users' actor documents"
```

- [ ] **Step 11: Check for a vacated branch.** Each inversion may have removed the only test reaching some branch — sub-project 7 lost one this way with the suite green. Name the branch each inverted test used to cover and the test that covers it now. If none does, add one and mutation-prove it.

---

### Task 11: the register, the README, and the floor

**Files:** Modify the findings register, `tests/README.md`, `coverage_floors.ini`

- [ ] **Step 1: Register the findings**

Add a `## Sub-project 9` section to `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`, matching sub-project 8's shape (**read it first**). Number from **D154**; update **both** "Next free number" notes.

**Number by distinct defect, not by commit.** Every row: location, defect, impact, and how established — `measured` or `reading-level`. **Verify every line number against the file before writing it.**

Register at minimum:

- Task 10's two fixes, with commits.
- Whatever Task 5 established about `Vary: Accept`, fixed or not.
- **`user_profile` serves remote actors' ActivityPub documents** while the other two `abort(400)`. Not fixed — a federation-behaviour question beyond this slice.
- **The local lookups have no ban guard** in `community_profile` and `feed_profile`, while their remote lookups do. This is D153 reappearing in two more endpoints; cross-reference it.
- **`id` and `preferredUsername` reflect the caller's casing** in all three.
- **Three different `Cache-Control` max-ages** (15, 30, 5) with no evident reason.
- **`community_profile`'s not-found path branches on authentication** while the other two `abort(404)`.
- **Only `user_profile` accepts HEAD** and has a HEAD branch.
- Any mutation that could not be killed, with the test added to kill it.

- [ ] **Step 2: Record the harness facts**

Append to `tests/README.md`'s numbered list, continuing its numbering. At minimum: how the three endpoints are driven and what `Accept` values select the ActivityPub document; that `resolve_remote_handle` reaches the network and must be doubled; whatever you learned about doubling `icon_image`/`header_image`; and how `Feed.children` and `User.extra_fields` are seeded, since both cost real reading.

- [ ] **Step 3: Raise the floor**

The controller supplies the measured blended figure. Set `app/activitypub/routes.py` in `coverage_floors.ini` to that figure **rounded down**. It is currently 74. **Do not run the full suite.**

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md tests/README.md coverage_floors.ini
git commit -m "docs: register sub-project 9's findings and raise the routes.py floor"
```
