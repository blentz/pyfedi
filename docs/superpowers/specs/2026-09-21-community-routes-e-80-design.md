# Sub-project 80 slice E: `add_post` — the posting surface

**Status:** slice E complete. SIX production defects fixed (D998–D1003); the
design anticipated three.
**Branch:** `blentz`
**Predecessor:** slice D, which fixed D991–D993. **64 floors.**

## Scope

| function | gaps |
|---|---|
| `add_post` | 69 |

One function, but the widest surface in the blueprint: six post types, six
form classes, an upload dispatch, a cross-post prefill and a query-parameter
prefill.

## THE PRODUCTION CHANGES

### P1 — the cross-post form leaked a private community's posts (D998)

`add_post?source=<id>` copies the source post's title, body, url and tags into
the new-post form. Nothing checked who was asking. Measured: a non-member read
the title, body and url of a post in a private, local-only community by opening
the cross-post form for a community they could post to.

**Fix:** the condition `app/post/routes.py:102` uses to guard the post page
itself — `source_post.community.private and source_post.community_id not in
community_membership_private(current_user.id)` → `abort(403)`.

### P2 — two more `None` dereferences (D999)

D992's shape, fourth and fifth instances in this file. `actor_to_community`
returns `None` for an actor it cannot resolve and the next lines read
`community.default_post_type`; `db.session.get(Post, request.args.get('source'))`
returns `None` for a stale cross-post link and the next line reads
`source_post.deleted`.

### P3 — the failure path echoed the exception (D1000)

`make_post` reaches image processing, remote fetches and the plugin hooks, so
`str(ex)` can name a filesystem path, a relay host or a library internal, and
it went straight onto the page. Same class as D895 and D950. The detail now
goes to the log and the user gets a fixed message.

### P4 — `allow_local_image_posts = False` did nothing (D1001)

`CreateImageForm.validate` appended 'Images cannot be posted to local
communities.' to `communities.errors` and then **returned True**. Measured with
the setting off, against a local community:

```
PROBE i1 community is_local: True
PROBE i1 make_post called? True
```

The same four lines appear verbatim in `EditImageForm.validate` and
`CreateEventForm.validate`; all three were inert, and all three now return
False. An instance that has turned local image hosting off was still hosting
images.

### P5 — a refused post lost what was typed (D1002)

`add_post`'s POST branch ended with `else:` rather than `elif request.method ==
'GET':`. **D907's shape, the fifth instance the campaign has found**, and every
one was found by covering the function rather than by reading it. Measured:

```
PROBE g1 submitted language 3 redisplayed as 2
PROBE g1 submitted timezone Europe/London redisplayed as Europe/Paris
PROBE g1 submitted notify_author off, redisplayed as True
```

The cross-post and `?link=` prefills sit in that same branch, so a refused
submission also had its title and body overwritten.

### P6 — the poll form could not see two thirds of its own fields (D1003)

`CreatePollForm.validate` counted `range(1, 10)` of the **fifteen** choice
fields it declares, while `make_post` reads `range(1, 16)`. A poll whose
options were typed into choices 10–15 was refused for having none, while
showing six of them. The `choices_made > 15` branch below could never fire —
fifteen fields cannot produce sixteen choices — so it was removed.

The same line was `.data.strip()` on a field WTForms leaves at `None` when it
is not submitted:

```
PROBE p3 RAISED: AttributeError 'NoneType' object has no attribute 'strip'
```

A 500 for any poll submission that did not carry all fifteen fields. The
browser form does; nothing else has to.

## What the slice found beyond the design

P4, P5 and P6 were all found the same way: a row that could not be made to pass
led to a probe, and the probe measured a defect. The upload-dispatch rows in
particular only ran once each post type's own required fields were supplied,
and supplying them is what put the poll and image validators under a client
that was not the browser form.

## Success criteria

- `add_post` at `[]` on the **full-suite** run.
- No floor yet on `app/community/routes.py`.
- P1–P6 land with their pins inverted — eight inversions, one per edit.
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D998**; `tests/README.md` facts from **432**.
