# Native Federation Steps 0–1 (Interop Harness + Microblogs) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a local PieFed + Mastodon interop harness. Then make posts in the local `microblogs` community federate as Mastodon-native `Note`s, with the same object pushed and fetched, and with replies, likes and boosts coming back.

**Architecture:**
- A new package `app/activitypub/serializers/` holds `microblog_note(post, mentions)`.
- `post_to_page` (every fetch, outbox, and Tombstone `formerType` path) delegates to it for microblog posts. `send_post` also delegates, through a new `send_microblog_note`. This puts push and fetch on one builder.
- The harness is `compose.interop.yaml` plus a top-level `interop/` pytest tree. Caddy serves both apps on `*.test` names with certificates from its internal CA.

**Tech Stack:**
- App: Flask, SQLAlchemy, Celery.
- Tests: pytest + respx (unit), httpx (interop).
- Harness: podman-compose, Caddy 2, Mastodon v4.5.

**Spec:** `docs/superpowers/specs/2026-10-14-native-federation-design.md` (as amended by §"Spec amendments" below, which Task 0 writes into the spec).

## Global Constraints

- R9: 100% line and branch coverage on every line this work adds or changes under `app/`. Gate: `python tests/check_changed_line_coverage.py coverage.json main --branches` reports zero misses.
- Every new `app/` module gets a floor of `100` in `coverage_floors.ini`.
- Coverage counts the unit suite (`./run_tests.sh`) only. Interop tests never count toward it.
- TDD: each behaviour gets a failing test first, then the code.
- One commit per task. Commit messages use Conventional Commits and end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Gates and tools live in the repo. Never use `/tmp` scripts.
- A slow test is a bug. Interop polling is bounded at 30 s per wait.
- Unit tests run only via `./run_tests.sh <args>`. The host has no Python environment.
- Existing comments in edited files stay as they are unless the task changes the code they describe.

## Spec amendments (written into the spec by Task 0)

- **A1 (§4.1):** there is no separate `serialize_post`.
  - `post_to_page` stays the single fetch-side entry point and returns `microblog_note(...)` for microblog posts.
  - `send_post` branches to `send_microblog_note`.
  - The existing `Page`/`Question`/`Event` code stays where it is and is not moved. Its push and fetch versions already differ (D298–D300), and fixing that is outside this feature.
  - "Push equals fetch" applies to the new builders.
- **A2 (§4.2):** local microblog posts carry a title the user typed, because the form requires one. The Note `content` starts with the title as its own paragraph, except when the body's text already starts with the title. A link or video-link post's title paragraph links to `post.url`. The duplication the spec feared happens only when the title was derived from the body, which this test catches.
- **A3 (§3.1):** `Community.kind` lands in step 4, where it is first used. Step 1 keys on the existing `is_local_microblogs(community)`.
- **A4 (§8.1):** the harness tests live in a top-level `interop/` directory, not `tests/interop/`. `tests/conftest.py` blocks all outbound HTTP for everything under `tests/`, and the interop tests need real HTTP.
- **A5 (§7):** new env `ALLOWED_PRIVATE_NETWORKS`, a comma-separated list of CIDRs, empty by default. `is_refused_address` admits addresses inside these networks. Only the harness sets it, so containers can reach each other on the podman network. Production leaves it unset.
- **A6 (§6.1):** for microblogs, the author's `Create`/`Update` goes to every follower and mention instance, whether or not that instance also gets the community's `Announce`. Receivers dedupe by object id. Today's cross-dedupe can leave a Mastodon follower with only a boost.

## Review Focus

1. **Body that repeats the title:** a title equal to the first words of the body (including HTML entities such as `&amp;`) must appear once, not twice. *Test: Task 4, `test_title_already_leading_body_is_not_repeated`.*
2. **Title with markup characters:** `<`, `>` and `&` in a title or URL must be escaped, never injected. *Test: Task 4, `test_title_and_url_are_escaped`.*
3. **Followers-only fetch:** a followers-only microblog fetched by an entitled signer must carry exactly the pushed addressing, with no Public, `searchableBy` or `audience`. *Test: Task 5, `test_followers_only_microblog_fetch_matches_push_addressing`.*
4. **Edit before any follower exists:** editing a microblog whose author has no followers and no mentions sends nothing and does not crash. *Test: Task 6, `test_microblog_edit_with_no_audience_sends_nothing`.*
5. **Poll or event in microblogs:** a poll or event posted in `/c/microblogs` keeps its `Question`/`Event` shape. *Test: Task 4, `test_polls_and_events_keep_their_own_builder`.*

## File Structure

| Path | Responsibility |
|---|---|
| `app/pinned_http.py` (modify) | `ALLOWED_PRIVATE_NETWORKS` parse; `is_refused_address` admits them |
| `app/activitypub/serializers/__init__.py` (create) | Package doc; re-exports |
| `app/activitypub/serializers/microblog.py` (create) | `uses_microblog_note`, `note_content`, `note_addressing`, `note_attachments`, `microblog_note` |
| `app/activitypub/util.py` (modify `post_to_page`) | Delegate to `microblog_note` |
| `app/shared/tasks/pages.py` (modify) | `send_microblog_note`; `send_post` branches to it |
| `coverage_floors.ini` (modify) | Floors of 100 for the new modules |
| `tests/test_pinned_http_allowed_networks.py` (create) | A5 |
| `tests/test_serializers_microblog.py` (create) | Builder unit and golden tests |
| `tests/fixtures/mastodon_note_v4_5.json` (create) | Captured Mastodon Note, the reference shape |
| `tests/test_microblog_fetch.py` (create) | Fetch path |
| `tests/test_send_microblog_note.py` (create) | Push path and push==fetch |
| `compose.interop.yaml` (create) | Harness services |
| `interop/Caddyfile`, `interop/run.sh`, `interop/gen_env.sh`, `interop/seed_piefed.py`, `interop/seed_mastodon.rb`, `interop/trust_ca.sh` (create) | Harness plumbing |
| `interop/pytest.ini`, `interop/conftest.py`, `interop/clients.py`, `interop/test_smoke.py`, `interop/test_microblog_mastodon.py` (create) | Interop tests |
| `interop/README.md` (create) | How to run |
| `.gitignore` (modify) | `interop/.env.interop` |

---

### Task 0: Write the spec amendments

**Files:**
- Modify: `docs/superpowers/specs/2026-10-14-native-federation-design.md`

- [ ] **Step 1: Add §10 "Amendments (2026-10-14, plan for steps 0–1)"** to the end of the spec, holding amendments A1–A6 verbatim from this plan. In §3.1, §4.1, §4.2, §6.1, §7 and §8.1, add the line `(Amended: see §10 A<n>.)` under each affected heading.

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-10-14-native-federation-design.md
git commit -m "docs: native federation spec amendments A1-A6 from the steps 0-1 plan

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1: `ALLOWED_PRIVATE_NETWORKS` admits harness addresses (A5)

**Files:**
- Modify: `app/pinned_http.py:31-36`
- Test: `tests/test_pinned_http_allowed_networks.py`

**Interfaces:**
- Produces:
  - `app.pinned_http.parse_networks(value: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]`
  - Module attribute `ALLOWED_PRIVATE_NETWORKS: tuple`, read from env at import.
  - `is_refused_address(ip)` returns False for an `ip` inside any of these networks.

- [ ] **Step 1: Write the failing tests**

```python
"""ALLOWED_PRIVATE_NETWORKS (spec 2026-10-14 §10 A5): the interop harness's containers talk over a podman network
of private addresses, which the SSRF guard refuses. An operator-listed network is admitted; nothing else changes."""
import ipaddress

import pytest

from app import pinned_http
from app.pinned_http import is_refused_address, parse_networks


def test_parse_networks_reads_a_comma_separated_list():
    assert parse_networks('10.89.0.0/16, fd00::/8') == (ipaddress.ip_network('10.89.0.0/16'),
                                                         ipaddress.ip_network('fd00::/8'))


def test_parse_networks_of_empty_is_empty():
    assert parse_networks('') == ()
    assert parse_networks(' , ') == ()


def test_parse_networks_refuses_garbage():
    with pytest.raises(ValueError):
        parse_networks('not-a-network')


def test_private_address_refused_by_default(monkeypatch):
    monkeypatch.setattr(pinned_http, 'ALLOWED_PRIVATE_NETWORKS', ())
    assert is_refused_address(ipaddress.ip_address('10.89.0.5'))


def test_listed_network_is_admitted(monkeypatch):
    monkeypatch.setattr(pinned_http, 'ALLOWED_PRIVATE_NETWORKS', parse_networks('10.89.0.0/16'))
    assert not is_refused_address(ipaddress.ip_address('10.89.0.5'))


def test_address_outside_listed_network_still_refused(monkeypatch):
    monkeypatch.setattr(pinned_http, 'ALLOWED_PRIVATE_NETWORKS', parse_networks('10.89.0.0/16'))
    assert is_refused_address(ipaddress.ip_address('10.90.0.5'))
    assert is_refused_address(ipaddress.ip_address('127.0.0.1'))


def test_ipv4_address_against_ipv6_network_is_not_admitted(monkeypatch):
    monkeypatch.setattr(pinned_http, 'ALLOWED_PRIVATE_NETWORKS', parse_networks('fd00::/8'))
    assert is_refused_address(ipaddress.ip_address('10.89.0.5'))


def test_global_address_unaffected(monkeypatch):
    monkeypatch.setattr(pinned_http, 'ALLOWED_PRIVATE_NETWORKS', parse_networks('10.89.0.0/16'))
    assert not is_refused_address(ipaddress.ip_address('8.8.8.8'))
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `./run_tests.sh tests/test_pinned_http_allowed_networks.py -v`
Expected: FAIL with `ImportError: cannot import name 'parse_networks'`

- [ ] **Step 3: Implement it.** Add the following above `is_refused_address` in `app/pinned_http.py`, add `import os` with the other imports, and change `is_refused_address`:

```python
def parse_networks(value: str) -> tuple:
    """The networks a comma-separated CIDR list names. A malformed entry raises ValueError, so a typo in
    ALLOWED_PRIVATE_NETWORKS stops the process at import instead of silently admitting nothing (spec §10 A5)."""
    return tuple(ipaddress.ip_network(part.strip(), strict=False) for part in value.split(',') if part.strip())


