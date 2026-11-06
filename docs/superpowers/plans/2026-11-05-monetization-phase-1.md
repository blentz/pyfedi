# Monetization phase 1: foundations and Stripe. Implementation plan

**Goal:** An admin can connect Stripe, define offers and see the ledger on a live site, with credentials encrypted in the
database. Existing donors and existing Stripe setups keep working through the deploy with nothing done by hand.

**Architecture:** A new `app/payments/` package. Provider adapters turn webhooks into neutral events. Core code alone
writes the ledger, subscriptions and the Donor label from those events. Admin pages live in
`app/admin/payments_routes.py`, registered as `roles_routes` is. `/donations` moves from `app/user/subscription.py` to
`app/payments/routes.py`.

**Tech stack:** Flask, SQLAlchemy/Postgres 17, Alembic, Celery, Jinja, the `stripe` SDK (16.x), `cryptography`
(`MultiFernet`), pytest via `./run_tests.sh`.

**Spec:** `docs/superpowers/specs/2026-11-05-monetization-infrastructure-design.md` (binding), rulings M1-M12, C1-C12.
Phase 1 delivers M1, M2, M5 (Stripe only), M6, M7, M9, C1-C7. It does not deliver guest donations, link-outs, paid
registration, private domains, BTCPay, credits or PayPal.

## Global constraints

- TDD: a failing test first in every task, then the code. One commit per task.
- Branch `feat/monetization-phase-1`. No push and no merge without the owner.
- New code has 100% line and branch coverage. Before merging: the full suite with `-n 3 --cov=app --cov-branch
  --cov-report=json:coverage.json`, then `tests/check_coverage_floors.py` and `tests/check_changed_line_coverage.py
  coverage.json blentz --branches`, in the `test-runner` image.
- Tests: deterministic parametrize ids, one login per test, `csrf()` before any request. A slow test is a bug to fix.
- Inline imports only if unavoidable. `import stripe` stays lazy because it takes 0.7s, in one place only (Task 5), which
  lowers today's count.
- No file may name the first domain's host (`tests/test_no_first_domain_literal.py`).
- Don't pin dependencies. Tooling lives in the repo, never in `/tmp`.
- No secret reaches a log, an error report, a form's rendered value or a test fixture that looks like a real key.
- Every template change also goes into `app/templates/themes/dillo/` where that theme has its own copy.

## Decisions this plan makes

These fill gaps in the spec. The owner can overrule any of them.

| # | Decision | Why |
|---|---|---|
| P1 | The Donor label reads a new column, `User.donor_until`, kept up to date by core, not a query per author link. A sentinel far-future date means "while subscribed". | The label appears on every author link in a list. A per-link query would add a query per row. A sentinel also means the label doesn't depend on renewal events, which existing sites don't send. This refines the spec's "reads the same data". |
| P2 | `User.stripe_customer_id` stays through phase 1. | It is how a renewal maps to a member. C6 removes the columns only after a release proves the migration, and a neutral customer table can wait until a second provider needs one. |
| P3 | Members can make one-off Stripe donations in phase 1. Guests can't until phase 2. | The per-offer Donor length (M9) needs a one-off offer to exist. |
| P4 | Retention (M6) starts blank, meaning rows are never pseudonymised, and the ledger page asks the admin to set it. | Record-keeping duties vary by jurisdiction, so the app shouldn't delete links by default. Once set, the task runs daily. |
| P5 | Checkout redirects to the Checkout Session's `url`. `stripe_redirect.html` and its `js.stripe.com` script go away. | Removes an off-site script from the page, with less code. |
| P6 | An offer's amount, currency and interval are read from Stripe when the admin enters a Price ID. A price in another currency than the instance's is refused. | The admin can't type an amount that disagrees with what Stripe charges, and C3 holds. |

## Task 1: Permission and admin shell

**Files:**
- Modify: `app/permissions.py`, adding to the `site` group of `CATALOGUE`:
  `Permission('manage monetization', 'site', False, False, 'Payment providers, offers and the payment ledger')`.
- Create: `app/admin/payments_routes.py` with `GET /admin/payments`, guarded by `@login_required` and
  `@permission_required('manage monetization')`.
- Modify: `app/admin/__init__.py`, `app/templates/admin/_tabbed_nav.html` (a "Payments" tab).
- Create: `app/templates/admin/payments.html`.
- Test: `tests/test_payments_admin.py`. Add rows to `tests/rbac_parity.py` and `tests/test_admin_content_routes.py`'s
  route table as those files require.

