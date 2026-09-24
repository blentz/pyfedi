# Sub-project 84 slice M: `get_post_list`

**Status:** slice M complete. SEVEN production defects fixed
(D1223–D1228, D1230), three equivalent mutants recorded (D1229).
**Branch:** `blentz`
**Predecessor:** slice L, the actions half of the same module.
**Successor:** slice N, `get_post_list2` — a near-copy of this function, which
already carries most of these fixes — and then `get_post_replies`, which
closes the module and takes the floor.

## Scope

`get_post_list` and the three prefetch helpers that feed it
(`get_post_votes_for_posts`, `get_post_unread_counts`,
`get_post_interacted_at`). 590 lines: every statement and every branch is now
covered.

## THE FUNCTION'S SHAPE, WHICH IS ALSO ITS PROBLEM

`get_post_list` builds **two queries in parallel** — a sqlalchemy one in
`posts` and a raw SQL string in `post_query_criteria` — and `use_faster_query`
decides which one actually runs. Every filter therefore has to be written
twice, and a filter written once is silently dropped for half the requests.

Two of this slice's defects are exactly that, and one of them is the worst
thing this campaign has found in the API:

### Private communities on the front page (D1227)

```python
posts = posts.filter(or_(Community.private == False,
                         Community.id.in_(private_community_ids)))
if private_community_ids:
    post_query_criteria.append('(c.private is false OR community_id IN :private_community_ids)')
```

The sqlalchemy filter is unconditional. The SQL criterion was appended **only
when the reader already belonged to a private community** — and an anonymous
caller belongs to none. So `GET /post/list`, the default listing, with no
authentication, answered with every private community's posts on the instance.

### The URL search that searched nothing (D1228)

`search_type == 'Url'` filtered the sqlalchemy query and left the fast path
switched on, so the SQL ran with no url condition: a URL search answered with
everything.

## THE OTHER FIVE

* **D1224** — `has_next_page = len(post_ids) > page + 1 * limit`. `*` binds
  tighter than `+`. Page 6 of a nine-post listing came back empty and named
  page 7.
* **D1225** — `' ORDER BY ' + ', '.join(sql_order_by)` with nothing to join.
  Reachable by `GET /search?type_=Url&sort=Relevance` and by any unrecognised
  sort with `ignore_sticky` set; both were
  `psycopg2.errors.SyntaxError: syntax error at or near "LIMIT"`.
* **D1223** — 'Subscribed', 'Moderating' and 'ModeratorView' asked without an
  account fell through to the All arm. D1199 and D1208's shape, third module.
* **D1226** — a feed or topic id nobody holds was an AttributeError, at five
  sites across the two listing functions.
* **D1230** — `minimum_upvotes` compared `up_votes - down_votes` in one query
  and `score` in the other, so the same request answered differently
  depending on which path ran.

Fixed in `get_post_list2` at the same time wherever it shares the code, which
is everywhere except the pagination.

## THE TESTS

`tests/test_api_post_list.py`, 117 rows. Every filter that exists in both
queries is asked **both ways** — the front page for the raw SQL, a
community-narrowed listing for sqlalchemy — because a filter present in one
and missing from the other is precisely this module's failure mode.

The sort fixture carries one post per time window, so that no Top* arm is
indistinguishable from the one-day fallback the chain ends in, and gives the
eleven posts five different orderings built in a sixth, so that no assertion
can be satisfied by the order the database happens to return.

## THE MUTATION PASS

63 mutants, 45 killed on the measuring pass. Eighteen survivors, every one a
row of mine, in three groups: sorts that agreed with the fallback, orderings
read on the path that discards them, and filters read on the path that does
not carry them. 60 killed and 3 equivalent afterwards.

## RECORDED, NOT REPAIRED

Instance stickies do not lead the front page, though `sql_order_by` starts
with `instance_sticky DESC`: the page is re-sorted by `post_ids_to_models`,
which knows nothing about stickies. The community-narrowed path does order by
sticky, and that is pinned. Fixing the other one means changing a shared
helper in `app/utils.py`.

## FLOORS

Unchanged at 78. The module takes its floor when `get_post_list2` and
`get_post_replies` are closed.
