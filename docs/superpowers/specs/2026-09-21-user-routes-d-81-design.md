# Sub-project 81 slice D: a profile's RSS feed, and file uploads

**Status:** slice D complete. SIX production defects fixed (D1056–D1060,
D1062); D1061 registered. **65 floors.**
**Branch:** `blentz`
**Predecessor:** slice C, which fixed D1051–D1053.

## Scope

| function | gaps |
|---|---|
| `show_profile_rss` | 54 |
| `user_file_upload` | 41 |

## THE PRODUCTION CHANGES

### P1 — a private community's posts escaped through the author's feed (D1057)

`user.posts` is every post the account has made, whatever community it is in,
and this route never looked at the community — while `show_community_rss`
refuses a private community outright (D1013). Measured:

```
PROBE x2 private post listed: True
```

**D998's family at a third surface**: the cross-post form, the sidebar
fragment, and now the author's own RSS. Posts in a banned community went the
same way and are filtered with them.

### P2 — a deleted account was still syndicated (D1056)

`find_local_user` filters `banned` but not `deleted`, so the feed outlived the
profile page, which refuses.

### P3 — most link posts were missing from every user feed (D1059)

```python
if post.body_html is None:
    continue
```

A link or image post ordinarily has no body. **An absence is not reported as a
bug** — a feed that is short looks like an author who posts rarely.

### P4 — the feed described a community (D1058)

Three urls said `/c/{actor}` in a feed about a person. Only one of the three is
observable through this route, and the test says which: feedgen's RSS writer
emits no atom id, and the channel `<link>` carries whichever link was set last.

### P5 — the storage quota could not refuse an upload (D1060)

It was checked only on the render path, after the POST had stored everything
and returned a redirect. `process_upload` has no quota check of its own.
Measured: an account already over quota uploaded two more files and got a 302.
D1001's shape — a limit that records a complaint after the fact.

### P6 — the URL box had no cap (D1062)

10,000 characters is roughly a thousand lines, and each became a `File` row.
D993's family. Capped at 25.

## Registered, not fixed

**D1061** — a file added by URL is recorded with `size=0`, so it never counts
towards the quota. Recording a real size means fetching the remote file, which
is the outbound-request-driven-by-user-input shape D1025 and D1046 were just
closed for. The count cap bounds it; the sizing question is a product decision.

## Success criteria

- Both functions at `[]` on the **full-suite** run.
- P1–P6 land with their pins inverted, and D1058's pin says what it can see.
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D1056**; `tests/README.md` facts from **479**.
