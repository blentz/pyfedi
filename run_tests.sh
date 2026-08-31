#!/usr/bin/env bash
# Run the test suite against disposable containers.
#
#   ./run_tests.sh                      # everything
#   ./run_tests.sh tests/test_foo.py -v # passed straight through to pytest
#   ./run_tests.sh --down               # stop and remove the containers
set -euo pipefail

cd "$(dirname "$0")"

COMPOSE="podman-compose -f compose.test.yaml"

if [ "${1:-}" = "--down" ]; then
    # podman-compose names the project after this directory, so this only stops
    # the stack belonging to THIS checkout. Another checkout's stack keeps running.
    $COMPOSE down
    exit 0
fi

# Waits for Postgres to accept connections. Returns non-zero if it never does.
# Used twice: after starting the stack, and again after a staleness reset.
wait_for_postgres() {
    printf 'Waiting for Postgres'
    for _ in $(seq 1 60); do
        if $COMPOSE exec -T test-db pg_isready -U pyfedi -d pyfedi_test >/dev/null 2>&1; then
            printf '\n'
            return 0
        fi
        printf '.'
        sleep 1
    done
    printf '\n'
    return 1
}

if ! $COMPOSE up -d; then
    echo "run_tests.sh: could not start the test stack. See the output above." >&2
    exit 1
fi

if ! wait_for_postgres; then
    {
        echo
        echo "run_tests.sh: Postgres did not become ready within 60 seconds."
        echo
        echo "Container status:"
        $COMPOSE ps 2>&1 || true
        echo
        echo "The usual cause is another checkout of this repository running its own"
        echo "test stack. podman-compose names the project after the directory, so a"
        echo "second checkout (a git worktree, for instance) starts a SEPARATE stack."
        echo "Check with:  podman ps"
        echo "Then stop the other one from ITS OWN directory:  ./run_tests.sh --down"
    } >&2
    exit 1
fi

# Reset the database once it has done enough work to have gone slow.
#
# tests/conftest.py's db_session teardown truncates all ~90 tables after EVERY
# test, so a full run issues a quarter of a million table truncations. Postgres
# degrades badly under that: measured 2026-08-31, one such TRUNCATE costs about
# 0.05ms against a fresh database and about 124ms after two or three full runs
# -- roughly 2500x. That turns a ~190s suite into one that cannot finish inside
# ten minutes, with no individual test slow enough to look suspicious, because
# the cost is spread evenly across every test's teardown.
#
# VACUUM does not recover it: VACUUM FULL on pg_class and friends left TRUNCATE
# at ~124ms. Only a fresh database helps. Restarting test-db discards the tmpfs
# volume, and the `flask db upgrade` below then rebuilds the schema in about 8
# seconds -- so this is cheap, and much cheaper than the run it saves.
#
# pg_class's size is the odometer, not the cause -- vacuuming it away does not
# make TRUNCATE fast again. It is used here only because it tracks how much
# relfilenode churn this database has seen and costs nothing to read: about
# 300kB fresh, tens of MB by the time TRUNCATE has gone slow.
#
# The probe is skipped for a database with no schema yet (a fresh volume), where
# there is nothing to be stale.
STALE_KB=${PYFEDI_TEST_STALE_KB:-4096}
catalog_kb=$($COMPOSE exec -T test-db psql -U pyfedi -d pyfedi_test -tAc \
    "select pg_total_relation_size('pg_class')/1024" 2>/dev/null | tr -dc '0-9') || true
if [ -n "${catalog_kb:-}" ] && [ "$catalog_kb" -gt "$STALE_KB" ]; then
    echo "Test database is stale (pg_class ${catalog_kb}kB > ${STALE_KB}kB); resetting it."
    $COMPOSE restart test-db
    if ! wait_for_postgres; then
        echo "run_tests.sh: Postgres did not come back after the staleness reset." >&2
        exit 1
    fi
fi

# Tests run inside test-runner: there is no host Python environment.
#
# This repository has ~269 migrations. `flask db upgrade` is a fast no-op while
# the database persists between runs, which is the normal case. It replays in
# full -- measured at about 8 seconds, not the minutes its count suggests --
# after `--down` or after the staleness reset above, because both leave an empty
# database behind.
echo "Applying migrations..."
$COMPOSE exec -T test-runner flask db upgrade

exec $COMPOSE exec -T test-runner pytest "$@"
