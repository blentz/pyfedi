# Per-domain branding (project 4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each domain can set its own name, logo, theme, CSS, sidebar, description and About page. A blank field
falls back to the first domain's `Site` values. On the onion, branding never causes a clearnet fetch. No file in the
repo names the first domain.

**Architecture:**
- A new `domain_branding` table, one optional row per sister domain, stores the domain's own values.
- `app/branding.py` merges them over `Site` row 1 into one effective `Branding` per domain. The result is memoised
  and cleared with `clear_domain_cache`.
- Templates read `branding.<field>` instead of `g.site.<field>`.
- Federation, email and manifest code ask `branding_for(domain)`.
- An off-site reference scanner backs the admin warning and the onion's save refusal, render stripping and CSP.

**Tech Stack:** Flask, Jinja, SQLAlchemy/Alembic, Celery, Pillow, pytest (`./run_tests.sh`), Caddy.

**Spec:** docs/superpowers/specs/2026-10-25-per-domain-branding-design.md

## Global Constraints

- "The first domain's settings are the app-wide defaults." Its branding is `Site` row 1, edited in the site
  settings as today.
- "A blank field means 'use the default'; there is no separate on/off switch."
- Theme order: "the user's own theme, if they set one; else the domain's default theme; else the site default
  theme; else `piefed`."
- "On the onion, branding never makes a visitor's browser fetch anything from outside the onion." This is enforced
  on the onion's *effective* branding, inherited values included. Elsewhere, saving succeeds with a warning that
  names each reference.
- Federation-facing output (NodeInfo, instance actor, API) publishes a sister domain's own value if set, else
  "exactly what it is today". The first domain's output "does not change by a byte".
- "No domain name is hard-coded." Everything in the repo is covered: app, tests, docs and deploy config. A guard
  test compares a SHA-256 hash, so the guard never contains the name.
- Do not hard-code the first domain's host anywhere, including in this plan's code, tests and commit messages. Use
  SERVER_NAME, fixture hosts, or `example.org`.
- Project rules:
  - TDD, one commit per task or defect.
  - 100% changed-line and branch coverage: `python tests/check_changed_line_coverage.py coverage.json <BASE> --branches`.
  - Inline imports only if technically unavoidable.
  - Slow tests are bugs.
  - Invalid client input stays a 400.
  - Fuzz targets reaching the registry stub `storefront.domain_rows`.
  - Custom CSS and sidebar HTML get no less escaping than the `Site` values.

## Review Focus

1. A sister domain uploads a logo, then replaces it. Only that domain's old files are removed, never the `Site`
   logo files it was inheriting (Task 5 test).
2. The onion inherits the first domain's sidebar, and the first domain later adds `<img src="https://…">`. The
   image is stripped from onion pages and still shown on the first domain (Task 4 test).
3. A domain's stored default theme is no longer in `theme_list()`. The page renders with the next fallback in theme
   order, with no 500 (Task 2 test).
4. A Celery email to an onion account, with no request and no `g.site`, carries the onion's display name and never
   raises (Task 3 test).
5. An admin saves branding, and the next page load on that domain shows it. The memo is cleared, so no other worker
   keeps serving the old values past the shared cache (Task 5 test).

---

### Task 1: Branding storage and the effective-branding loader

**Files:**
- Create: `app/branding.py`
- Create: a migration under `migrations/versions/` adding table `domain_branding`
- Modify: `app/models.py` (new `DomainBranding` model next to `SiteDomain`)
- Modify: `app/storefront.py` (`clear_domain_cache` also clears the branding memo)
- Test: `tests/test_branding.py`

**Interfaces (produces):**
- `DomainBranding` columns:
  - `site_domain_id` Integer PK, FK `site_domain.id` ON DELETE CASCADE;
  - `name` String(256);
  - `description` String(256);
  - `logo`, `logo_180`, `logo_152`, `logo_32`, `logo_16` String(40);
  - `default_theme` String(20);
  - `additional_css` Text;
  - `sidebar` Text, `sidebar_html` Text;
  - `about` Text, `about_html` Text.
  - All are nullable. NULL or '' means "inherit".
- `class Branding(NamedTuple)` holds the same fields minus the Markdown sources: `name, description, logo, logo_180,
  logo_152, logo_32, logo_16, default_theme, additional_css, sidebar_html, about_html`, all `str` with '' for
  nothing.
