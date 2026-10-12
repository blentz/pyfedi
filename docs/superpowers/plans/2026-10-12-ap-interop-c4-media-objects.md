# AP interop C4: Create of Audio, Image, Document and Video as posts — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A peer's Create of an `Audio`, `Image`, `Document` or `Video` object becomes a post through the same inbox path as a `Note`/`Page`, mapped onto the existing post types.

**Architecture:** One pure reader `media_url_from(obj, kind)` picks the media URL out of the object's `url`. The inbox's new-content types gain the four media types (the Video-only Update arm goes); `process_new_content` ignores an Update of an unknown media object and refuses one with no usable URL. `Post.new` and `update_post_from_activity` map Image → Image post, Audio → Link post with an `extensions['media_type'] = 'audio'` marker, Document → Link post; Video stays as is. The post page's audio player also keys on the marker.

**Tech Stack:** Flask, SQLAlchemy, Jinja2, pytest via `./run_tests.sh`.

**Spec:** `docs/superpowers/specs/2026-10-12-ap-interop-c4-media-objects-design.md`

## Global Constraints

- `MEDIA_OBJECT_TYPES = ('Audio', 'Document', 'Image', 'Video')`; `MEDIA_URL_KIND = {'Image': 'image', 'Audio': 'audio', 'Document': None}` (Video keeps its object id as the url, unchanged).
- At most the first 20 entries of a `url` list are read (`MAX_MEDIA_URL_ENTRIES = 20`).
- A media URL must be `http`/`https` and pass `url_is_storable`.
- Update of an unknown media object: ignored, logged `Update of unknown media object`. Create with no usable URL (Image/Audio/Document): refused, logged `Unacceptable media object`.
- An Update never erases a stored media URL.
- Title: `name` if non-empty; else the microblog rule (first sentence of the body); else the type name `'Image'` / `'Audio'` / `'Document'` (plain English, stored data).
- No new network access on the inbox path (D738/D775): Audio and Document posts skip the link-post OpenGraph fetch.
- TDD; 100% line and branch coverage of changed lines; one commit per defect; gates via `./run_tests.sh` (single file: `./run_tests.sh tests/test_x.py -v`).
- Never assign `app.config[...]` in a test; use `monkeypatch.setitem`. No new function-level imports (ratchet `tests/test_no_inline_imports.py`); add names to the existing local import lines in `Post.new`.
- Commit attribution line: `Co-Authored-By: <your model> <noreply@anthropic.com>`.
- No migration (the `post.extensions` JSON column exists).

## Plan rulings (controller, for the owner's visibility)

- **R1 Refusal log at the inbox.** `Post.new` returning None logs the generic "Post creation refused" (and, for a local community, sends a Delete). To log the spec's `Unacceptable media object`, `process_new_content` checks `media_url_from` before `create_post` for Image/Audio/Document Creates. `Post.new` still returns None for the same case, because the fetch path (`resolve_remote_post`) calls it directly.
- **R2 OpenGraph.** `Post.new` and the Update fetch OpenGraph for a link post without an image. For an Audio or Document post that would fetch the media file itself on the inbox path; both skip it for those types.
- **R3 Existing Video tests.** Three tests in `tests/test_inbox_dispatch_create_update.py` pin the old Video arm (`_seed_video_post` users). Their expectations move to the new path: owner edit → `update_post_from_activity` via `process_new_content`; another user → `Edit attempt denied` (same message); unknown post → `Update of unknown media object`.
- **R4 Moderator edits.** Through `process_new_content`, a community moderator or instance admin may also Update a Video, as for every other post type; the old arm allowed only the author.

## Review Focus

1. A `url` of `javascript:alert(1)`, `data:…`, a relative path, or a 1000-entry list never becomes `post.url` — Task 1 `test_hostile_urls_are_refused`, `test_only_the_first_20_entries_are_read`.
2. PeerTube's frequent Update of a video we never stored creates nothing — Task 2 `test_update_of_an_unknown_media_object_is_ignored`.
3. An Update whose `url` is missing or junk keeps the stored media URL — Task 4 `test_an_update_without_a_usable_url_keeps_the_stored_one`.
4. A Funkwhale Audio whose url list puts a `text/html` page first stores the audio file, not the page — Task 1 `test_a_matching_media_type_beats_an_earlier_entry`; Task 3 `test_funkwhale_audio_becomes_a_link_post_with_the_marker`.
5. An Audio post's ingest makes no network request — Task 3 `test_audio_ingest_fetches_nothing`.

