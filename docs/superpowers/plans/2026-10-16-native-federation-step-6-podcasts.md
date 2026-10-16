# Native Federation Step 6: Podcasts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An audio episode uploaded to a local `podcast` community plays natively on Mastodon (an audio player in the boosted status) and on other PieFed instances, and the community serves a Podcasting 2.0 RSS feed a podcast app can subscribe to.

**Architecture:**
- **One new builder.** `app/activitypub/serializers/podcast.py` holds `uses_podcast_episode(post)` and `episode_attachment(post)`. Both `Page` builders, `send_post` (push) and `post_to_page` (fetch), put that one `Document` in `attachment` where today they put a bare `Link`. Push and fetch therefore carry the same attachment.
- **Delivery and object type do not change.** Mastodon still gets the community's `Announce` of the id and fetches the `Page`. Threadiverse instances still get the community's `Announce` of the `Create(Page)`. The author's followers and mentions still get the amended `Note`, which copies the `Page`'s attachment.
- **Episode metadata.** The audio upload and edit forms of a podcast community gain episode number, season and explicit. These are stored in `Post.extensions['podcast']`, beside the Castopod `credits` key that is already there. Communities gain `podcast_category`, settable from the forms and the API.
- **RSS.** A local `podcast` community's existing `/c/<name>/feed` lists only its episodes, with Podcasting 2.0 and iTunes tags. RSS for other kinds does not change.

**Tech Stack:** Flask, WTForms, SQLAlchemy + Alembic, feedgen 1.0.0 (its `podcast` extension, plus a small custom extension for the `podcast:` namespace), marshmallow (alpha API), pytest, atheris (fuzz track), and the podman interop harness with Mastodon v4.5.

**Spec:** `docs/superpowers/specs/2026-10-14-native-federation-design.md`. This plan covers §3.3, §3.4 (episode fields), §4.5, §8.2 ("Podcast RSS") and §8.3 (fuzz targets for the new builder and the feed). Amendments A1–A16 apply; A16's owner ruling (option 1) sets the scope. Task 0 adds A17 below, whose rulings bind this plan.

## Global Constraints

