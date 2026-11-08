#!/usr/bin/env bash
# interop/run.sh on another machine's podman: the stack is heavy, and the owner keeps test containers off the laptop.
#
#   interop/run_remote.sh apps               the client-app harness (interop/apps/README.md)
#   interop/run_remote.sh test -k pixelfed   any interop/run.sh command and its arguments
#
# REMOTE_PODMAN names a connection from `podman system connection list` (default remote-gpu); its host must answer ssh
# with podman, python3 and tar. The checkout goes to REMOTE_INTEROP_DIR there (default ~/pyfedi-interop): what git
# tracks or would track, never an ignored file that may hold a real secret. The stack's generated state (the .env files,
# .tokens.json, the Loops clip) is the remote stack's own and stays there between runs, since its volumes were made
# with those secrets. podman-compose, which the host's distribution lacks, is installed into a venv beside the copy.
# Each run's results (interop/apps/results, interop/conformance/results) come back here.
set -euo pipefail

cd "$(dirname "$0")/.."

REMOTE_PODMAN="${REMOTE_PODMAN:-remote-gpu}"
REMOTE_INTEROP_DIR="${REMOTE_INTEROP_DIR:-pyfedi-interop}"   # relative to the remote home

uri=$(podman system connection list --format '{{.Name}} {{.URI}}' | awk -v name="$REMOTE_PODMAN" '$1 == name {print $2}')
if [ -z "$uri" ]; then
    echo "run_remote.sh: no podman connection named $REMOTE_PODMAN." >&2
    exit 1
fi
authority=${uri#ssh://}
authority=${authority%%/*}
port=22
case "$authority" in
    *:*) port=${authority##*:}; authority=${authority%:*} ;;
esac
remote() { ssh -p "$port" -o BatchMode=yes "$authority" "$@"; }

# the generated files that survive a re-copy, relative to the checkout
STATE="interop/.env.interop interop/.env.pixelfed interop/.env.loops interop/.tokens.json interop/.loops_clip.mp4"

echo "Copying this checkout to $REMOTE_PODMAN:~/$REMOTE_INTEROP_DIR..."
remote "set -e; d=\$HOME/$REMOTE_INTEROP_DIR; s=\$HOME/$REMOTE_INTEROP_DIR.state; rm -rf \"\$s\"; mkdir -p \"\$s/interop\" \"\$d\";
        for f in $STATE; do [ -e \"\$d/\$f\" ] && mv \"\$d/\$f\" \"\$s/\$f\"; done; rm -rf \"\$d\"; mkdir -p \"\$d\"; mv \"\$s/interop\" \"\$d/interop\"; rmdir \"\$s\""
git ls-files -co --exclude-standard -z | while IFS= read -r -d '' file; do
    [ -e "$file" ] && printf '%s\0' "$file"
done | tar --null -T - -czf - | remote "tar -xzf - -C \"\$HOME/$REMOTE_INTEROP_DIR\""

venv="\$HOME/.local/share/pyfedi-interop/venv"
remote "set -e; v=$venv; [ -x \"\$v/bin/podman-compose\" ] || python3 -m venv \"\$v\"; \"\$v/bin/pip\" install -q -U --disable-pip-version-check podman-compose"

set +e
remote "cd \"\$HOME/$REMOTE_INTEROP_DIR\" && PATH=$venv/bin:\$PATH interop/run.sh $(printf '%q ' "$@")"
status=$?
set -e

for results in interop/apps/results interop/conformance/results; do
    if remote "test -d \"\$HOME/$REMOTE_INTEROP_DIR/$results\""; then
        rm -rf "$results"
        mkdir -p "$results"
        remote "tar -czf - -C \"\$HOME/$REMOTE_INTEROP_DIR/$results\" ." | tar -xzf - -C "$results"
        echo "run_remote.sh: results in $results"
    fi
done
exit "$status"
