#!/usr/bin/env bash
# Generate interop/.env.interop (gitignored) with fresh secrets. Idempotent.
set -euo pipefail
cd "$(dirname "$0")"
hex() { openssl rand -hex "$1"; }
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
    printf 'APP_KEY=base64:%s\n' "$(openssl rand -base64 32)" > .env.pixelfed
fi
# Loops joined the harness later: its own env file (Laravel APP_KEY), for Pixelfed's reason
if [ ! -f .env.loops ]; then
    printf 'APP_KEY=base64:%s\n' "$(openssl rand -base64 32)" > .env.loops
fi
