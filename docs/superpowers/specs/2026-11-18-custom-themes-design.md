# Custom themes per domain: design

Status: APPROVED 2026-11-18 (CT1-CT9). Phase 6 (§3.10) is specified in `2026-11-18-theme-code-import-export-design.md`.

Date: 2026-11-18. This builds on per-domain branding (`2026-10-25-per-domain-branding-design.md`) and RBAC
(`2026-11-03-rbac-design.md`). It reverses one branding non-goal: "Uploading new themes. A domain picks from the
shipped themes."

**Terms.**
- A *shipped theme* is a directory in `app/templates/themes/` with its `<name>.json`, plus `piefed` (no directory).
- A *custom theme* is a theme stored in the database, owned by one domain.
- A custom theme's *base theme* is the shipped theme it was first copied from, directly or through other copies.
- *Theme holders* of a domain are the people allowed to manage its themes (§3.6).

## 1. Intent

Domain admins want each domain to look like its own site. The shipped themes are close, but not close enough, and
the custom CSS field means writing a theme from nothing. A theme holder should be able to copy a theme, rename it,
change its CSS and add images and fonts to it, then use it on their domain. The shipped themes stay as they are.

## 2. Owner rulings (2026-11-18)

| # | Ruling |
|---|---|
| CT1 | **Shipped themes are immutable.** No page or API writes to them. They can be copied. |
| CT2 | **A custom theme is CSS plus uploaded images and fonts.** Template overrides and `scripts.js` come from its base theme, never from the custom theme. Theme holders cannot supply Jinja or JavaScript. |
| CT3 | **A custom theme belongs to one domain and is used only there.** On its domain it appears in every theme picker: the domain default, users' own theme, and communities' theme. On any other domain it does not exist (§3.4). |
| CT4 | **Theme holders are `edit domain branding` holders** on that domain; for the first domain, as for its branding, holders of `change instance settings`. No new permission for phases 1-5. |
| CT5 | **No copying between domains** in this design. A theme reaches another domain only through import/export (CT6). |
| CT6 | **Follow-on phase: import/export with full theme code.** Admins holding an additional permission can export a theme and import one, including Jinja template overrides and JavaScript (§3.10). |
| CT7 | **A theme in use cannot be deleted.** The delete is refused and lists where it is used. |
| CT8 | **Limits accepted:** 2 MB a file, 10 MB and 50 files a theme, 512 KB a stylesheet (config defaults). |
| CT9 | **A custom theme can be retired.** A retired theme is offered in no picker but still renders for every user, community and default that already chose it. |

## 3. Design

### 3.1 What a custom theme is

Each custom theme has:
- a display name, unique within its domain (case-insensitive) and different from every shipped theme's name;
- a base theme: a shipped theme name, or `piefed`;
- a stylesheet: CSS text, edited in the admin;
- files: images and fonts the stylesheet refers to;
- a `retired` flag (CT9);
- created and updated times, and who last changed it.

Storage (builder may refine):
- `custom_theme`: `id`, `site_domain_id` (FK, `ON DELETE CASCADE`), `name`, `base_theme`, `css`,
  `retired` (boolean, default false), `created_at`,
  `updated_at`, `updated_by` (user FK, `SET NULL`).
- `custom_theme_file`: `id`, `custom_theme_id` (FK, cascade), `filename` (as the CSS names it), `path` (the stored
  file), `mime_type`, `size`. Unique on (`custom_theme_id`, `filename`).

**Identifier.** Every `theme` column (`User.theme`, `Community.theme`, `Site.default_theme`,
`DomainBranding.default_theme`) stores `custom:<id>` for a custom theme. It fits the existing `String(20)`, no shipped
directory can be named this way, and renaming a theme changes nothing that refers to it.

### 3.2 Copy, rename, edit, delete

On the domain's Themes page (§3.5):
- **Copy** works on any theme the domain can use: shipped or one of its own custom themes. The copy is new, named
  "Copy of <name>" (made unique), with the same base theme.
  - Copying a shipped theme copies its `styles.css` text. Each relative reference (`@import`, `url()`) is rewritten to
    the absolute `/static/themes/<base>/…` path it resolved to, so the copy still loads the shared files it used
    (e.g. `hacker_bbs_shared/base.css`, `hercules.woff`, `irix/bg_pattern.gif`).
  - Copying a custom theme also copies its files.
  - A copy is a snapshot. Later upgrades to the shipped theme's CSS do not reach it. Its base theme's templates and
    `scripts.js` do follow upgrades (CT2).
- **Rename** changes the display name only.
- **Retire / Unretire** toggles CT9's flag. A retired theme stays listed on the Themes page, marked retired, with
  its uses; it can still be edited, copied and previewed. A copy of a retired theme is not retired.
- **Edit** changes the CSS, uploads files and removes files. Saving sets `updated_at`, which busts the stylesheet's
  cache (§3.3).
