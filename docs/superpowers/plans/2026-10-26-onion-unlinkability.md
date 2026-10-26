# Onion Unlinkability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nothing the server keeps links a user, IP or email to onion activity, and raw logs live one hour in RAM.

**Architecture:**
- Per-request writes that would record onion use are skipped at their source: `audit_field`, `last_seen`, and the onion-only log lines.
- Onion state moves to a second Redis that never persists.
- Logging loses its file handler. An error mail for an onion request is scrubbed. A log handler counts errors per day into a DB table, which sits on the LUKS volume.
- The deploy side (journald, rsyslog, fail2ban, new unit, cache location) is a controller task run on prod.

**Tech Stack:** Flask, SQLAlchemy/Alembic, Postgres, Redis (redis-py, fakeredis in tests), Flask-Limiter, Celery, systemd/journald, rootful podman quadlets, fail2ban.

**Spec:** `docs/superpowers/specs/2026-10-26-onion-unlinkability-design.md`

## Global Constraints

- "nothing the server keeps may connect any user, IP address or email address with onion activity."
- "Onion state is RAM-only and gone at reboot." The one exception is the media cache, which goes on the LUKS volume with a 5 GiB cap.
- `audit_field` on an onion request "skips the write, and writes no NULL".
- Onion-registered accounts: "Every write of `User.ip_address` for such an account stores `169.254.88.88`". Their `ip_address_country` stays NULL, and no ipinfo lookup is made.
- "Onion traffic does not update `User.last_seen`."
- Raw logs: 1 h, RAM-only. Daily counts: "no IP, user name, email, URL path, query, host name or activity id", kept 21 days.
- IP bans: "Leave existing ip ban feature as-is."
- The clearnet fail2ban lines stay byte-identical: `auth: login failed ip=<ip>`, `auth: register attempt ip=<ip>`, `scan: probe refused ip=<ip>`.
- The first domain's behaviour, output and federation do not change.
- Project rules:
  - TDD, one commit per task or defect;
  - 100% changed-line and branch coverage: `python tests/check_changed_line_coverage.py coverage.json <BASE> --branches`;
  - inline imports only if technically unavoidable;
  - slow tests are bugs;
  - invalid client input stays a 400;
  - no file names the first domain (`tests/test_no_first_domain_literal.py`);
  - fuzz targets that reach the registry stub `storefront.domain_rows`.
- Tests run with `./run_tests.sh <files>`. Check `pgrep -a -x podman | grep pyfedi_test-runner` first. Never two runs at once, never kill one, no `timeout` wrapper. The full gate is a bare `./run_tests.sh -q --cov=app --cov-branch --cov-report=json:coverage.json -p no:randomly`.

## Review Focus

1. **A clearnet account with a stored IP logs in over the onion, and later over the clearnet.** The first login leaves the IP untouched. The clearnet login updates it as today. Task 1 pins this.
2. **An onion-registered account logs in from the clearnet through OAuth or the API.** Every login path writes the sentinel, not only the web form. Task 1 tests each writer: `auth/util.py`, `auth/oauth_util.py`, `shared/auth.py`, `shared/post.py`, `shared/reply.py`, `post/routes.py`.
3. **The onion Redis is down mid-request.** Onion login and registration answer 503. Browsing the onion, and every clearnet route, still work. Task 3 pins this.
4. **The counting handler is entered recursively.** It must not log through the logger it counts, and a DB error inside it must not raise into the request. Task 5 pins this.
5. **The media cache directory is absent or unwritable.** Onion pages render, media links fall back as for a cache miss, and nothing 500s. Task 7 pins this.

---

### Task 1: `audit_field` skips on the onion; onion accounts get the sentinel IP

**Files:**
- Modify: `app/onion.py` (`audit_field`, ~:100)
- Modify: every caller (`app/auth/util.py:348,354,377,700,703`, `app/auth/oauth_util.py:139,140,259,260`, `app/shared/auth.py:51,52`, `app/shared/post.py:342`, `app/shared/reply.py:243`, `app/post/routes.py:1095`, `app/activitypub/routes.py:981`)
- Modify: `app/auth/util.py` (`get_country`, ~:92; the ipinfo call ~:60)
- Modify: `app/shared/tasks/users.py:30-60` (ban-check IP leg)
- Create: an alembic migration that sets `ip_address='169.254.88.88', ip_address_country=NULL` for every `user` row whose `site_domain_id` is an `is_onion` `site_domain` row
- Test: `tests/test_onion_unlinkable_ip.py`

