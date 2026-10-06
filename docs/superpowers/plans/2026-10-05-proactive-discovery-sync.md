# Proactive Discovery Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep an admin-set number of PeerTube channels and Castopod podcasts per remote host followed by the instance actor and polled daily. Give that content a "Videos & Podcasts" feed tab, search filters, and an opt-in search of the wider PeerTube network.

**Architecture:** A new `discovery_sync` table records which communities are synced. A daily reconcile (`flask sync_discovery`, after `refresh_discovery`) diffs the desired set (top N `DiscoveryEntry` rows per host) against the table. For each addition it creates the community, backfills it and sends Follow from `/actor`. For each drop it sends Undo. A poll task re-walks each synced outbox and stops at the first stored item. The inbox learns to answer Accept/Reject of instance-actor Follows. A single media predicate (instance software `peertube`/`castopod`) drives the feed tab, the search filter and the community browse filter. External video search is a cached SepiaSearch call behind a user toggle and an admin kill-switch.

**Tech Stack:** Flask, SQLAlchemy, Flask-Migrate (Alembic), Celery, flask-caching, WTForms, Jinja, pytest + pytest-cov (branch coverage), podman test stack (`./run_tests.sh`).

**Spec:** `docs/superpowers/specs/2026-10-05-proactive-discovery-sync-design.md`

## Global Constraints

- Default `discovery_sync_per_host = 0`. Sync is off until an admin sets it. Valid range 0–50.
- Default `discovery_sync_platforms = ['peertube', 'castopod']`. No other platform may sync.
- Default `discovery_external_search = True`. The user toggle `external` defaults to off.
- Ramp cap: at most **10** new follows per host per reconcile run.
- Follow re-sent when `pending` with `followed_at` older than **7 days**, or when the state is `none`.
- Poll stops at the first stored item or after **50** items (`BACKFILL_ITEMS`). It never retries in-task (D738/D775).
- Polls of one host are spaced **2 s** apart (`countdown`).
- External search: `https://sepiasearch.org/api/v1/search/videos`, `count=10`, **3 s** timeout, cache **600 s** per normalised (stripped, lowercased) query. Failures hide the block and log at info. Never a 500.
- Follow id: `SERVER_URL/activities/follow/<uuid>`. Undo id: `SERVER_URL/activities/undo/<uuid>`. Signing key: `Site(id=1).private_key`, key id `SERVER_URL/actor#main-key`.
- An instance-actor Accept/Reject creates no `CommunityMember` and never changes `subscriptions_count`.
- User 1's existing memberships are not touched when the pre-load is removed.
- **100% line and branch coverage** of new modules (`= 100` floors in `coverage_floors.ini`) and of every changed line in existing modules. No `# pragma: no cover` in new code.
- One commit per task. TDD: failing test first. Commit messages end with `(interop D24)` and the `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` trailer.
- Invalid client input stays 400, not a generic 500 (ruling list, D895 follow-up).
- POST + CSRF for every state-changing route. `app.utils.login_required` validates a bare `csrf_token` field.

**Spec deviations, decided while planning (each recorded in the spec in Task 17):**
- The media predicate lives in a new `app/discovery/media.py`, not `sync.py`. `sync.py` imports Celery and ActivityPub code, and the feed and search modules must not pull those in (import-order ratchet, `tests/test_import_order.py`).
- "Own SAVEPOINT per host" becomes per-host `try`/`rollback`, because `find_actor_or_create` commits and would end a SAVEPOINT early. One host's failure still never stops the others.
- The external block shows no thumbnails. A thumbnail is a third-party image the viewer's browser would fetch, which leaks their IP to every listed host.
- The media feed also requires `c.show_all is true`. It is a subset of All, so a silenced instance stays out of it.

## Review Focus

1. **An Accept forged by another host.** Any peer can POST an Accept that names our Follow uuid. Expected: the Accept is honoured only when its signed `actor` is on the host of the row's `follow_target`. Test owned by Task 4.
2. **An Accept from a Lemmy community the user joined, whose join-request uuid shares the `/activities/follow/` prefix.** Expected: it still reaches the existing `CommunityJoinRequest` path unchanged. Test owned by Task 4.
3. **An admin lowering N from 20 to 5.** Expected: exactly the 15 lowest-ranked channels per host are dropped. The 5 kept are not re-followed. Test owned by Task 8.
4. **A synced channel that a local user has also joined, then dropped from the set.** Expected: the instance actor sends Undo, but the user's `CommunityMember` row and their own follow stay. Test owned by Task 8.
5. **A SepiaSearch result whose `url` host differs from its `channel.host`, or that is not https.** Expected: dropped, so a directory row cannot steer a click to a third party. Test owned by Task 13.

---

### Task 0: Spike — do PeerTube and Castopod accept an `Application` follower? (throwaway, owner-gated)

**Files:** none committed. The scratch script lives in the session scratchpad.

This sends a real Follow from a real public PieFed instance. **Stop and ask the owner for (a) the public instance to run it on and (b) one PeerTube channel and one Castopod podcast to follow.** Do not pick hosts yourself.

- [ ] **Step 1: Ask the owner for the instance and the two target actors.** Wait for the answer.
- [ ] **Step 2: On that instance, run in `flask shell`:**

```python
import uuid
from flask import current_app
from app import db
from app.models import Site
from app.activitypub.signature import send_post_request
from app.activitypub.util import find_actor_or_create
site = db.session.get(Site, 1)
actor = f"{current_app.config['SERVER_URL']}/actor"
results = {}
for target in ['<PEERTUBE_CHANNEL_ACTOR_URL>', '<CASTOPOD_PODCAST_ACTOR_URL>']:
    community = find_actor_or_create(target, community_only=True)
    follow_uuid = str(uuid.uuid4())
    follow = {'actor': actor, 'to': [target], 'object': target, 'type': 'Follow',
              'id': f"{current_app.config['SERVER_URL']}/activities/follow/{follow_uuid}"}
    results[target] = (follow_uuid, send_post_request(community.ap_inbox_url, follow, site.private_key,
                                                       actor + '#main-key', timeout=10, new_task=False))
print(results)
```

- [ ] **Step 3: Watch `activitypub_log` for 24 hours.** Record whether an Accept, a Reject or nothing arrived for each uuid, and whether any Announce or Create from each target arrived afterwards. Note whether deliveries went to `/inbox` or to `/actor/inbox`: the latter shows as 404s in the web server log, because that route does not exist yet.
- [ ] **Step 4: Undo both Follows** with the same script, using `{'type': 'Undo', 'actor': actor, 'object': follow, 'id': ...}`.
- [ ] **Step 5: Report to the owner.** For a platform that did not Accept, Task 3's `send_instance_follow` must leave the row in `none` and skip the send: add the platform to a `POLL_ONLY_PLATFORMS` tuple in `app/discovery/instance_actor.py`, with a test. Record the outcome in the spec's "Spike before planning" section (Task 17 commits the spec edit).

---

### Task 1: `discovery_sync` model and migration

**Files:**
- Modify: `app/models.py` (after `class DiscoveryEntry`, ~line 5698)
- Create: `migrations/versions/e8a4c1f9d2b6_discovery_sync.py`
- Test: `tests/test_discovery_sync_model.py`

**Interfaces:**
- Produces:
  - `app.models.DiscoverySync`, with columns `community_id` (PK, FK `community.id` ON DELETE CASCADE), `entry_id` (FK `discovery_entry.id` ON DELETE SET NULL), `follow_target` (String 1024), `follow_uuid` (String 36, nullable, indexed), `follow_state` (String 10, default `'none'`), `followed_at`, `last_polled_at`, `last_error` (String 255), `created_at`.
  - Constants `SYNC_NONE='none'`, `SYNC_PENDING='pending'`, `SYNC_ACCEPTED='accepted'`, `SYNC_REJECTED='rejected'` in `app/discovery/__init__.py`.

- [ ] **Step 1: Write the failing test**

```python
"""Interop D24 proactive sync: one discovery_sync row per synced community."""
import pytest
from sqlalchemy.exc import IntegrityError

from app import db
from app.discovery import SYNC_NONE
from app.models import Community, DiscoverySync
from tests.discovery_fixtures import add_entry
from tests.factories import make_community

pytestmark = pytest.mark.usefixtures('site')


def test_a_row_defaults_to_no_follow_sent(db_session):
    community = make_community('zqsync', host='tube.example')
    entry = add_entry('Zqsync', host='tube.example')
    row = DiscoverySync(community_id=community.id, entry_id=entry.id, follow_target=entry.actor_url)
    db.session.add(row)
    db.session.commit()

    assert row.follow_state == SYNC_NONE
    assert row.follow_uuid is None and row.followed_at is None and row.last_polled_at is None
    assert row.created_at is not None


def test_a_community_has_at_most_one_row(db_session):
    community = make_community('zqsync', host='tube.example')
    db.session.add(DiscoverySync(community_id=community.id, follow_target='https://tube.example/a'))
    db.session.commit()
    db.session.add(DiscoverySync(community_id=community.id, follow_target='https://tube.example/b'))

    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_deleting_the_entry_keeps_the_row(db_session):
    community = make_community('zqsync', host='tube.example')
    entry = add_entry('Zqsync', host='tube.example')
    db.session.add(DiscoverySync(community_id=community.id, entry_id=entry.id, follow_target=entry.actor_url))
    db.session.commit()

    db.session.delete(entry)
    db.session.commit()

    assert db.session.get(DiscoverySync, community.id).entry_id is None


def test_deleting_the_community_deletes_the_row(db_session):
    community = make_community('zqsync', host='tube.example')
    db.session.add(DiscoverySync(community_id=community.id, follow_target='https://tube.example/a'))
    db.session.commit()

    db.session.execute(db.delete(Community).where(Community.id == community.id))
    db.session.commit()

    assert db.session.get(DiscoverySync, community.id) is None
```

- [ ] **Step 2: Run it and check that it fails**

Run: `./run_tests.sh tests/test_discovery_sync_model.py -v`
Expected: FAIL. `ImportError: cannot import name 'SYNC_NONE'`.

- [ ] **Step 3: Implement**

Append to `app/discovery/__init__.py`:

```python
# discovery_sync.follow_state (proactive sync): no Follow sent yet / sent, unanswered / accepted / refused
SYNC_NONE = 'none'
SYNC_PENDING = 'pending'
SYNC_ACCEPTED = 'accepted'
SYNC_REJECTED = 'rejected'
```

Add to `app/models.py` after `DiscoveryEntry`:

```python
class DiscoverySync(db.Model):
    """A community this instance keeps synced on its own (interop D24 proactive sync): followed by the instance
    actor (/actor) and polled daily. Built by app/discovery/sync.py's reconcile; the community and its posts
    outlive the row."""
    __tablename__ = 'discovery_sync'
    community_id = db.Column(db.Integer, db.ForeignKey('community.id', ondelete='CASCADE'), primary_key=True)
    entry_id = db.Column(db.Integer, db.ForeignKey('discovery_entry.id', ondelete='SET NULL'), nullable=True)
    follow_target = db.Column(db.String(1024), nullable=False)   # the actor the Follow goes to, case kept
    follow_uuid = db.Column(db.String(36), nullable=True, index=True)
    follow_state = db.Column(db.String(10), nullable=False, default='none', server_default='none')
    followed_at = db.Column(db.DateTime, nullable=True)
    last_polled_at = db.Column(db.DateTime, nullable=True)
    last_error = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
```

Create `migrations/versions/e8a4c1f9d2b6_discovery_sync.py`:

```python
"""discovery_sync holds the communities the instance actor keeps synced

Revision ID: e8a4c1f9d2b6
Revises: d7f3a9c2e4b1
Create Date: 2026-10-05 12:00:00.000000

Interop D24 proactive sync. One row per synced community; built by `flask sync_discovery`.
"""
from alembic import op
import sqlalchemy as sa

revision = 'e8a4c1f9d2b6'
down_revision = 'd7f3a9c2e4b1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'discovery_sync',
        sa.Column('community_id', sa.Integer(), nullable=False),
        sa.Column('entry_id', sa.Integer(), nullable=True),
        sa.Column('follow_target', sa.String(length=1024), nullable=False),
        sa.Column('follow_uuid', sa.String(length=36), nullable=True),
        sa.Column('follow_state', sa.String(length=10), server_default='none', nullable=False),
        sa.Column('followed_at', sa.DateTime(), nullable=True),
        sa.Column('last_polled_at', sa.DateTime(), nullable=True),
        sa.Column('last_error', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['community_id'], ['community.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['entry_id'], ['discovery_entry.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('community_id'),
    )
    with op.batch_alter_table('discovery_sync', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_discovery_sync_follow_uuid'), ['follow_uuid'], unique=False)


def downgrade():
    with op.batch_alter_table('discovery_sync', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_discovery_sync_follow_uuid'))
    op.drop_table('discovery_sync')
```

Before writing the migration, confirm the head: run `flask db heads` in the dev container. It must print `d7f3a9c2e4b1`. If it prints anything else, use that as `down_revision`.

- [ ] **Step 4: Run it and check that it passes**

Run: `./run_tests.sh tests/test_discovery_sync_model.py tests/test_discovery_models.py -v`
Expected: PASS. `run_tests.sh` runs `flask db upgrade`, so the migration is exercised too.

- [ ] **Step 5: Commit**

```bash
git add app/models.py app/discovery/__init__.py migrations/versions/e8a4c1f9d2b6_discovery_sync.py tests/test_discovery_sync_model.py
git commit -m "feat: a discovery_sync table records the communities the instance keeps synced (interop D24)"
```

---

### Task 2: Settings and the desired set

**Files:**
- Create: `app/discovery/sync.py`
- Test: `tests/test_discovery_sync_desired.py`

**Interfaces:**
- Consumes: `DiscoveryEntry`, `Community`, `host_is_excluded(host, frozenset())`, `get_setting`.
- Produces:
  - `SYNC_PLATFORMS = ('peertube', 'castopod')`, `MAX_PER_HOST = 50`
  - `sync_per_host() -> int` (0–50; anything invalid is 0)
  - `sync_platforms() -> list[str]`
  - `desired_entries() -> dict[str, list[DiscoveryEntry]]`: keyed by lowercase host, each list ranked by followers desc, name, id, at most `sync_per_host()` long

- [ ] **Step 1: Write the failing test**

```python
"""Interop D24 proactive sync: which directory entries the instance keeps synced."""
import pytest

from app import db
from app.discovery import sync
from app.discovery.sync import desired_entries, sync_per_host, sync_platforms
from app.utils import set_setting
from tests.discovery_fixtures import add_entry, fresh_cache  # noqa: F401
from tests.factories import make_banned_instance, make_community

pytestmark = pytest.mark.usefixtures('site', 'fresh_cache')


def names(desired):
    return {host: [e.name for e in entries] for host, entries in desired.items()}


@pytest.mark.parametrize('stored, expected', [
    (None, 0), (5, 5), (0, 0), (50, 50), (51, 50), (-1, 0), ('7', 0), (True, 0), (2.5, 0)])
def test_per_host_is_an_int_from_0_to_50(db_session, stored, expected):
    if stored is not None:
        set_setting('discovery_sync_per_host', stored)
    assert sync_per_host() == expected


@pytest.mark.parametrize('stored, expected', [
    (None, ['peertube', 'castopod']), (['castopod'], ['castopod']),
    (['mastodon', 'peertube'], ['peertube']), ('peertube', []), ([], [])])
def test_platforms_keep_only_syncable_ones(db_session, stored, expected):
    if stored is not None:
        set_setting('discovery_sync_platforms', stored)
    assert sync_platforms() == expected


def test_nothing_is_desired_while_per_host_is_zero(db_session):
    add_entry('Bigchan', host='tube.example', followers=900)
    assert desired_entries() == {}


def test_nothing_is_desired_with_no_platform(db_session):
    set_setting('discovery_sync_per_host', 5)
    set_setting('discovery_sync_platforms', [])
    add_entry('Bigchan', host='tube.example', followers=900)
    assert desired_entries() == {}


def test_top_n_per_host_ranked_by_followers_then_name(db_session):
    set_setting('discovery_sync_per_host', 2)
    add_entry('Bigchan', host='tube.example', followers=900)
    add_entry('Bbtie', host='tube.example', followers=500)
    add_entry('Aatie', host='tube.example', followers=500)
    add_entry('Tiny', host='tube.example', followers=1)
    add_entry('Pod', host='pod.example', platform='castopod', url='https://pod.example/@pod', followers=3)

    assert names(desired_entries()) == {'tube.example': ['Bigchan', 'Aatie'], 'pod.example': ['Pod']}


def test_people_nsfw_disabled_platforms_and_excluded_hosts_are_never_desired(db_session):
    set_setting('discovery_sync_per_host', 5)
    set_setting('discovery_sync_platforms', ['peertube'])
    add_entry('Ok', host='tube.example')
    add_entry('Spicy', host='tube.example', nsfw=True)
    add_entry('Ann', host='m.example', platform='mastodon', kind='person', url='https://m.example/users/ann')
    add_entry('Pod', host='pod.example', platform='castopod', url='https://pod.example/@pod')
    add_entry('Gone', host='banned.example')
    make_banned_instance('banned.example')

    assert names(desired_entries()) == {'tube.example': ['Ok']}


def test_an_entry_whose_community_is_banned_or_deleted_is_skipped_and_does_not_use_a_slot(db_session):
    from app.models import utcnow
    set_setting('discovery_sync_per_host', 1)
    banned = add_entry('Banned', host='tube.example', followers=900)
    deleted = add_entry('Deleted', host='tube.example', followers=800)
    add_entry('Next', host='tube.example', followers=1)
    for entry, name in ((banned, 'banned'), (deleted, 'deleted')):
        community = make_community(name, host='tube.example')
        community.ap_profile_id = entry.actor_url.lower()
    db.session.query(sync.Community).filter_by(name='banned').one().banned = True
    db.session.query(sync.Community).filter_by(name='deleted').one().ap_deleted_at = utcnow()
    db.session.commit()

    assert names(desired_entries()) == {'tube.example': ['Next']}
```

