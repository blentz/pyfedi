# Interop harness

Runs PieFed (built from this tree), Mastodon, PeerTube, Castopod and Pixelfed side by side, behind Caddy with an internal CA,
on the names `piefed.test`, `mastodon.test`, `peertube.test`, `castopod.test` and `pixelfed.test`. Later interop tests drive real servers against each other here.
See `compose.interop.yaml`; spec 2026-10-14 section 8.1.

## Use

    interop/run.sh up                   start and seed the stack (users alice, bob and carol; tokens in interop/.tokens.json)
    interop/run.sh conformance [args]   up, then interop/conformance/run.sh in the conformance-runner container
    interop/run.sh test [args]          up, then pytest interop/ in the interop-runner container (args pass through)
    interop/run.sh down                 remove the stack and its volumes

Seeded: PieFed `alice` / `interop-alice-pw`, the local `microblogs` community, the `video_channel` community `vc1`, and
the media upload communities `vc_media` (`video_channel`) and `pod_media` (`podcast`), which alice moderates, with video
and audio uploads open to users; Mastodon `bob`; PeerTube admin
`root` / `interop-root-pw` and user `carol` / `interop-carol-pw`; Castopod superadmin `admin@castopod.test` /
`interop-admin-pw-1` and podcast `@dave` with one published episode (REST API basic auth `interop` / `interop-api-pw`);
Pixelfed user `erin` / `interop-erin-pw` (`interop/seed_pixelfed.php`).
`interop/.tokens.json` holds the API tokens `mastodon_bob`, `pixelfed_erin` (Mastodon-compatible API), `peertube_root` and `peertube_carol` (PeerTube OAuth
access tokens last a day; `up` renews them), and the URLs `castopod_podcast` and `castopod_episode`.
`interop/.env.interop` (secrets), `interop/.env.pixelfed` (Pixelfed's `APP_KEY`) and `interop/.tokens.json` are generated and gitignored. `up` is safe to repeat.

## Tests

- `test_smoke.py`: TLS, DNS, webfinger, a federated follow both ways, and Castopod's podcast and episode as ActivityPub.
- `test_microblog_mastodon.py`: a local microblogs post shows on Mastodon, and replies, favourites and boosts land in PieFed.
- `test_peertube_subscribe.py`: a PeerTube user subscribes to a `video_channel` and PeerTube sees it accepted.
- `test_media_pipeline.py`: video and audio uploads are probed, published and served in ranges.
- `test_podcast.py`: an audio episode uploaded to `pod_media` plays on Mastodon as an audio attachment, a Mastodon reply lands in the PieFed thread, and the community feed carries the episode's enclosure, duration and guid.
- `test_pixelfed.py`: a PieFed album (one picture a WebP, sent as its JPEG) shows whole in Pixelfed erin's home feed,
  with each alt text; an edit and a delete reach Pixelfed; Pixelfed's reply, like and boost come back; a Pixelfed album
  arrives whole, a sensitive one behind its warning, and Pixelfed's edit and delete reach PieFed.
- `test_video_channel.py`: a video uploaded to `vc_media` plays on PeerTube under the subscribed channel (duration,
  file URL, thumbnail), a PeerTube like comes back as an upvote, an edit renames it, a delete removes it, and a
  restore creates it again.

## Conformance suites

`interop/run.sh conformance` brings the stack up and runs `interop/conformance/run.sh` in the `conformance-runner`
container (`interop/conformance/Containerfile`; the base image is pinned by digest and each tool to an exact SHA or version, but
what those tools pull in is not: FediTest's and httpx's own Python dependencies, activitypub-testing's npm tree
(it ships no shrinkwrap) and the apt packages `nodejs`, `npm`, `jq` and `git` all resolve when the image is built). It is a local gate,
not wired to CI, like `test`. It runs three suites and then `python -m interop.conformance.baseline`, which exits 0
only when every result matches `interop/conformance/baseline.json`:

- `activitypub-testing@0.16.1` checks four actors, `alice`, the `microblogs` community, the feed `conf-feed` and the
  instance actor, into `results/aptest-<actor>.ndjson`.
- FediTest's WebFinger server tests write `results/webfinger.{tap,json,html}`.
- FediTest's `system2` tests run PieFed against Mastodon once in each direction, with PieFed as the sender and then as
  the receiver (`feditest/system2.testplan.template.json`, with the in-repo node driver `feditest/nodedrivers/piefed.py`
  and the stock Mastodon driver), into `results/system2-<piefed-to-mastodon|mastodon-to-piefed>.{tap,json,html}`.
  The driver is also unit-tested against the test database (`tests/test_piefed_node_driver.py`): the unit-test image
  installs FediTest at the same pinned commit (`requirements-test.txt`), so a change to the pin goes in both places.

`results/` is gitignored and rebuilt on every run. Read `results/*.html` for the FediTest runs. For the actors, list
what did not pass:

    jq 'select(.result.outcome != "passed")' interop/conformance/results/aptest-alice.ndjson

The gate reads FediTest's JSON, not its TAP, because the TAP writer reports a skipped test as `ok`.

Between the two `system2` directions `reset_state.py` undoes every follow between alice and bob (a Mastodon note reaches
PieFed only through a follow, so one direction's follows would change what the other sees) and ages bob: PieFed refuses
a post from a remote account under a day old once it has more than 3 posts (`app.utils.can_create_post`), and the tests
make a dozen. It sets bob's `created` 30 days back.

A missing transcript fails the run: `run.sh` exits 1 when `results/webfinger.json` or a `system2` JSON is absent or
empty, since a run that wrote nothing has nothing to compare and could otherwise pass on the actor results alone.

FediTest prints its own `FAILED.` lines while baselined failures exist, and its exit code is ignored. The checker's exit
code is the verdict. It exits 1, naming the test, when:

- a test fails or errors and is not in the baseline (a skip must be baselined too, so new skips are noticed),
- a baselined test now passes (remove the entry), did not run, or ended in another outcome than the entry records,
- a baselined FediTest failure or error is now somewhere else than the entry's `at` ("fails elsewhere"), or
- there are no results at all, or an entry has no `reason`.

To change the baseline, edit `baseline.json`: the key is the test id as the checker prints it, `outcome` is `failed`,
`error` or `skipped` for FediTest and `inapplicable` or `cantTell` (the EARL outcome, kept distinct) for
`activitypub-testing`, and `reason` is required. A FediTest entry that is `failed` or `error` also carries `at`, the
`<file>.py:<line>` where the test fails (the innermost stacktrace frame under the tests dir, as the transcript's
`stacktrace` gives it, e.g. `reply_single_hop.py:76`), so a different failure of the same test is not hidden by it.
A reason for a deliberate gap cites its section of `docs/PYFEDI_GAPS.md`; one for a bug in FediTest says `upstream:` and gives the file and line at the pinned SHA. A real
PieFed failure is a defect to fix, not to baseline. Add the same row to the "External conformance suites" table in
`docs/PYFEDI_GAPS.md` §2. `tests/test_conformance_baseline.py` pins the checker itself.

## Needs

- podman and podman-compose; about 6 GB RAM; the first `up` pulls images and builds PieFed (a few minutes).
- Subnet 10.90.0.0/16 must be free (`podman network ls`). The apps get it in `ALLOWED_PRIVATE_NETWORKS` /
  `ALLOWED_PRIVATE_ADDRESSES`, and PeerTube runs with `PEERTUBE_FEDERATION_PREVENT_SSRF=false` (harness only), so
  they accept each other's private container addresses.

## Notes

- The stack is the podman-compose project `pyfedi-interop` (`name:` in `compose.interop.yaml`), apart from
  `run_tests.sh`'s `pyfedi` stack, so the two can run at once without either's teardown removing the other.
- Nothing is published to the host. Reach the apps from inside the network, e.g.
  `podman-compose -f compose.interop.yaml exec -T piefed-web curl -fsS --cacert /tmp/harness-ca.crt https://mastodon.test/health`.
- Caddy's CA root is published to every app container as `/caddy-data/harness-root.crt`; `trust_ca.sh` adds it to
  `/tmp/harness-ca.crt` and sets `SSL_CERT_FILE`. Node ignores that variable, so PeerTube gets
  `NODE_EXTRA_CA_CERTS=/caddy-data/harness-root.crt` instead. PHP's curl and openssl ignore it too, so Castopod gets
  `curl.cainfo` and `openssl.cafile` from `interop/castopod-ca.ini`; Pixelfed (also PHP) mounts the same file.
- Pixelfed refuses every peer whose name resolves to a non-global address and has no setting to allow one
  (`Helpers::isPublicIp`). `interop/pixelfed_harness.sh` patches the running container's copy to accept
  10.90.0.0/16 before the server starts (harness only). Pixelfed delivers nothing outside `APP_ENV=production`, and
  runs every federation job on Horizon (`pixelfed-horizon`): with Horizon down, follows stay `requested`. Inbound
  posts reach a Pixelfed home feed only after the inbox and feed queues run, which can take over 45 s.
- `piefed-celery-background` serves the celery `background` queue (`probe_media`), as production's
  `pyfedi-celery-background` does. piefed-web and both workers share the `piefed-media` and `piefed-tmp` volumes.
  The runner mounts `tests/fixtures/media` for the media uploads.
- Logs: `podman-compose -f compose.interop.yaml logs piefed-celery` (also `piefed-celery-background`, `piefed-web`, `mastodon-web`, `mastodon-sidekiq`, `peertube-web`, `castopod-web`, `pixelfed-web`, `pixelfed-horizon`, `caddy`). Castopod's application log is
  `/var/www/html/writable/logs/log-<date>.log` inside `castopod-web`.
- The conformance gate runs FediTest's `system2` tests (PieFed and Mastodon, once in each direction) but not
  `system3`. Its tests need three nodes, and a PieFed in two roles on one host would share one database: the
  note node1 posts is already in node3's PieFed, so `actor_has_received_object` for node3 is true at once and
  the delivery it should prove (`announce.py` line 93, `delete_double_hop.py` lines 93 and 104) is not tested. Running
  `system3` needs a second PieFed stack.
