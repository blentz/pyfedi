# Monetization phase 3: paid registration and private domains. Implementation plan

**Goal:** A domain can charge a first subscription fee at registration, require login to browse, and restrict a member
whose subscription has lapsed, beside public domains on the same instance.

**Spec:** `docs/superpowers/specs/2026-11-05-monetization-infrastructure-design.md`, M10, M11, M12, C9, C10, C11. Global
constraints as in `docs/superpowers/plans/2026-11-05-monetization-phase-1.md`.

**Needs a provider:** only Stripe can collect the fee until phase 4 (BTCPay). Live Stripe calls are unverified by owner
ruling, so a live test-mode signup is owed before anyone is charged real money.

## Found while planning

Measured 2026-11-06 on a test stack, logged out, with `Site.private_instance` on and the API enabled:

| Request | Answer |
|---|---|
| `/api/alpha/post/list`, `/api/alpha/post?id=1`, `/api/alpha/search?q=...` | 200, with the post's title |
| `/api/alpha/community/list`, `/api/alpha/community?name=...` | 200, with the community's name |
| `/community/get_sidebar/<id>` | 200, with the community's sidebar text |

So today's private setting doesn't cover the API at all: 152 of 628 route functions take a GET with no login gate of any
kind, 45 of them in the API. Most are meant to be open (login, health, federation), but nothing tells those from a
forgotten one. This is why S1 below denies by default, and it closes the leak on existing private instances too.

## Decisions this plan makes

The owner can overrule any of them.

### Private domains (M11)

| # | Decision | Why |
|---|---|---|
| S1 | One `before_request` gate replaces the decorators' test. On a login-required storefront, a logged-out request is refused unless its endpoint is on a short written list. A test walks the URL map and fails on any endpoint not classified. | The measurement above: 51 decorators left the API open. The existing decorators stay as they are for now, reading the same predicate, so RSS keeps its 404 and its token. |
| S2 | The list: login, logout, registration, password reset and email verification, static files, health, robots, `/about`, `/privacy`, CMS pages, payment webhooks, and `/donations/return`. On a federating domain, federation routes too. | A logged-out person must be able to join or log in. `/about`, `/privacy` and CMS pages are what an operator wants a stranger to read before paying, and an admin writes them. |
| S3 | A refused page redirects to login, an API call gets 401, and a machine-readable route gets 404. | Each client gets the answer it can act on. |
| S4 | The default domain keeps `Site.private_instance` as its setting. Every other domain gets `SiteDomain.login_required`. Migration copies the site flag to every other domain. | Same rule as `registration`, where the default row reads the Site row. Nothing opens up on deploy. |
| S5 | C9 applies when the setting is turned on: only a `federates=False` domain can turn it on. A domain already on (S4), or the default domain, keeps working while federating. | C9 would otherwise shut existing private instances. |
| S6 | An existing federating domain can't become private. It must start as a local-only domain. | Answers the spec's open question: C9 and the `federates` lock together already say this, because federated content has already left the server. |
| S7 | On a domain that doesn't federate, a request asking for ActivityPub JSON gets no exception from the gate. | `is_activitypub_request()` is a header anyone can send. |

### Paid registration (M10)

| # | Decision | Why |
|---|---|---|
| S8 | `registration` gains `paid`, with `registration_offer_id` naming an active subscription offer on that domain, from an integrated provider. Default domains and onion domains can't be `paid`. | The default row ignores `registration` today. C11 leaves an onion no provider until BTCPay. |
| S9 | `public_registration_open` stays false for `paid`, so every path that doesn't know about payment refuses. Only `/register` learns the new mode. | C10 and RC5: failing closed covers OAuth, the API and clients reading `registration_mode`, which see `Closed`. |
| S10 | Referral codes and application questions don't apply on a paid domain. `Site.registration_mode == 'Closed'` still closes it. | Payment is the gate, and taking money before an admin may reject the application is worse than either alone. `Closed` is the operator's shutter. |
| S11 | LDAP neither creates nor writes accounts on a paid domain. | LDAP is already default-domain only for reads. Writing would put a pending account's password in the directory. |
| S12 | A new column, `User.registration_pending_since`, marks a pending account, which also has `verified=False`. `is_active` is false, so `login_user` refuses it, and the API refuses it by the existing `verified` test. | `verified=False` already keeps an account out of listings and off the API. |
| S13 | No email is sent until payment. The webhook then sends the verification email, or finishes setup if verification is off. `/resend_email` and `/verify_email` do nothing for a pending account. | An unpaid signup can't be used to send mail to a stranger. |
| S14 | A pending account that logs in with the right password goes straight to a new checkout, with no session. | Someone who closes the Stripe tab can come back, with no token to design. A wrong password gets the same answer as for any account. |
| S15 | A paid domain's fee is the offer's subscription, charged from the first day, so no trial. | M10: registration collects the first fee. |
| S16 | Unpaid accounts are removed after 48 hours by the daily task, hard deleted. Stripe checkout sessions expire at 24 hours. | Answers the spec's open question. It holds a name for at most about three days, and leaves room for BTCPay's slow confirmations later. Nothing else references an account that never logged in. |
| S17 | A payment arriving for an account that no longer exists: the provider is told to cancel the subscription, and the ledger row stays for the admin to refund. | A late payment must not keep charging somebody with no account. Should be rare because of S16. |
| S18 | The offer named by a paid domain can't be deleted or deactivated. If its provider goes away, the register page says registration is unavailable. | Otherwise registration quietly opens or breaks. |

