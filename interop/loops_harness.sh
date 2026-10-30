#!/bin/sh
# Harness only. Loops refuses every url whose host resolves outside the global address range and has no setting to
# allow a private one: SanitizeService::isPrivateOrReservedIp (url validation for actors, objects and media) and
# ProcessRemoteVideoJob::validateUrlHost (the remote video download), at v1.0.0-beta.15. Every peer here is on
# 10.90.0.0/16, so count that subnet as public, then exec the container's real command. Idempotent: the marker
# comment guards a re-run.
set -eu
marker='harness: interop subnet'
f=/var/www/html/app/Services/SanitizeService.php
if ! grep -q "$marker" "$f"; then
    sed -i '/private function isPrivateOrReservedIp(string \$ip): bool/{n;a\        if (str_starts_with($ip, '"'"'10.90.'"'"')) { return false; } // harness: interop subnet
}' "$f"
    grep -q "$marker" "$f" || { echo "loops_harness: SanitizeService patch did not apply" >&2; exit 1; }
fi
f=/var/www/html/app/Jobs/Federation/ProcessRemoteVideoJob.php
if ! grep -q "$marker" "$f"; then
    sed -i '/protected function validateUrlHost(string \$url): void/,/^    }/{
/foreach (\$ips as \$ip) {/a\            if (str_starts_with($ip, '"'"'10.90.'"'"')) { continue; } // harness: interop subnet
}' "$f"
    grep -q "$marker" "$f" || { echo "loops_harness: ProcessRemoteVideoJob patch did not apply" >&2; exit 1; }
fi
exec "$@"