- **Branch:** `feat/podcasts` off `blentz` at `4c726f138`. Never push or merge.
- **Commits:**
  - Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`, or names the model that actually wrote it.
  - Make one commit per task, or one per defect inside a fix round.
- **TDD:** write the failing test, run it red, then implement.
- **Coverage:** 100% line and branch coverage on changed lines, from the changed-line gate at base `4c726f138` with `--branches`. Floor 100 for `app/activitypub/serializers/podcast.py` in `coverage_floors.ini`.
- **Gates:** run them only from repo-committed tools, never ad-hoc `/tmp` scripts. The coverage chain is the one in `tests/README.md` ("coverage ratchet"):
  ```
  ./run_tests.sh tests/ -q --cov=app --cov-branch --cov-report=json && \
  python3 tests/check_coverage_floors.py coverage.json coverage_floors.ini && \
  python3 tests/check_changed_line_coverage.py coverage.json 4c726f138 --branches
  ```
- **No inline imports:** no new function-level imports unless technically unavoidable. `KEPT` in `tests/test_no_inline_imports.py` stays 85.
- **Slow tests:** a test that is slow or flaky is a bug to fix, not a timeout to raise.
- **Static files:** tests must leave no file under `app/static/media`, `app/static/tmp` or `app/static/posts` (`tests/static_leak_gate.py`).
- **Kinds** (`app/constants.py`): `COMMUNITY_KIND_PODCAST = 'podcast'`. Remote communities always keep `discussion` (A15.2), so a `podcast` community is always local; the code still checks `is_local()`.
- **Audio mimes** (`app/shared/media.py` `MEDIA_EXT`): `audio/mpeg`, `audio/mp4`, `audio/ogg`. The media record of a ready audio upload holds `status`, `uuid`, `mime`, `bytes`, `duration_s` and `codec`. It also holds `thumbnail_file_id`, `thumbnail_width` and `thumbnail_height` only when the file had cover art, and then `post.image` is that cover.
- **Mastodon** (v4.5, read from the harness image): a `Page` is a converted type. Its status text is `<h2>title</h2>` followed by a link. Its `attachment` entries are still downloaded, and an entry needs `url` (not `href`) and an audio `mediaType` to become an audio player. Audio and video downloads are capped at 99 MB; a bigger file stays a remote link.
- **feedgen 1.0.0** `podcast` extension: `itunes_explicit` takes only `'yes'`, `'no'` or `'clean'`. `itunes_duration` takes whole seconds. `itunes_category` does not validate names.

## Spec amendment A17 (Task 0 writes it; it binds this plan)

The code survey of 2026-10-16 found these gaps between the spec and the code. Rulings:

1. **What an episode is.** `uses_podcast_episode(post)` holds only when all of these are true:
   - the post's community is a local `podcast` community;
   - the post is `POST_TYPE_LINK` (an audio upload is a link post, A13.5);
   - its media record is `ready`, with a `mime` starting `audio/`;
   - the record's `bytes` and `duration_s` are positive ints, and its `uuid` is a non-empty string.

   A link to remote audio (`audio_url`) has no media record, so it is not an episode. It federates as today, and the feed leaves it out.
2. **The `Audio`-bearing object (A16 option 1).** The `Page` stays a `Page`. For an episode, its `attachment` is one `Document` built by `episode_attachment(post)`, in place of today's `{'href': post.url, 'type': 'Link'}`:
   - `type: Document`, `mediaType`: the media mime, `url`: `post.url`, `name`: the title cut to 1500 characters (Mastodon's description limit).
   - `icon: {type: Image, url: post.image.source_url}` when the episode has cover art. Mastodon shows this as the audio player's artwork.

   It is a `Document` and not an `Audio` because Lemmy 0.19's `Attachment` deserializes only `Link`, `Image` and `Document`. An `Audio` entry there would reject the whole `Page`. `send_post`'s `Page` and `post_to_page` both call `episode_attachment`, and the amended `Note` for mentions and author followers copies the `Page`'s `attachment`. Delivery shapes per software stay as they are today (§6.1 `podcast` row, A16).
3. **Inbound (PieFed peers).** `Post.new` already takes a `Document`'s `url` as `post.url`. It now also sets `extensions['media_type'] = 'audio'` when the `Document` or `Audio` attachment it took has a `mediaType` starting `audio/`. The player in `_post_full.html` then shows for any URL, with or without an audio file extension.
4. **Episode fields (§3.4).** `Post.extensions['podcast']` gains `episode_number` and `season` (ints of at least 1, optional) and `explicit` (bool, default false). They are written by `with_episode(post, episode_number, season, explicit)` (`app/shared/media.py`), which keeps every other key, including `credits` (`app/discovery/credits.py`).
   - The web form shows these fields in two cases: the audio upload form of a podcast community, when a file can be uploaded (`media_upload`), and the edit form of an audio upload in a podcast community.
   - The edit form is prefilled from the stored values.
   - The alpha API neither shows nor takes them. An API edit leaves them as they are.
5. **Show fields (§3.3).**
   - `Community.podcast_category = String(64)`, nullable, added by migration `e5b8d2f0a4c7` (after `d4a7c1e9b3f6`).
   - The fixed list is Apple Podcasts' 19 top-level categories, in `APPLE_PODCAST_CATEGORIES` (`app/constants.py`). Subcategories are not offered.
   - `apply_podcast_category(community, value)` (`app/shared/community.py`) sets the field. An empty value clears it. Any other value outside the list is refused with `podcast_category must be one of the Apple Podcasts categories`.
   - The create and edit forms (`add_local`, `community_edit`) and the API community create and edit (`podcast_category`) use it. It may be set on any kind, but only a `podcast` community's feed reads it.
6. **Feed (§4.5).** For a local `podcast` community, `show_community_rss` keeps its route, cache, ETag, private check and query parameters, with these changes:
   - It lists only episodes that are `is_open`. The query filters in SQL (link posts whose media record is `ready` with an `audio/` mime), so the `limit` counts episodes.
   - Each item keeps today's fields and adds:
     - `<enclosure url length type>` from `post.url`, `bytes` and `mime`;
     - `<itunes:duration>` (`duration_s`);
     - `<itunes:episode>` and `<itunes:season>` when set;
     - `<itunes:explicit>`: `yes` when the episode is explicit or the post is `nsfw`/`nsfl`, else `no`;
     - `<itunes:image>` when the episode has cover art;
     - `<podcast:guid>` (the media `uuid`).
   - The channel adds:
     - `<itunes:category>` when `podcast_category` is set;
     - `<itunes:image>`: the community icon, else its banner, else the site's `apple-touch-icon.png`;
     - `<itunes:author>`: the owner's display name, else the community title;
     - `<itunes:explicit>`: `yes` when the community is `nsfw` or `nsfl`, else `no`;
     - `<podcast:locked>no</podcast:locked>`.
   - The channel's `language` is the community's first language other than `und`, else `en`. The feed of every other kind keeps `en`.
7. **Fuzz (§8.3).** Atheris targets for `episode_attachment` (over a fuzzed media record and title) and for the podcast feed's item builder (over a fuzzed media record and episode fields) join the fuzz registry under the surface `podcasts`.

## File structure

| File | Responsibility | Task |
|---|---|---|
| `docs/superpowers/specs/2026-10-14-native-federation-design.md` | A17; gates evidence | 0, 6 |
| `app/activitypub/serializers/podcast.py` (new) | `uses_podcast_episode`, `episode_attachment` | 1 |
| `app/activitypub/serializers/__init__.py` | export them | 1 |
| `app/shared/tasks/pages.py` | `send_post` `Page` attachment | 1 |
| `app/activitypub/util.py` | `post_to_page` attachment | 1 |
| `app/models.py` | inbound audio `media_type`; `Community.podcast_category` | 1, 3 |
| `app/shared/media.py` | `with_episode`, `episode_of` | 2 |
| `app/community/forms.py` | `EpisodeFieldsMixin`; `podcast_category` select | 2, 3 |
| `app/community/routes.py`, `app/post/routes.py` | show or prune episode fields; save them; category | 2, 3 |
| `app/shared/post.py` | `edit_post` stores episode fields (web) | 2 |
| `app/templates/post/post_edit.html` | episode field block | 2 |
| `app/templates/community/add_local.html`, `community_edit.html` | category select | 3 |
| `app/constants.py` | `APPLE_PODCAST_CATEGORIES` | 3 |
| `app/shared/community.py` | `apply_podcast_category` | 3 |
| `app/api/alpha/schema.py`, `app/api/alpha/utils/community.py` | API `podcast_category` | 3 |
| `migrations/versions/e5b8d2f0a4c7_community_podcast_category.py` (new) | column | 3 |
| `app/rss_extras.py` | `PodcastIndexExtension`, `PodcastChannel`, episode items | 4 |
| `app/community/routes.py` (`show_community_rss`) | podcast branch | 4 |
| `tests/podcast_fixtures.py` (new) | shared episode fixture | 1 |
| `tests/test_podcast_episode.py`, `test_episode_fields.py`, `test_podcast_category.py`, `test_podcast_feed.py` (new) | unit tests | 1–4 |
| `tests/fuzz/targets/podcasts.py` (new), `tests/fuzz/targets/__init__.py`, `tests/fuzz/surfaces.py`, `tests/fuzz/corpus/<target>/`, `tests/fuzz/README.md` | fuzz | 5 |
| `interop/test_podcast.py` (new), `interop/README.md` | interop | 6 |

## Review Focus

1. **An episode without cover art.** This is the common case: most MP3s carry no embedded image. The `Document` has no `icon`, the `Page` has no `image`, and the feed item has no `<itunes:image>`. Nothing raises. (Task 1 `test_episode_without_cover_has_no_icon`; Task 4 `test_item_without_cover_has_no_itunes_image`.)
2. **A podcast community with more non-episodes than `limit`.** Twenty text posts and one episode older than all of them: the feed must still list the episode, because the SQL filter, not a Python filter after `limit`, picks episodes. (Task 4 `test_limit_counts_episodes_only`.)
3. **An episode that is processing, failed, or a remote-audio link.** It is not an episode. The `Page` keeps today's `Link` attachment, and the feed leaves it out. (Task 1 `test_not_ready_or_linked_audio_is_not_an_episode`; Task 4 `test_feed_leaves_out_non_episodes`.)
4. **An edit that does not carry the episode fields.** Two cases: an API edit, and a web edit of a post that is not an episode. Neither may wipe `episode_number`, `season`, `explicit` or `credits`. (Task 2 `test_api_edit_keeps_episode_fields`, `test_with_episode_keeps_credits`.)
5. **A title that is not valid XML.** The existing `_is_valid_xml_utf8` skip still applies to episode items, so one bad title does not break the whole feed. A title over 1500 characters cannot occur (`Post.title` is 255), but `episode_attachment` cuts `name` anyway, for the fuzz target. (Task 4 `test_invalid_xml_title_skips_only_that_item`; Task 1 `test_name_is_cut_to_1500`.)

---

### Task 0: Spec amendment A17

**Files:**
- Modify: `docs/superpowers/specs/2026-10-14-native-federation-design.md`

**Interfaces:** none.

- [ ] **Step 1: Add A17.** In §10, after the A16 entry (the last bullet before `## 11. Evidence: PeerTube spike (2026-10-15)`), add a bullet `- **A17 (§3.3, §3.4, §4.5, §6.1, §8.3), step 6 podcasts; code survey 2026-10-16:** the survey found these gaps between the spec and the code. Rulings:`. Under it, copy rulings 1–7 of this plan's "Spec amendment A17" section verbatim, indented as sub-items in the style of A15.
- [ ] **Step 2: Cross-references.** Add `(Amended: see §10 A17.)` on its own line directly under each of these headings: `### 3.3 Podcast show fields`, `### 3.4 Per-post media metadata` and `### 4.5 Podcast RSS`. In §9, append ` (Amended: see §10 A16, A17: RSS and Audio-bearing Pages; no PodcastEpisode, no episode route, no Castopod delivery.)` to step 6's line.
- [ ] **Step 3: Commit.**

```bash
git add docs/superpowers/specs/2026-10-14-native-federation-design.md
git commit -m "docs: spec amendment A17, step 6 podcasts (episode rule, Document attachment, episode and show fields, feed)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1: The episode attachment, push and fetch, and inbound audio

**Files:**
- Create: `app/activitypub/serializers/podcast.py`
- Modify: `app/activitypub/serializers/__init__.py`, `app/shared/tasks/pages.py` (the `attachment = []` block in `send_post`, near line 280), `app/activitypub/util.py` (`post_to_page`, near line 422), `app/models.py` (`Post.new` attachment loop, near lines 2872–2887), `coverage_floors.ini`
- Create: `tests/podcast_fixtures.py`, `tests/test_podcast_episode.py`

**Interfaces:**
- Produces:
  - `uses_podcast_episode(post) -> bool`
  - `episode_attachment(post) -> dict`
  - `NAME_MAX = 1500`

  All three are in `app.activitypub.serializers.podcast`. `uses_podcast_episode` and `episode_attachment` are also re-exported from `app.activitypub.serializers`.
- Produces: the fixture `episode_post`, in `tests/podcast_fixtures.py`. It is a ready MP3 episode with no cover art, in local podcast community `pod`, by local `alice` (with keys). Its community followers are on `mastodon.example` (mastodon) and `lemmy.example` (lemmy). It reuses `sent` and `_remote` from `tests/peertube_video_fixtures.py`.

- [ ] **Step 1: Write the shared fixture.** `tests/podcast_fixtures.py`:

```python
"""Fixtures for the tests of a local podcast community's episodes: a ready MP3 upload with no cover art, followed
from a Mastodon and a Lemmy instance."""
import uuid

