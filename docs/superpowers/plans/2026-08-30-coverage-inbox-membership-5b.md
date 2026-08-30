# Inbox Membership Handshake 5b Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring `process_inbox_request`'s `Follow`, `Accept` and `Reject` arms — 175 statements, 3 executed — to full statement and branch coverage with mutation evidence per guard, fix the two live defects the spec names, and register everything else found.

**Architecture:** Tests call `dispatch(activity, store_ap_json=True)` from `tests/test_inbox_dispatch_preamble.py`, which invokes `process_inbox_request` directly — production's DEBUG branch at `routes.py:758-759`. The arm's target branch is selected by what the **outer actor** resolves to in the preamble (`routes.py:861-870`), so tests choose a branch by choosing the actor. Outbound `send_post_request` calls are doubled at their binding site on the routes module, with their payloads asserted.

**Tech Stack:** pytest, `podman-compose` via `./run_tests.sh`, `coverage.py` (branch mode), fakeredis, respx.

**Spec:** `docs/superpowers/specs/2026-08-30-coverage-inbox-membership-5b-design.md`

## Global Constraints

- **`if TYPE_CHECKING` is always a bug.** Never introduce it.
- **Imports go at the top of the file. No inline imports.**
- **Findings are registered, not fixed — with exactly two exceptions**, Task 5 and Task 7, which the spec authorises by name. No other defect is fixed in this sub-project.
- **Floors only ever rise.** `coverage_floors.ini` is the ratchet; current floor for `app/activitypub/routes.py` is 26.
- **One suite run at a time per worktree.** Never `./run_tests.sh --down` (destroys the tmpfs database, replays ~269 migrations).
- **`--cov=app.module` (dotted) works; `--cov=app/module.py` (path) silently measures nothing.**
- **A guard is tested on the whole domain it claims to reject**, not the one example that motivated it.
- **Each half of a compound guard is dropped separately and must die distinctly.**
- **A mutant killed by `respx.models.AllMockedAssertionError` is an INFRASTRUCTURE kill, not a behavioural one** — it fires inside a blocked network fetch before any assertion runs. Re-run such a mutant with the fetch served (`federation_peer` / `http_mock`) before claiming a guard is load-bearing. A mutant that survives once the fetch succeeds is a legitimate finding — record it; never contrive a test to hide it.
- **Tests asserting an exception use `pytest.raises` with a specific `match=`.** A bare `Exception` catch is a vacuous test.
- **Any mutation is restored exactly**; verify `git diff --stat app/` is empty before every commit except Tasks 5 and 7.
- `tests/test_activitypub_util.py` (3 tests) needs live network and is expected to SKIP.
- Baseline at `d16248a3`: `app/activitypub/routes.py` **510/1813 statements, 203/890 branches, 26.378 % blended, floor 26**. Full suite **2825 passed, 3 skipped**.
- Commit trailer: `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`

---

## File structure

| File | Responsibility |
|---|---|
| `tests/factories.py` (modify) | `make_feed`, `make_community_join_request`, `make_feed_join_request`, `make_user_follow_request` — the shared seed helpers all three arms need |
| `tests/test_inbox_dispatch_follow.py` (create) | Tasks 2–4: the Follow arm's Community, Feed and User branches |
| `tests/test_inbox_dispatch_accept_reject.py` (create) | Tasks 5–8: both Accept forms, both fixes' regression tests, the Reject branches |
| `app/activitypub/routes.py` (modify, Tasks 5 and 7 ONLY) | the two authorised fixes |
| `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` (modify) | Task 9: the register, continuing from D64 |
| `coverage_floors.ini`, `tests/README.md` (modify) | Task 10: the raised floor and any harness fact this slice adds |

Two test files, not four: Follow is self-contained; Accept and Reject share the join-request fixtures and are near-mirrors of each other, and the mirror is exactly where the `APLOG_ACCEPT` mislabelling and the missing guard become visible.

### Shared recorder, defined once in Task 1

