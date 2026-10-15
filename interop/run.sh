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
        # plain podman run: podman-compose cannot clear the service entrypoint, and no CA exists yet at this point
        podman run --rm --entrypoint '' --env-file interop/.env.interop ghcr.io/mastodon/mastodon:v4.5@sha256:c64a30678a0e6591d7dbcb0924b4aa98eb1fe54ced5b8fe558449d00915bdb42 \
            bundle exec rails mastodon:webpush:generate_vapid_key | grep '^VAPID_' >> interop/.env.interop
    fi
    $COMPOSE build piefed-web
    $COMPOSE up -d
    wait_http piefed-web https://piefed.test/health
    wait_http piefed-web https://mastodon.test/health   # piefed-web has curl; the Mastodon image may not
    # init-db drops every table first, so run it only on an empty instance table (a re-run of `up` keeps the data)
    if [ "$($COMPOSE exec -T piefed-db psql -U piefed -tAc 'select count(*) from instance' | tr -d '[:space:]')" = 0 ]; then
        $COMPOSE exec -T piefed-web sh -c 'FLASK_APP=pyfedi.py flask init-db --interactive no'
    fi
    # piped into `python -` (an interactive `flask shell` chokes on the blank-line rules of a pasted script)
    { printf 'from pyfedi import app\napp.app_context().push()\n'; cat interop/seed_piefed.py; } \
        | $COMPOSE exec -T piefed-web python -
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
