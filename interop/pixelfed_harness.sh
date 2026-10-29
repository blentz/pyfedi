#!/bin/sh
# Harness only. Pixelfed refuses every url whose host resolves outside the global address range
# (Helpers::isPublicIp, app/Util/ActivityPub/Helpers.php at v0.15.2) and has no setting to allow a private one, as
# Mastodon's ALLOWED_PRIVATE_ADDRESSES or PeerTube's PREVENT_SSRF do. Every peer here is on 10.90.0.0/16, so count
# that subnet as public, then exec the container's real command. Idempotent: the marker comment guards a re-run.
set -eu
f=/var/www/html/app/Util/ActivityPub/Helpers.php
if ! grep -q 'harness: interop subnet' "$f"; then
    sed -i '/public static function isPublicIp(string \$ip): bool/{n;a\        if (str_starts_with($ip, '"'"'10.90.'"'"')) { return true; } // harness: interop subnet
}' "$f"
    grep -q 'harness: interop subnet' "$f" || { echo "pixelfed_harness: isPublicIp patch did not apply" >&2; exit 1; }
fi
exec "$@"
