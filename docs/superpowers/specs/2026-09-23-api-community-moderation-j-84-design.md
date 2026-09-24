# Sub-project 84 slice J: the moderating half of `app/api/alpha/utils/community.py`

**Status:** slice J complete. FIVE production defects fixed (D1202–D1206),
two equivalent mutants recorded (D1207).
**Branch:** `blentz`
**Predecessor:** slice I, which closed `app/api/alpha/utils/reply.py`.
**Successor:** slice K, the listing and CRUD half of the same module, which
takes the floor.

## Scope

`get_community_moderate_bans`, `put_community_moderate_unban`,
`post_community_moderate_ban`, `post_community_moderate_post_nsfw`,
`post_community_mod`, `post_community_flair_create`,
`put_community_flair_edit`, `post_community_flair_delete`, and the four
helpers this slice introduces (`a_community`, `a_user`, `a_post`,
`a_ban_expiry`).

The module was at **13.6%**. After this slice it is at **58% with no partial
branches**: everything from the ban listing down is covered, and what is left
is one contiguous block — `get_community_list`, `get_community`,
`post_community_follow`, `post_community_leave_all`, `post_community_block`,
`post_community`, `put_community`, `put_community_subscribe`,
`post_community_delete` — which is slice K.

Highest priority first, per the standing instruction: this half is the one
that bans people, unbans them, and decides who moderates.

## THE PRODUCTION CHANGES

### The ban window was measured against the host's clock (D1203)

`CommunityBan.ban_until` holds UTC. Three reads compared it against
`datetime.now()`:

```python
ban_until = datetime.strptime(data['expires_at'], '%Y-%m-%dT%H:%M:%S.%fZ')
if ban_until < datetime.now():
    raise Exception("expires_at must be a time in the future. - "
                    f"Current time: {utcnow().isoformat(...)}Z - ")
...
    ban_until = datetime.now() + relativedelta(years=1)
...
    elif cb.ban_until < datetime.now():
        ban_json['expired'] = True
```

The refusal prints `utcnow()` as "the current time" while testing against
`datetime.now()` — the two halves of one sentence disagree on what time it
is. On an instance nine hours east of Greenwich:

* a ban with four hours left is listed to its own moderators as expired;
* an expiry four hours out is refused as being in the past, in a message
  quoting a "current time" four hours behind the one it used;
* a ban with no `expires_at` runs for a year plus nine hours.

All three now read `utcnow()`. The tests move the process clock with a POSIX
`TZ` string (fact 564) and assert the shift took before probing.

### The unban could never federate (D1206)

```python
res['expired_at'] = utcnow().isoformat(timespec="microseconds") + "Z"
...
task_selector('unban_from_community', ..., expiry=res['expired_at'], ...)
```

`ban_person` hands `expiry` to `ap_datetime()`, which calls `.isoformat()` on
it: every unban raised `AttributeError: 'str' object has no attribute
'isoformat'` inside the task, so the `Undo Block` was never built and never
sent. The local row said the ban was lifted; every remote instance kept it.
The ban path one function below already passes a datetime. Now so does this
one — `expired_at` is kept as a datetime and formatted only for the response.

This was not found by a probe. It was found by the first unban test written,
because eager Celery runs the task inline under test; in production the
exception lands in a worker log.

### One timestamp format where several are ordinary (D1204)

`strptime(..., '%Y-%m-%dT%H:%M:%S.%fZ')` accepted exactly the spelling this
module emits. `2030-01-01T00:00:00Z` and `2030-01-01T00:00:00+09:00` — the
same instants, written the way most clients write them — came back as
`time data '...' does not match format`. `a_ban_expiry` now accepts any ISO
8601 timestamp, converts an offset to UTC, drops the tzinfo for the naive
column, and refuses anything else by name.

### The duplicate check could not see its own rows (D1205)

```python
CommunityFlair.query.filter_by(community_id=..., flair=data['flair_title'],
                               text_color=..., background_color=...,
                               blur_images=...).one()
...
new_flair.flair = data['flair_title'].strip()
```

Matched unstripped, stored stripped — and matched on the presentation columns
as well, so `' news '` and a recoloured `'news'` were both "new". Three calls
differing only in padding and colour produced three flairs named `news`.
Matched on the community and the stripped title now.

### Five endpoints read off a row nobody holds (D1202)

D1194's shape, one module along: `Community.query.filter_by(id=...).one()`
and `db.session.get(...)` used without testing the answer. Because the alpha
API turns every exception into a 400 carrying `str(e)` (fact 567), a caller
who typed a wrong id got `No row was found when one was required` or
`'NoneType' object has no attribute 'is_owner'` — after a logged traceback
and a Sentry event. `a_community`, `a_user` and `a_post` answer by name.

`post_community_mod` still answers `NoResultFound`, because its lookup lives
in `add_mod_to_community` (`app/shared/community.py`), shared with the web
UI. Left for that module's slice, and recorded in the findings.

## THE TESTS

`tests/test_api_community_moderation.py`, 76 rows in nine classes — the ban
listing, banning, unbanning, the NSFW switch, the moderator list, the three
flair endpoints, and `a_ban_expiry`'s accepted spellings.

Every endpoint is pinned three ways before anything else: a row nobody holds,
an account with no standing in the community, and a community this instance
does not host. `api_baseline.user1` is id 1, which `User.is_admin` answers
True for unconditionally, so it is this file's admin and never its stranger.

## THE MUTATION PASS

41 mutants, 38 killed on the measuring pass. Three survivors, all rows of
mine:

* a "within a year" bound loose enough to swallow the nine-hour displacement
  it was written to catch — now an equality against
  `before + relativedelta(years=1)`;
* two rows that asserted an `ap_id` from the response, which cannot witness
  what the endpoint stored, because `flair_view` calls a getter that assigns
  and commits (fact 565). Recorded as D1207.

41/41 accounted for after: 39 killed, 2 equivalent.

## FLOORS

Unchanged at 77. `app/api/alpha/utils/community.py` takes its floor when
slice K closes it.
