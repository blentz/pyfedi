# AP interop C2: quote posts on input — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An inbound quote post or quote comment records the id it quotes, loses the sender's "RE: <link>" fallback, and renders a compact card (or a plain "Quoting: <link>" line) for the quoted object.

**Architecture:** A nullable `quote_ap_id` column on `post` and `post_reply` is filled at ingest by one reader (`quote_id_from`). A new module `app/quotes.py` resolves quoted ids to local rows — primed in one batch per rendered template by a `before_render_template` subscriber — and decides card vs line for the current viewer. One Jinja macro renders the result everywhere a post or comment body is shown.

**Tech Stack:** Flask, SQLAlchemy, Alembic, Jinja2, BeautifulSoup, marshmallow (alpha API), pytest via `./run_tests.sh`.

**Spec:** `docs/superpowers/specs/2026-10-12-ap-interop-c2-quote-posts-design.md`

## Global Constraints

- Inbox path: no fetch, sleep or retry for a quote (D738/D775).
- `quote_ap_id`: `db.String(255)`, nullable, no index, on both `post` and `post_reply`.
- Quote keys, in precedence order: `quote`, `quoteUrl`, `quoteUri`, `_misskey_quote`.
- Valid quoted id: `http`/`https` URL with a host, at most 255 characters, ASCII, no whitespace or control characters; or a dict whose `id` is one. A self-quote is None.
- Fallback stripping runs only when `quote_ap_id` is set and only on HTML content; the `source` Markdown and `text/markdown` paths are untouched.
- Card nesting is one level: the card never renders the quoted object's own quote.
- TDD; 100% line and branch coverage of changed lines; one commit per defect; gates via `./run_tests.sh` (single file: `./run_tests.sh tests/test_x.py -v`).
- Commit attribution line: `Co-Authored-By: <your model> <noreply@anthropic.com>`.
- Migration on the single head `f4b8d2a6c1e7`.

## Plan rulings (controller, for the owner's visibility)

- **R1 Misskey fallback form.** The spec's rule "last `<p>` whose text is `RE: <link>`" misses Misskey, which puts `RE: <a>` at the end of the *same* paragraph after `<br><br>`. The rule implemented: in the last top-level `<p>`, when its last link's `href` equals `quote_ap_id`, that link ends the paragraph's text, and the text node just before it is exactly `RE:` (stripped), remove the link, that text node and the `<br>`s right before it; drop the paragraph if nothing visible is left. The author's text is never touched. The spec gets this amendment in Task 3's commit.
- **R2 Batching.** 47 templates render posts; wiring `resolve_quotes` into each view is brittle. A `before_render_template` subscriber scans the template context (Post/PostReply rows, lists, Pagination `.items`, comment-tree dicts) and resolves every quoted id with one query per table. A lookup the scan missed falls back to a single memoised query per id per request.
- **R3 Local URL forms.** A local post's `ap_id` may be the slug form (`/c/<name>@<domain>/p/<id>/<slug>`), while a peer may quote `/post/<id>` or `/comment/<id>`. Resolution also accepts `{SERVER_URL}/post/<int>` and `{SERVER_URL}/comment/<int>` by primary key.
- **R4 NSFW.** The card shows a sensitive (NSFW/NSFL) quoted object only to a signed-in viewer whose matching setting is Show (`hide_nsfw == 0` / `hide_nsfl == 0`). Blur, Hide and anonymous viewers get the plain line.
- **R5 Site instance bans.** "Instance blocked by the site" is a `BannedInstances` row for the quoted object's instance domain.

## Review Focus

1. A viewer who cannot see a followers-only quoted post sees only the plain line, never its excerpt — Task 4 `test_followers_only_quoted_post_is_a_line_for_a_stranger`.
2. A `quote` value of `javascript:alert(1)`, `https://x.test/"><script>`, or 300 characters is never stored, so never reaches a template — Task 1 `test_hostile_values_are_refused`; the line is escaped — Task 5 `test_the_line_escapes_the_url`.
3. A page of many quoting posts issues one query per table — Task 4 `test_priming_resolves_a_page_in_one_query_per_table`.
4. Stripping never removes the author's text — Task 3 `test_text_beside_the_link_keeps_the_paragraph` and `test_misskey_fallback_keeps_the_text_before_it`.
5. A quoted post in a community the viewer blocked or is banned from is a line — Task 4 `test_blocked_community_is_a_line` and `test_banned_from_community_is_a_line`.

---

### Task 1: `quote_ap_id` columns and `quote_id_from`

**Files:**
- Create: `migrations/versions/a7c3e9f1b5d2_quote_ap_id.py`
- Modify: `app/models.py` (class `Post` near line 2559 `ap_id`, class `PostReply` near line 4026 `ap_id`)
- Modify: `app/activitypub/util.py` (new function beside `process_quote_boost`, ~line 5375)
- Test: `tests/test_quote_posts.py` (new)

**Interfaces:**
- Produces: `Post.quote_ap_id`, `PostReply.quote_ap_id` (str | None); `quote_id_from(obj: dict) -> str | None` in `app/activitypub/util.py`.

- [ ] **Step 1: Write the failing tests**