**Steps:** write tests that a holder gets 200, a non-holder gets 403 and a stranger is sent to log in, and that the tab
shows only to holders. See them fail, then build. No migration: Admin holds every permission implicitly, and no other
role gains it.

## Task 2: Encrypted credentials

**Files:**
- Create: `app/payments/__init__.py`, `app/payments/credentials.py`.
- Modify: `config.py` (`PAYMENT_SECRETS_KEY`, comma-separated, newest first), `env.sample`, `env.docker.sample`.
- Modify: `app/cli.py`: `flask generate-payment-key` prints a new key and stores nothing.
- Test: `tests/test_payment_credentials.py`.

**Interfaces:**
- `credentials_available() -> bool`: the key is set and valid.
- `seal(values: dict) -> str`, `unseal(token: str) -> dict`, using `MultiFernet`, raising `CredentialsUnavailable` when
  there is no valid key.

**Tests:** round trip; rotation (a token sealed under the old key opens with `new,old`); a wrong key raises; a bad key
format means unavailable rather than a crash at startup (C4); `seal` output contains no plaintext.

## Task 3: Schema

**Files:**
- Create: `migrations/versions/b2e6c4a8d1f3_payments.py` (down_revision `a7d3f1c9e5b2`).
- Modify: `app/models.py`.
- Test: `tests/test_payments_migration.py`, using `Operations.context` as `tests/test_branding.py` does.

**Tables:**
- `payment_provider`: `kind` (unique), `enabled`, `credentials` (sealed text), `settings` (JSON, non-secret).
- `offer`: `provider_kind`, `kind` (`donation`, `subscription`), `title`, `offer_group`, `provider_price_id`,
  `amount_minor` (nullable), `currency`, `interval`, `grants_donor`, `donor_days` (nullable), `position`, `active`.
- `offer_domain`: `offer_id`, `site_domain_id`. No rows means all domains (M2).
- `payment`: `user_id` (nullable, `ON DELETE SET NULL`), `offer_id` (nullable), `provider_kind`, `provider_reference`,
  `amount_minor`, `currency`, `status` (`paid`, `refunded`, `mismatch`), `created_at`, `settled_at`,
  `UNIQUE (provider_kind, provider_reference)`.
- `member_subscription`: `user_id`, `offer_id` (nullable), `provider_kind`, `provider_reference` (unique), `status`
  (`active`, `ended`), `current_period_end` (nullable), `created_at`, `ended_at`.
- `payment_event`: `provider_kind`, `event_id`, `received_at`, `UNIQUE (provider_kind, event_id)`.
- `User.donor_until` (nullable datetime). `User.is_donor` property, true while it is in the future.

**Data migration (C5, C6), reading `os.environ` as `f1c3a9d7b2e4_site_domain.py` does:**
- If `STRIPE_MONTHLY_SMALL` or `STRIPE_MONTHLY_BIG` is set, create offers in group `legacy` from them and their `_TEXT`,
  `grants_donor` true, `amount_minor` NULL (P6 fills it on import). Credentials are not touched here.
- For each user with `stripe_subscription_id`, create an `active` subscription row with `offer_id` NULL, and set
  `donor_until` to the sentinel. A NULL offer counts as group `legacy`, so a new plan still replaces it.

**Tests:** a site with env plans and subscribers; a site with neither gets no rows; a user with no subscription is
untouched; the downgrade removes all of it and leaves `User.stripe_*` intact.

## Task 4: Core events

**Files:**
- Create: `app/payments/events.py` (frozen dataclasses `Paid`, `Refunded`, `SubscriptionStarted`, `SubscriptionEnded`).
- Create: `app/payments/core.py`.
- Test: `tests/test_payments_core.py`.

**Interfaces:**
- `apply_event(provider_kind, event_id, events: list) -> None`: inserts `payment_event` and applies everything in one
  transaction. A repeated `event_id` does nothing.
- `holds_offer(user, offer) -> bool`.
- `refresh_donor(user) -> None`: the sentinel if a label-granting subscription is active, otherwise the latest expiry
  among paid label-granting one-off payments (no `donor_days` means the sentinel), otherwise NULL.

**Tests (each must fail first):**
- A paid event writes one row. The same event again, and a different event with the same reference (a session and its
  first invoice), still make one row.
- An amount or currency that differs from the offer writes `mismatch` and grants nothing. An offer with `amount_minor`
  NULL is not checked (legacy only, until imported).
- Starting a subscription replaces active ones in its group, including NULL-offer rows when the group is `legacy`, and
  leaves other groups alone (M7). Replacing queues a provider cancel after commit (Task 5).
