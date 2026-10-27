# Per-domain feeds (follow-up F4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every local feed belongs to one hosted domain, as communities do: its name, links, lists, ActivityPub identity and local-only rules are that domain's, and nothing about an onion feed reaches another domain, a logged-out visitor off the onion, or another server.

**Architecture:** A `feed.site_domain_id` column (backfilled to the default domain) and a per-domain partial unique index replace the global `Feed.name` uniqueness. Feeds then reuse the community machinery unchanged: `local_named`/`named_by_actor` gain a Feed arm, `owner_domain()` returns a feed's home domain (the central switch), one builder `feed_identity(domain, name)` replaces the three hand-built SERVER_NAME id blocks, and the federation routes build on `owner_base(feed)`. A new module `app/feed_domains.py` composes the existing storefront and local-only helpers into the few feed decisions that have no community twin: which domain's feeds a viewer's lists show (with the onion fallback), whether a feed is shown on this storefront, and which of a feed's communities this viewer or server may see.

**Tech Stack:** Flask, SQLAlchemy/Postgres 17, Alembic, Celery, Jinja, pytest via `./run_tests.sh`.

**Spec:** `docs/superpowers/specs/2026-10-26-per-domain-feeds-design.md` (binding). Prior art: plans `2026-10-20-per-domain-names.md` (C1), `2026-10-21-per-domain-instances.md` (C2), `2026-10-22-per-domain-federation.md` (C3); migration `b7e3c1a9d5f2_per_domain_ownership.py`.

## Global Constraints

- Mirror how communities became per-domain (spec §3): the `b7e3c1a9d5f2` column/backfill/partial-index pattern; `instance_id_of(domain)` at creation; `local_names` lookups, `path_name`/`stored_path_name`, `domain_base_url`/`domain_authority`; the `federation_urls` owner helpers; WebFinger; `local_only` clauses. Reuse those helpers; write no feed-only copies.
- `owner_domain()` in `app/federation_urls.py` is the central switch for a feed's federated URLs.
- Feeds sign with their own key (`feed.private_key`). No per-domain key.
- Existing feeds belong to the first domain. Their URLs, `ap_profile_id`, actor documents, WebFinger answers, keys and federation stay byte-identical. Stored ids are never rewritten.
- `get_feed` keeps accepting `name@ap_domain`; every API feed call stays compatible.
- TDD, one commit per task.
- 100% changed-line and branch coverage: `python tests/check_changed_line_coverage.py coverage.json <BASE> --branches`, no pragmas. `<BASE>` is `git rev-parse HEAD` recorded before Task 1.
- Inline imports only if technically unavoidable. Break a cycle with the module-import pattern (`import app.models as models`, as `app/local_only.py` does).
- Slow tests are bugs: find and fix the cause, never only report it.
- Invalid client input is a 400 or 404, never a 500.
- No tracked file may contain the first domain's literal host or its bare stem (`tests/test_no_first_domain_literal.py`). Use `example.org`, `shop.example`, `abc.onion` or the fixture's `FIRST`.
- Test runs: `./run_tests.sh <paths>`. Full gate: `./run_tests.sh -q --cov=app --cov-branch --cov-report=json:coverage.json -p no:randomly`.
- Before each run, `pgrep -a -x podman | grep pyfedi_test-runner` must print nothing. Never run two runs at once, never kill a run, never use `timeout`.
- Implementer commit trailer: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- The owner pushes. `deploy/` is git-ignored and local-only.

## Review Focus

1. Case and host spelling: `/f/NEWS@SHOP.EXAMPLE` and WebFinger `acct:~News@Shop.Example` resolve like their lower-case forms; `/f/news@<hosted host>` with no such feed is a 404 with no HTTP fetch. (Task 4, Task 6)
2. A private feed (name `slug/owner`) on another domain: `/f/slug/owner@shop.example` opens it for its owner, and its `.rss` URL answers 200 or 404, never 500. (Task 4)
3. A sister-domain account's feed renamed by a first-domain admin (via the first domain's storefront) keeps its home domain: new ids on shop.example, `site_domain_id` unchanged. A `parent_feed_id` that is missing, not an integer, or on another domain is a 400. (Task 3)
4. The API from a clearnet host: `get_feed(name='onionfeed@abc.onion')`, `get_feed(id=<onion feed id>)` and `resolve_object(q='http://abc.onion/f/onionfeed')` answer `feed_not_found` / "No object found." (400), never the feed and never a 500; `name='a@b@c'` stays a 400. (Task 9)
5. Cache bleed: `menu_instance_feeds` and the anonymous `/feeds` page rendered on shop.example and then on the first domain (and in the reverse order) each show their own domain's feeds. (Task 5)

---

### Task 1: Feeds have a home domain column, backfilled to the first domain

**Files:**
- Create: `migrations/versions/d5a9c3e7f1b4_per_domain_feeds.py` (revision `d5a9c3e7f1b4`, down_revision `c6e2a8f4b1d9`, the current head after follow-up F6).
- Modify: `app/models.py:5946-5960` Feed: `site_domain_id = db.Column(db.Integer, db.ForeignKey('site_domain.id', name='fk_feed_site_domain_id'))` with the comment Community carries at `:1631`; `name` becomes `db.Column(db.String(256), index=True)` (no `unique`); `__table_args__` with `ix_feed_domain_lower_name` exactly as Community's `:1706-1713`; `home_domain()` as `Community.home_domain()` (`:1717-1719`).
- Modify: `tests/factories.py:237` `make_local_feed(name='localfeed', public=False, domain: DomainInfo | None = None, user=None)`: with a domain, stamps `site_domain_id`, `instance_id_of(domain)`, `ap_domain=domain_authority(domain)` and the five URLs on `domain_base_url(domain)`; without one, today's row plus the default domain's id.
- Create: `tests/three_domains.py`: fixture `three_domains(app, db_session)` built on `tests/two_domains.two_domains`, adding a federating `SiteDomain(domain='shop.example', scheme='https', federates=True)`; yields `ThreeDomains(first, shop, onion, account, local_feed, local_community)` where `account(name, domain) -> User` is `two_domains.local_user` plus keys and `verified=True`, `local_feed(name, domain, owner=None, public=True) -> Feed` wraps `make_local_feed` (owner gets a `FeedMember(is_owner=True)`), and `local_community` is `two_domains.local_community`. Re-export `request_on`.
- Modify: `tests/test_c3_golden_documents.py:38-88, 113-125`.
- Test: `tests/test_per_domain_feeds_migration.py`.