```python
def record_sends(monkeypatch):
    """Double send_post_request at its binding site on the routes module and
    return the list it records into.

    routes.py imports send_post_request by name, so patching
    app.activitypub.signature would leave routes' copy pointing at the
    original. Same binding-site trap tests/conftest.py:394 documents.
    """
    sends = []
    monkeypatch.setattr(
        activitypub_routes, 'send_post_request',
        lambda uri, body, private_key, key_id, **kw: sends.append((uri, body, key_id)))
    return sends
```

Tasks 2–4 and 6 import it from `tests/test_inbox_dispatch_follow.py`.

---

### Task 1: Shared fixtures, and the Follow outcome table

**Files:**
- Modify: `tests/factories.py`
- Modify: `tests/test_inbox_dispatch_announce.py`, `tests/test_inbox_dispatch_preamble.py` (de-duplication only)
- Create: `tests/test_inbox_dispatch_follow.py`

**Interfaces:**
- Produces: `make_feed(instance, name='peerfeed', public=True, local=False)`, `make_community_join_request(user, community, joined_via_feed=False)`, `make_feed_join_request(user, feed)`, `make_user_follow_request(requestor, target)`, and `record_sends(monkeypatch)`.

- [ ] **Step 1: Add `make_feed` to `tests/factories.py`**

5a wrote the same 7-line `Feed(...)` literal three times (`test_inbox_dispatch_preamble.py:440` and `:702`, `test_inbox_dispatch_announce.py:65`); its final review flagged the duplication. This sub-project needs feeds in most of its tests, so factor it now:

```python
def make_feed(instance, name: str = 'peerfeed', public: bool = True,
              local: bool = False, with_keys: bool = False) -> Feed:
    """A Feed the preamble's feed_only lookup can resolve.

    `ap_profile_id` must contain '/f/': find_remote_actor (app/activitypub/
    actor.py:86-131) branches on that literal substring before falling
    through to its unconditional queries.
    """
    host = 'test.piefed.local' if local else instance.domain
    feed = Feed(name=name, title=name, instance_id=instance.id,
                public=public,
                ap_id=f'{name}@{host}', ap_domain=host,
                ap_profile_id=f'https://{host}/f/{name}',
                ap_public_url=f'https://{host}/f/{name}',
                ap_fetched_at=utcnow())
    if with_keys:
        private_key, public_key = RsaKeys.generate_keypair()
        feed.private_key = private_key
        feed.public_key = public_key
    db.session.add(feed)
    db.session.commit()
    return feed
```

- [ ] **Step 2: Replace the three duplicated literals with `make_feed`, and prove nothing changed**

Rewrite those three call sites to use the factory. The seeded values must stay equivalent — same name, domain and `ap_profile_id` — or 5a's tests will resolve different actors.

```bash
./run_tests.sh tests/test_inbox_dispatch_preamble.py tests/test_inbox_dispatch_announce.py -q
```

Expected: the same counts 5a recorded — 17 in the preamble file, 10 in the announce file. **If any test fails, the factory is not equivalent to the literal it replaced.** Fix the factory, not the test.

- [ ] **Step 3: Add the three join-request factories**

```python
def make_community_join_request(user: User, community: Community,
                                joined_via_feed: bool = False) -> CommunityJoinRequest:
    """The row Accept and Reject consume. `uuid` defaults to uuid4; the
    a.gup.pe Accept path looks the row up by the LAST path segment of the
    activity's string object, so tests build that string from this uuid."""
    jr = CommunityJoinRequest(user_id=user.id, community_id=community.id,
                              joined_via_feed=joined_via_feed)
    db.session.add(jr)
    db.session.commit()
    return jr


def make_feed_join_request(user: User, feed: Feed) -> FeedJoinRequest:
    jr = FeedJoinRequest(user_id=user.id, feed_id=feed.id)
    db.session.add(jr)
    db.session.commit()
    return jr


def make_user_follow_request(requestor: User, target: User) -> UserFollowRequest:
    """user_id is the requestor; follow_id is who they asked to follow."""
    jr = UserFollowRequest(user_id=requestor.id, follow_id=target.id)
    db.session.add(jr)
    db.session.commit()
    return jr
```

