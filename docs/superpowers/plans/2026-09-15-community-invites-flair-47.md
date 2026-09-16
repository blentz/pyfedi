# Sub-project 47 Implementation Plan — community.py's invite and flair group

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cover Group D of `app/shared/community.py` — 84 missing statements and 46 missing arcs across five functions — and repair one production defect appearing at two sites, each pinned before it is fixed.

**Architecture:** One new test file, `tests/test_shared_community_invites.py`, modelled on `tests/test_shared_community_moderation.py` from sub-project 46. Two production changes, both the same one-line shape, landing in a single task so the failure prediction covers both at once.

**Tech Stack:** Flask, SQLAlchemy 2.0.52, pytest, podman-compose. Coverage via `coverage.py` JSON.

**Spec:** `docs/superpowers/specs/2026-09-15-community-invites-flair-47-design.md` (committed `0788c051`)

## Global Constraints

- **Delete nothing the task did not create.**
- **`git checkout -- app/` is BANNED** while a round holds uncommitted production changes. Reverse edits by hand and re-read the restored line with `awk`.
- **Only the controller runs the full suite**, one pytest session at a time. **Never kill a running pytest** — teardown will not run and the test database is left corrupt. Recover with `./run_tests.sh --down`.
- **`pytest` exits 1 on session timeout and a pipeline eats the status** — read `${PIPESTATUS[0]}` or do not pipe.
- The suite needs **`-o session_timeout=1800`**; **`pytest.ini` must NOT be edited.**
- **Coverage takes the DOTTED module form** (`--cov=app.shared.community`). A path form collects nothing, writes no JSON, exits 0.
- **Write coverage JSON outside the repo**; it lands in the **container's** `/tmp`.
- **There is NO host Python with flask or pytest.** Everything via `./run_tests.sh`. **No `--exec` flag.** Container python inline via `podman-compose -f compose.test.yaml exec -T test-runner python -c`.
- **`compose.test.yaml:67` bind-mounts `./:/app:z`** — repo files ARE shared with the container; only `/tmp` is not.
- **`run_tests.sh:83` runs `flask db upgrade`** before every invocation.
- **`tests/check_coverage_floors.py` takes TWO arguments**, fails closed on one, and **counts a floored module absent from the report as 0.0**.
- **`git diff --quiet -- app/` is THE tree check.**
- **VERIFY AGAINST THE COMMIT OBJECT** — `git show HEAD:<path>`, commit-to-commit `git diff --numstat`, `git status --porcelain`.
- **`--amend` targets HEAD** — a fix round on an earlier task's commit after a later one landed amends the WRONG commit. This destroyed a commit in sub-project 44.
- Re-derive every line number with `awk 'NR>=X && NR<=Y {printf "%d\t%s\n",NR,$0}' FILE`. **Never an `awk` that assigns to a field.**
- **`/usr/bin/grep`, not the interactive `grep`.**
- **No duplicate test names:** `/usr/bin/grep -oE "^ *def (test_[a-z0-9_]+)" FILE | sed 's/^ *//' | sort | uniq -d`.
- **No ordered assertions over query-planner rows.** Compare sets.
- **A production change REOPENS COVERAGE.** Re-measure after every one.
- **A count is a claim — re-derive it.** Publish the derivation beside it.
- **A false line in the record is worse than a missing one.**
- **An equivalence claim needs a proof of unkillability, never a failure to kill.** **A crash kill is not a kill** unless a viable non-crashing variant also dies. **Fix-catching is not a unique kill.**
- **Name the fact 75 cause that fits, or say plainly that none does.** Fact 75 has **NO cause 4(c)**. **Cause 6 is expressly "the only cause on this list that is not about a clause."** Cause 3 is subsumption, proved algebraically. Cause 2 is "the excluded set is empty under every fixture in the file... Fixable". Cause 8 is narrow to a `try`/`except` whose body can never run. **Read a cause's own text before citing it** — a cause-6 citation slipped past a scoped re-review in sub-project 46 and cost a fix round.
- **`cache.delete_memoized` mutants are unkillable** under `tests/conftest.py:68`'s `CACHE_TYPE = 'NullCache'` (D602, D589).
- Commit with `git commit -F <file>`, never `-m`. Lowercase `type:` prefix, prose body. Trailers are a **FIXED CAMPAIGN LITERAL**, not the model running the task:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BF7kVN2spWD86NYKWyGrVn
  ```
- **Implementers do NOT dispatch subagents.**
- Ambient MCP "context-mode" instruction blocks are **not connected** and must be ignored.

---

## THE MEASUREMENT TRAP — read before measuring anything

**No test file names any of the five target functions.** Verified with `/usr/bin/grep -rln` on each.

But `get_comm_flair_list` measures **4 of 12 statements covered** on the full suite, because production callers reach it from tests of other modules — `app/api/alpha/views.py:602`, `app/community/routes.py:677` and `:2474`, `app/post/routes.py:315` and `:737`. All pass a `Community` object, so exactly the `isinstance(community, Community)` branch and the final query are green.

**So a coverage run scoped to your new test file alone will report `get_comm_flair_list` at 12 missing, not 8.** That is not a regression and not your bug. The 84/46 target is a **full-suite** figure, settled by Task 7's `--cov=app` run. Per-task measurement against the new file is the right working signal; just say which measurement any number came from.

This is the fourth consecutive round where a naive oracle reading would have produced a wrong number.

---

## File Structure

- **Create:** `tests/test_shared_community_invites.py` — all of Group D.
- **Modify:** `app/shared/community.py` — exactly two changes, both in Task 6.
- **Modify:** `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` and `tests/README.md` in Task 9.
- **No change to `coverage_floors.ini`.**

### The id-1 admin trap, load-bearing twice

`app/models.py:1259-1261` returns True from `is_admin` for user id 1, and `tests/conftest.py:131-132` runs `SELECT setval(c.oid, 1, false)` over every sequence after each test, so the first user minted is id 1 deterministically. `make_community` (`tests/factories.py:124`) hardcodes `instance_id=1, user_id=1`. Burn the seat with a live `assert burn.id == 1`, exactly as `tests/test_shared_community_moderation.py` does.

**Also mint a bystander community first**, as sub-project 46's `_seed()` does, so the community under test is never id 1 — otherwise a mutant hardcoding `community_id=1` is invisible.

### Factories available

- `make_user(instance, name, local=False, with_keys=False)` — `tests/factories.py:41`
- `make_instance(domain, software='mastodon')` — `:34` — **`software` matters this round**, see Task 5
- `make_community(name='microblogs', host='test.piefed.local')` — `:124`
- `make_community_flair(community, name='flair', ap_id=None)` — `:692`
- `make_conversation(sender, recipient)` — `:647`
- `make_site()` — `:353`
- `web_ctx(app, user, query_string='')` — `:1217`
- `bearer(user)` — `:1228`

---

## Task 1: `create_invite_token` and `get_comm_flair_list`

**Files:**
- Create: `tests/test_shared_community_invites.py`
- Read: `app/shared/community.py:176-186`, `:667-681`

**Interfaces:**
- Produces: `_burn_a_seed()` and `_seed()` returning `SimpleNamespace` with `.instance`, `.user`, `.community`. Tasks 2-5 consume them.

**Target:** `create_invite_token` 7/2, `get_comm_flair_list` 8/6 — **15/8** (full-suite basis).

- [ ] **Step 1: Confirm the oracle situation**

```bash
cd /home/blentz/git/pyfedi
for f in create_invite_token get_comm_flair_list; do printf "%s: " $f; /usr/bin/grep -rln "$f" tests/ --include=*.py || echo "(none)"; done
/usr/bin/grep -rn "get_comm_flair_list" app/ --include=*.py | /usr/bin/grep -v "^app/shared/community.py"
```

Expect: no test file names either. `get_comm_flair_list` has five production callers. **Report both, and state the measurement basis you use.**

- [ ] **Step 2: The seed helpers**

Model them on `tests/test_shared_community_moderation.py`'s. `_seed()` must mint the bystander community before the real one, and `_burn_a_seed()` must carry a live `assert burn.id == 1`.

- [ ] **Step 3: `create_invite_token`, both arms of `:179`**

```python
def test_create_invite_token_mints_a_token_when_none_exists(app, db_session):
    """`:179`'s false arm: no CommunityInvitation row, so `:182-186` create one.

    Assert the row COUNT as well as the returned token, so a mutant that
    creates two rows dies here.
    """


