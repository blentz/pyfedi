# pyfedi/

A fork of PieFed: a Flask + Postgres + Redis + Celery fediverse link aggregator / forum, federating over ActivityPub.

## Deployment agnostic

This repo is application code only and must stay deployment agnostic:

- Never name, hard-code or describe any specific instance, domain, host, server path or user that this software is
  deployed to: not in code, tests, fixtures, comments, docs, specs, plans, commit messages or this file. Use
  configuration (env vars, settings, admin UI) and generic examples (`example.com`, `piefed.example`) instead.
- No server-management tooling here (units, host config, deploy/backup scripts). `deploy/` is in `.gitignore`; keep it
  there and never recreate or commit it.

## Remotes and branches

- `origin` is upstream PieFed (codeberg.org/rimu/pyfedi); `github` is our fork (github.com/blentz/pyfedi).
- Day-to-day work is on `blentz`; feature branches as `feat/...`.
- Keep upstream mergeable: prefer additive changes over rewriting upstream code.
- Push only when the user asks; some branches are deployed automatically (see the workspace CLAUDE.md, if present).

## Docs

- `docs/ARCHITECTURE.md`, `docs/KEY_TERMS.md`, `docs/developer_docs/`: how PieFed works.
- `dev_notes.md`: upstream's conventions; `FEDERATION.md`; `SECURITY.md`.
- `docs/CHANGES-since-v1.8.10.md`: what the fork changes relative to upstream.
- `docs/superpowers/specs/` and `docs/superpowers/plans/`: design specs and implementation plans for fork features.

## Tests and lint

- `./run_tests.sh [pytest args]`: runs the suite in disposable podman-compose containers (`compose.test.yaml`); also
  runs the JS tests if `node` is installed. `./run_tests.sh --down` cleans up after a killed run.
- `./run_tests_remote.sh [pytest args]`: the same on another podman connection (`REMOTE_PODMAN`, default
  `remote-gpu`). Prefer it for full-suite or coverage runs so the laptop isn't loaded.
- `coverage_floors.ini` sets per-module coverage floors. Floors only ever rise: never lower one to make a run pass.
- Lint with `ruff` (`ruff.toml`).

## Notes

- `compose.*.yaml`, `Dockerfile`, `entrypoint*.sh` and the `*.sh` job scripts (`daily.sh`, `send_queue.sh`, …) are
  the app's generic packaging. Deployments invoke the job scripts by name, so treat their names and arguments as an
  interface: don't rename or change them casually, and call out such changes to the user.
- `pgdata/`, `media/`, `logs/`, `tmp/` are local runtime data: don't read through or commit them.
- Git worktrees live under `.claude/worktrees/`.
