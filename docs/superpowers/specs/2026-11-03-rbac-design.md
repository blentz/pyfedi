# Custom roles with domain scope (RBAC) — design

Date: 2026-11-03. Status: spec only. Nothing has been built.

## Summary

The feature is a capability: on a live instance, instance admins create their own roles in the admin UI and grant
them to users, either instance-wide or on one `SiteDomain`. A role is a named bundle of permissions. This ships no
new role. Admin and Staff are the only roles that exist out of the box, as today.

The use case that drives the design is a "domain admin": a role an admin should be able to compose themselves, with
domain branding, the users homed on the domain and the communities homed on the domain, and nothing site-wide. The
spec is acceptable when an admin can build that role through the UI alone, with no code, deploy or restart, and it
behaves as RB3/RB4 describe. The acceptance test does exactly that.

This needs the ~200 hard-coded `is_admin()` / `is_staff()` / `is_admin_or_staff()` checks (157 in Python, 36 in
templates, plus `g.admin_ids` in templates) to become named permission checks. Without that, a custom role can't
reach anything that isn't already one of today's 8 permission strings.

## Rulings

| # | Ruling |
|---|---|
| RB1 | The scope goes on the assignment. A role is a reusable bundle of permissions. A **grant** is (user, role, `site_domain_id`), where NULL means instance-wide. A user may hold several grants, including different roles on different domains. |
| RB2 | **Admin** stays a built-in role that can't be deleted. It implicitly holds every permission, is instance-wide only, and is never granted with a domain scope. User 1 stays a superuser who can't be banned. Every `is_staff()` / `is_admin_or_staff()` site, and every `is_admin()` site a delegate may need, becomes a named permission. **Staff** becomes an ordinary seeded role (RB16) whose permissions reproduce exactly what it can do today. |
| RB3 | **Acceptance use case, not a shipped role.** The catalogue must be fine-grained enough that an admin can compose a "domain admin" role that holds, on its domains: domain branding and settings (not adding or deleting domains, not `federates`), the users homed there (approve, edit, ban, purge, referrals), and the communities homed there (create, edit, delete, moderators, content, reports). It must be possible to leave out CMS pages and newsletters. |
| RB4 | A domain-scoped grant reaches any content inside its domains' communities, and can community-ban any author, remote ones included. Instance-wide account actions (instance ban, edit, purge, delete) reach only users homed on its domains. Remote users and instances, and other domains' users, are out of reach. |
| RB5 | Only Admin creates or edits roles and makes or revokes grants. There is no delegation. |
| RB6 | A holder acting under a scoped grant can never act on any user who holds **any** grant, whether ban, edit, purge, delete or a role change. Only Admin can. |
| RB7 | Each permission is flagged **scopable** or **instance-only**. A domain-scoped grant of a role containing an instance-only permission is refused, with an error naming the permission. Editing a role so that it gains an instance-only permission is refused while the role has any domain-scoped grant. |
| RB8 | There is one `/admin` area. The menu shows only what the user's grants allow. Every list and form is filtered in its query to the user's domains, with a domain picker when the user holds more than one. |
| RB9 | Every grant, revoke and role edit writes a ModLog entry. Every action taken under a scoped grant records the `site_domain_id` it was taken under. The admin modlog can be filtered by domain. |
| RB10 | Revocation takes effect immediately. Any change to grants or roles clears that user's permission caches (`user_access`, `is_admin`, `is_staff`, `admin_ids`) on every worker. |
| RB11 | Grants have an optional `expires_at`. An expired grant is ignored at check time. A Celery beat task deletes expired grants and writes a ModLog entry for each one. |
| RB12 | A scopable permission, `shown as admin`, makes its holder appear as an admin of domain X on X's host: admin=true in the API (session/user views), and listed among X's admins (API site `admins`, `/about` admin list, nodeinfo). An admin ticks it to give a custom role this behaviour. Instance admins on sister domains keep today's behaviour (O1, ruled): a sister domain lists only the admins homed on it. |
| RB13 | A holder of `approve registrations` on X approves X's applications and is notified of them. A holder of `administer all users` on X may create accounts on X when X's `registration` mode is `'staff'` or `'admin'`. Instance admins still are too. |
| RB14 | No custom role is seeded or shipped, including a "Domain admin". Every non-built-in role is created by an instance admin in the UI. |
| RB16 | **Admin is the only hard-coded role.** It is identified by `role.built_in`, never by name or id. Its name is locked, and no other role may be named "Admin" (case-insensitive). Staff is a **seeded** role: created by `flask init-db` and kept by the upgrade migration, then an ordinary custom role an admin may rename, re-permission or delete. No code refers to Staff by name or id. |
| RB17 | The admin RBAC UI supports full CRUD on custom roles (create, list/view, edit, duplicate, delete) and on grants (add, change expiry, revoke), all from `/admin/roles` and the user edit page. Admin's row is read-only apart from its grants. |
| RB15 | Live: roles, their permissions and grants are runtime data. Creating, editing, granting and revoking take effect on the next request for every holder, with no deploy, migration or restart (RB10). The permission catalogue is code-defined, because each permission guards a code path. A new permission ships with code and appears in the role editor once deployed. |