import pytest
from flask import current_app

from app import db
from app.constants import COMMUNITY_KIND_PODCAST, POST_TYPE_LINK
from app.models import CommunityMember
from app.shared.media import with_media
from tests.factories import a_keypair, make_community, make_instance, make_post, make_user
from tests.peertube_video_fixtures import _remote, sent  # noqa: F401  (sent is a fixture the tests use)


@pytest.fixture
def episode_post(db_session):
    local = make_instance('test.piefed.local', software='piefed')
    alice = make_user(local, 'alice', local=True, with_keys=True)
    community = make_community('pod', kind=COMMUNITY_KIND_PODCAST)
    community.private_key, community.public_key = a_keypair()
    post = make_post(community, alice, ap_id='https://test.piefed.local/post/1', title='Episode one')
    post.type = POST_TYPE_LINK
    post.body = 'Show notes'
    post.url = f"{current_app.config['SERVER_URL']}/static/media/posts/ab/cd/abcdefghijklmno.mp3"
    with_media(post, status='ready', uuid=str(uuid.uuid4()), mime='audio/mpeg', bytes=4406, duration_s=2,
               codec='mp3')
    post.extensions = {**post.extensions, 'media_type': 'audio'}
    for domain, software, name in (('mastodon.example', 'mastodon', 'ma'), ('lemmy.example', 'lemmy', 'le')):
        _, follower = _remote(domain, software, name)
        db.session.add(CommunityMember(user_id=follower.id, community_id=community.id))
    db.session.commit()
    return post
```

If `make_post` already sets `extensions`, keep that. If it leaves `extensions` as None, write `post.extensions = {**(post.extensions or {}), 'media_type': 'audio'}` instead.

- [ ] **Step 2: Write the failing tests.** `tests/test_podcast_episode.py`:

```python
"""Spec 2026-10-14 §10 A17.1-A17.3: what a podcast episode is, the Document both Page builders carry, and how an
inbound audio Document is marked."""
from flask import current_app

from app import db
from app.activitypub.serializers.podcast import NAME_MAX, episode_attachment, uses_podcast_episode
from app.activitypub.util import post_to_page
from app.constants import COMMUNITY_KIND_DISCUSSION
from app.models import File
from app.shared.media import with_media
from app.shared.tasks.pages import send_post
from tests.podcast_fixtures import episode_post, sent  # noqa: F401


def test_episode_attachment_is_an_audio_document(episode_post):
    assert uses_podcast_episode(episode_post)
    assert episode_attachment(episode_post) == {'type': 'Document', 'mediaType': 'audio/mpeg',
                                                'url': episode_post.url, 'name': 'Episode one'}


def test_episode_without_cover_has_no_icon(episode_post):
    assert 'icon' not in episode_attachment(episode_post)
    assert 'image' not in post_to_page(episode_post)


def test_episode_with_cover_has_an_icon(episode_post):
    server = current_app.config['SERVER_URL']
    episode_post.image = File(source_url=f'{server}/static/media/posts/ef/gh/coverartcoverar.jpg', width=500,
                              height=500)
    with_media(episode_post, thumbnail_width=500, thumbnail_height=500)
    db.session.commit()
    assert episode_attachment(episode_post)['icon'] == {'type': 'Image', 'url': episode_post.image.source_url}


def test_name_is_cut_to_1500(episode_post):
    episode_post.title = 'x' * 2000
    assert len(episode_attachment(episode_post)['name']) == NAME_MAX == 1500


def test_fetch_carries_the_document(episode_post):
    assert post_to_page(episode_post)['attachment'] == [episode_attachment(episode_post)]


def test_push_carries_the_same_document(episode_post, sent):
    send_post(episode_post.id, session=db.session)
    lemmy = sent['https://lemmy.example/inbox'][0]   # the community's Announce of the author's Create(Page)
    assert lemmy['object']['object']['attachment'] == [episode_attachment(episode_post)]
    mastodon = sent['https://mastodon.example/inbox'][0]   # an Announce of the id: Mastodon fetches the Page
    assert mastodon['object'] == episode_post.ap_id


def test_not_ready_or_linked_audio_is_not_an_episode(episode_post):
    for media in ({'status': 'processing'}, {'status': 'failed'}, {'mime': 'video/mp4'}, {'bytes': 0},
                  {'duration_s': True}, {'duration_s': 2.5}, {'uuid': ''}):
        with_media(episode_post, **{'status': 'ready', 'mime': 'audio/mpeg', 'bytes': 4406, 'duration_s': 2,
                                    'uuid': 'u', **media})
        assert not uses_podcast_episode(episode_post), media
        assert post_to_page(episode_post)['attachment'] == [{'href': episode_post.url, 'type': 'Link'}]
    episode_post.extensions = {'media_type': 'audio'}   # an audio_url link: no media record
    assert not uses_podcast_episode(episode_post)


def test_a_discussion_community_has_no_episodes(episode_post):
    episode_post.community.kind = COMMUNITY_KIND_DISCUSSION
    assert not uses_podcast_episode(episode_post)
