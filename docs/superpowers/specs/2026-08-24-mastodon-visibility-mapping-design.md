# Mastodon Visibility Mapping

Date: 2026-08-24
Status: Approved design, ready for implementation planning

## Problem

PieFed has no reliable way to tell a public Mastodon post from a followers-only
one. The only signal anywhere in the codebase is a single heuristic on replies —
`request_json['to'][0].endswith('/followers')` at `app/models.py:2827-2830` — which
fires only when `to` has exactly one element. Posts have no equivalent check at all.

This matters because PieFed now ingests boosts from followed accounts. A boost can
carry content the author never made public.

### The immediate breakage

The boost feed clause added in the previous project does not work. It reads:

```sql
(p.private is false AND EXISTS (SELECT 1 FROM post_boost pb ...))
```

`app/utils.py:3271`. It was written believing `Post.private` marks a followers-only
post. It does not. `Post.new` sets it for *any* object without a `name`:

```python
if 'name' not in request_json['object']:  # Microblog posts
    private = True
```

`app/models.py:1796-1797`. Mastodon Notes have no `name` — that is why
`microblog_content_to_title` exists (`app/models.py:1860`). Ingestion reaches this
through `create_post` → `Post.new` (`app/activitypub/util.py:2494`).

So every ingested Mastodon post has `private = True`, and the clause excludes all of
them. **Boosted posts never appear in the feed.** The feature is inert in
production.

No test caught it because `make_post()` in `tests/factories.py` defaults
`private=False`, producing a post shape ingestion never creates. Every feed test
asserted against a fiction.

### What `private` actually means

`Post.private == False` is the filter on every discovery surface:

- search — `app/search/routes.py:59`
- tags — `app/tag/routes.py:38`
- domains — `app/domain/routes.py:53,127`
- community listings — `app/community/routes.py:370,715`
- profiles — `app/user/utils.py:175`

while the subscribed feed deliberately skips it (`app/utils.py:3281-3282` applies it
only when `not include_following`).

That is unlisted behaviour: excluded from discovery, visible to followers. `private`
is a badly named unlisted marker, not a broken privacy field. `PostReply.private`
means something different again — followers-only. The two models use the same column
name for two different concepts, which is what misled the previous project.

## Policy

Ingest **public** and **unlisted**. Refuse **followers-only** and **direct** at the
door, with a logged reason.

Refusing rather than storing is the core decision. PieFed has no per-post ACL
machinery, and honouring followers-only faithfully would mean adding a "viewer
follows author" check to all seven discovery surfaces above plus the feed and the
API views. A single missed surface is a privacy leak of exactly the kind this
project exists to close. Content never stored cannot leak.

Direct messages are unaffected: `process_chat` handles them at
`app/activitypub/routes.py:1169` and `:1191`, before the Create dispatch reaches
`create_post`.

## Scope

### In scope

1. A visibility classifier for ActivityPub objects.
2. Refusal of followers-only and direct content at ingest, for both posts and
   replies.
3. Removal of the incorrect `p.private is false` gate on the boost feed clause.
4. Test factories that produce production-shaped microblog posts, and feed tests
   re-pointed at that shape.

### Out of scope

- **A `visibility` column.** Not needed: public and unlisted are both ingested and
  both keep today's `private` semantics, and the other two levels are never stored.
  Adding a column would cost a migration and a backfill that nothing reads.
- **Changing what `private` means, or renaming it.** The name is misleading, but it
  is load-bearing on seven read paths. Renaming it is a separate mechanical change
  with its own risk, and it would not fix anything this project fixes.
- **Letting genuinely public microblog posts into discovery surfaces.** Distinguishing
  them is possible once the classifier exists, but pushing remote microblog volume
  into search, tags and domain pages is a visible product change, and a separate
  decision.
- **Purging content already stored.** Any followers-only replies stored under the old
  heuristic stay. Deciding whether to remove them is a data question, not a code one.
- **Outbound visibility.** PieFed's own addressing of the posts it federates is
  unchanged.

## Architecture

### The classifier

```
activitypub_visibility(obj: dict) -> str
```

in `app/activitypub/util.py`, returning `'public'`, `'unlisted'`, `'followers'`, or
`'direct'`. Pure: no database, no network, no app context.

Rules, evaluated against the object's `to` and `cc`:

| Condition | Result |
|---|---|
| Public appears in `to` | `public` |
| Public appears in `cc` but not `to` | `unlisted` |
| No Public anywhere, some `/followers` URL addressed | `followers` |
| Neither | `direct` |

Two details are load-bearing:

**`as:Public` has three legal spellings** — `https://www.w3.org/ns/activitystreams#Public`,
`as:Public`, and bare `Public`. All three must match. Recognising only the first
would classify followers-only content from some implementations as public, which is
the precise failure this project exists to prevent.

**`to` and `cc` are independently a string, a list, or absent.** Each must be
normalised to a list before inspection. An unguarded `obj['to'][0]` is how the
existing reply heuristic ended up only working for single-element addressing.

### Where the classifier reads from

It takes the **object**, never the activity.

In the boost path, `create_resolved_object` synthesises the activity as
`{'id': ..., 'object': post_data}` with no addressing at the activity level — only
the fetched object carries `to`/`cc`. A classifier reading the activity would see
nothing and default everything to public, silently defeating the whole design.

Callers pass `request_json['object']`.

### Enforcement point

`create_post` (`app/activitypub/util.py:2487`) and `create_post_reply` (`:2311`)
each classify and refuse `followers` and `direct`, logging a distinct reason via
`log_incoming_ap`.

Both sit downstream of the Create path, the Announce path, and the boost path, so
one check per model covers all three. Enforcing further up would mean three copies,
and duplicated security checks rot.

### Removing the incorrect gate

`app/utils.py:3271` drops `p.private is false AND`, leaving the boost disjunct as the
`EXISTS` alone. This restores the boost feature.

**Ordering matters.** Removing the gate before refusal is in place would reopen a
real hole, because today that gate is the only thing keeping any non-public boosted
content out of the feed — even though it does so by excluding everything. The
implementation must land refusal first and removal second, and must not leave the
tree in a state where the gate is gone and refusal is absent.

## Testing

Pure classifier tests, no database, following `tests/test_microblog_announce.py`:
every combination in the table above, all three `as:Public` spellings, `to`/`cc` as
string, as list, and absent, and a `/followers` URL in each position.

Database-backed ingest tests: a followers-only post is refused and no row is created;
a direct post is refused; public and unlisted are accepted; the same four for
replies. Each asserts the logged reason, using the `log_spy` pattern already in
`tests/test_process_microblog_announce.py`.

Feed tests re-pointed at production shape: `make_post` gains a `microblog=True` mode
producing `private=True` with no title, and a test asserts a boosted *microblog* post
appears in the subscribed feed. That test fails against current code — it is the
regression test for the breakage described above, and its failing first is the proof
it bites.

DM regression: a direct `Create`/`Note` still reaches `process_chat` and is not
refused by the new check.

## Risks

- **Mis-classifying followers-only as public** would leak content. Mitigated by
  covering all three `as:Public` spellings and by testing each explicitly.
- **The ordering dependency** between refusal and gate removal, above.
- **Refusal is silent to the sender.** A Mastodon user whose followers-only post is
  boosted into PieFed gets no signal it was dropped. That is correct — the
  alternative is storing it — but it means the logged reason is the only diagnostic,
  which is why each refusal path logs distinctly.
- **`private`'s misleading name survives this project.** The next person to read
  `Post.private` will make the same wrong assumption the previous project did. The
  classifier's docstring should say plainly what `private` does and does not mean.
