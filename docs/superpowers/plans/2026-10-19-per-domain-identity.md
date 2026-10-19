# Per-domain identity Implementation Plan (plan B)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every local account and community belongs to one hosted domain, logins resolve per domain, the onion takes no sign-ups, and accounts and content on a federation-off domain never leave the server, with a "Local" badge.

**Architecture:** One migration adds `site_domain_id` to `user` and `community` and `local_only` to `post` and `post_reply`, backfilling every local row to the default domain. New code lives in `app/local_only.py` (who and what is local-only, and the one outbound guard every delivery passes). hell.cloud stays the only federating domain. Per-domain federation for a second domain is plan C, written after this lands.

**Tech Stack:** Flask, SQLAlchemy + Alembic (Postgres 17), Celery, Jinja, pytest via `./run_tests.sh`.

**Spec:** `docs/superpowers/specs/2026-10-19-per-domain-identity-design.md` (§3.1, §3.2, §3.4, §3.5 Local filter, §3.6). Plan A: `docs/superpowers/plans/2026-10-19-access-log-flag.md`. Plan C (§3.3, the §3.5 ranking, the sister-domain `instance` rows and the cross-domain `/post/N` redirect) is out of scope here.

## Global Constraints

- **hell.cloud's identity is byte-identical:** every existing `ap_profile_id`, `ap_public_url`, `ap_inbox_url`, key, actor document, WebFinger answer and NodeInfo shape for default-domain rows is unchanged. New default-domain rows get exactly the URLs they get today (`SERVER_URL`-based).
- A local name is unique per domain, case-insensitively; remote rows (`ap_id IS NOT NULL`) are unaffected. Prod has no case duplicates (checked 2026-10-19).
- A bare login name resolves only in the storefront's domain; `name@domain` only in that hosted domain; an unhosted or unknown domain fails exactly like a wrong password (same message, same `log_login_failure`). Email login resolves site-wide as today.
- Post and comment object IDs stay where they are built today in this plan. **Ruling** — spec §3.1 puts them on the author's home domain; that only matters for a second federating domain, so plan C moves them, and a local-only object's ID never leaves the server anyway — cost if wrong: onion objects keep hell.cloud-host IDs until plan C.
- Local-only = the owning domain has `federates` False. Local-only content is stamped at creation (`local_only` True) and never recomputed.
- Votes and follows are not stamped: they are local-only exactly when their actor is, and the actor's domain cannot change (Task 5 locks it). **Ruling** — spec §3.4 says "stamped on each … vote and follow"; deriving them from the immutable actor domain is equivalent and needs no columns — cost if wrong: two boolean columns added later.
- Nothing local-only reaches another server: no delivery, no AP GET (404), no WebFinger, no NodeInfo count, no outbox or featured entry, no mention `href`, no moderator listing.
- Only logged-in users (or an RSS token user) see local-only content; anonymous requests never do.
- "Local" filters mean the request's domain (`current_storefront()`).
- TDD, one commit per task, 100% changed-line and branch coverage, gates from repo tools (`./run_tests.sh`), inline imports only if technically unavoidable (break cycles), slow tests are bugs. Fuzz campaigns touch no DB: any fuzz target reaching the new helpers stubs them.
- Migration runs through autodeploy (encrypted dump first). The owner pushes. `deploy/` is local-only.

## Review Focus

1. A remote server fetching a hell.cloud post whose comment thread holds a local-only reply: the replies collection and context omit it (no onion URL in any AP response).
2. A hell.cloud user's post mentioning `@x` where `x` is an onion account: the post federates, the mention goes out as plain text, and the onion account gets no delivery.
3. Two accounts `wakko` on hell.cloud and on the onion with different passwords: each password works only for its own login form/handle, and a passkey or NNTP login reaches the right one.
4. A federating user replying, voting and boosting on local-only content: all stored, none sent, and the federating community's Announce of that reply never goes out.
5. Deleting an onion account or post: no Delete is sent (the guard refuses it like any other local-only activity).

Each line's test is in the owning task (Tasks 3, 7, 6, 7, 7).

---

### Task 1: Schema, ownership and `DomainInfo.id`