**Interfaces:**
- Produces:
  - `ONION_ACCOUNT_IP = '169.254.88.88'` in `app/onion.py`.
  - `is_onion_account(user) -> bool` in `app/onion.py`: true when `domain_by_id(user.site_domain_id).is_onion`.
  - `audit_ip(user, value: str | None) -> str | object` and `audit_country(user, value) -> str | object` in `app/onion.py`. Each returns `KEEP` (a module sentinel) on an onion request, `ONION_ACCOUNT_IP` or `None` for an onion account, and `value` otherwise.
  - `apply_audit(obj, field: str, result) -> None` sets the attribute unless `result is KEEP`.
  - At user creation, where there is no previous value, `KEEP` means the column default (NULL).
  - `audit_field` is kept for `Instance.ip_address` and gets the same `KEEP` semantics.

- [ ] **Step 1:** Write the failing tests:
  - `test_onion_login_keeps_a_clearnet_accounts_ip_and_country`: the user has `ip_address='203.0.113.7'` and country `'NZ'`. After an onion login, a post, a reply and an API login, both values are unchanged.
  - `test_clearnet_login_still_updates_the_ip`.
  - `test_onion_registered_account_always_has_the_sentinel`: register on the onion, then a clearnet login, OAuth login, API login, post and reply. Assert `ip_address == '169.254.88.88'` and `ip_address_country is None` each time.
  - `test_no_ipinfo_lookup_for_an_onion_account`: patch the ipinfo fetch and assert it is not called.
  - `test_ban_check_sends_no_ip_for_an_onion_account`.
  - `test_migration_sets_existing_onion_accounts_to_the_sentinel`.
  - `test_instance_ip_unchanged_by_an_onion_request`.
- [ ] **Step 2:** Run them, and see them fail.
- [ ] **Step 3:** Implement the interfaces above. Route every listed caller through `audit_ip` / `audit_country` / `apply_audit`.
- [ ] **Step 4:** Run the focused tests, `tests/test_onion*.py` and the auth tests. All pass.
- [ ] **Step 5:** Commit `fix: an onion request never rewrites a stored IP, and an onion account's IP is a fixed sentinel (project 5)`.

### Task 2: Onion traffic leaves no activity stamps

**Files:**
- Modify: `app/request_hooks.py:~236` (`last_seen`, `email_unread_sent`)
- Modify: the other `last_seen` writers: `app/utils.py:4421`, `app/auth/util.py:384,699`, `app/api/alpha/utils/user.py:651`, `app/models.py:4366,5039`
- Test: `tests/test_onion_no_activity_stamp.py`

**Interfaces:**
- Produces: `stamp_activity(user) -> None` in `app/onion.py`. Unless `is_onion_request()`, it sets `last_seen = utcnow()` and `email_unread_sent = False`. The spec names both. Every writer above calls it.
- Search for any other per-request "last activity" stamp and route it through `stamp_activity` too, e.g. `grep -rn "= utcnow()" app/request_hooks.py app/utils.py`. List what you find in the report.

- [ ] **Step 1:** Write the failing tests:
  - `test_onion_page_view_vote_and_api_call_leave_last_seen_unchanged`: freeze the time and compare before and after.
  - `test_clearnet_request_still_stamps_last_seen`.
  - `test_onion_login_leaves_last_seen_unchanged`.
  - `test_onion_request_leaves_email_unread_sent_unchanged`.
- [ ] **Step 2:** Run them, and see them fail.
- [ ] **Step 3:** Implement `stamp_activity` and route every writer above through it.
- [ ] **Step 4:** Run the focused tests and the NodeInfo tests. All pass.
- [ ] **Step 5:** Commit `fix: onion traffic never moves an account's last_seen (project 5)`.

### Task 3: Onion state lives in a RAM-only Redis

