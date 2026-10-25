# AP interop C3: multi-language content and a self-hosted LibreTranslate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An inbound post or comment that carries several languages in `contentMap`/`nameMap`/`summaryMap` keeps every variant and shows each viewer the one for their locale; the first domain runs its own LibreTranslate so the translate buttons work.

**Architecture:** One reader (`language_variants_from`) turns the maps into a default language code plus a dict of non-default variants, stored in a nullable JSONB column `language_variants` on `post` and `post_reply`; a second (`object_language`) picks the item's `Language` from that code, replacing the four "first key of `contentMap`" sites. A new module `app/variants.py` chooses the variant for the viewer (`?lang=` first, then the locale's primary subtag) and exposes it to templates as the Jinja global `shown(item)`. The LibreTranslate container is a quadlet unit in the git-ignored `deploy/` directory, installed by the owner.

**Tech Stack:** Flask, SQLAlchemy (PostgreSQL JSONB), Alembic, Jinja2, marshmallow (alpha API), podman quadlet, pytest via `./run_tests.sh`.

**Spec:** `docs/superpowers/specs/2026-10-12-ap-interop-c3-multi-language-design.md`

## Global Constraints

- Inbox path: no fetch, sleep or retry (D738/D775).
- `language_variants`: JSONB, nullable, no index, no backfill, on `post` and `post_reply`; null, never `{}`, when there is no non-default variant.
- Variant shape: `{"<code>": {"title": str | None, "body_html": str, "cw": str | None}}`; `<code>` is the lowercase BCP 47 primary subtag; the default code is never a key.
- Valid key: `^[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8})*$`; normalised to its lowercase primary subtag; first of two same-subtag keys wins.
- At most 10 variants; a variant whose `body_html` source is over 100 000 characters is dropped (ruling R2).
- Default code: the `contentMap` key whose value equals `content`; else the explicit `language` code; else the first valid key.
- Every variant's HTML goes through `allowlist_html` then `strip_quote_fallback(..., quote_ap_id)`.
- The C2 quote card, RSS, search, notifications and the outbound AP object keep using the default.
- TDD; 100% line and branch coverage of changed lines; one commit per defect; gates via `./run_tests.sh` (single file: `./run_tests.sh tests/test_x.py -v`).
- Never assign `app.config[...]` in a test; use `monkeypatch.setitem(app.config, ...)` (ratchet: `tests/test_parallel_workers.py::TestNothingLeaksConfigIntoLaterTests`).
- Commit attribution line: `Co-Authored-By: <your model> <noreply@anthropic.com>`.
- Migration on the single head `a7c3e9f1b5d2`.
- `deploy/` is git-ignored: files there are written but never committed; their test runs with `python3 -m pytest deploy/autodeploy -q`.

## Plan rulings (controller, for the owner's visibility)

- **R1 Reader signature.** The spec's `language_variants_from(obj, default_code)` cannot strip the C2 fallback without the quote id, and every caller would repeat the `language` lookup. The reader is `language_variants_from(obj: dict, quote_ap_id: str | None = None) -> tuple[str | None, dict | None]` and reads `language` itself through `language_from_ap`. Language choice moves into `object_language(obj, map_code)`, shared by all four sites.
- **R2 Body cap.** The spec ties a variant's cap to "the length the item's own body is allowed"; the default body has no cap today. Ruling: 100 000 characters of source HTML per variant. Cost if wrong: a longer variant is dropped and the default shows.
- **R3 Unknown map language.** A map code with no `Language` row keeps today's behaviour: `language_id` None (not the site language), because a wrong language is worse than none. The default code shown to templates is then `'und'`.
- **R4 Switcher placement.** The "Also in:" line shows on the post page and on comments, not in feed teasers (teasers show the chosen variant only). Its links are `?lang=<code>` on the current page (`#comment_<id>` for a comment). The `lang` parameter applies to every item on that page that has the variant.
- **R5 Celery units.** No Celery task calls the translator (`libretranslate_string` is used only by `app/post/routes.py`), so only `pyfedi-web.container` gets `Wants=`/`After=` on the new unit; the spec's "both celery units" is dropped.
- **R6 Reply translation sanitising.** `post_reply_translate` returns the translator's answer unsanitised, unlike the post route (D1381). Task 5 fixes it in its own commit because the route is touched anyway.

## Review Focus

1. A variant whose HTML holds `<script>` or `onerror=` reaches the page sanitised — Task 1 `test_variant_html_is_sanitised_and_quote_fallback_stripped`.
2. `?lang=<script>` or a code the item does not carry changes nothing and is never echoed — Task 3 `test_an_unknown_or_hostile_lang_is_ignored`.
3. A default content warning still hides a variant that has no warning of its own — Task 3 `test_the_default_warning_covers_a_variant_without_one`; Task 4 `test_post_page_variant_body_stays_inside_the_warning`.
4. A feed page of variant posts costs no extra query per post — Task 3 `test_language_names_is_one_query_per_request`.
5. A Mastodon object (`contentMap` with one key equal to `content`) ingests exactly as before: same language, no variants — Task 2 `test_mastodon_single_key_map_is_unchanged`.

---

### Task 1: `language_variants` column, `language_variants_from`, `object_language`

**Files:**
- Create: `migrations/versions/b8e2d4f6a1c3_language_variants.py`
- Create: `tests/test_migration_language_variants.py`
- Modify: `app/models.py` (import line 26; class `Post` after `quote_ap_id` ~line 2560; class `PostReply` after `quote_ap_id` ~line 4030)
- Modify: `app/activitypub/util.py` (new functions right after `content_warning_from`, ~line 211)
- Test: `tests/test_language_variants.py` (new)

**Interfaces:**
- Produces: `Post.language_variants`, `PostReply.language_variants` (dict | None);
  `language_variants_from(obj: dict, quote_ap_id: str | None = None) -> tuple[str | None, dict | None]`;
  `object_language(obj: dict, map_code: str | None) -> Language | None`;
  `primary_subtag(code) -> str | None`; constants `MAX_LANGUAGE_VARIANTS = 10`, `MAX_VARIANT_SOURCE = 100_000` — all in `app/activitypub/util.py`.

- [ ] **Step 1: Write the failing tests** `tests/test_language_variants.py`:

```python
"""C3: multi-language objects (docs/superpowers/specs/2026-10-12-ap-interop-c3-multi-language-design.md)."""
import pytest

from app import db
from app.activitypub.util import (MAX_LANGUAGE_VARIANTS, language_variants_from, object_language,
                                  primary_subtag)
from app.models import Language, Post, PostReply

QUOTED = 'https://other.test/notes/9'


def _obj(**fields):
    obj = {'id': 'https://remote.test/notes/1', 'type': 'Note', 'content': '<p>hello</p>'}
    obj.update(fields)
    return obj


@pytest.mark.parametrize('value,expected', [
    ('en', 'en'), ('pt-BR', 'pt'), ('ZH-Hant-TW', 'zh'), ('ast', 'ast'),
    ('e', None), ('engl', None), ('en_US', None), ('', None), (5, None), (None, None), ('<b>', None),
])
def test_primary_subtag(value, expected):
    assert primary_subtag(value) == expected


def test_no_map_is_nothing():
    assert language_variants_from(_obj()) == (None, None)


@pytest.mark.parametrize('bad', [None, 'en', [], {}, {'en': 5}, {5: 'x'}, {'not a tag': '<p>x</p>'}])
def test_an_unusable_map_is_nothing(bad):
    assert language_variants_from(_obj(contentMap=bad)) == (None, None)


def test_mastodon_single_key_map_has_no_variants():
    assert language_variants_from(_obj(contentMap={'de': '<p>hello</p>'})) == ('de', None)


def test_the_key_matching_content_is_the_default_whatever_its_position():
    code, variants = language_variants_from(_obj(contentMap={'fr': '<p>bonjour</p>', 'en': '<p>hello</p>'}))
    assert code == 'en' and set(variants) == {'fr'}
    assert variants['fr'] == {'title': None, 'body_html': '<p>bonjour</p>', 'cw': None}


def test_without_a_match_the_language_property_is_the_default():
    obj = _obj(content='<p>other</p>', language={'identifier': 'fr', 'name': 'French'},
               contentMap={'en': '<p>hello</p>', 'fr': '<p>bonjour</p>'})
    code, variants = language_variants_from(obj)
    assert code == 'fr' and set(variants) == {'en'}


def test_without_a_match_or_language_the_first_valid_key_is_the_default():
    code, variants = language_variants_from(_obj(content='<p>other</p>',
                                                 contentMap={'x y': '<p>?</p>', 'es': '<p>hola</p>', 'en': '<p>hi</p>'}))
    assert code == 'es' and set(variants) == {'en'}


def test_region_keys_collapse_and_the_first_wins():
    code, variants = language_variants_from(_obj(contentMap={'en': '<p>hello</p>', 'pt-BR': '<p>olá</p>',
                                                             'pt-PT': '<p>olá PT</p>'}))
    assert code == 'en' and variants['pt']['body_html'] == '<p>olá</p>'


def test_name_and_summary_maps_fill_title_and_warning():
    obj = _obj(contentMap={'en': '<p>hello</p>', 'de': '<p>hallo</p>'},
               nameMap={'en': 'Title', 'de': ' Titel &amp; mehr '}, summaryMap={'de': '<b>Achtung</b>'})
    _, variants = language_variants_from(obj)
    assert variants['de'] == {'title': 'Titel & mehr', 'body_html': '<p>hallo</p>', 'cw': 'Achtung'}


def test_a_page_summary_without_sensitive_is_no_warning():
    obj = _obj(type='Page', contentMap={'en': '<p>hello</p>', 'de': '<p>hallo</p>'}, summaryMap={'de': 'desc'})
    assert language_variants_from(obj)[1]['de']['cw'] is None


def test_variant_html_is_sanitised_and_quote_fallback_stripped():
    obj = _obj(contentMap={'en': '<p>hello</p>',
                           'de': f'<p>hallo<script>x()</script><img src=x onerror=alert(1)></p>'
                                 f'<p>RE: <a href="{QUOTED}">{QUOTED}</a></p>'})
    body = language_variants_from(obj, QUOTED)[1]['de']['body_html']
    assert '<script' not in body and 'onerror' not in body and QUOTED not in body and 'hallo' in body


def test_bare_text_is_wrapped_in_a_paragraph():
    assert language_variants_from(_obj(contentMap={'en': '<p>hello</p>', 'de': 'hallo'}))[1]['de']['body_html'] \
        == '<p>hallo</p>'


def test_an_empty_or_oversized_variant_is_dropped():
    obj = _obj(contentMap={'en': '<p>hello</p>', 'de': '<p> </p>', 'fr': '<p>' + 'a' * 100_000 + '</p>',
                           'es': '<p>hola</p>'})
    assert set(language_variants_from(obj)[1]) == {'es'}


def test_variants_are_capped():
    codes = ['de', 'fr', 'es', 'it', 'nl', 'pl', 'ja', 'zh', 'ko', 'ru', 'sv', 'fi']
    obj = _obj(contentMap={'en': '<p>hello</p>', **{c: f'<p>{c}</p>' for c in codes}})
    assert list(language_variants_from(obj)[1]) == codes[:MAX_LANGUAGE_VARIANTS]


@pytest.fixture
def languages(app, db_session):
    for code, name in (('en', 'English'), ('de', 'Deutsch')):
        if not Language.query.filter_by(code=code).first():
            db.session.add(Language(code=code, name=name))
    db.session.commit()


def test_object_language_uses_the_language_property_when_it_agrees(languages):
    lang = object_language(_obj(language={'identifier': 'eo', 'name': 'Esperanto'}), None)
    assert (lang.code, lang.name) == ('eo', 'Esperanto')


def test_object_language_prefers_the_map_code_over_a_disagreeing_property(languages):
    assert object_language(_obj(language={'identifier': 'en', 'name': 'English'}), 'de').code == 'de'


def test_object_language_for_an_unknown_map_code_is_none(languages):
    assert object_language(_obj(), 'xx') is None


def test_object_language_with_nothing_is_none(languages):
    assert object_language(_obj(), None) is None


def test_columns_exist(app, db_session):
    assert Post.language_variants.type.__class__.__name__ == 'JSONB'
    assert PostReply.language_variants.type.__class__.__name__ == 'JSONB'
```

`tests/test_migration_language_variants.py` — copy `tests/test_migration_quote_ap_id.py` with: `MIGRATION = ... 'b8e2d4f6a1c3_language_variants.py'`, module name `'language_variants_migration'`, column name `'language_variants'`, chain `('b8e2d4f6a1c3', 'a7c3e9f1b5d2')`, and the type test replaced by:

```python
@pytest.mark.parametrize('table', TABLES)
def test_a_nullable_jsonb_column(app, db_session, table):
    with db.engine.connect() as connection:
        found = column(connection, table)
    assert found is not None and found['nullable'] is True and found['type'].__class__.__name__ == 'JSONB'
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./run_tests.sh tests/test_language_variants.py tests/test_migration_language_variants.py -v`
Expected: FAIL — `ImportError: cannot import name 'MAX_LANGUAGE_VARIANTS'` and a missing migration file.

- [ ] **Step 3: Add the columns.** `app/models.py` line 26 becomes `from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID`. Right after `quote_ap_id` in both `Post` and `PostReply`:

```python
    language_variants = db.Column(JSONB)  # C3: {code: {title, body_html, cw}} for each non-default language a peer sent
```

- [ ] **Step 4: Write the migration** `migrations/versions/b8e2d4f6a1c3_language_variants.py`:

```python
"""post and post_reply language_variants (C3 multi-language content)

Revision ID: b8e2d4f6a1c3
Revises: a7c3e9f1b5d2
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = 'b8e2d4f6a1c3'
down_revision = 'a7c3e9f1b5d2'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('post', sa.Column('language_variants', postgresql.JSONB(), nullable=True))
    op.add_column('post_reply', sa.Column('language_variants', postgresql.JSONB(), nullable=True))


def downgrade():
    op.drop_column('post_reply', 'language_variants')
    op.drop_column('post', 'language_variants')
```

- [ ] **Step 5: Write the readers** in `app/activitypub/util.py`, right after `content_warning_from` (`html`, `re`, `allowlist_html`, `strip_quote_fallback`, `html_to_text`, `shorten_string`, `language_from_ap`, `find_language`, `find_language_or_create` are already importable there; add any missing import at the top of the module):

