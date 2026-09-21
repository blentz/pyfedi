# Sub-project 82 slice B: `show_post`

**Status:** slice B complete. THREE production defects fixed (D1084, D1086,
D1087), ONE registered and pinned (D1085).
**Branch:** `blentz`
**Predecessor:** slice A, which closed the eight comment routes and fixed
D1077 and D1078. **66 floors, unchanged** — `app/post/routes.py`'s floor is
taken when the module closes.

## Scope

`show_post` — 185 uncovered statements, the largest function in the module and
the page every other post route leads to. It is not a route of its own: it is
called by `activitypub.post_ap` (`/post/<id>`), `post_nice` (`/c/<community>/
p/<id>/<slug>`) and the community post routes, which is why an access check
missing here is missing on every one of those URLs.

## THE PRODUCTION CHANGE

### A scheduled post was readable by anyone who guessed the id (D1084)

`post.status` is `POST_STATUS_SCHEDULED` (`-2`) until its publication time
arrives. Two other places in the application treat that as private:

* the scheduled-posts page is scoped to `Post.user_id == current_user.id`
  (`app/user/routes.py:2040`);
* the ActivityPub representation of the same post answers **403** for
  `post.status < POST_STATUS_PUBLISHED` (`app/activitypub/routes.py:2179`).

`show_post` rendered it in full to an anonymous visitor. Measured:

```
PROBE af2 scheduled status: 200 | body visible: True
PROBE af2b title visible: True
```

Post ids are sequential, so an embargoed post — an announcement timed for a
release, a moderation notice — was readable ahead of its time by walking ids.
Fact 478 for the third time in two slices: the same content has more than one
end, and only some of them were guarded.

The guard lets the author, the community's moderators and staff through, which
is what makes the existing "This post is scheduled to be published at …" flash
reachable at all.

### The ActivityPub discovery header, deleted by the line after it (D1086)

`response.headers.set('Link', ...)` twice. `set` replaces **every** value for
that name, so the oembed alternate deleted the ActivityPub one -- and both of
them together deleted the framework's `preload` hints. Measured:

```
PROBE ah1 Link headers: ['<.../oembed>; rel="alternate"; type="application/json+oembed"',
                         '<.../rsl.xml>; rel="license"; type="application/rsl+xml"']
```

The licence link survives only because `after_request` uses `add`. Fact 509.

### "Hide read posts" marked nothing (D1087)

```python
main_post_id = [post.id] + post.cross_posts if post.cross_posts is not None else []
```

The conditional binds looser than `+`, so this is `([post.id] +
post.cross_posts) if ... else []`: a post with **no** cross-posts -- nearly all
of them -- called `mark_post_read([], True, user_id)`. The setting did nothing
and the page looked right. Fact 511.

## Registered and pinned

**D1085** — a post its **author** deleted still renders: status 200, title
visible, and the whole comment thread with it; only the body is withheld by
the template. A post a **moderator** deleted is 404 for the same visitor, and
`continue_discussion` answers 404 for both. Measured:

```
PROBE af1 self-deleted status: 200 | body visible: False
PROBE af4 self-deleted title visible: True | reply visible: True
PROBE af3 mod-deleted status: 404 | body visible: False
```

The flash on that path — "This post has been deleted by the author." — is
written for a general reader, so the tombstone looks deliberate; but nothing
says the title and the replies should outlive the deletion, and the other end
of the same feature disagrees. Which end is correct is a product decision
about what deletion means, not a coverage fix, so this round **pins today's
behaviour** in a row whose assertion carries "update this test (D1085)". The
D1001 pattern, and D1001 was found and fixed because its pin turned red.

## Success criteria

- `show_post` at `[]` on the **full-suite** run.
- D1084 lands with its pin inverted; D1085 pinned as it stands.
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D1084**; `tests/README.md` facts from **508**.
- 20 mutants, 19 killed on the measuring pass; `p4` became a row (D1088) once
  fact 514 explained why the obvious row could not be written.