- Events out of order: a renewal or end for an unknown subscription doesn't raise and doesn't create a false active row.
- The Donor label for: subscribed, ended, one-off with and without a length, refunded, an offer that doesn't grant it.
- A user id that doesn't exist, or isn't a number, is recorded with no account and doesn't raise (as `user_from_reference`
  guards today), so Stripe doesn't retry for days.

## Task 5: Adapter contract and Stripe adapter

**Files:**
- Create: `app/payments/adapters/__init__.py` (`adapter_for(kind)`), `app/payments/adapters/base.py`,
  `app/payments/adapters/stripe.py`.
- Create: `app/payments/tasks.py`: Celery `cancel_replaced_subscription(kind, reference)` with retries, routed to the
  `background` queue in `app/__init__.py`.
- Test: `tests/test_payments_stripe_adapter.py`.

**Interfaces, per the spec's adapter contract:**
- `checkout(offer, user, success_url, cancel_url) -> str` returns a URL. `client_reference_id` is the user, and
  `metadata` (and `subscription_data.metadata`) carry `offer_id`.
- `parse_webhook(body, headers) -> tuple[str, list]` returns the event id and neutral events, raising `InvalidWebhook`.
- `cancel(reference)`, `billing_portal_url(customer, return_url)`, `describe_price(price_id)`, `validate_credentials()`.
- Credentials: the sealed row, else the environment (C5). Every call passes `api_key=` and never sets the global
  `stripe.api_key`, which is shared across threads.

**Event mapping:**
- `checkout.session.completed`, when `payment_status` is `paid` or `no_payment_required`: `Paid` with the amount from
  `amount_subtotal`, so discounts don't read as mismatches. The reference is the session's `invoice` for a subscription
  and its `payment_intent` otherwise. Also `SubscriptionStarted` when it has a `subscription`.
- `checkout.session.async_payment_succeeded`: as above.
- `invoice.paid`: `Paid`, referenced by invoice id, mapped to a member through `stripe_customer_id` and to an offer
  through the line's price.
- `customer.subscription.deleted`: `SubscriptionEnded`.
- `charge.refunded`: `Refunded`, by `payment_intent` or `invoice`.
- Anything else: no events, still 200.

**Tests:** each mapping, from payloads in the shape Stripe sends; a bad signature; a body that isn't JSON; missing and
wrongly typed fields at every level (the current webhook guards these, and those tests carry over); a session with no
customer doesn't erase a stored one; an unpaid session grants nothing.

## Task 6: Webhook route

**Files:**
- Create: `app/payments/routes.py` (blueprint), registered in `app/__init__.py`.
- Modify: `app/user/subscription.py`, removing the old webhook.
- Test: `tests/test_payments_webhook.py`, porting the webhook classes of `tests/test_user_subscription.py`.

**Behaviour:** `POST /payments/webhook/<kind>`, with `POST /stripe_webhook` as an alias (C7). An unknown or disabled kind
gives 404, and an invalid signature gives 400 with no change. Requests must reach it with no login and no CSRF token.
Check its status under `app/hidden_routes.py` and the private-instance gates, because a private site must still accept
webhooks.

## Task 7: Member pages

**Files:**
- Modify: `app/payments/routes.py`; `app/templates/user/choose_plan.html`; `app/cms.py` (`stripe_plans_available`
  becomes `offers_available`, which is true when an enabled provider has an active offer on this storefront); `base.html`
  and the dillo theme's menu links.
- Delete: `app/templates/user/stripe_redirect.html`, `app/user/subscription.py`.
- Test: `tests/test_payments_member_pages.py`, porting the rest of `tests/test_user_subscription.py`. Update
  `tests/test_onion_third_party.py`, `tests/test_cms_donate_page.py`, `tests/storefront_url_inventory.py`.

**Behaviour:**
- `GET /donations`: offers for this storefront, grouped, with the member's active subscriptions and payment history. The
  same self-explaining 404 as today when there are none.
- `POST /donations/checkout/<offer_id>` (CSRF): redirects to the provider (P5). An offer not on this storefront, or
  inactive, gives 404.
