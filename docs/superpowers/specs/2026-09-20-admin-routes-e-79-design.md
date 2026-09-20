# Sub-project 79 slice E: who may do what

**Status:** slice E complete. THREE production defects fixed (D937-D939), plus
one found by the mutation pass (D941).
**Branch:** `blentz`, at `a54587a5d`
**Predecessor:** slice D, which fixed D931-D936. Module at **55.0%**, 796
missing.

## Scope

| function | gaps |
|---|---|
| `admin_permissions` | 17 |
| `masquerade` | 5 |

Twenty-two lines, and the smallest slice of the six — deliberately. This is the
page that decides what Staff and Admin may do, and the route that lets an
administrator become any local user. Everything else in the module is guarded
by what these two write; the user-administration group (`admin_users`,
`admin_users_add`, `admin_user_edit`, the delete tasks — 185 gaps) is slice F.

## The production changes

`admin_permissions` derived the set of permissions it offers from `SELECT
DISTINCT permission FROM role_permission` — from the rows it was about to
delete. All three defects follow from that one decision.

### P1 — unchecking every box deletes the permission (D937)

Measured:

```
PROBE p1 before: ['approve registrations', 'change user roles', 'manage users']
PROBE p1 after unchecking "approve registrations": ['change user roles', 'manage users']
PROBE p1 offered on the page now: ['change user roles', 'manage users']
```

A one-way door on a security control, reachable by unticking two boxes: the
permission is gone from the page and can never be granted to anyone again.

**Fix:** `ROLE_PERMISSIONS` in `app/constants.py`. A permission nobody
currently holds is still a permission.

### P2 — a save strips every other role (D938)

`DELETE FROM role_permission` cleared the whole table while only roles 3 and 4
were written back. Measured with a `Moderator` role holding `manage users`:
`[('manage users',)]` before a save that changed nothing about Staff or Admin,
`[]` after.

**Fix:** scope the delete to `EDITABLE_ROLE_IDS`.

### P3 — the cache invalidation never matched (D939)

`cache.delete_memoized(user_access, permission, staff_user_id)` where
`permission` was the SELECT's `Row` — repr `('change instance settings',)` —
not the string `user_access` is memoized under. And only users holding role 3
were collected, so an admin was never invalidated at all.

**Fix:** pass the string; collect users of both editable roles.

## The durable artefact

`test_the_permission_vocabulary_matches_the_codebase` walks every `.py` under
`app/` with `ast`, collects the literal first argument of every
`permission_required(...)` and `user_access(...)` call, and asserts the set
equals `ROLE_PERMISSIONS` in both directions. A permission the code checks but
the constant omits is unreachable — nobody can be granted it and the check can
never pass — which is D937 again by another route. A permission in the constant
that nothing checks is dead UI.

`ast` and not a regex: a regex cannot tell a call from the same words in a
docstring, and the test file's own prose names most of these strings.

## Result

- Both functions at `[]`; mutation pass **22 of 22** after one repair.
- D941, from the mutation pass: `login_user(user, False)` survived becoming
  `True`. With a remember-me cookie the masquerade would outlive the browser
  session, and for a feature with no confirmation and no audit record "ends
  with the session" is the only containment there is.
- Still no floor on `app/admin/routes.py`: slice F remains.