- `branding_for(domain: DomainInfo) -> Branding` returns the effective values, each the domain's own if non-blank,
  else `Site` row 1's. For `domain.is_default` it is exactly `Site`'s values. It is memoised per `domain.id`, like
  `domain_rows`.
- `own_branding(domain: DomainInfo) -> DomainBranding | None` returns the raw row, for the admin form and for
  federation "own value if set".
- `current_branding() -> Branding` is `branding_for(current_storefront())`.

- [ ] Write tests:
  - `test_default_domain_is_the_site`: every field equals `Site` row 1's value.
  - `test_blank_fields_inherit_per_field`: the onion sets only `name`, gets its own name and every other field from
    `Site`.
  - `test_own_values_win`.
  - `test_saved_change_is_seen_after_clear_domain_cache`.
  - `test_deleting_a_domain_deletes_its_branding`.
- [ ] Run them and see them fail. Implement. Run them and see them pass. Run `flask db upgrade` on the test stack;
  the migration has a working downgrade.
- [ ] Commit: `feat: a domain can store its own branding, falling back to the first domain's (project 4)`.

### Task 2: Pages render the domain's branding

**Files:**
- Modify: the request hooks or context processor that inject template globals (`app/request_hooks.py:72`).
- Modify: every template and view that reads a branding field from `g.site` or `site`. Find them with
  `grep -rnE "(g\.site|site)\.(name|description|logo(_180|_152|_32|_16)?|sidebar_html|about_html|additional_css|default_theme)\b" app`.
  As of 2026-10-25 that is about 25 files. Leave the federation, email and manifest uses for Task 3. Leave the
  admin site-settings form, which edits `Site` itself.
- Modify: `current_theme` (app/utils.py:4878).
- Test: `tests/test_branding_pages.py`.

**Interfaces:**
- Consumes `current_branding()` from Task 1.
- Produces the Jinja global `branding`, a `Branding`.

- [ ] Write tests (test client, two_domains fixture or equivalent):
  - `test_first_domain_pages_unchanged`: the home page, About, a community page and the sidebar on the first domain
    are byte-identical to a snapshot taken before this task's code. Capture the snapshot in the test's first,
    failing run from the unchanged code and commit it.
  - `test_onion_header_title_logo_sidebar_about_css_are_its_own`.
  - `test_user_theme_beats_domain_theme`.
  - `test_domain_theme_beats_site_theme_for_visitors`.
  - `test_unknown_stored_theme_falls_back`, for Review Focus 3.
  - `test_no_template_reads_site_branding_directly`: a ratchet. It greps `app/templates` for `g.site.<branding
    field>` and fails on any hit.
- [ ] Run them and see them fail. Implement. Run them and see them pass.
- [ ] Commit: `feat: each domain's pages show its own branding (project 4)`.

### Task 3: Emails, the manifest and what other servers read

**Files:**
- Modify: `app/email.py`.
- Modify: `/manifest.json` (app/main/routes.py:1391-1420).
- Modify: `domain_site_name` (app/federation_urls.py:102).
- Modify: the instance actor (app/activitypub/util.py:6210-6230).
- Modify: NodeInfo (app/activitypub/routes.py:397).
- Modify: the API site documents (app/api/alpha/views.py:1418-1455, 1520-1530).
- Test: `tests/test_branding_outbound.py`.

**Interfaces:**
- Consumes `branding_for` and `own_branding` from Task 1, and `storefront.domain_by_id`.
- An email's domain is the recipient account's `site_domain_id`, or the first domain when there is no account.
  Emails do not depend on `g.site`.
- Federation and API fields use `own_branding(domain).<field>` when it is non-blank, else today's expression
  unchanged. `domain_site_name(domain, site)` returns the own name if set, else today's value.

- [ ] Write tests:
  - `test_email_sender_names_recipients_home_domain`, with a Celery-style call: no request context, no `g.site`.
    This covers Review Focus 4.
  - `test_manifest_is_per_host`.
  - `test_sister_with_own_name_publishes_it_in_nodeinfo_actor_api`.
  - `test_sister_without_own_values_publishes_exactly_today`. Compare against the existing per-domain document
    tests, and keep them green.
  - `test_first_domain_documents_unchanged`: tests/test_c3_golden_documents.py passes untouched.
- [ ] Run them and see them fail. Implement. Run them and see them pass.
- [ ] Commit: `feat: emails, the manifest and federation name a domain by its own branding (project 4)`.

### Task 4: Off-site references: the scanner, the onion's render guard and CSP