```python
"""C2: inbound quote posts (docs/superpowers/specs/2026-10-12-ap-interop-c2-quote-posts-design.md)."""
import pytest

from app.activitypub.util import quote_id_from

OWN = 'https://remote.test/notes/1'
QUOTED = 'https://other.test/notes/9'


@pytest.mark.parametrize('key', ['quote', 'quoteUrl', 'quoteUri', '_misskey_quote'])
def test_each_key_is_read(key):
    assert quote_id_from({'id': OWN, key: QUOTED}) == QUOTED


def test_quote_wins_over_the_legacy_keys():
    obj = {'id': OWN, '_misskey_quote': 'https://a.test/1', 'quoteUri': 'https://b.test/1',
           'quoteUrl': 'https://c.test/1', 'quote': QUOTED}
    assert quote_id_from(obj) == QUOTED


def test_an_invalid_first_key_falls_through():
    assert quote_id_from({'id': OWN, 'quote': 42, 'quoteUrl': QUOTED}) == QUOTED


def test_dict_form_uses_its_id():
    assert quote_id_from({'id': OWN, 'quote': {'id': QUOTED, 'type': 'Note'}}) == QUOTED


@pytest.mark.parametrize('value', [
    None, '', 42, [QUOTED], {'id': 7}, {}, 'ftp://other.test/1', 'https:///nohost', 'javascript:alert(1)',
    'https://other.test/' + 'a' * 240, 'https://ötherr.test/1', 'https://other.test/a b',
    'https://other.test/\x00', 'https://[::1/', 'other.test/1',
])
def test_hostile_values_are_refused(value):
    assert quote_id_from({'id': OWN, 'quote': value}) is None


def test_exactly_255_characters_is_accepted():
    url = 'https://other.test/' + 'a' * (255 - len('https://other.test/'))
    assert quote_id_from({'id': OWN, 'quote': url}) == url


def test_a_self_quote_is_none():
    assert quote_id_from({'id': OWN, 'quote': OWN}) is None


def test_no_quote_is_none():
    assert quote_id_from({'id': OWN}) is None


def test_columns_exist(app, db_session):
    from app.models import Post, PostReply
    assert Post.quote_ap_id.type.length == 255
    assert PostReply.quote_ap_id.type.length == 255
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./run_tests.sh tests/test_quote_posts.py -v`
Expected: FAIL — `ImportError: cannot import name 'quote_id_from'`.

- [ ] **Step 3: Add the columns** — in `class Post`, right after the `ap_id` column (models.py ~2559), and in `class PostReply` right after its `ap_id` (~4026):

```python
    quote_ap_id = db.Column(db.String(255))  # C2: the id this inbound object quotes (FEP-044f quote or a legacy key)
```

- [ ] **Step 4: Write the migration** `migrations/versions/a7c3e9f1b5d2_quote_ap_id.py`:

```python
"""post and post_reply quote_ap_id (C2 quote posts on input)

Revision ID: a7c3e9f1b5d2
Revises: f4b8d2a6c1e7
"""
import sqlalchemy as sa
from alembic import op

revision = 'a7c3e9f1b5d2'
down_revision = 'f4b8d2a6c1e7'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('post', sa.Column('quote_ap_id', sa.String(length=255), nullable=True))
    op.add_column('post_reply', sa.Column('quote_ap_id', sa.String(length=255), nullable=True))


def downgrade():
    op.drop_column('post_reply', 'quote_ap_id')
    op.drop_column('post', 'quote_ap_id')
```

- [ ] **Step 5: Write `quote_id_from`** in `app/activitypub/util.py`, just above `def process_quote_boost` (`urlsplit` from `urllib.parse`; add the import if the module lacks it):

```python
QUOTE_KEYS = ('quote', 'quoteUrl', 'quoteUri', '_misskey_quote')  # FEP-044f first, then Fedibird/Misskey legacy


def _valid_quote_id(value) -> bool:
    if not isinstance(value, str) or not value or len(value) > 255 or not value.isascii():
        return False
    if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7f for c in value):
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme in ('http', 'https') and bool(parts.hostname)


def quote_id_from(obj: dict) -> str | None:
    """The id `obj` quotes (C2): the first valid value of QUOTE_KEYS, as a string or an object's `id`; None for
    none, or for a self-quote. An invalid value is skipped, so a later key still counts."""
    for key in QUOTE_KEYS:
        value = obj.get(key)
        if isinstance(value, dict):
            value = value.get('id')
        if _valid_quote_id(value):
            return None if value == obj.get('id') else value
    return None
```

- [ ] **Step 6: Run the tests and the migration check**

Run: `./run_tests.sh tests/test_quote_posts.py -v` — Expected: PASS.
Run: `./run_tests.sh tests/ -k "migration or alembic" -q` — Expected: PASS (single head).

- [ ] **Step 7: Commit**

```bash
git add migrations/versions/a7c3e9f1b5d2_quote_ap_id.py app/models.py app/activitypub/util.py tests/test_quote_posts.py
git commit -m "feat: post and comment store the id an inbound object quotes"
```

---

### Task 2: Ingest and Update set `quote_ap_id`

**Files:**
- Modify: `app/models.py` — `Post.new` (~2670, the `Post(...)` constructor ~2703), `PostReply.new` (~4069, constructor ~4107)
- Modify: `app/activitypub/util.py` — `update_post_from_activity` (~4443), `update_post_reply_from_activity` (~4282)
- Test: `tests/test_quote_posts.py`

**Interfaces:**
- Consumes: `quote_id_from(obj: dict) -> str | None` (Task 1).
- Produces: every inbound Create/Update of a post or comment leaves `quote_ap_id == quote_id_from(request_json['object'])`.

- [ ] **Step 1: Write the failing tests** (append; reuse the setup idiom of `tests/test_ap_peer_source_and_timestamps.py:117-145` and `tests/test_ap_create_reply.py:95-150`)

```python
from app import db
from app.activitypub.util import create_post_reply, update_post_from_activity, update_post_reply_from_activity
from app.models import Post
from tests.factories import make_community, make_community_member, make_instance, make_post, make_user

PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'
PEER = 'remote.test'


@pytest.fixture
def world(app, db_session):
    instance = make_instance(PEER)
    community = make_community('quoteland')
    author = make_user(instance, 'quoter')
    author.ap_id = f'quoter@{PEER}'
    author.ap_profile_id = author.ap_public_url = f'https://{PEER}/u/quoter'
    make_community_member(author, community)
    parent = make_post(community, author, ap_id=f'https://{PEER}/post/1')
    db.session.commit()
    return community, author, parent


def _create(obj):
    return {'id': obj['id'] + '/create', 'type': 'Create', 'to': [PUBLIC], 'object': obj}


def _note(n, **fields):
    obj = {'id': f'https://{PEER}/notes/{n}', 'type': 'Note', 'attributedTo': f'https://{PEER}/u/quoter',
           'to': [PUBLIC], 'cc': [], 'content': '<p>hello there friends</p>', 'published': '2026-01-01T00:00:00Z'}
    obj.update(fields)
    return obj


def test_post_new_stores_the_quote(world):
    community, author, _ = world
    post = Post.new(author, community, _create(_note(2, quote=QUOTED)))
    assert post.quote_ap_id == QUOTED


def test_post_new_without_a_quote_stores_none(world):
    community, author, _ = world
    assert Post.new(author, community, _create(_note(3))).quote_ap_id is None


def test_reply_stores_the_quote(world, redis_lock_only_double):
    community, author, parent = world
    reply = create_post_reply(store_ap_json=False, community=community, in_reply_to=parent.ap_id,
                              request_json=_create(_note(4, inReplyTo=parent.ap_id, quoteUri=QUOTED)), user=author)
    assert reply.quote_ap_id == QUOTED


@pytest.mark.parametrize('before,after', [(None, QUOTED), (QUOTED, 'https://other.test/notes/10'), (QUOTED, None)])
def test_post_update_sets_changes_and_clears(world, before, after):
    community, author, _ = world
    post = Post.new(author, community, _create(_note(5, **({'quote': before} if before else {}))))
    update_post_from_activity(post, {'id': 'u', 'type': 'Update', 'object': _note(5, **({'quote': after} if after else {}))})
    assert post.quote_ap_id == after


@pytest.mark.parametrize('before,after', [(None, QUOTED), (QUOTED, None)])
def test_reply_update_sets_and_clears(world, redis_lock_only_double, before, after):
    community, author, parent = world
    reply = create_post_reply(store_ap_json=False, community=community, in_reply_to=parent.ap_id,
                              request_json=_create(_note(6, inReplyTo=parent.ap_id, **({'quote': before} if before else {}))),
                              user=author)
    update_post_reply_from_activity(reply, {'id': 'u', 'type': 'Update',
                                            'object': _note(6, inReplyTo=parent.ap_id, **({'quote': after} if after else {}))})
    assert reply.quote_ap_id == after
```

