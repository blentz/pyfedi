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
    $COMPOSE down
    exit 0
fi

$COMPOSE up -d

echo "Waiting for Postgres..."
for _ in $(seq 1 60); do
    if $COMPOSE exec -T test-db pg_isready -U pyfedi -d pyfedi_test >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
$COMPOSE exec -T test-db pg_isready -U pyfedi -d pyfedi_test >/dev/null

# Tests run inside test-runner: there is no host Python environment.
$COMPOSE exec -T test-runner flask db upgrade
exec $COMPOSE exec -T test-runner pytest "$@"