**Files:**
- Create: `migrations/versions/<rev>_per_domain_ownership.py`
- Modify: `app/models.py` (`User` ~1798, `Community` ~1345, `Post`, `PostReply`, `SiteDomain` 5472)
- Modify: `app/storefront.py:34` (`DomainInfo`), `:86` (`_synthesised_default`), `:94` (`domain_rows`)
- Test: `tests/test_per_domain_ownership.py` (create)

**Interfaces:**
- Produces: `DomainInfo.id: int | None` (None only for the synthesised default); `domain_by_id(site_domain_id: int | None) -> DomainInfo` in `app/storefront.py` (None or unknown → `default_domain()`); columns `user.site_domain_id`, `community.site_domain_id` (FK `site_domain.id`, nullable, NULL for remote rows), `post.local_only`, `post_reply.local_only` (Boolean, not null, server default false); `User.home_domain() -> DomainInfo` and `Community.home_domain() -> DomainInfo`.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_migration_backfills_every_local_user_and_community_to_the_default_domain(app, db_session): ...
    # seed a local user, a local community and a remote user, run the backfill SQL the migration uses,
    # assert local rows -> default row id, remote -> None
def test_a_local_name_is_unique_per_domain_case_insensitively(app, db_session):
    # two local users 'Wakko' and 'wakko' on the default domain -> IntegrityError on flush
    # 'wakko' on the default domain and 'wakko' on an onion row -> both stored
def test_remote_rows_are_not_held_to_the_index(app, db_session): ...   # two remote 'wakko' on different ap_ids
def test_domain_by_id_falls_back_to_the_default(app, db_session):
    assert domain_by_id(None) == default_domain() and domain_by_id(999999) == default_domain()
def test_home_domain_reads_the_owning_row(app, db_session): ...
```

- [ ] **Step 2:** `./run_tests.sh tests/test_per_domain_ownership.py -q` — FAIL (no columns).
- [ ] **Step 3: Implement.** Migration: add the four columns; `UPDATE "user" SET site_domain_id = (SELECT id FROM site_domain WHERE is_default) WHERE ap_id IS NULL`, the same for `community`; partial unique indexes `ix_user_domain_lower_name` on `(site_domain_id, lower(user_name)) WHERE ap_id IS NULL` and `ix_community_domain_lower_name` on `(site_domain_id, lower(name)) WHERE ap_id IS NULL`. Downgrade drops them. `domain_rows()` carries the id.
- [ ] **Step 4:** same command — PASS; `./run_tests.sh tests/test_storefront*.py -q` still PASS.
- [ ] **Step 5: Commit** `feat: each local account and community belongs to a hosted domain`

### Task 2: New accounts and communities get their domain

**Files:**
- Modify: `app/utils.py:4310` (`finalize_user_setup`), `app/auth/util.py:335-372` (`create_new_user`, LDAP creation), `app/auth/oauth_util.py:122`, `app/admin/forms.py:334` (`AddUserForm`), `app/admin/routes.py:2305` (`admin_users_add`), `app/community/routes.py:99-170` (`add_local`), `app/shared/community.py:260-317` (`make_community`), `app/activitypub/util.py:7263-7276` (microblogs community)
- Test: `tests/test_per_domain_creation.py` (create)

**Interfaces:**
- Consumes: Task 1.
- Produces: `domain_base_url(info: DomainInfo) -> str` in `app/storefront.py` (`f'{scheme}://{domain}'`; for the default domain it equals `current_app.config['SERVER_URL']`); `AddUserForm.site_domain` (SelectField of SiteDomain ids, default the default row).

- [ ] **Step 1: Write the failing tests**

```python
def test_registering_on_a_storefront_stamps_its_domain(app, db_session): ...   # anyone-registration row
def test_an_admin_added_onion_user_gets_onion_urls(app, db_session, admin_client):
    # POST /admin/users/add with site_domain=<onion row id>
    # -> user.site_domain_id == onion id; ap_profile_id == 'http://<onion>/u/<name>'; inbox under the onion
def test_a_default_domain_user_gets_exactly_todays_urls(app, db_session):
    # finalize_user_setup on a default-domain user -> ap_profile_id == f"{SERVER_URL}/u/{name}" (byte-equal)
def test_a_community_belongs_to_its_creators_home_domain(app, db_session):
    # hell.cloud user creating a community while on the onion storefront -> site_domain_id == default id,
    # ap_profile_id on SERVER_URL; an onion user creating one -> onion id and onion URLs
