# Monetization infrastructure — design

Date: 2026-11-05. Status: approved by the owner 2026-11-05. Phase 1 is built (plan
`docs/superpowers/plans/2026-11-05-monetization-phase-1.md`), and so is phase 2 (plan
`docs/superpowers/plans/2026-11-06-monetization-phase-2.md`). Live Stripe calls are unverified: the owner ruled out a
test-mode run.

## Summary

Admins configure, on a live site, how an instance takes money: link-outs, Stripe, BTCPay Server and PayPal, for
donations, subscriptions and prepaid credits. One provider-neutral ledger records every payment. A domain can charge a
first subscription fee at registration and require login to browse, beside public domains on the same instance.

Follows `2026-11-05-cms-donate-page-design.md`, which moved the Stripe plans to `/donations`.

## Non-goals

- **What payment buys.** Premium features, exclusive communities and other perks are deferred by owner ruling. This
  track gives them something to check ("does this member hold offer X?") and builds none of them. The only benefits
  here are the existing Donor label (M9) and access to a paid domain (M10-M12).
- **Paying other members.** Money goes to the instance's operator only.
- **Holding card details.** Every card payment happens on the provider's own hosted page.
- **Federating anything about a payment.**

## Rulings

Owner rulings, 2026-11-05.

| # | Ruling |
|---|---|
| M1 | Provider credentials are encrypted in the database. Admins enter them in the UI, can replace them, and can never read them back. The encryption key comes from the environment and is dedicated to this purpose. |
| M2 | One set of provider accounts per instance. Each offer is available on all domains or on chosen ones. |
| M3 | Microtransactions are paid from prepaid credits, not by a checkout per purchase. Members top up a balance and spend from it. |
| M4 | The first crypto route is a self-hosted BTCPay Server. A crypto subscription is a prepaid period the member renews. |
| M5 | Providers wanted: link-outs, Stripe managed in admin, crypto, and PayPal or similar. |
| M6 | The ledger stores only account, offer, amount, time, status and provider reference: no names, emails or IP addresses. After an admin-set retention period, settled rows lose the account link and keep the amounts. |
| M7 | A member may hold several subscriptions. Offers belong to groups: choosing a plan replaces any other plan in its group, and plans in different groups run together. |
| M8 | Logged-out visitors may make one-off donations through integrated providers, with no account attached. Everything else needs an account, except M10. |
| M9 | Each offer says whether it grants the Donor label, with an optional length of time for one-off payments. Existing Stripe plans grant it. |
| M10 | A domain's registration can be `paid`. Registration collects the first subscription fee. The account is created pending, cannot log in, and is activated by the payment confirmation. Unpaid accounts are removed after a set time. |
| M11 | "Require login to browse" becomes a per-domain setting, so a paid private domain can sit beside public ones. |
| M12 | When the subscription of an account on a paid domain ends, after an admin-set grace period the member can only log in to renew or to export their data. Nothing is deleted. Members elsewhere are unaffected. |

## Defaults accepted by the owner

Proposed by Claude, accepted 2026-11-05.

| # | Default | Why |
|---|---|---|
| C1 | Built into core with one adapter per provider, not the plugin system. | Plugins offer only event hooks, and referral codes (RC1) went the same way. |
| C2 | New instance-wide permission `manage monetization`, covering credentials, offers and the ledger. Not domain-scoped. | Per M2 the accounts are instance-wide, and payment history is sensitive. |
| C3 | One currency per instance, set once. Amounts are integer minor units. BTCPay prices invoices in that currency. | Multi-currency adds exchange-rate and accounting questions nobody has asked for. |
| C4 | If the encryption key is unset or wrong, integrated providers are off and admins are told. The site keeps running, and link-outs keep working. | A payments mistake must not take the site down. |
| C5 | While Stripe has no stored credentials, it reads the existing environment variables. The admin page offers a one-click import. The environment path is removed one release later. | A running Stripe setup must not break on deploy. M1 rules out keeping env as a permanent source. |
| C6 | Existing subscribers migrate to subscription rows, in a `legacy` group, with no action from them. The `User.stripe_*` columns go after the release that proves it. | Donors must not have to resubscribe. |
| C7 | `/stripe_webhook` keeps answering as an alias of the new webhook route. | Stripe's dashboard has that URL configured on existing sites. |
| C8 | Credits never expire, cannot be cashed out, and are bought only with money. A refund or chargeback removes the credits it bought and can take the balance negative, which blocks spending. | Simplest honest rule. **Check tax and stored-value law in the operator's jurisdiction before enabling credits.** This spec is not legal advice. |
| C9 | A login-to-browse domain must have `federates=False`. | Federated content has left the server, so it cannot be gated. Your own example was access to "content that is not federated". |
| C10 | Paid registration works through the web form only. OAuth signup and API registration are refused on a paid domain, with a message pointing to the web form. | RC5 requires every signup path to be covered. Refusal covers them until there is a reason for more. |
| C11 | Stripe and PayPal are never offered on the onion. BTCPay is offered there only if its own `.onion` address is configured, so that no redirect names a clearnet or onion host to the wrong party. | Extends the existing onion rule. |
| C12 | Guest donation rows are stored with no account from the start (M6, M8). | Nothing to pseudonymise later. |

