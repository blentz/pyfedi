# Sub-project 82 slice E: the fragments — ical, lazy replies, previews, AI checks

**Status:** slice E complete. ELEVEN production defects fixed (D1105–D1115).
**Branch:** `blentz`
**Predecessor:** slice D, which closed the moderation group and fixed
D1097–D1103. **66 floors, unchanged.**

## Scope

The remaining large functions: `post_lazy_replies`, `post_check_ai`,
`post_reply_check_ai`, `post_search_community_suggestions`, `post_cross_post`,
`preview`, `show_post_ical`, `post_fixup_from_remote`, `post_source`,
`post_reply_source`, `post_reminder`, `post_reply_reminder`,
`post_block_image` and `post_block_image_purge_posts`.

Everything in it is a **fragment or a side door** — a page the main post view
delegates to, or an endpoint the front end calls directly. That is the theme
of the round: the checks live on the page, and the fragments it delegates to
did not repeat them.

## THE PRODUCTION CHANGES

### Four more ways to read a private community (D1105, D1106, D1109, D1111)

| Route | What it handed out | Measured |
|---|---|---|
| `/post/<id>/ical` | a private event's title, description and start time, **to anyone** — the route carries no access decorator at all | `PROBE an1 ical of a private event: 200 \| title: True` |
| `/post/<id>/lazy_replies/<nonce>` | a private community's **whole comment thread**; this is where `show_post` defers the thread past a hundred comments | `PROBE an2 lazy_replies of a private post: 200 \| reply: True` |
| `/post/<id>/cross-post` | the post itself, to a non-member — D998 fixed the community list on this same form and not this end of it | `PROBE ao1 cross-post of a private post: 200 \| post handed to the template: SECRETTITLE` |
| `/post/search_community_suggestions` | **private communities by name**, unauthenticated — a private community's existence is what its membership withholds | `PROBE ap1 anonymous suggestions: 200 \| private named: True` |

Fact 478 for the fifth, sixth, seventh and eighth time in this sub-project.
The helpers from slices A–C (`refuse_private_community`,
`refuse_unpublished_post`) are what each of the first three now calls; the
fourth is a filter on the query.

### An unauthenticated outbound request (D1107)

`post_check_ai` and `post_reply_check_ai` carried **no decorator**. Each call
makes this instance issue a request to the configured detector, so an
anonymous caller could drive one outbound request per HTTP request, with no
rate limit of its own. D1025's shape — the unauthenticated URL fetcher found
in `app/community/routes.py`.

### Two more `db.session.get` without a guard (D1108)

Both AI routes then read `post.ap_id` / `post_reply.body` off the result:
`AttributeError: 'NoneType' object has no attribute 'ap_id'` for an id that
does not resolve. D992's shape.

### A cookie read as an integer (D1110)

`post_cross_post` did
`db.session.get(Community, int(request.cookies.get('cross_post_community_id')))`.
A cookie is whatever the caller sends: `int('banana')` is `ValueError:
invalid literal for int() with base 10: 'banana'`, and an id that no longer
resolves is an `AttributeError` on `.lemmy_link()`. Both were 500s on a page
that has a perfectly good answer without the convenience — an empty field.

## Success criteria

- The fourteen functions at `[]` on the **full-suite** run.
- D1105–D1115 land with their pins inverted (24 rows red without them).
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D1105**; `tests/README.md` facts from **521**.
- 20 mutants, 16 killed on the measuring pass; all four survivors were rows
  asserting the right thing for the wrong reason (D1116–D1118).

### Four more found while writing the rows

* **D1113** — both AI routes are a chain of `if`s with a return for the happy
  path only, so a detector answering 502 fell off the end of the view.
* **D1114** — `search_for_community` unpacks `name@server`, and the cross-post
  form submits whatever was typed: a local community's name was a `ValueError`.
  `post_move` normalises the same input one screen away.
* **D1115** — the `OPTIONS` arm at the top of `post_lazy_replies` can never
  run, because `before_request` answers every OPTIONS first.
* **D1112** — the markdown-source fragments, found by asking what `post.body`
  is for a link post.