```python
LANGUAGE_TAG = re.compile(r'[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8})*')
MAX_LANGUAGE_VARIANTS = 10
MAX_VARIANT_SOURCE = 100_000  # characters of a variant's source HTML (C3 ruling R2)


def primary_subtag(code) -> str | None:
    """`pt-BR` -> `pt`: the lowercase primary subtag of a BCP 47 tag, or None for anything that is not one."""
    if not isinstance(code, str) or not LANGUAGE_TAG.fullmatch(code):
        return None
    return code.split('-')[0].lower()


def _language_map(obj: dict, key: str) -> dict:
    """A peer's `contentMap`/`nameMap`/`summaryMap` as {primary subtag: str}; unusable entries are skipped."""
    value = obj.get(key)
    result = {}
    if isinstance(value, dict):
        for tag, text in value.items():
            code = primary_subtag(tag)
            if code is not None and isinstance(text, str):
                result.setdefault(code, text)
    return result


def language_variants_from(obj: dict, quote_ap_id: str | None = None) -> tuple[str | None, dict | None]:
    """C3. The default language code of `obj` and its other languages as {code: {title, body_html, cw}}.

    The default is the `contentMap` key whose text is `content`, else the `language` property's code, else the
    first usable key; (None, None) without a usable `contentMap`. Variant HTML is sanitised and loses the C2 quote
    fallback exactly as `content` does."""
    contents = _language_map(obj, 'contentMap')
    if not contents:
        return None, None
    ap_language = language_from_ap(obj.get('language'))
    default = (next((code for code, text in contents.items() if text == obj.get('content')), None)
               or (primary_subtag(ap_language[0]) if ap_language else None)
               or next(iter(contents)))
    names, summaries = _language_map(obj, 'nameMap'), _language_map(obj, 'summaryMap')
    variants = {}
    for code, source in contents.items():
        if code == default or len(source) > MAX_VARIANT_SOURCE:
            continue
        if len(variants) == MAX_LANGUAGE_VARIANTS:
            break
        if not (source.startswith('<p>') or source.startswith('<blockquote>')):
            source = '<p>' + source + '</p>'
        body_html = strip_quote_fallback(allowlist_html(source), quote_ap_id)
        if not html_to_text(body_html).strip():
            continue
        title = html.unescape(names[code].strip()) if names.get(code, '').strip() else None
        cw = content_warning_from({**obj, 'summary': summaries[code]}) if code in summaries else None
        variants[code] = {'title': shorten_string(title, 255) if title else None, 'body_html': body_html, 'cw': cw}
    return default, variants or None


def object_language(obj: dict, map_code: str | None):
    """C3. The Language of an inbound object: its `language` property when no map names another default, else the
    map's default code if this instance knows it. None means "leave it to the caller" (the four sites differ)."""
    ap_language = language_from_ap(obj.get('language'))
    if ap_language is not None and map_code in (None, primary_subtag(ap_language[0])):
        return find_language_or_create(*ap_language)
    if map_code is not None:
        return find_language(map_code)
    return None
```

Note on `test_the_key_matching_content_is_the_default_whatever_its_position`: `allowlist_html('<p>bonjour</p>')` must return `'<p>bonjour</p>'` unchanged; if it adds attributes or whitespace, change the test's expected value to the actual `allowlist_html` output computed in the test (`allowlist_html('<p>bonjour</p>')`), not the implementation.

- [ ] **Step 6: Run the tests**

Run: `./run_tests.sh tests/test_language_variants.py tests/test_migration_language_variants.py -v` — Expected: PASS.
Run: `./run_tests.sh tests/ -k "migration or alembic" -q` — Expected: PASS (single head).

- [ ] **Step 7: Commit**

```bash
git add migrations/versions/b8e2d4f6a1c3_language_variants.py app/models.py app/activitypub/util.py \
        tests/test_language_variants.py tests/test_migration_language_variants.py
git commit -m "feat: read a peer's language maps into a default language and per-language variants"
```

---

### Task 2: Create and Update store variants and the right language

**Files:**
- Modify: `app/models.py` — `Post.new` language block (~line 3043–3057), `PostReply.new` constructor call (~line 4118)
- Modify: `app/activitypub/util.py` — `create_post_reply` language block (~line 3716–3731), `update_post_reply_from_activity` language block (~line 4306–4318), `update_post_from_activity` language block (~line 4525–4538)
- Test: `tests/test_language_variants_ingest.py` (new)

**Interfaces:**
- Consumes: `language_variants_from`, `object_language` (Task 1).
- Produces: `Post.language_variants` / `PostReply.language_variants` set on Create; replaced on Update.

- [ ] **Step 1: Write the failing tests** `tests/test_language_variants_ingest.py` (fixture and helpers copied from `tests/test_quote_posts.py`):

```python
"""C3: Create and Update store language variants and the default language."""
import contextlib

import pytest

from app import db
from app.activitypub.util import create_post_reply, update_post_from_activity, update_post_reply_from_activity
from app.models import Language, Post
from app.utils import site_language_id, utcnow
from tests.factories import make_community, make_community_member, make_instance, make_post, make_site, make_user

PEER = 'remote.test'
PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'
MAPS = {'content': '<p>hello there</p>',
        'contentMap': {'fr': '<p>bonjour</p>', 'en': '<p>hello there</p>'},
        'nameMap': {'fr': 'Bonjour'}}


class _RedisLockOnlyDouble:
    def lock(self, *args, **kwargs):
        return contextlib.nullcontext()


@pytest.fixture
def redis_lock_only_double(monkeypatch):
    monkeypatch.setattr('app.redis_client', _RedisLockOnlyDouble())


@pytest.fixture
def world(app, db_session):
    make_site()
    for code, name in (('en', 'English'), ('fr', 'Français')):
        if not Language.query.filter_by(code=code).first():
            db.session.add(Language(code=code, name=name))
    instance = make_instance(PEER)
    make_user(instance, 'community_owner')
    community = make_community(host=PEER)
    community.ap_fetched_at = utcnow()
    author = make_user(instance, 'polyglot')
    author.ap_profile_id = author.ap_public_url = f'https://{PEER}/u/polyglot'
    make_community_member(author, community)
    parent = make_post(community, author, ap_id=f'https://{PEER}/post/1')
    db.session.commit()
    return community, author, parent


def _create(obj):
    return {'id': obj['id'] + '/create', 'type': 'Create', 'to': [PUBLIC], 'object': obj}


def _note(n, **fields):
    obj = {'id': f'https://{PEER}/notes/{n}', 'type': 'Note', 'attributedTo': f'https://{PEER}/u/polyglot',
           'to': [PUBLIC], 'cc': [], 'content': '<p>hello there friends</p>', 'published': '2026-01-01T00:00:00Z'}
    obj.update(fields)
    return obj


def _new_reply(world, n, **fields):
    community, author, parent = world
    return create_post_reply(store_ap_json=False, community=community, in_reply_to=parent.ap_id,
                             request_json=_create(_note(n, inReplyTo=parent.ap_id, **fields)), user=author)


def test_post_new_stores_variants_and_the_matching_language(world):
    community, author, _ = world
    post = Post.new(author, community, _create(_note(2, **MAPS)))
    assert post.language.code == 'en'
    assert post.language_variants == {'fr': {'title': 'Bonjour', 'body_html': '<p>bonjour</p>', 'cw': None}}


def test_mastodon_single_key_map_is_unchanged(world):
    community, author, _ = world
    post = Post.new(author, community, _create(_note(3, contentMap={'fr': '<p>hello there friends</p>'})))
    assert post.language.code == 'fr' and post.language_variants is None


def test_post_new_unknown_map_language_has_no_language(world):
    community, author, _ = world
    post = Post.new(author, community, _create(_note(4, contentMap={'xx': '<p>hello there friends</p>'})))
    assert post.language_id is None


def test_post_new_without_maps_uses_the_site_language(world):
    community, author, _ = world
    assert Post.new(author, community, _create(_note(5))).language_id == site_language_id()


def test_reply_stores_variants_and_the_matching_language(world, redis_lock_only_double):
    reply = _new_reply(world, 6, **MAPS)
    assert reply.language.code == 'en' and set(reply.language_variants) == {'fr'}
    assert reply.language_variants['fr']['title'] is None  # a comment has no title


def test_reply_unknown_map_language_has_no_language(world, redis_lock_only_double):
    assert _new_reply(world, 7, contentMap={'xx': '<p>hello there friends</p>'}).language_id is None


def test_reply_with_a_new_language_property_gets_its_id(world, redis_lock_only_double):
    reply = _new_reply(world, 8, language={'identifier': 'eo', 'name': 'Esperanto'})
    assert reply.language_id is not None and reply.language_variants is None


def test_post_update_sets_changes_and_clears(world, redis_lock_only_double):
    community, author, _ = world
    post = Post.new(author, community, _create(_note(9)))
    db.session.add(post)
    db.session.commit()
    update_post_from_activity(post, {'id': 'u1', 'type': 'Update', 'object': _note(9, **MAPS)})
    assert set(post.language_variants) == {'fr'} and post.language.code == 'en'
    update_post_from_activity(post, {'id': 'u2', 'type': 'Update', 'object': _note(9)})
    assert post.language_variants is None


def test_reply_update_sets_and_clears(world, redis_lock_only_double):
    _, _, parent = world
    reply = _new_reply(world, 10)
    update_post_reply_from_activity(reply, {'id': 'u1', 'type': 'Update',
                                            'object': _note(10, inReplyTo=parent.ap_id, **MAPS)})
    assert set(reply.language_variants) == {'fr'} and reply.language.code == 'en'
    update_post_reply_from_activity(reply, {'id': 'u2', 'type': 'Update', 'object': _note(10, inReplyTo=parent.ap_id)})
    assert reply.language_variants is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./run_tests.sh tests/test_language_variants_ingest.py -v`
