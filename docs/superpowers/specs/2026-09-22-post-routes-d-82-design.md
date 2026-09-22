# Sub-project 82 slice D: deleting, purging, moving, reporting and blocking

**Status:** slice D complete. SEVEN production defects fixed (D1097–D1103).
**Branch:** `blentz`
**Predecessor:** slice C, which closed the edit, flair and embed routes and
fixed D1089–D1096. **66 floors, unchanged.**

## Scope

The moderation and self-service group: `post_delete`, `post_restore`,
`post_purge`, `post_reply_purge`, `post_move`, `post_report`,
`post_mea_culpa`, and the six block routes (`post_block_user`,
`post_block_domain`, `post_block_community`, `post_block_instance`,
`post_reply_block_user`, `post_reply_block_instance`).

## THE PRODUCTION CHANGES

### Anyone could silence anyone's post (D1097)

`post_mea_culpa` had **no authorization at all**. "I changed my mind" marks a
post as a mistake and **turns its comments off**; any logged-in account could
do that to anybody's post. Measured:

```
PROBE al1 stranger mea_culpa: 302 | mea_culpa now=True comments_enabled=False
```

The worst of this slice: a stranger silences the conversation under someone
else's post, and the post then carries a notice in the author's name saying
they made a mistake.

### A post could be moved into a private community (D1102)

`move_post` (`app/shared/post.py`) checked only the **source** community's
moderators. The destination was never consulted, so a moderator of any
community could move a post into any other one — including a **private**
community they do not belong to. Measured:

```
PROBE am1 mod moves into a private community: 302 | post now in theirs (private=True)
```

The guard tests the destination's **membership**, not `can_create_post`:
moving is a moderation action, and `can_create_post` would also bring in the
poster-side conditions — verification, keys, `ban_posts` — which are about
authoring a new post rather than about where an existing one may be filed.
Using it broke six rows in `tests/test_shared_post_moderation.py` that move
posts with actors who have no keys, which is how the distinction surfaced. The
guard is placed in the shared helper so the web route and the API are both
covered by one check (fact 478).

### Refusals that were 500s

* **D1098** — `post_delete` had no `else`, so a caller who is not permitted
  fell off the end: `TypeError: The view function for 'post.post_delete' did
  not return a valid response`. D1012's shape, fact 500.
* **D1099** — `post_block_domain` on a post with no domain (anything that is
  not a link) was `psycopg2.errors.NotNullViolation: null value in column
  "domain_id" of relation "domain_block" violates not-null constraint`.
* **D1100** — `request.headers.get('HX-Current-Url')` read straight into `in`
  tests at **six** more sites. D1091's shape; the whole family is now guarded.
* **D1101** — `post_block_instance` flashed "Content from … will be hidden."
  immediately after `block_remote_instance` had flashed "You cannot block the
  local instance." — two messages contradicting each other, one of them false
  — and a post with no instance was an `AttributeError` on
  `post.instance.domain`. Its twin `post_reply_block_instance` had both.

### D1103: two more sites of D1077

`post_reply_purge` and `post_reply_block_user` take a post id and a comment id
and relate them nowhere. Purging is the one deletion that cannot be undone, so
its mismatch is the one that matters: the permission is tested against
`post.community` and the row deleted is `post_reply`. **Tenth and eleventh
sites** of the shape first recorded as D1010.

## Success criteria

- The fifteen functions at `[]` on the **full-suite** run.
- D1097–D1103 land with their pins inverted.
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D1097**; `tests/README.md` facts from **518**.
- 19 mutants, 18 killed on the measuring pass; `r19` became a row (D1104) —
  the guard's third arm, "actor banned from the destination", had none.