- [ ] **Step 4: Create the Follow test file with `record_sends` and the derived outcome table**

Create `tests/test_inbox_dispatch_follow.py` containing `record_sends` exactly as given in the File structure section above, plus a module docstring holding an outcome table you **derive from `routes.py:935-1073` yourself**. Record, per branch: what selects it, what it writes, what it sends, and what it logs — including the paths that log nothing. Do not copy the spec's prose; every enumeration in this campaign has been wrong somewhere on re-derivation, and finding that is the point of the step.

- [ ] **Step 5: Run and commit**

```bash
./run_tests.sh tests/test_inbox_dispatch_preamble.py tests/test_inbox_dispatch_announce.py tests/test_inbox_dispatch_follow.py -q && \
  git add tests/factories.py tests/test_inbox_dispatch_follow.py tests/test_inbox_dispatch_preamble.py tests/test_inbox_dispatch_announce.py && \
  git commit -m "test: factor the feed and join-request fixtures the membership arms need

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Follow, Community target

**Files:**
- Modify: `tests/test_inbox_dispatch_follow.py`

**Interfaces:**
- Consumes: `dispatch`, `make_feed`, `record_sends`.

`routes.py:942-981`. A remote user follows a **local** community. The preamble resolves the Follow's actor as the remote user; `core_activity['object']` names the community, and `find_actor_or_create_cached` at :938 resolves it.

- [ ] **Step 1: Cover the unfound target**

```python
def test_a_follow_of_an_unresolvable_target_is_refused(app, db_session, monkeypatch):
    """routes.py:939-941, 'Could not find target of Follow'."""
```

- [ ] **Step 2: Cover the two reject paths, and assert the Reject payload**

`local_only` (:945-947) and a `CommunityBan` row (:950-953) both set `reject_follow`, then :956-962 sends a `Reject`. Two tests. Each asserts the log AND the send: the recorded body's `type` is `'Reject'`, its `object.id` echoes the Follow's `id`, its `actor` is the community's `public_url()`, and the `key_id` is `f"{community.public_url()}#main-key"`. A test that only counted sends would not notice the echo breaking.

- [ ] **Step 3: Cover the accept path**

```python
def test_a_follow_of_a_local_community_creates_membership_and_accepts(app, db_session, monkeypatch):
    """routes.py:964-980. Assert ALL of it: the CommunityMember row exists,
    subscriptions_count incremented, community.last_active and user.last_seen
    stamped, the Accept payload (type, object.id echo, actor, key_id), and
    the APLOG_FOLLOW/APLOG_SUCCESS log row."""
```

- [ ] **Step 4: Cover the silent already-a-member path**

```python
def test_a_follow_from_an_existing_member_writes_nothing_and_stays_silent(app, db_session, monkeypatch):
    """routes.py:964-965 with existing_member truthy. Nothing is sent, nothing
    is logged, subscriptions_count is unchanged. Assert
    `ActivityPubLog.query.count() == 0` WITH logging enabled -- the assertion
    that would fail if a log call were ever added -- plus `sends == []`.
    Registered as a finding by Task 9, not fixed."""
```

- [ ] **Step 5: Mutate the reject disjunction**

`local_only` and the `CommunityBan` lookup are two independent reasons to reject. Drop each separately; each needs a **distinct** killer, which means a test whose community is `local_only` but whose user is not banned, and one whose user is banned but whose community is not. Report each mutant and which KIND of kill it produced.

- [ ] **Step 6: Run and commit**

```bash
./run_tests.sh tests/test_inbox_dispatch_follow.py -q && \
  git add tests/test_inbox_dispatch_follow.py && \
  git commit -m "test: cover the Follow arm's community target

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Follow, Feed target

**Files:**
- Modify: `tests/test_inbox_dispatch_follow.py`

`routes.py:983-1017`. Same shape as Task 2, with two differences worth pinning.

- [ ] **Step 1: Cover the non-public reject, and pin that it logs nothing**

