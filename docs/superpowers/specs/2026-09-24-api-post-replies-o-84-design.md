# Sub-project 84 slice O: `get_post` and `get_post_replies`, closing `app/api/alpha/utils/post.py`

**Status:** slice O complete, and the module with it. FIVE production defects
fixed (D1237–D1241), two equivalent mutants recorded (D1242).
**Branch:** `blentz`
**Predecessor:** slice N, `get_post_list2`.

## Scope

`get_post` and `get_post_replies` — the comment tree, its depth filter and its
cursor paginator — plus one fix in `app/post/util.py`, which this endpoint
shares with the web UI.

The module closes at **every statement covered and one partial branch**,
floored at 99. That branch is `if included_branches:` in the cursor
paginator: nothing can make it false, because the empty-tree case returns
earlier and the loop always admits its first branch, but the body indexes
`reply_tree` and would be wrong if it ever were. Kept.

## THE DEFECTS

### The moderator check that asked about the web session (D1241)

```python
if viewer.reply_hide_threshold and not (viewer.is_admin_or_staff()
                                        or post.community.is_moderator()):
```

`is_moderator()` with no argument falls back to `current_user.get_id()`. In
the API path the viewer is a `User` the caller was handed, not a session — so
an authenticated reader who moderates the community was treated as an outsider
and had the reply-hide threshold applied anyway. Called with no request
context at all, which is how the tests reach it, `current_user` is None and
the line raises. Both sites in `app/post/util.py` now pass `viewer`; the web
callers already hand in `current_user`, so nothing changes for them.

This is the kind of defect only a test that calls a shared helper directly can
find, and it sat in a function the web UI has exercised for years.

### The depth nobody could ask for (D1237)

`if max_depth:` skipped the filter for `max_depth=0` — "the top level and
nothing under it", which is how a collapsed thread is drawn. The nested
function it guards had the right test (`if max_depth is None`) where it could
never see a zero.

### The replies to no post (D1238)

A request naming neither a post nor a parent handed None to
`db.session.get(Post, None)` and died inside `post_replies`. An unknown post
or parent id took the same route.

### One post, two ways of asking badly (D1239, D1240)

`get_post` answered the wordless `NoResultFound: ()` for an unknown id, and
`invalid literal for int() with base 10: 'abc'` for one that is not a number.

## THE TESTS

`tests/test_api_post_replies.py`, 41 rows: the whole thread, each depth, a
branch from any comment and how its depth is counted, the cursor paginator
(including that a branch is never split across pages), the reader's own vote,
bookmark and ban state, what only the top level carries, and every sort.

## THE MUTATION PASS

29 mutants, 25 killed on the measuring pass. Four survivors: one was my own
occurrence-counting mistake, one a branch-depth row rooted where the two ways
of counting agree, and two are equivalent prefetches. 27 killed, 2 equivalent.

## THE MODULE

Four slices, 1,943 lines, 381 rows, twenty-six defects (D1217–D1242). Floored
at 99.

## FLOORS

`app/api/alpha/utils/post.py = 99`. 79 floors.