**Files:**
- Modify: `config.py`. Add `ONION_REDIS_URL = os.environ.get('ONION_REDIS_URL') or 'redis://localhost:6379/2'`. Test config uses fakeredis as the other clients do.
- Modify: `app/__init__.py`. Add `onion_redis_client` next to `redis_client` (~:170, :343). The limiter gets a second storage for onion keys, `limiter.onion` or the builder's equivalent, so the `onion-anon` and `onion-user:<id>` buckets never reach `CACHE_REDIS_URL`.
- Modify: `app/onion_guard.py`. Every `app_pkg.redis_client` becomes `app_pkg.onion_redis_client`: `_login_key`, `_account_key`, `_spend`, `onion_wait_current`, `reserve_onion_login_attempt`, `clear_onion_login_failures`.
- Modify: `app/onion_media.py` (`BYTES_KEY`) and `app/onion_media_fill.py`. Both move to the onion client.
- Modify: per-user keys an onion request would touch: `votes_cast_<date>_<id>` (`app/models.py:4348-4350, 5026-5028`), `ai_detection:user:<id>` (`:3739, 4709`), `em-dash_used_by_<author>` (`:4681`). On an onion request, use the onion client.
- Test: `tests/test_onion_ram_store.py`

**Interfaces:**
- Produces:
  - `app_pkg.onion_redis_client`.
  - `onion_store_unavailable()`: a 503 response, "Please try again shortly." (translatable). Onion login, registration and the PoW/wait endpoints return it when the onion client raises `RedisError`.

- [ ] **Step 1:** Write the failing tests:
  - `test_onion_crawl_writes_nothing_to_the_main_redis`. Use two separate fakeredis servers. Crawl the onion's index, community, post, user, login and register pages, plus a failed login, a PoW spend, an API login and a vote. Assert that the main server's key set is unchanged, except `celery*`/kombu keys.
  - `test_onion_guard_keys_are_in_the_onion_store`.
  - `test_onion_login_is_503_when_the_onion_store_is_down`.
  - `test_onion_browsing_works_when_the_onion_store_is_down`.
  - `test_clearnet_login_unaffected_when_the_onion_store_is_down`.
  - `test_clearnet_rate_limit_keys_still_in_the_main_redis`.
- [ ] **Step 2:** Run them, and see them fail.
- [ ] **Step 3:** Implement. Audit every `redis_client` / `cache` write reachable from an onion request: `grep -rn "redis_client\|cache.set\|cache.delete" app`. Each key is either moved to the onion client, or skipped on the onion. List each key and its decision in the report.
- [ ] **Step 4:** Run the focused tests and `tests/test_onion*.py`. All pass.
- [ ] **Step 5:** Commit `feat: onion state lives in a Redis that never touches disk (project 5)`.

### Task 4: Logs carry no onion trace and no file copy

**Files:**
- Modify: `app/__init__.py:466-477`. Remove the `RotatingFileHandler` and the `logs` mkdir. `app.logger` keeps INFO, and Flask's stderr handler goes to the journal.
- Modify: `app/utils.py:3700-3720`. When `loggable_client()` is `'onion'`, the three fail2ban lines are not written. Clearnet output stays byte-identical.
- Modify: `app/__init__.py:445-464`. Attach an `OnionScrubFilter` to the `SMTPHandler`. Install a Flask `got_request_exception` / `log_exception` override that logs `Exception on <path>` only for non-onion requests.
- Create: `app/log_scrub.py`
- Test: `tests/test_onion_logging.py`

**Interfaces:**
- Produces: `class OnionScrubFilter(logging.Filter)` in `app/log_scrub.py`. Its `filter(record)` rewrites, in a request context with `is_onion_request()`:
  - `record.msg` becomes the exception class name, or `'error on an onion request'`;
  - `record.args` becomes `()`;
  - `record.exc_text` becomes the frames only, as `File "<app path>", line N, in <func>`, with no source lines and no exception message.
  - It always returns True.
- Clearnet records pass unchanged.

- [ ] **Step 1:** Write the failing tests:
  - `test_onion_login_failure_writes_no_log_line`;
  - `test_clearnet_fail2ban_lines_are_byte_identical`: use caplog and compare exact strings for all three lines;
  - `test_onion_error_mail_has_no_path_host_query_user_or_message`: raise inside an onion request with a user name in the exception text, and capture the SMTPHandler's formatted record;
  - `test_clearnet_error_mail_unchanged`;
  - `test_no_file_log_handler`: assert there is no `RotatingFileHandler` on `app.logger` and no `logs/` created.