```python
def test_a_follow_of_a_non_public_feed_is_rejected_without_any_log(app, db_session, monkeypatch):
    """routes.py:989-999. `if not feed.public` sets reject_follow, a Reject is
    sent -- and unlike the community reject path (:946, :952), NOTHING is
    logged. Assert the Reject payload AND
    `ActivityPubLog.query.count() == 0` with logging enabled. Registered as an
    asymmetry by Task 9, not fixed."""
```

- [ ] **Step 2: Cover the accept path**

Assert the `FeedMember` row, `feed.subscriptions_count`, the Accept payload signed with the feed's key (`f"{feed.public_url()}#main-key"`), and the success log.

- [ ] **Step 3: Cover the already-subscribed path**

`routes.py:1001` guards on `feed_membership(user, feed) != SUBSCRIPTION_MEMBER`. Seed a `FeedMember` so the guard is False; assert nothing is written, sent or logged.

Note `feed_membership` (`app/utils.py:1661`) delegates to `feed.subscribed(user.id)`, which queries directly — the `NullCache` test config means no memoization interferes.

- [ ] **Step 4: Mutate the public guard**

Drop `not` from `if not feed.public:` and confirm a distinct test kills it in each direction: a public feed must not be rejected, and a non-public one must not be accepted. One test cannot prove both.

- [ ] **Step 5: Run and commit**

```bash
./run_tests.sh tests/test_inbox_dispatch_follow.py -q && \
  git add tests/test_inbox_dispatch_follow.py && \
  git commit -m "test: cover the Follow arm's feed target

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Follow, User target

**Files:**
- Modify: `tests/test_inbox_dispatch_follow.py`

`routes.py:1018-1072`, the richest branch: a remote user follows a **local** user.

- [ ] **Step 1: Cover the remote-target refusal**

`routes.py:1021-1024` — `if not local_user.is_local()`, 'Follow request for remote user received'.

- [ ] **Step 2: Cover each alternative of the block guard separately**

`routes.py:1025` is three alternatives with three different subjects:

```python
local_user.has_blocked_user(remote_user.id) or local_user.has_blocked_instance(remote_user.instance_id) or instance_banned(remote_user.instance.domain)
```

Three tests, each seeding **only** its own condition — a `UserBlock`, an `InstanceBlock`, and a `BannedInstances` row respectively. Then drop each alternative separately; each must be killed by a different test. 5a's Task 7 hit this exact shape and found one seeded row silently satisfying two alternatives, so verify each test's fixture leaves the other two false.

- [ ] **Step 3: Cover the auto-accept path**

`ap_manually_approves_followers` False → `auto_accept` True (:1031). Assert: the `UserFollower` row with `is_accepted=True` and `is_inward=True`; `local_user.ap_followers_url` backfilled if it was empty (:1034-1035); the Accept payload and key_id; a `Notification` with `notif_type=NOTIF_FOLLOW` and `subtype='new_follower'`; `local_user.unread_notifications` incremented; and the success log.

- [ ] **Step 4: Cover the manual-approval path**

`ap_manually_approves_followers` True → `auto_accept` False. Assert `is_accepted is None` (**not** `False`), no send at all, and a `Notification` with `notif_type=NOTIF_FOLLOW_REQUEST`.

Add a test pinning that `is_accepted` is never `False` on this path:

```python
def test_a_follow_never_records_is_accepted_false(app, db_session, monkeypatch):
    """routes.py:1033, `is_accepted=auto_accept if auto_accept else None`.
    UserFollower.is_accepted's own comment (app/models.py:3533) documents
    False as 'Rejected', so the column has three meaningful states and this
    expression can only ever produce two. Parametrised over both settings of
    ap_manually_approves_followers, assert the stored value is True or None
    and never False. Registered by Task 9, not fixed."""
