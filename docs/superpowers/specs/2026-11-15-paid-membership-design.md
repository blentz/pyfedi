# Paid membership on the default domain: pay to join, gift codes, discount codes

Status: APPROVED 2026-11-15 with the defaults below, Q5 changed (PM8). Phase 1 starts after the subscription-status branch merges.

## Why

The owner's site's default domain runs in referral-code mode, so nobody without a code can make an account, and the
`/subscribe` page's buttons ask a visitor to log in first: they cannot subscribe without an account, and cannot get an
account without a code. The owner wants paying to be the other way in, codes to grant a free time-limited
subscription, and discount codes.

## Owner rulings (2026-11-15)

| # | Ruling |
|---|---|
| PM1 | On the registration page, someone without a code picks any tier, the account is made pending, they pay at the provider, and paying activates it. |
| PM2 | The tiers offered are whatever subscription offers are active and usable here (today the six BTCPay ones; Stripe and PayPal ones appear when set up). |
| PM3 | A code still admits for free, but now as a **free, time-limited subscription** to a tier (e.g. 1 month, 1 year of Furcas), not an open-ended account. |
| PM4 | When a member's subscription (paid or free) runs out, the account is **locked until they pay**: logging in leads only to renewing. |
| PM5 | Accounts made before membership is switched on are **grandfathered, free for good**. Admins and staff are always exempt. |
| PM6 | Discount codes come in three kinds: a **free period of a tier**, **percent off**, **fixed amount off**. |
| PM7 | A referral code's gift is set **per code**: each code carries its own tier and length. |
| PM8 | **We never short anybody.** A payment adds its full length to whatever is held now: paying while on a gift (or any prepaid period) starts the new period when the current one ends; renewals always extend. Q1-Q4 and Q6-Q7 are accepted as proposed. |

## What exists to build on

- **Paid registration** (monetization phase 3, M10, plan S1-S25): `/register` makes a pending account
  (`User.registration_pending_since`), checks out, and `SubscriptionStarted` clears it; unpaid pending accounts are
  removed after two days (`remove_unpaid_registrations`). Today only for a non-default domain with
  `registration = 'paid'` and one `registration_offer_id`.
- **Lapsing** (M12, S19): `User.access_lapsed_at` from `refresh_access`, over the subscriptions in the domain's
  registration group; `access_has_lapsed` after the domain's `lapse_grace_days`; `refuse_lapsed` sends a lapsed member
  to `/donations` only; admins are never lapsed. An account that has held no plan in the group never lapses, which is
  already the grandfathering PM5 asks for.
- **Prepaid periods**: BTCPay subscriptions keep `current_period_end`; the daily sweep ends them; one reminder 7 days
  before.
- **Referral codes** (2026-10-31 spec, RC1-RC13): one per member (`User.referral_code`), plus `Referral` rows.

## Design

### D1 Membership on the default domain

A site setting **Membership required** (off by default) makes the default domain behave as a paid domain for
lapsing: `paid_registration` and `access_has_lapsed` read it for the default domain. Switching it on stamps
`membership_since`; accounts created before it are grandfathered (PM5) because they hold nothing in the group. The
registration group is the one the tier offers share (`tiers` today), chosen in the setting.

### D2 Registration with a tier (PM1, PM2)

`/register` on a domain with membership shows the usual fields plus a **tier choice** (radio list of the active
subscription offers in the membership group, with price and period) and an optional **code** field. With a code that
works, the code's gift applies (D3) and no payment is taken. Without one, the chosen tier is remembered on the
pending account (`User.registration_offer_id`, new) so logging in again within two days resumes that checkout rather
than the domain's single offer. The `/subscribe` page's buttons for a visitor become **Join** links to `/register`
with the tier preselected (`?offer=N`), instead of "Log in to subscribe".

### D3 Codes and gifts (PM3, PM6, PM7)

