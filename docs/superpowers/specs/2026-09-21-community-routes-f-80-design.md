# Sub-project 80 slice F: `show_community` — the community page

**Status:** slice F complete. THREE production defects fixed (D1005–D1007),
across five edits.
**Branch:** `blentz`
**Predecessor:** slice E, which fixed D998–D1003. **64 floors.**

## Scope

| function | gaps |
|---|---|
| `show_community` | 172 |

The largest single function in the blueprint and the page every visitor
reaches. Two listings (posts and comments), twelve sort arms each, a dozen
per-visitor filters, two breadcrumb walks and a per-visitor caching policy.

## THE PRODUCTION CHANGES

### P1 — the comments view listed what the posts view refuses (D1005)

`comments = community.replies` is every `PostReply` in the community, with **no
join to Post at all**. Two lines above it, the posts branch filters
`Post.deleted == False, Post.status > POST_STATUS_REVIEWING`. So
`/c/<name>?content_type=comments` listed:

```
PROBE s1 replies shown: ['reply to a removed post']
PROBE s2 replies shown for a post under review: ['reply to a pending post']
```

A post a moderator has removed keeps its discussion on display, and a post
still awaiting review — which has never been public — leaks its discussion
before anyone has approved it.

**Fix:** the same two filters, through a join to Post.

### P2 — two breadcrumb walks that do not end (D1006)

```python
while previous_topic.parent_id:
    topic = db.session.get(Topic, previous_topic.parent_id)
    topics.append(topic)
    previous_topic = topic
```

`Topic.parent_id` carries no foreign key, so a deleted parent leaves a
dangling id and `db.session.get` returns None:

```
PROBE s3 RAISED: AttributeError 'NoneType' object has no attribute 'parent_id'
```

The same walk runs over `Feed.parent_feed_id`. Both also walk forever on a
cycle — two rows each naming the other as parent — which is a hang rather than
a failure. Both faults are fixed in both walks; on the feed side only the cycle
is reachable, because `feed_parent_feed_id_fkey` refuses a dangling parent.

### P3 — a remote community with no instance row (D1007)

`Community.instance_id` is nullable (`app/models.py:575`), and
`is_dead = community.instance.gone_forever` dereferenced it:

```
PROBE s4 RAISED: AttributeError 'NoneType' object has no attribute 'gone_forever'
```

Unknown is not dead; the guard answers False.

## What the fixtures cost

Two of this slice's runs went on the harness rather than the code, and both
are now facts:

* `Site.private_instance` defaults to **True**, so an anonymous request is
  redirected to the login form before the route runs;
* `login_required_if_private_instance` redirects **every** visitor without the
  `warned` cookie to `/content_warning` whenever `CONTENT_WARNING` is on — so
  a row about the community's own nsfw/nsfl handling has to carry that cookie
  or it never reaches the route.

A third: `community_moderators` synthesises the community's own `user_id` into
the moderator list, so the founder is always a moderator, and Flask-Login
stamps `last_seen` on the current user, so an admin who is also a moderator
makes themselves active just by loading the page.

## Success criteria

- `show_community` at `[]` on the **full-suite** run.
- A floor on `app/community/routes.py` once the blueprint's last slice lands.
- P1–P3 land with their pins inverted — five inversions, one per edit.
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D1005**; `tests/README.md` facts from **439**.
