# Paid membership, phase 2: gift codes

Spec: docs/superpowers/specs/2026-11-15-paid-membership-design.md (PM3, PM6 gift kind, PM7, PM8, Q1, Q5).
Branch feat/paid-membership-2, from phase 1.

## Decisions (from the code)

- **G1 One table for every code.** `promo_code`: `code` (unique, stored normalised), `kind` (`gift` now; `percent`
  and `amount` are phase 3, their columns made now so phase 3 needs no second table), `offer_id` (a gift's tier),
  `days`, `percent`, `amount_minor`, `max_uses`, `expires_at`, `active`, `owner_id` (set for a member's referral
  code), `created_by`, `created_at`. `promo_code_use` records each use (code, user, subscription, when), which
  `max_uses` counts.
- **G2 A member's referral code is a promo_code row** (PM7: per code). `generate_code` keeps the member's row in step
  with `User.referral_code` (regenerating changes the row's code and keeps its gift). A new row takes the site's
  **default gift** (settings: tier and days, Q1); admins change any row's gift on the codes page. Members never set
  their own. The referral rules (who may generate, caps, home domain, RC1-RC13) are unchanged: `referrer_for`
  still decides whether a referral code admits.
- **G3 A gift is a subscription of kind `gift`**: `MemberSubscription(provider_kind='gift', offer_id=tier,
  current_period_end=end)`, prepaid-like, so the sweep ends it, the 7-day reminder warns, and lapsing (PM4)
  follows. A `GiftAdapter` (prepaid, never offered at checkout, cancel a no-op) lets the code that looks a row's
  provider up treat it like the others.
- **G4 Where a code is used.**
  - At `/register` on a paid default domain: a working code (a gift code, or a referral code whose row has a gift)
    admits for free and starts the gift; a referral code also records the `Referral`. A referral code whose row has
    no gift admits as in phase 1 (no subscription, so it never lapses).
  - By a member, on `/donations` ("Have a code?"): a gift code adds its period. Not a member's referral code (it is
    for joining).
  - With membership off, codes behave as before phase 2 (referral codes admit; gifts are not started).
- **G5 We never short anybody (PM8).** A gift starts when the member's held period in its group ends (a gift on a
  gift stacks), and a prepaid payment already does (`_prepaid_until` counts the gift's end). For a tier that renews
  by itself, checkout defers the first charge to the held period's end: Stripe `subscription_data.trial_end`,
  PayPal `start_time`. Mocks only, as before.
- **G6 Admin codes page** (Admin -> Payments -> Codes, `manage monetization`): the default gift; make a gift code
  (code text or generated, tier, days, max uses, expiry); list every code with its uses; edit a code's gift;
  deactivate. Members' referral codes are listed there too, each with its gift.
- **G7 Abuse limits.** One use of a given gift code per account; `max_uses` and `expires_at` hold under concurrent
  use (the code's row is locked, as `referrer_for` locks the referrer); a wrong code answers the same whatever the
  reason, as RC3 does.

## Tasks (TDD each)

1. Migration + models: `promo_code`, `promo_code_use`; settings for the default gift; GiftAdapter.
2. Gift core: `start_gift(user, code)` with PM8 stacking, limits (G7), use recording.
3. Referral codes carry gifts (G2): rows kept in step by `generate_code`; default gift.
4. `/register` (G4) and `/donations` redeem (G4).
5. Recurring tiers defer their first charge (G5): Stripe trial_end, PayPal start_time.
6. Admin codes page (G6).
7. Gates: suite, floors, changed-line, cold apps run, browser check.