- [ ] **Step 2: Run it and check that it fails**

Run: `./run_tests.sh tests/test_discovery_sync_desired.py -v`
Expected: FAIL. `ModuleNotFoundError: No module named 'app.discovery.sync'`.

- [ ] **Step 3: Implement**

Create `app/discovery/sync.py`:

```python
"""Proactive sync of PeerTube channels and Castopod podcasts (interop D24,
docs/superpowers/specs/2026-10-05-proactive-discovery-sync-design.md). Each remote host's top N directory entries
are followed by the instance actor and polled daily; the rest are left alone."""
from sqlalchemy import func

from app import db
from app.discovery import KIND_COMMUNITY
from app.discovery.filters import host_is_excluded
from app.models import Community, DiscoveryEntry
from app.utils import get_setting

SYNC_PLATFORMS = ('peertube', 'castopod')
MAX_PER_HOST = 50


def sync_per_host() -> int:
    """discovery_sync_per_host, or 0 for anything that is not an int from 0 to MAX_PER_HOST (bools included)."""
    value = get_setting('discovery_sync_per_host', 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return min(value, MAX_PER_HOST)


def sync_platforms() -> list:
    value = get_setting('discovery_sync_platforms', list(SYNC_PLATFORMS))
    if not isinstance(value, list):
        return []
    return [platform for platform in SYNC_PLATFORMS if platform in value]


def _unusable_community_urls() -> set:
    """ap_profile_ids of communities an admin banned or the remote deleted: never synced, never counted."""
    rows = db.session.query(Community.ap_profile_id).filter(
        (Community.banned == True) | (Community.ap_deleted_at != None), Community.ap_profile_id != None)
    return {url for (url,) in rows}


def desired_entries() -> dict:
    per_host, platforms = sync_per_host(), sync_platforms()
    if per_host == 0 or not platforms:
        return {}
    unusable = _unusable_community_urls()
    query = db.session.query(DiscoveryEntry).filter(DiscoveryEntry.kind == KIND_COMMUNITY,
                                                    DiscoveryEntry.platform.in_(platforms),
                                                    DiscoveryEntry.nsfw == False) \
        .order_by(func.lower(DiscoveryEntry.host), DiscoveryEntry.followers.desc(), DiscoveryEntry.name,
                  DiscoveryEntry.id)
    desired, excluded = {}, {}
    for entry in query:
        host = entry.host.lower()
        if host not in excluded:
            excluded[host] = host_is_excluded(host, frozenset())
        if excluded[host] or entry.actor_url.lower() in unusable:
            continue
        chosen = desired.setdefault(host, [])
        if len(chosen) < per_host:
            chosen.append(entry)
    return {host: entries for host, entries in desired.items() if entries}
```

- [ ] **Step 4: Run it and check that it passes, with coverage**

Run: `./run_tests.sh tests/test_discovery_sync_desired.py --cov=app.discovery.sync --cov-branch --cov-report=term-missing -v`
Expected: PASS, `app/discovery/sync.py` 100%.

- [ ] **Step 5: Commit**

```bash
git add app/discovery/sync.py tests/test_discovery_sync_desired.py
git commit -m "feat: proactive sync picks each host's top N PeerTube channels and Castopod podcasts (interop D24)"
```

---

### Task 3: The instance actor's Follow and Undo

**Files:**
- Create: `app/discovery/instance_actor.py`
- Test: `tests/test_discovery_instance_actor.py`

**Interfaces:**
- Consumes: `DiscoverySync`, `send_post_request(uri, body, private_key, key_id, timeout=10)`, `Site`.
- Produces:
  - `instance_actor_url() -> str` (`SERVER_URL/actor`)
  - `follow_activity(row) -> dict`
  - `send_instance_follow(row, community) -> bool`: sets `follow_state` and `followed_at`, assigns `follow_uuid` if missing, commits
  - `send_instance_undo(row, community) -> None`: best-effort, never raises

- [ ] **Step 1: Write the failing test**

```python
"""Interop D24 proactive sync: the instance actor (/actor) follows and unfollows synced channels."""
from datetime import timedelta

import pytest
from flask import current_app

from app import db
from app.discovery import SYNC_NONE, SYNC_PENDING
from app.discovery import instance_actor
from app.discovery.instance_actor import follow_activity, instance_actor_url, send_instance_follow, \
    send_instance_undo
from app.models import DiscoverySync, Site, utcnow
from tests.factories import make_community, make_instance

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(instance_actor, 'send_post_request',
                        lambda uri, body, private_key, key_id, timeout=10: calls.append(
                            dict(uri=uri, body=body, private_key=private_key, key_id=key_id)) or True)
    return calls


@pytest.fixture
def synced(db_session):
    instance = make_instance('tube.example', software='peertube')
    community = make_community('zqchan', host='tube.example')
    community.instance_id = instance.id
    community.ap_inbox_url = 'https://tube.example/video-channels/zqchan/inbox'
    row = DiscoverySync(community_id=community.id, follow_target='https://tube.example/video-channels/ZqChan')
    db.session.add(row)
    db.session.commit()
    return row, community


def test_the_follow_comes_from_the_instance_actor_and_names_the_target_with_its_case(synced, sent):
    row, community = synced
    server = current_app.config['SERVER_URL']

    assert send_instance_follow(row, community) is True

    assert len(sent) == 1
    call = sent[0]
    assert call['uri'] == 'https://tube.example/video-channels/zqchan/inbox'
    assert call['key_id'] == f'{server}/actor#main-key'
    assert call['private_key'] == db.session.get(Site, 1).private_key
    assert call['body'] == {'actor': f'{server}/actor', 'to': ['https://tube.example/video-channels/ZqChan'],
                            'object': 'https://tube.example/video-channels/ZqChan', 'type': 'Follow',
                            'id': f'{server}/activities/follow/{row.follow_uuid}'}
    assert row.follow_state == SYNC_PENDING and row.followed_at is not None


def test_a_resend_keeps_the_same_uuid(synced, sent):
    row, community = synced
    send_instance_follow(row, community)
    first = row.follow_uuid

    send_instance_follow(row, community)

    assert row.follow_uuid == first
    assert sent[0]['body']['id'] == sent[1]['body']['id']


def test_an_offline_instance_gets_no_follow_and_stays_none(synced, sent):
    row, community = synced
    community.instance.dormant = True
    db.session.commit()

    assert send_instance_follow(row, community) is False

    assert sent == [] and row.follow_state == SYNC_NONE


def test_a_community_with_no_inbox_gets_no_follow(synced, sent):
    row, community = synced
    community.ap_inbox_url = None
    db.session.commit()

    assert send_instance_follow(row, community) is False
    assert sent == []


def test_a_community_with_no_instance_gets_no_follow(synced, sent):
    row, community = synced
    community.instance_id = None
    db.session.commit()

    assert send_instance_follow(row, community) is False
    assert sent == []


def test_undo_wraps_the_stored_follow(synced, sent):
    row, community = synced
    send_instance_follow(row, community)

    send_instance_undo(row, community)

    undo = sent[1]['body']
    assert undo['type'] == 'Undo' and undo['actor'] == instance_actor_url()
    assert undo['object'] == follow_activity(row)
    assert undo['id'].startswith(f"{current_app.config['SERVER_URL']}/activities/undo/")
    assert sent[1]['uri'] == community.ap_inbox_url


def test_undo_without_a_follow_sends_nothing(synced, sent):
    row, community = synced
    send_instance_undo(row, community)
    assert sent == []


def test_undo_without_an_inbox_sends_nothing(synced, sent):
    row, community = synced
    row.follow_uuid = 'abc'
    community.ap_inbox_url = None
    send_instance_undo(row, community)
    assert sent == []


def test_an_undo_transport_failure_is_logged_not_raised(synced, monkeypatch, caplog):
    row, community = synced
    row.follow_uuid = 'abc'

    def boom(*args, **kwargs):
        raise OSError('broker down')
    monkeypatch.setattr(instance_actor, 'send_post_request', boom)

    send_instance_undo(row, community)

    assert 'undo' in caplog.text.lower()
```

- [ ] **Step 2: Run it and check that it fails**

Run: `./run_tests.sh tests/test_discovery_instance_actor.py -v`
Expected: FAIL. `ModuleNotFoundError: No module named 'app.discovery.instance_actor'`.

- [ ] **Step 3: Implement**

Create `app/discovery/instance_actor.py`:

```python
"""The instance actor (/actor) as the follower of proactively synced channels and podcasts (interop D24). A Follow
from /actor puts no user in the community: Accept and Reject only change the discovery_sync row."""
import uuid

from flask import current_app

from app import db
from app.activitypub.signature import send_post_request
from app.discovery import SYNC_NONE, SYNC_PENDING
from app.models import Site, utcnow


def instance_actor_url() -> str:
    return f"{current_app.config['SERVER_URL']}/actor"


def _signing():
    site = db.session.get(Site, 1)   # not g.site: this runs from Celery and the CLI too
    return site.private_key, instance_actor_url() + '#main-key'


def follow_activity(row) -> dict:
    return {'actor': instance_actor_url(), 'to': [row.follow_target], 'object': row.follow_target, 'type': 'Follow',
            'id': f"{current_app.config['SERVER_URL']}/activities/follow/{row.follow_uuid}"}


def send_instance_follow(row, community) -> bool:
    """Send (or re-send, under the same uuid) the instance actor's Follow. An offline or inbox-less peer gets
    nothing and the row stays 'none', which the next reconcile retries."""
    instance = community.instance
    if instance is None or not instance.online() or not community.ap_inbox_url:
        row.follow_state = SYNC_NONE
        db.session.commit()
        return False
    if not row.follow_uuid:
        row.follow_uuid = str(uuid.uuid4())
    private_key, key_id = _signing()
    send_post_request(community.ap_inbox_url, follow_activity(row), private_key, key_id, timeout=10)
    row.follow_state = SYNC_PENDING
    row.followed_at = utcnow()
    db.session.commit()
    return True


def send_instance_undo(row, community) -> None:
    """Best-effort: a peer that cannot be told still loses the row, so a dead host never pins it."""
    if not row.follow_uuid or not community.ap_inbox_url:
        return
    undo = {'type': 'Undo', 'actor': instance_actor_url(), 'object': follow_activity(row),
            'id': f"{current_app.config['SERVER_URL']}/activities/undo/{uuid.uuid4()}"}
    private_key, key_id = _signing()
    try:
        send_post_request(community.ap_inbox_url, undo, private_key, key_id, timeout=10)
    except Exception as error:
        current_app.logger.info(f'discovery sync: undo to {community.ap_inbox_url} failed: {type(error).__name__}')
```

Note: `instance.dormant = True` makes `online()` false (`app/models.py:704`). If the `Instance` factory leaves `dormant` as None, `online()` is still True, so the test is valid.

- [ ] **Step 4: Run it and check that it passes, with coverage**

Run: `./run_tests.sh tests/test_discovery_instance_actor.py --cov=app.discovery.instance_actor --cov-branch --cov-report=term-missing -v`
Expected: PASS, 100%.

- [ ] **Step 5: Commit**

```bash
git add app/discovery/instance_actor.py tests/test_discovery_instance_actor.py
git commit -m "feat: the instance actor can follow and unfollow a synced channel (interop D24)"
```

---

### Task 4: Accept and Reject of an instance-actor Follow

**Files:**
- Modify: `app/discovery/instance_actor.py`
- Modify: `app/activitypub/routes.py` (Accept branch at `if core_activity['type'] == 'Accept':` ~line 1185; Reject branch at ~line 1265)
- Test: `tests/test_inbox_instance_actor_answers.py`

**Interfaces:**
- Consumes: `host_of(url)` from `app.activitypub.util`.
- Produces:
  - `instance_actor_answer(activity: dict, signer: str) -> tuple[bool, DiscoverySync | None]`. The first element says whether the activity answers an instance-actor Follow. The second is the matching row, which exists only when the signer is on the target's host.
  - `record_answer(row, state: str) -> None`

- [ ] **Step 1: Write the failing test**

```python
"""Interop D24 proactive sync: an Accept or Reject of the instance actor's Follow changes only the sync row."""
import uuid

import pytest
from flask import current_app

from app import db
from app.constants import APLOG_ACCEPT
from app.discovery import SYNC_ACCEPTED, SYNC_PENDING, SYNC_REJECTED
from app.discovery.instance_actor import follow_activity, instance_actor_answer
from app.models import ActivityPubLog, CommunityMember, DiscoverySync, utcnow
from tests.factories import inbox_activity, make_community, make_community_join_request, make_instance, make_user
from tests.test_inbox_dispatch_preamble import dispatch

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def pending(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('tube.example', software='peertube')
    community = make_community('zqchan', host='tube.example')
    community.instance_id = instance.id
    community.ap_fetched_at = utcnow()
    row = DiscoverySync(community_id=community.id, follow_target=community.ap_profile_id,
                        follow_uuid=str(uuid.uuid4()), follow_state=SYNC_PENDING)
    db.session.add(row)
    db.session.commit()
    return row, community


def answer(community, kind, obj):
    return inbox_activity(community, activity_type=kind, object=obj)


@pytest.mark.parametrize('kind, state', [('Accept', SYNC_ACCEPTED), ('Reject', SYNC_REJECTED)])
def test_an_answer_with_the_follow_object_sets_the_state_and_adds_no_member(pending, kind, state):
    row, community = pending
    members_before = community.subscriptions_count

    dispatch(answer(community, kind, follow_activity(row)))

    db.session.expire_all()
    assert db.session.get(DiscoverySync, community.id).follow_state == state
    assert CommunityMember.query.filter_by(community_id=community.id).count() == 0
    assert community.subscriptions_count == members_before
    assert ActivityPubLog.query.one().result == 'success'


@pytest.mark.parametrize('kind, state', [('Accept', SYNC_ACCEPTED), ('Reject', SYNC_REJECTED)])
def test_an_answer_with_the_follow_id_as_a_string_sets_the_state(pending, kind, state):
    row, community = pending

    dispatch(answer(community, kind, follow_activity(row)['id']))

    db.session.expire_all()
    assert db.session.get(DiscoverySync, community.id).follow_state == state


def test_an_answer_whose_follow_object_has_no_id_matches_by_target(pending):
    row, community = pending
    follow = follow_activity(row)
    del follow['id']

    dispatch(answer(community, 'Accept', follow))

    db.session.expire_all()
    assert db.session.get(DiscoverySync, community.id).follow_state == SYNC_ACCEPTED


def test_an_accept_signed_by_another_host_is_ignored(pending):
    """Review Focus 1: anyone can name our uuid; only the followed host may answer."""
    row, community = pending
    other = make_instance('evil.example', software='peertube')
    impostor = make_community('impostor', host='evil.example')
    impostor.instance_id = other.id
    impostor.ap_fetched_at = utcnow()
    db.session.commit()

    dispatch(answer(impostor, 'Accept', follow_activity(row)))

    db.session.expire_all()
    assert db.session.get(DiscoverySync, community.id).follow_state == SYNC_PENDING
    assert ActivityPubLog.query.one().result == 'ignored'


def test_an_answer_to_an_instance_actor_follow_we_do_not_hold_is_ignored(pending):
    row, community = pending
    follow = follow_activity(row)
    follow['id'] = f"{current_app.config['SERVER_URL']}/activities/follow/{uuid.uuid4()}"

    dispatch(answer(community, 'Accept', follow))

    assert ActivityPubLog.query.one().result == 'ignored'


def test_a_users_join_request_string_accept_still_takes_the_old_path(pending):
    """Review Focus 2: same /activities/follow/ prefix, different table."""
    row, community = pending
    joiner = make_user(make_instance('people.example'), 'joiner')
    join_request = make_community_join_request(joiner, community)

    dispatch(answer(community, 'Accept', f"{current_app.config['SERVER_URL']}/activities/follow/{join_request.uuid}"))

    assert CommunityMember.query.filter_by(user_id=joiner.id, community_id=community.id).count() == 1
    db.session.expire_all()
    assert db.session.get(DiscoverySync, community.id).follow_state == SYNC_PENDING


@pytest.mark.parametrize('obj', [None, 7, {'type': 'Follow', 'actor': 'https://x.example/u/a'}, {'actor': None}])
def test_objects_that_are_not_ours_return_not_ours(pending, obj):
    assert instance_actor_answer({'object': obj}, 'https://tube.example/video-channels/zqchan') == (False, None)


def test_a_string_object_with_no_matching_row_is_not_ours(pending):
    assert instance_actor_answer({'object': 'https://x.example/activities/follow/nope'},
                                 'https://tube.example/x') == (False, None)
```

