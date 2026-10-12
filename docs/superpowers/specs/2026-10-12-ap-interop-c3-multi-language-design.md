# AP interop C3: multi-language content and a self-hosted LibreTranslate — design

Date: 2026-10-12. Track: tier C of `docs/superpowers/specs/2026-10-10-ap-gaps-roadmap.md`
("`contentMap`/`nameMap`/`summaryMap` with more than one language"), widened by the owner to a self-hosted
LibreTranslate container for hell.cloud.

## Goal

A post or comment that arrives with the same text in several languages shows each viewer the language they read,
and the existing translate buttons work on hell.cloud against a LibreTranslate the instance runs itself.

Today only the first key of `contentMap` is read, and only as the item's language; the body always comes from
`content`, so a map whose first key is not the language of `content` mislabels the item, and every other variant
is dropped. `nameMap` and `summaryMap` are ignored. hell.cloud has no `TRANSLATE_ENDPOINT`, so no translate
button shows.

Out of scope: sending multi-language objects (local authors cannot write variants), machine translation at
ingest, choosing a variant by `read_language_ids`, searching inside variants, translating the C2 quote card.

## Owner rulings (2026-10-12)

1. **Display:** every variant is stored; each viewer sees the variant for their locale, else the sender's default.
2. **Storage:** approach 1 — one nullable JSONB column per table holding only the non-default variants.
3. **Switcher:** an "Also in:" line lists the other languages and lets any viewer switch.
4. **Container models:** `LT_LOAD_ONLY` from `.env.quadlet`, default `en,es,fr,de,pt,it,ja,nl,pl,zh`.
5. **Container storage:** a named volume keeps downloaded models across restarts.

## Data

- `post.language_variants` and `post_reply.language_variants`: `JSONB`, nullable, no index, no backfill. One
  Alembic migration on the current single head (`a7c3e9f1b5d2`).
- Shape: `{"<code>": {"title": str | None, "body_html": str, "cw": str | None}}`. Keys are lowercase BCP 47
  primary subtags (`pt-BR` is stored as `pt`). The default variant is never a key. `title` is set only on a
  `Post` whose object carried `nameMap`; `cw` only when `summaryMap` carried that language.
- Null, not `{}`, when there is no non-default variant.

## Ingest

### Reading the maps

`language_variants_from(obj: dict, default_code: str | None) -> tuple[str | None, dict | None]` (new, in
`app/activitypub/util.py`), pure, no network:

- Reads `contentMap`, `nameMap`, `summaryMap`. A map that is not a non-empty dict of str → str is ignored.
- A key is valid when it matches `^[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8})*$`; it is normalised to its lowercase primary
  subtag. An invalid key is skipped. Two keys with the same primary subtag: the first wins.
- **Default code:** the `contentMap` key whose value equals `content` exactly; else the code from the explicit
  `language` property (`default_code`, from the existing `language_from_ap`); else the first valid `contentMap`
  key; else None. The item's `language_id` is set from this code through the existing `find_language` /
  `find_language_or_create` path, replacing today's "first key" rule. With no `contentMap`, language handling is
  unchanged.
- **Variants:** every other valid `contentMap` key. Its HTML goes through `allowlist_html` then
  `strip_quote_fallback(..., quote_ap_id)` exactly like `content`. Its `title` comes from `nameMap[code]`
  (`Post` only, through the same title cleaning as `name`), its `cw` from `summaryMap[code]` through the same
  cleaning as the existing content warning (`CONTENT_WARNING_MAX_LENGTH`).
- A variant whose cleaned `body_html` is empty is dropped.
- Limits: at most 10 variants (the first 10 valid keys in map order); each variant's `body_html` at most the length
  the item's own body is allowed. Over the cap: the extra variants are dropped, the object is still accepted.
- Returns `(default_code, variants or None)`.

### Where it is applied

- `Post.new` and `PostReply.new` set `language_variants`.
- `update_post_from_activity` and `update_post_reply_from_activity` replace it from the updated object: an Update
  that drops the maps clears it.
- `source` Markdown: when `source` is present the default body comes from it as today; variants still come from
  `contentMap` HTML (Markdown senders do not send multi-key maps).
- D738/D775 hold: nothing on the inbox path fetches, sleeps or retries.

## Render

### Choosing the variant

`chosen_variant(item, requested: str | None) -> tuple[str, dict | None]` (new, small module `app/variants.py`):