If a fixture name above (`redis_lock_only_double`) or a factory differs, read `tests/test_ap_create_reply.py` and copy its exact setup; the assertions stay as written.

- [ ] **Step 2: Run to verify failure**

Run: `./run_tests.sh tests/test_quote_posts.py -v` — Expected: the new tests FAIL with `quote_ap_id` None.

- [ ] **Step 3: Implement**
  - `Post.new`: add `quote_id_from` to the function-local import from `app.activitypub.util` (~2672), and the constructor argument `quote_ap_id=quote_id_from(request_json['object']),` beside `ap_id=...`.
  - `PostReply.new`: same local import pattern (or module-level if `PostReply.new` already imports from `app.activitypub.util` locally), and constructor argument `quote_ap_id=quote_id_from(request_json['object']) if request_json else None,`.
  - `update_post_from_activity` and `update_post_reply_from_activity`: first statement after the existing guards that read `request_json['object']`:

```python
    post.quote_ap_id = quote_id_from(request_json['object'])  # C2: an Update sets, changes or clears the quote
```
  (`reply.quote_ap_id = ...` in the reply function.)

- [ ] **Step 4: Run to verify pass**

Run: `./run_tests.sh tests/test_quote_posts.py tests/test_ap_create_reply.py tests/test_ap_peer_source_and_timestamps.py -q` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/models.py app/activitypub/util.py tests/test_quote_posts.py
git commit -m "feat: an inbound post or comment, and its Update, record what it quotes"
```

---

### Task 3: Strip the fallback; the quoted id is never the post URL

**Files:**
- Modify: `app/utils.py` — new `strip_quote_fallback` beside `microblog_content_to_link` (~1719); `microblog_content_to_link` gains `skip`
- Modify: `app/models.py` — `Post.new` HTML branches (~2743, ~2752) and microblog branch (~2754-2767)
- Modify: `app/activitypub/util.py` — `create_post_reply` (~3701), `update_post_from_activity` (~4461, ~4471, and its microblog block ~4481), `update_post_reply_from_activity` (~4292, ~4302)
- Modify: `docs/superpowers/specs/2026-10-12-ap-interop-c2-quote-posts-design.md` (ruling R1 amendment)
- Test: `tests/test_quote_posts.py`

**Interfaces:**
- Consumes: `quote_ap_id` set before content handling (Task 2 sets it in the constructor / first statement).
- Produces: `strip_quote_fallback(html: str, quote_id: str | None) -> str`; `microblog_content_to_link(html: str, exclude: str, skip: str | None = None)`.

- [ ] **Step 1: Write the failing tests**

```python
from app.utils import microblog_content_to_link, strip_quote_fallback


def test_quote_inline_element_is_removed():
    html = f'<p>my take</p><p class="quote-inline">RE: <a href="{QUOTED}">{QUOTED}</a></p>'
    assert strip_quote_fallback(html, QUOTED) == '<p>my take</p>'


def test_trailing_re_paragraph_is_removed():
    html = f'<p>my take</p><p>RE: <a href="{QUOTED}">other.test/notes/9</a></p>'
    assert strip_quote_fallback(html, QUOTED) == '<p>my take</p>'


def test_misskey_fallback_keeps_the_text_before_it():
    html = f'<p><span>my take<br><br>RE: <a href="{QUOTED}">{QUOTED}</a></span></p>'
    assert strip_quote_fallback(html, QUOTED) == '<p><span>my take</span></p>'


def test_a_different_link_is_kept():
    html = '<p>my take</p><p>RE: <a href="https://elsewhere.test/1">x</a></p>'
    assert strip_quote_fallback(html, QUOTED) == html


def test_text_beside_the_link_keeps_the_paragraph():
    html = f'<p>my take</p><p>RE: <a href="{QUOTED}">this</a> is wrong</p>'
    assert strip_quote_fallback(html, QUOTED) == html


def test_not_the_last_paragraph_is_kept():
    html = f'<p>RE: <a href="{QUOTED}">x</a></p><p>my take</p>'
    assert strip_quote_fallback(html, QUOTED) == html


def test_words_before_re_in_the_same_text_node_are_kept():
    html = f'<p>see RE: <a href="{QUOTED}">x</a></p>'
    assert strip_quote_fallback(html, QUOTED) == html


@pytest.mark.parametrize('quote_id', [None, ''])
def test_no_quote_changes_nothing(quote_id):
    html = f'<p class="quote-inline">RE: <a href="{QUOTED}">x</a></p>'
    assert strip_quote_fallback(html, quote_id) == html


def test_empty_html_is_returned():
    assert strip_quote_fallback('', QUOTED) == ''


def test_link_scan_skips_the_quoted_id():
    html = f'<p><a href="{QUOTED}">q</a> and <a href="https://news.test/a">n</a></p>'
    assert microblog_content_to_link(html, exclude='remote.test', skip=QUOTED) == 'https://news.test/a'