## Data

```
role            id, name UNIQUE (lower(name)), built_in BOOL NOT NULL DEFAULT false, description,
                listed_publicly BOOL NOT NULL DEFAULT false, created_at, updated_at
                (weight dropped: RB6 makes rank irrelevant; partial unique index allows one built_in row)
role_permission (role_id, permission) — unchanged
role_grant      id PK,
                user_id → user ON DELETE CASCADE,
                role_id → role ON DELETE RESTRICT,
                site_domain_id → site_domain ON DELETE CASCADE, NULL = instance-wide,
                granted_by → user ON DELETE SET NULL, granted_at, expires_at NULL
                UNIQUE (user_id, role_id, COALESCE(site_domain_id, 0))
mod_log         + site_domain_id → site_domain ON DELETE SET NULL, NULL
```

- `user_role` is migrated into `role_grant` rows with `site_domain_id` NULL and `granted_by` NULL, then dropped.
  The `User.roles` relationship is rebuilt over `role_grant`, so existing readers keep working during the conversion.
- Roles 1 ("Anonymous user") and 2 ("Authenticated user") confer nothing. Role 2 is only the admin user form's
  "no role" default (`admin/forms.py` 418, 507). The migration drops their `user_role` rows and deletes both
  roles. The role 4 row, found by name 'Admin', gets `built_in = true`, and its permission rows are deleted
  because Admin implicitly has every permission. The role 3 row ('Staff') stays as an ordinary role with
  `built_in = false` and `listed_publicly = true`, and gets the split permissions (note 1). If an instance has
  renamed or deleted it, nothing is created.
- `ROLE_STAFF`, `ROLE_ADMIN`, `ROLE_STAFF_NAME`, `ROLE_ADMIN_NAME` and `EDITABLE_ROLE_IDS` are removed, along with
  every lookup by role id or name: `cli.py` 269–284 and 371, `auth/util.py` 406 (`get(Role, 4)`), `admin/routes.py`
  2572 and 2839–2849, `request_hooks.py` 248, and `Site.admins()`/`Site.staff()` in `models.py`. Admin is found
  by `built_in`. A lint test fails on any of those names, or on `Role.name ==`, reappearing in `app/`.
- `flask init-db` on a new instance creates Admin (built-in) and Staff (seeded, with the Staff column of the
  catalogue). Nothing else is created (RB14).
- A role with grants can't be deleted (RESTRICT). Deleting one through the UI revokes its grants in the same
  transaction, but only when the admin confirms the revocation explicitly (see Admin UI).
- User 1 is Admin implicitly even with no grant, so revoking Admin grants can never lock the instance out. An
  Admin can't revoke their own Admin grant (it guards against accidents; another Admin or user 1 can).
- Migration + downgrade in one Alembic revision. The downgrade collapses grants back into `user_role`, dropping
  scoped grants (logged as lossy).