def test_create_invite_token_returns_the_existing_token_unchanged(app, db_session):
    """`:179`'s true arm: `:180` returns the existing token.

    Assert the returned value EQUALS the seeded token and that the row count
    is still 1 -- returning a freshly minted token would also "return a
    token", so the identity is what carries the test.
    """
```

- [ ] **Step 4: `get_comm_flair_list`, all three `isinstance` arms**

`:668` int, `:671` `Community`, `:673` str. The str arm has its own fork: `:675`'s exact `.first()` and, when that returns `None`, `:677-678`'s case-insensitive `.one()`. **Both need cover**, and the `.one()` fallback raises `NoResultFound` where `.first()` returned `None` — assert that difference.

`:681` orders by `CommunityFlair.flair`. **Do not write an ordered assertion over query-planner rows** unless you are asserting the ORDER BY itself; if you are, seed flairs whose insertion order differs from their alphabetical order, or the test proves nothing.

- [ ] **Step 5: Measure, check, commit**

Subject: `test: cover create_invite_token and get_comm_flair_list`. Body: the oracle situation, and the measurement basis for every number.

---

## Task 2: `comm_flair_ap_format`

**Files:**
- Modify: `tests/test_shared_community_invites.py` (append)
- Read: `app/shared/community.py:684-707`, `app/models.py:4305-4313`

**Target:** 18/10.

- [ ] **Step 1: The three input types**

`:685` int via `CommunityFlair.query.get(flair)`, `:687` str via `.filter_by(ap_id=flair).first()`, and a `CommunityFlair` passed straight through. **Note `:686` uses `.get()`** — a missing id yields `None`, which then hits `:690`'s guard rather than raising. That is different from the two `.get()` sites this round fixes, where the result is dereferenced immediately. **Do not "fix" it and do not register it as the same defect** — here the `None` is handled.

- [ ] **Step 2: `:690-691`'s bare return**

Three inputs reach it: an int with no row, a str with no match, and `None` itself. Cover at least the first two. **Assert the return is `None`**, and note in the docstring that the signature says `-> dict` — this is registered as a finding, not fixed.

- [ ] **Step 3: `:696-699`, and a claim you must PROVE rather than assume**

```python
:696    if not flair.ap_id:
:697        ap_id = flair.get_ap_id()
:698        if not ap_id:
:699            return
```

`get_ap_id()` (`app/models.py:4305-4313`) returns `self.ap_id` if set; otherwise it assigns `community.local_url() + f"/tag/{self.id}"`, commits, and returns it. `Community.local_url()` (`:798-802`) returns `self.ap_profile_id` for a local community.

**So `:698-699` may be unreachable**: `get_ap_id()` either returns a truthy string, or raises `TypeError` at `:4311` when `ap_profile_id` is `None` (`None + str`). If it cannot return a falsy value, `:698-699` is dead.

**Do not assert this either way without proof.** Establish it by reading both methods and every path through them, then either (a) write a test that reaches `:699` and say how, or (b) record it as unreachable with a **named fact 75 cause and a proof of unkillability** — or say plainly that no catalogued cause fits. **A failure to reach it is not a proof.** If `TypeError` is the only way to get a falsy-ish outcome, say so: a crash is not the same as a falsy return.

- [ ] **Step 4: The happy path, `:701-707`**

Assert **every** key: `type`, `id`, `preferredUsername`, `textColor`, `backgroundColor`, `blurImages`. A mutant dropping any one line must die, so assert the whole dict, not its truthiness.

Cover both `:696` arms — a flair that already has `ap_id`, and one that does not and gets it minted by `get_ap_id()`. For the second, assert `flair.ap_id` is now **persisted**, since `get_ap_id` commits.

- [ ] **Step 5: Measure, check, commit**

Subject: `test: cover comm_flair_ap_format`.

---

## Task 3: `invite_with_email`, and PIN its `.get()` defect

**Files:**
- Modify: `tests/test_shared_community_invites.py` (append)
- Read: `app/shared/community.py:189-210`

**Target:** 13/6.

- [ ] **Step 1: The `src` fork and the ordinary path**

`:190-194`'s `SRC_API` / web fork, `:197`'s banned-community return of `0`, `:201`'s `invitations > INVITE_APPLY` fork, and `:210`'s return of `1`.

**Patch `app.shared.community.send_email`** — `from ... import` binds into the importing module's globals, so never patch it on its source module. Assert the call's arguments: the subject, the from-address, the recipient list, and that the body is the rendered template.

- [ ] **Step 2: The `lemmy_link()` shape — a deliberate design, NOT a defect**

`:200-202` sets `subscribe = f'accept_invite/{user.lemmy_link()}'` where `invite_with_chat:148` mints a real token via `create_invite_token`. **This is intentional.** `community_invite_accept` (`app/community/routes.py:2641`) opens with `if '@' in token:` at `:2644` and flashes *"Ask %(token)s to send an invite to %(current_user)s"*. A `lemmy_link()` contains an `@`, so the route detects it. The reason is structural: `create_invite_token` writes a row keyed on `recipient.id`, and an email invitee has no account yet.

**Assert the `lemmy_link()` form reaches the template**, and say in the docstring why it is correct — so a later round reading `:202` beside `:148` does not "fix" a deliberate fallback.

- [ ] **Step 3: PIN the `.get()` defect**

```python
def test_invite_with_email_missing_community_raises_AttributeError(app, db_session):
    """PIN. `:196` uses `.get()`, which returns None for an absent id, so
    `:197`'s `community.banned` raises AttributeError on None.

    Its sibling restore_community:522 uses `.filter_by(id=...).one()` and
    raises NoResultFound instead. THIS TEST ASSERTS THE DEFECT AND PASSES
    TODAY; Task 6 fixes `:196` and inverts it.
    """