def test_link_scan_with_only_the_quoted_id_is_none():
    assert microblog_content_to_link(f'<p><a href="{QUOTED}">q</a></p>', exclude='remote.test', skip=QUOTED) is None


def test_ingested_microblog_post_has_no_fallback_and_no_quote_url(world):
    community, author, _ = world
    content = f'<p>my take on this</p><p class="quote-inline">RE: <a href="{QUOTED}">{QUOTED}</a></p>'
    post = Post.new(author, community, _create(_note(7, quote=QUOTED, content=content, mediaType='text/html')))
    assert 'RE:' not in post.body_html and 'RE:' not in post.body
    assert post.url != QUOTED


def test_author_link_to_the_quoted_post_is_not_the_url(world):
    community, author, _ = world
    content = f'<p>my take on <a href="{QUOTED}">this</a></p>'
    post = Post.new(author, community, _create(_note(8, quote=QUOTED, content=content)))
    assert post.url != QUOTED


def test_markdown_source_is_untouched(world):
    community, author, _ = world
    md = f'my take\n\nRE: {QUOTED}'
    post = Post.new(author, community, _create(_note(9, quote=QUOTED, content='<p>x</p>',
                                                     source={'content': md, 'mediaType': 'text/markdown'})))
    assert post.body == md


def test_reply_fallback_is_stripped(world, redis_lock_only_double):
    community, author, parent = world
    content = f'<p>agreed</p><p>RE: <a href="{QUOTED}">x</a></p>'
    reply = create_post_reply(store_ap_json=False, community=community, in_reply_to=parent.ap_id,
                              request_json=_create(_note(10, inReplyTo=parent.ap_id, quote=QUOTED, content=content)),
                              user=author)
    assert 'RE:' not in reply.body_html


def test_post_and_reply_updates_strip_the_fallback(world, redis_lock_only_double):
    community, author, parent = world
    content = f'<p>edited take</p><p>RE: <a href="{QUOTED}">x</a></p>'
    post = Post.new(author, community, _create(_note(11)))
    update_post_from_activity(post, {'id': 'u', 'type': 'Update', 'object': _note(11, quote=QUOTED, content=content)})
    assert 'RE:' not in post.body_html
    reply = create_post_reply(store_ap_json=False, community=community, in_reply_to=parent.ap_id,
                              request_json=_create(_note(12, inReplyTo=parent.ap_id)), user=author)
    update_post_reply_from_activity(reply, {'id': 'u', 'type': 'Update',
                                            'object': _note(12, inReplyTo=parent.ap_id, quote=QUOTED, content=content)})
    assert 'RE:' not in reply.body_html
```

Each HTML branch (Post.new `text/html` and plain; both Update functions' `text/html` and plain) needs a test hit for branch coverage: add `mediaType='text/html'` variants of the Update and reply tests where the plain one above does not reach the `text/html` branch.

- [ ] **Step 2: Run to verify failure**

Run: `./run_tests.sh tests/test_quote_posts.py -v` — Expected: FAIL, `cannot import name 'strip_quote_fallback'`.

- [ ] **Step 3: Implement `strip_quote_fallback`** in `app/utils.py` (BeautifulSoup and NavigableString are already used in this module; import `NavigableString` from `bs4` if absent):

```python
def strip_quote_fallback(html: str, quote_id: str | None) -> str:
    """`html` without the "RE: <link>" a quote's sender adds for software that cannot show quotes (C2): Mastodon's
    `quote-inline` element, and a final `RE: <a href=quote_id>` -- its own paragraph, or the tail of the last one after
    line breaks (Misskey). Text the author wrote is never removed."""
    if not quote_id or not html:
        return html
    soup = BeautifulSoup(html, 'html.parser')
    changed = False
    for element in soup.select('.quote-inline'):
        element.decompose()
        changed = True
    paragraphs = soup.find_all('p', recursive=False)
    if paragraphs:
        last = paragraphs[-1]
        links = last.find_all('a')
        link = links[-1] if links else None
        before = link.previous_sibling if link is not None else None
        if (link is not None and link.get('href') == quote_id
                and last.get_text().rstrip().endswith(link.get_text().strip())
                and isinstance(before, NavigableString) and before.strip() == 'RE:'):
            previous = before.previous_sibling
            link.decompose()
            before.extract()
            while previous is not None and (getattr(previous, 'name', None) == 'br' or
                                            (isinstance(previous, NavigableString) and not previous.strip())):
                earlier = previous.previous_sibling
                previous.extract()
                previous = earlier
            if not last.get_text().strip() and last.find('img') is None:
                last.decompose()
            changed = True
    return str(soup) if changed else html
```

- [ ] **Step 4: `microblog_content_to_link` gains `skip`**

```python
def microblog_content_to_link(html: str, exclude: str, skip: str | None = None):
    soup = BeautifulSoup(html, "html.parser")

    for link in soup.find_all("a"):
        if link.get("href") != skip and furl(link.get("href")).host != exclude:  # C2: skip is the quoted id
            return link.get("href")
    return None
```

- [ ] **Step 5: Apply at every HTML site** — wrap each `allowlist_html(...)` result named in Files with `strip_quote_fallback(..., <obj>.quote_ap_id)`, before `body` is derived from it. Example, `Post.new` ~2743:

```python
                post.body_html = strip_quote_fallback(allowlist_html(request_json['object']['content']), post.quote_ap_id)
```

`create_post_reply` has no row yet: compute `quote_id = quote_id_from(request_json['object'])` above `body_html = allowlist_html(content)` (~3701) and use `body_html = strip_quote_fallback(allowlist_html(content), quote_id)`. In the two Update functions use `post.quote_ap_id` / `reply.quote_ap_id` (set by Task 2's first statement). Add `strip_quote_fallback` to the relevant `from app.utils import` lists.

- [ ] **Step 6: Microblog URL** — in `Post.new` (~2764):

```python
                if link != '' and link != post.quote_ap_id:  # C2: the quoted post is shown as a card, not as the url
                    post.url = link
                else:
                    if link_from_body := microblog_content_to_link(post.body_html, exclude=furl(request_json['object']['id']).host,
                                                                   skip=post.quote_ap_id):
                        post.url = link_from_body