## Check API

One function replaces every check:

```python
user_access(permission: str, user: User | int, on=None) -> bool
```

- `on` is the object acted on: a `SiteDomain`, `Community`, `Post`, `PostReply`, `User`, `Feed`, `Report`,
  `UserRegistration`, or None. The object's **home domain** is resolved by the table below.
- The check passes when the user is Admin (or user 1), **or** holds an unexpired instance-wide grant whose role
  has the permission, **or** the permission is scopable, `on` resolves to domain D, and the user holds an unexpired
  grant on D whose role has the permission. An instance-wide grant always covers scoped checks.
- `on=None` with a scopable permission asks "does the user hold it **anywhere**". It is used only for showing
  menus and nav items, never to authorise an action. A lint test fails if a route or a `shared/` function calls
  it with `on=None` for a scopable permission outside nav and template code.
- RB6 lives in one helper, `may_act_on_user(actor, target, permission)`: the target must resolve to a domain the
  actor holds the permission on, and if the actor's only qualifying grant is scoped, the target must hold no
  grant at all. The existing rule "staff/admin targets can't be banned" in `shared/user.py` and `user/routes.py`
  becomes this helper.
- `permission_required(perm)` stays for instance-only permissions. Scopable routes load their object first and call
  `user_access(perm, user, on=obj)`, aborting with 403.
- `is_admin()` stays and means only "holds the built-in role, or is user 1". `is_staff()` and `is_admin_or_staff()` are
  deleted once their last caller is converted. A test fails if either name reappears in `app/`.
- Caching: memoized per (user, permission, domain) and keyed by a per-user `permission_version` integer in Redis.
  Any change to that user's grants, or to a role they hold, bumps it (RB10). `admin_ids` is invalidated the same way.

### Domain resolution

| Object | Home domain |
|---|---|
| `SiteDomain` | itself |
| `Community` | `community.site_domain_id`. NULL (a remote community) means instance-only. |
| `Post`, `PostReply`, `Report` on content | the community's |
| `User`, `UserRegistration`, `Report` on a user | `user.site_domain_id`. NULL (a remote user) means instance-only. |
| `Feed` | `feed.site_domain_id` (no feed permission is scopable in v1) |
| `Conversation`, `ChatMessage`, `Instance`, `Topic`, `Domain` (link domain), `Tag`, `File`, `Site` | always instance-only |

RB4 falls out of this table. Content inside X's communities resolves to X. A community ban is an action on the
community, so `on=community` resolves to X whoever the author is. An instance ban, purge or edit is an action on the
user, so `on=user` resolves to the user's home, and a remote user's home is NULL.

## Permission catalogue

"Today" is who passes now. **S** = scopable, **I** = instance-only. "Staff" says whether the seeded Staff role gets
it, which keeps today's behaviour (RB2). "DA" says whether the acceptance test's "domain admin" role (RB3) ticks it. It is not a shipped role.

### Instance-only