def test_ldap_and_oauth_users_belong_to_the_default_domain(...): ...
```

- [ ] **Step 2:** run `./run_tests.sh tests/test_per_domain_creation.py -q` — FAIL.
- [ ] **Step 3: Implement.** Registration stamps `current_storefront().id`; LDAP, OAuth and the microblogs community stamp the default domain; admin add uses the form field; communities use the creator's `home_domain()`. Every URL built in these paths uses `domain_base_url(owner.home_domain())` instead of `SERVER_URL`/`SERVER_NAME`.
- [ ] **Step 4:** PASS, plus `./run_tests.sh tests/test_admin_users*.py tests/test_auth*.py tests/test_community_add*.py -q`.
- [ ] **Step 5: Commit** `feat: new accounts and communities are created on their domain`

### Task 3: Login resolves per domain

**Files:**
- Modify: `app/auth/util.py:525-545` (`find_user_lookups`, `find_user`), `:468-503` (`process_login`), `:603` (LDAP), `app/shared/auth.py:20` (`api_log_user_in`), `app/auth/passkeys.py:27,81`, `app/nntp/server.py:426-434` (`_authenticate`), `app/mentions.py:64,70`, `app/auth/util.py` `user_name_is_taken`
- Test: `tests/test_per_domain_login.py` (create)

**Interfaces:**
- Produces: `split_handle(typed: str, storefront: DomainInfo) -> tuple[str, DomainInfo | None]` in `app/auth/util.py` — bare name → `(name, storefront)`; `name@host` → `(name, domain_for_host(host))` (None when not hosted); a leading `@` is dropped. `find_user(user_name: str, storefront: DomainInfo | None = None) -> User | None` (None means `current_storefront()`); an email still matches site-wide; the `SERVER_URL/u/<name>` profile-URL lookup becomes `domain_base_url(domain)/u/<name>`. `user_name_is_taken(name, site_domain_id)`.

- [ ] **Step 1: Write the failing tests** — two `wakko` users (default and onion rows, different passwords):

```python
def test_a_bare_name_on_the_onion_is_the_onion_account(...)
def test_a_full_handle_on_the_onion_reaches_the_hell_cloud_account(...)   # 'wakko@<default domain>'
def test_a_full_handle_for_an_unhosted_domain_fails_like_a_wrong_password(...)   # same flash, one log line
def test_an_email_logs_in_on_any_storefront(...)
def test_passkey_options_and_verify_resolve_per_domain(...)
def test_nntp_login_takes_a_bare_default_name_or_a_full_handle(...)
def test_api_login_resolves_on_the_request_storefront(...)
def test_a_bare_mention_resolves_in_the_authors_home_domain(...)
def test_a_name_taken_on_one_domain_is_free_on_another(...)
```

- [ ] **Step 2–4:** run `./run_tests.sh tests/test_per_domain_login.py tests/test_login*.py tests/test_onion_guard*.py tests/test_mentions*.py -q`; FAIL then PASS.
- [ ] **Step 5: Commit** `feat: a login name means the account on the storefront's domain, or the domain it names`

### Task 4: Registration is enforced per domain

**Files:**
- Modify: `app/auth/routes.py:71` (`register`), `app/api/alpha` registration endpoint (the one `app/api/alpha/utils/site.py:64` serves), `app/auth/util.py:217` (`process_registration_form`), `app/admin/routes.py:2305` (permission per domain)
- Test: `tests/test_per_domain_registration.py` (create)

**Interfaces:**
- Produces: `public_registration_open(storefront: DomainInfo) -> bool` in `app/auth/util.py` — True only when `storefront.registration == 'anyone'` (then `Site.registration_mode` applies as today).

- [ ] **Step 1: Failing tests**

```python
def test_the_onion_register_page_refuses(...)          # GET and POST on an 'admin' row -> the closed-registration page, no user
def test_a_staff_row_refuses_public_sign_up(...)
def test_an_anyone_row_follows_the_site_registration_mode(...)   # Open / RequireApplication / Closed as today
def test_api_registration_follows_the_storefront(...)
def test_adding_a_user_to_an_admin_row_needs_an_admin(...)       # staff without admin -> 403; admin -> created
def test_adding_a_user_to_a_staff_row_allows_staff(...)
```

