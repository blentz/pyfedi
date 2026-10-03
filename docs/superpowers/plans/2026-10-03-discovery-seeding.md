# Discovery Seeding and Castopod Podcasts-as-Communities Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a PieFed instance find and pre-load PeerTube channels and Castopod podcasts, find opt-in Mastodon/Pixelfed people in search, and treat a Castopod podcast as a community whose episodes show their hosts and guests as credits.

**Architecture:** A new fork-only package `app/discovery/` holds one fetcher per directory (SepiaSearch, Podcast Index, joinmastodon + Mastodon directories, FediDB + Pixelfed directories), a refresh job that cleans and upserts their output into a new `discovery_entry` table, the admin pre-load and credentials page, the search fallback, the Castopod dual-actor helpers and the RSS credits pipeline. Upstream files get thin seams only: one model class and one column in `app/models.py`, one-line hooks in `app/activitypub/{util,actor,routes}.py`, `app/main/routes.py`, `app/instance/routes.py`, `app/api/alpha/views.py`, `app/cli.py`, two blueprint `__init__` imports and three template includes. One fork migration revises the current head.

**Tech Stack:** Python 3.13, Flask, Flask-WTF, SQLAlchemy (PostgreSQL `INSERT ... ON CONFLICT`), Alembic, Celery (eager in tests), Jinja2, httpx via `app.utils.get_request`, stdlib `xml.etree.ElementTree` (expat), pytest + respx (`http_mock`).

**Spec:** `docs/superpowers/specs/2026-10-03-discovery-seeding-design.md` (interop spec D24; fork-only per D16/D17).

## Global Constraints

- Fork-only code lives in `app/discovery/`; upstream files get thin seams only (spec D17).
- Exactly ONE new migration, `migrations/versions/c6e1d9a4b7f2_discovery_entry_and_post_extensions.py`, `down_revision = 'b5d2c8e1f7a3'` (the single current head, on the `'fork'` branch opened by `a7e3f1c2b9d4`), `branch_labels = None`. It adds table `discovery_entry` and column `post.extensions` (JSON, nullable).
- `discovery_entry` columns: `id`, `kind`, `platform`, `actor_url` (unique), `name`, `host`, `avatar_url`, `followers`, `nsfw`, `source`, `first_seen`, `last_seen`.
- Normalised entry dict keys, exactly: `{kind: community|person, platform, actor_url, name, host, avatar, followers, nsfw, source}`.
- Sources: SepiaSearch `GET https://sepiasearch.org/api/v1/search/video-channels`; Podcast Index API (only with admin-entered key and secret): `/podcasts/trending?max=200`, then at most 40 `/episodes/byfeedid` lookups; keep feeds that look like Castopod (`generator` or a `/@handle/feed.xml` URL) and whose episodes' `socialInteract` has `protocol: activitypub`, actor URL from it (Podcast Index reports `socialInteract` on episodes, not feeds); top 20 servers of `https://api.joinmastodon.org/servers` then `GET /api/v1/directory?local=true&order=active&limit=80`; FediDB `https://api.fedidb.org/v1/servers?software=pixelfed` then `GET /api/landing/v1/directory` (404 = skip host).
- Per-run budgets: PeerTube at most 5 pages; Mastodon at most 20 servers x 1 directory page of 80; Pixelfed at most 20 servers x 2 pages; Podcast Index at most 200 results. Calls to the same host are spaced (`POLITE_DELAY_SECONDS = 1.0`).
- Podcast Index signing: headers `X-Auth-Key`, `X-Auth-Date` (unix seconds as a string), `Authorization` = SHA-1 hex of `api_key + api_secret + X-Auth-Date`, plus `User-Agent` (set by `get_request`). Verified against the Podcast Index API docs (`pi_api.yaml` example: `sha1($apiKey.$apiSecret.$apiHeaderTime)`).
- Credentials are site settings `podcastindex_api_key` / `podcastindex_api_secret`: write-only fields, never rendered back (page shows "configured" / "not set"), never logged.
- Every fetch goes through `app.utils.get_request` (SSRF guards, redirects off). A failing source is logged and skipped; its old entries stay until expiry.
- Refresh filters: banned instances (and the allowlist when in allowlist mode), banned `Domain` rows, PeerTube isolation-list hosts, names failing `is_bad_name`; tags NSFW; caps per host (`MAX_PER_HOST = 20`) and per source (`MAX_PER_SOURCE = 500`); upserts on `actor_url`; deletes entries not seen for 30 days (`EXPIRY_DAYS = 30`).
- Names stored as plain text, capped (256; credits 100), escaped on render (Jinja autoescape). Avatar and credit image URLs must be https and pass `url_is_storable`. Nothing is downloaded until someone follows or joins.
- Scheduling: `flask refresh_discovery`, called from `daily.sh`. Never runs on a request. Pre-load is a manual admin action that runs as a Celery task, idempotent (known communities skipped), and subscribes through `do_subscribe(..., admin_preload=True)`.
- Search fallback only when nothing local matches; NSFW entries shown only when the viewer's NSFW setting allows (community-search rule).
- Castopod: a `Podcast` actor gets a `User` row (technical owner, `Post.user_id`) and a `Community` row with the same `ap_profile_id`; episodes go to that community; credits `{name, role, image, profile_url, user_id}` live in `Post.extensions["podcast"]["credits"]`; the API exposes `post.extensions.podcast.credits`; byline "Hosted by A, B · with guest C", falling back to the podcast's name.
- RSS for credits: fetched with `get_request`, capped at 2 MB (`MAX_FEED_BYTES = 2 * 1024 * 1024`), parsed with the stdlib XML parser (expat; any `<!DOCTYPE`/`<!ENTITY` refused). A broken or missing feed leaves the post without credits.
- Tests: no network (`http_mock` / respx); app config only through `monkeypatch.setitem(app.config, ...)` (ratchet in `tests/test_parallel_workers.py`); anchored regex checks use `fullmatch` (`tests/test_anchored_validators.py`); NO new function-level imports in `app/` (`tests/test_no_inline_imports.py`: `KEPT = 85`, and the tree already holds 85), so every import in new and touched `app/` code is at module top. Cycles are broken with module imports (`import app.x.y as y_mod`), never with in-function imports.
- Tests run with `./run_tests.sh <pytest args>` from `/home/blentz/git/pyfedi`. Implementers keep at most one waiter per test run (one `./run_tests.sh` invocation at a time; never start a second while one runs).
- Every task ends green and is committed; every commit message ends with the trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. A directory answers 200 with rows of the wrong shape (a row that is `null` or a string, a numeric `url`, `followersCount: "many"`, `avatars: "nope"`): the bad rows are skipped, the good ones kept, the refresh does not crash. Pinned in Task 2 (`test_malformed_rows_are_skipped_not_fatal`) and Task 3 (`test_malformed_server_rows_are_ignored`).
2. The same actor URL arrives twice in one run (a directory repeats a row across pages): one `discovery_entry` row, no PostgreSQL "ON CONFLICT DO UPDATE command cannot affect row a second time". Pinned in Task 6 (`test_a_duplicate_actor_in_one_run_is_stored_once`).
3. A search text containing `%` or `_` (e.g. `100%`): the fallback matches it literally rather than as a LIKE wildcard. Pinned in Task 10 (`test_like_wildcards_in_the_search_text_match_literally`).
4. An admin re-saves the credentials form with blank fields, or pastes a key with surrounding spaces: blank fields leave the stored values alone, padding is trimmed. Pinned in Task 8 (`test_blank_fields_keep_what_is_stored_and_padding_is_trimmed`).
5. The RSS item's `<link>` differs from the episode URL by a trailing slash, or only its `<guid>` equals the episode URL: the guests are still credited. Pinned in Task 14 (`test_an_item_matches_by_guid_or_with_a_trailing_slash`).

---

## File Structure

New, fork-only (`app/discovery/`):

- `app/discovery/__init__.py` — constants only (`KIND_COMMUNITY`, `KIND_PERSON`, `PLATFORMS`); no imports, so any module may import it without a cycle.
- `app/discovery/sources.py` — what every fetcher shares: `fetch_json`, `polite_pause`, `make_entry`, `as_count`, `is_hostname`, `is_username`, `DiscoverySourceError`.
- `app/discovery/peertube.py` — SepiaSearch fetcher.
- `app/discovery/mastodon.py` — joinmastodon + Mastodon directory fetcher.
- `app/discovery/pixelfed.py` — FediDB + Pixelfed directory fetcher.
- `app/discovery/castopod.py` — Podcast Index fetcher and request signing.
- `app/discovery/filters.py` — host exclusion (bans, allowlist, banned domains, PeerTube isolation list), name/URL cleaning, NSFW tagging.
- `app/discovery/refresh.py` — runs fetchers, cleans, caps, upserts, expires.
- `app/discovery/cli.py` — `flask refresh_discovery`, `flask discovery-seed-fixtures`.
- `app/discovery/forms.py` — admin credentials and pre-load forms.
- `app/discovery/admin_views.py` — `/admin/federation/discovery` on the `admin` blueprint.
- `app/discovery/preload.py` — pre-load candidates and the Celery subscribe task.
- `app/discovery/search.py` — the search fallback query and the NSFW rule for people.
- `app/discovery/views.py` — `/discovery/<id>/resolve` and the `podcast_byline` template global, on the `main` blueprint.
- `app/discovery/podcast.py` — Castopod dual actor: `ensure_podcast_community`, `podcast_community_for`.
- `app/discovery/credits.py` — RSS credits: parse, fetch, resolve, store, read back, byline data.
- `app/discovery/seed.py` — local validation seeding from fixture files.
- `app/discovery/fixtures/` — `sepiasearch.json`, `mastodon_directory.json`, `pixelfed_directory.json`, `castopod_podcastindex.json`, `castopod_feed.xml`, `castopod_actor.json` (shared by tests and the seed command).
- `app/templates/admin/federation_discovery.html`, `app/templates/discovery/_fallback.html`, `app/templates/discovery/_byline.html`.
- `migrations/versions/c6e1d9a4b7f2_discovery_entry_and_post_extensions.py`.

Upstream seams (thin):

- `app/models.py` — `DiscoveryEntry` class (must be in the always-imported models module so `db.metadata` and the test teardown see it) and `Post.extensions`.
- `app/cli.py` — top import + one call in `register(app)`.
- `daily.sh` — one line.
- `app/admin/__init__.py`, `app/main/__init__.py` — one module-level import each, registering the fork routes on existing blueprints (no new blueprint: registering one needs a function-level import in `create_app`, which the ratchet forbids).
- `app/templates/admin/federation_preload.html` — a link to the new page.
- `app/main/routes.py` (`list_communities`), `app/instance/routes.py` (`instance_people`), `app/templates/list_communities.html`, `app/templates/instance/people.html` — search fallback.
- `app/activitypub/util.py` — `actor_json_to_model` Podcast branch, `refresh_user_profile_task`, `fetch_castopod_episode_audio`.
- `app/activitypub/actor.py` — `find_actor_by_url` and `create_actor_from_remote` community hints.
- `app/activitypub/routes.py` — `process_new_content` routing.
- `app/templates/post/_post_full.html` — byline include (two sites).
- `app/api/alpha/views.py`, `app/api/alpha/schema.py` — `extensions.podcast.credits`.

Tests (one file per task): `tests/test_discovery_models.py`, `tests/test_discovery_peertube.py`, `tests/test_discovery_mastodon.py`, `tests/test_discovery_pixelfed.py`, `tests/test_discovery_castopod_source.py`, `tests/test_discovery_refresh.py`, `tests/test_discovery_cli.py`, `tests/test_discovery_admin_credentials.py`, `tests/test_discovery_preload.py`, `tests/test_discovery_search.py`, `tests/test_podcast_community.py`, `tests/test_podcast_dual_actor_lookups.py`, `tests/test_podcast_episode_routing.py`, `tests/test_podcast_credits_parse.py`, `tests/test_podcast_credits_store.py`, `tests/test_podcast_byline.py`, `tests/test_podcast_credits_api.py`, `tests/test_discovery_seed.py`.

---

### Task 1: Migration, `DiscoveryEntry` model and `Post.extensions`

**Files:**
- Create: `migrations/versions/c6e1d9a4b7f2_discovery_entry_and_post_extensions.py`
- Create: `app/discovery/__init__.py`
- Modify: `app/models.py` (add `extensions` after `post_boosts` in `class Post`, ~line 2515; add `class DiscoveryEntry` directly after `class RssFeedItem`, ~line 5670)
- Test: `tests/test_discovery_models.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `app.discovery.KIND_COMMUNITY = 'community'`, `KIND_PERSON = 'person'`, `PLATFORMS = ('peertube', 'castopod', 'mastodon', 'pixelfed')`.
  - `app.models.DiscoveryEntry` (table `discovery_entry`): `id: int`, `kind: str(10)`, `platform: str(20)`, `actor_url: str(1024) unique`, `name: str(256)`, `host: str(255)`, `avatar_url: str(1024) | None`, `followers: int` (default 0), `nsfw: bool` (default False), `source: str(50)`, `first_seen: datetime`, `last_seen: datetime`.
  - `app.models.Post.extensions: dict | None` (`db.JSON`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_discovery_models.py`:

```python
"""Interop D24: the fork migration adding discovery_entry and post.extensions, and the two models."""
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import DiscoveryEntry, Post
from tests.factories import make_community, make_instance, make_post, make_user

MIGRATION = (Path(__file__).resolve().parent.parent / 'migrations' / 'versions'
             / 'c6e1d9a4b7f2_discovery_entry_and_post_extensions.py')


def load_migration():
    spec = importlib.util.spec_from_file_location('discovery_migration', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def an_entry(**changes):
    values = dict(kind='community', platform='peertube', actor_url='https://tube.example/video-channels/linux',
                  name='Linux', host='tube.example', followers=5, source='sepiasearch')
    values.update(changes)
    return DiscoveryEntry(**values)


def test_the_migration_revises_the_previous_fork_head():
    migration = load_migration()
    assert migration.revision == 'c6e1d9a4b7f2'
    assert migration.down_revision == 'b5d2c8e1f7a3'
    assert migration.branch_labels is None


def test_an_entry_round_trips_with_its_defaults(app, db_session):
    entry = an_entry()
    db.session.add(entry)
    db.session.commit()
    db.session.expire_all()

    stored = db.session.get(DiscoveryEntry, entry.id)

    assert stored.nsfw is False
    assert stored.avatar_url is None
    assert stored.first_seen is not None and stored.last_seen is not None


def test_actor_url_is_unique(app, db_session):
    db.session.add(an_entry())
    db.session.commit()
    db.session.add(an_entry(name='Again'))

    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_post_extensions_stores_json(app, db_session):
    author = make_user(make_instance('m.example'), 'alice')
    post = make_post(make_community(), author, 'https://m.example/p/1')
    assert post.extensions is None

    post.extensions = {'podcast': {'credits': [{'name': 'Ann', 'role': 'host'}]}}
    db.session.commit()
    db.session.expire_all()

    assert db.session.get(Post, post.id).extensions['podcast']['credits'][0]['name'] == 'Ann'


def test_downgrade_then_upgrade_restores_both(app, db_session):
    db.session.close()  # nothing of the session's may still hold a lock on post
    migration = load_migration()

    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
                inspector = inspect(connection)
                assert 'discovery_entry' not in inspector.get_table_names()
                assert 'extensions' not in [c['name'] for c in inspector.get_columns('post')]
                migration.upgrade()
            inspector = inspect(connection)
            assert 'discovery_entry' in inspector.get_table_names()
            unique = [i for i in inspector.get_indexes('discovery_entry') if i['column_names'] == ['actor_url']]
            assert unique and unique[0]['unique']
            column = next(c for c in inspector.get_columns('post') if c['name'] == 'extensions')
            assert column['nullable'] is True
        finally:
            transaction.rollback()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_discovery_models.py -v`
Expected: FAIL at collection with `ImportError: cannot import name 'DiscoveryEntry' from 'app.models'`.

- [ ] **Step 3: Write the migration, the package marker and the models**

Create `migrations/versions/c6e1d9a4b7f2_discovery_entry_and_post_extensions.py`:

```python
"""discovery_entry holds what opt-in directories list; post.extensions holds per-post fork data

Revision ID: c6e1d9a4b7f2
Revises: b5d2c8e1f7a3
Create Date: 2026-10-03 12:00:00.000000

Interop spec D24. discovery_entry is the search/pre-load index built daily by
`flask refresh_discovery`; actor_url is unique because the refresh upserts on it.
post.extensions is the first piece of D8's content-kind layout: Castopod credits
live at extensions['podcast']['credits']. Nullable, no backfill.
"""
from alembic import op
import sqlalchemy as sa

revision = 'c6e1d9a4b7f2'
down_revision = 'b5d2c8e1f7a3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'discovery_entry',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=10), nullable=False),
        sa.Column('platform', sa.String(length=20), nullable=False),
        sa.Column('actor_url', sa.String(length=1024), nullable=False),
        sa.Column('name', sa.String(length=256), nullable=False),
        sa.Column('host', sa.String(length=255), nullable=False),
        sa.Column('avatar_url', sa.String(length=1024), nullable=True),
        sa.Column('followers', sa.Integer(), server_default='0', nullable=False),
        sa.Column('nsfw', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('source', sa.String(length=50), nullable=False),
        sa.Column('first_seen', sa.DateTime(), nullable=False),
        sa.Column('last_seen', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('discovery_entry', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_discovery_entry_actor_url'), ['actor_url'], unique=True)
        batch_op.create_index(batch_op.f('ix_discovery_entry_host'), ['host'], unique=False)
        batch_op.create_index(batch_op.f('ix_discovery_entry_kind'), ['kind'], unique=False)
        batch_op.create_index(batch_op.f('ix_discovery_entry_last_seen'), ['last_seen'], unique=False)
    with op.batch_alter_table('post', schema=None) as batch_op:
        batch_op.add_column(sa.Column('extensions', sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table('post', schema=None) as batch_op:
        batch_op.drop_column('extensions')
    with op.batch_alter_table('discovery_entry', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_discovery_entry_last_seen'))
        batch_op.drop_index(batch_op.f('ix_discovery_entry_kind'))
        batch_op.drop_index(batch_op.f('ix_discovery_entry_host'))
        batch_op.drop_index(batch_op.f('ix_discovery_entry_actor_url'))
    op.drop_table('discovery_entry')
```

Create `app/discovery/__init__.py`:

```python
"""Discovery seeding for non-Lemmy platforms, and Castopod podcasts as communities (interop D24).

Fork-only (D16/D17). Constants only: this module imports nothing, so every module,
upstream or fork, can import it without risking a cycle.
"""
KIND_COMMUNITY = 'community'
KIND_PERSON = 'person'
PLATFORMS = ('peertube', 'castopod', 'mastodon', 'pixelfed')
```

In `app/models.py`, inside `class Post`, directly after
`    post_boosts = db.Column(db.JSON)                # a cache of the boosts(retweets) a microblog post has received, to avoid joins`
add:

```python
    extensions = db.Column(db.JSON)                 # fork per-post data (D8/D24), e.g. {'podcast': {'credits': [...]}}
```

In `app/models.py`, directly after the end of `class RssFeedItem` (the lines `    __table_args__ = (` / `        db.UniqueConstraint('feed_id', 'guid'),` / `    )`) and before `def _large_community_subscribers`, add:

```python
class DiscoveryEntry(db.Model):
    """An actor an opt-in directory lists (interop D24): a PeerTube channel, a Castopod podcast, or a
    Mastodon/Pixelfed person. Third-party data, stored as plain text and escaped on render; nothing is
    fetched from `actor_url` or `avatar_url` until someone follows or joins. Built daily by
    `flask refresh_discovery` (app/discovery/refresh.py), which upserts on `actor_url`.
    """
    __tablename__ = 'discovery_entry'
    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(10), nullable=False, index=True)         # 'community' or 'person'
    platform = db.Column(db.String(20), nullable=False)                 # 'peertube', 'castopod', 'mastodon', 'pixelfed'
    actor_url = db.Column(db.String(1024), nullable=False, unique=True, index=True)
    name = db.Column(db.String(256), nullable=False)
    host = db.Column(db.String(255), nullable=False, index=True)
    avatar_url = db.Column(db.String(1024))
    followers = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    nsfw = db.Column(db.Boolean, nullable=False, default=False, server_default='false')
    source = db.Column(db.String(50), nullable=False)
    first_seen = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_seen = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
```

(`utcnow` is already defined in `app/models.py`. The model must live here, not in `app/discovery/`: the test teardown builds its `DELETE` list once from `db.metadata`, so a model imported late would leak rows between tests.)

- [ ] **Step 4: Run test to verify it passes**

Run: `./run_tests.sh tests/test_discovery_models.py tests/test_migration_visibility.py tests/test_no_inline_imports.py -v`
Expected: PASS (`run_tests.sh` applies the new migration with `flask db upgrade` first).

- [ ] **Step 5: Commit**

```bash
git add migrations/versions/c6e1d9a4b7f2_discovery_entry_and_post_extensions.py app/discovery/__init__.py app/models.py tests/test_discovery_models.py
git commit -m "$(cat <<'EOF'
feat: discovery_entry table and post.extensions column (interop D24)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Shared fetch helpers and the PeerTube (SepiaSearch) fetcher

**Files:**
- Create: `app/discovery/sources.py`
- Create: `app/discovery/peertube.py`
- Create: `app/discovery/fixtures/sepiasearch.json`
- Test: `tests/test_discovery_peertube.py`

**Interfaces:**
- Consumes: `app.discovery.KIND_COMMUNITY` (Task 1); `app.utils.get_request(uri, params=None, headers=None) -> httpx.Response`.
- Produces:
  - `app.discovery.sources.POLITE_DELAY_SECONDS: float = 1.0`
  - `class DiscoverySourceError(Exception)` — the source's index could not be read.
  - `polite_pause() -> None`
  - `fetch_json(url: str, params: dict | None = None, headers: dict | None = None) -> dict | list | None` (None on any transport error, non-200 or non-JSON)
  - `make_entry(*, kind, platform, actor_url, name, host, avatar, followers, nsfw, source) -> dict`
  - `as_count(value) -> int` (non-negative int or 0)
  - `is_hostname(value) -> bool`, `is_username(value) -> bool`
  - `app.discovery.peertube.SEPIASEARCH_URL`, `SOURCE = 'sepiasearch'`, `PAGE_SIZE = 100`, `MAX_PAGES = 5`
  - `channel_to_entry(channel) -> dict | None`
  - `fetch_peertube_channels(exclude: Callable[[str], bool]) -> list[dict]` (sorted by followers, descending; raises `DiscoverySourceError` when the first page cannot be read)
  - Fetchers call `sources.polite_pause()` / `sources.fetch_json()` through the module, so tests patch `app.discovery.sources.<name>` once for every fetcher.

- [ ] **Step 1: Write the fixture and the failing test**

Create `app/discovery/fixtures/sepiasearch.json`:

```json
{"total": 3, "data": [
  {"id": 1, "url": "https://tube.example/video-channels/linux_videos", "name": "linux_videos",
   "displayName": "Linux Videos", "host": "tube.example", "followersCount": 4581, "videosCount": 469,
   "avatars": [{"url": "https://tube.example/lazy-static/avatars/small.png", "width": 48},
               {"url": "https://tube.example/lazy-static/avatars/big.png", "width": 600}]},
  {"id": 2, "url": "https://video.example/video-channels/cooking", "name": "cooking",
   "displayName": "Cooking Together", "host": "video.example", "followersCount": 120, "videosCount": 30,
   "avatars": []},
  {"id": 3, "url": "https://other.example/video-channels/spoofed", "name": "spoofed",
   "displayName": "Spoofed", "host": "tube.example", "followersCount": 9999, "avatars": []}
]}
```

Create `tests/test_discovery_peertube.py`:

```python
"""Interop D24: PeerTube channels from SepiaSearch, and the helpers every fetcher shares."""
import json
from pathlib import Path

import httpx
import pytest

from app.discovery import peertube, sources
from app.discovery.peertube import SEPIASEARCH_URL, channel_to_entry, fetch_peertube_channels

FIXTURE = Path(__file__).resolve().parent.parent / 'app' / 'discovery' / 'fixtures' / 'sepiasearch.json'

pytestmark = pytest.mark.usefixtures('no_real_sleeping')


def nobody_excluded(host):
    return False


