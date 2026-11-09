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
# Postgres's own account of a crashed backend: which signal ended it and the query it was running (run_tests.sh saves
# the log only when the run fails). Printed with the notice, so a crash is never read as a test failure.
postgres_crash=$(grep -hE 'terminated by signal|Failed process was running' "$diagnostics/postgres.log" 2>/dev/null \
                 | sed -E 's/\x1b\[[0-9;]*m//g; s/^[^|]*\| //' | cut -c1-240 || true)
if [ -n "$new" ] || [ -n "$postgres_crash" ] || \
        grep -qE 'Fatal Python error|worker .* crashed|server closed the connection unexpectedly' "$diagnostics/pytest.log"; then
    echo "run_tests_remote.sh: A CRASH during this run. Diagnostics are in $diagnostics. A clean rerun does not explain it." >&2
    [ -n "$new" ] && echo "$new" >&2
    [ -n "$postgres_crash" ] && echo "$postgres_crash" >&2
    [ "$status" -ne 0 ] || status=70
    # The tests that failed or errored in it, rerun once, alone (owner, 2026-11-09: the remote host's crashes are its own
    # to follow up; a crashed run's tests may be retried). The crash is still reported above and kept in $diagnostics;
    # coverage is appended to the run's, so the gates read the whole suite.
    mapfile -t failed < <(grep -E '^(FAILED|ERROR) tests/' "$diagnostics/pytest.log" | sed -E 's/^(FAILED|ERROR) //; s/ - .*$//' \
                          | sort -u)
    if [ "${#failed[@]}" -gt 0 ]; then
        retry_args=()
        for argument in "$@"; do
            case "$argument" in tests|tests/|tests/*) ;; *) retry_args+=("$argument") ;; esac
        done
        for argument in "$@"; do
            case "$argument" in --cov|--cov=*) retry_args+=(--cov-append); break ;; esac
        done
        echo "run_tests_remote.sh: rerunning the ${#failed[@]} test(s) that failed or errored in the crash, once, alone." >&2
        set +e
        CONTAINER_CONNECTION="$REMOTE_PODMAN" PYTEST_WORKERS=1 ./run_tests.sh "${retry_args[@]}" "${failed[@]}" 2>&1 \
            | tee "$diagnostics/retry.log"
        retry_status=${PIPESTATUS[0]}
        set -e
        if [ "$retry_status" -eq 0 ]; then
            echo "run_tests_remote.sh: the rerun passed. The crash stays on record in $diagnostics." >&2
            status=0
        else
            echo "run_tests_remote.sh: the rerun failed too: these are not the crash's alone (see $diagnostics/retry.log)." >&2
            status=$retry_status
        fi
    fi
elif [ "$status" -eq 0 ]; then
    rm -rf "$diagnostics"
fi

if remote "test -f '$PWD/coverage.json'"; then
    remote "cat '$PWD/coverage.json'" > coverage.json
fi
remote "rm -rf '$PWD'"
exit "$status"
