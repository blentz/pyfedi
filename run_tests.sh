#!/usr/bin/env bash
# Run the test suite against disposable containers. The stack lives only as
# long as the run: it is started here and removed on exit, pass or fail.
#
#   ./run_tests.sh                      # everything
#   ./run_tests.sh tests/test_foo.py -v # passed straight through to pytest
#   ./run_tests.sh --down               # remove a stack left by a killed run
set -euo pipefail

cd "$(dirname "$0")"

COMPOSE="podman-compose -f compose.test.yaml"

if [ "${1:-}" = "--down" ]; then
    # podman-compose names the project after this directory, so this only stops
    # the stack belonging to THIS checkout. Another checkout's stack keeps running.
    $COMPOSE down
    exit 0
fi

# The client-side tests (package.json's test:js) need only node, on the host: the test
# container has none. They take about a second and fail below 100% coverage of live_feed.js.
if command -v node >/dev/null 2>&1; then
    npm run --silent test:js
else
    echo "run_tests.sh: node not found; the JavaScript tests did not run." >&2
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

# One run at a time per checkout. The stack is shared by name, so a second
# concurrent run would have it torn down underneath it when the first one exits.
# The second run waits here instead. The lock lives in the git dir, which is
# per-worktree, so other checkouts still run in parallel. (No flock on macOS:
# the runs are then not serialised, as before.)
if command -v flock >/dev/null 2>&1; then
    exec 9>"$(git rev-parse --git-dir)/run_tests.lock"
    if ! flock -n 9; then
        echo "run_tests.sh: another run in this checkout holds the test stack; waiting." >&2
        flock 9
    fi
fi

# The stack must not outlive the run. EXIT fires on success, failure and
# Ctrl-C alike; `down` also stops a pytest still running inside test-runner.
teardown() {
    $COMPOSE down >/dev/null 2>&1 || echo "run_tests.sh: could not remove the test stack; run ./run_tests.sh --down" >&2
}
trap teardown EXIT

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
# This repository has ~269 migrations. Every run starts from an empty tmpfs
# database, so they replay in full each time -- measured at about 8 seconds, not
# the minutes their count suggests. That is the price of a stack that does not
# outlive the run.
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
# else that is not an option does.
#
# Options that take a SEPARATE value have to consume it, or the value is read as
# a path and the whole suite silently drops to one worker. `-p no:randomly` did
# exactly that: `-p` was listed beside -n and --dist as if it conflicted with
# them, so `./run_tests.sh tests/ -q -p no:randomly` ran the 12,000-test suite
# serially -- 23 minutes instead of 8 -- and said nothing about why.
#
# -n and --dist stay here because they genuinely conflict: this script passes its
# own, and pytest takes the last one. -k and -m name a subset by definition.
names_a_subset() {
    skip_next=false
    for argument in "$@"; do
        if [ "$skip_next" = true ]; then
            skip_next=false
            continue
        fi
        case "$argument" in
            -n|-n*|--dist|--dist=*) return 0 ;;
            -k|-k*|-m|-m*) return 0 ;;
            -p|-c|-o|--rootdir|--override-ini|--deselect|--ignore|--junit-xml|--log-file)
                skip_next=true ;;
            -*) ;;
            tests|tests/) ;;
            *) return 0 ;;
        esac
    done
    return 1
}

# `--max-worker-restart 0`, so a worker that dies FAILS THE RUN. Without it xdist
# silently starts a replacement, and a run whose workers kept dying reported
# "5 failed, 7073 passed" -- 4,800 tests short -- followed by an INTERNALERROR
# from its own scheduler. A short count is not a pass, and this makes it say so.
if [ "$WORKERS" -gt 1 ] && ! names_a_subset "$@"; then
    set -- -n "$WORKERS" --dist loadgroup --max-worker-restart 0 "$@"
fi

# Not `exec`: the shell has to survive pytest so the EXIT trap removes the stack.
# pytest's exit status is still the script's.
status=0
$COMPOSE exec -T test-runner pytest "$@" || status=$?
exit "$status"