- **Delete** is refused while the theme is in use (CT7): as the domain default, the site default, any user's own
  theme or any community's theme. The refusal lists the uses with counts. An unused theme is deleted after
  confirmation.
  - Consequence: a theme that any user has picked stays until that user picks another. The holder cannot change
    users' settings, so the list says how many users hold it, and offers **Retire** instead (CT9).
  - A domain's removal still cascades its themes; the stale `custom:<id>` values then fail §3.4's check and fall
    back.
- **Shipped themes** show View CSS and Copy, nothing else (CT1). There is no code path that writes under
  `app/templates/themes/`.

### 3.3 Serving

- The stylesheet is served at `/theme/<id>/styles.css?v=<updated_at>` as `text/css`, with long cache lifetime
  (the `v` changes on save) and `X-Content-Type-Options: nosniff`.
- Files are served at `/theme/<id>/files/<filename>`, so the CSS can name them relatively: `url(files/logo.png)`.
- Both answer only on the theme's own domain; elsewhere they 404 (CT3).
- `base.html` (around lines 59-62) emits the custom stylesheet in place of the shipped `styles.css` when the theme in
  effect is custom. The community-theme branch does the same.
- The `theme()` Jinja global keeps meaning "the shipped theme whose templates render". For a custom theme that is its
  base theme. So the many `{% if theme() != 'piefed' and file_exists(...base.html) %}` headers and the `scripts.js`
  tag need no change. A new helper gives the stylesheet URL. This keeps the change small for upstream merges.
- If a base theme stops shipping after an upgrade, the custom theme renders with `piefed` templates and still loads
  its own CSS.

### 3.4 Which theme a page uses

`current_theme` (app/utils.py:5244) gains a validity check for custom themes. A custom theme counts only when the
request's domain owns it, and a shipped theme counts only when it still ships. The order stays as branding §3.2 set
it:
1. the user's own theme, if they set one and it counts here;
2. else the domain's default theme, if it counts;
3. else the site default theme, if it counts;
4. else `piefed`.

A user who picks a custom theme on domain A and visits domain B gets B's default there. Their stored pick is kept,
so A still shows it.

A community's theme (`base.html:59`) is applied only when it counts on the request's domain. Otherwise the page uses
the viewer's theme as above.

**Pickers.** `theme_list()` gains the request domain's custom themes after the shipped ones, labelled
"<name> (custom)":
- the user settings theme picker;
- the community theme picker (`community_theme_list`), using the community's home domain;
- the domain default picker on the branding page (custom themes of that domain);
- the site default picker in site settings (the first domain's custom themes);
- the API's theme choices (`app/shared/community.py:381` and the user settings API).

Retired themes are left out of every picker, except that a picker whose saved value is a retired theme still shows
that one option, labelled "<name> (retired)", so saving the form unchanged keeps it. A retired theme still counts in
§3.4: it renders wherever it is already chosen.

Every save that takes a theme validates it against the same list. A posted `custom:<id>` of another domain is a 400,
and so is a newly chosen retired theme (one that is not the field's current value).

### 3.5 Editing

- A **Themes** link sits beside Branding on `/admin/domains`, for the same people who see Branding. It goes to
  `/admin/domains/<id>/themes`. The first domain's link is on the site settings page.
- The list shows shipped themes (read-only) and the domain's custom themes, with the actions in §3.2 and where each is
  in use.
- The edit page has the name, the base theme (read-only), a monospace CSS textarea, the file list with each file's
  `url(files/…)` reference to copy, an upload control, and remove buttons.
- **Preview** shows the site in that theme for the theme holder only, without changing any default: a session flag
  that `current_theme` honours for that user on that domain, with a banner and a button to stop.
- Every save writes a mod-log entry for the domain (create, copy, rename, edit, upload, remove, delete).

### 3.6 Who

- Per CT4. A holder on domain A sees and manages only A's custom themes. The permission check is on every route,
  read and write. Holding the permission on A and posting to B's theme is a 403.
- RBAC's acceptance test (RB3, `tests/test_rbac_acceptance.py`) gains: the composed "domain admin" manages its
  domain's themes and cannot reach another domain's.

### 3.7 Files

- **Allowed types**, checked by content (magic bytes / Pillow / font parser), not by the name: PNG, JPEG, GIF, WebP,
  AVIF, SVG; WOFF2, WOFF, TTF, OTF. The stored MIME type comes from the check, never from the upload.
- **SVG** may hold script. It is served with `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline';
  sandbox`, so opening it directly runs nothing. Loaded from CSS it never runs script anyway.
- **Filenames** are reduced to `[A-Za-z0-9._-]`, at most 100 characters, no leading dot. The stored path is a random
  name under the media directory, never the posted name.
- **Limits**, config with defaults: 2 MB a file, 10 MB and 50 files a theme. Over a limit is a form error, not a 500.
- Removing a file or a theme unlinks its stored files, with the same care as `remove_superseded_logos`: only a regular
  file the upload wrote, inside the media directory, that no row still names.
- Files live with the other local media, so whatever already backs up media covers them.

### 3.8 Safety of the CSS

- The CSS has the power that a domain's existing custom CSS field has today. Theme holders can already set that, so
  this adds no new power. It is served as its own file, so it cannot close a `<style>` element.
- **Off-site references** follow branding §3.4 using `offsite_references` (app/branding.py:271):
  - on the onion domain, a save with an off-site `url()` or `@import` is refused, naming each one; on render,
    off-site references are emptied, as `_scan_css` does today, and logged; the onion CSP stays the backstop;
  - on every other domain the save succeeds with a warning naming each reference.
- A theme stylesheet larger than 512 KB (config) is refused. The largest shipped one is about 270 KB.

### 3.9 Federation

Community themes federate today: out at `app/activitypub/util.py:1128`, in at `:1779` and `:2585`.
- **Out:** a community whose theme is custom publishes its base theme's name (or nothing for `piefed`), never
  `custom:<id>`.
- **In:** a remote community's `theme` is stored only if it is a shipped theme name. Anything else, `custom:…`
  included, is ignored. Today any string is stored. This closes a remote server pointing a community at a local
  domain's custom theme.

### 3.10 Follow-on: import and export with theme code (CT6)

Outlined here; it gets its own spec before it is built.

- **New RBAC permission**, e.g. `manage theme code`, domain-scoped like `edit domain branding`. Not part of any
  built-in role but Admin. It is never implied by `edit domain branding`.
- **Why it is separate:** Jinja templates run on the server with the app's full context (config, database session,
  secrets), and JavaScript runs in every visitor's browser on that domain, admins included. Holding this permission
  on any domain is equivalent to full instance admin. The permission's description in the RBAC UI says so.
- **Export:** any theme holder can export a custom theme as an archive: a manifest (name, base theme, format
  version), `styles.css` and the files. Exports of a theme that has code include it.
- **Import:** a holder of the new permission can import an archive into a domain they hold it on. An archive with
  no templates or scripts can be imported by any theme holder: it is then just CSS and files, checked as in §3.7-3.8.
- **Code in a custom theme:** template overrides with the same names as a shipped theme's (`base.html`,
  `index.html`, `post/…`), and `scripts.js`. Where a custom theme has an override, it wins over its base theme's.
  Scripts get the page's existing nonce.
