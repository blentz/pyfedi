# Complimentary membership

Owner, 2026-11-15: "there should be a default free subscription with no end date for all accounts that don't currently
have one." Rulings, in the owner's words: no public tier, just a **hidden tier used by admins to give a permanent 100%
discount**; **every local account without one** (now, and new accounts made without one); **decline to accept payment
for accounts with 100% discount and/or with no subscription end date**.

## Decisions

- **K1 A hidden offer and a provider of its own.** One `Offer` (title "Complimentary", provider `complimentary`,
  group `complimentary`, price 0, inactive so never offered, no donor label), made by the migration. A grant is a
  `MemberSubscription` of kind `complimentary`, active, with no end date. `ComplimentaryAdapter` is never offered and
  has no webhook; a member cannot cancel it.
- **K2 Who gets it.** The migration grants it to every local account (not deleted, not pending) holding no active
  subscription. After that, `finalize_user_setup`, which every way of making an account ends in, grants it to a new
  local account that holds none then: an admin-made account, OAuth, LDAP, a referral code with no gift, a sign-up while
  membership is off. A paid or gift sign-up holds one already, so gets none.
- **K3 Not on lapsing.** A member whose paid or gift subscription runs out does not get it: that is what locking (PM4)
  is for. Only admins grant it afterwards.
- **K4 It never lapses.** `refresh_access` treats an active complimentary subscription as access, whatever the group.
- **K5 No payment.** A complimentary member's checkout is declined (/donations, the shortcode buttons, codes): the
  page says the membership is complimentary instead of offering anything to buy.
- **K6 Admins grant and revoke it** on the account's edit page (`manage monetization`). Revoking ends it; the account
  then follows the membership rules (an account made before membership began stays free for good, PM5).
- **K7 Seen on /admin/users** as "Complimentary · since <date>", counted as subscribed now.
- **K8 No import cycle.** `app/complimentary.py` needs only the models, so `app.utils` can call it (anything under
  `app.payments` imports its routes, which import `app.utils`).
