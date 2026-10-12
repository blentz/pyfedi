# AP interop C2: quote posts on input — design

Date: 2026-10-12. Track: tier C of `docs/superpowers/specs/2026-10-10-ap-gaps-roadmap.md` ("Read legacy quote
properties: `quoteUrl`, `_misskey_quote`, `quoteUri`"), widened by the owner to FEP-044f `quote` and to an
embedded card.

## Goal

A quote post or quote comment arriving from Mastodon, Misskey, Fedibird or another FEP-044f sender shows what it
quotes. Today the quote is dropped: the object becomes a plain post or comment, and the quoted link survives only
when the sender's "RE: <link>" fallback happens to become the microblog post's `url`.

Out of scope: sending quotes, fetching unknown quoted objects, verifying remote `quoteAuthorization` stamps,
quote counts, notifications to the quoted author. The outbound FEP-044f QuoteRequest/Accept flow
(`process_quote_boost`, `/quote_boost_auth`, `QuoteAuthorization`) is unchanged.

## Owner rulings (2026-10-12)

1. **Display:** an embedded card when the quoted object is known locally; otherwise a plain "Quoting: <link>" line.
   No fetch of an unknown quoted object.
2. **Authorization:** a quoted object authored locally shows as a card only when it is open (`is_open`) or a
   `QuoteAuthorization` row exists for that (object, quoting id) pair; otherwise the plain line. A quoted remote
   object shows as a card without verification.
3. **Scope:** both posts (`Post`) and comments (`PostReply`) carry a quote; the quoted object may be either.
4. **Fallback:** the sender's fallback is stripped from the body at ingest, and the quoted id never becomes the
   post's `url`.
5. **Storage:** approach 1 — one string column per table, resolved at render in one batch per page.

## Data

- `post.quote_ap_id` and `post_reply.quote_ap_id`: `db.String(255)`, nullable, no index. 255 matches
  `Post.ap_id`/`PostReply.ap_id`; a longer id cannot match any stored object, so it is not stored.
- One Alembic migration adding both columns, on the current single head. Nullable add, no backfill.

## Ingest

### Reading the quoted id

`quote_id_from(obj: dict) -> str | None` (new, in `app/activitypub/util.py`):

- Reads `quote`, then `quoteUrl`, then `quoteUri`, then `_misskey_quote`. The first key whose value is valid wins;
  an invalid value is skipped, not fatal.
- Valid: an `http`/`https` URL string with a host, at most 255 characters, ASCII only; or a dict whose `id` is
  such a string. A list, number, null, other scheme, or empty string is invalid.
- Returns None when the id equals the object's own `id` (a self-quote).

### Where it is applied

- `Post.new` and `PostReply.new` set `quote_ap_id = quote_id_from(object)`.
- The Update paths for a post and for a comment set it again from the updated object: an Update that adds, changes
  or drops the quote properties sets, changes or clears `quote_ap_id`.
- No network access. D738/D775 hold: nothing on the inbox path fetches, sleeps or retries for a quote.

### Fallback stripping

Only when `quote_ap_id` is set, and only on HTML content (the `source` Markdown path is untouched — PieFed and
other Markdown senders do not add the fallback):

- Remove every element whose `class` contains `quote-inline` (Mastodon). `allowlist_html` keeps `class`, so this runs
  on its output.
- Ruling R1 (Misskey fallback form). The rule "last `<p>` whose text is `RE: <link>`" misses Misskey, which puts
  `RE: <a>` at the end of the same paragraph after `<br><br>`. The rule implemented: in the last top-level `<p>`, when
  its last link's `href` equals `quote_ap_id`, that link ends the paragraph's text, and the text node just before it is
  exactly `RE:` (stripped), remove the link, that text node and the `<br>`s right before it; drop the paragraph if
  nothing visible is left. The author's text is never touched.
- `body` is derived from the stripped `body_html` as today.

### Microblog url

In `Post.new`'s microblog branch, a link equal to `quote_ap_id` is not taken as `post.url`, from either
`microblog_content_to_title` or `microblog_content_to_link`; the next qualifying link, if any, is used.

## Render

### Batch resolution

`resolve_quotes(items) -> dict[str, Post | PostReply]` (new, `app/utils.py` or a small `app/quotes.py`): collects
the distinct non-null `quote_ap_id` of the posts/comments on the page, and loads matching `Post` and `PostReply`
rows with one `IN` query per table. Every list and single-object view that renders a post or comment body calls it
once and hands the map to the templates.

### Card or line

For one quoting item and its resolved quoted object `q` (possibly missing), the card shows only when all hold;
otherwise the plain line `Quoting: <a href="quote_ap_id">quote_ap_id</a>` (escaped, `rel="nofollow ugc"`):

- `q` exists and is not deleted; `q`'s community (the post's, or the comment's post's) is not gone, not blocked
  by the viewer, and the viewer is not banned from it.
- `can_view(q, viewer_id)`.
- `q`'s author is not blocked by the viewer, and `q`'s instance is not blocked by the viewer or the site.
- NSFW/NSFL `q` follows the viewer's existing hide/blur settings for posts; hidden means the plain line.
- Authorization (ruling 2): `q.author.is_local()` implies `is_open(q)` or a `QuoteAuthorization` row with
  `quoting_uri == quoting item's ap_id` and `post_id`/`post_reply_id == q.id`.

The card is a compact, read-only block: author, community (posts), title (posts) or a text excerpt (comments, and
posts without a title), relative time, and a link to `q`'s local page. It never renders `q`'s own quote: nesting is
one level.

### Templates

A single macro `quote_card(item, quotes)` included by the full post view, the post teaser macros (both the default
theme and `themes/dillo`), and the comment teaser. Exact include points are fixed in the plan.

## API

`quote_ap_id` (string or null) is added read-only to the alpha API's post view and comment view schemas.

## Errors and limits

- Malformed quote properties are ignored; the object is still accepted.
- A quote pointing at an object we never receive stays a plain line forever. Accepted by ruling 1.
- A quoted object deleted later turns the card back into a plain line at the next render.

## Testing

TDD, 100% line and branch coverage of changed lines, one commit per defect, gates via `./run_tests.sh`.

- `quote_id_from`: each of the four keys; precedence when several are present; an invalid first key falling
  through to a valid later one; dict form; list, number, null, `ftp:`, hostless, over-255, non-ASCII, self-quote.
- Ingest: `Post.new` and `PostReply.new` store the id; Update sets, changes and clears it.
- Fallback: `quote-inline` removed; trailing `RE: <link>` removed when the link matches; kept when the link differs,
  when extra text is present, when it is not the last paragraph, and when no quote is set; Markdown source untouched.
- Microblog url: the quoted link is skipped and the next link used; no other link means no url.
- Resolution: one query per table for a page of many quoting items; missing ids absent from the map.
- Card vs line: each condition above flips a card to the line; local open, local followers-only without and with
  a `QuoteAuthorization`; remote shown unverified; nesting stops at one level.
- API: field present on post and comment views.
- Migration: upgrade and downgrade on the single head.

## Review focus

- A quote of a followers-only local post must not leak its content to a viewer who cannot see it.
- A hostile `quote` value (very long, `javascript:`, HTML in the URL) must never reach the template unescaped.
- A feed page of 50 quoting posts must not issue 50 lookups.
- Stripping must never remove the author's own text.
- A quoted post in a community the viewer blocked or was banned from shows only the plain line.
