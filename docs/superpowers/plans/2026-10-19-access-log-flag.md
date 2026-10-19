# Access-log flag Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Gunicorn's access log is off unless `ACCESS_LOG` turns it on, and fail2ban's register and scan jails read two app log lines instead.

**Architecture:** `gunicorn.conf.py` reads the flag at startup. The app writes `auth: register attempt ip=<ip>` for each POST to the register page and `scan: probe refused ip=<ip>` for a 4xx answer to a probe path, through the same client formatting `log_login_failure` uses, so onion requests log `ip=onion`. The local-only fail2ban filters switch to those lines.

**Tech Stack:** gunicorn, Flask, fail2ban (systemd backend), pytest via `./run_tests.sh`.

**Spec:** `docs/superpowers/specs/2026-10-19-per-domain-identity-design.md` §3.7 (plan A of three; B is `2026-10-19-per-domain-identity.md`, C follows B).

## Global Constraints

- Default is OFF: no `ACCESS_LOG`, or any value other than `1`, `true`, `yes`, `on` (case-insensitive), means no access log.
- Event lines match the existing style exactly: `auth: login failed ip=%s` at WARNING, never a user name or email.
- An onion request logs `ip=onion`; an address with anything outside `[0-9a-fA-F:.]` logs `ip=invalid` (`LOGGABLE_IP_RE`, `app/utils.py:3589`).
- Probe paths, from the current scan filter: a path starting `/wp-`, `/.env`, `/phpmyadmin`, `/cgi-bin` or `/xmlrpc.php`, answered 4xx.
- TDD, one commit per task, 100% changed-line and branch coverage, gates from repo tools (`./run_tests.sh`), inline imports only if technically unavoidable. Slow tests are bugs.
- `deploy/` is git-ignored and local-only. Back up server files to `~/backups` before editing. The owner pushes.

## Review Focus

1. A POST to `/auth/register` that fails form validation still logs one attempt line (the old jail counted every POST).
2. A probe path answered 200 or 3xx logs nothing; `/wp-` as a substring later in a path (`/c/wp-fans`) logs nothing.
3. `ACCESS_LOG=0`, `ACCESS_LOG=` and `ACCESS_LOG=false` all mean off; `ACCESS_LOG=TRUE` means on.
4. An onion register POST or probe logs `ip=onion`, so neither jail can ban the proxy.
5. `/inbox` traffic never produces either line (both filters keep their `ignoreregex`).

---

### Task 1: Register and probe event lines

**Files:**
- Modify: `app/utils.py:3589-3611` (`log_login_failure` and neighbours)
- Modify: `app/auth/routes.py:71` (`register`)
- Modify: `app/request_hooks.py:211` (`after_request`)
- Test: `tests/test_login_refusal_log.py` (existing; add cases) and `tests/test_request_hooks.py`

**Interfaces:**
- Produces: `loggable_client(client: str | None = None) -> str` in `app/utils.py` (the `where` computation now inside `log_login_failure`, which calls it); `log_register_attempt() -> None`; `log_probe_refused() -> None`; `PROBE_PATH_RE` (compiled, `re.match` at path start).

- [ ] **Step 1: Write the failing tests**

```python
def test_a_register_post_logs_one_attempt_line(app, caplog):             # tests/test_login_refusal_log.py
    # clearnet requests as tests/test_login_failure_log.py:53 builds them (environ_base=CLEARNET, an address the
    # test picks); the expected ip is that address
    client = app.test_client()
    client.post('/auth/register', data={'user_name': ''})                # invalid form still counts
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith('auth: register attempt')]
    assert lines == ['auth: register attempt ip=127.0.0.1']

def test_a_register_get_logs_nothing(client, caplog):
    client.get('/auth/register')
    assert not [r for r in caplog.records if 'register attempt' in r.getMessage()]

@pytest.mark.parametrize('path', ['/wp-login.php', '/.env', '/phpmyadmin/', '/cgi-bin/x', '/xmlrpc.php'])
def test_a_probe_answered_4xx_logs_one_line(client, caplog, path):       # tests/test_request_hooks.py
    client.get(path)
    assert [r.getMessage() for r in caplog.records if r.getMessage().startswith('scan:')] == \
        ['scan: probe refused ip=127.0.0.1']

def test_a_non_probe_404_and_an_inner_wp_path_log_nothing(client, caplog):
    client.get('/no-such-page'); client.get('/c/wp-fans')
    assert not [r for r in caplog.records if r.getMessage().startswith('scan:')]

def test_onion_requests_log_ip_onion(app, onion, caplog):
    # `onion` fixture and onion_get/onion_post helpers: tests/test_onion_guard_limits.py:70,129,134
    client = app.test_client()
    onion_post(client, '/auth/register', data={}); onion_get(client, '/.env')
    assert {m for m in (r.getMessage() for r in caplog.records) if 'ip=' in m and ('register' in m or 'scan' in m)} \
        == {'auth: register attempt ip=onion', 'scan: probe refused ip=onion'}
```

Add one test that a probe path answered 200 (monkeypatch a route or `app.add_url_rule` in the test) logs nothing.

