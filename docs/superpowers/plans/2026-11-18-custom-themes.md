# Custom themes per domain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A domain's theme holders copy a shipped or custom theme, rename it, edit its CSS, upload images and fonts
for it, retire it and delete it when unused. Shipped themes never change. A custom theme exists only on its own
domain, in every theme picker there.

**Architecture:**
- Two new tables, `custom_theme` and `custom_theme_file`. A theme column holds `custom:<id>` for a custom theme.
- A new module `app/custom_themes.py` owns parsing identifiers, deciding whether a theme counts on a domain, picker
  choices, copying (with URL rewriting), in-use counts and file storage.
- `current_theme()` keeps returning the stored identifier in effect. A new `template_theme()` returns the shipped
  theme whose templates and `scripts.js` render (a custom theme's base theme); the Jinja global `theme` and
  `render_template`'s override lookup use it, so the many `themes/<theme()>/base.html` headers stay untouched.
- A new `theme_stylesheet_url(identifier)` gives the stylesheet URL for shipped and custom themes; `base.html` uses it.
- New routes serve `/theme/<id>/styles.css` and `/theme/<id>/files/<filename>` on the theme's own domain.
- Admin pages under `/admin/domains/<id>/themes`, gated as branding is.

**Tech Stack:** Flask, Jinja, SQLAlchemy/Alembic, Pillow, pytest (`./run_tests.sh`, `./run_tests_remote.sh`).

**Spec:** docs/superpowers/specs/2026-11-18-custom-themes-design.md (CT1-CT9). Phase 6 (§3.10, import/export with
code) is out of this plan; it needs its own spec.

## Global Constraints

- CT1: "No page or API writes to" shipped themes. Nothing in this plan opens a file under `app/templates/themes/`
  for writing.
- CT2: custom themes are CSS plus image and font files. No Jinja, no JavaScript.
- CT3: a custom theme counts only on the domain that owns it. Its routes 404 elsewhere.
- CT4: theme holders are `edit domain branding` holders on the domain; for the first domain, `change instance
  settings` holders, as for its branding.
- CT7: a theme in use cannot be deleted. CT9: retired themes are offered in no picker but still render where chosen.
- CT8 limits are config with these defaults: `CUSTOM_THEME_MAX_FILE_BYTES=2 MB`, `CUSTOM_THEME_MAX_BYTES=10 MB`,
  `CUSTOM_THEME_MAX_FILES=50`, `CUSTOM_THEME_MAX_CSS_BYTES=512 KB`.
- No domain or host is named anywhere: use SERVER_NAME, fixture domains or `example.org`.
- No visible change on any domain without a custom theme.
- Project rules:
  - TDD, one commit per task or defect.
  - 100% changed-line and branch coverage: `python tests/check_changed_line_coverage.py coverage.json 97a93a04a --branches`.
  - `coverage_floors.ini` floors only rise.
  - Inline imports only if technically unavoidable.
  - Slow tests are bugs.
  - Invalid client input stays a 400; a wrong-domain or no-permission request is a 403 (admin) or 404 (public routes).
  - Fuzz targets for every new route.
  - `ruff` clean.

## Review Focus

1. A holder of `edit domain branding` on domain A posts to any of B's theme routes (edit, upload, retire, delete,
   preview, copy). Every one is refused, and B's theme is unchanged (Task 5-8 tests).
2. A remote community's Update carries `"theme": "custom:1"`. It is not stored. A local community whose theme is
   custom federates its base theme name, never `custom:<id>` (Task 4 test).
3. A user picked domain A's custom theme and visits domain B. B renders in B's default with no 500, and A still shows
   the user's pick (Task 2 test).
4. A theme's file is uploaded as `x.png` but is HTML or an SVG with `<script>`. The HTML is refused; the SVG is served
   with the sandbox CSP and `nosniff` (Task 7 test).
5. Copying `hacker_bbs_amber` gives CSS whose `@import` resolves to `/static/themes/hacker_bbs_shared/base.css`, and
   the copy renders the same as the shipped theme (Task 5 test).
6. Delete is refused while the theme is anyone's pick, including a single user's; Retire hides it from pickers
   while that user still sees it (Task 5 and 3 tests).

---

### Task 1: Storage and identifiers

**Files:**
- Create: `app/custom_themes.py`
- Create: a migration under `migrations/versions/` adding `custom_theme` and `custom_theme_file`
- Modify: `app/models.py` (models next to `DomainBranding`)
- Modify: `config.py` (CT8 limits)
- Test: `tests/test_custom_themes.py`