**Interfaces:**
- Consumes: `domain_by_id`, `on_domain`, `domain_base_url`, `domain_authority`, `instance_id_of` (`app/storefront.py`).
- Produces: `Feed.site_domain_id: int | None`; `Feed.home_domain() -> DomainInfo` (the default domain for NULL); migration constant `BACKFILL_FEEDS = 'UPDATE "feed" SET site_domain_id = (SELECT id FROM site_domain WHERE is_default) WHERE ap_id IS NULL'`; index `ix_feed_domain_lower_name` on `(site_domain_id, lower(name)) WHERE ap_id IS NULL`; fixtures `three_domains`, `make_local_feed(domain=...)`.

- [ ] **Step 1: Capture the feed collections' golden copies from the unchanged code.** In the golden fixture give the feed the URLs `make_feed` stores (`ap_followers_url`, `ap_following_url`, `ap_outbox_url` on `{base}/f/goldfeed`) and one `FeedItem` holding `goldart`. Add to `GOLDEN_PATHS`: `'feed_outbox.json': '/f/goldfeed/outbox'`, `'feed_following.json': '/f/goldfeed/following'`, `'feed_followers.json': '/f/goldfeed/followers'`, `'feed_moderators.json': '/f/goldfeed/moderators'`. Run `./run_tests.sh tests/test_c3_golden_documents.py -q` twice: the first run captures the four files and fails; the second passes, with `feed.json`, `webfinger_feed.json` and every other existing copy unchanged. Review the four new files.

- [ ] **Step 2: Write the failing tests**

```python
def test_the_migration_backfills_every_local_feed_to_the_default_domain(app, three_domains):
    local, remote = make_local_feed('localf'), make_feed(peer, 'remotef')   # make_feed sets ap_id
    db.session.execute(text('UPDATE feed SET site_domain_id = NULL')); db.session.commit()
    db.session.execute(text(migration.BACKFILL_FEEDS)); db.session.commit(); db.session.expire_all()
    assert db.session.get(Feed, local.id).site_domain_id == three_domains.first.id
    assert db.session.get(Feed, remote.id).site_domain_id is None

def test_a_feed_name_is_unique_per_domain_case_insensitively(app, three_domains):
    t = three_domains
    t.local_feed('news', t.first); t.local_feed('news', t.shop)          # two domains: allowed
    with pytest.raises(IntegrityError):
        t.local_feed('News', t.first)

def test_remote_feeds_are_not_held_to_the_index(app, three_domains): ...   # two remote 'news' rows commit

@pytest.mark.parametrize('stamped', [None, 'first', 'shop'])
def test_a_feeds_home_domain_is_its_own_or_the_default(app, three_domains, stamped): ...
```

In the golden fixture stamp the feed `site_domain_id=first.id` (the post-backfill state).

- [ ] **Step 3: Run to verify failure.** `./run_tests.sh tests/test_per_domain_feeds_migration.py -q`; expected: FAIL, `Feed has no attribute site_domain_id` / migration file missing.

- [ ] **Step 4: Implement the migration and the model.** `upgrade()`: add the column and FK `fk_feed_site_domain_id`; run `BACKFILL_FEEDS`; drop `ix_feed_name` and recreate it non-unique; create `ix_feed_domain_lower_name` (`postgresql_where=sa.text('ap_id IS NULL')`). `downgrade()` reverses in order; recreating the unique `ix_feed_name` fails while two domains share a feed name, and its docstring says so.

- [ ] **Step 5: Run to verify pass.** `./run_tests.sh tests/test_per_domain_feeds_migration.py tests/test_c3_golden_documents.py tests/test_factories_feed.py tests/test_per_domain_ownership.py -q`; expected: PASS, every golden copy byte-identical.

- [ ] **Step 6: Commit.** `feat: each local feed has a home domain, the first domain for existing ones (F4)`.

### Task 2: A feed's owner domain, names and URL builders are its home domain's

**Files:**
- Modify: `app/federation_urls.py:37-48` (`owner_domain`: `models.Feed` arm returns `obj.home_domain() if obj.is_local() else None`; docstring updated), `:205-216` (`_named_row`: drop the Feed special case; Feed goes through `named_by_actor`/`local_named` like User and Community).
- Modify: `app/local_names.py:9, 31-36, 55-58, 61-67`: `local_named`, `local_path_name` and `actor_handle` accept `Feed` (column `Feed.name`).
- Modify: `app/models.py` Feed `:6031-6040, 6087-6093`: `link()` → `storefront.path_name(self.name, self.site_domain_id)` for a local feed; new `stored_link()` as Community's `:1759-1762`; `lemmy_link()` → `~{name}@{storefront.link_host(self.home_domain())}`; `profile_id()`/`public_url()` fallbacks on `storefront.domain_base_url(self.home_domain())` (Community's `:1838-1846`, including its comment on why not `owner_base`).
- Modify: `app/shared/feed.py:211`: add `feed_identity` beside `feed_machine_name`.
- Test: `tests/test_feed_owner_domain.py`.

**Interfaces:**
- Consumes: Task 1's `Feed.home_domain()`, `three_domains`.
- Produces:
  - `owner_domain(feed) -> DomainInfo | None`; so `owner_base(feed)`, `activity_id(verb, feed)` and `owned_elsewhere(feed)` follow the feed's domain with no further change.
  - `local_named(Feed, name, domain)`, `named_by_actor(Feed, actor)`, `local_clause(Feed, actor)`.
  - `Feed.link() -> str`, `Feed.stored_link() -> str`, `Feed.lemmy_link() -> str`.
  - `feed_identity(domain: DomainInfo, name: str) -> dict` with keys `ap_profile_id`, `ap_public_url`, `ap_followers_url`, `ap_following_url`, `ap_outbox_url` (all on `f'{domain_base_url(domain)}/f/{name}'`), `ap_domain=domain_authority(domain)`, `site_domain_id=domain.id`, `instance_id=instance_id_of(domain)`. The onion's ids are minted on the onion's own base (spec goal 12), as `domain_base_url` does for onion communities.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_local_feeds_owner_is_its_home_domain(app, three_domains): ...   # first, shop, onion; remote feed -> None