**Files:**
- Modify: `app/branding.py`.
- Modify: `app/request_hooks.py` (CSP, near :264-271).
- Test: `tests/test_branding_offsite.py`.

**Interfaces (produces):**
- `offsite_references(css: str = '', html: str = '', urls: Iterable[str] = (), own_host: str = '') -> list[str]`
  - It returns every reference a browser would fetch from a host other than `own_host`:
    - CSS `url(...)` and `@import`, quoted, unquoted and `//`-relative;
    - HTML auto-loading attributes: `src`, `srcset`, `poster`, and `<link href>` for stylesheets and icons, plus
      inline `style` `url()`;
    - each of `urls`.
  - A relative URL is on-site.
  - Plain `<a href>` links are not fetches, so they are ignored.
- `onion_safe(branding: Branding, own_host: str) -> Branding` returns a copy with every off-site reference removed
  and logs each removal at info. Wire it in so that, on the onion, `current_branding()` returns
  `onion_safe(branding_for(...))`.
- CSP on onion responses, for every visitor, logged in or not:
  - `img-src`, `font-src`, `style-src`, `connect-src` and `media-src` are limited to `'self'` (plus `data:` for
    images and fonts);
  - the existing `script-src` nonce policy is kept;
  - a view's own policy (`setdefault`, as the onion media proxy uses) still stands.

- [ ] Write tests:
  - `test_scanner_finds_each_form`, with parametrised cases for every form above, plus on-site and relative cases
    that must not be flagged.
  - `test_inherited_offsite_image_is_stripped_on_onion_only`, for Review Focus 2.
  - `test_onion_pages_send_the_csp_for_anonymous_and_logged_in`.
  - `test_onion_csp_keeps_proxied_remote_media_working`: an onion post with remote media renders its proxy URL,
    and that URL satisfies the policy.
  - `test_clearnet_csp_unchanged`.
- [ ] Run them and see them fail. Implement. Run them and see them pass.
- [ ] Commit: `feat: the onion's branding never loads anything off the onion (project 4)`.

### Task 5: Admin: edit a domain's branding

**Files:**
- Modify: `app/admin/routes.py`:
  - new `admin_domain_branding`;
  - extract the logo handling from `admin_site` (app/admin/routes.py:186-300).
- Modify: `app/admin/forms.py` (new `DomainBrandingForm`).
- Modify: `app/templates/admin/domains.html` (a "Branding" link per sister row).
- Create: `app/templates/admin/domain_branding.html`.
- Test: `tests/test_admin_domain_branding.py`.

**Interfaces:**
- Route `GET/POST /admin/domains/<int:domain_id>/branding`, for admins only.
  - The default row redirects to `admin.admin_site`.
  - An unknown id gives 404.
- `DomainBrandingForm` has fields `name, description, logo (FileField), default_theme (Select: '' meaning inherit,
  then theme_list()), additional_css, sidebar, about`.
  - Markdown is rendered to `sidebar_html` and `about_html` the way `admin_site` does it.
- `save_logo_upload(upload, directory: str) -> dict[str, str]` returns the five logo paths, extracted from
  `admin_site` and used by both routes. `admin_site` behaviour is unchanged: its existing tests stay green.
  - Replacing a domain's logo removes only that domain's previous files. It never removes a path that is also in
    use by `Site`'s logo fields (Review Focus 1).
- Saving calls `clear_domain_cache()`.
- Off-site references from Task 4's `offsite_references`:
  - On the onion row, the save is refused: a form error naming each reference, and nothing is written.
  - On any other row, and on `admin_site`'s CSS, sidebar and About, the save succeeds and flashes a warning naming
    each reference.

- [ ] Write tests:
  - `test_admin_sets_and_clears_each_field`: a cleared field inherits again.
  - `test_default_row_redirects_to_site_settings`.
  - `test_non_admin_403`.
  - `test_bad_upload_is_400_not_500`.
  - `test_logo_replace_never_deletes_site_logo_files`, for Review Focus 1.
  - `test_onion_offsite_refused`.
  - `test_other_domain_offsite_warns`.
  - `test_site_settings_offsite_warns`.
  - `test_saved_branding_shows_on_next_load`, for Review Focus 5.
- [ ] Run them and see them fail. Implement. Run them and see them pass.
- [ ] Commit: `feat: admins edit each domain's branding on the Domains page (project 4)`.

### Task 6: The hacker_bbs themes host their fonts

