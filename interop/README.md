# Interop harness

Runs PieFed (built from this tree), Mastodon and PeerTube side by side, behind Caddy with an internal CA, on the
names `piefed.test`, `mastodon.test` and `peertube.test`. Later interop tests drive real servers against each other here.
See `compose.interop.yaml`; spec 2026-10-14 section 8.1.

## Use

    interop/run.sh up            start and seed the stack (users alice, bob and carol; tokens in interop/.tokens.json)
    interop/run.sh test [args]   up, then pytest interop/ in the interop-runner container (args pass through)
    interop/run.sh down          remove the stack and its volumes

Seeded: PieFed `alice` / `interop-alice-pw` and the local `microblogs` community; Mastodon `bob`; PeerTube admin
`root` / `interop-root-pw` and user `carol` / `interop-carol-pw`. `interop/.tokens.json` holds the API tokens
`mastodon_bob`, `peertube_root` and `peertube_carol` (PeerTube OAuth access tokens last a day; `up` renews them).
`interop/.env.interop` (secrets) and `interop/.tokens.json` are generated and gitignored. `up` is safe to repeat.

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
  `NODE_EXTRA_CA_CERTS=/caddy-data/harness-root.crt` instead.
- Logs: `podman-compose -f compose.interop.yaml logs piefed-celery` (also `piefed-web`, `mastodon-web`, `mastodon-sidekiq`, `peertube-web`, `caddy`).