# Private networks this instance may fetch from anyway: set only by the interop harness (compose.interop.yaml),
# whose containers reach each other on a podman network. Unset in production.
ALLOWED_PRIVATE_NETWORKS = parse_networks(os.environ.get('ALLOWED_PRIVATE_NETWORKS', ''))


def is_refused_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """(existing docstring unchanged) ... An address inside ALLOWED_PRIVATE_NETWORKS is admitted."""
    if any(ip in network for network in ALLOWED_PRIVATE_NETWORKS):
        return False
    return not ip.is_global or ip.is_reserved or ip.is_multicast or getattr(ip, 'is_site_local', False)
```

Keep the existing docstring text and append the last sentence to it. `ip in network` is False when the IP versions differ, which is what the v4-against-v6 test pins.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `./run_tests.sh tests/test_pinned_http_allowed_networks.py tests/ -k "pinned or refused or invalid_get_request" -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/pinned_http.py tests/test_pinned_http_allowed_networks.py
git commit -m "feat: ALLOWED_PRIVATE_NETWORKS lets the interop harness reach private container addresses

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Interop harness — PieFed + Mastodon behind Caddy

**Files:**
- Create: `compose.interop.yaml`, `interop/Caddyfile`, `interop/gen_env.sh`, `interop/trust_ca.sh`, `interop/seed_piefed.py`, `interop/seed_mastodon.rb`, `interop/run.sh`, `interop/README.md`
- Modify: `.gitignore` (add `interop/.env.interop` and `interop/.tokens.json`)

**Interfaces:**
- Produces:
  - `interop/run.sh [up|test|down] [pytest args...]`. `up` starts the stack and seeds it. `test` runs `up` and then pytest in the `interop-runner` container. `down` removes everything.
  - Seeded identities:
    - PieFed: user `alice`, password `interop-alice-pw`, on `https://piefed.test`. The local `microblogs` community exists.
    - Mastodon: user `bob` on `https://mastodon.test`.
  - Tokens are written to `interop/.tokens.json` as `{"mastodon_bob": "<token>"}`. The runner reads that file.
  - Network subnet: `10.89.0.0/16`. Both apps get `ALLOWED_PRIVATE_NETWORKS` / `ALLOWED_PRIVATE_ADDRESSES=10.89.0.0/16`.

- [ ] **Step 1: Write `interop/Caddyfile`**

```
{
	local_certs
	skip_install_trust
}

piefed.test {
	reverse_proxy piefed-web:5000
}

mastodon.test {
	reverse_proxy mastodon-web:3000
}
```

- [ ] **Step 2: Write `interop/trust_ca.sh`.** It runs inside every app container before the real command. It waits up to 30 s for Caddy's root certificate, then trusts it both system-wide and in certifi, which httpx uses.

```bash
#!/bin/sh
# Trust the harness CA (Caddy's internal root), then exec the container's real command.
set -eu
ROOT=/caddy-data/caddy/pki/authorities/local/root.crt
i=0
while [ ! -s "$ROOT" ]; do
    i=$((i + 1)); [ "$i" -gt 30 ] && { echo "trust_ca: no CA at $ROOT after 30s" >&2; exit 1; }
    sleep 1
done
cat /etc/ssl/certs/ca-certificates.crt "$ROOT" > /tmp/harness-ca.crt
export SSL_CERT_FILE=/tmp/harness-ca.crt
if command -v python >/dev/null 2>&1 && python -c 'import certifi' 2>/dev/null; then
    cat "$ROOT" >> "$(python -c 'import certifi; print(certifi.where())')"
fi
exec "$@"
```

- [ ] **Step 3: Write `interop/gen_env.sh`.** It creates `interop/.env.interop` once and leaves an existing file alone.

```bash
#!/usr/bin/env bash
# Generate interop/.env.interop (gitignored) with fresh secrets. Idempotent.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env.interop ] && exit 0
hex() { openssl rand -hex "$1"; }
cat > .env.interop <<EOF
# PieFed
SERVER_NAME=piefed.test
SECRET_KEY=$(hex 32)
DATABASE_URL=postgresql+psycopg2://piefed:piefed@piefed-db/piefed
CACHE_TYPE=RedisCache
CACHE_REDIS_URL=redis://piefed-redis:6379/0
CELERY_BROKER_URL=redis://piefed-redis:6379/1
ENABLE_ALPHA_API=true
ALLOWED_PRIVATE_NETWORKS=10.89.0.0/16
# Mastodon
LOCAL_DOMAIN=mastodon.test
RAILS_ENV=production
DB_HOST=mastodon-db
DB_USER=mastodon
DB_PASS=mastodon
DB_NAME=mastodon
REDIS_HOST=mastodon-redis
SECRET_KEY_BASE=$(hex 64)
OTP_SECRET=$(hex 64)
ACTIVE_RECORD_ENCRYPTION_DETERMINISTIC_KEY=$(hex 16)
ACTIVE_RECORD_ENCRYPTION_KEY_DERIVATION_SALT=$(hex 16)
ACTIVE_RECORD_ENCRYPTION_PRIMARY_KEY=$(hex 16)
ALLOWED_PRIVATE_ADDRESSES=10.89.0.0/16
EOF
echo "gen_env: wrote interop/.env.interop (VAPID keys are added by run.sh up)"
```

- [ ] **Step 4: Write `compose.interop.yaml`.** Pin each image by digest. Run `podman pull <image>` and then `podman image inspect --format '{{.Digest}}' <image>` for `docker.io/library/caddy:2.10`, `ghcr.io/mastodon/mastodon:v4.5`, `docker.io/library/postgres:17.11` and `docker.io/library/redis:8.10`, and write each digest as `image@sha256:...`.

```yaml
# Interop harness (spec 2026-10-14 §8.1): PieFed built from this tree, and Mastodon, behind Caddy with an internal
# CA on *.test names. Run via interop/run.sh, never by hand. Nothing here is published to the host network.
x-trust: &trust
  volumes:
    - caddy-data:/caddy-data:ro
    - ./interop/trust_ca.sh:/trust_ca.sh:ro
  env_file: ./interop/.env.interop
  networks: [interop]

services:
  caddy:
    image: docker.io/library/caddy:2.10   # pin @sha256 per Step 4
    volumes:
      - ./interop/Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy-data:/data
    networks:
      interop:
        aliases: [piefed.test, mastodon.test]

  piefed-db:
    image: docker.io/library/postgres:17.11
    environment: {POSTGRES_USER: piefed, POSTGRES_PASSWORD: piefed, POSTGRES_DB: piefed}
    tmpfs: [/var/lib/postgresql/data]
    networks: [interop]

  piefed-redis:
    image: docker.io/library/redis:8.10
    networks: [interop]

  piefed-web:
    <<: *trust
    build: {context: ., target: runtime}
    image: localhost/piefed-interop:latest
    entrypoint: ["/trust_ca.sh", "/usr/bin/tini", "--", "/app/entrypoint.sh"]
    depends_on: [caddy, piefed-db, piefed-redis]

  piefed-celery:
    <<: *trust
    image: localhost/piefed-interop:latest
    entrypoint: ["/trust_ca.sh", "/app/entrypoint_celery.sh"]
    depends_on: [piefed-web]

  mastodon-db:
    image: docker.io/library/postgres:17.11
    environment: {POSTGRES_USER: mastodon, POSTGRES_PASSWORD: mastodon, POSTGRES_DB: mastodon}
    tmpfs: [/var/lib/postgresql/data]
    networks: [interop]

  mastodon-redis:
    image: docker.io/library/redis:8.10
    networks: [interop]

  mastodon-web:
    <<: *trust
    image: ghcr.io/mastodon/mastodon:v4.5   # pin @sha256 per Step 4
    entrypoint: ["/trust_ca.sh"]
    command: ["bash", "-c", "bundle exec rails db:prepare && exec bundle exec puma -C config/puma.rb"]
    depends_on: [caddy, mastodon-db, mastodon-redis]

  mastodon-sidekiq:
    <<: *trust
    image: ghcr.io/mastodon/mastodon:v4.5   # pin @sha256 per Step 4
    entrypoint: ["/trust_ca.sh"]
    command: ["bundle", "exec", "sidekiq"]
    depends_on: [mastodon-web]

  interop-runner:
    <<: *trust
    build: {context: ., target: test}
    image: localhost/piefed-interop-runner:latest
    entrypoint: ["/trust_ca.sh"]
    working_dir: /app
    volumes:
      - caddy-data:/caddy-data:ro
      - ./interop/trust_ca.sh:/trust_ca.sh:ro
      - ./interop:/app/interop:ro
    profiles: [runner]

volumes:
  caddy-data:

networks:
  interop:
    ipam:
      config:
        - subnet: 10.89.0.0/16
```