| Permission | Guards | Today | Staff | DA |
|---|---|---|---|---|
| `change instance settings` | /admin/site, /misc, /instance_chooser, /federation*, /activities, /activity_json*, /emoji*, /perf_test, community move (`/community/<id>/move`) | perm | no | no |
| `manage domains` *(split from the above)* | /admin/domains add/delete, `federates` | `change instance settings` | no | no |
| `masquerade` *(split)* | /admin/masquerade | `change instance settings` | no | no |
| `send newsletter` *(split)* | /admin/newsletter | `change instance settings` | no | no |
| `manage remote instances` *(split)* | /admin/instances, /instance/<id>/edit, /contact, /create_offline, /federation/ban_lists, `instance_people` admin view, community `fixup_from_remote`, remote-community side-pane admin link | mixed: settings / administer all communities / is_admin | yes¹ | no |
| `manage topics` *(split)* | /admin/topics*, /topic* | `administer all communities` | yes | no |
| `manage blocklists` *(split)* | /admin/blocked_image*, /block_image_purge_posts, link-domain ban controls (`domain/routes.py` show_domain, domains, `domain*.html`), tag bans (`tag/routes.py` tags, `tag.html`) | `administer all communities`; is_admin_or_staff; `ban users`/`manage users` | yes | no |
| `view vote analytics` *(split)* | /admin/votes, /votes_for_noone, /serial_downvoters, /high_posts_per_day, /no_link_diversity, profile posting/comment-pattern charts, a user's upvoted posts | `administer all communities`; is_admin | yes¹ | no |
| `manage media` *(split)* | /admin/media list, /media/<id>/delete, /media/<user>/delete_all | `administer all communities` / `administer all users` | yes¹ | no |
| `edit cms pages` | /admin/pages* | perm | no | no |
| `read private messages` | `chat_home`, `chat_options`, `chat_delete`, `chat_report`, `chat_conversation` as a non-member; `private_message_ap`; PM report lists (API) | is_admin; `administer all users` | no¹ | no |
| `add remote communities` *(new)* | `add_remote` when `allow_default_user_add_remote_community` is off | is_admin_or_staff | yes³ | no |
| `manage instance feeds` | `feed_new`, `feed_copy`, editing others' feeds, instance feeds (`make_feed`, `edit_feed`), private feed visibility (`_feed_open_to`, `feed_view`, `_feed_table_row.html`) | is_admin | no | no |
| `feature instance-wide` | `post_instance_sticky`, API `post_post_feature` (instance) | is_admin | no | no |
| `exempt from upload limits` | `upload_access == 'admins'`, `can_send_pm_to` exemptions | is_admin_or_staff | yes² | no |
| `exempt from account limits` | referral caps (`remaining_signups`, `user_referrals`), `community_invite` new-account limit, `trustworthy()` | is_admin | no² | no |
| `generate referral codes` *(new)* | generating referral codes when the setting is `'staff'` (`referral.py` `may_generate`) | is_admin_or_staff | yes | no |
| `see admin dashboard` | dropped: shown when the user holds any grant | is_admin_or_staff | — | — |
| `change user roles` | **removed**: grants and role edits are Admin-only (RB5). /admin/permissions, the role field in `admin_user_edit` | perm | — | — |

### Scopable

