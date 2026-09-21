# Sub-project 80 slice H: moderators, owners and membership

**Status:** slice H complete. FIVE production defects fixed (D1017–D1021), two
of them in `app/post/routes.py`, plus a strengthened D989 ratchet.
**Branch:** `blentz`
**Predecessor:** slice G, which fixed D1010–D1014. **64 floors.**

## Scope

| function | gaps |
|---|---|
| `community_membership_manage` | 17 |
| `community_my_flair` | 16 |
| `community_find_moderator` | 10 |
| `community_kick_user` | 9 |
| `community_mod_list` | 8 |
| `community_remove_moderator` | 7 |
| `community_add_moderator` | 4 |
| `community_remove_owner`, `community_make_owner`, `get_sidebar` | the rest |

## THE PRODUCTION CHANGES

### P1 — the sidebar fragment had no access control at all (D1017)

`/community/get_sidebar/<id>` renders the community's title and description
with **no login, no membership check and no banned check**, while
`show_community` — the page the fragment belongs to — answers 404 for a banned
community and 403 for a private one. Measured anonymously against a private
community:

```
PROBE h2 description leaked: True title leaked: True
```

**Fix:** the same three refusals, in the same order.

### P2 — promoting a moderator on a bare GET (D1018)

`community_add_moderator` was `methods=['GET', 'POST']` and called
`add_mod_to_community`. `login_required` validates CSRF only for POST, so an
owner who loaded `<img src=".../moderators/add/123">` promoted account 123.
It also let the helper's `Exception('no_permission')` escape:

```
PROBE h4 RAISED: Exception no_permission
```

— a 500 where the sibling `community_remove_moderator` answers 401, and the
same for `NoResultFound` on an unknown community or user, which now answers
404.

### P3 — a 500 for a community name that does not resolve (D1019)

`community_my_flair`'s whole body sat inside `if community is not None:` with
no else. D1012's shape, second instance.

### P4 — the membership form read every community's flair blocks (D1020)

`CommunityFlairBlock.query.filter(user_id == current_user.id)`, unscoped. One
community's form opened pre-checked with another's flair ids, which are not
among its own choices — so WTForms refused the submission and the page
silently would not save while a foreign block existed.

### P5 — two more mutating GETs, in the post blueprint (D1021)

`post_sticky` (`/post/<id>/sticky/<mode>`) and `post_vote`
(`/post/<id>/<direction>/<federate>`) both accepted GET and both mutated. D987
fixed the instance-wide sticky and left the community-level twin; the vote
route is the one with reach, since an `<img>` on any page cast the viewer's
vote. The site's own vote buttons are `hx-post` with no anchor fallback, and
`comment_vote` next door was already POST-only, so the GET arm was used by
nothing but the forgery. `post_options.html`'s two sticky links move to the
`confirm_first send_post` pattern D987 introduced in that same file.

### P6 — the ratchet could not see any of them

`tests/test_mutating_get_routes.py` looked for `db.session` writes **in the
view body**. D1018 and D1021 all write one call deeper, through
`app/shared/`. The detector now also matches a list of mutating helpers, which
immediately surfaced three more routes — `community.subscribe`,
`post.post_notification`, `post.post_reply_notification` — all three carrying
the deliberate "POST from htmx, GET when JS is off" comment, so they join
`KNOWN_GET_MUTATORS` with that reason rather than being changed. The row that
names the fixed routes gains the three new ones, so none can quietly return.

## Removed as dead

`community_kick_user`'s `if community is not None: ... else: abort(404)` could
not reach its false arm — the line above is `or abort(404)`. Removed rather
than covered, the D983 precedent.

## Success criteria

- The ten functions at `[]` on the **full-suite** run.
- P1–P5 land with their pins inverted — seven inversions.
- The ratchet green at 49 rows, with the three new entries justified.
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D1017**; `tests/README.md` facts from **452**.