- [ ] **Step 2–4:** `./run_tests.sh tests/test_per_domain_registration.py tests/test_auth_register*.py -q`; FAIL then PASS. "Staff" means a user holding the `administer users` role permission (or whatever permission `admin_users_add` already requires); "admin" means `is_admin()`.
- [ ] **Step 5: Commit** `feat: each domain's registration setting decides who may create its accounts`

### Task 5: Domain federation lock

**Files:**
- Modify: `app/models.py:5472` (`SiteDomain`), `app/admin/routes.py:353` (`admin_domains_delete`), `app/admin/forms.py:516` (drop "takes effect in a later release" from the `federates` description)
- Test: `tests/test_site_domain_lock.py` (create)

**Interfaces:**
- Produces: `domain_has_owners(site_domain_id: int) -> bool` in `app/local_only.py` (create the module here); a `@validates('federates')` on `SiteDomain` that raises `ValueError` when the row has owners and the value changes.

- [ ] **Step 1: Failing tests:** changing `federates` on a row with one account or one community raises; on an empty row it is allowed; `/admin/domains/<id>/delete` on a row with owners flashes an error and keeps the row; on an empty row it deletes.
- [ ] **Step 2–4:** `./run_tests.sh tests/test_site_domain_lock.py tests/test_admin_domains*.py -q`; FAIL then PASS.
- [ ] **Step 5: Commit** `feat: a domain's federation setting and the domain itself are locked once it has accounts`

### Task 6: Local-only accounts and content

**Files:**
- Modify: `app/local_only.py`, `app/models.py` (`Post.new` ~3219, `PostReply.new` ~4351)
- Test: `tests/test_local_only_stamp.py` (create)

**Interfaces:**
- Consumes: Tasks 1, 5.
- Produces in `app/local_only.py`: `user_local_only(user: User) -> bool` (local and `home_domain().federates` False); `community_local_only(community: Community) -> bool`; `content_local_only(author: User, community: Community, parent=None) -> bool` (any of: author local-only, community local-only, parent post/reply `local_only`); `Post.new` and `PostReply.new` set `local_only` from it for local authors (remote authors: False).

- [ ] **Step 1: Failing tests**

```python
def test_an_onion_users_post_in_a_hell_cloud_community_is_local_only(...)
def test_an_onion_users_post_in_a_remote_community_is_local_only(...)   # memes@lemmy.world row
def test_a_hell_cloud_users_post_in_an_onion_community_is_local_only(...)
def test_a_hell_cloud_users_reply_to_local_only_content_is_local_only(...)   # Review Focus 4
def test_a_hell_cloud_users_post_in_a_hell_cloud_community_is_not(...)
def test_the_stamp_is_not_recomputed(...)   # changing nothing later flips it; there is no recompute path
```

- [ ] **Step 2–4:** `./run_tests.sh tests/test_local_only_stamp.py tests/test_models_post_new.py -q`; FAIL then PASS.
- [ ] **Step 5: Commit** `feat: content by or for a local-only domain is stamped local-only when it is made`

### Task 7: Nothing local-only is delivered

**Files:**
- Modify: `app/local_only.py`, `app/activitypub/signature.py:98` (`send_post_request`) and `post_request` (retries), `app/activitypub/routes.py:2459-2510` (`announce_activity_to_followers`, direct `signed_request`), `app/community/util.py:1125-1144` (fast path), mention builders `app/shared/tasks/pages.py:99,279`, `app/shared/tasks/notes.py:142`, `app/activitypub/serializers/microblog.py:132`, `app/chat/util.py:68-100`
- Test: `tests/test_local_only_outbound.py` (create)

**Interfaces:**
- Produces: `outbound_refusal(body: dict) -> str | None` in `app/local_only.py` — a reason string when the activity must not leave: any URL anywhere in `body` (walk dicts and lists; check `id`, `actor`, `object`, `target`, `inReplyTo`, `audience`, `to`, `cc`, `attributedTo`) whose host is a hosted domain with `federates` False, or that names one of our `/post/<id>` or `/comment/<id>` rows with `local_only` True. `send_post_request`, `post_request` and the two direct-signing sites return without sending when it refuses (one DEBUG log line, no user name). `federated_mentions(users: list[User]) -> list[User]` drops local-only users before tags and recipients are built.

