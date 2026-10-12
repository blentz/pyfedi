# AP interop C4: Create of Audio, Image, Document and Video as posts — design

Date: 2026-10-12. Track: tier C of `docs/superpowers/specs/2026-10-10-ap-gaps-roadmap.md` ("Accept Create of Audio,
Image, Document and Video as first-class objects. This is a product decision.").

## Goal

A peer that publishes a media object directly — PeerTube `Video`, Funkwhale `Audio`, an `Image`, a `Document` — gets
a post on this instance, in the right community, through the same path as a `Note` or `Page`.

Today a Create of a `Video` this instance has not already stored is dropped as "PeerTube post not found"
(`app/activitypub/routes.py`, the `object_type == 'Video'` arm), and `Audio`, `Image` and `Document` are dropped as
"Unacceptable type (create)". A Video reaches a post only when it is fetched or announced (`Post.new`'s Video branch).

Out of scope: sending these types, a new post type, fetching the media at ingest, media transcoding, galleries of
several media objects.

## Owner rulings (2026-10-12)

1. **Mapping onto existing post types:** Video → Video post (unchanged); Image → Image post; Audio → Link post to the
   audio file with an audio player; Document → Link post to the file. No new post type.
2. **Path:** the four types join the inbox's new-content types and go through `process_new_content` → `Post.new` /
   `update_post_from_activity`, so every existing check (community routing, `can_create_post`, local-only and gone
   communities, blocked phrases, domain bans, C2 quotes, C3 languages) applies unchanged.
3. **Community:** as for a Note — the addressed community (`audience`/`to`/`cc`, `find_community`), a PeerTube
   channel `Group` in `attributedTo`, else the microblogs community.

## Ingest

### Which types

`MEDIA_OBJECT_TYPES = ('Audio', 'Document', 'Image', 'Video')` (new, `app/activitypub/util.py`). The inbox's
`new_content_types` becomes `['Page', 'Article', 'Link', 'Note', 'Question', 'Event', *MEDIA_OBJECT_TYPES]`, and
`DISPATCHED_AP_TYPES` gains `Audio`, `Document`, `Image` so an array-valued `type` resolves to them.

- **Create** of a media object not yet stored creates a post.
- **Update** of a media object already stored updates it (author, moderator or admin, as for a Page today).
- **Update** of a media object not stored is ignored and logged `Update of unknown media object` — PeerTube sends
  frequent Updates (view counts) and an Update must not create a post for these types. The current Video arm
  (update an existing Video post, else log "PeerTube post not found") is replaced by this rule.
- **Delete** is unchanged (by id).
- A media object with `inReplyTo` is treated as a comment exactly as a Note with `inReplyTo` is today (no new code).

### The media URL

`media_url_from(obj: dict, kind: str) -> str | None` (new, `app/activitypub/util.py`), pure, no network. `kind` is
`'image'`, `'audio'` or `None` (any):

- Reads `obj['url']`: a string; a dict with `href` (Link) or `url`; or a list of those. The first entry whose
  `mediaType` starts with `kind + '/'` wins; when `kind` is `None`, or no entry carries a matching `mediaType`, the
  first entry with an `http`/`https` href. A list entry whose `mediaType` names another kind (`video/*` for an
  Audio) is skipped.
- The result must pass `url_is_storable`; otherwise None.
- At most the first 20 entries of a list are read.

### Mapping in `Post.new`

Applied where `Post.new` sets the post's url and type today (after the attachment handling, before the existing
`url_is_storable` guard, so that guard still runs):

- **Video:** unchanged.
- **Image:** `post.url = media_url_from(obj, 'image')`; the post is an Image post even when the URL has no image file
  extension (`is_image_url` is extension-based); the existing image branch then runs (hash, File row with
  `source_url`, no fetch at ingest beyond what an Image post does today). `obj['name']`, when present, is the image's
  alt text.
- **Audio:** `post.url = media_url_from(obj, 'audio')`; a Link post; `post.extensions['media_type'] = 'audio'`
  (the existing JSON column, assigned as a whole new dict).
- **Document:** `post.url = media_url_from(obj, None)`; a Link post.
- No usable URL (Image, Audio, Document): the post is not created; the inbox logs `Unacceptable media object`.
- **Title:** `name` when present and non-empty; otherwise the existing microblog rule (first sentence of the body);
  otherwise the type's name (`'Image'`, `'Audio'`, `'Document'`) as a plain English string (stored data, not
  translated). Body from `content` as today (including PeerTube's `mediaType: text/markdown`).

### Update

`update_post_from_activity` applies the same mapping for a media object: URL, title, body, and for Audio the
`media_type` marker. A URL that fails `media_url_from` leaves the stored URL unchanged (an Update must not erase the
media). The existing Video likes/dislikes refresh is unchanged.

## Render

- The post page's audio player shows when `post.url` ends in an audio extension (today) **or**
  `(post.extensions or {}).get('media_type') == 'audio'`.
- Nothing else changes: Image and Video posts render as today; a Document is a link post.

## API

No change: the post types are the existing ones.

## Errors and limits

- Malformed `url` shapes are skipped entry by entry; an object with no usable URL is refused, not stored half-made.
- No fetch, sleep or retry on the inbox path (D738/D775) beyond what the existing Video likes/dislikes refresh
  already does.

## Testing

TDD, 100% line and branch coverage of changed lines, one commit per defect, gates via `./run_tests.sh`.

- `media_url_from`: string, Link dict (`href`), dict with `url`, list with matching and non-matching `mediaType`,
  no `mediaType`, `javascript:` and relative URLs, non-list junk, more than 20 entries.
- Create from fixtures shaped like real senders: PeerTube `Video` (channel Group in `attributedTo`), Funkwhale
  `Audio` (url list of Links with `audio/*`), an `Image` with a bare string url, a `Document`; each lands in the right
  community with the right post type, url, title and body.
- Refusals: no URL; only a `javascript:` URL.
- Update of a stored media post changes URL/title/body; Update of an unknown media object is ignored and logged; an
  Update with no usable URL keeps the stored URL.
- Audio player shows for an Audio post whose URL has no audio extension.
- Array-valued `type` (`['Audio', 'schema:AudioObject']`) resolves to Audio.

## Review focus

- A hostile `url` (javascript:, data:, relative, huge list) never reaches `post.url` or a template.
- An Update must never create a post for a media type, and never erase a stored URL.
- Every check a Note passes through still applies (blocked phrases, banned domains, local-only and gone
  communities, `can_create_post`).
- No new network access on the inbox path.
