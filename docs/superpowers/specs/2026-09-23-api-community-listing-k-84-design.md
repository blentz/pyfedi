# Sub-project 84 slice K: the listing and lifecycle half, closing `app/api/alpha/utils/community.py`

**Status:** slice K complete. EIGHT production defects fixed (D1208–D1216),
three behaviours recorded and not repaired.
**Branch:** `blentz`
**Predecessor:** slice J, the moderating half of the same module.

## Scope

`get_community_list`, `get_community`, `post_community_follow`,
`post_community_leave_all`, `post_community_block`, `post_community`,
`put_community`, `put_community_subscribe`, `post_community_delete` — and
two guards that had to be fixed outside the module for the API to be
testable at all: `app/shared/community.py`'s `make_community` and
`app/shared/tasks/follows.py`'s `leave_community`.

The module goes from 58% (after slice J) to **99%: every statement, and every
branch but one the code keeps on purpose.**

## THE PRODUCTION CHANGES

### A banned account rejoined by asking (D1209)

`post_community_follow` checked nothing. The web path refuses the same
request twice — `communities_banned_from`, then a direct `CommunityBan` read,
both hardened earlier in this campaign as D991 — and the API refused it not
at all: the membership row went in, and the community came back into the
banned account's Subscribed listing.

This is the slice's security finding, and the shape it takes is worth a fact
(572): the rule was enforced in a ROUTE, not in the shared function both
paths call, so the second path never inherited it.

### Three account settings read as flags (D1211, D1212, D1216)

`hide_nsfw`, `hide_nsfl` and `hide_gen_ai` are four-valued — 0 Show, 1 Hide
completely, 2 Blur, 3 Semi-transparent — and the listing tested all three for
truthiness, so "show it, blurred" was read as "hide it". Two other modules
already test `== 1`.

`show_nsfl = show_nsfw` tied two separate settings together, and `show_genai`
defaulted to True, which made the gen-AI filter unreachable for every caller.

### A listing that answered a different question (D1208)

Asked for `Subscribed`, `Moderating` or `ModeratorView` without an account,
the type check fell through and answered with every community on the
instance. D1199's shape, one module along.

### Four endpoints, one unchecked id (D1210, D1213)

Follow, block, subscribe and delete each handed a community id straight to a
shared function. The block was the worst: an insert against a community that
does not exist, so the caller got `ForeignKeyViolation` with the SQL in the
message and the rest of the request's session was poisoned behind it.

### A community with no name (D1214)

`slugify('---')` is `''`, and the empty name was accepted — a community whose
every link is the bare `/c/` prefix. Fixed in `app/shared/community.py`, so
the web path is covered too.

### The unfollow that could never federate (D1215)

`leave_community`'s task read `join_request.uuid` off a `.first()`, and
`join_community` writes that row only when the remote instance was online at
the time. Every leave without one was an `AttributeError` that took the Undo
Follow with it. The user-follow task three functions below already handles
the same case with a fresh `gibberish(15)` id; this now matches it.

## RECORDED, NOT REPAIRED

* `ListCommunitiesRequest` declares neither `q` nor `show_genai`, and
  `DefaultSchema` excludes unknown keys, so the search block — including the
  `!name@host` resolver call — cannot be reached over HTTP at all.
* Nothing consults `Community.invitations` when joining, on either path: a
  community set to "must be invited by the owner" is joined by anyone who
  asks. Closing that belongs in the shared join, as a product decision.
* One branch in `post_community_leave_all` is dead against the query that
  feeds it, and is kept because that query is memoized for a day.

## THE TESTS

`tests/test_api_community_listing.py`, 81 rows. The listing's filters are
each pinned by what they EXCLUDE (fact 562), the visibility settings are
parametrised across all four values, and the sorts are pinned with
`last_active` running the OTHER way from `created_at` so the default ordering
cannot agree with `New` by accident.

## THE MUTATION PASS

47 mutants, 42 killed on the measuring pass. Five survivors, all rows of
mine: two sorts whose ordering the default sort reproduced, a resolver row
that asserted the call and not the answer, and two picture rows built on a
`File` with no `source_url`, which made the endpoint's value and `None`
compare the same way. 47/47 after.

## FLOORS

`app/api/alpha/utils/community.py` takes its first floor here. 78 floors.
