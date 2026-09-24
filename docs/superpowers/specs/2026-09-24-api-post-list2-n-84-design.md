# Sub-project 84 slice N: `get_post_list2`

**Status:** slice N complete. SIX production defects fixed (D1231–D1236), one
block of dead code removed, one equivalent mutant recorded.
**Branch:** `blentz`
**Predecessor:** slice M, `get_post_list`, whose shared fixes this function
already carried.
**Successor:** slice O, `get_post` and `get_post_replies` — the last 200 lines
of the module, which take the floor.

## Scope

`get_post_list2`, the keyset-paginated listing behind `/post/list2`. 500
lines. After this slice the module is at **89% with no partial branches**;
what is left is `get_post` and `get_post_replies`.

## THE DEFECTS

### Eleven sorts that could not run (D1231)

```python
posts = posts.filter(...).order_by(desc(Post.score, desc(Post.id)))
```

One call to `desc()` with two arguments. `desc()` takes one. Every `Top*` arm
in this function was written that way, so the entire score-ordered half of the
endpoint — eleven sorts, every time window — was
`TypeError: desc() takes 1 positional argument but 2 were given`, raised while
the query was being built.

### NSFW to anyone who did not ask (D1234)

The anonymous arm tested `nsfw == 'Exclude'` where `get_post_list` tests
`nsfw == 'Exclude' or nsfw == ''`. `''` is what a caller who said nothing
sends.

### The search that searched titles only (D1232)

The query was applied twice: once as `posts.search(...)`, which reads the
full-text vector, and once more as `Post.title.ilike(...)`, which ANDed a
title-only match onto it. A word in a post's body found nothing here and the
post through `get_post_list`.

### Three more

* **D1235** — the reader's `read_language_ids` filter was absent entirely.
* **D1233** — no `PAGE_LENGTH` clamp; `limit=100000` was served.
* **D1236** — `minimum_upvotes` read `up_votes - down_votes` here and `score`
  there, D1230's twin.

### The dead branch

A second `elif feed_id:` sat below the first in the same chain — thirty-one
unreachable lines. Its one difference was `segregate_instance_stickies =
False`, which the reachable branch lacked and every other narrowing branch
has. The dead copy is gone and the line is where it belongs.

## THE DUPLICATION, WHICH IS THE REAL STORY

This function applies its whole filter sequence **twice**: liked_only,
saved_only, hide_read_posts, the keyword filter and the sort chain all appear
in two copies. The filters are idempotent so the duplication is waste; the
sorts concatenate so every ORDER BY column appears twice. D1232 is what
happened when the two copies disagreed.

Left in place deliberately: merging them means choosing between the copy that
has `Old` and `Relevance` and the copy that has the keyset tiebreakers and the
poll/event exclusion, and that refactor deserves its own slice.

## THE TESTS

`tests/test_api_post_list2.py`, 90 rows — the same questions slice M asks of
`get_post_list`, so the two listings can be compared directly, plus the
keyset cursor, the poll/event exclusion and the Active sort's reply
requirement.

## THE MUTATION PASS

44 mutants, 33 killed on the measuring pass. Eight of the eleven survivors
were shadowed by the duplication and died when both copies were mutated; two
were weak rows of mine, now fixed; one is equivalent. 43 killed, 1 equivalent.

## WHAT THIS SLICE COSTS

The suite's warning count rises by ~200, all `UserWarning: Ordering by
nullable column post.X can cause rows to be incorrectly omitted` from
sqlakeyset. The warnings were always emitted in production; no test had ever
provoked them. The remedy is a migration making those seven columns
`nullable=False`, recorded in the findings.

## FLOORS

Unchanged at 78. The module takes its floor when slice O closes it.
