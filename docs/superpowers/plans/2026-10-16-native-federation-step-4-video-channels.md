# Native Federation Step 4: Video Channels Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A video uploaded to a local `video_channel` community appears on a subscribed PeerTube instance as a native, playable video under that channel, with the right title, duration and thumbnail. Edits update it there, and a delete removes it. A moderator can make a community a video channel (or a podcast) from the create and edit forms and the API.

**Architecture:**
- **One new builder.** `app/activitypub/serializers/peertube.py` holds `uses_peertube_video(post)` and `peertube_video(post)`. `post_to_page` (fetch) and a new `send_peertube_video` (push) both call it, so push equals fetch.
- **Delivery per software.** PeerTube follower instances get one `Create`/`Update(Video)` signed by the author, and an owner-signed `Delete`. Microblog apps and threadiverse instances keep today's shapes, carrying the `Video`.
- **`Community.kind` becomes settable.** Forms and API apply one shared rule: offered values only, change only while the community has no posts. New communities record their creator in `Community.user_id`, the owner PeerTube needs.
- **The microblogs row is marked.** A data migration sets `kind='microblogs'`, and `is_local_microblogs` reads `kind`.

**Tech Stack:** Flask, WTForms, SQLAlchemy + Alembic, Celery (eager in tests), marshmallow (alpha API), pytest, atheris (fuzz track), and the podman interop harness with PeerTube v8.3.1.

**Spec:** `docs/superpowers/specs/2026-10-14-native-federation-design.md`. This plan covers §3.1 (microblogs marking), §3.2, §4.3, §6.1 (`video_channel` row), §6.2 and §8.3 (fuzz targets for the new builder and for ffprobe JSON). Amendments A1–A14 apply. Task 0 adds A15 below, whose rulings bind this plan.

## Global Constraints

- **Branch:** `feat/video-channels` off `blentz` at `9d48fee29`. Never push or merge.
- **Commits:**
  - Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`, or names the model that actually wrote it.
  - Make one commit per task, or per defect inside a fix round.
- **TDD:** write the failing test, run it red, then implement.
- **Coverage:** 100% line and branch coverage on changed lines, from the changed-line gate at base `9d48fee29` with `--branches`. Floor 100 for `app/activitypub/serializers/peertube.py` in `coverage_floors.ini`.
- **Gates:** run them only from repo-committed tools, never ad-hoc `/tmp` scripts. The coverage chain is the one in `tests/README.md` ("coverage ratchet"):
  ```
  ./run_tests.sh tests/ -q --cov=app --cov-branch --cov-report=json && \
  python3 tests/check_coverage_floors.py coverage.json coverage_floors.ini && \
  python3 tests/check_changed_line_coverage.py coverage.json 9d48fee29 --branches
  ```
- **No inline imports:** no new function-level imports unless technically unavoidable. `KEPT` in `tests/test_no_inline_imports.py` stays 85.
- **Slow tests:** a test that is slow or flaky is a bug to fix, not a timeout to raise.
- **Static files:** tests must leave no file under `app/static/media`, `app/static/tmp` or `app/static/posts` (`tests/static_leak_gate.py`).
- **Kinds** (`app/constants.py`): `COMMUNITY_KIND_DISCUSSION = 'discussion'`, `COMMUNITY_KIND_MICROBLOGS = 'microblogs'`, `COMMUNITY_KIND_VIDEO_CHANNEL = 'video_channel'`, `COMMUNITY_KIND_PODCAST = 'podcast'`. Forms and the API offer only discussion, video_channel and podcast.
- **PeerTube limits** (spec §4.3): `name` 3–120 characters; `duration` is the string `PT<n>S`; `uuid` is version 4; `content`, when present, is at least 3 characters with `mediaType` `text/markdown`; `Hashtag` names 2–30 characters; the file `Link` needs `height` and `size`; `icon` needs `width` and `height`; `to` holds `https://www.w3.org/ns/activitystreams#Public` in full; `commentsPolicy` is 1 (enabled) or 2 (disabled).
- **Instance software:** a PeerTube instance has `Instance.software == 'peertube'`, the literal the codebase already uses (`app/utils.py:4832`, `app/activitypub/util.py:1763`).

## Spec amendment A15 (Task 0 writes it; it binds this plan)

The code survey of 2026-10-16 found these gaps between the spec and the code. Rulings:

1. **Owner.** No local community records an owner in `Community.user_id` today: `add_local` and `make_community` leave it NULL, and ownership lives only in `CommunityMember.is_owner`. A video channel without it has no `attributedTo` Person, and PeerTube cannot subscribe (§6.2).
   - Every new local community, web or API, stores its creator in `Community.user_id`.
   - When an existing community becomes `video_channel` and its `user_id` is NULL, `user_id` is set to its owner member with the lowest `CommunityMember.id`, or to the editing user when it has no owner member.
   - Moving ownership later does not change `user_id`. PeerTube cannot change a channel's account either.
2. **Kind rules (§3.2).**
   - Create (web form and API `kind`) offers `discussion`, `video_channel` and `podcast`; the default is `discussion`.
   - Edit accepts a different kind only while the community has no `Post` rows, deleted ones included. Otherwise:
     - the web form refuses the POST with a form error on `kind`, and saves nothing;
     - the API refuses it with the error `kind can change only while the community has no posts`.
   - Once the community has posts, the web form shows the kind as a disabled select. A disabled select is not submitted, so a POST without `kind` keeps the stored kind.
   - A community of kind `microblogs` never changes kind, and `microblogs` is never accepted as a choice.
   - `podcast` is offered now because step 3 audio uploads need it. Until step 6, a podcast community's posts federate as `Page`.
3. **Microblogs marking (§3.1, A12).**
   - Migration `d4a7c1e9b3f6` sets `kind='microblogs'` on the row `find_microblogging_community` matches (`instance_id=1, user_id=1, name='microblogs'`). Its downgrade sets that row back to `discussion`.
   - `find_microblogging_community` creates the row with `kind='microblogs'`, and still finds it by name.
   - Both `is_local_microblogs` functions read `kind`: `app/utils.py` (`kind == 'microblogs' and is_local()`) and `app/community/live.py` (`kind == 'microblogs' and ap_id is None`).
   - Every other `'microblogs'` name test is kept. Each one deliberately also matches the `microblogs` communities of remote PieFed servers, which keep `kind='discussion'`:
     - the post-destination list (`app/utils.py`, `Community.name != 'microblogs'`);
     - the share page (`app/main/routes.py`);
     - relay expiry (`app/relays/expiry.py`);
     - `hide_community_actions` (`app/community/routes.py`, `app/post/routes.py`);
     - `Post.is_microblog`;
     - `LIVE_COMMUNITY` and the joined-server SQL (`app/community/live.py`, `app/utils.py` `JOINED_SERVER_SQL`).
4. **The `Video` builder (§4.3).**
   - `uses_peertube_video(post)` holds only when all of these are true:
     - the post is a `POST_TYPE_VIDEO` in a local `video_channel` community;
     - `is_open(post)` is true;
     - its media is `ready` with a `video/*` mime;
     - `post.image` has `width` and `height`.
   - Any other post in that community uses the `Page` path (§4.1).
   - `name` is the stripped title cut to 120 characters. A title shorter than 3 characters is padded with `.` to 3. The web forms already require 3; the API's edit may not.
   - `content` is `post.body` as `text/markdown`, sent only when the stripped body has at least 3 characters.
   - `tag` holds only the post's `Hashtag` entries whose name, without `#`, has 2 to 30 characters. Flair entries (`lemmy:CommunityTag`) are left out.
   - `url` holds the file `Link` and a `text/html` `Link` to the post page.
   - The file `Link`'s `mediaType` is the media mime, except that `video/quicktime` (MOV) is sent as `video/mp4`. PeerTube keeps a remote file `Link` only for `video/mp4`, `video/webm` and `video/ogg`, and a MOV holds the same streams.
   - The file name in `href` is the upload's random 15-character name (A13.4). It is unique, which is what §4.3 asks of `<uuid>-<height>.<ext>`.
   - The thumbnail `File` gets `width` and `height` when `probe_media` writes it, from the saved image. Before this, it had only `source_url`.
   - `likes` and `dislikes` are the collection URLs as strings, as in the spike.
   - `sensitive` is `nsfw or nsfl or content_warning` (A7). `language` is sent when the post has one.
