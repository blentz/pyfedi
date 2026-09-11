# Coverage sub-project 35: `post.py` Group B, the moderator verbs

**Status:** approved, not yet implemented
**Branch:** `blentz`
**Predecessor:** sub-project 34, 19 commits `3d1d4553..5d194edb`, Group A closed at zero missing statements and zero missing arcs, module floor 40 → 50

## Goal

Take the six moderator verbs of `app/shared/post.py` to zero missing statements
and zero missing arcs, and fix two production defects found while reading them.

This is the second of five sub-projects against `app/shared/post.py`. Like the
first, it does not close the module — Groups C, D and E remain.

## Scope

110 statements and 56 missing branch arcs, measured against the full suite at
commit `5d194edb` (4479 passed, 3 skipped, exit 0) on the current 1187-line
tree:

| Function | Lines | Missing stmts | Missing arcs |
|---|---|---|---|
| `mod_remove_post` | `:1039-1081` | 26 | 12 |
| `lock_post` | `:927-960` | 22 | 14 |
| `mod_restore_post` | `:1082-1114` | 20 | 8 |
| `sticky_post` | `:990-1019` | 18 | 10 |
| `move_post` | `:961-989` | 15 | 8 |
| `hide_post` | `:1020-1038` | 9 | 4 |

Counts are **decorator-inclusive**, this campaign's convention, and were
measured against the FULL suite. The module stands at **50.8237%** covered
against a floor of 50 — 0.82 points of headroom.

The total matches the decomposition recorded in D392 exactly: 110 and 56. That
decomposition was computed against the 1174-line tree before Group A ran, and
Group B's line range `:927-1114` is unchanged because all three of sub-project
34's production changes landed outside it. **Any implementer re-measuring must
use the full suite** or the per-function numbers will not reproduce.

Roughly 70 tests, against Group A's 58 for 63 statements and 51 arcs.

## The harness is inherited, and consolidating it is this round's first task

Sub-project 34 paid for a harness that four more rounds need. It is recorded in
`tests/README.md` facts 206-212 and in the module docstring of
`tests/test_shared_post_interactions.py`:

- **No test may request `redis_double`.** It reaches function-body
  `from app import redis_client` sites and then breaks them, because
  `redis_client.lock(...)`'s `__exit__` releases through `EVALSHA` and fakeredis
  does not implement it.
- **WEB arms return a Flask `Response`, not a `str`.** `render_template` at
  `app/shared/post.py:23` is `app.utils.render_template`, which wraps the
  rendered string in `make_response`. Assert `status_code` and
  `get_data(as_text=True)`.
- **SRC_API arms need no request context.** `get_ip_address` swallows the
  missing-context `RuntimeError` and returns `''`.
- **`_seed()` must seed LOCAL users.** `authorise_api_user` rejects a user whose
  `ap_id is not None` with `Exception('incorrect_login')` before the called
  function runs — which is also why every Group B function's own gate is
  unreachable through SRC_API for a banned user.
- **`community.private` is the federation lever; `local_only` is a trap**,
  because `can_downvote` reads it.

### The helpers must move to `tests/factories.py` first

`_seed` (`tests/test_shared_post_interactions.py:149`), `_web_ctx` (`:183-184`)
and `_bearer` (`:195`) are general to every group. `_clear_votes_cast` (`:204`)
and `_seed_poll` (`:1295`) are Group A's own and stay put.

Duplicating the three general helpers into a second file — then a third, fourth
and fifth — is the defect this campaign penalises. The repository's own
precedent is `tests/factories.py:101`'s `feed_ids`, whose docstring records it
being deduplicated after five files each carried a copy.

**Move the three to `tests/factories.py` as this round's first task, as its own
commit, with the full suite green before any Group B test is written.** It
touches 58 passing tests; it must not be entangled with new work.

### Three facts new to Group B

- **`add_to_modlog` (`app/utils.py:3564`) commits.** `:3582` is
  `db.session.commit()`, so it is a commit point inside all five gated
  functions. `:3581` reads `get_setting('public_modlog', False)`.
- **`add_to_modlog:3569` raises `Exception('Invalid action: ' + action)`** for an
  action outside `ModLog.action_map`. All seven strings Group B passes —
  `lock_post`, `unlock_post`, `move_post`, `featured_post`, `unfeatured_post`,
  `delete_post`, `restore_post` — were verified present. This is checked and
  dismissed, not a risk to carry.
- **`Community.moderators()` filters `is_banned == False`**, so a banned
  moderator is not a moderator. `make_community_member(user, community,
  is_moderator=True)` (`tests/factories.py:384`) is the factory.

`mod_remove_post:1045` and `mod_restore_post:1088` both do
`from app import redis_client` inside the function body and then lock on it —
the exact shape the inherited `redis_double` rule exists for. The new file's
docstring must say so rather than leaving the rule unexplained.