- [ ] **Step 2: Run it and check that it fails**

Run: `./run_tests.sh tests/test_inbox_instance_actor_answers.py -v`
Expected: FAIL. `ImportError: cannot import name 'instance_actor_answer'`.

- [ ] **Step 3: Implement**

Append to `app/discovery/instance_actor.py`. Add `from app.models import DiscoverySync` to the models import, and `from app.activitypub.util import host_of`:

```python
def _row_for(follow, signer: str):
    """The row a Follow (a dict, or its id as a string) names, when `signer` is on the followed actor's host."""
    row = None
    follow_id = follow.get('id') if isinstance(follow, dict) else follow
    if isinstance(follow_id, str) and '/activities/follow/' in follow_id:
        row = db.session.query(DiscoverySync).filter_by(follow_uuid=follow_id.rsplit('/', 1)[-1]).first()
    elif isinstance(follow, dict) and isinstance(follow.get('object'), str):
        row = db.session.query(DiscoverySync).filter(
            db.func.lower(DiscoverySync.follow_target) == follow['object'].lower()).first()
    if row is None or host_of(row.follow_target).lower() != host_of(signer).lower():
        return None
    return row


def instance_actor_answer(activity: dict, signer: str):
    """(ours, row) for an Accept/Reject. `ours` is True when the answered Follow came from the instance actor: a
    Follow object whose actor is /actor, or a bare follow id that a sync row holds. `row` is that row, only when the
    signed `signer` is on the followed actor's host (anyone can name a uuid)."""
    obj = activity.get('object')
    if isinstance(obj, dict):
        if obj.get('actor') != instance_actor_url():
            return False, None
        return True, _row_for(obj, signer)
    if isinstance(obj, str):
        held = db.session.query(DiscoverySync.community_id).filter_by(follow_uuid=obj.rsplit('/', 1)[-1]).first()
        if held is None:
            return False, None
        return True, _row_for(obj, signer)
    return False, None


def record_answer(row, state: str) -> None:
    row.follow_state = state
    db.session.commit()
```

In `app/activitypub/routes.py`, add `from app.discovery.instance_actor import instance_actor_answer, record_answer` and `from app.discovery import SYNC_ACCEPTED, SYNC_REJECTED` near the other `app.discovery` imports. The test `tests/test_import_order.py` must still pass. If it reports a cycle, import inside the branch and add a `# cycle:` comment.

Insert as the first statements under `if core_activity['type'] == 'Accept':`:

```python
                    ours, sync_row = instance_actor_answer(core_activity, actor_id)   # D24 proactive sync
                    if ours:
                        if sync_row is None:
                            log_incoming_ap(id, APLOG_ACCEPT, APLOG_IGNORED, saved_json,
                                            'Accept of an instance actor Follow this server does not hold')
                        else:
                            record_answer(sync_row, SYNC_ACCEPTED)
                            log_incoming_ap(id, APLOG_ACCEPT, APLOG_SUCCESS, saved_json)
                        return
```

Insert the same block as the first statements under `if core_activity['type'] == 'Reject':`, with `APLOG_REJECT`, `SYNC_REJECTED` and the message `'Reject of an instance actor Follow this server does not hold'`.

- [ ] **Step 4: Run it and check that it passes, with coverage**

Run: `./run_tests.sh tests/test_inbox_instance_actor_answers.py tests/test_inbox_dispatch_accept_reject.py tests/test_import_order.py --cov=app.discovery.instance_actor --cov-branch --cov-report=term-missing -v`
Expected: PASS, `instance_actor.py` 100%. The existing Accept/Reject tests stay green.

- [ ] **Step 5: Commit**

```bash
git add app/discovery/instance_actor.py app/activitypub/routes.py tests/test_inbox_instance_actor_answers.py
git commit -m "feat: an Accept or Reject of the instance actor's Follow updates its sync row and adds no member (interop D24)"
```

---

### Task 5: `/actor/inbox`

**Files:**
- Modify: `app/activitypub/routes.py` (next to `shared_inbox`, ~line 696)
- Test: `tests/test_instance_actor_inbox.py`

**Interfaces:**
- Produces: POST `/actor/inbox` → the same response as POST `/inbox`.

- [ ] **Step 1: Write the failing test**

```python
"""Interop D24: /actor advertises /actor/inbox; a peer that ignores sharedInbox delivers there."""
import pytest

from app.activitypub import routes

pytestmark = pytest.mark.usefixtures('site')


def test_actor_inbox_is_handled_by_the_shared_inbox(app, db_session, monkeypatch):
    calls = []
    monkeypatch.setattr(routes, 'shared_inbox', lambda: calls.append('shared') or ('', 200))
    response = app.test_client().post('/actor/inbox', data=b'{}', content_type='application/activity+json')

    assert response.status_code == 200
    assert calls == ['shared']


def test_actor_inbox_refuses_get(app, db_session):
    assert app.test_client().get('/actor/inbox').status_code == 405
```

- [ ] **Step 2: Run it and check that it fails**

Run: `./run_tests.sh tests/test_instance_actor_inbox.py -v`
Expected: FAIL with a 404, or a 405 on the POST.

- [ ] **Step 3: Implement**

Below `shared_inbox` in `app/activitypub/routes.py`:

```python
@bp.route('/actor/inbox', methods=['POST'])
def instance_actor_inbox():
    """/actor's own inbox (interop D24 proactive sync): the actor document advertises it, and a peer that ignores
    sharedInbox delivers a synced channel's Accept and posts here."""
    return shared_inbox()
```

If `shared_inbox` carries a decorator such as a rate limit or a CSRF exemption, apply the same decorators here. Check with `sed -n 690,700p app/activitypub/routes.py`.

- [ ] **Step 4: Run it and check that it passes**

Run: `./run_tests.sh tests/test_instance_actor_inbox.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/routes.py tests/test_instance_actor_inbox.py
git commit -m "feat: /actor/inbox, which the instance actor advertises, is handled like the shared inbox (interop D24)"
```

---

### Task 6: The backfill can stop at the first stored item and reports an unreadable outbox

**Files:**
- Modify: `app/community/util.py` (`retrieve_mods_and_backfill`, ~line 150)
- Modify: `app/discovery/backfill.py`
- Test: `tests/test_community_backfill_poll.py`

**Interfaces:**
- Produces:
  - `retrieve_mods_and_backfill(community_id, server, name, community_json=None, stop_at_known=False)`. It returns `BACKFILL_UNREADABLE = 'outbox unreadable'` when the outbox (or its first page) answers nothing, `BACKFILL_NO_OUTBOX = 'no outbox'` when the community has no outbox URL, and otherwise `None`.
  - `app.discovery.backfill.run_backfill(community_id, stop_at_known=False) -> str | None`: the body of `backfill_discovered_community`, returning that outcome. `None` means the community is gone.

- [ ] **Step 1: Write the failing test**

```python
"""Interop D24 proactive sync: a poll re-walks an outbox newest-first and stops at the first post already here."""
import pytest

from app import db
from app.community import util
from app.community.util import BACKFILL_NO_OUTBOX, BACKFILL_UNREADABLE, retrieve_mods_and_backfill
from app.discovery import backfill
from app.discovery.backfill import run_backfill
from app.models import Post
from tests.factories import make_community, make_post, make_user

pytestmark = pytest.mark.usefixtures('site')

OUTBOX = 'https://tube.example/video-channels/zqchan/outbox'


@pytest.fixture
def channel(db_session):
    community = make_community('zqchan', host='tube.example')
    community.ap_profile_id = 'https://tube.example/video-channels/zqchan'
    community.ap_outbox_url = OUTBOX
    db.session.commit()
    return community


def announces(*video_ids):
    return {'type': 'OrderedCollection', 'orderedItems': [
        {'type': 'Announce', 'id': f'{v}/announce', 'object': v} for v in video_ids]}


def test_stop_at_known_fetches_nothing_past_the_first_stored_video(channel, monkeypatch):
    author = make_user(channel.instance, 'zqauthor')
    make_post(channel, author, 'https://tube.example/videos/watch/2')
    fetched = []

    def remote(url):
        fetched.append(url)
        return announces('https://tube.example/videos/watch/3', 'https://tube.example/videos/watch/2',
                         'https://tube.example/videos/watch/1') if url == OUTBOX else None
    monkeypatch.setattr(util, 'remote_object_to_json', remote)

    retrieve_mods_and_backfill(channel.id, 'tube.example', 'zqchan', None, stop_at_known=True)

    assert fetched == [OUTBOX, 'https://tube.example/videos/watch/3']


def test_without_stop_at_known_the_walk_goes_past_stored_videos(channel, monkeypatch):
    author = make_user(channel.instance, 'zqauthor')
    make_post(channel, author, 'https://tube.example/videos/watch/2')
    fetched = []

    def remote(url):
        fetched.append(url)
        return announces('https://tube.example/videos/watch/2', 'https://tube.example/videos/watch/1') \
            if url == OUTBOX else None
    monkeypatch.setattr(util, 'remote_object_to_json', remote)

    retrieve_mods_and_backfill(channel.id, 'tube.example', 'zqchan', None)

    assert 'https://tube.example/videos/watch/1' in fetched


@pytest.mark.parametrize('item, known_id', [
    ({'type': 'Announce', 'id': 'a', 'object': {'type': 'Create', 'object': {'id': 'https://l.example/post/9'}}},
     'https://l.example/post/9'),
    ({'type': 'Create', 'id': 'c', 'object': {'id': 'https://pod.example/@pod/episodes/9'}},
     'https://pod.example/@pod/episodes/9')])
def test_stop_at_known_reads_lemmy_and_castopod_shapes(channel, monkeypatch, item, known_id):
    channel.ap_profile_id = 'https://l.example/c/zqchan'   # not a PeerTube channel url
    db.session.commit()
    author = make_user(channel.instance, 'zqauthor')
    make_post(channel, author, known_id)
    created = []
    monkeypatch.setattr(util, 'remote_object_to_json',
                        lambda url: {'type': 'OrderedCollection', 'orderedItems': [item]} if url == OUTBOX else None)
    monkeypatch.setattr(util, 'create_post', lambda *a, **k: created.append(a) or None)

    retrieve_mods_and_backfill(channel.id, 'l.example', 'zqchan', None, stop_at_known=True)

    assert created == []


@pytest.mark.parametrize('item, by_reference, expected', [
    ('https://tube.example/x', False, None),
    ({'object': 'https://tube.example/v/1'}, True, 'https://tube.example/v/1'),
    ({'object': {'id': 'x'}}, True, None),
    ({'type': 'Announce'}, False, None),
    ({'object': 5}, False, None),
    ({'object': {'object': {'id': 7}}}, False, None),
    ({'object': {'id': 7}}, False, None),
    ({'object': {'object': 'not-a-dict', 'id': 'https://l.example/p/1'}}, False, 'https://l.example/p/1')])
def test_stored_object_id(item, by_reference, expected):
    from app.community.util import _stored_object_id
    assert _stored_object_id(item, by_reference) == expected


def test_an_unreadable_outbox_is_reported(channel, monkeypatch):
    monkeypatch.setattr(util, 'remote_object_to_json', lambda url: None)
    assert retrieve_mods_and_backfill(channel.id, 'tube.example', 'zqchan', None) == BACKFILL_UNREADABLE


def test_an_empty_outbox_is_not_an_error(channel, monkeypatch):
    monkeypatch.setattr(util, 'remote_object_to_json', lambda url: {'type': 'OrderedCollection', 'totalItems': 0})
    assert retrieve_mods_and_backfill(channel.id, 'tube.example', 'zqchan', None) is None


def test_an_unreadable_first_page_is_reported(channel, monkeypatch):
    monkeypatch.setattr(util, 'remote_object_to_json',
                        lambda url: {'type': 'OrderedCollection', 'first': OUTBOX + '?page=1'} if url == OUTBOX else None)
    assert retrieve_mods_and_backfill(channel.id, 'tube.example', 'zqchan', None) == BACKFILL_UNREADABLE


def test_a_community_with_no_outbox_is_reported(channel, monkeypatch):
    channel.ap_outbox_url = None
    db.session.commit()
    monkeypatch.setattr(util, 'remote_object_to_json', lambda url: None)
    assert retrieve_mods_and_backfill(channel.id, 'tube.example', 'zqchan', None) == BACKFILL_NO_OUTBOX


def test_run_backfill_passes_stop_at_known_and_returns_the_outcome(channel, monkeypatch):
    calls = []
    monkeypatch.setattr(backfill, 'remote_object_to_json', lambda url: {'type': 'Group'})
    monkeypatch.setattr(backfill, 'retrieve_mods_and_backfill',
                        lambda cid, server, name, json, stop_at_known=False: calls.append(stop_at_known) or 'x')

    assert run_backfill(channel.id, stop_at_known=True) == 'x'
    assert calls == [True]


def test_run_backfill_of_a_missing_community_returns_none(db_session):
    assert run_backfill(999999) is None
```

- [ ] **Step 2: Run it and check that it fails**

Run: `./run_tests.sh tests/test_community_backfill_poll.py -v`
Expected: FAIL. `ImportError: cannot import name 'BACKFILL_NO_OUTBOX'`.

- [ ] **Step 3: Implement**

In `app/community/util.py`, below `BACKFILL_MAX_PAGES`:

```python
BACKFILL_UNREADABLE = 'outbox unreadable'
BACKFILL_NO_OUTBOX = 'no outbox'


def _stored_object_id(announce, by_reference: bool):
    """The id of the post an outbox entry carries, without fetching it: the Announce's object url (PeerTube,
    a.gup.pe), the Announce's inner object's id (Lemmy), or a Create's object id (Castopod, WordPress)."""
    if not isinstance(announce, dict):
        return None
    obj = announce.get('object')
    if by_reference:
        return obj if isinstance(obj, str) else None
    if not isinstance(obj, dict):
        return None
    inner = obj.get('object')
    if isinstance(inner, dict):
        return inner.get('id') if isinstance(inner.get('id'), str) else None
    return obj.get('id') if isinstance(obj.get('id'), str) else None
```

Change the signature to `def retrieve_mods_and_backfill(community_id: int, server, name, community_json=None, stop_at_known=False):`. In the outbox section:

- Replace `if community.ap_outbox_url:` with:
  ```python
  if not community.ap_outbox_url:
      return BACKFILL_NO_OUTBOX
  ```
  and dedent its body one level.