```

If `update_post_from_activity`'s microblog block (~4481) assigns `post.url` from a link, apply the same two guards there and add a test for it; if it only resets the title, leave it.

- [ ] **Step 7: Amend the spec** — in the spec's "Fallback stripping" section replace the second bullet with ruling R1's wording (see "Plan rulings" above).

- [ ] **Step 8: Run**

Run: `./run_tests.sh tests/test_quote_posts.py tests/test_allowlist_html.py tests/test_ap_create_reply.py -q` — Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add app/utils.py app/models.py app/activitypub/util.py tests/test_quote_posts.py docs/superpowers/specs/2026-10-12-ap-interop-c2-quote-posts-design.md
git commit -m "feat: a quote's RE: fallback is stripped and the quoted id is never the post url"
```

---

### Task 4: `app/quotes.py` — resolution, priming, card-or-line

**Files:**
- Create: `app/quotes.py`
- Modify: `app/request_hooks.py` (register global `quote_state` near line 82; connect the signal in `register_request_hooks`)
- Test: `tests/test_quote_cards.py` (new)

**Interfaces:**
- Consumes: `quote_ap_id` columns; `can_view`, `is_open` (app/visibility.py); `blocked_users`, `blocked_communities`, `communities_banned_from`, `blocked_or_banned_instances` (app/utils.py, each `(user_id: int) -> list[int]`, `0` = anonymous); `QuoteAuthorization(post_id, post_reply_id, quoting_uri)`; `BannedInstances(domain)`; `Community.is_gone()`.
- Produces:
  - `resolve_quotes(ids: set[str]) -> dict[str, Post | PostReply]`
  - `prime_quotes(sender, template, context, **extra) -> None` (a `before_render_template` receiver)
  - `quote_target(item) -> Post | PostReply | None`
  - `quote_state(item, user=None) -> dict | None` — None when `item.quote_ap_id` is empty; else `{'kind': 'card' | 'line', 'url': str, 'target': q | None, 'href': str | None, 'author': User | None, 'community': Community | None, 'text': str | None, 'posted_at': datetime | None}`.

- [ ] **Step 1: Write the failing tests** (`tests/test_quote_cards.py`)

```python
"""C2 quote cards: resolution and the card-or-line decision."""
import pytest
from flask import g, render_template_string
from sqlalchemy import event

from app import db
from app.constants import VISIBILITY_FOLLOWERS
from app.models import BannedInstances, QuoteAuthorization
from app.quotes import quote_state, quote_target, resolve_quotes
from tests.factories import (make_community, make_community_member, make_instance, make_post, make_post_reply,
                             make_user, make_user_block)

PEER = 'remote.test'


@pytest.fixture
def world(app, db_session):
    local = make_instance(app.config['SERVER_NAME'], software='piefed')
    remote = make_instance(PEER)
    community = make_community('cards')
    quoted_author = make_user(remote, 'quoted')
    quoter = make_user(remote, 'quoter')
    viewer = make_user(local, 'viewer', local=True)
    for u in (quoted_author, quoter):
        make_community_member(u, community)
    target = make_post(community, quoted_author, ap_id=f'https://{PEER}/post/9')
    target.body = 'the quoted words'
    quoting = make_post(community, quoter, ap_id=f'https://{PEER}/post/10')
    quoting.quote_ap_id = target.ap_id
    db.session.commit()
    return locals()


def test_no_quote_is_none(world):
    assert quote_state(world['target'], world['viewer']) is None


def test_known_remote_target_is_a_card(world):
    state = quote_state(world['quoting'], world['viewer'])
    assert state['kind'] == 'card' and state['target'] is world['target'] and state['href'] == f"/post/{world['target'].id}"


def test_unknown_target_is_a_line(world):
    world['quoting'].quote_ap_id = 'https://nowhere.test/1'
    state = quote_state(world['quoting'], world['viewer'])
    assert state == {'kind': 'line', 'url': 'https://nowhere.test/1', 'target': None, 'href': None,
                     'author': None, 'community': None, 'text': None, 'posted_at': None}


def test_quoted_comment_links_to_its_anchor(world):
    reply = make_post_reply(world['target'], world['quoted_author'], body='a comment')
    reply.ap_id = f'https://{PEER}/comment/3'
    world['quoting'].quote_ap_id = reply.ap_id
    db.session.commit()
    state = quote_state(world['quoting'], world['viewer'])
    assert state['href'] == f"/post/{world['target'].id}#comment_{reply.id}"


def test_local_post_slug_form_resolves_by_id(world, app):
    world['quoting'].quote_ap_id = f"{app.config['SERVER_URL']}/post/{world['target'].id}"
    assert quote_target(world['quoting']) is world['target']


def test_deleted_target_is_a_line(world):
    world['target'].deleted = True
    assert quote_state(world['quoting'], world['viewer'])['kind'] == 'line'


def test_followers_only_quoted_post_is_a_line_for_a_stranger(world):
    world['target'].visibility = VISIBILITY_FOLLOWERS
    assert quote_state(world['quoting'], world['viewer'])['kind'] == 'line'


def test_blocked_author_is_a_line(world):
    make_user_block(world['viewer'], world['quoted_author'])
    assert quote_state(world['quoting'], world['viewer'])['kind'] == 'line'


def test_blocked_community_is_a_line(world):
    from tests.factories import make_community_block  # add to factories if absent: a CommunityBlock row
    make_community_block(world['viewer'], world['community'])
    assert quote_state(world['quoting'], world['viewer'])['kind'] == 'line'


def test_banned_from_community_is_a_line(world):
    from tests.factories import make_community_ban  # add to factories if absent: a CommunityBan row
    make_community_ban(world['viewer'], world['community'])
    assert quote_state(world['quoting'], world['viewer'])['kind'] == 'line'


def test_gone_community_is_a_line(world):
    from app.models import utcnow
    world['community'].ap_deleted_at = utcnow()
    assert quote_state(world['quoting'], world['viewer'])['kind'] == 'line'


def test_site_banned_instance_is_a_line(world):
    db.session.add(BannedInstances(domain=PEER))
    db.session.commit()
    assert quote_state(world['quoting'], world['viewer'])['kind'] == 'line'


@pytest.mark.parametrize('flag,setting,kind', [('nsfw', 0, 'card'), ('nsfw', 1, 'line'), ('nsfw', 2, 'line'),
                                               ('nsfl', 0, 'card'), ('nsfl', 1, 'line')])
def test_sensitive_target_follows_the_viewer_setting(world, flag, setting, kind):
    setattr(world['target'], flag, True)
    setattr(world['viewer'], 'hide_' + flag, setting)
    assert quote_state(world['quoting'], world['viewer'])['kind'] == kind


def test_sensitive_target_is_a_line_for_anonymous(world, app):
    from flask_login import AnonymousUserMixin
    world['target'].nsfw = True
    assert quote_state(world['quoting'], AnonymousUserMixin())['kind'] == 'line'


def test_local_open_target_is_a_card(world):
    world['quoted_author'].instance_id = world['local'].id
    world['quoted_author'].ap_id = None
    world['target'].ap_id = None
    world['quoting'].quote_ap_id = f"https://{world['local'].domain}/post/{world['target'].id}"
    db.session.commit()
    assert quote_state(world['quoting'], world['viewer'])['kind'] == 'card'


def test_local_followers_only_target_needs_an_authorization(world):
    world['quoted_author'].instance_id = world['local'].id
    world['quoted_author'].ap_id = None
    world['target'].ap_id = None
    world['target'].visibility = VISIBILITY_FOLLOWERS
    world['quoting'].quote_ap_id = f"https://{world['local'].domain}/post/{world['target'].id}"
    world['viewer'].id  # the viewer follows the author, so can_view passes; only authorization decides:
    from tests.factories import make_user_follower  # use the factory that records an accepted follow
    make_user_follower(world['viewer'], world['quoted_author'])
    db.session.commit()
    assert quote_state(world['quoting'], world['viewer'])['kind'] == 'line'
    db.session.add(QuoteAuthorization(post_id=world['target'].id, quoting_uri=world['quoting'].ap_id))
    db.session.commit()
    g.pop('quote_targets', None)
    assert quote_state(world['quoting'], world['viewer'])['kind'] == 'card'


def test_resolve_quotes_with_no_ids_queries_nothing(world):
    assert resolve_quotes(set()) == {}


def test_priming_resolves_a_page_in_one_query_per_table(world, app):
    posts = []
    for n in range(20):
        p = make_post(world['community'], world['quoter'], ap_id=f'https://{PEER}/post/q{n}')
        p.quote_ap_id = f'https://{PEER}/post/target{n}'
        posts.append(p)
    db.session.commit()
    statements = []
    listener = lambda *args: statements.append(args[2])
    event.listen(db.engine, 'before_cursor_execute', listener)
    try:
        with app.test_request_context('/'):
            render_template_string('{% for p in posts %}{{ quote_state(p).kind }}{% endfor %}', posts=posts)
    finally:
        event.remove(db.engine, 'before_cursor_execute', listener)
    lookups = [s for s in statements if '.ap_id IN' in s]  # resolve_quotes' batch selects, one per table
    assert len(lookups) <= 2
    assert not [s for s in statements if '.ap_id = ' in s]  # and no per-item lookups
```