```

- [ ] **Step 5: Cover the existing-follower path**

`routes.py:1030` with a seeded `UserFollower(is_inward=True)`. Nothing written, nothing sent, nothing logged — `ActivityPubLog.query.count() == 0` with logging enabled.

- [ ] **Step 6: Pin the mixed-session write**

```python
def test_the_follower_row_and_its_notification_are_written_through_different_sessions(
        app, db_session, monkeypatch):
    """routes.py:1036 adds the UserFollower through the task-local `session`;
    routes.py:1067-1069 adds the Notification through `db.session` and commits
    it separately.

    Under a direct dispatch() call patch_db_session makes those the same
    underlying session, so both rows land -- assert that, which is the
    behaviour today. Then record in the docstring what D60 establishes about
    the inline path, where they are NOT the same, and that this test cannot
    exhibit that divergence. Do NOT claim to have tested the divergence.
    Registered by Task 9, not fixed."""
```

- [ ] **Step 7: Run and commit**

```bash
./run_tests.sh tests/test_inbox_dispatch_follow.py -q && \
  git add tests/test_inbox_dispatch_follow.py && \
  git commit -m "test: cover the Follow arm's user target and its block guard

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: FIX 1 — the a.gup.pe Accept path cannot succeed

**Files:**
- Create: `tests/test_inbox_dispatch_accept_reject.py`
- Modify: `app/activitypub/routes.py:1085` **(authorised fix — one identifier)**

**This is one of exactly two tasks permitted to change `app/`.**

- [ ] **Step 1: Establish what the a.gup.pe actor resolves to**

Before touching anything, determine by reading and by a throwaway probe what the preamble (`routes.py:861-870`) resolves an a.gup.pe `Accept` actor to. a.gup.pe groups are Communities, so `community` should be set and `feed`/`user` None — which decides that the `if community:` branch at :1095 is the one the fixed path reaches. **Write down what you found.** If it does not resolve to a Community, stop and report: the fix's blast radius is different from what the spec assumed.

- [ ] **Step 2: Write the failing test**

```python
def test_an_agupe_string_accept_admits_the_join_requests_user(app, db_session, monkeypatch):
    """routes.py:1077-1085. a.gup.pe accepts by sending the follow request's
    ID as a bare string rather than embedding the Follow object.

    The branch finds the CommunityJoinRequest and reads its user -- then
    control reaches `if not requestor_user:` at :1091, which this branch never
    assigns, so it ALWAYS logs 'Could not find recipient of Accept' and
    returns. The lookup's result is discarded every time.

    This test asserts the corrected behaviour: the join request's user is
    admitted to the community.
    """
```

Build the activity's string object as a URL whose last path segment is the join request's `uuid` (`str(join_request.uuid)`), because :1078-1080 splits on `/` and filters by that segment.

- [ ] **Step 3: Run it and record the failure**

```bash
./run_tests.sh tests/test_inbox_dispatch_accept_reject.py -v
```

Expected: FAIL. Record the exact failure in your report — a test that merely passes after a fix is not evidence the fix did anything.

- [ ] **Step 4: Apply the fix**

`app/activitypub/routes.py:1085`, one identifier:

```python
# before
                            user = session.query(User).get(join_request.user_id)
# after
                            requestor_user = session.query(User).get(join_request.user_id)
```

- [ ] **Step 5: Confirm the test passes, then run the WHOLE suite**

```bash
./run_tests.sh -q
```

This fix makes a previously-always-failing path succeed, so anything that asserted the broken outcome now breaks. Expected: 2825 passed, 3 skipped. **If anything else fails, that is a finding about the existing tests — report it, do not silently amend them.**

- [ ] **Step 6: Cover the branch the fix now reaches**

The fixed path falls into `if community:` at :1095. Add a test for the old-style numeric join request too: :1081-1083's `except Exception:` catches the UUID filter failing on a non-UUID segment, rolls back, and retries with `.get()` on the primary key. Give it a string object ending in the join request's integer `id`.

- [ ] **Step 7: Commit the fix on its own**