def test_feed_identity_for_the_first_domain_is_todays_strings(app, three_domains):
    base = f'https://{FIRST}/f/news'
    assert feed_identity(three_domains.first, 'news') == {
        'ap_profile_id': base, 'ap_public_url': base, 'ap_followers_url': f'{base}/followers',
        'ap_following_url': f'{base}/following', 'ap_outbox_url': f'{base}/outbox', 'ap_domain': FIRST,
        'site_domain_id': three_domains.first.id, 'instance_id': three_domains.first.instance_id}

def test_feed_identity_for_shop_and_the_onion_is_on_their_own_base(app, three_domains):
    assert feed_identity(three_domains.shop, 'news')['ap_profile_id'] == 'https://shop.example/f/news'
    assert feed_identity(three_domains.onion, 'news')['ap_profile_id'] == 'http://abc.onion/f/news'

def test_each_domains_feed_url_names_its_own_row(app, three_domains):
    first, shop = three_domains.local_feed('news', three_domains.first), three_domains.local_feed('news', three_domains.shop)
    assert local_object_for_url('https://shop.example/f/news') == shop
    assert local_object_for_url(f'https://{FIRST}/f/news') == first

def test_a_feed_link_is_bare_on_its_storefront_and_qualified_elsewhere(app, three_domains):
    # shop feed: 'news' on shop, 'news@shop.example' on the first domain; stored_link(): 'news@shop.example' anywhere,
    # a first-domain feed's stored_link() bare; lemmy_link(): '~news@shop.example'
```

- [ ] **Step 2: Run to verify failure.** `./run_tests.sh tests/test_feed_owner_domain.py -q`; expected: FAIL (`feed_identity` undefined; shop URL resolves to the first domain's row).
- [ ] **Step 3: Implement** the signatures above. In `local_names`, pick the column with a small mapping `{User: User.user_name, Community: Community.name, Feed: Feed.name}`.
- [ ] **Step 4: Run to verify pass.** `./run_tests.sh tests/test_feed_owner_domain.py tests/test_federation_urls.py tests/test_local_names.py tests/test_c3_golden_documents.py -q`; expected: PASS.
- [ ] **Step 5: Commit.** `feat: a feed's owner domain, links and ids are its home domain's (F4)`.

### Task 3: Creation, edit and copy make a feed on the home domain, in one-domain trees

**Files:**
- Modify: `app/shared/feed.py:229-243` (`store_icon_and_banner(icon, banner, owner: DomainInfo)`: uploads on the feed's domain, replacing the C1 `default_domain()` owner), `:245-360` `make_feed` (home = `user.home_domain()`; `Feed(**feed_identity(home, url), ...)` replaces `base`, `ap_domain` and `instance_id=1`; parent via `parent_feed_on`), `:372-465` `edit_feed` (rename rebuilds the five URLs with `feed_identity(feed.home_domain(), url)`; the domain never changes; parent via `parent_feed_on`).
- Modify: `app/feed/routes.py:251-330` `feed_copy` (home = `current_user.home_domain()`; `feed_identity(home, ...)`; parent via `parent_feed_on`), `:165-178` `feed_edit` and `:53-110` `feed_new` (parent choices `feeds_for_form(..., domain)`).
- Modify: `app/feed/util.py:19-37` `feeds_for_form(current_feed: int, user_id: int, domain: DomainInfo)`; `app/utils.py:4528` `feed_tree(user_id, domain: DomainInfo | None = None)` (restricted with `on_domain(domain, Feed)` when given).
- Modify: `tests/test_federated_id_literals.py:54-61`: remove the `make_feed`, `edit_feed` and `feed_copy` entries.
- Test: `tests/test_per_domain_feed_creation.py`.

**Interfaces:**
- Consumes: Task 2 `feed_identity`, `Feed.home_domain()`.
- Produces: `parent_feed_on(domain: DomainInfo, parent_feed_id) -> int | None` in `app/shared/feed.py`: None for a falsy id; the id when it names a feed whose home domain is `domain`; else `ValueError('parent_feed_invalid')` (missing, non-integer, or on another domain). The API arm re-raises it as `Exception('parent_feed_invalid')` (the API's existing 400, as `feed_has_subscribers`); the web arm calls `abort(400)`. `feed_tree(user_id, domain=None)`, `feeds_for_form(current_feed, user_id, domain)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_feed_belongs_to_its_creators_home_domain(app, three_domains):
    # a shop account POSTs /feed/new on the FIRST domain's host
    feed = Feed.query.filter_by(name='shopnews').one()
    assert feed.site_domain_id == three_domains.shop.id and feed.instance_id == three_domains.shop.instance_id
    assert feed.ap_profile_id == 'https://shop.example/f/shopnews' and feed.ap_domain == 'shop.example'

def test_a_first_domain_feed_gets_exactly_todays_ids(app, three_domains): ...   # ap_* == f'https://{FIRST}/f/x...', instance_id 1
def test_an_onion_creator_gets_onion_ids(app, three_domains): ...                # http://abc.onion/f/x, site_domain_id onion
def test_the_api_make_feed_follows_the_token_users_home(app, three_domains): ...

def test_a_rename_keeps_the_feeds_home_domain(app, three_domains):   # Review Focus 3
    # a first-domain admin, on the first domain's host, renames a shop account's feed (1 subscriber)
    assert feed.site_domain_id == three_domains.shop.id and feed.ap_profile_id == 'https://shop.example/f/renamed'