def a_page(size):
    return {'total': size, 'data': [{'url': f'https://t{i}.example/video-channels/c{i}', 'host': f't{i}.example',
                                     'name': f'c{i}', 'followersCount': i} for i in range(size)]}


def test_the_fixture_is_normalised_and_ranked_by_followers(app, http_mock):
    http_mock.get(SEPIASEARCH_URL).respond(json=json.loads(FIXTURE.read_text()))

    entries = fetch_peertube_channels(nobody_excluded)

    assert [e['name'] for e in entries] == ['Linux Videos', 'Cooking Together']
    assert entries[0] == {'kind': 'community', 'platform': 'peertube',
                          'actor_url': 'https://tube.example/video-channels/linux_videos',
                          'name': 'Linux Videos', 'host': 'tube.example',
                          'avatar': 'https://tube.example/lazy-static/avatars/big.png',
                          'followers': 4581, 'nsfw': False, 'source': 'sepiasearch'}


def test_a_channel_whose_url_is_not_on_its_host_is_dropped(app):
    assert channel_to_entry({'url': 'https://other.example/video-channels/x', 'host': 'tube.example',
                             'name': 'x'}) is None


def test_malformed_rows_are_skipped_not_fatal(app, http_mock):
    """Review focus 1."""
    rows = [None, 'a string', {'url': 42, 'host': 'tube.example'},
            {'url': 'https://tube.example/video-channels/ok', 'host': 'tube.example', 'name': 'ok',
             'followersCount': 'many', 'avatars': 'nope'}]
    http_mock.get(SEPIASEARCH_URL).respond(json={'total': 4, 'data': rows})

    entries = fetch_peertube_channels(nobody_excluded)

    assert [(e['name'], e['followers'], e['avatar']) for e in entries] == [('ok', 0, None)]


def test_paging_stops_at_five_pages_and_pauses_between_them(app, http_mock, monkeypatch):
    pauses = []
    monkeypatch.setattr(sources, 'polite_pause', lambda: pauses.append(1))
    route = http_mock.get(SEPIASEARCH_URL).respond(json=a_page(peertube.PAGE_SIZE))

    fetch_peertube_channels(nobody_excluded)

    assert route.call_count == peertube.MAX_PAGES == 5
    assert [call.request.url.params['start'] for call in route.calls] == ['0', '100', '200', '300', '400']
    assert {call.request.url.params['count'] for call in route.calls} == {'100'}
    assert len(pauses) == 4


def test_a_short_page_ends_paging(app, http_mock):
    route = http_mock.get(SEPIASEARCH_URL).respond(json=a_page(3))

    assert len(fetch_peertube_channels(nobody_excluded)) == 3
    assert route.call_count == 1


def test_excluded_hosts_are_left_out(app, http_mock):
    http_mock.get(SEPIASEARCH_URL).respond(json=a_page(3))

    entries = fetch_peertube_channels(lambda host: host == 't1.example')

    assert sorted(e['host'] for e in entries) == ['t0.example', 't2.example']


def test_an_unreadable_index_is_a_source_error(app, http_mock):
    http_mock.get(SEPIASEARCH_URL).respond(503)

    with pytest.raises(sources.DiscoverySourceError):
        fetch_peertube_channels(nobody_excluded)


def test_fetch_json_answers_none_for_a_transport_error(app, http_mock):
    http_mock.get('https://down.example/x').mock(side_effect=httpx.ConnectError('refused'))

    assert sources.fetch_json('https://down.example/x') is None


def test_fetch_json_answers_none_for_a_body_that_is_not_json(app, http_mock):
    http_mock.get('https://html.example/x').respond(200, text='<html></html>')

    assert sources.fetch_json('https://html.example/x') is None


@pytest.mark.parametrize('value, expected', [(5, 5), (0, 0), (-1, 0), ('5', 0), (True, 0), (None, 0), (2.5, 0)])
def test_as_count(value, expected):
    assert sources.as_count(value) == expected


@pytest.mark.parametrize('value, expected', [('tube.example', True), ('Tube.Example', False), ('a/b.example', False),
                                             ('localhost', False), ('tube.example\n', False), (None, False)])
def test_is_hostname(value, expected):
    assert sources.is_hostname(value) is expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_discovery_peertube.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'app.discovery.peertube'`.

- [ ] **Step 3: Write `sources.py` and `peertube.py`**

Create `app/discovery/sources.py`:

```python
"""What every discovery fetcher shares: one polite GET, one entry shape, one failure type (interop D24)."""
import re
import time

import httpx
from flask import current_app

from app.utils import get_request

POLITE_DELAY_SECONDS = 1.0

_HOSTNAME = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+')
_USERNAME = re.compile(r'[A-Za-z0-9_.-]{1,64}')


class DiscoverySourceError(Exception):
    """The source's own index could not be read. The refresh skips the source and keeps its old entries."""


def polite_pause() -> None:
    """Spacing between two calls to one host, well under every published rate limit."""
    time.sleep(POLITE_DELAY_SECONDS)


def fetch_json(url: str, params: dict | None = None, headers: dict | None = None):
    """The decoded JSON body of a 200 answer, or None for a transport error, any other status, or a body
    that is not JSON. Goes through get_request, so the SSRF guards apply and redirects are not followed.
    Logs the url only: request headers may carry credentials."""
    try:
        response = get_request(url, params=params, headers=dict(headers or {}))
    except httpx.HTTPError as error:
        current_app.logger.info(f'discovery: {url} failed: {type(error).__name__}')
        return None
    try:
        if response.status_code != 200:
            return None
        try:
            return response.json()
        except ValueError:
            return None
    finally:
        response.close()


def make_entry(*, kind: str, platform: str, actor_url: str, name: str, host: str, avatar, followers: int,
               nsfw: bool, source: str) -> dict:
    return {'kind': kind, 'platform': platform, 'actor_url': actor_url, 'name': name, 'host': host,
            'avatar': avatar, 'followers': followers, 'nsfw': nsfw, 'source': source}


def as_count(value) -> int:
    """A peer's count. Anything that is not a non-negative int (bools included) is 0."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def is_hostname(value) -> bool:
    """A lower-case DNS name with at least one dot. Peers name hosts; we build urls from them."""
    return isinstance(value, str) and _HOSTNAME.fullmatch(value) is not None


def is_username(value) -> bool:
    return isinstance(value, str) and _USERNAME.fullmatch(value) is not None
```

Create `app/discovery/peertube.py`:

```python
"""PeerTube channels from SepiaSearch (interop D24). Public publisher channels. SepiaSearch cannot sort
by followers, so up to MAX_PAGES pages are read and ranked here."""
from urllib.parse import urlparse

from app.discovery import KIND_COMMUNITY, sources

SEPIASEARCH_URL = 'https://sepiasearch.org/api/v1/search/video-channels'
SOURCE = 'sepiasearch'
PAGE_SIZE = 100
MAX_PAGES = 5


def _largest_avatar(avatars):
    if not isinstance(avatars, list):
        return None
    best_width, best_url = -1, None
    for avatar in avatars:
        if isinstance(avatar, dict) and isinstance(avatar.get('url'), str):
            width = sources.as_count(avatar.get('width'))
            if width > best_width:
                best_width, best_url = width, avatar['url']
    return best_url


def channel_to_entry(channel) -> dict | None:
    """One SepiaSearch channel row as a normalised entry. The channel url is its ActivityPub id, and it
    must be on the host the row names, so a directory row cannot point us at a third party."""
    if not isinstance(channel, dict):
        return None
    url, host = channel.get('url'), channel.get('host')
    if not isinstance(url, str) or not isinstance(host, str) or urlparse(url).hostname != host.lower():
        return None
    name = channel.get('displayName') or channel.get('name')
    return sources.make_entry(kind=KIND_COMMUNITY, platform='peertube', actor_url=url,
                              name=name if isinstance(name, str) else '', host=host.lower(),
                              avatar=_largest_avatar(channel.get('avatars')),
                              followers=sources.as_count(channel.get('followersCount')), nsfw=False, source=SOURCE)


def fetch_peertube_channels(exclude) -> list[dict]:
    """At most MAX_PAGES pages of PAGE_SIZE channels, excluded hosts dropped, most-followed first."""
    entries = []
    for page in range(MAX_PAGES):
        if page:
            sources.polite_pause()
        payload = sources.fetch_json(SEPIASEARCH_URL, params={'start': page * PAGE_SIZE, 'count': PAGE_SIZE})
        data = payload.get('data') if isinstance(payload, dict) else None
        if not isinstance(data, list):
            if page == 0:
                raise sources.DiscoverySourceError(f'{SOURCE}: no channel list')
            break
        for channel in data:
            entry = channel_to_entry(channel)
            if entry is not None and not exclude(entry['host']):
                entries.append(entry)
        if len(data) < PAGE_SIZE:
            break
    return sorted(entries, key=lambda entry: entry['followers'], reverse=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./run_tests.sh tests/test_discovery_peertube.py tests/test_no_inline_imports.py tests/test_anchored_validators.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/discovery/sources.py app/discovery/peertube.py app/discovery/fixtures/sepiasearch.json tests/test_discovery_peertube.py
git commit -m "$(cat <<'EOF'
feat: discovery fetch helpers and the SepiaSearch PeerTube channel fetcher (interop D24)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Mastodon people fetcher (joinmastodon + profile directories)

**Files:**
- Create: `app/discovery/mastodon.py`
- Create: `app/discovery/fixtures/mastodon_directory.json`
- Test: `tests/test_discovery_mastodon.py`

**Interfaces:**
- Consumes: `app.discovery.KIND_PERSON` (Task 1); `app.discovery.sources.{fetch_json, polite_pause, make_entry, as_count, is_hostname, is_username, DiscoverySourceError}` (Task 2).
- Produces:
  - `app.discovery.mastodon.SERVERS_URL = 'https://api.joinmastodon.org/servers'`, `SOURCE = 'joinmastodon'`, `MAX_SERVERS = 20`, `DIRECTORY_LIMIT = 80`
  - `top_mastodon_servers(servers) -> list[str]` (domains by `last_week_users`, descending)
  - `account_to_entry(account, domain: str) -> dict | None` (only `discoverable is True`, non-bot accounts)
  - `fetch_mastodon_people(exclude: Callable[[str], bool]) -> list[dict]`

- [ ] **Step 1: Write the fixture and the failing test**

Create `app/discovery/fixtures/mastodon_directory.json`:

```json
[
  {"id": "1", "username": "ann", "acct": "ann", "display_name": "Ann Example", "discoverable": true, "bot": false,
   "uri": "https://mastodon.example/users/ann", "url": "https://mastodon.example/@ann",
   "avatar": "https://mastodon.example/avatars/ann.png", "followers_count": 340},
  {"id": "2", "username": "hidden", "acct": "hidden", "display_name": "Not Listed", "discoverable": false,
   "bot": false, "uri": "https://mastodon.example/users/hidden", "followers_count": 9000},
  {"id": "3", "username": "newsbot", "acct": "newsbot", "display_name": "News Bot", "discoverable": true,
   "bot": true, "uri": "https://mastodon.example/users/newsbot", "followers_count": 50},
  {"id": "4", "username": "ben", "acct": "ben", "display_name": "", "discoverable": true, "bot": false,
   "url": "https://mastodon.example/@ben", "avatar": "https://mastodon.example/avatars/ben.png",
   "followers_count": 12}
]
```

Create `tests/test_discovery_mastodon.py`:

```python
"""Interop D24: Mastodon people from the top joinmastodon servers' opt-in profile directories."""
import json
from pathlib import Path

import pytest

from app.discovery import mastodon, sources
from app.discovery.mastodon import SERVERS_URL, account_to_entry, fetch_mastodon_people, top_mastodon_servers

FIXTURE = Path(__file__).resolve().parent.parent / 'app' / 'discovery' / 'fixtures' / 'mastodon_directory.json'
DIRECTORY = 'https://mastodon.example/api/v1/directory'

pytestmark = pytest.mark.usefixtures('no_real_sleeping')


def nobody_excluded(host):
    return False


def servers(count):
    return [{'domain': f'm{i}.example', 'last_week_users': i} for i in range(count)]


def test_the_fixture_keeps_only_discoverable_people(app, http_mock):
    http_mock.get(SERVERS_URL).respond(json=[{'domain': 'mastodon.example', 'last_week_users': 100}])
    route = http_mock.get(DIRECTORY).respond(json=json.loads(FIXTURE.read_text()))

    entries = fetch_mastodon_people(nobody_excluded)

    assert [(e['name'], e['actor_url']) for e in entries] == [
        ('Ann Example', 'https://mastodon.example/users/ann'),
        ('ben', 'https://mastodon.example/users/ben')]
    assert entries[0] == {'kind': 'person', 'platform': 'mastodon', 'actor_url': 'https://mastodon.example/users/ann',
                          'name': 'Ann Example', 'host': 'mastodon.example',
                          'avatar': 'https://mastodon.example/avatars/ann.png', 'followers': 340, 'nsfw': False,
                          'source': 'joinmastodon'}
    params = route.calls.last.request.url.params
    assert (params['local'], params['order'], params['limit']) == ('true', 'active', '80')


def test_an_account_uri_on_another_host_is_not_trusted(app):
    entry = account_to_entry({'username': 'ann', 'discoverable': True, 'uri': 'https://evil.example/users/ann'},
                             'mastodon.example')

    assert entry['actor_url'] == 'https://mastodon.example/users/ann'


def test_only_the_twenty_busiest_servers_are_asked(app, http_mock, monkeypatch):
    pauses = []
    monkeypatch.setattr(sources, 'polite_pause', lambda: pauses.append(1))
    http_mock.get(SERVERS_URL).respond(json=servers(25))
    route = http_mock.get(url__regex=r'https://m\d+\.example/api/v1/directory').respond(json=[])

    fetch_mastodon_people(nobody_excluded)

    assert route.call_count == mastodon.MAX_SERVERS == 20
    assert [call.request.url.host for call in route.calls][:2] == ['m24.example', 'm23.example']
    assert len(pauses) == 19


def test_an_excluded_server_is_never_asked_and_the_next_one_is(app, http_mock):
    http_mock.get(SERVERS_URL).respond(json=servers(25))
    route = http_mock.get(url__regex=r'https://m\d+\.example/api/v1/directory').respond(json=[])

    fetch_mastodon_people(lambda host: host == 'm24.example')

    hosts = [call.request.url.host for call in route.calls]
    assert 'm24.example' not in hosts
    assert hosts[0] == 'm23.example' and len(hosts) == 20


def test_a_server_whose_directory_fails_is_skipped(app, http_mock):
    http_mock.get(SERVERS_URL).respond(json=[{'domain': 'a.example', 'last_week_users': 2},
                                             {'domain': 'mastodon.example', 'last_week_users': 1}])
    http_mock.get('https://a.example/api/v1/directory').respond(500)
    http_mock.get(DIRECTORY).respond(json=json.loads(FIXTURE.read_text()))

    assert len(fetch_mastodon_people(nobody_excluded)) == 2


def test_malformed_server_rows_are_ignored(app):
    """Review focus 1: a directory of servers is third-party data like any other."""
    rows = [None, {'domain': 'evil.example/path'}, {'domain': 42}, {'domain': 'ok.example', 'last_week_users': 'x'},
            {'domain': 'Big.Example ', 'last_week_users': 9}]

    assert top_mastodon_servers(rows) == ['big.example', 'ok.example']


def test_an_unreadable_server_list_is_a_source_error(app, http_mock):
    http_mock.get(SERVERS_URL).respond(502)

    with pytest.raises(sources.DiscoverySourceError):
        fetch_mastodon_people(nobody_excluded)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_discovery_mastodon.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'app.discovery.mastodon'`.

- [ ] **Step 3: Write `mastodon.py`**

Create `app/discovery/mastodon.py`:

```python
"""Mastodon people (interop D24): the top MAX_SERVERS servers from joinmastodon.org, then one page of
each server's local profile directory. The directory lists only accounts that opted in (`discoverable`);
a row that says otherwise, or a bot, is dropped here as well."""
from urllib.parse import urlparse

from app.discovery import KIND_PERSON, sources

SERVERS_URL = 'https://api.joinmastodon.org/servers'
SOURCE = 'joinmastodon'
MAX_SERVERS = 20
DIRECTORY_LIMIT = 80


def top_mastodon_servers(servers) -> list[str]:
    """Server domains, busiest first, junk rows dropped."""
    if not isinstance(servers, list):
        return []
    rows = []
    for server in servers:
        if not isinstance(server, dict) or not isinstance(server.get('domain'), str):
            continue
        domain = server['domain'].strip().lower()
        if sources.is_hostname(domain):
            rows.append((sources.as_count(server.get('last_week_users')), domain))
    rows.sort(key=lambda row: row[0], reverse=True)
    return [domain for _users, domain in rows]


def account_to_entry(account, domain: str) -> dict | None:
    if not isinstance(account, dict) or account.get('discoverable') is not True or account.get('bot') is True:
        return None
    username = account.get('username')
    if not sources.is_username(username):
        return None
    uri = account.get('uri')
    if isinstance(uri, str) and urlparse(uri).scheme == 'https' and urlparse(uri).hostname == domain:
        actor_url = uri
    else:
        actor_url = f'https://{domain}/users/{username}'
    name = account.get('display_name')
    return sources.make_entry(kind=KIND_PERSON, platform='mastodon', actor_url=actor_url,
                              name=name if isinstance(name, str) and name.strip() else username, host=domain,
                              avatar=account.get('avatar') if isinstance(account.get('avatar'), str) else None,
                              followers=sources.as_count(account.get('followers_count')), nsfw=False, source=SOURCE)


def fetch_mastodon_people(exclude) -> list[dict]:
    """One directory page from each of the MAX_SERVERS busiest servers that are not excluded."""
    servers = sources.fetch_json(SERVERS_URL)
    if not isinstance(servers, list):
        raise sources.DiscoverySourceError(f'{SOURCE}: no server list')
    entries = []
    asked = 0
    for domain in top_mastodon_servers(servers):
        if asked >= MAX_SERVERS:
            break
        if exclude(domain):
            continue
        if asked:
            sources.polite_pause()
        asked += 1
        accounts = sources.fetch_json(f'https://{domain}/api/v1/directory',
                                      params={'local': 'true', 'order': 'active', 'limit': DIRECTORY_LIMIT})
        if not isinstance(accounts, list):
            continue
        for account in accounts:
            entry = account_to_entry(account, domain)
            if entry is not None:
                entries.append(entry)
    return entries
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./run_tests.sh tests/test_discovery_mastodon.py tests/test_no_inline_imports.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/discovery/mastodon.py app/discovery/fixtures/mastodon_directory.json tests/test_discovery_mastodon.py
git commit -m "$(cat <<'EOF'
feat: Mastodon opt-in directory fetcher for discovery (interop D24)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Pixelfed people fetcher (FediDB + landing directories)

**Files:**
- Create: `app/discovery/pixelfed.py`
- Create: `app/discovery/fixtures/pixelfed_directory.json`
- Test: `tests/test_discovery_pixelfed.py`

**Interfaces:**
- Consumes: `app.discovery.KIND_PERSON` (Task 1); `app.discovery.sources.*` (Task 2).
- Produces:
  - `app.discovery.pixelfed.FEDIDB_URL = 'https://api.fedidb.org/v1/servers'`, `SOURCE = 'fedidb'`, `MAX_SERVERS = 20`, `MAX_PAGES = 2`, `FEDIDB_LIMIT = 40`
  - `pixelfed_hosts(payload) -> list[str]`
  - `profile_to_entry(profile, host: str) -> dict | None` (actor `https://{host}/users/{username}`)
  - `fetch_pixelfed_people(exclude: Callable[[str], bool]) -> list[dict]`

The Pixelfed directory is Laravel `cursorPaginate(20)` of profiles with `is_suggestable` true, not private, active (pixelfed `LandingController::getDirectoryApi`); `meta.next_cursor` gives the next page; a 404 means the admin turned the directory off.

- [ ] **Step 1: Write the fixture and the failing test**

Create `app/discovery/fixtures/pixelfed_directory.json`:

```json
{"data": [
  {"id": 10, "name": "Cara Photos", "username": "cara", "url": "https://pixelfed.example/cara",
   "avatar": "https://pixelfed.example/storage/avatars/cara.jpg", "followers_count": 210, "statuses_count": 40},
  {"id": 11, "name": null, "username": "dev", "url": "https://pixelfed.example/dev",
   "avatar": "https://pixelfed.example/storage/avatars/default.jpg", "followers_count": 3}
 ],
 "links": {"next": null},
 "meta": {"path": "https://pixelfed.example/api/landing/v1/directory", "per_page": 20, "next_cursor": null,
          "prev_cursor": null}}
```

Create `tests/test_discovery_pixelfed.py`:

```python
"""Interop D24: Pixelfed people from FediDB's Pixelfed hosts and each host's opt-in landing directory."""
import json
from pathlib import Path

import pytest

from app.discovery import pixelfed, sources
from app.discovery.pixelfed import FEDIDB_URL, fetch_pixelfed_people, pixelfed_hosts

FIXTURE = Path(__file__).resolve().parent.parent / 'app' / 'discovery' / 'fixtures' / 'pixelfed_directory.json'
DIRECTORY = 'https://pixelfed.example/api/landing/v1/directory'

pytestmark = pytest.mark.usefixtures('no_real_sleeping')


def nobody_excluded(host):
    return False


def fedidb(*domains):
    return {'data': [{'domain': domain} for domain in domains], 'meta': {'next_cursor': None}}


def test_the_fixture_is_normalised(app, http_mock):
    route = http_mock.get(FEDIDB_URL).respond(json=fedidb('pixelfed.example'))
    http_mock.get(DIRECTORY).respond(json=json.loads(FIXTURE.read_text()))

    entries = fetch_pixelfed_people(nobody_excluded)

    assert [(e['name'], e['actor_url']) for e in entries] == [
        ('Cara Photos', 'https://pixelfed.example/users/cara'), ('dev', 'https://pixelfed.example/users/dev')]
    assert entries[0] == {'kind': 'person', 'platform': 'pixelfed', 'actor_url': 'https://pixelfed.example/users/cara',
                          'name': 'Cara Photos', 'host': 'pixelfed.example',
                          'avatar': 'https://pixelfed.example/storage/avatars/cara.jpg', 'followers': 210,
                          'nsfw': False, 'source': 'fedidb'}
    assert route.calls.last.request.url.params['software'] == 'pixelfed'


def test_a_host_whose_directory_is_off_is_skipped(app, http_mock):
    http_mock.get(FEDIDB_URL).respond(json=fedidb('off.example', 'pixelfed.example'))
    http_mock.get('https://off.example/api/landing/v1/directory').respond(404)
    http_mock.get(DIRECTORY).respond(json=json.loads(FIXTURE.read_text()))

    assert {e['host'] for e in fetch_pixelfed_people(nobody_excluded)} == {'pixelfed.example'}


def test_at_most_two_pages_per_host(app, http_mock):
    http_mock.get(FEDIDB_URL).respond(json=fedidb('pixelfed.example'))
    second = http_mock.get(DIRECTORY, params={'cursor': 'abc'}).respond(
        json={'data': [{'username': 'two', 'name': 'Two'}], 'meta': {'next_cursor': 'def'}})
    first = http_mock.get(DIRECTORY).respond(
        json={'data': [{'username': 'one', 'name': 'One'}], 'meta': {'next_cursor': 'abc'}})

    entries = fetch_pixelfed_people(nobody_excluded)

    assert [e['name'] for e in entries] == ['One', 'Two']
    assert first.call_count == 1 and second.call_count == 1


def test_only_twenty_hosts_are_asked_and_excluded_ones_never(app, http_mock):
    http_mock.get(FEDIDB_URL).respond(json=fedidb(*[f'p{i}.example' for i in range(25)]))
    route = http_mock.get(url__regex=r'https://p\d+\.example/api/landing/v1/directory').respond(
        json={'data': [], 'meta': {'next_cursor': None}})

    fetch_pixelfed_people(lambda host: host == 'p0.example')

    hosts = [call.request.url.host for call in route.calls]
    assert len(hosts) == pixelfed.MAX_SERVERS == 20
    assert 'p0.example' not in hosts


def test_junk_host_rows_are_ignored(app):
    assert pixelfed_hosts({'data': [None, {'domain': 'a/b'}, {'domain': ' Pix.Example '}]}) == ['pix.example']
    assert pixelfed_hosts(['not', 'a', 'dict']) == []


def test_an_unreadable_fedidb_is_a_source_error(app, http_mock):
    http_mock.get(FEDIDB_URL).respond(500)

    with pytest.raises(sources.DiscoverySourceError):
        fetch_pixelfed_people(nobody_excluded)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_discovery_pixelfed.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'app.discovery.pixelfed'`.