- [ ] **Step 5: Write `interop/seed_piefed.py`.** `run.sh` pipes it into `flask shell` in `piefed-web`. It is idempotent.

```python
# Seed the harness PieFed: site setup, user alice, the local microblogs community. Idempotent.
from app import db
from app.activitypub.signature import RsaKeys
from app.activitypub.util import find_microblogging_community
from app.models import User
from app.utils import gibberish, utcnow
from flask import current_app

if User.query.filter_by(user_name='alice', ap_id=None).first() is None:
    private_key, public_key = RsaKeys.generate_keypair()
    alice = User(user_name='alice', title='alice', email='alice@piefed.test', instance_id=1,
                 private_key=private_key, public_key=public_key, alt_user_name=gibberish(12),
                 email_unread_sent=False, verified=True, last_seen=utcnow())
    alice.set_password('interop-alice-pw')
    url = current_app.config['SERVER_URL']
    alice.ap_profile_id = f'{url}/u/alice'
    alice.ap_public_url = f'{url}/u/alice'
    alice.ap_inbox_url = f'{url}/u/alice/inbox'
    db.session.add(alice)
    db.session.commit()
find_microblogging_community()
print('seed_piefed: ok')
```

- [ ] **Step 6: Write `interop/seed_mastodon.rb`.** `run.sh` runs it with `bin/rails runner`. It prints only the token.

```ruby
# Seed the harness Mastodon: user bob and an API token for him. Idempotent.
account = Account.find_local('bob')
unless account
  owner = User.create!(email: 'bob@mastodon.test', password: 'interop-bob-pw', agreement: true,
                       confirmed_at: Time.now.utc, approved: true, account_attributes: { username: 'bob' })
  account = owner.account
end
app = Doorkeeper::Application.find_or_create_by!(name: 'interop') do |a|
  a.redirect_uri = 'urn:ietf:wg:oauth:2.0:oob'
  a.scopes = 'read write follow'
end
token = Doorkeeper::AccessToken.create!(application: app, resource_owner_id: account.user.id, scopes: 'read write follow')
puts token.token
```

- [ ] **Step 7: Write `interop/run.sh`**

```bash
#!/usr/bin/env bash
# Interop harness (spec 2026-10-14 §8.1).
#   interop/run.sh up            start and seed the stack
#   interop/run.sh test [args]   up, then pytest interop/ in the runner (args pass through)
#   interop/run.sh down          remove the stack and its volumes
set -euo pipefail
cd "$(dirname "$0")/.."
COMPOSE="podman-compose -f compose.interop.yaml"

wait_http() {  # wait_http <service> <url>: up to 120s for a 2xx/3xx from inside the network
    for _ in $(seq 1 120); do
        if $COMPOSE exec -T "$1" sh -c "curl -fsS -o /dev/null --cacert /tmp/harness-ca.crt '$2'" 2>/dev/null; then
            return 0
        fi
        sleep 1
    done
    echo "run.sh: $2 never answered" >&2; return 1
}

up() {
    interop/gen_env.sh
    if ! grep -q '^VAPID_PRIVATE_KEY=' interop/.env.interop; then
        $COMPOSE run --rm --no-deps --entrypoint '' mastodon-web \
            bundle exec rails mastodon:webpush:generate_vapid_key >> interop/.env.interop
    fi
    $COMPOSE build piefed-web
    $COMPOSE up -d
    wait_http piefed-web https://piefed.test/health
    wait_http piefed-web https://mastodon.test/health   # piefed-web has curl; the Mastodon image may not
    $COMPOSE exec -T piefed-web sh -c 'FLASK_APP=pyfedi.py flask init-db --interactive no'
    $COMPOSE exec -T piefed-web sh -c 'FLASK_APP=pyfedi.py flask shell' < interop/seed_piefed.py
    token=$($COMPOSE exec -T mastodon-web bin/rails runner - < interop/seed_mastodon.rb | tail -n 1)
    printf '{"mastodon_bob": "%s"}\n' "$token" > interop/.tokens.json
}

case "${1:-}" in
    up)   up ;;
    test) shift; up; $COMPOSE --profile runner run --rm interop-runner \
              python -m pytest -c interop/pytest.ini interop "$@" ;;
    down) $COMPOSE down -v ;;
    *)    echo "usage: interop/run.sh up|test|down" >&2; exit 2 ;;
esac
```

`chmod +x interop/*.sh`.

- [ ] **Step 8: Bring the stack up and check it by hand**

Run: `interop/run.sh up`
Expected:
- The output ends with `seed_piefed: ok`.
- `interop/.tokens.json` holds a non-empty token.
- `podman-compose -f compose.interop.yaml exec -T piefed-web curl -fsS --cacert /tmp/harness-ca.crt "https://piefed.test/.well-known/webfinger?resource=acct:alice@piefed.test"` returns JSON naming `https://piefed.test/u/alice`. (The Mastodon-to-PieFed direction is proven by the Task 3 smoke test.)

If PieFed refuses Mastodon's addresses, check that `ALLOWED_PRIVATE_NETWORKS` reached the container: `exec piefed-web env | grep ALLOWED`.

- [ ] **Step 9: Write `interop/README.md`.** Cover what the harness is, the three `run.sh` verbs, that it needs podman-compose and about 6 GB RAM, that nothing is published to the host, and how to read logs (`podman-compose -f compose.interop.yaml logs piefed-celery`). Add `interop/.env.interop` and `interop/.tokens.json` to `.gitignore`.

- [ ] **Step 10: Commit**

```bash
git add compose.interop.yaml interop/ .gitignore
git commit -m "test: interop harness runs PieFed and Mastodon behind Caddy with an internal CA

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Interop pytest tree and smoke test (Mastodon follows a PieFed user)

**Files:**
- Create: `interop/pytest.ini`, `interop/conftest.py`, `interop/clients.py`, `interop/test_smoke.py`

**Interfaces:**
- Produces (in `interop/clients.py`):
  - `wait_for(fn, timeout=30.0, interval=0.5)` returns the first truthy `fn()` or raises `AssertionError`.
  - `class Mastodon(token)` with methods:
    - `resolve_account(acct) -> dict`
    - `follow(account_id) -> dict`
    - `relationship(account_id) -> dict`
    - `home() -> list[dict]`
    - `search_status(url) -> dict | None`
    - `status(id) -> dict | None` (None on 404)
    - `reply(status_id, text) -> dict`
    - `favourite(status_id)`
    - `reblog(status_id)`
  - `class PieFed(user, password)` with methods:
    - `microblogs_id() -> int`
    - `post(title, body, community_id, **extra) -> dict`, returning `post_view`
    - `edit(post_id, **fields) -> dict`
    - `delete(post_id)`
    - `get_post(post_id) -> dict`
    - `replies(post_id) -> list[dict]`
  - Fixtures `mastodon`, `piefed`, `bob_follows_alice` (session scoped).

- [ ] **Step 1: Write `interop/pytest.ini`**

```ini
[pytest]
# Interop harness tests (spec 2026-10-14 §8.1). Run only via interop/run.sh test. These talk to real containers;
# tests/conftest.py (which blocks outbound HTTP) does not apply here.
testpaths = interop
timeout = 120
```

- [ ] **Step 2: Write `interop/clients.py`**

```python
"""Thin API clients for the harness apps. Every wait is bounded: a slow interop test is a bug (spec §8.1)."""
import json
import os
import time

import httpx

CA = '/tmp/harness-ca.crt'


def wait_for(fn, timeout=30.0, interval=0.5):
    deadline = time.monotonic() + timeout
    while True:
        result = fn()
        if result:
            return result
        if time.monotonic() > deadline:
            raise AssertionError(f'wait_for: {getattr(fn, "__name__", fn)} never became true in {timeout}s')
        time.sleep(interval)


def tokens() -> dict:
    with open(os.path.join(os.path.dirname(__file__), '.tokens.json')) as f:
        return json.load(f)


