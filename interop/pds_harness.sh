#!/bin/sh
# Harness only. The PDS refuses an OAuth client id whose top-level domain is test, local, localhost, invalid or example
# (@atproto/oauth-types isLocalHostname, PDS 0.4.5051), and the harness PieFed is piefed.test. Let `test` through,
# then exec the container's real command. Production PieFed is on a public domain and needs nothing of this.
# Idempotent: the marker comment guards a re-run.
set -eu
marker='harness: test TLD allowed'
for f in /app/node_modules/.pnpm/@atproto+oauth-types@*/node_modules/@atproto/oauth-types/dist/util.js; do
    if ! grep -q "$marker" "$f"; then
        sed -i "s#return (tld === 'test' ||#return ( /* $marker */#" "$f"
        grep -q "$marker" "$f" || { echo "pds_harness: isLocalHostname patch did not apply to $f" >&2; exit 1; }
    fi
done
exec "$@"
