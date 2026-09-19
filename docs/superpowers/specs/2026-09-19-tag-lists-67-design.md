# Sub-project 67: `app/tag/routes.py` — the lists, the ban pair, and the post list

**Status:** design approved, ready to implement
**Branch:** `blentz`, at `1bf93f295`
**Predecessor:** sub-project 66, which covered the tag page and its feed — 45 floors.

## Goal

Cover the five remaining functions that are not the tag cloud — `tags`,
`tags_blocked_list`, `tag_ban`, `tag_unban` and `tag_posts` — and repair the
five defects probed while scoping them. The floor rises; `app/tag` closes in 68
with `tag_cloud`.

## Targets

From the full-suite JSON at `1bf93f295` (`app/tag/routes.py` 47.688%, 119
missing statements):

| function | where | missing statements |
|---|---|---|
| `tags` | `:169-191` | 13 |
| `tags_blocked_list` | `:194-213` | 12 |
| `tag_ban` | `:216-226` | 6 |
| `tag_unban` | `:229-238` | 6 |
| `tag_posts` | `:342-405` | 40 |
| **total** | | **77** |

`tag_cloud` (`:241-339`, 42 statements) is 68's and is not touched here.

## THE FIVE PRODUCTION CHANGES

### P1 — banning a tag that does not exist is a 500

```python
def tag_ban(tag):
    tag = Tag.query.filter(Tag.name == tag.lower()).first()
    if tag:
        ...
        return redirect(url_for('tag.tags'))
```

There is no `else`. A miss falls off the end of the function and returns `None`:

```
PROBE k2 exception: TypeError The view function for 'tag.tag_ban' did not
return a valid response. The function either returned None or ended without a
return statement.
PROBE n1 exception: ... for 'tag.tag_unban' ...
```

Both halves of the pair have it. The route is POST-only behind
`permission_required('manage users')`, so the request that reaches it comes
from a moderator's own page — and a tag banned by someone else a moment earlier
is exactly the ordinary race that produces the miss.

**Fix:** `abort(404)`, which is what every other lookup in this module does with
a tag it cannot find.

### P2 — paging the banned list drops you into the unbanned one

```python
# in tags_blocked_list
next_url = url_for('tag.tags', page=tags.next_num) if tags.has_next else None
prev_url = url_for('tag.tags', page=tags.prev_num) if tags.has_prev and page != 1 else None
```

Copied from `tags` and never re-pointed.

**Probe** — 101 banned tags:

```
PROBE k3 next_url: /tags?page=2
```

Page 2 of "tags blocked on this instance" is page 2 of "all known tags", which
is a different list with different contents. An admin auditing bans past the
hundredth silently reads the wrong one.

**Fix:** `url_for('tag.tags_blocked_list', ...)`. Note the search term is also
dropped from both links in both functions — registered as R1, since it is one
decision for the whole module and 68 meets it again.

### P3 — the tag's post list shows private communities to anyone

`show_tag` filters `Community.private` against the reader's memberships and
`Post.private` outright (`:37-38`, `:58`, `:60`). `tag_posts` — the same tag's
posts, served to the same readers — filters **neither**.

**Probe** — one post in a community with `private = True`, requested
anonymously:

```
PROBE k4 status: 200
PROBE k4 posts: ['post 0']
PROBE k5 microblog posts: 1      (tag_posts)
PROBE k5 show_tag posts: 0       (show_tag, same data)
```

A private community is invite-only and is real access control
(`tests/README.md`, "Three different tables have a `private` column"). This
route hands its posts to an anonymous request.

**Fix:** the two filters `show_tag` already applies — `Post.private == False` in
the base query, and the membership-aware community filter for an authenticated
reader with `Community.private == False` for an anonymous one.

### P4 — three crafted query parameters are three 500s

```python
if community_id := request.args.get('community_id'):
    posts = posts.filter(Post.community_id == int(community_id))
if topic_id := request.args.get('topic_id'):
    topic = Topic.query.get(topic_id)
    if topic.show_posts_in_children:
if feed_id := request.args.get('feed_id'):
    feed = Feed.query.get(feed_id)
    if feed.show_posts_in_children:
```

**Probes:**

```
PROBE n3 exception: ValueError invalid literal for int() with base 10: 'abc'
PROBE k7 exception: AttributeError 'NoneType' object has no attribute 'show_posts_in_children'
PROBE n4 exception: AttributeError 'NoneType' object has no attribute 'show_posts_in_children'
```

`show_tag` reaches for `get_or_404` on the same two lookups (`:67`, `:81`) and
takes `category_id` through `type=int`; this function does neither.

**Fix:** `type=int` for `community_id`, `get_or_404` for the topic and the feed.
That matches the sibling exactly, and a crafted query earns a 404 rather than a
traceback — the ruling sub-projects 60 and 62 already made for the same shape.

### P5 — the ban tells the moderator content was deleted, and it was not

```python
tag.banned = True
db.session.commit()
# tag.purge_content()
flash(_('%(name)s banned for all users and all content deleted.', name=tag.name))
```

`purge_content()` is commented out. The flash is not.

**Probe** — one post carrying the tag, then a ban:

```
PROBE n2 tag banned: True
PROBE n2 post still there: True
```

The moderator is told the content is gone and it is not. This is the failure
mode D813 named — an action whose only observable is a message that does not
match what happened — and it is worse here, because the reader acts on it: a
moderator who believes the posts are deleted does not go and delete them.

**Fix:** say what the ban does. The commented-out call is left exactly as it is;
whether banning a tag should purge is a product decision and is registered as
R2.

## Registered, not fixed

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 | `:186-187`, `:210-211` | **Both lists drop `search` from their pagination links.** Page 2 of a search is page 2 of everything, which is the same class of defect as P2 but a different decision: P2 points a link at the wrong ENDPOINT, this one drops a filter the endpoint still accepts. | One decision for the whole module, and `tag_cloud` (68) drops `view` from its own links the same way. Registered so 68 settles both. |
| R2 | `:224` | **`# tag.purge_content()`** — whether banning a tag deletes the posts carrying it. | A product and moderation decision, not a coverage one. P5 only stops the message claiming it already happened. |
| R3 | `:176`, `:200` | **The search term is interpolated straight into an `ilike` pattern**, so `%` and `_` from the query string are wildcards: `PROBE n5 wildcard hits: ['rain', 'solarstorm']` for `search=%`. Not injection — the value is still bound — but the user controls the pattern. | It is a search box; a wildcard reaching the pattern is arguably the feature. Registered so the next reader does not have to re-derive that it is deliberate-looking. |
| R4 | `:342` | **`tag_posts` takes a `tag_id` and never checks the tag exists or is unbanned**, so `/tags/posts/999999` renders an empty list with a 200, and a banned tag's posts are still listed by id. | `show_tag` refuses a banned tag only by never linking it; the id route has no such shield. Whether a banned tag's posts should be reachable by id is R2's decision. |

## Success criteria

- All five functions at `[]`/`[]` on the **full-suite** run, except arcs
  declared unreachable with a named cause and a proof.
- `app/tag/routes.py`'s floor rises from 47 to the measured figure rounded down.
- All five repairs land with pins inverted; `git diff --numstat` names exactly
  `app/tag/routes.py`.
- Suite green; floors checked with `&&`; a mutation pass over the five
  functions, per D602, with anchors checked for uniqueness first — D824.
- Findings registered from **D826**; `tests/README.md` facts from **324**.
