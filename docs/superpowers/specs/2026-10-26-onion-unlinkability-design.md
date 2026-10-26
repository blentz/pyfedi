# Onion unlinkability and log minimisation: design

Date: 2026-10-26. This is project 5 of `docs/superpowers/specs/2026-10-17-onion-storefront-design.md` (§6:
"logging minimisation across the whole deploy … balance against the fail2ban jails in §4.5"). Projects 1–4
are deployed.

**Terms.** The *first domain* is the default domain (SERVER_NAME). *The onion* is the Tor onion domain. An
*onion request* is one for which `is_onion_request()` is true. *Onion state* is any data that exists because
of an onion request.

## 1. Intent

The threat is **seizure of the server** (owner: "server seizure; minimise what an attacker could learn").
Supporting Tor responsibly means protecting the anonymity of people who use the onion.

**Primary rule (owner):** nothing the server keeps may connect any user, IP address or email address with
onion activity. "The primary detail we are looking to omit … is connecting any user with any of our onion
activity."

The secondary aim is that logs live minutes, not weeks. The owner needs about the last 15 minutes when
debugging.

## 2. Goals, non-goals, anti-goals

Goals:
1. **Onion state is RAM-only and gone at reboot** (owner). A powered-off seized box holds no onion trace.
   The only exception is the onion media cache (goal 6).
2. **`audit_field` skips the write on an onion request, and writes no NULL** (owner). A clearnet account
   that logs in or posts over the onion keeps its stored `ip_address` and `ip_address_country` unchanged.
   The same applies to `Instance.ip_address` from inbox posts, where the onion never applies.
