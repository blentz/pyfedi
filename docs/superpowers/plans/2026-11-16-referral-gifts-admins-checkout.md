# Referral codes always give something; admins are complimentary; checkout shows its price

Owner, 2026-11-16: "make referral codes require linking to a gift. nulls aren't allowed; admin account gets
complimentary sub. make sure invoice pricing is clear, including a notice of lapsed discounts with dates." Rulings:
**gift or discount, never nothing**, each code storing its own; **every admin, kept so**; **a summary page, then pay**.

## Decisions

- **X1 A referral code always gives a gift or a discount** (replaces plan 2026-11-16-referral-benefits.md R1, R3's
  "nothing" and "the default"). There is no "Nothing" choice, for a code or for the default. A code stores its own
  benefit, set from the default when it is made (automatic: one billing period of the cheapest tier on its owner's
  domain), and kept until an admin changes it; changing the default changes codes made after. A code whose tier or
  discount code is gone, or that was made before this (no benefit stored), takes the default then, and keeps it.
- **X2 Where nothing can be given, nothing admits.** On a domain that charges, a referral code that can give no gift
  or discount (its owner's domain offers no tier, and the default is not a discount) refuses the account, as one past
  its limits does (R6). On a domain that doesn't charge, a referral code gives nothing whatever it stores, as before.
- **X3 Every admin holds complimentary membership** (complimentary plan K6 amended): user 1 and every holder of the
  built-in Admin role, granted now (migration) to any without it, and when the Admin role is granted. An admin's
  complimentary membership cannot be ended on the edit page.
- **X4 A summary before paying.** A checkout (a member's, or a new account's at registration) makes the provider's
  checkout as now, then shows a summary whose Pay button goes there: the offer; what it covers (a prepaid period
  from-to, or a subscription's first charge date and how often it renews; a one-off: once); the price, any discount
  (its code and what it takes off) and the total; for Bitcoin, the amount in BTC and the day's rate with its date.
- **X5 A lapsed discount is told, with dates.** A held code (a member's, or one given at registration by a code or a
  referral code) that can no longer be used is named on the summary with why: expired on its last day (the date), used
  up (the date of its last use), switched off, already used by this account (the date), or not for this offer; and
  that the full price applies. It is no longer held. A member's checkout no longer stops for it.
- **X6 The BTCPay invoice** carries the same breakdown as its description.
