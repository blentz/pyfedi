# Sub-project 80 slice I: the rest of the blueprint — and the floor

**Status:** slice I complete. SEVEN production defects fixed (D1025–D1031).
**This is the last slice of `app/community/routes.py`**, so the floor on that
module is taken in this round: **every statement covered** (1,944 of 1,944),
one excluded as unreachable, and 33 partial branches left as the next target.
The floor is 98, because `percent_covered` combines statements with branches.
**Branch:** `blentz`
**Predecessor:** slice H, which fixed D1017–D1021. **64 floors, rising to 65.**

## Scope

Everything left: 171 gaps across eighteen functions — the metadata fetcher,
the two htmx fragments, the name-search datalist, the moderation queues, the
mod log, the remote lookup, the icon and banner buttons, the theme toggle,
favouriting, notifications, the remote refresh and the wiki delete.

## THE PRODUCTION CHANGES

### P1 — an unauthenticated outbound-fetch primitive (D1025)

`check_url_already_posted` carried no `@login_required` and calls
`retrieve_metadata_of_url`, which issues `httpx_client.get` against whatever
the caller passed. Measured with no session at all:

```
PROBE i1 outbound fetch attempted: True ('https://example.com/x',)
```

`is_invalid_get_request_uri` keeps those requests off private ranges, so this
was never SSRF to the inside — it was **anyone on the internet making this
instance issue outbound GETs from its own address, as fast as they liked**, the
same family as D993's unbounded email. Its only caller is the new-post form,
which is behind a login already.

### P2 — the second fragment endpoint with no access control (D1026, D1027)

`community_changed` renders the community's flair list and the whole side pane.
D1017's shape exactly, one endpoint along:

```
PROBE i2 status: 200   title leaked: True
```

and `db.session.get(Community, 'abc')` reached the database as a string —
`DataError: invalid input syntax for type integer: "abc"`, an unauthenticated
500 from a query parameter.

### P3 — no authorization at all on the image buttons (D1028)

`remove_icon` and `remove_header` had `@login_required` **and nothing else**.
Any account could delete any community's icon or banner, from disk as well as
from the database:

```
PROBE i4 status: 200   icon_id now: None
```

from an account with no relationship to the community. Both now require the
owner, a moderator or an admin — the check `community_edit`, the page these
buttons live on, already applies before showing them.

### P4 — an IDOR on somebody else's preference (D1029)

`flip_community_theme_allowed(community_id, user_id)` passed the URL's
`user_id` straight to `set_community_theme_allowed`:

```
PROBE i5 victim theme setting before/after: True False
```

### P5 — the moderation queue 500s when it overflows (D1030)

Both pagination links omitted `actor`, which the endpoint's rule requires:

```
PROBE i6 RAISED: BuildError Could not build url for endpoint
'community.community_moderate' with values ['page']. Did you forget to specify
values ['actor']?
```

They are only built when the queue has more than one page — **so the page
failed exactly when a community was being flooded with reports and its
moderators most needed it.**

### P6 — D1012's shape for the third time (D1031)

`community_moderate_comments` returned None for a non-moderator and for a name
that does not resolve. Its sibling `community_moderate`, written from the same
template, answers both properly.

## The one line not covered

`show_community`'s feed-parent walk carries `if feed is None: break`, which
`feed_parent_feed_id_fkey` makes unreachable — unlike the topic walk eight
lines up, whose `Topic.parent_id` has no such constraint and which produced
D1006's measured AttributeError. Marked `# pragma: no cover` with that reason
rather than covered, because reaching it means dropping a foreign key.

## Success criteria

- **`app/community/routes.py` at `[]` for statements**, and floored at 98.
- P1–P6 land with their pins inverted — eight inversions.
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D1025**; `tests/README.md` facts from **457**.