- [ ] **Step 2:** Run them, and see them fail.
- [ ] **Step 3:** Implement. Audit the `current_app.logger` calls an onion request reaches that log a user name, user id or email: `app/auth/*`, `app/onion*.py`, `app/user/*`, the API. Drop each one, or log it without the identifier on the onion. List them in the report.
- [ ] **Step 4:** Run the focused tests and the logging, auth and onion tests. All pass.
- [ ] **Step 5:** Commit `fix: an onion request leaves no log line naming anyone, and logs stay off disk (project 5)`.

### Task 5: IP-free daily error counts, 21 days

**Files:**
- Create: `app/error_counts.py`
- Modify: `app/models.py`. Add `ErrorCount`: table `error_count`, `day` Date, `unit` String(32), `kind` String(256), `level` String(8), `count` Integer. PK (`day`, `unit`, `kind`, `level`).
- Create: an alembic migration
- Modify: `app/__init__.py`. Attach `ErrorCountHandler` to `app.logger` at WARNING. The Celery worker init attaches it too, so it lands in `celery_worker.py` or `app/__init__.py`, wherever Celery's app is built.
- Modify: `app/cli.py`. Add `flask error-counts [--days N]`, which prints `day unit level count kind` sorted, and `flask prune-error-counts`, which deletes `day < today - 21`.
- Create: `deploy/systemd/pyfedi-job@prune_error_counts.timer` (daily). deploy/ is git-ignored; the controller installs it in Task 8.
- Test: `tests/test_error_counts.py`

**Interfaces:**
- Produces:
  - `error_kind(record) -> str`. With `exc_info`: `"<ExceptionClass> at <app-relative path>:<lineno of the innermost app/ frame>"`. Otherwise: `"<app-relative pathname>:<lineno>"`. Never message text.
  - `class ErrorCountHandler(logging.Handler)`. It upserts `count = count + 1` with its own engine connection, never `db.session`. `unit` comes from `PIEFED_UNIT`, else `web`. Every exception inside `emit` is swallowed, and the handler sets a thread-local guard so it never re-enters itself.

- [ ] **Step 1:** Write the failing tests:
  - `test_kind_has_no_message_ip_path_or_name`: log `'auth: login failed ip=203.0.113.7'` and an exception whose message holds a user name and a URL. Assert the stored kinds hold neither and match the format above.
  - `test_same_kind_twice_counts_two`.
  - `test_handler_never_raises_when_the_db_is_down`.
  - `test_handler_does_not_reenter_on_its_own_failure`.
  - `test_prune_deletes_older_than_21_days_only`.
  - `test_error_counts_cli_prints_sorted`.
  - `test_info_lines_are_not_counted`.
- [ ] **Step 2:** Run them, and see them fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the focused tests and the CLI tests. All pass.
- [ ] **Step 5:** Commit `feat: daily error counts with nothing personal in them, kept 21 days (project 5)`.

### Task 6: Onion registration recommends a private email

**Files:**
- Modify: `app/templates/auth/register.html`. Next to the email field, on the onion only (`current_storefront().is_onion`), add `{{ _('We recommend registering with ProtonMail or another privacy-preserving email service.') }}` as plain text, with no link.
- Test: `tests/test_onion_register_email_hint.py`

- [ ] **Step 1:** Write the failing tests: `test_onion_register_page_recommends_a_private_email` and `test_first_domain_register_page_has_no_recommendation`.
- [ ] **Step 2:** Run them, and see them fail.
- [ ] **Step 3:** Implement. Run `pybabel extract` the way the repo does it for new strings, if the repo commits the `.pot`. Check `git log -- app/translations` first.
- [ ] **Step 4:** Run the focused tests and the registration tests. All pass.
- [ ] **Step 5:** Commit `feat: the onion's sign-up page recommends a privacy-preserving email (project 5)`.

### Task 7: Media cache cap 5 GiB, safe when its directory is absent

**Files:**
- Modify: `app/cli.py:1608`, where the default `onion_media_cache_bytes` changes from `10737418240` to `5368709120`, plus any other reader of that setting.
- Modify: `app/onion_media.py` and `app/onion_media_fill.py`. An absent or unwritable `MEDIA_PROXY_DIR` means a cache miss and a refused fill, logged once at WARNING with no URL. It is never a 500, and the code never `mkdir`s a parent.
- Test: `tests/test_onion_media_absent_dir.py`