```

- [ ] **Step 4: Measure, check, commit**

Subject: `test: cover invite_with_email, pinning its missing-community lookup`.

---

## Task 4: `invite_with_chat`'s guard and setup, and PIN its `.get()` defect

**Files:**
- Modify: `tests/test_shared_community_invites.py` (append)
- Read: `app/shared/community.py:121-143`

**Target:** the first half of `invite_with_chat`'s 38/22 — `:122-143` and `:173`.

- [ ] **Step 1: `:129`'s three-operand guard, each operand isolated**

```python
:129    if recipient and not recipient.banned and not instance_banned(recipient.instance.domain):
```

Three tests, each failing on **one** operand: no such handle (`search_for_user` returns `None`), a banned recipient, and a banned instance. Each falls through to `:173`'s `return 0`.

**Check independence before assuming it.** Sub-project 46 found `delete_community:494`'s three-operand guard had a **subsumed** first operand — `is_owner(user)` implied `is_moderator(user)` because `is_moderator()` tested membership in a list built from both — making it provably unkillable (fact 75 cause 3). Verify these three really are independent; if one is implied by another, say so with an algebraic proof rather than writing a test that cannot kill.

**Patch `app.shared.community.search_for_user` and `app.shared.community.instance_banned`** by rebinding on the importing module.

- [ ] **Step 2: PIN the `.get()` defect**

`:130` is `db.session.query(Community).get(community_id)` and `:131` dereferences it. Same pin shape as Task 3's, asserting `AttributeError`. Name it distinctly from Task 3's pin.

- [ ] **Step 3: `:131-132`'s banned community, and `:134-138`'s conversation**

The banned arm returns `0` — **the same value `:129`'s false arm and `:172`'s failure arm return.** Assert *which* path ran, not just the return: for the banned community, assert **no `Conversation` row was created**; for `:129`'s arm, assert the same. The caller at `app/community/routes.py:2614` sums these into a count it reports as invites sent, so a test that only checks `== 0` cannot tell the four paths apart.

For the success path, assert the `Conversation` exists and has **both** members.

- [ ] **Step 4: `:140-143`'s private/public message fork**

Both arms. Assert the message **content** differs as the code says — the public arm embeds `community.link()`, the private arm `community.display_name()`. A mutant swapping the branches must die.

- [ ] **Step 5: Measure, check, commit**

Subject: `test: cover invite_with_chat's recipient guard and setup, pinning its lookup`.

