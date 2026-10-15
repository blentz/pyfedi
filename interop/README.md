# Interop harness

Runs PieFed (built from this tree) and Mastodon side by side, behind Caddy with an internal CA, on the names
`piefed.test` and `mastodon.test`. Later interop tests drive real servers against each other here.
See `compose.interop.yaml`; spec 2026-10-14 section 8.1.

## Use

    interop/run.sh up            start and seed the stack (PieFed user alice, Mastodon user bob, tokens in interop/.tokens.json)
    interop/run.sh test [args]   up, then pytest interop/ in the interop-runner container (args pass through)
    interop/run.sh down          remove the stack and its volumes

Seeded: PieFed `alice` / `interop-alice-pw` and the local `microblogs` community; Mastodon `bob`.
`interop/.env.interop` (secrets) and `interop/.tokens.json` are generated and gitignored. `up` is safe to repeat.

## Needs

- podman and podman-compose; about 6 GB RAM; the first `up` pulls images and builds PieFed (a few minutes).
- Subnet 10.90.0.0/16 must be free (`podman network ls`). Both apps get it in `ALLOWED_PRIVATE_NETWORKS` /
  `ALLOWED_PRIVATE_ADDRESSES`, so they accept each other's private container addresses.

## Notes

- Nothing is published to the host. Reach the apps from inside the network, e.g.
  `podman-compose -f compose.interop.yaml exec -T piefed-web curl -fsS --cacert /tmp/harness-ca.crt https://mastodon.test/health`.
- Caddy's CA root is published to every app container as `/caddy-data/harness-root.crt`; `trust_ca.sh` adds it to
  `/tmp/harness-ca.crt` and sets `SSL_CERT_FILE`.
- Logs: `podman-compose -f compose.interop.yaml logs piefed-celery` (also `piefed-web`, `mastodon-web`, `mastodon-sidekiq`, `caddy`).