- `POST /donations/subscriptions/<id>/cancel` (CSRF, and the row must be the member's): replaces the GET
  `/plan_unsubscribe`, which is removed.
- `POST /donations/billing` (CSRF): the billing portal.
- `success_url`, `cancel_url` and the portal's return URL use the current storefront's host, and never the onion (C11).
- A return URL grants nothing. Only a webhook does. The success page says the payment is being confirmed.

**Tests:** per-domain filtering (`tests/two_domains.py`); onion refusal of all three POSTs; another member's subscription
gives 404; a GET to a POST route gives 405; an old `/stripe_redirect/...` link gives 404, or a redirect to `/donations` if
ported tests show members may hold one.

## Task 8: Donor label

**Files:**
- Modify: `app/templates/_macros.html`, `app/templates/themes/dillo/_macros.html`,
  `app/templates/post/post_embed.html`, `app/templates/user/show_profile.html`, to read `user.is_donor`.
- Test: `tests/test_payments_donor_label.py`.

**Tests:** the label for each state, in both themes; rendering a list of posts runs no more queries than before; a grep
test that no template reads `stripe_subscription_id` any more.

## Task 9: Admin pages

**Files:**
- Modify: `app/admin/payments_routes.py`, `app/admin/forms.py`, `app/templates/admin/payments*.html`.
- Test: `tests/test_payments_admin.py`.

**Behaviour:**
- **Status:** whether the key is set, and what to do if not (C4), with the command from Task 2.
- **Stripe credentials:** write-only fields. Blank means "keep". The stored value is never rendered. Saving runs
  `validate_credentials()`. The page lists the webhook URL and required events, and warns that renewals and refunds won't
  appear until the five events are sent, since existing sites send only two.
- **Import (C5):** a button that seals the environment's Stripe values and fills legacy offers through `describe_price`.
- **Offers:** add, edit, deactivate, order; title, kind, group, Price ID (P6), Donor grant and length, domains. Offers
  with payments can be deactivated but not deleted.
- **Settings:** currency (fixed once an offer exists), retention (P4).
- **Ledger:** paged, filterable by status and offer, showing amounts and account links.

**Tests:** the rendered edit page contains no credential value, including after a failed save; a blank save keeps the
stored value; failed validation keeps the old credentials; import is idempotent; another currency's price is refused; an
offer with payments can't be deleted; a credential value isn't in any log output captured during these flows.

## Task 10: Account deletion

**Files:**
- Modify: `app/user/routes.py` (`delete_account`), `app/user/utils.py` (`purge_user_then_delete_task`),
  `app/api/alpha/routes.py` (the delete path), through one shared helper `end_subscriptions_of(user)` in core.
- Test: `tests/test_payments_account_deletion.py`.

**Behaviour:** Deleting an account cancels its active subscriptions at the provider. Today a deleted account keeps being
charged. A failed cancel is retried by the Task 5 task and doesn't block the deletion. Ledger rows lose the account link
through `ON DELETE SET NULL` and keep their amounts (M6). Check first which deletion paths really remove the row, since
some only mark it deleted, in which case the link must be cleared explicitly.

## Task 11: Pseudonymisation

**Files:**
- Modify: `app/payments/core.py` (`pseudonymise_settled()`), `app/cli.py` (`daily_maintenance` and
  `daily_maintenance_celery`, whichever each deployment runs).
- Test: `tests/test_payments_retention.py`.

**Tests:** blank retention changes nothing; past retention clears `user_id` and keeps the amounts; rows younger than
retention stay; a row tied to an active subscription stays; running twice is harmless.

## Task 12: Docs and final checks

**Files:**
- Modify: `INSTALL.md` (the Stripe section now describes the admin page, the key and the five events),
  `docs/CHANGES-since-v1.8.10.md`, the deploy guide, spec status.

**Steps:**
1. Full gates, as in the global constraints.
2. **Stripe test mode, with the owner's test keys on a local stack, never committed:** connect, subscribe, renew through
   a test clock or CLI event, switch tier in a group, one-off donation, refund, billing portal, cancel, delete an account
   with a subscription. This is the run the spec requires, because all automated Stripe tests use mocks.
3. A browser check of the admin pages and `/donations`.
4. Report results to the owner. The owner decides on merge, push and deploy.

## Deploy notes

- **Migration:** adds tables and backfills. It makes no network calls and needs no new secret.
- **Before or after deploy, in either order (C4, C5):** run `flask generate-payment-key` and add `PAYMENT_SECRETS_KEY` to
  `.env.quadlet`. Until then Stripe keeps running from the environment, and the admin page says what's missing.
- **The key is a backup item:** keep it apart from database backups, as with the age restore key. Losing it means
  re-entering the credentials, and no payment data is lost.
- **Stripe dashboard:** add `invoice.paid`, `charge.refunded` and `checkout.session.async_payment_succeeded` to the webhook.
  Existing behaviour doesn't depend on them, but the ledger is incomplete without them.
