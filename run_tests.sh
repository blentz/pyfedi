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
# Called once, at the only place the stack is started. (It used to be called
# twice -- the second call followed the staleness reset described below, which
# was removed with the TRUNCATE teardown in ec98595c.)
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

# NOTE: there used to be a staleness reset here -- a pg_class size probe that
# restarted test-db once the catalog had grown past a threshold. It existed
# because the db_session teardown TRUNCATEd all ~90 tables after every test, and
# TRUNCATE allocates a fresh relfilenode per table. That churn drove the catalog
# up and each teardown from ~76ms on a fresh database to ~124ms, which is what
# pushed the suite past pytest.ini's session_timeout.
#
# The teardown now uses DELETE with FK triggers disabled instead (see
# tests/conftest.py), which touches no catalog at all: measured 2026-09-06 on an
# identical fresh stack, the same 629-test subset went from 124.5ms mean
# teardown / 55.13s to 5.2ms / 16.18s. With the churn gone there is nothing for
# a staleness reset to detect, so it was removed rather than left to fire on
# nothing and pay for an 8-second migration replay.

# Tests run inside test-runner: there is no host Python environment.
#
# This repository has ~269 migrations. `flask db upgrade` is a fast no-op while
# the database persists between runs, which is the normal case. It replays in
# full -- measured at about 8 seconds, not the minutes its count suggests --
# after `--down`, because that leaves an empty database behind. (It used to
# replay after the staleness reset described above as well; that reset was
# removed with the TRUNCATE teardown, so `--down` is now the only trigger.)
echo "Applying migrations..."
$COMPOSE exec -T test-runner flask db upgrade

# Parallel, but only for a WHOLE-suite run.
#
# Each worker builds its own copy of the database (tests/conftest.py's
# build_worker_database) and takes its own pair of Redis databases, because the
# suite's per-test teardown DELETEs every row and resets every sequence -- two
# workers against one database would wipe each other's rows mid-test. That copy
# costs about a second per worker, which is nothing against a 35-minute suite
# and most of the cost of a single-file run, so a run that NAMES A PATH stays
# serial and behaves exactly as it always has. That keeps every mutation pass
# (which runs one file, hundreds of times) on the old, cheap path.
#
# PYTEST_WORKERS overrides the count; 0 or 1 forces serial. Seven is the ceiling
# because Redis ships with 16 databases and each worker takes two of them.
WORKERS="${PYTEST_WORKERS:-4}"
if [ "$WORKERS" -gt 7 ]; then
    echo "run_tests.sh: PYTEST_WORKERS is capped at 7 (Redis has 16 databases," >&2
    echo "and each worker takes two). Using 7." >&2
    WORKERS=7
fi

# True when the caller asked for a SUBSET of the suite -- a file, a node id, a
# -k expression -- or has already said how it wants to be distributed. `tests`
# and `tests/` are the whole suite, so they do not count as a subset; anything
# else that is not an option does. A value that follows -k or -m looks like a
# path here, which keeps those runs serial: they are subsets anyway.
names_a_subset() {
    for argument in "$@"; do
        case "$argument" in
            -n|-n*|--dist|--dist=*|-p) return 0 ;;
            -*) ;;
            tests|tests/) ;;
            *) return 0 ;;
        esac
    done
    return 1
}

if [ "$WORKERS" -gt 1 ] && ! names_a_subset "$@"; then
    exec $COMPOSE exec -T test-runner pytest -n "$WORKERS" --dist loadgroup "$@"
fi

exec $COMPOSE exec -T test-runner pytest "$@"
