# Roles carried by subscription tiers

Owner, 2026-11-16: "we need to be able to assign roles to subscription tiers."

## Rulings (owner, 2026-11-16)

- **T1** Any role except the built-in Admin may be carried by a tier. A role with permissions makes its holders
  privileged (RB6: a domain-scoped holder can no longer act on them); the tier page says so.
- **T2** A subscriber holds the role while they have access: through a cancelled-but-paid-up period and the lapse grace
  days; it goes when the account locks (lapses) or the subscription ends.
- **T3** Gifted periods and complimentary membership carry their tier's roles too (complimentary: every holder gets them).
- **T4** Holders of `manage monetization` set a tier's roles, not Admin alone (a change to RB5).
- **T5** (security) A non-admin may attach or remove only a role whose every permission they hold themselves, on at
  least the same domains. Otherwise a monetization manager could give a tier a powerful role and a gift code for it to
  themselves.

## Decisions (mine, from the rulings)

- **D1** Only subscription offers (kind `subscription`) and the hidden complimentary offer carry roles; one-offs and
  links don't. A tier may carry several roles (table `offer_role`).
- **D2** A grant made by a tier is marked `role_grant.via_tier`. One per user and role (the existing unique index): a
  role the member already holds from an admin is left alone and never ended by a tier. An admin granting a role the
  member holds through a tier makes it their own grant (no longer `via_tier`).
- **D3** `refresh_tier_roles(user)` runs wherever `refresh_access` does (provider events, the daily prepaid sweep,
  cancel, gifts, complimentary given or ended). Wanted = the roles of the offers of the member's active subscriptions.
  A wanted role is granted (or its `via_tier` grant's expiry cleared). A `via_tier` grant no longer wanted gets
  `expires_at` = `access_lapsed_at` + the home domain's grace where the account lapses there, else now; an expiry already
  set is kept. The checks ignore an expired grant; `remove_expired_grants` deletes it with a modlog row. So T2 holds
  without a new timer, and PM5 (role holders never lock) agrees: the grant ends the moment the lock begins.
- **D4** Changing a tier's roles refreshes every member holding that tier at once.
- **D5** A `via_tier` grant can't be revoked or have its expiry changed on the grants pages: the page says it is held
  through a subscription. Remove the role from the tier, or end the subscription.
- **D6** Automatic grants are logged like expiries: a modlog row naming no admin.
- **D7** The UI: a "Roles it gives" checkbox list on a subscription offer's edit page; one for complimentary membership on
  Admin -> Payments; the user's grants list says "through a subscription".

## After the security reviews (owner, 2026-11-16)

- **T6** A tier carrying a role a monetization manager could not attach is Admin's alone, wherever it could be handed
  out or repriced: editing or deleting the offer, gift codes for it, discount codes reaching it (one for any offer
  too), the referral default (automatic may pick any tier) and a member's referral benefit, and complimentary
  membership when that carries such a role. Only an admin's grant with no end counts toward what a manager holds.
  A tier gaining a role turns off gift and discount codes reaching it whose maker may not manage it now.
- **T7** Referral benefits already pointing at a tier when an admin gives it a role with permissions are left as they
  are, and the automatic default may fall onto such a tier: the admin giving the role checks the referral settings.
