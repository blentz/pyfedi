# Bitcoin's daily price, and ending at a month's end

Owner, 2026-11-15: "fix btcpay prices as the price of the currency at midnight each day. the fixed price remains for 24
hours. a purchase at exactly midnight receives yesterday's price. refunds and cancellations should end the subscription
at the end of the last paid month or the current month." Rulings: midnight is **UTC**; when the rate can't be read,
**keep yesterday's price** and try again hourly; months are **subscription months** (counted from the subscription's
start); the end rule covers **all providers**.

## Decisions

- **B1 A day's rate.** `btc_daily_rate(day, currency, rate, fixed_at)`: the price of one bitcoin in the site's currency,
  read from the BTCPay store's own rate source (Greenfield `GET /stores/{id}/rates?currencyPair=BTC_USD`, which needs
  the key permission `btcpay.store.canviewstoresettings`). An hourly job (`fix_btc_price`, timer at minute 0 + 5s) fixes
  today's rate if it isn't fixed yet: at midnight normally, later only after an outage.
- **B2 Which rate a checkout takes.** Day D's rate holds from just after D 00:00 UTC to D+1 00:00 UTC inclusive: the
  rate fixed latest with `D 00:00 < now`. So a checkout at exactly midnight takes yesterday's, and so does one before
  today's is fixed (an outage). With no rate ever fixed (a new site), the rate is read and fixed for today at once; if
  that fails, BTCPay checkout says the provider is unavailable.
- **B3 Invoiced in bitcoin.** The invoice is for the offer's price (after a discount code) in sats at that rate,
  rounded down to the sat (the member's way), in currency `BTC`. Each invoice made is recorded
  (`btcpay_invoice`: invoice id, offer, account, fiat price expected, sats, rate's day), so the settlement is checked
  against what this site asked, not against the invoice's metadata, which a public invoice can carry too.
- **B4 Settled.** An invoice this site recorded: paid in full in BTC at its sats is the expected fiat price (so core,
  discounts and the ledger keep working in the site's currency); anything else is a mismatch. An invoice not recorded
  (one made before this release, priced in the site's currency) is read as before.
- **B5 Shown.** /donations shows a BTCPay offer's price in bitcoin for today beside its price.
- **B6 Ending at a month's end (all providers).** A member cancelling, or a provider's refund of a subscription's
  payment, ends that subscription at the end of its current subscription month (months from its start, day clamped
  to the month's length), or when what was paid for runs out if that is sooner. The provider is told to stop charging
  at once; access runs to that end, then the daily sweep ends it and lapsing (PM4) follows. This replaces running a
  cancelled plan to the provider's period end (S22) and ending a regular donation at once. A prepaid (BTCPay) period
  can now be cancelled too. Gifts and complimentary membership are not payments and are not cancelled by members.
- **B7 No reminder to pay again** for a cancelled period.

## Owner to-do

- Add `btcpay.store.canviewstoresettings` to the BTCPay API key (Account -> API keys) and re-save it on Admin ->
  Payments, which now checks it.
- The hourly timer `pyfedi-job@fix_btc_price.timer` is installed with the deploy.