One table, `promo_code` (new): `code`, `kind` (`gift`, `percent`, `amount`), `offer_id` (the tier a gift grants, or
the offers a discount applies to: NULL for all), `days` (a gift's length), `percent` / `amount_minor`, `max_uses`,
`expires_at`, `active`, `created_by`. A member's referral code (`User.referral_code`) is linked to a `promo_code` row
of kind `gift`; using it still records a `Referral`.

- **Gift**: makes a `MemberSubscription` with provider `gift`, `current_period_end = now + days`, no charge. It
  lapses like a prepaid one (sweep, reminder, then PM4), and the member renews by paying for a tier.
- **Percent / amount off**: applied at checkout. BTCPay: the invoice amount is reduced. Stripe: a one-off coupon
  attached to the Checkout Session. PayPal: see open question Q3.
- Each use is recorded (`promo_code_use`: code, user, payment or subscription), so `max_uses` holds and the ledger
  shows what was discounted. A discounted payment is not a mismatch: core checks it against the offer's price less
  the discount.
- **Where a code is taken** (owner 2026-11-20): an admin's gift or discount code admits at signup on any domain where
  joining is paid for and that sells the tier it gives or takes money off (`sold_on`: one of the domain's membership
  tiers, `tiers_on`); a discount for any offer, on every such domain. Elsewhere it is refused ("That code cannot be
  used on this site"). This holds on the default domain too, which took any gift before. A member's referral code is
  still taken on the default domain only (G4). The link to join with a code made for a member (their referral page)
  names the domain they are viewing it on when the code admits there, else the first that it admits to, the default
  domain first (`join_domain`); none where it admits nowhere.
- **Deleting a gift code** (owner 2026-11-19): an admin (Admin → Payments → Codes, or the code's own page) and the
  member an admin made it for (their referral page) can delete a gift code. It stops working at once and leaves every
  list. Whoever used it keeps the period it gave: a gift subscription names no code. Its `promo_code_use` rows go with
  it. A member's referral code is not deleted this way (it is regenerated), nor is a discount code, whose uses hold
  checkouts in progress.

### D4 Locking (PM4)

Reuses `refuse_lapsed`, with the lapsed page moved from `/donations` to a renew page listing the tiers. API clients
get `access_lapsed` as today.

## Open questions (with proposed defaults)

| # | Question | Proposed default |
|---|---|---|
| Q1 | Who sets a member's referral-code gift (PM7)? | Admins only: a new code gets the site's **default gift** (setting, e.g. 30 days of the cheapest tier); an admin can change any code's tier and length in /admin. Members cannot raise their own. |
| Q2 | Does percent / amount off apply to the first payment only, or every renewal? | First payment only. Every renewal works for Stripe (coupon duration `forever`) but not BTCPay (each renewal is a fresh invoice the member starts) without remembering the code per member. |
| Q3 | PayPal discounts | Not offered for PayPal offers (codes say "not valid with PayPal") until a sandbox run shows a plan override works; PayPal has no coupons for subscriptions. |
| Q4 | Grace before locking | 3 days after the period ends (the domain's `lapse_grace_days`, per-domain setting as today). |
| Q5 | Can a member stack a gift and a paid tier? | **Ruled PM8:** the payment's length is added after the gift's end; nothing held is lost. For a tier that renews by itself (Stripe, PayPal), its first charge is deferred to when the held period ends (a trial until then). |
| Q6 | Codes on the onion storefront | Same as clearnet; BTCPay only there (C11). |
| Q7 | Email verification for paid signups | As on paid domains today: the verification email is sent once payment confirms. |

## Phases

1. D1 + D2 + D4: membership switch, pay-to-join with a tier choice, Join buttons, locking. (Unblocks the owner's site.)
2. D3 gifts: promo codes of kind gift, referral codes carrying gifts, admin page for codes.
3. D3 discounts: percent and amount off for BTCPay and Stripe; PayPal per Q3.

Each phase: TDD, full suite + both coverage gates, one cold apps-harness run, then the owner's go-ahead to merge
and deploy.