**Files:**
- Modify: `app/templates/themes/hacker_bbs_shared/base.css:13`. Replace the Google Fonts `@import` with local
  `@font-face` rules.
- Create: `app/static/fonts/hacker_bbs/`. Add the woff2 files for Share Tech Mono, VT323, Press Start 2P,
  Silkscreen, Major Mono Display, and Crimson Pro 300 and 400. Add each font's `OFL.txt`.
- Test: `tests/test_shipped_css_offsite.py`.

Download the fonts from their upstream OFL sources, such as google/fonts on GitHub. Verify each licence is OFL
before committing it. If a licence is not OFL, stop and report.

- [ ] Write the test `test_no_shipped_css_loads_offsite`. It runs `offsite_references(css=…)` (Task 4) over every
  `.css` under `app/templates/themes/` and `app/static/` (the vendored libraries included) and expects no result.
- [ ] Run it and see it fail on `hacker_bbs_shared/base.css`. Vendor the fonts. Run it and see it pass.
- [ ] Commit: `fix: the hacker_bbs themes serve their fonts themselves (project 4)`.

### Task 7: No hard-coded first-domain name in app, tests or deploy config

**Files:**
- Modify: every tracked file outside `docs/` that names the first domain. Find them with `git ls-files | xargs grep
  -l <name>`; the name is SERVER_NAME in `.env.quadlet` on prod. As of 2026-10-25 that is 8 `app/` files (comments
  and docstrings only), about 60 test files, the `Caddyfile`, `compose.prod.yaml` and the root docs.
- In comments and docstrings, write "the first domain", or "SERVER_NAME" where the config value is meant.
- Test literals become SERVER_NAME, the fixture hosts, or `example.org`.
- `Caddyfile`:
  - the main site address becomes `{$PIEFED_DOMAIN}`;
  - the ACME email becomes `{$ACME_EMAIL}`;
  - the www redirect comment uses `{$PIEFED_DOMAIN}`.
- `compose.prod.yaml` comments use `example.org`.
- `tests/test_caddyfile_routing.py` resolves the placeholders with test values.

- [ ] Run the full gate. Every test still passes, with no behaviour change. Check that `caddy validate` passes on
  the Caddyfile with `PIEFED_DOMAIN` and `ACME_EMAIL` set to the prod values, run in the pyfedi-caddy image.
- [ ] Commit: `chore: no file outside docs names the first domain (project 4)`.

### Task 8: Docs too, and the guard

**Files:**
- Modify: every tracked file under `docs/` that names the first domain, historical specs and plans included. Use
  "the first domain"; for an address in an example, use `example.org`.
- Create: `tests/test_no_first_domain_literal.py`.

**Interfaces:**
- `FIRST_DOMAIN_SHA256 = '<hex>'` is the SHA-256 of the lower-cased first-domain host, committed in the test.
- For every `git ls-files` path, the test lower-cases each host-like token (regex `[a-z0-9.-]+\.[a-z]{2,}`) and
  every dot-separated suffix of it. It fails, naming the file and line, if any hash equals `FIRST_DOMAIN_SHA256`.
- The test must finish in under 5 s. Hash each distinct token once.

- [ ] Write the guard. Run it and see it fail; it lists the remaining docs hits. Rewrite the docs. Run it and see it
  pass.
- [ ] Commit: `docs: no document names the first domain, and a test keeps it so (project 4)`.

### Task 9: Deploy and verify (controller)

The Caddyfile now needs `PIEFED_DOMAIN` and `ACME_EMAIL` set in the pyfedi-caddy unit, **before** the owner pushes.
Without them Caddy serves nothing.

- [ ] On prod:
  - back up `pyfedi-caddy.container`;
  - add `Environment=PIEFED_DOMAIN=…` and `Environment=ACME_EMAIL=…`, with the values from the current Caddyfile;
  - mirror the change in the local `deploy/quadlet/`.
- [ ] Run `caddy validate` with the new Caddyfile and env, inside the running container image, before the push.
- [ ] Owner pushes. After autodeploy:
  - 0 new traceback kinds over 10 min;
  - the identity documents are byte-identical to `~/backups/2026-10-25-identity/after`, with only `updated` and
    counts differing;
  - first-domain pages look unchanged;
  - an onion page carries the CSP and has no off-site `src` or `url(`;
  - a hacker_bbs theme on the onion loads its fonts from the onion.
- [ ] Update DEPLOY_GUIDE §11 (branding and the Caddy env) and the memory `onion-storefront-track`.
