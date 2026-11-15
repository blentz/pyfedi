# Paid membership, phase 3: discount codes

Spec: docs/superpowers/specs/2026-11-15-paid-membership-design.md (PM6 percent and amount off, Q2 first payment only,
Q3 no PayPal discounts). Branch feat/paid-membership-3, from phase 2.

## Decisions (from the code)

- **D1 The same code box.** Wherever a code is typed (registration, /donations' "Have a code?"), a gift code starts
  its gift as in phase 2, and a discount code is **held for the next checkout**: on the pending account at
  registration (`user.pending_promo_code_id`), in the session for a member on /donations, which then says what is
  held ("20% off your first payment of Furcas").
- **D2 What a discount is.** Percent off (1-99) or an amount off in the site's currency, less than the price; free is
  what gifts are for. `offer_id` set: that offer only; NULL: any subscription or one-off. First payment only (Q2).
- **D3 Who.** An account: a member, or a pending account paying at registration. A guest's one-off takes no code (no
  account to hold it, nothing to rate-limit it by but the address).
- **D4 Reserved at checkout.** Starting a discounted checkout writes a `promo_code_use` with the offer and the amount
  expected (`offer_id`, `expected_minor`, `payment_id` NULL until paid). It counts towards `max_uses` while it is
  less than a day old or paid, so an abandoned checkout frees its use after a day. One account uses a code once.
- **D5 Paid at the discounted price is no mismatch.** Core's `_paid`: an amount below the offer's price matches when
  the member holds an unpaid reservation for that offer at exactly that amount, which it then marks paid. Anything
  else stays a mismatch, as now.
- **D6 Providers.** BTCPay: the invoice is made for the discounted amount. Stripe: a one-off coupon (`duration=once`,
  percent or amount) on the Checkout Session, for subscriptions and one-offs alike. PayPal: refused (Q3): its offers
  say a code cannot be used with them, and checkout goes ahead at full price only if the member clears the code.
- **D7 Admin.** The codes page makes and edits discount codes too (kind, percent or amount, which offer), and lists
  their uses with what was paid.
- **D8 Limits.** As gifts: the shared code-typing limit, the code's row locked when reserved, the same refusal for
  every reason.

## Tasks (TDD each)

1. Migration: `promo_code_use.offer_id`, `expected_minor`, `payment_id`; `user.pending_promo_code_id`.
2. Discount core: find a discount code; the price after it; reserve; core's match (D5).
3. Providers (D6): BTCPay amount, Stripe coupon, PayPal refusal.
4. Holding a code (D1): registration and /donations; checkout applies and reserves.
5. Admin (D7).
6. Gates: suite, floors, changed-line, cold apps run, browser check.