## Data model (sketch)

- `payment_provider`: kind (`link`, `stripe`, `btcpay`, `paypal`), enabled, encrypted credentials, non-secret settings.
- `offer`: title, kind (`donation`, `subscription`, `credit_pack`), amount and period or provider price id, `group`,
  grants-donor flag and length, position. Link-out offers hold a URL instead.
- `offer_domain`: which domains an offer is on. No rows means all (M2).
- `payment`: the M6 ledger, with a unique `(provider, provider_reference)`.
- `member_subscription`: account, offer, status, current period end, provider reference.
- `credit_entry`: an append-only balance ledger (M3). A balance is the sum of its entries, never a stored number.
- `payment_event`: a unique `(provider, event_id)`, so a repeated webhook does nothing.
- `User`: `donor_until`.
- `SiteDomain`: `registration` gains `paid`, plus `registration_offer_id`, `login_required` and `lapse_grace_days`.

A query helper, `holds_offer(user, offer)`, is what later perks will read. The Donor label reads `User.donor_until`, which
core derives from the same data, because the label appears on every author link in a list.

## Adapter contract

Each integrated provider implements the same small interface: start a checkout, verify and parse a webhook into neutral
events (`paid`, `refunded`, `subscription_renewed`, `subscription_ended`), cancel a subscription, and validate
credentials. Core code handles the ledger, subscriptions, credits and the Donor label from those events and never reads
a provider's payload.

## Security

- **Key handling:** encryption is `MultiFernet` keyed from `PAYMENT_SECRETS_KEY`, so keys can rotate. The key never
  enters the database, backups of it, or logs.
- **Webhooks:** verified by each provider's signature before anything is parsed. Invalid means 400 and no change.
  Activation (M10) comes only from a verified webhook, never from the return URL a browser visits.
- **Amounts:** a webhook's amount and currency are checked against the offer. A mismatch is recorded and grants nothing.
- **Admin:** credentials are write-only in forms and absent from logs and error reports.
- **Existing issues to fix in phase 1:** `/plan_unsubscribe` cancels on a GET, so it needs POST and CSRF. Stripe
  cancellation is tested only against mocks, so it needs a run in Stripe's test mode.

## Federation and privacy

- Nothing about a payment, offer or subscription federates. The Donor label stays local display.
- M11's checks replace the site-wide flag: `private_instance` has 86 uses across 16 files, with 55 route decorators.
  They should collapse into one per-storefront predicate, with a test that walks the URL map, as `app/hidden_routes.py`
  does for onion content.
- Pseudonymisation (M6) runs in a scheduled task. Rows tied to an active subscription, a running Donor label, a credit
  balance or an open dispute keep their link until they settle. A label granted for good holds its row for good, so an
  admin who wants those to lapse gives the offer a length.

## Phases

Each phase ships on its own, with the full gates.

1. **Foundations and Stripe:** encrypted credentials, permission, admin area, adapter contract, ledger, webhook route.
   Stripe moves to the adapter with admin-managed offers, groups and the per-offer Donor label. Existing donors migrate.
2. **Link-outs and guest donations:** admin-managed links, and one-off donations without an account.
3. **Paid registration and private domains:** M10, M11, M12. This is the first concrete use case.
4. **BTCPay Server:** including prepaid periods and the onion.
5. **Prepaid credits:** M3. Nothing can spend credits until perks are decided, so this can wait until then.
6. **PayPal.**

## Open questions

- Which retention period and grace period should be the defaults? They are admin-set either way (M6, M12).
- How long may a pending paid account wait before removal? It has to allow for slow crypto confirmation (M10).
- Does an existing domain with accounts and `federates=True` have any way to become private? C9 and the existing lock on
  changing `federates` suggest not. If so, a private domain must start out as one.
- What should the phase 3 renewal page look like for a member who can't log in normally (M12)?
