# Monetization phase 2: link-outs and guest donations. Implementation plan

**Goal:** Admins manage donation links at Admin > Payments, shown at `/donations` to anybody who may browse the site. A
logged-out visitor can make a one-off donation through an integrated provider, with no account attached.

**Spec:** `docs/superpowers/specs/2026-11-05-monetization-infrastructure-design.md`, M5 (link-outs), M8, C11, C12. Global
constraints as in `docs/superpowers/plans/2026-11-05-monetization-phase-1.md`.

## Decisions this plan makes

The owner can overrule any of them.

| # | Decision | Why |
|---|---|---|
| Q1 | A link-out is an `Offer` with `provider_kind='link'`, `kind='link'` and a new `url` column. Only `http` and `https` are accepted. | The spec's data model says so, and domains, order and active state then work as for any offer. |
| Q2 | Links are offered on the onion unless an admin limits them by domain. Nothing else is, until BTCPay. | A link is only a link: it tells a provider nothing about the onion. Every link carries `rel="noopener noreferrer nofollow"`, so no `Referer` names the host. |
| Q3 | On a private instance, `/donations` asks a logged-out visitor to log in, like any other page. | "Require login to browse" has no exception for money, so guests can't give there except through the CMS page's own links. |
| Q4 | A guest checkout carries no CSRF token, and is rate limited per address. | A logged-out visitor has no session cookie to bind a token to. A forged one acts for no account and only creates an unpaid Stripe session, so the risk that remains is volume. A member's checkout still needs its token. |
| Q5 | A subscription offer shows a guest a "Log in to subscribe" link. | M8: subscriptions need an account. |
| Q6 | A failed checkout explains itself on a page with status 503, for everyone. | A guest's flash would not survive a redirect. |

## Tasks

1. **Link-outs:** the `url` column and migration, `offers_here` and the menu counting links and applying Q2, the admin's add
   and edit form, and the links on `/donations`.
2. **Guest donations:** the page opens to anybody who may browse, guest checkout (Q4, Q6), the adapter without a user, the
   return page without a login.
3. **Docs and gates:** `INSTALL.md`, changes, spec status, the full gates, a browser check.
