# Sub-project 84 slice L: the actions half of `app/api/alpha/utils/post.py`

**Status:** slice L complete. FIVE production defects fixed (D1217–D1221), one
equivalent mutant recorded (D1222), three dead guards removed.
**Branch:** `blentz`
**Predecessor:** slice K, which closed `app/api/alpha/utils/community.py`.
**Successor:** slice M, `get_post_list`, `get_post_list2` and
`get_post_replies` — the other 1,350 lines, which take the floor.

## Scope

`post_post_like`, `put_post_save`, `put_post_subscribe`, `post_post`,
`put_post`, `post_post_delete`, `post_post_report`, `get_post_report_list`,
`put_post_report_resolve`, `post_post_lock`, `post_post_hide`,
`post_post_feature`, `post_post_remove`, `post_post_mark_as_read`,
`get_post_like_list`, `put_post_set_flair`, `post_poll_vote`, and the three
helpers this slice introduces (`a_post`, `a_community`, `a_report`).

The module was at 14.9%. This half is now covered in full with no partial
branches; what is left is the listing, lines 61–1409.

The actions half first, per the standing instruction: this is the surface that
removes posts, resolves reports and decides who may read a post's votes.

## THE PRODUCTION CHANGES

### The guard that refused the wrong reports (D1217)

```python
if not report.suspect_post_id and report.suspect_post_reply_id:
    raise Exception("invalid target of resolution")
```

The post-report resolver's admission test. As written it refuses a report
against a **comment** and passes everything else: a report against a person, a
community or a conversation names neither a post nor a reply, so
`not None and None` is falsy, and the request went on to
`db.session.get(Community, report.in_community_id)` with None and died on
`community.moderators()`.

### Fourteen endpoints, one unchecked lookup (D1218)

The widest instance of D1194's shape so far. `a_post`, `a_community` and
`a_report` answer by name; the whole family is pinned by a parametrised row per
endpoint, so the next endpoint added to this module has an obvious place to be
listed.

### A nullable language compared with `<` (D1219)

`if language_id < 2` in both `post_post` and `put_post`. The default is
`site_language_id()`, which answers None on an instance whose languages are
not seeded; `Post.language_id` is itself nullable. This is D1195, fixed
earlier in `app/api/alpha/utils/reply.py`, in the module next door — fact 478,
the same question asked in two places and guarded in one.

### No bodyless post could be edited (D1220)

`body` defaulted to `post.body`, which is NULL for every link and image post,
and `edit_post` runs a regex substitution over it. Editing a link post to fix
its title was a TypeError. `post_post` already defaults its body to `''`.

### A feature type that bound nothing (D1221)

`if feature_type == "Community": ... elif feature_type == "Local": ...` with no
other arm, and a response below that reads `post` and `user_id`.

### Three dead guards removed

Each of three functions followed `authorise_api_user(auth,
return_type="model")` with `if not user: raise`. That function raises on every
rejecting path and returns a `User` otherwise, so the guard could not run.

## THE TESTS

`tests/test_api_post_actions.py`, 93 rows. The permission arms are pinned from
both sides (a moderator, an administrator, and an account with no standing),
the report listing is pinned by what it EXCLUDES, and the flags that only
decide federation are read off `task_selector`'s own arguments rather than off
the response, which cannot see them (fact 575).

## THE MUTATION PASS

43 mutants, 37 killed on the measuring pass. Six survivors, all rows of mine:
two flags read from the response instead of the task, an alt-text row whose
post had no url so the branch never ran, an event row that asserted the title
instead of the event, a removal reason that never reached the modlog, and
D1222. 42 killed, 1 equivalent.

## A TEST-INFRASTRUCTURE FIX

Strengthening the vote rows turned every voting test red with `429 Too Many
Requests`: the vote quota is read from the shared test Redis, which the
database fixture never cleared, so the counters had been climbing across runs
for the whole life of the suite. `db_session` now clears them. Fact 573.

## FLOORS

Unchanged at 78. `app/api/alpha/utils/post.py` takes its floor when slice M
closes it.