```bash
git add app/activitypub/routes.py tests/test_inbox_dispatch_accept_reject.py
git commit -m "fix(activitypub): let the a.gup.pe string Accept reach its recipient

The branch looked up the CommunityJoinRequest and assigned its user to
`user`, but the check below it reads `requestor_user`, which this branch
never sets -- so the path always logged 'Could not find recipient of
Accept' and returned, discarding the lookup. The function's own comment at
routes.py:1076 says the follow request's originator is `requestor_user`
and `user` is whoever sent the Accept, so the assignment was backwards
twice: it left requestor_user unset and clobbered the Accept's sender.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: The Accept arm

**Files:**
- Modify: `tests/test_inbox_dispatch_accept_reject.py`

`routes.py:1086-1148`, now that Task 5 has made the string form reachable.

- [ ] **Step 1: Derive the arm's outcome table from source** and write it into the file as a comment, as Task 1 Step 4 did for Follow. Note especially which paths log nothing.

- [ ] **Step 2: Cover the Follow-object form and its banned check**

`routes.py:1086-1090`: `object.type == 'Follow'` resolves `object.actor` as the requestor; a banned requestor is refused with `f'{requestor_user.ap_id} is banned'`. Also cover :1091-1093, the unfound requestor.

- [ ] **Step 3: Cover the community branch**

`routes.py:1095-1116`. The join request exists and no membership does: a `CommunityMember` is created with `joined_via_feed` copied from the join request (:1103-1106), `subscriptions_count` increments **only if the user is not a bot** (:1108-1109), `last_active` is stamped, and SUCCESS is logged. Cover the bot case separately — that is a guard with its own domain.

Then cover the already-a-member case (:1101 false), where SUCCESS is still logged at :1113.

- [ ] **Step 4: Cover the `IntegrityError` path**

`routes.py:1114-1117` catches `IntegrityError`, rolls back, and logs SUCCESS with `"Membership already exists"`. Drive it by making the membership appear between the check and the insert — patch `session.add` to raise `IntegrityError` once. Assert the rollback happened and the distinct message was logged.

- [ ] **Step 5: Cover the feed and user branches**

`routes.py:1118-1129` (FeedMember, `subscriptions_count`, SUCCESS) and `:1130-1147` (`UserFollower` with `is_inward=False`, or flipping an existing row's `is_accepted` to True, then `requestor_user.num_following += 1`).

Note the asymmetry to register: Accept's user branch filters `UserFollower` with `is_inward=False`, while Reject's (Task 8) does not.

- [ ] **Step 6: Cover the silent no-join-request paths**

Each of the three branches does nothing and logs nothing when no join request is found. Three tests, each asserting `ActivityPubLog.query.count() == 0` with logging enabled.

- [ ] **Step 7: Run and commit**

```bash
./run_tests.sh tests/test_inbox_dispatch_accept_reject.py -q && \
  git add tests/test_inbox_dispatch_accept_reject.py && \
  git commit -m "test: cover the Accept arm's three target branches

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: FIX 2 — Reject's user branch dereferences an absent join request

**Files:**
- Modify: `tests/test_inbox_dispatch_accept_reject.py`
- Modify: `app/activitypub/routes.py:1180-1187` **(authorised fix)**

**This is the second and last task permitted to change `app/`.**

- [ ] **Step 1: Write the failing test**

```python
def test_a_reject_for_a_missing_follow_request_is_handled(app, db_session, monkeypatch):
    """routes.py:1180-1187. The community branch (:1157) and the feed branch
    (:1169) both guard their bodies with `if join_request:`. The user branch
    does not, so a Reject naming a follow request that is already gone --
    withdrawn, or already rejected -- raises AttributeError: 'NoneType' object
    has no attribute 'user_id' instead of logging.

    Peer-triggerable: nothing stops a remote instance sending a Reject for a
    request this instance has no record of.
    """
```

- [ ] **Step 2: Run it and record the failure**

Expected: FAIL with `AttributeError`. Record the exact text.

- [ ] **Step 3: Apply the fix**

Give the branch the guard its siblings have, with the counter decrement inside it:

```python
                        elif user:
                            join_request = session.query(UserFollowRequest).filter_by(user_id=requestor_user.id,
                                                                                      follow_id=user.id).first()
                            if join_request:
                                existing_follow = session.query(UserFollower).filter_by(local_user_id=join_request.user_id,
                                                                                        remote_user_id=join_request.follow_id).first()
                                if existing_follow:
                                    existing_follow.is_accepted = False
                                requestor_user.num_following -= 1
                                session.commit()
                                log_incoming_ap(id, APLOG_ACCEPT, APLOG_SUCCESS, saved_json)
```

Keep `APLOG_ACCEPT` exactly as it is. The mislabelling is registered by Task 9 as a separate finding and fixing it here would exceed this task's authorisation.

- [ ] **Step 4: Confirm the test passes, then run the WHOLE suite**

```bash
./run_tests.sh -q
```

Expected: 2825 passed, 3 skipped, plus this sub-project's additions. Report any other failure rather than amending it.

- [ ] **Step 5: Commit the fix on its own**

```bash
git add app/activitypub/routes.py tests/test_inbox_dispatch_accept_reject.py
git commit -m "fix(activitypub): guard Reject's user branch against a missing follow request

The community and feed branches both guard their bodies with
`if join_request:`; the user branch dereferenced join_request.user_id
directly, so a Reject naming a follow request that no longer exists raised
AttributeError instead of logging. Peer-triggerable.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: The Reject arm

**Files:**
- Modify: `tests/test_inbox_dispatch_accept_reject.py`

`routes.py:1150-1191`.

- [ ] **Step 1: Cover the unfound requestor** (`routes.py:1152-1155`).

- [ ] **Step 2: Cover the community branch**

`routes.py:1157-1168`: the `CommunityJoinRequest` is deleted if present, the `CommunityMember` is deleted if present, and SUCCESS is logged. Cover both present and absent for each of the two rows — four combinations, but two tests suffice if each covers one row present and the other absent, so the two `if`s are exercised in both directions.

- [ ] **Step 3: Cover the feed branch** — `routes.py:1169-1178`, same shape over `FeedJoinRequest` and `FeedMember`.

- [ ] **Step 4: Cover the user branch, both sides of Task 7's new guard**

The join request present with an existing follower (flipped to `is_accepted=False`, `num_following` decremented), and present with no follower row (decrement still runs). The absent case is Task 7's regression test.

Add a test pinning the counter behaviour:

```python
def test_a_reject_decrements_num_following_even_with_no_follower_row(app, db_session, monkeypatch):
    """routes.py:1187 runs whenever a join request exists, regardless of
    whether existing_follow was found, so num_following can drift below the
    number of rows it counts -- and nothing floors it at zero. Task 7's fix
    deliberately did not change this. Registered by Task 9, not fixed."""
```

- [ ] **Step 5: Cover the silently-ignored object types**

`routes.py:1151` handles only `object['type'] == 'Follow'`; anything else falls out of the arm logging nothing. Assert with `ActivityPubLog.query.count() == 0` and logging enabled.

- [ ] **Step 6: Pin the APLOG_ACCEPT mislabelling**

```python
def test_a_reject_is_logged_as_an_accept(app, db_session, monkeypatch):
    """routes.py:1154, :1168, :1178, :1189 all pass APLOG_ACCEPT, so every
    Reject outcome is recorded in ActivityPubLog as an Accept. Assert the
    stored activity_type is what APLOG_ACCEPT produces -- this test documents
    the defect rather than the intent, and Task 9 registers it. Same class as
    D63."""
```

- [ ] **Step 7: Run and commit**

```bash
./run_tests.sh tests/test_inbox_dispatch_accept_reject.py -q && \
  git add tests/test_inbox_dispatch_accept_reject.py && \
  git commit -m "test: cover the Reject arm's three target branches

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Whole-unit confirmation and the register

**Files:**
- Modify: `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md`

**Report only. Fix nothing.**

- [ ] **Step 1: Measure the three spans whole**

```bash
./run_tests.sh tests/test_inbox_dispatch_follow.py tests/test_inbox_dispatch_accept_reject.py -q --cov=app.activitypub.routes --cov-report=json
```

