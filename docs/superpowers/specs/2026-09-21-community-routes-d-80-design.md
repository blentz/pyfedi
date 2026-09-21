# Sub-project 80 slice D: membership — joining, leaving and inviting

**Status:** slice D complete. FIVE production defects fixed (D991-D993, and
D991's second site plus two more instances of D992); registered D994-D997.
**Branch:** `blentz`, at `fda311319`
**Predecessor:** slice C, then the application-wide sweep that produced
`tests/test_mutating_get_routes.py`. **64 floors.**

## Scope

| function | gaps |
|---|---|
| `community_invite` | 34 |
| `do_subscribe` | 31 |
| `unsubscribe` | 24 |
| `community_invite_accept` | 21 |
| `community_leave_all` | 19 |
| `join_then_add` | 18 |

Three of the eleven routes inventoried as D988 live here.

## THE PRODUCTION CHANGES

### P1 — a community ban does not prevent joining (D991)

`do_subscribe` checks twice. The first is the real gate:

```python
if community.id in communities_banned_from(user.id):
    abort(401)
```

The second reads the row directly, and does nothing with it:

```python
banned = CommunityBan.query.filter_by(user_id=user.id, community_id=community.id).first()
if banned:
    if not admin_preload:
        if current_user and current_user.id == user_id:
            flash(_('You cannot join this community'))
    ...
# for local communities, joining is instant
existing_membership = ...
if not existing_membership:
    member = CommunityMember(...)      # joins anyway
```

The branch flashes and falls through. Measured, with a `CommunityBan` row
present and the memoized list not yet reflecting it:

```
PROBE n1 flashed: []
PROBE n1 banned user is now a member? True
```

That matters because `communities_banned_from` is
`@cache.memoize(timeout=86400)` — a **24-hour** cache. `community_ban_user`
invalidates it, but a ban that arrives any other way (federated in, or written
by a tool that does not know to) leaves the only working gate stale for a day,
and the direct read that should catch it is inert.

**Fix:** the direct read refuses the join, which is what its own flash already
claims.

### P2 — an unresolvable remote community is a 500 (D992)

```python
community = Community.query.filter_by(ap_id=actor).first()
if community is None:
    community = search_for_community(...)
if community.banned:
```

`search_for_community` returns `None` for a handle it cannot resolve, and the
next line dereferences it:

```
PROBE n2 RAISED: AttributeError 'NoneType' object has no attribute 'banned'
```

The function already has a "community not found" path at the bottom; this
never reaches it.

### P3 — an unbounded outbound email primitive (D993)

`community_invite` splits `form.to.data` on newlines and calls
`invite_with_email(...)` once per line. `InviteCommunityForm.to` carries
`DataRequired()` and a check for commas — **no length limit, and no cap on the
number of lines.** `Community.invitations` defaults to `0`, and `can_invite()`
returns True for anyone when it is 0.

So any account past `created_very_recently()` can paste ten thousand addresses
into a default community's invite box and have the instance send ten thousand
emails, from its own mail server, with its own reputation. The only existing
limits are the ban check and the account-age check.

**Fix:** cap the number of invitations per submission in the form, with a
message that says the limit. The feature is for inviting people you know.

## Registered, not fixed

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 | `app/community/routes.py:921`, `:985` | `unsubscribe` and `join_then_add` accept GET and mutate — two of D988's eleven. **Both carry a deliberate comment**: `# POST is used by htmx, GET when JS is disabled`. The CSRF is real (an attacker can make someone leave a community), but removing the GET removes the no-JS path, which is a product decision rather than a mechanical fix. | Needs a decision about no-JS support, not a method change. Recorded against D988 so the reason is attached to the entry. |

## Success criteria

- The six functions at `[]` on the **full-suite** run.
- No floor yet on `app/community/routes.py`.
- P1-P3 land with their pins inverted.
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D991**; `tests/README.md` facts from **424**.