5. **Delivery (§6.1).** `send_post` hands a post `uses_peertube_video` accepts to `send_peertube_video`. Each of the community's follower instances gets one activity:
   - `peertube`: the author's `Create(Video)`, or `Update(Video)` on edit.
   - `MICROBLOG_APPS`: on create, the community's `Announce` of the id; on edit, the author's `Update`. Both are as today.
   - Every other instance: the community's `Announce` of the author's activity, with `audience` on the activity and on the `Video` (A9).
   - Then each mentioned user's instance, and each instance where the author has followers, gets the author's activity once, if it got nothing above.
6. **Delete and restore (§6.1, §4.6).** For a post `uses_peertube_video` accepts, in a local community:
   - Delete: each `peertube` follower instance gets a `Delete` signed by the community's owner, `Community.user_id`, instead of the community's `Announce(Delete)`. A community without an owner sends PeerTube nothing.
   - Restore: each `peertube` follower instance gets the author's `Create(Video)` again, because PeerTube has no `Undo(Delete)`.
   - Every other instance keeps today's shapes.
7. **Fuzz (§8.3).** Atheris targets for `peertube_video` (over a fuzzed media record and title) and for `validate_probe` (over fuzzed ffprobe JSON) join the fuzz registry.

## Review Focus

1. **A video channel made in the web form or the API, which today stores no `Community.user_id`.** Without an owner its `Group` has no `attributedTo` Person, and PeerTube refuses to subscribe. Task 2 tests that create sets `user_id`, and that switching to `video_channel` fills a NULL `user_id`.
2. **Editing a community that has posts, where the kind select is disabled and so not submitted.** The missing field must keep the stored kind, not reset it to `discussion` or raise an error. Task 2 tests a POST without `kind`.
3. **A MOV upload.** PeerTube drops a `video/quicktime` `Link` and keeps a video with no file. Task 3 tests that the `Link` says `video/mp4`.
4. **A title of 1–2 or more than 120 characters, a body under 3 characters, or a hashtag outside 2–30.** Any one makes PeerTube reject the whole `Video`. Task 3 tests each edge.
5. **A delete by a moderator who is not the owner, or in a community without an owner.** PeerTube honours only the owner's `Delete`, and a missing owner must not crash the task. Task 5 tests both.

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `docs/superpowers/specs/2026-10-14-native-federation-design.md` | modify | A15 |
| `migrations/versions/d4a7c1e9b3f6_mark_microblogs_kind.py` | create | mark the microblogs row |
| `app/activitypub/util.py` | modify | `find_microblogging_community` sets kind; `post_to_page` dispatches to the Video builder |
| `app/utils.py`, `app/community/live.py` | modify | `is_local_microblogs` reads kind |
| `app/constants.py` | modify | `COMMUNITY_KINDS_OFFERED` |
| `app/shared/community.py` | modify | `apply_kind(community, kind, user) -> str \| None`; create and edit set kind and `user_id` |
| `app/community/forms.py`, `app/community/routes.py` | modify | `kind` select on create and edit |
| `app/templates/community/add_local.html`, `app/templates/community/community_edit.html` | modify | render `kind` |
| `app/api/alpha/schema.py`, `app/api/alpha/utils/community.py` | modify | `kind` on create and edit |
| `app/activitypub/serializers/peertube.py` | create | `uses_peertube_video`, `peertube_video` |
| `app/activitypub/serializers/__init__.py` | modify | export the two |
| `app/shared/tasks/media.py` | modify | thumbnail `File` gets `width`/`height` |
| `app/shared/tasks/pages.py` | modify | `send_peertube_video`; `send_post` branch |
| `app/shared/tasks/deletes.py` | modify | owner-signed `Delete` and restore `Create` to PeerTube |
| `coverage_floors.ini` | modify | floor 100 for the new module |
| `tests/fuzz/targets/…`, `tests/fuzz/surfaces.py`, `tests/fuzz/corpus/…` | modify/create | two fuzz targets |
| `interop/clients.py`, `interop/test_video_channel.py` | modify/create | end to end against PeerTube |
| `tests/test_microblogs_kind.py`, `tests/test_community_kind_rules.py`, `tests/test_peertube_video.py`, `tests/test_send_peertube_video.py`, `tests/test_delete_peertube_video.py` | create | unit tests |

---

### Task 0: Branch, spec amendment A15

**Files:**
- Modify: `docs/superpowers/specs/2026-10-14-native-federation-design.md` (append to §10, after A14)

- [ ] **Step 1: Branch**

```bash
git checkout blentz && git checkout -b feat/video-channels
git log --oneline -1   # expect 9d48fee29 (or the plan commit on top of it)
```

- [ ] **Step 2: Append A15.** Copy the "Spec amendment A15" section of this plan into §10 as a `- **A15 (§3.1, §3.2, §4.3, §6.1, §6.2, §8.3), step 4 video channels; code survey 2026-10-16:**` bullet with the seven numbered rulings nested under it, in the spec's existing A13 style. Add `(Amended: see §10 A15.)` under the headings §3.1, §3.2, §4.3 and §6.1.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-10-14-native-federation-design.md
git commit -m "docs(spec): A15, step 4 video channel rulings

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1: Mark the microblogs community's kind

**Files:**
- Create: `migrations/versions/d4a7c1e9b3f6_mark_microblogs_kind.py`
- Modify: `app/activitypub/util.py` (`find_microblogging_community`, ~line 7207), `app/utils.py:470-473`, `app/community/live.py:41-44`
- Test: `tests/test_microblogs_kind.py`; fix every test that builds a microblogs community by hand

**Interfaces:**
- Produces: the local microblogs community has `kind == COMMUNITY_KIND_MICROBLOGS`. Task 2 refuses kind changes on it.

- [ ] **Step 1: Write the failing tests.** Follow the migration test in `tests/test_video_channel_owner.py`.

```python
"""The local microblogs community is marked by kind (spec 2026-10-14 §3.1, §10 A15.3). Name tests that also match
a remote PieFed's microblogs community keep the name."""
import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from app import db
from app.activitypub.util import find_microblogging_community
from app.community.live import is_local_microblogs as live_is_local_microblogs
from app.constants import COMMUNITY_KIND_DISCUSSION, COMMUNITY_KIND_MICROBLOGS
from app.models import Community
from app.utils import is_local_microblogs
from tests.factories import make_community, make_instance, make_site, make_user

MIGRATION = Path(__file__).resolve().parent.parent / 'migrations' / 'versions' / 'd4a7c1e9b3f6_mark_microblogs_kind.py'


def _local():
    make_site()
    make_instance('test.piefed.local')
    make_user(None, 'admin', local=True)   # id 1: find_microblogging_community's owner


def test_a_new_microblogs_community_has_the_kind(app, db_session):
    _local()
    assert find_microblogging_community().kind == COMMUNITY_KIND_MICROBLOGS


def test_is_local_microblogs_reads_the_kind(app, db_session):
    _local()
    named = make_community('microblogs')          # the name alone no longer makes it the microblogs community
    assert not is_local_microblogs(named)
    assert not live_is_local_microblogs(named)
    named.kind = COMMUNITY_KIND_MICROBLOGS
    db.session.commit()
    assert is_local_microblogs(named)
    assert live_is_local_microblogs(named)


def test_a_remote_community_of_the_kind_is_not_the_local_one(app, db_session):
    _local()
    remote_instance = make_instance('other.example')
    remote = make_community('microblogs', instance=remote_instance)   # adapt to the factory's remote form
    remote.kind = COMMUNITY_KIND_MICROBLOGS
    db.session.commit()
    assert not is_local_microblogs(remote)
    assert not live_is_local_microblogs(remote)


def test_the_migration_marks_the_row_and_reverses(app, db_session):
    _local()
    row = make_community('microblogs')
    row.user_id, row.instance_id, row.kind = 1, 1, COMMUNITY_KIND_DISCUSSION
    other = make_community('books')
    db.session.commit()
    spec = importlib.util.spec_from_file_location('mark_microblogs_kind', MIGRATION)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    db.session.close()
    with db.engine.connect() as connection:
        def kind(community_id):
            return connection.execute(text('SELECT kind FROM community WHERE id = :id'), {'id': community_id}).scalar()
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert kind(row.id) == COMMUNITY_KIND_MICROBLOGS
        assert kind(other.id) == COMMUNITY_KIND_DISCUSSION
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert kind(row.id) == COMMUNITY_KIND_DISCUSSION
        connection.rollback()
```

