# Monetization phase 4: BTCPay Server. Implementation plan

**Goal:** Admins connect their own BTCPay Server and take crypto for donations and prepaid periods, on onion domains too.

**Spec:** `docs/superpowers/specs/2026-11-05-monetization-infrastructure-design.md`, M4, C3, C4, C11. Global constraints as in
`docs/superpowers/plans/2026-11-05-monetization-phase-1.md`.

## Checked on a real server

Unlike Stripe, this adapter was run against BTCPay Server on regtest, on `remote-gpu`: `compose.btcpay-regtest.yaml` and
`tests/test_payments_btcpay_regtest.py`. What that showed, which the docs left open:

- `checkoutLink` names whichever host the API was asked on, so the adapter builds `{address}/i/{id}` itself.
- A redelivered webhook has a new `deliveryId` and the same `originalDeliveryId`, so that's the event's identity.
- The webhook carries no amount, so the invoice is fetched.
- BTCPay refuses to deliver a webhook to a private address unless told otherwise, so the live test signs what the server
  stored and posts it itself. **The signature format comes from the docs.** Everything else is the server's own behaviour.

## Decisions this plan makes

The owner can overrule any of them.

| # | Decision | Why |
|---|---|---|
| T1 | A month is 30 days and a year 365. | One payment buys a fixed length, which is simple to state and to check. |
| T2 | Time left carries over on renewing or on moving to another plan in the group. | Nobody should lose what they paid for by paying early. |
| T3 | Each payment makes a new subscription row, replacing the last in its group. | Reuses M7's rule, and each row names the invoice that paid for it. |
| T4 | **No reminder before a period runs out.** The date is on `/donations`, and a paid domain's grace period follows. | A reminder is a feature of its own, for the owner to ask for. |
| T5 | Only a settled invoice grants anything. An invoice marked invalid later is recorded as refunded and ends its subscription. | BTCPay has no refund event of its own, as refunds are made by hand there. |
| T6 | An expired period is noticed by the daily task. | Same as other housekeeping, so access can run up to a day over. |
| T7 | The API key needs only permission to create and view invoices. | The least that works. |
| T8 | The address is an admin's, so requests skip the site's pinned client, which refuses private addresses. | An operator's server is often on a private network. An admin learns only yes or no about it. |
| T9 | On an onion domain, a member sees their prepaid periods and can pay, and sees no card or payment history. | C11 and the onion's existing rule. |