### Lapse (M12)

| # | Decision | Why |
|---|---|---|
| S19 | A new column, `User.access_lapsed_at`, is set when a member's last active subscription in the registration offer's group ends, and cleared when one starts. The default grace is 7 days, per domain, in `SiteDomain.lapse_grace_days`. | Answers the spec's open question. A plan change within the group doesn't count as a lapse (M7). Admin-made accounts and members who joined before the domain became paid never get a value, so they're never locked out. |
| S20 | Past grace, a lapsed member may use: `/donations` and checkout, cancel, billing, the return page, logout, settings export, delete account. All else redirects to `/donations`. The API answers 403, and an RSS token stops working. | M12 says renew or export. Delete is added because a member leaving must be able to leave. Nothing is deleted by this. |
| S21 | Admins are never lapsed. | They'd otherwise lock themselves out of the admin pages that fix it. |
| S22 | **A member cancelling on a paid domain cancels at the end of the paid period**, and the page shows the date. Elsewhere, cancelling stays immediate as now. | Immediate cancellation would take away time they paid for. This adds an `at_period_end` argument to the adapter. Mocks only, like all Stripe calls here. |
| S23 | "Export their data" means the existing settings export. | It's the only export the site has. A full export of posts and comments is a feature of its own, for the owner to ask for. |

### Admin

| # | Decision | Why |
|---|---|---|
| S24 | A new settings page per domain: who may register, registration offer, login required, lapse grace. Editing needs `manage domains`, and choosing an offer needs `manage monetization` too. | C2 keeps money with its own permission. There's no domain edit page today, only add and delete. |
| S25 | A form refuses what S5, S6, S8 forbid. | So a mistake is explained rather than stored. |

## Tasks

1. **Columns and predicate:** migration (`registration` check, `registration_offer_id`, `login_required`,
   `lapse_grace_days`, `User.registration_pending_since`, `User.access_lapsed_at`, the S4 copy), `DomainInfo` fields, one
   `login_required_here()`, both decorators and the inline RSS tests reading it.
2. **The gate (S1-S3, S7):** `app/login_gate.py`, the URL-map walk test, and tests for the API and sidebar leaks above, on
   a private default domain and on a private sister domain beside a public one.
3. **Domain settings page (S24, S25).**
4. **Paid registration (S8-S15):** `/register` on a paid domain with the price shown, the pending account, checkout,
   refusal on OAuth, API and LDAP, the login path into checkout, and the return page's wording.
5. **Activation (S13, S17):** `core.py` activates on a matching subscription start, never on a mismatch. Mail and setup run
   after the commit, under the account's home domain. A retried webhook doesn't activate twice.
6. **Removal (S16, S18):** the daily task and checkout expiry, with offer protection in the admin.
7. **Lapse (S19-S23):** setting and clearing `access_lapsed_at`, the gate for members, API and RSS refusal, cancel at period
   end, and the page's date.
8. **Docs and gates:** `INSTALL.md`, changes, spec status and answered questions, the full gates, a browser check.

## Tests that must exist

- A member of a paid private domain and a public domain's visitor, on one instance, each get what they should from the same
  URLs (M11).
- Every signup path is refused on a paid domain but the web form: OAuth, API, LDAP, referral link (C10, RC5).
- A browser returning from Stripe with `success` activates nothing. Only a verified webhook does.
- A webhook with the wrong amount leaves the account pending.
- A webhook for a removed account cancels the subscription (S17).
- A plan change within the group leaves `access_lapsed_at` empty, and a member elsewhere is unaffected by any lapse (M12).
- A lapsed member keeps every row they had, and renewing restores access at once.
- Every endpoint in the URL map is classified by the gate's walk test.

## Deploy notes

- Migration only. The S4 copy means nothing opens on deploy.
- **S1 changes what an existing private instance answers:** logged-out API clients and the undecorated pages stop working
  on it. That's the leak being closed, but any logged-out integration that relied on it will need to log in.