If `make_community` has no remote form, build the remote row with the fields `make_community` sets, plus `ap_id='microblogs@other.example'` and the remote `instance_id`.

- [ ] **Step 2: Run red**

Run: `./run_tests.sh tests/test_microblogs_kind.py -q`
Expected: FAIL. The migration file is missing, the new row has kind `discussion`, and `is_local_microblogs(named)` is true.

- [ ] **Step 3: Implement.**

Check the head with `grep -l "down_revision = 'b7e3c9a1f5d2'" migrations/versions/*.py`, which should print nothing. If something prints, chain the new migration after that file instead.

```python
"""mark the local microblogs community's kind (spec 2026-10-14 §3.1, §10 A15.3)

Revision ID: d4a7c1e9b3f6
Revises: b7e3c9a1f5d2
Create Date: 2026-10-16

"""
from alembic import op


revision = 'd4a7c1e9b3f6'
down_revision = 'b7e3c9a1f5d2'
branch_labels = None
depends_on = None

# find_microblogging_community()'s row
ROW = "instance_id = 1 AND user_id = 1 AND name = 'microblogs'"


def upgrade():
    op.execute(f"UPDATE community SET kind = 'microblogs' WHERE {ROW}")


def downgrade():
    op.execute(f"UPDATE community SET kind = 'discussion' WHERE {ROW}")
```

`app/activitypub/util.py`, `find_microblogging_community`: add `kind=COMMUNITY_KIND_MICROBLOGS` to the `Community(...)` call and import the constant at module level. The lookup stays by name.

`app/utils.py`:

```python
def is_local_microblogs(community) -> bool:
    """The local `microblogs` community (find_microblogging_community), marked by kind (spec §10 A15.3): the only
    place a local user may publish a followers-only post (Amendment B, S7.1). A remote community does not count."""
    return community.kind == COMMUNITY_KIND_MICROBLOGS and community.is_local()
```

`app/community/live.py`:

```python
def is_local_microblogs(community) -> bool:
    """The row find_microblogging_community() returns, marked by kind (spec §10 A15.3): the community server views
    filter."""
    return community.kind == COMMUNITY_KIND_MICROBLOGS and community.ap_id is None
```

- [ ] **Step 4: Fix the fixtures.** Find tests that build a local microblogs community without `find_microblogging_community`:

```bash
grep -rln "'microblogs'" tests/*.py tests/**/*.py
```

Make each of them set `kind=COMMUNITY_KIND_MICROBLOGS`, or call `find_microblogging_community()`. Then run every file the grep listed, and every test that touches `is_local_microblogs`:

Run: `./run_tests.sh $(grep -rln "microblogs" tests --include=*.py) -q -p no:randomly`
Expected: PASS

- [ ] **Step 5: Run green**

Run: `./run_tests.sh tests/test_microblogs_kind.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add migrations/versions/d4a7c1e9b3f6_mark_microblogs_kind.py app/activitypub/util.py app/utils.py app/community/live.py tests/
git commit -m "feat: the local microblogs community is marked by kind

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Kind on create and edit; the owner in `Community.user_id`

**Files:**
- Modify: `app/constants.py`, `app/shared/community.py` (`make_community` ~line 222, `edit_community` ~line 329), `app/community/forms.py` (`AddCommunityForm`, `EditCommunityForm`), `app/community/routes.py` (`add_local` ~line 97, `community_edit` ~line 1518), `app/templates/community/add_local.html`, `app/templates/community/community_edit.html`, `app/api/alpha/schema.py` (`CreateCommunityRequest` ~line 793, `EditCommunityRequest` ~line 812), `app/api/alpha/utils/community.py` (`post_community`, `put_community`)
- Test: `tests/test_community_kind_rules.py`

**Interfaces:**
- Consumes: `COMMUNITY_KIND_MICROBLOGS` marks the local microblogs community (Task 1).
- Produces:
  - `COMMUNITY_KINDS_OFFERED = (COMMUNITY_KIND_DISCUSSION, COMMUNITY_KIND_VIDEO_CHANNEL, COMMUNITY_KIND_PODCAST)` in `app/constants.py`.
  - `apply_kind(community, kind, user) -> str | None` in `app/shared/community.py`. It returns an error message, or `None` once the kind is applied (or unchanged).
  - Every new local community has `user_id` = its creator.

- [ ] **Step 1: Write the failing tests.**

The routes are exercised with the Flask test client, logged in, with CSRF off as the existing community-form tests do; copy their login helper (see `tests/test_community_add_post.py`). The API is exercised through `app/api/alpha/utils/community.py` with an auth token, as `tests/test_api_*community*` do.

```python
"""Community.kind on create and edit, web and API (spec 2026-10-14 §3.2, §10 A15.1-2)."""
import pytest

from app import db
from app.constants import (COMMUNITY_KIND_DISCUSSION, COMMUNITY_KIND_MICROBLOGS, COMMUNITY_KIND_PODCAST,
                           COMMUNITY_KIND_VIDEO_CHANNEL)
from app.models import Community, CommunityMember
from app.shared.community import apply_kind
from tests.factories import make_community, make_post, make_site, make_instance, make_user


@pytest.fixture
def owner(app, db_session):
    make_site()
    make_instance('test.piefed.local')
    return make_user(None, 'alice', local=True)


def test_an_empty_community_takes_any_offered_kind(owner):
    community = make_community('vc')
    for kind in (COMMUNITY_KIND_VIDEO_CHANNEL, COMMUNITY_KIND_PODCAST, COMMUNITY_KIND_DISCUSSION):
        assert apply_kind(community, kind, owner) is None
        assert community.kind == kind


def test_microblogs_is_never_a_choice(owner):
    community = make_community('vc')
    assert apply_kind(community, COMMUNITY_KIND_MICROBLOGS, owner) == 'kind must be one of discussion, video_channel, podcast'
    assert community.kind == COMMUNITY_KIND_DISCUSSION


def test_the_microblogs_community_keeps_its_kind(owner):
    community = make_community('microblogs')
    community.kind = COMMUNITY_KIND_MICROBLOGS
    assert apply_kind(community, COMMUNITY_KIND_VIDEO_CHANNEL, owner) == 'the microblogs community keeps its kind'
    assert community.kind == COMMUNITY_KIND_MICROBLOGS


def test_a_community_with_a_post_keeps_its_kind(owner):
    community = make_community('vc')
    post = make_post(community, owner)
    post.deleted = True                       # deleted posts count too
    db.session.commit()
    assert apply_kind(community, COMMUNITY_KIND_VIDEO_CHANNEL, owner) == \
        'kind can change only while the community has no posts'
    assert community.kind == COMMUNITY_KIND_DISCUSSION
    assert apply_kind(community, COMMUNITY_KIND_DISCUSSION, owner) is None   # unchanged is always fine