Read `executed_lines` and `missing_branches` against `935-1073`, `1075-1148` and `1150-1191`. Report the figures and **explain every gap**. An unexplained remainder fails this task.

- [ ] **Step 2: File the findings**

The campaign's next free number is **D64**; take numbers in order and update the allocation ledger in the same commit. At minimum: the mixed-session write at :1067-1069; the Feed reject path logging nothing; the three silent success-adjacent paths; `is_accepted` never `False`; the four `APLOG_ACCEPT` sites in Reject; Reject ignoring non-Follow objects silently; the unguarded `object['type']` reads at :1086 and :1151; `User.query.get` at :1108; the `is_inward` filter asymmetry between Accept and Reject; and `num_following` drifting below zero.

Argue each severity from **what the tests executed**, not from reading, and say which claims are reading-level.

- [ ] **Step 3: Record the two fixes as fixed**

Task 5's and Task 7's defects get rows too, marked fixed with their commit SHAs, and the register notes that this sub-project was authorised to fix exactly those two while registering everything else.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md
git commit -m "docs: register the membership handshake's findings

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: The floor, and the harness notes

**Files:**
- Modify: `coverage_floors.ini`, `tests/README.md`

- [ ] **Step 1: Measure the full suite**

```bash
./run_tests.sh -q --cov=app.activitypub.routes --cov-report=json
```

The whole suite, not this sub-project's files.

- [ ] **Step 2: Raise the floor**

Set `app/activitypub/routes.py` to one point below the measured blended figure, as 5a set 26 against 26.378. Record the measured figure in the commit message. **Floors only rise** — if the measurement is at or below 26, stop and report BLOCKED with the figures.

- [ ] **Step 3: Add what this slice learned to `tests/README.md`**

Append to the harness section 5a Task 10 created: the `make_feed` and join-request factories and what they are for; that the Accept and Reject arms select their target branch by what the OUTER actor resolves to in the preamble, not by the activity's object; and that `send_post_request` must be doubled at its binding site on the routes module.

- [ ] **Step 4: Run the ratchet and commit**

```bash
./run_tests.sh -q && \
  git add coverage_floors.ini tests/README.md && \
  git commit -m "docs: raise app/activitypub/routes.py's floor after the membership arms

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Plan self-review

**Spec coverage.** All three spans map to tasks: Follow to Tasks 2–4, Accept to Tasks 5–6, Reject to Tasks 7–8. The spec's two authorised fixes are Tasks 5 and 7, each test-first with the failure recorded, each committed alone, and each followed by a full-suite run because both change behaviour. The spec's "fixes come early" ordering holds: no Accept coverage is written before Fix 1, and no Reject coverage before Fix 2. Every item in the spec's "what reading has already found" list has a task that pins it — :1067-1069 is Task 4 Step 6, the Feed reject silence is Task 3 Step 1, the three silent paths are Tasks 2/3/4 Steps 4/3/5, `is_accepted` is Task 4 Step 4, the `APLOG_ACCEPT` sites are Task 8 Step 6, and the rest are Task 9's register.

**Fix 1's blast radius**, which the spec flagged as a risk, is Task 5 Steps 1, 5 and 6: establish what the actor resolves to before fixing, run the whole suite after, and cover the branch the fix newly reaches.

**Interface consistency.** `make_feed(instance, name='peerfeed', public=True, local=False, with_keys=False)`, the three join-request factories and `record_sends(monkeypatch)` are defined once in Task 1 with the exact signatures Tasks 2–8 use. `dispatch` keeps the signature 5a gave it.

**The de-duplication in Task 1 Step 2 touches 5a's reviewed files.** It is bounded to replacing three identical literals with the factory, and Step 2 requires the two files' existing counts (17 and 10) to be unchanged, so a non-equivalent factory fails loudly rather than silently resolving a different actor.

**Two things may prove awkward**, and both are handled by reporting rather than contrivance: the `IntegrityError` path (Task 6 Step 4) needs the insert to fail after the check passes, and Task 4 Step 6 can only assert today's same-session behaviour — it is explicitly forbidden from claiming to have tested D60's divergence.