- [ ] **Step 2: Run tests to verify they fail**

Run: `./run_tests.sh tests/test_login_refusal_log.py tests/test_request_hooks.py -q`
Expected: the new tests FAIL (no such lines).

- [ ] **Step 3: Implement**

Extract `loggable_client` from `log_login_failure` unchanged in behaviour. `log_register_attempt` logs `'auth: register attempt ip=%s'` at WARNING and is called once at the top of `register()` when `request.method == 'POST'`. `log_probe_refused` logs `'scan: probe refused ip=%s'`; `after_request` calls it when `400 <= response.status_code < 500` and `PROBE_PATH_RE.match(request.path)`, with `PROBE_PATH_RE = re.compile(r'/(?:wp-|\.env|phpmyadmin|cgi-bin|xmlrpc\.php)')`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./run_tests.sh tests/test_login_refusal_log.py tests/test_request_hooks.py -q`
Expected: PASS, including the existing `log_login_failure` tests.

- [ ] **Step 5: Commit**

```bash
git add app/utils.py app/auth/routes.py app/request_hooks.py tests/test_login_refusal_log.py tests/test_request_hooks.py
git commit -m "feat: registration attempts and refused probes log one line each for fail2ban"
```

### Task 2: `ACCESS_LOG` flag

**Files:**
- Modify: `gunicorn.conf.py:16-17`
- Modify: `env.docker.sample` (document `ACCESS_LOG`, commented, default off)
- Test: `tests/test_gunicorn_conf.py` (create)

**Interfaces:**
- Produces: `access_log_enabled(value: str | None) -> bool` defined in `gunicorn.conf.py`; `accesslog = '-' if access_log_enabled(os.environ.get('ACCESS_LOG')) else None`.

- [ ] **Step 1: Write the failing tests**

```python
import runpy, pathlib
CONF = pathlib.Path(__file__).parent.parent / 'gunicorn.conf.py'

@pytest.mark.parametrize('value,expected', [(None, None), ('', None), ('0', None), ('false', None), ('no', None),
                                            ('1', '-'), ('true', '-'), ('TRUE', '-'), ('yes', '-'), ('on', '-')])
def test_the_access_log_follows_the_flag(monkeypatch, value, expected):
    monkeypatch.delenv('ACCESS_LOG', raising=False)
    if value is not None:
        monkeypatch.setenv('ACCESS_LOG', value)
    assert runpy.run_path(str(CONF))['accesslog'] == expected

def test_the_error_log_stays_on(monkeypatch):
    monkeypatch.delenv('ACCESS_LOG', raising=False)
    assert runpy.run_path(str(CONF))['errorlog'] == '-'
```

- [ ] **Step 2: Run to verify failure** — `./run_tests.sh tests/test_gunicorn_conf.py -q`; expected FAIL (`accesslog` is `'-'`).
- [ ] **Step 3: Implement** the flag in `gunicorn.conf.py` (one `import os` at top) and the commented `# ACCESS_LOG=1  # gunicorn access log, for debugging only; off by default` line in `env.docker.sample`.
- [ ] **Step 4: Run to verify pass** — same command; PASS.
- [ ] **Step 5: Commit**

```bash
git add gunicorn.conf.py env.docker.sample tests/test_gunicorn_conf.py
git commit -m "feat: the gunicorn access log is off unless ACCESS_LOG turns it on"
```

### Task 3: fail2ban filters and deploy (controller, local-only `deploy/`, after the owner pushes)

**Files:**
- Modify: `deploy/fail2ban/filter.d/pyfedi-register.conf`, `deploy/fail2ban/filter.d/pyfedi-scan.conf`
- Modify: `deploy/DEPLOY_GUIDE.md` §11 fail2ban table, and the relay-diagnosis note that needs `ACCESS_LOG=1`

- [ ] **Step 1:** Set `failregex = auth: register attempt ip=<ADDR>$` and `failregex = scan: probe refused ip=<ADDR>$`, keeping `journalmatch`, `datepattern` and `ignoreregex` as they are (as `pyfedi-login.conf` does).
- [ ] **Step 2:** On the server, back up `/etc/fail2ban/filter.d/pyfedi-{register,scan}.conf` to `~/backups/<date>-access-log/`, install the new filters, and check each with `sudo fail2ban-regex systemd-journal /etc/fail2ban/filter.d/<name>.conf` after one `curl -s -o /dev/null https://hell.cloud/.env` and one register POST from the server. Expected: 1 matched line each, 0 for `ip=onion`.
- [ ] **Step 3:** `sudo systemctl reload fail2ban`; `sudo fail2ban-client status pyfedi-register pyfedi-scan` shows both jails active.
- [ ] **Step 4:** After the autodeploy restart, `sudo journalctl -u pyfedi-web --since -5min -o cat | grep -c '" [0-9][0-9][0-9] '` is 0 (no access lines).
- [ ] **Step 5:** Update DEPLOY_GUIDE §11 (jail table: matches the two new lines) and the memories `hellcloud-quadlet-deploy` and `relay-delivery-diagnosis` (access lines need `ACCESS_LOG=1` in `.env.docker`, regenerate `.env.quadlet`, restart web).