- [ ] **Step 3: Write `pixelfed.py`**

Create `app/discovery/pixelfed.py`:

```python
"""Pixelfed people (interop D24): Pixelfed hosts from FediDB, then up to MAX_PAGES pages of each host's
landing directory, which lists only public accounts marked `is_suggestable`. A 404 means the host's
admin has the directory off; that host is skipped."""
from app.discovery import KIND_PERSON, sources

FEDIDB_URL = 'https://api.fedidb.org/v1/servers'
SOURCE = 'fedidb'
MAX_SERVERS = 20
MAX_PAGES = 2
FEDIDB_LIMIT = 40


def pixelfed_hosts(payload) -> list[str]:
    data = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    hosts = []
    for row in data:
        if isinstance(row, dict) and isinstance(row.get('domain'), str):
            domain = row['domain'].strip().lower()
            if sources.is_hostname(domain):
                hosts.append(domain)
    return hosts


def profile_to_entry(profile, host: str) -> dict | None:
    if not isinstance(profile, dict) or not sources.is_username(profile.get('username')):
        return None
    username = profile['username']
    name = profile.get('name')
    return sources.make_entry(kind=KIND_PERSON, platform='pixelfed', actor_url=f'https://{host}/users/{username}',
                              name=name if isinstance(name, str) and name.strip() else username, host=host,
                              avatar=profile.get('avatar') if isinstance(profile.get('avatar'), str) else None,
                              followers=sources.as_count(profile.get('followers_count')), nsfw=False, source=SOURCE)


def fetch_pixelfed_people(exclude) -> list[dict]:
    payload = sources.fetch_json(FEDIDB_URL, params={'software': 'pixelfed', 'limit': FEDIDB_LIMIT})
    if not isinstance(payload, dict):
        raise sources.DiscoverySourceError(f'{SOURCE}: no server list')
    entries = []
    asked_hosts = 0
    calls = 0
    for host in pixelfed_hosts(payload):
        if asked_hosts >= MAX_SERVERS:
            break
        if exclude(host):
            continue
        asked_hosts += 1
        cursor = None
        for _page in range(MAX_PAGES):
            if calls:
                sources.polite_pause()
            calls += 1
            body = sources.fetch_json(f'https://{host}/api/landing/v1/directory',
                                      params={'cursor': cursor} if cursor else None)
            data = body.get('data') if isinstance(body, dict) else None
            if not isinstance(data, list):
                break
            for profile in data:
                entry = profile_to_entry(profile, host)
                if entry is not None:
                    entries.append(entry)
            meta = body.get('meta')
            cursor = meta.get('next_cursor') if isinstance(meta, dict) else None
            if not isinstance(cursor, str) or not cursor:
                break
    return entries
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./run_tests.sh tests/test_discovery_pixelfed.py tests/test_no_inline_imports.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/discovery/pixelfed.py app/discovery/fixtures/pixelfed_directory.json tests/test_discovery_pixelfed.py
git commit -m "$(cat <<'EOF'
feat: Pixelfed opt-in directory fetcher for discovery (interop D24)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 5: Castopod podcasts from the Podcast Index API (signed requests)

**Files:**
- Create: `app/discovery/castopod.py`
- Create: `app/discovery/fixtures/castopod_podcastindex.json`
- Test: `tests/test_discovery_castopod_source.py`

**Interfaces:**
- Consumes: `app.discovery.KIND_COMMUNITY` (Task 1); `app.discovery.sources.*` (Task 2); `app.utils.get_setting(name, default=None)`.
- Produces:
  - `app.discovery.castopod.PODCASTINDEX_API = 'https://api.podcastindex.org/api/1.0'`, `SOURCE = 'podcastindex'`, `MAX_RESULTS = 200`, `MAX_EPISODE_LOOKUPS = 40`, `SETTING_KEY = 'podcastindex_api_key'`, `SETTING_SECRET = 'podcastindex_api_secret'`
  - `now_unix() -> int`
  - `podcastindex_headers(api_key: str, api_secret: str, now: int) -> dict` (`X-Auth-Key`, `X-Auth-Date`, `Authorization`)
  - `is_castopod_feed(feed: dict) -> bool`
  - `actor_url_from_social_interact(items) -> str | None`
  - `podcast_to_entry(feed: dict, actor_url: str) -> dict | None`
  - `fetch_castopod_podcasts(exclude: Callable[[str], bool]) -> list[dict]` (returns `[]` without both credentials)

How the API is used (from `pi_api.yaml`, v1.12.1): `/podcasts/trending?max=` lists feeds (`id`, `url`, `title`, `image`/`artwork`, `trendScore`, `explicit`); `socialInteract` is reported on episode items (`/episodes/byfeedid?id=&max=1`), not on feeds. So: one trending call (at most `MAX_RESULTS` = 200 results), keep feeds that look like Castopod (channel `generator` naming Castopod, or a Castopod feed url `https://host/@handle/feed.xml`), then at most `MAX_EPISODE_LOOKUPS` episode lookups to read the ActivityPub `accountUrl`. Podcast Index has no follower count, so `trendScore` fills `followers` as the ranking value.

- [ ] **Step 1: Write the fixture and the failing test**

Create `app/discovery/fixtures/castopod_podcastindex.json`:

```json
{"trending": {"status": "true", "feeds": [
   {"id": 101, "url": "https://pod.example/@mypodcast/feed.xml", "title": "My Podcast",
    "artwork": "https://pod.example/media/cover.jpg", "trendScore": 9, "explicit": false},
   {"id": 102, "url": "https://feeds.example/other.rss", "title": "Not Castopod", "trendScore": 30},
   {"id": 103, "url": "https://cast.example/@late/feed.xml", "title": "Late Night",
    "artwork": "https://cast.example/late.jpg", "trendScore": 4, "explicit": true}
 ]},
 "episodes": {
   "101": {"status": "true", "items": [{"id": 1, "title": "Episode 1",
            "link": "https://pod.example/@mypodcast/episodes/ep-1",
            "socialInteract": [{"url": "https://pod.example/@mypodcast/episodes/ep-1", "protocol": "activitypub",
                                "accountId": "@mypodcast@pod.example",
                                "accountUrl": "https://pod.example/@mypodcast"}]}]},
   "103": {"status": "true", "items": [{"id": 2, "title": "Pilot",
            "socialInteract": [{"protocol": "twitter", "accountUrl": "https://twitter.example/late"}]}]}
 }}
```

Create `tests/test_discovery_castopod_source.py`:

```python
"""Interop D24: Castopod podcasts found through the Podcast Index API, signed with the admin's key."""
import hashlib
import json
import logging
from pathlib import Path

import httpx
import pytest

from app import cache
from app.discovery import castopod, sources
from app.discovery.castopod import PODCASTINDEX_API, fetch_castopod_podcasts, podcastindex_headers
from app.utils import set_setting

FIXTURE = Path(__file__).resolve().parent.parent / 'app' / 'discovery' / 'fixtures' / 'castopod_podcastindex.json'
TRENDING = f'{PODCASTINDEX_API}/podcasts/trending'
EPISODES = f'{PODCASTINDEX_API}/episodes/byfeedid'
NOW = 1700000000

pytestmark = pytest.mark.usefixtures('no_real_sleeping')


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(castopod, 'now_unix', lambda: NOW)
    cache.clear()  # get_setting is memoized
    yield
    cache.clear()


@pytest.fixture
def credentials(db_session):
    set_setting(castopod.SETTING_KEY, 'KEYabc')
    set_setting(castopod.SETTING_SECRET, 'SECRETxyz')


def nobody_excluded(host):
    return False


def serve_fixture(http_mock):
    fixture = json.loads(FIXTURE.read_text())
    trending = http_mock.get(TRENDING).respond(json=fixture['trending'])
    episodes_101 = http_mock.get(EPISODES, params={'id': '101'}).respond(json=fixture['episodes']['101'])
    episodes_103 = http_mock.get(EPISODES, params={'id': '103'}).respond(json=fixture['episodes']['103'])
    return trending, episodes_101, episodes_103


def test_without_credentials_nothing_is_fetched(app, db_session, http_mock):
    assert fetch_castopod_podcasts(nobody_excluded) == []


def test_the_signature_is_sha1_of_key_secret_and_time():
    headers = podcastindex_headers('KEYabc', 'SECRETxyz', NOW)

    assert headers == {'X-Auth-Key': 'KEYabc', 'X-Auth-Date': str(NOW),
                       'Authorization': hashlib.sha1(f'KEYabcSECRETxyz{NOW}'.encode()).hexdigest()}


def test_every_request_is_signed(app, credentials, http_mock):
    trending, episodes_101, _episodes_103 = serve_fixture(http_mock)

    fetch_castopod_podcasts(nobody_excluded)

    for request in (trending.calls.last.request, episodes_101.calls.last.request):
        assert request.headers['X-Auth-Key'] == 'KEYabc'
        assert request.headers['X-Auth-Date'] == str(NOW)
        assert request.headers['Authorization'] == hashlib.sha1(f'KEYabcSECRETxyz{NOW}'.encode()).hexdigest()
        assert request.headers['User-Agent'].startswith('PieFed/')
    assert trending.calls.last.request.url.params['max'] == '200'


def test_only_podcasts_with_an_activitypub_social_interact_are_kept(app, credentials, http_mock):
    serve_fixture(http_mock)

    entries = fetch_castopod_podcasts(nobody_excluded)

    assert entries == [{'kind': 'community', 'platform': 'castopod', 'actor_url': 'https://pod.example/@mypodcast',
                        'name': 'My Podcast', 'host': 'pod.example', 'avatar': 'https://pod.example/media/cover.jpg',
                        'followers': 9, 'nsfw': False, 'source': 'podcastindex'}]


def test_an_excluded_podcast_host_is_dropped(app, credentials, http_mock):
    serve_fixture(http_mock)

    assert fetch_castopod_podcasts(lambda host: host == 'pod.example') == []


def test_episode_lookups_are_capped(app, credentials, http_mock):
    feeds = [{'id': i, 'url': f'https://c{i}.example/@show/feed.xml', 'title': f'Show {i}'} for i in range(50)]
    http_mock.get(TRENDING).respond(json={'feeds': feeds})
    route = http_mock.get(EPISODES).respond(json={'items': []})

    fetch_castopod_podcasts(nobody_excluded)

    assert route.call_count == castopod.MAX_EPISODE_LOOKUPS == 40


def test_a_failing_index_is_a_source_error_and_the_secret_is_never_logged(app, credentials, http_mock, caplog):
    caplog.set_level(logging.INFO)
    http_mock.get(TRENDING).mock(side_effect=httpx.ConnectError('refused'))

    with pytest.raises(sources.DiscoverySourceError):
        fetch_castopod_podcasts(nobody_excluded)

    assert 'SECRETxyz' not in caplog.text
    assert 'KEYabc' not in caplog.text


@pytest.mark.parametrize('feed, expected', [
    ({'url': 'https://pod.example/@show/feed.xml'}, True),
    ({'url': 'https://pod.example/@show/feed'}, True),
    ({'url': 'https://pod.example/feed.xml', 'generator': 'Castopod 1.12'}, True),
    ({'url': 'https://pod.example/@show/feed.xml\n'}, False),
    ({'url': 'https://feeds.example/other.rss'}, False),
])
def test_is_castopod_feed(feed, expected):
    assert castopod.is_castopod_feed(feed) is expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_discovery_castopod_source.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'app.discovery.castopod'`.

- [ ] **Step 3: Write `castopod.py`**

Create `app/discovery/castopod.py`:

```python
"""Castopod podcasts from the Podcast Index API (interop D24). Used only when the admin has entered an
API key and secret (site settings, never rendered back, never logged). A podcast is kept only when its
feed opts in to ActivityPub through <podcast:socialInteract protocol="activitypub">, which Podcast Index
reports on episode items; the actor url comes from that tag's accountUrl.

Signing (Podcast Index docs): Authorization = sha1(api_key + api_secret + X-Auth-Date) in hex.
"""
import hashlib
import re
import time
from urllib.parse import urlparse

from app.discovery import KIND_COMMUNITY, sources
from app.utils import get_setting

PODCASTINDEX_API = 'https://api.podcastindex.org/api/1.0'
SOURCE = 'podcastindex'
MAX_RESULTS = 200
MAX_EPISODE_LOOKUPS = 40
SETTING_KEY = 'podcastindex_api_key'
SETTING_SECRET = 'podcastindex_api_secret'

_CASTOPOD_FEED = re.compile(r'https://[^/\s]+/@[^/\s]+/feed(?:\.xml)?')


def now_unix() -> int:
    return int(time.time())


def podcastindex_headers(api_key: str, api_secret: str, now: int) -> dict:
    stamp = str(now)
    return {'X-Auth-Key': api_key, 'X-Auth-Date': stamp,
            'Authorization': hashlib.sha1(f'{api_key}{api_secret}{stamp}'.encode('utf-8')).hexdigest()}


def is_castopod_feed(feed) -> bool:
    if not isinstance(feed, dict):
        return False
    generator, url = feed.get('generator'), feed.get('url')
    if isinstance(generator, str) and 'castopod' in generator.lower():
        return True
    return isinstance(url, str) and _CASTOPOD_FEED.fullmatch(url) is not None


def actor_url_from_social_interact(items) -> str | None:
    """The first https accountUrl of an activitypub socialInteract tag on any item."""
    if not isinstance(items, list):
        return None
    for item in items:
        interacts = item.get('socialInteract') if isinstance(item, dict) else None
        if not isinstance(interacts, list):
            continue
        for interact in interacts:
            if not isinstance(interact, dict) or interact.get('protocol') != 'activitypub':
                continue
            account = interact.get('accountUrl')
            if isinstance(account, str) and urlparse(account).scheme == 'https' and urlparse(account).hostname:
                return account
    return None


def podcast_to_entry(feed, actor_url: str) -> dict | None:
    if not isinstance(feed, dict):
        return None
    image = feed.get('artwork') or feed.get('image')
    title = feed.get('title')
    return sources.make_entry(kind=KIND_COMMUNITY, platform='castopod', actor_url=actor_url,
                              name=title if isinstance(title, str) else '', host=urlparse(actor_url).hostname,
                              avatar=image if isinstance(image, str) else None,
                              followers=sources.as_count(feed.get('trendScore')), nsfw=feed.get('explicit') is True,
                              source=SOURCE)


def _signed_json(path: str, params: dict, api_key: str, api_secret: str):
    return sources.fetch_json(f'{PODCASTINDEX_API}{path}', params=params,
                              headers=podcastindex_headers(api_key, api_secret, now_unix()))


def fetch_castopod_podcasts(exclude) -> list[dict]:
    api_key = (get_setting(SETTING_KEY, '') or '').strip()
    api_secret = (get_setting(SETTING_SECRET, '') or '').strip()
    if not api_key or not api_secret:
        return []
    trending = _signed_json('/podcasts/trending', {'max': MAX_RESULTS}, api_key, api_secret)
    feeds = trending.get('feeds') if isinstance(trending, dict) else None
    if not isinstance(feeds, list):
        raise sources.DiscoverySourceError(f'{SOURCE}: no feed list')
    entries = []
    lookups = 0
    for feed in feeds[:MAX_RESULTS]:
        if lookups >= MAX_EPISODE_LOOKUPS:
            break
        if not is_castopod_feed(feed) or isinstance(feed.get('id'), bool) or not isinstance(feed.get('id'), int):
            continue
        sources.polite_pause()
        lookups += 1
        episodes = _signed_json('/episodes/byfeedid', {'id': feed['id'], 'max': 1}, api_key, api_secret)
        actor_url = actor_url_from_social_interact(episodes.get('items') if isinstance(episodes, dict) else None)
        if actor_url is None or exclude(urlparse(actor_url).hostname):
            continue
        entry = podcast_to_entry(feed, actor_url)
        if entry is not None:
            entries.append(entry)
    return entries
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./run_tests.sh tests/test_discovery_castopod_source.py tests/test_no_inline_imports.py tests/test_anchored_validators.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/discovery/castopod.py app/discovery/fixtures/castopod_podcastindex.json tests/test_discovery_castopod_source.py
git commit -m "$(cat <<'EOF'
feat: Podcast Index fetcher for Castopod podcasts, with signed requests (interop D24)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Refresh — filters, cleaning, caps, upsert and 30-day expiry

**Files:**
- Create: `app/discovery/filters.py`
- Create: `app/discovery/refresh.py`
- Test: `tests/test_discovery_refresh.py`

**Interfaces:**
- Consumes: `DiscoveryEntry` (Task 1); `sources.DiscoverySourceError`, `sources.as_count` (Task 2); `peertube.SOURCE/fetch_peertube_channels` (Task 2), `mastodon.SOURCE/fetch_mastodon_people` (Task 3), `pixelfed.SOURCE/fetch_pixelfed_people` (Task 4), `castopod.SOURCE/fetch_castopod_podcasts` (Task 5); `app.utils.{instance_banned, instance_allowed, get_setting, url_is_storable, retrieve_peertube_block_list}`; `app.community.util.is_bad_name(name) -> bool`.
- Produces:
  - `app.discovery.filters.NAME_LIMIT = 256`, `URL_LIMIT = 1024`
  - `peertube_isolated_hosts() -> frozenset[str]` (memoized one day)
  - `host_is_excluded(host: str, isolated: frozenset) -> bool`
  - `clean_name(value, limit: int = NAME_LIMIT) -> str | None`
  - `clean_https_url(value, limit: int = URL_LIMIT) -> str | None`
  - `looks_nsfw(name: str) -> bool`
  - `app.discovery.refresh.EXPIRY_DAYS = 30`, `MAX_PER_HOST = 20`, `MAX_PER_SOURCE = 500`
  - `FETCHERS: dict[str, Callable[[Callable[[str], bool]], list[dict]]]` keyed by source name
  - `clean_entries(entries: list[dict], exclude: Callable[[str], bool]) -> list[dict]`
  - `upsert_entries(entries: list[dict], now: datetime) -> int`
  - `expire_entries(now: datetime) -> int`
  - `refresh_discovery(now: datetime | None = None) -> dict[str, int | str]` (per source: rows upserted or `'failed'`; plus `'expired'`)

- [ ] **Step 1: Write the failing test**

Create `tests/test_discovery_refresh.py`:

```python
"""Interop D24: the daily refresh cleans what the directories send, filters it, caps it, upserts it on
actor_url, and forgets what nobody has listed for 30 days."""
from datetime import timedelta

import pytest

from app import cache, db
from app.discovery import filters, refresh, sources
from app.discovery.refresh import clean_entries, refresh_discovery
from app.models import DiscoveryEntry, Domain, utcnow
from tests.factories import make_banned_instance

NOW = utcnow()


@pytest.fixture(autouse=True)
def fresh_cache():
    cache.clear()  # instance_banned and peertube_isolated_hosts are memoized
    yield
    cache.clear()


@pytest.fixture
def only(monkeypatch):
    """Every fetcher returns nothing unless the test installs one; no isolation list is fetched."""
    monkeypatch.setattr(refresh, 'peertube_isolated_hosts', lambda: frozenset())
    for source in list(refresh.FETCHERS):
        monkeypatch.setitem(refresh.FETCHERS, source, lambda exclude: [])

    def install(source, result):
        monkeypatch.setitem(refresh.FETCHERS, source, result if callable(result) else (lambda exclude: result))
    return install


def entry(name='Linux Videos', host='tube.example', slug=None, **changes):
    values = {'kind': 'community', 'platform': 'peertube',
              'actor_url': f'https://{host}/video-channels/{slug or name.lower().replace(" ", "_")}',
              'name': name, 'host': host, 'avatar': None, 'followers': 1, 'nsfw': False, 'source': 'sepiasearch'}
    values.update(changes)
    return values


def nobody_excluded(host):
    return False


def test_an_upsert_is_idempotent_and_keeps_first_seen(app, db_session, only):
    only('sepiasearch', [entry(followers=1)])
    refresh_discovery(now=NOW - timedelta(days=2))
    only('sepiasearch', [entry(followers=7)])

    result = refresh_discovery(now=NOW)

    row = DiscoveryEntry.query.one()
    assert result['sepiasearch'] == 1
    assert row.followers == 7
    assert row.first_seen == NOW - timedelta(days=2)
    assert row.last_seen == NOW


def test_entries_not_seen_for_thirty_days_expire(app, db_session, only):
    db.session.add_all([
        DiscoveryEntry(kind='community', platform='peertube', actor_url='https://old.example/video-channels/a',
                       name='Old', host='old.example', source='sepiasearch',
                       first_seen=NOW - timedelta(days=40), last_seen=NOW - timedelta(days=31)),
        DiscoveryEntry(kind='community', platform='peertube', actor_url='https://new.example/video-channels/b',
                       name='Recent', host='new.example', source='sepiasearch',
                       first_seen=NOW - timedelta(days=40), last_seen=NOW - timedelta(days=29))])
    db.session.commit()

    result = refresh_discovery(now=NOW)

    assert result['expired'] == 1
    assert [row.name for row in DiscoveryEntry.query.all()] == ['Recent']


def test_a_failing_source_keeps_its_entries_and_the_others_still_run(app, db_session, only):
    db.session.add(DiscoveryEntry(kind='community', platform='peertube', actor_url='https://t.example/video-channels/k',
                                  name='Kept', host='t.example', source='sepiasearch',
                                  first_seen=NOW - timedelta(days=10), last_seen=NOW - timedelta(days=10)))
    db.session.commit()

    def broken(exclude):
        raise sources.DiscoverySourceError('down')
    only('sepiasearch', broken)
    only('joinmastodon', [entry(name='Ann', host='m.example', kind='person', platform='mastodon',
                                actor_url='https://m.example/users/ann', source='joinmastodon')])

    result = refresh_discovery(now=NOW)

    assert result['sepiasearch'] == 'failed'
    assert result['joinmastodon'] == 1
    kept = DiscoveryEntry.query.filter_by(name='Kept').one()
    assert kept.last_seen == NOW - timedelta(days=10)


def test_names_and_urls_are_cleaned(app, db_session):
    rows = [entry(name='  <b>Linux</b>\x00 Videos  ', slug='linux', avatar='https://tube.example/a.png'),
            entry(name='Plain http', host='h.example', actor_url='http://h.example/video-channels/x'),
            entry(name='Script avatar', host='s.example', avatar='javascript:alert(1)'),
            entry(name='Wrong host', host='w.example', actor_url='https://elsewhere.example/video-channels/x'),
            entry(name='   ', host='e.example', slug='blank')]

    cleaned = clean_entries(rows, nobody_excluded)

    assert [(e['name'], e['avatar']) for e in cleaned] == [('Linux Videos', 'https://tube.example/a.png'),
                                                           ('Script avatar', None)]


def test_banned_blocked_isolated_and_badly_named_entries_are_dropped(app, db_session, only, monkeypatch):
    make_banned_instance('banned.example')
    db.session.add(Domain(name='blockeddomain.example', banned=True))
    db.session.commit()
    monkeypatch.setattr(refresh, 'peertube_isolated_hosts', lambda: frozenset({'isolated.example'}))
    only('sepiasearch', [entry(name='Fine'), entry(name='Banned', host='banned.example'),
                         entry(name='Blocked', host='blockeddomain.example'),
                         entry(name='Isolated', host='isolated.example'), entry(name='shitposting central')])

    refresh_discovery(now=NOW)

    assert [row.name for row in DiscoveryEntry.query.all()] == ['Fine']


def test_nsfw_is_tagged_from_the_source_or_the_name(app, db_session):
    cleaned = clean_entries([entry(name='Art'), entry(name='Art NSFW', slug='a2'), entry(name='Flagged', nsfw=True)],
                            nobody_excluded)

    assert [(e['name'], e['nsfw']) for e in cleaned] == [('Art', False), ('Art NSFW', True), ('Flagged', True)]


def test_entries_are_capped_per_host_and_per_source(app, db_session, monkeypatch):
    many = [entry(name=f'Channel {i}', slug=f'c{i}') for i in range(25)]
    assert len(clean_entries(many, nobody_excluded)) == refresh.MAX_PER_HOST == 20

    monkeypatch.setattr(refresh, 'MAX_PER_SOURCE', 3)
    spread = [entry(name=f'Channel {i}', host=f'h{i}.example') for i in range(10)]
    assert len(clean_entries(spread, nobody_excluded)) == 3


