# Custom themes, phase 6: import, export and theme code: design

Status: APPROVED 2026-11-18 (TC1-TC4 below; CT1-CT9 of the phase 1-5 spec still hold, except where TC rulings extend
CT2).

Date: 2026-11-18. Builds on `2026-11-18-custom-themes-design.md` (its §3.10 outlined this phase) and RBAC
(`2026-11-03-rbac-design.md`).

**Terms** as in the phase 1-5 spec, plus:
- *Theme code* is a custom theme's Jinja template overrides and its `scripts.js`.
- A *code theme* is a custom theme that has theme code.
- An *archive* is a theme exported to, or imported from, one zip file.

## 1. Intent

A domain's theme holders can make a theme look different, but not lay out pages differently: templates and scripts
come from the shipped base theme (CT2). Admins who can be trusted with the server want to ship a theme with its own
templates and scripts, and to move a theme between domains or instances. This phase adds both, behind a permission
that says plainly what it is worth.

## 2. Owner rulings (2026-11-18)

| # | Ruling |
|---|---|
| TC1 | **No second approval.** Holding `manage theme code` on a domain is the approval. An import with code goes live when it is imported; every import and code edit is mod-logged. |
| TC2 | **Templates are stored in the database**, read by a database-backed Jinja loader. Nothing is written under the code tree or the media directory for them. |
| TC3 | **An override may replace any template the app has**, by its name, not only those shipped themes override today. |
| TC4 | **Code themes work on onion domains**, under the onion CSP as it stands (scripts and connections same-origin only) and the onion CSS stripping. |

From the phase 1-5 spec (CT6, §3.10), unchanged: a new permission, never implied by `edit domain branding`; any theme
holder may export; an archive without code may be imported by any theme holder; only holders of the new permission
add, change or remove code; a copy keeps the code.

## 3. Design

### 3.1 The permission

`manage theme code`, in the `domains` group, scopable (a grant may carry it on one domain), not in Staff. Its role
editor description: "Import and edit theme templates and scripts. Templates run on the server with all of its
secrets, and scripts run in every visitor's browser, admins included: on any domain, this is as much as full
instance admin." The built-in Admin role and user 1 hold it as they hold everything.

It is checked on the theme's domain: `user_access('manage theme code', user_id, on=domain)`. Holding it does not
make someone a theme holder: importing, exporting and editing still need `edit domain branding` (or, on the first
domain, `change instance settings`), as in phase 1-5 §3.6. Code actions need both.

### 3.2 What a code theme adds

- **Template overrides:** `custom_theme_template` (`id`, `custom_theme_id` FK cascade, `name`, `source` text,
  `updated_at`), unique on (`custom_theme_id`, `name`). `name` is an app template name such as `index.html` or
  `post/_post_teaser.html` (TC3).
- **Script:** `custom_theme.script`, nullable text: the theme's `scripts.js`.
- Limits (config, defaults): `CUSTOM_THEME_MAX_TEMPLATES` 100, `CUSTOM_THEME_MAX_TEMPLATE_BYTES` 256 KB each,
  `CUSTOM_THEME_MAX_SCRIPT_BYTES` 512 KB, `CUSTOM_THEME_MAX_ARCHIVE_BYTES` 12 MB (the zip as uploaded).

A template name is accepted only if the app's own loader knows it (`app.jinja_env.list_templates()`), it does not
start with `themes/`, and it ends in `.html`, `.xml` or `.txt`. Its source must be UTF-8 and parse
(`jinja_env.parse`); a template that does not parse is refused with the parser's message and line.

### 3.3 Rendering

`render_template` picks the template, in this order, for the theme in effect (phase 1-5 §3.4):
1. the custom theme's override of that name, if the theme in effect is a custom theme that counts here;
2. else its base theme's shipped override (as now, through `template_theme()`);
3. else the app's template.

Overrides are loaded through a database loader registered on the app's Jinja loader under the prefix
`custom_theme/<id>/<version>/`, where `<version>` is the theme's `updated_at` in microseconds (as the stylesheet's).
A changed theme gets new template names, so no worker can serve a stale compiled template, and old entries age out of
Jinja's cache.

Inside an override, `{% extends %}`, `{% include %}` and `{% import %}` of a template name resolve the same way:
the same theme's override if it has one, else the base theme's, else the app's. (An environment `join_path` that maps
names from a `custom_theme/…` parent; names from any other parent are untouched.)

**A broken override does not take the domain down.** If rendering an override raises, the request renders with
step 2/3 instead, the exception is logged with the theme id and template name, and the theme holders see a flash on
the theme's admin page the next time they open it ("Template X failed: …"). A preview (phase 1-5 §3.5) is the way to
try a code theme before it is the default.

