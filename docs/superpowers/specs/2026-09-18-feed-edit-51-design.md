# Sub-project 51: `edit_feed` — Group C, and the module's close

**Status:** design approved, ready for an implementation plan
**Branch:** `blentz`, at `a5d62a65`
**Predecessor:** sub-project 50, which closed Group B at `[]`/`[]` on the
full-suite basis, shipped four production repairs, and left the module at
**73.854** with no floor, because a floor on two thirds of a module ratchets
against the third.

## Goal

Cover `edit_feed` — the last uncovered function in `app/shared/feed.py` — repair
the four production defects found while scoping it, and **take the module's
first coverage floor**, which closes `app/shared` at last: every other module in
the tier already carries one.

## THE FACT THAT SHAPES EVERY TASK IN THIS ROUND

**`edit_feed` deletes rows it does not own.**

```python
# app/shared/feed.py:362-365
if feed.public and not public:
    db.session.query(FeedMember).filter(FeedMember.is_owner == False).delete()
    db.session.query(FeedJoinRequest).filter_by(user_id=user.id, feed_id=feed.id).delete()
    feed.subscriptions_count = db.session.query(FeedMember).filter(FeedMember.is_owner == False).count()
```

The `FeedMember` delete carries **no `feed_id` filter**. Making any feed private
unsubscribes every non-owner member of **every feed on the instance**. The
`FeedJoinRequest` delete underneath it is filtered by the **editor's** user id
rather than the feed's pending requests, so it deletes the wrong row and leaves
the right ones. The count that follows is global too, and reads 0 only because
the delete above it has just emptied the table.

Reproduced by execution before it was written down (a feed being made private,
with a second feed owned by a third party carrying its own owner and member):

```
PROBE P1 our feed non-owner members left: 0
PROBE P1 OTHER feed owner rows left: 1 non-owner rows left: 0
PROBE P1 our feed subscriptions_count: 0
```

The other feed's member is gone. Nothing in this codebase logs it, and the
owner of that feed is never told.

This is the most destructive defect the campaign has found. It is the reason
this round exists in the order it does: `edit_feed` is 105 statements of
otherwise ordinary field copying, and the untested statement in the middle of it
deletes other people's data.

## Targets

From the delivered tree's full-suite `--cov=app` JSON at `a5d62a65`, filtered in
Python on the function's line range:

| function | lines | missing statements | missing arcs |
|---|---|---|---|
| `edit_feed` | `:260-385` | **105** | **48** |

105 statements is inside the campaign's 110-130 pace at its lower edge, and 48
arcs is below the 56-72 band. The round is taken at that size because it is what
is left: the module has no other uncovered function, and padding it would mean
pulling in code from another module that has its own round.

## THE ORACLE — measured, not assumed

**No test executes `edit_feed`, and none ever has.** The per-test contexts run in
sub-project 50 reported **zero** contexts for `:260-385`, and
`/usr/bin/grep -rln "edit_feed" tests/` returns four files, none of which calls
it: `tests/test_apply_feed_url_rules.py` mentions `feed_edit`, the ROUTE, in a
docstring; `tests/test_shared_feed_lifecycle.py` and
`tests/test_shared_feed_wiring.py` mention it in prose about neighbouring
functions.

**This is the eighth consecutive oracle trap, and the mention-only kind is the
one this round has to name**: a grep for the function's name finds four files, so
a reader who stops at the grep concludes it is covered somewhere. Three of those
four hits are prose in docstrings, and the fourth is about a route with a similar
name.

**A false claim in one of those docstrings is corrected by this round.**
`tests/test_shared_feed_lifecycle.py:731` says Group C "rebuilds these fields in
`edit_feed(from_scratch=True)`". Both halves are wrong: **no caller passes
`from_scratch=True`** (`/usr/bin/grep -rn "from_scratch" app/` finds it only in
`post.py` and `community.py`), and `edit_feed` does not rebuild the `ap_*` fields
at all — which is defect R2 below. The line was written by sub-project 50 and is
struck rather than silently deleted.

## Callers, verified with `/usr/bin/grep -rn` over `app/`

- `app/api/alpha/utils/feed.py:202` — `edit_feed(input_data, feed, SRC_API, auth)`
  inside `put_feed`. **`put_feed` has no ownership check of its own**; the only
  thing standing between an API client and another user's feed is
  `edit_feed:311`, which this round moves (P4).
- `app/feed/routes.py:156` — `edit_feed(edit_feed_form, feed_to_edit, SRC_WEB,
  None, ..., from_scratch=False)`, behind the route's own ownership check at
  `:133`.
- **Nobody passes `from_scratch=True`.** Its True arms — `:310`'s skip of the
  ownership check, and the `from_scratch or ...` disjuncts at `:331` and `:343`
  — are reachable only by calling the function directly, which the tests do,
  since the signature offers the parameter.