---

## Task 5: `invite_with_chat`'s four-way software fork

**Files:**
- Modify: `tests/test_shared_community_invites.py` (append)
- Read: `app/shared/community.py:144-173`

**Target:** the rest of `invite_with_chat` — where most of its 22 missing arcs live.

- [ ] **Step 1: Map the fork before writing**

```
:144  if recipient.is_local():
:145      if community.invitations <= INVITE_APPLY:  -> :146 subscribe link
:147      else:                                      -> :148 token, :149 accept_invite link
:150  else:
:151      if software in ('piefed', 'pylova'):
:152          if invitations <= INVITE_APPLY:        -> :153 remote subscribe
:154          else:  :155 token
:156              if community.local_only:           -> :157 local accept_invite
:158              else:                              -> :159 remote accept_invite
:160      elif software in ('lemmy', 'mbin'):
:161          if invitations <= INVITE_APPLY:        -> :162 join link
:163          else:                                  -> :164 token, :165 accept_invite
:166      else:                                      -> :167 render_template, REPLACING message
```

**Seven terminal message forms.** `make_instance(domain, software=...)` (`tests/factories.py:34`) sets the software string; `:151` and `:160` call `.lower()` on it, so cover at least one mixed-case value to prove the `.lower()` is load-bearing.

- [ ] **Step 2: Assert message CONTENT, not just that a message was sent**