---

### Task 1: `media_url_from` and the media type constants

**Files:**
- Modify: `app/activitypub/util.py` (constants and function right after `content_warning_from`'s C3 neighbours — place them after `object_language`; `DISPATCHED_AP_TYPES` ~line 1060)
- Test: `tests/test_media_objects.py` (new)

**Interfaces:**
- Produces: `MEDIA_OBJECT_TYPES`, `MEDIA_URL_KIND`, `MAX_MEDIA_URL_ENTRIES`, `media_url_from(obj: dict, kind: str | None) -> str | None` in `app/activitypub/util.py`; `DISPATCHED_AP_TYPES` contains `'Audio'`, `'Document'`, `'Image'`.

- [ ] **Step 1: Write the failing tests** `tests/test_media_objects.py`:

```python
"""C4: Create of Audio, Image, Document and Video as posts
(docs/superpowers/specs/2026-10-12-ap-interop-c4-media-objects-design.md)."""
import pytest

from app.activitypub.util import (DISPATCHED_AP_TYPES, MAX_MEDIA_URL_ENTRIES, MEDIA_OBJECT_TYPES, MEDIA_URL_KIND,
                                  _single_type, media_url_from)

AUDIO = 'https://tunes.example/api/v1/listen/abc/?upload=1'
PAGE = 'https://tunes.example/library/tracks/1'
IMAGE = 'https://pics.example/media/1'


@pytest.fixture(autouse=True)
def _app_context(app):
    """url_is_storable needs no app, but keep parity with the C3 tests' harness."""
    yield


def test_constants():
    assert MEDIA_OBJECT_TYPES == ('Audio', 'Document', 'Image', 'Video')
    assert MEDIA_URL_KIND == {'Image': 'image', 'Audio': 'audio', 'Document': None}
    assert MAX_MEDIA_URL_ENTRIES == 20
    assert {'Audio', 'Document', 'Image', 'Video'} <= DISPATCHED_AP_TYPES


def test_an_array_type_resolves_to_the_media_type():
    assert _single_type(['Audio', 'schema:AudioObject']) == 'Audio'


def test_a_bare_string_url():
    assert media_url_from({'url': IMAGE}, 'image') == IMAGE


def test_a_link_dict_with_href():
    assert media_url_from({'url': {'type': 'Link', 'href': AUDIO, 'mediaType': 'audio/ogg'}}, 'audio') == AUDIO


def test_a_dict_with_url():
    assert media_url_from({'url': {'url': IMAGE}}, 'image') == IMAGE


def test_a_matching_media_type_beats_an_earlier_entry():
    obj = {'url': [{'type': 'Link', 'href': PAGE, 'mediaType': 'text/html'},
                   {'type': 'Link', 'href': AUDIO, 'mediaType': 'audio/mpeg'}]}
    assert media_url_from(obj, 'audio') == AUDIO


def test_an_entry_of_another_kind_is_skipped_but_one_without_a_media_type_is_the_fallback():
    obj = {'url': [{'href': PAGE, 'mediaType': 'text/html'}, {'href': AUDIO}]}
    assert media_url_from(obj, 'audio') == AUDIO


def test_only_another_kind_is_none():
    assert media_url_from({'url': [{'href': PAGE, 'mediaType': 'text/html'}]}, 'audio') is None


def test_no_kind_takes_the_first_usable_entry():
    obj = {'url': [{'href': 'javascript:x()'}, {'href': PAGE, 'mediaType': 'text/html'}, {'href': AUDIO}]}
    assert media_url_from(obj, None) == PAGE


@pytest.mark.parametrize('value', [
    'javascript:alert(1)', 'data:image/png;base64,AAAA', '/relative/path.png', 'ftp://pics.example/1.png',
    '', None, 5, ['x'], {'href': 5}, [{'href': None}], {}, [],
])
def test_hostile_urls_are_refused(value):
    assert media_url_from({'url': value}, 'image') is None


def test_no_url_key_is_none():
    assert media_url_from({}, None) is None


def test_only_the_first_20_entries_are_read():
    junk = [{'href': PAGE, 'mediaType': 'text/html'}] * MAX_MEDIA_URL_ENTRIES
    assert media_url_from({'url': junk + [{'href': AUDIO, 'mediaType': 'audio/ogg'}]}, 'audio') is None


def test_a_non_string_media_type_counts_as_none():
    assert media_url_from({'url': [{'href': AUDIO, 'mediaType': 7}]}, 'audio') == AUDIO
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./run_tests.sh tests/test_media_objects.py -v`
Expected: FAIL — `ImportError: cannot import name 'MAX_MEDIA_URL_ENTRIES'`.

- [ ] **Step 3: Implement** in `app/activitypub/util.py`. Add `'Audio', 'Document', 'Image'` to `DISPATCHED_AP_TYPES` (keep the frozenset alphabetised within its line groups). After `object_language`:

```python
MEDIA_OBJECT_TYPES = ('Audio', 'Document', 'Image', 'Video')  # C4: media objects a peer may Create as posts
MEDIA_URL_KIND = {'Image': 'image', 'Audio': 'audio', 'Document': None}  # Video keeps its object id as the url
MAX_MEDIA_URL_ENTRIES = 20


def media_url_from(obj: dict, kind: str | None) -> str | None:
    """C4. The media URL of an Image/Audio/Document object: from `url` (a string, a Link dict's `href`, a dict's
    `url`, or a list of those), the first entry whose `mediaType` is `kind/*`; else the first entry without a
    `mediaType` (any entry when `kind` is None). Only http(s) URLs that url_is_storable accepts count."""
    value = obj.get('url')
    entries = value[:MAX_MEDIA_URL_ENTRIES] if isinstance(value, list) else [value]
    fallback = None
    for entry in entries:
        media_type = None
        if isinstance(entry, dict):
            media_type = entry.get('mediaType') if isinstance(entry.get('mediaType'), str) else None
            entry = entry.get('href') or entry.get('url')
        if not isinstance(entry, str) or not entry.lower().startswith(('http://', 'https://')) \
                or not url_is_storable(entry):
            continue
        if kind and media_type:
            if media_type.lower().startswith(kind + '/'):
                return entry
            continue  # an entry of another kind, e.g. Funkwhale's text/html page beside the audio
        if fallback is None:
            fallback = entry
    return fallback
```

If `_single_type(['Audio', 'schema:AudioObject'])` does not return `'Audio'` after adding the types to `DISPATCHED_AP_TYPES`, read `_single_type` and report — do not change it beyond the set.

- [ ] **Step 4: Run** — `./run_tests.sh tests/test_media_objects.py tests/test_ap_interop_array_type.py -v` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/util.py tests/test_media_objects.py
git commit -m "feat: read the media URL out of an Audio, Image or Document object"
```

---

### Task 2: The inbox routes media objects through the new-content path

**Files:**
- Modify: `app/activitypub/routes.py` — the Create/Update arm (`new_content_types` ~line 1635 and the `elif object_type == 'Video':` arm right after it); `process_new_content` (~line 2870, the `if not in_reply_to:` branch)
- Modify: `tests/test_inbox_dispatch_create_update.py` (the three `_seed_video_post` tests, ~lines 516–567; ruling R3)
- Test: `tests/test_media_objects_inbox.py` (new)

**Interfaces:**
- Consumes: `MEDIA_OBJECT_TYPES`, `MEDIA_URL_KIND`, `media_url_from` (Task 1).

- [ ] **Step 1: Write the failing tests** `tests/test_media_objects_inbox.py`, using the dispatch harness of `tests/test_inbox_dispatch_create_update.py` (copy its imports, `create_activity`, and `_seed_video_post`):

```python
"""C4: the inbox sends media objects through process_new_content."""
import pytest

from app import db
from app.activitypub import routes as activitypub_routes
from app.models import ActivityPubLog
from tests.factories import inbox_activity, make_community, make_instance, make_post, make_user, seed_community_owner
from tests.test_inbox_dispatch_lock_delete import record_moderation
from tests.test_inbox_dispatch_preamble import dispatch


def create_activity(actor, obj, *, activity_type='Create', **outer):
    return inbox_activity(actor, activity_type=activity_type, object=obj, **outer)


@pytest.fixture
def peer(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    community = make_community(host='peer.example')
    author = make_user(instance, 'author')
    db.session.commit()
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json, author=None: community)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)
    return instance, community, author


@pytest.mark.parametrize('kind', ['Audio', 'Document', 'Image', 'Video'])
def test_a_create_of_each_media_type_reaches_create_post(peer, monkeypatch, kind):
    _, community, author = peer
    calls = record_moderation(monkeypatch, 'create_post')
    dispatch(create_activity(author, {'type': kind, 'id': f'https://peer.example/m/{kind}',
                                      'url': 'https://peer.example/files/1', 'name': 'x'}))
    assert len(calls['create_post']) == 1


def test_update_of_an_unknown_media_object_is_ignored(peer, monkeypatch):
    _, _, author = peer
    calls = record_moderation(monkeypatch, 'create_post', 'update_post_from_activity')
    dispatch(create_activity(author, {'type': 'Video', 'id': 'https://peer.example/videos/watch/404',
                                      'name': 'v'}, activity_type='Update'))
    assert calls['create_post'] == [] and calls['update_post_from_activity'] == []
    assert ActivityPubLog.query.one().exception_message == 'Update of unknown media object'


@pytest.mark.parametrize('kind', ['Audio', 'Document', 'Image'])
def test_a_create_without_a_usable_url_is_refused(peer, monkeypatch, kind):
    _, _, author = peer
    calls = record_moderation(monkeypatch, 'create_post')
    dispatch(create_activity(author, {'type': kind, 'id': f'https://peer.example/m/{kind}',
                                      'url': 'javascript:alert(1)', 'name': 'x'}))
    assert calls['create_post'] == []
    assert ActivityPubLog.query.one().exception_message == 'Unacceptable media object'


def test_a_media_reply_still_goes_down_the_reply_path(peer, monkeypatch):
    _, community, author = peer
    parent = make_post(community, author, 'https://peer.example/post/1')
    db.session.commit()
    calls = record_moderation(monkeypatch, 'create_post', 'create_post_reply')
    dispatch(create_activity(author, {'type': 'Image', 'id': 'https://peer.example/m/r',
                                      'url': 'https://peer.example/files/1.png', 'inReplyTo': parent.ap_id,
                                      'content': '<p>reply</p>'}))
    assert calls['create_post'] == []
```

If `record_moderation` cannot record `create_post` or `create_post_reply` (they are imported into `activitypub_routes` under those names — check with `grep -n "create_post\b\|create_post_reply" app/activitypub/routes.py | head`), record the names `process_new_content` actually calls and say so in the report. For the reply test, assert whichever reply entry point is reached instead of `create_post` if `create_post_reply` is not patchable; the point is that `create_post` is not called.

- [ ] **Step 2: Run them to verify they fail** — `./run_tests.sh tests/test_media_objects_inbox.py -v` — Expected: FAIL (`Unacceptable type (create): Audio`, `PeerTube post not found`).

- [ ] **Step 3: The dispatch arm.** In the Create/Update arm replace

```python
                        new_content_types = ['Page', 'Article', 'Link', 'Note', 'Question', 'Event']
```

with

```python
                        new_content_types = ['Page', 'Article', 'Link', 'Note', 'Question', 'Event', *MEDIA_OBJECT_TYPES]  # C4
```

and delete the whole `elif object_type == 'Video':  # PeerTube: editing a video ...` arm (through its final `return` after `'PeerTube post not found'`). Import `MEDIA_OBJECT_TYPES, MEDIA_URL_KIND, media_url_from` in the module's existing `from app.activitypub.util import (...)` block.

- [ ] **Step 4: `process_new_content`.** In the `if not in_reply_to:` branch, inside its `else:` (the post is not stored yet), before `if can_create_post(user, community):` add:

```python
            object_type = activity_json['object'].get('type')
            if object_type in MEDIA_OBJECT_TYPES:  # C4
                if activity_json['type'] == 'Update':  # PeerTube Updates videos often (views); only a Create creates
                    log_incoming_ap(id, APLOG_UPDATE, APLOG_IGNORED, saved_json, 'Update of unknown media object')
                    return
                if object_type in MEDIA_URL_KIND and media_url_from(activity_json['object'],
                                                                     MEDIA_URL_KIND[object_type]) is None:
                    log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'Unacceptable media object')
                    return
```

Check `APLOG_IGNORED` is already imported in routes.py (it is used elsewhere in the file); if not, add it to the existing import.

- [ ] **Step 5: Ruling R3 — the three old Video tests.** In `tests/test_inbox_dispatch_create_update.py`:
  - `test_a_peertube_video_edit_by_its_owner_updates_the_post`: unchanged expectation (`update_post_from_activity` called once, log success) — it must still pass through `process_new_content`; if `can_create_post` refuses the seeded user, make the seeded owner a community member the way neighbouring tests do (`make_community_member`), not by patching `can_create_post`.
  - `test_a_peertube_video_edit_by_another_user_is_denied`: unchanged (`Edit attempt denied`).
  - `test_a_peertube_video_edit_for_an_unknown_post_is_refused`: rename to `test_a_peertube_video_update_for_an_unknown_post_is_ignored` and expect `'Update of unknown media object'`.
  Update each docstring/comment that names the removed arm.

- [ ] **Step 6: Run**

Run: `./run_tests.sh tests/test_media_objects_inbox.py tests/test_inbox_dispatch_create_update.py tests/test_inbox_dispatch_new_content.py tests/test_ap_interop_array_type.py -q` — Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/activitypub/routes.py tests/test_media_objects_inbox.py tests/test_inbox_dispatch_create_update.py
git commit -m "feat: the inbox takes a Create of Audio, Image, Document or Video as new content"
```

---

### Task 3: `Post.new` maps media objects onto post types

**Files:**
- Modify: `app/models.py` — `Post.new`: the local `from app.activitypub.util import ...` (~line 2674); the no-name branch (~line 2689–2694); the microblog title (~line 2766); a new block after the attachment handling and before `if post.url and not url_is_storable(post.url):` (~line 2859); `if is_image_url(post.url):` (~line 2865); `if post.image is None:  # This is a link post ...` (~line 3019)
- Test: `tests/test_media_objects_ingest.py` (new)

**Interfaces:**
- Consumes: `MEDIA_URL_KIND`, `media_url_from` (Task 1).
- Produces: Audio posts carry `post.extensions['media_type'] == 'audio'` (Task 5 reads it).

- [ ] **Step 1: Write the failing tests** `tests/test_media_objects_ingest.py` (the `world` fixture and helpers are the C3 ones from `tests/test_language_variants_ingest.py`, trimmed):

```python
"""C4: Post.new maps Audio, Image, Document and Video objects onto post types."""
import pytest

from app import db
from app.constants import POST_TYPE_IMAGE, POST_TYPE_LINK, POST_TYPE_VIDEO
from app.models import Post
from app.utils import utcnow
from tests.factories import make_community, make_community_member, make_instance, make_site, make_user

PEER = 'remote.test'
PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'
AUDIO = 'https://tunes.example/api/v1/listen/abc/?upload=1'


@pytest.fixture
def world(app, db_session):
    make_site()
    instance = make_instance(PEER)
    make_user(instance, 'community_owner')
    community = make_community(host=PEER)
    community.ap_fetched_at = utcnow()
    author = make_user(instance, 'mediamaker')
    author.ap_profile_id = author.ap_public_url = f'https://{PEER}/u/mediamaker'
    make_community_member(author, community)
    db.session.commit()
    return community, author


def _create(obj):
    return {'id': obj['id'] + '/create', 'type': 'Create', 'to': [PUBLIC], 'object': obj}


def _media(n, kind, **fields):
    obj = {'id': f'https://{PEER}/m/{n}', 'type': kind, 'attributedTo': f'https://{PEER}/u/mediamaker',
           'to': [PUBLIC], 'cc': [], 'published': '2026-01-01T00:00:00Z'}
    obj.update(fields)
    return obj


def test_funkwhale_audio_becomes_a_link_post_with_the_marker(world):
    community, author = world
    obj = _media(1, 'Audio', name='A song', content='<p>liner notes</p>',
                 url=[{'type': 'Link', 'href': 'https://tunes.example/library/tracks/1', 'mediaType': 'text/html'},
                      {'type': 'Link', 'href': AUDIO, 'mediaType': 'audio/ogg'}])
    post = Post.new(author, community, _create(obj))
    assert (post.type, post.url, post.title) == (POST_TYPE_LINK, AUDIO, 'A song')
    assert post.extensions == {'media_type': 'audio'}
    assert 'liner notes' in post.body_html


def test_image_without_an_extension_is_an_image_post_with_alt_text(world):
    community, author = world
    post = Post.new(author, community, _create(_media(2, 'Image', name='Sunset', url='https://pics.example/media/9')))
    assert post.type == POST_TYPE_IMAGE and post.url == 'https://pics.example/media/9'
    assert post.image is not None and post.image.source_url == 'https://pics.example/media/9'
    assert post.image.alt_text == 'Sunset'


def test_document_is_a_link_post(world):
    community, author = world
    post = Post.new(author, community, _create(_media(3, 'Document', name='Paper',
                                                      url='https://docs.example/paper.pdf')))
    assert (post.type, post.url) == (POST_TYPE_LINK, 'https://docs.example/paper.pdf')
    assert not (post.extensions or {}).get('media_type')


def test_a_bare_media_object_is_titled_by_its_type(world):
    community, author = world
    assert Post.new(author, community, _create(_media(4, 'Document', url='https://docs.example/a.pdf'))).title \
        == 'Document'


def test_a_media_object_without_a_name_takes_the_body_title(world):
    community, author = world
    post = Post.new(author, community, _create(_media(5, 'Image', url='https://pics.example/1.png',
                                                      content='<p>Look at this. More text.</p>')))
    assert post.title.startswith('Look at this')
    assert post.url == 'https://pics.example/1.png'  # the media URL, not a link from the content


def test_a_media_object_whose_body_gives_no_title_is_titled_by_its_type(world):
    community, author = world
    post = Post.new(author, community, _create(_media(6, 'Audio', url=AUDIO, content='<p></p>')))
    assert post.title == 'Audio'


def test_no_usable_url_creates_nothing(world):
    community, author = world
    assert Post.new(author, community, _create(_media(7, 'Image', name='x', url='javascript:alert(1)'))) is None


def test_peertube_video_is_unchanged(world):
    community, author = world
    post = Post.new(author, community, _create(_media(8, 'Video', name='Clip', content='<p>desc</p>',
                                                      mediaType='text/markdown')))
    assert post.type == POST_TYPE_VIDEO and post.url == f'https://{PEER}/m/8'


def test_audio_ingest_fetches_nothing(world, monkeypatch):
    community, author = world
    calls = []
    monkeypatch.setattr('app.models.opengraph_parse', lambda *a, **k: calls.append(a), raising=False)
    monkeypatch.setattr('app.utils.opengraph_parse', lambda *a, **k: calls.append(a))
    Post.new(author, community, _create(_media(9, 'Audio', name='s', url=AUDIO)))
    Post.new(author, community, _create(_media(10, 'Document', name='d', url='https://docs.example/b.pdf')))
    assert calls == []
```

`Post.new` imports `opengraph_parse` locally from `app.utils` (`from app.utils import ... opengraph_parse ...`), so patching `app.utils.opengraph_parse` is what the local import picks up. If the Image test's `make_image_sizes` or image hashing reaches the network in the test harness, read how `tests/` patch it for existing Image-post ingest tests (`grep -rln "POST_TYPE_IMAGE" tests/ | head`) and do the same.

- [ ] **Step 2: Run them to verify they fail** — `./run_tests.sh tests/test_media_objects_ingest.py -v` — Expected: FAIL (Audio/Image/Document titles, types, urls).

- [ ] **Step 3: Implement in `Post.new`.** Add `MEDIA_URL_KIND, media_url_from` to the local `from app.activitypub.util import ...`. At the top of the function body add `object_type = request_json['object'].get('type')`.

  a. The no-name branch:

```python
            if 'content' in request_json['object'] and request_json['object']['content'] is not None:
                title = ""
                microblog = True
            elif object_type in MEDIA_URL_KIND:  # C4: a bare media object is titled by its type
                title = object_type
            else:
                return None
```

  b. Right after the microblog title line (`post.title = shorten_string(post.content_warning, 255) if post.content_warning else title  # ...`):

```python
                if not post.title.strip() and object_type in MEDIA_URL_KIND:
                    post.title = object_type  # C4: a media object whose body gives no title
```

  c. A new block immediately before `if post.url and not url_is_storable(post.url):` (the D1404 guard after the attachments):

```python
        if object_type in MEDIA_URL_KIND:  # C4: an Image/Audio/Document object carries its media in `url`
            media_url = media_url_from(request_json['object'], MEDIA_URL_KIND[object_type])
            if media_url is None:
                return None
            post.url = media_url  # wins over a link the microblog branch took from the content
            if object_type == 'Image' and isinstance(request_json['object'].get('name'), str):
                alt_text = request_json['object']['name']
            if object_type == 'Audio':
                post.extensions = {**(post.extensions or {}), 'media_type': 'audio'}
```

  d. `if is_image_url(post.url):` becomes `if is_image_url(post.url) or object_type == 'Image':`, and directly after that branch's body (before `elif is_video_url(post.url) ...`) add:

```python
            elif object_type in ('Audio', 'Document'):
                post.type = constants.POST_TYPE_LINK  # C4: a media file is a link post, whatever its URL looks like
```

  e. `if post.image is None:  # This is a link post but the source instance has not provided a thumbnail image` becomes `if post.image is None and object_type not in ('Audio', 'Document'):  # ... (C4 ruling R2: never fetch a media file on the inbox path)` keeping the existing comment text.

- [ ] **Step 4: Run** — `./run_tests.sh tests/test_media_objects_ingest.py tests/test_media_objects.py -v`, then the existing ingest suites: `./run_tests.sh tests/ -k "post_new or ingest or peertube or castopod or podcast" -q` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/models.py tests/test_media_objects_ingest.py
git commit -m "feat: an Image, Audio or Document object becomes an image or link post"
```

---

### Task 4: `update_post_from_activity` applies the same mapping

**Files:**
- Modify: `app/activitypub/util.py` — `update_post_from_activity`: title block (`new_title = autogenerated_title.strip()` ~line 4548); the url block (`if new_url and not url_is_storable(new_url):` ~line 4926, `if is_image_url(new_url):` ~4957, `opengraph = opengraph_parse(thumbnail_url)` ~4970, `if is_video_hosting_site(embed_url) or is_video_url(new_url):` ~4977)
- Test: `tests/test_media_objects_ingest.py` (append)

**Interfaces:**
- Consumes: `MEDIA_URL_KIND`, `media_url_from` (Task 1); the `world`, `_create`, `_media` helpers (Task 3).

- [ ] **Step 1: Write the failing tests** (append; add `import contextlib` and `from app.activitypub.util import update_post_from_activity` to the file's imports):

```python
class _RedisLockOnlyDouble:
    def lock(self, *args, **kwargs):
        return contextlib.nullcontext()


@pytest.fixture
def redis_lock_only_double(monkeypatch):
    monkeypatch.setattr('app.redis_client', _RedisLockOnlyDouble())


def _stored(world, n, kind, **fields):
    community, author = world
    post = Post.new(author, community, _create(_media(n, kind, **fields)))
    db.session.add(post)
    db.session.commit()
    return post


def _update(post, n, kind, **fields):
    update_post_from_activity(post, {'id': f'u{n}', 'type': 'Update', 'object': _media(n, kind, **fields)})


def test_an_update_changes_the_audio_url_and_title(world, redis_lock_only_double):
    post = _stored(world, 20, 'Audio', name='Old', url=AUDIO)
    _update(post, 20, 'Audio', name='New', url=[{'href': 'https://tunes.example/2.ogg', 'mediaType': 'audio/ogg'}])
    assert (post.title, post.url, post.type) == ('New', 'https://tunes.example/2.ogg', POST_TYPE_LINK)
    assert post.extensions == {'media_type': 'audio'}


def test_an_update_without_a_usable_url_keeps_the_stored_one(world, redis_lock_only_double):
    post = _stored(world, 21, 'Document', name='Doc', url='https://docs.example/1.pdf')
    _update(post, 21, 'Document', name='Doc', url='javascript:alert(1)')
    assert post.url == 'https://docs.example/1.pdf'


def test_an_image_update_without_an_extension_stays_an_image_post(world, redis_lock_only_double):
    post = _stored(world, 22, 'Image', name='One', url='https://pics.example/media/1')
    _update(post, 22, 'Image', name='Two', url='https://pics.example/media/2')
    assert post.type == POST_TYPE_IMAGE and post.url == 'https://pics.example/media/2'
    assert post.image.source_url == 'https://pics.example/media/2'


def test_a_bare_media_update_is_titled_by_its_type(world, redis_lock_only_double):
    post = _stored(world, 23, 'Audio', name='Song', url=AUDIO)
    _update(post, 23, 'Audio', url=AUDIO)
    assert post.title == 'Audio'


def test_a_media_update_fetches_nothing(world, redis_lock_only_double, monkeypatch):
    post = _stored(world, 24, 'Document', name='d', url='https://docs.example/1.pdf')
    calls = []
    monkeypatch.setattr('app.activitypub.util.opengraph_parse', lambda *a, **k: calls.append(a))
    _update(post, 24, 'Document', name='d', url='https://docs.example/2.pdf')
    assert calls == [] and post.url == 'https://docs.example/2.pdf'
```

- [ ] **Step 2: Run them to verify they fail** — `./run_tests.sh tests/test_media_objects_ingest.py -v` — Expected: the five new tests FAIL.

- [ ] **Step 3: Implement in `update_post_from_activity`.** At the top of the locked body add `object_type = request_json['object'].get('type')`.

  a. Title — right after `new_title = autogenerated_title.strip()` inside the `else:` of the name check, and before the warning override, add (at the same indentation as the `if episode_title:` chain's siblings, after that chain):

```python
            if object_type in MEDIA_URL_KIND and not new_title.replace('[Microblog]', '').strip():
                new_title = object_type  # C4: a bare media object is titled by its type
```

  b. URL — immediately before `if new_url and not url_is_storable(new_url):` add:

```python
        if object_type in MEDIA_URL_KIND:  # C4: the media URL replaces the stored one only when it is usable
            new_url = media_url_from(request_json['object'], MEDIA_URL_KIND[object_type]) or old_url
            if object_type == 'Audio':
                post.extensions = {**(post.extensions or {}), 'media_type': 'audio'}
```

  c. `if is_image_url(new_url):` becomes `if is_image_url(new_url) or object_type == 'Image':`. In that branch the alt text comes from the attachment today; for an Image object also take `request_json['object']['name']` when it is a string and there is no attachment (guard the existing `request_json['object']['attachment']` read so a media object without `attachment` does not raise — it currently indexes it only inside an `if` that checks membership; keep that).
  d. The OpenGraph `else:` arm (`opengraph = opengraph_parse(thumbnail_url)`): skip it when `object_type in ('Audio', 'Document')` (ruling R2).
  e. `if is_video_hosting_site(embed_url) or is_video_url(new_url):` — prefix with `if object_type in ('Audio', 'Document'): post.type = POST_TYPE_LINK` / `elif ...` so a media file is a link post.

- [ ] **Step 4: Run** — `./run_tests.sh tests/test_media_objects_ingest.py -v`, then `./run_tests.sh tests/ -k "update_post or peertube or castopod" -q` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/activitypub/util.py tests/test_media_objects_ingest.py
git commit -m "feat: an Update of an Image, Audio or Document object updates its post and never drops the media URL"
```

---

### Task 5: The audio player shows for Audio posts

**Files:**
- Modify: `app/templates/post/_post_full.html` (~line 133: `{% if post.url.endswith(('.mp3', ...)) -%}`)
- Test: `tests/test_media_objects_ingest.py` (append)

- [ ] **Step 1: Write the failing test** (append; `logged_in_client` as in `tests/test_language_variant_templates.py`, and `monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)` if the signed-in page needs it, as that file does):

```python
def test_the_post_page_plays_an_audio_post_without_an_audio_extension(world, app, monkeypatch):
    from bs4 import BeautifulSoup
    community, author = world
    post = _stored(world, 30, 'Audio', name='Song', url=AUDIO)
    html = app.test_client().get(f'/post/{post.id}').get_data(as_text=True)
    audio = BeautifulSoup(html, 'html.parser').select_one('audio')
    assert audio is not None and audio['src'] == AUDIO


def test_a_plain_link_without_an_audio_extension_has_no_player(world, app):
    from bs4 import BeautifulSoup
    post = _stored(world, 31, 'Document', name='Doc', url='https://docs.example/1.pdf')
    html = app.test_client().get(f'/post/{post.id}').get_data(as_text=True)
    assert BeautifulSoup(html, 'html.parser').select_one('audio') is None
```

Move the two `from bs4 import BeautifulSoup` lines to the file's top-level imports (ratchet: no function-level imports in tests is not enforced, but keep the file consistent). If an anonymous GET of the post page needs the community to be public or a different setup, follow `tests/test_language_variant_templates.py`'s `page` helper.

- [ ] **Step 2: Run to verify it fails** — `./run_tests.sh tests/test_media_objects_ingest.py -k audio_post -v` — Expected: FAIL (no `<audio>`).

- [ ] **Step 3: Implement.** The condition becomes:

```jinja
{% if post.url.endswith(('.mp3', '.m4a', '.ogg', '.oga', '.opus', '.wav', '.aac', '.flac')) or (post.extensions or {}).get('media_type') == 'audio' -%}
```

(keep the existing extension tuple exactly as it is in the file).

- [ ] **Step 4: Run** — `./run_tests.sh tests/test_media_objects_ingest.py -v` — PASS.

- [ ] **Step 5: Commit**

```bash
git add app/templates/post/_post_full.html tests/test_media_objects_ingest.py
git commit -m "feat: the post page plays an Audio post whose URL has no audio extension"
```

---

### Task 6: Final gate

- [ ] **Step 1:** `./run_tests.sh tests/ -q -p no:cacheprovider --cov=app --cov-branch --cov-report=json:coverage.json` — all pass.
- [ ] **Step 2:** `python3 tests/check_changed_line_coverage.py coverage.json 90e4808c9 --branches` — every changed line and branch covered.
- [ ] **Step 3:** `python3 tests/check_coverage_floors.py coverage.json coverage_floors.ini` — all floors met.