class Mastodon:
    def __init__(self, token):
        self.http = httpx.Client(base_url='https://mastodon.test', verify=CA, timeout=10,
                                 headers={'Authorization': f'Bearer {token}'})

    def resolve_account(self, acct):
        r = self.http.get('/api/v2/search', params={'q': acct, 'resolve': 'true', 'type': 'accounts'})
        r.raise_for_status()
        accounts = r.json()['accounts']
        return accounts[0] if accounts else None

    def follow(self, account_id):
        return self.http.post(f'/api/v1/accounts/{account_id}/follow').raise_for_status().json()

    def relationship(self, account_id):
        return self.http.get('/api/v1/accounts/relationships', params={'id[]': account_id}).raise_for_status().json()[0]

    def home(self):
        return self.http.get('/api/v1/timelines/home', params={'limit': 40}).raise_for_status().json()

    def search_status(self, url):
        r = self.http.get('/api/v2/search', params={'q': url, 'resolve': 'true', 'type': 'statuses'})
        r.raise_for_status()
        statuses = r.json()['statuses']
        return statuses[0] if statuses else None

    def status(self, status_id):
        r = self.http.get(f'/api/v1/statuses/{status_id}')
        return None if r.status_code == 404 else r.raise_for_status().json()

    def reply(self, status_id, text):
        return self.http.post('/api/v1/statuses', data={'status': text, 'in_reply_to_id': status_id}).raise_for_status().json()

    def favourite(self, status_id):
        self.http.post(f'/api/v1/statuses/{status_id}/favourite').raise_for_status()

    def reblog(self, status_id):
        self.http.post(f'/api/v1/statuses/{status_id}/reblog').raise_for_status()


class PieFed:
    def __init__(self, user, password):
        self.http = httpx.Client(base_url='https://piefed.test/api/alpha', verify=CA, timeout=10)
        jwt = self.http.post('/user/login', json={'username': user, 'password': password}).raise_for_status().json()['jwt']
        self.http.headers['Authorization'] = f'Bearer {jwt}'

    def microblogs_id(self):
        return self.http.get('/community', params={'name': 'microblogs'}).raise_for_status().json()['community_view']['community']['id']

    def post(self, title, body, community_id, **extra):
        payload = {'title': title, 'body': body, 'community_id': community_id, **extra}
        return self.http.post('/post', json=payload).raise_for_status().json()['post_view']

    def edit(self, post_id, **fields):
        return self.http.put('/post', json={'post_id': post_id, **fields}).raise_for_status().json()['post_view']

    def delete(self, post_id):
        self.http.post('/post/delete', json={'post_id': post_id, 'deleted': True}).raise_for_status()

    def get_post(self, post_id):
        return self.http.get('/post', params={'id': post_id}).raise_for_status().json()['post_view']

    def replies(self, post_id):
        return self.http.get('/comment/list', params={'post_id': post_id}).raise_for_status().json()['comments']
```

Before writing `PieFed`, confirm each alpha route and payload against `app/api/alpha/routes.py` and its schemas: `/user/login`, `GET /community?name=`, `POST /post`, `PUT /post`, `POST /post/delete`, `GET /post?id=` and `GET /comment/list?post_id=`. Use the names those files define. If one differs, fix it here and nowhere else.

- [ ] **Step 3: Write `interop/conftest.py`**

```python
import pytest

from interop.clients import Mastodon, PieFed, tokens, wait_for


@pytest.fixture(scope='session')
def mastodon():
    return Mastodon(tokens()['mastodon_bob'])


@pytest.fixture(scope='session')
def piefed():
    return PieFed('alice', 'interop-alice-pw')


@pytest.fixture(scope='session')
def bob_follows_alice(mastodon):
    account = wait_for(lambda: mastodon.resolve_account('alice@piefed.test'))
    mastodon.follow(account['id'])
    wait_for(lambda: mastodon.relationship(account['id'])['following'])
    return account
```

- [ ] **Step 4: Write the smoke test `interop/test_smoke.py`**

```python
"""The harness itself works: TLS, DNS, webfinger and a federated follow both ways (spec §8.1)."""


def test_bob_on_mastodon_follows_alice_on_piefed(bob_follows_alice):
    assert bob_follows_alice['acct'] == 'alice@piefed.test'
```

- [ ] **Step 5: Run it**

Run: `interop/run.sh test -k smoke -v`
Expected: `1 passed`. If the follow never becomes `following`, read `podman-compose -f compose.interop.yaml logs piefed-celery mastodon-sidekiq` and fix the harness. A raised timeout is not a fix.

- [ ] **Step 6: Commit**

```bash
git add interop/
git commit -m "test: interop smoke test, a Mastodon user follows a PieFed user through the harness

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: `microblog_note` builder