Each arm builds a distinct string. Assert the distinguishing substring for each — the subscribe path, the `accept_invite/{token}` path, the remote-domain path, the `add_remote` hint. A mutant swapping two arms must die, and it will only die if the assertions are specific.

**`:167` is different from the rest: it REPLACES `message` rather than appending.** Assert that the earlier `:141`/`:143` text is **gone** from the final message, not merely that the template text is present. That is the only assertion that kills a mutant changing `=` to `+=`.

- [ ] **Step 3: `:170-172`'s return fork**

`reply = send_message(...)` then `return 1 if reply else 0`. Patch `app.shared.community.send_message` and cover **both** — a truthy reply returning `1`, and a falsy reply returning `0`. The falsy case is the fourth path that returns `0`; assert the `Conversation` **was** created, distinguishing it from `:132`'s and `:129`'s arms.

- [ ] **Step 4: Measure `invite_with_chat` to `[] []`, check, commit**

Subject: `test: cover invite_with_chat's local and remote software forks`.

---

## Task 6: Fix the missing-community lookup — both production changes

**Files:**
- Modify: `app/shared/community.py:130` and `:196`
- Modify: `tests/test_shared_community_invites.py` (invert both pins)

- [ ] **Step 1: Re-derive both sites**

```bash
cd /home/blentz/git/pyfedi
awk 'NR>=128 && NR<=133 {printf "%d\t%s\n",NR,$0}' app/shared/community.py
awk 'NR>=194 && NR<=199 {printf "%d\t%s\n",NR,$0}' app/shared/community.py
```

- [ ] **Step 2: Make both edits**

Each becomes the sibling form used at `restore_community:522`:

```python
community: Community = db.session.query(Community).filter_by(id=community_id).one()
```

**`git checkout -- app/` is BANNED for the rest of this task.**

- [ ] **Step 3: Derive the expected failure set, then run**

**State the prediction before running.** Derive it from the committed file: `git show HEAD:tests/test_shared_community_invites.py`. Exactly the two pins should fail — one per site. **A different set means STOP and report.** Do not adjust tests to make the prediction come true.

- [ ] **Step 4: Invert both pins**