- `requested` is the `lang` query parameter, used only when it is a key of `item.language_variants` or the item's
  own default code; anything else is ignored.
- Without a valid `requested`, the viewer's code is the primary subtag of `get_locale()`. Note `get_locale()`
  only returns UI locales PieFed ships; a viewer whose language has no UI locale sees the default. Accepted.
- Returns the code shown and the variant dict, or the default code and None when the default is shown.

### Where it shows

- The post page, the default and dillo post teasers, and the comment teaser/full comment show the chosen
  variant's `title` (falling back to the default title when the variant has none), `body_html`, and `cw`
  (falling back to the default content warning).
- A line "Also in: <name> · <name>" lists the other available languages (default included), each a link to the
  item's page with `?lang=<code>`. Names come from the `Language` table, else the code. The line is omitted when
  there is no variant.
- The translate buttons translate what is shown: the routes take the same `lang` parameter and translate the
  chosen variant. No change to the LibreTranslate client.
- The C2 quote card and every non-HTML output (RSS, search snippets, notifications, the outbound AP object) use the
  default, unchanged.
- Caching: HTML responses already carry `Vary: Accept-Language, Cookie`; the `lang` parameter is part of the URL.

## API

`language_variants` (object or null) is added read-only to the alpha API post view and comment view schemas;
null for a deleted item, as C2 does for `quote_ap_id`.

## LibreTranslate container (hell.cloud)

`deploy/` is local-only (git-ignored); these files live there and the owner installs them per `DEPLOY_GUIDE.md`.

- `deploy/quadlet/pyfedi-libretranslate.container`: `Image=docker.io/libretranslate/libretranslate:<pinned tag>`,
  `ContainerName=pyfedi_libretranslate`, `EnvironmentFile=/home/rocky/pyfedi/.env.quadlet`,
  `Network=pyfedi.network`, `NetworkAlias=libretranslate`, no `PublishPort`, the data volume mounted at the image's
  model directory, a health check on `/languages`, `Restart=always`.
- `deploy/quadlet/pyfedi-libretranslate-data.volume`.
- `.env.quadlet` (via `.env.docker`, regenerated as section 6 describes): `LT_LOAD_ONLY=en,es,fr,de,pt,it,ja,nl,pl,zh`,
  `TRANSLATE_ENDPOINT=http://libretranslate:5000`, `TRANSLATE_KEY=` empty (no API keys on a private network).
- `pyfedi-web.container` and both celery units: `Wants=` and `After=pyfedi-libretranslate.service`, not `Requires=`:
  the site must keep running when the translator is down.
- The autodeploy script does not restart the translator on each deploy (it is not an app unit).
- `DEPLOY_GUIDE.md` gains a section: install, first start (models download), memory, how to change languages.
- Tests: `deploy/autodeploy/tests/` (the deploy dir's own suite) gains a test that parses the new unit and checks
  the keys above, and that the web unit wants rather than requires it.

## Errors and limits

- Malformed maps or keys are ignored; the object is still accepted.
- A translator that is down: the translate button already reports failure; nothing else depends on it.
- An item edited remotely to drop its variants loses them at the next Update.

## Testing

TDD, 100% line and branch coverage of changed lines, one commit per defect, gates via `./run_tests.sh`.

- `language_variants_from`: no maps; one key; default picked by `content` match, by `language`, by first key; each
  invalid map shape; invalid and duplicate keys; region subtags normalised; `nameMap`/`summaryMap` alone and
  together; HTML sanitised and quote fallback stripped in a variant; empty variant dropped; 11 variants capped to
  10; over-long body.
- Ingest: Create of post and comment stores variants and the right language; Update sets, changes, clears.
- `chosen_variant`: locale match, region locale, no match, valid and invalid `lang`, `lang` naming the default.
- Render: each template shows the variant title/body/cw and the "Also in" line; no line without variants; teaser
  content warning hides the variant body as it hides the default.
- Translate routes translate the chosen variant.
- API: field present, null when deleted.
- Migration: upgrade and downgrade on the single head.

## Review focus

- A variant's HTML must never reach a template without `allowlist_html`; a hostile key must never reach a URL or
  template unescaped.
- `?lang=` must not let a viewer see anything the default would not show them (it only picks among stored
  variants of an item they can already see).
- A content warning on the default must still cover a variant with no `cw` of its own.
- A teaser page of many items must not add queries for variants (the column loads with the row; language names
  come from one cached lookup).
- The site must keep serving when the LibreTranslate unit is stopped or missing.
