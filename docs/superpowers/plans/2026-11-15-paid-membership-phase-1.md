# Paid membership, phase 1: pay to join the default domain

Spec: docs/superpowers/specs/2026-11-15-paid-membership-design.md (PM1-PM8). Branch feat/paid-membership-1.

## Decisions (made while planning, from the code)

- **M1 The switch lives on the default domain's own row.** `site_domain.registration = 'paid'` on the default row
  turns membership on; `registration_offer_id` names the tier group (and the tier preselected); `lapse_grace_days`
  is its grace (Q4: the admin sets 3). `paid_registration()` honours it for the default domain too. Everything that
  already keys on `paid_registration` (lapsing, the directory and LDAP refusals, the donations page's wording) then
  applies unchanged. Edited in the site's registration settings, since the domain settings page excludes the first
  domain (S4, S24).
- **M2 Grandfathering is a date.** `site_domain.paid_since` (new) is stamped when a domain first turns paid; an
  account created before it never lapses (PM5), even if it later subscribes and stops. NULL (every existing paid
  domain) keeps today's rule. Anyone holding a role (staff, custom roles) is exempt, like admins (PM5).
- **M3 Any tier in the group (PM1, PM2).** `/register` on a paid domain lists every active subscription offer usable
  here in the registration offer's group, preselecting `?offer=N` or the domain's offer. The choice is kept on the
  pending account (`user.pending_offer_id`, new) so logging in again resumes that checkout.
- **M4 Codes beside paying, on the default domain only.** In referral-code mode a code is optional: a working one
  makes a free account as today (gifts are phase 2); none means paying; a wrong one is refused, never charged.
  A sister paid domain keeps ignoring codes (S10).
- **M5 Other ways in stay closed** on a paid default domain, as on a paid sister domain: OAuth signup, the directory
  and LDAP. The API's registration follows the web form: a code admits, otherwise it is closed (paying needs the
  provider's page).
- **M6 Join, not Log in.** For a visitor on a paid domain, a `[checkout N]` shortcode or /donations offer in the
  registration group links to `/register?offer=N` ("Join"); anything else keeps "Log in to subscribe".
- **M7 PM8 for prepaid already holds** (`_prepaid_until` carries time left in the group over). Deferring a renewing
  tier's first charge to a held period's end is phase 2, where gifts first make that possible.

## Tasks (TDD each: failing test, code, pass, commit)

1. Migration: `site_domain.paid_since`, `user.pending_offer_id` (FK offer, ON DELETE SET NULL); DomainInfo carries
   `paid_since`.
2. `paid_registration` honours the default row; `access_has_lapsed` grandfathers by `paid_since` and exempts role
   holders.
3. Site registration settings: Membership (on/off, tier group offer, grace days); turning on stamps `paid_since`.
4. `/register`: tier choice, `?offer=`, pending_offer_id, checkout of the chosen tier; code-or-pay on the default
   domain in referral mode.
5. Other ways in: OAuth signup and API registration closed without a code on a paid default domain.
6. Join links in the checkout shortcode and /donations.
7. Gates: full suite, floors, changed-line, one cold apps run; browser check of join-and-pay on a test stack with
   BTCPay regtest if reachable, else mocked.
