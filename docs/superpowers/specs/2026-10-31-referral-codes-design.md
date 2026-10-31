# Referral-code registration — design

Date: 2026-10-31. Status: built on feat/referral-codes (tests: tests/test_referral_codes.py).

## Summary

A new registration mode, **Referral code**. While it is selected, public signup is closed. The only accounts
created are those whose signup carries a valid referral code, and they are approved automatically.
Codes are generated in the user's settings, shown as a link and a QR code, and every referral is recorded.

## Rulings

| # | Ruling |
|---|---|
| RC1 | Built into core (auth/admin/user), not the plugin system. New `Site.registration_mode` value `'ReferralCode'`. |
| RC2 | Code = `uuid5(NS, f"{user_name}:{nonce}")`, where `NS = uuid5(NAMESPACE_DNS, SERVER_NAME)` and `nonce` is a random value stored per user. "Regenerate" rotates the nonce: the old code and every printed QR code stop working. A plain `uuid5(NS, user_name)` was rejected because anyone could compute it. |
| RC3 | A missing or wrong code is refused outright: the form shows an "invalid referral code" error and no account is created. Nothing is queued for approval. |
| RC4 | Caps are admin settings: **max signups per code** (it counts the current code and resets on regenerate) and **max referrals per user (lifetime)** (regenerating doesn't reset it). In both, 0 means unlimited, and admins are exempt from both. A capped code is refused like an invalid one. |
| RC5 | Every signup path is covered. The web form uses the question/answer field, prefilled from `?ref=`. API `POST /user/register` takes the code in its `answer` field (see API registration below). OAuth signup is refused unless the session holds a `?ref=` code captured before the OAuth redirect, and that code is validated again at account creation. |
| RC6 | Advertised as `RequireApplication` (API `registration_mode`) and `openRegistrations: false` (nodeinfo), so Lemmy-compatible clients show the question field. |
| RC7 | A valid code skips only the approval queue. Email verification, honeypot/captcha, the onion proof, blocked words, the country block and the referrer block all still apply. |
| RC8 | Codes are checked at use time. The code's owner must be local, not banned, not deleted, verified, and currently allowed by "Who can generate referral codes?" Narrowing that setting disables existing codes at once, and widening it brings them back. |
| RC9 | Link/QR URL = `domain_base_url(referrer.home_domain()) + /auth/register?ref=<code>`. |
| RC10 | A code is valid only on its referrer's home storefront, so the new account gets the referrer's `site_domain_id`. Used on any other domain it reads as invalid. |
| RC11 | A valid code opens signup on a domain whose public registration is off ('staff'/'admin' storefronts). |
| RC12 | The QR code is an inline SVG rendered server-side with `segno` (pure Python, unpinned per the latest-versions policy), plus a PNG download. No JavaScript is needed (onion). |
| RC13 | Who-referred-whom is visible only to admins, on an admin referral list (referrer → referred, date, code; filterable by referrer with `?by=`). There is no user-facing list and no public "invited by". |

## Admin UI (/admin/misc)

- Registration mode drop-down gains **Referral code**. On change to it, JS sets the application-question textarea
  to "Enter your referral code". The admin can still edit it, and the text is not forced on save.
- New drop-down **Who can generate referral codes?**: Admins only / Admins + Staff / All local users.
  The default is Admins only.
- New integer fields: **Max signups per code**, **Max referrals per user (lifetime)**. Both default to 0 (unlimited).

## User settings

A referral panel is shown only when the mode is ReferralCode and the user is allowed to generate codes (RC8).
- Before any code exists: a "Generate referral code" button, which is POST + CSRF.
- Once a code exists: the code, a copyable link (RC9), the SVG QR code, a PNG download, uses so far / cap, the lifetime count / cap,
  and "Regenerate" (POST + CSRF, behind a confirmation that says old links stop working).

## Data

- `user.referral_nonce` (String, nullable; NULL = never generated) and `user.referral_code` (UUID string, unique
  index), the derived code stored so a submitted code resolves in one query.
- New table `referral`: `id`, `referrer_id` → user, `referred_id` → user (unique), `code` (the UUID string used),
  `created_at`. Uses of the current code = rows with `code == referrer.referral_code`. Lifetime uses = all rows for
  that `referrer_id`.
- Alembic migration `a7c2e5f9d3b1` plus downgrade. The three admin settings live in the key-value `settings` table
  (`get_setting`/`set_setting`): `referral_generators` (default `admins`), `referral_max_per_code`, `referral_max_per_user` (default 0).

## Edge notes

- `handle_abandoned_open_instance` affects only `Open`, so it doesn't apply to this mode.
- Applications already pending when the mode switches to ReferralCode stay in the queue, untouched.
- A `referral` row is written in the same transaction as the new user. Caps are checked under a row lock on the
  referrer, so two simultaneous signups can't both slip past the cap.
- The admin referral list is `/admin/referrals`, gated by the 'administer all users' permission like `/admin/users`.
- Rate limiting: the existing `/auth/register` limits apply. A wrong code gives the same generic error as an unknown code,
  so no oracle reveals whether a code exists.

## API registration (added 2026-10-31)

`POST /api/alpha/user/register` and `GET /api/alpha/user/get_captcha` are implemented (`app.auth.util.register_from_api`)
so apps can sign people up. The web form's `RegistrationForm` validates the JSON body, then the web form's gates run in
the same order, so all three registration modes and referral codes behave as they do in the browser. Owner rulings:

| # | Ruling |
|---|---|
| AR1 | Captcha: Lemmy's `get_captcha`. `ok` is `{png, wav, uuid}` (base64, no `data:` prefix; the image is a JPEG), or null when captcha is off. Register sends `captcha_uuid`/`captcha_answer`, checked once with `decode_captcha`. |
| AR2 | Registering is agreeing to the terms: the request has no terms field, as in Lemmy. |
| AR3 | The onion refuses (`registration_closed`): its proof-of-work and wait step belong to the web form. |
| AR4 | `jwt` is returned only when the account can log in now. It is null while email verification or an application is pending, and for an account created banned. `registration_created` means an application was created. |

Refusals are 400 with a message: `registration_closed`, `captcha_incorrect`, `registration_declined` (country),
`invalid_referral_code`, the web form's own refusal texts, or `<field>: <validator message>` for form validation.
Rate limits match the web form's (300/day, 10 per 5 minutes); get_captcha is 60/hour.