## The permission gates, which are the round's spine

Five of the six functions gate on permission, and they do not agree.

`Community.is_admin_or_staff(user)` (`app/models.py:778-779`) is
`return user.is_admin_or_staff()` — a pure delegating wrapper. So the gates
differ in **two** ways, not the three a first reading suggests:

| Predicate | Functions |
|---|---|
| **P1** — `is_moderator or user.is_admin_or_staff()` | `lock_post:941`, `mod_remove_post:1049`, `mod_restore_post:1091` |
| **P2** — `is_moderator or community.is_instance_admin(user) or user.is_admin_or_staff()` | `move_post:969`, `sticky_post:999` |

`hide_post` has no gate, correctly: it writes per-user state through
`user.mark_post_as_hidden`.

`Community.is_instance_admin(user)` (`app/models.py:769-776`) checks
`InstanceRole` for **the community's** instance — for a remote community, a
remote admin. So P1 denies an actor P2 permits, and nothing in the source says
which is intended.

**The divergence is registered, not fixed.** Choosing a predicate is a product
decision this round cannot make from the source alone, and both directions are
harmful if wrong: unifying on P2 grants permission three functions currently
withhold, and unifying on P1 revokes a working path for remote-instance admins.

The gates are also **compounds that coverage.py records as single arc pairs**.
`:941`, `:969`, `:999`, `:1049` and `:1091` each need their conjuncts exercised
separately, and only mutation can see inside them.

## Production changes

Two. Each lands as its own commit with its own failing observation.

**Every one must be observed failing before it is fixed.** A production change
with no failing observation behind it is the same error as a test that cannot
fail, one level up.

**PC1 lands before PC2.** PC2 moves the return statement, and doing it first
would obscure which change closed the federation hole.

### PC1 — `sticky_post` federates outside its own permission gate

`:999` opens the gate at 4-space indent. `:1012`'s `if featured:` is **also** at
4-space indent — the same level, outside the gate. So `:1013`'s
`task_selector('sticky_post', ...)` and `:1015`'s
`task_selector('unsticky_post', ...)` fire whether or not the gate passed.

An unauthorized user therefore federates a sticky or unsticky that never
happened locally. Remote instances act on an activity the origin refused.

This is reachable by an ordinary user, not only a crafted API call:
`app/community/routes.py:1109` calls `sticky_post(post.id, True, SRC_WEB)` during
post creation, commented "federating post's stickiness is separate from creating
it" — and the caller there is the post's author, who need not be a moderator.

Same family as sub-project 34's PC2, which federated a poll vote that was never
recorded — but reached through a permission failure rather than a duplicate.

**Fix:** move the federation inside the gate. Verify the indentation change with
`cat -A` rather than by eye.

### PC2 — three functions return success to an unauthorized API caller

`lock_post:957`, `move_post:986` and `sticky_post:1017` are all outside their
gates. An API caller who fails the permission check receives `user.id, post` and
a 200 with an unchanged post, indistinguishable from success.

`mod_remove_post:1050` and `mod_restore_post:1092` already do the right thing in
the same module: `raise Exception('Does not have permission')`.

**Fix:** raise for `SRC_API`, matching `mod_remove_post:1050` verbatim. Leave
`SRC_WEB` silently no-op.

The asymmetry is not a compromise, it is forced by the callers, and the check
that establishes this is the one sub-project 34's PC1 failed to make until a
Critical was found:

- `app/post/routes.py:1661-1665` carries `@login_required` and nothing else — no
  permission check, no error handling, an unconditional redirect. A raise here
  is a 500.
- `app/community/routes.py:1109` runs mid-post-creation for the post's own
  author. A raise there means a non-moderator creating a sticky post gets a 500
  instead of a post.
- The API side already handles it: `mod_remove_post` raises and is called the
  same way at `app/api/alpha/utils/post.py:1703`.

**The implementer must confirm each of those three claims against the source
before writing the fix**, not take them from this spec.

## Test design

New file: `tests/test_shared_post_moderation.py`.

### Every test must be able to fail

Sub-project 34's reviews caught seven docstrings claiming more than their tests
proved, and mutation refuted two equivalence claims — one of which a reviewer
had independently confirmed. The lesson registered there governs here: **an
equivalence argument must quantify over the inputs that REACH the site**, not
the one input the test happens to use, and a reviewer checking only the
reasoning inherits the reasoning's blind spot.

Specific traps for this round:

- **A gate test must assert the side effect did not happen**, not merely that a
  call returned. Every one of these functions returns `user.id, post` on both
  the permitted and the refused path today, so the return value alone
  distinguishes nothing. Assert the post's field, the `ModLog` row count, and —
  for PC1 — whether `task_selector` fired.
