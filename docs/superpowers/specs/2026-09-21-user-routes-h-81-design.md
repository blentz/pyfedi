# Sub-project 81 slice H: the rest of `app/user/routes.py`, and the floor

**Status:** slice H complete. ONE production defect fixed (D1074), ONE
registered and pinned (D1073). **This is the last slice of the module**, so its
floor is taken in this round.
**Branch:** `blentz`
**Predecessor:** slice G, which fixed D1071–D1072. **65 floors, rising to 66.**

## Scope

The fifteen functions left: the reading history and its deletion, hidden posts,
scheduled posts, upvotes, the two feed pages, the profile preview, the remote
lookup, follow / unfollow, the bot challenge, the OAuth connections page and
the people redirect.

## THE PRODUCTION CHANGE

### A guard in the wrong place (D1074)

`user_upvotes` called `_get_user_upvoted_posts(user)` — which reads `user.id` —
**three lines above its own `if user is not None:` check**. So an actor this
instance cannot resolve was

```
AttributeError: 'NoneType' object has no attribute 'id'
```

rather than the 404 the function goes on to produce. D992's shape with a twist:
the guard existed, in the wrong order.

### A sort the settings page offers, that matched no arm (D1076)

`user_read_posts`'s ascending arm was `elif sort == 'oldest':`, and the page's
nav links exactly that — but `sort` defaults to `current_user.default_sort`,
whose choices call the same order **`'old'`**. An account whose default sort is
"Old" therefore matched no arm and got its reading history with **no `ORDER
BY`**.

Found only because a mutation on that arm survived: the row was asking for
`/read-posts/old`, which reached no `order_by` to mutate. The row also had to
be turned around — it created the oldest post first, so insertion order
happened to look sorted (facts 499, 500).

## Registered and pinned

**D1073** — an account created through OAuth never gets a password
(`initialize_new_user` does not call `set_password`), and `connect_oauth` will
disconnect its only provider. Measured:

```
PROBE ab1 google id now: None | password_hash: None
```

The account is recoverable — OAuth signups store a verified address, so an
emailed reset works — but the state is one click away and nothing warns.
Refusing the disconnect is a login-flow decision rather than a coverage fix, so
this round **pins today's behaviour** in a row whose assertion carries "update
this test (D1073)". That is the pattern this campaign inherited for D1001, and
D1001 was found and fixed precisely because its pin turned red.

## What the coverage found without a defect

`GET /read-posts/delete` does not answer 405 although the delete route is
POST-only: `/read-posts/<sort>` is registered on the same prefix, so the GET is
read as a sort named "delete" and renders the history unsorted. The history
survives, which is what the row asserts. Fact 495.

## The floor

`app/user/routes.py` went from **12.7%** at slice A to its final figure here,
across eight slices and **22 production defects**. The floor is taken at the
measured combined statement+branch figure, as `app/community/routes.py`'s was.

## Success criteria

- The fifteen functions at `[]` on the **full-suite** run.
- D1074 lands with its pin inverted; D1073 pinned as it stands.
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D1073**; `tests/README.md` facts from **495**.
