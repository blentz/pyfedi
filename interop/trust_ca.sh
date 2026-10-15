#!/bin/sh
# Trust the harness CA (Caddy's internal root), then exec the container's real command.
set -eu
ROOT=/caddy-data/harness-root.crt
i=0
while [ ! -s "$ROOT" ]; do
    i=$((i + 1)); [ "$i" -gt 30 ] && { echo "trust_ca: no CA at $ROOT after 30s" >&2; exit 1; }
    sleep 1
done
cat /etc/ssl/certs/ca-certificates.crt "$ROOT" > /tmp/harness-ca.crt
export SSL_CERT_FILE=/tmp/harness-ca.crt
export REQUESTS_CA_BUNDLE=/tmp/harness-ca.crt
# certifi is root-owned in the PieFed image (runs as user python): appending is best effort.
# httpx honours SSL_CERT_FILE, so the bundle above is what actually matters.
if command -v python >/dev/null 2>&1 && python -c 'import certifi' 2>/dev/null; then
    cat "$ROOT" >> "$(python -c 'import certifi; print(certifi.where())')" 2>/dev/null || true
fi
exec "$@"
