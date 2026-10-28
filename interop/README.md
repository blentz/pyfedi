# Interop harness

Runs PieFed (built from this tree), Mastodon, PeerTube and Castopod side by side, behind Caddy with an internal CA,
on the names `piefed.test`, `mastodon.test`, `peertube.test` and `castopod.test`. Later interop tests drive real servers against each other here.
See `compose.interop.yaml`; spec 2026-10-14 section 8.1.

## Use

    interop/run.sh up            start and seed the stack (users alice, bob and carol; tokens in interop/.tokens.json)
    interop/run.sh conformance [args]   up, then interop/conformance/run.sh in the conformance-runner container
    interop/run.sh test [args]   up, then pytest interop/ in the interop-runner container (args pass through)
    interop/run.sh down          remove the stack and its volumes

Seeded: PieFed `alice` / `interop-alice-pw`, the local `microblogs` community, the `video_channel` community `vc1`, and
the media upload communities `vc_media` (`video_channel`) and `pod_media` (`podcast`), which alice moderates, with video
and audio uploads open to users; Mastodon `bob`; PeerTube admin
`root` / `interop-root-pw` and user `carol` / `interop-carol-pw`; Castopod superadmin `admin@castopod.test` /
`interop-admin-pw-1` and podcast `@dave` with one published episode (REST API basic auth `interop` / `interop-api-pw`).
`interop/.tokens.json` holds the API tokens `mastodon_bob`, `peertube_root` and `peertube_carol` (PeerTube OAuth
access tokens last a day; `up` renews them), and the URLs `castopod_podcast` and `castopod_episode`.
`interop/.env.interop` (secrets) and `interop/.tokens.json` are generated and gitignored. `up` is safe to repeat.

## Tests

- `test_smoke.py`: TLS, DNS, webfinger, a federated follow both ways, and Castopod's podcast and episode as ActivityPub.
- `test_microblog_mastodon.py`: a local microblogs post shows on Mastodon, and replies, favourites and boosts land in PieFed.
- `test_peertube_subscribe.py`: a PeerTube user subscribes to a `video_channel` and PeerTube sees it accepted.
- `test_media_pipeline.py`: video and audio uploads are probed, published and served in ranges.
- `test_podcast.py`: an audio episode uploaded to `pod_media` plays on Mastodon as an audio attachment, a Mastodon reply lands in the PieFed thread, and the community feed carries the episode's enclosure, duration and guid.
- `test_video_channel.py`: a video uploaded to `vc_media` plays on PeerTube under the subscribed channel (duration,
  file URL, thumbnail), a PeerTube like comes back as an upvote, an edit renames it, a delete removes it, and a
  restore creates it again.

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
  `curl.cainfo` and `openssl.cafile` from `interop/castopod-ca.ini`.
- `piefed-celery-background` serves the celery `background` queue (`probe_media`), as production's
  `pyfedi-celery-background` does. piefed-web and both workers share the `piefed-media` and `piefed-tmp` volumes.
  The runner mounts `tests/fixtures/media` for the media uploads.
- Logs: `podman-compose -f compose.interop.yaml logs piefed-celery` (also `piefed-celery-background`, `piefed-web`, `mastodon-web`, `mastodon-sidekiq`, `peertube-web`, `castopod-web`, `caddy`). Castopod's application log is
  `/var/www/html/writable/logs/log-<date>.log` inside `castopod-web`.
- The conformance gate runs FediTest's `system2` tests (PieFed and Mastodon, once in each direction) but not
  `system3`. Its tests need three nodes, and a PieFed in two roles on one host would share one database: the
  note node1 posts is already in node3's PieFed, so `actor_has_received_object` for node3 is true at once and
  the delivery it should prove (`announce.py` line 93, `delete_double_hop.py` lines 93 and 104) is not tested. Running
  `system3` needs a second PieFed stack.