- **Editing code** in the admin after import is allowed only to holders of the new permission. Theme holders without
  it can still rename, edit the CSS and files, and copy (the copy keeps the code); every change is mod-logged.
- **To decide in that spec:** the archive format and its validation (zip-slip, size, allowed names), where template
  overrides live so the Jinja loader finds them (a database-backed loader or files under the media directory), how
  the onion CSP treats imported scripts, and whether an import must be approved by an instance admin.

## 4. Non-goals

- Template overrides or JavaScript in custom themes in phases 1-5 (CT2); they come with §3.10.
- Copying a custom theme between domains (CT5), or a site-wide custom theme library.
- A visual theme builder or colour pickers; a CSS editor beyond a textarea.
- Theme uploads by ordinary users or community moderators.
- Following shipped-theme CSS upgrades into copies (§3.2).

## 5. Constraints

- **Deployment agnostic.** No domain or host is named; tests use the test app's SERVER_NAME and fixture domains.
- **No visible change** for any domain that has no custom theme, including the first domain.
- **Shipped themes byte-identical:** a test checks that no route writes under `app/templates/themes/`, and that every
  shipped theme still lists and renders.
- Project rules: TDD, one commit per task or defect, 100% changed-line and branch coverage, `coverage_floors.ini`
  only rises, invalid client input stays a 400, fuzz targets for the new routes, ruff clean, inline imports only when
  unavoidable, slow tests are bugs.
- Upstream mergeability: additive tables, routes and templates; the edits to upstream files are `current_theme`,
  `theme_list`, `base.html`'s stylesheet tag, the pickers' validation and the federation lines.

## 6. Proposed phases

1. Model, migration, identifier and `current_theme`/`theme_list` changes; stylesheet route; `base.html` tag.
2. Admin Themes page: list, copy (with URL rewriting), rename, edit CSS, retire, delete refused while in use; RBAC
   checks; mod log.
3. File uploads, file route, limits, SVG handling, unlinking.
4. Off-site checks and onion enforcement; federation in/out; API choices.
5. Preview; RBAC acceptance test extension; fuzz targets; docs (`docs/CHANGES-since-v1.8.10.md`).
6. Follow-on, own spec first: import/export and theme code (§3.10).

## 7. Owner questions

Answered 2026-11-18: Q1 no cross-domain copy (CT5), import/export with code as a follow-on (CT6); Q2 limits accepted
(CT8); Q3 a theme in use cannot be deleted (CT7).

Q4 (users' picks can keep a theme in use indefinitely): add the `retired` flag (CT9). No questions open.