- Split the empty-outbox return:
  ```python
  outbox_data = remote_object_to_json(community.ap_outbox_url)
  if not outbox_data:
      return BACKFILL_UNREADABLE
  if 'totalItems' in outbox_data and outbox_data['totalItems'] == 0:
      return
  if 'first' in outbox_data:
      outbox_data = _walk_outbox_pages(outbox_data['first'])
      if not outbox_data:
          return BACKFILL_UNREADABLE
  ```
- As the first statements inside `for announce in outbox_data['orderedItems']:`, after `announce = _as_dict(announce)`:
  ```python
  if stop_at_known:
      known_id = _stored_object_id(announce, is_peertube or is_guppe)
      if known_id and Post.get_by_ap_id(known_id) is not None:
          break   # outboxes are newest first: everything after this is here already
  ```

The early `if not community: return` and the NSFW `return` stay as `None`.

Check that `Post` is imported in `app/community/util.py` (`grep -n "^from app.models import" app/community/util.py`) and add it if it is missing.

In `app/discovery/backfill.py`, split the task:

```python
def run_backfill(community_id: int, stop_at_known: bool = False):
    """Moderators, then the outbox, as a first backfill does; with stop_at_known (a proactive sync poll) the walk ends
    at the first post already stored. Returns retrieve_mods_and_backfill's outcome, None for a missing community."""
    community = db.session.get(Community, community_id)
    if community is None or not community.ap_profile_id:
        return None
    # The actor document gives the moderators of a PeerTube channel (attributedTo), which its videos are
    # attributed to; without it the backfill skips every video.
    community_json = remote_object_to_json(community.ap_profile_id)
    return retrieve_mods_and_backfill(community.id, community.ap_domain, community.name,
                                      community_json if isinstance(community_json, dict) else None,
                                      stop_at_known=stop_at_known)


@celery.task
def backfill_discovered_community(community_id: int):
    try:
        run_backfill(community_id)
    finally:
        cache.delete(_in_progress_key(community_id))
```

- [ ] **Step 4: Run it and check that it passes**

Run: `./run_tests.sh tests/test_community_backfill_poll.py tests/test_community_backfill.py tests/test_community_backfill_body.py tests/test_backfill_author_gate.py tests/test_backfill_reply_visibility.py tests/test_discovery_backfill.py --cov=app.community.util --cov=app.discovery.backfill --cov-branch --cov-report=term-missing -v`
Expected: PASS. Every line changed in `retrieve_mods_and_backfill` and in `backfill.py` is covered: look for the new line numbers in the "Missing" column.

- [ ] **Step 5: Commit**

```bash
git add app/community/util.py app/discovery/backfill.py tests/test_community_backfill_poll.py
git commit -m "feat: a backfill can stop at the first post already stored and reports an unreadable outbox (interop D24)"
```

---

### Task 7: Poll one synced community

**Files:**
- Modify: `app/discovery/sync.py`
- Test: `tests/test_discovery_sync_poll.py`

**Interfaces:**
- Consumes: `run_backfill(community_id, stop_at_known=True)`.
- Produces: Celery task `poll_synced_community(community_id: int) -> None`, which updates `last_polled_at` and `last_error`.

- [ ] **Step 1: Write the failing test**

```python
"""Interop D24 proactive sync: the daily poll of one synced community."""
import pytest

from app import db
from app.discovery import sync
from app.discovery.sync import poll_synced_community
from app.models import DiscoverySync, utcnow
from tests.factories import make_community

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def row(db_session):
    community = make_community('zqchan', host='tube.example')
    row = DiscoverySync(community_id=community.id, follow_target=community.ap_profile_id, last_error='old')
    db.session.add(row)
    db.session.commit()
    return row


def stub(monkeypatch, outcome=None, raises=None):
    calls = []

    def run(community_id, stop_at_known=False):
        calls.append((community_id, stop_at_known))
        if raises:
            raise raises
        return outcome
    monkeypatch.setattr(sync, 'run_backfill', run)
    return calls


def test_a_good_poll_stops_at_known_posts_records_the_time_and_clears_the_error(row, monkeypatch):
    calls = stub(monkeypatch)

    poll_synced_community(row.community_id)

    fresh = db.session.get(DiscoverySync, row.community_id)
    assert calls == [(row.community_id, True)]
    assert fresh.last_polled_at is not None and fresh.last_error is None


def test_an_unreadable_outbox_is_recorded(row, monkeypatch):
    stub(monkeypatch, outcome='outbox unreadable')
    poll_synced_community(row.community_id)
    assert db.session.get(DiscoverySync, row.community_id).last_error == 'outbox unreadable'


def test_an_exception_is_recorded_truncated_and_not_raised(row, monkeypatch):
    stub(monkeypatch, raises=RuntimeError('x' * 400))
    poll_synced_community(row.community_id)
    error = db.session.get(DiscoverySync, row.community_id).last_error
    assert error.startswith('RuntimeError: x') and len(error) == 255


def test_a_missing_row_does_nothing(db_session, monkeypatch):
    calls = stub(monkeypatch)
    poll_synced_community(999999)
    assert calls == []


@pytest.mark.parametrize('change', ['banned', 'deleted'])
def test_a_banned_or_deleted_community_loses_its_row_unpolled(row, monkeypatch, change):
    calls = stub(monkeypatch)
    community = db.session.get(sync.Community, row.community_id)
    if change == 'banned':
        community.banned = True
    else:
        community.ap_deleted_at = utcnow()
    db.session.commit()

    poll_synced_community(row.community_id)

    assert calls == [] and db.session.get(DiscoverySync, row.community_id) is None
```

- [ ] **Step 2: Run it and check that it fails**

Run: `./run_tests.sh tests/test_discovery_sync_poll.py -v`
Expected: FAIL. `ImportError: cannot import name 'poll_synced_community'`.

- [ ] **Step 3: Implement**

Add to `app/discovery/sync.py`. Imports: `from flask import current_app`, `from app import celery`, `from app.discovery.backfill import run_backfill`, `from app.models import DiscoverySync, utcnow`.

```python
ERROR_LIMIT = 255   # discovery_sync.last_error


def _unusable(community) -> bool:
    return community is None or community.banned or community.ap_deleted_at is not None


@celery.task
def poll_synced_community(community_id: int) -> None:
    """Re-walk one synced outbox, newest first, up to the first post already here. Failures are recorded on the row
    and left for tomorrow's run: no retry here (D738/D775)."""
    row = db.session.get(DiscoverySync, community_id)
    if row is None:
        return
    if _unusable(db.session.get(Community, community_id)):
        db.session.delete(row)
        db.session.commit()
        return
    try:
        outcome = run_backfill(community_id, stop_at_known=True)
    except Exception as error:
        current_app.logger.info(f'discovery sync: poll of community {community_id} failed: {type(error).__name__}')
        db.session.rollback()
        outcome = f'{type(error).__name__}: {error}'
    row = db.session.get(DiscoverySync, community_id)   # the backfill ran on its own task session
    row.last_polled_at = utcnow()
    row.last_error = outcome[:ERROR_LIMIT] if outcome else None
    db.session.commit()
```

- [ ] **Step 4: Run it and check that it passes, with coverage**

Run: `./run_tests.sh tests/test_discovery_sync_poll.py tests/test_discovery_sync_desired.py --cov=app.discovery.sync --cov-branch --cov-report=term-missing -v`
Expected: PASS, 100%.

- [ ] **Step 5: Commit**

```bash
git add app/discovery/sync.py tests/test_discovery_sync_poll.py
git commit -m "feat: a synced community is polled up to its first stored post and the outcome is recorded (interop D24)"
```

---

### Task 8: Reconcile and poll scheduling

**Files:**
- Modify: `app/discovery/sync.py`
- Test: `tests/test_discovery_sync_reconcile.py`

**Interfaces:**
- Consumes: `desired_entries()`, `send_instance_follow`, `send_instance_undo`, `queue_backfill`, `find_actor_or_create`.
- Produces:
  - `ADDS_PER_HOST_PER_RUN = 10`, `FOLLOW_RETRY_DAYS = 7`, `POLL_SPACING_SECONDS = 2`
  - `reconcile_sync() -> dict` with keys `added`, `dropped`, `refollowed`, `failed_hosts`
  - `drop_row(row) -> None`
  - `enqueue_polls() -> int` (number enqueued)
  - Celery task `reconcile_sync_task() -> dict` (reconcile, then `enqueue_polls`)

- [ ] **Step 1: Write the failing test**

```python
"""Interop D24 proactive sync: the daily reconcile keeps exactly each host's top N synced."""
from datetime import timedelta

import pytest

from app import db
from app.discovery import SYNC_ACCEPTED, SYNC_NONE, SYNC_PENDING, sync
from app.discovery.sync import enqueue_polls, reconcile_sync, reconcile_sync_task
from app.models import Community, CommunityMember, DiscoverySync, utcnow
from app.utils import set_setting
from tests.discovery_fixtures import add_entry, fresh_cache  # noqa: F401
from tests.factories import make_community, make_community_member, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site', 'fresh_cache')


@pytest.fixture
def fed(monkeypatch):
    """Doubles for everything that leaves this process; records what was asked."""
    log = {'follow': [], 'undo': [], 'backfill': [], 'resolve': []}

    def resolve(actor_url, community_only=False):
        log['resolve'].append(actor_url)
        name = actor_url.rstrip('/').rsplit('/', 1)[-1].lower()
        host = actor_url.split('/')[2]
        community = make_community(name, host=host)
        community.ap_profile_id = actor_url.lower()
        db.session.commit()
        return community

    def follow(row, community):
        log['follow'].append(row.follow_target)
        row.follow_state = SYNC_PENDING
        row.followed_at = utcnow()
        row.follow_uuid = row.follow_uuid or 'u-' + str(row.community_id)
        db.session.commit()
        return True

    monkeypatch.setattr(sync, 'find_actor_or_create', resolve)
    monkeypatch.setattr(sync, 'send_instance_follow', follow)
    monkeypatch.setattr(sync, 'send_instance_undo', lambda row, community: log['undo'].append(row.follow_target))
    monkeypatch.setattr(sync, 'queue_backfill', log['backfill'].append)
    return log


def chans(host, count, start=1000):
    return [add_entry(f'C{i:02d}{host[:2]}', host=host, followers=start - i) for i in range(count)]


def test_off_by_default_adds_nothing(db_session, fed):
    chans('tube.example', 3)
    assert reconcile_sync() == {'added': 0, 'dropped': 0, 'refollowed': 0, 'failed_hosts': []}
    assert fed['follow'] == []


def test_adds_top_n_creates_backfills_and_follows(db_session, fed):
    set_setting('discovery_sync_per_host', 2)
    entries = chans('tube.example', 3)

    summary = reconcile_sync()

    assert summary['added'] == 2
    assert fed['follow'] == [entries[0].actor_url, entries[1].actor_url]
    assert len(fed['backfill']) == 2
    assert {r.entry_id for r in DiscoverySync.query} == {entries[0].id, entries[1].id}


def test_an_existing_community_with_posts_is_not_backfilled_again(db_session, fed, monkeypatch):
    set_setting('discovery_sync_per_host', 1)
    entry = chans('tube.example', 1)[0]
    community = make_community('c00tu', host='tube.example')
    community.ap_profile_id = entry.actor_url.lower()
    community.post_count = 3
    db.session.commit()

    reconcile_sync()

    assert fed['resolve'] == [] and fed['backfill'] == []
    assert fed['follow'] == [entry.actor_url]


def test_a_resolve_that_gives_no_community_is_skipped(db_session, fed, monkeypatch):
    set_setting('discovery_sync_per_host', 1)
    chans('tube.example', 1)
    monkeypatch.setattr(sync, 'find_actor_or_create', lambda url, community_only=False: None)

    assert reconcile_sync()['added'] == 0
    assert DiscoverySync.query.count() == 0


def test_adds_ramp_at_ten_per_host_per_run(db_session, fed):
    set_setting('discovery_sync_per_host', 25)
    chans('tube.example', 25)

    assert reconcile_sync()['added'] == 10
    assert reconcile_sync()['added'] == 10
    assert reconcile_sync()['added'] == 5


def test_lowering_n_drops_the_lowest_ranked_and_keeps_the_rest_untouched(db_session, fed):
    """Review Focus 3."""
    set_setting('discovery_sync_per_host', 4)
    entries = chans('tube.example', 4)
    reconcile_sync()
    fed['follow'].clear()
    set_setting('discovery_sync_per_host', 1)

    summary = reconcile_sync()

    assert summary['dropped'] == 3
    assert sorted(fed['undo']) == sorted(e.actor_url for e in entries[1:])
    assert fed['follow'] == []
    assert [r.entry_id for r in DiscoverySync.query] == [entries[0].id]
    assert Community.query.count() == 4   # dropped communities and their posts stay


def test_per_host_zero_drops_everything(db_session, fed):
    set_setting('discovery_sync_per_host', 2)
    chans('tube.example', 2)
    reconcile_sync()
    set_setting('discovery_sync_per_host', 0)

    assert reconcile_sync()['dropped'] == 2
    assert DiscoverySync.query.count() == 0


def test_dropping_a_channel_a_local_user_joined_keeps_their_membership(db_session, fed):
    """Review Focus 4."""
    set_setting('discovery_sync_per_host', 1)
    entry = chans('tube.example', 1)[0]
    reconcile_sync()
    community = Community.query.one()
    local = make_user(make_instance('test.piefed.local', software='piefed'), 'zqlocal', local=True)
    make_community_member(local, community)
    set_setting('discovery_sync_per_host', 0)

    reconcile_sync()

    assert fed['undo'] == [entry.actor_url]
    assert CommunityMember.query.filter_by(user_id=local.id, community_id=community.id).count() == 1


def test_a_row_whose_community_is_gone_is_dropped(db_session, fed):
    set_setting('discovery_sync_per_host', 1)
    chans('tube.example', 1)
    reconcile_sync()
    community = Community.query.one()
    community.banned = True
    db.session.commit()

    assert reconcile_sync()['dropped'] == 1


def test_drop_row_with_no_community_still_deletes_it(db_session, fed):
    community = make_community('orphan', host='tube.example')
    db.session.add(DiscoverySync(community_id=community.id, follow_target='https://tube.example/x'))
    db.session.commit()
    row = db.session.get(DiscoverySync, community.id)
    db.session.delete(db.session.get(Community, community.id))
    db.session.flush()

    sync.drop_row(row)

    assert DiscoverySync.query.count() == 0 and fed['undo'] == []


@pytest.mark.parametrize('state, age_days, resent', [
    (SYNC_NONE, 0, True), (SYNC_PENDING, 8, True), (SYNC_PENDING, 6, False), (SYNC_ACCEPTED, 30, False)])
def test_follow_retry_rules(db_session, fed, state, age_days, resent):
    set_setting('discovery_sync_per_host', 1)
    chans('tube.example', 1)
    reconcile_sync()
    row = DiscoverySync.query.one()
    row.follow_state = state
    row.followed_at = utcnow() - timedelta(days=age_days)
    db.session.commit()
    fed['follow'].clear()

    assert reconcile_sync()['refollowed'] == (1 if resent else 0)
    assert len(fed['follow']) == (1 if resent else 0)


def test_one_host_failing_does_not_stop_the_others(db_session, fed, monkeypatch):
    set_setting('discovery_sync_per_host', 1)
    chans('aaa.example', 1)
    good = chans('bbb.example', 1)[0]
    real_resolve = sync.find_actor_or_create

    def flaky(url, community_only=False):
        if 'aaa.example' in url:
            raise RuntimeError('peer exploded')
        return real_resolve(url, community_only=community_only)
    monkeypatch.setattr(sync, 'find_actor_or_create', flaky)

    summary = reconcile_sync()

    assert summary['failed_hosts'] == ['aaa.example']
    assert [r.follow_target for r in DiscoverySync.query] == [good.actor_url]


def test_polls_are_spaced_per_host(db_session, fed, monkeypatch, app):
    set_setting('discovery_sync_per_host', 2)
    chans('tube.example', 2)
    chans('pod.example', 1)
    reconcile_sync()
    scheduled = []
    monkeypatch.setattr(app, 'debug', False)
    monkeypatch.setattr(sync.poll_synced_community, 'apply_async',
                        lambda args, countdown: scheduled.append((args[0], countdown)))

    assert enqueue_polls() == 3

    by_host = {}
    for community_id, countdown in scheduled:
        host = db.session.get(DiscoverySync, community_id).follow_target.split('/')[2]
        by_host.setdefault(host, []).append(countdown)
    assert sorted(by_host['tube.example']) == [0, 2] and by_host['pod.example'] == [0]


def test_in_debug_polls_run_inline(db_session, fed, monkeypatch, app):
    set_setting('discovery_sync_per_host', 1)
    chans('tube.example', 1)
    reconcile_sync()
    ran = []
    monkeypatch.setattr(app, 'debug', True)
    monkeypatch.setattr(sync, 'poll_synced_community', ran.append)

    enqueue_polls()

    assert ran == [DiscoverySync.query.one().community_id]


def test_the_task_reconciles_then_polls(db_session, monkeypatch):
    order = []
    monkeypatch.setattr(sync, 'reconcile_sync', lambda: order.append('reconcile') or {'added': 0})
    monkeypatch.setattr(sync, 'enqueue_polls', lambda: order.append('poll') or 0)

    assert reconcile_sync_task() == {'added': 0}
    assert order == ['reconcile', 'poll']
```

