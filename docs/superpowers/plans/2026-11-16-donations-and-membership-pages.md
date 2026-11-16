# /donations is for donations; membership is managed in user settings

Owner, 2026-11-16: "the /donations page is not the membership page. there is a /subscribe page that needs the day's BTC
price. the /donations page should match the upstream page." Then: "We are a multi-tenant capable site. So, some domains
may show the /donations page, some domains may show the /subscribe page. /donations is for charitable donations.
/subscribe already exists in [the live site]'s CMS and is for the first domain's subscriptions." Rulings: membership managed in
user settings (DM3); /donations lists donations only; keep upstream's "Support PieFed development" section; the BTC price both
beside each BTCPay button and as a `[btcprice N]` shortcode.

## Decisions

- **DM1 What a tier is.** A subscription offer in the group of any domain's membership offer
  (`site_domain.registration_offer_id`). Everything else on offer (one-offs, regular donations, link-outs) is a
  donation. Gifts and complimentary membership are neither.
- **DM2 /donations as upstream.** "Donate to <site>", the intro and "Please choose from one of the plans below" while
  the member gives nothing regularly, a "Manage existing donation plan" card per regular donation (cancel, card
  details), a card per donation offer, "Prices are in <currency>", and upstream's "Support PieFed development" section
  with its Patreon and Liberapay buttons. No tiers, codes, membership status, payment history or BTC price there.
  Complimentary membership still declines payment (K5): its holder is told so instead of offered plans.
- **DM3 Settings -> Subscription.** Owner, later the same day: "users should manage their subscription from the user
  settings." (This replaces a `[membership]` shortcode, never deployed.) `/user/settings/subscription`, a settings tab
  shown wherever some domain has membership tiers or the member has held a subscription or paid: each subscription
  that is not a regular donation (tiers, gifts, complimentary) with its date, Cancel where it can be cancelled (B6), "See
  the plans", a held discount code with Clear, "Have a code?", and their payments. Its forms come back to it.
- **DM4 Where members are sent.** A lapsed member, and the "Your current plan" and "Complimentary" buttons, go to
  Settings -> Subscription. "See the plans" and the prepaid reminder mail go to the plans page: the first CMS page with
  a `[checkout N]` button in it (the first domain's /subscribe), else /donations.
- **DM5 BTC price.** Each BTCPay `[checkout N]` button shows "<amount> BTC today" beside it; `[btcprice N]` writes the
  amount alone wherever it is put (nothing when the offer isn't BTCPay's or no price is fixed yet).