def test_a_duplicate_actor_in_one_run_is_stored_once(app, db_session, only):
    """Review focus 2: ON CONFLICT DO UPDATE refuses to touch one row twice in one statement."""
    only('sepiasearch', [entry(followers=1), entry(followers=2)])

    result = refresh_discovery(now=NOW)

    assert result['sepiasearch'] == 1
    assert DiscoveryEntry.query.count() == 1


def test_the_isolation_list_is_read_into_lower_case_hosts(app, monkeypatch):
    monkeypatch.setattr(filters, 'retrieve_peertube_block_list', lambda: 'a.example\nB.Example\n\n')

    assert filters.peertube_isolated_hosts() == frozenset({'a.example', 'b.example'})


def test_an_unreachable_isolation_list_is_empty(app, monkeypatch):
    monkeypatch.setattr(filters, 'retrieve_peertube_block_list', lambda: None)

    assert filters.peertube_isolated_hosts() == frozenset()


def test_allowlist_mode_excludes_hosts_not_on_the_list(app, db_session):
    from app.models import AllowedInstances
    from app.utils import set_setting
    set_setting('use_allowlist', True)
    db.session.add(AllowedInstances(domain='allowed.example'))
    db.session.commit()

    assert filters.host_is_excluded('allowed.example', frozenset()) is False
    assert filters.host_is_excluded('other.example', frozenset()) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_discovery_refresh.py -v`
Expected: FAIL at collection with `ImportError: cannot import name 'filters' from 'app.discovery'`.

- [ ] **Step 3: Write `filters.py` and `refresh.py`**

Create `app/discovery/filters.py`:

```python
"""What discovery refuses to store or show (interop D24): banned or non-allowlisted instances, banned
domains, PeerTube isolation-list hosts; and how third-party names and urls are cleaned."""
import re
from urllib.parse import urlparse

from app import cache, db
from app.models import Domain
from app.utils import get_setting, instance_allowed, instance_banned, retrieve_peertube_block_list, url_is_storable

NAME_LIMIT = 256
URL_LIMIT = 1024

_TAG = re.compile(r'<[^>]*>')
_CONTROL = re.compile(r'[\x00-\x1f\x7f]+')
_SPACE = re.compile(r'\s+')
_NSFW = re.compile(r'\bnsfw\b|\b18\+|\bporn', re.IGNORECASE)


@cache.memoize(timeout=86400)
def peertube_isolated_hosts() -> frozenset:
    """Hosts on the PeerTube isolation list (https://peertube_isolation.frama.io/). Empty when the list
    cannot be fetched: `flask init-db` already copied it into banned_instances, which is checked anyway."""
    listing = retrieve_peertube_block_list()
    return frozenset(line.strip().lower() for line in (listing or '').split('\n') if line.strip())


def host_is_excluded(host: str, isolated: frozenset) -> bool:
    if not host:
        return True
    host = host.strip().lower()
    if host in isolated or instance_banned(host):
        return True
    if get_setting('use_allowlist', False) and not instance_allowed(host):
        return True
    return db.session.query(Domain.id).filter(Domain.name == host, Domain.banned == True).first() is not None


def clean_name(value, limit: int = NAME_LIMIT) -> str | None:
    """Plain text: tags and control characters removed, whitespace collapsed, capped. None if empty."""
    if not isinstance(value, str):
        return None
    text = _SPACE.sub(' ', _CONTROL.sub(' ', _TAG.sub('', value))).strip()
    return text[:limit].strip() or None


def clean_https_url(value, limit: int = URL_LIMIT) -> str | None:
    if not isinstance(value, str) or len(value) > limit:
        return None
    parsed = urlparse(value)
    if parsed.scheme != 'https' or not parsed.hostname or not url_is_storable(value):
        return None
    return value


def looks_nsfw(name: str) -> bool:
    return isinstance(name, str) and _NSFW.search(name) is not None
```

Create `app/discovery/refresh.py`:

```python
"""The daily discovery refresh (interop D24): `flask refresh_discovery`, from daily.sh. Never on a request."""
from datetime import timedelta
from urllib.parse import urlparse

from flask import current_app
from sqlalchemy.dialects.postgresql import insert

from app import db
from app.community.util import is_bad_name
from app.discovery import KIND_COMMUNITY, KIND_PERSON, PLATFORMS, castopod, mastodon, peertube, pixelfed, sources
from app.discovery.filters import clean_https_url, clean_name, host_is_excluded, looks_nsfw, peertube_isolated_hosts
from app.models import DiscoveryEntry, utcnow

EXPIRY_DAYS = 30
MAX_PER_HOST = 20
MAX_PER_SOURCE = 500

FETCHERS = {
    peertube.SOURCE: peertube.fetch_peertube_channels,
    castopod.SOURCE: castopod.fetch_castopod_podcasts,
    mastodon.SOURCE: mastodon.fetch_mastodon_people,
    pixelfed.SOURCE: pixelfed.fetch_pixelfed_people,
}

_UPDATED = ('kind', 'platform', 'name', 'host', 'avatar_url', 'followers', 'nsfw', 'source', 'last_seen')


def clean_entries(entries, exclude) -> list[dict]:
    """Validated, filtered, de-duplicated and capped copies of one source's entries, in source order."""
    kept, per_host, seen = [], {}, set()
    for entry in entries:
        if len(kept) >= MAX_PER_SOURCE:
            break
        if not isinstance(entry, dict) or entry.get('kind') not in (KIND_COMMUNITY, KIND_PERSON) \
                or entry.get('platform') not in PLATFORMS:
            continue
        actor_url = clean_https_url(entry.get('actor_url'))
        name = clean_name(entry.get('name'))
        host = entry.get('host').strip().lower() if isinstance(entry.get('host'), str) else ''
        if actor_url is None or name is None or urlparse(actor_url).hostname != host:
            continue
        if actor_url in seen or per_host.get(host, 0) >= MAX_PER_HOST or exclude(host) or is_bad_name(name):
            continue
        seen.add(actor_url)
        per_host[host] = per_host.get(host, 0) + 1
        kept.append({**entry, 'actor_url': actor_url, 'name': name, 'host': host,
                     'avatar': clean_https_url(entry.get('avatar')),
                     'followers': sources.as_count(entry.get('followers')),
                     'nsfw': entry.get('nsfw') is True or looks_nsfw(name)})
    return kept


def upsert_entries(entries, now) -> int:
    if not entries:
        return 0
    rows = [{'kind': e['kind'], 'platform': e['platform'], 'actor_url': e['actor_url'], 'name': e['name'],
             'host': e['host'], 'avatar_url': e['avatar'], 'followers': e['followers'], 'nsfw': e['nsfw'],
             'source': e['source'], 'first_seen': now, 'last_seen': now} for e in entries]
    statement = insert(DiscoveryEntry).values(rows)
    statement = statement.on_conflict_do_update(index_elements=['actor_url'],
                                                set_={column: statement.excluded[column] for column in _UPDATED})
    db.session.execute(statement)
    db.session.commit()
    return len(rows)


def expire_entries(now) -> int:
    expired = db.session.query(DiscoveryEntry).filter(
        DiscoveryEntry.last_seen < now - timedelta(days=EXPIRY_DAYS)).delete(synchronize_session=False)
    db.session.commit()
    return expired


def refresh_discovery(now=None) -> dict:
    now = now or utcnow()
    isolated = peertube_isolated_hosts()

    def exclude(host):
        return host_is_excluded(host, isolated)

    results = {}
    for source, fetch in FETCHERS.items():
        try:
            fetched = fetch(exclude)
        except sources.DiscoverySourceError as error:
            current_app.logger.warning(f'discovery: {source} skipped: {error}')
            results[source] = 'failed'
            continue
        except Exception:  # not bare: a worker shutdown must propagate. One broken source must not stop the rest.
            current_app.logger.exception(f'discovery: {source} failed')
            db.session.rollback()
            results[source] = 'failed'
            continue
        results[source] = upsert_entries(clean_entries(fetched, exclude), now)
    results['expired'] = expire_entries(now)
    return results
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./run_tests.sh tests/test_discovery_refresh.py tests/test_no_inline_imports.py tests/test_anchored_validators.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/discovery/filters.py app/discovery/refresh.py tests/test_discovery_refresh.py
git commit -m "$(cat <<'EOF'
feat: discovery refresh - filter, clean, cap, upsert and expire directory entries (interop D24)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: `flask refresh_discovery` and `daily.sh`

**Files:**
- Create: `app/discovery/cli.py`
- Modify: `app/cli.py` (one top-level import; one call as the first statement of `register(app)`, line 66)
- Modify: `daily.sh` (append one line)
- Test: `tests/test_discovery_cli.py`

**Interfaces:**
- Consumes: `refresh_discovery(now=None) -> dict` (Task 6); `refresh.FETCHERS`, `refresh.peertube_isolated_hosts` (Task 6).
- Produces: `app.discovery.cli.register_discovery_commands(app) -> None` registering `flask refresh_discovery`; Task 18 adds a second command to the same function.

- [ ] **Step 1: Write the failing test**

Create `tests/test_discovery_cli.py`:

```python
"""Interop D24: `flask refresh_discovery`, run daily from daily.sh and never on a request."""
from pathlib import Path

from app import cli
from app.discovery import refresh

ROOT = Path(__file__).resolve().parent.parent


def test_the_command_prints_each_sources_outcome(app, db_session, monkeypatch):
    monkeypatch.setattr('app.discovery.cli.refresh_discovery',
                        lambda: {'sepiasearch': 3, 'podcastindex': 'failed', 'expired': 1})
    cli.register(app)   # pyfedi.py registers the commands; the test app has none

    result = app.test_cli_runner().invoke(args=['refresh_discovery'])

    assert result.exception is None, result.exception
    assert 'sepiasearch: 3' in result.output
    assert 'podcastindex: failed' in result.output
    assert 'expired: 1' in result.output


def test_the_command_runs_the_real_refresh(app, db_session, monkeypatch):
    monkeypatch.setattr(refresh, 'peertube_isolated_hosts', lambda: frozenset())
    for source in list(refresh.FETCHERS):
        monkeypatch.setitem(refresh.FETCHERS, source, lambda exclude: [])
    cli.register(app)

    result = app.test_cli_runner().invoke(args=['refresh_discovery'])

    assert result.exception is None, result.exception
    assert 'expired: 0' in result.output


def test_daily_sh_runs_the_refresh_after_daily_maintenance():
    lines = [line.strip() for line in (ROOT / 'daily.sh').read_text().splitlines()]

    assert lines.index('flask refresh_discovery') > lines.index('flask daily-maintenance')
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_discovery_cli.py -v`
Expected: FAIL — `test_the_command_prints_each_sources_outcome` with `AttributeError: ... has no attribute 'cli'` (module `app.discovery.cli` missing), and the daily.sh test with `ValueError: 'flask refresh_discovery' is not in list`.

- [ ] **Step 3: Write the command and the two seams**

Create `app/discovery/cli.py`:

```python
"""Discovery CLI commands (interop D24). Registered from app/cli.py's register()."""
import click

from app.discovery.refresh import refresh_discovery


def register_discovery_commands(app) -> None:
    @app.cli.command('refresh_discovery')
    def refresh_discovery_command():
        """Refresh the discovery directory from SepiaSearch, Podcast Index, Mastodon and Pixelfed directories."""
        for source, outcome in refresh_discovery().items():
            click.echo(f'{source}: {outcome}')
```

In `app/cli.py`, add to the top-level imports (next to the other `from app...` imports):

```python
from app.discovery.cli import register_discovery_commands
```

and make the first statement inside `def register(app):` (line 66):

```python
    register_discovery_commands(app)   # fork (interop D24): flask refresh_discovery
```

Append to `daily.sh` (after `flask daily-maintenance`):

```bash
flask refresh_discovery
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./run_tests.sh tests/test_discovery_cli.py tests/test_cli_publish_scheduled_posts.py tests/test_no_inline_imports.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/discovery/cli.py app/cli.py daily.sh tests/test_discovery_cli.py
git commit -m "$(cat <<'EOF'
feat: flask refresh_discovery, run from daily.sh (interop D24)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Admin discovery page — Podcast Index credentials and attribution

**Files:**
- Create: `app/discovery/forms.py`
- Create: `app/discovery/admin_views.py`
- Create: `app/templates/admin/federation_discovery.html`
- Modify: `app/admin/__init__.py` (one module-level import after `from app.admin import routes`)
- Modify: `app/templates/admin/federation_preload.html` (one link paragraph)
- Test: `tests/test_discovery_admin_credentials.py`

**Interfaces:**
- Consumes: `castopod.SETTING_KEY`, `castopod.SETTING_SECRET` (Task 5); `app.utils.{get_setting, set_setting, login_required, permission_required, render_template, roles_with}`; `app.admin.bp`.
- Produces:
  - `app.discovery.forms.PodcastIndexCredentialsForm` with fields `podcastindex_api_key`, `podcastindex_api_secret` (PasswordField, never re-rendered), `podcastindex_save`, `podcastindex_remove`.
  - Route `GET/POST /admin/federation/discovery`, endpoint `admin.admin_federation_discovery`, view `app.discovery.admin_views.admin_federation_discovery`; template `admin/federation_discovery.html` receiving `credentials_form`, `preload_form` (Task 9; `None` until then), `candidates` (Task 9; `None` until then), `credentials_configured: bool`.
  - Field names are unprefixed and distinct across the page's forms: `app.utils.login_required` validates the POST's bare `csrf_token`, which a `prefix=` form would rename.

- [ ] **Step 1: Write the failing test**

Create `tests/test_discovery_admin_credentials.py`:

```python
"""Interop D24, decision 4: the Podcast Index key and secret are write-only site settings, and the
discovery page names every data source."""
import pytest

from app import cache, db
from app.discovery.castopod import SETTING_KEY, SETTING_SECRET
from app.utils import get_setting, set_setting
from tests.factories import grant_permission, make_instance, make_user
from tests.test_admin_federation import csrf, login

pytestmark = pytest.mark.usefixtures('site')

PAGE = '/admin/federation/discovery'


@pytest.fixture(autouse=True)
def fresh_cache():
    cache.clear()  # get_setting is memoized
    yield
    cache.clear()


@pytest.fixture
def admin(app, db_session):
    instance = make_instance('test.piefed.local', software='piefed')
    make_user(instance, 'founder', local=True)
    user = make_user(instance, 'settingsadmin', local=True)
    user.verified = True
    db.session.commit()
    grant_permission(user, 'change instance settings')
    client = app.test_client()
    login(client, user)
    return client, csrf(app, client)


def save(client, token, key='', secret='', button='podcastindex_save'):
    return client.post(PAGE, data={'podcastindex_api_key': key, 'podcastindex_api_secret': secret,
                                   button: 'go', 'csrf_token': token})


def test_saved_credentials_are_stored_and_never_rendered_back(admin):
    client, token = admin

    assert save(client, token, 'KEYabc', 'SECRETxyz').status_code == 302
    page = client.get(PAGE).get_data(as_text=True)

    assert get_setting(SETTING_KEY) == 'KEYabc'
    assert get_setting(SETTING_SECRET) == 'SECRETxyz'
    assert 'KEYabc' not in page and 'SECRETxyz' not in page
    assert 'Status: configured' in page


def test_blank_fields_keep_what_is_stored_and_padding_is_trimmed(admin):
    """Review focus 4."""
    client, token = admin
    save(client, token, '  KEYabc  ', ' SECRETxyz ')

    save(client, token, '', '')

    assert get_setting(SETTING_KEY) == 'KEYabc'
    assert get_setting(SETTING_SECRET) == 'SECRETxyz'


def test_remove_clears_both(admin):
    client, token = admin
    save(client, token, 'KEYabc', 'SECRETxyz')

    save(client, token, button='podcastindex_remove')

    assert not get_setting(SETTING_KEY) and not get_setting(SETTING_SECRET)
    assert 'Status: not set' in client.get(PAGE).get_data(as_text=True)


def test_the_page_names_every_data_source(admin):
    client, _token = admin

    page = client.get(PAGE).get_data(as_text=True)

    for source in ('https://sepiasearch.org/', 'https://podcastindex.org/', 'https://joinmastodon.org/',
                   'https://fedidb.org/'):
        assert source in page


def test_the_preload_page_links_here(admin):
    client, _token = admin

    assert PAGE in client.get('/admin/federation/preload').get_data(as_text=True)


def test_someone_without_the_permission_cannot_open_it(app, db_session):
    instance = make_instance('test.piefed.local', software='piefed')
    make_user(instance, 'founder', local=True)
    ordinary = make_user(instance, 'ordinary', local=True)
    client = app.test_client()
    login(client, ordinary)

    assert client.get(PAGE).status_code != 200
    set_setting(SETTING_KEY, 'unchanged')
    save(client, csrf(app, client), 'stolen', 'stolen')
    assert get_setting(SETTING_KEY) == 'unchanged'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_discovery_admin_credentials.py -v`
Expected: FAIL — GET `/admin/federation/discovery` answers 404, so `'Status: configured' in page` fails.

- [ ] **Step 3: Write the form, the view, the template and the two seams**

Create `app/discovery/forms.py`:

```python
"""Admin forms for discovery (interop D24). Field names are distinct across the page because
app.utils.login_required validates a bare `csrf_token`, which a prefixed form would rename."""
from flask_babel import lazy_gettext as _l
from flask_wtf import FlaskForm
from wtforms import PasswordField, SubmitField
from wtforms.validators import Length, Optional


class PodcastIndexCredentialsForm(FlaskForm):
    # PasswordField never renders its value back, which is what makes these write-only
    podcastindex_api_key = PasswordField(_l('Podcast Index API key'), validators=[Optional(), Length(max=128)])
    podcastindex_api_secret = PasswordField(_l('Podcast Index API secret'), validators=[Optional(), Length(max=128)])
    podcastindex_save = SubmitField(_l('Save credentials'))
    podcastindex_remove = SubmitField(_l('Remove credentials'))
```

Create `app/discovery/admin_views.py`:

```python
"""The admin discovery page (interop D24): Podcast Index credentials, the channel/podcast pre-load
(Task 9 of the plan) and the data-source attribution. Lives on the admin blueprint."""
from flask import flash, redirect, url_for
from flask_babel import _

from app.admin import bp
from app.discovery.castopod import SETTING_KEY, SETTING_SECRET
from app.discovery.forms import PodcastIndexCredentialsForm
from app.utils import get_setting, login_required, permission_required, render_template, roles_with, set_setting


def _credentials_configured() -> bool:
    return bool(get_setting(SETTING_KEY, '') and get_setting(SETTING_SECRET, ''))


@bp.route('/federation/discovery', methods=['GET', 'POST'])
@login_required
@permission_required('change instance settings')
def admin_federation_discovery():
    credentials_form = PodcastIndexCredentialsForm()
    preload_form = None
    candidates = None

    if credentials_form.podcastindex_save.data and credentials_form.validate_on_submit():
        # A blank field means "leave it": the stored values are never sent back to the browser to resubmit
        api_key = (credentials_form.podcastindex_api_key.data or '').strip()
        api_secret = (credentials_form.podcastindex_api_secret.data or '').strip()
        if api_key:
            set_setting(SETTING_KEY, api_key)
        if api_secret:
            set_setting(SETTING_SECRET, api_secret)
        flash(_('Podcast Index credentials saved.'))
        return redirect(url_for('admin.admin_federation_discovery'))
    if credentials_form.podcastindex_remove.data and credentials_form.validate_on_submit():
        set_setting(SETTING_KEY, '')
        set_setting(SETTING_SECRET, '')
        flash(_('Podcast Index credentials removed.'))
        return redirect(url_for('admin.admin_federation_discovery'))

    return render_template('admin/federation_discovery.html', title=_('Federation settings - discovery'),
                           credentials_form=credentials_form, preload_form=preload_form, candidates=candidates,
                           credentials_configured=_credentials_configured(),
                           roles_with=roles_with('change instance settings'))
```

Create `app/templates/admin/federation_discovery.html`:

```html
{% if theme() != 'piefed' and file_exists('app/templates/themes/' + theme() + '/base.html') -%}
    {% extends 'themes/' + theme() + '/base.html' -%}
{% else -%}
    {% extends "base.html" -%}
{% endif -%}
{% from 'bootstrap5/form.html' import render_form %}
{% set active_child = 'admin_federation' %}

{% block app_content %}
{% include 'admin/_tabbed_nav.html' %}
<br>
<div class="row">
    <div class="col">
        <h1>{{ _('Discovery: channels, podcasts and people') }}</h1>

        <h2 class="h4 mt-4">{{ _('Podcast Index credentials') }}</h2>
        <p id="podcastindex_status">{% if credentials_configured %}{{ _('Status: configured') }}{% else %}{{ _('Status: not set') }}{% endif %}</p>
        <p>{{ _('Castopod podcasts are found through the Podcast Index API. The key and secret are stored on this server and are never shown again; leave a field blank to keep what is stored.') }}</p>
        {{ render_form(credentials_form) }}

        {% if preload_form is not none %}
            {% include 'admin/_discovery_preload.html' %}
        {% endif %}

        <h2 class="h4 mt-4">{{ _('Data sources') }}</h2>
        <p class="discovery_attribution">
            {{ _('Directory data comes from') }}
            <a href="https://sepiasearch.org/" rel="noopener">SepiaSearch</a> (PeerTube),
            <a href="https://podcastindex.org/" rel="noopener">Podcast Index</a> (Castopod),
            <a href="https://joinmastodon.org/" rel="noopener">joinmastodon.org</a> {{ _("and each server's opt-in profile directory") }} (Mastodon),
            <a href="https://fedidb.org/" rel="noopener">FediDB</a> {{ _("and each server's opt-in directory") }} (Pixelfed).
            {{ _('None of them grants a bulk-use licence, so this instance samples them politely, once a day.') }}
        </p>
    </div>
</div>
<hr />
<div class="row">
    <div class="col">
        {% include 'admin/_nav.html' %}
    </div>
</div>
<hr />
{% endblock %}
```

(`admin/_discovery_preload.html` is created in Task 9; the `{% if preload_form is not none %}` guard keeps this page rendering until then.)

In `app/admin/__init__.py`, after the line `from app.admin import routes`, add:

```python
from app.discovery import admin_views as discovery_admin_views  # noqa: E402,F401  fork (interop D24): /admin/federation/discovery
```

In `app/templates/admin/federation_preload.html`, directly after the line `                {{ render_form(preload_form) }}`, add:

```html
                <p class="mt-3"><a href="{{ url_for('admin.admin_federation_discovery') }}">{{ _('Pre-load PeerTube channels and Castopod podcasts') }}</a></p>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./run_tests.sh tests/test_discovery_admin_credentials.py tests/test_admin_federation.py tests/test_mutating_get_routes.py tests/test_no_inline_imports.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/discovery/forms.py app/discovery/admin_views.py app/templates/admin/federation_discovery.html app/admin/__init__.py app/templates/admin/federation_preload.html tests/test_discovery_admin_credentials.py
git commit -m "$(cat <<'EOF'
feat: admin discovery page with write-only Podcast Index credentials and source attribution (interop D24)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Admin pre-load of PeerTube channels and Castopod podcasts

**Files:**
- Create: `app/discovery/preload.py`
- Create: `app/templates/admin/_discovery_preload.html`
- Modify: `app/discovery/forms.py` (add `MultiCheckboxField`, `DiscoveryPreloadForm`)
- Modify: `app/discovery/admin_views.py` (preview / subscribe handling)
- Test: `tests/test_discovery_preload.py`

**Interfaces:**
- Consumes: `DiscoveryEntry` (Task 1); `KIND_COMMUNITY` (Task 1); `admin_federation_discovery` and its template (Task 8); `app.activitypub.util.find_actor_or_create(actor, create_if_not_found=True, community_only=False, ...)`; `app.community.routes.do_subscribe(actor, user_id, admin_preload=False, joined_via_feed=False) -> dict` (Celery task, called synchronously inside ours); `app.utils.instance_banned(domain) -> bool`.
- Produces:
  - `app.discovery.preload.PRELOAD_PLATFORMS = ('peertube', 'castopod')`, `PRELOAD_USER_ID = 1`
  - `preload_candidates(count: int, platforms: list[str]) -> list[DiscoveryEntry]` (community entries of the chosen platforms, not NSFW, not on a banned instance, not already a `Community`, most-followed first, at most `count`)
  - Celery task `preload_discovered_communities(entry_ids: list[int], user_id: int) -> list[dict]`
  - `app.discovery.forms.DiscoveryPreloadForm` fields `preload_count` (1..200, default 25), `preload_platforms` (checkboxes), `preload_preview`, `preload_subscribe`.