- [ ] **Step 2: Run it and check that it fails**

Run: `./run_tests.sh tests/test_discovery_sync_reconcile.py -v`
Expected: FAIL. `ImportError: cannot import name 'enqueue_polls'`.

- [ ] **Step 3: Implement**

Add to `app/discovery/sync.py`. Imports: `from datetime import timedelta`, `from app.activitypub.util import find_actor_or_create`, `from app.discovery import SYNC_NONE, SYNC_PENDING`, `from app.discovery.backfill import queue_backfill, run_backfill`, `from app.discovery.instance_actor import send_instance_follow, send_instance_undo`.

```python
ADDS_PER_HOST_PER_RUN = 10   # a larger N ramps up over several days rather than bursting at one host
FOLLOW_RETRY_DAYS = 7
POLL_SPACING_SECONDS = 2


def _host(url: str) -> str:
    return url.split('/')[2].lower() if url.count('/') >= 2 else ''


def drop_row(row) -> None:
    """Unfollow and forget a synced community. The community and its posts stay."""
    community = db.session.get(Community, row.community_id)
    if community is not None:
        send_instance_undo(row, community)
    db.session.delete(row)
    db.session.commit()


def _needs_refollow(row, now) -> bool:
    if row.follow_state == SYNC_NONE:
        return True
    return row.follow_state == SYNC_PENDING and (row.followed_at is None
                                                 or row.followed_at < now - timedelta(days=FOLLOW_RETRY_DAYS))


def _add(entry) -> bool:
    community = db.session.query(Community).filter(Community.ap_profile_id == entry.actor_url.lower()).first()
    if community is None:
        community = find_actor_or_create(entry.actor_url, community_only=True)
        if not isinstance(community, Community):
            current_app.logger.info(f'discovery sync: {entry.actor_url} did not resolve to a community')
            return False
    if not community.post_count:
        queue_backfill(community.id)
    row = DiscoverySync(community_id=community.id, entry_id=entry.id, follow_target=entry.actor_url)
    db.session.add(row)
    db.session.commit()
    send_instance_follow(row, community)
    return True


def reconcile_sync() -> dict:
    desired = desired_entries()
    wanted = {entry.actor_url.lower() for entries in desired.values() for entry in entries}
    summary = {'added': 0, 'dropped': 0, 'refollowed': 0, 'failed_hosts': []}
    rows = {row.community_id: row for row in db.session.query(DiscoverySync)}
    for row in list(rows.values()):
        community = db.session.get(Community, row.community_id)
        if _unusable(community) or community.ap_profile_id not in wanted:
            drop_row(row)
            del rows[row.community_id]
            summary['dropped'] += 1
    held = {db.session.get(Community, cid).ap_profile_id: row for cid, row in rows.items()}
    now = utcnow()
    for host in sorted(desired):
        try:
            added = 0
            for entry in desired[host]:
                row = held.get(entry.actor_url.lower())
                if row is not None:
                    if _needs_refollow(row, now):
                        send_instance_follow(row, db.session.get(Community, row.community_id))
                        summary['refollowed'] += 1
                elif added < ADDS_PER_HOST_PER_RUN and _add(entry):
                    added += 1
            summary['added'] += added
        except Exception:
            current_app.logger.exception(f'discovery sync: reconcile of {host} failed')
            db.session.rollback()
            summary['failed_hosts'].append(host)
    return summary


def enqueue_polls() -> int:
    """One poll per synced community; one host sees at most one poll every POLL_SPACING_SECONDS."""
    per_host = {}
    rows = db.session.query(DiscoverySync).order_by(DiscoverySync.community_id).all()
    for row in rows:
        slot = per_host.get(_host(row.follow_target), 0)
        per_host[_host(row.follow_target)] = slot + 1
        if current_app.debug:
            poll_synced_community(row.community_id)
        else:
            poll_synced_community.apply_async(args=[row.community_id], countdown=slot * POLL_SPACING_SECONDS)
    return len(rows)


@celery.task
def reconcile_sync_task() -> dict:
    summary = reconcile_sync()
    enqueue_polls()
    return summary
```

`_unusable` comes from Task 7. Coverage of `_host`'s `''` arm needs `test_host_of_a_malformed_target_is_empty`. Add it:

```python
def test_host_of_a_malformed_target_is_empty():
    assert sync._host('nohost') == '' and sync._host('https://Tube.Example/x') == 'tube.example'
```

`_needs_refollow` with `followed_at is None` and `pending`: add a parametrised case `(SYNC_PENDING, None, True)` by setting `row.followed_at = None` when `age_days is None`. Extend `test_follow_retry_rules` to handle `None`.

- [ ] **Step 4: Run it and check that it passes, with coverage**

Run: `./run_tests.sh tests/test_discovery_sync_reconcile.py tests/test_discovery_sync_poll.py tests/test_discovery_sync_desired.py --cov=app.discovery.sync --cov-branch --cov-report=term-missing -v`
Expected: PASS, 100%.

- [ ] **Step 5: Commit**

```bash
git add app/discovery/sync.py tests/test_discovery_sync_reconcile.py
git commit -m "feat: the daily reconcile follows each host's top N, unfollows the rest and spaces the polls (interop D24)"
```

---

### Task 9: `flask sync_discovery` and `daily.sh`

**Files:**
- Modify: `app/discovery/cli.py`
- Modify: `daily.sh`
- Test: `tests/test_discovery_cli.py` (append)

**Interfaces:**
- Consumes: `reconcile_sync()`, `enqueue_polls()`.
- Produces: CLI command `sync_discovery`, which prints `added: N`, `dropped: N`, `refollowed: N`, `failed hosts: a, b` and `polls queued: N`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_discovery_cli.py`)

```python
def test_sync_discovery_reconciles_then_queues_polls(app, db_session, monkeypatch):
    from app.discovery import cli as discovery_cli
    order = []
    monkeypatch.setattr(discovery_cli, 'reconcile_sync', lambda: order.append('reconcile') or
                        {'added': 2, 'dropped': 1, 'refollowed': 0, 'failed_hosts': ['bad.example']})
    monkeypatch.setattr(discovery_cli, 'enqueue_polls', lambda: order.append('poll') or 3)

    result = app.test_cli_runner().invoke(args=['sync_discovery'])

    assert result.exit_code == 0
    assert order == ['reconcile', 'poll']
    assert 'added: 2' in result.output and 'dropped: 1' in result.output
    assert 'failed hosts: bad.example' in result.output and 'polls queued: 3' in result.output


def test_daily_sh_syncs_after_refreshing_the_directory():
    lines = [line.strip() for line in open('daily.sh')]
    assert lines.index('flask sync_discovery') > lines.index('flask refresh_discovery')
```

- [ ] **Step 2: Run it and check that it fails**

Run: `./run_tests.sh tests/test_discovery_cli.py -v`
Expected: FAIL. The new tests fail: there is no such command, and `ValueError` from `lines.index`.

- [ ] **Step 3: Implement**

In `app/discovery/cli.py`, add `from app.discovery.sync import enqueue_polls, reconcile_sync` and, inside `register_discovery_commands`:

```python
    @app.cli.command('sync_discovery')
    def sync_discovery_command():
        """Keep each host's top PeerTube channels and Castopod podcasts followed by the instance actor, then queue
        their daily polls. Run after refresh_discovery."""
        summary = reconcile_sync()
        click.echo(f"added: {summary['added']}")
        click.echo(f"dropped: {summary['dropped']}")
        click.echo(f"refollowed: {summary['refollowed']}")
        click.echo(f"failed hosts: {', '.join(summary['failed_hosts'])}")
        click.echo(f'polls queued: {enqueue_polls()}')
```

Append `flask sync_discovery` as the last line of `daily.sh`.

- [ ] **Step 4: Run it and check that it passes**

Run: `./run_tests.sh tests/test_discovery_cli.py --cov=app.discovery.cli --cov-branch --cov-report=term-missing -v`
Expected: PASS, `cli.py` 100%.

- [ ] **Step 5: Commit**

```bash
git add app/discovery/cli.py daily.sh tests/test_discovery_cli.py
git commit -m "feat: flask sync_discovery runs the reconcile and queues the polls, daily after the directory refresh (interop D24)"
```

---

### Task 10: The admin Discovery tab replaces the pre-load with sync settings

**Files:**
- Modify: `app/discovery/forms.py`, `app/discovery/admin_views.py`
- Delete: `app/discovery/preload.py`, `app/templates/admin/_discovery_preload.html`, `tests/test_discovery_preload.py`
- Modify: `app/templates/admin/federation_discovery.html`
- Modify: `tests/test_discovery_backfill.py` (drop its `preload` imports and pre-load tests)
- Modify: `app/templates/admin/federation_preload.html`, if it links to the discovery pre-load. Check with `grep -n discovery app/templates/admin/federation_preload.html`.
- Test: `tests/test_discovery_admin_sync.py`; keep `tests/test_discovery_admin_page.py` green

**Interfaces:**
- Consumes: `sync_per_host()`, `sync_platforms()`, `reconcile_sync_task`, `DiscoverySync`, `set_setting`, `get_setting`.
- Produces: `DiscoverySyncForm` (fields `sync_per_host`, `sync_platforms`, `sync_external_search`, `sync_save`) and `DiscoverySyncNowForm` (`sync_now`).

- [ ] **Step 1: Write the failing test**

```python
"""Interop D24 proactive sync: the admin Discovery tab sets the sync up and shows what is synced."""
import pytest

from app import db
from app.discovery import SYNC_ACCEPTED, admin_views
from app.models import DiscoverySync, utcnow
from app.utils import get_setting, set_setting
from tests.discovery_fixtures import admin, fresh_cache  # noqa: F401
from tests.factories import make_community, make_instance

pytestmark = pytest.mark.usefixtures('site', 'fresh_cache')
PAGE = '/admin/federation/discovery'


def test_the_form_shows_the_defaults(admin):
    client, _ = admin
    html = client.get(PAGE).get_data(as_text=True)
    assert 'name="sync_per_host"' in html and 'value="0"' in html
    assert 'name="sync_external_search"' in html
    assert 'preload_count' not in html and 'preload_subscribe' not in html


def test_saving_stores_the_settings(admin):
    client, token = admin
    response = client.post(PAGE, data={'csrf_token': token, 'sync_per_host': '5', 'sync_platforms': ['castopod'],
                                       'sync_save': 'Save'})
    assert response.status_code == 302
    assert get_setting('discovery_sync_per_host') == 5
    assert get_setting('discovery_sync_platforms') == ['castopod']
    assert get_setting('discovery_external_search') is False   # unchecked box


@pytest.mark.parametrize('value', ['-1', '51', 'many'])
def test_an_out_of_range_count_is_refused_and_nothing_stored(admin, value):
    client, token = admin
    response = client.post(PAGE, data={'csrf_token': token, 'sync_per_host': value, 'sync_save': 'Save'})
    assert response.status_code == 200
    assert get_setting('discovery_sync_per_host') is None


def test_sync_now_queues_the_reconcile(admin, monkeypatch, app):
    client, token = admin
    queued = []
    monkeypatch.setattr(app, 'debug', False)
    monkeypatch.setattr(admin_views.reconcile_sync_task, 'delay', lambda: queued.append(True))

    response = client.post(PAGE, data={'csrf_token': token, 'sync_now': 'Sync now'})

    assert response.status_code == 302 and queued == [True]


def test_sync_now_in_debug_runs_inline(admin, monkeypatch, app):
    client, token = admin
    ran = []
    monkeypatch.setattr(app, 'debug', True)
    monkeypatch.setattr(admin_views, 'reconcile_sync_task', lambda: ran.append(True))

    client.post(PAGE, data={'csrf_token': token, 'sync_now': 'Sync now'})

    assert ran == [True]


def test_sync_now_without_csrf_is_refused(admin, monkeypatch):
    client, _ = admin
    queued = []
    monkeypatch.setattr(admin_views.reconcile_sync_task, 'delay', lambda: queued.append(True))
    client.post(PAGE, data={'sync_now': 'Sync now'})
    assert queued == []


def test_the_status_table_lists_each_synced_community(admin):
    client, _ = admin
    instance = make_instance('tube.example', software='peertube')
    community = make_community('zqchan', host='tube.example')
    community.instance_id = instance.id
    db.session.add(DiscoverySync(community_id=community.id, follow_target='https://tube.example/video-channels/zqchan',
                                 follow_state=SYNC_ACCEPTED, last_polled_at=utcnow(), last_error='outbox unreadable'))
    db.session.commit()

    html = client.get(PAGE).get_data(as_text=True)

    assert 'tube.example' in html and 'accepted' in html and 'outbox unreadable' in html


def test_an_empty_status_table_says_so(admin):
    client, _ = admin
    assert 'Nothing is synced yet.' in client.get(PAGE).get_data(as_text=True)
```

- [ ] **Step 2: Run it and check that it fails**

Run: `./run_tests.sh tests/test_discovery_admin_sync.py -v`
Expected: FAIL. `AttributeError: module 'app.discovery.admin_views' has no attribute 'reconcile_sync_task'`.

- [ ] **Step 3: Implement**

Replace `DiscoveryPreloadForm` in `app/discovery/forms.py` with:

```python
class DiscoverySyncForm(FlaskForm):
    sync_per_host = IntegerField(_l('Channels and podcasts to keep synced per server'), default=0,
                                 validators=[InputRequired(), NumberRange(min=0, max=50)])
    sync_platforms = MultiCheckboxField(_l('Platforms'), default=['peertube', 'castopod'],
                                        choices=[('peertube', _l('PeerTube channels')),
                                                 ('castopod', _l('Castopod podcasts'))])
    sync_external_search = BooleanField(_l('Let people include PeerTube videos from the wider network in search '
                                           '(their search text is sent to sepiasearch.org)'), default=True)
    sync_save = SubmitField(_l('Save'))


class DiscoverySyncNowForm(FlaskForm):
    sync_now = SubmitField(_l('Sync now'))
```

The imports become `from wtforms import BooleanField, IntegerField, SelectMultipleField, SubmitField` and `from wtforms.validators import InputRequired, NumberRange`.

Rewrite `app/discovery/admin_views.py`:

```python
"""The admin discovery page (interop D24): proactive sync settings, the sync status, and the data-source
attribution. Lives on the admin blueprint."""
from flask import current_app, flash, redirect, request, url_for
from flask_babel import _

from app import db
from app.admin import bp
from app.discovery.forms import DiscoverySyncForm, DiscoverySyncNowForm
from app.discovery.sync import reconcile_sync_task, sync_per_host, sync_platforms
from app.models import Community, DiscoverySync
from app.utils import get_setting, login_required, permission_required, render_template, roles_with, set_setting