| Permission | Guards | Today | Staff | DA |
|---|---|---|---|---|
| `edit domain branding` *(split from settings)* | /admin/domains/<id>/branding: name, description, logos, registration mode (RB3) | `change instance settings` | no | yes |
| `approve registrations` | /admin/approve_registrations*, registration notifications (`auth/util.py` recipients), creating accounts on a 'staff'/'admin'-registration domain (`admin_users_add`, RB13) | perm; is_admin | yes | yes |
| `administer all users` | /admin/users, /user/<id>/edit, /resend_email, /users/add, /user/<id>/delete, /admin/referrals, /admin/reports (filtered by domain) | perm | no | yes |
| `ban users` | instance ban and unban (`ban_profile`, `unban_profile`, `shared/user.py ban_user`, API ban/unban), via `may_act_on_user` | perm (or `manage users`) | yes | yes |
| `manage users` | delete or purge a profile (`delete_profile`), the profile admin panel (`show_profile.html` 421/428, `base.html` 275) | perm | yes | yes |
| `view user details` *(new)* | same-IP accounts (`_get_user_same_ip`, results limited to the actor's domains), a user's subscribed communities and moderated communities, deleted items on a profile (`show_profile`, `_get_user_posts*`), profile admin blocks (`show_profile.html` 124/383/492) | is_admin_or_staff / is_admin | yes | yes |
| `create local communities` *(new)* | `add_local` when `community_creation_admin_only`, `add_post` community picker (`main/routes.py` 371). Scoped by the new community's home domain | is_admin | no³ | yes |
| `moderate all communities` *(split from administer all communities)* | `can_moderate()`'s staff branch and everything that reads it; community ban and unban (web and API); add/remove moderator, make/remove owner; flair create/edit/delete (API); post and reply flair; post approve; NSFW (API); reply delete; inline reply edit; reply hide threshold bypass; deleted-content visibility (`deleted_post_state`, `post_refusal`, `refuse_unpublished_post`, `show_deleted` everywhere); post and reply voting activity, and like lists (API); `post_set_ai`; `community_mod_list.html`, `post_options.html`, `post_reply_options.html` controls | is_admin_or_staff / is_staff, or `administer all communities` | yes | yes |
| `administer all communities` *(narrowed)* | admin-only community powers: community delete/restore (`delete_community`, `restore_community`, D615), community edit page and remove icon/header, `community_find_moderator`, wiki, RSS feeds, `community_kick_user`, `community_moderate`, `community_moderate_comments`, report escalate/resolve/ignore, community modlog, `post_edit`, `post_purge`, `post_reply_purge`, `post_sticky`, deleted post or reply source, `add_post` to a mod-only community, `can_create_post_reply` | is_admin | no⁴ | yes |
| `administer communities pages` *(split, O2)* | /admin/communities*, /admin/community/<id>/edit and /delete, /admin/content (each filtered by domain) | `administer all communities` | yes | yes |
| `view modlog` *(new)* | private entries in `/modlog` and API `get_modlog`, filtered to the actor's domains; `add_to_modlog` "admin" type | is_admin / is_staff | yes | yes |
| `shown as admin` *(new, RB12)* | admin=true in the API and listing among the domain's admins (API site `admins`, `/about`, nodeinfo) on the domain's own host. Grants no power by itself | is_admin | no | yes |

Notes:

1. Staff gets it only if Staff held `administer all communities` (it does by default, see `cli.py` 270–273), because
   that permission guards the route today. A role that today holds `administer all communities` is migrated to hold
   every permission split from it: `moderate all communities`, `manage topics`, `manage blocklists`,
   `view vote analytics`, `manage media`, `manage remote instances` (the parts it guarded), and
   `administer communities pages`. For `read private messages`, Staff gets it only on the API PM report lists, which
   `administer all users` guards today. Staff doesn't hold that by default.
2. Today Staff is exempt from `upload_access == 'admins'` and from PM restrictions, but not from referral caps or
   `trustworthy()`. That is why there are two exemption permissions, and both are instance-only.
3. Staff passes `add_remote` today but not `add_local`. That is why there are two permissions.
4. The narrowed `administer all communities` covers only sites that pass only Admin today (wiki, kick, post
   edit/purge, …), so default Staff doesn't get it. Note 1's migration maps an existing `administer all communities`
   row to `administer communities pages`, not to this one. **See O2.**

## Seeded roles

- **Admin**: the only built-in role. It has every permission (RB2, RB16).
- **Staff**: seeded, not built-in. It holds the "Staff" column above plus whatever its rows hold today, and has
  `listed_publicly = true`. From then on it is an ordinary custom role (RB16).
- No other role is seeded (RB14).

## Admin UI

All of it is Admin-only (RB5), POST + CSRF for every change, ModLog on every change (RB9), and live (RB15).
It replaces `/admin/permissions` (which redirects to `/admin/roles`) and the single **Role** drop-down on the
user add and edit forms.

### Roles (RB17)

| Operation | Route | Behaviour |
|---|---|---|
| List | `GET /admin/roles` | One row per role: name, description, permission count, "can be granted on a domain" (all ticked permissions scopable, RB7), listed publicly, holder count, and Edit / Duplicate / Delete actions. Admin is pinned first with a "Built-in" badge and only a View action. |
| View | `GET /admin/roles/<id>` | The role's permissions in plain language, and its grants (user, scope, granted by, granted at, expires) with an Add grant form. For Admin: "Has every permission". |
| Create | `GET/POST /admin/roles/new` | Name (required, unique case-insensitive, not "Admin"), description, listed publicly, and the permission checklist. |
| Duplicate | `GET /admin/roles/new?from=<id>` | The create form prefilled from another role, Admin included. The name becomes "Copy of …". Grants are not copied. |
| Edit | `GET/POST /admin/roles/<id>/edit` | The same fields. When the role has holders, a confirmation step shows "this changes access for N users" before saving. Adding an instance-only permission is refused while the role has domain-scoped grants, and the error lists those grants (RB7). Admin can't be edited. |
| Delete | `POST /admin/roles/<id>/delete` | A confirmation page lists the role's grants. With grants, delete needs a "Revoke all N grants" tick, and then revokes and deletes in one transaction with a ModLog entry per grant. Admin can't be deleted. |

The permission checklist is grouped by area: site, domains, users, communities, content, federation, blocklists,
analytics, private messages, feeds, exemptions, display. Each permission shows a scopable/instance-only badge and
the catalogue's plain-language "Guards" text, because an admin composes roles without the spec in front of them.
Group-level "select all" works in the browser without JavaScript (one checkbox per permission; the group toggle is a
progressive enhancement only, for the onion).

### Grants (RB1, RB11, RB17)

| Operation | Where | Behaviour |
|---|---|---|
| Add | role view page, and the **Roles** panel on `/admin/user/<id>/edit` | Pick a user (on the role page) or a role (on the user page), a scope ("Instance-wide" or one domain), and an optional expiry. RB7 refusals and duplicates (same user, role and scope) are form errors. |
| Change expiry | same two places | Set or clear `expires_at`. |
| Revoke | same two places | A POST button. An Admin can't revoke their own Admin grant. User 1 never needs a grant. |
| Add user with grants | `/admin/users/add` | The single Role field becomes an optional list of grants. |

### What scoped holders see

- The nav shows only the sections they hold anywhere (`on=None`). Every list query takes
  `domains_for(user, permission)` and filters `site_domain_id IN (...)`. The domain picker narrows that further.
  A scoped holder never sees an unfiltered list, and a test proves it for every admin list route.
- **Domain branding**: a holder of `edit domain branding` on some domains sees /admin/domains limited to those
  domains, with branding links only. The add/delete/`federates` controls are hidden and, on the server side,
  require `manage domains`.

### Public display of roles

Today the profile badge (`show_profile.html` 157, `user_preview.html` 15), the `/about` staff list
(`main/routes.py` 769) and the domain admin list in `activitypub/util.py` 6416 name Admin and Staff. They now
show Admin plus every role with `listed_publicly`, by role name, under today's `hidden_here` rules. The seeded Staff
role is listed publicly, so nothing visible changes until an admin edits it.

### Settings that named Staff

- Referral setting "Who can generate referral codes?" (`referral.py` 24/55, `admin/forms.py` 105): the value
  `'staff'` becomes "Admins and holders of `generate referral codes`", a new instance-only permission. The seeded
  Staff role gets it, so behaviour is unchanged. The stored value stays `'staff'`.
- A domain's registration mode `'staff'` (`admin/forms.py` 591, "Staff only") is relabelled "User administrators
  only". It means holders of `administer all users` on that domain (RB13). The stored value stays `'staff'`.

## Notifications

Reports and approval requests today go to `Site.admins()` / `Site.staff()` (`auth/util.py` 123–159,
`community/util.py` 1353–1362 and the other readers listed by `grep Site.admins`). Staff is no longer special, so
each list becomes Admins plus permission holders. Each recipient list now also includes
the holders of the relevant permission on the object's domain: registrations go to `approve registrations`
(RB13), reports on content to `moderate all communities`, and reports on a user to `administer all users`.
Instance-wide holders are still notified as they are today. A replacement helper
`recipients_for(permission, on=obj)` handles every call site listed under `Site.admins()` in `app/`.

## API and federation

- The checks are shared, so API actions follow the same rules as the web.
- RB12: `user_view`'s `is_admin`, the API site response's `admins`, the `/about` (`main/routes.py` 768) admin list
  and nodeinfo, when served on domain X's host, count a user as admin if they are an Admin homed on X (today's
  rule, O1) or hold `shown as admin` on X.