**Interfaces (produces):**
- `CustomTheme`: `id` PK; `site_domain_id` FK `site_domain.id` ON DELETE CASCADE, not null, indexed; `name`
  String(100) not null; `base_theme` String(20) not null; `css` Text not null default ''; `retired` Boolean not null
  default false; `created_at`, `updated_at` DateTime; `updated_by` FK `user.id` ON DELETE SET NULL. Unique index on
  (`site_domain_id`, `lower(name)`).
- `CustomThemeFile`: `id` PK; `custom_theme_id` FK ON DELETE CASCADE; `filename` String(100); `path` String(255);
  `mime_type` String(64); `size` Integer. Unique (`custom_theme_id`, `filename`).
- `CUSTOM_PREFIX = 'custom:'`; `custom_theme_id(identifier: str) -> int | None` (None unless `custom:` followed by
  a positive int of at most 12 digits); `custom_identifier(theme_id: int) -> str`.
- `theme_counts(identifier: str, domain: DomainInfo) -> bool`: `piefed` and shipped themes count everywhere; a
  custom identifier counts only when its row exists and `site_domain_id == domain.id`. Memoised per request (`g`).
- `custom_themes_for(domain_id: int, include_retired: bool) -> list[CustomTheme]`, ordered by name.

- [ ] Write tests: identifier parsing (valid, `custom:0`, `custom:-1`, `custom:abc`, over-long, shipped names);
  `theme_counts` for shipped, piefed, own domain, other domain, deleted row; domain deletion cascades themes and file
  rows; name uniqueness is case-insensitive within a domain and free across domains.
- [ ] Fail, implement, pass. `flask db upgrade` and `downgrade` work on the test stack.
- [ ] Commit: `feat: custom themes: storage and identifiers`.

### Task 2: Rendering a custom theme

**Files:**
- Modify: `app/utils.py` (`current_theme`, `render_template` at :101, new `template_theme`, `theme_stylesheet_url`)
- Modify: `app/request_hooks.py:174` (Jinja globals: `theme` → `template_theme`; add `theme_stylesheet_url`,
  `theme_display_name`)
- Modify: `app/templates/base.html:59-62`, `app/templates/_side_pane.html:172-175`
- Create: `app/custom_themes_routes.py` (blueprint, registered in `app/__init__.py`): `GET /theme/<int:id>/styles.css`
- Test: `tests/test_custom_themes_render.py`

**Interfaces:**
- `current_theme()` walks the spec §3.4 order and accepts each candidate only if `theme_counts` on the request
  domain. Preview (Task 8) slots in first later.
- `template_theme() -> str`: the base theme of `current_theme()` if custom, else `current_theme()`; `'piefed'` if the
  base theme no longer ships.
- `theme_stylesheet_url(identifier) -> str | None`: shipped → today's `url_for('static', …, changed=mtime)`; custom →
  `url_for('custom_themes.stylesheet', theme_id=…, v=<updated_at epoch>)`; piefed or missing → None.
- `theme_display_name(identifier) -> str`: shipped json name, or the custom theme's name.
- Stylesheet route: 404 unless `theme_counts` on the request domain; `text/css; charset=utf-8`; `nosniff`;
  `Cache-Control: public, max-age=31536000, immutable` when `v` matches `updated_at`, else short max-age. On the onion,
  the CSS passes through `_scan_css` (app/branding.py:195) before serving.
- `base.html`: the community branch applies `community.theme` only if `theme_counts`; both branches link
  `theme_stylesheet_url(...)`. `_side_pane.html` shows `theme_display_name(community.theme)`.

- [ ] Write tests: a domain with no custom theme renders byte-identically to before (snapshot the `<head>` of `/`
  before the change, as the branding plan did); domain default custom theme links its stylesheet and uses the base
  theme's `base.html` (dillo base) and `scripts.js` (high_contrast base); user pick on its own domain vs another domain
  (Review Focus 3); community custom theme on and off its domain; base theme removed → piefed templates, own CSS;
  stylesheet route 404 on another domain, 404 for unknown id, cache headers; onion strips an off-site `url()`.
- [ ] Fail, implement, pass. Commit: `feat: custom themes render on their own domain`.

### Task 3: Pickers and validation

**Files:**
- Modify: `app/custom_themes.py` (`theme_choices`)
- Modify: `app/user/routes.py:688`, `app/community/routes.py:135,1701`, `app/community/util.py:1343`
  (`community_theme_list(domain, current)`), `app/shared/community.py:381`, `app/admin/routes.py:618,696`, and the
  user-settings API's theme choices