@bp.route('/federation/discovery', methods=['GET', 'POST'])
@login_required
@permission_required('change instance settings')
def admin_federation_discovery():
    sync_form, now_form = DiscoverySyncForm(), DiscoverySyncNowForm()
    if request.method == 'POST' and now_form.sync_now.data and now_form.validate_on_submit():
        if current_app.debug:
            reconcile_sync_task()
        else:
            reconcile_sync_task.delay()
        flash(_('Sync started in the background.'))
        return redirect(url_for('admin.admin_federation_discovery'))
    if request.method == 'POST' and sync_form.sync_save.data and sync_form.validate_on_submit():
        set_setting('discovery_sync_per_host', sync_form.sync_per_host.data)
        set_setting('discovery_sync_platforms', sync_form.sync_platforms.data or [])
        set_setting('discovery_external_search', bool(sync_form.sync_external_search.data))
        flash(_('Discovery settings saved. They take effect at the next sync.'))
        return redirect(url_for('admin.admin_federation_discovery'))
    if request.method == 'GET':
        sync_form.sync_per_host.data = sync_per_host()
        sync_form.sync_platforms.data = sync_platforms()
        sync_form.sync_external_search.data = get_setting('discovery_external_search', True)
    synced = db.session.query(DiscoverySync, Community).join(Community, Community.id == DiscoverySync.community_id) \
        .order_by(Community.ap_domain, Community.name).all()
    return render_template('admin/federation_discovery.html', title=_('Federation settings - discovery'),
                           sync_form=sync_form, now_form=now_form, synced=synced,
                           roles_with=roles_with('change instance settings'))
```

Create `app/templates/admin/_discovery_sync.html`. Remember the forms note: field names are distinct, so a bare `csrf_token` field works.

```html
<h2 class="h4">{{ _('Keep channels and podcasts synced') }}</h2>
<p>{{ _('Each day this instance follows the most-followed PeerTube channels and Castopod podcasts on every server in the directory, up to the number below per server, and fetches their new posts. 0 turns this off.') }}</p>
<form method="post" class="mb-3">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    {{ render_field(sync_form.sync_per_host) }}
    {{ render_field(sync_form.sync_platforms) }}
    {{ render_field(sync_form.sync_external_search) }}
    {{ render_field(sync_form.sync_save) }}
</form>
<form method="post" class="mb-4">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    {{ render_field(now_form.sync_now, button_style='secondary') }}
</form>
<h3 class="h5">{{ _('Synced now') }}</h3>
{% if synced %}
<table class="table table-sm">
    <thead><tr><th>{{ _('Community') }}</th><th>{{ _('Server') }}</th><th>{{ _('Follow') }}</th><th>{{ _('Followed') }}</th><th>{{ _('Last poll') }}</th><th>{{ _('Last error') }}</th></tr></thead>
    <tbody>
    {% for row, community in synced %}
        <tr><td>{{ community.title }}</td><td>{{ community.ap_domain }}</td><td>{{ row.follow_state }}</td>
            <td>{{ row.followed_at or '' }}</td><td>{{ row.last_polled_at or '' }}</td><td>{{ row.last_error or '' }}</td></tr>
    {% endfor %}
    </tbody>
</table>
{% else %}
<p>{{ _('Nothing is synced yet.') }}</p>
{% endif %}
```

In `federation_discovery.html`, change the import to `{% from 'bootstrap5/form.html' import render_form, render_field %}`, and replace the `{% if preload_form is not none %}...{% endif %}` block with `{% include 'admin/_discovery_sync.html' %}`.

Delete `app/discovery/preload.py`, `app/templates/admin/_discovery_preload.html` and `tests/test_discovery_preload.py`. In `tests/test_discovery_backfill.py`, remove `preload` from `from app.discovery import backfill, preload, views`, remove `from app.discovery.preload import ...`, remove the `monkeypatch.setattr(preload, ...)` line in `queued`, and delete any test that calls `preload_discovered_communities`. Then:
- run `grep -rn "preload_discovered\|PRELOAD_USER_ID\|preload_candidates\|DiscoveryPreloadForm\|_discovery_preload" app tests docs/superpowers/specs`
- fix every hit in `app` and `tests`. Leave the specs alone: they are history.

`tests/test_discovery_admin_page.py::test_the_preload_page_links_here` checks that the lemmyverse pre-load page links to discovery. It stays as it is, because that page is untouched.

- [ ] **Step 4: Run it and check that it passes, with coverage**

Run: `./run_tests.sh tests/test_discovery_admin_sync.py tests/test_discovery_admin_page.py tests/test_discovery_backfill.py --cov=app.discovery.admin_views --cov=app.discovery.forms --cov-branch --cov-report=term-missing -v`
Expected: PASS, both modules 100%.

- [ ] **Step 5: Commit**

```bash
git add -A app/discovery app/templates/admin tests/test_discovery_admin_sync.py tests/test_discovery_backfill.py tests/test_discovery_preload.py
git commit -m "feat: the admin Discovery tab sets up proactive sync and shows its status; the user-1 pre-load is gone (interop D24)"
```

---

### Task 11: The media predicate and the "Videos & Podcasts" feed tab

**Files:**
- Create: `app/discovery/media.py`
- Modify: `app/discovery/__init__.py` (add `MEDIA_SOFTWARE`)
- Modify: `app/main/routes.py` (`home_page`, insert before `elif view_filter == 'all' or current_user.is_anonymous:`)
- Modify: `app/templates/_view_filter_nav.html`
- Test: `tests/test_discovery_media.py`, `tests/test_front_page_media_filter.py`

**Interfaces:**
- Produces:
  - `MEDIA_SOFTWARE = ('peertube', 'castopod')`
  - `media_community_clause()`: a SQLAlchemy boolean on `Community`
  - `media_post_clause()`: a SQLAlchemy boolean on `Post`
  - `MEDIA_COMMUNITY_SQL`: a raw fragment on alias `c`, for `home_page`
  - `platform_of(community) -> str | None`: `'peertube'`, `'castopod'` or None

- [ ] **Step 1: Write the failing tests**

`tests/test_discovery_media.py`:

```python
"""Interop D24: one predicate decides what is a video or podcast community."""
import pytest
from sqlalchemy import text

from app import db
from app.discovery.media import MEDIA_COMMUNITY_SQL, media_community_clause, media_post_clause, platform_of
from app.models import Community, Post
from tests.factories import make_community, make_instance, make_post, make_user

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def three(db_session):
    made = {}
    for software, host in (('PeerTube', 'tube.example'), ('castopod', 'pod.example'), ('lemmy', 'lemmy.example')):
        instance = make_instance(host, software=software)
        community = make_community(f'c_{software.lower()}', host=host)
        community.instance_id = instance.id
        author = make_user(instance, f'a_{software.lower()}')
        made[software.lower()] = (community, make_post(community, author, f'https://{host}/p/1'))
    db.session.commit()
    return made


def test_the_clause_matches_peertube_and_castopod_case_insensitively(three):
    assert {c.name for c in Community.query.filter(media_community_clause())} == {'c_peertube', 'c_castopod'}


def test_the_post_clause_matches_their_posts(three):
    assert {p.ap_id for p in Post.query.filter(media_post_clause())} == \
        {'https://tube.example/p/1', 'https://pod.example/p/1'}


def test_the_raw_fragment_selects_the_same_communities(three):
    rows = db.session.execute(text(f'SELECT c.name FROM community c WHERE {MEDIA_COMMUNITY_SQL}')).scalars()
    assert set(rows) == {'c_peertube', 'c_castopod'}


def test_platform_of(three):
    assert platform_of(three['peertube'][0]) == 'peertube'
    assert platform_of(three['castopod'][0]) == 'castopod'
    assert platform_of(three['lemmy'][0]) is None


def test_platform_of_a_community_with_no_instance_or_software(db_session):
    community = make_community('orphan', host='x.example')
    community.instance_id = None
    assert platform_of(community) is None
    instance = make_instance('blank.example', software=None)
    community.instance_id = instance.id
    db.session.commit()
    assert platform_of(community) is None
```

`tests/test_front_page_media_filter.py`. Reuse the harness of `tests/test_front_page_view_filters.py`:

```python
"""Interop D24: the Videos & Podcasts tab shows only PeerTube and Castopod posts, with All's guards."""
import pytest

from app import db
from tests.factories import make_community, make_community_member, make_instance, make_post, make_user
from tests.test_front_page_view_filters import _titles, env  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def media(env):
    tube = make_instance('tube.example', software='peertube')
    for name, private, show_all, low_quality in (('tube_open', False, True, False), ('tube_private', True, True, False),
                                                 ('tube_silenced', False, False, False), ('tube_low', False, True, True)):
        community = make_community(name, host='tube.example')
        community.instance_id = tube.id
        community.private, community.show_all, community.low_quality = private, show_all, low_quality
        make_post(community, env.author, f'https://tube.example/{name}/1', title=f'post in {name}')
        env.communities[name] = community
    db.session.commit()
    make_community_member(env.reader, env.communities['tube_private'])
    db.session.commit()
    return env


def test_anonymous_readers_get_public_media_posts_only(media):
    assert _titles(media, 'media') == ['post in tube_open']


def test_a_member_also_gets_their_private_media_community(media):
    assert _titles(media, 'media', user=media.reader) == ['post in tube_low', 'post in tube_open',
                                                         'post in tube_private']


def test_a_reader_hiding_low_quality_loses_it(media):
    media.reader.hide_low_quality = True
    db.session.commit()
    assert 'post in tube_low' not in _titles(media, 'media', user=media.reader)


def test_the_nav_offers_the_tab(media):
    html = media.app.test_client().get('/home/new/media').get_data(as_text=True)
    assert '/home/new/media' in html and 'Videos &amp; Podcasts' in html
```

- [ ] **Step 2: Run them and check that they fail**

Run: `./run_tests.sh tests/test_discovery_media.py tests/test_front_page_media_filter.py -v`
Expected: FAIL. `ModuleNotFoundError: No module named 'app.discovery.media'`.

- [ ] **Step 3: Implement**

Append to `app/discovery/__init__.py`:

```python
# Instance.software values whose communities are video channels or podcasts (lower-case)
MEDIA_SOFTWARE = ('peertube', 'castopod')
```

Create `app/discovery/media.py`:

```python
"""What counts as a video or podcast community (interop D24): one predicate for the Videos & Podcasts feed, the post
search filter and the community browse filter, so the three cannot drift. Imports only models, so the feed and search
modules can use it without a cycle."""
from sqlalchemy import func, select

from app.discovery import MEDIA_SOFTWARE
from app.models import Community, Instance, Post

_SOFTWARE_LIST = ', '.join(f"'{software}'" for software in MEDIA_SOFTWARE)   # constants, never user input
# home_page builds its community filter as SQL on alias `c`
MEDIA_COMMUNITY_SQL = f'c.instance_id IN (SELECT id FROM instance WHERE lower(software) IN ({_SOFTWARE_LIST}))'


def _media_instance_ids():
    return select(Instance.id).where(func.lower(Instance.software).in_(MEDIA_SOFTWARE))


def media_community_clause():
    return Community.instance_id.in_(_media_instance_ids())


def media_post_clause():
    return Post.community_id.in_(select(Community.id).where(media_community_clause()))


def platform_of(community):
    instance = community.instance if community.instance_id else None
    software = (instance.software or '').lower() if instance is not None else ''
    return software if software in MEDIA_SOFTWARE else None
```

In `home_page` (`app/main/routes.py`), add `from app.discovery.media import MEDIA_COMMUNITY_SQL` at the top, then insert before `elif view_filter == 'all' or current_user.is_anonymous:`:

```python
    elif view_filter == 'media':   # D24: PeerTube and Castopod only, a subset of All
        if current_user.is_anonymous:
            community_sql = f'{MEDIA_COMMUNITY_SQL} AND c.show_all is true AND c.private is false AND c.low_quality is false'
        else:
            community_sql = f'(c.private is false OR c.id IN {private_communities}) AND c.show_all is true AND {MEDIA_COMMUNITY_SQL} {low_quality_filter}'
        community_ids = [0]
```

In `_view_filter_nav.html`, add after the Popular `<option>`:

```html
    <option value="/home/{{ sort }}/media" aria-label="{{ _('Videos and podcasts') }}" {{ 'selected="selected"'|safe  if view_filter == 'media' }}>{{ _('Videos & Podcasts') }}</option>
```

Add after the Popular `<a>` button:

```html
</a><a href="/home/{{ sort }}/media" class="btn {{ 'btn-primary' if view_filter == 'media' else 'btn-outline-secondary' }}" rel="nofollow noindex" title="{{ _('Videos and podcasts') }}">
      {{ _('Videos & Podcasts') }}
```

Keep the `</a><a` chaining that the template already uses, so no whitespace appears between the buttons.

- [ ] **Step 4: Run them and check that they pass, with coverage**

Run: `./run_tests.sh tests/test_discovery_media.py tests/test_front_page_media_filter.py tests/test_front_page_view_filters.py tests/test_import_order.py --cov=app.discovery.media --cov-branch --cov-report=term-missing -v`
Expected: PASS, `media.py` 100%. The new `home_page` arm is covered both anonymous and logged in.

- [ ] **Step 5: Commit**

```bash
git add app/discovery/__init__.py app/discovery/media.py app/main/routes.py app/templates/_view_filter_nav.html tests/test_discovery_media.py tests/test_front_page_media_filter.py
git commit -m "feat: a Videos & Podcasts feed tab shows PeerTube and Castopod posts (interop D24)"
```

---

### Task 12: The post-search "Videos & Podcasts only" filter, kept across pages

**Files:**
- Modify: `app/search/routes.py`
- Modify: `app/templates/search/_search_form.html`
- Test: `tests/test_search_media_filter.py`

**Interfaces:**
- Consumes: `media_post_clause()`.
- Produces: query arg `media=1`. `next_url` and `prev_url` carry `media` and `external`.

- [ ] **Step 1: Write the failing test**

```python
"""Interop D24: post search can be narrowed to videos and podcasts, and the narrowing survives paging."""
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.models import Site
from tests.factories import make_community, make_instance, make_post, make_user

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def posts(app, db_session):
    g.site = db.session.get(Site, 1)
    for software, host in (('peertube', 'tube.example'), ('lemmy', 'lemmy.example')):
        instance = make_instance(host, software=software)
        community = make_community(f'c_{software}', host=host)
        community.instance_id = instance.id
        make_post(community, make_user(instance, f'a_{software}'), f'https://{host}/p/1', title=f'zqword {software}')
    db.session.commit()
    return app


def captured(app, **args):
    seen = {}
    with patch('app.search.routes.render_template', side_effect=lambda t, **k: seen.update(k) or 'ok'):
        assert app.test_client().get('/search', query_string=args).status_code == 200
    return seen


def test_media_keeps_only_peertube_and_castopod_posts(posts):
    seen = captured(posts, q='zqword', media='1')
    assert [p.title for p in seen['posts'].items] == ['zqword peertube']
    assert seen['media'] is True


def test_media_alone_runs_a_search(posts):
    assert [p.title for p in captured(posts, media='1')['posts'].items] == ['zqword peertube']


def test_without_media_both_come_back(posts):
    assert sorted(p.title for p in captured(posts, q='zqword')['posts'].items) == ['zqword lemmy', 'zqword peertube']


def test_the_form_has_the_checkbox(posts):
    html = posts.test_client().get('/search?q=zqword&media=1').get_data(as_text=True)
    assert 'name="media"' in html and 'checked' in html


def test_paging_links_keep_media_and_external(posts):
    """Anonymous page size is 50 (hardcoded in run_search), so 52 matching posts give a second page."""
    instance = make_instance('tube2.example', software='peertube')
    community = make_community('c_tube2', host='tube2.example')
    community.instance_id = instance.id
    author = make_user(instance, 'a_tube2')
    for i in range(51):
        make_post(community, author, f'https://tube2.example/p/{i}', title=f'zqword {i}')
    db.session.commit()

    seen = captured(posts, q='zqword', media='1', external='1')

    assert 'media=1' in seen['next_url'] and 'external=1' in seen['next_url']
```

- [ ] **Step 2: Run it and check that it fails**

Run: `./run_tests.sh tests/test_search_media_filter.py -v`
Expected: FAIL. `KeyError: 'media'`, or the lemmy post is present.

- [ ] **Step 3: Implement**

In `run_search`, add `from app.discovery.media import media_post_clause` to the imports, then:

- After `minimum_upvote_value = ...`:
  ```python
  media = request.args.get('media', '') == '1'          # D24: videos and podcasts only
  external = request.args.get('external', '') == '1'    # D24: also ask the wider network (Task 14)
  ```
- Add `or media` to the condition `if q != '' or type != 0 ...`.
- In the posts branch, next to `if software:`:
  ```python
  if media:
      posts = posts.filter(media_post_clause())
  ```
- Change both posts paging URLs to carry the flags:
  ```python
  flags = {'media': '1' if media else None, 'external': '1' if external else None}
  next_url = url_for('search.run_search', page=posts.next_num, q=q, **flags) if posts.has_next else None
  prev_url = url_for('search.run_search', page=posts.prev_num, q=q, **flags) if posts.has_prev and page != 1 else None
  ```
- Pass `media=media, external=external` to the `results.html` `render_template`.

In `_search_form.html`, after the keyword `form-group`:

```html
    <div class="form-check mt-2">
        <input class="form-check-input" type="checkbox" id="media" name="media" value="1" {{ 'checked' if media else '' }}>
        <label class="form-check-label" for="media">{{ _('Videos & Podcasts only') }}</label>
    </div>