**Files:**
- Create: `app/activitypub/serializers/__init__.py`, `app/activitypub/serializers/microblog.py`
- Create: `tests/fixtures/mastodon_note_v4_5.json`
- Test: `tests/test_serializers_microblog.py`
- Modify: `coverage_floors.ini` (add `app/activitypub/serializers/__init__.py = 100` and `app/activitypub/serializers/microblog.py = 100`, in the file's existing key format)

**Interfaces:**
- Consumes:
  - `is_local_microblogs(community) -> bool` from `app.utils`
  - `followers_only_addressing(author, recipients) -> dict` and `is_open(post) -> bool` from `app.visibility`
  - `post_count_collections(post) -> dict` from `app.activitypub.util`
  - `ap_datetime`, `html_to_text`, `guess_mime_type` from `app.utils`
- Produces (in `app.activitypub.serializers`):
  - `uses_microblog_note(post) -> bool`
  - `note_content(post) -> str`
  - `note_addressing(post, mentions: list) -> dict` with keys `to`, `cc`, `interactionPolicy`
  - `note_attachments(post) -> list[dict]`
  - `microblog_note(post, mentions: list) -> dict`, the AP Note without `@context`

- [ ] **Step 1: Capture the reference fixture.** With the harness up, post a status as bob that has an image, a hashtag and a CW. Fetch it as AS2:

```bash
podman-compose -f compose.interop.yaml exec -T piefed-web curl -fsS --cacert /tmp/harness-ca.crt \
  -H 'Accept: application/activity+json' https://mastodon.test/users/bob/statuses/<id> > tests/fixtures/mastodon_note_v4_5.json
```

The fixture is the reference for the key set Mastodon itself emits. The golden test below asserts that our Note uses Mastodon's names for every key the two share.

- [ ] **Step 2: Write the failing tests** in `tests/test_serializers_microblog.py`

```python
"""microblog_note (spec 2026-10-14 §4.2, amended §10 A2): a local microblogs post as a Mastodon-native Note."""
import json
from pathlib import Path

import pytest

from app import db
from app.activitypub.serializers import (microblog_note, note_addressing, note_attachments, note_content,
                                         uses_microblog_note)
from app.constants import (POST_TYPE_ARTICLE, POST_TYPE_EVENT, POST_TYPE_IMAGE, POST_TYPE_LINK, POST_TYPE_POLL,
                           POST_TYPE_VIDEO, VISIBILITY_FOLLOWERS)
from app.models import File
from tests.factories import make_community, make_instance, make_post, make_user

PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'
FIXTURE = Path(__file__).parent / 'fixtures' / 'mastodon_note_v4_5.json'


def _local(community_name='microblogs', title='Hello there', body=None, body_html=None, post_type=POST_TYPE_ARTICLE):
    local = make_instance('test.piefed.local', software='piefed')
    alice = make_user(local, 'alice', local=True)
    community = make_community(community_name)
    post = make_post(community, alice, ap_id='https://test.piefed.local/post/1', title=title)
    post.type = post_type
    post.body = body
    post.body_html = body_html
    db.session.commit()
    return post


def test_uses_microblog_note_only_in_local_microblogs(db_session):
    post = _local()
    assert uses_microblog_note(post)


def test_other_local_community_does_not_use_it(db_session):
    assert not uses_microblog_note(_local(community_name='c1'))


def test_remote_microblogs_community_does_not_use_it(db_session):
    post = _local()
    post.community.ap_profile_id = 'https://peer.example/c/microblogs'
    post.community.ap_id = 'microblogs@peer.example'
    db.session.commit()
    assert not uses_microblog_note(post)


@pytest.mark.parametrize('post_type', [POST_TYPE_POLL, POST_TYPE_EVENT])
def test_polls_and_events_keep_their_own_builder(db_session, post_type):
    assert not uses_microblog_note(_local(post_type=post_type))


def test_content_is_title_paragraph_then_body(db_session):
    post = _local(title='Hello there', body='more', body_html='<p>more</p>')
    assert note_content(post) == '<p>Hello there</p><p>more</p>'


def test_title_only_post_is_its_title(db_session):
    assert note_content(_local(title='Just a title')) == '<p>Just a title</p>'


def test_title_already_leading_body_is_not_repeated(db_session):
    post = _local(title='Fish & chips today', body='Fish & chips today, yes',
                  body_html='<p>Fish &amp; chips today, yes</p>')
    assert note_content(post) == '<p>Fish &amp; chips today, yes</p>'


def test_empty_title_is_body(db_session):
    post = _local(title='', body='b', body_html='<p>b</p>')
    assert note_content(post) == '<p>b</p>'


@pytest.mark.parametrize('post_type', [POST_TYPE_LINK, POST_TYPE_VIDEO])
def test_link_title_links_the_url(db_session, post_type):
    post = _local(title='A site', post_type=post_type)
    post.url = 'https://example.com/x'
    assert note_content(post) == '<p><a href="https://example.com/x">A site</a></p>'


def test_link_post_without_url_has_plain_title(db_session):
    assert note_content(_local(title='A site', post_type=POST_TYPE_LINK)) == '<p>A site</p>'


def test_title_and_url_are_escaped(db_session):
    post = _local(title='<b>x</b> & "y"', post_type=POST_TYPE_LINK)
    post.url = 'https://example.com/?a=1&b="2"'
    assert note_content(post) == ('<p><a href="https://example.com/?a=1&amp;b=&#34;2&#34;">'
                                  '&lt;b&gt;x&lt;/b&gt; &amp; &#34;y&#34;</a></p>')


def test_public_addressing(db_session):
    post = _local()
    bob = make_user(make_instance('mastodon.example'), 'bob')
    bob.ap_profile_id = bob.ap_public_url = 'https://mastodon.example/users/bob'
    db.session.commit()
    addressing = note_addressing(post, [bob])
    assert addressing['to'] == [PUBLIC]
    assert addressing['cc'] == [post.author.followers_url(), 'https://mastodon.example/users/bob']
    assert addressing['interactionPolicy'] == {'canQuote': {'automaticApproval': [PUBLIC]}}


def test_followers_only_addressing_is_amendment_b(db_session):
    post = _local()
    post.visibility = VISIBILITY_FOLLOWERS
    db.session.commit()
    assert note_addressing(post, []) == {'to': [post.author.followers_url()], 'cc': [],
                                         'interactionPolicy': {'canQuote': {'automaticApproval':
                                                                            [post.author.public_url()]}}}


def test_image_attachment_is_a_document(db_session):
    post = _local(post_type=POST_TYPE_IMAGE)
    post.image = File(source_url='https://test.piefed.local/static/media/a.webp', alt_text='a cat',
                      width=640, height=480)
    db.session.commit()
    assert note_attachments(post) == [{'type': 'Document', 'mediaType': 'image/webp',
                                       'url': 'https://test.piefed.local/static/media/a.webp',
                                       'name': 'a cat', 'width': 640, 'height': 480}]


def test_image_without_size_or_alt(db_session):
    post = _local(post_type=POST_TYPE_IMAGE)
    post.image = File(source_url='https://test.piefed.local/static/media/a.png')
    db.session.commit()
    assert note_attachments(post) == [{'type': 'Document', 'mediaType': 'image/png',
                                       'url': 'https://test.piefed.local/static/media/a.png', 'name': ''}]


def test_image_post_with_no_image_has_no_attachment(db_session):
    assert note_attachments(_local(post_type=POST_TYPE_IMAGE)) == []


def test_link_post_attaches_a_link(db_session):
    post = _local(post_type=POST_TYPE_LINK)
    post.url = 'https://example.com/x'
    assert note_attachments(post) == [{'type': 'Link', 'href': 'https://example.com/x'}]


def test_text_post_has_no_attachments(db_session):
    assert note_attachments(_local()) == []


def test_note_shape(db_session):
    post = _local(title='Hi', body='#tag body', body_html='<p>#tag body</p>')
    note = microblog_note(post, [])
    assert note['type'] == 'Note'
    assert note['id'] == post.public_url()
    assert note['attributedTo'] == post.author.public_url()
    assert note['content'] == '<p>Hi</p><p>#tag body</p>'
    assert note['inReplyTo'] is None
    assert 'name' not in note and 'audience' not in note
    assert post.community.public_url() not in note['to'] + note['cc']
    assert note['source'] == {'content': '#tag body', 'mediaType': 'text/markdown'}


def test_content_warning_becomes_summary_and_sensitive(db_session):
    post = _local()
    post.content_warning = 'spoilers'
    db.session.commit()
    note = microblog_note(post, [])
    assert note['summary'] == 'spoilers' and note['sensitive'] is True


def test_no_content_warning_no_summary(db_session):
    note = microblog_note(_local(), [])
    assert 'summary' not in note and note['sensitive'] is False


def test_mentions_become_mention_tags(db_session):
    post = _local()
    bob = make_user(make_instance('mastodon.example'), 'bob')
    bob.ap_profile_id = bob.ap_public_url = 'https://mastodon.example/users/bob'
    bob.ap_id = 'bob@mastodon.example'
    db.session.commit()
    note = microblog_note(post, [bob])
    assert {'href': 'https://mastodon.example/users/bob', 'name': bob.mention_tag(), 'type': 'Mention'} in note['tag']


def test_edited_post_has_updated(db_session):
    from datetime import datetime
    post = _local()
    post.edited_at = datetime(2026, 10, 14, 12, 0, 0)
    db.session.commit()
    assert microblog_note(post, [])['updated'] == '2026-10-14T12:00:00+00:00'


def test_unedited_post_has_no_updated(db_session):
    assert 'updated' not in microblog_note(_local(), [])


def test_language_gives_content_map(db_session):
    from app.models import Language
    post = _local(title='Hi')
    post.language = Language(code='en', name='English')
    db.session.commit()
    assert microblog_note(post, [])['contentMap'] == {'en': '<p>Hi</p>'}


def test_no_language_no_content_map(db_session):
    assert 'contentMap' not in microblog_note(_local(), [])


def test_indexable_public_post_is_searchable(db_session):
    post = _local()
    post.indexable = True
    db.session.commit()
    assert microblog_note(post, [])['searchableBy'] == PUBLIC


def test_followers_only_post_never_searchable_and_no_count_collections(db_session):
    post = _local()
    post.indexable = True
    post.visibility = VISIBILITY_FOLLOWERS
    db.session.commit()
    note = microblog_note(post, [])
    assert 'searchableBy' not in note and 'likes' not in note


def test_keys_shared_with_mastodon_use_mastodons_names(db_session):
    mastodon = json.loads(FIXTURE.read_text())
    ours = microblog_note(_local(title='Hi', body='b', body_html='<p>b</p>'), [])
    for key in ('id', 'type', 'attributedTo', 'to', 'cc', 'content', 'published', 'url', 'sensitive', 'tag',
                'attachment', 'inReplyTo', 'replies'):
        assert key in mastodon, f'fixture lacks {key}: recapture it'
        assert key in ours
```

Adjust the `ap_datetime` expected string in `test_edited_post_has_updated` to whatever `app.utils.ap_datetime` produces for a naive UTC datetime. Read its body at `app/utils.py:3555` first; the test must pin the real format. Likewise, check which `post_count_collections` key marks the collections (`likes` above) by reading `app/activitypub/util.py:352`.

- [ ] **Step 3: Run them and confirm they fail**

Run: `./run_tests.sh tests/test_serializers_microblog.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.activitypub.serializers'`

- [ ] **Step 4: Write `app/activitypub/serializers/__init__.py`**

```python
"""One builder per outbound object shape, used by both push (send_post) and fetch (post_to_page), so the object a
peer is sent and the object it fetches are the same object (spec 2026-10-14 §4, amended §10 A1)."""
from app.activitypub.serializers.microblog import (microblog_note, note_addressing, note_attachments, note_content,
                                                   uses_microblog_note)

__all__ = ['microblog_note', 'note_addressing', 'note_attachments', 'note_content', 'uses_microblog_note']
```

- [ ] **Step 5: Write `app/activitypub/serializers/microblog.py`**

```python
"""A post in the local microblogs community as a Mastodon-native Note (spec 2026-10-14 §4.2, amended §10 A2)."""
from flask import current_app
from markupsafe import escape

from app.activitypub.util import post_count_collections
from app.constants import POST_TYPE_EVENT, POST_TYPE_IMAGE, POST_TYPE_LINK, POST_TYPE_POLL, POST_TYPE_VIDEO
from app.utils import ap_datetime, guess_mime_type, html_to_text, is_local_microblogs
from app.visibility import followers_only_addressing, is_open

PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'


def uses_microblog_note(post) -> bool:
    """A post goes out as a Note when it is in the local microblogs community. Polls and events keep their
    Question and Event shapes."""
    return is_local_microblogs(post.community) and post.type not in (POST_TYPE_POLL, POST_TYPE_EVENT)


def note_content(post) -> str:
    """The title as its own paragraph, then the body. A Note has no title, and the form requires one, so leaving it
    out would lose what the author wrote. Left out only when the body's text already starts with it."""
    body = post.body_html or ''
    title = (post.title or '').strip()
    if not title or html_to_text(body).strip().startswith(title):
        return body
    if post.type in (POST_TYPE_LINK, POST_TYPE_VIDEO) and post.url:
        return f'<p><a href="{escape(post.url)}">{escape(title)}</a></p>{body}'
    return f'<p>{escape(title)}</p>{body}'


def note_addressing(post, mentions: list) -> dict:
    """Public: to Public, cc the author's followers collection and each Mention. Followers-only: Amendment B."""
    if not is_open(post):
        return followers_only_addressing(post.author, mentions)
    return {'to': [PUBLIC],
            'cc': [post.author.followers_url()] + [mention.public_url() for mention in mentions],
            'interactionPolicy': {'canQuote': {'automaticApproval': [PUBLIC]}}}


def note_attachments(post) -> list:
    if post.type == POST_TYPE_IMAGE and post.image is not None and post.image.source_url:
        attachment = {'type': 'Document', 'mediaType': guess_mime_type(post.image.source_url),
                      'url': post.image.source_url, 'name': post.image.alt_text or ''}
        if post.image.width and post.image.height:
            attachment['width'] = post.image.width
            attachment['height'] = post.image.height
        return [attachment]
    if post.type in (POST_TYPE_LINK, POST_TYPE_VIDEO) and post.url:
        return [{'type': 'Link', 'href': post.url}]
    return []


def microblog_note(post, mentions: list) -> dict:
    """The Note, without @context. `mentions` are the users the body Mentions: the push finds them with fetching
    allowed, a fetch with it refused (mentioned_users(post, allow_fetch=False)), as for followers-only addressing."""
    server_url = current_app.config['SERVER_URL']
    addressing = note_addressing(post, mentions)
    content = note_content(post)
    tag = post.tags_for_activitypub()
    for mention in mentions:
        tag.append({'href': mention.public_url(), 'name': mention.mention_tag(), 'type': 'Mention'})
    note = {
        'id': post.public_url(),
        'type': 'Note',
        'attributedTo': post.author.public_url(),
        'to': addressing['to'],
        'cc': addressing['cc'],
        'content': content,
        'mediaType': 'text/html',
        'source': {'content': post.body or '', 'mediaType': 'text/markdown'},
        'published': ap_datetime(post.posted_at),
        'url': post.public_url(),
        'context': f'{server_url}/post/{post.id}/context',
        'replies': f'{server_url}/post/{post.id}/replies',
        'inReplyTo': None,
        'tag': tag,
        'attachment': note_attachments(post),
        'sensitive': bool(post.nsfw or post.nsfl or post.content_warning),
        'commentsEnabled': post.comments_enabled,
        'interactionPolicy': addressing['interactionPolicy'],
    }
    if post.language_id:
        note['contentMap'] = {post.language_code(): content}
    if post.content_warning:
        note['summary'] = post.content_warning
    if post.edited_at is not None:
        note['updated'] = ap_datetime(post.edited_at)
    if is_open(post):
        note.update(post_count_collections(post))
        if post.indexable:
            note['searchableBy'] = PUBLIC
    return note
```

`test_language_gives_content_map` sets `post.language` directly. If `Post.language_id` is not populated by that relationship assignment before `language_id` is read, set `post.language_id` after `db.session.flush()`. The test must exercise the `if post.language_id:` branch for real.

- [ ] **Step 6: Run the tests and confirm they pass**

Run: `./run_tests.sh tests/test_serializers_microblog.py -q`
Expected: PASS

- [ ] **Step 7: Check that the new modules are at 100%**

Run: `./run_tests.sh tests/test_serializers_microblog.py --cov=app.activitypub.serializers --cov-branch --cov-report=term-missing --cov-fail-under=100`
Expected: `Required test coverage of 100% reached`

- [ ] **Step 8: Commit**

```bash
git add app/activitypub/serializers/ tests/test_serializers_microblog.py tests/fixtures/mastodon_note_v4_5.json coverage_floors.ini
git commit -m "feat: microblog_note builds a local microblogs post as a Mastodon-native Note

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Fetch path serves the Note

**Files:**
- Modify: `app/activitypub/util.py:369` (top of `post_to_page`)
- Test: `tests/test_microblog_fetch.py`

**Interfaces:**
- Consumes: `uses_microblog_note`, `microblog_note` (Task 4); `mentioned_users(post, allow_fetch=False)` from `app.shared.tasks.pages`
- Produces: `post_to_page(post)` returns `microblog_note(post, mentioned_users(post, allow_fetch=False))` when `uses_microblog_note(post)`. All existing callers inherit this: the fetch route, the outbox `create_activity_of`, and `former_type_of_post`.

- [ ] **Step 1: Write the failing tests**

```python
"""Fetching a local microblogs post returns the same Note send_post pushes (spec §4.1, amended §10 A1)."""
from app import db
from app.activitypub.routes import former_type_of_post
from app.activitypub.util import create_activity_of, post_to_page
from app.constants import POST_TYPE_ARTICLE, POST_TYPE_POLL, VISIBILITY_FOLLOWERS
from tests.factories import make_community, make_instance, make_post, make_user

PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'


def _post(community_name='microblogs'):
    local = make_instance('test.piefed.local', software='piefed')
    alice = make_user(local, 'alice', local=True)
    post = make_post(make_community(community_name), alice, ap_id='https://test.piefed.local/post/1', title='Hello')
    post.type = POST_TYPE_ARTICLE
    db.session.commit()
    return post


def test_post_to_page_gives_a_note_for_microblogs(db_session):
    page = post_to_page(_post())
    assert page['type'] == 'Note' and 'name' not in page and page['content'] == '<p>Hello</p>'


def test_post_to_page_unchanged_elsewhere(db_session):
    page = post_to_page(_post('c1'))
    assert page['type'] == 'Page' and page['name'] == 'Hello'


def test_post_to_page_poll_in_microblogs_keeps_page_builder(db_session):
    post = _post()
    post.type = POST_TYPE_POLL
    db.session.commit()
    assert post_to_page(post)['type'] in ('Page', 'Question')


def test_fetch_route_serves_the_note(app, db_session):
    post = _post()
    with app.test_client() as client:
        resp = client.get(f'/post/{post.id}', headers={'Accept': 'application/activity+json'})
    assert resp.status_code == 200
    assert resp.json['type'] == 'Note' and '@context' in resp.json


def test_tombstone_former_type_is_note(db_session):
    assert former_type_of_post(_post()) == 'Note'


def test_outbox_create_wraps_the_note(db_session):
    assert create_activity_of(_post())['object']['type'] == 'Note'


def test_followers_only_microblog_fetch_matches_push_addressing(db_session):
    """post_ap serves a followers-only post to an entitled signer as addressed_as_pushed(post_to_page(post), post);
    the signer check itself is covered by tests/test_ap_security_entitlement.py, so this pins the document."""
    from app.activitypub.routes import addressed_as_pushed
    from app.activitypub.serializers import microblog_note
    post = _post()
    post.visibility = VISIBILITY_FOLLOWERS
    post.indexable = True
    db.session.commit()
    served = addressed_as_pushed(post_to_page(post), post)
    pushed = microblog_note(post, [])
    assert served == pushed
    assert served['to'] == [post.author.followers_url()]
    assert PUBLIC not in served['to'] + served['cc']
    assert 'searchableBy' not in served and 'audience' not in served
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `./run_tests.sh tests/test_microblog_fetch.py -q`
Expected: FAIL: `assert 'Page' == 'Note'`

- [ ] **Step 3: Implement it.** Insert the following at the very top of `post_to_page` in `app/activitypub/util.py`:

```python
def post_to_page(post: Post):
    # Spec 2026-10-14 §10 A1: a local microblogs post is a Note, built by the same function send_post pushes with.
    # Mentions are resolved among stored actors only: answering a fetch never fetches.
    from app.activitypub.serializers import microblog_note, uses_microblog_note
    if uses_microblog_note(post):
        from app.shared.tasks.pages import mentioned_users
        return microblog_note(post, mentioned_users(post, allow_fetch=False))
    activity_data = {
        ...existing body unchanged...
```

The imports are local because `serializers.microblog` imports `post_count_collections` from this module, and `app.shared.tasks.pages` imports from `app.activitypub`.

- [ ] **Step 4: Run the tests and confirm they pass, along with the neighbouring suites**

Run: `./run_tests.sh tests/test_microblog_fetch.py tests/test_activitypub_util.py tests/test_microblog_announce.py tests/test_community_shows_microblogs.py -q`
Expected: PASS. If an existing test asserted `Page` for a post in a community named `microblogs`, it was asserting the defect this feature fixes. Change that assertion to `Note`, and cite §10 A1 in a comment.

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/util.py tests/test_microblog_fetch.py
git commit -m "feat: fetching a local microblogs post returns its Mastodon-native Note

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Push path sends the Note (`send_microblog_note`)

**Files:**
- Modify: `app/shared/tasks/pages.py` (new function above `send_post`; branch in `send_post` after the banned/blocked early returns, before `if post.type == POST_TYPE_POLL:` at about line 186)
- Test: `tests/test_send_microblog_note.py`

**Interfaces:**
- Consumes: `microblog_note`, `uses_microblog_note` (Task 4); `can_deliver_to`, `follower_instances`, `send_post_request`, `default_context`, `gibberish`, `MICROBLOG_APPS` (all already imported in `pages.py` except the serializers; check `MICROBLOG_APPS`'s import line)
- Produces: `send_microblog_note(post, user, community, recipients: list, edit: bool) -> None`

Delivery rules (A6):
1. **Community Announce**, public posts only, to `community.following_instances()`, each filtered by `can_deliver_to(user, instance)`:
   - `MICROBLOG_APPS` on create: `Announce(<post id>)`.
   - `MICROBLOG_APPS` on edit: the author's `Update`, sent directly.
   - Everything else: `Announce(<Create/Update without @context>)`.
2. **Author's Create/Update**, sent to:
   - each mentioned recipient's instance, skipping local recipients (instance id 1), and
   - `user.following_instances()` for public posts, or `follower_instances(user)` for followers-only posts.

   Each instance gets it at most once and is filtered by `can_deliver_to`. Nothing is skipped because rule 1 already reached that instance.

- [ ] **Step 1: Write the failing tests**

```python
"""send_post for a local microblogs post pushes the Note microblog_note builds (spec §4.2, §6.1, amended §10 A6)."""
import json

import pytest

from app import db
from app.activitypub.util import post_to_page
from app.constants import POST_TYPE_ARTICLE, VISIBILITY_FOLLOWERS
from app.models import CommunityMember
from app.shared.tasks.pages import send_post
from tests.factories import make_community, make_follow, make_instance, make_post, make_user


def _seed():
    local = make_instance('test.piefed.local', software='piefed')
    alice = make_user(local, 'alice', local=True, with_keys=True)
    community = make_community('microblogs')
    from app.activitypub.signature import RsaKeys
    community.private_key, community.public_key = RsaKeys.generate_keypair()
    post = make_post(community, alice, ap_id='https://test.piefed.local/post/1', title='Hello')
    post.type = POST_TYPE_ARTICLE
    post.body, post.body_html = 'world', '<p>world</p>'
    db.session.commit()
    return alice, community, post


def _remote(domain, software, name):
    instance = make_instance(domain, software=software)
    instance.inbox = f'https://{domain}/inbox'
    user = make_user(instance, name)
    user.ap_profile_id = user.ap_public_url = f'https://{domain}/users/{name}'
    db.session.commit()
    return instance, user


def _bodies(route):
    return [json.loads(call.request.content) for call in route.calls]


def test_follower_on_mastodon_gets_the_authors_create_note(db_session, http_mock):
    alice, community, post = _seed()
    instance, bob = _remote('mastodon.example', 'mastodon', 'bob')
    make_follow(alice, bob, is_inward=True)
    route = http_mock.post('https://mastodon.example/inbox').respond(202)
    send_post(post.id, session=db.session)
    (create,) = _bodies(route)
    assert create['type'] == 'Create' and create['actor'] == alice.public_url()
    assert create['object']['type'] == 'Note'
    assert 'audience' not in create


def test_pushed_note_equals_fetched_note(db_session, http_mock):
    alice, community, post = _seed()
    instance, bob = _remote('mastodon.example', 'mastodon', 'bob')
    make_follow(alice, bob, is_inward=True)
    route = http_mock.post('https://mastodon.example/inbox').respond(202)
    send_post(post.id, session=db.session)
    (create,) = _bodies(route)
    assert create['object'] == post_to_page(post)


def test_edit_sends_update_of_the_same_note(db_session, http_mock):
    from datetime import datetime
    alice, community, post = _seed()
    post.edited_at = datetime(2026, 10, 14, 12, 0)
    db.session.commit()
    instance, bob = _remote('mastodon.example', 'mastodon', 'bob')
    make_follow(alice, bob, is_inward=True)
    route = http_mock.post('https://mastodon.example/inbox').respond(202)
    send_post(post.id, edit=True, session=db.session)
    (update,) = _bodies(route)
    assert update['type'] == 'Update' and update['object'] == post_to_page(post)


def test_threadiverse_community_follower_gets_group_announce_and_follower_still_gets_create(db_session, http_mock):
    alice, community, post = _seed()
    lemmy, carol = _remote('lemmy.example', 'lemmy', 'carol')
    db.session.add(CommunityMember(user_id=carol.id, community_id=community.id))
    make_follow(alice, carol, is_inward=True)
    db.session.commit()
    route = http_mock.post('https://lemmy.example/inbox').respond(202)
    send_post(post.id, session=db.session)
    kinds = sorted((b['type'], b['actor']) for b in _bodies(route))
    assert kinds == sorted([('Announce', community.public_url()), ('Create', alice.public_url())])


def test_microblog_app_community_follower_gets_announce_of_id_on_create(db_session, http_mock):
    alice, community, post = _seed()
    masto, dave = _remote('mastodon.example', 'mastodon', 'dave')
    db.session.add(CommunityMember(user_id=dave.id, community_id=community.id))
    db.session.commit()
    route = http_mock.post('https://mastodon.example/inbox').respond(202)
    send_post(post.id, session=db.session)
    (announce,) = _bodies(route)
    assert announce['type'] == 'Announce' and announce['object'] == post.ap_id


def test_microblog_app_community_follower_gets_authors_update_on_edit(db_session, http_mock):
    alice, community, post = _seed()
    masto, dave = _remote('mastodon.example', 'mastodon', 'dave')
    db.session.add(CommunityMember(user_id=dave.id, community_id=community.id))
    db.session.commit()
    route = http_mock.post('https://mastodon.example/inbox').respond(202)
    send_post(post.id, edit=True, session=db.session)
    (update,) = _bodies(route)
    assert update['type'] == 'Update' and update['actor'] == alice.public_url()


def test_mentioned_remote_user_gets_the_create(db_session, http_mock, monkeypatch):
    alice, community, post = _seed()
    instance, bob = _remote('mastodon.example', 'mastodon', 'bob')
    monkeypatch.setattr('app.shared.tasks.pages.mentioned_users', lambda p, allow_fetch=True: [bob])
    route = http_mock.post('https://mastodon.example/inbox').respond(202)
    send_post(post.id, session=db.session)
    (create,) = _bodies(route)
    assert 'https://mastodon.example/users/bob' in create['cc']


def test_mentioned_local_user_is_not_delivered_to(db_session, http_mock, monkeypatch):
    alice, community, post = _seed()
    local_bob = make_user(alice.instance, 'bob', local=True)
    monkeypatch.setattr('app.shared.tasks.pages.mentioned_users', lambda p, allow_fetch=True: [local_bob])
    send_post(post.id, session=db.session)   # http_mock has no route: any delivery fails the test


def test_followers_only_goes_to_accepted_follower_instances_and_never_announces(db_session, http_mock):
    alice, community, post = _seed()
    post.visibility = VISIBILITY_FOLLOWERS
    db.session.commit()
    instance, bob = _remote('mastodon.example', 'mastodon', 'bob')
    make_follow(alice, bob, is_inward=True)
    lemmy, carol = _remote('lemmy.example', 'lemmy', 'carol')
    db.session.add(CommunityMember(user_id=carol.id, community_id=community.id))
    db.session.commit()
    route = http_mock.post('https://mastodon.example/inbox').respond(202)
    send_post(post.id, session=db.session)
    (create,) = _bodies(route)
    assert create['object']['to'] == [alice.followers_url()]


def test_blocked_instance_gets_nothing(db_session, http_mock):
    from tests.factories import make_instance_block
    alice, community, post = _seed()
    instance, bob = _remote('mastodon.example', 'mastodon', 'bob')
    make_follow(alice, bob, is_inward=True)
    make_instance_block(alice, instance)
    send_post(post.id, session=db.session)   # no route registered: a delivery would fail


def test_microblog_edit_with_no_audience_sends_nothing(db_session, http_mock):
    alice, community, post = _seed()
    send_post(post.id, edit=True, session=db.session)


def test_each_instance_gets_one_create_even_with_two_followers(db_session, http_mock):
    alice, community, post = _seed()
    instance, bob = _remote('mastodon.example', 'mastodon', 'bob')
    erin = make_user(instance, 'erin')
    make_follow(alice, bob, is_inward=True)
    make_follow(alice, erin, is_inward=True)
    route = http_mock.post('https://mastodon.example/inbox').respond(202)
    send_post(post.id, session=db.session)
    assert len(route.calls) == 1


def test_non_microblog_community_still_uses_page_path(db_session, http_mock):
    alice, community, post = _seed()
    community.name = 'c1'
    db.session.commit()
    instance, bob = _remote('mastodon.example', 'mastodon', 'bob')
    make_follow(alice, bob, is_inward=True)
    route = http_mock.post('https://mastodon.example/inbox').respond(202)
    send_post(post.id, session=db.session)
    assert any(b['object'].get('type') == 'Note' and b['object']['content'].startswith('<p>Hello</p>')
               for b in _bodies(route) if isinstance(b.get('object'), dict))
```

Before running, read the "NO NETWORK IS TOUCHED" note in `tests/test_shared_tasks_send_post.py` around line 240. Copy its autouse `.example` resolver fixture, or import it if it is importable, so delivery signing never does real DNS. Also check how `send_post_request` behaves under eager Celery in that file, and match it.

- [ ] **Step 2: Run them and confirm they fail**

Run: `./run_tests.sh tests/test_send_microblog_note.py -q`
Expected: FAIL. `test_pushed_note_equals_fetched_note` fails because the pushed object is the rewritten Page copy, not `post_to_page`'s Note.

- [ ] **Step 3: Implement it.** Add the following to `app/shared/tasks/pages.py`, importing `from app.activitypub.serializers import microblog_note, uses_microblog_note` at the top. If that import is circular at module load, make it local inside both functions and note why in a comment.

```python
def send_microblog_note(post, user, community, recipients: list, edit: bool) -> None:
    """Push a local microblogs post as the Note microblog_note builds (spec 2026-10-14 §4.2, §6.1, §10 A6).

    Two independent fan-outs. The community Announces a public post to its threadiverse followers, as any local
    community does. The author's Create (or Update) goes to every mentioned user's instance and every follower's
    instance, whether or not that instance also got the Announce. A Mastodon follower given only a boost would not
    see the post as its author's own status, and a receiver dedupes by object id."""
    note = microblog_note(post, recipients)
    verb = 'update' if edit else 'create'
    activity = {'id': f"{current_app.config['SERVER_URL']}/activities/{verb}/{gibberish(15)}",
                'type': 'Update' if edit else 'Create', 'actor': user.public_url(), 'object': note,
                'to': note['to'], 'cc': note['cc'], '@context': default_context()}
    followers_only = not is_open(post)

    if not followers_only:
        announce_id = f"{current_app.config['SERVER_URL']}/activities/announce/{gibberish(15)}"
        inner = {key: value for key, value in activity.items() if key != '@context'}
        group_announce = {'id': announce_id, 'type': 'Announce', 'actor': community.public_url(), 'object': inner,
                          'to': note['to'], 'cc': [community.ap_followers_url], '@context': default_context()}
        microblog_announce = dict(group_announce, object=post.ap_id)
        for instance in community.following_instances():
            if not can_deliver_to(user, instance):
                continue
            if instance.software in MICROBLOG_APPS:
                if edit:
                    send_post_request(instance.inbox, activity, user.private_key, user.public_url() + '#main-key')
                else:
                    send_post_request(instance.inbox, microblog_announce, community.private_key,
                                      community.public_url() + '#main-key')
            else:
                send_post_request(instance.inbox, group_announce, community.private_key,
                                  community.public_url() + '#main-key')

    targets = [recipient.instance for recipient in recipients if recipient.instance_id != 1]
    targets += follower_instances(user) if followers_only else user.following_instances()
    sent_to = set()
    for instance in targets:
        if instance.domain in sent_to or not can_deliver_to(user, instance):
            continue
        send_post_request(instance.inbox, activity, user.private_key, user.public_url() + '#main-key')
        sent_to.add(instance.domain)
```

Then, in `send_post`, insert this immediately before `if post.type == POST_TYPE_POLL:` (after the `banned`/blocked early returns):

```python
    if uses_microblog_note(post):   # spec 2026-10-14 §10 A1: the Note push and fetch share
        send_microblog_note(post, user, community, recipients, edit)
        return
```

Two points to check while implementing:
- A microblog-app instance that is both a community follower and an author follower receives `Announce(id)` and the author's `Create`. That is intended (A6).
- `user.following_instances()` already excludes instance 1 and gone instances. `follower_instances(user)` must still exclude pending followers, as Amendment B requires. Read `app/visibility.py` to confirm, and do not re-filter.

- [ ] **Step 4: Run the tests and confirm they pass, along with the existing send_post suite**

Run: `./run_tests.sh tests/test_send_microblog_note.py tests/test_shared_tasks_send_post.py tests/test_microblog_announce.py -q`
Expected: PASS. If an existing `test_shared_tasks_send_post.py` test builds its post in the default `make_community()` (whose default name is `microblogs`, so it is local microblogs) and asserts the old Page/Note-copy behaviour, decide which case it is:
- If the test was about generic community behaviour, give it `make_community('c1')`. `_seed` there already does.
- If it was about microblogs specifically, it now asserts the new behaviour.

Note each changed test in the commit body.

- [ ] **Step 5: Run the changed-line coverage gate over the branch so far**

Run: `./run_tests.sh --cov=app --cov-branch --cov-report=json:coverage.json -q && python3 tests/check_changed_line_coverage.py coverage.json main --branches app/pinned_http.py app/activitypub/util.py app/activitypub/serializers app/shared/tasks/pages.py`
Expected: exit 0, no uncovered lines or branches. If `python3` on the host lacks what the script needs, run it inside the test container: `podman-compose -f compose.test.yaml run --rm test-runner python tests/check_changed_line_coverage.py ...`, the same way `tests/README.md` documents running the floors check.

- [ ] **Step 6: Commit**

```bash
git add app/shared/tasks/pages.py tests/test_send_microblog_note.py
git commit -m "feat: a local microblogs post is pushed as the same Note a fetch returns

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Interop — Mastodon shows the microblog natively, and interactions return

**Files:**
- Create: `interop/test_microblog_mastodon.py`

**Interfaces:**
- Consumes: fixtures `mastodon`, `piefed`, `bob_follows_alice` and `wait_for` (Task 3)

- [ ] **Step 1: Write the tests**

```python
"""Spec 2026-10-14 §1 row 1: a post in PieFed's local microblogs shows on Mastodon as alice's own status, and
Mastodon's replies, favourites and boosts land in its PieFed thread."""
import uuid

import psycopg2
import pytest

from interop.clients import wait_for


def _in_home(mastodon, ap_id):
    def find():
        return next((s for s in mastodon.home() if s['uri'] == ap_id), None)
    find.__name__ = f'home has {ap_id}'
    return wait_for(find)


@pytest.fixture
def note(piefed, mastodon, bob_follows_alice):
    marker = uuid.uuid4().hex[:8]
    post = piefed.post(f'Title {marker}', f'Body {marker} #interop', piefed.microblogs_id())
    ap_id = post['post']['ap_id']
    return post, _in_home(mastodon, ap_id), marker


def test_status_reaches_home_timeline_as_alices_own(note):
    post, status, marker = note
    assert status['account']['acct'] == 'alice@piefed.test'
    assert status['reblog'] is None


def test_status_text_has_title_once_and_body(note):
    post, status, marker = note
    assert status['content'].count(f'Title {marker}') == 1
    assert f'Body {marker}' in status['content']


def test_status_is_not_a_link_card_and_hashtag_is_live(note):
    post, status, marker = note
    assert status['card'] is None
    assert any(tag['name'] == 'interop' for tag in status['tags'])


def test_fetch_by_url_resolves_to_the_same_status(note, mastodon):
    post, status, marker = note
    found = mastodon.search_status(post['post']['ap_id'])
    assert found['id'] == status['id'] and found['content'] == status['content']


def test_mastodon_reply_lands_in_piefed_thread(note, mastodon, piefed):
    post, status, marker = note
    mastodon.reply(status['id'], f'@alice@piefed.test reply {marker}')
    wait_for(lambda: any(f'reply {marker}' in c['comment']['body'] for c in piefed.replies(post['post']['id'])))


def test_mastodon_favourite_is_an_upvote(note, mastodon, piefed):
    post, status, marker = note
    before = piefed.get_post(post['post']['id'])['counts']['upvotes']
    mastodon.favourite(status['id'])
    wait_for(lambda: piefed.get_post(post['post']['id'])['counts']['upvotes'] > before)


def _boost_count(post_id):
    """PieFed records an inbound Announce of a local post as a post_boost row (app/models.py PostBoost); the alpha
    API does not expose it, so the harness reads the harness database directly."""
    with psycopg2.connect(host='piefed-db', dbname='piefed', user='piefed', password='piefed') as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM post_boost WHERE post_id = %s', (post_id,))
            return cur.fetchone()[0]


def test_mastodon_boost_reaches_piefed(note, mastodon, piefed):
    post, status, marker = note
    assert _boost_count(post['post']['id']) == 0
    mastodon.reblog(status['id'])
    wait_for(lambda: _boost_count(post['post']['id']) == 1)


def test_edit_updates_the_status(note, mastodon, piefed):
    post, status, marker = note
    piefed.edit(post['post']['id'], body=f'Edited {marker}')
    wait_for(lambda: f'Edited {marker}' in (mastodon.status(status['id']) or {}).get('content', ''))


def test_delete_removes_the_status(note, mastodon, piefed):
    post, status, marker = note
    piefed.delete(post['post']['id'])
    wait_for(lambda: mastodon.status(status['id']) is None)
```

The runner image (`test` target) already carries `psycopg2` from `requirements.txt`, and it sits on the `interop` network, so `piefed-db` resolves. Check the response field names (`counts.upvotes`, `comment.body`, `post.ap_id`) against `app/api/alpha/` schemas before running, and correct them in this file.

- [ ] **Step 2: Run them**

Run: `interop/run.sh test -v`
Expected: every test passes. If one fails, treat it as a bug in Tasks 4–6 or in an inbound handler (§6.4). Fix it test-first with a unit test in `tests/` reproducing the payload Mastodon sent, captured from `podman-compose -f compose.interop.yaml logs piefed-celery`, then rerun.

- [ ] **Step 3: Commit**

```bash
git add interop/test_microblog_mastodon.py interop/clients.py compose.interop.yaml
git commit -m "test: interop proves Mastodon shows PieFed microblogs natively and its replies, likes and boosts return

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Final gates

- [ ] **Step 1: Run the full unit suite with coverage**

Run: `./run_tests.sh --cov=app --cov-branch --cov-report=json:coverage.json`
Expected: all pass, with no new warnings (the suite's warning count is a ratchet).

- [ ] **Step 2: Check the changed-line gate and the floors**

Run (in the test container if needed, see Task 6 Step 5):
`python tests/check_changed_line_coverage.py coverage.json main --branches && python tests/check_coverage_floors.py coverage.json coverage_floors.ini`
Expected: both exit 0.

- [ ] **Step 3: Run the interop suite from clean**

Run: `interop/run.sh down && interop/run.sh test`
Expected: all pass from a fresh stack.

- [ ] **Step 4: Check the static-tree leak gate**

Run: `./run_tests.sh tests/static_leak_gate.py -q` (or however `tests/README.md` says to run it)
Expected: PASS
