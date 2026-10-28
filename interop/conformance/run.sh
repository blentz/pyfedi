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
        cd "$(dirname "$0")"   # every path below is relative to interop/conformance, wherever the caller stands
        rm -rf results && mkdir -p results/plans
        while read -r name url; do
            activitypub-testing test actor "$url" > "results/aptest-$name.ndjson"
        done < <(actors)
        feditest run --testsdir /opt/feditest-tests-fediverse/tests --testplan feditest/webfinger.testplan.json \
            --tap results/webfinger.tap --json results/webfinger.json --html results/webfinger.html </dev/null || true   # the gate judges the outcomes, not feditest's exit code
        [ -s results/webfinger.json ] || { echo "conformance: feditest wrote no results/webfinger.json" >&2; exit 1; }   # no test is baselined, so the aptest results alone would pass
        token=$(jq -r .mastodon_bob ../.tokens.json)   # per stack, so the committed template carries none and the plans are rendered here
        for dir in piefed-to-mastodon mastodon-to-piefed; do   # the template's sender is PieFed; the other direction swaps the two roles
            (cd ../.. && python -m interop.conformance.reset_state)   # each direction starts without the follows the other left
            jq --arg tok "$token" --arg dir "$dir" '.constellations[0].roles.receiver_node.accounts[0].oauth_token = $tok | .name = $dir
                | if $dir == "mastodon-to-piefed" then .constellations[0].roles |= {sender_node: .receiver_node, receiver_node: .sender_node} else . end' \
                feditest/system2.testplan.template.json > "results/plans/system2-$dir.json"
            feditest run --testsdir /opt/feditest-tests-fediverse/tests --nodedriversdir feditest/nodedrivers --testplan "results/plans/system2-$dir.json" \
                --tap "results/system2-$dir.tap" --json "results/system2-$dir.json" --html "results/system2-$dir.html" </dev/null || true
            [ -s "results/system2-$dir.json" ] || { echo "conformance: feditest wrote no results/system2-$dir.json" >&2; exit 1; }
        done
        cd ../..
        python -m interop.conformance.baseline interop/conformance/results ;;
    *) echo "usage: interop/conformance/run.sh [--version-check]" >&2; exit 2 ;;
esac