```

Then add the inbound test. It belongs in this file and must imitate an existing `Post.new` test: find one with `grep -ln "Post.new(" tests/*.py`, take the one that builds a remote `Create(Page)` with a `Link` attachment, and copy its setup. The test sends a remote `Page` from `piefed.example` whose `attachment` is `[{'type': 'Document', 'mediaType': 'audio/mpeg', 'url': 'https://piefed.example/static/media/posts/xx/yy/noextension', 'name': 'Ep'}]`. It asserts `post.url` is that URL, `post.type == POST_TYPE_LINK` and `post.extensions['media_type'] == 'audio'`. A second test sends the same `Page` with `'mediaType': 'image/jpeg'` and asserts `(post.extensions or {}).get('media_type') != 'audio'`. Name them `test_inbound_audio_document_is_marked_audio` and `test_inbound_image_document_is_not_marked_audio`.

- [ ] **Step 3: Run them red.**

Run: `./run_tests.sh tests/test_podcast_episode.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'app.activitypub.serializers.podcast'`. After Step 4 creates the module, the push/fetch and inbound tests fail until Steps 5–6 are done.

- [ ] **Step 4: Write the builder.** `app/activitypub/serializers/podcast.py`:

```python
"""The audio a local podcast episode carries in its Page (spec 2026-10-14 §10 A16 option 1, A17.1-A17.2). The Page
stays a Page for every peer; only its attachment changes, from a bare Link to a Document Mastodon plays and Lemmy
parses (Lemmy 0.19's Attachment has no Audio variant, and one unknown entry rejects the whole Page)."""
from app.constants import COMMUNITY_KIND_PODCAST, POST_TYPE_LINK
from app.models import media_of

NAME_MAX = 1500   # Mastodon's MediaAttachment::MAX_DESCRIPTION_LENGTH


def _positive_int(value) -> bool:
    return type(value) is int and value > 0   # not a bool or a float: the feed's length and duration are whole


def uses_podcast_episode(post) -> bool:
    """A ready audio upload in a local podcast community (A17.1). A remote-audio link has no media record, so it is
    not one."""
    community = post.community
    media = media_of(post)
    return bool(community.kind == COMMUNITY_KIND_PODCAST and community.is_local()
                and post.type == POST_TYPE_LINK
                and media is not None and media.get('status') == 'ready'
                and str(media.get('mime', '')).startswith('audio/')
                and _positive_int(media.get('bytes')) and _positive_int(media.get('duration_s'))
                and isinstance(media.get('uuid'), str) and media['uuid'] != '')


def episode_attachment(post) -> dict:
    """The one attachment of an episode's Page, pushed and fetched alike (A17.2)."""
    document = {'type': 'Document', 'mediaType': media_of(post)['mime'], 'url': post.url,
                'name': (post.title or '')[:NAME_MAX]}
    if post.image is not None and post.image.source_url:   # cover art: Mastodon's audio player artwork
        document['icon'] = {'type': 'Image', 'url': post.image.source_url}
    return document
```

Add to `app/activitypub/serializers/__init__.py`:

```python
from app.activitypub.serializers.podcast import episode_attachment, uses_podcast_episode
```

Also add `'episode_attachment', 'uses_podcast_episode'` to `__all__`.

- [ ] **Step 5: Use it in both `Page` builders.** In `app/shared/tasks/pages.py` `send_post`, replace

```python
    if (post.type == POST_TYPE_LINK or post.type == POST_TYPE_VIDEO or post.type == POST_TYPE_EVENT) and post.url is not None:
        attachment.append({'href': post.url, 'type': 'Link'})
```

with

```python
    if uses_podcast_episode(post):   # spec §10 A17.2: the Document post_to_page serves too
        attachment.append(episode_attachment(post))
    elif (post.type == POST_TYPE_LINK or post.type == POST_TYPE_VIDEO or post.type == POST_TYPE_EVENT) and post.url is not None:
        attachment.append({'href': post.url, 'type': 'Link'})
```

Import both names at the top of `pages.py`, next to the existing `uses_peertube_video` import, in the same style. In `app/activitypub/util.py` `post_to_page`, replace

```python
    if (post.type == POST_TYPE_LINK or post.type == POST_TYPE_VIDEO or post.type == POST_TYPE_EVENT) and post.url is not None:
        activity_data["attachment"] = [{"href": post.url, "type": "Link"}]
```

with

```python
    if podcast_serializer.uses_podcast_episode(post):   # spec §10 A17.2: the Document send_post pushes too
        activity_data["attachment"] = [podcast_serializer.episode_attachment(post)]
    elif (post.type == POST_TYPE_LINK or post.type == POST_TYPE_VIDEO or post.type == POST_TYPE_EVENT) and post.url is not None:
        activity_data["attachment"] = [{"href": post.url, "type": "Link"}]
```

Import the module the way `peertube_serializer` is imported in `util.py`: find its line with `grep -n "peertube_serializer" app/activitypub/util.py | head -1` and add `podcast_serializer` beside it.

- [ ] **Step 6: Inbound.** In `app/models.py` `Post.new`, add one statement to the `Document` branch and one to the `Audio` branch of the attachment loop, right after each sets `post.url = attachment['url']`:

```python
                    if str(attachment.get('mediaType', '')).startswith('audio/'):   # A17.3: a PieFed episode
                        post.extensions = {**(post.extensions or {}), 'media_type': 'audio'}
```

- [ ] **Step 7: Floor.** Append to `coverage_floors.ini`, after the `peertube.py` entry:

```
# Native federation step 6 (2026-10-16), task 1: the podcast episode attachment, born fully covered.
app/activitypub/serializers/podcast.py = 100
```

- [ ] **Step 8: Run green.** Run `./run_tests.sh tests/test_podcast_episode.py tests/test_peertube_video.py tests/test_send_peertube_video.py -q`, then the files whose name matches `send_post` or `post_to_page`: `ls tests | grep -E "send_post|post_to_page"`. Expected: all pass.
- [ ] **Step 9: Commit.**

```bash
git add app/activitypub/serializers/ app/shared/tasks/pages.py app/activitypub/util.py app/models.py coverage_floors.ini tests/podcast_fixtures.py tests/test_podcast_episode.py
git commit -m "feat: a podcast episode's Page carries its audio as a Document Mastodon plays, pushed and fetched alike" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Episode fields

**Files:**
- Modify: `app/shared/media.py`, `app/community/forms.py`, `app/community/routes.py` (`add_post`), `app/post/routes.py` (`post_edit`), `app/shared/post.py` (`edit_post`, web branch), `app/templates/post/post_edit.html`
- Create: `tests/test_episode_fields.py`

**Interfaces:**
- Consumes: `media_of` (`app.models`); `COMMUNITY_KIND_PODCAST`.
- Produces:
  - `with_episode(post, episode_number: int | None, season: int | None, explicit: bool) -> None`, in `app.shared.media`;
  - `episode_of(post) -> dict`, in `app.shared.media`: the stored `extensions['podcast']`, or `{}`;
  - `EpisodeFieldsMixin`, in `app.community.forms`, with fields `episode_number`, `season` and `explicit`;
  - `episode_form_wanted(community, post=None) -> bool`, in `app.shared.media`.

  Task 4 reads `episode_of(post).get('episode_number')`, `.get('season')` and `.get('explicit', False)`.

- [ ] **Step 1: Write the failing tests.** `tests/test_episode_fields.py`. Copy the client, login and CSRF-off setup from `tests/test_media_upload.py`, which already posts the audio form; find a test there that POSTs `/community/<name>/submit/audio` with a file. Write these tests:

```python
def test_with_episode_keeps_credits(episode_post):
    episode_post.extensions = {**episode_post.extensions, 'podcast': {'credits': [{'name': 'Host'}]}}
    with_episode(episode_post, 3, 1, True)
    assert episode_post.extensions['podcast'] == {'credits': [{'name': 'Host'}], 'episode_number': 3, 'season': 1,
                                                  'explicit': True}
    assert episode_post.extensions['media']['status'] == 'ready'   # every other key kept


def test_with_episode_clears_unset_numbers(episode_post):
    with_episode(episode_post, 3, 1, True)
    with_episode(episode_post, None, None, False)
    assert episode_of(episode_post) == {'explicit': False}


def test_episode_of_without_podcast_key(episode_post):
    assert episode_of(episode_post) == {}
    episode_post.extensions = {**episode_post.extensions, 'podcast': 'garbage'}
    assert episode_of(episode_post) == {}
```

Then write the form tests, each using the setup copied from `tests/test_media_upload.py`:
- `test_upload_form_of_a_podcast_shows_episode_fields`: as a moderator who may upload, GET `/community/pod/submit/audio`; the page contains `name="episode_number"`, `name="season"` and `name="explicit"`.
- `test_upload_form_without_media_upload_has_no_episode_fields`: as a member who may not upload, the same GET has no `name="episode_number"`.
- `test_upload_stores_episode_fields`: POST the upload with `episode_number=4`, `season=2`, `explicit=y`; the new post's `episode_of(post) == {'episode_number': 4, 'season': 2, 'explicit': True}`.
- `test_episode_number_below_one_is_refused`: POST `episode_number=0`; no post is made and the page shows the field error.
- `test_edit_form_is_prefilled_and_saves`: after `with_episode(post, 4, 2, True)`, GET `/post/<id>/edit` shows `value="4"`. A POST with `episode_number=5` and no `explicit` stores `{'episode_number': 5, 'season': 2, 'explicit': False}`.
- `test_edit_of_a_non_episode_link_has_no_episode_fields`: a plain link post in a discussion community; its edit page has no `name="episode_number"`, and a POST to its edit leaves `extensions` without `podcast`.
- `test_api_edit_keeps_episode_fields`: edit the episode through the alpha API's post edit (copy the call from a test in `tests/test_api_routes_post.py`, or wherever `grep -ln "post_edit\|/api/alpha/post" tests/*.py` finds one). Then `episode_of(post)` still equals what `with_episode` stored.

- [ ] **Step 2: Run them red.** Run: `./run_tests.sh tests/test_episode_fields.py -q`. Expected: `ImportError` for `with_episode`, then field-missing failures.
- [ ] **Step 3: Helpers.** In `app/shared/media.py`, under `with_media`:

```python
def episode_of(post) -> dict:
    """The episode fields of `post` (spec §3.4, §10 A17.4), or {}."""
    podcast = (post.extensions or {}).get('podcast') if isinstance(post.extensions, dict) else None
    return dict(podcast) if isinstance(podcast, dict) else {}


def with_episode(post, episode_number, season, explicit) -> None:
    """Store the episode fields, keeping every other key, such as the Castopod `credits` (A17.4)."""
    podcast = {key: value for key, value in episode_of(post).items() if key not in ('episode_number', 'season')}
    if episode_number is not None:
        podcast['episode_number'] = episode_number
    if season is not None:
        podcast['season'] = season
    podcast['explicit'] = bool(explicit)
    extensions = dict(post.extensions) if isinstance(post.extensions, dict) else {}
    extensions['podcast'] = podcast
    post.extensions = extensions   # a new dict: db.JSON does not track in-place changes


def episode_form_wanted(community, post=None) -> bool:
    """The episode fields belong on a podcast community's audio upload, and on the edit of one (A17.4)."""
    if community.kind != COMMUNITY_KIND_PODCAST or not community.is_local():
        return False
    if post is None:
        return True
    return str((media_of(post) or {}).get('mime', '')).startswith('audio/')
```

Import `COMMUNITY_KIND_PODCAST` from `app.constants`. Check how `app/shared/media.py` gets `media_of`. If it defines its own `media_of`, use that one. If importing from `app.models` would make a cycle, place the three functions where `media_of` is defined instead and say so in the report.

- [ ] **Step 4: The mixin.** In `app/community/forms.py`:

```python
class EpisodeFieldsMixin:
    """Spec §3.4, §10 A17.4: a podcast episode's number, season and explicit flag."""
    episode_number = IntegerField(_l('Episode number'), validators=[Optional(), NumberRange(min=1)])
    season = IntegerField(_l('Season'), validators=[Optional(), NumberRange(min=1)])
    explicit = BooleanField(_l('Explicit episode'))
```

Make `CreateAudioForm` inherit it: `class CreateAudioForm(EpisodeFieldsMixin, MediaUrlOrFileMixin, CreatePostForm)`. Do the same for `CreateLinkForm`, which is the edit form of an audio upload: find its class line with `grep -n "^class CreateLinkForm" app/community/forms.py`. Import `IntegerField` and `NumberRange` if they are missing.

- [ ] **Step 5: Show or prune.** In `app/community/routes.py` `add_post`, right after the existing `if type in (MEDIA_KIND_VIDEO, MEDIA_KIND_AUDIO) and not media_upload: del form.image_file`, add:

```python
    if hasattr(form, 'episode_number') and not (type == MEDIA_KIND_AUDIO and media_upload
                                                and episode_form_wanted(community)):
        del form.episode_number, form.season, form.explicit   # A17.4: only a podcast's audio upload has them
```

In `app/post/routes.py` `post_edit`, after the form is built (the block that picks `CreateLinkForm` and the others), add:

```python
    if hasattr(form, 'episode_number') and not episode_form_wanted(post.community, post):
        del form.episode_number, form.season, form.explicit
```

Where the GET prefills the form, add:

```python
        if hasattr(form, 'episode_number'):
            episode = episode_of(post)
            form.episode_number.data = episode.get('episode_number')
            form.season.data = episode.get('season')
            form.explicit.data = episode.get('explicit', False)
```

Find the prefill block with `grep -n "form.title.data = post.title" app/post/routes.py`.

In `app/shared/post.py` `edit_post`, in the web branch (the `else` that reads `input.title.data`), add:

```python
        if hasattr(input, 'episode_number'):   # A17.4: the web form of a podcast episode; the API never has them
            with_episode(post, input.episode_number.data, input.season.data, input.explicit.data)
```

`make_post` calls `edit_post(..., from_scratch=True)` (`app/shared/post.py:346`), so this one write covers both create and edit. The routes add no `with_episode` call of their own.

- [ ] **Step 6: Template.** In `app/templates/post/post_edit.html`, directly after the `{% if 'audio_url' in form %} … {% else %} … link_url …` block (around line 129–137), add:

```html
            {% if 'episode_number' in form -%}
                <div class="row">
                    <div class="col-6">{{ render_field(form.episode_number) }}</div>
                    <div class="col-6">{{ render_field(form.season) }}</div>
                </div>
                {{ render_field(form.explicit) }}
            {% endif -%}
```

- [ ] **Step 7: Run green.** Run: `./run_tests.sh tests/test_episode_fields.py tests/test_media_upload.py tests/test_podcast_episode.py -q`. Expected: pass. Then run every test that touches the post edit or add-post routes: `./run_tests.sh $(grep -ln "submit/\|/edit'" tests/test_*.py | tr '\n' ' ') -q`. Expected: pass.
- [ ] **Step 8: Commit.**

```bash
git add app/shared/media.py app/community/forms.py app/community/routes.py app/post/routes.py app/shared/post.py app/templates/post/post_edit.html tests/test_episode_fields.py
git commit -m "feat: a podcast episode's number, season and explicit flag, set on its upload and edit forms" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Podcast category

**Files:**
- Create: `migrations/versions/e5b8d2f0a4c7_community_podcast_category.py`, `tests/test_podcast_category.py`
- Modify: `app/constants.py`, `app/models.py` (`Community`, beside `kind`), `app/shared/community.py`, `app/community/forms.py` (both community forms with `kind`, lines 66 and 158), `app/community/routes.py` (`add_local` near line 162, `community_edit` near line 1546), `app/templates/community/add_local.html`, `app/templates/community/community_edit.html`, `app/api/alpha/schema.py` (lines 806, 840), `app/api/alpha/utils/community.py` (lines 331–336, 380)

**Interfaces:**
- Produces:
  - `APPLE_PODCAST_CATEGORIES: tuple[str, ...]`, in `app.constants`;
  - `Community.podcast_category`;
  - `apply_podcast_category(community, value) -> str | None`, in `app.shared.community`.

  Task 4 reads `community.podcast_category`.

- [ ] **Step 1: Write the failing tests.** `tests/test_podcast_category.py`:

```python
"""Spec §3.3, §10 A17.5: a podcast's Apple Podcasts category."""
from app.constants import APPLE_PODCAST_CATEGORIES
from app.shared.community import apply_podcast_category
from tests.factories import make_community

REFUSAL = 'podcast_category must be one of the Apple Podcasts categories'


def test_the_list_is_apples_19(db_session):
    assert len(APPLE_PODCAST_CATEGORIES) == 19
    assert 'Technology' in APPLE_PODCAST_CATEGORIES and 'True Crime' in APPLE_PODCAST_CATEGORIES


def test_apply_sets_clears_and_refuses(db_session):
    community = make_community('pod')
    assert apply_podcast_category(community, 'Technology') is None
    assert community.podcast_category == 'Technology'
    assert apply_podcast_category(community, 'Podcasting') == REFUSAL   # a subcategory, not offered
    assert community.podcast_category == 'Technology'
    assert apply_podcast_category(community, '') is None
    assert community.podcast_category is None
    assert apply_podcast_category(community, None) is None
```

Copy the form and API setups from `tests/test_community_kind_rules.py`, which already exercises both forms and both API calls for `kind`. Then add:
- `test_create_form_sets_category`: a POST to `add_local` with `podcast_category=Technology` makes a community with that category.
- `test_edit_form_sets_and_refuses`: a POST to `community_edit` with `podcast_category=Comedy` stores it. A POST with `podcast_category=Nonsense` is a form error and saves nothing; the WTForms choice validation refuses it.
- `test_api_create_and_edit`: the API create with `podcast_category: 'News'` stores it. The API edit with `'Nonsense'` answers 400, the marshmallow `OneOf` refusal, and an edit without the key keeps the stored value.
- `test_migration_adds_and_drops_the_column`: copy the up/down pattern of the `kind` migration test (`grep -ln "c2f8d4b6a9e1\|d4a7c1e9b3f6" tests/*.py`).

- [ ] **Step 2: Run them red.** Run: `./run_tests.sh tests/test_podcast_category.py -q`. Expected: `ImportError: cannot import name 'APPLE_PODCAST_CATEGORIES'`.
- [ ] **Step 3: Constant, column, migration.** In `app/constants.py`, near the kinds:

```python
# Apple Podcasts' top-level categories (spec §3.3, §10 A17.5); subcategories are not offered
APPLE_PODCAST_CATEGORIES = ('Arts', 'Business', 'Comedy', 'Education', 'Fiction', 'Government', 'Health & Fitness',
                            'History', 'Kids & Family', 'Leisure', 'Music', 'News', 'Religion & Spirituality',
                            'Science', 'Society & Culture', 'Sports', 'Technology', 'True Crime', 'TV & Film')
```

In `Community`, after `kind`:

```python
    podcast_category = db.Column(db.String(64))   # APPLE_PODCAST_CATEGORIES; read by a podcast's feed (§4.5)
```

`migrations/versions/e5b8d2f0a4c7_community_podcast_category.py`:

```python
"""community podcast_category (spec 2026-10-14 §3.3, §10 A17.5)

Revision ID: e5b8d2f0a4c7
Revises: d4a7c1e9b3f6
"""
from alembic import op
import sqlalchemy as sa

revision = 'e5b8d2f0a4c7'
down_revision = 'd4a7c1e9b3f6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('community', schema=None) as batch_op:
        batch_op.add_column(sa.Column('podcast_category', sa.String(length=64), nullable=True))


def downgrade():
    with op.batch_alter_table('community', schema=None) as batch_op:
        batch_op.drop_column('podcast_category')
```

Before writing it, confirm that `d4a7c1e9b3f6` is still the only head: no file has `down_revision = 'd4a7c1e9b3f6'`.

- [ ] **Step 4: Shared rule.** In `app/shared/community.py`, under `apply_kind`:

```python
def apply_podcast_category(community, value) -> str | None:
    """Set `community.podcast_category` (spec §3.3, §10 A17.5); the error message, or None once applied. An empty
    value clears it."""
    if not value:
        community.podcast_category = None
        return None
    if value not in APPLE_PODCAST_CATEGORIES:
        return 'podcast_category must be one of the Apple Podcasts categories'
    community.podcast_category = value
    return None
```

- [ ] **Step 5: Forms and routes.** In both community forms, beside `kind`:

```python
    podcast_category = SelectField(_l('Podcast category'), choices=PODCAST_CATEGORY_CHOICES, default='',
                                   validators=[Optional()], render_kw={'class': 'form-select'},
                                   description=_l('Used by the RSS feed of a podcast community'))
```

Also add, next to `KIND_CHOICES`:

```python
PODCAST_CATEGORY_CHOICES = [('', _l('None'))] + [(category, category) for category in APPLE_PODCAST_CATEGORIES]
```

In `add_local`, after the community row is built from the form, call `shared_community.apply_podcast_category(community, form.podcast_category.data)`. The choice validation has already refused bad values, so the return value is None. In `community_edit`, beside the `apply_kind` call, call it and append any refusal to `form.podcast_category.errors` the way the kind refusal is handled. On the GET, set `form.podcast_category.data = community.podcast_category or ''`. In both templates, render `{{ render_field(form.podcast_category) }}` directly after `{{ render_field(form.kind) }}`.

- [ ] **Step 6: API.** In both community schemas, beside `kind`:

```python
    podcast_category = fields.String(validate=validate.OneOf(('',) + APPLE_PODCAST_CATEGORIES))
```

In `app/api/alpha/utils/community.py`:
- For create, pass `'podcast_category': data.get('podcast_category', '')` into the input dict.
- For edit, add it to `input` only `if 'podcast_category' in data`.

In `app/shared/community.py`'s `make_community` and `edit_community` API arms, call `apply_podcast_category(community, input['podcast_category'])` when the key is present. Turn a refusal into the same exception the kind refusal raises.

- [ ] **Step 7: Run green.** Run: `./run_tests.sh tests/test_podcast_category.py tests/test_community_kind_rules.py tests/test_api_routes_community.py -q`. Expected: pass.
- [ ] **Step 8: Commit.**

```bash
git add app/constants.py app/models.py migrations/versions/e5b8d2f0a4c7_community_podcast_category.py app/shared/community.py app/community/forms.py app/community/routes.py app/templates/community/add_local.html app/templates/community/community_edit.html app/api/alpha/schema.py app/api/alpha/utils/community.py tests/test_podcast_category.py
git commit -m "feat: a community's Apple Podcasts category, set from the forms and the API" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: The podcast feed

**Files:**
- Modify: `app/rss_extras.py`, `app/community/routes.py` (`show_community_rss`, near lines 829–890)
- Create: `tests/test_podcast_feed.py`

**Interfaces:**
- Consumes: `uses_podcast_episode` (Task 1), `episode_of` (Task 2), `Community.podcast_category` (Task 3), `media_of`, `is_open`.
- Produces:
  - `PodcastChannel` (a dataclass: `category: str | None`, `image: str`, `author: str`, `explicit: bool`);
  - `PodcastIndexExtension` / `PodcastIndexEntryExtension` (namespace `podcast` = `https://podcastindex.org/namespace/1.0`);
  - `RSSFeed.podcast: PodcastChannel | None = None`;
  - `episode_item_fields(post) -> dict`: enclosure, duration, episode, season, explicit, image and guid, for the fuzz target.

  All of these are in `app.rss_extras`.

- [ ] **Step 1: Write the failing tests.** `tests/test_podcast_feed.py` parses the feed with `xml.etree.ElementTree`. Its namespaces are `{'itunes': 'http://www.itunes.com/dtds/podcast-1.0.dtd', 'podcast': 'https://podcastindex.org/namespace/1.0'}`. Fetch with the test client: `client.get('/c/pod/feed')`. The route is cached, so clear the cache between requests the way existing feed tests do (`grep -ln "/feed" tests/test_*.py`); if none does, call `cache.clear()`. Use the `episode_post` fixture and add a second local post where a test needs one. Tests:
  - `test_channel_has_podcast_tags`: with `podcast_category='Technology'` and the owner's display name `Alice`, the channel has `itunes:category[@text="Technology"]`, `itunes:author` = `Alice`, `itunes:explicit` = `no`, `podcast:locked` = `no`, and an `itunes:image` whose `href` is the community icon's URL.
  - `test_channel_falls_back`: with no category, no icon and no banner, there is no `itunes:category`; `itunes:image@href` ends with `/static/images/apple-touch-icon.png`; with `community.user_id = None`, `itunes:author` is the community title; `nsfw=True` gives `itunes:explicit` = `yes`.
  - `test_item_has_enclosure_and_tags`: after `with_episode(post, 4, 2, False)`, the item has `enclosure@url == post.url`, `@length == '4406'`, `@type == 'audio/mpeg'`, `itunes:duration` = `2`, `itunes:episode` = `4`, `itunes:season` = `2`, `itunes:explicit` = `no`, and `podcast:guid` = the media uuid.
  - `test_explicit_episode_or_nsfw_post_is_explicit`: `with_episode(post, None, None, True)` gives item `itunes:explicit` = `yes`. Separately, a post with `nsfw=True` and `explicit` False also gives `yes`.
  - `test_item_without_cover_has_no_itunes_image`, and with a cover (`post.image` set as in Task 1) `itunes:image@href == post.image.source_url`.
  - `test_feed_leaves_out_non_episodes`: a text post, a processing audio post, a remote-audio link post and a followers-only episode are absent. Check the post status and visibility constants: `grep -n "is_open\|def is_open" app/visibility.py`. Only `episode_post` is listed.
  - `test_limit_counts_episodes_only`: 25 newer text posts plus `episode_post`; `/c/pod/feed?limit=20` still lists `episode_post`.
  - `test_invalid_xml_title_skips_only_that_item`: a second episode titled `'bad \x01 title'` is skipped; `episode_post` is listed.
  - `test_language_is_the_communitys`: a community whose first language is `fr` gives channel `language` = `fr`; with only `und`, `en`.
  - `test_other_kinds_unchanged`: for a discussion community the feed has no `itunes:` or `podcast:` element, no `enclosure`, and `language` = `en`.

- [ ] **Step 2: Run them red.** Run: `./run_tests.sh tests/test_podcast_feed.py -q`. Expected: assertion failures, because there are no podcast tags.
- [ ] **Step 3: Extension and item fields.** In `app/rss_extras.py`, beside `SlashExtension`:

```python
PODCAST_NS = 'https://podcastindex.org/namespace/1.0'


class PodcastIndexExtension(BaseExtension):
    """The Podcasting 2.0 tags feedgen has no extension for (spec §4.5): channel <podcast:locked>, item <podcast:guid>."""
    def __init__(self):
        self._locked = None

    def extend_ns(self):
        return {'podcast': PODCAST_NS}

    def extend_rss(self, rss_feed):
        if self._locked is not None:
            xml_elem('{%s}locked' % PODCAST_NS, rss_feed[0]).text = self._locked
        return rss_feed

    def locked(self, value: bool):
        self._locked = 'yes' if value else 'no'


class PodcastIndexEntryExtension(BaseExtension):
    def __init__(self):
        self._guid = None

    def extend_rss(self, entry):
        if self._guid:
            xml_elem('{%s}guid' % PODCAST_NS, entry).text = self._guid
        return entry

    def guid(self, value: str):
        self._guid = value


@dataclass
class PodcastChannel:
    """A podcast community's show fields for its feed (spec §3.3, §10 A17.6)."""
    category: str | None
    image: str
    author: str
    explicit: bool


def episode_item_fields(post) -> dict:
    """What an episode's feed item adds (A17.6), from its media record and episode fields."""
    media = media_of(post)
    episode = episode_of(post)
    fields = {'enclosure': (post.url, str(media['bytes']), media['mime']), 'duration': media['duration_s'],
              'explicit': 'yes' if episode.get('explicit') or post.nsfw or post.nsfl else 'no',
              'guid': media['uuid']}
    for key in ('episode_number', 'season'):
        if type(episode.get(key)) is int and episode[key] >= 1:   # a stored value is trusted only in this shape
            fields[key] = episode[key]
    if post.image is not None and post.image.source_url:
        fields['image'] = post.image.source_url
    return fields
```

Import `media_of` (from `app.models`), `episode_of` (from `app.shared.media`) and `xml_elem` (already imported). The file's comment says it is meant for upstreaming and imports no models. If importing `app.models` or `app.shared.media` makes a cycle, move `episode_item_fields` to `app/shared/media.py` and import it from there, and say so in the report.

- [ ] **Step 4: Use them in `RSSFeed`.** Add the field `podcast: PodcastChannel | None = None`. In `create_feed`, when `self.podcast`:

```python
        if self.podcast:
            fg.load_extension('podcast', rss=True)
            fg.register_extension('podcastindex', PodcastIndexExtension, PodcastIndexEntryExtension)
            if self.podcast.category:
                fg.podcast.itunes_category({'cat': self.podcast.category})
            fg.podcast.itunes_image(self.podcast.image)
            fg.podcast.itunes_author(self.podcast.author)
            fg.podcast.itunes_explicit('yes' if self.podcast.explicit else 'no')
            fg.podcastindex.locked(False)
```

Place the extension calls before the `for post in reversed(posts)` loop. Pass `podcast=self.podcast is not None` into `_add_post`. At the end of `_add_post`, when it is true:

```python
        if podcast:
            fields = episode_item_fields(post)
            fe.enclosure(*fields['enclosure'])
            fe.podcast.itunes_duration(fields['duration'])
            fe.podcast.itunes_explicit(fields['explicit'])
            if 'episode_number' in fields:
                fe.podcast.itunes_episode(fields['episode_number'])
            if 'season' in fields:
                fe.podcast.itunes_season(fields['season'])
            if 'image' in fields:
                fe.podcast.itunes_image(fields['image'])
            fe.podcastindex.guid(fields['guid'])
```

`fe.podcast.itunes_image` in feedgen 1.0 requires the URL to end with `.jpg` or `.png`. Check its source (`podcast_entry.py:127`). If it refuses other URLs (it raises ValueError), set the item image only when the URL ends in one of those. The thumbnail is always a `.jpg` (A13), so the ValueError arm is not reachable; leave it out. Feedgen's channel `itunes_image` has the same check, so the channel image falls back as specified if the icon URL has another extension; the test for the fallback chain covers it.

- [ ] **Step 5: The route.** In `show_community_rss`, after `posts = Post.query.filter(...)` and the score, tag and flair filters, and before `limit`:

```python
        podcast = None
        if community.kind == COMMUNITY_KIND_PODCAST and community.is_local():   # spec §4.5, §10 A17.6
            posts = posts.filter(Post.type == POST_TYPE_LINK,
                                 Post.extensions['media']['status'].as_string() == 'ready',
                                 Post.extensions['media']['mime'].as_string().startswith('audio/'))
```

Then, after `.limit(limit).all()`:

```python
        if community.kind == COMMUNITY_KIND_PODCAST and community.is_local():
            posts = [post for post in posts if uses_podcast_episode(post) and is_open(post)]
            podcast = PodcastChannel(
                category=community.podcast_category,
                image=(community.icon.source_url if community.icon_id and community.icon.source_url else image),
                author=(owner.display_name() if (owner := db.session.get(User, community.user_id)) else community.title)
                if community.user_id else community.title,
                explicit=bool(community.nsfw or community.nsfl))
```

Verify the names in this snippet before using it:
- `User.display_name()` (`app/models.py`; `Community.user_id` has no relationship, hence `db.session.get`);
- that the icon's public URL is `source_url`. Otherwise use `served_path(community.icon.file_path)` with `SERVER_URL`, as `icon_image()` does.

The Python filter is a second check for the shapes SQL does not test, such as positive-int `bytes`. The SQL filter is what makes `limit` count episodes. Set the channel `language` to the community's first language other than `und` (`community.languages`), else `'en'`, and only for a podcast community. Pass `podcast=podcast` to `RSSFeed`. If `.as_string()` is not available on this column type, use `Post.extensions[('media', 'status')].astext`. `test_limit_counts_episodes_only` proves whichever form you use.

- [ ] **Step 6: Run green.** Run: `./run_tests.sh tests/test_podcast_feed.py $(grep -ln "/feed\|RSSFeed" tests/test_*.py | tr '\n' ' ') -q`. Expected: pass.
- [ ] **Step 7: Commit.**

```bash
git add app/rss_extras.py app/community/routes.py tests/test_podcast_feed.py
git commit -m "feat: a podcast community's RSS feed is a Podcasting 2.0 feed of its episodes" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Fuzz targets

**Files:**
- Create: `tests/fuzz/targets/podcasts.py`, `tests/fuzz/corpus/podcast_episode_attachment/` (3 seeds), `tests/fuzz/corpus/podcast_feed_item/` (3 seeds)
- Modify: `tests/fuzz/targets/__init__.py` (`SURFACE_MODULES`), `tests/fuzz/surfaces.py`, `tests/fuzz/README.md` (targets table)

**Interfaces:**
- Consumes: `episode_attachment`, `uses_podcast_episode` (Task 1); `episode_item_fields` (Task 4).

- [ ] **Step 1: Copy the pattern.** Read `tests/fuzz/targets/video_channels.py`, especially `check_peertube_video` and its `Target`, plus `tests/fuzz/README.md`'s instructions for adding a surface. Write `tests/fuzz/targets/podcasts.py` with two targets, `needs_app=True`, surface `'podcasts'`. Both use the same `FuzzedDataProvider` decoding as `check_peertube_video` to build a fuzzed media record (any JSON-ish values for `status`, `mime`, `bytes`, `duration_s` and `uuid`) and a fuzzed title.
  - `podcast_episode_attachment`: builds an unsaved post in a local podcast community, the way `check_peertube_video` builds its own. If `uses_podcast_episode(post)` holds, it calls `episode_attachment(post)` and asserts: the result is a dict with `type == 'Document'`; `mediaType` starts with `audio/`; `url == post.url`; `len(name) <= 1500`. Otherwise it asserts nothing. The invariant is that no input raises.
  - `podcast_feed_item`: also fuzzes `extensions['podcast']` (any value, any shape). If `uses_podcast_episode(post)` holds, it calls `episode_item_fields(post)` and asserts: `enclosure[1]` is a decimal string; `explicit in ('yes', 'no')`; `episode_number` and `season`, when present, are ints of at least 1. Then it passes the fields through `feedgen` exactly as `_add_post` does, so a value feedgen refuses (`itunes_duration` raising ValueError) is found.
- [ ] **Step 2: Register.** Add `podcasts` to `SURFACE_MODULES`. In `tests/fuzz/surfaces.py`, add `'podcasts': ['app.activitypub.serializers.podcast.episode_attachment', 'app.rss_extras.episode_item_fields']`. Add 3 corpus seeds per target, in the format `video_channels`' seeds use: a valid episode, one with a float duration, and one with a garbage `podcast` key. Add both rows to the README targets table, dated 2026-10-16.
- [ ] **Step 3: Run.** First run the registry and smoke checks the README names. For example, `./run_tests.sh tests/fuzz -q` runs the registry tests; the smoke runner is the one the README names. Then run a short campaign per target with the README's campaign command, at the budget it gives for a new target. Expected: no crash. A crash is a defect: fix it TDD, in its own commit, with a unit test in `tests/test_podcast_episode.py` or `tests/test_podcast_feed.py` that reproduces it first.
- [ ] **Step 4: Commit.**

```bash
git add tests/fuzz/
git commit -m "test(fuzz): targets for the podcast episode attachment and feed item" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Interop and gates

**Files:**
- Create: `interop/test_podcast.py`
- Modify: `interop/README.md`, `docs/superpowers/specs/2026-10-14-native-federation-design.md` (A17 gates line)

**Interfaces:**
- Consumes: `_upload` in `interop/test_media_pipeline.py`; `Mastodon` (`resolve_account`, `follow`, `relationship`, `home`, `reply`) and `PieFed` (`replies`) in `interop/clients.py`; the seeded `pod_media` community (podcast kind, alice moderates).

- [ ] **Step 1: Write the test.** `interop/test_podcast.py`:

```python
"""Step 6 (spec §10 A16, A17): an audio episode uploaded to pod_media plays on Mastodon, a Mastodon reply comes back,
and the community's feed is a podcast feed."""
import re
import ssl
import xml.etree.ElementTree as ET

import httpx
import pytest

from interop.clients import CA, PieFedWeb, wait_for
from interop.test_media_pipeline import _upload

pytestmark = pytest.mark.interop
NS = {'itunes': 'http://www.itunes.com/dtds/podcast-1.0.dtd', 'podcast': 'https://podcastindex.org/namespace/1.0'}


@pytest.fixture(scope='module')
def alice_web():
    return PieFedWeb('alice', 'interop-alice-pw')


@pytest.fixture(scope='module')
def bob_follows_pod_media(mastodon):
    account = wait_for(lambda: mastodon.resolve_account('pod_media@piefed.test'))
    mastodon.follow(account['id'])
    wait_for(lambda: mastodon.relationship(account['id'])['following'])
    return account


def test_an_episode_plays_on_mastodon_and_a_reply_comes_back(alice_web, bob_follows_pod_media, mastodon, piefed):
    url, post_id, media = _upload(alice_web, 'pod_media', 'audio', 'audio.mp3', 'audio/mpeg')

    def boosted():
        for status in mastodon.home():
            shown = status.get('reblog') or status
            if any(m.get('remote_url') == url for m in shown.get('media_attachments', [])):
                return shown
        return None
    status = wait_for(boosted, timeout=60)
    audio = next(m for m in status['media_attachments'] if m['remote_url'] == url)
    assert audio['type'] == 'audio'   # Mastodon downloaded it and made a player, not an 'unknown' link
    mastodon.reply(status['id'], '@alice@piefed.test nice episode')
    wait_for(lambda: any('nice episode' in c['comment']['body'] for c in piefed.replies(post_id)), timeout=60)


def test_the_feed_is_a_podcast_feed(alice_web):
    url, post_id, media = _upload(alice_web, 'pod_media', 'audio', 'audio.mp3', 'audio/mpeg')
    with httpx.Client(verify=ssl.create_default_context(cafile=CA), timeout=10) as http:
        feed = ET.fromstring(http.get('https://piefed.test/c/pod_media/feed?limit=100').content)
    items = {item.find('enclosure').get('url'): item for item in feed.iter('item')}
    item = items[url]
    assert item.find('enclosure').get('length') == str(media['bytes'])
    assert item.find('enclosure').get('type') == 'audio/mpeg'
    assert item.find('itunes:duration', NS).text == str(media['duration_s'])
    assert item.find('podcast:guid', NS).text == media['uuid']
    assert feed.find('channel/podcast:locked', NS).text == 'no'
```

The feed is cached for 600 s. The upload creates a new post, and the route's ETag changes with `last_active`, but `@cache.cached` keys on the URL and query string. The `limit=100` query string is a fresh cache key for this test. If a rerun on a reused stack hits a cached feed, add a unique dummy query parameter (`&t=<post_id>`) and say so in a comment.

- [ ] **Step 2: Run it.** Run: `interop/run.sh test -q interop/test_podcast.py`. Expected: 2 passed. If Mastodon shows the attachment as `unknown`, read `podman-compose -f compose.interop.yaml logs mastodon-sidekiq` for the download error before changing any code; the media URL must be served with `Access-Control-Allow-Origin` and reachable through the harness CA. Then run the full suite on a fresh stack: `interop/run.sh down && interop/run.sh test -q`. Expected: 22 passed, the 20 earlier tests plus these 2. Then run it once more on the reused stack: `interop/run.sh test -q`. Expected: 22 passed.
- [ ] **Step 3: README.** Add to `interop/README.md`'s test list: `` - `test_podcast.py`: an audio episode uploaded to `pod_media` plays on Mastodon as an audio attachment, a Mastodon reply lands in the PieFed thread, and the community feed carries the episode's enclosure, duration and guid. ``
- [ ] **Step 4: Full gate.** Run the coverage chain from Global Constraints with base `4c726f138`. Expected: all tests pass, every floor met, and zero changed-line misses with `--branches`.
- [ ] **Step 5: Gates evidence.** Append to A17 in the spec a sub-bullet `- **Gates (2026-10-16, Task 6).**`. Give the interop results (the two tests, what they proved, the fresh and reused suite counts), the unit suite's pass count, and the coverage outcome. Keep it in the style of A15's gates line.
- [ ] **Step 6: Commit.**

```bash
git add interop/test_podcast.py interop/README.md docs/superpowers/specs/2026-10-14-native-federation-design.md
git commit -m "test(interop): a podcast episode plays on Mastodon, its reply returns, and the feed is a podcast feed" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