```

`start.html` renders the form without `media`, and Jinja treats an undefined `media` as falsy, so the start page needs no change. Check that `search/start.html` still renders: `test_search_routes.py` covers it.

- [ ] **Step 4: Run it and check that it passes**

Run: `./run_tests.sh tests/test_search_media_filter.py tests/test_search_routes.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/search/routes.py app/templates/search/_search_form.html tests/test_search_media_filter.py
git commit -m "feat: post search can be narrowed to videos and podcasts, and paging keeps it (interop D24)"
```

---

### Task 13: External video search module

**Files:**
- Modify: `app/discovery/sources.py` (`fetch_json` gains `max_seconds`)
- Create: `app/discovery/external_search.py`
- Test: `tests/test_discovery_external_search.py`; existing source tests stay green

**Interfaces:**
- Consumes: `sources.fetch_json(url, params, headers, max_bytes, max_seconds=15)`, `host_is_excluded`, `cache`, `Post`.
- Produces:
  - `search_videos(q: str, allow_nsfw: bool) -> list[dict]`. Each dict has the keys `url`, `title`, `channel`, `host`, `nsfw`. At most 10.
  - `video_from(raw) -> dict | None`
  - constants `SEPIASEARCH_VIDEOS_URL`, `RESULT_LIMIT = 10`, `TIMEOUT_SECONDS = 3`, `CACHE_SECONDS = 600`

- [ ] **Step 1: Write the failing test**

```python
"""Interop D24: an opt-in search of PeerTube videos across the network, through SepiaSearch."""
import pytest

from app import db
from app.discovery import external_search, sources
from app.discovery.external_search import CACHE_SECONDS, search_videos, video_from
from tests.discovery_fixtures import fresh_cache  # noqa: F401
from tests.factories import make_banned_instance, make_community, make_instance, make_post, make_user

pytestmark = pytest.mark.usefixtures('site', 'fresh_cache')


def raw(n=1, host='tube.example', nsfw=False, **over):
    video = {'url': f'https://{host}/videos/watch/{n}', 'name': f'Video {n}', 'nsfw': nsfw,
             'channel': {'displayName': 'Chan', 'host': host}}
    video.update(over)
    return video


@pytest.fixture
def answers(monkeypatch):
    calls = []

    def fetch(url, params=None, headers=None, max_bytes=None, max_seconds=15):
        calls.append(dict(url=url, params=params, max_seconds=max_seconds))
        return answers.payload
    answers.payload = {'data': [raw(1), raw(2)]}
    monkeypatch.setattr(external_search.sources, 'fetch_json', fetch)
    return calls


def test_a_search_asks_sepiasearch_with_a_3_second_cap(db_session, answers):
    videos = search_videos('  Cats ', allow_nsfw=False)

    assert [v['title'] for v in videos] == ['Video 1', 'Video 2']
    assert answers[0]['url'] == 'https://sepiasearch.org/api/v1/search/videos'
    assert answers[0]['params'] == {'search': 'Cats', 'count': 10} and answers[0]['max_seconds'] == 3


def test_the_same_query_differently_spaced_or_cased_is_served_from_cache(db_session, answers):
    search_videos('cats', allow_nsfw=False)
    search_videos(' CATS ', allow_nsfw=False)
    assert len(answers) == 1


def test_a_failure_is_not_cached_and_gives_nothing(db_session, answers):
    answers.payload = None
    assert search_videos('cats', allow_nsfw=False) == []
    answers.payload = {'data': [raw(1)]}
    assert len(search_videos('cats', allow_nsfw=False)) == 1


@pytest.mark.parametrize('payload', [None, [], {'data': 'x'}, {'nodata': []}])
def test_unreadable_payloads_give_nothing(db_session, answers, payload):
    answers.payload = payload
    assert search_videos('cats', allow_nsfw=False) == []


def test_an_empty_query_asks_nothing(db_session, answers):
    assert search_videos('   ', allow_nsfw=False) == [] and answers == []


def test_nsfw_needs_permission(db_session, answers):
    answers.payload = {'data': [raw(1, nsfw=True), raw(2)]}
    assert [v['title'] for v in search_videos('x', allow_nsfw=False)] == ['Video 2']
    assert [v['title'] for v in search_videos('x', allow_nsfw=True)] == ['Video 1', 'Video 2']


def test_banned_hosts_are_dropped(db_session, answers):
    answers.payload = {'data': [raw(1, host='bad.example'), raw(2)]}
    make_banned_instance('bad.example')
    assert [v['title'] for v in search_videos('x', allow_nsfw=False)] == ['Video 2']


def test_videos_already_stored_here_are_dropped(db_session, answers):
    instance = make_instance('tube.example', software='peertube')
    community = make_community('chan', host='tube.example')
    make_post(community, make_user(instance, 'author'), 'https://tube.example/videos/watch/1')
    assert [v['title'] for v in search_videos('x', allow_nsfw=False)] == ['Video 2']


def test_at_most_ten(db_session, answers):
    answers.payload = {'data': [raw(i) for i in range(15)]}
    assert len(search_videos('x', allow_nsfw=False)) == 10


@pytest.mark.parametrize('bad', [
    'not a dict',
    raw(url='http://tube.example/videos/watch/1'),              # not https
    raw(url='https://elsewhere.example/videos/watch/1'),        # Review Focus 5: url host != channel host
    raw(channel={'host': 'Not A Host'}),
    raw(channel='x'),
    raw(url=5),
    raw(name=''),
    raw(name=None),
    raw(url='https://[::1/x')])
def test_malformed_rows_are_dropped(bad):
    assert video_from(bad) is None


def test_a_good_row(db_session):
    assert video_from(raw(3, nsfw='yes')) == {'url': 'https://tube.example/videos/watch/3', 'title': 'Video 3',
                                             'channel': 'Chan', 'host': 'tube.example', 'nsfw': False}


def test_a_row_with_no_channel_name_uses_the_host(db_session):
    assert video_from(raw(channel={'host': 'tube.example'}))['channel'] == 'tube.example'


def test_fetch_json_passes_max_seconds_to_the_capped_get(monkeypatch, app):
    seen = {}
    monkeypatch.setattr(sources, 'get_request_capped',
                        lambda url, max_bytes, headers=None, max_seconds=15: seen.update(s=max_seconds) or (200, b'{}'))
    with app.app_context():
        assert sources.fetch_json('https://x.example/', max_seconds=3) == {}
    assert seen['s'] == 3
```

- [ ] **Step 2: Run it and check that it fails**

Run: `./run_tests.sh tests/test_discovery_external_search.py -v`
Expected: FAIL. `ImportError: cannot import name 'external_search'`.

- [ ] **Step 3: Implement**

In `app/discovery/sources.py`, change the `fetch_json` signature to add `max_seconds: float = 15`, and the call to `get_request_capped(target, max_bytes, headers=dict(headers or {}), max_seconds=max_seconds)`. Add to the docstring: "`max_seconds` caps the whole read (15 unless a caller needs less)."

Create `app/discovery/external_search.py`:

```python
"""Search PeerTube videos across the network through SepiaSearch (interop D24). Only when the viewer ticks "Include
results from the wider network" and the admin allows it: the search text leaves this server. Results are cached ten
minutes per query; a failure shows nothing and is not cached."""
import hashlib
from urllib.parse import urlparse

from app import cache, db
from app.discovery import sources
from app.discovery.filters import host_is_excluded
from app.models import Post

SEPIASEARCH_VIDEOS_URL = 'https://sepiasearch.org/api/v1/search/videos'
RESULT_LIMIT = 10
TIMEOUT_SECONDS = 3
CACHE_SECONDS = 600
MAX_BYTES = 512 * 1024


def video_from(raw) -> dict | None:
    """One SepiaSearch video, or None. Its url must be https on the host its channel names, so a row cannot send a
    click to a third party."""
    if not isinstance(raw, dict) or not isinstance(raw.get('channel'), dict):
        return None
    url, host, title = raw.get('url'), raw['channel'].get('host'), raw.get('name')
    if not isinstance(url, str) or not isinstance(host, str) or not sources.is_hostname(host.lower()):
        return None
    if not isinstance(title, str) or not title.strip():
        return None
    try:
        parsed = urlparse(url)
        url_host = parsed.hostname
    except ValueError:
        return None
    if parsed.scheme != 'https' or url_host != host.lower():
        return None
    return {'url': url, 'title': title.strip(), 'channel': sources.display_name(raw['channel'].get('displayName'),
                                                                              host.lower()),
            'host': host.lower(), 'nsfw': raw.get('nsfw') is True}


def _cache_key(q: str) -> str:
    return 'discovery:videos:' + hashlib.sha1(q.lower().encode()).hexdigest()


def _videos_for(q: str) -> list:
    key = _cache_key(q)
    cached = cache.get(key)
    if cached is not None:
        return cached
    payload = sources.fetch_json(SEPIASEARCH_VIDEOS_URL, params={'search': q, 'count': RESULT_LIMIT},
                                 max_bytes=MAX_BYTES, max_seconds=TIMEOUT_SECONDS)
    data = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    videos = [video for video in map(video_from, data) if video is not None]
    cache.set(key, videos, timeout=CACHE_SECONDS)
    return videos


def search_videos(q: str, allow_nsfw: bool) -> list:
    q = (q or '').strip()
    if not q:
        return []
    videos = [v for v in _videos_for(q) if (allow_nsfw or not v['nsfw']) and not host_is_excluded(v['host'], frozenset())]
    if videos:
        stored = {ap_id.lower() for (ap_id,) in db.session.query(Post.ap_id).filter(
            db.func.lower(Post.ap_id).in_([v['url'].lower() for v in videos]))}
        videos = [v for v in videos if v['url'].lower() not in stored]
    return videos[:RESULT_LIMIT]
```

Note on `raw(name=None)`: `video_from` refuses `None` titles. `nsfw='yes'` is not `True`, so it counts as not NSFW: only a real boolean `true` from SepiaSearch marks a video NSFW.

- [ ] **Step 4: Run it and check that it passes, with coverage**

Run: `./run_tests.sh tests/test_discovery_external_search.py tests/test_discovery_peertube.py tests/test_discovery_castopod_source.py tests/test_discovery_refresh.py --cov=app.discovery.external_search --cov=app.discovery.sources --cov-branch --cov-report=term-missing -v`
Expected: PASS, `external_search.py` 100%, and the changed `sources.py` lines covered.

- [ ] **Step 5: Commit**

```bash
git add app/discovery/sources.py app/discovery/external_search.py tests/test_discovery_external_search.py
git commit -m "feat: PeerTube videos across the network can be searched through SepiaSearch, cached and capped (interop D24)"
```

---

### Task 14: The search page toggle and the "From the wider network" block

**Files:**
- Modify: `app/search/routes.py`
- Modify: `app/templates/search/_search_form.html`, `app/templates/search/results.html`
- Create: `app/templates/discovery/_wider_videos.html`
- Test: `tests/test_search_external_toggle.py`

**Interfaces:**
- Consumes: `search_videos(q, allow_nsfw)`, `viewer_allows_nsfw(user, site)` from `app.discovery.search`, `get_setting('discovery_external_search', True)`.
- Produces: template variables `external_allowed: bool`, `external: bool`, `wider_videos: list[dict]`.

- [ ] **Step 1: Write the failing test**

```python
"""Interop D24: the search page's opt-in "Include results from the wider network"."""
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.models import Site
from app.search import routes
from app.utils import set_setting
from tests.discovery_fixtures import fresh_cache  # noqa: F401

pytestmark = pytest.mark.usefixtures('site', 'fresh_cache')
VIDEO = {'url': 'https://tube.example/videos/watch/1', 'title': 'Zq video', 'channel': 'Chan',
         'host': 'tube.example', 'nsfw': False}


@pytest.fixture
def asked(app, db_session, monkeypatch):
    g.site = db.session.get(Site, 1)
    calls = []
    monkeypatch.setattr(routes, 'search_videos', lambda q, allow_nsfw: calls.append((q, allow_nsfw)) or [VIDEO])
    return calls


def page(app, **args):
    return app.test_client().get('/search', query_string=args).get_data(as_text=True)


def test_off_by_default_nothing_leaves_the_server(app, asked):
    html = page(app, q='zq')
    assert asked == [] and 'From the wider network' not in html
    assert 'name="external"' in html and 'id="external" name="external" value="1" checked' not in html


def test_on_shows_the_block_below_local_results(app, asked):
    html = page(app, q='zq', external='1')
    assert asked == [('zq', False)]
    assert 'From the wider network' in html and 'Zq video' in html
    assert html.index('From the wider network') > html.index('id="search_term"')


def test_anonymous_viewers_get_a_plain_link(app, asked):
    html = page(app, q='zq', external='1')
    assert 'href="https://tube.example/videos/watch/1"' in html and 'video/resolve' not in html


def test_the_admin_kill_switch_hides_the_toggle_and_ignores_the_flag(app, asked):
    set_setting('discovery_external_search', False)
    html = page(app, q='zq', external='1')
    assert asked == [] and 'name="external"' not in html and 'From the wider network' not in html


def test_only_page_one_of_a_post_search_asks(app, asked):
    page(app, q='zq', external='1', page=2)
    page(app, q='zq', external='1', search_for='comments')
    assert asked == []


def test_no_query_asks_nothing(app, asked):
    page(app, external='1', media='1')
    assert asked == []


def test_nothing_found_shows_no_block(app, monkeypatch, db_session):
    g.site = db.session.get(Site, 1)
    monkeypatch.setattr(routes, 'search_videos', lambda q, allow_nsfw: [])
    assert 'From the wider network' not in page(app, q='zq', external='1')
```

Add one logged-in test. It uses the `login`/`csrf` helpers from `tests/test_admin_federation.py` and a local user with `hide_nsfw = 0` on a site with `enable_nsfw = True`. Assert that `asked == [('zq', True)]`, and that the HTML contains `action="/discovery/video/resolve"` and `name="csrf_token"`:

```python
def test_a_logged_in_viewer_gets_a_resolve_button_and_their_nsfw_choice(app, asked):
    from tests.factories import make_instance, make_user
    from tests.test_admin_federation import login
    site = db.session.get(Site, 1)
    site.enable_nsfw = True
    user = make_user(make_instance('test.piefed.local', software='piefed'), 'zqviewer', local=True)
    user.verified, user.hide_nsfw = True, 0
    db.session.commit()
    client = app.test_client()
    login(client, user)

    html = client.get('/search', query_string={'q': 'zq', 'external': '1'}).get_data(as_text=True)

    assert asked == [('zq', True)]
    assert 'action="/discovery/video/resolve"' in html and 'name="csrf_token"' in html
```

- [ ] **Step 2: Run it and check that it fails**

Run: `./run_tests.sh tests/test_search_external_toggle.py -v`
Expected: FAIL. `AttributeError: <module 'app.search.routes'> has no attribute 'search_videos'`.

- [ ] **Step 3: Implement**

In `app/search/routes.py`, add the imports `from app.discovery.external_search import search_videos` and `from app.discovery.search import viewer_allows_nsfw`. Change the Task 12 line to:

```python
    external_allowed = get_setting('discovery_external_search', True)
    external = external_allowed and request.args.get('external', '') == '1'   # D24: off unless ticked
```

In the posts branch, after paging:

```python
            if external and q and page == 1:
                wider_videos = search_videos(q, allow_nsfw=viewer_allows_nsfw(current_user, g.site))
```

Initialise `wider_videos = []` next to `posts = None`. Pass `external_allowed=external_allowed, external=external, wider_videos=wider_videos` to `results.html`, and `external_allowed=external_allowed` to `start.html`.

In `_search_form.html`, after the media checkbox:

```html
    {% if external_allowed %}
    <div class="form-check">
        <input class="form-check-input" type="checkbox" id="external" name="external" value="1" {{ 'checked' if external else '' }}>
        <label class="form-check-label" for="external">{{ _('Include results from the wider network') }}</label>
        <div class="form-text">{{ _('Your search text is sent to sepiasearch.org to find PeerTube videos on other servers.') }}</div>
    </div>
    {% endif %}