@pytest.mark.parametrize('parent', ['missing', 'abc', 'other_domain'])
def test_a_bad_parent_is_refused(app, three_domains, parent):        # Review Focus 3
    # web create and edit -> 400; API create and edit -> 400 with 'parent_feed_invalid'; no Feed row changed

def test_feed_copy_makes_the_copy_on_the_copiers_home_domain(app, three_domains): ...   # source on first, copier on shop
def test_parent_choices_are_the_feeds_own_domain_only(app, three_domains): ...          # feeds_for_form
def test_a_feed_icon_is_uploaded_on_the_feeds_domain(app, three_domains, monkeypatch): ...  # process_upload owner == shop
```

- [ ] **Step 2: Run to verify failure.** `./run_tests.sh tests/test_per_domain_feed_creation.py -q`; expected: FAIL (feeds land on the first domain with `instance_id=1`).
- [ ] **Step 3: Implement** the changes above. The name pre-checks (`make_feed:316`, `feed_copy:292`) stay global until Task 9.
- [ ] **Step 4: Run to verify pass.** `./run_tests.sh tests/test_per_domain_feed_creation.py tests/test_shared_feed_identity.py tests/test_shared_feed_edit.py tests/test_feed_copy_routes.py tests/test_feed_lifecycle_routes.py tests/test_api_feeds.py tests/test_federated_id_literals.py -q`; expected: PASS.
- [ ] **Step 5: Commit.** `feat: a new or copied feed is made on its creator's home domain, its tree on one domain (F4)`.

### Task 4: `/f/name` is this storefront's feed, `/f/name@domain` another's

**Files:**
- Modify: `app/feed/util.py:113-119` `actor_to_feed(actor) -> Feed | None`: `Feed.query.filter(named_by_actor(Feed, actor.strip().lower()), Feed.banned.is_(False)).first()`. One resolver for every path segment that names a feed.
- Modify: `app/feed/util.py:56-59` `search_for_feed` hosted arm: `local_named(Feed, name, domain_for_host(server))`, as `search_for_community:108-109`.
- Modify: `app/activitypub/routes.py:3768-3784` `feed_profile` and `:3880, 3916, 3960, 4005` (the collections): the lookup is `actor_to_feed`. An ActivityPub request whose actor holds `@` stays a 400.
- Modify: `app/shared/feed.py:32-45` `join_feed` (`actor_to_feed`; `remote = not feed.is_local()`).
- Modify: `app/feed/routes.py`: `:666` `feed_create_post` and `:855` `show_feed_rss` (last segment) via `actor_to_feed`; `:203` edit redirect, `:522, 527` login `next`, `:599-601` paging `url_for`, all from `feed.link()`; `:735` unsubscribe (`if not feed.is_local()`); `:804-806` lookup (a hosted domain redirects to `'/f/' + path_name(feedname, domain_for_host(domain).id)`).
- Modify: `app/models.py:6042-6052` `Feed.path()`: the last segment is `path_name(machine_name, site_domain_id)`, parents unchanged.
- Modify templates (each `feed.ap_id if feed.ap_id else feed.name`, `feed_menu_item.ap_id if ... else ...name` and `feed_name=feed.machine_name` becomes `.link()`): `feed/_feed_table_row.html`, `feed/show_feed.html:142-153, 174`, `feed/_feed_nav.html:141`, `feeds_menu.html`, `explore_menu.html`, `explore.html`, `themes/dillo/base.html`, `user/alerts/_feeds.html`.
- Test: `tests/test_per_domain_feed_routes.py`.

**Interfaces:**
- Consumes: Task 2 `named_by_actor(Feed, ...)`, `Feed.link()`.
- Produces: `actor_to_feed(actor: str) -> Feed | None`, the one name-to-feed resolver (Task 7 adds the shown-here rule inside it).

- [ ] **Step 1: Write the failing tests**

```python
def test_a_bare_name_opens_this_storefronts_feed(app, three_domains):
    # 'news' on the first domain (title 'First news') and on shop ('Shop news')
    assert b'First news' in get('/f/news', host=FIRST).data and b'Shop news' in get('/f/news', host='shop.example').data

@pytest.mark.parametrize('path', ['/f/news@shop.example', '/f/NEWS@SHOP.EXAMPLE'])   # Review Focus 1
def test_name_at_domain_opens_the_other_domains_feed(app, three_domains, path): ...  # on FIRST: 'Shop news'

def test_a_hosted_handle_with_no_feed_is_404_without_a_fetch(app, three_domains, no_http):
    assert get('/f/nothing@shop.example', host=FIRST).status_code == 404 and no_http == []

def test_a_private_feed_on_another_domain_opens_by_its_suffixed_path(app, three_domains):   # Review Focus 2
    # owner logged in on FIRST: /f/slug/owner@shop.example -> 200; /f/slug/owner@shop.example.rss -> 200 or 404, not 500

def test_rss_submit_and_subscribe_act_on_the_named_domains_feed(app, three_domains): ...
    # /f/news.rss on shop -> shop's title; /f/news@shop.example/submit on FIRST lists shop feed's communities;
    # POST /feed/news@shop.example/subscribe on FIRST -> FeedMember on the shop feed, no outbound request
def test_lookup_redirects_a_hosted_feed_to_its_path_name(app, three_domains): ...      # 302 /f/news@shop.example
def test_search_for_feed_finds_each_hosted_domains_own_row(app, three_domains, no_http): ...
def test_a_shop_feeds_page_on_the_first_domain_links_it_as_name_at_shop(app, three_domains): ...
    # subscribe action /feed/news@shop.example/subscribe, rss /f/news@shop.example.rss, sub-feed hrefs
```

- [ ] **Step 2: Run to verify failure.** `./run_tests.sh tests/test_per_domain_feed_routes.py -q`; expected: FAIL (bare `/f/news` finds whichever row comes first; `@shop.example` is read as remote).
- [ ] **Step 3: Implement** the changes above. Tests that address a `make_feed(local=True)` row (it carries an `ap_id`, which the product never gives a local feed) as `name@<first host>` move to `make_local_feed`; list each in the report.
- [ ] **Step 4: Run to verify pass.** `./run_tests.sh tests/test_per_domain_feed_routes.py tests/test_feed_reading_routes.py tests/test_feed_create_post.py tests/test_feed_util.py tests/test_shared_feed_lifecycle.py tests/test_feed_visibility_sweep.py -q`; expected: PASS.
- [ ] **Step 5: Commit.** `feat: a feed path names this storefront's feed, or another domain's with @domain (F4)`.