**The structural divergence worth stating once**: `make_community` delegates to
`edit_community(from_scratch=True)` (`app/shared/community.py:282`) and
`make_post` to `edit_post(from_scratch=True)` (`app/shared/post.py:243`), but
`make_feed` **duplicates** `edit_feed`'s body instead. That duplication is why
sub-project 50 found `make_feed` accepting `is_instance_feed` from anyone
(**D675**) while `edit_feed` has always gated it behind `user.is_admin()` at
`:367`: the two copies drifted, and only one of them was ever right.

## THE FOUR PRODUCTION CHANGES

Each was reproduced by execution in a throwaway probe before being written here.

### P1 — `:363` deletes every feed's members

Described above. **Fix:** scope the delete to this feed
(`FeedMember.feed_id == feed.id`). The pin asserts a second feed's member row
survives; without a second feed in the fixture the defect is invisible, which is
exactly why no existing test would have caught it.

### P2 — `:364` deletes the editor's join request instead of the feed's

`filter_by(user_id=user.id, feed_id=feed.id)` names the person doing the editing.
The rows that should go are the **pending requests to join this feed**, which
belong to other people; the editor is the owner and has no pending request
except by accident.

```
PROBE P2 members join requests left: 1
PROBE P2 owners join requests left: 0
```

**Fix:** filter by `feed_id` alone. **Deliberate consequence, stated rather than
discovered later:** a pending request whose row is deleted leaves that user at
`SUBSCRIPTION_NONMEMBER` rather than `SUBSCRIPTION_PENDING`, which is what
**D674** established is the correct state for someone with no membership — so
this repair and D674's agree, and a feed going private now genuinely clears its
queue.

### P3 — `:365` counts every feed's members

`feed.subscriptions_count` is assigned a **global** count of non-owner
memberships. It reads 0 today only because `:363` has just deleted all of them;
the moment P1 scopes that delete, this line would start writing another feed's
membership count into this feed. **The two must be repaired together, and the
test for P1 must assert the count**, or P1's repair turns a hidden bug into a
visible one.

**Fix:** count this feed's remaining members. **The semantics are stated because
they are not obvious:** after the scoped delete only the owner's row remains, and
`subscriptions_count` elsewhere in this module counts the owner too
(`make_feed:228` sets it to 1 for a feed with exactly the owner). The repair
therefore counts **all** of this feed's `FeedMember` rows, not the non-owner
ones, and the round registers the disagreement between that reading and the line
being replaced rather than picking one silently.

### P4 — `:292-305` rewrites the feed before `:311` decides whether the caller may

The ownership check sits **nineteen lines below** the first field write. A caller
who may not edit the feed gets `Exception('incorrect_login')` — after `name`,
`machine_name`, `title`, `description`, `description_html`,
`show_posts_in_children` and `parent_feed_id` have already been assigned on the
live ORM object.

```
PROBE P4 exception: Exception incorrect_login
PROBE P4 in-session title after refusal: Hijacked (was editablefeed)
PROBE P4 session dirty: True
PROBE P4 title after a later commit: Hijacked
```

The last line is the one that matters: the refusal does not roll back, so **any
later commit in the same request persists the rejected edit**. `put_feed` has no
ownership check of its own, so the API path reaches this with an arbitrary
caller's data.

**Fix:** move the authorization check above the field writes. The pin asserts
both the raise and the persisted title, and the inverted test asserts the title
is unchanged after a commit — asserting the raise alone would pass against the
defect.

## Registered, not fixed — with the reason for each

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 | `:310-312`; the signature's `from_scratch` | **`from_scratch=True` skips the ownership check entirely**, and probing confirms it: `PROBE from_scratch: completed; title now Hijacked`. Latent, because no caller passes True -- unlike `edit_post` and `edit_community`, whose `make_*` twins do. | Removing a dead parameter is a refactor of a function two other modules' twins still use in the opposite way; the round pins the behaviour instead, so a future caller that starts passing True fails a test rather than a review. |
| R2 | `:296-297`, against `make_feed:222-226` | **Renaming a feed leaves its ActivityPub identity behind.** `name` and `machine_name` are rewritten; `ap_profile_id`, `ap_public_url`, `ap_followers_url`, `ap_following_url` and `ap_outbox_url` are not. Probe: `PROBE P5 name: renamedfeed` with `ap_profile_id ... /f/editablefeed` unchanged. | The repair is a federation decision, not a coverage one: rewriting an actor's id mid-life either orphans remote followers or requires a `Move`, and `app/shared/feed.py` has no precedent for either. Pinned as current behaviour so the round that takes it starts from a reproduction. **This also corrects sub-project 50's D685, which said Group C "rebuilds these fields": it does not.** |
| R3 | `app/feed/routes.py:142-143` | **The rename guard is browser-side only.** `edit_feed_form.url.render_kw = {'disabled': True}` when `subscriptions_count > 1` disables the widget and constrains nothing on the server, so a crafted POST renames a feed with subscribers -- and R2 then leaves its actor url pointing at the old name. **The same shape as D675**, which sub-project 50 repaired in `make_feed`. | The server-side rule this needs is not "refuse" -- an owner may legitimately rename -- but a decision about subscribers, which is product design. Registered with both halves so whoever takes it sees R2 attached. |
| R4 | `:311`, against `app/feed/routes.py:133` | **The two paths disagree about admins.** `edit_feed` admits `user.is_admin()`; the web route aborts 404 for anyone who is not the owner, admin or not. So an admin can edit any feed through the API and not through the UI. | A divergence, registered as one. Neither behaviour is obviously the intended one and the round does not get to pick. |
| R5 | `:357-360` | **`nsfw`/`nsfl` are only ever written when the site enables them**, so a feed flagged NSFW keeps the flag after an admin disables NSFW site-wide, and the owner cannot clear it. | Same shape as the campaign has registered before: a guard that protects the *set* direction and strands the *clear* direction. Registered with its test. |

