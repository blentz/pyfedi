#!/usr/bin/env bash
# Conformance runner entry point (replaced by the full runner in a later task).
set -euo pipefail

case "${1:-}" in
    --version-check) feditest version; activitypub-testing help | head -1 ;;
    *) echo "usage: interop/conformance/run.sh --version-check" >&2; exit 2 ;;
esac