def test_a_new_video_channel_without_an_owner_takes_its_owner_member(owner):
    community = make_community('vc')
    community.user_id = None
    other = make_user(None, 'bob', local=True)
    db.session.add_all([CommunityMember(user_id=owner.id, community_id=community.id, is_moderator=True, is_owner=True),
                        CommunityMember(user_id=other.id, community_id=community.id, is_moderator=True, is_owner=True)])
    db.session.commit()
    assert apply_kind(community, COMMUNITY_KIND_VIDEO_CHANNEL, other) is None
    assert community.user_id == owner.id      # the lowest owner membership, not the editor


def test_with_no_owner_member_the_editor_becomes_the_owner(owner):
    community = make_community('vc')
    community.user_id = None
    db.session.commit()
    assert apply_kind(community, COMMUNITY_KIND_VIDEO_CHANNEL, owner) is None
    assert community.user_id == owner.id


def test_an_existing_owner_is_kept(owner):
    community = make_community('vc')
    first = make_user(None, 'carol', local=True)
    community.user_id = first.id
    db.session.commit()
    assert apply_kind(community, COMMUNITY_KIND_VIDEO_CHANNEL, owner) is None
    assert community.user_id == first.id


def test_a_podcast_does_not_set_an_owner(owner):
    community = make_community('pod')
    community.user_id = None
    db.session.commit()
    assert apply_kind(community, COMMUNITY_KIND_PODCAST, owner) is None
    assert community.user_id is None
```

Add route and API tests to the same file, using the existing helpers:
- **Web create:** POST `/community/add_local` with `kind=video_channel` makes a community whose `kind` is `video_channel` and whose `user_id` is the creator. With no `kind` it is `discussion`; with `kind=microblogs` the form errors and makes no community.
- **Web edit, empty community:** POST `/community/<id>/edit` with `kind=podcast` saves `podcast`.
- **Web edit, community with a post:**
  - A POST with `kind=video_channel` re-renders with the `kind` field error `kind can change only while the community has no posts`, and the stored title is unchanged.
  - A POST with no `kind` field saves the other fields and keeps the kind (Review Focus 2).
  - A GET renders the select with `disabled`.
- **API create:** `kind=video_channel` sets the kind and `user_id`. `kind=microblogs` is refused by the schema (`OneOf`). No `kind` gives `discussion`, and `user_id` is the caller.
- **API edit:** `kind` on a community with a post raises `kind can change only while the community has no posts`, and nothing is saved. Without `kind`, the kind is kept.

- [ ] **Step 2: Run red**

Run: `./run_tests.sh tests/test_community_kind_rules.py -q`
Expected: FAIL with `ImportError: cannot import name 'apply_kind'`

- [ ] **Step 3: Implement the rule.** `app/constants.py`, after the kind constants:

```python
COMMUNITY_KINDS_OFFERED = (COMMUNITY_KIND_DISCUSSION, COMMUNITY_KIND_VIDEO_CHANNEL, COMMUNITY_KIND_PODCAST)
```

`app/shared/community.py`, using the module's own imports:

```python
def apply_kind(community, kind, user) -> str | None:
    """Set `community.kind` under spec §3.2 and §10 A15.2; the error message, or None once applied. The kind changes
    only while the community has no posts, deleted ones included, and microblogs is never chosen. A community that
    becomes a video channel needs an owner Person for PeerTube (§6.2): a NULL user_id becomes its first owner member,
    or else `user`."""
    if kind == community.kind:
        return None
    if kind not in COMMUNITY_KINDS_OFFERED:
        return 'kind must be one of ' + ', '.join(COMMUNITY_KINDS_OFFERED)
    if community.kind == COMMUNITY_KIND_MICROBLOGS:
        return 'the microblogs community keeps its kind'
    if db.session.query(Post.id).filter(Post.community_id == community.id).first() is not None:
        return 'kind can change only while the community has no posts'
    community.kind = kind
    if kind == COMMUNITY_KIND_VIDEO_CHANNEL and community.user_id is None:
        first_owner = db.session.query(CommunityMember.user_id).filter_by(community_id=community.id, is_owner=True) \
            .order_by(CommunityMember.id).first()
        community.user_id = first_owner.user_id if first_owner else user.id
    return None
```

If `CommunityMember` has no `id` column (check `app/models.py`), order by `CommunityMember.created_at, CommunityMember.user_id`. Then update the ruling text in the spec (A15.1) to match.

- [ ] **Step 4: Wire create.**
  - `AddCommunityForm`: `kind = SelectField(_l('Kind'), choices=KIND_CHOICES, default=COMMUNITY_KIND_DISCUSSION, render_kw={'class': 'form-select'})`, with module-level `KIND_CHOICES = [(COMMUNITY_KIND_DISCUSSION, _l('Discussion')), (COMMUNITY_KIND_VIDEO_CHANNEL, _l('Video channel')), (COMMUNITY_KIND_PODCAST, _l('Podcast'))]`. A SelectField refuses values outside its choices, so `microblogs` fails validation.
  - `add_local`: pass `kind=form.kind.data, user_id=current_user.id` to `Community(...)`.
  - `make_community`: read `input.get('kind', COMMUNITY_KIND_DISCUSSION)` and pass it with `user_id=user_id` to `Community(...)`.
  - `post_community`: forward `data.get('kind', COMMUNITY_KIND_DISCUSSION)` into `input`.
  - `CreateCommunityRequest`: `kind = fields.String(validate=validate.OneOf(COMMUNITY_KINDS_OFFERED))`. Use the module's existing `validate`/`OneOf` import style.
  - `add_local.html`: render `form.kind` beside the other selects, the way the template renders `form.invitations`.

- [ ] **Step 5: Wire edit.**
  - `EditCommunityForm`: the same `kind` field, plus `validators=[Optional()]`.
  - `community_edit`:
    - Set `locked = db.session.query(Post.id).filter(Post.community_id == community.id).first() is not None`.
    - When locked, set `form.kind.render_kw = {'class': 'form-select', 'disabled': True}`.
    - On GET, set `form.kind.data = community.kind`. When the community's kind is `microblogs`, also set `form.kind.choices = [(COMMUNITY_KIND_MICROBLOGS, _('Microblogs'))]` and disable the field.
    - Inside `if form.validate_on_submit():`, before `edit_community`, apply the kind:
      ```python
      kind = form.kind.data if form.kind.raw_data else community.kind   # a disabled select is not submitted (A15.2)
      refusal = shared_community.apply_kind(community, kind, current_user)
      if refusal is not None:
          db.session.rollback()
          form.kind.errors = list(form.kind.errors) + [refusal]
          return render_template('community/community_edit.html', title=_('Edit community'), form=form,
                                 current_app=current_app, current="edit_settings", community=community)
      ```
      The `edit_community` call that follows commits the kind with the rest.
  - `EditCommunityRequest`: `kind = fields.String(validate=validate.OneOf(COMMUNITY_KINDS_OFFERED))`.
  - `put_community`: put `data['kind']` into `input` only when it is present.
  - `edit_community`, SRC_API branch: after `can_moderate`, when `'kind' in input`, call `refusal = apply_kind(community, input['kind'], user)`. When it is not None, `raise Exception(refusal)` before anything is changed.
  - `community_edit.html`: render `form.kind` with its errors, like the other selects.

- [ ] **Step 6: Run green**

Run: `./run_tests.sh tests/test_community_kind_rules.py tests/test_video_channel_owner.py -q`
Expected: PASS. Also run the existing community form and API tests:

Run: `./run_tests.sh $(grep -rln "add_local\|community_edit\|post_community\|put_community\|make_community(" tests --include=*.py) -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app/constants.py app/shared/community.py app/community/forms.py app/community/routes.py \
    app/templates/community/add_local.html app/templates/community/community_edit.html \
    app/api/alpha/schema.py app/api/alpha/utils/community.py tests/test_community_kind_rules.py
git commit -m "feat: a moderator sets a community's kind; new communities record their owner

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: The `Video` builder, served on fetch