## Shapes the tests must handle

- **`g.site` is read at `:357` and `:359`.** `tests/conftest.py`'s request
  fixtures do not populate it inside a bare `test_request_context`, because
  `before_request` never runs there. Set `g.site = Site.query.get(1)` after
  `make_site()`, inside the context, and say so in the file's docstring.
- **`edit_feed` ends by calling `existing_communities`, `form_communities_to_ids`,
  `_feed_add_community` and `_feed_remove_community`** — all Group A's, all
  covered. Patch them on `app.shared.feed` and assert dispatch; a test that lets
  them run is re-testing Group A through a 105-statement function.
- **The icon/banner change detection at `:319-329` reads `feed.icon.source_url`
  and `feed.icon.medium_url()`.** A feed whose `icon_id` points at a missing
  `File` raises; cover the id-set and id-unset arms separately, and mint a real
  `File` for the set arm.
- **`:338` and `:350` delete the OLD file** and call `delete_from_disk()`. Patch
  it; a test that lets it run touches the filesystem.
- **`:331` and `:343` are three-operand conjunctions** —
  `url and (from_scratch or changed) and is_image_url(url)`. Each operand needs
  isolating, and `from_scratch` is one of them, so the sweep needs rows that
  reach the disjunct both ways.
- **The `public`-to-private block only fires on a TRANSITION** (`feed.public and
  not public`). Cover both directions and the two non-transitions, or the guard
  is covered by rows that never disagree.
- **Four id parameters again** — `feed.id`, `user.id`, and the community ids that
  travel into `_feed_add_community(added, 0, feed.id, user.id)`. Mint decoys and
  assert the separation live.

## The method this round inherits

Unchanged, plus the two corrections sub-project 50 wrote into the record:

1. Key the decoupling table by **branch site**, not condition name.
2. Record **observability**, not reached-with-value.
3. Build the table over **every** test in the file.
4. Sweep `{site} × {operand drops, and→or}` **exhaustively**. Sub-project 50's
   sweep found a lockstep gap at a guard whose flash nothing asserted; three
   rows are not enough for a two-operand guard whose operands are both
   parameters.
5. **A filter drop that looks equivalent may be visible only in a side effect**
   — fact 285. This round has three filters in one block and a counter beside
   them, which is precisely that shape.
6. **Every inherited closing test is a hypothesis until executed** — fact 286.

## Success criteria

- `edit_feed` at `missing_lines []` / `missing_branches []` measured on the
  **full-suite** `--cov=app` run, with any arc ruled unreachable carrying a
  named fact 75 cause and a proof, per fact 252.
- **`app/shared/feed.py` TAKES ITS FIRST FLOOR this round**, at the measured
  blended `percent_covered` rounded down, which closes `app/shared`: 28 floors,
  and `coverage_floors.ini` gains exactly one line.
- The four production changes land with pins inverted and every control still
  passing. `git diff --numstat <base> HEAD -- app/` names exactly
  `app/shared/feed.py`.
- No regression: `post.py`, `reply.py`, `user.py`, `domain.py`, `site.py` at
  `100.0` `[]`/`[]`; `community.py` at `99.714` `[699]`/`[[698, 699]]`; Groups A
  and B of `feed.py` unchanged, Group A still carrying only `[[518, 524]]`.
- Full suite green; floors check chained with `&&` against a `--cov=app` JSON
  whose printed mtime matches the run.
- A mutation pass over `edit_feed`, scoped as D602 requires, with an exhaustive
  conjoined sweep, and zero survivors or each survivor carrying a proof.
- The false claim at `tests/test_shared_feed_lifecycle.py:731` struck and
  corrected, and **D685 corrected in the register** — it inherited the same
  error.
- Findings registered from **D688**; `tests/README.md` facts from **288**.

## Environment

Unchanged and binding. Everything runs through `./run_tests.sh`; dotted
`--cov=app.shared.feed`; one pytest session at a time; `pytest.ini` not edited;
per-test contexts via an untracked `.coveragerc.ctx` whose `[json] output` is not
`coverage.json` (fact 284); after a killed run clear orphaned pytest processes
before retrying, and apply D610's asymmetric `--down` reflex.

**The suite now takes ~441s** (5293 tests), above D610's 287-385s band, which was
measured at ~5200 tests. Read the count with the time before calling a run slow.