- **`pytest.raises(Exception)` is satisfied by any exception.** After PC2 there
  will be five sites raising bare `Exception` in this module. Every such test
  needs a specific `match=` and a state assertion beside it.
- **`add_to_modlog` commits**, so a test asserting "nothing was written" must
  check the database rather than the session.

### Coverage per function

**`lock_post` (`:927-960`)** — the source fork at `:928`; `:934`'s
`locked` fork setting both `comments_enabled` and `modlog_type`; `:941`'s gate on
both arms and conjunct by conjunct; `:948`'s `locked` fork inside the gate, with
`:949` and `:953`'s `SRC_WEB` flash arms; `:957`'s return fork.

**`move_post` (`:961-989`)** — `:962`'s source fork; `:969`'s three-conjunct gate;
`:973`'s `move_to`, which does NOT commit — `:974` does; `:980`'s flash arm;
`:986`'s return fork.

**`sticky_post` (`:990-1019`)** — `:991`'s source fork; `:999`'s gate; `:1001`'s
`featured` fork; `:1005`'s `ap_featured_url` fork on both arms; `:1012`'s
federation fork, which after PC1 moves inside the gate and needs both arms
exercised in both gate states.

**`hide_post` (`:1020-1038`)** — `:1021`'s source fork; `:1028`'s `hidden` fork,
with `mark_post_as_hidden`'s own `has_hidden_post` guard meaning a second hide is
a no-op; `:1031`'s raw DELETE.

**`mod_remove_post` (`:1039-1081`)** — `:1040`'s source fork; `:1049`'s gate,
which RAISES rather than falling through; `:1052`'s `post.url` fork; the
notification loop at `:1069` with `:1071`'s report-type `continue` on both arms
and a zero-iteration exit; `:1076`'s return fork.

**`mod_restore_post` (`:1082-1114`)** — `:1083`'s source fork; `:1091`'s gate;
`:1094`'s `post.url` fork; `:1109`'s return fork.

## Verification

- **Mutation pass** over every branch point, one at a time: dry-run without `-i`
  and read the produced line, apply, run, restore, then assert an empty
  `git diff -- app/` and the expected `wc -l`. **Enumerate every site before
  mutating any.** Restore before any point where the work might stop — a process
  died mid-round in sub-project 34 and the controller's `git diff -- app/` check
  on resume is what caught it.
- **Both directions on any boundary**, and **conjunct by conjunct on the five
  gates**, which coverage sees as single arc pairs.
- **Mutate PC1's new construct specifically**: hoisting the federation back out
  of the gate must be caught, or PC1 is untested.
- **Coverage** measured with the dotted form `--cov=app.shared.post` and
  `--cov-branch`, across the new file, `tests/test_shared_post_interactions.py`
  and `tests/test_shared_post_edit.py`. A path form collects nothing, writes no
  JSON and exits 0. Read `summary.percent_covered`. Write the JSON outside the
  repository.
- **Floor** raised from 50 to the measured integer, rounded down. This round does
  not close the module; a sub-100 result is correct.
- **Full suite** run by the controller alone, foreground and unpiped. Note the
  correct in-container python invocation is
  `podman-compose -f compose.test.yaml exec -T test-runner python ...`
  (`tests/README.md:403-405`) — `run_tests.sh` has no `--exec` flag and rejects
  with pytest exit 4.

## Register

Findings from **D416**, facts from **213**.

- The two production changes, each with its observed failure recorded.
- **The P1/P2 gate divergence**, with both predicates spelled out, the fact that
  `Community.is_admin_or_staff` is a pure delegating wrapper, and the reason it
  is registered rather than fixed.
- **`.get()` returns `None` for a missing post id** at `hide_post:1026`,
  `mod_remove_post:1047` and `mod_restore_post:1090`. Group A registered the same
  shape at `vote_for_post:33`; D403 established that `get_or_404` appears only
  twice in the whole module, so these are the absence of a pattern rather than
  instances of one.
- **`mod_remove_post:1068-1074` duplicates `delete_post:782-788` verbatim.**
  Group C meets the same block from the other side and should not re-derive it.
- Whatever the mutation pass finds, including every survivor with its argument.

## Carried in from sub-project 34

Two parked minors, folded into this round because they sit in the file this
round builds on and Groups C through E would otherwise copy them:

- **M3** — an overbroad SRC_WEB assertion rule in the Group A module docstring.
- **M8** — two overclaiming docstrings in the same file.

## Risks carried deliberately

- **Moving the helpers touches 58 passing tests.** It is the round's first task
  and its own commit precisely so that a failure there is unambiguous.
- **PC2's asymmetry rests on three claims about callers.** The spec states them;
  the implementer verifies them. Sub-project 34 shipped a Critical by reasoning
  about a caller instead of reading it.
- **The gate divergence stays live.** Registering it means a real difference in
  who can moderate persists until a round with product authority takes it.