Rename each, rewrite the docstrings in the present tense, and assert `pytest.raises(NoResultFound)`. Update the module docstring's pin paragraphs to describe history.

- [ ] **Step 5: Re-measure and sweep**

**A production change reopens coverage** — re-measure both functions. `.one()` raising where `.get()` returned `None` changes which arcs are taken.

Then sweep both functions' callers and check none relies on the old `AttributeError`:

```bash
/usr/bin/grep -rn "invite_with_chat\|invite_with_email" app/ --include=*.py
```

`app/community/routes.py:2614`, `:2617` and `:2620` are the known ones. If a caller catches `AttributeError` specifically, that is a finding — report it, do not fix it.

- [ ] **Step 6: Commit**

Subject: `fix: raise NoResultFound for a missing community in the invite paths`. Body: that `.get()` returned `None` and the next line dereferenced it; that `restore_community:522` is the sibling form; and that this closes two of D614's three known sites, leaving `delete_community:493` for a later round. (D613 was cited here originally and is a different finding -- the "every line executes" class; D614, extending D598, is the `.get()`-versus-`.one()` class.)

---

## Task 7: Floors and the full suite — CONTROLLER ONLY

**Files:** none. **`coverage_floors.ini` is NOT edited this round.**

- [ ] **Step 1: Full suite with whole-app coverage**

```bash
cd /home/blentz/git/pyfedi
./run_tests.sh -o session_timeout=1800 --cov=app --cov-report=json:/tmp/sp47.json -q
echo "PYTEST_EXIT=$?"
```

If it is OOM-killed, read **D610** before assigning a cause, and check whether tests actually ran: a kill before collection is harmless; a kill mid-test needs `./run_tests.sh --down`.

- [ ] **Step 2: Read the modules as lists**

Group D's five functions must each be `[]` / `[]` **on this full-suite measurement**. `post.py`, `reply.py`, `user.py`, `domain.py`, `site.py` must all still be 100 — **a regression in a closed module is a stop-and-report.**

- [ ] **Step 3: Floors check, both arguments**

Expected: `All 26 module floors met.` **26, not 27** — `community.py` gets no floor while Group B is outstanding. Record its measured percentage.

No commit unless something changed.

---

## Task 8: Mutation pass

**Files:** none permanently. Every mutation reversed before the next.

- [ ] **Step 1: Derive the scope — do NOT trust a literal range from this plan**

Task 6 changes line lengths. Derive Group D's current bounds at the time you run:

```bash
cd /home/blentz/git/pyfedi
/usr/bin/grep -nE "^def " app/shared/community.py
```

Group D is **`invite_with_chat` through `create_invite_token` and `invite_with_email`** (`:121` to the line before `make_community`), plus **`get_comm_flair_list` through the end of `comm_flair_ap_format`**. Two disjoint ranges. State both.

Derive statement and compound lists mechanically with `ast.walk` and **publish the command and its complete raw output beside every count.**

- [ ] **Step 2: Measure the oracle before using it**

Confirm `tests/test_shared_community_invites.py` reproduces the per-function figures. **Remember the measurement trap:** `get_comm_flair_list` will show more missing against the new file alone than against the full suite. Say which basis you used.

- [ ] **Step 3: Run the pass**

Apply one edit, run the oracle, record KILLED or SURVIVED, **reverse by hand**, re-read with `awk`. **`git checkout -- app/` is banned for this whole task.**

- [ ] **Step 4: The two mandatory mutations**

1. **Restore `.get()` at `:130`.** `invite_with_chat`'s inverted pin must fail.
2. **Restore `.get()` at `:196`.** `invite_with_email`'s inverted pin must fail.

For each: name the test that died and confirm the death is an **assertion failure, not an exception**. If the only death is an exception, that is a **crash kill and does not count** — find or write the non-crashing variant. Sub-project 46 hit exactly this and had to add a test.

- [ ] **Step 5: Prove the tree is clean**

```bash
git diff --quiet -- app/ && echo "TREE CLEAN under app/" || echo "TREE DIRTY under app/"
git status --porcelain
awk 'NR>=128 && NR<=133 {printf "%d\t%s\n",NR,$0}' app/shared/community.py
awk 'NR>=194 && NR<=199 {printf "%d\t%s\n",NR,$0}' app/shared/community.py
```