- The ActivityPub instance actor of a sister domain doesn't publish an admin list today, so there is nothing to change there.
- Incoming remote `InstanceRole` admins (`is_instance_admin()`) are unrelated to local roles and stay unchanged.

## Testing and gates

- **Parity test, written first**: for every route and `shared/` function in the catalogue, a parametrised test
  records allow/deny for anonymous, plain user, default Staff, Admin and user 1, against the current code. After
  migration, the same table must pass unchanged. The only intended change is the removal of `change user roles` from non-Admins (RB5).
- **Acceptance test (RB3, RB15)**: logged in as an Admin, the test creates the "domain admin" role through
  `/admin/roles` HTTP posts (ticking the DA column), grants it on domain X through the grant form, and then, as the
  grantee and with no restart or cache flush in between, runs the scope tests below. It then edits the role (unticks
  a permission) and revokes the grant, checking each takes effect on the next request.
- **Scope tests**: a holder of that role on X is allowed on X's objects and denied on Y's, on remote objects and on
  instance-only routes. RB6 has a test against users holding each kind of grant. RB7 is tested on both grant
  creation and role edit. RB10 checks that a revoke takes effect on the next request, and RB11 that an expired grant
  is ignored before the beat task runs.
- Lint tests: no `is_staff` / `is_admin_or_staff`, no role constants and no lookup by role name or id left in `app/` (RB16), and no scopable permission checked with `on=None`
  outside nav code.
