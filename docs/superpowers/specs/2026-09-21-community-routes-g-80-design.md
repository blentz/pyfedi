# Sub-project 80 slice G: syndication — RSS out, iCal out, RSS in

**Status:** slice G complete. FIVE production defects fixed (D1010–D1014).
**Branch:** `blentz`
**Predecessor:** slice F, which fixed D1005–D1007. **64 floors.**

## Scope

| function | gaps |
|---|---|
| `community_rss_feed_edit` | 27 |
| `show_community_ical` | 26 |
| `community_rss_feed_delete` | 12 |
| `show_community_rss` | 11 |
| `community_rss_feeds` | 7 |

Everything that leaves the community as a feed, and the feeds that come in.

## THE PRODUCTION CHANGES

### P1 — a moderator could take over another community's feed (D1010)

`community_rss_feed_edit(community_id, feed_id)` takes both ids from the URL
and **never checks that the feed belongs to the community**:

```python
rss_feed = db.session.get(RssFeed, feed_id) if feed_id else None
```

The authorization check above it asks only whether the caller moderates
`community_id` — their own community. Measured, as a moderator of `mine`
against a feed belonging to `theirs`:

```
PROBE r1 their feed is now: Taken over https://attacker.example/feed.xml
```

`RssFeed.url` is the input to the background fetcher that creates posts in the
feed's community, so this is a **cross-community content-ingest takeover**: a
moderator of any community could make another community's importer poll a URL
they control, and the posts land there.

`community_rss_feed_delete` has the same hole, and its effect is worse —
`delete_dependencies()` deletes every post the feed created:

```
PROBE r2 their feed still exists? False
```

**Fix:** both routes check `rss_feed.community_id != community.id` and answer
404.

### P2 — an unknown feed id was an AttributeError (D1011)

`rss_feed.title = form.name.data` on a `None` row. The ownership check above
answers this too, since a missing row cannot belong to the community.

### P3 — two 500s where refusals were meant (D1012)

`community_rss_feeds` ended both of its arms without returning, so a
non-moderator and an unresolvable name each produced

```
TypeError: The view function for 'community.community_rss_feeds' did not
return a valid response.
```

**Fix:** `abort(404)` and `abort(403)`, hoisted, which also unnests the body.

### P4 — the RSS feed answered 304 for a private community (D1013)

The conditional-request check ran **before** the access check:

```
PROBE r5 private feed with matching etag: 304
PROBE r5 private feed without etag: 403
```

A client holding an ETag from before the community was made private kept
getting `304 Not Modified` where a fresh request was refused. The ETag is
`{id}_{hash(last_active)}`, so a 304 on a guessed value also confirms the
community's current `last_active`. Refuse first, then answer conditionally.

### P5 — one bad event failed the whole calendar (D1014)

`evt.begin = post.event.start`, where `Post.event` is a relationship that can
be absent for a `POST_TYPE_EVENT` post — a federated event whose object
carried no usable times, or a post whose type changed:

```
PROBE r6 RAISED: AttributeError 'NoneType' object has no attribute 'start'
```

The entry is skipped rather than the calendar lost.

## Registered, not fixed

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 | `pytest.ini` | `ics` 0.7.3 serializes an alarm by calling `str(alarm)` on it, and `Component.__str__` warns that its behaviour changes in 0.9 — a FutureWarning raised **inside** the library, once per event with an alarm. The only way to avoid it from our side is to stop attaching the 30-minute reminder, which is a product feature. Filtered by module, category and message text, with the reason in the file. | An `ics` upgrade; 0.8 is still alpha. |

## Success criteria

- The five functions at `[]` on the **full-suite** run.
- P1–P5 land with their pins inverted — five inversions.
- Suite green, **and the warning count unchanged**; floors checked with `&&`;
  a mutation pass, anchors first (D833).
- Findings from **D1010**; `tests/README.md` facts from **446**.
