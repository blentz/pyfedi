#!/usr/bin/env bash
# run_tests.sh on another machine's podman, for when this one hasn't the memory. Arguments go straight to pytest.
#
#   ./run_tests_remote.sh tests/test_foo.py -v
#   PYTEST_WORKERS=7 ./run_tests_remote.sh --cov=app --cov-branch --cov-report=json:coverage.json
#
# REMOTE_PODMAN names a connection from `podman system connection list`. Its user must have this checkout's path free,
# since compose.test.yaml's bind mount is resolved on that machine: the copy there is replaced on every run. Only what git
# tracks or would track goes across (never an ignored file holding a real secret), plus .env.test, and .git for the tests
# that ask git. coverage.json comes back if the run wrote one.
set -euo pipefail

cd "$(dirname "$0")"

REMOTE_PODMAN="${REMOTE_PODMAN:-remote-gpu}"
IMAGE=localhost/pyfedi_test-runner:latest

uri=$(podman system connection list --format '{{.Name}} {{.URI}}' | awk -v name="$REMOTE_PODMAN" '$1 == name {print $2}')
if [ -z "$uri" ]; then
    echo "run_tests_remote.sh: no podman connection named $REMOTE_PODMAN." >&2
    exit 1
fi
authority=${uri#ssh://}
authority=${authority%%/*}
port=22
case "$authority" in
    *:*) port=${authority##*:}; authority=${authority%:*} ;;
esac
remote() { ssh -p "$port" -o BatchMode=yes "$authority" "$@"; }

if ! podman --connection "$REMOTE_PODMAN" image exists "$IMAGE"; then
    echo "Copying the test image to $REMOTE_PODMAN..."
    podman save "$IMAGE" | podman --connection "$REMOTE_PODMAN" load
fi

echo "Copying this checkout to $REMOTE_PODMAN..."
remote "rm -rf '$PWD' && mkdir -p '$PWD'"
{ git ls-files -co --exclude-standard -z | while IFS= read -r -d '' file; do
      [ -e "$file" ] && printf '%s\0' "$file"
  done
  printf '%s\0' .env.test .git
} | tar --null -T - -czf - | remote "tar -xzf - -C '$PWD'"

status=0
CONTAINER_CONNECTION="$REMOTE_PODMAN" ./run_tests.sh "$@" || status=$?

if remote "test -f '$PWD/coverage.json'"; then
    remote "cat '$PWD/coverage.json'" > coverage.json
fi
exit "$status"
