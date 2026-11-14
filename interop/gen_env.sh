#!/usr/bin/env bash
# Generate interop/.env.interop (gitignored) with fresh secrets. Idempotent.
set -euo pipefail
cd "$(dirname "$0")"
# python3, not openssl: the remote podman host interop/run_remote.sh uses has no openssl
hex() { python3 -c 'import secrets, sys; print(secrets.token_hex(int(sys.argv[1])))' "$1"; }
b64() { python3 -c 'import base64, secrets, sys; print(base64.b64encode(secrets.token_bytes(int(sys.argv[1]))).decode())' "$1"; }
if [ ! -f .env.interop ]; then
cat > .env.interop <<EOT
# PieFed
SERVER_NAME=piefed.test
SECRET_KEY=$(hex 32)
CACHE_TYPE=RedisCache
CACHE_REDIS_URL=redis://piefed-redis:6379/0
CELERY_BROKER_URL=redis://piefed-redis:6379/1
ENABLE_ALPHA_API=true
ALLOWED_PRIVATE_NETWORKS=10.90.0.0/16
# Mastodon
LOCAL_DOMAIN=mastodon.test
RAILS_ENV=production
DB_HOST=mastodon-db
DB_USER=mastodon
DB_PASS=mastodon
DB_NAME=mastodon
REDIS_HOST=mastodon-redis
SECRET_KEY_BASE=$(hex 64)
OTP_SECRET=$(hex 64)
ACTIVE_RECORD_ENCRYPTION_DETERMINISTIC_KEY=$(hex 16)
ACTIVE_RECORD_ENCRYPTION_KEY_DERIVATION_SALT=$(hex 16)
ACTIVE_RECORD_ENCRYPTION_PRIMARY_KEY=$(hex 16)
ALLOWED_PRIVATE_ADDRESSES=10.90.0.0/16
EOT
echo "gen_env: wrote interop/.env.interop (VAPID keys are added by run.sh up)"
fi
# PeerTube joined the harness later: add its secret to an older .env.interop too
if ! grep -q '^PEERTUBE_SECRET=' .env.interop; then
    printf '# PeerTube\nPEERTUBE_SECRET=%s\n' "$(hex 32)" >> .env.interop
fi
# Castopod joined the harness later: its analytics salt is mandatory
if ! grep -q '^CP_ANALYTICS_SALT=' .env.interop; then
    printf '# Castopod\nCP_ANALYTICS_SALT=%s\n' "$(hex 32)" >> .env.interop
fi
# Pixelfed joined the harness later: its own env file (Laravel APP_KEY), apart from .env.interop's Mastodon DB_HOST
if [ ! -f .env.pixelfed ]; then
    printf 'APP_KEY=base64:%s\n' "$(b64 32)" > .env.pixelfed
fi
# Loops joined the harness later: its own env file (Laravel APP_KEY), for Pixelfed's reason
if [ ! -f .env.loops ]; then
    printf 'APP_KEY=base64:%s\n' "$(b64 32)" > .env.loops
fi
# The Bluesky PDS joined the harness later (connected accounts): its JWT secret, admin password and PLC rotation key
# (any 32 random bytes are a secp256k1 private key, short of a 2^-128 chance)
if ! grep -q '^PDS_JWT_SECRET=' .env.interop; then
    printf '# Bluesky PDS\nPDS_JWT_SECRET=%s\nPDS_ADMIN_PASSWORD=%s\nPDS_PLC_ROTATION_KEY_K256_PRIVATE_KEY_HEX=%s\n' \
        "$(hex 32)" "$(hex 16)" "$(hex 32)" >> .env.interop
fi
# Connected accounts: members' tokens are sealed under a Fernet key (32 random bytes, urlsafe base64)
if ! grep -q '^CONNECTIONS_SECRETS_KEY=' .env.interop; then
    printf 'CONNECTIONS_SECRETS_KEY=%s\n' "$(python3 -c 'import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')" >> .env.interop
fi
# Loops' dropped first Follow is re-sent after this many seconds (default 300): test_loops waits on it
if ! grep -q '^LOOPS_FOLLOW_RESEND_SECONDS=' .env.interop; then
    printf '# Loops\nLOOPS_FOLLOW_RESEND_SECONDS=10\n' >> .env.interop
fi
