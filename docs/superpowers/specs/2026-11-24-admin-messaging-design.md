# Admin messaging — design

Date: 2026-11-24. Status: scope agreed with the owner 2026-11-24 (rulings MS1-MS4 below); details proposed by Claude.

## Summary

The admin Newsletter page becomes **Messaging**: one page that lists every email the software sends, says when each is
sent and to whom, and lets an admin

- override the subject and text of the built-in emails (welcome, verification, password reset, subscription
  expiring, unread notifications, community invite), and
- write, store, edit and schedule their own messages to all members, a filtered set of them, or chosen accounts.

It also changes when the subscription-expiring reminder goes out: daily, from ten days before a prepaid period runs
out, instead of once at seven days.

## Rulings

Owner rulings, 2026-11-24.

| # | Ruling |
|---|---|
| MS1 | Built-in emails: the admin edits **text only** (subject and body). When an email is sent, and to whom, stays in code and is shown read-only. |
| MS2 | An admin message goes to all members by default, or to those matching filters (domain, subscription, role), or to accounts picked by name. The page shows how many it would reach. |
| MS3 | An admin message is sent now or once at a chosen time. No recurring schedules. |
| MS4 | Admin messages honour the newsletter opt-out, unless the admin marks one a **service notice**. |
| MS5 | A member whose prepaid subscription has ten days or less left gets the expiring email once a day until it runs out or is renewed, encouraging them to renew. |
| MS6 | An admin message can be sent from one chosen domain: its name, and links to its address, in every copy, whatever each member's home domain. By default each member gets it from their own home domain. (2026-11-26) |

## Non-goals

- Changing *when* a built-in is sent (other than MS5), turning built-ins off, or adding new triggers.
- Per-language overrides. An override is one text; without one, each email keeps its translated default.
- Recurring messages, A/B tests, open or click tracking.
- Editing the admin-addressed emails (topic suggestion, community move request) and the debug test email. They are
  listed with their conditions but carry structured data and stay as they are.

## Decisions

### Catalogue (D1-D4)

- **D1** `app/messaging/catalog.py` holds the list of every email, as code: key, name, when it is sent, who gets it,
  whether it is editable, its placeholders (each with a description and a sample value) and which of them are
  required. The Messaging page and the renderer both read it. A test fails if a template under `app/templates/email/`
  has no catalogue entry.
- **D2** Editable built-ins: `welcome`, `verify_email`, `password_reset`, `subscription_expiring`,
  `unread_notifications`, `community_invite`. Listed only: `topic_suggestion`, `community_move_request`, `test_email`.
  Admin messages are listed as their own section.
- **D3** An override is a row of `email_template` (key, subject, body, updated_at, updated_by). No row means the
  bundled Jinja templates, unchanged. "Reset to default" deletes the row.
- **D4** `welcome` already has a body setting (`registration_approved_email`). An override takes precedence over it;
  without one the setting still applies as before, and the page says which is in use.

### Override text (D5-D9)

- **D5** The body is Markdown. The HTML part is rendered with `markdown_to_html` and moved onto the member's home
  domain with `storefront_html`, as the welcome email's setting already is; the text part is the Markdown source.
- **D6** Placeholders are written `{name}`. Only the entry's own placeholders are allowed: saving refuses an unknown
  one, and one missing a required placeholder (the link that makes `verify_email` and `password_reset` work, the list in
  `unread_notifications`). `{{` and `}}` write a literal brace.
- **D7** Placeholders are filled **after** Markdown is rendered, with HTML-escaped values in the HTML part, so a
  display name cannot inject markup. A placeholder that Markdown percent-encoded inside a link (`%7Bname%7D`) is
  filled too.
- **D8** Structural parts are placeholders rendered by code: `{notifications}` and `{recent_posts}` in
  `unread_notifications` are lists (HTML in the HTML part, plain lines in the text part).
- **D9** The layout around an override keeps what the default has around its text: the logo header, and the
  unsubscribe footer and `List-Unsubscribe` header where the default has them (`welcome`, `unread_notifications`).

### Admin messages (D10-D18, D24)

- **D10** `admin_message`: subject, Markdown body, audience, `service_notice`, status, `scheduled_for`, creator,
  timestamps, and counts. Placeholders `{name}`, `{site_name}`, `{site_url}`.