- [ ] **Step 1: Failing tests** (assert no call to the HTTP layer, using the suite's outbound-HTTP block or a patched `signed_request`):

```python
def test_an_onion_users_post_vote_follow_and_delete_send_nothing(...)       # Review Focus 5
def test_a_reply_vote_and_boost_on_local_only_content_send_nothing(...)     # Review Focus 4
def test_a_federating_communitys_announce_of_a_local_only_reply_is_not_sent(...)
def test_a_mention_of_an_onion_user_goes_out_as_plain_text(...)             # Review Focus 2: no Mention tag,
    # no recipient inbox, the post itself is sent
def test_a_queued_retry_of_a_local_only_activity_is_dropped(...)
def test_a_hell_cloud_post_in_a_hell_cloud_community_is_sent_unchanged(...) # body byte-equal to today
```

- [ ] **Step 2–4:** `./run_tests.sh tests/test_local_only_outbound.py tests/test_shared_tasks*.py tests/test_signature*.py -q`; FAIL then PASS.
- [ ] **Step 5: Commit** `feat: no activity by, for or about local-only content leaves the server`

### Task 8: Local-only is invisible to other servers

**Files:**
- Modify: `app/activitypub/routes.py` — `webfinger`/`process_webfinger_request` :142-161, `user_profile` :473 / `local_actor_user` :618, user collections :685,2657,2667,2677, `community_profile` :708 and community collections :2524-2644, `post_ap_refusal` :2861, `comment_ap_refusal` :2687, `/post/<id>/replies` :2923, `/post/<id>/context` :2966, likes collections :2724,2951, NodeInfo :302-381; counts in `app/activitypub/util.py:134-170`; moderator add route for communities (refuse a local-only moderator on a federating community)
- Test: `tests/test_local_only_invisible.py` (create)

**Interfaces:**
- Consumes: Task 6 helpers.

- [ ] **Step 1: Failing tests**

```python
def test_a_local_only_account_is_404_to_webfinger_and_activitypub(...)   # actor, outbox, followers, following, liked
def test_a_local_only_community_is_404_too(...)
def test_a_local_only_post_and_comment_are_404_as_activitypub(...)
def test_replies_and_context_of_a_federated_post_omit_local_only_replies(...)   # Review Focus 1
def test_outbox_and_featured_omit_local_only_posts(...)
def test_nodeinfo_counts_leave_local_only_rows_out(...)
def test_a_federating_communitys_moderators_and_counts_leave_local_only_members_out(...)
def test_a_local_only_account_cannot_be_made_moderator_of_a_federating_community(...)
def test_hell_cloud_actor_and_webfinger_are_byte_identical(...)   # snapshot of today's JSON for a seeded user
```

- [ ] **Step 2–4:** `./run_tests.sh tests/test_local_only_invisible.py tests/test_ap_routes*.py tests/test_webfinger*.py tests/test_nodeinfo*.py -q`; FAIL then PASS.
- [ ] **Step 5: Commit** `feat: local-only accounts, communities and content do not exist to other servers`

### Task 9: Only logged-in users see local-only content

**Files:**
- Modify: post and comment page routes in `app/post/routes.py` (view, embed, embed_code, oembed), `app/main/routes.py:781` (sitemap), `:1444-1516` (`index_rss`), the RSS routes listed under `refuse_if_private_instance` users (`app/community/routes.py:836`, `app/user/routes.py:2485`, `app/topic/routes.py:217`, `app/feed/routes.py:844`, `app/tag/routes.py:137`, `app/domain/routes.py:116`), every alpha API endpoint that returns posts or comments without auth, OpenGraph tags in `app/templates/base.html:85-108`
- Test: `tests/test_local_only_anonymous.py` (create)

**Interfaces:**
- Produces: `local_only_visible() -> bool` in `app/local_only.py` (True for an authenticated user or an RSS token user, else False); `without_local_only(query, model)` that adds `model.local_only.is_(False)` when not visible.

- [ ] **Step 1: Failing tests** with `private_instance` False (the only way anonymous reaches these): anonymous post page and comment permalink → 404; logged-in → 200; RSS without a token omits it, with a token includes it; sitemap omits it always; anonymous API list and get omit/404 it; oEmbed 404; a public post's OpenGraph tags unchanged.
- [ ] **Step 2–4:** `./run_tests.sh tests/test_local_only_anonymous.py -q` and the touched routes' existing test files; FAIL then PASS.
- [ ] **Step 5: Commit** `feat: anonymous visitors, feeds and the API never see local-only content`

### Task 10: "Local" means the request's domain

**Files:**
- Modify: `app/main/routes.py:167-172` (home `view_filter == 'local'`), `:436-438` (local communities list), `:1510-1516` (RSS local type), `app/api/alpha/utils/community.py:124`, `app/api/alpha/utils/user.py:100-101`
- Test: `tests/test_per_domain_local_filter.py` (create)

- [ ] **Step 1: Failing tests:** on the default storefront the Local view lists exactly what it lists today (a seeded default-domain community and a remote one: only the first); on the onion storefront Local lists only onion-domain communities' posts; the API `Local` listing type follows the storefront.
- [ ] **Step 2–4:** condition becomes `community.site_domain_id = :storefront_id` (raw SQL) / `Community.site_domain_id == current_storefront().id`; run `./run_tests.sh tests/test_per_domain_local_filter.py tests/test_main_routes*.py tests/test_api_community*.py -q`; FAIL then PASS. The memoized/cached views already key on the storefront (`storefront_cache_key`); check each touched view does.
- [ ] **Step 5: Commit** `feat: the Local filter means the domain the visitor is on`

### Task 11: "Local" badge

**Files:**
- Modify: `app/templates/_macros.html` (new macro `local_only_badge(item)`), `app/templates/post/_post_teaser.html:18,70`, `app/templates/post/_post_teaser_masonry.html`, `app/templates/post/_post_full.html:39,78`, `app/templates/post/_post_reply_teaser.html:44`
- Test: `tests/test_local_only_badge.py` (create)

- [ ] **Step 1: Failing tests:** a local-only post renders `<span class="badge text-bg-secondary">Local</span>` (translated `_('Local')`, title `_('Not shared with other servers')`) once beside the author in the teaser, the full post and a local-only comment; a federated post renders none.
- [ ] **Step 2–4:** `./run_tests.sh tests/test_local_only_badge.py -q`; FAIL then PASS.
- [ ] **Step 5: Commit** `feat: local-only posts and comments carry a Local badge`

### Task 12: Mail links to the recipient's home domain

**Files:**
- Modify: `app/storefront.py:944` (`default_storefront`) and its seven callers (`grep -rn "default_storefront()" app`), the newsletter sender
- Test: `tests/test_mail_home_domain.py` (create)

**Interfaces:**
- Produces: `home_storefront(user: User)` context manager in `app/storefront.py`, the same mechanics as `default_storefront()` with `g.storefront = user.home_domain()`. Each call site that mails a specific user switches to it; `default_storefront()` stays for mail with no single recipient account.

- [ ] **Step 1: Failing tests:** an approval email to an onion account links to the onion; to a hell.cloud account sent from an onion request links to hell.cloud (the I1 guarantee still holds for clearnet recipients); a newsletter renders per recipient.
- [ ] **Step 2–4:** `./run_tests.sh tests/test_mail_home_domain.py tests/test_onion_third_party.py -q`; FAIL then PASS. Update the docstring of `default_storefront` to say ruling I1 is replaced by recipient home domains.
- [ ] **Step 5: Commit** `feat: mail to an account links to its home domain`

### Task 13: Deploy and verify (controller, after the owner pushes)

- [ ] **Step 1 (before the push):** on the server, save `curl -s -H 'Accept: application/activity+json' https://hell.cloud/u/<a real account>`, its WebFinger answer, `/c/<a real community>` and `/nodeinfo/2.1` to `~/backups/<date>-identity/before/`.
- [ ] **Step 2:** after autodeploy, the same four, and `diff` against before: identical (NodeInfo counts may differ only by activity since; compare the shape and `software`).
- [ ] **Step 3:** confirm the onion row (`site_domain` id 2) has `federates` False and `registration` `admin`; onion `/auth/register` refuses; logging in on the onion as `wakko@hell.cloud` works; an admin-added onion test account's WebFinger is 404 from clearnet.
- [ ] **Step 4:** 0 tracebacks in `pyfedi-web`, `pyfedi-celery`, `pyfedi-celery-background` for 10 min.
- [ ] **Step 5:** update memories (`onion-storefront-track`) and DEPLOY_GUIDE §11 (adding onion accounts, the federation lock).
