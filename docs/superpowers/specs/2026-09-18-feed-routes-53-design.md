# Sub-project 53: `app/feed/routes.py` Group B — copy, add-remote, lookup

**Status:** design approved, ready for an implementation plan
**Branch:** `blentz`, at `49c65baa`
**Predecessor:** sub-project 52, which closed the module's five lifecycle routes
and took it from 32.272 to 58.143 with four production repairs.

## Goal

Cover `feed_copy`, `feed_add_remote` and `lookup`, and repair the four
production defects found while scoping them — **three of which are the same
defects sub-projects 50 and 52 already repaired elsewhere, living on in a fourth
copy of feed creation that neither round touched.**

## Targets

From the delivered tree's full-suite JSON at `49c65baa`, filtered per function:

| route | lines | missing statements | missing arcs |
|---|---|---|---|
| `feed_copy` | `:226-329` | 67 | 32 |
| `feed_add_remote` | `:97-135` | 27 | 18 |
| `lookup` | `:688-724` | 15 | 11 |
| **total** | | **109** | **61** |

Inside both campaign bands. **No floor this round**; Group C closes the module
and takes it.

## THE FACT THAT SHAPES THIS ROUND

**`feed_copy` is the fourth implementation of "create a feed", and it is the one
nobody has fixed.**

The other three: `make_feed` (`app/shared/feed.py:172`), which sub-project 50
gave an admin check for `is_instance_feed` (**D675**); `edit_feed`, which has
always had one; and `feed_new`, which sub-project 52 gave a site-switch check
for NSFW/NSFL (**D702**). `feed_copy` builds its `Feed(...)` inline at `:246-263`
and reaches none of that work.

Three of this round's four repairs are therefore **the same defects again**:

| defect | first found | repaired in | still live in `feed_copy` |
|---|---|---|---|
| `is_instance_feed` taken from the caller | D675 (sub-project 50) | `make_feed` | **yes** |
| NSFW/NSFL taken past the site's switches | D702 (sub-project 52) | `feed_new` | **yes** |
| the NSFL pre-fill reading the NSFW column | D701 (sub-project 52) | `feed_edit` | **yes, verbatim** |

**Probe**, a non-admin copying a feed on a site with both switches off:

```
PROBE copy status: 302
PROBE copy feed: (True, True, True, None, ...)
        is_instance_feed ^     ^    ^    ^ ap_outbox_url
                          nsfw ^  nsfl
```

and the pre-fill, on a source feed with `nsfw=False, nsfl=True`:

```
PROBE copy prefill source: False True
PROBE copy prefill form:   False False
```

**The lesson this round writes into the register, and it is about the campaign's
own method:** repairing a defect at the site where it was found does not repair
its copies, and this campaign has now met the same three defects twice each. A
round that repairs a defect owes a grep for the shape, not just the line.

## THE FOUR PRODUCTION CHANGES

### P1 — `feed_copy:254` mints an instance feed for anyone

`is_instance_feed=copy_feed_form.is_instance_feed.data`, with only the widget
disabled at `:234-235`. **Repair:** refuse it from a non-admin, matching
`make_feed`'s D675 repair and `edit_feed`'s long-standing check.

### P2 — `feed_copy:251` ignores the site's NSFW/NSFL switches

`nsfw=copy_feed_form.nsfw.data, nsfl=copy_feed_form.nsfl.data`, unguarded — and
unlike `feed_new`, the copy route does not even disable the widgets on the way
in; it disables them only on the GET re-render at `:318-323`. **Repair:** the
same server-side coercion sub-project 52 put in `feed_new`.

### P3 — `feed_copy:325` is D701 again, verbatim

```python
if g.site.enable_nsfl is False:
    copy_feed_form.nsfl.render_kw = {'disabled': True}
else:
    copy_feed_form.nsfw.data = feed_to_copy.nsfw     # nsfl, from nsfl
```

