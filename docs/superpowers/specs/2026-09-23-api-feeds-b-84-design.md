# Sub-project 84 slice B: `app/api/alpha/utils/feed.py`

**Status:** slice B complete. TWO production defects fixed (D1173, D1174).
**Branch:** `blentz`
**Predecessor:** slice A, which closed the private-message API and fixed
D1167–D1172. **73 floors, rising to 74.**

## Scope

The whole module: `get_feed_list`, `get_feed`, `post_feed_follow`,
`post_feed`, `put_feed`, `post_feed_delete`.

`app/api/alpha/utils/feed.py` was **7.3%** and is now **100%**.

## THE PRODUCTION CHANGES

### Following a private feed was a way into it (D1173)

`get_feed` refuses a private feed to anybody but its owner:

```
PROBE bb1 outcome: Exception: access_denied
```

`post_feed_follow`, one function below it, took the id and joined:

```
PROBE bb2 outcome: accepted | member now: True
```

That is not merely an unwanted membership row. `show_feed`
(app/feed/routes.py:447) reads:

```python
if not feed.public:
    if current_user.is_authenticated and current_user.id == feed.user_id:
        ...
    elif current_user.is_authenticated and feed.subscribed(current_user.id):
        ...
```

— a private feed is served to **anyone who is subscribed**. So the API's
follow endpoint flipped exactly the predicate the page branches on:

```
PROBE bb7 subscribed before: 0 | after: 1
```

`edit_feed` unsubscribes every non-owner member when a feed is made private
(app/shared/feed.py:382), which is the codebase's own statement that
membership of a private feed **is** access to it.

Leaving stays ungated: somebody already in a feed that has since been made
private must still be able to get out, and the guard is on `follow` only.

### A bare feed name (D1174)

```python
parts = name.split('@')
feed = Feed.query.filter(Feed.name == parts[0], Feed.ap_domain == parts[1])
```

`name` is whatever the caller sent.

```
PROBE bb4 outcome: IndexError: list index out of range
```

## What the coverage found without a defect

`feed_view` builds a private feed's `actor_id` as
`feed.public_url() + "/" + feed.name.rsplit("/", 1)[1]`, which looks like an
IndexError waiting for a private feed with no `/` in its name — but there is
no such feed: `post_feed` names a private one `<url>/<owner>`, and `edit_feed`
renames to that shape when a feed is made private. A probe confirmed the
rename (`PROBE bc1 name now: 'plainname/user2'`). The first draft of the
fixture built the unreachable state and was corrected rather than the code.

## The mutation pass

22 mutants, 20 killed on the measuring pass. **Both survivors were rows of
mine that could not fail**, which is the third round running that the pass has
found a test rather than a gap:

* the nsfw row asserted on a flag `edit_feed` writes only when
  `g.site.enable_nsfw` is set — and the fixture's Site did not set it, so the
  assertion held whatever the code did (fact 551);
* the "leaving is always allowed" row made the feed private with an attribute
  write **after** calling `post_feed_follow`. `join_feed` ends with `finally:
  db.session.remove()`, discarding the whole scoped session, so every object
  the test held was detached and the write never reached the row (fact 552).
  The endpoint re-read a feed that was still public and the row proved
  nothing.

Both rewritten; 22/22.

## Success criteria

- `app/api/alpha/utils/feed.py` at **100%**, 0 functions with gaps.
- D1173 and D1174 land with their pins inverted.
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D1173**; `tests/README.md` facts from **551**.