Factory names marked "add to factories if absent" must be checked first with `grep -n "def make_community_block\|def make_community_ban\|def make_user_follower" tests/factories.py`; use the existing helper under its real name, or add a minimal one beside its neighbours. Keep each assertion as written.

- [ ] **Step 2: Run to verify failure**

Run: `./run_tests.sh tests/test_quote_cards.py -v` — Expected: FAIL, `No module named 'app.quotes'`.

- [ ] **Step 3: Write `app/quotes.py`**

```python
"""Inbound quote posts (C2, docs/superpowers/specs/2026-10-12-ap-interop-c2-quote-posts-design.md): the local row a
post or comment quotes, and whether the viewer sees it as a card or as a plain link.

Lookups are cached per request in `g.quote_targets`. `prime_quotes`, a before_render_template receiver, fills the
cache for every quoting row in a template's context with one query per table, so a feed of quoting posts does not
look each one up."""
import re

from flask import current_app, g
from flask_login import current_user

from app import db
from app.models import BannedInstances, Community, Instance, Post, PostReply, QuoteAuthorization
from app.utils import blocked_communities, blocked_or_banned_instances, blocked_users, communities_banned_from, \
    shorten_string
from app.visibility import can_view, is_open

_LOCAL_PATH = re.compile(r'/(post|comment)/(\d+)$')


def resolve_quotes(ids: set[str]) -> dict:
    """Quoted id -> local Post or PostReply, for those of `ids` this instance has (by ap_id, or a local
    /post/<id> or /comment/<id> URL)."""
    if not ids:
        return {}
    found = {p.ap_id: p for p in db.session.query(Post).filter(Post.ap_id.in_(ids))}
    rest = ids - found.keys()
    if rest:
        found.update({r.ap_id: r for r in db.session.query(PostReply).filter(PostReply.ap_id.in_(rest))})
    prefix = current_app.config['SERVER_URL']
    for quoted_id in ids - found.keys():
        match = _LOCAL_PATH.match(quoted_id[len(prefix):]) if quoted_id.startswith(prefix) else None
        if match:
            row = db.session.get(Post if match.group(1) == 'post' else PostReply, int(match.group(2)))
            if row is not None:
                found[quoted_id] = row
    return found


def _cache() -> dict:
    if 'quote_targets' not in g:
        g.quote_targets = {}
    return g.quote_targets


def _quoting_rows(context: dict) -> list:
    rows, stack, seen = [], list(context.values()), set()
    while stack:
        value = stack.pop()
        if isinstance(value, (Post, PostReply)):
            if value.quote_ap_id:
                rows.append(value)
        elif isinstance(value, (list, tuple, dict)) or isinstance(getattr(value, 'items', None), list):
            if id(value) in seen:
                continue
            seen.add(id(value))
            if isinstance(value, dict):
                if 'comment' in value:  # a comment-tree node: {'comment': row, 'replies': [...]}
                    stack.append(value['comment'])
                    stack.extend(value.get('replies') or [])
            elif isinstance(value, (list, tuple)):
                stack.extend(value)
            else:  # a Pagination: its rows are already loaded
                stack.extend(value.items)
    return rows


def prime_quotes(sender, template, context, **extra):
    cache = _cache()
    missing = {row.quote_ap_id for row in _quoting_rows(context)} - cache.keys()
    if missing:
        found = resolve_quotes(missing)
        cache.update({quoted_id: found.get(quoted_id) for quoted_id in missing})


def quote_target(item):
    cache = _cache()
    if item.quote_ap_id not in cache:
        cache[item.quote_ap_id] = resolve_quotes({item.quote_ap_id}).get(item.quote_ap_id)
    return cache[item.quote_ap_id]


def _shows_card(item, q, user) -> bool:
    viewer_id = user.get_id() if user is not None else None
    uid = int(viewer_id) if viewer_id else 0
    if q.deleted:
        return False
    community = db.session.get(Community, q.community_id)
    if community is None or community.is_gone() or community.id in blocked_communities(uid) \
            or community.id in communities_banned_from(uid):
        return False
    if not can_view(q, int(viewer_id) if viewer_id else None):
        return False
    if q.user_id in blocked_users(uid) or q.instance_id in blocked_or_banned_instances(uid):
        return False
    instance = db.session.get(Instance, q.instance_id) if q.instance_id else None
    if instance is not None and db.session.query(BannedInstances.id).filter_by(domain=instance.domain).first():
        return False
    post = q if isinstance(q, Post) else q.post
    for flag in ('nsfw', 'nsfl'):  # R4: only a signed-in viewer set to Show sees a sensitive card
        if getattr(post, flag) and (not uid or getattr(user, 'hide_' + flag) != 0):
            return False
    if q.author.is_local() and not is_open(q):  # spec ruling 2: our own object needs our approval
        column = {'post_id': q.id} if isinstance(q, Post) else {'post_reply_id': q.id}
        if db.session.query(QuoteAuthorization.id).filter_by(quoting_uri=item.ap_id, **column).first() is None:
            return False
    return True


def quote_state(item, user=None) -> dict | None:
    """How `item`'s quote shows to `user` (default: the request's viewer): None when it quotes nothing."""
    if not getattr(item, 'quote_ap_id', None):
        return None
    user = current_user if user is None else user
    q = quote_target(item)
    if q is None or not _shows_card(item, q, user):
        return {'kind': 'line', 'url': item.quote_ap_id, 'target': None, 'href': None, 'author': None,
                'community': None, 'text': None, 'posted_at': None}
    is_post = isinstance(q, Post)
    return {'kind': 'card', 'url': item.quote_ap_id, 'target': q,
            'href': f'/post/{q.id}' if is_post else f'/post/{q.post_id}#comment_{q.id}',
            'author': q.author, 'community': db.session.get(Community, q.community_id),
            'text': (q.title if is_post and q.title else shorten_string(q.body or '', 200)),
            'posted_at': q.posted_at}
```

