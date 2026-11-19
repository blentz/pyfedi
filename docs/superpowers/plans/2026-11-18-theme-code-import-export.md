# Custom themes phase 6: import, export and theme code Implementation Plan

**Goal:** Export any custom theme as a zip; import one into a domain; let holders of the new `manage theme code`
permission give a custom theme Jinja template overrides and a `scripts.js`.

**Spec:** docs/superpowers/specs/2026-11-18-theme-code-import-export-design.md (TC1-TC4).

**Architecture:**
- `manage theme code` joins `app/permissions.py`'s CATALOGUE (domains group, scopable, not Staff).
- New table `custom_theme_template`; new column `custom_theme.script`. Migration revises `e8b3d6a1f4c7`.
- `app/theme_code.py` owns: template-name rules and parse checks, the database loader (`CustomThemeLoader`, prefix
  `custom_theme/<id>/<version>/`), the `join_path` mapping, and the archive reader/writer (`read_archive`,
  `write_archive`) with all zip checks.
- `render_template` (app/utils.py) asks `theme_code.override_name(template_name)` first and falls back to today's
  choice when there is none or when rendering the override raises.
- `/theme/<id>/scripts.js` beside the stylesheet route; `base.html`'s script tag uses `theme_script_url()`.

## Global Constraints

- CT1: nothing writes under `app/templates/`. TC2: overrides live only in the database.
- Code actions need `manage theme code` AND theme-holder rights on the theme's domain.
- An archive is read entirely and checked before anything is stored (all or nothing).
- Sizes are counted while decompressing; zip header sizes are never trusted.
- Invalid client input is a 400 with a reason, never a 500.
- Project rules: TDD, one commit per task, 100% changed-line and branch coverage
  (`python tests/check_changed_line_coverage.py coverage.json <base> --branches`), floors only rise, ruff clean, fuzz
  seeds for new routes, no domain or host named.

### Task 1: Permission, storage and limits
- [ ] Tests: the permission is in the catalogue (scopable, not Staff, description warns it is admin-equivalent); an
  RBAC grant on one domain passes `user_access(..., on=that domain)` only; the migration up/down; config defaults.
- [ ] `Permission('manage theme code', 'domains', True, False, ...)`; `CustomThemeTemplate` model and
  `CustomTheme.script`; relationship `templates` (cascade); ModLog actions in `admin_only_actions`; config limits.

### Task 2: Template rules and the database loader
- [ ] Tests: allowed and refused names (unknown, `themes/…`, wrong extension, `..`); parse errors give line and
  message; the loader serves `custom_theme/<id>/<version>/<name>`, refuses a stale version and other ids; `join_path`
  maps an include inside an override to the same theme's override, else the base theme's, else the app's.
- [ ] `app/theme_code.py`: `check_template_name`, `check_template_source`, `CustomThemeLoader`, environment
  `join_path`; register the loader in `app/__init__.py` (ChoiceLoader in front of the app loader).

### Task 3: Rendering with overrides
- [ ] Tests: a code theme's override renders on its domain; not on another domain; a theme without that override
  falls through to the base theme's shipped override and then the app's; an override that raises renders the
  fallback, logs, and records the failure for the admin flash; preview renders overrides for the previewer alone;
  community themes stay stylesheet-only; onion page with a code theme keeps onion rewriting.
- [ ] `render_template` change; failure record (cache key per theme) read by the edit page.

### Task 4: The script
- [ ] Tests: `/theme/<id>/scripts.js` content type, nosniff, caching with matching `v`, 404 elsewhere and for a theme
  without a script; base.html includes it with the nonce and drops the base theme's script; login gate open lists
  include it; onion CSP unchanged.
- [ ] Route, `theme_script_url`, base.html and dillo base tag, login gate, hidden-routes allow-list.

### Task 5: Editing code in the admin
- [ ] Tests: code holder adds, edits, removes an override and the script; invalid name/source is a 400 re-render with
  the message; limits; theme holder without the permission sees the read-only list and gets 403 on every code POST;
  mod log entries; `updated_at` bumps.
- [ ] Routes under `/admin/domains/<id>/themes/<tid>/templates…` and `/script`; template changes.

### Task 6: Export
- [ ] Tests: zip layout and manifest; templates and script included; a missing stored file is skipped with a flash;
  non-holder 403; other domain's theme 404; GET not allowed.
- [ ] `write_archive`, the export route, an Export button.

### Task 7: Import
- [ ] Tests (each a 400 with the reason, nothing stored): not a zip, too large, `..`/absolute/backslash/duplicate
  names, symlink entry, encrypted entry, unknown top-level entry, bad manifest, newer format, missing base theme,
  entry over its limit, decompressed total over the limit (a crafted zip bomb), bad template, bad file type. And:
  CSS-only import by a plain theme holder; code import needs the permission (403 otherwise); name clash gets the copy
  suffix; export→import round trip is identical; onion import strips off-site CSS; mod log.
- [ ] `read_archive`, the import route and form.

### Task 8: Copy, acceptance, fuzz, docs
- [ ] Tests: copy keeps overrides and script; delete cascades; RBAC acceptance rows for the new permission; fuzz
  family seeds for the new routes and a zip corpus seed.
- [ ] Docs: CHANGES list (migration row, config rows, feature row), spec status line.