- Test: `tests/test_custom_themes_pickers.py`

**Interfaces:**
- `theme_choices(domain: DomainInfo, current: str = '') -> list[tuple[str, str]]`: `theme_list()`, then the domain's
  non-retired custom themes as `(custom:<id>, "<name> (custom)")`; if `current` is a retired theme of that domain, it
  is added as `"<name> (retired)"`.
- Domain per picker: user settings → request domain; community → the community's home domain
  (`community.site_domain_id`, default domain when NULL); branding page → that domain; site settings → first domain.
- WTForms `SelectField` validation already rejects values not in `choices`; API paths validate explicitly and return
  400.

- [ ] Write tests: each picker lists own-domain custom themes and not another domain's; retired theme absent unless it
  is the current value (Review Focus 6); posting another domain's `custom:<id>`, a newly chosen retired theme, or
  garbage is a 400 (form and API); saving unchanged with a retired current value keeps it.
- [ ] Fail, implement, pass. Commit: `feat: custom themes appear in their domain's theme pickers`.

### Task 4: Federation

**Files:**
- Modify: `app/activitypub/util.py:1128` (out), `:1779` and `:2585` (in)
- Test: `tests/test_custom_themes_federation.py`

- [ ] Write tests (Review Focus 2): outbound community actor with a custom theme publishes the base theme name, with
  `piefed` base publishes no `theme`; shipped theme unchanged; inbound create and update store only shipped theme
  names, ignore `custom:…`, unknown names and non-strings, and leave the stored value untouched.
- [ ] Fail, implement (a `shipped_theme_or_none(value)` helper in `app/custom_themes.py`), pass.
- [ ] Commit: `fix: a community's theme federates only shipped theme names`.

### Task 5: Admin Themes page: list, copy, rename, retire, delete

**Files:**
- Modify: `app/admin/routes.py` (new routes), `app/admin/forms.py`
- Create: `app/templates/admin/domain_themes.html`, `app/templates/admin/domain_theme_view.html` (shipped CSS,
  read-only)
- Modify: `app/templates/admin/domains.html` (Themes link beside Branding), site settings template (first domain's
  link)
- Modify: `app/custom_themes.py` (`copy_theme`, `rewrite_relative_urls`, `uses`)
- Test: `tests/test_custom_themes_admin.py`

**Interfaces:**
- Routes, all `POST` except views, CSRF-protected, permission per CT4 checked on every one with `on=row`:
  - `GET /admin/domains/<id>/themes`
  - `GET /admin/domains/<id>/themes/shipped/<name>` (view CSS; 404 unless `is_shipped_theme`)
  - `POST /admin/domains/<id>/themes/copy` (`source` = shipped name or own `custom:<id>`)
  - `POST /admin/domains/<id>/themes/<theme_id>/rename`, `/retire`, `/unretire`, `/delete`
  - A `theme_id` not owned by `<id>` is a 404.
- `rewrite_relative_urls(css: str, base: str) -> str`: every relative `url()` / `@import` target (no scheme, not
  starting with `/`, not `data:`, not `#`) becomes `/static/themes/<base>/<normalised path>`; query strings kept.
  Uses `storefront._css_url_spans` as `_scan_css` does.
- `copy_theme(source, domain_id, user) -> CustomTheme`: name "Copy of <name>", made unique with " (2)", " (3)"…;
  copies file rows and stored files for a custom source (new random paths).
- `uses(theme) -> dict`: `{'domain_default': bool, 'site_default': bool, 'users': int, 'communities': int}`.
- Delete is refused while any use is non-zero (CT7): the page re-renders with a flash listing the uses.
- Every change writes `add_to_modlog` with the domain.

- [ ] Write tests: holder on A sees only A; cross-domain refusals (Review Focus 1); copy of every shipped theme
  produces CSS with no remaining relative reference and the hacker_bbs case (Review Focus 5); copy of a custom theme
  copies files; rename uniqueness and shipped-name clash; retire/unretire; delete refused with each kind of use, then
  allowed when unused and its files unlinked; no write under `app/templates/themes/` (patch `open` and `os` write
  calls during the whole admin test module and assert none target that path).
- [ ] Fail, implement, pass. Commit: `feat: admins copy, rename, retire and delete a domain's custom themes`.

### Task 6: Editing the CSS

