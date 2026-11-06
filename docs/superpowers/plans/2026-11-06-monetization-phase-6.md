# Monetization phase 6: PayPal. Implementation plan

**Goal:** Admins connect PayPal for subscriptions and one-off donations.

**Spec:** `docs/superpowers/specs/2026-11-05-monetization-infrastructure-design.md`, M5, C4, C11. Global constraints as in
`docs/superpowers/plans/2026-11-05-monetization-phase-1.md`. Phase 5 (credits) waits: nothing can spend them until perks are decided.

## Not run against PayPal

All of this is against mocks. PayPal's docs confirmed the calls and the webhook check, but **not the fields inside a webhook's payload**,
which are from what PayPal is known to send. A payload of another shape grants nothing, so that's the failure to expect. A sandbox
run needs the owner's sandbox credentials.

## Decisions this plan makes

The owner can overrule any of them.

| # | Decision | Why |
|---|---|---|
| U1 | A subscription starts on its first completed payment, not on approval. | Approval isn't money, and the amount can only be checked against a payment. |
| U2 | An order is captured when the webhook says it was approved. | A browser coming back grants nothing. |
| U3 | A plan with a trial, a setup fee or added tax is refused. | Its first payment wouldn't match its price, so every one would be recorded as a mismatch. |
| U4 | On a paid domain, cancelling reads the next billing date first. PayPal cancels at once, so our record of that date is what's kept to, and its immediate event is ignored until then. | A member keeps what they paid for (S22). |
| U5 | A suspended subscription counts as ended, and coming back means subscribing again. | Nothing is being paid while it lasts. |
| U6 | Only a whole refund or reversal counts. | As for Stripe. |
| U7 | The client ID is sealed with the secret. | The same form treats both alike, and there's no reason to show it. |