2a. **Onion-registered accounts always have the IP `169.254.88.88`** (owner). An onion-registered account
   is one whose home domain is the onion.
   - Every write of `User.ip_address` for such an account stores `169.254.88.88`, never a real address.
     This covers registration, login, API and OAuth login, post and reply, over the onion or the
     clearnet.
   - `ip_address_country` stays NULL, and there is no ipinfo lookup for such an account (Claude's call).
   - At rollout, a data migration sets every existing onion-registered account to `169.254.88.88` and
     clears its country.
   - The ban-check servers receive no IP for such an account (Claude's call: the sentinel is not an
     address).
   - Consequence: every onion account shares one IP, so an IP ban of `169.254.88.88` bans them all.
     The IP-ban code stays as it is (owner). The deploy guide warns admins never to ban it (Claude's
     call).
3. **Onion traffic does not update `User.last_seen`** (owner). The same holds for every other "last
   activity" stamp written per request: `email_unread_sent`, and any similar per-request stamp the builder
   finds.
4. **Raw logs are kept for 1 hour, in RAM only** (Claude's call under the owner's "15 minutes").
   - This covers the journal (all units), gunicorn, the app, Caddy, tor and fail2ban.
   - The journal is volatile (`Storage=volatile`) with 1 h retention. It is the only copy.
   - rsyslog's `/var/log/messages*` copies of container output stop. The existing files are deleted at
     rollout.
   - The app's `logs/pyfedi.log*` file handler goes; the app logs to stderr, that is, to the journal.
   - fail2ban logs to the journal, not `/var/log/fail2ban.log`. Its database keeps a ban only as long as
     the ban (`dbpurgeage` = 1 h, the longest bantime).
5. **IP-free daily counts are kept for a rolling 3 weeks** (owner). They are per day: how many times each
   error or traceback kind appeared, per unit, named by exception type and the app's file:line.
   - They contain no IP, user name, email, URL path, query, host name or activity id.
   - They stay on disk for 21 days, then are deleted.
   - They replace the "prior week" log searches in the post-deploy check (DEPLOY_GUIDE §11).
   - The builder picks the counting point (a log handler, or a minutely journal reader) and the storage
     (a file under the LUKS mount, or a DB table).
6. **The onion media cache moves to the LUKS volume, capped at 5 GiB** (owner).
   - It lives on the encrypted pgdata volume, not the root disk.
   - When the volume is locked, the cache path is absent. The app then serves no cached media and never
     500s (the DB is locked too, so the site is down anyway).
   - The existing `media_proxy/` on the root disk is deleted at rollout.
7. **Redis onion keys live in a RAM-only store** (owner: RAM-only).
   - This covers the onion guard (`onion_login_fail:*`, `onion_pow_used:*`, `onion_wait_*`), the onion
     rate-limit buckets (`onion-anon`, `onion-user:<id>`) and any other key written only because of an onion
     request.
   - The store never persists to disk: no RDB, no AOF.
   - The main Redis, which holds the Celery broker, keeps its persistence, so queued federation tasks
     survive a restart.
   - At rollout, the existing onion keys are deleted from the main Redis, and a fresh `dump.rdb` is written
     without them.
8. **Onion registration recommends a privacy-preserving email** (owner).
   - The onion's registration page suggests registering with ProtonMail or another privacy-preserving
     email service.
   - Showing it on the onion only is Claude's call.
   - The text is translatable, and it links nowhere off the onion: plain text, no link.
9. **Log lines that would only tag the onion go.**
   - `auth: login failed ip=onion`, `auth: register attempt ip=onion` and `scan: probe refused ip=onion`
     are not written. fail2ban never matches them (`<ADDR>` refuses `onion`), so they only record onion
     use (Claude's call).
   - Any other line that logs a user name, user id or email for an onion request is reworded or dropped.
     The builder audits `current_app.logger` calls on paths an onion request reaches.
10. **Error emails for an onion request carry no request details** (Claude's call).
    - These are the `ERRORS_TO` SMTP handler mails and Flask's "Exception on <path>" line.
    - They leave the box through the mail provider, so for an onion request they carry the exception
      type and app traceback only: no path, host, query, form, user or headers.

Non-goals:
- **IP bans.** "Leave existing ip ban feature as-is. no changes needed" (owner). Clearnet `User.ip_address`
  storage, `IpBan`, ipinfo and the ban-check servers are unchanged.
- **The email linkage on onion accounts.** An email on an onion-registered account is an "acceptable
  linkage" (owner), mitigated only by the recommendation in goal 8.
- **What an onion account is.** An account whose home domain is the onion is an onion account by design
  (project 3). The aim is that nothing links it, or a clearnet account's onion use, to an IP or another
  identity.
- **Admin access records** (ssh `wtmp`/`btmp`/`secure`, `audit`). These record the operator and scanners,
  not users (Claude's call).
- **Content timestamps** (`Post.created_at`, votes, `read_posts`). They do not record which domain the
  request came over, so they cannot show onion use. They are unchanged.
- **Past data.** `last_seen` values already moved by onion traffic cannot be told apart, and stay.

Anti-goals:
- A seized box, powered off, holds anything that shows a given account, IP or email used the onion.
- A seized box, running, holds such a link older than 1 hour, outside the email linkage, the media cache, and the
  onion store's per-user daily limit keys. Those keys (vote quota, AI-detection tally, daily API limits, feed cache)
  live at most 24 hours, in RAM only, so daily caps stay as strict as on the clearnet (owner, 2026-10-26).
- fail2ban stops banning clearnet login floods, registration floods or scanners.
- The first domain's behaviour, output or federation changes. Only its logs shrink.
- A deploy loses queued Celery tasks.

## 3. Constraints

- **No swap**, unless it is encrypted. Prod has none (checked 2026-10-26). RAM-only state means nothing if
  pages can be swapped to the root disk. The deploy guide records this rule.
- **fail2ban keeps working.**
  - Jails read the journal, which now holds 1 h. Their `findtime` (≤ 10 min) and `bantime` (1 h) fit
    inside that.
  - Each jail is checked with a real test ban after rollout. Spec §4.5 already requires this.
- **Post-deploy verification keeps working** through the daily counts (goal 5) and the 1 h journal.
- **The LUKS volume** is the postgres data directory's mount. Placing the cache there must not disturb
  `PGDATA`. A separate directory on the same encrypted device is fine; how is the builder's call (for
  example, a sub-mount or a second LV).
- **Every rule holds on rollout day.** Existing files are deleted, not just stopped: `/var/log/messages*`,
  `/var/log/fail2ban.log*`, `logs/pyfedi.log*`, `media_proxy/` on the root disk, the persistent journal under
  `/var/log/journal`, and the onion keys in `dump.rdb`. The owner is asked before each deletion.
- **Back up env and unit files before editing** (owner rule). Never push; the owner pushes.
- **Project rules:**
  - TDD, one commit per task or defect;
  - 100% changed-line and branch coverage (`tests/check_changed_line_coverage.py --branches`);
  - inline imports only if technically unavoidable;
  - slow tests are bugs;
  - invalid client input stays a 400;
  - no file names the first domain (`tests/test_no_first_domain_literal.py`).

## 4. Tests that must exist

- An onion login, post, reply and vote by a clearnet account with a stored IP leaves `ip_address`,
  `ip_address_country` and `last_seen` byte-identical. A clearnet request still updates them as today.
- An onion-registered account has `ip_address = '169.254.88.88'` and a NULL country after registration, and
  after a clearnet login, API login, post or reply. Its real IP is never stored, and no lookup is made.
- No onion request writes any key to the main Redis. Check this by scanning keys after a crawl of the
  onion's main pages, a login failure, PoW and an API login.
- No onion request writes an app log line containing a user name, user id, email or `ip=onion`.
- An error on an onion request produces an error email without path, host, query or user.
- The daily counts hold no IP, path or name, and anything older than 21 days is deleted.
- The media cache refuses to grow past 5 GiB and copes with an absent directory.
- The onion registration page shows the email recommendation. The first domain's does not.

## 5. Calls made after the builder check (Claude's)

- **The main Redis rule.** No key in the main Redis may be written, or have its value or TTL changed,
  because of an onion request. The exception is Celery broker messages, which are transient and consumed
  in seconds.
  - A per-user key that an onion request would touch goes to the RAM-only store or is skipped. An example
    is `votes_cast_<date>_<id>`.
  - At rollout, an audit enumerates the onion key prefixes. Those prefixes are removed by `SCAN` + `DEL`,
    never `FLUSHDB`, then a `BGSAVE` runs.
  - Sessions are signed client cookies, and the Flask cache is a `/dev/shm` FileSystemCache. Both are
    already RAM or client-side.
- **The RAM-only store** is a Redis with `save ""`, `appendonly no` and no volume, as one more quadlet unit.
  With no swap, that is RAM-only.
  - The onion's Flask-Limiter buckets use it as a second storage.
  - If it is unreachable, the onion **login and registration** guard fails closed (503, "try again
    shortly"). Browsing the onion does not depend on it.
- **Celery tasks** started by an onion request carry what they carry today. They must not record that the
  request came over the onion: no onion flag, no request host.
- **Daily counts** are counted in-process by a log handler at emit time, in the app and Celery units, so
  nothing is counted twice.
  - A kind is either the exception class plus the innermost app frame (file:line), or, for a WARNING or
    ERROR line, the logger call's file:line and level.
  - gunicorn and Caddy lines are not counted.
  - The split is per day only. A comparison within the last hour reads the journal.
  - A systemd timer deletes rows older than 21 days (prod has no Celery beat).
- **Error emails for an onion request** keep only the exception class and the app traceback frames
  (file:line and function). There is no `str(exc)`, no locals and no request context.
- **The journal** uses `Storage=volatile`, `MaxRetentionSec=1h`, and a `RuntimeMaxUse` cap the builder sizes
  (about 512 MB). If the cap is reached first, the window shrinks. fail2ban's 10-minute `findtime` must
  still fit.
- **The clearnet fail2ban lines stay byte-identical**, and a test asserts it.
- **The file log handler** is removed everywhere, dev and tests included.
- **Rollout** is runbook text in DEPLOY_GUIDE, plus idempotent scripts where they help.
  - Settings change first (volatile journal, rsyslog, fail2ban), then the deletions, each confirmed with
    the owner.
  - A ban-coverage gap of a few minutes during the switch is acceptable.
- **The email recommendation** is text only. Email stays as the registration settings have it.

## 6. Left to the builder

- The RAM-only store: a second Redis with persistence off, or another shared RAM store reachable by every
  process that reads the keys.
- The daily-count mechanism and storage (goal 5).
- How the cache directory sits on the LUKS volume.
- The journald, rsyslog and fail2ban settings that give the 1 h RAM-only window.
- The exact wording of the email recommendation.
