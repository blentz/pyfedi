#!/usr/bin/env bash
# Conformance runner entry point: the suites' checks of PieFed's actors, then the baseline gate over the results.
set -euo pipefail

actors() {  # one "name url" per line
    echo "alice https://piefed.test/u/alice"
    echo "microblogs https://piefed.test/c/microblogs"
    echo "conf-feed https://piefed.test/f/conf-feed"
    echo "instance https://piefed.test/actor"
}

case "${1:-}" in
    --version-check) feditest version; activitypub-testing help | head -1 ;;
    "")
        cd interop/conformance
        rm -rf results && mkdir results
        while read -r name url; do
            activitypub-testing test actor "$url" > "results/aptest-$name.ndjson"
        done < <(actors)
        feditest run --testsdir /opt/feditest-tests-fediverse/tests --testplan feditest/webfinger.testplan.json \
            --tap results/webfinger.tap --json results/webfinger.json --html results/webfinger.html </dev/null || true   # the gate judges the outcomes, not feditest's exit code
        [ -s results/webfinger.json ] || { echo "conformance: feditest wrote no results/webfinger.json" >&2; exit 1; }   # no test is baselined, so the aptest results alone would pass
        cd ../..
        python -m interop.conformance.baseline interop/conformance/results ;;
    *) echo "usage: interop/conformance/run.sh [--version-check]" >&2; exit 2 ;;
esac