**Files:**
- Create: `app/activitypub/serializers/peertube.py`
- Modify: `app/activitypub/serializers/__init__.py`, `app/activitypub/util.py` (`post_to_page`, ~line 371), `app/shared/tasks/media.py` (`_thumbnail` / `_save_thumbnail_image`, ~lines 200-245), `coverage_floors.ini`
- Test: `tests/test_peertube_video.py`; extend `tests/test_media_probe.py`

**Interfaces:**
- Consumes: `media_of(post)` from `app.models`; the media record keys `uuid, mime, bytes, duration_s, width, height, fps`; `post.image` as the thumbnail (A13.4).
- Produces: `uses_peertube_video(post) -> bool` and `peertube_video(post) -> dict` (no `@context`), exported from `app.activitypub.serializers`.

- [ ] **Step 1: Write the failing tests.** Build a ready video post with the factories:
  - `community.kind = COMMUNITY_KIND_VIDEO_CHANNEL`, `post.type = POST_TYPE_VIDEO`;
  - `post.url = f"{SERVER_URL}/static/media/posts/ab/cd/abcdefghijklmno.mp4"`;
  - `with_media(post, status='ready', uuid=str(uuid.uuid4()), mime='video/mp4', bytes=12741, duration_s=10, width=640, height=360, fps=30)`;
  - `post.image = File(source_url=f'{SERVER_URL}/static/media/posts/ef/gh/thumbthumbthumb.jpg', width=640, height=360)`.

```python
def test_a_ready_video_is_a_peertube_video(video_post):
    video = peertube_video(video_post)
    server = current_app.config['SERVER_URL']
    assert video['type'] == 'Video'
    assert video['id'] == video_post.public_url()
    assert video['uuid'] == media_of(video_post)['uuid']
    assert video['name'] == video_post.title
    assert video['duration'] == 'PT10S'
    assert video['published'] and video['updated']
    assert video['views'] == 0
    assert video['likes'] == f'{server}/post/{video_post.id}/likes'
    assert video['dislikes'] == f'{server}/post/{video_post.id}/dislikes'
    assert video['url'][0] == {'type': 'Link', 'mediaType': 'video/mp4', 'href': video_post.url,
                               'height': 360, 'size': 12741, 'width': 640, 'fps': 30}
    assert video['url'][1] == {'type': 'Link', 'mediaType': 'text/html', 'href': video_post.public_url()}
    assert video['icon'] == [{'type': 'Image', 'url': video_post.image.source_url, 'mediaType': 'image/jpeg',
                              'width': 640, 'height': 360}]
    assert video['attributedTo'] == [{'type': 'Person', 'id': video_post.author.public_url()},
                                     {'type': 'Group', 'id': video_post.community.public_url()}]
    assert video['to'] == ['https://www.w3.org/ns/activitystreams#Public']
    assert video['cc'] == [video_post.community.ap_followers_url]
    assert video['commentsPolicy'] == 1
    assert (video['state'], video['waitTranscoding'], video['isLiveBroadcast']) == (1, False, False)


def test_fetch_equals_push(video_post):
    assert post_to_page(video_post) == peertube_video(video_post)


@pytest.mark.parametrize('title, name', [('ab', 'ab.'), ('x', 'x..'), ('  abc  ', 'abc'), ('t' * 121, 't' * 120)])
def test_the_name_fits_peertube(video_post, title, name):
    video_post.title = title
    assert peertube_video(video_post)['name'] == name


def test_a_mov_is_sent_as_mp4(video_post):
    with_media(video_post, mime='video/quicktime')
    assert peertube_video(video_post)['url'][0]['mediaType'] == 'video/mp4'


def test_webm_keeps_its_type_and_no_fps_is_left_out(video_post):
    with_media(video_post, mime='video/webm', fps=None, width=0)
    link = peertube_video(video_post)['url'][0]
    assert link['mediaType'] == 'video/webm'
    assert 'fps' not in link and 'width' not in link


@pytest.mark.parametrize('body, sent', [('', False), ('ab ', False), ('abc', True)])
def test_the_description_is_sent_from_three_characters(video_post, body, sent):
    video_post.body = body
    video = peertube_video(video_post)
    assert ('content' in video) is sent
    if sent:
        assert (video['content'], video['mediaType']) == ('abc', 'text/markdown')


def test_only_hashtags_of_two_to_thirty_characters_are_sent(video_post):
    # attach tags 'a', 'ok', 't'*30, 't'*31 and one flair, as test_microblog_body_tags_forms.py attaches tags
    names = [tag['name'].lstrip('#') for tag in peertube_video(video_post)['tag']]
    assert names == ['ok', 't' * 30]


def test_comments_off_is_policy_two_and_flags_are_sensitive(video_post):
    video_post.comments_enabled = False
    video_post.content_warning = 'cw'
    video = peertube_video(video_post)
    assert video['commentsPolicy'] == 2
    assert video['sensitive'] is True


def test_language_is_sent_when_set(video_post):
    # give the post a language (as test_shared_tasks_send_post.py does)
    assert peertube_video(video_post)['language'] == {'identifier': video_post.language_code(),
                                                      'name': video_post.language_name()}


@pytest.mark.parametrize('change', ['discussion', 'remote', 'link', 'processing', 'audio', 'followers', 'no_image',
                                    'no_dims', 'no_media'])
def test_anything_else_is_not_a_peertube_video(video_post, change):
    # discussion: community.kind = discussion; remote: the community has an ap_id and a remote instance;
    # link: post.type = POST_TYPE_LINK; processing: media status 'processing'; audio: mime 'audio/mpeg';
    # followers: visibility followers-only; no_image: post.image = None; no_dims: image.width = None;
    # no_media: extensions without 'media'
    ...
    assert uses_peertube_video(video_post) is False


def test_a_text_post_in_a_video_channel_is_a_page(video_post):
    video_post.type = POST_TYPE_ARTICLE
    assert post_to_page(video_post)['type'] == 'Page'
```

Write out each parametrized `change` as a real mutation, not `...`.

In `tests/test_media_probe.py`, add `test_the_thumbnail_records_its_size`: after a ready `video_h264.mp4` probe, `post.image.width` and `post.image.height` are the saved thumbnail's pixel size. Open the file with Pillow to compare.

- [ ] **Step 2: Run red**

Run: `./run_tests.sh tests/test_peertube_video.py tests/test_media_probe.py -q -k "peertube or thumbnail_records"`
Expected: FAIL with `ModuleNotFoundError: app.activitypub.serializers.peertube`

- [ ] **Step 3: Implement the builder** in `app/activitypub/serializers/peertube.py`:

```python
"""The PeerTube Video a local video_channel post is pushed and fetched as (spec 2026-10-14 §4.3, §10 A11, A15.4).
Every field PeerTube v8.3.1 requires is always present; an optional field is sent only in a form PeerTube accepts,
since one bad field rejects the whole object."""
from flask import current_app

from app.constants import COMMUNITY_KIND_VIDEO_CHANNEL, POST_TYPE_VIDEO
from app.models import media_of
from app.utils import ap_datetime, guess_mime_type
from app.visibility import is_open

PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'
NAME_MIN, NAME_MAX = 3, 120
CONTENT_MIN = 3
TAG_MIN, TAG_MAX = 2, 30
COMMENTS_ENABLED, COMMENTS_DISABLED = 1, 2
# PeerTube keeps a remote file Link only for mp4, webm and ogg; a MOV holds the same streams an mp4 does
LINK_MIME = {'video/quicktime': 'video/mp4'}


def uses_peertube_video(post) -> bool:
    """A public, ready video upload in a local video_channel community whose thumbnail has a size. Anything else in
    that community is a Page (spec §4.1)."""
    community = post.community
    media = media_of(post)
    image = post.image
    return bool(community.kind == COMMUNITY_KIND_VIDEO_CHANNEL and community.is_local()
                and post.type == POST_TYPE_VIDEO and is_open(post)
                and media is not None and media.get('status') == 'ready'
                and str(media.get('mime', '')).startswith('video/')
                and image is not None and image.width and image.height)


def video_name(title) -> str:
    name = (title or '').strip()[:NAME_MAX]
    return name.ljust(NAME_MIN, '.')


def _file_link(post, media) -> dict:
    link = {'type': 'Link', 'mediaType': LINK_MIME.get(media['mime'], media['mime']), 'href': post.url,
            'height': media['height'], 'size': media['bytes']}
    if media.get('width'):
        link['width'] = media['width']
    if media.get('fps'):
        link['fps'] = media['fps']
    return link


def _hashtags(post) -> list:
    return [tag for tag in post.tags_for_activitypub()
            if tag.get('type') == 'Hashtag' and TAG_MIN <= len(tag['name'].lstrip('#')) <= TAG_MAX]


def peertube_video(post) -> dict:
    """The Video, without @context."""
    media = media_of(post)
    community = post.community
    base = f"{current_app.config['SERVER_URL']}/post/{post.id}"
    video = {
        'id': post.public_url(),
        'type': 'Video',
        'uuid': media['uuid'],
        'name': video_name(post.title),
        'duration': f"PT{media['duration_s']}S",
        'published': ap_datetime(post.posted_at),
        'updated': ap_datetime(post.edited_at or post.posted_at),
        'views': 0,
        'likes': f'{base}/likes',
        'dislikes': f'{base}/dislikes',
        'url': [_file_link(post, media), {'type': 'Link', 'mediaType': 'text/html', 'href': post.public_url()}],
        'icon': [{'type': 'Image', 'url': post.image.source_url, 'mediaType': guess_mime_type(post.image.source_url),
                  'width': post.image.width, 'height': post.image.height}],
        'attributedTo': [{'type': 'Person', 'id': post.author.public_url()},
                         {'type': 'Group', 'id': community.public_url()}],
        'to': [PUBLIC],
        'cc': [community.ap_followers_url],
        'commentsPolicy': COMMENTS_ENABLED if post.comments_enabled else COMMENTS_DISABLED,
        'sensitive': bool(post.nsfw or post.nsfl or post.content_warning),
        'state': 1,
        'waitTranscoding': False,
        'isLiveBroadcast': False,
        'downloadEnabled': True,
        'tag': _hashtags(post),
    }
    if len((post.body or '').strip()) >= CONTENT_MIN:
        video['content'] = post.body
        video['mediaType'] = 'text/markdown'
    if post.language_id:
        video['language'] = {'identifier': post.language_code(), 'name': post.language_name()}
    return video
```

Before you rely on two assumptions, check them:
- `post.url` of a local upload is an absolute URL. Read `store_uploaded_media` in `app/shared/media.py`. If it is a path, build `href` with `SERVER_URL` the way `media_local_path` reads it.
- `tags_for_activitypub()` gives `Hashtag` entries a `name` that starts with `#` (`app/models.py:3820`).

`app/activitypub/serializers/__init__.py`: import and export `peertube_video` and `uses_peertube_video` beside the microblog names.

`app/activitypub/util.py`, `post_to_page`, after the microblog branch, in the module's existing serializer import style:

```python
    if peertube_serializer.uses_peertube_video(post):    # spec §10 A15.4: push and fetch share one builder
        return peertube_serializer.peertube_video(post)
```

`app/shared/tasks/media.py`:
- `_save_thumbnail_image` returns `(image_place, img.size)`, the size after `img.thumbnail(...)`.
- `_thumbnail` builds `File(source_url=..., width=width, height=height)` from it.

`coverage_floors.ini`: add `app/activitypub/serializers/peertube.py = 100` in the file's existing format.

- [ ] **Step 4: Run green**

Run: `./run_tests.sh tests/test_peertube_video.py tests/test_media_probe.py tests/test_activitypub_post_to_page*.py -q`
Expected: PASS. If the file name differs, find the `post_to_page` tests with `grep -rln post_to_page tests`.

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/serializers/ app/activitypub/util.py app/shared/tasks/media.py coverage_floors.ini \
    tests/test_peertube_video.py tests/test_media_probe.py
git commit -m "feat: a ready video in a video channel is a PeerTube Video on fetch

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Push the `Video`

**Files:**
- Modify: `app/shared/tasks/pages.py` (`send_post` ~line 175; new `send_peertube_video` beside `send_microblog_note`)
- Test: `tests/test_send_peertube_video.py`

**Interfaces:**
- Consumes: `uses_peertube_video`, `peertube_video` (Task 3); `can_deliver_to`, `send_post_request`, `default_context`, `gibberish`, `MICROBLOG_APPS`, already imported in `pages.py`.
- Produces: `send_peertube_video(post, user, community, recipients: list, edit: bool) -> None`.

- [ ] **Step 1: Write the failing tests.** Patch `app.shared.tasks.pages.send_post_request` and collect `(inbox, body, key_id)` calls, as `tests/test_shared_tasks_send_post.py` does. The video channel has follower instances `peertube.example` (software `peertube`), `mastodon.example` (`mastodon`) and `lemmy.example` (`lemmy`). The author has a remote follower on `follower.example`, and the body mentions a user on `mention.example`. Each instance is online with an inbox.

```python
def test_peertube_gets_the_authors_create_of_the_video(sent, video_post):
    send_post(video_post.id, session=db.session)
    body, key = sent['https://peertube.example/inbox']
    assert body['type'] == 'Create' and body['actor'] == video_post.author.public_url()
    assert body['object'] == peertube_video(video_post)
    assert key == video_post.author.public_url() + '#main-key'


def test_a_microblog_app_gets_the_communitys_announce_of_the_id(sent, video_post):
    send_post(video_post.id, session=db.session)
    body, key = sent['https://mastodon.example/inbox']
    assert (body['type'], body['object']) == ('Announce', video_post.ap_id)
    assert key == video_post.community.public_url() + '#main-key'


def test_the_threadiverse_gets_the_announce_of_the_create_with_audience(sent, video_post):
    send_post(video_post.id, session=db.session)
    body, key = sent['https://lemmy.example/inbox']
    audience = video_post.community.public_url()
    assert body['type'] == 'Announce' and body['object']['type'] == 'Create'
    assert body['object']['audience'] == audience and body['object']['object']['audience'] == audience
    assert body['object']['object']['type'] == 'Video'


def test_followers_and_mentions_get_the_authors_create_once(sent, video_post):
    send_post(video_post.id, session=db.session)
    for inbox in ('https://follower.example/inbox', 'https://mention.example/inbox'):
        assert sent[inbox][0]['type'] == 'Create'
    assert len(sent.calls) == 5            # one per instance


def test_an_edit_is_the_authors_update_to_peertube_and_microblog_apps(sent, video_post):
    send_post(video_post.id, edit=True, session=db.session)
    assert sent['https://peertube.example/inbox'][0]['type'] == 'Update'
    assert sent['https://mastodon.example/inbox'][0]['type'] == 'Update'
    assert sent['https://lemmy.example/inbox'][0]['object']['type'] == 'Update'


def test_an_undeliverable_instance_is_skipped(sent, video_post):
    # make peertube.example offline (or banned, or blocked by the author)
    send_post(video_post.id, session=db.session)
    assert 'https://peertube.example/inbox' not in sent


def test_a_processing_video_is_not_sent(sent, video_post):
    with_media(video_post, status='processing')
    send_post(video_post.id, session=db.session)
    assert not sent.calls


def test_a_text_post_in_a_video_channel_keeps_the_page_path(sent, video_post):
    video_post.type = POST_TYPE_ARTICLE
    send_post(video_post.id, session=db.session)
    assert sent['https://lemmy.example/inbox'][0]['object']['object']['type'] == 'Page'
```

