# Per-domain branding: design

Date: 2026-10-25. This is project 4 of `docs/superpowers/specs/2026-10-17-onion-storefront-design.md` (§6:
"per-domain site name, logo, colours, custom CSS and sidebar"). It builds on projects 1–3: the domain registry, the
Tor deploy, and per-domain identity C1–C4, all deployed.

**Terms.** The *first domain* is the default domain: the `site_domain` row with `is_default`, whose host is
SERVER_NAME, and whose site settings are `Site` row 1. A *sister domain* is any other `site_domain` row. The onion
is one of these; other storefronts may be added later.

## 1. Intent

One PieFed server answers on several domains. Every domain looks like the first domain today: the same header name,
logo, theme, CSS, sidebar, description and About page. That is because all of them live on the single `Site` row.
Each domain should be able to look like its own site. A domain that sets nothing should keep looking exactly as it
does now.

## 2. Goals, non-goals, anti-goals

Goals (owner):
- Each domain can have its own **theming**: a default theme chosen from the shipped themes
  (`app/templates/themes/`), plus its own custom CSS. Colours come from these two. (Owner: "theme plus custom CSS
  is enough".)
- Each domain can have its own **site name** in the page header, its own **logo**, its own **sidebar**, its own
  **description** and its own **About page**.
- **The first domain's settings are the app-wide defaults.** Its current `Site` values stay where they are, and
  they stay the defaults. Every other domain can set its own value or take the default (owner).
- **A logged-in user's own theme pick wins** over any domain's theme (owner).
- **On the onion, branding never makes a visitor's browser fetch anything from outside the onion.** This is
  enforced. On every other domain, branding that loads off-site resources is strongly discouraged, and the admin
  is warned (owner).
- **Names:** the domain's display name goes wherever a person reads a name. The bare domain goes wherever an
  address or identifier is expected (owner). §3.3 says which is which.
- **No domain name is hard-coded.** Nothing in the implementation names a specific domain. Every domain-specific
  value comes from `site_domain` rows, `Site` and config (owner). Tests use the test app's SERVER_NAME and fixture
  domains.

Non-goals:
- A colour picker, or any branding control beyond theme plus CSS (owner).
- Per-domain settings other than branding. Registration and federation are already per domain; everything else
  stays global (per-domain identity spec §2).
- Per-domain user preferences. A user's theme and CSS are theirs on every domain.
- Uploading new themes. A domain picks from the shipped themes.
- Content embeds, such as video iframes in posts. They are not branding. The onion media proxy already handles
  remote media on the onion.

Anti-goals (each is a failure even if everything else works):
- Any visible change to the first domain: header, logo, theme, CSS, sidebar, description, About, emails, PWA
  manifest, NodeInfo or ActivityPub documents.
- A browser on the onion fetching any clearnet resource because of branding, including branding the onion
  inherits from the first domain.
- A sister domain presenting itself to other servers under the first domain's name. The per-domain identity owner
  ruling stands: nothing a sister domain publishes names the first domain.

## 3. Design

### 3.1 What a domain can set

Per domain, each item optional:
- display name;
- logo, with the icon sizes the site keeps today: `logo`, `logo_180`, `logo_152`, `logo_32`, `logo_16`;
- default theme;
- custom CSS;
- sidebar: Markdown rendered to HTML, as `Site.sidebar` / `sidebar_html`;
- description: the short tagline, `Site.description`, used in the home page title, meta tags, the manifest and
  donate page (Claude's call: the same field everywhere `Site.description` is used today);
- About page: Markdown rendered to HTML, as `Site.about` / `about_html`.

### 3.2 Fallback

- The fallback is per field. A blank field means "use the default"; there is no separate on/off switch (Claude's
  call).
- The default for every field is the `Site` row's value. The first domain has no branding of its own: its branding
  is the `Site` row, edited where it is today, in the site settings.
- Theme order (changes `current_theme`, app/utils.py:4878):
  1. the user's own theme, if they set one;
  2. else the domain's default theme;
  3. else the site default theme;
  4. else `piefed`.
- A user's own custom CSS still layers on top of the site or domain CSS (base.html:116-123), as today (Claude's
  call). It affects only that user, who chose it, so §3.4 does not police it.

### 3.3 Where the name and other branding go (Claude's calls under the owner's rule)

**Pages.** These use the domain's value if set, else the `Site` value, as the owner asked:
- the header;
- the browser tab title;
- the logo;
- the sidebar;
- the description;
- the About page.

**Emails.** The sender name is the display name of the domain the email is for:
- the recipient's home domain (`User.site_domain_id`, from per-domain identity);
- the first domain when there is no recipient account.

Today `app/email.py` uses `g.site.name`, which is not set in a Celery task. Fallback as for pages.

**PWA manifest** (`/manifest.json`, app/main/routes.py:1391). The name, description and icons are the domain's own,
if set, else as today. This is per request host, as today.

**What other servers and clients read.** This covers:
- the NodeInfo title (app/activitypub/routes.py:397);
- the instance actor's `name`, `summary`/sidebar and icon (app/activitypub/util.py:6217-6223);
- the API site document (app/api/alpha/views.py:1430-1454, 1523-1527).

A sister domain's own value is published if set. Otherwise the output is exactly what it is today. For the name,
today's output is the bare domain (`domain_site_name`, app/federation_urls.py:102). That rule becomes: the domain's
display name if set, else today's answer. The first domain's output does not change by a byte.

**Bare domain, always:** handles, URLs, WebFinger, the instance actor's `preferredUsername`, and anything matched
or linked by host.

### 3.4 The onion: nothing off-site

**Off-site** means any URL whose host is not the onion's own. The onion media proxy (app/onion_media_routes.py) is
served on the onion, so it is on-site.

**Enforced on the onion's effective branding**, meaning its own values and anything it inherits from `Site`:
- CSS `url()` and `@import` targets;
- the logo and icon URLs;
- images and other auto-loading references in the rendered sidebar and About HTML.

How it is enforced:
- **On save:** the onion's branding is refused when it has an off-site reference. The message names the reference.
- **On render:** an inherited value can change from the first domain's settings page, so the render path never
  emits an off-site load on the onion. An inherited off-site reference is dropped from the onion's page and logged
  at info. The page still renders (Claude's call).
- **Shipped themes:** these are covered as well (see §6 ruling 1).
- **Browser backstop:** a Content-Security-Policy on onion pages limits `img-src`, `font-src`, `style-src` and
  `connect-src` to the onion. It must keep working the onion pages that work today: remote media through the
  proxy, and the existing script nonce (app/request_hooks.py:271). The exact policy is the builder's.

Today the first domain's custom CSS is empty, and its sidebar and About have no off-site references. Its logo is
local and its default theme (`pyroclast`) loads nothing off-site, so the onion's look does not change on day one.
Checked on prod 2026-10-25.

**Advised everywhere else**, including the first domain's site settings. Saving branding with an off-site reference
succeeds but shows a clear warning naming each reference.

### 3.5 Editing

- Site admins only edit a domain's branding, on the admin Domains page next to the domain's existing settings
  (federates, registration) (Claude's call).
- Logo upload works as the site logo upload does today, including the icon sizes.
- Values are cached per domain the way `domain_rows` / `g.site` are today. A saved change shows on the next page
  load (`clear_domain_cache` or its equivalent).

## 4. Constraints

- **The first domain is byte-identical.** After deploy, the identity check in DEPLOY_GUIDE §11 must match: actor,
  WebFinger, NodeInfo, community and instance actor. Only `updated` and counts may differ. Pages, emails and the
  manifest look the same.
- **No hard-coded domain** (owner). This is enforced by a test, so a reintroduction fails the gate. §6 ruling 2
  says what it scans.
- Project rules:
  - TDD, one commit per task or defect;
  - 100% changed-line and branch coverage (`tests/check_changed_line_coverage.py --branches`);
  - inline imports only if technically unavoidable;
  - slow tests are bugs;
  - invalid client input stays a 400;
  - fuzz targets that reach the registry stub `storefront.domain_rows`.
- Custom CSS and sidebar HTML are admin-supplied and rendered `| safe` today. Per-domain values get no less
  escaping than the `Site` values.
- The onion stays plain http (owner ruling 2026-10-19).

## 5. Left to the builder

- Storage: new `site_domain` columns or a side table, and how values reach templates (`DomainInfo`, a per-request
  branding object, or similar).
- The exact onion enforcement code and CSP text, within §3.4.
- How the warning in §3.4 is worded and shown.
- How a blank logo falls back when only some icon sizes are set.

## 6. Owner rulings from the design review

1. **Shipped themes host their fonts locally** (owner: "Host fonts locally").
   - The `hacker_bbs_*` themes `@import` Google Fonts (`app/templates/themes/hacker_bbs_shared/base.css:13`).
   - The font files ship with the app, and the import becomes local `@font-face` rules. Every shipped theme is then
     safe on the onion, and clearnet visitors stop contacting Google too.
   - Check the fonts' licences: they must allow redistribution, as Google Fonts' OFL fonts do. Keep the licence
     files next to the fonts.
   - No other shipped theme or core CSS loads anything off-site (checked 2026-10-25). With this fix the CSP backstop
     breaks no shipped theme.
2. **No hard-coded first-domain name anywhere in the repo** (owner: "Everything in the repo").
   - **App:** `app/` comments and docstrings name it in 8 files today, with no logic. Reword them to "the first
     domain", or name the role.
   - **Tests:** about 60 docstrings and a few literals (for example tests/test_storefront_domains.py:54,
     tests/test_memo_shape.py:64) name it. Use SERVER_NAME, fixture domains or neutral example hosts instead.
   - **Deploy config:**
     - `Caddyfile` takes its site address and admin email from environment placeholders (Caddy's `{$VAR}`),
       set in the deploy environment.
     - `compose.prod.yaml` names only placeholders, such as `example.org`, in its comments.
     - The live deploy must behave exactly as before. Verify with the Caddy routing tests
       (tests/test_caddyfile_routing.py) and a config `caddy validate` with the prod values.
   - **Docs:** `docs/` and the other tracked docs are rewritten the same way, including historical specs and plans
     (Claude's reading of "everything in the repo").
   - **Out of the repo's reach:** git history and commit messages. The git-ignored `deploy/` guide stays as it is,
     since it is local-only and records the real host.
   - **Guard:** a test scans every tracked file (`git ls-files`) and fails if the first domain's name appears. The
     name it looks for is not a literal, so the guard does not hard-code it either. The test app's SERVER_NAME is a
     test host, so the real name has to come from somewhere else: the deploy environment, or a hash the guard
     compares against (Claude's call: a SHA-256 of each lower-cased host-like token, compared with a committed hash
     of the name, works with no secret). The builder picks; the gate must actually check it.
