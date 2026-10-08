# ActivityPub Relays Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an admin subscribe hell.cloud to Mastodon-style and LitePub/FediBuzz-style ActivityPub relays. Relayed public top-level posts land in `/c/microblogs` (and its Live view) and expire unless someone engages with them.

**Architecture:**
- **New package:** `app/relays/` holds the code: context, subscribe, inbound, expiry, admin views, forms and CLI.
- **Data:** a `Relay` model and a nullable `Post.relay_id`.
- **Subscribing:** the existing instance actor (`/actor`) sends the Follow, signed with the site key.
- **Inbox, LitePub style:** a gate runs before the normal actor lookup and handles traffic *from* relay actors: Accept, Reject and Announce.
- **Inbox, Mastodon style:** forwarded posts are recognised from the relay's HTTP signature. The relay id travels to `Post.new` through a `ContextVar`.
- **Expiry:** a daily step removes relayed posts nobody engaged with.

**Tech Stack:** Flask, SQLAlchemy and Alembic, Celery (the `background` queue), Flask-WTF, click; pytest in containers.

**Spec:** `docs/superpowers/specs/2026-10-08-activitypub-relays-design.md`

## Global Constraints

- **Relay states:** `pending`, `accepted`, `refused`, `failed`. Styles: `mastodon` and `litepub`.
- **Follow object:** `https://www.w3.org/ns/activitystreams#Public` for Mastodon style; the relay's `actor_id` for LitePub style. The Follow is sent by the instance actor `<SERVER_URL>/actor` with key id `<SERVER_URL>/actor#main-key`.
- **Relay follow ids:** `<SERVER_URL>/activities/relay-follow/<uuid>`. This deliberately differs from discovery-sync's `/activities/follow/<uuid>`, so the two never collide.
- **Which relays count:** only rows that are `accepted` and have a `public_key` take part in delivery handling.
- **Relay actors:** no `User` row is ever created for one.
- **Kept content:**
  - top-level posts only;
  - a post addressed (via `audience`) to a community this instance doesn't have is dropped;
  - a reply is kept only if its parent is stored;
  - relayed boosts are dropped;
  - an existing post is never modified.
- **Moderation:** every existing gate still applies. The *only* gate skipped is "announcer must be followed by a local user", and only for an accepted relay's Announce.
- **Expiry:**
  - **Setting:** `relay_retention_days` (default 7; `<= 0` disables expiry).
  - **Batches:** 500 posts each, with at most 5,000 per run.
  - **Keep reasons:** a local vote, reply, bookmark or report; a local follower of the author; a non-microblogs community with a local member; sticky; `status <= POST_STATUS_REVIEWING`.
- **Coverage:**
  - 100% line and branch coverage of new and changed lines: `python3 tests/check_changed_line_coverage.py coverage.json 9be178f4a --branches app/ fastapi_server.py`. `9be178f4a` is the spec commit.
  - New `app/relays/*.py` modules get floor `100` in `coverage_floors.ini`.
  - No floor may drop. Full suite green.
- **Imports:** no function-level imports in `app/` (`tests/test_no_inline_imports.py`).
- **Running tests:** Python tests run only in containers, `./run_tests.sh <paths> -q`. Gate scripts run on the host with `python3`.
- **Commits:** Conventional Commits, ending in `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Migration:** `down_revision = 'a3c9e5f1b7d4'` (the current head); the downgrade must be reversible.

**Spec clarification, recorded here:** the spec says `instance_actor_answer` "also looks for a relay row". That can't work for LitePub relays. The inbox resolves `request_json['actor']` and verifies its signature *before* any Accept handler runs, and a relay actor is not a user. So relay Accept and Reject are handled in the new relay-actor gate (Task 3), which runs before the actor lookup. `instance_actor_answer` is unchanged.

A Mastodon-style relay whose actor fetch failed has `actor_id = NULL`. Its Accept can't be attributed, so the row stays `pending` until an admin Retry re-fetches the actor. That matches the spec's error table.

## Review Focus

1. **A relay that pushes before it accepts.** Deliveries from a `pending` relay must be treated as ordinary traffic: no `relay_id`, and the follow gate still applies to its Announces. Tested in Tasks 3 and 4.
2. **A forged relay signature.** A Mastodon-style delivery whose `keyId` names a relay but doesn't verify against that relay's key must not get `relay_id`, though it is still processed as today. A LitePub Announce claiming a relay actor with a bad signature gets 401. Tested in Tasks 3 and 4.
3. **The allowlist.** In allowlist mode, the inbox refuses a relay host that isn't allowlisted, before the relay gate runs. Admins must add the relay host to the allowlist. Tested in Task 3, which asserts the refusal holds and that the admin page says so.
4. **Expiry deleting something someone cares about.** Each keep reason has its own test. Non-relayed posts are never selected. Tested in Task 5.
5. **A huge relay backlog on the first run.** The 5,000-per-run cap holds, and the run commits per batch. Tested in Task 5.

---

## File Structure

| File | Responsibility |
|---|---|
| `app/models.py` | `Relay` model; `Post.relay_id` column; `Post.new` sets `relay_id` from the context |
| `migrations/versions/b7e1c2d9f4a6_relays.py` | creates the `relay` table, `post.relay_id` and the partial index |
| `app/relays/__init__.py` | constants (`RELAY_*` states and styles, `PUBLIC`); `current_relay_id` ContextVar plus `relay_context()` |
| `app/relays/subscribe.py` | `detect_relay`, `add_relay`, `send_relay_follow`, `retry_relay`, `remove_relay`, `record_relay_refusal` |
| `app/relays/inbound.py` | `relay_actor_gate`, `relay_for_forwarded`, `relayed_activity_allowed`, `relayed_object_allowed`, `process_relayed_announce` task |
| `app/relays/expiry.py` | `expire_relayed_posts` task |
| `app/relays/forms.py`, `app/relays/admin_views.py`, `app/templates/admin/federation_relays.html` | the admin page |
| `app/relays/cli.py` | `flask relays add/remove/retry/list` |
| `app/activitypub/routes.py` | calls the gate in `shared_inbox`; passes `relay_id` to `process_inbox_request` |
| `app/activitypub/signature.py` | `post_request` also calls `record_relay_refusal` |
| `app/cli.py`, `app/admin/__init__.py`, `app/templates/admin/_nav.html` | register the CLI, the views and the nav link; daily maintenance calls expiry |
| `tests/test_relays_*.py` | the tests, one file per task |

---

### Task 1: Data model, migration, and relay context

**Files:**
- Create: `app/relays/__init__.py`, `migrations/versions/b7e1c2d9f4a6_relays.py`
- Modify: `app/models.py` (add `Relay` after `DiscoveryExclusion` ~L5750; add `Post.relay_id` near the other `Post` columns ~L2497; `Post.new` ~L2690 where the `Post(...)` is built)
- Modify: `coverage_floors.ini`
- Test: `tests/test_relays_model.py`

**Interfaces:**
- Produces:
  - `app.relays`:
    - constants `RELAY_PENDING = 'pending'`, `RELAY_ACCEPTED = 'accepted'`, `RELAY_REFUSED = 'refused'`, `RELAY_FAILED = 'failed'`, `STYLE_MASTODON = 'mastodon'`, `STYLE_LITEPUB = 'litepub'`, `PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'`;
    - `current_relay_id: ContextVar[int | None]`;
    - `relay_context(relay_id)`, a context manager.
  - `app.models.Relay` with the columns from the spec.
  - `Post.relay_id`.
  - `Post.new` stores `current_relay_id.get()` into `post.relay_id`.

- [ ] **Step 1: Write the failing tests**

`tests/test_relays_model.py`:

```python
"""Relay data (spec: Data) and the relay context Post.new reads."""
import pytest

from app import db
from app.models import Post, Relay
from app.relays import RELAY_PENDING, STYLE_MASTODON, current_relay_id, relay_context
from app.utils import utcnow
from tests.factories import make_community, make_post, make_user

pytestmark = pytest.mark.usefixtures('site')


def make_relay(url='https://relay.example/inbox', **columns):
    relay = Relay(url=url, style=STYLE_MASTODON, inbox_url=url, state=RELAY_PENDING,
                  follow_activity_id='https://test.piefed.local/activities/relay-follow/x', created_at=utcnow())
    for name, value in columns.items():
        setattr(relay, name, value)
    db.session.add(relay)
    db.session.commit()
    return relay


def test_the_relay_context_is_scoped(app):
    assert current_relay_id.get() is None
    with relay_context(7):
        assert current_relay_id.get() == 7
    assert current_relay_id.get() is None


def test_deleting_a_relay_keeps_its_posts_and_clears_the_link(app, db_session):
    relay = make_relay()
    community = make_community('microblogs')
    post = make_post(community, make_user(None, 'someone', local=True), 'https://remote.example/p/1')
    post.relay_id = relay.id
    db.session.commit()

    db.session.delete(relay)
    db.session.commit()
    db.session.expire_all()

    assert db.session.get(Post, post.id).relay_id is None
```

Add one test that drives `Post.new` with the context set. Copy the `TestPostNewAnnounces` fixture pattern from `tests/test_microblog_live.py` (the `api_baseline` env, a remote author and a Create document):
- inside `with relay_context(relay.id):` the created post has `relay_id == relay.id`;
- outside the context it is `None`.

- [ ] **Step 2: Run to verify they fail**