`sent` is a fixture: a dict keyed by inbox holding `(body, key_id)`, plus `.calls`, the list of every call. Copy its shape from the existing send_post tests, so a duplicate send to one inbox is visible in `.calls`.

- [ ] **Step 2: Run red**

Run: `./run_tests.sh tests/test_send_peertube_video.py -q`
Expected: FAIL. PeerTube gets the group `Announce` of a `Page`.

- [ ] **Step 3: Implement.** Add to `app/shared/tasks/pages.py`, after `send_microblog_note`:

```python
def send_peertube_video(post, user, community, recipients: list, edit: bool) -> None:
    """Push a ready video of a local video_channel community as the Video peertube_video builds (spec 2026-10-14
    §6.1, §10 A11, A15.5). One activity per instance. PeerTube gets the author's Create (or Update): it ignores the
    Announce of a Create that threadiverse followers get. Microblog apps get the community's Announce of the id, or the
    author's Update on edit, as for any post. Other followers get the community's Announce of the author's activity,
    naming the community as its audience (§10 A9). Then the author's followers and the users the body mentions get the
    author's activity."""
    video = peertube_video(post)
    verb = 'update' if edit else 'create'
    activity = {'id': f"{current_app.config['SERVER_URL']}/activities/{verb}/{gibberish(15)}",
                'type': 'Update' if edit else 'Create', 'actor': user.public_url(), 'object': video,
                'to': video['to'], 'cc': video['cc'], '@context': default_context()}
    audience = community.public_url()
    inner = {key: value for key, value in activity.items() if key != '@context'}
    microblog_announce = {'id': f"{current_app.config['SERVER_URL']}/activities/announce/{gibberish(15)}",
                          'type': 'Announce', 'actor': community.public_url(), 'object': post.ap_id,
                          'to': video['to'], 'cc': [community.ap_followers_url], '@context': default_context()}
    group_announce = dict(microblog_announce, object=dict(inner, audience=audience, object=dict(video, audience=audience)))
    author_key, community_key = user.public_url() + '#main-key', community.public_url() + '#main-key'

    sent_to = set()
    for instance in community.following_instances():
        if not can_deliver_to(user, instance):
            continue
        if instance.software == 'peertube' or (edit and instance.software in MICROBLOG_APPS):
            send_post_request(instance.inbox, activity, user.private_key, author_key)
        elif instance.software in MICROBLOG_APPS:
            send_post_request(instance.inbox, microblog_announce, community.private_key, community_key)
        else:
            send_post_request(instance.inbox, group_announce, community.private_key, community_key)
        sent_to.add(instance.domain)

    targets = [recipient.instance for recipient in recipients if recipient.instance_id != 1]
    for instance in targets + user.following_instances():
        if instance.domain in sent_to or not can_deliver_to(user, instance):
            continue
        send_post_request(instance.inbox, activity, user.private_key, author_key)
        sent_to.add(instance.domain)
```

In `send_post`, after the `uses_microblog_note` branch:

```python
    if uses_peertube_video(post):   # spec 2026-10-14 §10 A15.5: the Video push and fetch share one builder
        send_peertube_video(post, user, community, recipients, edit)
        return
```

Import `peertube_video, uses_peertube_video` from `app.activitypub.serializers` on the existing import line.

- [ ] **Step 4: Run green**

Run: `./run_tests.sh tests/test_send_peertube_video.py tests/test_shared_tasks_send_post.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/shared/tasks/pages.py tests/test_send_peertube_video.py
git commit -m "feat: a video channel's video goes to PeerTube as the author's Create(Video)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Owner-signed `Delete`, and restore as `Create`

**Files:**
- Modify: `app/shared/tasks/deletes.py` (`delete_object`, the `elif community.is_local():` arm, ~line 210)
- Test: `tests/test_delete_peertube_video.py`

**Interfaces:**
- Consumes: `uses_peertube_video`, `peertube_video` (Task 3).

- [ ] **Step 1: Write the failing tests.** Use the same follower instances and `sent` fixture as Task 4. `delete_object(user_id, post, is_post=True, session=db.session)` is called directly, the way the existing delete tests do.

```python
def test_peertube_gets_the_owners_delete(sent, video_post, owner):
    delete_object(video_post.author.id, video_post, is_post=True, session=db.session)
    body, key = sent['https://peertube.example/inbox']
    assert (body['type'], body['actor'], body['object']) == ('Delete', owner.public_url(), video_post.public_url())
    assert key == owner.public_url() + '#main-key'


def test_a_moderators_removal_is_still_signed_by_the_owner(sent, video_post, owner, moderator):
    delete_object(moderator.id, video_post, is_post=True, reason='spam', session=db.session)
    assert sent['https://peertube.example/inbox'][0]['actor'] == owner.public_url()


def test_other_followers_keep_the_communitys_announce(sent, video_post):
    delete_object(video_post.author.id, video_post, is_post=True, session=db.session)
    assert sent['https://lemmy.example/inbox'][0]['type'] == 'Announce'
    assert sent['https://mastodon.example/inbox'][0]['type'] == 'Announce'


def test_without_an_owner_peertube_gets_nothing(sent, video_post):
    video_post.community.user_id = None
    db.session.commit()
    delete_object(video_post.author.id, video_post, is_post=True, session=db.session)
    assert 'https://peertube.example/inbox' not in sent
    assert 'https://lemmy.example/inbox' in sent


def test_a_restore_sends_peertube_the_create_again(sent, video_post):
    delete_object(video_post.author.id, video_post, is_post=True, is_restore=True, session=db.session)
    body, key = sent['https://peertube.example/inbox']
    assert (body['type'], body['actor']) == ('Create', video_post.author.public_url())
    assert body['object'] == peertube_video(video_post)


def test_a_page_post_keeps_the_announce_for_peertube(sent, video_post):
    video_post.type = POST_TYPE_ARTICLE
    delete_object(video_post.author.id, video_post, is_post=True, session=db.session)
    assert sent['https://peertube.example/inbox'][0]['type'] == 'Announce'
```

- [ ] **Step 2: Run red**

Run: `./run_tests.sh tests/test_delete_peertube_video.py -q`
Expected: FAIL. PeerTube gets the community's `Announce(Delete)`.

- [ ] **Step 3: Implement.** In `delete_object`, inside `elif community.is_local():`, replace the follower loop:

```python
        video = is_post and uses_peertube_video(object)   # spec §10 A15.6: PeerTube takes a video's Delete only from its channel's account
        owner = session.get(User, community.user_id) if video and community.user_id else None
        for instance in community.following_instances():
            if instance.inbox and instance.online() and not user.has_blocked_instance(instance.id) and not instance_banned(instance.domain):
                if video and instance.software == 'peertube':
                    if is_restore:      # PeerTube has no Undo(Delete): the video is sent again
                        send_post_request(instance.inbox, _video_create(object), object.author.private_key,
                                          object.author.public_url() + '#main-key')
                    elif owner is not None:
                        send_post_request(instance.inbox, dict(delete, actor=owner.public_url(),
                                                               **{'@context': default_context()}),
                                          owner.private_key, owner.public_url() + '#main-key')
                    domains_sent_to.append(instance.domain)
                    continue
                send_post_request(instance.inbox, announce, community.private_key, community.public_url() + '#main-key')
                domains_sent_to.append(instance.domain)
```

Add a module helper:

```python
def _video_create(post) -> dict:
    video = peertube_video(post)
    return {'id': f"{current_app.config['SERVER_URL']}/activities/create/{gibberish(15)}", 'type': 'Create',
            'actor': post.author.public_url(), 'object': video, 'to': video['to'], 'cc': video['cc'],
            '@context': default_context()}
