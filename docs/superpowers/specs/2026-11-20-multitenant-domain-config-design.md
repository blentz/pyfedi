# Domains as tenants: one configuration per domain — design

Date: 2026-11-20. Status: approved (owner 2026-11-20). Phase 1 built: 1a (the table, `Site`'s and `site_domain`'s columns, `domain_branding`) and 1b (the per-tenant `settings` keys). Phase 2a (the picker on settings pages, the Site page per domain with Branding folded in) and 2b (Misc per domain with Domain settings folded in; the first domain's row holds every setting's value) built; 2c (the Chooser, Sidebar and Themes pages) next.

## Why

A site answers on several domains (spec 2026-10-19). Until now only the first domain had a full configuration: `Site`
row 1 and the `settings` table. Every other domain had fragments in three shapes, each with its own form, route and
inheritance rule:

- `domain_branding`: name, description, logos, theme, CSS, sidebars, about. Blank inherits `Site`.
- Columns on `site_domain`: `registration`, `registration_offer_id`, `login_required`, `referral_codes`,
  `lapse_grace_days`, `paid_since`. No inheritance; the first domain's equivalents are other `Site` columns and
  settings with a different vocabulary (`Site.registration_mode` against `site_domain.registration`,
  `Site.private_instance` against `site_domain.login_required`).
- Everything else: the first domain's value, whatever domain the request is on.

Reads branch on `is_default` in about 25 modules, and admin has a page per fragment (Branding, Sidebar, Domain settings,
Themes) beside the first domain's (Site, Sidebar, Misc). The owner asked for the data model to be multitenant, for
most settings to be per domain, and for one admin UX in which a domain picker decides which domain a settings page is
for.

## Rulings (owner 2026-11-20)

| # | Ruling |
|---|---|
| MT1 | A domain is a tenant. Each tenant has one typed configuration row, `domain_config`, the first domain included. |
| MT2 | The split is maximal: everything a tenant's visitors or members experience is per tenant. Only what is one thing for the whole server stays instance-wide (table below). |
| MT3 | A non-first tenant's NULL inherits the first tenant's value, so changing the first domain changes every domain that has not set its own. The first tenant's row has no NULLs that mean "inherit". |
| MT4 | One admin UX: a domain picker in the admin navigation chooses the tenant; each settings page edits that tenant. A per-tenant field on another tenant offers "use the first domain's"; an instance-wide field is shown, not editable, with a note that it applies to every domain. |
| MT5 | Built in phases, each its own commit and full green suite, pushed only when the owner asks. |

## Schema

- `site_domain` keeps identity only: `domain`, `scheme`, `federates`, `is_default`, `is_onion`, `instance_id`, keys.
  Its configuration columns move to `domain_config` and are dropped. It reads and writes them by their old names
  through its row (`SiteDomain.config()`), so code and tests that set `row.registration` keep working.
- `domain_config(id, site_domain_id UNIQUE FK site_domain ON DELETE CASCADE, …)` holds every per-tenant column below.
  - The first tenant's row is the one with no `site_domain_id` (a unique partial index allows one), whether or not a
    `site_domain` row names the first domain: local rows with no domain are already the first domain's, and a site
    that never seeded a default `site_domain` row (a fresh install, most tests) still has a first tenant. It is made
    with the old `Site` defaults (`FIRST_DEFAULTS`) the first time it is needed.
  - A sister's row is made the first time it sets something; with none it inherits everything.
  - The storefront columns `site_domain` had (`registration`, `registration_offer_id`, `login_required`,
    `referral_codes`, `lapse_grace_days`, `paid_since`: `OWN_COLUMNS`) are each tenant's own, never inherited, until
    phase 3 unifies registration and login with the first domain's mode and private instance. The default domain's
    are the first tenant's row's.
  - Markdown and its HTML stay paired columns, written together, as today.
- `domain_branding` is dropped: its columns are `domain_config`'s.
- `Site` keeps the instance-wide columns; its per-tenant columns move to the first tenant's `domain_config` row and are
  dropped. `Site` still reads and writes them by name, as the first tenant's (`SITE_TENANT_COLUMNS`), so upstream code
  that loads `Site` row 1 is unchanged. `settings` keeps the instance-wide keys; per-tenant keys become `domain_config` columns.

## What is per tenant (MT2)

| Area | Per tenant (`domain_config`) |
|---|---|
| Profile | name, description (tagline), logos (all sizes, dark), sidebar, about, legal information, ToS URL, source URL, bug report URL, get involved URL, contact email, announcement |
| Look | default theme, additional CSS, additional JS, home and community sidebar boxes, language, default filter, inoculation block |
| Joining | registration mode (one vocabulary: open, application, closed, referral code, paid), application question, paid tier and lapse grace days and paid-since, whether referral codes open signup, referral generators and caps, email verification, captcha, approval email, topic choice and filter selection at signup, auto-decline countries and referrers, login required, NSFW country restriction |
| Content | downvotes, NSFW, NSFL, local image posts, video and audio uploads and their limits, album size, who may create communities, adding remote communities, gif-reply and chan-image and "this" comment filters, meme communities as low quality, read-posts cutoff, public modlog, report emails, em-dash report settings, redirect policy, ban posts |
| Chooser | the instance chooser page and its listing: enabled, main list, elevator pitch, number of admins, financial stability, daily backups, crawl known instances |

Instance-wide (`Site`, `settings`): instance keys, federation mode and lists, allowlist mode, blocked phrases, actors and
bios, hosted inbox hosts, auto-add remote communities, cross-origin delete delay, signed fetch, defederation
subscriptions, remote image caching and its days, onion media cache and limits, onion PoW difficulty, ActivityPub
logging, honeypot, the OAuth admin-scope role, ban check servers, statistics, payment providers and currency, discovery
and relays, emoji filter, admin ids.

A per-tenant content setting that also decides what the server stores (NSFW and NSFL ingestion, uploads) takes, for
what is stored, the most permissive tenant's value, and for what a tenant shows, its own. Each such setting's reader is
named in phase 3.

## Read path

- `app.domain_config.effective(domain)`: the tenant's row over the first tenant's, memoised per domain. Any commit
  that writes a `domain_config` or `site_domain` row clears it (and the domain list) itself, so no writer has to
  remember to. Replaces `app.branding._effective`.
- `g.site` becomes a `SiteView`: the request's tenant's configuration over `Site`'s instance-wide columns, with the
  same attribute names and `Site`'s methods, so upstream code reading `g.site.<name>` reads its tenant's value without change.
- `get_setting(name)` for a key that moved reads the request's tenant; outside a request, the first tenant's.
- Code that branches on `is_default` to choose between `Site` and a sister's fragment reads the effective
  configuration instead.

## Phases

1. Schema and read path, no change in what any page shows except that a sister domain's `g.site` fields that were the
   first domain's now come from its own configuration (which, after the migration, inherits them). Migration with a
   tested downgrade. Admin forms keep their pages but write `domain_config`.
2. The admin UX (MT4): the picker in the navigation, Site, Misc, Sidebar, Chooser and Themes pages for the picked
   tenant, field-level inheritance and instance-wide notes. The Branding and Domain settings pages fold into these and
   their URLs redirect.
3. The newly per-tenant settings are read per tenant (the Content and Chooser rows), with the storage rule above, and
   the remaining `is_default` branches about configuration are removed.

## Upstream

Upstream adds `Site` columns and reads `g.site.<name>`. Reads keep working (`g.site`). A new upstream `Site` column is
instance-wide until it is classified; a merge that changes a moved column needs its migration rewritten onto
`domain_config`.