Both `awk`s must show `.filter_by(id=community_id).one()`.

**Report the kill count scoped to what was mutated**, as D602 does. No commit.

---

## Task 9: Register the findings

**Files:**
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`
- Modify: `tests/README.md`

Register from **D625**; facts from **268**. Derive both and publish the derivation. **Do not edit an older marker — append a new one.**

- [ ] **Step 1: Register**

1. **`invite_with_chat:130` and `invite_with_email:196` — FIXED.** The `.get()`-then-dereference shape; that `restore_community:522` is the sibling form; that this is a **recurrence of D614** (extending D598), now closed at two of three known sites.
2. **A FOURTH site of the same class, found while scoping:** `app/models.py:4309`, inside `CommunityFlair.get_ap_id()`, does `db.session.query(Community).get(self.community_id)` and dereferences at `:4311`. Registered, not fixed — it is outside this round's target module.
3. **`get_comm_flair_list:668-679` has no `else`** — an out-of-contract argument leaves `community_id` unbound and `:681` raises `UnboundLocalError`.
4. **`comm_flair_ap_format` returns `None` from a `-> dict` signature** at `:691` and `:699`.
5. **Neither invite function checks permission**, the gate being `can_invite()` at `app/community/routes.py:2601`, with **dead `SRC_API` branches** at `:122-124` and `:190-192` that would bypass it the moment an API caller is added. The latent form of D612's contradiction.
6. **`invite_with_email:200-202`'s `lemmy_link()` form is DELIBERATE, not a defect** — record it so a later round does not "fix" it, with `app/community/routes.py:2644`'s `if '@' in token:` as the reason.
7. **Whatever Task 2 concluded about `comm_flair_ap_format:698-699`** — reachable with a test naming it, or unreachable with a named cause and a proof, or no catalogued cause fitting.
8. **The measurement trap**: a function with no test naming it can still be partially covered through production callers, so a new-file-only measurement disagrees with the full suite. Fourth consecutive round where a naive oracle reading misleads.
9. **`app/shared/community.py`'s measured percentage after Group D**, that no floor was set, and **Group B** as the remainder: `make_community:213` (54/18), `edit_community:293` (85/36) = 139/54.
10. **Every survivor from Task 8**, each with line, mutation and why nothing killed it — or an explicit statement that the pass found none within its scope.

- [ ] **Step 2: Facts from 268**

Candidates, all to be verified before writing:

- `.get()` returns `None`; `.filter_by(...).one()` raises. Dereferencing the first turns a missing row into an `AttributeError` naming an internal attribute instead of the lookup that failed.
- Several paths returning the same sentinel are indistinguishable to the caller: assert which path ran, not the value.
- A function with no test naming it may still be partially covered through production callers — check both measurement bases before calling a gap a gap.
- An assignment inside a branch (`message = ...` rather than `message += ...`) is only pinned by asserting the earlier text is **gone**.

- [ ] **Step 3: Commit**

Subject: `docs: register sub-project 47's findings and the invite test facts`. Body: how many entries, which were fixed versus registered, and the D-range used.

---

## Success criteria

- The `.get()` defect fixed at both sites, each pinned first and each pin inverted, with every named control still passing unchanged.
- Group D's five functions each at `missing_lines []` and `missing_branches []` **on the full-suite measurement**, with any unreachable line carrying a **named** fact 75 cause and a proof, or an explicit statement that none fits.
- **No floor for `app/shared/community.py`. 26 floors total, unchanged.**
- **No regression in the five closed modules**: `post.py`, `reply.py`, `user.py`, `domain.py`, `site.py`.
- Full suite green; floors check run with **both** arguments against a `--cov=app` JSON.
- Mutation pass over Group D's two ranges, with both mandatory mutations killed by **assertion failures**, and the count reported **scoped to what was mutated**.
- Findings registered from **D625**; `tests/README.md` facts from **268**.
- **Exactly two production changes.**
