# Per-domain names Implementation Plan (plan C1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local name is unique per domain, not site-wide: `wakko@example.org` and `wakko@<onion>` can be two accounts, and every lookup of a local user or community by name finds the one on the right domain.

**Architecture:** A new `app/local_names.py` turns a name or `name@host` into (name, domain) and gives the one SQL clause for "the local row of this model with this name on this domain". Every bare-name lookup moves onto it. Links to a local user or community carry `@domain` when that domain is not the storefront's, both in links the app builds and in stored HTML that the storefront rebases. Only then do the creation checks go per domain: the order means no two rows can share a name until every lookup can tell them apart.

**Tech Stack:** Flask, SQLAlchemy (Postgres 17), Jinja, pytest via `./run_tests.sh`.

**Spec:** `docs/superpowers/specs/2026-10-19-per-domain-identity-design.md` §2 (identity spaces), §3.1 (names unique per domain), §3.2 (bare name = the storefront's domain). Plan C was split by owner ruling 2026-10-20 into C1 (this: names), C2 (per-domain instance rows and ranking), C3 (per-domain federation: keys, instance actor, WebFinger/NodeInfo, IDs on the owning host) and C4 (sister domains without HTTP). This plan lifts plan B's ruling "local names are unique across ALL domains at the app layer" (ledger `.superpowers/sdd/2026-10-19-per-domain-identity/progress.md`).

## Global Constraints

- **The first domain is byte-identical:** every page, link, actor document, WebFinger answer and redirect for a default-domain user or community on the default storefront is unchanged. A link only gains `@domain` when the row's domain differs from the storefront's.
- A bare name means the request's storefront domain (`current_storefront()`; the default domain outside a request). `name@host` means that host's domain when the host is one this site hosts (`domain_for_host`); any other host is remote, as today.
- Names compare case-insensitively (`func.lower`), hosts via `normalise_host`.
- A hosted `name@host` that matches no row is a miss (404 / None), never a fetch.
- No migration. The per-domain unique indexes `ix_user_domain_lower_name` and `ix_community_domain_lower_name` (migration `b7e3c1a9d5f2`) already exist.
- **Feed names stay globally unique** (`Feed.name` is a global unique column and feeds have no domain). **Ruling** — the spec gives domains to accounts and communities only; a per-domain feed namespace needs a column and an index for no asked-for gain — cost if wrong: a feed name taken on one domain is unavailable on the others.
- A new local user or community always gets a `site_domain_id` (never NULL).
- TDD, one commit per task, 100% changed-line and branch coverage (`python tests/check_changed_line_coverage.py coverage.json <BASE> --branches`), gates from repo tools (`./run_tests.sh`), inline imports only if technically unavoidable, slow tests are bugs. Fuzz campaigns touch no DB: a fuzz target reaching `app/local_names.py` stubs `storefront.domain_rows`.
- The owner pushes. `deploy/` is local-only.

## Review Focus

1. A post stored with `<a href="https://example.org/u/bob">` shown on the onion, where both domains have a `bob`: the link goes to the first domain's bob (`/u/bob@example.org`), not the onion's.
2. On the onion, the first domain's bob's profile at `/u/bob@example.org`: its follow, block and note buttons act on the first domain's bob.
3. `/u/BOB@EXAMPLE.ORG` and `/c/Art@Example.Org` resolve like their lower-case forms.
4. `/u/x@<hosted host>` with no such account: 404 and no HTTP request. `/u/x@mastodon.social` works as today.
5. A notification email to an onion account, rendered in Celery, links the onion's users as bare names and the first domain's with `@example.org`.

Each line's test is in the owning task (Tasks 2, 4, 3, 3, 2).

---

### Task 1: `app/local_names.py` and a shared two-domain fixture

**Files:**
- Create: `app/local_names.py`
- Create: `tests/two_domains.py` (fixture module; promote the `two_bobs` setup from `tests/test_resolve_object_domains.py:42-64` into it and make that file use it)
- Test: `tests/test_local_names.py`

**Interfaces:**
- Consumes: `current_storefront()`, `domain_for_host(host) -> DomainInfo | None`, `on_domain(domain, model)`, `normalise_host` (all `app/storefront.py`).
- Produces:
  - `split_local_name(raw: str) -> tuple[str, DomainInfo] | None`: strips white space and one leading `@` or `!`. A bare `name` gives `(name, current_storefront())`. `name@host` gives `(name, domain_for_host(host))` when the host is hosted. It gives None for an unhosted host, an empty name or more than one `@`.
  - `local_named(model, name: str, domain: DomainInfo)`: a SQLAlchemy clause for `model` in (`User`, `Community`): lower-case name equal (`User.user_name`, `Community.name`), `ap_id IS NULL`, `on_domain(domain, model)`.
  - `local_path_name(obj) -> str`: `obj.user_name` / `obj.name` when `obj.home_domain()` (both `User` and `Community` have it) is the current storefront's domain, else `f'{name}@{domain.domain}'`.
  - The fixture `two_domains(app, db_session)` yields `(hell, onion)` as `DomainInfo` plus a factory `local_user(name, domain)` / `local_community(name, domain)` that inserts the row directly (creation refuses the duplicate until Task 5).

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize('raw', ['bob', ' bob ', '@bob'])
def test_a_bare_name_is_on_the_storefront(app, two_domains, raw):
    hell, onion = two_domains.domains
    with onion_request(app):                       # helper from tests/two_domains.py: g.storefront = onion
        assert split_local_name(raw) == ('bob', onion)

@pytest.mark.parametrize('raw,name', [(f'bob@{HELL.upper()}', 'bob'), (f'@Bob@{HELL}', 'Bob'), (f'!bob@{HELL}', 'bob')])
def test_a_hosted_handle_is_on_its_hosts_domain(app, two_domains, raw, name):
    hell, onion = two_domains.domains
    with onion_request(app):
        assert split_local_name(raw) == (name, hell)      # the name as typed, less its prefix

@pytest.mark.parametrize('raw', ['bob@mastodon.social', '', '@', 'a@b@c'])
def test_anything_else_is_none(app, two_domains, raw): assert split_local_name(raw) is None

def test_local_named_finds_only_that_domains_row(app, two_domains):
    hell, onion = two_domains.domains
    hb, ob = two_domains.local_user('bob', hell), two_domains.local_user('bob', onion)
    assert User.query.filter(local_named(User, 'BOB', onion)).all() == [ob]
    assert User.query.filter(local_named(User, 'bob', hell)).all() == [hb]

def test_local_path_name_qualifies_only_off_its_own_domain(app, two_domains):
    # hell's bob on hell -> 'bob'; on the onion -> 'bob@<hell host>'; onion's bob on hell -> 'bob@<onion host>'
```

- [ ] **Step 2: Run to verify failure** — `./run_tests.sh tests/test_local_names.py -q`; expected: FAIL, `ModuleNotFoundError: app.local_names`.
- [ ] **Step 3: Implement** the three functions and the fixture module. `split_local_name` keeps the name's case (callers compare with `local_named`, which lowers).
- [ ] **Step 4: Run to verify pass** — the same command, plus `./run_tests.sh tests/test_resolve_object_domains.py -q`; both PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat: one place turns a local name or handle into its domain's row (C1)"`

### Task 2: Links name the domain when it is not the storefront's

**Files:**
- Modify: `app/models.py:1472` (`Community.link`), `:2128` (`User.link`); `lemmy_link` at `:1475` and `:2134` use the row's home domain host instead of `SERVER_NAME`.
- Modify: `app/storefront.py:350` (`_rebase`) and its callers in `rewrite_links` (`:403`).
- Test: `tests/test_per_domain_links.py`

**Interfaces:**
- Consumes: `local_path_name`, `split_local_name` (Task 1).
- Produces: `User.link()` / `Community.link()` for a local row return `local_path_name(self)`. Remote rows are unchanged. When `_rebase` moves an absolute URL from our host A onto storefront B, and A's domain is not B's, a path of exactly `/u/<name>` or `/c/<name>` (`<name>` with no `@`, any query and fragment kept) becomes `/u/<name>@<A's domain>`. Every other path is unchanged.

- [ ] **Step 1: Write the failing tests**

```python
def test_links_on_the_default_storefront_are_unchanged(app, two_domains):   # hell's bob -> 'bob'; lemmy_link 'bob@<hell>'
def test_on_the_onion_a_hell_account_links_with_its_domain(app, two_domains):  # hell's bob.link() == 'bob@<hell>'
def test_on_hell_an_onion_community_links_with_its_domain(app, two_domains):   # onion's art.link() == 'art@<onion>'

def test_a_stored_hell_profile_link_on_the_onion_keeps_its_account(app, two_domains):
    html = f'<a href="https://{HELL}/u/bob">bob</a> <a href="https://{HELL}/c/art?x=1#y">art</a> <a href="https://{HELL}/post/5">p</a>'
    with onion_request(app):
        out = storefront_html(html)
    assert f'http://{ONION}/u/bob@{HELL}"' in out and f'http://{ONION}/c/art@{HELL}?x=1#y"' in out
    assert f'http://{ONION}/post/5"' in out

def test_an_onion_recipients_email_links_by_their_domain(app, two_domains):
    # under home_storefront(onion account) (plan B pin), outside a request: an onion user links bare, a hell user with @<hell>
```

- [ ] **Step 2: Run to verify failure** — `./run_tests.sh tests/test_per_domain_links.py -q`; FAIL.
- [ ] **Step 3: Implement.** Do the path rewrite inside `_rebase`, so links, srcset and text get it together. Match the path with one anchored pattern, `^/(u|c)/([^/@?#]+)$`, applied to the path part only.
- [ ] **Step 4: Run to verify pass** — the same command, plus `./run_tests.sh tests/test_storefront_presentation.py tests/test_storefront_request.py -q`. All PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat: a link to another domain's account or community names that domain (C1)"`

### Task 3: ActivityPub and WebFinger routes look names up on their domain

**Files:**
- Modify: `app/activitypub/routes.py`. These are the bare-name sites the recon lists:
  - `:214` and `:225`/`:229`: WebFinger user and feed;
  - `:492` and `:501`: `user_profile` and its deleted lookup;
  - `:629` and `:633`: `local_actor_user`, behind outbox, followers, following and liked;
  - `:729`: `community_profile`;
  - `:2544`, `:2576`, `:2611`, `:2647`, `:2676`: community outbox, featured, moderators, followers and following;
  - `:2953`: `/c/<community_name>/p/...`.
- Modify: `app/activitypub/actor.py:410-414`, the bare-name fallback in `find_actor_by_url`.
- Test: `tests/test_per_domain_ap_routes.py`

**Interfaces:**
- Consumes: `split_local_name`, `local_named` (Task 1).
- Produces: every route above resolves `<actor>` with `split_local_name`, and then `local_named` plus its existing filters. A `None` split (unhosted handle) keeps today's remote path. WebFinger `acct:name@host` looks the name up on `domain_for_host(host)`. The `SERVER_URL` `ap_profile_id` fallbacks (`:496` and others) stay as they are, so first-domain rows keep matching.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize('path', ['/u/bob', '/u/bob/outbox', '/u/bob/followers', '/c/art', '/c/art/outbox', '/c/art/featured'])
def test_hell_ap_documents_name_hells_row(app, two_domains, path):
    # AP GET on the hell host: the document's id is hell's row's, never the onion's (two_domains has bob and art on both)

def test_webfinger_answers_for_the_hosts_own_account(app, two_domains):
    # acct:bob@<hell> -> hell bob's self link; byte-identical to the answer with no onion bob present

def test_a_hosted_handle_with_no_account_is_404_and_fetches_nothing(app, two_domains, no_http):   # /u/nobody@<onion>
def test_an_uppercase_handle_resolves(app, two_domains):   # /u/BOB@<HELL> HTML on the onion -> hell's bob
```

`no_http` is the `requests` recorder in `tests/test_resolve_object_domains.py`, moved into `tests/two_domains.py`.

- [ ] **Step 2: Run to verify failure** — `./run_tests.sh tests/test_per_domain_ap_routes.py -q`; FAIL. Before the fix, the duplicate rows make `.first()` pick either row.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run to verify pass** — the same command, plus every existing `tests/test_activitypub_*` and `tests/test_webfinger*` file. All PASS.
- [ ] **Step 5: Commit** — `git commit -m "fix: ActivityPub and WebFinger find a local name on its own domain (C1)"`

### Task 4: HTML, API and the remaining name lookups

**Files:**
- Modify: `app/user/routes.py`: `:305`, `:1802`, `:1851`, `:2149`, `:2269`, `:2345`, `:2374`, `:2395`, `:2413`, plus every other `<actor>` route in the file.
- Modify: `app/community/routes.py`: `:850`, `:947`, `:1083`, plus every other `<actor>`/`<community_name>` route in the file.
- Modify: `app/community/util.py:548`, `actor_to_community`.
- Modify: `app/feed/util.py:57` and `:117`, for the user or community part only (feed names stay global).
- Modify: `app/api/alpha/utils/user.py:933`, the API login. A bare name means the storefront's domain, and an unhosted handle fails exactly as a wrong password does, as web login does (`find_user`).
- Modify: `app/main/routes.py:651` and `:669` (modlog), `app/discovery/credits.py:295`, `app/models.py:5066`.
- Test: `tests/test_per_domain_html_routes.py`, plus `tests/test_local_name_lookups.py` (the source-scan gate).

**Interfaces:**
- Consumes: Task 1.
- Produces: no bare-name local lookup is left outside `app/local_names.py`. The gate scans `app/` for `User.user_name ==`, `func.lower(User.user_name)`, `Community.name ==`, `func.lower(Community.name)`, and `filter_by(` with `user_name=`/`name=` together with `ap_id=None`. Allowed: `app/local_names.py`, `user_name_is_taken` (Task 5), and an explicit allowlist. Each allowlist entry carries a one-line reason: it is a remote-row lookup, a lookup keyed by `ap_profile_id`, or it is scoped by `ap_domain`.

- [ ] **Step 1: Write the failing tests**

```python
def test_on_the_onion_a_hell_profiles_buttons_act_on_hells_account(app, two_domains, onion_login):
    # logged in on the onion, POST /u/bob@<hell>/block blocks hell's bob; POST /u/bob/block blocks the onion's bob

def test_api_login_with_a_bare_name_is_the_storefronts_account(app, two_domains):    # different passwords per bob
def test_api_login_with_an_unhosted_handle_fails_like_a_wrong_password(app, two_domains):

def test_no_bare_name_lookup_is_left_unscoped():          # tests/test_local_name_lookups.py, the source scan above
```

- [ ] **Step 2: Run to verify failure** — `./run_tests.sh tests/test_per_domain_html_routes.py tests/test_local_name_lookups.py -q`; FAIL. The scan lists every remaining site.
- [ ] **Step 3: Implement.** Move each site onto `split_local_name` + `local_named`. A site that legitimately looks up any domain goes on the allowlist with its reason, never silently.
- [ ] **Step 4: Run to verify pass** — the same command, plus the user, community, feed and API test files those modules already have. All PASS.
- [ ] **Step 5: Commit** — `git commit -m "fix: every page and API call finds a local name on its own domain (C1)"`

### Task 5: Names are unique per domain at creation

**Files:**
- Modify: `app/utils.py:6669`. `user_name_is_taken(user_name, site_domain_id)` makes `site_domain_id` required. The Feed check stays global.
- Modify: the callers, each passing the domain the new row will belong to:
  - `app/auth/forms.py:71-85`, registration: `current_storefront().id`;
  - `app/auth/oauth_util.py:300` and `app/auth/util.py:667`: default domain;
  - `app/admin/forms.py:395-403`, admin add user: the form's `site_domain` field;
  - `app/admin/forms.py:477`, admin add community: default domain;
  - `app/community/forms.py:126-141` and `app/shared/community.py:303-336`, community create: the creator's `home_storefront`;
  - `app/feed/forms.py:46-60` and `app/shared/feed.py:311`: the user and community part on the owner's home domain, the feed part global;
  - `app/cli.py:277-299`, the admin CLI: default domain, and it now checks at all.
- Modify: `app/admin/routes.py:2360`. `form.site_domain.data or None` becomes the default domain's id when the field is empty.
- Test: `tests/test_per_domain_name_creation.py`. Update the plan B tests that pin the global refusal; list each one in the report.

**Interfaces:**
- Consumes: Task 1.
- Produces: the same name can be created on two domains, through every path. A name already held on the same domain is refused with the existing messages.

- [ ] **Step 1: Write the failing tests**

```python
def test_an_admin_can_add_an_onion_bob_when_hell_has_one(app, two_domains, admin_login):   # admin add-user, domain = onion
def test_registering_hell_bob_works_when_only_the_onion_has_a_bob(app, two_domains):      # public registration on hell; the onion's bob was admin-added
def test_a_name_is_still_refused_on_its_own_domain(app, two_domains):                        # every path above, same domain -> refused
def test_a_community_name_on_one_domain_does_not_block_the_other(app, two_domains):
def test_a_feed_name_is_still_global(app, two_domains):
def test_a_new_local_row_never_has_a_null_domain(app, two_domains):                          # admin add with empty picker -> default id
```

- [ ] **Step 2: Run to verify failure** — `./run_tests.sh tests/test_per_domain_name_creation.py -q`; FAIL.
- [ ] **Step 3: Implement.** A `TypeError` from a caller that still passes no domain is the check that all of them were found. Drop the "until plan C" sentence from the docstring.
- [ ] **Step 4: Run to verify pass** — the full suite with coverage, `./run_tests.sh -q --cov=app --cov-branch --cov-report=json`, then `python tests/check_changed_line_coverage.py coverage.json <BASE> --branches`. Both clean.
- [ ] **Step 5: Commit** — `git commit -m "feat: a local name is unique per domain, so two domains can each have a bob (C1)"`

### Task 6: Deploy and verify (controller, after the owner pushes)

- [ ] **Step 1 (before the push):** save the first-domain identity set to `~/backups/<date>-identity/before/`, as plan B Task 13 did (DEPLOY_GUIDE §11, "Check after a deploy that touches identity").
- [ ] **Step 2:** after the autodeploy, fetch the same set and `cmp`. Only `updated` and NodeInfo counts may differ. Check for 0 tracebacks in the three app units over 10 min.
- [ ] **Step 3:** on the onion, as a logged-in first-domain user, open a post holding a first-domain profile link. It goes to `/u/<name>@example.org`.
- [ ] **Step 4:** update DEPLOY_GUIDE §11 "Accounts on the onion": names are per domain, and an admin may reuse a first-domain name for an onion account. Update the memory `onion-storefront-track`.