Castopod entries resolve to a `Community` through `find_actor_or_create(..., community_only=True)` only once Task 12 lands; until then this task's tests replace the resolver, and PeerTube channels (plain `Group` actors) work unchanged.

- [ ] **Step 1: Write the failing test**

Create `tests/test_discovery_preload.py`:

```python
"""Interop D24, goal (a): an admin pre-loads PeerTube channels and Castopod podcasts the way the
lemmyverse pre-load does for Lemmy communities: preview, then subscribe through the join path."""
from types import SimpleNamespace

import pytest

from app import cache, db
from app.discovery import preload
from app.discovery.preload import preload_candidates, preload_discovered_communities
from app.models import Community, DiscoveryEntry, utcnow
from tests.factories import grant_permission, make_banned_instance, make_community, make_instance, make_user
from tests.test_admin_federation import csrf, login

pytestmark = pytest.mark.usefixtures('site')

PAGE = '/admin/federation/discovery'


@pytest.fixture(autouse=True)
def fresh_cache():
    cache.clear()
    yield
    cache.clear()


def add_entry(name, followers=1, platform='peertube', kind='community', nsfw=False, host=None, url=None):
    host = host or f'{name.lower()}.example'
    entry = DiscoveryEntry(kind=kind, platform=platform, actor_url=url or f'https://{host}/video-channels/{name.lower()}',
                           name=name, host=host, followers=followers, nsfw=nsfw, source='test',
                           first_seen=utcnow(), last_seen=utcnow())
    db.session.add(entry)
    db.session.commit()
    return entry


@pytest.fixture
def world(app, db_session):
    instance = make_instance('test.piefed.local', software='piefed')
    founder = make_user(instance, 'founder', local=True)
    add_entry('Bigchan', followers=900)
    add_entry('Midchan', followers=500)
    add_entry('Tinychan', followers=10)
    add_entry('Zqpodshow', followers=700, platform='castopod', url='https://pod.example/@pod', host='pod.example')
    add_entry('Ann', followers=999, platform='mastodon', kind='person', url='https://m.example/users/ann',
              host='m.example')
    add_entry('Spicy', followers=950, nsfw=True)
    known = add_entry('Known', followers=800, url='https://known.example/video-channels/Known')
    make_community('known', host='known.example')
    community = Community.query.filter_by(name='known').one()
    community.ap_profile_id = 'https://known.example/video-channels/known'   # stored lower-case, as ingest stores it
    db.session.commit()
    return SimpleNamespace(founder=founder, known=known)


def test_candidates_honour_n_and_the_platform_filter(world):
    assert [e.name for e in preload_candidates(2, ['peertube'])] == ['Bigchan', 'Midchan']
    assert [e.name for e in preload_candidates(10, ['castopod'])] == ['Zqpodshow']
    assert [e.name for e in preload_candidates(10, ['peertube', 'castopod'])] == ['Bigchan', 'Zqpodshow', 'Midchan', 'Tinychan']


def test_candidates_never_include_people_nsfw_known_or_unknown_platforms(world):
    names = [e.name for e in preload_candidates(50, ['peertube', 'castopod', 'mastodon'])]

    assert 'Ann' not in names and 'Spicy' not in names
    assert 'Known' not in names   # known case-insensitively: the entry says /Known, the community /known


def test_candidates_skip_a_host_banned_after_the_refresh(world):
    make_banned_instance('bigchan.example')

    assert 'Bigchan' not in [e.name for e in preload_candidates(10, ['peertube'])]


def test_subscribe_joins_each_new_community_once_and_skips_known_ones(world, monkeypatch):
    resolved, joined = [], []

    def resolve(actor_url, community_only=False):
        resolved.append((actor_url, community_only))
        name = actor_url.rstrip('/').rsplit('/', 1)[-1].lower()
        community = make_community(name, host=f'{name}.example')
        community.ap_id = f'{name}@{name}.example'
        db.session.commit()
        return community

    monkeypatch.setattr(preload, 'find_actor_or_create', resolve)
    monkeypatch.setattr(preload, 'do_subscribe', lambda actor, user_id, admin_preload=False:
                        joined.append((actor, user_id, admin_preload)) or {'community': actor, 'status': 'joined'})
    big = DiscoveryEntry.query.filter_by(name='Bigchan').one()

    results = preload_discovered_communities([big.id, world.known.id, 999999], world.founder.id)

    assert resolved == [(big.actor_url, True)]
    assert joined == [('bigchan@bigchan.example', world.founder.id, True)]
    assert [r['status'] for r in results] == ['joined', 'already known', 'gone']


def test_an_actor_that_does_not_resolve_to_a_community_is_reported(world, monkeypatch):
    monkeypatch.setattr(preload, 'find_actor_or_create', lambda actor_url, community_only=False: None)
    monkeypatch.setattr(preload, 'do_subscribe', lambda *a, **k: pytest.fail('nothing to join'))
    small = DiscoveryEntry.query.filter_by(name='Tinychan').one()

    assert preload_discovered_communities([small.id], world.founder.id) == [{'entry': small.id, 'status': 'not found'}]


@pytest.fixture
def admin(app, world):
    instance = make_instance('admin.example')
    user = make_user(instance, 'settingsadmin', local=True)
    user.verified = True
    db.session.commit()
    grant_permission(user, 'change instance settings')
    client = app.test_client()
    login(client, user)
    return client, csrf(app, client)


def test_the_preview_lists_the_candidates_and_subscribes_nobody(admin, monkeypatch):
    client, token = admin
    monkeypatch.setattr('app.discovery.admin_views.preload_discovered_communities',
                        SimpleNamespace(delay=lambda *a: pytest.fail('a preview must not subscribe')))

    page = client.post(PAGE, data={'preload_count': '2', 'preload_platforms': ['peertube'],
                                   'preload_preview': 'go', 'csrf_token': token}).get_data(as_text=True)

    assert '<td>Bigchan</td>' in page and '<td>Midchan</td>' in page
    assert 'Tinychan' not in page and 'Zqpodshow' not in page


def test_subscribe_hands_the_candidate_ids_to_the_celery_task(admin, monkeypatch):
    client, token = admin
    queued = []
    monkeypatch.setattr('app.discovery.admin_views.preload_discovered_communities',
                        SimpleNamespace(delay=lambda entry_ids, user_id: queued.append((entry_ids, user_id))))

    response = client.post(PAGE, data={'preload_count': '2', 'preload_platforms': ['peertube', 'castopod'],
                                       'preload_subscribe': 'go', 'csrf_token': token})

    big = DiscoveryEntry.query.filter_by(name='Bigchan').one()
    pod = DiscoveryEntry.query.filter_by(name='Zqpodshow').one()
    assert response.status_code == 302
    assert queued == [([big.id, pod.id], preload.PRELOAD_USER_ID)]


def test_a_count_outside_one_to_two_hundred_is_refused(admin, monkeypatch):
    client, token = admin
    monkeypatch.setattr('app.discovery.admin_views.preload_discovered_communities',
                        SimpleNamespace(delay=lambda *a: pytest.fail('invalid form')))

    response = client.post(PAGE, data={'preload_count': '0', 'preload_platforms': ['peertube'],
                                       'preload_subscribe': 'go', 'csrf_token': token})

    assert response.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_discovery_preload.py -v`
Expected: FAIL at collection with `ImportError: cannot import name 'preload' from 'app.discovery'`.

- [ ] **Step 3: Write `preload.py`, the form, the view handling and the partial**

Create `app/discovery/preload.py`:

```python
"""Pre-load PeerTube channels and Castopod podcasts (interop D24, goal (a)). Always a manual admin
action; runs as a Celery task; idempotent, because a community this instance already knows is skipped;
each subscription goes through the existing join path, do_subscribe(admin_preload=True)."""
from sqlalchemy import exists, func

from app import celery, db
from app.activitypub.util import find_actor_or_create
from app.community.routes import do_subscribe
from app.discovery import KIND_COMMUNITY
from app.models import Community, DiscoveryEntry
from app.utils import instance_banned

PRELOAD_PLATFORMS = ('peertube', 'castopod')
# Subscribe as user 1, the first instance admin, as the lemmyverse pre-load does (see admin_federation_preload)
PRELOAD_USER_ID = 1


def preload_candidates(count: int, platforms) -> list[DiscoveryEntry]:
    wanted = [platform for platform in (platforms or []) if platform in PRELOAD_PLATFORMS]
    if not wanted or count is None or count < 1:
        return []
    known = exists().where(Community.ap_profile_id == func.lower(DiscoveryEntry.actor_url))
    query = db.session.query(DiscoveryEntry).filter(DiscoveryEntry.kind == KIND_COMMUNITY,
                                                    DiscoveryEntry.platform.in_(wanted),
                                                    DiscoveryEntry.nsfw == False, ~known) \
        .order_by(DiscoveryEntry.followers.desc(), DiscoveryEntry.name)
    candidates = []
    for entry in query:
        if instance_banned(entry.host):
            continue
        candidates.append(entry)
        if len(candidates) >= count:
            break
    return candidates


@celery.task
def preload_discovered_communities(entry_ids, user_id):
    results = []
    for entry_id in entry_ids:
        entry = db.session.get(DiscoveryEntry, entry_id)
        if entry is None or entry.kind != KIND_COMMUNITY:
            results.append({'entry': entry_id, 'status': 'gone'})
            continue
        if db.session.query(Community.id).filter(Community.ap_profile_id == entry.actor_url.lower()).first():
            results.append({'entry': entry_id, 'status': 'already known'})
            continue
        community = find_actor_or_create(entry.actor_url, community_only=True)
        if not isinstance(community, Community):
            results.append({'entry': entry_id, 'status': 'not found'})
            continue
        results.append(do_subscribe(community.ap_id, user_id, admin_preload=True))
    return results
```

In `app/discovery/forms.py`, extend the imports to:

```python
from flask_babel import lazy_gettext as _l
from flask_wtf import FlaskForm
from wtforms import IntegerField, PasswordField, SelectMultipleField, SubmitField
from wtforms.validators import Length, NumberRange, Optional
from wtforms.widgets import CheckboxInput, ListWidget
```

and append:

```python
class MultiCheckboxField(SelectMultipleField):
    widget = ListWidget(prefix_label=False)
    option_widget = CheckboxInput()


class DiscoveryPreloadForm(FlaskForm):
    preload_count = IntegerField(_l('How many to subscribe to'), default=25,
                                 validators=[NumberRange(min=1, max=200)])
    preload_platforms = MultiCheckboxField(_l('Platforms'), default=['peertube', 'castopod'],
                                           choices=[('peertube', _l('PeerTube channels')),
                                                    ('castopod', _l('Castopod podcasts'))])
    preload_preview = SubmitField(_l('Preview'))
    preload_subscribe = SubmitField(_l('Subscribe'))
```

In `app/discovery/admin_views.py`, change the imports to:

```python
from flask import current_app, flash, redirect, url_for
from flask_babel import _, ngettext

from app.admin import bp
from app.discovery.castopod import SETTING_KEY, SETTING_SECRET
from app.discovery.forms import DiscoveryPreloadForm, PodcastIndexCredentialsForm
from app.discovery.preload import PRELOAD_USER_ID, preload_candidates, preload_discovered_communities
from app.utils import get_setting, login_required, permission_required, render_template, roles_with, set_setting
```

replace `    preload_form = None` with `    preload_form = DiscoveryPreloadForm()`, and directly before the final `return render_template(...)` add:

```python
    if (preload_form.preload_preview.data or preload_form.preload_subscribe.data) and preload_form.validate_on_submit():
        candidates = preload_candidates(preload_form.preload_count.data, preload_form.preload_platforms.data)
        if preload_form.preload_subscribe.data:
            entry_ids = [entry.id for entry in candidates]
            if current_app.debug:
                preload_discovered_communities(entry_ids, PRELOAD_USER_ID)
            else:
                preload_discovered_communities.delay(entry_ids, PRELOAD_USER_ID)
            flash(ngettext('Subscribing to %(num)d channel or podcast in the background.',
                           'Subscribing to %(num)d channels and podcasts in the background.',
                           len(entry_ids), num=len(entry_ids)))
            return redirect(url_for('admin.admin_federation_discovery'))
```

Create `app/templates/admin/_discovery_preload.html`:

```html
<h2 class="h4 mt-4">{{ _('Pre-load channels & podcasts') }}</h2>
<p>{{ _('Subscribe this instance to the most-followed PeerTube channels and Castopod podcasts in the discovery directory. NSFW entries, banned instances and communities this instance already knows are left out. Preview first; subscribing runs in the background.') }}</p>
{{ render_form(preload_form) }}
{% if candidates is not none %}
    {% if candidates %}
        <table class="table table-sm mt-3 discovery_preview">
            <thead><tr><th scope="col">{{ _('Name') }}</th><th scope="col">{{ _('Platform') }}</th><th scope="col">{{ _('Host') }}</th><th scope="col">{{ _('Followers') }}</th></tr></thead>
            <tbody>
            {% for entry in candidates %}
                <tr><td>{{ entry.name }}</td><td>{{ entry.platform }}</td><td>{{ entry.host }}</td><td>{{ entry.followers }}</td></tr>
            {% endfor %}
            </tbody>
        </table>
    {% else %}
        <p class="mt-3">{{ _('Nothing to pre-load. Run flask refresh_discovery, or choose another platform.') }}</p>
    {% endif %}
{% endif %}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./run_tests.sh tests/test_discovery_preload.py tests/test_discovery_admin_credentials.py tests/test_no_inline_imports.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/discovery/preload.py app/discovery/forms.py app/discovery/admin_views.py app/templates/admin/_discovery_preload.html tests/test_discovery_preload.py
git commit -m "$(cat <<'EOF'
feat: admin pre-load of PeerTube channels and Castopod podcasts through the join path (interop D24)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 10: Search fallback for communities and people, and the resolve button

**Files:**
- Create: `app/discovery/search.py`
- Create: `app/discovery/views.py`
- Create: `app/templates/discovery/_fallback.html`
- Modify: `app/main/__init__.py` (one module-level import after `from app.main import routes`)
- Modify: `app/main/routes.py` (`list_communities`, after `communities = communities.paginate(...)`, ~line 486; and `context.update`; two top-level imports)
- Modify: `app/instance/routes.py` (`instance_people`, after `people = people.paginate(...)`, ~line 166; its `render_template` call; one top-level import)
- Modify: `app/templates/list_communities.html` (the "No communities match your search." branch, ~line 388)
- Modify: `app/templates/instance/people.html` (the `{% else %}` of the people loop, ~line 82)
- Test: `tests/test_discovery_search.py`

**Interfaces:**
- Consumes: `DiscoveryEntry`, `KIND_COMMUNITY`, `KIND_PERSON` (Task 1); `app.activitypub.util.find_actor_or_create`; `app.utils.{instance_banned, login_required}`; `app.main.bp`.
- Produces:
  - `app.discovery.search.FALLBACK_LIMIT = 20`
  - `discovery_fallback(kind: str, q: str, allow_nsfw: bool, limit: int = FALLBACK_LIMIT) -> list[DiscoveryEntry]`
  - `viewer_allows_nsfw(user, site) -> bool`
  - Route `POST /discovery/<int:entry_id>/resolve`, endpoint `main.discovery_resolve`, view `app.discovery.views.discovery_resolve`: resolves through `find_actor_or_create(entry.actor_url, community_only=entry.kind == 'community')` and redirects to the local community or profile page, where the ordinary Join/Follow buttons are.
  - Template variable `discovered: list[DiscoveryEntry]` passed to `list_communities.html` and `instance/people.html`.

The community rule for NSFW is the page's own `nsfw` variable (forced to `'no'` when the site disables NSFW or the user hides it); people search has no such variable, so `viewer_allows_nsfw` applies the same rule: site allows NSFW, viewer logged in, `hide_nsfw != 1`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_discovery_search.py`:

```python
"""Interop D24, goal (b): when nothing local matches, community search and people search offer what the
discovery directory knows, with a platform badge and a button that resolves the actor."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import g

from app import cache, db
from app.discovery import views
from app.discovery.search import discovery_fallback, viewer_allows_nsfw
from app.models import DiscoveryEntry, Site, utcnow
from tests.factories import make_banned_instance, make_community, make_instance, make_user
from tests.test_admin_federation import csrf, login


@pytest.fixture(autouse=True)
def fresh_cache():
    cache.clear()
    yield
    cache.clear()


def add_entry(name, kind='community', platform='peertube', nsfw=False, host='tube.example', followers=1):
    path = 'video-channels' if kind == 'community' else 'users'
    entry = DiscoveryEntry(kind=kind, platform=platform, name=name, host=host, nsfw=nsfw, followers=followers,
                           actor_url=f'https://{host}/{path}/{name.lower().replace(" ", "_").replace("%", "pct")}',
                           source='test', first_seen=utcnow(), last_seen=utcnow())
    db.session.add(entry)
    db.session.commit()
    return entry


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    user = api_baseline.user1
    user.verified = True
    db.session.commit()
    client = app.test_client()
    login(client, user)
    return SimpleNamespace(app=app, client=client, user=user, token=csrf(app, client))


def test_the_fallback_matches_names_case_insensitively_and_by_kind(app, db_session):
    add_entry('ZqTilvids Linux')
    add_entry('Ann Zqtilvids', kind='person', platform='mastodon', host='m.example')

    assert [e.name for e in discovery_fallback('community', 'zqtilvids', False)] == ['ZqTilvids Linux']
    assert [e.name for e in discovery_fallback('person', 'ZQTILVIDS', False)] == ['Ann Zqtilvids']
    assert discovery_fallback('community', '   ', True) == []


def test_nsfw_entries_need_the_viewers_permission(app, db_session):
    add_entry('Zqspicy', nsfw=True)

    assert discovery_fallback('community', 'zqspicy', False) == []
    assert [e.name for e in discovery_fallback('community', 'zqspicy', True)] == ['Zqspicy']


def test_entries_on_a_host_banned_since_the_refresh_are_hidden(app, db_session):
    add_entry('Zqbanned', host='banned.example')
    make_banned_instance('banned.example')

    assert discovery_fallback('community', 'zqbanned', True) == []


def test_like_wildcards_in_the_search_text_match_literally(app, db_session):
    """Review focus 3."""
    add_entry('100% Linux')
    add_entry('1000 Linux')
    add_entry('a_b')
    add_entry('axb')

    assert [e.name for e in discovery_fallback('community', '100%', True)] == ['100% Linux']
    assert [e.name for e in discovery_fallback('community', 'a_b', True)] == ['a_b']


def test_viewer_allows_nsfw(app, db_session):
    site = SimpleNamespace(enable_nsfw=True)
    anonymous = SimpleNamespace(is_authenticated=False)
    shown = SimpleNamespace(is_authenticated=True, hide_nsfw=0)
    hidden = SimpleNamespace(is_authenticated=True, hide_nsfw=1)

    assert viewer_allows_nsfw(shown, site) is True
    assert viewer_allows_nsfw(hidden, site) is False
    assert viewer_allows_nsfw(anonymous, site) is False
    assert viewer_allows_nsfw(shown, SimpleNamespace(enable_nsfw=False)) is False


def test_the_communities_page_offers_the_directory_when_nothing_local_matches(env):
    entry = add_entry('Zqtilvids Linux')

    html = env.client.get('/communities?search=zqtilvids').get_data(as_text=True)

    assert 'Zqtilvids Linux' in html
    assert 'PeerTube' in html
    assert f'/discovery/{entry.id}/resolve' in html


def test_a_local_match_means_no_fallback(env):
    add_entry('Zqtilvids Linux')
    community = make_community('zqtilvids')
    community.title = 'Zqtilvids'
    db.session.commit()

    with patch('app.main.routes.render_template', return_value='rendered') as render:
        env.client.get('/communities?search=zqtilvids')

    assert render.call_args.kwargs['discovered'] == []


def test_nsfw_entries_are_hidden_on_the_communities_page_by_default(env):
    add_entry('Zqspicy', nsfw=True)

    with patch('app.main.routes.render_template', return_value='rendered') as render:
        env.client.get('/communities?search=zqspicy')

    assert render.call_args.kwargs['discovered'] == []


def test_people_search_offers_directory_people(env):
    add_entry('Zqann Example', kind='person', platform='mastodon', host='m.example')

    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        env.client.get('/instance/all/people?q=zqann')

    assert [e.name for e in render.call_args.kwargs['discovered']] == ['Zqann Example']


def test_the_people_page_renders_a_follow_button_for_a_directory_person(env):
    entry = add_entry('Zqann Example', kind='person', platform='pixelfed', host='px.example')

    html = env.client.get('/instance/all/people?q=zqann').get_data(as_text=True)

    assert 'Zqann Example' in html and 'Pixelfed' in html
    assert f'/discovery/{entry.id}/resolve' in html


def test_resolve_finds_the_community_and_redirects_to_it(env, monkeypatch):
    entry = add_entry('Zqtilvids Linux')
    community = make_community('zqtilvids_linux', host='tube.example')
    community.ap_id = 'zqtilvids_linux@tube.example'
    db.session.commit()
    calls = []
    monkeypatch.setattr(views, 'find_actor_or_create',
                        lambda actor_url, community_only=False: calls.append((actor_url, community_only)) or community)

    response = env.client.post(f'/discovery/{entry.id}/resolve', data={'csrf_token': env.token})

    assert response.status_code == 302
    assert response.headers['Location'].endswith(f'/c/{community.link()}')
    assert calls == [(entry.actor_url, True)]


def test_resolve_finds_a_person_and_redirects_to_their_profile(env, monkeypatch):
    entry = add_entry('Zqann Example', kind='person', platform='mastodon', host='m.example')
    person = make_user(make_instance('m.example'), 'zqann')
    monkeypatch.setattr(views, 'find_actor_or_create', lambda actor_url, community_only=False: person)

    response = env.client.post(f'/discovery/{entry.id}/resolve', data={'csrf_token': env.token})

    assert response.headers['Location'].endswith(f'/u/{person.link()}')


def test_an_actor_that_cannot_be_reached_sends_the_viewer_back(env, monkeypatch):
    entry = add_entry('Zqgone')
    monkeypatch.setattr(views, 'find_actor_or_create', lambda actor_url, community_only=False: None)

    response = env.client.post(f'/discovery/{entry.id}/resolve', data={'csrf_token': env.token})

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/communities')


def test_resolving_needs_a_login(app, api_baseline, monkeypatch):
    entry = add_entry('Zqtilvids Linux')
    monkeypatch.setattr(views, 'find_actor_or_create', lambda *a, **k: pytest.fail('anonymous must not resolve'))

    response = app.test_client().post(f'/discovery/{entry.id}/resolve')

    assert response.status_code in (302, 401)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_discovery_search.py -v`
Expected: FAIL at collection with `ImportError: cannot import name 'views' from 'app.discovery'`.

- [ ] **Step 3: Write the fallback, the view, the partial and the seams**

Create `app/discovery/search.py`:

```python
"""The discovery fallback for community and people search (interop D24, goal (b)): used only when
nothing local matches. NSFW entries need the viewer's permission; a host banned since the last refresh
is hidden."""
from sqlalchemy import or_

from app import db
from app.models import DiscoveryEntry
from app.utils import instance_banned

FALLBACK_LIMIT = 20


def _contains_pattern(q: str) -> str:
    escaped = q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    return f'%{escaped}%'


def discovery_fallback(kind: str, q: str, allow_nsfw: bool, limit: int = FALLBACK_LIMIT) -> list:
    q = (q or '').strip()
    if not q:
        return []
    pattern = _contains_pattern(q)
    query = db.session.query(DiscoveryEntry).filter(
        DiscoveryEntry.kind == kind,
        or_(DiscoveryEntry.name.ilike(pattern, escape='\\'), DiscoveryEntry.actor_url.ilike(pattern, escape='\\')))
    if not allow_nsfw:
        query = query.filter(DiscoveryEntry.nsfw == False)
    found = []
    for entry in query.order_by(DiscoveryEntry.followers.desc(), DiscoveryEntry.name).limit(limit * 2):
        if instance_banned(entry.host):
            continue
        found.append(entry)
        if len(found) >= limit:
            break
    return found


def viewer_allows_nsfw(user, site) -> bool:
    """The community-search rule, for a page that has no NSFW selector of its own."""
    return bool(site is not None and getattr(site, 'enable_nsfw', False) and user is not None
                and user.is_authenticated and getattr(user, 'hide_nsfw', 1) != 1)
```

