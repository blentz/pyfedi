#!/usr/bin/env bash
# run_tests.sh on another machine's podman, for when this one hasn't the memory. Arguments go straight to pytest.
#
#   ./run_tests_remote.sh tests/test_foo.py -v
#   PYTEST_WORKERS=7 ./run_tests_remote.sh --cov=app --cov-branch --cov-report=json:coverage.json
#
# REMOTE_PODMAN names a connection from `podman system connection list`. Its user must have this checkout's path free,
# since compose.test.yaml's bind mount is resolved on that machine: the copy there is replaced on every run, and removed
# after it. Only what git tracks or would track goes across, never an ignored file that may hold a real secret, plus
# .env.test. This .git stays here, with its config, stashes and history: the copy gets an index of its own, for the tests
# that ask git which files there are. So a test or gate that reads commits runs here, not there. coverage.json comes back
# if the run wrote one.
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
  printf '%s\0' .env.test
} | tar --null -T - -czf - | remote "tar -xzf - -C '$PWD' && cd '$PWD' && git init -q && git add -A"

# What a crash leaves behind, kept here: Postgres's log, the kernel's crash lines since the run began, and pytest's output.
crashes() { remote "dmesg 2>/dev/null | grep -iE 'segfault|general protection|traps:|oom' || true"; }
diagnostics=".crash-diagnostics/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$diagnostics"
crashes > "$diagnostics/kernel-before.log" || true

set +e
CONTAINER_CONNECTION="$REMOTE_PODMAN" TEST_DIAGNOSTICS_DIR="$diagnostics" ./run_tests.sh "$@" 2>&1 | tee "$diagnostics/pytest.log"
status=${PIPESTATUS[0]}
set -e
crashes > "$diagnostics/kernel-after.log" || true
new=$(comm -13 <(sort "$diagnostics/kernel-before.log") <(sort "$diagnostics/kernel-after.log") | tee "$diagnostics/kernel-new.log")
if [ -n "$new" ] || grep -qE 'Fatal Python error|worker .* crashed|server closed the connection unexpectedly' "$diagnostics/pytest.log"; then
    echo "run_tests_remote.sh: A CRASH during this run. Diagnostics are in $diagnostics. A clean rerun does not explain it." >&2
    [ -n "$new" ] && echo "$new" >&2
    [ "$status" -ne 0 ] || status=70
elif [ "$status" -eq 0 ]; then
    rm -rf "$diagnostics"
fi

if remote "test -f '$PWD/coverage.json'"; then
    remote "cat '$PWD/coverage.json'" > coverage.json
fi
remote "rm -rf '$PWD'"
exit "$status"