Run: `./run_tests.sh tests/test_relays_model.py -q`
Expected: `ImportError` (`app.relays` and `Relay` don't exist).

- [ ] **Step 3: Implement**

`app/relays/__init__.py`:

```python
"""ActivityPub relay support (spec docs/superpowers/specs/2026-10-08-activitypub-relays-design.md).
Imports nothing from app.models, so models.py can import the context at the top."""
from contextlib import contextmanager
from contextvars import ContextVar

RELAY_PENDING = 'pending'
RELAY_ACCEPTED = 'accepted'
RELAY_REFUSED = 'refused'
RELAY_FAILED = 'failed'
STYLE_MASTODON = 'mastodon'
STYLE_LITEPUB = 'litepub'
PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'

# The relay a post being ingested came through; Post.new records it as post.relay_id.
current_relay_id: ContextVar = ContextVar('current_relay_id', default=None)


@contextmanager
def relay_context(relay_id):
    token = current_relay_id.set(relay_id)
    try:
        yield
    finally:
        current_relay_id.reset(token)
```

`app/models.py`:
- Add `from app.relays import current_relay_id` with the other top-level `app.*` imports. Check that `app/relays/__init__.py` imports no `app` module, so there is no cycle.
- In `Post`: `relay_id = db.Column(db.Integer, db.ForeignKey('relay.id', ondelete='SET NULL'), nullable=True)` (the index is created in the migration).
- In `Post.new`, where the `Post(...)` constructor gets its columns (search `post = Post(` inside `Post.new`), add `relay_id=current_relay_id.get(),`.
- New model after `DiscoveryExclusion`:

```python
class Relay(db.Model):
    """An ActivityPub relay the instance actor subscribes to (spec 2026-10-08-activitypub-relays)."""
    __tablename__ = 'relay'
    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.String(2048), nullable=False, unique=True)
    style = db.Column(db.String(16), nullable=False)
    inbox_url = db.Column(db.String(2048), nullable=False)
    actor_id = db.Column(db.String(2048), nullable=True, index=True)
    public_key = db.Column(db.Text, nullable=True)
    follow_activity_id = db.Column(db.String(2048), nullable=False)
    state = db.Column(db.String(16), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    answered_at = db.Column(db.DateTime, nullable=True)
    last_error = db.Column(db.String(1024), nullable=True)
```

`migrations/versions/b7e1c2d9f4a6_relays.py`:

```python
"""relays

Revision ID: b7e1c2d9f4a6
Revises: a3c9e5f1b7d4
"""
import sqlalchemy as sa
from alembic import op

revision = 'b7e1c2d9f4a6'
down_revision = 'a3c9e5f1b7d4'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('relay',
                    sa.Column('id', sa.Integer(), nullable=False),
                    sa.Column('url', sa.String(length=2048), nullable=False),
                    sa.Column('style', sa.String(length=16), nullable=False),
                    sa.Column('inbox_url', sa.String(length=2048), nullable=False),
                    sa.Column('actor_id', sa.String(length=2048), nullable=True),
                    sa.Column('public_key', sa.Text(), nullable=True),
                    sa.Column('follow_activity_id', sa.String(length=2048), nullable=False),
                    sa.Column('state', sa.String(length=16), nullable=False),
                    sa.Column('created_at', sa.DateTime(), nullable=False),
                    sa.Column('answered_at', sa.DateTime(), nullable=True),
                    sa.Column('last_error', sa.String(length=1024), nullable=True),
                    sa.PrimaryKeyConstraint('id'),
                    sa.UniqueConstraint('url'))
    op.create_index('ix_relay_actor_id', 'relay', ['actor_id'])
    op.add_column('post', sa.Column('relay_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_post_relay_id', 'post', 'relay', ['relay_id'], ['id'], ondelete='SET NULL')
    op.create_index('ix_post_relay_id', 'post', ['relay_id'], postgresql_where=sa.text('relay_id IS NOT NULL'))


def downgrade():
    op.drop_index('ix_post_relay_id', table_name='post')
    op.drop_constraint('fk_post_relay_id', 'post', type_='foreignkey')
    op.drop_column('post', 'relay_id')
    op.drop_index('ix_relay_actor_id', table_name='relay')
    op.drop_table('relay')
```

Before writing it, confirm the head with `grep -l "down_revision = 'a3c9e5f1b7d4'" migrations/versions/*.py`. That must return nothing; if a newer migration exists, use it as `down_revision`.

`coverage_floors.ini`, at the end of `[floors]`:

```ini
# ActivityPub relays (2026-10-08): new modules, born fully covered.
app/relays/__init__.py = 100
```

- [ ] **Step 4: Run to verify they pass**

Run: `./run_tests.sh tests/test_relays_model.py tests/test_no_inline_imports.py -q`
Expected: PASS. `run_tests.sh` applies migrations, so the upgrade runs.

- [ ] **Step 5: Commit**

```bash
git add app/relays/__init__.py app/models.py migrations/versions/b7e1c2d9f4a6_relays.py coverage_floors.ini tests/test_relays_model.py
git commit -m "feat: relays and the post field that records which relay delivered a post

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Subscribing, retry, remove

**Files:**
- Create: `app/relays/subscribe.py`, `app/relays/refusals.py`
- Modify: `app/activitypub/signature.py` (in `post_request`, next to `record_delivery_refusal(session, body, http_status_code)` ~L179); `coverage_floors.ini`
- Test: `tests/test_relays_subscribe.py`

**Interfaces:**
- Consumes: Task 1's `Relay` and constants; `instance_actor_url()` (`app/discovery/instance_answers.py`); `send_post_request` (`app/activitypub/signature.py`); `remote_object_to_json` (`app/activitypub/util.py`); `Site` keys.
- Produces (`app.relays.subscribe`):
  - `class RelayError(Exception)`;
  - `detect_relay(url) -> dict` with keys `style`, `inbox_url`, `actor_id`, `public_key` (raises `RelayError` for a LitePub URL whose actor can't be fetched);
  - `add_relay(url) -> Relay` (raises `RelayError` if the URL is already present);
  - `send_relay_follow(relay) -> None`;
  - `retry_relay(relay) -> None`;
  - `remove_relay(relay) -> None`;
  - `relay_follow_activity(relay) -> dict`.
  - `app.relays.refusals.record_relay_refusal(session, body, status_code) -> None`.
  - `app.relays.FOLLOW_PATH`.

- [ ] **Step 1: Write the failing tests**

`tests/test_relays_subscribe.py`. Use a recorder in place of the network: monkeypatch `app.relays.subscribe.remote_object_to_json` and `app.relays.subscribe.send_post_request`.

```python
"""Subscribing to relays (spec: Subscribing)."""
import pytest

from app import db
from app.models import Relay
from app.relays import (PUBLIC, RELAY_ACCEPTED, RELAY_FAILED, RELAY_PENDING, RELAY_REFUSED, STYLE_LITEPUB,
                        STYLE_MASTODON)
from app.relays import subscribe
from app.relays.refusals import record_relay_refusal

pytestmark = pytest.mark.usefixtures('site')

ACTOR = {'id': 'https://relay.example/actor', 'type': 'Application', 'inbox': 'https://relay.example/inbox',
         'publicKey': {'id': 'https://relay.example/actor#main-key', 'publicKeyPem': 'PEM'}}
TAG = {'id': 'https://relay.fedi.buzz/tag/cats', 'type': 'Service', 'inbox': 'https://relay.fedi.buzz/tag/cats/inbox',
       'publicKey': {'id': 'https://relay.fedi.buzz/tag/cats#key', 'publicKeyPem': 'TAGPEM'}}


@pytest.fixture
def net(monkeypatch):
    calls = {'get': [], 'post': []}
    documents = {}

    def fake_get(uri):
        calls['get'].append(uri)
        return documents.get(uri)

    def fake_post(uri, body, private_key, key_id, **kwargs):
        calls['post'].append((uri, body, key_id))
        return True

    monkeypatch.setattr(subscribe, 'remote_object_to_json', fake_get)
    monkeypatch.setattr(subscribe, 'send_post_request', fake_post)
    calls['documents'] = documents
    return calls


class TestDetect:

    def test_an_inbox_url_is_mastodon_style_and_its_actor_is_looked_up(self, app, net):
        net['documents']['https://relay.example/actor'] = ACTOR
        with app.test_request_context():
            found = subscribe.detect_relay('https://relay.example/inbox')
        assert found == {'style': STYLE_MASTODON, 'inbox_url': 'https://relay.example/inbox',
                         'actor_id': 'https://relay.example/actor', 'public_key': 'PEM'}

    def test_a_mastodon_relay_whose_actor_cannot_be_fetched_has_no_key(self, app, net):
        with app.test_request_context():
            found = subscribe.detect_relay('https://relay.example/inbox')
        assert found['actor_id'] is None and found['public_key'] is None

    def test_a_fedibuzz_tag_url_is_litepub_style(self, app, net):
        net['documents']['https://relay.fedi.buzz/tag/cats'] = TAG
        with app.test_request_context():
            found = subscribe.detect_relay('https://relay.fedi.buzz/tag/cats')
        assert found == {'style': STYLE_LITEPUB, 'inbox_url': 'https://relay.fedi.buzz/tag/cats/inbox',
                         'actor_id': 'https://relay.fedi.buzz/tag/cats', 'public_key': 'TAGPEM'}

    @pytest.mark.parametrize('document', [None, {'id': 'https://x.example/a'}])
    def test_a_litepub_url_without_a_usable_actor_is_refused(self, app, net, document):
        net['documents']['https://x.example/a'] = document
        with app.test_request_context(), pytest.raises(subscribe.RelayError):
            subscribe.detect_relay('https://x.example/a')


class TestFollow:

    def test_add_stores_a_pending_row_and_follows_public_for_mastodon(self, app, net):
        net['documents']['https://relay.example/actor'] = ACTOR
        with app.test_request_context():
            relay = subscribe.add_relay('https://relay.example/inbox')
        uri, body, key_id = net['post'][-1]
        assert relay.state == RELAY_PENDING
        assert uri == 'https://relay.example/inbox'
        assert body['type'] == 'Follow' and body['object'] == PUBLIC and body['to'] == [PUBLIC]
        assert body['actor'].endswith('/actor') and key_id.endswith('/actor#main-key')
        assert body['id'] == relay.follow_activity_id and '/activities/relay-follow/' in body['id']

    def test_a_litepub_follow_names_the_relay_actor(self, app, net):
        net['documents']['https://relay.fedi.buzz/tag/cats'] = TAG
        with app.test_request_context():
            subscribe.add_relay('https://relay.fedi.buzz/tag/cats')
        assert net['post'][-1][1]['object'] == 'https://relay.fedi.buzz/tag/cats'

    def test_adding_the_same_url_twice_is_refused(self, app, net):
        net['documents']['https://relay.example/actor'] = ACTOR
        with app.test_request_context():
            subscribe.add_relay('https://relay.example/inbox')
            with pytest.raises(subscribe.RelayError):
                subscribe.add_relay('https://relay.example/inbox')

    def test_retry_sends_a_new_follow_id_and_goes_back_to_pending(self, app, net):
        net['documents']['https://relay.example/actor'] = ACTOR
        with app.test_request_context():
            relay = subscribe.add_relay('https://relay.example/inbox')
            first = relay.follow_activity_id
            relay.state = RELAY_REFUSED
            db.session.commit()
            subscribe.retry_relay(relay)
        assert relay.state == RELAY_PENDING and relay.follow_activity_id != first

    def test_remove_sends_undo_and_deletes(self, app, net):
        net['documents']['https://relay.example/actor'] = ACTOR
        with app.test_request_context():
            relay = subscribe.add_relay('https://relay.example/inbox')
            follow = subscribe.relay_follow_activity(relay)
            subscribe.remove_relay(relay)
        undo = net['post'][-1][1]
        assert undo['type'] == 'Undo' and undo['object'] == follow
        assert Relay.query.count() == 0

    def test_remove_still_deletes_when_the_undo_cannot_be_sent(self, app, net, monkeypatch):
        net['documents']['https://relay.example/actor'] = ACTOR
        with app.test_request_context():
            relay = subscribe.add_relay('https://relay.example/inbox')

            def broken(*args, **kwargs):
                raise OSError('down')
            monkeypatch.setattr(subscribe, 'send_post_request', broken)
            subscribe.remove_relay(relay)
        assert Relay.query.count() == 0


class TestRefusal:

    @pytest.mark.parametrize('status, expected', [(403, RELAY_FAILED), (500, RELAY_PENDING)])
    def test_a_definitive_refusal_marks_the_row_failed(self, app, net, status, expected):
        net['documents']['https://relay.example/actor'] = ACTOR
        with app.test_request_context():
            relay = subscribe.add_relay('https://relay.example/inbox')
            record_relay_refusal(db.session, subscribe.relay_follow_activity(relay), status)
        assert relay.state == expected

    def test_an_accepted_relay_is_not_failed_by_a_late_refusal(self, app, net):
        net['documents']['https://relay.example/actor'] = ACTOR
        with app.test_request_context():
            relay = subscribe.add_relay('https://relay.example/inbox')
            relay.state = RELAY_ACCEPTED
            db.session.commit()
            record_relay_refusal(db.session, subscribe.relay_follow_activity(relay), 403)
        assert relay.state == RELAY_ACCEPTED

    @pytest.mark.parametrize('body', [None, {'type': 'Create'}, {'type': 'Follow', 'id': 'https://elsewhere/x'}])
    def test_other_bodies_are_ignored(self, app, body):
        with app.test_request_context():
            record_relay_refusal(db.session, body, 403)
```


- [ ] **Step 2: Run to verify they fail**

Run: `./run_tests.sh tests/test_relays_subscribe.py -q`
Expected: `ImportError` (`app.relays.subscribe`).

- [ ] **Step 3: Implement**

`app/relays/subscribe.py`:

```python
"""Subscribing the instance actor to relays (spec: Subscribing)."""
import uuid
from urllib.parse import urlsplit, urlunsplit

from flask import current_app

from app import db
from app.activitypub.signature import send_post_request
from app.activitypub.util import remote_object_to_json
from app.discovery.instance_answers import instance_actor_url
from app.models import Relay, Site, utcnow
from app.relays import FOLLOW_PATH, PUBLIC, RELAY_PENDING, STYLE_LITEPUB, STYLE_MASTODON


class RelayError(Exception):
    pass


def _signing():
    site = db.session.get(Site, 1)
    return site.private_key, instance_actor_url() + '#main-key'


def _actor_parts(document):
    if not isinstance(document, dict) or not isinstance(document.get('id'), str):
        return None
    key = document.get('publicKey')
    pem = key.get('publicKeyPem') if isinstance(key, dict) else None
    return document['id'], document.get('inbox'), pem


def detect_relay(url: str) -> dict:
    parts = urlsplit(url)
    if parts.path.rstrip('/').endswith('/inbox'):
        actor_url = urlunsplit(parts._replace(path=parts.path.rstrip('/')[:-len('inbox')] + 'actor'))
        found = _actor_parts(remote_object_to_json(actor_url))
        actor_id, public_key = (found[0], found[2]) if found else (None, None)
        return {'style': STYLE_MASTODON, 'inbox_url': url, 'actor_id': actor_id, 'public_key': public_key}
    found = _actor_parts(remote_object_to_json(url))
    if not found or not isinstance(found[1], str):
        raise RelayError(f'{url} is not a relay actor with an inbox')
    return {'style': STYLE_LITEPUB, 'inbox_url': found[1], 'actor_id': found[0], 'public_key': found[2]}


def relay_follow_activity(relay) -> dict:
    target = PUBLIC if relay.style == STYLE_MASTODON else relay.actor_id
    return {'type': 'Follow', 'id': relay.follow_activity_id, 'actor': instance_actor_url(),
            'object': target, 'to': [target]}


def _new_follow_id() -> str:
    return f"{current_app.config['SERVER_URL']}{FOLLOW_PATH}{uuid.uuid4()}"


def send_relay_follow(relay) -> None:
    private_key, key_id = _signing()
    send_post_request(relay.inbox_url, relay_follow_activity(relay), private_key, key_id, timeout=10)


def add_relay(url: str) -> Relay:
    url = url.strip()
    if db.session.query(Relay).filter_by(url=url).first():
        raise RelayError(f'{url} is already a relay')
    found = detect_relay(url)
    relay = Relay(url=url, follow_activity_id=_new_follow_id(), state=RELAY_PENDING, created_at=utcnow(), **found)
    db.session.add(relay)
    db.session.commit()
    send_relay_follow(relay)
    return relay


def retry_relay(relay) -> None:
    for name, value in detect_relay(relay.url).items():
        setattr(relay, name, value)
    relay.follow_activity_id = _new_follow_id()
    relay.state = RELAY_PENDING
    relay.answered_at = None
    relay.last_error = None
    db.session.commit()
    send_relay_follow(relay)


def remove_relay(relay) -> None:
    try:
        undo = {'type': 'Undo', 'actor': instance_actor_url(), 'object': relay_follow_activity(relay),
                'id': f"{current_app.config['SERVER_URL']}/activities/undo/{uuid.uuid4()}"}
        private_key, key_id = _signing()
        send_post_request(relay.inbox_url, undo, private_key, key_id, timeout=10)
    except Exception as error:
        current_app.logger.info(f'relays: undo to {relay.inbox_url} failed: {type(error).__name__}')
    db.session.delete(relay)
    db.session.commit()
```

`app/relays/refusals.py`. It is a separate module because `app.activitypub.signature` imports it, and `app.relays.subscribe` imports `app.activitypub.signature`. Putting it in `subscribe` would be an import cycle.

```python
"""A relay that refuses our Follow outright (spec: Subscribing). Imports nothing from app.activitypub."""
from app.discovery.instance_answers import DEFINITIVE_REFUSALS
from app.models import Relay
from app.relays import FOLLOW_PATH, RELAY_ACCEPTED, RELAY_FAILED


def record_relay_refusal(session, body, status_code) -> None:
    """Mark the relay row failed; an Accept already received stands."""
    if status_code not in DEFINITIVE_REFUSALS or not isinstance(body, dict) or body.get('type') != 'Follow':
        return
    follow_id = body.get('id')
    if not isinstance(follow_id, str) or FOLLOW_PATH not in follow_id:
        return
    relay = session.query(Relay).filter_by(follow_activity_id=follow_id).first()
    if relay is None or relay.state == RELAY_ACCEPTED:
        return
    relay.state = RELAY_FAILED
    relay.last_error = f'Follow refused: HTTP {status_code}'
    session.commit()
```

In `app/relays/__init__.py`, add `FOLLOW_PATH = '/activities/relay-follow/'` next to the other constants.

In `app/activitypub/signature.py`'s `post_request`, directly after `record_delivery_refusal(session, body, http_status_code)`, add `record_relay_refusal(session, body, http_status_code)`, imported from `app.relays.refusals` at the top of the module, next to `record_delivery_refusal`.

`coverage_floors.ini`: add `app/relays/subscribe.py = 100` and `app/relays/refusals.py = 100`.

- [ ] **Step 4: Run to verify they pass**

Run: `./run_tests.sh tests/test_relays_subscribe.py tests/test_relays_model.py tests/test_no_inline_imports.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/relays/ app/activitypub/signature.py coverage_floors.ini tests/test_relays_subscribe.py
git commit -m "feat: the instance actor subscribes to relays of both styles, and can retry or leave them

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: The relay-actor gate in the inbox, and relayed Announces

**Files:**
- Create: `app/relays/inbound.py`
- Modify: `app/activitypub/routes.py` (`shared_inbox`, directly before the `HttpSignature.precheck(request)` try at ~L762); `coverage_floors.ini`
- Test: `tests/test_relays_inbound.py`

**Interfaces:**
- Consumes: Task 1's `Relay`, constants and `relay_context`; Task 2's `detect_relay`; `HttpSignature` and `VerificationError` (`app/activitypub/signature.py`); `remote_object_to_json`, `is_top_level`, `create_resolved_object`, `host_of`, `find_microblogging_community` and `log_incoming_ap` (`app/activitypub/util.py`); the `APLOG_*` constants.
- Produces (`app.relays.inbound`):
  - `relay_actor_gate(request, request_json) -> tuple[str, int] | None`. It returns a Flask response tuple when the activity is *from* a relay actor that has a stored key, and `None` otherwise.
  - `relayed_object_allowed(obj) -> tuple[bool, str]`.
  - `relayed_activity_allowed(activity) -> tuple[bool, str]`.
  - `process_relayed_announce(relay_id, object_uri)`, a Celery task. The gate calls it with `.apply_async(..., queue='background')`, or directly when `current_app.debug`.

- [ ] **Step 1: Write the failing tests**

`tests/test_relays_inbound.py`. Build signed requests the way the existing signature tests do; find one with `grep -rln "verify_request\|HttpSignature" tests/ | head` and copy its technique for producing a signed POST. In the first tests, monkeypatch `app.relays.inbound.HttpSignature.verify_request` to a recorder that raises `VerificationError` for keys other than the expected PEM, so the gate logic is tested without real crypto. Then add **one** end-to-end test that signs a real request with an RSA keypair (`tests.factories.a_keypair()`) and posts it to `/inbox` through the test client.

Cases, one test each:
- **Accept from an accepted-or-pending relay actor** whose `object` is our Follow (as a dict, or as the id string) → the row becomes `accepted` with `answered_at`, and the response is 200.
- **Accept whose object id is another relay's follow id** → ignored, and the row is unchanged.
- **Accept for a row that isn't `pending`** → ignored.
- **Reject** → `refused`.
- **Announce from an accepted relay**, with an object URI → `process_relayed_announce` is queued (monkeypatch the task's `apply_async` to a recorder) with `(relay.id, uri)` on queue `'background'`, and the response is 200.
- **Announce from a `pending` relay** → the gate returns `None`, i.e. normal inbox processing: the follow gate drops it (assert the gate's return value).
- **Activity types other than Accept, Reject or Announce** from an accepted relay → 200 and ignored.
- **Bad signature:**
  - the key is re-fetched once (monkeypatch `app.relays.inbound.detect_relay`) and the retry verifies → handled;
  - it still fails → 401.
- **No `User` is created:** after a handled relay Announce, `User.query.filter_by(ap_profile_id=relay.actor_id).count() == 0`.
- **Allowlist mode with the relay host not allowlisted** → the inbox answers 403 before the gate runs (`shared_inbox` checks `instance_allowed` first). Set `g.site.allowlist_mode` to `ALLOWLIST_STRONG` (`grep -n ALLOWLIST_STRONG app/constants.py`).
- **`process_relayed_announce`:**
  - for a fetched top-level Note → a post is created in microblogs with `relay_id`;
  - a fetch that returns `None` → nothing is created and the failure is logged;
  - a reply whose parent is unknown → dropped;
  - an object addressed (`audience`) to an unknown community → dropped;
  - an existing post → unchanged, and its `relay_id` stays `None`.

  Monkeypatch `app.relays.inbound.remote_object_to_json` to return a Note document whose `attributedTo` is on the same host as the URI, so `create_resolved_object`'s domain check passes. Use the same document shape `tests/test_ap_peer_source_and_timestamps.py` uses.
- **`relayed_object_allowed`:**
  - a top-level object with no audience → allowed;
  - audience = a known community's `ap_profile_id` → allowed;
  - audience unknown → refused;
  - `inReplyTo` a stored Post's `ap_id` → allowed;
  - `inReplyTo` a stored PostReply's `ap_id` → allowed;
  - `inReplyTo` unknown → refused;
  - `inReplyTo` as a dict with an `id` → handled;
  - a non-dict object → refused.
- **`relayed_activity_allowed`:** Announce → refused; Create → delegates; Update or Delete → allowed.

- [ ] **Step 2: Run to verify they fail**

Run: `./run_tests.sh tests/test_relays_inbound.py -q`
Expected: `ImportError` (`app.relays.inbound`).

- [ ] **Step 3: Implement**

`app/relays/inbound.py`:

```python
"""Relay traffic in the inbox (spec: Recognising a relay delivery; What is kept)."""
from flask import current_app
from sqlalchemy import func

from app import celery, db
from app.activitypub.signature import HttpSignature, VerificationError, parse_signature_header
from app.activitypub.util import (create_resolved_object, find_microblogging_community, host_of, is_top_level,
                                  log_incoming_ap, remote_object_to_json)
from app.constants import APLOG_ACCEPT, APLOG_ANNOUNCE, APLOG_FAILURE, APLOG_IGNORED, APLOG_SUCCESS
from app.models import Community, Post, PostReply, Relay, utcnow
from app.relays import RELAY_ACCEPTED, RELAY_PENDING, RELAY_REFUSED, relay_context
from app.relays.subscribe import RelayError, detect_relay
from app.utils import get_task_session, patch_db_session


def _verified(request, relay) -> bool:
    try:
        HttpSignature.verify_request(request, relay.public_key, skip_date=True)
        return True
    except VerificationError:
        return False


def _verified_with_refetch(request, relay) -> bool:
    if relay.public_key and _verified(request, relay):
        return True
    try:
        relay.public_key = detect_relay(relay.url)['public_key']
    except RelayError:
        return False
    db.session.commit()
    return bool(relay.public_key) and _verified(request, relay)


def _answered_follow_id(activity):
    obj = activity.get('object')
    if isinstance(obj, dict):
        return obj.get('id')
    return obj if isinstance(obj, str) else None


def relay_actor_gate(request, request_json):
    actor = request_json.get('actor')
    if not isinstance(actor, str):
        return None
    relay = db.session.query(Relay).filter(Relay.actor_id == actor).first()
    if relay is None:
        return None
    activity_type = request_json.get('type')
    if activity_type == 'Announce' and relay.state != RELAY_ACCEPTED:
        return None   # not subscribed (yet): ordinary traffic, the follow gate applies
    if not _verified_with_refetch(request, relay):
        return '', 401
    if activity_type in ('Accept', 'Reject'):
        if relay.state == RELAY_PENDING and _answered_follow_id(request_json) == relay.follow_activity_id:
            relay.state = RELAY_ACCEPTED if activity_type == 'Accept' else RELAY_REFUSED
            relay.answered_at = utcnow()
            db.session.commit()
        return '', 200
    if activity_type == 'Announce':
        obj = request_json.get('object')
        uri = obj.get('id') if isinstance(obj, dict) else obj
        if isinstance(uri, str):
            if current_app.debug:
                process_relayed_announce(relay.id, uri)
            else:
                process_relayed_announce.apply_async(args=(relay.id, uri), queue='background')
        return '', 200
    return '', 200


def _exists(ap_id) -> bool:
    return bool(Post.get_by_ap_id(ap_id) or PostReply.get_by_ap_id(ap_id))


def relayed_object_allowed(obj):
    if not isinstance(obj, dict):
        return False, 'relayed object is not an object'
    reply_to = obj.get('inReplyTo')
    if reply_to:
        target = reply_to.get('id') if isinstance(reply_to, dict) else reply_to
        if not isinstance(target, str) or not _exists(target):
            return False, 'relayed reply to a post this instance does not have'
        return True, ''
    audience = obj.get('audience')
    if isinstance(audience, str) and audience:
        known = db.session.query(Community.id).filter(func.lower(Community.ap_profile_id) == audience.lower()).first()
        if known is None:
            return False, 'relayed post for a community this instance does not have'
    return True, ''


def relayed_activity_allowed(activity):
    activity_type = activity.get('type')
    if activity_type == 'Announce':
        return False, 'relayed boost'
    if activity_type == 'Create':
        return relayed_object_allowed(activity.get('object'))
    return True, ''


@celery.task
def process_relayed_announce(relay_id, object_uri):
    session = get_task_session()
    try:
        with patch_db_session(session):
            if Post.get_by_ap_id(object_uri):
                return
            post_data = remote_object_to_json(object_uri)
            if not post_data:
                log_incoming_ap(object_uri, APLOG_ANNOUNCE, APLOG_FAILURE, None, 'Could not fetch relayed object')
                return
            allowed, reason = relayed_object_allowed(post_data)
            if not allowed or not is_top_level(post_data):
                log_incoming_ap(object_uri, APLOG_ANNOUNCE, APLOG_IGNORED, None, reason or 'relayed reply')
                return
            with relay_context(relay_id):
                create_resolved_object(object_uri, post_data, host_of(object_uri), find_microblogging_community(),
                                       object_uri, False)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

Notes:
- Check the names `APLOG_ACCEPT` and `APLOG_SUCCESS` exist and are used, or drop unused ones; the linter and the tests decide.
- Confirm `get_task_session` and `patch_db_session` are importable from `app.utils`. `app/shared/tasks/maintenance.py` imports them, so copy its import line.
- `create_resolved_object` takes a community. When the fetched object's `audience` names a known community, pass that community instead of microblogs. Implement that branch, with a test, by looking the community up the same way `relayed_object_allowed` does. Make it a small `_target_community(post_data)` helper used in both places.

In `app/activitypub/routes.py` `shared_inbox`, directly before `try: HttpSignature.precheck(request)`:

```python
    answered = relay_actor_gate(request, request_json)   # relays (spec 2026-10-08): never a User row
    if answered is not None:
        return answered
```

Import `relay_actor_gate` at the top of `routes.py`. If the import creates a cycle, because `app.relays.inbound` imports `app.activitypub.util` and `routes.py` is imported by `util`, run `python -c "import app.activitypub.routes"` inside the container (`./run_tests.sh tests/test_relays_inbound.py` will fail at import if so). Resolve it by having `routes.py` import only from a module that imports nothing from `app.activitypub.routes`. Don't fall back to a function-level import.

`coverage_floors.ini`: add `app/relays/inbound.py = 100`.

- [ ] **Step 4: Run to verify they pass**

Run: `./run_tests.sh tests/test_relays_inbound.py tests/test_relays_subscribe.py tests/test_no_inline_imports.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/relays/inbound.py app/activitypub/routes.py coverage_floors.ini tests/test_relays_inbound.py
git commit -m "feat: relay actors are answered before the normal actor lookup, and their Announces are stored as relayed posts

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Mastodon-style relayed deliveries

**Files:**
- Modify: `app/relays/inbound.py` (add `relay_for_forwarded`); `app/activitypub/routes.py` (`shared_inbox` after the signature block; the `process_inbox_request` signature and start, ~L924)
- Test: `tests/test_relays_forwarded.py`

**Interfaces:**
- Consumes: Task 3's `relayed_activity_allowed`, plus `relay_context`.
- Produces:
  - `relay_for_forwarded(request) -> Relay | None`: an accepted relay with a key whose actor owns the request's `keyId` and whose signature verifies.
  - `process_inbox_request(request_json, store_ap_json, relay_id=None)`.

- [ ] **Step 1: Write the failing tests**

`tests/test_relays_forwarded.py`. Drive `process_inbox_request` directly, as the existing inbox-processing tests do (`grep -ln "process_inbox_request(" tests/*.py | head -3` and copy a fixture). Cover `relay_for_forwarded` with a fake `request`, an object with `.headers`, and the verification recorder from Task 3.

Cases:
- **`relay_for_forwarded`:**
  - `keyId` equal to an accepted relay's `actor_id + '#main-key'`, signature verifies → that relay;
  - signature fails → `None`;
  - relay `pending` → `None`;
  - no `Signature` header → `None`;
  - `keyId` of another host → `None`.
- **`shared_inbox`:** a request that fails the author's HTTP signature, passes the LD-signature fallback (monkeypatch `LDSignature.verify_signature` to succeed) and is HTTP-signed by an accepted relay → `process_inbox_request` is called with `relay_id=<relay.id>`. Monkeypatch `process_inbox_request` (and its `.delay`) to a recorder. When the relay check fails, `relay_id=None`.
- **`process_inbox_request(..., relay_id=R)`:**
  - a top-level Create with no community → the post is in microblogs with `relay_id == R`;
  - a Create addressed to an unknown community → no post, and the log says "relayed post for a community this instance does not have";
  - a reply to an unknown parent → no reply, and no fetch is attempted (monkeypatch `remote_object_to_json` to fail the test if it's called);
  - a reply to a stored post → the reply is stored;
  - an Announce → dropped;
  - a Create for an already-stored post → unchanged, `relay_id` still `None`;
  - with `relay_id=None` → today's behavior (no relay rules applied).
- **Moderation through a relay** (with `relay_id=R`):
  - an author on a banned instance → no post;
  - a banned user → no post;
  - a Create to a `local_only` community → no post.

- [ ] **Step 2: Run to verify they fail**

Run: `./run_tests.sh tests/test_relays_forwarded.py -q`
Expected: `ImportError` for `relay_for_forwarded`, and a `TypeError` (unexpected `relay_id`).

- [ ] **Step 3: Implement**

`app/relays/inbound.py`, add:

```python
def relay_for_forwarded(request):
    """The accepted relay that HTTP-signed this request on behalf of the activity's author (Mastodon style)."""
    key_id = parse_signature_header(request.headers.get('Signature')).get('keyid', '')
    owner = key_id.split('#', 1)[0]
    if not owner:
        return None
    relay = db.session.query(Relay).filter(Relay.actor_id == owner, Relay.state == RELAY_ACCEPTED,
                                           Relay.public_key.isnot(None)).first()
    if relay is None or not _verified(request, relay):
        return None
    return relay
```

`app/activitypub/routes.py`:
- In `shared_inbox`, after the signature `try/except` block and before `if actor.instance_id:`, add:

  ```python
      relay = relay_for_forwarded(request) if bounced else None   # relays: a Mastodon-style relay signs forwards
  ```

  Pass `relay.id if relay else None` as `relay_id` in both `process_inbox_request(...)` and `process_inbox_request.delay(...)` calls, as a keyword argument.
- Change `def process_inbox_request(request_json, store_ap_json):` to `def process_inbox_request(request_json, store_ap_json, relay_id=None):`.
- At the start of its `with patch_db_session(session):` block, insert:

  ```python
                  if relay_id is not None:
                      allowed, reason = relayed_activity_allowed(request_json)
                      if not allowed:
                          log_incoming_ap(request_json.get('id', ''), APLOG_NOTYPE, APLOG_IGNORED,
                                          request_json if store_ap_json else None, reason)
                          return
  ```

- Wrap the rest of that block's body in `with relay_context(relay_id):`. Re-indenting a long function body is risky. Instead, use `token = current_relay_id.set(relay_id)` before the existing body and `current_relay_id.reset(token)` in the existing `finally:`. Add both lines and keep the indentation.
- Import `relay_for_forwarded`, `relayed_activity_allowed` and `current_relay_id` at module top.

- [ ] **Step 4: Run to verify they pass**

Run: `./run_tests.sh tests/test_relays_forwarded.py tests/test_relays_inbound.py tests/test_no_inline_imports.py -q`
Then a broader regression run of the inbox tests: `./run_tests.sh $(ls tests/test_inbox_*.py tests/test_ap_*.py | tr '\n' ' ') -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/relays/inbound.py app/activitypub/routes.py tests/test_relays_forwarded.py
git commit -m "feat: posts a Mastodon-style relay forwards are marked relayed and kept only when they are top-level and wanted

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Expiry

**Files:**
- Create: `app/relays/expiry.py`
- Modify: `app/cli.py` (the daily maintenance block, after `remove_old_bot_content()` ~L921); `coverage_floors.ini`
- Test: `tests/test_relays_expiry.py`

**Interfaces:**
- Consumes: `Post.relay_id`; `get_setting`; `get_task_session` and `patch_db_session`; the models `PostVote`, `PostReply`, `PostBookmark`, `Report`, `UserFollower`, `CommunityMember` and `User` (check the exact class names with `grep -n "^class PostVote\|^class PostBookmark\|^class Report\|^class UserFollower\|^class CommunityMember" app/models.py`).
- Produces:
  - `expire_relayed_posts()`, a Celery task returning `{'deleted': int, 'kept': int}`;
  - the constants `RELAY_EXPIRY_BATCH = 500` and `RELAY_EXPIRY_MAX = 5000`;
  - `relay_retention_days() -> int` (setting `relay_retention_days`, default 7).

- [ ] **Step 1: Write the failing tests**

`tests/test_relays_expiry.py`. Fixture:
- a relay;
- the local microblogs community;
- a remote author;
- `old_relayed(**kw)`, which makes a relayed post with `posted_at = utcnow() - timedelta(days=8)`.

Cases:
- **Deletion:** an old, unengaged relayed post is deleted (`db.session.get(Post, id) is None`) and the result is `{'deleted': 1, 'kept': 0}`.
- **Kept**, one test each:
  - a local user's vote (`PostVote` by a local user);
  - a local reply;
  - a local bookmark;
  - a report;
  - the author followed by a local user;
  - a post in a non-microblogs community with a local member;
  - sticky;
  - `status = POST_STATUS_REVIEWING`.
- **Not selected:**
  - a post younger than the retention period;
  - a non-relayed old post;
  - a relayed post whose relay was deleted (`relay_id` is now NULL).
- **Setting:** `relay_retention_days = 0` → nothing is deleted.
- **Cap:** with `RELAY_EXPIRY_MAX` monkeypatched to 3 and `RELAY_EXPIRY_BATCH` to 2, five deletable posts → 3 deleted, 2 remain.
- **Daily maintenance** calls it: monkeypatch `app.cli.expire_relayed_posts` to a recorder and invoke the daily CLI command with the app's CLI runner (`app.test_cli_runner().invoke(args=[<command name>])`; find the name in `app/cli.py` around L900). If that command does heavy work, monkeypatch its other steps too, or test only that the import and call exist. Choose whichever keeps the test fast, and note it in the report.

- [ ] **Step 2: Run to verify they fail**

Run: `./run_tests.sh tests/test_relays_expiry.py -q`
Expected: `ImportError`.

- [ ] **Step 3: Implement**

`app/relays/expiry.py`:

```python
"""Relayed posts nobody here engaged with expire (spec: Expiry)."""
from datetime import timedelta

from flask import current_app
from sqlalchemy import exists, and_

from app import celery
from app.constants import POST_STATUS_REVIEWING
from app.models import (Community, CommunityMember, Post, PostBookmark, PostReply, PostVote, Report, User,
                        UserFollower, utcnow)
from app.utils import get_setting, get_task_session, patch_db_session

RELAY_EXPIRY_BATCH = 500
RELAY_EXPIRY_MAX = 5000


def relay_retention_days() -> int:
    return int(get_setting('relay_retention_days', 7))


def _keep_clauses():
    local_user = and_(User.instance_id == 1)
    return [
        exists().where(PostVote.post_id == Post.id, PostVote.user_id == User.id, local_user),
        exists().where(PostReply.post_id == Post.id, PostReply.user_id == User.id, local_user),
        exists().where(PostBookmark.post_id == Post.id),
        exists().where(Report.suspect_post_id == Post.id),
        exists().where(UserFollower.remote_user_id == Post.user_id, UserFollower.local_user_id == User.id, local_user),
        exists().where(CommunityMember.community_id == Post.community_id, CommunityMember.user_id == User.id,
                       local_user, Community.id == Post.community_id, Community.name != 'microblogs'),
    ]


@celery.task
def expire_relayed_posts():
    days = relay_retention_days()
    if days <= 0:
        return {'deleted': 0, 'kept': 0}
    session = get_task_session()
    deleted = kept = 0
    try:
        with patch_db_session(session):
            cut_off = utcnow() - timedelta(days=days)
            candidates = session.query(Post.id).filter(Post.relay_id.isnot(None), Post.posted_at < cut_off)
            keep = session.query(Post.id).filter(Post.relay_id.isnot(None), Post.posted_at < cut_off).filter(
                (Post.sticky == True) | (Post.status <= POST_STATUS_REVIEWING) | _any(_keep_clauses()))
            kept = keep.count()
            doomed = [row[0] for row in candidates.except_(keep).limit(RELAY_EXPIRY_MAX).all()]
            for start in range(0, len(doomed), RELAY_EXPIRY_BATCH):
                for post in session.query(Post).filter(Post.id.in_(doomed[start:start + RELAY_EXPIRY_BATCH])):
                    post.delete_dependencies()
                    session.delete(post)
                    deleted += 1
                session.commit()
        current_app.logger.info(f'relays: expired {deleted} relayed posts, kept {kept}')
        return {'deleted': deleted, 'kept': kept}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

The block above is a sketch. Check each column name against `app/models.py` before relying on it: `PostVote.user_id`, `PostReply.post_id/user_id`, `PostBookmark.post_id`, `Report.suspect_post_id`, `UserFollower.local_user_id/remote_user_id`, `CommunityMember.user_id/community_id`, and "local user" as `User.ap_id is None` *or* `User.instance_id == 1`. Use whatever `User.is_local()` uses. Correlated `exists()` clauses must join through `User` correctly. Write each one as `exists().where(<child>.post_id == Post.id).where(<child>.user_id.in_(select(User.id).where(<local>)))` if that is clearer. Define `_any(clauses)` as `or_(*clauses)`.

The tests are the spec. Make every keep reason's test pass, and don't keep the sketch where it disagrees with the models.

In `app/cli.py`'s daily maintenance, directly after `remove_old_bot_content()`, add `expire_relayed_posts()`, imported at the top of `app/cli.py`.

`coverage_floors.ini`: add `app/relays/expiry.py = 100`.

- [ ] **Step 4: Run to verify they pass**

Run: `./run_tests.sh tests/test_relays_expiry.py tests/test_no_inline_imports.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/relays/expiry.py app/cli.py coverage_floors.ini tests/test_relays_expiry.py
git commit -m "feat: relayed posts nobody here engaged with expire after the retention period

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Admin page and CLI

**Files:**
- Create: `app/relays/forms.py`, `app/relays/admin_views.py`, `app/relays/cli.py`, `app/templates/admin/federation_relays.html`
- Modify: `app/admin/__init__.py` (import the views, as the discovery views are imported at L6); `app/cli.py` (register the commands, as `register_discovery_commands` at L61/L68); `app/templates/admin/_nav.html` (a "Relays" link next to "Discovery", L27); `app/templates/admin/federation.html` (a link near L39); `coverage_floors.ini`
- Test: `tests/test_relays_admin.py`

**Interfaces:**
- Consumes: Task 2's `add_relay`, `retry_relay`, `remove_relay` and `RelayError`; Task 5's `relay_retention_days`; `set_setting`.
- Produces: route `admin.admin_federation_relays` at `/admin/federation/relays` (GET and POST); CLI group `flask relays` with `add`, `remove`, `retry` and `list`.

- [ ] **Step 1: Write the failing tests**

`tests/test_relays_admin.py`. Use the admin-login pattern from the discovery admin tests (`grep -ln "admin_federation_discovery" tests/*.py`), and monkeypatch `app.relays.admin_views.add_relay`, `retry_relay` and `remove_relay` to recorders.

Cases:
- **Permissions:** anonymous → redirect to login; a logged-in non-admin → 403 (or whatever `permission_required` returns; assert that).
- **GET as admin:**
  - lists relay rows with url, style, state and the 24 h post count. Create two relayed posts with `posted_at` now and one older than 24 h; the count shows 2.
  - shows the allowlist note when `g.site.allowlist_mode` is strong or higher.
- **POST:**
  - add with a URL → `add_relay(url)` is called, a flash appears, and the response redirects;
  - `add_relay` raising `RelayError` → the error is flashed, and nothing crashes;
  - retry with an id → `retry_relay(row)`;
  - remove with an id → `remove_relay(row)`;
  - an unknown id → 404;
  - saving the retention setting stores `relay_retention_days`.
- **CLI:** `app.test_cli_runner().invoke(args=['relays', ...])`:
  - `add` calls `add_relay` and prints the state;
  - `add` with a `RelayError` prints the error with exit code 1;
  - `list` prints one line per relay;
  - `retry` and `remove` by URL call their functions;
  - an unknown URL prints "no such relay" with exit code 1.

- [ ] **Step 2: Run to verify they fail**

Run: `./run_tests.sh tests/test_relays_admin.py -q`
Expected: 404 or `ImportError`.

- [ ] **Step 3: Implement**

`app/relays/forms.py`. Field names are unique across the page; see the docstring of `app/discovery/forms.py` for why.

```python
from flask_babel import lazy_gettext as _l
from flask_wtf import FlaskForm
from wtforms import HiddenField, IntegerField, StringField, SubmitField
from wtforms.validators import DataRequired, InputRequired, NumberRange, URL


class RelayAddForm(FlaskForm):
    relay_url = StringField(_l('Relay URL (inbox or actor)'), validators=[DataRequired(), URL(require_tld=True)])
    relay_add = SubmitField(_l('Subscribe'))


class RelayActionForm(FlaskForm):
    relay_id = HiddenField(validators=[DataRequired()])
    relay_retry = SubmitField(_l('Retry'))
    relay_remove = SubmitField(_l('Remove'))


class RelaySettingsForm(FlaskForm):
    relay_retention = IntegerField(_l('Days to keep relayed posts nobody here engaged with (0 keeps them)'),
                                   validators=[InputRequired(), NumberRange(min=0, max=365)])
    relay_settings_save = SubmitField(_l('Save'))
```

`app/relays/admin_views.py`:

```python
"""The admin relays page (spec: Admin and CLI). Lives on the admin blueprint."""
from datetime import timedelta

from flask import abort, flash, g, redirect, request, url_for
from flask_babel import _
from sqlalchemy import func

from app import db
from app.admin import bp
from app.constants import ALLOWLIST_STRONG
from app.models import Post, Relay, utcnow
from app.relays.expiry import relay_retention_days
from app.relays.forms import RelayActionForm, RelayAddForm, RelaySettingsForm
from app.relays.subscribe import RelayError, add_relay, remove_relay, retry_relay
from app.utils import login_required, permission_required, render_template, set_setting


@bp.route('/federation/relays', methods=['GET', 'POST'])
@login_required
@permission_required('change instance settings')
def admin_federation_relays():
    add_form, action_form, settings_form = RelayAddForm(), RelayActionForm(), RelaySettingsForm()
    if request.method == 'POST':
        if add_form.relay_add.data and add_form.validate_on_submit():
            try:
                relay = add_relay(add_form.relay_url.data)
                flash(_('Subscribed to %(url)s; waiting for it to accept.', url=relay.url))
            except RelayError as error:
                flash(str(error), 'error')
        elif action_form.relay_id.data and action_form.validate_on_submit():
            relay = db.session.get(Relay, int(action_form.relay_id.data)) or abort(404)
            if action_form.relay_remove.data:
                remove_relay(relay)
                flash(_('Relay removed.'))
            else:
                retry_relay(relay)
                flash(_('Subscription request sent again.'))
        elif settings_form.relay_settings_save.data and settings_form.validate_on_submit():
            set_setting('relay_retention_days', settings_form.relay_retention.data)
            flash(_('Relay settings saved.'))
        return redirect(url_for('admin.admin_federation_relays'))
    settings_form.relay_retention.data = relay_retention_days()
    since = utcnow() - timedelta(hours=24)
    counts = dict(db.session.query(Post.relay_id, func.count(Post.id)).filter(
        Post.relay_id.isnot(None), Post.posted_at > since).group_by(Post.relay_id).all())
    relays = db.session.query(Relay).order_by(Relay.created_at).all()
    return render_template('admin/federation_relays.html', title=_('Federation settings - relays'),
                           relays=relays, counts=counts, add_form=add_form, action_form=action_form,
                           settings_form=settings_form,
                           allowlist_note=g.site.allowlist_mode >= ALLOWLIST_STRONG)
```

`retry_relay` can raise `RelayError` (from `detect_relay`). Catch it in the action branch too and flash it, with a test.

`app/templates/admin/federation_relays.html` follows `federation_discovery.html`'s skeleton: the same extends block and `{% include 'admin/_tabbed_nav.html' %}`, with `active_child = 'admin_relays'`. The body has:
- an h1 "Relays";
- when `allowlist_note`, a paragraph saying "This instance federates only with allowlisted servers: add each relay's host to the allowlist, or its deliveries are refused.";
- `render_form(add_form)`;
- a table (URL, style, state, answered, last error, posts in the last 24 h, actions). Each row has a small POST form carrying `action_form.csrf_token`, a hidden `relay_id` with the row id, and Retry and Remove submit buttons. Remove uses the existing `confirm_first` class;
- `render_form(settings_form)`.

`app/relays/cli.py`:

```python
"""Relay CLI commands (spec: Admin and CLI). Registered from app/cli.py."""
import sys

import click

from app import db
from app.models import Relay
from app.relays.subscribe import RelayError, add_relay, remove_relay, retry_relay


def register_relay_commands(app) -> None:
    @app.cli.group('relays')
    def relays():
        """Subscribe this instance to ActivityPub relays."""

    @relays.command('add')
    @click.argument('url')
    def add(url):
        try:
            relay = add_relay(url)
        except RelayError as error:
            click.echo(str(error))
            sys.exit(1)
        click.echo(f'{relay.url}: {relay.style}, {relay.state}')

    def _row(url):
        relay = db.session.query(Relay).filter_by(url=url.strip()).first()
        if relay is None:
            click.echo('no such relay')
            sys.exit(1)
        return relay

    @relays.command('remove')
    @click.argument('url')
    def remove(url):
        remove_relay(_row(url))
        click.echo('removed')

    @relays.command('retry')
    @click.argument('url')
    def retry(url):
        relay = _row(url)
        retry_relay(relay)
        click.echo(f'{relay.url}: {relay.state}')

    @relays.command('list')
    def list_relays():
        for relay in db.session.query(Relay).order_by(Relay.created_at):
            click.echo(f'{relay.url}\t{relay.style}\t{relay.state}\t{relay.last_error or ""}')
```

`retry` should catch `RelayError` the same way `add` does, with a test. Register it in `app/cli.py`: `from app.relays.cli import register_relay_commands` at the top, and `register_relay_commands(app)` next to `register_discovery_commands(app)`. In `app/admin/__init__.py`, add `from app.relays import admin_views as relay_admin_views  # noqa: E402,F401  fork: /admin/federation/relays` next to the discovery import.

`coverage_floors.ini`: add `app/relays/forms.py = 100`, `app/relays/admin_views.py = 100` and `app/relays/cli.py = 100`.

- [ ] **Step 4: Run to verify they pass**

Run: `./run_tests.sh tests/test_relays_admin.py tests/test_no_inline_imports.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/relays/ app/admin/__init__.py app/cli.py app/templates/admin/ coverage_floors.ini tests/test_relays_admin.py
git commit -m "feat: admins manage relays from the federation admin and the flask relays command

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Gates and verification

**Files:** none new. Fix whatever the gates find, test-first, in the owning task's files.

- [ ] **Step 1: Full suite with coverage**

Run: `./run_tests.sh tests/ --cov=app --cov=fastapi_server --cov-branch --cov-report=json:coverage.json -q`
Expected: 0 failed. The JS tests run first and pass.

- [ ] **Step 2: Changed lines and branches**

Run: `python3 tests/check_changed_line_coverage.py coverage.json 9be178f4a --branches app/ fastapi_server.py`
Expected: exit 0.

- [ ] **Step 3: Floors**

Run: `python3 tests/check_coverage_floors.py coverage.json coverage_floors.ini`
Expected: exit 0, including the new `app/relays/*` floors at 100.

- [ ] **Step 4: Migration round trip**

Inside the test container (it has the app and the DB):

```bash
podman-compose -f compose.test.yaml up -d && podman-compose -f compose.test.yaml exec -T test-runner sh -c 'flask db upgrade && flask db downgrade a3c9e5f1b7d4 && flask db upgrade'; ./run_tests.sh --down
```

Expected: all three commands succeed.

- [ ] **Step 5: Real federation (verification, not a gate; needs the owner's go-ahead)**

A local stack at `127.0.0.1` cannot receive deliveries from internet relays. Report that Steps 1-4 are done, and ask the owner whether to:
- deploy the branch to hell.cloud;
- subscribe it to one Mastodon-style relay and one FediBuzz tag relay with `flask relays add <url>`;
- watch `/admin/federation/relays` for `accepted`, and `/c/microblogs?sort=live` for relayed posts.

Do not do this without that go-ahead.