Expected: FAIL — `language_variants` is None / language is the first key (`fr`).

- [ ] **Step 3: `Post.new`** — replace the block from `ap_language = language_from_ap(...)` through `post.language_id = site_language_id()` (~3048–3057, keep the comments above it) with (add `language_variants_from, object_language` to the function's local `from app.activitypub.util import ...`):

```python
            map_code, post.language_variants = language_variants_from(request_json['object'], post.quote_ap_id)  # C3
            language = object_language(request_json['object'], map_code)
            if language is not None:
                post.language = language
            elif map_code is None:
                from app.utils import site_language_id  # cycle: app.utils imports from this module
                post.language_id = site_language_id()
```

- [ ] **Step 4: `create_post_reply`** — replace the block from `language_id = None` through the closing `else: language_id = site_language_id()` (~3717–3731) with (add `language_variants_from, object_language` to the module's imports if missing — they live in this module):

```python
        # Language: C3 takes contentMap's default (the key whose text is `content`) as well as Lemmy's 'language'
        language_id = None
        map_code = language_variants_from(request_json['object'])[0]
        language = object_language(request_json['object'], map_code)
        if language is not None:
            if language.id is None:
                # A newly created Language is only add()ed, and under autoflush=False its id
                # stays None until a flush, so the reply was created with no language (D260).
                db.session.flush()
            language_id = language.id
        elif map_code is None:
            language_id = site_language_id()
```

- [ ] **Step 5: `PostReply.new`** — add to the constructor call, right after `quote_ap_id=...` (and `language_variants_from` to the local `from app.activitypub.util import ...`):

```python
                          language_variants=language_variants_from(request_json['object'],
                                                                   quote_id_from(request_json['object']))[1]
                          if request_json else None,
```

- [ ] **Step 6: Both Update functions** — in `update_post_reply_from_activity` and `update_post_from_activity`, replace from `ap_language = language_from_ap(...)` through `new_language = find_language(next(iter(...)))` with:

```python
        map_code, reply.language_variants = language_variants_from(request_json['object'], reply.quote_ap_id)  # C3
        new_language = object_language(request_json['object'], map_code)
```

(`post.` instead of `reply.` in the post function). Keep `old_language_id = ...`, `new_language = None` removal is fine, and keep the following `if new_language and (...)` block unchanged.

- [ ] **Step 7: Run the tests**

Run: `./run_tests.sh tests/test_language_variants_ingest.py tests/test_quote_posts.py tests/test_ap_create_reply.py -q` — Expected: PASS.
Run: `./run_tests.sh tests/ -k "language or contentMap or update_post" -q` — Expected: PASS (existing language tests unchanged).

- [ ] **Step 8: Commit**

```bash
git add app/models.py app/activitypub/util.py tests/test_language_variants_ingest.py
git commit -m "feat: Create and Update store a peer's language variants and take the language of content"
```

---

### Task 3: `app/variants.py` — choose the variant for the viewer

**Files:**
- Create: `app/variants.py`
- Modify: `app/request_hooks.py` (import beside `from app.quotes import ...` line 25; global beside `quote_state` line 84)
- Test: `tests/test_variants.py` (new)

**Interfaces:**
- Consumes: `primary_subtag` (Task 1), `item.language_variants` (Task 2).
- Produces: `Shown` namedtuple `(code, title, body_html, cw, others)` where `others` is a list of `(code, name)`; `chosen_variant(variants: dict, default: str, requested: str | None, viewer: str | None) -> tuple[str, dict | None]`; `shown(item) -> Shown` (Jinja global `shown`); `language_names() -> dict[str, str]`.

- [ ] **Step 1: Write the failing tests** `tests/test_variants.py`:

```python
"""C3: which language variant a viewer sees."""
from unittest.mock import patch

import pytest
from flask import g
from sqlalchemy import event

from app import db
from app.models import Language
from app.variants import chosen_variant, language_names, shown
from tests.factories import make_community, make_instance, make_post, make_post_reply, make_user

FR = {'title': 'Bonjour', 'body_html': '<p>bonjour</p>', 'cw': None}
DE = {'title': None, 'body_html': '<p>hallo</p>', 'cw': 'Achtung'}


@pytest.mark.parametrize('requested,viewer,expected', [
    (None, 'fr', ('fr', FR)), (None, 'en', ('en', None)), (None, None, ('en', None)), (None, 'ja', ('en', None)),
    ('de', 'fr', ('de', DE)), ('en', 'fr', ('en', None)), ('xx', 'fr', ('fr', FR)), ('<b>', None, ('en', None)),
])
def test_chosen_variant(requested, viewer, expected):
    assert chosen_variant({'fr': FR, 'de': DE}, 'en', requested, viewer) == expected


@pytest.fixture
def item(app, db_session):
    for code, name in (('en', 'English'), ('fr', 'Français'), ('de', 'Deutsch')):
        if not Language.query.filter_by(code=code).first():
            db.session.add(Language(code=code, name=name))
    instance = make_instance('remote.test')
    user = make_user(instance, 'polyglot')
    community = make_community('variants')
    post = make_post(community, user)
    post.title, post.body_html, post.content_warning = 'Hello', '<p>hello</p>', None
    post.language = Language.query.filter_by(code='en').first()
    post.language_variants = {'fr': FR, 'de': DE}
    db.session.commit()
    g.pop('language_names', None)
    return post


def test_shown_without_variants_is_the_default(item, app):
    item.language_variants = None
    with app.test_request_context('/'):
        assert shown(item) == ('en', 'Hello', '<p>hello</p>', None, [])


def test_shown_follows_the_locale(item, app):
    with app.test_request_context('/'), patch('app.variants.get_locale', return_value='fr_CA'):
        v = shown(item)
    assert (v.code, v.title, v.body_html, v.cw) == ('fr', 'Bonjour', '<p>bonjour</p>', None)
    assert v.others == [('en', 'English'), ('de', 'Deutsch')]


def test_lang_parameter_wins_and_falls_back_to_the_default_title(item, app):
    with app.test_request_context('/?lang=de'), patch('app.variants.get_locale', return_value='fr'):
        v = shown(item)
    assert (v.code, v.title, v.body_html, v.cw) == ('de', 'Hello', '<p>hallo</p>', 'Achtung')


def test_an_unknown_or_hostile_lang_is_ignored(item, app):
    with app.test_request_context('/?lang=%3Cscript%3E'), patch('app.variants.get_locale', return_value='en'):
        v = shown(item)
    assert v.code == 'en' and all('<' not in code for code, _ in v.others)


def test_the_default_warning_covers_a_variant_without_one(item, app):
    item.content_warning = 'spoilers'
    with app.test_request_context('/?lang=fr'):
        assert shown(item).cw == 'spoilers'


def test_a_comment_has_no_title(item, app):
    reply = make_post_reply(item, item.author, body='hi')
    reply.body_html, reply.language_variants = '<p>hi</p>', {'fr': FR}
    reply.language = None
    with app.test_request_context('/?lang=fr'):
        v = shown(reply)
    assert v.title is None and v.body_html == '<p>bonjour</p>' and [c for c, _ in v.others] == ['und']


def test_an_unnamed_code_is_listed_by_its_code(item, app):
    item.language_variants = {'eo': FR}
    with app.test_request_context('/'), patch('app.variants.get_locale', return_value='en'):
        assert shown(item).others == [('eo', 'eo')]


def test_shown_outside_a_request_is_the_default(item):
    assert shown(item).code == 'en'


def test_language_names_is_one_query_per_request(item, app):
    with app.test_request_context('/'):
        statements = []
        listener = lambda *args: statements.append(args[2])
        event.listen(db.engine, 'before_cursor_execute', listener)
        try:
            for _ in range(5):
                assert language_names()['fr'] == 'Français'
        finally:
            event.remove(db.engine, 'before_cursor_execute', listener)
    assert len(statements) == 1


def test_language_names_outside_a_request_is_not_cached_in_g(item):
    assert language_names()['de'] == 'Deutsch'
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./run_tests.sh tests/test_variants.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.variants'`.

- [ ] **Step 3: Write `app/variants.py`**:

```python
"""C3: the language variant of a post or comment a viewer sees (docs/superpowers/specs/2026-10-12-ap-interop-c3-
multi-language-design.md). Variants are stored at ingest in `language_variants`; nothing here queries per item."""
from collections import namedtuple

from flask import g, has_request_context, request

from app import db, get_locale
from app.activitypub.util import primary_subtag
from app.models import Language

Shown = namedtuple('Shown', 'code title body_html cw others')


def chosen_variant(variants: dict, default: str, requested: str | None, viewer: str | None) -> tuple[str, dict | None]:
    """The code to show and its variant (None for the default): a valid `requested` code first, then the viewer's."""
    for code in (requested, viewer):
        if code == default:
            return default, None
        if code in variants:
            return code, variants[code]
    return default, None


def language_names() -> dict:
    """{code: name} for every Language, one query per request (cached on `g` only under a request context)."""
    if has_request_context() and 'language_names' in g:
        return g.language_names
    names = dict(db.session.query(Language.code, Language.name).all())
    if has_request_context():
        g.language_names = names
    return names


def shown(item) -> Shown:
    """What a template renders for `item`: the chosen variant's title, body and warning, each falling back to the
    default's, and the other languages a viewer can switch to."""
    title = getattr(item, 'title', None)
    default = item.language.code if item.language_id else 'und'
    variants = item.language_variants or {}
    if not variants:
        return Shown(default, title, item.body_html, item.content_warning, [])
    requested = request.args.get('lang') if has_request_context() else None
    viewer = primary_subtag((get_locale() or '').replace('_', '-')) if has_request_context() else None
    code, variant = chosen_variant(variants, default, requested, viewer)
    names = language_names()
    others = [(c, names.get(c, c)) for c in [default, *variants] if c != code]
    if variant is None:
        return Shown(default, title, item.body_html, item.content_warning, others)
    return Shown(code, variant.get('title') or title, variant['body_html'], variant.get('cw') or item.content_warning,
                 others)
```

If `from app import get_locale` is a circular import at startup, import it inside `shown` instead and patch `app.get_locale` in the tests; record which in the report.

- [ ] **Step 4: Register the global** in `app/request_hooks.py`: `from app.variants import shown` beside the `app.quotes` import, and `app.jinja_env.globals['shown'] = shown` beside `quote_state`.

- [ ] **Step 5: Run the tests**

Run: `./run_tests.sh tests/test_variants.py -v` — Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/variants.py app/request_hooks.py tests/test_variants.py
git commit -m "feat: choose the language variant of a post or comment for the viewer"
```

---

### Task 4: Templates show the chosen variant and the "Also in" line

**Files:**
- Create: `app/templates/post/_language_switch.html`
- Modify: `app/templates/post/_post_full.html` (lines 43–44 title, 85/278/293/295/425 warning and body)
- Modify: `app/templates/post/_post_reply_teaser.html` (lines 84–87)
- Modify: `app/templates/post/post_teaser/_macros.html` (`render_title` lines 15, 32; every `post.content_warning` / `first_paragraph(post.body_html)` site)
- Modify: `app/templates/themes/dillo/post/post_teaser/_macros.html` (the same sites)
- Test: `tests/test_language_variant_templates.py` (new)

**Interfaces:**
- Consumes: Jinja global `shown(item)` (Task 3).
- Produces: macro `render_language_switch(v, anchor='')` in `post/_language_switch.html`.

- [ ] **Step 1: Write the failing tests** `tests/test_language_variant_templates.py`:

```python
"""C3: templates render the viewer's language variant (post page, comments, teasers in both themes)."""
import pytest
from bs4 import BeautifulSoup
from flask import g, render_template_string

from app import cache, db
from app.models import Language
from tests.factories import (make_community, make_community_member, make_instance, make_post, make_post_reply,
                             make_user)

PEER = 'remote.test'
FR_POST = {'title': 'Bonjour', 'body_html': '<p>bonjour</p>', 'cw': None}
FR_REPLY = {'title': None, 'body_html': '<p>salut</p>', 'cw': None}


@pytest.fixture
def page_world(app, db_session):
    local = make_instance(app.config['SERVER_NAME'], software='piefed')
    cache.clear()
    for code, name in (('en', 'English'), ('fr', 'Français')):
        if not Language.query.filter_by(code=code).first():
            db.session.add(Language(code=code, name=name))
    english = Language.query.filter_by(code='en').first()
    author = make_user(make_instance(PEER), 'polyglot')
    viewer = make_user(local, 'viewer', local=True)
    community = make_community('variants')  # after the users: it is owned by user 1
    make_community_member(author, community)
    post = make_post(community, author, ap_id=f'https://{PEER}/post/1')
    post.title, post.body, post.body_html = 'Hello', 'hello', '<p>hello</p>'
    post.language, post.language_variants = english, {'fr': FR_POST}
    reply = make_post_reply(post, author, body='hi')
    reply.body_html, reply.language, reply.language_variants = '<p>hi</p>', english, {'fr': FR_REPLY}
    db.session.commit()
    g.pop('language_names', None)
    return locals()


def logged_in_client(app, user):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True
    return client


def page(app, world, path):
    return BeautifulSoup(logged_in_client(app, world['viewer']).get(path).get_data(as_text=True), 'html.parser')


def test_post_page_shows_the_requested_variant_and_the_switch(page_world, app):
    soup = page(app, page_world, f"/post/{page_world['post'].id}?lang=fr")
    title = soup.select_one('h1.post_title')
    assert title.get_text(strip=True) == 'Bonjour' and title['lang'] == 'fr'
    assert 'bonjour' in soup.select_one('.post_body').get_text()
    switch = soup.select_one('p.language_switch')
    assert [a['href'] for a in switch.select('a')] == ['?lang=en'] and 'English' in switch.get_text()


def test_post_page_default_has_a_switch_to_the_variant(page_world, app):
    soup = page(app, page_world, f"/post/{page_world['post'].id}")
    assert soup.select_one('h1.post_title').get_text(strip=True) == 'Hello'
    assert soup.select_one('p.language_switch a')['href'] == '?lang=fr'


def test_no_switch_without_variants(page_world, app):
    page_world['post'].language_variants = page_world['reply'].language_variants = None
    db.session.commit()
    assert page(app, page_world, f"/post/{page_world['post'].id}").select('p.language_switch') == []


def test_post_page_variant_body_stays_inside_the_warning(page_world, app):
    page_world['post'].content_warning = 'spoilers'
    db.session.commit()
    details = page(app, page_world, f"/post/{page_world['post'].id}?lang=fr").select_one('details.content_warning')
    assert details.summary.get_text(strip=True) == 'spoilers' and 'bonjour' in details.get_text()


def test_comment_shows_its_variant_and_a_switch_with_its_anchor(page_world, app):
    reply = page_world['reply']
    soup = page(app, page_world, f"/post/{page_world['post'].id}?lang=fr")
    comment = soup.select_one(f'#comment_{reply.id}')
    assert 'salut' in comment.get_text()
    assert [a['href'] for a in comment.select('p.language_switch a')] == [f'?lang=en#comment_{reply.id}']


TEASER = ("{% from '{theme}post/post_teaser/_macros.html' import render_{kind} %}"
          "{{ render_{kind}(post, blur_content=False, current_user=current_user, locale='en', request=request, "
          "user_pronouns={}, reported_posts=[], admin_ids=[], user_flair={}, user_notes={}, "
          "moderated_community_ids=[], joined_communities=[], communities_banned_from_list=[]) }}")


@pytest.mark.parametrize('theme', ['', 'themes/dillo/'])
@pytest.mark.parametrize('kind', ['article', 'event', 'link', 'poll', 'image'])
def test_teaser_shows_the_variant_paragraph_without_a_switch(page_world, app, theme, kind):
    source = TEASER.replace('{theme}', theme).replace('{kind}', kind)
    with app.test_request_context('/?lang=fr'):
        html = render_template_string(source, post=page_world['post'])
    assert 'bonjour' in html and '<p>hello</p>' not in html and 'language_switch' not in html


@pytest.mark.parametrize('theme', ['', 'themes/dillo/'])
def test_teaser_title_is_the_variant_title(page_world, app, theme):
    source = (f"{{% from '{theme}post/post_teaser/_macros.html' import render_title %}}"
              "{{ render_title(post, current_user=current_user, request=request, locale='en') }}")
    with app.test_request_context('/?lang=fr'):
        soup = BeautifulSoup(render_template_string(source, post=page_world['post']), 'html.parser')
    heading = soup.select('h3')[-1]
    assert 'Bonjour' in heading.get_text() and heading['lang'] == 'fr'


@pytest.mark.parametrize('theme', ['', 'themes/dillo/'])
def test_teaser_warning_hides_the_variant_body(page_world, app, theme):
    page_world['post'].content_warning = 'spoilers'
    source = TEASER.replace('{theme}', theme).replace('{kind}', 'article')
    with app.test_request_context('/?lang=fr'):
        html = render_template_string(source, post=page_world['post'])
    assert 'spoilers' in html and 'bonjour' not in html
```

If `render_title` in the dillo file takes other parameters, or a macro needs one the `TEASER` call does not pass, read the macro signature and pass what it needs; do not change the macro signatures. If a teaser kind does not render the body paragraph at all for this post (e.g. the image kind with no image), assert only `'language_switch' not in html` for that kind and say so in the report.

- [ ] **Step 2: Run them to verify they fail**

Run: `./run_tests.sh tests/test_language_variant_templates.py -v`
Expected: FAIL — the title is `Hello`, no `p.language_switch`.

- [ ] **Step 3: The switch macro** `app/templates/post/_language_switch.html`:

```jinja
{# C3: the other languages a post or comment came in. `v` is shown(item); links switch the whole page (?lang=). #}
{% macro render_language_switch(v, anchor='') -%}
{% if v.others -%}
<p class="language_switch small text-muted">{{ _('Also in:') }}
{%- for code, name in v.others %} <a href="?lang={{ code }}{{ anchor }}" lang="{{ code }}">{{ name }}</a>{{ ' ·' if not loop.last }}{% endfor %}</p>
{%- endif %}
{%- endmacro %}
```

- [ ] **Step 4: `_post_full.html`.** Add `{% from 'post/_language_switch.html' import render_language_switch %}` at the top and `{% set v = shown(post) %}` before line 43. Then:
  - line 43: `lang="{{ v.code }}"`; line 44: `{{ v.title if v.title != '(content in post body)' else '' }}`; directly after the `</h1>` closing that title, `{{ render_language_switch(v) }}`.
  - lines 85, 293, 295, 425: `post.content_warning` → `v.cw` (both the test and the `<summary>` text).
  - line 278: `post.body_html` → `v.body_html` (both occurrences on the line).
  Leave image `alt` text, share links and `_schema_org.html` on `post.title` (Global Constraints: non-HTML outputs keep the default).

- [ ] **Step 5: `_post_reply_teaser.html`.** Add the import at the top; `{% set v = shown(post_reply) %}` before line 84; lines 84–87: `post_reply.content_warning` → `v.cw`, `post_reply.body_html` → `v.body_html`; after the closing `{% if v.cw -%}</details>{% endif -%}` add `{{ render_language_switch(v, '#comment_' ~ post_reply.id) }}`. Also set `lang="{{ v.code }}"` on the element that today carries `post_reply.language_code()` in this file, if any (`grep -n language_code app/templates/post/_post_reply_teaser.html`).

- [ ] **Step 6: Both teaser macro files.** In every macro of `post/post_teaser/_macros.html` and `themes/dillo/post/post_teaser/_macros.html` that reads `post.title` as displayed text, `post.language_code()` in a `lang=` attribute, `post.content_warning`, or `first_paragraph(post.body_html)`: add `{% set v = shown(post) %}` as the macro's first line and swap to `v.title`, `v.code`, `v.cw`, `first_paragraph(v.body_html)`. The `{% if not post.content_warning %}{{ render_quote(...) }}` guards become `{% if not v.cw %}`. Keep `{% if post.body_html -%}` guards on `post.body_html` (a variant exists only when the default body does). Leave `alt=` and `aria-label` uses of `post.title`. Verify:

Run: `grep -n "post.content_warning\|first_paragraph(post.body_html)\|lang=\"{{ post.language_code() }}\"" app/templates/post/post_teaser/_macros.html app/templates/themes/dillo/post/post_teaser/_macros.html`
Expected: no output.

- [ ] **Step 7: Run the tests**

Run: `./run_tests.sh tests/test_language_variant_templates.py tests/test_quote_cards.py tests/test_dillo_content_warning.py -q` — Expected: PASS.
Run: `./run_tests.sh tests/ -k "template or teaser or post_page or dillo" -q` — Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/templates/post/_language_switch.html app/templates/post/_post_full.html \
        app/templates/post/_post_reply_teaser.html app/templates/post/post_teaser/_macros.html \
        app/templates/themes/dillo/post/post_teaser/_macros.html tests/test_language_variant_templates.py
git commit -m "feat: posts and comments show the viewer's language variant with an Also in line"
```

---

### Task 5: Translate buttons translate the shown variant

**Files:**
- Modify: `app/post/routes.py` — `post_teaser_translate` (~1514), `post_translate` (~1541), `post_reply_translate` (~1580)
- Modify: `app/templates/post/_post_full.html:561`, `app/templates/post/_post_reply_teaser.html:169`, `app/templates/post/post_teaser/_macros.html:190`, `app/templates/themes/dillo/post/post_teaser/_macros.html:150` (append `?lang=` to each `hx-post`)
- Test: `tests/test_language_variant_translate.py` (new)

**Interfaces:**
- Consumes: `shown(item)` (Task 3) — called inside the route's request context, so it reads the `lang` query parameter.

- [ ] **Step 1: Write the failing tests** `tests/test_language_variant_translate.py`. Copy the `page_world` fixture and `logged_in_client` from `tests/test_language_variant_templates.py` (Task 4) under the name `world`, giving the reply the variant `{'fr': {'title': None, 'body_html': '<p>bonjour</p>', 'cw': None}}`, and add:

```python
from unittest.mock import patch

import pytest


@pytest.fixture
def client(app, world, monkeypatch):
    monkeypatch.setitem(app.config, 'TRANSLATE_ENDPOINT', 'http://lt.test')
    return logged_in_client(app, world['viewer'])


def test_post_translate_sends_the_chosen_variant_with_its_language(world, client):
    with patch('app.post.routes.libretranslate_string', side_effect=lambda text, source, target: f'[{source}]{text}') as lt:
        html = client.post(f"/post/{world['post'].id}/translate?lang=fr").get_data(as_text=True)
    assert lt.call_args_list[0].args[0] == '<p>bonjour</p>' or lt.call_args_list[0].kwargs.get('text') == '<p>bonjour</p>'
    assert all(call.kwargs['source'] == 'fr' for call in lt.call_args_list)
    assert 'Bonjour' in html


def test_teaser_translate_sends_the_variant_title(world, client):
    with patch('app.post.routes.libretranslate_string', return_value='Hi') as lt:
        client.post(f"/post_teaser/{world['post'].id}/translate?lang=fr")
    assert lt.call_args.args[0] == 'Bonjour'


def test_reply_translate_sends_the_variant(world, client):
    with patch('app.post.routes.libretranslate_string', return_value='<p>hi</p>') as lt:
        client.post(f"/post_reply/{world['reply'].id}/translate?lang=fr")
    assert lt.call_args.args[0] == '<p>bonjour</p>' and lt.call_args.kwargs['source'] == 'fr'


def test_without_lang_the_default_is_sent_as_before(world, client):
    with patch('app.post.routes.libretranslate_string', return_value='x') as lt:
        client.post(f"/post/{world['post'].id}/translate")
    assert lt.call_args_list[0].args[0] == world['post'].body_html


def test_the_button_carries_the_shown_language(world, client):
    html = client.get(f"/post/{world['post'].id}?lang=fr").get_data(as_text=True)
    assert f"/post/{world['post'].id}/translate?lang=fr" in html
```

Adjust the `args[0]` / `kwargs` reads to how the routes actually call `libretranslate_string` (positional text, keyword `source`/`target` today). The "source equals site language becomes auto" rule stays: with English site language, `?lang=en` sends `source='auto'`.

- [ ] **Step 2: Run them to verify they fail**

Run: `./run_tests.sh tests/test_language_variant_translate.py -v` — Expected: FAIL (the default body is sent).

- [ ] **Step 3: The routes.** In each of the three routes, inside `if current_app.config['TRANSLATE_ENDPOINT']:`, compute `v = shown(post)` (`shown(post_reply)` in the reply route; `from app.variants import shown` at the top of the module) and:

```python
        v = shown(post)
        source = v.code if v.code != 'und' else 'auto'
        if source == site_language_code():
            source = 'auto'
```

replacing the two existing `source = ...` lines, and pass `v.title` / `v.body_html` instead of `post.title` / `post.body_html` (`post_reply.body_html` in the reply route).

- [ ] **Step 4: The buttons.** Append the language to each `hx-post`, e.g. in `_post_full.html`:

```jinja
hx-post="{{ url_for('post.post_translate', post_id=post.id) }}{{ '?lang=' ~ v.code if v.others }}"
```

and the same suffix in `_post_reply_teaser.html` (its `v` from Task 4) and both teaser macro files (the macro that renders the translate icon needs `{% set v = shown(post) %}` if Task 4 did not add one there).

- [ ] **Step 5: Run the tests, commit**

Run: `./run_tests.sh tests/test_language_variant_translate.py tests/ -k "translate" -q` — Expected: PASS.

```bash
git add app/post/routes.py app/templates/post/_post_full.html app/templates/post/_post_reply_teaser.html \
        app/templates/post/post_teaser/_macros.html app/templates/themes/dillo/post/post_teaser/_macros.html \
        tests/test_language_variant_translate.py
git commit -m "feat: the translate buttons translate the language variant the viewer sees"
```

- [ ] **Step 6: Ruling R6, own commit.** Failing test first, in the same test file:

```python
def test_reply_translation_is_sanitised(world, client):
    with patch('app.post.routes.libretranslate_string', return_value='<p>ok</p><script>x()</script>'):
        html = client.post(f"/post_reply/{world['reply'].id}/translate").get_data(as_text=True)
    assert 'ok' in html and '<script' not in html
```

Run it (FAIL), then in `post_reply_translate` wrap the result: `... lang="{recipient_language}">' + allowlist_html(result) + "</div>"` (and `escape(recipient_language)` in the attribute). Run (PASS), then:

```bash
git add app/post/routes.py tests/test_language_variant_translate.py
git commit -m "fix: a comment's translation is sanitised before it is swapped into the page, as a post's is"
```

---

### Task 6: Alpha API exposes `language_variants`

**Files:**
- Modify: `app/api/alpha/views.py` (post view ~line 78–84; reply view ~line 796–808)
- Modify: `app/api/alpha/schema.py` (beside `quote_ap_id`, lines 452 and 586)
- Test: `tests/test_language_variants_api.py` (new)

- [ ] **Step 1: Write the failing tests** `tests/test_language_variants_api.py` (the `world` fixture copied from `tests/test_quote_posts.py`, lines 78–95, with its imports):

```python
"""C3: the alpha API carries language variants."""
from app import db
from app.api.alpha.schema import Comment, Post as PostSchema
from app.api.alpha.views import post_view, reply_view
from tests.factories import make_post_reply

FR = {'fr': {'title': 'Bonjour', 'body_html': '<p>bonjour</p>', 'cw': None}}


def test_post_view_has_language_variants(world):
    _, _, parent = world
    parent.language_variants = FR
    db.session.commit()
    assert post_view(parent, variant=1)['language_variants'] == FR


def test_post_view_without_variants_is_null(world):
    assert post_view(world[2], variant=1)['language_variants'] is None


def test_a_stub_of_a_hidden_post_does_not_carry_variants(world):
    _, _, parent = world
    parent.language_variants, parent.visibility = FR, 'followers'
    db.session.commit()
    assert 'language_variants' not in post_view(parent, variant=1, user_id=None)


def test_deleted_post_hides_its_variants(world):
    _, _, parent = world
    parent.language_variants, parent.deleted = FR, True
    db.session.commit()
    assert post_view(parent, variant=1)['language_variants'] is None


def test_reply_view_has_language_variants(world):
    _, author, parent = world
    reply = make_post_reply(parent, author)
    reply.language_variants = FR
    db.session.commit()
    assert reply_view(reply, variant=1)['language_variants'] == FR


def test_deleted_reply_hides_its_variants(world):
    _, author, parent = world
    reply = make_post_reply(parent, author)
    reply.language_variants, reply.deleted = FR, True
    db.session.commit()
    assert reply_view(reply, variant=1)['language_variants'] is None


def test_schema_declares_the_field():
    assert 'language_variants' in PostSchema().fields and 'language_variants' in Comment().fields
```

- [ ] **Step 2: Run them to verify they fail** — `./run_tests.sh tests/test_language_variants_api.py -v` — FAIL (`KeyError`).

- [ ] **Step 3: Implement.** In both views add `'language_variants': post.language_variants` (`reply.` in the reply view) to the dict beside `'quote_ap_id'`, and `v1['language_variants'] = None` on the two lines that blank `quote_ap_id` for deleted items. In the schema, beside each `quote_ap_id`:

```python
    language_variants = fields.Dict(allow_none=True)  # C3: {code: {title, body_html, cw}} for each other language
```

- [ ] **Step 4: Run, commit**

Run: `./run_tests.sh tests/test_language_variants_api.py tests/ -k "api and (post or comment or reply)" -q` — PASS.

```bash
git add app/api/alpha/views.py app/api/alpha/schema.py tests/test_language_variants_api.py
git commit -m "feat: the alpha API returns a post's and a comment's language variants"
```

---

### Task 7: LibreTranslate quadlet unit (local `deploy/`, not committed)

**Files (all git-ignored; write them, never `git add` them):**
- Create: `deploy/quadlet/pyfedi-libretranslate.container`
- Create: `deploy/quadlet/pyfedi-libretranslate-data.volume`
- Modify: `deploy/quadlet/pyfedi-web.container`
- Modify: `deploy/DEPLOY_GUIDE.md`
- Test: `deploy/autodeploy/tests/test_quadlet_libretranslate.py` (new)

- [ ] **Step 1: Write the failing test** `deploy/autodeploy/tests/test_quadlet_libretranslate.py`:

```python
"""C3: the LibreTranslate unit runs on the pyfedi network and the web unit does not depend on it hard."""
from pathlib import Path

QUADLET = Path(__file__).resolve().parent.parent.parent / 'quadlet'


def keys(name):
    """Every `Key=value` line of a unit as a list of (key, value); quadlet repeats keys, so no configparser."""
    pairs = []
    for line in (QUADLET / name).read_text().splitlines():
        if '=' in line and not line.lstrip().startswith(('#', '[')):
            key, value = line.split('=', 1)
            pairs.append((key.strip(), value.strip()))
    return pairs


def test_the_translator_unit():
    unit = dict(keys('pyfedi-libretranslate.container'))
    assert unit['Image'] == 'docker.io/libretranslate/libretranslate:v1.9.6'
    assert unit['ContainerName'] == 'pyfedi_libretranslate'
    assert unit['Network'] == 'pyfedi.network' and unit['NetworkAlias'] == 'libretranslate'
    assert unit['EnvironmentFile'] == '/home/rocky/pyfedi/.env.quadlet'
    assert unit['Volume'] == 'pyfedi-libretranslate-data.volume:/home/libretranslate/.local:z'
    assert 'PublishPort' not in unit and unit['Restart'] == 'always'
    assert 'healthcheck.py' in unit['HealthCmd']


def test_the_data_volume_exists():
    assert (QUADLET / 'pyfedi-libretranslate-data.volume').read_text().startswith('[Volume]')


def test_web_wants_but_does_not_require_the_translator():
    pairs = keys('pyfedi-web.container')
    assert ('Wants', 'pyfedi-libretranslate.service') in pairs
    assert any(k == 'After' and 'pyfedi-libretranslate.service' in v for k, v in pairs)
    assert not any(k == 'Requires' and 'libretranslate' in v for k, v in pairs)


def test_autodeploy_does_not_restart_the_translator():
    script = (QUADLET.parent / 'autodeploy' / 'pyfedi-autodeploy').read_text()
    assert 'libretranslate' not in script
```

- [ ] **Step 2: Run it to verify it fails** — `python3 -m pytest deploy/autodeploy/tests/test_quadlet_libretranslate.py -q` — FAIL (file not found).

- [ ] **Step 3: The units.** `deploy/quadlet/pyfedi-libretranslate.container`:

```ini
[Unit]
Description=PieFed LibreTranslate (translate buttons; TRANSLATE_ENDPOINT=http://libretranslate:5000)

[Container]
ContainerName=pyfedi_libretranslate
Image=docker.io/libretranslate/libretranslate:v1.9.6
# Reads LT_LOAD_ONLY (default list in DEPLOY_GUIDE.md section 9) from the shared env file.
EnvironmentFile=/home/rocky/pyfedi/.env.quadlet
# Downloaded models; without this every recreate downloads them again.
Volume=pyfedi-libretranslate-data.volume:/home/libretranslate/.local:z
Network=pyfedi.network
NetworkAlias=libretranslate
HealthCmd=./venv/bin/python scripts/healthcheck.py
HealthInterval=60s
HealthTimeout=10s
HealthRetries=3
# The first start downloads models before it serves.
HealthStartPeriod=600s

[Service]
Restart=always
TimeoutStartSec=900

[Install]
WantedBy=multi-user.target
```

`deploy/quadlet/pyfedi-libretranslate-data.volume`:

```ini
[Volume]
VolumeName=pyfedi-libretranslate-data
```

In `deploy/quadlet/pyfedi-web.container` `[Unit]`: add `Wants=pyfedi-libretranslate.service` and append ` pyfedi-libretranslate.service` to the `After=` line. Do not touch `Requires=`.

- [ ] **Step 4: `DEPLOY_GUIDE.md`** — add a section "9. LibreTranslate" (renumber if 9 exists) containing, verbatim in substance:
  - What it is for (the translate buttons) and that the site keeps running when it is down (`Wants=`, not `Requires=`).
  - `.env.docker` additions, then regenerate `.env.quadlet` as section 6 says:
    `LT_LOAD_ONLY=en,es,fr,de,pt,it,ja,nl,pl,zh`, `TRANSLATE_ENDPOINT=http://libretranslate:5000`, `TRANSLATE_KEY=`.
  - Install: `scp deploy/quadlet/pyfedi-libretranslate.container deploy/quadlet/pyfedi-libretranslate-data.volume deploy/quadlet/pyfedi-web.container rocky@<first-domain>:` then the same `sudo install -m 644 -o root -g root ... /etc/containers/systemd/ && sudo restorecon ... && sudo systemctl daemon-reload` pattern as the existing web-unit section, then `sudo systemctl start pyfedi-libretranslate` and `sudo systemctl restart pyfedi-web`.
  - First start downloads the models into the volume (minutes); check with `sudo podman logs -f pyfedi_libretranslate` and `curl -s http://127.0.0.1:...` is not available (no published port): use `sudo podman exec piefed_app1 curl -s http://libretranslate:5000/languages`.
  - Memory: roughly 1–2 GB with the default list; changing `LT_LOAD_ONLY` then `sudo systemctl restart pyfedi-libretranslate`.
  - The autodeploy timer does not restart it.

- [ ] **Step 5: Run the deploy suite** — `python3 -m pytest deploy/autodeploy -q` — Expected: PASS (new and existing tests).

- [ ] **Step 6: No commit.** `git status --short deploy/` must print nothing (the directory is ignored). Report the four file paths in the task report.

---

### Task 8: Final gate

- [ ] **Step 1:** `./run_tests.sh tests/ -q -p no:cacheprovider --cov=app --cov-branch --cov-report=json:coverage.json` — Expected: all pass.
- [ ] **Step 2:** `python3 tests/check_changed_line_coverage.py coverage.json fd2d24102 --branches` — Expected: every changed line and branch covered.
- [ ] **Step 3:** `python3 tests/check_coverage_floors.py coverage.json coverage_floors.ini` — Expected: all floors met.
- [ ] **Step 4:** `python3 -m pytest deploy/autodeploy -q` — Expected: PASS.
- [ ] **Step 5:** `flask db heads` (or the repo's single-head test) — Expected: one head, `b8e2d4f6a1c3`.
