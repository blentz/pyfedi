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

if ! $COMPOSE up -d; then
    echo "run_tests.sh: could not start the test stack. See the output above." >&2
    exit 1
fi

ready=
printf 'Waiting for Postgres'
for _ in $(seq 1 60); do
    if $COMPOSE exec -T test-db pg_isready -U pyfedi -d pyfedi_test >/dev/null 2>&1; then
        ready=1
        break
    fi
    printf '.'
    sleep 1
done
printf '\n'

if [ -z "$ready" ]; then
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

# Tests run inside test-runner: there is no host Python environment.
#
# This repository has ~269 migrations. `flask db upgrade` is a fast no-op while the
# database persists between runs, which is the normal case. It is slow exactly once
# after `--down`, because that destroys the tmpfs volume and every migration has to
# replay against an empty database. If this step is taking minutes, that is why.
echo "Applying migrations..."
$COMPOSE exec -T test-runner flask db upgrade

exec $COMPOSE exec -T test-runner pytest "$@"