```

`delete` has had its `@context` removed before this arm, so the owner's copy adds it back. Leave the `summary` (reason) in place: PeerTube ignores it. Import `peertube_video, uses_peertube_video` from `app.activitypub.serializers` at module level, and confirm that adds no import cycle (`python -c "import app"` with the test config, or the suite's import test).

- [ ] **Step 4: Run green**

Run: `./run_tests.sh tests/test_delete_peertube_video.py $(grep -rln "delete_object\|restore_post" tests --include=*.py) -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/shared/tasks/deletes.py tests/test_delete_peertube_video.py
git commit -m "feat: a video channel's video is deleted on PeerTube by the owner's Delete

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Fuzz targets for the builder and for ffprobe JSON

**Files:**
- Modify: `tests/fuzz/surfaces.py`, `tests/fuzz/targets/` (the module that fits; read `tests/fuzz/targets/__init__.py` for `REGISTRY`, `SURFACE_MODULES` and `Target`)
- Create: `tests/fuzz/corpus/<target names>/` seeds

**Interfaces:**
- Consumes: `peertube_video` (Task 3); `validate_probe(probe: dict, kind: str) -> dict` from `app/shared/tasks/media.py`, which raises `MediaToolError` on bad input.

- [ ] **Step 1: Read the track.** Read `tests/fuzz/surfaces.py`, `tests/fuzz/targets/__init__.py`, one existing target module, and `tests/test_fuzz_registry.py`. These say how a surface is declared, how a target is registered, what a corpus directory holds, and what "no finding" means for a target (an expected exception vs. a crash).

- [ ] **Step 2: Add the two targets.**
  - **`validate_probe`:** decode the fuzz bytes as JSON. A non-dict is skipped. Call `validate_probe(data, kind)` for both kinds. Only `MediaToolError` is an expected outcome; any other exception is a finding. Seed the corpus with the real ffprobe JSON of each `tests/fixtures/media` file. Make the JSON in the test container with `ffprobe -v error -print_format json -show_format -show_streams -protocol_whitelist file <fixture>`, and commit it as corpus files.
  - **`peertube_video`:** build an unsaved video post in the target's app context, the way other model-backed targets do. Fill the media record (`duration_s`, `width`, `height`, `fps`, `bytes`, `mime`) and the title and body from the fuzz bytes. Every returned object must satisfy the Global Constraints' PeerTube limits:
    - `3 <= len(name) <= 120`;
    - `duration` matches `^PT\d+S$`;
    - `content` is absent or 3+ characters;
    - every tag is 2–30 characters.

    Any violation is a finding. Seed the corpus with the Task 3 test values.
  - If either target finds a real defect, fix it TDD in its own commit, with a unit test first.

- [ ] **Step 3: Run the registry gate and a short campaign**

Run: `./run_tests.sh tests/test_fuzz_registry.py tests/test_fuzz_smoke.py -q`
Expected: PASS

Then run the two targets for a short time with the campaign command `tests/README.md` documents for the fuzz track, e.g. `python3 tests/fuzz/run_campaign.py --target <name> --seconds 120`.
Expected: no findings, or each one fixed.

- [ ] **Step 4: Commit**

```bash
git add tests/fuzz/
git commit -m "test(fuzz): targets for peertube_video and validate_probe

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Interop end to end, gates, evidence

**Files:**
- Modify: `interop/clients.py` (`PeerTube`), `interop/README.md`, `docs/superpowers/specs/2026-10-14-native-federation-design.md` (A15 gates line)
- Create: `interop/test_video_channel.py`

**Interfaces:**
- Consumes: the harness (`compose.interop.yaml`, project `pyfedi-interop`), the seeded `vc_media` (video_channel, `user_id` alice, alice an owner moderator), `PieFedWeb.submit_media`, `PieFed.edit`, `PieFed.delete`, `PieFed.get_post`, `PeerTube.subscribe/unsubscribe/channel_videos/video`, and `interop/test_media_pipeline.py`'s `_upload_and_wait_ready`.

- [ ] **Step 1: Add client methods** to `PeerTube` in `interop/clients.py`, in the style of the existing ones:
  - `rate(self, video_id, rating)`: `PUT /api/v1/videos/{video_id}/rate` with `{'rating': rating}`.
  - `video_or_none(self, video_id)`: `GET /api/v1/videos/{video_id}`. It returns `None` on 404 and raises on any other error.

- [ ] **Step 2: Write the test** `interop/test_video_channel.py`:

```python
"""A video uploaded to a PieFed video_channel shows on PeerTube as a native, playable video under the subscribed
channel; a like comes back as an upvote; an edit renames it; a delete removes it (spec 2026-10-14 §4.3, §6.1,
§10 A11, A15)."""
import math
import subprocess

import pytest

from interop.clients import wait_for
from interop.test_media_pipeline import _upload_and_wait_ready

pytestmark = pytest.mark.interop


def test_a_video_channel_upload_plays_on_peertube_and_round_trips(peertube, alice_web, alice_api):
    peertube.unsubscribe('vc_media@piefed.test')
    peertube.subscribe('vc_media@piefed.test')
    # wait for the subscription to be accepted, as test_peertube_subscribe.py does
    ...
    url, post_id = _upload_and_wait_ready(alice_web, 'vc_media', 'video', 'video_h264.mp4', 'video/mp4')  # adapt to its return

    def listed():
        return next((v for v in peertube.channel_videos('vc_media@piefed.test')['data']
                     if v['name'] == title), None)
    video = wait_for(listed, timeout=60)
    detail = peertube.video(video['uuid'])
    assert detail['duration'] == expected_duration      # ceil of ffprobe's duration, as test_media_pipeline does
    assert detail['files'][0]['fileUrl'] == url
    assert detail['thumbnailPath']

    peertube.rate(video['uuid'], 'like')
    wait_for(lambda: alice_api.get_post(post_id)['post_view']['counts']['upvotes'] >= 1, timeout=30)

    alice_api.edit(post_id, title=title + ' renamed')
    wait_for(lambda: peertube.video(video['uuid'])['name'] == title + ' renamed', timeout=30)

    alice_api.delete(post_id)
    wait_for(lambda: peertube.video_or_none(video['uuid']) is None, timeout=30)
```

Replace `...` and the adapt comments with real code from the neighbouring interop tests: their fixtures, the post id from the upload redirect, and the API's post-view shape. Use a unique `title` per run (e.g. with `uuid4().hex[:6]`), so reused stacks do not match an old video.

- [ ] **Step 3: Restore check.** PeerTube's behaviour on a re-`Create` after `Delete` is unverified (A15.6). Add a second test that uploads, waits until listed, deletes, waits until gone, restores the post, and waits until listed again.
  - Restore with the API's post restore, or `PieFed.restore` if it exists. Otherwise add a client method for the alpha restore endpoint.
  - If PeerTube refuses the re-`Create` (e.g. `filename must be unique`), stop: do not weaken the test. Record the exact PeerTube log line. Change ruling A15.6 to "restore sends PeerTube nothing", with that evidence. Make the code and the unit test match the new ruling, and drop this test.

- [ ] **Step 4: Run the harness.** Use the commands in `interop/README.md`:

```bash
interop/run.sh            # fresh stack: build, seed, run every interop test
```

Expected: every interop test passes, the 17 existing ones plus the new ones. Run the new file twice more on the reused stack:

```bash
interop/run.sh interop/test_video_channel.py   # or the README's reused-stack form
```

Expected: PASS each time.

- [ ] **Step 5: Full gates.** Run the Global Constraints coverage chain (full suite, floors, changed-line at `9d48fee29` with `--branches`), and `./run_tests.sh tests/test_no_inline_imports.py -q`.
Expected: all pass, with zero changed-line misses and KEPT still 85.

- [ ] **Step 6: Evidence.** Add a **Gates (2026-10-16, Task 7)** line to A15, in A13's style. It names the interop test, the fresh-stack and reused-stack results, and the restore outcome. Update `interop/README.md`'s test list.

- [ ] **Step 7: Commit**

```bash
git add interop/ docs/superpowers/specs/2026-10-14-native-federation-design.md
git commit -m "test(interop): a video channel upload plays on PeerTube, round-trips a like, edit and delete

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
