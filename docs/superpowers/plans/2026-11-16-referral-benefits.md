# What a member's referral code gives

Owner, 2026-11-16: "admins should be able to select which discounts are associated with each user's referral code when
referral codes are enabled." Clarified: "the admin should be able to configure what the gift/discount is based on the
available subscriptions and discounts for the domain"; "limit one benefit per referral code"; by default "one free
unit of the smallest tier (100% discount for one billing period)"; a discount's limits all still count.

## Decisions

- **R1 One benefit.** A member's referral code gives one of: a gift (a free period of a tier), a discount code, or
  nothing. An admin sets it per code, or leaves the code on the site's default.
- **R2 What can be chosen** comes from the code owner's home domain: its tiers (the subscriptions in its membership
  group on offer there; on a domain without membership, every subscription on offer there), and the discount codes
  that apply to one of them or to any offer (not a member's own code).
- **R3 The default** (Admin -> Payments -> Codes) is one of the same, or **automatic**, which is the start: one billing
  period (a month or a year) of the cheapest tier on the code owner's domain. A code on the default takes it when it is
  used, so changing the default changes every such code.
- **R4 Joining with it** on a paid domain: a gift admits for free and starts the gift (as before, G4); a discount
  admits as a paying account, the discount held for the first payment of the tier chosen, and refused for a tier it
  doesn't apply to; nothing admits as before (G4). The referral is recorded every way.
- **R5 Limits.** A discount used through a referral code is a use of that discount: its max uses, expiry and active
  switch hold, one account uses it once, and it is reserved at checkout (D4). A gift counts against the referral code's
  own limits, as before.
- **R6 Never free by accident** (audit, 2026-11-16). On a paid domain, a referral code whose gift or discount it can no
  longer give (switched off, past its last day, used up, or its last use taken first) refuses the account, as a gift
  code typed at registration does: admitting it without the benefit would make it free for good (complimentary
  membership). A code whose chosen tier or discount code has been deleted falls back to the site default, and a
  default whose choice is gone falls back to automatic. Only an explicit "nothing" admits with nothing.