Create `app/discovery/views.py`:

```python
"""Discovery routes on the main blueprint (interop D24)."""
from flask import abort, flash, redirect, url_for
from flask_babel import _

from app import db
from app.activitypub.util import find_actor_or_create
from app.discovery import KIND_COMMUNITY
from app.main import bp
from app.models import Community, DiscoveryEntry, User
from app.utils import login_required


@bp.route('/discovery/<int:entry_id>/resolve', methods=['POST'])
@login_required
def discovery_resolve(entry_id):
    """Fetch a directory entry's actor (find_actor_or_create, with its usual guards) and open its page here,
    where the ordinary Join or Follow button is. This is the first time anything is fetched from it."""
    entry = db.session.get(DiscoveryEntry, entry_id)
    if entry is None:
        abort(404)
    actor = find_actor_or_create(entry.actor_url, community_only=entry.kind == KIND_COMMUNITY)
    if isinstance(actor, Community):
        return redirect(url_for('activitypub.community_profile', actor=actor.link()))
    if isinstance(actor, User):
        return redirect(url_for('activitypub.user_profile', actor=actor.link()))
    flash(_('%(name)s could not be reached. Please try again later.', name=entry.name), 'warning')
    if entry.kind == KIND_COMMUNITY:
        return redirect(url_for('main.list_communities'))
    return redirect(url_for('instance.instance_people', instance_domain='all'))
```

Create `app/templates/discovery/_fallback.html`:

```html
{% if discovered %}
<section class="discovery_fallback mt-4" aria-labelledby="discovery_fallback_heading">
    <h2 id="discovery_fallback_heading" class="h5">{{ _('Elsewhere in the fediverse') }}</h2>
    <p class="text-muted small">{{ _('Not on this server yet. Opening one fetches it from its home server.') }}</p>
    <ul class="list-unstyled">
    {% for entry in discovered %}
        <li class="d-flex align-items-center gap-2 mb-2 discovery_entry">
            <span class="badge text-bg-secondary platform_badge">{{ {'peertube': 'PeerTube', 'castopod': 'Castopod', 'mastodon': 'Mastodon', 'pixelfed': 'Pixelfed'}.get(entry.platform, entry.platform) }}</span>
            <span class="fw-semibold text-break">{{ entry.name }}</span>
            <small class="text-muted">{{ entry.host }}</small>
            <form method="post" action="{{ url_for('main.discovery_resolve', entry_id=entry.id) }}" class="ms-auto">
                <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                <button type="submit" class="btn btn-sm btn-primary">{% if entry.kind == 'community' %}{{ _('Open to join') }}{% else %}{{ _('Open to follow') }}{% endif %}</button>
            </form>
        </li>
    {% endfor %}
    </ul>
</section>
{% endif %}
```

In `app/main/__init__.py`, after `from app.main import routes`, add:

```python
from app.discovery import views as discovery_views  # noqa: E402,F401  fork (interop D24): /discovery/<id>/resolve
```

In `app/main/routes.py`, add top-level imports:

```python
from app.discovery import KIND_COMMUNITY
from app.discovery.search import discovery_fallback
```

In `list_communities`, directly after the `communities = communities.paginate(page=page, ... error_out=False)` statement, add:

```python
    # Interop D24: nothing local matched the search, so offer what the discovery directory knows
    discovered = discovery_fallback(KIND_COMMUNITY, search_param, allow_nsfw=nsfw != 'no') \
        if search_param and page == 1 and communities.total == 0 else []
```

and add `"discovered": discovered,` to the `context.update({...})` dict (after `"is_admin": is_admin,`).

In `app/instance/routes.py`, add a top-level import:

```python
from app.discovery import KIND_PERSON
from app.discovery.search import discovery_fallback, viewer_allows_nsfw
```

In `instance_people`, directly after the `people = people.paginate(...)` statement, add:

```python
    # Interop D24: nobody here matched the search, so offer opt-in directory people
    discovered = discovery_fallback(KIND_PERSON, search, allow_nsfw=viewer_allows_nsfw(current_user, g.site)) \
        if search and page == 1 and people.total == 0 else []
```

and pass `discovered=discovered,` to that function's `render_template('instance/people.html', ...)` call (after `q=search,`).

In `app/templates/list_communities.html`, replace:

```html
            <p class="mt-4">
                {{ _('No communities match your search.') }}
            </p>
```

with:

```html
            <p class="mt-4">
                {{ _('No communities match your search.') }}
            </p>
            {% include 'discovery/_fallback.html' %}
```

In `app/templates/instance/people.html`, replace:

```html
                        <p><a href="/instance/add_people" class="btn btn-primary">{{ _('Add remote people') }}</a></p>
                    </div>
```

with:

```html
                        <p><a href="/instance/add_people" class="btn btn-primary">{{ _('Add remote people') }}</a></p>
                        {% include 'discovery/_fallback.html' %}
                    </div>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./run_tests.sh tests/test_discovery_search.py tests/test_instance_routes.py tests/test_query_string_integers.py tests/test_mutating_get_routes.py tests/test_no_inline_imports.py tests/test_anchored_validators.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/discovery/search.py app/discovery/views.py app/templates/discovery/_fallback.html app/main/__init__.py app/main/routes.py app/instance/routes.py app/templates/list_communities.html app/templates/instance/people.html tests/test_discovery_search.py
git commit -m "$(cat <<'EOF'
feat: community and people search fall back to the discovery directory (interop D24)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 11: A Castopod `Podcast` actor also gets its `Community` row

**Files:**
- Create: `app/discovery/podcast.py`
- Modify: `app/activitypub/util.py` (top-level import; `actor_json_to_model` Podcast branch at ~line 1392 and ~line 1494; `refresh_user_profile_task` at ~line 865)
- Test: `tests/test_podcast_community.py`

**Interfaces:**
- Consumes: `app.models.{Community, Instance, Site, User, utcnow}`.
- Produces:
  - `app.discovery.podcast.ensure_podcast_community(user: User, actor_json: dict) -> Community | None` — finds or creates the `Community` sharing the podcast user's `ap_profile_id`, `ap_id`, inbox, followers url, key and instance; `user_id` = the podcast user; `rss_url` = the actor's https `rssFeed` (kept current); `None` for a local user, or for a `sensitive` podcast on a site with NSFW off.
  - `app.discovery.podcast.podcast_community_for(user) -> Community | None` — the non-banned `Community` with the remote user's `ap_profile_id`.
  - `Community.rss_url` (an existing, unused column) now holds a podcast's feed url; Task 15 reads it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_podcast_community.py`:

```python
"""Interop D24 (spec D4): a Castopod Podcast actor is a User, because it authors the episodes, and a
Community, because it is what a reader subscribes to. Both rows carry the same ActivityPub id."""
from app.activitypub.util import actor_json_to_model, refresh_user_profile_task
from app.discovery.podcast import ensure_podcast_community, podcast_community_for
from app.models import Community, User
from tests.factories import make_site, make_user, peer_actor_json, peer_instance

PEER = 'peer.example'
ACTOR = f'https://{PEER}/u/mypodcast'
FEED = f'https://{PEER}/@mypodcast/feed.xml'


def podcast_document(**fields):
    values = {'type': 'Podcast', 'name': 'My Podcast', 'rssFeed': FEED, 'inbox': f'{ACTOR}/inbox',
              'outbox': f'{ACTOR}/outbox', 'followers': f'{ACTOR}/followers'}
    values.update(fields)
    return peer_actor_json('Person', name='mypodcast', server=PEER, fields=values)


def test_a_podcast_actor_gets_a_user_and_a_community_with_one_id(app, db_session):
    peer_instance(PEER)

    user = actor_json_to_model(podcast_document(), 'mypodcast', PEER)

    community = Community.query.one()
    assert isinstance(user, User)
    assert community.ap_profile_id == user.ap_profile_id == ACTOR
    assert community.ap_id == user.ap_id
    assert community.user_id == user.id and community.instance_id == user.instance_id
    assert community.title == 'My Podcast'
    assert community.rss_url == FEED
    assert community.ap_outbox_url == f'{ACTOR}/outbox'
    assert community.ap_followers_url == f'{ACTOR}/followers'
    assert community.ap_fetched_at is not None
    assert podcast_community_for(user).id == community.id


def test_a_person_gets_no_community(app, db_session):
    peer_instance(PEER)

    user = actor_json_to_model(peer_actor_json('Person', name='alice', server=PEER), 'alice', PEER)

    assert Community.query.count() == 0
    assert podcast_community_for(user) is None


def test_seeing_the_podcast_again_does_not_duplicate_and_keeps_the_feed_current(app, db_session):
    peer_instance(PEER)
    actor_json_to_model(podcast_document(), 'mypodcast', PEER)

    actor_json_to_model(podcast_document(rssFeed=f'https://{PEER}/@mypodcast/feed2.xml'), 'mypodcast', PEER)

    assert Community.query.one().rss_url == f'https://{PEER}/@mypodcast/feed2.xml'
    assert User.query.count() == 1


def test_a_feed_that_is_not_https_is_not_kept(app, db_session):
    peer_instance(PEER)

    actor_json_to_model(podcast_document(rssFeed=f'http://{PEER}/feed.xml'), 'mypodcast', PEER)

    assert Community.query.one().rss_url is None


def test_a_sensitive_podcast_on_a_site_without_nsfw_stays_a_user_only(app, db_session):
    make_site()   # enable_nsfw defaults to False
    peer_instance(PEER)

    user = actor_json_to_model(podcast_document(sensitive=True), 'mypodcast', PEER)

    assert user is not None
    assert Community.query.count() == 0


def test_a_podcast_known_from_before_gets_its_community_on_refresh(app, db_session):
    user = make_user(peer_instance(PEER), 'mypodcast')

    refresh_user_profile_task(user.id, podcast_document(id=user.ap_profile_id))

    assert Community.query.one().ap_profile_id == user.ap_profile_id


def test_a_local_user_never_gets_a_podcast_community(app, db_session):
    local = make_user(peer_instance('local.example'), 'me', local=True)

    assert ensure_podcast_community(local, {'type': 'Podcast'}) is None
    assert podcast_community_for(local) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_podcast_community.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'app.discovery.podcast'`.

- [ ] **Step 3: Write `podcast.py` and the three seams**

Create `app/discovery/podcast.py`:

```python
"""Castopod's Podcast actor as a community (interop D24, spec D4 and decision 5).

A podcast actor is a person-like author (G1: it gets a User row, the technical owner, so Post.user_id and
the per-author machinery keep working) and the thing a reader subscribes to (a Community row). Both rows
carry the same ap_profile_id; that column is unique per table, not across tables. Imports only models,
so app/activitypub/* can import this at module top without a cycle.
"""
from urllib.parse import urlparse

from flask import current_app
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import Community, Instance, Site, User, utcnow


def _https_url(value, limit: int):
    if isinstance(value, str) and len(value) <= limit and urlparse(value).scheme == 'https' \
            and urlparse(value).hostname:
        return value
    return None


def podcast_community_for(user) -> Community | None:
    if user is None or user.is_local() or not user.ap_profile_id:
        return None
    return db.session.query(Community).filter(Community.ap_profile_id == user.ap_profile_id,
                                              Community.banned == False).first()


def ensure_podcast_community(user: User, actor_json: dict) -> Community | None:
    if user is None or user.is_local() or not user.ap_profile_id or not isinstance(actor_json, dict):
        return None
    rss_url = _https_url(actor_json.get('rssFeed'), 2048)
    community = db.session.query(Community).filter(Community.ap_profile_id == user.ap_profile_id).first()
    if community is not None:
        if rss_url and community.rss_url != rss_url:
            community.rss_url = rss_url
            db.session.commit()
        return community
    sensitive = actor_json.get('sensitive') is True
    if sensitive:
        site = db.session.get(Site, 1)   # not g.site: this runs from Celery too
        if site is None or not site.enable_nsfw:
            return None
    instance = db.session.get(Instance, user.instance_id) if user.instance_id else None
    community = Community(name=user.user_name, title=user.title or user.user_name,
                          description=user.about or '', description_html=user.about_html or '',
                          nsfw=sensitive, user_id=user.id, instance_id=user.instance_id,
                          ap_id=user.ap_id, ap_profile_id=user.ap_profile_id, ap_public_url=user.ap_public_url,
                          ap_followers_url=user.ap_followers_url, ap_inbox_url=user.ap_inbox_url,
                          ap_outbox_url=_https_url(actor_json.get('outbox'), 255),
                          ap_preferred_username=user.ap_preferred_username, ap_domain=user.ap_domain,
                          public_key=user.public_key, rss_url=rss_url, ap_fetched_at=utcnow(),
                          created_at=user.created or utcnow(), last_active=utcnow(), first_federated_at=utcnow(),
                          content_retention=current_app.config['DEFAULT_CONTENT_RETENTION'],
                          default_post_type='link', subscriptions_count=0,
                          show_popular=instance.popular if instance else True,
                          show_all=not instance.silenced if instance else True)
    db.session.add(community)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return db.session.query(Community).filter(Community.ap_profile_id == user.ap_profile_id).first()
    return community
```

In `app/activitypub/util.py`, add to the top-level imports (after `from app.visibility import OPEN_VISIBILITIES, can_view, post_title_for`):

```python
from app.discovery.podcast import ensure_podcast_community
```

In `actor_json_to_model`, replace:

```python
    if activity_json['type'] in ('Person', 'Service', 'Podcast'):
        user = db.session.query(User).filter(User.ap_profile_id == activity_json['id'].lower()).first()
        if user:
            return user
```

with:

```python
    if activity_json['type'] in ('Person', 'Service', 'Podcast'):
        user = db.session.query(User).filter(User.ap_profile_id == activity_json['id'].lower()).first()
        if user:
            if activity_json['type'] == 'Podcast':
                ensure_podcast_community(user, activity_json)  # D24: a podcast is also a community
            return user
```

and, at the end of the same branch, replace:

```python
        if user.cover_id and get_setting('cache_remote_images_locally', True):
            make_image_sizes(user.cover_id, 878, None, 'users')
        return user
    elif activity_json['type'] == 'Group':
```

with:

```python
        if user.cover_id and get_setting('cache_remote_images_locally', True):
            make_image_sizes(user.cover_id, 878, None, 'users')
        if activity_json['type'] == 'Podcast':
            ensure_podcast_community(user, activity_json)  # D24: a podcast is also a community
        return user
    elif activity_json['type'] == 'Group':
```

In `refresh_user_profile_task`, replace:

```python
                                cover_changed = True

                    session.commit()
                    if user.avatar_id and avatar_changed and get_setting('cache_remote_images_locally', True):
```

with:

```python
                                cover_changed = True

                    session.commit()
                    if activity_json.get('type') == 'Podcast':
                        ensure_podcast_community(user, activity_json)  # D24: a podcast known from before G1/D24
                    if user.avatar_id and avatar_changed and get_setting('cache_remote_images_locally', True):
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./run_tests.sh tests/test_podcast_community.py tests/test_podcast_actor.py tests/test_podcast_episode_audio.py tests/test_no_inline_imports.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/discovery/podcast.py app/activitypub/util.py tests/test_podcast_community.py
git commit -m "$(cat <<'EOF'
feat: a Castopod Podcast actor gets a Community row beside its User (interop D24)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 12: Actor lookups resolve the right row for a dual actor (Accept, Undo, community hints)

**Files:**
- Modify: `app/activitypub/actor.py` (`find_actor_by_url` remote branch, ~line 345; `create_actor_from_remote`, ~line 303)
- Test: `tests/test_podcast_dual_actor_lookups.py`

**Interfaces:**
- Consumes: `ensure_podcast_community` via `actor_json_to_model` (Task 11); existing `find_local_community(actor_url) -> Community` in `actor.py`.
- Produces: `find_actor_by_url(url, community_only=True)` and `create_actor_from_remote(url, community_only=True)` answer the `Community` twin of a Podcast actor; hint-less lookups keep answering the `User`. Consumed by the inbox Accept preamble, Task 9's pre-load and Task 10's resolve button.

Audit of the lookups that can meet a podcast's id without a type hint (recorded in the test module docstring below):

| Call site | Hint | Resolves to | Status |
|---|---|---|---|
| `routes.py` `process_inbox_request` preamble for Announce/Accept/Reject | `community_only=True` first | Community | fixed here (was None: `find_remote_actor` answered the User) |
| `routes.py` `process_inbox_request` preamble for every other type | none | User (the podcast as author) | correct, pinned by the Undo test |
| `routes.py` Accept{Follow} body `requestor_user` | none | the local follower | never the podcast |
| `routes.py` Undo{Follow} `target` | none | the local user or community being unfollowed | a podcast is never the target of an Undo delivered to us |
| `routes.py` HTTP-signature key lookup `find_actor_by_url(key_id)` | none | User | both rows hold the same key |
| `routes.py` Add/Remove/Move `community_to_*`, `origin/target_community` | `community_only=True` | Community | fixed here |
| `community/routes.py` subscribe / unsubscribe | `Community.ap_id` query | Community | already right |
| `discovery/preload.py`, `discovery/views.py` | `community_only=True` | Community | fixed here |

- [ ] **Step 1: Write the failing test**

Create `tests/test_podcast_dual_actor_lookups.py`:

```python
"""Interop D24: a Castopod podcast is a User and a Community with one id. A lookup with no type hint
answers the User (the author); a community lookup answers the Community.

Audit of hint-less lookups that can meet a podcast id (app/activitypub/routes.py unless noted):
  Announce/Accept/Reject preamble -- community_only=True first -> Community (fixed by D24)
  every other activity's preamble -- unhinted -> User, the podcast as author -- correct
  Accept{Follow} requestor -- the local follower, never the podcast
  Undo{Follow} target -- the local user/community unfollowed; a podcast is never an Undo's target here
  HTTP-signature key lookup -- User; both rows carry the same key
  Add/Remove/Move community lookups -- community_only=True -> Community (fixed by D24)
  community/routes.py subscribe/unsubscribe -- Community.ap_id -> Community
  discovery/preload.py, discovery/views.py -- community_only=True -> Community (fixed by D24)
"""
from types import SimpleNamespace

import pytest

from app import cache, db
from app.activitypub.actor import find_actor_by_url
from app.activitypub.routes import process_inbox_request
from app.activitypub.util import actor_json_to_model, find_actor_or_create
from app.models import Community, CommunityMember, User, UserFollower
from tests.factories import make_community_join_request, make_follow, make_instance, make_site, make_user, \
    peer_actor_json, peer_instance

PEER = 'peer.example'
PODCAST = f'https://{PEER}/u/mypodcast'


@pytest.fixture(autouse=True)
def fresh_cache():
    cache.clear()  # find_actor_or_create_cached memoizes (id, class) per url and hint
    yield
    cache.clear()


def podcast_document():
    return peer_actor_json('Person', name='mypodcast', server=PEER,
                           fields={'type': 'Podcast', 'name': 'My Podcast', 'inbox': f'{PODCAST}/inbox',
                                   'outbox': f'{PODCAST}/outbox', 'followers': f'{PODCAST}/followers'})


@pytest.fixture
def podcast(app, db_session):
    make_site()
    local = make_instance(app.config['SERVER_NAME'], software='piefed')
    peer_instance(PEER)
    user = actor_json_to_model(podcast_document(), 'mypodcast', PEER)
    return SimpleNamespace(local=local, user=user, community=Community.query.filter_by(ap_profile_id=PODCAST).one())


def local_user(app, instance, name):
    user = make_user(instance, name, local=True)
    user.ap_profile_id = f"https://{app.config['SERVER_NAME']}/u/{name}"
    db.session.commit()
    return user


def test_an_unhinted_lookup_finds_the_user(podcast):
    assert find_actor_by_url(PODCAST) == podcast.user
    assert find_actor_or_create(PODCAST, create_if_not_found=False) == podcast.user


def test_a_community_lookup_finds_the_community(podcast):
    assert find_actor_by_url(PODCAST, community_only=True) == podcast.community
    assert find_actor_or_create(PODCAST, community_only=True, create_if_not_found=False) == podcast.community


def test_a_community_lookup_of_an_unknown_podcast_creates_both_and_answers_the_community(app, db_session, http_mock):
    make_site()
    peer_instance(PEER)
    http_mock.get(PODCAST).respond(json=podcast_document())

    found = find_actor_or_create(PODCAST, community_only=True)

    assert isinstance(found, Community) and found.ap_profile_id == PODCAST
    assert User.query.filter_by(ap_profile_id=PODCAST).count() == 1


def test_an_accept_from_the_podcast_admits_the_join_request(app, podcast):
    joiner = local_user(app, podcast.local, 'joiner')
    make_community_join_request(joiner, podcast.community)

    process_inbox_request({'id': f'{PODCAST}/activities/accept/1', 'type': 'Accept', 'actor': PODCAST,
                           'object': {'id': f"https://{app.config['SERVER_NAME']}/activities/follow/1",
                                      'type': 'Follow', 'actor': joiner.ap_profile_id, 'object': PODCAST}}, True)

    assert CommunityMember.query.filter_by(user_id=joiner.id, community_id=podcast.community.id).count() == 1


def test_an_undo_follow_sent_by_the_podcast_is_the_users(app, podcast):
    followed = local_user(app, podcast.local, 'followed')
    make_follow(followed, podcast.user, is_accepted=True, is_inward=True)

    process_inbox_request({'id': f'{PODCAST}/activities/undo/1', 'type': 'Undo', 'actor': PODCAST,
                           'object': {'id': f'{PODCAST}/activities/follow/1', 'type': 'Follow', 'actor': PODCAST,
                                      'object': followed.ap_profile_id}}, True)

    assert UserFollower.query.count() == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_podcast_dual_actor_lookups.py -v`
Expected: `test_a_community_lookup_finds_the_community`, `test_a_community_lookup_of_an_unknown_podcast_creates_both_and_answers_the_community` and `test_an_accept_from_the_podcast_admits_the_join_request` FAIL (the community lookup answers `None`); the unhinted and Undo tests PASS (they pin existing behaviour).

- [ ] **Step 3: Add the two community hints**

In `app/activitypub/actor.py`, in `find_actor_by_url`, replace:

```python
    if actor_url.startswith('https://') or actor_url.startswith('http://'):
        actor = find_remote_actor(actor_url)
```

with:

```python
    if actor_url.startswith('https://') or actor_url.startswith('http://'):
        # D24: a Castopod podcast is a User and a Community with one id; a community lookup wants the Community
        actor = (find_local_community(actor_url) if community_only else None) or find_remote_actor(actor_url)
```

In `create_actor_from_remote`, replace:

```python
        actor_model = actor_json_to_model(actor_json, address, server)

        if community_only and not isinstance(actor_model, Community):
```

with:

```python
        actor_model = actor_json_to_model(actor_json, address, server)
        if community_only and isinstance(actor_model, User):
            actor_model = find_local_community(actor_model.ap_profile_id)  # D24: a podcast's Community twin

        if community_only and not isinstance(actor_model, Community):
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./run_tests.sh tests/test_podcast_dual_actor_lookups.py tests/test_inbox_dispatch_accept_reject.py tests/test_inbox_dispatch_preamble.py tests/test_podcast_actor.py tests/test_no_inline_imports.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/actor.py tests/test_podcast_dual_actor_lookups.py
git commit -m "$(cat <<'EOF'
fix: community lookups resolve a Castopod podcast's Community, unhinted ones its User (interop D24)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 13: Episodes from a podcast land in its community

**Files:**
- Modify: `app/activitypub/routes.py` (top-level import; `process_new_content`, ~line 2608)
- Test: `tests/test_podcast_episode_routing.py`

**Interfaces:**
- Consumes: `podcast_community_for(user) -> Community | None` (Task 11).
- Produces: `process_new_content(user, None, ...)` files a Podcast actor's Note in its own community; everyone else's community-less Note still goes to `microblogs`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_podcast_episode_routing.py`:

```python
"""Interop D24 (spec D4): a Castopod episode Note is a post in the podcast's community, not in
`microblogs`; the episode audio (WP-C, C1) keeps arriving."""
from types import SimpleNamespace

import pytest