**Script.** `base.html` includes, with the page's nonce, the custom theme's script when it has one
(`/theme/<id>/scripts.js?v=<version>`, served like the stylesheet: long-cached with a matching `v`, open through the
login gate, `application/javascript`, nosniff), else the base theme's `scripts.js` as now. Never both.

A community's theme still changes only the stylesheet on community pages, as for shipped themes: a community's code
theme contributes its CSS there, not its templates or script.

**Onion (TC4).** Templates render through the same response pipeline as every page, so onion link rewriting and
scrubbing apply. The script is same-origin, which the onion policy already allows; it cannot load or connect
off-site there. The stylesheet keeps its onion stripping.

### 3.4 Export

Any theme holder of the domain can export one of its custom themes: `POST /admin/domains/<id>/themes/<tid>/export`
answers `<slug>.piefed-theme.zip`, `Content-Disposition: attachment`, containing:

```
manifest.json      {"format": 1, "name": "...", "base_theme": "...", "exported_at": "...", "software": "piefed"}
styles.css
files/<filename>   each uploaded file, under the name the CSS uses
templates/<name>   each override (e.g. templates/post/_post_teaser.html)
scripts.js         if it has one
```

Export reads the database and the stored files; it changes nothing and is not mod-logged. A stored file that has gone
missing is left out and named in a flash, not a 500.

### 3.5 Import

`POST /admin/domains/<id>/themes/import` takes one zip upload. Any theme holder may import an archive without
`templates/` or `scripts.js`; an archive with either needs `manage theme code` on that domain as well (403 otherwise,
with the reason). The import is all or nothing: every check below runs before anything is stored.

**Archive checks** (each failure is a 400 naming the entry and the reason):
- the upload is at most `CUSTOM_THEME_MAX_ARCHIVE_BYTES` and is a zip;
- every entry name is relative, uses `/`, has no `..`, `.`, empty or absolute part and no backslash; no two entries
  have the same name; directory entries are ignored; symlinks (Unix mode bits), encrypted entries and any compression
  other than stored or deflate are refused;
- only `manifest.json`, `styles.css`, `scripts.js`, `files/<name>` and `templates/<name>` are allowed;
- sizes are counted while decompressing, never taken from the zip headers: each entry stops at its own limit and the
  whole archive at the sum of the theme limits (files, stylesheet, templates, script), so a zip bomb fails early;
- `manifest.json` is a JSON object with `format` 1 (a higher format is refused: "made by a newer version"), a `name`
  and a `base_theme`.

**Content checks:** the name follows phase 1-5 §3.2 (a taken name gets the copy suffix, as a copy does); the base
theme must ship here (else 400: "needs base theme X, which this site does not have"); `styles.css` and the files go
through phase 1-5 §3.7-3.8 exactly as an upload does (types sniffed, limits, onion stripping on the onion); templates
through §3.2 here.

The imported theme is a new custom theme of that domain, not retired, last changed by the importer. Mod log
`import_theme`, with the archive's name and whether it had code.

### 3.6 Editing code

On a code theme's edit page, holders of `manage theme code` see:
- a list of its overrides, each with Edit and Remove; an "Add an override" form (template name, chosen from the app's
  template names, and source);
- the script in a textarea, with Remove.

Saving runs the §3.2 checks. Mod log: `edit_theme_template`, `remove_theme_template`, `edit_theme_script`,
`remove_theme_script`, each naming the template where there is one. Every code change bumps the theme's `updated_at`.

Theme holders without the permission see the list of overrides and whether there is a script, read-only, with the
note "Changing theme code needs the manage theme code permission." They can still rename, retire, edit CSS and files,
copy (the copy keeps the code), export and delete.

The shipped-theme view (phase 1-5 §3.5) does not change: shipped themes stay immutable (CT1).

### 3.7 Copy, retire, delete, preview

As phase 1-5. A copy of a code theme copies its overrides and script. Deleting a theme deletes its overrides (cascade).
Preview renders the theme's overrides and script for the previewing holder alone.

## 4. Non-goals

- A template editor beyond a textarea, syntax highlighting, or a diff against the app's template.
- Sandboxing theme templates (`SandboxedEnvironment`): the permission is admin-equivalent by design (§3.1).
- Following app template changes into overrides: an override of a template the app later changes is the theme
  author's to update. An override whose template name the app no longer has is kept, shown as "unused", and never
  rendered.
- Importing shipped themes, or exporting them (copy first, then export the copy).
- Signing or verifying archives.

## 5. Constraints

- Deployment agnostic; no visible change for a domain without a code theme.
- Shipped themes byte-identical (CT1); nothing written under `app/templates/`.
- Project rules: TDD, one commit per task, 100% changed-line and branch coverage, floors only rise, invalid client
  input is a 400, fuzz seeds for the new routes, ruff clean.
- Upstream mergeability: additive tables, routes and templates; the upstream edits are `render_template`'s template
  choice, the Jinja loader registration and `base.html`'s script tag.
