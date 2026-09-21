# Sub-project 80 slice A: community moderation authority

**Status:** slice A complete. FIVE production defects fixed (D955-D959); the
design anticipated three. See the findings ledger, D955-D965.
**Branch:** `blentz`, at `9075267f2`
**Predecessor:** sub-project 79, which closed `app/admin/routes.py`'s security
surface across six slices and floored it at 67. **64 floors.**

## How the target was chosen

D891 ranked `app/community/routes.py` second by authorization density: 143
constructs over 1441 missed statements. It is 1865 statements at **18.7%** and
will take several rounds, split by its own seams as `app/admin/routes.py` was.

Slice A takes the group that decides **who may exercise authority inside a
community** — who can ban, unban, promote and demote:

| function | gaps |
|---|---|
| `community_ban_user` | 46 |
| `community_moderate_subscribers` | 42 |
| `community_unban_user` | 27 |
| `community_remove_owner` | 21 |
| `community_block` | 20 |
| `community_make_owner` | 19 |

Reading them found three defects before a line of test was written.

## THE PRODUCTION CHANGES

### P1 — an unban is a GET, with no CSRF token

```python
@bp.route('/community/<int:community_id>/<int:user_id>/unban_user_community',
          methods=['GET', 'POST'])
@login_required
def community_unban_user(community_id: int, user_id: int):
```

There is no form in the body — the unban happens unconditionally on whichever
method arrives — and `app/utils.py`'s `login_required` validates CSRF **only
for POST**:

```python
if request.method == 'POST' and csrf:
    validate_csrf(...)
```

So a GET with no token at all removes the ban. Measured:

```
PROBE c1 url /community/community/1/3/unban_user_community
PROBE c1 GET with NO csrf token, status: 302
PROBE c1 ban row still there? False
```

A moderator who loads `<img src="https://instance/community/community/1/3/unban_user_community">`
on any page anywhere unbans user 3. The template renders it as a plain `<a href>`.

**Fix:** POST-only, and the template switches to the pattern this codebase
already uses for exactly this — `class="confirm_first send_post" href="#"
data-url="..."`, as `community_mod_list.html` does for Make owner and Remove
owner. `app/static/js/scripts.js:658` attaches the CSRF token from the meta tag.

### P2 — a banned user keeps their community moderation powers

`current_user.banned` is checked by `add_post`, `community_edit`,
`community_delete`, `community_add_moderator`, `community_find_moderator`,
`community_moderate`, the RSS routes and `community_moderate_comments`. It is
**not** checked by `community_make_owner`, `community_remove_owner`,
`community_ban_user`, `community_unban_user` or
`community_moderate_subscribers`.

So an instance-banned account that holds community authority cannot post, edit
the community or add a moderator — but can still ban and unban people and
promote and demote owners. Measured:

```
PROBE c2 banned moderator ban POST status: 302
PROBE c2 victim now banned from community? True
PROBE c3 banned owner make_owner status: 302
PROBE c3 other is now an owner? True
```

`banned` is the instance's primary sanction. A sanction that leaves the
person's authority over other accounts intact is not one.

**Fix:** the same two lines the neighbouring routes use, plus a ratchet (below)
so the next route added does not repeat it.

### P3 — a non-moderator can make the server fetch an arbitrary actor

`community_moderate_subscribers` runs its find-and-ban form **before** it
checks anything:

```python
if ban_user_form.submit.data and ban_user_form.validate():
    user_to_ban = find_actor_or_create(ban_user_form.user_name.data)
elif community is not None:
    if community.is_moderator() or current_user.is_admin():
```

`find_actor_or_create` reaches `create_actor_from_remote`, an outbound HTTP
fetch of a handle the submitter chose. Measured, as a user with no relationship
to the community at all:

```
PROBE s1 url /community/general/moderate/subscribers  is nobody a moderator? False
PROBE s1 find_actor_or_create called by a NON-moderator? [call('victim@attacker.example')]
```

The ban itself is safe — the redirect lands on `community_ban_user`, which
checks — but the fetch has already happened. Any logged-in account can drive
the instance's outbound fetcher at a host of its choosing from this route.

**Fix:** move the authorization check above the form handling.

## The durable artefact

A blueprint-wide ratchet, in the shape D901 established for `app/admin`: walk
`app.url_map` for every rule on the community blueprint, and for each one that
is **state-changing**, assert a banned account is refused. P2's five routes are
the ones it would have caught; the point is the ones added later.

Read-only rules are excluded by name in a list that the test prints when it
fails, so adding a route means deciding which side it is on rather than
silently landing on the permissive one.

## Registered, not fixed

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 | `app/community/routes.py:1912` | `community_moderate_subscribers` gates on `current_user.is_admin()` while every neighbouring moderation route uses `is_admin_or_staff()`, so staff can ban from a community but cannot see who is banned. | A decision about what staff are for, which spans the blueprint. |
| R2 | `app/community/routes.py:1907` | `flash(_(f'User: {ban_user_form.user_name.data} unable to be found'))` — D815's shape, interpolated before gettext sees it. | The same repair as D936, in a slice whose production changes are all security; grouping it with the rest of the blueprint's instances is cheaper to review. |

## What the slice found beyond the design

- **D958** -- `validate_csrf` raises wtforms' `ValidationError`, and no
  `CSRFProtect` is registered on this app, so a missing or stale token answered
  **500** on every route using `app.utils.login_required`. Found only because a
  row asserting "a POST with no token is refused" had to say what refusal looks
  like.
- **D959** -- the ban path's "delete replies" query filtered on
  `Post.community_id` without joining `Post`, so it matched every reply the
  user had ever written anywhere. A moderator of one community destroyed that
  person's comments across the whole instance.
- **D962** -- hoisting D957's check orphaned the `abort(401)` below it. Removed
  in the same commit.

R1 became **D963**, R2 became **D964**, and a third registration, **D965**, was
added: `is_admin_or_staff()` reads role NAMES with no constant and no ratchet
behind them.

## Success criteria

- The six functions at `[]` on the **full-suite** run.
- **No floor yet**: `app/community/routes.py` stays unfloored until its last
  slice, as `app/admin/routes.py` did.
- P1-P3 land with their pins inverted; the banned-user ratchet passes.
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D955**; `tests/README.md` facts from **395**.