**Files:**
- Modify: `app/admin/routes.py`, `app/admin/forms.py`
- Create: `app/templates/admin/domain_theme_edit.html`
- Test: extend `tests/test_custom_themes_admin.py`

**Interfaces:**
- `GET|POST /admin/domains/<id>/themes/<theme_id>/edit`: name, base theme (read-only), CSS textarea.
- Save: over `CUSTOM_THEME_MAX_CSS_BYTES` is a form error; `offsite_references(css=…, own_host=…)`
  (app/branding.py:271) refuses on the onion naming each reference, else saves and flashes the warning, as branding's
  `offsite_by_field` flow does (app/admin/routes.py:360-380). Sets `updated_at`, `updated_by`.

- [ ] Write tests: save changes the served stylesheet and its `v`; size cap; onion refusal; clearnet warning; a CSS
  containing `</style><script>` is served verbatim as `text/css` and never inlined into a page.
- [ ] Fail, implement, pass. Commit: `feat: admins edit a custom theme's CSS`.

### Task 7: Files

**Files:**
- Modify: `app/custom_themes.py` (`store_theme_file`, `remove_theme_files`, `sniff_type`)
- Modify: `app/custom_themes_routes.py` (`GET /theme/<id>/files/<filename>`)
- Modify: admin edit route and template (upload, list with `url(files/<name>)`, remove)
- Test: `tests/test_custom_themes_files.py`

**Interfaces:**
- `sniff_type(data: bytes) -> str | None`: PNG, JPEG, GIF, WebP, AVIF via Pillow `Image.open(...).verify()` and
  format; SVG by parsing as XML with an `svg` root (defusedxml or the parser branding already uses); WOFF2 `wOF2`,
  WOFF `wOFF`, TTF `\x00\x01\x00\x00`/`true`, OTF `OTTO`. Anything else None → form error.
- Filename: reduced to `[A-Za-z0-9._-]`, ≤100 chars, no leading dot, extension matching the sniffed type replaced in;
  duplicate name replaces the old file after confirmation field.
- Stored as `app/static/media/themes/<random hex>.<ext>`; never served by that static path in pages: CSS references
  go through the route. The route 404s off-domain, sends the stored MIME type, `nosniff`, long cache, and for SVG
  `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; sandbox`.
- Limits (CT8) checked before writing; over a limit is a form error.
- `remove_theme_files(paths)`: same guards as `remove_superseded_logos` (app/admin/routes.py:333): only a regular,
  non-symlink file directly inside the themes media directory, named as `store_theme_file` names, that no row names.

- [ ] Write tests (Review Focus 4): each allowed type stored and served with the right MIME; HTML-as-png refused;
  SVG with script served sandboxed; path traversal names (`../x.png`, `.htaccess`, `a/b.png`) cleaned; each limit;
  removal unlinks only owned files; a symlink in the media dir is not followed.
- [ ] Fail, implement, pass. Commit: `feat: custom themes carry uploaded images and fonts`.

### Task 8: Preview

**Files:**
- Modify: `app/utils.py` (`current_theme` checks the preview first), admin routes, `base.html` (banner)
- Test: `tests/test_custom_themes_preview.py`

**Interfaces:**
- `POST /admin/domains/<id>/themes/<theme_id>/preview` sets `session['theme_preview'] = {'domain_id', 'theme'}` and
  redirects to `/`; `POST /admin/theme-preview/stop` clears it.
- `current_theme` honours it only for an authenticated user who still holds CT4's permission on that domain, on that
  domain, for a theme that counts there. Otherwise it is ignored and cleared.
- The banner names the theme and has the stop button.

- [ ] Write tests: preview applies only to that user and domain; losing the permission ends it; another domain is
  unaffected; retired themes can be previewed.
- [ ] Fail, implement, pass. Commit: `feat: theme holders preview a custom theme`.

### Task 9: RBAC acceptance, fuzz targets, docs

**Files:**
- Modify: `tests/test_rbac_acceptance.py` (the composed domain admin manages its domain's themes, not another's)
- Modify: the fuzz route registry (`tests/test_fuzz_routes_registry.py` / `tests/fuzz/`) for every new route
- Modify: `docs/CHANGES-since-v1.8.10.md`; `coverage_floors.ini` (add `app/custom_themes.py`,
  `app/custom_themes_routes.py` at their measured coverage)

- [ ] Add tests and targets; run the full suite with `./run_tests_remote.sh` and the changed-line coverage check.
- [ ] Commit: `test: custom themes in the RBAC acceptance test and route fuzzing`, then
  `docs: custom themes in the fork's change list`.
