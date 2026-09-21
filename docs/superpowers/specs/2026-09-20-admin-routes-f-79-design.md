# Sub-project 79 slice F: user administration — the last of `app/admin/routes.py`'s security surface

**Status:** slice F complete, and sub-project 79 with it. SEVEN production
defects fixed (D944-D950), two pieces of dead code removed (D951, D952).
`app/admin/routes.py` floored at **67**.
**Branch:** `blentz`, at `ac0323614`
**Predecessor:** slice E, which fixed D937-D939 and added the permission
vocabulary ratchet. Module at 56%.

## Scope

| function | gaps |
|---|---|
| `admin_user_edit` | 53 |
| `admin_users_add` | 44 |
| `admin_users` | 29 |
| `admin_user_delete_task` | 21 |
| `admin_user_delete` | 13 |
| `admin_user_resend_email` | 13 |
| `delete_user_files_in_background` | 12 |

Plus `app/admin/util.py`'s `unsubscribe_from_everything_then_delete_task`,
which is where deleting a local user actually happens.

This is where accounts are created, banned, promoted, demoted and destroyed.
After it, the remaining gaps in the module are communities, topics, content and
CMS pages — none of which decide who may do what.

## THE PRODUCTION CHANGES

### P1 — the "Banned" and "Verified" checkboxes on the add-user form do nothing (D944)

`AddUserForm` declares both. `admin_users_add` sets sixteen attributes from the
form and **neither of these two**.

```
PROBE u1 ticked Banned and Verified -> banned: False verified: False
```

An admin creating a pre-banned account gets an active one, with no error. A
moderation control that silently does nothing is worse than one that is absent.

### P2 — a refused edit discards what the admin typed (D945)

`admin_user_edit` ends its POST branch with `else:` rather than `elif
request.method == 'GET':`, so a submission the form **refuses** falls into the
pre-fill arm and is overwritten from the database.

```
PROBE u2 errors: {'role': ['Not a valid choice.']}
PROBE u2 admin_note redisplayed as: 'the stored note'
PROBE u2 banned redisplayed as: False
```

The admin's note and their Banned tick are both gone, and the page looks as
though they never typed anything. This is D907's shape for the third time —
registered in slice A, found again in slice C's `admin_federation` — and here
it destroys moderation input rather than a settings field.

### P3 — a demoted admin keeps their permissions for 50 seconds (D946)

`admin_user_edit` rewrites `user_role` and **invalidates nothing**.
`user_access` is `@cache.memoize(timeout=50)`, so an administrator stripped of
their role goes on passing every permission check for up to fifty seconds after
the change is saved. The UI knows: it flashes *"Permissions are cached for 50
seconds so new admin roles won't take effect immediately."*

Slice E built exactly this invalidation for `admin_permissions`. Demotion is
the direction that matters, and the apology in the flash message is the tell.

### P4 — deleting a LOCAL user is never recorded (D947)

`admin_user_delete_task` calls `add_to_modlog('delete_user', ...)` in the
**remote** branch only. Neither local path — finalized or not — writes anything,
and `unsubscribe_from_everything_then_delete_task` in `app/admin/util.py`
contains no `add_to_modlog` at all (`grep -c` gives 0).

So the most destructive action available against a local account leaves no
trace in the modlog, while the equivalent against a remote one does. Deleting
one's own instance's users is precisely the case an audit trail exists for.

### P5 — `delete_dependencies()` outside its own `if user:` (D948)

`app/admin/util.py`: the `if user:` block ends, and then `user.delete_dependencies()`
and the UPDATE run unconditionally — so a user already gone when the task runs
raises `AttributeError: 'NoneType' object has no attribute
'delete_dependencies'`. Two admins pressing Delete, or a retried task, is
enough.

### P6 — the verified filter is dropped from the pagination links (D949)

`admin_users` builds `next_url` and `prev_url` without `verified`, so paging
past the first page silently drops the filter the admin selected.

### P7 — the resend-email route echoes the exception (D950)

`message = _("Problem sending email: ") + str(e)` returns the internal
exception text to the browser, and concatenates onto a translated string, which
is the D815 shape again. Same class as D895.

### P8 — dead avatar and banner removal in `admin_users_add` (D951)

`user = User()`, so `user.avatar_id` and `user.cover_id` are always `None` and
both "remove the old one" blocks are unreachable — ten lines copied from
`admin_user_edit`, where they do have a purpose.

### P9 — `form.role.data == 4` (D952)

`ROLE_ADMIN` is imported in this module and used two lines below. Slice E
replaced the same magic numbers with `EDITABLE_ROLE_IDS`.

## Success criteria

- All seven functions at `[]` on the **full-suite** run.
- **The floor is taken this round**: `app/admin/routes.py` has been unfloored
  since slice A by design, and slice F is the last of the six.
- P1-P9 land with their pins inverted.
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D944**; `tests/README.md` facts from **384**.

All met. P9/D952 dissolved rather than landing on its own: the `form.role.data
== 4` test guarded only the flash message that D946's fix made untrue, so it
left with it.