**Repair:** `copy_feed_form.nsfl.data = feed_to_copy.nsfl`.

### P4 — a copied feed has no `ap_outbox_url`

`:255-262` builds `ap_profile_id`, `ap_public_url`, `ap_followers_url` and
`ap_following_url` and **stops**. `make_feed:226` sets `ap_outbox_url` too, and
`app/activitypub/routes.py:2770` serves it as the `"id"` of the feed's outbox
document — so a copied feed publishes an outbox whose id is **null**.

**Probe:** `ap_outbox_url` is `None` on the copied row.

**Repair:** build it, as `make_feed` does.

## Registered, not fixed

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 | `feed_copy:268`, `:273` | **A non-multipart POST is a 400.** `request.files['icon_file']` raises `BadRequestKeyError` when the part is absent, so any client that posts urlencoded gets a bare 400 rather than a form error. Probed: `PROBE files status: 400` before the test switched to multipart. | The fix is an error-contract decision (`request.files.get`, plus what to tell the user), not a coverage change. Registered with the probe. |
| R2 | `feed_copy:299` | **`do_subscribe` is called synchronously**, where `join_feed:54-57` honours `current_app.debug` and otherwise dispatches. Copying a feed with many communities runs every subscribe inline. Same shape as **D683**. | A scheduling change, registered as a twin divergence. |
| R3 | `feed_copy:255-257` | **The split actor identity again** -- `ap_profile_id` lowercases the url, `ap_public_url` does not. D685 and D695 are the same finding in `make_feed` and `edit_feed`. Latent here for the same reason: the route slugifies and lowercases first. | Same reason as D695: rewriting an actor's identity is a federation decision. Registered as the third site. |
| R4 | `feed_add_remote:108-110`; `lookup:705-707` | **A search exception that is not "is blocked." is swallowed silently** -- caught, not re-raised, not logged, not flashed -- and the user is told "Feed not found." Both copies do it. | Deciding what to surface is a product question; the round pins the behaviour so it cannot change unnoticed. |
| R5 | `feed_copy:310` | Copying redirects to `main.index` where `feed_new` redirects to the owner's feed list. A divergence between two routes that do the same job. | Registered; neither is obviously right. |

**And one candidate the probe KILLED, recorded so nobody re-raises it:**
`feed_copy:283`'s `FeedItem.query.join(Feed, FeedItem.feed_id == feed_to_copy.id)`
*looks* like it would multiply the copied items by the number of feeds on the
instance, because the join condition is a constant predicate rather than a
join key. It does not: with four feeds and two items the probe copied **2 items,
2 distinct communities, num_communities 2**. Measured, not reasoned.

## Shapes the tests must handle

- **`feed_copy`'s POST must be multipart**, carrying empty `icon_file` and
  `banner_file` parts, or the route 400s at `request.files[...]` (R1).
- `save_icon_file` / `save_banner_file` touch the filesystem — patch both on
  `app.feed.routes`.
- `feed_add_remote` and `lookup` both call `search_for_feed`; patch it on
  `app.feed.routes` and drive the four address shapes through it.
- `lookup`'s anonymous arm returns `back('/')` after a flash; its authenticated
  arm renders, so the render patch applies there too.
- `feed_membership(current_user, None)` returns **False**, and `False >=
  SUBSCRIPTION_MEMBER` is a legal comparison — that is why the not-found render
  at `:134` and `:720` does not raise. Assert it rather than discovering it.

## Success criteria

- The three routes at `[]`/`[]` on the **full-suite** run, except arcs declared
  unreachable with a named cause and a proof.
- No floor; `coverage_floors.ini` unchanged at 28.
- `git diff --numstat <base> HEAD -- app/` names exactly `app/feed/routes.py`.
- No regression, suite green, floors checked with `&&`.
- A mutation pass over the three routes, scoped as D602 requires.
- Findings registered from **D712**; `tests/README.md` facts from **296**.