### Task 5: Lists and menus show the view's domain, with the onion fallback

**Files:**
- Create: `app/feed_domains.py`.
- Modify: `app/utils.py:4386-4389` `menu_instance_feeds(site_domain_id: int | None)` (memoized per argument; `cache.delete_memoized(menu_instance_feeds)` with no argument still clears all), `:4542-4556` `feed_tree_public(search_param=None, domain: DomainInfo | None = None)` (filtered by `listed_feeds_clause(domain)`).
- Modify: `app/main/routes.py:473` (`list_communities` feed dropdown), `:1037, 1053, 1494` (menus: `menu_instance_feeds(feed_view_domain().id)`), `:1458-1462` (`list_feeds` HTML), `:1805`; `app/community/routes.py:649-650` (sidebar `community_feeds`).
- Test: `tests/test_per_domain_feed_lists.py`.

**Interfaces:**
- Consumes: `current_storefront`, `default_domain`, `on_domain` (`app/storefront.py`); `current_user`.
- Produces in `app/feed_domains.py`:
  - `feed_view_domain(account=None) -> DomainInfo`: the domain whose feeds a viewer's lists show. `account` is the API's token user; else the signed-in local user; else none. Rule: the account's home domain, or the storefront when there is no account; an account homed on a local-only domain other than the storefront browses as logged out (the storefront); then, when that domain is local-only and owns no public, unbanned local feed, the default domain.
  - `listed_feeds_clause(domain: DomainInfo)`: `or_(Feed.ap_id.isnot(None), and_(Feed.ap_id.is_(None), on_domain(domain, Feed)))`. Remote feeds stay listed everywhere (spec non-goal).
  - Links in every list are `feed.link()` (Task 4), so a home-domain feed shown on another storefront is `/f/name@home` (goal 6a).

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize('who, host, expected', [
    (None, FIRST, 'first'), (None, 'shop.example', 'shop'),
    ('shop', FIRST, 'shop'),                       # an account sees its home domain's feeds anywhere
    ('first', 'abc.onion', 'first'),               # a first-domain account on the onion
    ('onion', 'abc.onion', 'onion'), ('onion', 'shop.example', 'shop'),   # onion account off the onion: as logged out
    (None, 'abc.onion', 'onion')])
def test_feed_view_domain(app, three_domains, who, host, expected): ...   # onion owns a public feed here

def test_the_onion_falls_back_to_the_first_domain_when_it_has_no_feed(app, three_domains): ...
    # onion owns no public feed: onion account on the onion and a logged-out onion visitor -> first
def test_each_storefront_lists_its_own_feeds_and_remote_ones(app, three_domains): ...   # /feeds on FIRST and shop
def test_a_sister_account_on_the_first_domain_sees_qualified_links(app, three_domains):
    assert b'href="/f/shopnews@shop.example"' in page.data and b'href="/f/firstnews"' not in page.data
def test_menu_and_feeds_page_have_no_cache_bleed(app, three_domains): ...   # Review Focus 5: shop then FIRST, FIRST then shop
def test_explore_sidebar_and_community_dropdown_list_only_the_view_domains_feeds(app, three_domains): ...
```

- [ ] **Step 2: Run to verify failure.** `./run_tests.sh tests/test_per_domain_feed_lists.py -q`; expected: FAIL (`app.feed_domains` missing).
- [ ] **Step 3: Implement** the functions and wire every list above to them.
- [ ] **Step 4: Run to verify pass.** `./run_tests.sh tests/test_per_domain_feed_lists.py tests/test_feed_cache.py tests/test_feed_reading_routes.py -q`; expected: PASS.
- [ ] **Step 5: Commit.** `feat: feed lists and menus show the viewer's domain, the onion falling back to the first domain (F4)`.

### Task 6: A sister-domain feed federates and answers WebFinger from its own domain

**Files:**
- Modify: `app/activitypub/routes.py:3786-3860` `feed_profile` document: every URL from `base = f'{owner_base(feed)}/f/{actor}'` (id, inbox, outbox, followers, following, moderators, attributedTo, url, publicKey id/owner), `sharedInbox` `f'{owner_base(feed)}/inbox'`, a relative icon/image prefixed with `owner_base(feed)`, the `Link` header on `base`; `:3978` moderators id and `:4012` followers id on `owner_base(feed)`.
- Modify: `app/activitypub/routes.py:240-248` WebFinger: both feed arms drop `served.is_default` and query `Feed.query.filter(local_clause(Feed, f'{actor.strip()}@{domain}')).filter_by(public=True, banned=False, ap_deleted_at=None).filter(federating_feed_clause())`.
- Modify: `app/main/routes.py:1468-1481` `list_feeds` ActivityPub JSON: `served = federating_storefront()` replaces the default-only abort; `lemmy_site_data(served)`; the list is `served`'s local public federating feeds as `f"{domain_base_url(served)}/f/{machine_name}"`.
- Modify: `app/shared/feed.py:744-806` `announce_feed_add_remove_to_subscribers`: ids `activity_id('announce', feed)` and `activity_id('feedadd', feed)`; the body runs in `acting_for(owner_domain(feed))`.
- Modify: `tests/test_federated_id_literals.py`: remove the `feed_profile`, `feed_moderators_route`, `feed_followers` and `announce_feed_add_remove_to_subscribers` entries and the `FEEDS` reason.
- Test: `tests/test_per_domain_feed_federation.py`.

**Interfaces:**
- Consumes: Task 2 `owner_domain(feed)`, `owner_base`, `activity_id`, `owned_elsewhere`, `local_clause(Feed, ...)`; `federating_storefront()`, `acting_for`.
- Produces: nothing new; the documents and activities now name the feed's home domain.

- [ ] **Step 1: Write the failing tests**