- [ ] **Step 1:** Write the failing tests:
  - `test_default_cache_cap_is_5_gib`;
  - `test_onion_page_renders_when_the_cache_dir_is_absent`;
  - `test_fill_refused_without_error_when_the_dir_is_absent`;
  - `test_evict_with_absent_dir_frees_nothing_and_does_not_raise`.
- [ ] **Step 2:** Run them, and see them fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the focused tests and the onion media tests. All pass.
- [ ] **Step 5:** Commit `fix: the onion media cache caps at 5 GiB and copes with its volume locked (project 5)`.

Then run the full bare gate and the coverage check against the plan's BASE.

### Task 8 (controller, on prod, after merge and BEFORE the owner pushes): deploy settings

All of this is local `deploy/` plus prod. Back up every unit and env file first. Confirm each deletion with the owner.

- [ ] **Step 1: RAM Redis.**
  - Create `deploy/quadlet/pyfedi-redis-onion.container`: image as `pyfedi-redis`, command `redis-server --save "" --appendonly no --maxmemory 64mb --maxmemory-policy volatile-lru`, no volume, on `pyfedi.network`.
  - Add `ONION_REDIS_URL=redis://pyfedi-redis-onion:6379/0` to `.env.docker` and regenerate `.env.quadlet`.
  - Install, `daemon-reload`, start.
  - Check from the web container that `redis-cli -u $ONION_REDIS_URL ping` answers `PONG`.
- [ ] **Step 2: Media cache on LUKS.**
  - Create `/home/rocky/pyfedi/pgdata-media`: a second filesystem image file on the LUKS volume, or another LV on the same LUKS device. Check its layout first, and never put the cache inside `PGDATA`.
  - Point `MEDIA_PROXY_DIR` at it in both the web and Caddy units (`/srv/media_proxy`).
  - Set the `onion_media_cache_bytes` setting to `5368709120`.
  - Add the unlock step to the DEPLOY_GUIDE §LUKS runbook.
- [ ] **Step 3: Logs.**
  - Add the journald drop-in `/etc/systemd/journald.conf.d/50-pyfedi.conf` with `Storage=volatile`, `MaxRetentionSec=1h` and `RuntimeMaxUse=512M`, then restart journald.
  - Set rsyslog so `/var/log/messages` drops container and pyfedi output: a filter in `/etc/rsyslog.d/10-no-pyfedi.conf`, or `ForwardToSyslog=no`. Pick the setting that also keeps sshd and cron logging to the journal.
  - fail2ban: `logtarget = SYSTEMD-JOURNAL`, `dbpurgeage = 1h`.
  - Make sure the jails read the journal backend. Check each jail with `fail2ban-regex` and one real test ban from a throwaway host.
- [ ] **Step 4: Timer.** Install `pyfedi-job@prune_error_counts.timer` and enable it.
- [ ] **Step 5: Owner push**, then autodeploy. The migrations run: the sentinel and `error_count`.
- [ ] **Step 6: Deletions** (owner confirms each):
  - `/var/log/journal/*`;
  - `/var/log/messages*`;
  - `/var/log/fail2ban.log*`;
  - `~/pyfedi/logs/`;
  - the old `~/pyfedi/media_proxy/`;
  - the onion keys in the main Redis: SCAN and DEL the prefixes Task 3's report lists, then `BGSAVE`;
  - old `dump.rdb` copies.
  - Shred them where shredding means anything.
- [ ] **Step 7: Verify.**
  - The identity docs are unchanged (DEPLOY_GUIDE §11 set).
  - `swapon --show` is empty.
  - No onion key in the main Redis after 15 min of onion use.
  - `journalctl --disk-usage` is under /run only.
  - `flask error-counts` shows today's rows.
  - The onion account rows hold the sentinel.
  - Onion login works.
  - A clearnet test ban works.
  - Tracebacks: no new kinds.
- [ ] **Step 8: DEPLOY_GUIDE.**
  - §11 notes: the 1 h RAM journal, the counts replacing week searches, the RAM Redis, the media cache on LUKS, "swap must stay off or be encrypted", and "never IP-ban 169.254.88.88: it is every onion account".
  - Update memory.