- The full gates from feedback-full-gates apply: suite, coverage floors, changed-line gates.

## Build order (each a commit, TDD)

1. Parity test against today's code.
2. Schema: `role_grant`, `mod_log.site_domain_id`, `role.built_in`/`listed_publicly`; migrate `user_role`; delete
   roles 1 and 2; Admin found by `built_in`; rebuild `User.roles`.
3. `user_access(perm, user, on)`, domain resolution, `may_act_on_user`, the cache version.
4. Split the permissions and migrate the role rows (note 1).
5. Convert the call sites one area at a time: admin routes, users, community moderation, community administration,
   posts/replies, chat/feeds, API, templates. The parity test stays green after each area.
6. Delete `is_staff`/`is_admin_or_staff`, add the lint tests.
7. Roles and grants CRUD UI (RB17), RB7 validation, ModLog entries (RB9), public role display, the referral and
   registration-mode relabels.
8. Scoped list filtering, the domain picker, notification routing.
9. RB13 registration modes and notifications.
10. Expiry beat task (RB11), `shown as admin` (RB12), and the end-to-end acceptance test (RB3, RB15).

## Open questions

- **O1 (ruled 2026-11-03: keep today's behaviour).** Today a sister domain's API site `admins` names only admins homed
  on that domain (`api/alpha/views.py` 1556: "another domain's would name that domain"). That is deliberate, because
  it keeps the storefronts separate. During the grilling, RB12's option said instance admins stay listed on every
  domain, which contradicts this. Ruled: keep today's behaviour. A sister domain lists the admins homed on it plus
  the holders of `shown as admin` on it.
- **O2 (resolved by RB2: Staff keeps exactly today's powers).** Today default Staff can't edit or purge posts, run wikis or RSS
  feeds, kick users or resolve community reports, but it can ban from communities, add moderators and see deleted
  content. That isn't consistent. The catalogue keeps it exact by splitting `administer communities pages`
  (the /admin routes, Staff yes) from `administer all communities` (the is_admin-only sites, Staff no), so the
  parity test needs no intended changes. An admin who wants Staff to be consistent can tick the extra permissions
  on the Staff role in the UI after upgrading.
- **O3. Feeds.** Feeds have `site_domain_id`, but RB3 doesn't give domain admins feeds. `manage instance feeds` stays
  instance-only in v1, and the permission can become scopable later without a migration.
- **O4. Referral caps for scoped holders.** Viewing `/admin/referrals` filtered to X comes with `administer all users`
  on X. `exempt from account limits` is instance-only, so a holder scoped to a domain can't be exempted from
  referral caps.