- **D11** Status: `draft` → `scheduled` → `sending` → `sent`. A scheduled message can go back to draft (unschedule).
  Only drafts and scheduled messages can be edited; drafts can be deleted. A sent message is read-only and can be
  duplicated into a new draft.
- **D12** Audience is one of: `all`; `filtered`, by domain(s), subscription (any active / none / particular offers)
  and role(s), AND across kinds and OR within one; `picked`, a list of accounts (`admin_message_pick`).
- **D13** Every recipient is a local, verified, unbanned, undeleted account with an email address. Unless the message
  is a service notice they must also not have opted out of the newsletter (MS4). A picked account that fails these is
  skipped, not mailed.
- **D14** Recipients are worked out when the message is sent, not when it is saved. The page shows the count as of now.
- **D15** `send-queue` (every few minutes) claims due messages with one `UPDATE ... WHERE status = 'scheduled' AND
  scheduled_for <= now RETURNING id`, then hands each to a Celery task. "Send now" schedules for now and dispatches at
  once. Sending runs in a task, never in the request (the old page sent inside the request).
- **D16** Each recipient is claimed with an `admin_message_delivery` row (unique on message and account) **before**
  the mail is handed to `send_email`, so a retried or concurrent task never mails anyone twice. A failure is recorded
  on the row and counted. A crash between claim and send loses one mail rather than sending one twice.
- **D17** A message whose task died (status `sending`, heartbeat older than 15 minutes) is dispatched again by
  `send-queue` and carries on from where it stopped (D16).
- **D18** "Send a test to me" mails the current admin one copy at once, for a built-in or an admin message, without
  changing anything.
- **D24** `admin_message.from_domain_id` (MS6) is the domain every copy is sent from, or null for each reader's home
  domain (the default, as before). Each copy renders under that domain (`domain_storefront`): the sender's display
  name and `{site_name}` are its branding's name, the logo is its branding's logo, and `{site_url}`, the unsubscribe
  link and Markdown links name its address. The page's preview renders under it too; sent from home domains, the
  preview shows the domain the page is on. The From address stays `MAIL_FROM`, which is one per server. A deleted domain sets it back to null. A copy
  of a message keeps it, and a test to the admin uses it.

### Subscription expiring (D19-D20)

- **D19** `REMINDER_DAYS` becomes 10. `reminded_at` now means the last reminder; one is due when it is null or before
  the start of the current UTC day. Renewing still resets it. A cancelled period still gets none (plan 2026-11-15 B7).
- **D20** The default text says how many days are left and links to the plans page to renew. Placeholders: `{name}`,
  `{plan}`, `{expiry_date}`, `{days_left}`, `{renew_url}`, `{site_name}`.

### Access (D21-D22)

- **D21** The page is `/admin/messaging`; `/admin/newsletter` redirects to it. Seeing it and sending admin messages
  needs `send newsletter` (the name is kept, as roles already hold it; its description becomes "Write, schedule and
  send messages to members").
- **D22** Editing built-in text needs a new permission, `edit email templates`. The password-reset and verification
  emails carry the links that sign people in, so this is kept apart from sending newsletters.
- **D23** A placeholder whose link carries a sign-in token (`{reset_url}`, `{verify_url}`) is a *secret* link. Otherwise
  someone holding `edit email templates` could send every member's token to their own server, for example in an image's
  address or inside a URL of theirs, and so take over any account, an admin's too. So:
  - Saving refuses a secret link anywhere but standing on its own or as the whole target of a Markdown link
    (`[words]({reset_url})`, not an image), and refuses it in the subject.
  - Rendering fills it only there too: in HTML only as the whole `href` of a link, or as a link of its own in text.
    Anywhere else it is dropped, so a row written some other way cannot leak it either.
  - Only the built-in Admin may change the emails that carry one (`password_reset`, `verify_email`). Other holders of
    `edit email templates` see them as "Admin only".

## Tests

Every decision above has a test, including: unknown and missing placeholders refused; escaping of placeholder values;
percent-encoded placeholders; each sender using its override and its default; audience filters alone and combined;
opted-out members with and without a service notice; claim, double dispatch and stale recovery; daily reminders on
consecutive days and not twice on one day; permission checks on every route.