Adjust the import list if a name lives elsewhere (`grep -n "^class BannedInstances\|^class Instance(" app/models.py`). Keep the function shapes.

- [ ] **Step 4: Register** — in `app/request_hooks.py`: `from app.quotes import prime_quotes, quote_state`; in `register_request_hooks(app)` next to line 82:

```python
    app.jinja_env.globals['quote_state'] = quote_state
    before_render_template.connect(prime_quotes, app)  # C2: one lookup per table per rendered page
```

with `from flask import before_render_template` (extend the existing `from flask import ...` line).

- [ ] **Step 5: Run**

Run: `./run_tests.sh tests/test_quote_cards.py tests/test_request_hooks.py -q` — Expected: PASS. Then cover any uncovered branch of `_quoting_rows` (tuple, Pagination, a repeated container, a dict without `comment`) with a direct test of `_quoting_rows`.

- [ ] **Step 6: Commit**

```bash
git add app/quotes.py app/request_hooks.py tests/test_quote_cards.py tests/factories.py
git commit -m "feat: a quoted object resolves once per page and shows as a card only to viewers allowed to see it"
```

---

### Task 5: Templates — the card macro and its include points

**Files:**
- Create: `app/templates/post/_quote_card.html`
- Modify: `app/templates/post/_post_full.html` (after the body at line ~277)
- Modify: `app/templates/post/_post_reply_teaser.html` (after the body at line ~84)
- Modify: `app/templates/post/post_teaser/_macros.html` (after each `first_paragraph(post.body_html)` block: ~249, ~274, ~304, ~331, ~360)
- Modify: `app/templates/themes/dillo/post/post_teaser/_macros.html` (~204, ~228, ~254, ~280, ~304)
- Modify: the default theme stylesheet that styles `.comment` blockquotes (find with `grep -rln "blockquote" app/static/*.scss app/static/*.css | head`) — add `.quote_card` rules
- Test: `tests/test_quote_cards.py`

**Interfaces:**
- Consumes: global `quote_state(item, user=None) -> dict | None` (Task 4); globals `localize_datetime`.
- Produces: macro `render_quote(item)` in `post/_quote_card.html`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_quote_cards.py`)

```python
TEMPLATE = "{% from 'post/_quote_card.html' import render_quote %}{{ render_quote(item) }}"


def test_card_renders_author_text_and_link(world, app):
    with app.test_request_context('/'):
        html = render_template_string(TEMPLATE, item=world['quoting'])
    assert 'class="quote_card"' in html and 'the quoted words' not in html  # a titled post shows its title
    assert world['target'].title in html and f"/post/{world['target'].id}" in html


def test_the_line_escapes_the_url(world, app):
    world['quoting'].quote_ap_id = 'https://nowhere.test/a?b=<x>&c="d"'
    with app.test_request_context('/'):
        html = render_template_string(TEMPLATE, item=world['quoting'])
    assert '<x>' not in html and '&lt;x&gt;' in html and 'rel="nofollow ugc"' in html


def test_nothing_renders_without_a_quote(world, app):
    with app.test_request_context('/'):
        assert render_template_string(TEMPLATE, item=world['target']).strip() == ''


def test_card_does_not_nest(world, app):
    world['target'].quote_ap_id = world['quoting'].ap_id  # the quoted post itself quotes something
    db.session.commit()
    with app.test_request_context('/'):
        html = render_template_string(TEMPLATE, item=world['quoting'])
    assert html.count('class="quote_card') == 1


def test_post_page_shows_the_card(world, client):
    response = client.get(f"/post/{world['quoting'].id}")
    assert response.status_code == 200 and b'quote_card' in response.data