```python
def test_every_url_in_a_shop_feeds_document_is_on_shop(app, three_domains):
    doc = ap_get('/f/shopnews', host='shop.example').json
    for url in urls_in(doc):    # id, inbox, outbox, followers, following, moderators, attributedTo, url,
        assert url.startswith('https://shop.example/')   # publicKey.id/owner, endpoints.sharedInbox, icon, image

def test_a_shop_feeds_document_is_404_on_the_first_domain(app, three_domains): ...
@pytest.mark.parametrize('resource', ['acct:~shopnews@shop.example', 'acct:~ShopNews@Shop.Example'])   # Review Focus 1
def test_webfinger_answers_a_shop_feed_on_shop_only(app, three_domains, resource): ...   # 200 on shop, 404 on FIRST
def test_the_first_domain_no_longer_answers_webfinger_for_a_shop_feed(app, three_domains): ...  # acct:~shopnews@FIRST -> 404
def test_a_shop_feeds_collections_are_on_shop(app, three_domains): ...   # followers/moderators ids
def test_a_follow_of_a_shop_feed_is_accepted_as_shop(app, three_domains, sent): ...
    # Accept actor https://shop.example/f/shopnews, id https://shop.example/activities/accept/..., keyId '#main-key' of that URL
def test_an_announce_of_an_add_is_on_the_feeds_domain(app, three_domains, sent): ...
    # shop feed: ids start https://shop.example/activities/announce/ and /feedadd/; first feed: f'https://{FIRST}/activities/...'
def test_the_feeds_list_document_is_per_federating_domain(app, three_domains): ...   # shop lists shop's; onion 404
```

`sent` records `send_post_request(inbox, body, key, key_id)` with monkeypatch.

- [ ] **Step 2: Run to verify failure.** `./run_tests.sh tests/test_per_domain_feed_federation.py -q`; expected: FAIL (documents on SERVER_NAME; WebFinger gated on the default domain).
- [ ] **Step 3: Implement** the changes above.
- [ ] **Step 4: Run to verify pass.** `./run_tests.sh tests/test_per_domain_feed_federation.py tests/test_c3_golden_documents.py tests/test_federated_id_literals.py tests/test_per_domain_discovery.py tests/test_ap_actor_json_feed.py -q`; expected: PASS, every golden copy byte-identical.
- [ ] **Step 5: Commit.** `feat: a sister domain's feed federates and answers WebFinger from that domain (F4)`.

### Task 7: An onion feed is shown only on the onion and never federates

