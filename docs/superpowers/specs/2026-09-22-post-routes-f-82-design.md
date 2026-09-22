# Sub-project 82 slice F: the rest of `app/post/routes.py`, and the floor

**Status:** slice F complete. NINE production defects fixed (D1119–D1127). **This is the last slice of the module**, so its floor is taken
in this round.
**Branch:** `blentz`
**Predecessor:** slice E, which closed the fragments and fixed D1105–D1115.
**66 floors, rising to 67.**

## Scope

The thirty-nine functions left: the preview endpoint, the reminder pair, the
blocked-image pair, the emoji routes, voting and bookmarks, notifications,
the translate trio, the voting-activity pages, sticky and lock and hide, the
oembed document, `post_options`, `post_fixup_from_remote` and
`post_share_mastodon`.

## THE PRODUCTION CHANGES

### The oembed document, and the options menu (D1119, D1123)

`/post/<id>/oembed` is what a chat client or a link preview fetches, which
makes it the widest audience any of these routes has. It carried a **private**
community's post title and its author's name to anyone. Measured:

```
PROBE aq1 oembed of a private post: 200 | title: True
```

`post_options` is the same miss one screen away: `post_reply_options` got its
check in slice A and the post-level twin did not.

### Translation is a read surface (D1124)

`post_translate`, `post_teaser_translate` and `post_reply_translate` hand back
the text they were given — and send it to the configured LibreTranslate
endpoint on the way. None of the three asked whose community it is, so a
private community's post both reached the caller and **left the instance**.

### A moderator could speak for a community they do not moderate (D1120)

`post_reply_distinguish` tests `post.community.is_moderator()` and sets the
flag on `post_reply`. D1077's **twelfth** site: a moderator of one community
marked their own comment in another — a private one, in the measurement — as
speaking for that community's moderators.

```
PROBE aq2 distinguish across communities: 302 | distinguished now=True
```

### An open redirect in the share button (D1121)

`ShareMastodonForm.domain` carries a `Length(max=512)` and nothing else, and
its value went straight into the host part of a redirect:

```
PROBE aq3 share to an arbitrary host: 302 ->
https://evil.example/phish?x=/share?text=MINE&url=https://test.piefed.localNone
```

Anything typed — or carried in on a crafted link — became the destination, and
the value is also stored in a cookie that expires in 2099. A domain is a
hostname: no scheme, no path, no credentials. The same measurement shows the
other end of that URL: `post.slug` is None until the post has one, and an
f-string writes it out as the four characters `None`.

### A poll vote with nothing ticked (D1122)

`int(request.form.get('poll_choice'))` — which is what a poll form sends when
nobody ticks anything. `TypeError: int() argument must be a string, a
bytes-like object or a real number, not 'NoneType'`.

### Three more found while writing the rows

* **D1125** — `bookmark_post` called `mark_post_read` **before** anything
  checked the post exists, and `read_posts.read_post_id` is a foreign key: an
  id that does not resolve was `psycopg2.errors.ForeignKeyViolation` although
  the route around it already catches `NoResultFound` to answer 404. Its three
  siblings — the remove twin and both comment-level helpers — never raised at
  all, so the routes' 404 arms were unreachable. All four now raise what the
  routes already catch.
* **D1126** — D1113's shape on the three translate routes: the whole body sits
  inside `if current_app.config['TRANSLATE_ENDPOINT']:` with no else, so an
  instance that has not configured a translator answered `TypeError: The view
  function ... did not return a valid response`.
* **D1127** — `post_set_ai` returned **'Done'** to a caller who is not
  permitted, with nothing written: a refusal reported as a success. It also
  read `post.user_id` off a `db.session.get` that can answer None.

## The floor

`app/post/routes.py` went from **14.5%** at the start of this sub-project to
its final figure here, across six slices and **51 production defects**
(D1077–D1127). The floor is taken at the measured combined statement+branch
figure, as `app/community/routes.py`'s and `app/user/routes.py`'s were.

## Success criteria

- The thirty-nine functions at `[]` — **`app/post/routes.py` measures 0
  functions with gaps and 98.6% combined** across the six slices' files.
- D1119–D1127 land with their pins inverted (19 rows red without them).
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D1119**; `tests/README.md` facts from **523**.