def test_community_listing_shows_the_card(world, client):
    response = client.get(f"/c/{world['community'].name}")
    assert response.status_code == 200 and b'quote_card' in response.data
```

If the community or post page needs more fixture state to render (site row, a logged-in user), copy what an existing page test does (`grep -ln "client.get(f'/post/" tests | head -3`).

- [ ] **Step 2: Run to verify failure**

Run: `./run_tests.sh tests/test_quote_cards.py -v` — Expected: FAIL, `TemplateNotFound: post/_quote_card.html`.

- [ ] **Step 3: Write the macro** `app/templates/post/_quote_card.html`:

```jinja
{# C2: what an inbound post or comment quotes. A card for a quoted object the viewer may see, else a plain link. #}
{% macro render_quote(item) -%}
{% set quote = quote_state(item) -%}
{% if quote -%}
{% if quote.kind == 'card' -%}
<blockquote class="quote_card">
    <div class="quote_card_meta">
        <a href="/u/{{ quote.author.link() }}">{{ quote.author.display_name() }}</a>
        {%- if quote.community %} · <a href="/c/{{ quote.community.link() }}">{{ quote.community.display_name() }}</a>{% endif %}
        · <time datetime="{{ localize_datetime(quote.posted_at) }}">{{ localize_datetime(quote.posted_at, locale) }}</time>
    </div>
    <a class="quote_card_text" href="{{ quote.href }}">{{ quote.text }}</a>
</blockquote>
{%- else -%}
<p class="quote_card quote_line">{{ _('Quoting:') }} <a href="{{ quote.url }}" rel="nofollow ugc">{{ quote.url }}</a></p>
{%- endif %}
{%- endif %}
{%- endmacro %}
```

If `locale` is undefined in a macro context, pass it from the caller or drop the second argument as the reply teaser does; check `localize_datetime`'s signature in app/utils.py.

- [ ] **Step 4: Include** — at the top of each modified template (or macro file) add `{% from 'post/_quote_card.html' import render_quote %}`, then insert `{{ render_quote(post) }}` right after each post-body render named in Files, and `{{ render_quote(post_reply) }}` after the reply body in `_post_reply_teaser.html`. In the teaser macros put it inside the same `{% if %}` structure as the body only when that `if` is about the body existing; a quote post with an empty stripped body must still show its quote, so place the include after the `{% endif %}` that closes the body block.

- [ ] **Step 5: Style** — add to the stylesheet found above:

```scss
.quote_card {
  border-left: 3px solid var(--bs-border-color, #ccc);
  padding: 0.25rem 0.75rem;
  margin: 0.5rem 0;
  font-size: 0.9em;
  .quote_card_meta { opacity: 0.8; }
  .quote_card_text { display: block; text-decoration: none; }
}
```

If the project builds CSS from SCSS, run the build the repo documents (check `package.json` / `README` for the sass command) and commit the generated CSS alongside.

- [ ] **Step 6: Run**

Run: `./run_tests.sh tests/test_quote_cards.py -q` and `./run_tests.sh tests/ -k "template or teaser or post_page" -q` — Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/templates app/static tests/test_quote_cards.py
git commit -m "feat: posts and comments render what they quote as a card or a plain link"
```

---

### Task 6: Alpha API exposes `quote_ap_id`

**Files:**
- Modify: `app/api/alpha/schema.py` — `Post` (~421), `Comment` (~567)
- Modify: `app/api/alpha/views.py` — `post_view` variant 1 `v1.update` (~72), `reply_view` variant 1 `v1.update` (~788)
- Test: `tests/test_quote_posts.py`

**Interfaces:**
- Produces: `post_view(...)['post']['quote_ap_id']` and `reply_view(...)['comment']['quote_ap_id']`: str or None. Check the exact nesting key by reading the lines; assert on it.

- [ ] **Step 1: Write the failing tests**

```python
from app.api.alpha.views import post_view, reply_view
from tests.factories import make_post_reply


def test_post_view_exposes_the_quote(world):
    community, author, parent = world
    parent.quote_ap_id = QUOTED
    db.session.commit()
    view = post_view(parent, variant=1)
    assert view['quote_ap_id'] == QUOTED


def test_post_view_quote_is_none_by_default(world):
    _, _, parent = world
    assert post_view(parent, variant=1)['quote_ap_id'] is None


def test_reply_view_exposes_the_quote(world):
    _, author, parent = world
    reply = make_post_reply(parent, author)
    reply.quote_ap_id = QUOTED
    db.session.commit()
    assert reply_view(reply, variant=1)['quote_ap_id'] == QUOTED
```

Read `post_view`/`reply_view` variant 1 first: if they return the column dict directly the assertions stand; if nested, index into the nesting. If either needs an app/request context or `user_id`, pass what the existing API tests pass (`grep -n "post_view(" tests/*.py | head`).

- [ ] **Step 2: Run to verify failure** — `./run_tests.sh tests/test_quote_posts.py -k view -v` — Expected: FAIL, `KeyError: 'quote_ap_id'`.

- [ ] **Step 3: Implement** — add `'quote_ap_id': post.quote_ap_id,` to `post_view`'s variant-1 `v1.update({...})`, `'quote_ap_id': reply.quote_ap_id,` to `reply_view`'s; in schema.py add to `Post` and `Comment`:

```python
    quote_ap_id = fields.String(allow_none=True)  # C2: the id this post quotes, when it came in as a quote
```

- [ ] **Step 4: Run** — `./run_tests.sh tests/test_quote_posts.py tests/ -k "api and (post or comment)" -q` — Expected: PASS (schema validation tests included).

- [ ] **Step 5: Commit**

```bash
git add app/api/alpha/schema.py app/api/alpha/views.py tests/test_quote_posts.py
git commit -m "feat: the API shows the id a post or comment quotes"
```

---

### Task 7: Final gate

- [ ] **Step 1:** `./run_tests.sh tests/ -q --cov=app --cov-branch --cov-report=json` — full suite green, writes `coverage.json`.
- [ ] **Step 2:** `python tests/check_changed_line_coverage.py coverage.json b97c7a7b8 --branches` — prints "Every added line since b97c7a7b8 ... was executed"; then `python tests/check_coverage_floors.py coverage.json` — "All N module floors met." (read each script's usage header if its arguments differ).
- [ ] **Step 3:** fix any gap with a test, one commit per gap.