**Files:**
- Modify: `app/feed_domains.py`: move `feed_local_only` and `federating_feed_clause` here from `app/activitypub/routes.py:3752-3765` (that file imports them) and make them domain-aware; add `feed_shown_here` and `feeds_shown_here_clause`.
- Modify: `app/feed/util.py` `actor_to_feed` (None for a feed not shown here); `app/utils.py:4495` `feed_readable_by` (False for a feed not shown here; this covers every reader of a caller-supplied feed id: tag routes, API post lists, `list_communities`, `feed_copy`); `:4392-4400` `menu_my_feeds` and the callers of `menu_subscribed_feeds` (results through `shown_here`); `feed_tree` (adds `feeds_shown_here_clause()`).
- Modify: `app/user/routes.py:176, 2416-2425, 2447` (a user's feed lists through `feeds_shown_here_clause()`).
- Modify: `app/activitypub/routes.py:1427-1462` inbound Follow: a `feed_local_only(feed)` target is logged `APLOG_FAILURE` 'Local only cannot be followed by remote users' and gets no answer.
- Modify: `app/shared/feed.py:744` announce task: returns before sending anything for a `feed_local_only` feed.
- Test: `tests/test_onion_feeds_hidden.py`.

**Interfaces:**
- Consumes: `federating_clause`, `user_local_only`, `any_domain_closed` (`app/local_only.py`); `on_domain`, `current_storefront`.
- Produces in `app/feed_domains.py`:
  - `feed_local_only(feed) -> bool`: a local feed whose home domain does not federate, or (legacy rows backfilled to the first domain) whose owner is local-only.
  - `federating_feed_clause()`: that rule as a Feed filter: `federating_clause(Feed)` and today's owner subquery; a plain `true()` while every domain federates.
  - `feed_shown_here(feed) -> bool`: a remote feed, a local feed whose home domain federates, or one on the current storefront.
  - `feeds_shown_here_clause()`: the same as a Feed filter: `or_(federating_clause(Feed), and_(Feed.ap_id.is_(None), on_domain(current_storefront(), Feed)))`.
  - `shown_here(feeds: list) -> list`: `feeds` filtered by `feed_shown_here`, for the memoized per-account menus.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize('host, who', [(FIRST, None), (FIRST, 'onion_owner'), ('shop.example', 'onion_owner')])
def test_an_onion_feed_is_404_off_the_onion(app, three_domains, host, who):
    assert get('/f/onionfeed@abc.onion', host=host, as_=who).status_code == 404

@pytest.mark.parametrize('who', [None, 'onion_owner'])
def test_an_onion_feed_opens_on_the_onion(app, three_domains, who): ...   # /f/onionfeed on abc.onion -> 200

def test_an_onion_feed_has_no_actor_or_webfinger_anywhere(app, three_domains): ...
    # AP GET /f/onionfeed on the onion -> 404; WebFinger acct:~onionfeed@abc.onion on FIRST, shop and the onion -> 404
def test_an_onion_feed_by_id_is_unreadable_off_the_onion(app, three_domains): ...   # feed_readable_by False; True on the onion
def test_an_onion_feed_cannot_be_copied_off_the_onion(app, three_domains): ...      # /feed/<id>/copy on FIRST -> 404
def test_an_onion_accounts_lists_off_the_onion_omit_its_onion_feeds(app, three_domains): ...
    # menus, /u/<actor>/myfeeds, the profile's public feeds, feed_tree
def test_a_follow_of_an_onion_feed_gets_no_answer(app, three_domains, sent): ...   # sent == []
def test_an_onion_feed_is_never_announced(app, three_domains, sent): ...           # a remote member, Add -> sent == []
def test_a_legacy_feed_of_an_onion_account_still_does_not_federate(app, three_domains): ...   # on the first domain, 404 to AP
def test_search_for_an_onion_feed_off_the_onion_finds_nothing(app, three_domains, no_http): ...
```

- [ ] **Step 2: Run to verify failure.** `./run_tests.sh tests/test_onion_feeds_hidden.py -q`; expected: FAIL (`/f/onionfeed@abc.onion` answers 200 on the first domain).
- [ ] **Step 3: Implement** the functions and wiring above.
- [ ] **Step 4: Run to verify pass.** `./run_tests.sh tests/test_onion_feeds_hidden.py tests/test_feed_visibility_filters.py tests/test_feed_visibility_sweep.py tests/test_user_profile_feed.py tests/test_per_domain_feed_federation.py -q`; expected: PASS.
- [ ] **Step 5: Commit.** `feat: an onion feed is shown only on the onion and never federates (F4)`.

### Task 8: A feed shows, announces and accepts only the communities its viewer may see

**Files:**
- Modify: `app/feed_domains.py`: add `shown_feed_communities` and `community_shown_here`.
- Modify: `app/feed/routes.py:575-595` `show_feed` (`feed_community_ids` and `feed_communities` through `shown_feed_communities`, so the posts of a skipped community are skipped too), `show_feed_rss` (same), `:666-686` `feed_create_post` (both community lists), `:417-457` `feed_add_community` (404 for a community not `community_shown_here`).
- Modify: `app/shared/feed.py:875-888` `form_communities_to_ids` (skips a community not `community_shown_here`); `:744` announce task (no Add/Remove for a `community_local_only` community).
- Modify: `app/api/alpha/views.py:1216-1222` `feed_view` (adds `Community.instance_id.not_in(hidden_instance_ids())` beside its existing `without_local_only_actors`).
- Test: `tests/test_feed_local_only_contents.py`.

**Interfaces:**
- Consumes: `without_local_only_actors`, `community_local_only`, `local_only_visible` (`app/local_only.py`); `hidden_instance_ids` (`app/storefront.py`).
- Produces in `app/feed_domains.py`:
  - `shown_feed_communities(query, visible: bool | None = None)`: `query` over Community through `without_local_only_actors(query, Community, visible)` and `Community.instance_id.not_in(hidden_instance_ids())`.
  - `community_shown_here(community, visible: bool | None = None) -> bool`: the same rule for one row.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_clearnet_feed_hides_its_onion_community_off_the_onion(app, three_domains):
    # a first-domain feed holds a first-domain and an onion community, each with one post
    page = get(f'/f/mixed', host=FIRST, as_='first_account')
    assert b'first post' in page.data and b'onion post' not in page.data and b'oniongroup' not in page.data
    assert b'onion post' in get('/f/mixed@' + FIRST, host='abc.onion', as_='onion_account').data

def test_a_logged_out_viewer_never_sees_a_local_only_community_in_a_feed(app, three_domains): ...   # on the onion too
def test_rss_and_the_api_view_omit_hidden_communities(app, three_domains): ...
def test_adding_an_onion_community_off_the_onion_is_refused(app, three_domains): ...
    # POST /feed/add_community on FIRST -> 404, no FeedItem; form text '!oniongroup@abc.onion' on FIRST -> skipped
def test_adding_an_onion_community_on_the_onion_is_allowed(app, three_domains): ...
def test_an_onion_community_in_a_clearnet_feed_is_never_announced(app, three_domains, sent): ...
```

- [ ] **Step 2: Run to verify failure.** `./run_tests.sh tests/test_feed_local_only_contents.py -q`; expected: FAIL (a logged-in first-domain viewer sees the onion post).
- [ ] **Step 3: Implement** the functions and wiring above.
- [ ] **Step 4: Run to verify pass.** `./run_tests.sh tests/test_feed_local_only_contents.py tests/test_feed_private_communities.py tests/test_feed_sorts.py tests/test_api_feeds.py tests/test_shared_feed_wiring.py -q`; expected: PASS.
- [ ] **Step 5: Commit.** `feat: a feed shows and announces only the communities its viewer may see (F4)`.

### Task 9: Feed names are per domain, and the API follows the request's storefront

**Files:**
- Modify: `app/feed/forms.py:46` (`local_named(Feed, name, current_user.home_domain())`; drop the "feed name is global" comment); `app/utils.py:6774-6815` `apply_feed_url_rules` (the duplicate check is `local_named(Feed, url, domain)` with `domain = getattr(self, 'feed_domain', None) or current_user.home_domain()`, still excluding `self.feed_id`); `app/feed/routes.py:176` (`edit_feed_form.feed_domain = feed_to_edit.home_domain()`), `:292` copy pre-check on the copier's home; `app/shared/feed.py:316` pre-check on the creator's home.
- Modify: `app/utils.py:6760-6771` `user_name_is_taken` (the feed arm is `local_named(Feed, name, domain)`; docstring: all three names per domain); `app/community/forms.py:139` (`local_named(Feed, ..., current_user.home_domain())`); `app/auth/forms.py:86` (`local_named(Feed, ..., current_storefront())`).
- Modify: `app/api/alpha/utils/feed.py:110-118` `get_feed`: a bare name is `local_named(Feed, name, current_storefront())`; `name@host` on a hosted host is `local_named(Feed, name, domain_for_host(host))`; any other `name@host` is today's `Feed.name == name, Feed.ap_domain == host`; more than one `@` stays `invalid_request`; a feed `feed_readable_by` refuses (Task 7) is `feed_not_found`. `:36-39` list: `feed_tree_public(domain=feed_view_domain(token user))`, `feed_tree(user_id)`.
- Modify: `app/api/alpha/utils/misc.py:156` (an `ap_profile_id` match not `feed_shown_here` is "No object found."), `:350-359` (a `/f/` URL's bare name takes the URL's own hosted domain, `domain_of_url(query)`, else the storefront's `link_host`, replacing `SERVER_NAME`).
- Test: `tests/test_per_domain_feed_names.py`.

**Interfaces:**
- Consumes: Task 2 `local_named(Feed, ...)`, Task 5 `feed_view_domain(account)`, Task 7 `feed_readable_by`, `feed_shown_here`.
- Produces: `EditFeedForm.feed_domain: DomainInfo` (set by the route); nothing else new.

- [ ] **Step 1: Write the failing tests**

```python
def test_two_domains_can_each_create_a_feed_called_news(app, three_domains): ...   # web create by a first and a shop account
def test_a_same_domain_duplicate_is_still_refused_in_any_case(app, three_domains): ...   # 'News' after 'news' on shop

def test_a_feed_name_does_not_block_a_user_or_community_on_another_domain(app, three_domains):
    three_domains.local_feed('news', three_domains.shop)
    assert not user_name_is_taken('news', three_domains.first.id)
    # registering 'news' on FIRST and creating community 'news' as a first-domain user both succeed

def test_a_feed_name_still_blocks_a_user_and_community_on_its_own_domain(app, three_domains): ...
def test_a_rename_is_checked_on_the_feeds_own_domain(app, three_domains): ...   # admin on FIRST renaming a shop feed

@pytest.mark.parametrize('name, host, expected', [('news', 'shop.example', 'shop'), (f'news@{FIRST}', 'shop.example', 'first'),
                                                  ('peerfeed@peer.example', FIRST, 'remote')])
def test_get_feed_by_name_follows_the_request_host(app, three_domains, name, host, expected): ...

@pytest.mark.parametrize('query', [{'name': 'onionfeed@abc.onion'}, {'id': 'ONION_ID'}, {'name': 'a@b@c'}])   # Review Focus 4
def test_get_feed_off_the_onion_never_returns_an_onion_feed(app, three_domains, query): ...   # 400, never 500
def test_resolve_object_never_returns_an_onion_feed_off_the_onion(app, three_domains): ...      # Review Focus 4
def test_resolve_object_by_url_names_the_urls_domain(app, three_domains): ...   # q=https://shop.example/f/news -> shop row
def test_the_feed_list_api_follows_the_storefront_and_token_user(app, three_domains): ...
```

- [ ] **Step 2: Run to verify failure.** `./run_tests.sh tests/test_per_domain_feed_names.py -q`; expected: FAIL ('A feed with that url already exists' for the second `news`; a bare API name is `invalid_request`).
- [ ] **Step 3: Implement** the changes above.
- [ ] **Step 4: Run to verify pass.** `./run_tests.sh tests/test_per_domain_feed_names.py tests/test_feed_forms.py tests/test_apply_feed_url_rules.py tests/test_api_feeds.py tests/test_per_domain_creation.py -q`; expected: PASS.
- [ ] **Step 5: Commit.** `feat: feed names are unique per domain, and the API reads a bare feed name on its own host (F4)`.

### Task 10: End-to-end and golden check

**Files:**
- Test: `tests/test_per_domain_feeds_end_to_end.py`; `tests/test_c3_golden_documents.py` (unchanged copies, re-run).

**Interfaces:**
- Consumes: everything above. Produces: nothing new.

- [ ] **Step 1: Write the tests** (spec §4 rows no earlier task holds whole)

```python
def test_a_feed_holds_communities_from_several_domains_and_remote_servers(app, three_domains):
    # one first-domain feed with a first-domain, a shop and a remote community, one post each:
    # /f/<feed> on FIRST and its .rss show all three posts, as before F4

def test_an_existing_feed_is_unchanged_after_the_backfill(app, three_domains):
    # a row as it was before F4 (site_domain_id NULL), BACKFILL_FEEDS run: ap_profile_id, the actor document,
    # WebFinger and its four collections equal the golden copies
```

- [ ] **Step 2: Run.** `./run_tests.sh tests/test_per_domain_feeds_end_to_end.py tests/test_c3_golden_documents.py tests/test_no_first_domain_literal.py -q`; expected: PASS. A failure here is a defect in the task that owns the code: fix it there, with its own test, in this task's commit.
- [ ] **Step 3: Full gate.** `./run_tests.sh -q --cov=app --cov-branch --cov-report=json:coverage.json -p no:randomly`, then `python tests/check_changed_line_coverage.py coverage.json <BASE> --branches`; expected: all pass, no uncovered changed line or branch.
- [ ] **Step 4: Commit.** `test: per-domain feeds end to end, the first domain's documents unchanged (F4)`.

### Task 11: Deploy and verify on prod (controller)

- [ ] **Pre-flight on prod, read-only.** Count local feeds whose owner's home domain does not federate (legacy onion-owned feeds; Task 7 keeps them off federation, but they stay listed on the first domain). Report the count to the owner before the push. Count `lower(name)` duplicates among local feeds (expect 0: the column was unique).
- [ ] **Before-set.** For each public local feed on the first domain, save the actor document, its four collections and its WebFinger JRD (`Accept: application/activity+json`) to `~/backups/<date>-feeds/before/`. Save the `/feeds` HTML list of feed names.
- [ ] **Backup.** Take the full database backup the DEPLOY_GUIDE backup section describes, and check the file is non-empty.
- [ ] **Owner pushes.** The hourly auto-deploy applies it and runs the migration. Check the alembic head is `d5a9c3e7f1b4` and every local feed has `site_domain_id` set to the default domain's id. Then run `flask restamp-notifications --all` once in the web container, so that notifications naming an onion feed written before this deploy are stamped (follow-up F6's stamp rule now covers feeds).
- [ ] **After-set.** Fetch the same documents to `.../after/` and `cmp` each pair: byte-identical. Check `/feeds` on the first domain lists the same names. Check the onion's `/feeds` lists the first domain's feeds while the onion has none, and `/f/<an onion feed>@<onion host>` is 404 on the first domain if one exists. Check for 0 tracebacks over 10 minutes in every app unit.
- [ ] **Notes.** Add a "Per-domain feeds (F4)" entry to `deploy/DEPLOY_GUIDE.md`: the migration, its downgrade caveat (it fails while two domains share a feed name), and the checks above. Update the memory `onion-storefront-track` with F4 done.
