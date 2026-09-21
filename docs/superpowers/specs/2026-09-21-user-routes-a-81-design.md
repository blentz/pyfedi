# Sub-project 81 slice A: banning, blocking, reporting and deletion

**Status:** slice A complete. SIX production defects fixed (D1033–D1038).
**Branch:** `blentz`
**Predecessor:** sub-project 80, which closed `app/community/routes.py` and
floored it. **65 floors.**

`app/user/routes.py` is the largest uncovered module that is not `app/cli.py`
or `app/nntp/*`: 1,319 uncovered statements of 1,579, at 12.7%. It holds the
account surface — profile, settings, blocking, reporting, deletion — so it is
where the standing priority (hosted-site security) points next.

## Scope

| function | gaps |
|---|---|
| `report_profile` | 34 |
| `block_profile` | 27 |
| `ban_profile` | 27 |
| `delete_profile` | 26 |
| `unblock_profile` | 23 |
| `user_block_instance` | 19 |
| `delete_account` | 18 |
| `send_deletion_requests` | 14 |
| `unban_profile` | 14 |
| `user_community_unblock`, `user_flair_unblock` | the rest |

## THE PRODUCTION CHANGES

### P1 — a banned account could still file reports (D1033)

Every report writes a `Report` row **and a `Notification` for every admin**.
Nothing stopped a banned account doing it:

```
PROBE u1 reports created: 1
PROBE u1 admin notifications: 1
```

This is exactly the abuse `community_report` was fixed against — the same
check, for the same reason, at the other report route.

### P2 — reporting yourself (D1034)

A self-report created a real report, notified every admin, and incremented
`user.reports` — the counter the "moderators have already assessed this"
message and the admin queue both read. Every sibling action on a profile
(block, unblock, ban, unban, delete) refuses self-targeting; this one did not.

### P3 — blocking your own instance (D1035)

`user_block_instance` never checked that the profile was remote. A local
profile's `instance_id` is this instance, so the button blocked the whole site
for the caller, and announced it as *'Content from None will be hidden.'*
because a local user has no `ap_domain`:

```
PROBE u3 block_remote_instance called with: (1, 1)
```

### P4 — the site admin could delete their own account (D1036)

`delete_account`'s "this user cannot be deleted" guard sat on the **GET branch
only**, so the refusal it states was advice rather than a rule:

```
PROBE u4 founder banned? True email: deleted_1@deleted.com
```

User 1 is the instance's first administrator, the account `delete_profile`
refuses to delete, and the one an instance cannot recover without.

### P5 — a report about a user with no instance row (D1037)

`source_instance.domain`, unguarded, on a nullable FK:
`AttributeError: 'NoneType' object has no attribute 'domain'`.

### P6 — a warning that could never fire (D1038)

`delete_profile` flashes *'Deleted user with role permissions.'* behind
`user.is_admin() or user.is_staff()`, and both walk `self.roles` — **read after
`delete_dependencies()`, which executes `DELETE FROM "user_role" WHERE user_id
= ...`.** So the warning was dead code for every account it was written for.
The flags are now read first.

## What the pins showed about one of the fixes

D1037 was written as two edits and only one of them is load-bearing: the guard
on the `.domain` read is what prevents the 500, while the companion `if
user.instance_id` before `db.session.get` only avoids a SQLAlchemy warning.
Reverting the second alone leaves every row green, and that is recorded in the
test and in the source comment rather than presented as two fixes.

## Success criteria

- The eleven functions at `[]` on the **full-suite** run.
- P1–P6 land with their pins inverted.
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D1033**; `tests/README.md` facts from **464**.