```

Create `app/templates/discovery/_wider_videos.html`:

```html
{% if wider_videos %}
<section class="wider_videos mt-4" aria-labelledby="wider_videos_heading">
    <h2 id="wider_videos_heading" class="h5">{{ _('From the wider network') }}</h2>
    <p class="text-muted small">{{ _('PeerTube videos not on this server yet, found through SepiaSearch.') }}</p>
    <ul class="list-unstyled">
    {% for video in wider_videos %}
        <li class="d-flex align-items-center gap-2 mb-2">
            <span class="badge text-bg-secondary platform_badge">PeerTube</span>
            {% if current_user.is_authenticated %}
            <span class="fw-semibold text-break">{{ video.title }}</span>
            <small class="text-muted">{{ video.channel }} · {{ video.host }}</small>
            <form method="post" action="{{ url_for('main.discovery_video_resolve') }}" class="ms-auto">
                <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                <input type="hidden" name="url" value="{{ video.url }}">
                <input type="hidden" name="q" value="{{ q or '' }}">
                <button type="submit" class="btn btn-sm btn-primary">{{ _('Open here') }}</button>
            </form>
            {% else %}
            <a class="fw-semibold text-break" href="{{ video.url }}" rel="nofollow noopener">{{ video.title }}</a>
            <small class="text-muted">{{ video.channel }} · {{ video.host }}</small>
            {% endif %}
        </li>
    {% endfor %}
    </ul>
</section>
{% endif %}
```

In `results.html`, include it after the posts list's closing block and before the paging links: `{% include 'discovery/_wider_videos.html' %}`. `url_for('main.discovery_video_resolve')` only exists after Task 15. **Do Task 15 first if you run this task's logged-in test on its own**, or run Tasks 14 and 15 back to back and commit them separately.

- [ ] **Step 4: Run it and check that it passes**

Run: `./run_tests.sh tests/test_search_external_toggle.py tests/test_search_media_filter.py tests/test_search_routes.py -v`
Expected: PASS. Every new line in `run_search` is hit: check with `--cov=app.search.routes --cov-branch --cov-report=term-missing`.

- [ ] **Step 5: Commit**

```bash
git add app/search/routes.py app/templates/search app/templates/discovery/_wider_videos.html tests/test_search_external_toggle.py
git commit -m "feat: search can include PeerTube videos from the wider network, off unless the viewer ticks it (interop D24)"
```

---

### Task 15: Opening a wider-network video

**Files:**
- Modify: `app/discovery/views.py`
- Test: `tests/test_discovery_video_resolve.py`

**Interfaces:**
- Consumes: `resolve_remote_post_from_search(uri) -> Post | None` (`app/activitypub/util.py:5323`), `host_is_excluded`, `is_hostname`, `host_of`.
- Produces: POST `/discovery/video/resolve` (endpoint `main.discovery_video_resolve`), form fields `url` and `q`.

- [ ] **Step 1: Write the failing test**

```python
"""Interop D24: a logged-in viewer opens a wider-network video here, through the authenticated resolve path."""
from types import SimpleNamespace

import pytest
from flask import g

from app import db
from app.discovery import views
from app.models import Site
from tests.discovery_fixtures import fresh_cache  # noqa: F401
from tests.factories import make_banned_instance, make_community, make_instance, make_post, make_user
from tests.test_admin_federation import csrf, login

pytestmark = pytest.mark.usefixtures('fresh_cache')
URL = 'https://tube.example/videos/watch/1'


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    user = api_baseline.user1
    user.verified = True
    db.session.commit()
    client = app.test_client()
    login(client, user)
    return SimpleNamespace(client=client, token=csrf(app, client), user=user)


def test_a_video_that_resolves_opens_its_post(env, monkeypatch):
    instance = make_instance('tube.example', software='peertube')
    post = make_post(make_community('chan', host='tube.example'), make_user(instance, 'author'), URL)
    monkeypatch.setattr(views, 'resolve_remote_post_from_search', lambda uri: post if uri == URL else None)

    response = env.client.post('/discovery/video/resolve', data={'csrf_token': env.token, 'url': URL, 'q': 'zq'})

    assert response.status_code == 302 and response.headers['Location'].endswith(f'/post/{post.id}')


def test_a_video_that_does_not_resolve_goes_back_to_the_search(env, monkeypatch):
    monkeypatch.setattr(views, 'resolve_remote_post_from_search', lambda uri: None)

    response = env.client.post('/discovery/video/resolve', data={'csrf_token': env.token, 'url': URL, 'q': 'zq'})

    assert response.status_code == 302
    assert '/search' in response.headers['Location'] and 'q=zq' in response.headers['Location']
    assert 'external=1' in response.headers['Location']


@pytest.mark.parametrize('url', ['', 'http://tube.example/v/1', 'https://not a host/v', 'ftp://x.example/v', None])
def test_a_bad_url_is_400(env, monkeypatch, url):
    monkeypatch.setattr(views, 'resolve_remote_post_from_search', lambda uri: pytest.fail('fetched'))
    data = {'csrf_token': env.token, 'q': 'zq'}
    if url is not None:
        data['url'] = url
    assert env.client.post('/discovery/video/resolve', data=data).status_code == 400


def test_a_banned_host_is_not_fetched(env, monkeypatch):
    make_banned_instance('tube.example')
    monkeypatch.setattr(views, 'resolve_remote_post_from_search', lambda uri: pytest.fail('fetched'))

    response = env.client.post('/discovery/video/resolve', data={'csrf_token': env.token, 'url': URL})

    assert response.status_code == 302 and '/search' in response.headers['Location']


def test_without_csrf_nothing_is_fetched(env, monkeypatch):
    monkeypatch.setattr(views, 'resolve_remote_post_from_search', lambda uri: pytest.fail('fetched'))
    response = env.client.post('/discovery/video/resolve', data={'url': URL})
    assert response.status_code in (302, 400)


def test_anonymous_viewers_are_sent_to_log_in(app, db_session, monkeypatch):
    monkeypatch.setattr(views, 'resolve_remote_post_from_search', lambda uri: pytest.fail('fetched'))
    response = app.test_client().post('/discovery/video/resolve', data={'url': URL})
    assert response.status_code == 302 and 'login' in response.headers['Location']
```

Before writing `test_without_csrf_nothing_is_fetched`, check what `login_required` does on a bad CSRF token: `grep -n "csrf" app/utils.py | head`. Then pin the exact status it returns. Do not keep `in (302, 400)`.

- [ ] **Step 2: Run it and check that it fails**

Run: `./run_tests.sh tests/test_discovery_video_resolve.py -v`
Expected: FAIL with a 404 or 405 on `/discovery/video/resolve`.

- [ ] **Step 3: Implement**

In `app/discovery/views.py`, add the imports `from app.activitypub.util import find_actor_or_create, host_of, resolve_remote_post_from_search` (merge them with the existing line) and `from flask_babel import _` (already there).

```python
@bp.route('/discovery/video/resolve', methods=['POST'])
@login_required
def discovery_video_resolve():
    """Open a wider-network search result here (interop D24): fetch the video through the authenticated resolve path
    (PERM-1) and go to its post. The url came from the viewer's form, so it is checked like any input."""
    url = (request.form.get('url') or '').strip()
    q = (request.form.get('q') or '').strip() or None
    host = host_of(url).lower() if url else ''
    if not url.startswith('https://') or not is_hostname(host):
        abort(400)
    back = redirect(url_for('search.run_search', q=q, external='1'))
    if host_is_excluded(host, frozenset()):
        flash(_('That video is on an instance this site does not federate with.'), 'warning')
        return back
    post = resolve_remote_post_from_search(url)
    if post is None:
        flash(_("Couldn't reach that server. Please try again later."), 'warning')   # D720
        return back
    return redirect(url_for('activitypub.post_ap', post_id=post.id))
```

- [ ] **Step 4: Run it and check that it passes, with coverage**

Run: `./run_tests.sh tests/test_discovery_video_resolve.py tests/test_search_external_toggle.py --cov=app.discovery.views --cov-branch --cov-report=term-missing -v`
Expected: PASS, `views.py` 100%.

- [ ] **Step 5: Commit**

```bash
git add app/discovery/views.py tests/test_discovery_video_resolve.py
git commit -m "feat: a logged-in viewer opens a wider-network video here through the resolve path (interop D24)"
```

---

### Task 16: Community browse platform filter and badge

**Files:**
- Modify: `app/main/routes.py` (`list_communities`, ~line 313; filter next to `if instance:`)
- Modify: the community list template that `list_communities` renders (find it: `grep -n "render_template" app/main/routes.py | sed -n '/list_communities/,+0p'`, then the template's filter form)
- Modify: the community list row partial. Find it with `grep -rln "community.title" app/templates/list_communities*.html app/templates/_community_list*.html 2>/dev/null`.
- Test: `tests/test_community_browse_platform.py`

**Interfaces:**
- Consumes: `media_community_clause()`, `platform_of(community)`, `MEDIA_SOFTWARE`.
- Produces: query arg `platform` (`''`, `peertube` or `castopod`, anything else ignored), and the template global `community_platform` (`platform_of`), registered in `app/discovery/views.py`.

- [ ] **Step 1: Write the failing test**

```python
"""Interop D24: the community list can show only PeerTube or Castopod communities, and badges them."""
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.models import Site
from tests.factories import make_community, make_instance

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def listed(app, db_session):
    g.site = db.session.get(Site, 1)
    for software, host in (('peertube', 'tube.example'), ('castopod', 'pod.example'), ('lemmy', 'lemmy.example')):
        instance = make_instance(host, software=software)
        community = make_community(f'c_{software}', host=host)
        community.instance_id = instance.id
        community.ap_id = f'c_{software}@{host}'
    db.session.commit()
    return app


def names(app, **args):
    seen = {}
    with patch('app.main.routes.render_template', side_effect=lambda t, **k: seen.update(k) or 'ok'):
        assert app.test_client().get('/communities', query_string=args).status_code == 200
    return sorted(c.name for c in seen['communities'].items), seen


@pytest.mark.parametrize('platform, expected', [
    ('peertube', ['c_peertube']), ('castopod', ['c_castopod'])])
def test_a_platform_keeps_only_its_communities(listed, platform, expected):
    assert [n for n in names(listed, platform=platform)[0] if n.startswith('c_')] == expected


@pytest.mark.parametrize('platform', ['', 'lemmy', 'x'])
def test_no_or_unknown_platform_filters_nothing(listed, platform):
    found = names(listed, platform=platform)[0]
    assert {'c_peertube', 'c_castopod', 'c_lemmy'} <= set(found)


def test_the_page_badges_media_communities_and_offers_the_filter(listed):
    html = listed.test_client().get('/communities').get_data(as_text=True)
    assert 'name="platform"' in html
    assert 'platform_badge' in html and 'PeerTube' in html and 'Castopod' in html
```

Check that `/communities` is the route of `list_communities` and that the render keyword is `communities`, holding a pagination object with `.items`: run `grep -n "def list_communities" -B3 app/main/routes.py` and read its `render_template` call. Adjust `names()` to match before running.

- [ ] **Step 2: Run it and check that it fails**

Run: `./run_tests.sh tests/test_community_browse_platform.py -v`
Expected: FAIL. The lemmy community is still listed under `platform=peertube`.

- [ ] **Step 3: Implement**

In `list_communities`, after `instance = request.args.get('instance', '')`:

```python
    platform = request.args.get('platform', '')
    if platform not in MEDIA_SOFTWARE:   # D24: anything else filters nothing
        platform = ''
```

After the `if instance:` filter:

```python
    if platform:
        communities = communities.filter(Community.instance_id.in_(
            db.session.query(Instance.id).filter(db.func.lower(Instance.software) == platform)))
```

Pass `platform=platform` to its `render_template`. Imports: `from app.discovery import MEDIA_SOFTWARE`. `Instance` is already imported in `main/routes.py`; check with `grep`.

In the filter form of the list template, add next to the instance or NSFW selector:

```html
<select class="form-select form-control-sm" name="platform" aria-label="{{ _('Platform') }}">
    <option value="" {{ 'selected' if not platform }}>{{ _('All platforms') }}</option>
    <option value="peertube" {{ 'selected' if platform == 'peertube' }}>PeerTube</option>
    <option value="castopod" {{ 'selected' if platform == 'castopod' }}>Castopod</option>
</select>
```

In the community row, after the community title:

```html
{% set community_kind = community_platform(community) %}{% if community_kind %} <span class="badge text-bg-secondary platform_badge">{{ {'peertube': 'PeerTube', 'castopod': 'Castopod'}[community_kind] }}</span>{% endif %}
```

At the bottom of `app/discovery/views.py`, register the global: `bp.app_template_global('community_platform')(platform_of)`, with `from app.discovery.media import platform_of`.

`platform_of` reads `community.instance`, which costs one query per row. If the list already joins `Instance`, accept it. Otherwise add `.options(joinedload(Community.instance))` to the listing query, and check that the relationship is named `instance` (`grep -n "instance = db.relationship" app/models.py`).

- [ ] **Step 4: Run it and check that it passes**

Run: `./run_tests.sh tests/test_community_browse_platform.py tests/test_community_search.py tests/test_discovery_search.py --cov=app.main.routes --cov-branch --cov-report=term-missing -v`
Expected: PASS. The new `list_communities` lines are covered.

- [ ] **Step 5: Commit**

```bash
git add app/main/routes.py app/discovery/views.py app/templates tests/test_community_browse_platform.py
git commit -m "feat: the community list filters and badges PeerTube channels and Castopod podcasts (interop D24)"
```

---

### Task 17: Coverage floors, spec record, full suite

**Files:**
- Modify: `coverage_floors.ini`
- Modify: `docs/superpowers/specs/2026-10-05-proactive-discovery-sync-design.md` (spike outcome, planning deviations)
- Test: the full suite

- [ ] **Step 1: Add floors.** Append to `coverage_floors.ini` under `[floors]`:

```ini
# Interop D24 proactive sync (docs/superpowers/plans/2026-10-05-proactive-discovery-sync.md): new modules, born at 100.
app/discovery/sync.py = 100
app/discovery/instance_actor.py = 100
app/discovery/media.py = 100
app/discovery/external_search.py = 100
app/discovery/admin_views.py = 100
app/discovery/forms.py = 100
app/discovery/views.py = 100
app/discovery/cli.py = 100
app/discovery/backfill.py = 100
```

Before adding the last five, check whether they already have floors (`grep -n "app/discovery" coverage_floors.ini`). Floors only rise: replace a lower value, and never add a duplicate key.

- [ ] **Step 2: Run the full suite with coverage**

Run: `./run_tests.sh --cov=app --cov-branch --cov-report=json && python tests/check_coverage_floors.py`
Expected: every test passes and every floor holds. If `check_coverage_floors.py` takes other arguments, read its `--help` or its header first.

- [ ] **Step 3: Check changed-line coverage in existing modules.** Run:

```bash
git diff --unified=0 d186f94d1..HEAD -- app/activitypub/routes.py app/main/routes.py app/search/routes.py app/community/util.py app/discovery/sources.py | grep '^@@'
```

For each hunk's new line range, check that `coverage.json`'s `executed_lines` for that file covers every executable line in the range. Use `ctx_execute` or a short Python script that reads `coverage.json` and prints any uncovered line in those ranges. Expected: none. For each uncovered line, add a test to the task that owns it, then commit the test separately.

- [ ] **Step 4: Record what the spec does not yet say.** Edit the spec:
  - Under "Spike before planning", add the spike's outcome per platform from Task 0.
  - Under "User-facing surfaces", note that the media predicate lives in `app/discovery/media.py`, and that the media feed also requires `show_all`.
  - Under "Reconcile", note that per-host isolation is `try`/`rollback`, not a SAVEPOINT, and why.
  - Under "Post search", note that the external block shows no thumbnails, and why.

- [ ] **Step 5: Commit**

```bash
git add coverage_floors.ini docs/superpowers/specs/2026-10-05-proactive-discovery-sync-design.md
git commit -m "test: coverage floors at 100 for the proactive sync modules; spec records the spike and planning decisions (interop D24)"
```
