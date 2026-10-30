#!/usr/bin/env bash
# Interop harness (spec 2026-10-14 §8.1).
#   interop/run.sh up                   start and seed the stack
#   interop/run.sh test [args]          up, then pytest interop/ in the runner (args pass through)
#   interop/run.sh conformance [args]   up, then interop/conformance/run.sh in the conformance-runner (args pass through)
#   interop/run.sh down                 remove the stack and its volumes
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
    # podman-compose's `up -d` keeps a running container whose image was just rebuilt: recreate PieFed's, or a re-run
    # of `up` (and `test`) runs the old code
    $COMPOSE up -d --force-recreate --no-deps piefed-web piefed-celery piefed-celery-background
    wait_http piefed-web https://piefed.test/health
    wait_http piefed-web https://mastodon.test/health   # piefed-web has curl; the Mastodon image may not
    wait_http piefed-web https://peertube.test/api/v1/ping
    # Castopod's boot migration loses the race with MariaDB and leaves no tables, so its login page answers 500 until
    # init-database (idempotent) has run; retry it until MariaDB accepts connections
    initialised=
    for _ in $(seq 1 60); do
        if $COMPOSE exec -T castopod-web php spark install:init-database > /dev/null 2>&1; then
            initialised=1
            break
        fi
        sleep 2
    done
    [ -n "$initialised" ] || { echo "run.sh: castopod init-database never succeeded" >&2; return 1; }
    wait_http piefed-web https://castopod.test/cp-auth/login
    # init-db drops every table first, so run it only before it has seeded the languages (a re-run of `up` keeps the
    # data). Not the instance table: the migrations `flask db upgrade` ran at start-up seed a row there. Not the site
    # table either: the first request makes Site(id=1) (D911), and wait_http above is a request.
    if [ "$($COMPOSE exec -T piefed-db psql -U piefed -tAc 'select count(*) from language' | tr -d '[:space:]')" = 0 ]; then
        $COMPOSE exec -T piefed-web sh -c 'FLASK_APP=pyfedi.py flask init-db --interactive no'
    fi
    # piped into `python -` (an interactive `flask shell` chokes on the blank-line rules of a pasted script)
    { printf 'from pyfedi import app\napp.app_context().push()\n'; cat interop/seed_piefed.py; } \
        | $COMPOSE exec -T piefed-web python -
    token=$($COMPOSE exec -T mastodon-web bin/rails runner - < interop/seed_mastodon.rb | tail -n 1)
    peertube=$($COMPOSE exec -T piefed-web python - < interop/seed_peertube.py | tail -n 1)
    # Castopod's image migrates at boot, but races MariaDB; init-database is idempotent. The superadmin prompt reads
    # its password twice from stdin, and the command exits 1 with "already created" once an owner exists.
    $COMPOSE exec -T castopod-web php spark install:init-database > /dev/null
    out=$(printf 'interop-admin-pw-1\ninterop-admin-pw-1\n' \
        | $COMPOSE exec -T castopod-web php spark install:create-superadmin -n admin -e admin@castopod.test) \
        || grep -q 'already created' <<< "$out" || { echo "$out" >&2; return 1; }
    castopod=$($COMPOSE exec -T piefed-web python - < interop/seed_castopod.py | tail -n 1)
    wait_http piefed-web https://pixelfed.test/api/nodeinfo/2.0.json
    pixelfed=$($COMPOSE exec -T pixelfed-web php artisan tinker --execute="$(sed 1d interop/seed_pixelfed.php)" \
        | sed -n 's/^TOKEN=//p')
    [ -n "$pixelfed" ] || { echo "run.sh: seed_pixelfed.php printed no token" >&2; return 1; }
    wait_http piefed-web https://loops.test/up
    wait_http piefed-web https://relay.test/actor   # seed_loops.php subscribes Loops to it
    seeded=$($COMPOSE exec -T loops-web php artisan tinker --execute="$(sed 1d interop/seed_loops.php)")
    loops=$(sed -n 's/^TOKEN=//p' <<< "$seeded")
    mia=$(sed -n 's/^MIA=//p' <<< "$seeded")
    [ -n "$loops" ] && [ -n "$mia" ] || { echo "run.sh: seed_loops.php printed no tokens" >&2; return 1; }
    { printf 'from pyfedi import app\napp.app_context().push()\n'; cat interop/seed_piefed_relay.py; } \
        | $COMPOSE exec -T -e SSL_CERT_FILE=/tmp/harness-ca.crt piefed-web python -
    # test_loops' clip, made by Loops' ffmpeg: Loops refuses an upload under 250 KB, and the fixtures are smaller
    if [ ! -s interop/.loops_clip.mp4 ]; then
        $COMPOSE exec -T loops-web sh -c 'ffmpeg -loglevel error -y -f lavfi -i testsrc2=size=720x1280:rate=30:duration=6 \
            -f lavfi -i sine=frequency=440:duration=6 -c:v libx264 -b:v 1500k -pix_fmt yuv420p -c:a aac -shortest \
            /tmp/interop_clip.mp4 && cat /tmp/interop_clip.mp4' > interop/.loops_clip.mp4
    fi
    printf '%s\n%s' "$peertube" "$castopod" | python3 -c 'import json, sys; t = {}; [t.update(json.loads(l)) for l in sys.stdin]; t["mastodon_bob"] = sys.argv[1]; t["pixelfed_erin"] = sys.argv[2]; t["loops_lucy"] = sys.argv[3]; t["loops_mia"] = sys.argv[4]; print(json.dumps(t))' \
        "$token" "$pixelfed" "$loops" "$mia" > interop/.tokens.json
}

case "${1:-}" in
    up)   up ;;
    test) shift; up; $COMPOSE --profile runner run --rm interop-runner \
              python -m pytest -c interop/pytest.ini interop "$@" ;;
    conformance) shift; up; $COMPOSE --profile conformance run --rm conformance-runner \
              interop/conformance/run.sh "$@" ;;
    down) $COMPOSE down -v
          # a container that survives keeps its app's state (Pixelfed remembering a follow of a PieFed database since
          # dropped), and the next `up` reuses it beside fresh ones: refuse to leave a mixed stack behind
          left=$(podman ps -a --filter name=pyfedi-interop_ --format '{{.Names}}')
          [ -z "$left" ] || { echo "run.sh: down left containers behind:" $left >&2; exit 1; } ;;
    *)    echo "usage: interop/run.sh up|test|conformance|down" >&2; exit 2 ;;
esac