from app.activitypub.routes import process_new_content
from app.activitypub.util import actor_json_to_model
from app.discovery.podcast import podcast_community_for
from app.models import Post
from tests.factories import make_site, peer_actor_json, peer_instance, seed_community_owner

PEER = 'peer.example'
PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'
EPISODE = f'https://{PEER}/@mypodcast/episodes/ep-1'
AUDIO = f'https://{PEER}/media/ep1.mp3'
COVER = f'https://{PEER}/media/ep1.jpg'
ANNOUNCEMENT = f'<a href="{EPISODE}">Episode 1: Hello</a><br/><p>New episode is out!</p>'


@pytest.fixture
def world(app, db_session):
    seed_community_owner('local.example')   # instance 1 and user 1, which the microblogs community needs
    make_site()
    peer_instance(PEER)
    # no rssFeed: credits (a later task) fetch nothing here
    podcast = actor_json_to_model(peer_actor_json('Person', name='mypodcast', server=PEER,
                                                  fields={'type': 'Podcast', 'name': 'My Podcast'}),
                                  'mypodcast', PEER)
    alice = actor_json_to_model(peer_actor_json('Person', name='alice', server=PEER), 'alice', PEER)
    return SimpleNamespace(podcast=podcast, alice=alice)


def create(author, note_id, content='<p>new episode</p>'):
    return {'id': f'{note_id}/activity', 'type': 'Create', 'actor': author.ap_profile_id, 'to': [PUBLIC], 'cc': [],
            'object': {'id': note_id, 'type': 'Note', 'content': content, 'attributedTo': author.ap_profile_id,
                       'to': [PUBLIC], 'cc': []}}


def test_an_episode_from_a_podcast_lands_in_its_community(world):
    process_new_content(world.podcast, None, False, create(world.podcast, f'https://{PEER}/n/1'), False)

    post = Post.query.one()
    assert post.community_id == podcast_community_for(world.podcast).id
    assert post.user_id == world.podcast.id


def test_a_note_from_a_person_still_goes_to_microblogs(world):
    process_new_content(world.alice, None, False, create(world.alice, f'https://{PEER}/n/2'), False)

    assert Post.query.one().community.name == 'microblogs'


def test_the_episode_audio_still_arrives(world, http_mock):
    http_mock.get(EPISODE).respond(json={
        'id': EPISODE, 'type': 'PodcastEpisode', 'attributedTo': world.podcast.ap_profile_id,
        'image': {'type': 'Image', 'mediaType': 'image/jpeg', 'url': COVER},
        'audio': {'id': AUDIO, 'type': 'Audio', 'url': {'href': AUDIO, 'type': 'Link', 'mediaType': 'audio/mpeg'}}})
    http_mock.get(COVER).respond(404)

    process_new_content(world.podcast, None, False,
                        create(world.podcast, f'https://{PEER}/@mypodcast/posts/1', ANNOUNCEMENT), False)

    post = Post.query.one()
    assert post.url == AUDIO
    assert post.community_id == podcast_community_for(world.podcast).id
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_podcast_episode_routing.py -v`
Expected: `test_an_episode_from_a_podcast_lands_in_its_community` and `test_the_episode_audio_still_arrives` FAIL (the post is in `microblogs`); the person test PASSES.

- [ ] **Step 3: Route community-less Notes from a podcast to its community**

In `app/activitypub/routes.py`, add to the top-level imports:

```python
from app.discovery.podcast import podcast_community_for
```

In `process_new_content`, replace:

```python
    if community is None:
        # community was not found earlier - this means the incoming post is from a microblogging platform
        community = find_microblogging_community()  # set community to the microblogging community
```

with:

```python
    if community is None:
        # community was not found earlier - this means the incoming post is from a microblogging platform,
        # or (D24) from a Castopod podcast, whose episodes belong in the podcast's own community
        community = podcast_community_for(user) or find_microblogging_community()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./run_tests.sh tests/test_podcast_episode_routing.py tests/test_podcast_episode_audio.py tests/test_podcast_actor.py tests/test_visibility_ingest.py tests/test_visibility_single_object.py tests/test_no_inline_imports.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/routes.py tests/test_podcast_episode_routing.py
git commit -m "$(cat <<'EOF'
feat: a Castopod episode lands in its podcast's community instead of microblogs (interop D24)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 14: Parse episode credits from a podcast's RSS feed

**Files:**
- Create: `app/discovery/credits.py`
- Create: `app/discovery/fixtures/castopod_feed.xml`
- Test: `tests/test_podcast_credits_parse.py`

**Interfaces:**
- Consumes: `app.discovery.filters.{clean_name, clean_https_url}` (Task 6).
- Produces:
  - `app.discovery.credits.MAX_FEED_BYTES = 2 * 1024 * 1024`, `MAX_CREDITS = 20`, `CREDIT_NAME_LIMIT = 100`, `PODCAST_NAMESPACE = 'https://podcastindex.org/namespace/1.0'`, `HOST_ROLES = ('host', 'co-host')`
  - `parse_feed_credits(feed_bytes: bytes, episode_url: str) -> list[dict]` — ordered credits `{'name': str, 'role': 'host' | 'guest', 'image': str | None, 'profile_url': str | None, 'user_id': None}`: channel-level `<podcast:person>` whose role is host/co-host (a missing role means host, per the namespace spec) are hosts; the item whose `<link>` or `<guid>` equals the episode url (trailing slash ignored) contributes its `role="guest"` people; capped at `MAX_CREDITS`; `[]` for an oversized, DOCTYPE/ENTITY-bearing or unparsable feed.

- [ ] **Step 1: Write the fixture and the failing test**

Create `app/discovery/fixtures/castopod_feed.xml`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:podcast="https://podcastindex.org/namespace/1.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>My Podcast</title>
    <link>https://pod.example/@mypodcast</link>
    <generator>Castopod - https://castopod.org/</generator>
    <podcast:person role="host" img="https://pod.example/media/ann.jpg" href="https://social.example/@ann">Ann Host</podcast:person>
    <podcast:person role="co-host" href="https://ben.example/about">Ben Cohost</podcast:person>
    <podcast:person role="producer">Pat Producer</podcast:person>
    <item>
      <title>Episode 1: Hello</title>
      <link>https://pod.example/@mypodcast/episodes/ep-1</link>
      <guid isPermaLink="false">0d9c2f5e-ep-1</guid>
      <podcast:person role="guest" href="https://social.example/@cara">Cara Guest</podcast:person>
      <podcast:person role="host">Ann Host</podcast:person>
    </item>
    <item>
      <title>Episode 2</title>
      <link>https://pod.example/@mypodcast/episodes/ep-2</link>
      <guid>https://pod.example/@mypodcast/episodes/ep-2</guid>
      <podcast:person role="guest">Dan Other</podcast:person>
    </item>
  </channel>
</rss>
```

Create `tests/test_podcast_credits_parse.py`:

```python
"""Interop D24, decision 6: credits come from the podcast's RSS <podcast:person> tags. Channel-level
people are hosts; the matching item's role="guest" people are guests. The feed is untrusted."""
from pathlib import Path

from app.discovery.credits import MAX_CREDITS, MAX_FEED_BYTES, parse_feed_credits

FIXTURE = Path(__file__).resolve().parent.parent / 'app' / 'discovery' / 'fixtures' / 'castopod_feed.xml'
EP1 = 'https://pod.example/@mypodcast/episodes/ep-1'
EP2 = 'https://pod.example/@mypodcast/episodes/ep-2'
ANN = {'name': 'Ann Host', 'role': 'host', 'image': 'https://pod.example/media/ann.jpg',
       'profile_url': 'https://social.example/@ann', 'user_id': None}
BEN = {'name': 'Ben Cohost', 'role': 'host', 'image': None, 'profile_url': 'https://ben.example/about', 'user_id': None}


def feed(channel_people='', items=''):
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<rss version="2.0" xmlns:podcast="https://podcastindex.org/namespace/1.0">'
            f'<channel><title>T</title>{channel_people}{items}</channel></rss>').encode()


def guest_item(link='', guid='', name='Gia Guest'):
    return (f'<item><link>{link}</link><guid>{guid}</guid>'
            f'<podcast:person role="guest">{name}</podcast:person></item>')


def test_channel_people_are_hosts_and_the_items_guests_are_guests(app):
    credits = parse_feed_credits(FIXTURE.read_bytes(), EP1)

    assert credits == [ANN, BEN, {'name': 'Cara Guest', 'role': 'guest', 'image': None,
                                  'profile_url': 'https://social.example/@cara', 'user_id': None}]


def test_another_episode_gets_its_own_guests(app):
    assert [c['name'] for c in parse_feed_credits(FIXTURE.read_bytes(), EP2)] == ['Ann Host', 'Ben Cohost', 'Dan Other']


def test_an_episode_missing_from_the_feed_gets_the_hosts_only(app):
    assert parse_feed_credits(FIXTURE.read_bytes(), 'https://pod.example/@mypodcast/episodes/nope') == [ANN, BEN]


def test_a_person_with_no_role_is_a_host(app):
    credits = parse_feed_credits(feed('<podcast:person>Rolf Noroll</podcast:person>'), EP1)

    assert [(c['name'], c['role']) for c in credits] == [('Rolf Noroll', 'host')]


def test_an_item_matches_by_guid_or_with_a_trailing_slash(app):
    """Review focus 5."""
    by_slash = feed(items=guest_item(link=EP1 + '/'))
    by_guid = feed(items=guest_item(link='https://pod.example/elsewhere', guid=EP1))

    assert [c['name'] for c in parse_feed_credits(by_slash, EP1)] == ['Gia Guest']
    assert [c['name'] for c in parse_feed_credits(by_guid, EP1 + '/')] == ['Gia Guest']


def test_a_broken_or_empty_feed_gives_no_credits(app):
    assert parse_feed_credits(b'', EP1) == []
    assert parse_feed_credits(b'not xml at all', EP1) == []
    assert parse_feed_credits(b'<rss><channel>', EP1) == []
    assert parse_feed_credits(b'<rss></rss>', EP1) == []


def test_a_feed_with_a_doctype_or_entity_is_refused(app):
    laughs = (b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]>'
              b'<rss xmlns:podcast="https://podcastindex.org/namespace/1.0"><channel>'
              b'<podcast:person>&lol2;</podcast:person></channel></rss>')

    assert parse_feed_credits(laughs, EP1) == []


def test_a_feed_over_two_megabytes_is_refused(app):
    padded = FIXTURE.read_bytes().replace(b'</channel>', b'<!--' + b'x' * MAX_FEED_BYTES + b'--></channel>')

    assert parse_feed_credits(padded, EP1) == []


def test_untrusted_values_are_cleaned(app):
    person = ('<podcast:person img="javascript:alert(1)" href="http://plain.example/me">'
              '<![CDATA[<b>Bold</b> Name]]></podcast:person>')

    assert parse_feed_credits(feed(person), EP1) == [{'name': 'Bold Name', 'role': 'host', 'image': None,
                                                      'profile_url': None, 'user_id': None}]


def test_credits_are_capped(app):
    people = ''.join(f'<podcast:person>Host {i}</podcast:person>' for i in range(30))

    assert len(parse_feed_credits(feed(people), EP1)) == MAX_CREDITS == 20
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_podcast_credits_parse.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'app.discovery.credits'`.

- [ ] **Step 3: Write the parser**

Create `app/discovery/credits.py`:

```python
"""Episode credits from a Castopod podcast's RSS feed (interop D24, decisions 5 and 6).

Hosts are the channel's <podcast:person> tags (role host or co-host; no role means host), guests the
matching item's role="guest" tags. The feed is untrusted: refused above MAX_FEED_BYTES or when it
declares a DOCTYPE or ENTITY, and parsed by the stdlib (expat) parser, which resolves no external entities.
"""
import re
import xml.etree.ElementTree as ElementTree

from app.discovery.filters import clean_https_url, clean_name

MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_CREDITS = 20
CREDIT_NAME_LIMIT = 100
PODCAST_NAMESPACE = 'https://podcastindex.org/namespace/1.0'
HOST_ROLES = ('host', 'co-host')

_PERSON = f'{{{PODCAST_NAMESPACE}}}person'
_DECLARATIONS = re.compile(rb'<!(?:DOCTYPE|ENTITY)', re.IGNORECASE)


def _same_episode(value, episode_url: str) -> bool:
    return isinstance(value, str) and value.strip().rstrip('/') == episode_url.strip().rstrip('/')


def _credit(person, role: str) -> dict | None:
    name = clean_name(''.join(person.itertext()), CREDIT_NAME_LIMIT)
    if name is None:
        return None
    return {'name': name, 'role': role, 'image': clean_https_url(person.get('img')),
            'profile_url': clean_https_url(person.get('href')), 'user_id': None}


def parse_feed_credits(feed_bytes: bytes, episode_url: str) -> list[dict]:
    if not feed_bytes or len(feed_bytes) > MAX_FEED_BYTES or _DECLARATIONS.search(feed_bytes):
        return []
    try:
        root = ElementTree.fromstring(feed_bytes)
    except ElementTree.ParseError:
        return []
    channel = root.find('channel')
    if channel is None:
        return []
    credits = []
    for person in channel.findall(_PERSON):
        if (person.get('role') or 'host').strip().lower() in HOST_ROLES:
            credit = _credit(person, 'host')
            if credit is not None:
                credits.append(credit)
    for item in channel.findall('item'):
        if _same_episode(item.findtext('link'), episode_url) or _same_episode(item.findtext('guid'), episode_url):
            for person in item.findall(_PERSON):
                if (person.get('role') or '').strip().lower() == 'guest':
                    credit = _credit(person, 'guest')
                    if credit is not None:
                        credits.append(credit)
            break
    return credits[:MAX_CREDITS]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./run_tests.sh tests/test_podcast_credits_parse.py tests/test_no_inline_imports.py tests/test_anchored_validators.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/discovery/credits.py app/discovery/fixtures/castopod_feed.xml tests/test_podcast_credits_parse.py
git commit -m "$(cat <<'EOF'
feat: parse Castopod episode hosts and guests from the podcast RSS feed (interop D24)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 15: Fetch the feed, resolve fediverse credits and store them on the episode

**Files:**
- Modify: `app/discovery/credits.py` (fetch, resolve, store, task)
- Modify: `app/activitypub/util.py` (top-level module import; `fetch_castopod_episode_audio`, ~line 3325)
- Test: `tests/test_podcast_credits_store.py`

**Interfaces:**
- Consumes: `parse_feed_credits` (Task 14); `podcast_community_for` and `Community.rss_url` (Task 11); `Post.extensions` (Task 1); `app.activitypub.util.find_actor_or_create` (through the module, see below); `app.utils.{get_request, get_task_session, patch_db_session}`.
- Produces:
  - `fetch_feed(rss_url: str) -> bytes | None` (200 only; `None` above `MAX_FEED_BYTES`, by `Content-Length` or by body)
  - `resolve_credit_user(profile_url: str | None) -> int | None` (https only; `find_actor_or_create` with its usual guards; a non-banned `User` only)
  - `store_credits(post: Post, credits: list[dict]) -> None` (keeps other `extensions` keys; does nothing for `[]`)
  - `fetch_episode_credits(post: Post, episode_url: str) -> None` (inline under `debug`, else `.delay`)
  - Celery task `fetch_episode_credits_task(post_id: int, episode_url: str) -> None`
  - Seam: `fetch_castopod_episode_audio(post, episode_url)` now also calls `discovery_credits.fetch_episode_credits(post, episode_url)`.

`credits.py` imports `app.activitypub.util` as a module (`import app.activitypub.util as ap_util`) and `util.py` imports `app.discovery.credits` as a module: each side looks the other's names up at call time, so neither import order closes a cycle, and no function-level import is needed.

- [ ] **Step 1: Write the failing test**

Create `tests/test_podcast_credits_store.py`:

```python
"""Interop D24, decisions 6 and 7: credits are fetched from the podcast's feed after an episode arrives,
fediverse hrefs are linked to their PieFed User, and the result lives in post.extensions['podcast']."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import cache, db
from app.activitypub.util import actor_json_to_model, create_post
from app.discovery import credits
from app.discovery.credits import MAX_FEED_BYTES, fetch_episode_credits_task, resolve_credit_user, store_credits
from app.discovery.podcast import podcast_community_for
from app.models import Post, User
from tests.factories import make_banned_instance, make_post, make_site, peer_actor_json, peer_instance

PEER = 'pod.example'
ACTOR = f'https://{PEER}/@mypodcast'
FEED_URL = f'https://{PEER}/@mypodcast/feed.xml'
EP1 = f'https://{PEER}/@mypodcast/episodes/ep-1'
FIXTURE = Path(__file__).resolve().parent.parent / 'app' / 'discovery' / 'fixtures' / 'castopod_feed.xml'
PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'

pytestmark = pytest.mark.usefixtures('no_real_sleeping')


@pytest.fixture(autouse=True)
def fresh_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def world(app, db_session):
    make_site()
    peer_instance(PEER)
    podcast = actor_json_to_model(peer_actor_json('Person', name='mypodcast', server=PEER,
                                                  fields={'id': ACTOR, 'type': 'Podcast', 'name': 'My Podcast',
                                                          'rssFeed': FEED_URL}), 'mypodcast', PEER)
    community = podcast_community_for(podcast)
    post = make_post(community, podcast, f'{ACTOR}/posts/1', microblog=True)
    return SimpleNamespace(podcast=podcast, community=community, post=post)


def stored(post_id):
    db.session.expire_all()
    return db.session.get(Post, post_id).extensions


def test_a_fediverse_href_resolves_to_its_user(app, db_session, http_mock):
    peer_instance('social.example')
    http_mock.get('https://social.example/@ann').respond(json=peer_actor_json('Person', name='ann',
                                                                               server='social.example'))

    user_id = resolve_credit_user('https://social.example/@ann')

    assert db.session.get(User, user_id).ap_profile_id == 'https://social.example/u/ann'


def test_an_ordinary_web_page_resolves_to_nobody(app, db_session, http_mock):
    http_mock.get('https://ben.example/about').respond(200, text='<html>Ben</html>')

    assert resolve_credit_user('https://ben.example/about') is None


def test_plain_http_and_banned_instances_are_never_fetched(app, db_session, http_mock):
    make_banned_instance('banned.example')

    assert resolve_credit_user('http://social.example/@ann') is None
    assert resolve_credit_user('https://banned.example/@ann') is None
    assert resolve_credit_user(None) is None


def test_the_task_stores_hosts_and_guests_with_their_users(world, http_mock, monkeypatch):
    http_mock.get(FEED_URL).respond(200, content=FIXTURE.read_bytes(), headers={'Content-Type': 'application/rss+xml'})
    monkeypatch.setattr(credits, 'resolve_credit_user', {'https://social.example/@ann': 77}.get)

    fetch_episode_credits_task(world.post.id, EP1)

    saved = stored(world.post.id)['podcast']['credits']
    assert [(c['name'], c['role'], c['user_id']) for c in saved] == [
        ('Ann Host', 'host', 77), ('Ben Cohost', 'host', None), ('Cara Guest', 'guest', None)]


@pytest.mark.parametrize('answer', [dict(status_code=500), dict(status_code=200, text='<html>not a feed</html>'),
                                    dict(status_code=200, content=b'<rss>' + b' ' * MAX_FEED_BYTES + b'</rss>')])
def test_a_broken_or_oversized_feed_leaves_no_credits(world, http_mock, answer):
    http_mock.get(FEED_URL).respond(**answer)

    fetch_episode_credits_task(world.post.id, EP1)

    assert stored(world.post.id) is None


def test_a_podcast_without_a_feed_fetches_nothing(world, http_mock):
    world.community.rss_url = None
    db.session.commit()

    fetch_episode_credits_task(world.post.id, EP1)

    assert stored(world.post.id) is None


def test_storing_keeps_other_extensions(world):
    world.post.extensions = {'other': 1}
    db.session.commit()

    store_credits(world.post, [{'name': 'Ann Host', 'role': 'host', 'image': None, 'profile_url': None,
                                'user_id': None}])

    assert stored(world.post.id) == {'other': 1, 'podcast': {'credits': [
        {'name': 'Ann Host', 'role': 'host', 'image': None, 'profile_url': None, 'user_id': None}]}}


def test_an_ingested_episode_announcement_gets_its_credits(world, http_mock, monkeypatch):
    episode = http_mock.get(EP1).respond(404)   # no audio this time; the credits do not depend on it
    feed = http_mock.get(FEED_URL).respond(200, content=FIXTURE.read_bytes())
    monkeypatch.setattr(credits, 'resolve_credit_user', lambda profile_url: None)
    note_id = f'{ACTOR}/posts/2'
    activity = {'id': f'{note_id}/activity', 'type': 'Create', 'to': [PUBLIC], 'cc': [],
                'object': {'id': note_id, 'type': 'Note', 'attributedTo': ACTOR, 'to': [PUBLIC], 'cc': [],
                           'content': f'<a href="{EP1}">Episode 1: Hello</a><br/><p>New episode is out!</p>'}}

    post = create_post(False, world.community, activity, world.podcast)

    assert episode.called and feed.called
    assert [c['name'] for c in stored(post.id)['podcast']['credits']] == ['Ann Host', 'Ben Cohost', 'Cara Guest']
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_podcast_credits_store.py -v`
Expected: FAIL at collection with `ImportError: cannot import name 'fetch_episode_credits_task' from 'app.discovery.credits'`.

- [ ] **Step 3: Write fetch / resolve / store / task, and hook it after the episode audio**

In `app/discovery/credits.py`, replace the import block with:

```python
import re
import xml.etree.ElementTree as ElementTree

import httpx
from flask import current_app

import app.activitypub.util as ap_util   # the module, not names: app.activitypub.util imports this module
from app import celery, db
from app.discovery.filters import clean_https_url, clean_name
from app.discovery.podcast import podcast_community_for
from app.models import Post, User
from app.utils import get_request, get_task_session, patch_db_session
```

and append:

```python
_FEED_ACCEPT = 'application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.1'


def fetch_feed(rss_url: str) -> bytes | None:
    try:
        response = get_request(rss_url, headers={'Accept': _FEED_ACCEPT})
    except httpx.HTTPError:
        return None
    try:
        if response.status_code != 200:
            return None
        declared = response.headers.get('Content-Length', '')
        if declared.isdigit() and int(declared) > MAX_FEED_BYTES:
            return None
        body = response.content
        return body if len(body) <= MAX_FEED_BYTES else None
    finally:
        response.close()


def resolve_credit_user(profile_url) -> int | None:
    """The PieFed User a credit's href names when it is a fediverse account, else None. find_actor_or_create
    applies the usual guards (banned and non-allowlisted instances, blocked words, get_request's SSRF guard)."""
    if clean_https_url(profile_url) is None:
        return None
    try:
        actor = ap_util.find_actor_or_create(profile_url)
    except httpx.HTTPError:
        return None
    return actor.id if isinstance(actor, User) and not actor.banned else None


def store_credits(post: Post, credits: list) -> None:
    if not credits:
        return
    extensions = dict(post.extensions) if isinstance(post.extensions, dict) else {}
    extensions['podcast'] = {'credits': credits}
    post.extensions = extensions   # a new dict: db.JSON does not track in-place changes
    db.session.commit()


def fetch_episode_credits(post: Post, episode_url: str) -> None:
    if current_app.debug:
        fetch_episode_credits_task(post.id, episode_url)
    else:
        fetch_episode_credits_task.delay(post.id, episode_url)


@celery.task
def fetch_episode_credits_task(post_id, episode_url):
    """Any failure leaves the post without credits; the byline then falls back to the podcast's name."""
    with current_app.app_context():
        session = get_task_session()
        try:
            with patch_db_session(session):
                post = session.get(Post, post_id)
                if post is None or post.deleted:
                    return
                community = podcast_community_for(post.author)
                if community is None or not community.rss_url:
                    return
                feed = fetch_feed(community.rss_url)
                if feed is None:
                    return
                credits = parse_feed_credits(feed, episode_url)
                for credit in credits:
                    credit['user_id'] = resolve_credit_user(credit['profile_url'])
                store_credits(post, credits)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
```

In `app/activitypub/util.py`, add next to `import app.activitypub.actor as activitypub_actor` (top level):

```python
import app.discovery.credits as discovery_credits   # the module: app.discovery.credits imports this one
```

and replace `fetch_castopod_episode_audio` with:

```python
def fetch_castopod_episode_audio(post: Post, episode_url: str):
    if current_app.debug:
        fetch_castopod_episode_audio_task(post.id, episode_url)
    else:
        fetch_castopod_episode_audio_task.delay(post.id, episode_url)
    discovery_credits.fetch_episode_credits(post, episode_url)  # D24: the episode's hosts and guests
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./run_tests.sh tests/test_podcast_credits_store.py tests/test_podcast_credits_parse.py tests/test_podcast_episode_audio.py tests/test_podcast_episode_routing.py tests/test_no_inline_imports.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/discovery/credits.py app/activitypub/util.py tests/test_podcast_credits_store.py
git commit -m "$(cat <<'EOF'
feat: fetch, resolve and store Castopod episode credits after the episode arrives (interop D24)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 16: The post byline shows the credits

**Files:**
- Modify: `app/discovery/credits.py` (add `podcast_credits`, `podcast_byline`)
- Modify: `app/discovery/views.py` (register `podcast_byline` as a template global)
- Create: `app/templates/discovery/_byline.html`
- Modify: `app/templates/post/_post_full.html` (the two `render_username(post.author, ...)` sites at lines 29 and 66)
- Test: `tests/test_podcast_byline.py`

**Interfaces:**
- Consumes: `Post.extensions` credits (Tasks 1, 15); `app.main.bp` and `views.py` (Task 10).
- Produces:
  - `app.discovery.credits.podcast_credits(post) -> list[dict] | None` (the stored list, or None when there is none)
  - `app.discovery.credits.podcast_byline(post) -> dict | None` — `{'hosts': [{'name', 'href', 'local'}], 'guests': [...]}`; `href` is `/u/<link>` for a credit whose `user_id` names a non-banned, non-deleted User, else its https `profile_url`, else None; with no host credits the podcast author stands in as host.
  - Jinja global `podcast_byline(post)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_podcast_byline.py`:

```python
"""Interop D24, decision 5: the post byline reads "Hosted by A, B · with guest C" from the credits, and
falls back to the podcast's own name when there are none."""
from types import SimpleNamespace

import pytest

from app import db
from app.activitypub.util import actor_json_to_model
from app.discovery.credits import podcast_byline
from app.discovery.podcast import podcast_community_for
from tests.factories import make_instance, make_post, make_site, make_user, peer_actor_json, peer_instance
from tests.test_visibility_single_object import client_as

PEER = 'pod.example'
ACTOR = f'https://{PEER}/@mypodcast'


def credit(name, role='host', profile_url=None, user_id=None):
    return {'name': name, 'role': role, 'image': None, 'profile_url': profile_url, 'user_id': user_id}


@pytest.fixture
def world(app, db_session):
    make_site()
    peer_instance(PEER)
    podcast = actor_json_to_model(peer_actor_json('Person', name='mypodcast', server=PEER,
                                                  fields={'id': ACTOR, 'type': 'Podcast', 'name': 'My Podcast'}),
                                  'mypodcast', PEER)
    ann = make_user(make_instance('social.example'), 'ann')
    community = podcast_community_for(podcast)
    return SimpleNamespace(podcast=podcast, ann=ann, community=community)


def with_credits(world, microblog=True, credits=None, number=1):
    post = make_post(world.community, world.podcast, f'{ACTOR}/posts/{number}', title='Episode 1', microblog=microblog)
    if credits is not None:
        post.extensions = {'podcast': {'credits': credits}}
        db.session.commit()
    return post


def page(app, post, monkeypatch):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)  # the post page renders form.csrf_token
    viewer = make_user(make_instance('viewer.example'), 'viewer', local=True)
    return client_as(app, viewer).get(f'/post/{post.id}').get_data(as_text=True)


CREDITS = [credit('Ann Host', profile_url='https://social.example/@ann'),
           credit('Ben Cohost', profile_url='https://ben.example/about'),
           credit('Cara Guest', role='guest')]


@pytest.mark.parametrize('microblog', [True, False])
def test_the_byline_names_hosts_and_guests(app, world, monkeypatch, microblog):
    CREDITS[0]['user_id'] = world.ann.id
    html = page(app, with_credits(world, microblog=microblog, credits=CREDITS), monkeypatch)

    assert 'Hosted by' in html
    assert f'href="/u/{world.ann.link()}"' in html
    assert 'href="https://ben.example/about"' in html
    assert 'with guest' in html and 'Cara Guest' in html


def test_without_credits_the_byline_is_the_podcast(app, world, monkeypatch):
    html = page(app, with_credits(world), monkeypatch)

    assert 'Hosted by' not in html
    assert 'My Podcast' in html


def test_a_banned_users_credit_links_to_their_profile_url_instead(world):
    world.ann.banned = True
    db.session.commit()
    post = with_credits(world, credits=[credit('Ann Host', profile_url='https://social.example/@ann',
                                               user_id=world.ann.id)])

    assert podcast_byline(post)['hosts'] == [{'name': 'Ann Host', 'href': 'https://social.example/@ann',
                                              'local': False}]


def test_guests_without_hosts_are_hosted_by_the_podcast(world):
    post = with_credits(world, credits=[credit('Cara Guest', role='guest')])

    byline = podcast_byline(post)

    assert byline['hosts'] == [{'name': world.podcast.display_name(), 'href': f'/u/{world.podcast.link()}',
                                'local': True}]
    assert byline['guests'] == [{'name': 'Cara Guest', 'href': None, 'local': False}]


def test_no_credits_means_no_byline(world):
    assert podcast_byline(with_credits(world)) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_podcast_byline.py -v`
Expected: FAIL at collection with `ImportError: cannot import name 'podcast_byline' from 'app.discovery.credits'`.

- [ ] **Step 3: Write the byline data, the template global, the partial and the template seams**

Append to `app/discovery/credits.py`:

```python
def podcast_credits(post) -> list | None:
    extensions = post.extensions if isinstance(post.extensions, dict) else {}
    podcast = extensions.get('podcast')
    stored = podcast.get('credits') if isinstance(podcast, dict) else None
    return stored if isinstance(stored, list) and stored else None


def _credit_link(credit: dict) -> dict:
    href = None
    user_id = credit.get('user_id')
    if isinstance(user_id, int) and not isinstance(user_id, bool):
        user = db.session.get(User, user_id)
        if user is not None and not user.banned and not user.deleted:
            href = f'/u/{user.link()}'
    local = href is not None
    if href is None:
        href = clean_https_url(credit.get('profile_url'))
    return {'name': credit.get('name') or '', 'href': href, 'local': local}


def podcast_byline(post) -> dict | None:
    """What the post byline shows for a podcast episode: hosts, then guests. None without credits."""
    stored = podcast_credits(post)
    if stored is None:
        return None
    hosts = [_credit_link(c) for c in stored if isinstance(c, dict) and c.get('role') == 'host']
    guests = [_credit_link(c) for c in stored if isinstance(c, dict) and c.get('role') == 'guest']
    if not hosts:
        hosts = [{'name': post.author.display_name(), 'href': f'/u/{post.author.link()}', 'local': True}]
    return {'hosts': hosts, 'guests': guests}
```

In `app/discovery/views.py`, add to the imports `from app.discovery.credits import podcast_byline` and append:

```python
bp.app_template_global('podcast_byline')(podcast_byline)   # D24: the post byline for a podcast episode
```

Create `app/templates/discovery/_byline.html`:

```html
<span class="podcast_byline">{{ _('Hosted by') }}
{% for credit in byline.hosts -%}
{% if credit.href %}<a href="{{ credit.href }}"{% if not credit.local %} rel="nofollow ugc noopener" target="_blank"{% endif %}>{{ credit.name }}</a>{% else %}{{ credit.name }}{% endif %}{% if not loop.last %}, {% endif %}
{%- endfor %}
{%- if byline.guests %} · {{ _('with guest') if byline.guests | length == 1 else _('with guests') }}
{% for credit in byline.guests -%}
{% if credit.href %}<a href="{{ credit.href }}"{% if not credit.local %} rel="nofollow ugc noopener" target="_blank"{% endif %}>{{ credit.name }}</a>{% else %}{{ credit.name }}{% endif %}{% if not loop.last %}, {% endif %}
{%- endfor %}
{%- endif %}</span>
```

In `app/templates/post/_post_full.html`, replace (line 28-29, microblog header; 16 and 20 spaces of indentation):

```html
                {% if not post.deleted or post.community.is_moderator() or post.community.is_owner() or current_user.get_id() in admin_ids -%}
                    {{ render_username(post.author, htmx_redirect_back_to=request.path, user_notes=user_notes, current_user=current_user, admin_ids=admin_ids, low_bandwidth=low_bandwidth) }}
```

with:

```html
                {% if not post.deleted or post.community.is_moderator() or post.community.is_owner() or current_user.get_id() in admin_ids -%}
                    {% set byline = podcast_byline(post) -%}
                    {% if byline %}{% include 'discovery/_byline.html' %}{% else %}{{ render_username(post.author, htmx_redirect_back_to=request.path, user_notes=user_notes, current_user=current_user, admin_ids=admin_ids, low_bandwidth=low_bandwidth) }}{% endif %}
```

and replace (lines 65-66, "submitted ... by"; 24 and 28 spaces of indentation):

```html
                        {% if not post.deleted or post.community.is_moderator() or post.community.is_owner() or current_user.get_id() in admin_ids -%}
                            {{ render_username(post.author, htmx_redirect_back_to=request.path, user_notes=user_notes, current_user=current_user, admin_ids=admin_ids, low_bandwidth=low_bandwidth) }}
```

with:

```html
                        {% if not post.deleted or post.community.is_moderator() or post.community.is_owner() or current_user.get_id() in admin_ids -%}
                            {% set byline = podcast_byline(post) -%}
                            {% if byline %}{% include 'discovery/_byline.html' %}{% else %}{{ render_username(post.author, htmx_redirect_back_to=request.path, user_notes=user_notes, current_user=current_user, admin_ids=admin_ids, low_bandwidth=low_bandwidth) }}{% endif %}
```

(Each `old_string` spans both lines: the second line alone also matches inside the more deeply indented site and the event site at line 427, which keeps `render_username`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `./run_tests.sh tests/test_podcast_byline.py tests/test_podcast_episode_audio.py tests/test_content_warning.py tests/test_no_inline_imports.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/discovery/credits.py app/discovery/views.py app/templates/discovery/_byline.html app/templates/post/_post_full.html tests/test_podcast_byline.py
git commit -m "$(cat <<'EOF'
feat: a podcast episode's byline names its hosts and guests (interop D24)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 17: The API carries `extensions.podcast.credits`

**Files:**
- Modify: `app/api/alpha/views.py` (top-level import; `post_view` variant 1, after the `content_warning` extension, ~line 138)
- Modify: `app/api/alpha/schema.py` (`PodcastCredit`, `PodcastExtension`, `PostExtensions.podcast`, ~line 399)
- Test: `tests/test_podcast_credits_api.py`

**Interfaces:**
- Consumes: `podcast_credits(post) -> list | None` (Task 16).
- Produces: `post_view(post, variant=1)['extensions']['podcast'] == {'credits': [...]}` for a non-deleted post with credits (and therefore in every variant that embeds variant 1); schema classes `PodcastCredit(name, role, image, profile_url, user_id)` and `PodcastExtension(credits)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_podcast_credits_api.py`:

```python
"""Interop D24, decision 7: the API exposes a podcast episode's credits as post.extensions.podcast.credits."""
import pytest
from flask import g

from app import db
from app.api.alpha.schema import PostExtensions
from app.api.alpha.views import post_view
from tests.factories import make_community, make_instance, make_post, make_site, make_user

CREDITS = [{'name': 'Ann Host', 'role': 'host', 'image': 'https://pod.example/ann.jpg',
            'profile_url': 'https://social.example/@ann', 'user_id': 7},
           {'name': 'Cara Guest', 'role': 'guest', 'image': None, 'profile_url': None, 'user_id': None}]


@pytest.fixture
def post(app, db_session):
    make_site()
    author = make_user(make_instance('pod.example'), 'mypodcast')
    post = make_post(make_community(), author, 'https://pod.example/@mypodcast/posts/1')
    g.admin_ids = []
    return post


def test_variant_one_carries_the_credits(post):
    post.extensions = {'podcast': {'credits': CREDITS}}
    db.session.commit()

    assert post_view(post=post, variant=1)['extensions']['podcast'] == {'credits': CREDITS}


def test_no_credits_means_no_podcast_extension(post):
    assert 'extensions' not in post_view(post=post, variant=1)


def test_a_deleted_post_does_not_carry_them(post):
    post.extensions = {'podcast': {'credits': CREDITS}}
    post.deleted = True
    db.session.commit()

    assert 'podcast' not in post_view(post=post, variant=1).get('extensions', {})


def test_the_schema_describes_the_credits():
    assert PostExtensions().load({'podcast': {'credits': CREDITS}}) == {'podcast': {'credits': CREDITS}}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_podcast_credits_api.py -v`
Expected: FAIL — `test_variant_one_carries_the_credits` with `KeyError: 'extensions'`, and `test_the_schema_describes_the_credits` because the unknown `podcast` key is excluded (`{} != {...}`).

- [ ] **Step 3: Add the extension and its schema**

In `app/api/alpha/views.py`, add a top-level import:

```python
from app.discovery.credits import podcast_credits
```

and directly after:

```python
        if post.content_warning and not post.deleted:
            # Fork extension: the warning a peer sent as `summary`, for a client that collapses the body under it
            v1.setdefault('extensions', {})['content_warning'] = post.content_warning
```

add:

```python
        if not post.deleted and (credits := podcast_credits(post)):
            # Fork extension (D24): a Castopod episode's hosts and guests, from the podcast's RSS feed
            v1.setdefault('extensions', {})['podcast'] = {'credits': credits}
```

In `app/api/alpha/schema.py`, directly before `class PostExtensions(DefaultSchema):` add:

```python
class PodcastCredit(DefaultSchema):
    name = fields.String(required=True)
    role = fields.String(required=True, metadata={"description": "'host' or 'guest'"})
    image = fields.String(allow_none=True, metadata={"format": "url"})
    profile_url = fields.String(allow_none=True, metadata={"format": "url"})
    user_id = fields.Integer(allow_none=True, metadata={"description": "The PieFed user the credit links to, when its profile is a fediverse account"})


class PodcastExtension(DefaultSchema):
    credits = fields.List(fields.Nested(PodcastCredit), metadata={"description": "Hosts, then guests, from the podcast's RSS <podcast:person> tags."})
```

and add to `class PostExtensions` after `content_warning`:

```python
    podcast = fields.Nested(PodcastExtension, metadata={"description": "A Castopod episode's credits. Fork extension; Lemmy clients ignore it."})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./run_tests.sh tests/test_podcast_credits_api.py tests/test_content_warning.py tests/test_no_inline_imports.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/api/alpha/views.py app/api/alpha/schema.py tests/test_podcast_credits_api.py
git commit -m "$(cat <<'EOF'
feat: the API exposes post.extensions.podcast.credits (interop D24)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 18: Local validation seed from fixture files

**Files:**
- Create: `app/discovery/seed.py`
- Create: `app/discovery/fixtures/castopod_actor.json`
- Modify: `app/discovery/cli.py` (second command)
- Test: `tests/test_discovery_seed.py`

**Interfaces:**
- Consumes: `peertube.channel_to_entry` (Task 2), `mastodon.account_to_entry` (Task 3), `pixelfed.profile_to_entry` (Task 4), `castopod.{actor_url_from_social_interact, podcast_to_entry}` (Task 5), `refresh.{clean_entries, upsert_entries}` and `filters.host_is_excluded` (Task 6), `register_discovery_commands` (Task 7), `podcast_community_for` (Task 11), `parse_feed_credits`, `store_credits` (Tasks 14, 15), `app.activitypub.util.{actor_json_to_model, create_post}`; all six fixture files.
- Produces:
  - `app.discovery.seed.FIXTURES: Path`, `EPISODE_URL`, `EPISODE_NOTE_ID`
  - `fixture_entries(fixtures: Path = FIXTURES) -> list[dict]`
  - `seed_podcast(fixtures: Path = FIXTURES) -> Post | None` (idempotent)
  - `seed_from_fixtures(fixtures: Path = FIXTURES) -> dict` (`{'entries': int, 'podcast_post': int | None}`)
  - `flask discovery-seed-fixtures [--force]` (refuses unless `app.debug` or `--force`; no network).

- [ ] **Step 1: Write the fixture and the failing test**

Create `app/discovery/fixtures/castopod_actor.json` (no `icon`/`image`, so nothing is downloaded):

```json
{"@context": ["https://www.w3.org/ns/activitystreams", "https://w3id.org/security/v1"],
 "type": "Podcast", "id": "https://pod.example/@mypodcast", "preferredUsername": "mypodcast", "name": "My Podcast",
 "summary": "<p>A podcast about the fediverse.</p>",
 "inbox": "https://pod.example/@mypodcast/inbox", "outbox": "https://pod.example/@mypodcast/outbox",
 "followers": "https://pod.example/@mypodcast/followers", "rssFeed": "https://pod.example/@mypodcast/feed.xml",
 "publicKey": {"id": "https://pod.example/@mypodcast#main-key", "owner": "https://pod.example/@mypodcast",
               "publicKeyPem": "-----BEGIN PUBLIC KEY-----\nnot-a-real-key\n-----END PUBLIC KEY-----\n"}}
```

Create `tests/test_discovery_seed.py`:

```python
"""Interop D24, local validation: `flask discovery-seed-fixtures` loads a PeerTube channel, a Castopod
podcast with RSS credits and Mastodon/Pixelfed directory people from fixture files, with no network."""
import pytest

from app import cli
from app.discovery.seed import seed_from_fixtures
from app.models import Community, DiscoveryEntry, Post

pytestmark = pytest.mark.usefixtures('site')


def test_the_command_refuses_outside_debug_without_force(app, db_session):
    cli.register(app)

    result = app.test_cli_runner().invoke(args=['discovery-seed-fixtures'])

    assert 'Refusing' in result.output
    assert DiscoveryEntry.query.count() == 0


def test_the_seed_loads_every_platform_and_a_podcast_with_credits(app, db_session, http_mock):
    cli.register(app)

    result = app.test_cli_runner().invoke(args=['discovery-seed-fixtures', '--force'])

    assert result.exception is None, result.exception
    assert sorted({entry.platform for entry in DiscoveryEntry.query}) == ['castopod', 'mastodon', 'peertube', 'pixelfed']
    assert DiscoveryEntry.query.count() == 7
    community = Community.query.filter_by(ap_profile_id='https://pod.example/@mypodcast').one()
    post = Post.query.filter_by(community_id=community.id).one()
    assert [(c['name'], c['role']) for c in post.extensions['podcast']['credits']] == [
        ('Ann Host', 'host'), ('Ben Cohost', 'host'), ('Cara Guest', 'guest')]


def test_seeding_twice_changes_nothing(app, db_session, http_mock):
    first = seed_from_fixtures()
    second = seed_from_fixtures()

    assert first == second
    assert DiscoveryEntry.query.count() == 7
    assert Post.query.count() == 1
```

(`http_mock` registers no routes, so any outbound request in these tests raises: the seed is proven network-free.)

- [ ] **Step 2: Run test to verify it fails**

Run: `./run_tests.sh tests/test_discovery_seed.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'app.discovery.seed'`.

- [ ] **Step 3: Write the seed and its command**

Create `app/discovery/seed.py`:

```python
"""Local validation data for discovery and Castopod (interop D24). Reads app/discovery/fixtures only and
fetches nothing: the directory rows go through the same normalisers and cleaning as a real refresh, and
the podcast through the same actor and post ingest as federation."""
import json
from pathlib import Path

from app import db
from app.activitypub.util import actor_json_to_model, create_post
from app.discovery import castopod, mastodon, peertube, pixelfed
from app.discovery.credits import parse_feed_credits, store_credits
from app.discovery.filters import host_is_excluded
from app.discovery.podcast import podcast_community_for
from app.discovery.refresh import clean_entries, upsert_entries
from app.models import Instance, Post, utcnow

FIXTURES = Path(__file__).resolve().parent / 'fixtures'
EPISODE_URL = 'https://pod.example/@mypodcast/episodes/ep-1'
EPISODE_NOTE_ID = 'https://pod.example/@mypodcast/posts/1'
PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'


def _json(fixtures: Path, name: str):
    return json.loads((fixtures / name).read_text())


def fixture_entries(fixtures: Path = FIXTURES) -> list[dict]:
    entries = [peertube.channel_to_entry(c) for c in _json(fixtures, 'sepiasearch.json')['data']]
    entries += [mastodon.account_to_entry(a, 'mastodon.example') for a in _json(fixtures, 'mastodon_directory.json')]
    entries += [pixelfed.profile_to_entry(p, 'pixelfed.example')
                for p in _json(fixtures, 'pixelfed_directory.json')['data']]
    podcastindex = _json(fixtures, 'castopod_podcastindex.json')
    for feed in podcastindex['trending']['feeds']:
        episodes = podcastindex['episodes'].get(str(feed['id']))
        actor_url = castopod.actor_url_from_social_interact(episodes['items']) if episodes else None
        if actor_url:
            entries.append(castopod.podcast_to_entry(feed, actor_url))
    return [entry for entry in entries if entry]


def _instance(domain: str, software: str) -> Instance:
    instance = db.session.query(Instance).filter_by(domain=domain).first()
    if instance is None:   # created first, so find_instance_id does not fetch the peer's nodeinfo
        instance = Instance(domain=domain, software=software)
        db.session.add(instance)
        db.session.commit()
    return instance


def seed_podcast(fixtures: Path = FIXTURES):
    existing = Post.get_by_ap_id(EPISODE_NOTE_ID)
    if existing is not None:
        return existing
    _instance('pod.example', 'castopod')
    podcast = actor_json_to_model(_json(fixtures, 'castopod_actor.json'), 'mypodcast', 'pod.example')
    community = podcast_community_for(podcast)
    if community is None:
        return None
    # The content does not open with the episode link, so no episode audio fetch is started
    activity = {'id': f'{EPISODE_NOTE_ID}/activity', 'type': 'Create', 'to': [PUBLIC], 'cc': [],
                'object': {'id': EPISODE_NOTE_ID, 'type': 'Note', 'attributedTo': podcast.ap_profile_id,
                           'to': [PUBLIC], 'cc': [],
                           'content': '<p>Episode 1 is out: a conversation with Cara Guest.</p>'}}
    post = create_post(False, community, activity, podcast)
    if post is not None:
        store_credits(post, parse_feed_credits((fixtures / 'castopod_feed.xml').read_bytes(), EPISODE_URL))
    return post


def seed_from_fixtures(fixtures: Path = FIXTURES) -> dict:
    entries = clean_entries(fixture_entries(fixtures), lambda host: host_is_excluded(host, frozenset()))
    count = upsert_entries(entries, utcnow())
    post = seed_podcast(fixtures)
    return {'entries': count, 'podcast_post': post.id if post is not None else None}
```

In `app/discovery/cli.py`, replace the imports with:

```python
import click
from flask import current_app

from app.discovery.refresh import refresh_discovery
from app.discovery.seed import seed_from_fixtures
```

and add inside `register_discovery_commands`, after the `refresh_discovery_command` definition:

```python
    @app.cli.command('discovery-seed-fixtures')
    @click.option('--force', is_flag=True, help='Seed even though the app is not in debug mode.')
    def discovery_seed_fixtures_command(force):
        """Load the discovery fixture files into this database for local validation. Fetches nothing."""
        if not current_app.debug and not force:
            click.echo('Refusing: this writes fixture rows. Run with FLASK_DEBUG=1, or pass --force.')
            return
        result = seed_from_fixtures()
        click.echo(f"entries: {result['entries']}")
        click.echo(f"podcast post: {result['podcast_post']}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./run_tests.sh tests/test_discovery_seed.py tests/test_discovery_cli.py tests/test_no_inline_imports.py -v`
Expected: PASS.

- [ ] **Step 5: Run the whole suite once**

Run: `./run_tests.sh`
Expected: PASS (every test, including `tests/test_no_inline_imports.py`, `tests/test_anchored_validators.py`, `tests/test_parallel_workers.py`, `tests/test_mutating_get_routes.py` and the visibility suite). Wait for this single run to finish before starting anything else.

- [ ] **Step 6: Commit**

```bash
git add app/discovery/seed.py app/discovery/fixtures/castopod_actor.json app/discovery/cli.py tests/test_discovery_seed.py
git commit -m "$(cat <<'EOF'
feat: flask discovery-seed-fixtures for local validation without network (interop D24)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```
