# Coverage 1b-i: `app/utils.py` Permission Functions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take the eight permission and authorisation functions of `app/utils.py` to 100% statement, branch AND condition coverage, and audit which gated actions never ask permission.

**Architecture:** Nine tasks. Task 1 extends `tests/factories.py` with the roles, memberships and bans the rest need. Tasks 2-8 cover one function or pair each, smallest first. Task 9 is the inverse call-site audit plus the ratchet floor.

**Tech Stack:** pytest, the fixtures from sub-project 0 (`app`, `db_session`, `site`), `tests/factories.py`.

**Spec:** `docs/superpowers/specs/2026-08-25-coverage-utils-permissions-design.md`

**Campaign findings that govern this work:** `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` — read first, it is short.

## Global Constraints

- `if TYPE_CHECKING` is always a bug. Never introduce it.
- Imports go at the top of the file. No inline imports.
- Every pragma carries a written justification.
- Tests assert on observable behaviour, never on whether a mock was called.
- **For every test, name the production change that would make it fail.**
- **Condition coverage, not just branch coverage.** Every sub-condition of every compound guard gets its own pair of cases — one tripping it, one passing it with everything else held equal. Branch coverage alone would pass with a sub-condition deleted.
- **Report defects; do not fix them.** Seven are already suspected (spec §"Suspected defects"). Authorisation changes need the project owner's decision, and a permission fix wrong in the restrictive direction locks users out of their own site.
- Floors in `coverage_floors.ini` only ever RISE. `app/utils.py` is currently 50.
- No new dependencies, no migration.
- NO host Python environment. Run `./run_tests.sh [pytest args]`; read `tests/README.md` first. Never `./run_tests.sh --down`. One suite at a time.

## Baseline

`./run_tests.sh tests/ -q --ignore=tests/test_activitypub_util.py` → **1482 passed, 3 skipped, 0 failed**. `app/utils.py` 2844 statements, 1241 uncovered, 50.91%.

## File Structure

| File | Responsibility |
|---|---|
| `tests/factories.py` | Task 1 — extended with roles, permissions, memberships, community bans |
| `tests/test_utils_roles.py` | Task 2 — `user_access`, `role_access` |
| `tests/test_utils_voting.py` | Tasks 3-4 — `can_upvote`, `can_downvote` |
| `tests/test_utils_can_post.py` | Tasks 5-6 — `can_create_post`, `can_create_post_reply` |
| `tests/test_utils_upload_video.py` | Task 7 — `can_upload_video` |
| `tests/test_utils_api_auth.py` | Task 8 — `authorise_api_user` |
| `docs/superpowers/specs/2026-08-25-permission-callsite-audit.md` | Task 9 — the audit table, committed |

---

### Task 1: Extend the factories

**Files:** Modify `tests/factories.py`; Test: `tests/test_factories_permissions.py`

**Interfaces produced** — every later task consumes these:
- `grant_permission(user, permission: str) -> Role` — creates a Role, a RolePermission row and a UserRole row, so `user_access(permission, user.id)` is true.
- `make_community_member(user, community, is_moderator=False) -> CommunityMember`
- `ban_user_from_community(user, community) -> CommunityBan`

- [ ] **Step 1: Read the models first**

`Role`, `RolePermission`, `UserRole`, `CommunityMember`, `CommunityBan` in `app/models.py`. Column names and NOT NULL constraints decide the factory signatures. If a model differs from what this plan assumes, follow the model and report the difference — do not force the plan's shape.

Also read `communities_banned_from` in `app/utils.py` to see which table it actually queries; the factory must write the rows that function reads, or every ban test will silently pass for the wrong reason.

- [ ] **Step 2: Write the failing test**

```python
from app.utils import communities_banned_from, user_access
from tests.factories import (ban_user_from_community, grant_permission, make_community,
                             make_community_member, make_user)


def test_grant_permission_makes_user_access_true(app, db_session):
    """Fails if the factory writes rows user_access does not read."""
    user = make_user(None, 'roleuser', local=True)
    grant_permission(user, 'change instance settings')

    assert user_access('change instance settings', user.id) is True
    assert user_access('some other permission', user.id) is False


def test_community_member_is_visible_to_the_model(app, db_session):
    user = make_user(None, 'member', local=True)
    community = make_community('memberland')
    make_community_member(user, community)

    assert community.is_member(user) is True


def test_moderator_membership_is_visible_to_the_model(app, db_session):
    user = make_user(None, 'mod', local=True)
    community = make_community('modland')
    make_community_member(user, community, is_moderator=True)

    assert community.is_moderator(user) is True


def test_community_ban_is_visible_to_communities_banned_from(app, db_session):
    """The ban factory must write what communities_banned_from reads.

    Fails if the factory targets a different table than the query does — which
    would make every later ban test pass without exercising a ban.
    """
    user = make_user(None, 'banned', local=True)
    community = make_community('banland')
    ban_user_from_community(user, community)

    assert community.id in communities_banned_from(user.id)
```

- [ ] **Step 3: Run — expect ImportError, then implement the factories**

Run: `./run_tests.sh tests/test_factories_permissions.py -q`

Implement following the existing factory style: build the row, `db.session.add`, `db.session.commit`, return the object.

- [ ] **Step 4: Verify `NullCache` really is in force**

`communities_banned_from` is `@cache.memoize`d. `TestConfig` sets `CACHE_TYPE = 'NullCache'`, so it should not cache — but a stale permission answer is exactly the failure that looks like a flake, so prove it rather than assume:

```python
def test_communities_banned_from_is_not_cached_between_calls(app, db_session):
    """A memoized permission lookup that cached across tests would produce
    permission failures indistinguishable from flakes."""
    user = make_user(None, 'cachecheck', local=True)
    community = make_community('cacheland')

    assert communities_banned_from(user.id) == []
    ban_user_from_community(user, community)
    assert community.id in communities_banned_from(user.id)
```

If this fails, STOP and report — every later task depends on it.

- [ ] **Step 5: Commit**

```bash
git add tests/factories.py tests/test_factories_permissions.py
git commit -m "test: add role, membership and community-ban factories"
```

---

### Task 2: `user_access` and `role_access`

**Files:** Create `tests/test_utils_roles.py`

**Interfaces consumed:** `grant_permission` from Task 1.

- [ ] **Step 1: Write the tests**

```python
from app.utils import role_access, user_access
from tests.factories import grant_permission, make_user


class TestUserAccess:
    def test_user_zero_is_always_denied(self, app, db_session):
        """The `user_id == 0` guard. Fails if it is removed, since there is no
        user 0 and the query would simply return no rows -- so this test only
        discriminates together with the query-path tests below."""
        assert user_access('change instance settings', 0) is False

    def test_user_one_is_always_permitted(self, app, db_session):
        """A hardcoded superuser bypass: user 1 is permitted WITHOUT any role.

        Fails if the bypass is removed. Documented rather than endorsed --
        see the spec's suspected-defects list.
        """
        assert user_access('change instance settings', 1) is True

    def test_a_granted_permission_is_permitted(self, app, db_session):
        user = make_user(None, 'granted', local=True)
        grant_permission(user, 'change instance settings')
        assert user_access('change instance settings', user.id) is True

    def test_a_different_permission_is_denied(self, app, db_session):
        """Fails if the query stops filtering on rp.permission."""
        user = make_user(None, 'granted2', local=True)
        grant_permission(user, 'change instance settings')
        assert user_access('ban users', user.id) is False

    def test_a_user_with_no_role_is_denied(self, app, db_session):
        user = make_user(None, 'norole', local=True)
        assert user_access('change instance settings', user.id) is False

    def test_another_users_permission_does_not_leak(self, app, db_session):
        """Fails if the query stops filtering on ur.user_id."""
        granted = make_user(None, 'has', local=True)
        other = make_user(None, 'hasnot', local=True)
        grant_permission(granted, 'ban users')
        assert user_access('ban users', other.id) is False


class TestRoleAccess:
    def test_a_role_with_the_permission(self, app, db_session):
        user = make_user(None, 'roleholder', local=True)
        role = grant_permission(user, 'ban users')
        assert role_access('ban users', role.id) is True

    def test_a_role_without_the_permission(self, app, db_session):
        user = make_user(None, 'roleholder2', local=True)
        role = grant_permission(user, 'ban users')
        assert role_access('change instance settings', role.id) is False

    def test_an_unknown_role(self, app, db_session):
        assert role_access('ban users', 999999) is False
```

- [ ] **Step 2: Run**

Run: `./run_tests.sh tests/test_utils_roles.py -q`

- [ ] **Step 3: Prove discrimination**

Delete `if user_id == 1: return True` from `user_access`; confirm `test_user_one_is_always_permitted` fails. Then change the SQL's `ur.user_id = :user_id` to `ur.user_id = ur.user_id`; confirm `test_another_users_permission_does_not_leak` fails. Restore with `git checkout -- app/utils.py` after each and record both observations.

- [ ] **Step 4: Commit**

```bash
git add tests/test_utils_roles.py
git commit -m "test: cover user_access and role_access"
```

---

### Task 3: `can_upvote`

**Files:** Create `tests/test_utils_voting.py`

`can_upvote` is thin — four sub-conditions in one guard, plus the banned-communities check with its two branches.

- [ ] **Step 1: Write the tests, one per sub-condition**

```python
from app.utils import can_upvote
from tests.factories import (ban_user_from_community, make_community, make_instance,
                             make_site, make_user)


class TestCanUpvote:
    """Each test isolates ONE sub-condition of

        if user is None or community is None or user.banned or user.bot:

    Branch coverage alone is satisfied by two tests and would pass with any
    single sub-condition deleted.
    """

    def test_an_ordinary_user_may_upvote(self, app, db_session):
        user = make_user(None, 'voter', local=True)
        community = make_community('voteland')
        assert can_upvote(user, community) is True

    def test_a_none_user_is_refused(self, app, db_session):
        assert can_upvote(None, make_community('voteland2')) is False

    def test_a_none_community_is_refused(self, app, db_session):
        assert can_upvote(make_user(None, 'voter2', local=True), None) is False

    def test_a_banned_user_is_refused(self, app, db_session):
        """Fails if `or user.banned` is deleted."""
        user = make_user(None, 'bannedvoter', local=True)
        user.banned = True
        community = make_community('voteland3')
        assert can_upvote(user, community) is False

    def test_a_bot_is_refused(self, app, db_session):
        """Fails if `or user.bot` is deleted."""
        user = make_user(None, 'botvoter', local=True)
        user.bot = True
        community = make_community('voteland4')
        assert can_upvote(user, community) is False

    def test_a_user_banned_from_the_community_is_refused(self, app, db_session):
        user = make_user(None, 'commbanned', local=True)
        community = make_community('voteland5')
        ban_user_from_community(user, community)
        assert can_upvote(user, community) is False

    def test_the_precomputed_ban_list_is_honoured(self, app, db_session):
        """The `communities_banned_from_list is not None` branch: callers pass a
        precomputed list to avoid a query per row. Fails if that branch stops
        being consulted, which would silently re-query and ignore the caller."""
        user = make_user(None, 'listbanned', local=True)
        community = make_community('voteland6')
        assert can_upvote(user, community, communities_banned_from_list=[community.id]) is False

    def test_an_empty_precomputed_list_permits(self, app, db_session):
        """Proves the precomputed branch is used INSTEAD of the query, not as
        well as it: the user is banned in the database but the list says no."""
        user = make_user(None, 'listok', local=True)
        community = make_community('voteland7')
        ban_user_from_community(user, community)
        assert can_upvote(user, community, communities_banned_from_list=[]) is True
```

- [ ] **Step 2: Run, then prove discrimination**

Run: `./run_tests.sh tests/test_utils_voting.py -q`

Delete `or user.bot` from `can_upvote`'s first guard; confirm only `test_a_bot_is_refused` fails. Restore and record.

Note `test_an_empty_precomputed_list_permits` is the sharpest test here — it distinguishes "the branch is taken" from "the branch happens to agree with the query".

- [ ] **Step 3: Commit**

```bash
git add tests/test_utils_voting.py
git commit -m "test: cover can_upvote, one case per sub-condition"
```

---

### Task 4: `can_downvote`

**Files:** Modify `tests/test_utils_voting.py`

The densest guard chain in scope: the shared four-part first guard, the `g.site` lookup with its bare `except`, `enable_downvotes`, `local_only`, the attitude/reputation floor, five `downvote_accept_mode` values, and the ban check.

- [ ] **Step 1: Write the tests**

```python
from app.constants import (DOWNVOTE_ACCEPT_ALL, DOWNVOTE_ACCEPT_INSTANCE,
                           DOWNVOTE_ACCEPT_MEMBERS, DOWNVOTE_ACCEPT_NONE,
                           DOWNVOTE_ACCEPT_TRUSTED)
from app.utils import can_downvote


class TestCanDownvoteBasics:
    def test_an_ordinary_user_may_downvote(self, app, db_session, site):
        user = make_user(None, 'downvoter', local=True)
        community = make_community('downland')
        assert can_downvote(user, community) is True

    def test_a_banned_user_is_refused(self, app, db_session, site):
        user = make_user(None, 'dbanned', local=True)
        user.banned = True
        assert can_downvote(user, make_community('downland2')) is False

    def test_a_bot_is_refused(self, app, db_session, site):
        user = make_user(None, 'dbot', local=True)
        user.bot = True
        assert can_downvote(user, make_community('downland3')) is False


class TestCanDownvoteSiteSetting:
    def test_downvotes_disabled_site_wide_refuses_everyone(self, app, db_session, site):
        """Fails if the enable_downvotes check is removed."""
        original = site.enable_downvotes
        site.enable_downvotes = False
        try:
            user = make_user(None, 'sitedown', local=True)
            assert can_downvote(user, make_community('downland4')) is False
        finally:
            site.enable_downvotes = original


class TestCanDownvoteReputation:
    """The `(user.attitude is not None and user.attitude < 0.0) or user.reputation < -10`
    guard: three sub-conditions, each needing its own case."""

    def test_a_negative_attitude_is_refused(self, app, db_session, site):
        user = make_user(None, 'sour', local=True)
        user.attitude = -0.5
        assert can_downvote(user, make_community('downland5')) is False

    def test_a_none_attitude_does_not_refuse(self, app, db_session, site):
        """The `attitude is not None` sub-condition. A new user has no attitude
        yet; treating None as negative would silently refuse every new account."""
        user = make_user(None, 'fresh', local=True)
        user.attitude = None
        assert can_downvote(user, make_community('downland6')) is True

    def test_low_reputation_is_refused(self, app, db_session, site):
        user = make_user(None, 'lowrep', local=True)
        user.reputation = -11
        assert can_downvote(user, make_community('downland7')) is False

    def test_reputation_at_the_boundary_is_permitted(self, app, db_session, site):
        """`< -10`, so exactly -10 must pass. Fails if it becomes `<=`."""
        user = make_user(None, 'boundary', local=True)
        user.reputation = -10
        assert can_downvote(user, make_community('downland8')) is True


class TestCanDownvoteAcceptMode:
    def test_accept_none_refuses(self, app, db_session, site):
        user = make_user(None, 'dm1', local=True)
        community = make_community('dmnone')
        community.downvote_accept_mode = DOWNVOTE_ACCEPT_NONE
        assert can_downvote(user, community) is False

    def test_accept_members_refuses_a_non_member(self, app, db_session, site):
        user = make_user(None, 'dm2', local=True)
        community = make_community('dmmembers')
        community.downvote_accept_mode = DOWNVOTE_ACCEPT_MEMBERS
        assert can_downvote(user, community) is False

    def test_accept_members_permits_a_member(self, app, db_session, site):
        user = make_user(None, 'dm3', local=True)
        community = make_community('dmmembers2')
        community.downvote_accept_mode = DOWNVOTE_ACCEPT_MEMBERS
        make_community_member(user, community)
        assert can_downvote(user, community) is True

    def test_accept_instance_refuses_a_different_instance(self, app, db_session, site):
        remote = make_instance('other.example')
        user = make_user(remote, 'dm4')
        community = make_community('dminstance')
        community.downvote_accept_mode = DOWNVOTE_ACCEPT_INSTANCE
        assert can_downvote(user, community) is False

    def test_accept_all_permits(self, app, db_session, site):
        user = make_user(None, 'dm5', local=True)
        community = make_community('dmall')
        community.downvote_accept_mode = DOWNVOTE_ACCEPT_ALL
        assert can_downvote(user, community) is True
```

`DOWNVOTE_ACCEPT_TRUSTED` needs an instance in `trusted_instance_ids()` and one outside it — write both cases, using a trusted `Instance` row. Read `trusted_instance_ids` before writing them.

- [ ] **Step 2: Handle `local_only` and the ban check**

Add cases for `community.local_only` with a remote user (refused) and a local user (permitted), and for the community ban with and without the precomputed list, mirroring Task 3.

- [ ] **Step 3: Investigate the bare `except`**

`can_downvote` has:

```python
try:
    site = g.site
except:
    site = Site.query.get(1)
```

Establish what exception is actually raised when `g.site` is absent, and write a test that reaches the fallback branch — a request context with no `g.site` set. **Report the bare `except` as a finding** (it catches `KeyboardInterrupt` and `SystemExit` too); do not narrow it without authorisation.

- [ ] **Step 4: Prove discrimination**

Delete the `enable_downvotes` check; confirm `test_downvotes_disabled_site_wide_refuses_everyone` fails. Change `user.reputation < -10` to `<= -10`; confirm `test_reputation_at_the_boundary_is_permitted` fails. Restore after each and record both.

- [ ] **Step 5: Commit**

```bash
git add tests/test_utils_voting.py
git commit -m "test: cover can_downvote, including every accept mode"
```

---

### Task 5: `can_create_post`

**Files:** Create `tests/test_utils_can_post.py`

Guards, in order: `content is None` (twice — see the spec), `user is None or content is None or user.banned`, `user.ban_posts`, the local branch (`verified`, `private_key`), the remote branch (allowlist or instance ban, plus the new-account rate limit), `content.banned`, moderator-or-admin early TRUE, `restricted_to_mods`, `local_only`, community ban, instance ban.

- [ ] **Step 1: Write the tests**

Cover, each as its own case with everything else held eligible:

- an ordinary verified local user may post
- `content is None` refused; `user is None` refused; `user.banned` refused
- `user.ban_posts` refused — **fails if that guard is deleted**
- a local user with `verified=False` refused
- a local user with `private_key=None` refused — use `make_user(..., with_keys=False)`
- a remote user from a banned instance refused (set `Instance.dormant`/ban per `instance_banned`)
- a remote user from a permitted instance allowed
- **the moderator early-return**: a moderator of a `restricted_to_mods` community is permitted, proving the early `return True` precedes the restriction check
- **an admin early-return**: same, via `is_admin`
- a non-moderator refused by `restricted_to_mods`
- `content.banned` refused
- `local_only` with a remote user refused; with a local user permitted
- community ban refused; instance ban refused

The moderator and admin cases matter most: they prove ORDER, not just outcome. If the early `return True` moved below `restricted_to_mods`, moderators would be locked out of their own restricted communities and only an ordering test would notice.

- [ ] **Step 2: The new-account rate limit**

```python
def test_a_very_new_remote_user_is_limited_to_three_posts(self, app, db_session, site):
    """`created_very_recently() and post_count > 3`. Fails if either half is
    dropped: without the count a new user could never post, without the recency
    check the limit would apply forever."""
```

Write both halves — a new user with 3 posts permitted, a new user with 4 refused, and an old user with 400 permitted. Read `created_very_recently` before writing them.

- [ ] **Step 3: Run and prove discrimination**

Delete `if user.ban_posts: return False`; confirm only its test fails. Move the `content.is_moderator(user) or user.is_admin()` early return to AFTER the `restricted_to_mods` check; confirm the moderator test fails. Restore after each and record both — the second is the ordering proof.

- [ ] **Step 4: Commit**

```bash
git add tests/test_utils_can_post.py
git commit -m "test: cover can_create_post, including guard ordering"
```

---

### Task 6: `can_create_post_reply`

**Files:** Modify `tests/test_utils_can_post.py`

Nearly parallel to Task 5, minus the new-account rate limit and the `restricted_to_mods` and instance-ban checks. **Read the function and write cases for what is there, not for what Task 5 had.**

- [ ] **Step 1: Write the tests**

Cover each guard as in Task 5: `user is None`, `content is None`, `user.banned`, `user.ban_comments`, local `verified`/`private_key`, the remote allowlist/ban branch, `content.banned`, the moderator/admin early return, `local_only`, community ban.

- [ ] **Step 2: Record the asymmetries as tests, not as prose**

```python
def test_a_very_new_user_may_reply_more_than_three_times(self, app, db_session, site):
    """can_create_post limits new accounts to 3 posts in 24h; this function has
    no equivalent limit for replies.

    Pinned deliberately. If the limit is later added here, this test fails and
    forces the change to be explicit rather than silent. Whether the asymmetry
    is correct policy is the project owner's call -- see the spec.
    """
```

Do the same for `restricted_to_mods`: if replies genuinely are not restricted in a mods-only community, pin it.

- [ ] **Step 3: Prove discrimination**

Delete `if user.ban_comments: return False`; confirm only its test fails. Restore and record.

- [ ] **Step 4: Commit**

```bash
git add tests/test_utils_can_post.py
git commit -m "test: cover can_create_post_reply and pin its asymmetries"
```

---

### Task 7: `can_upload_video`

**Files:** Create `tests/test_utils_upload_video.py`

Four policies from `get_setting('allow_video_file_uploads', 'no')`: `no`, `user 1`, `admins`, `users`.

- [ ] **Step 1: Write the tests**

Each policy needs a permitted case and a refused case, with `set_setting` restored in a `finally`. Use the injected `user` parameter rather than `current_user` where the signature allows it.

- [ ] **Step 2: Investigate the suspected bug**

The `'users'` branch reads:

```python
elif upload_access == 'users' and not current_user.is_authenticated and user is None:
    return False
```

`upload_user = user or current_user` is computed above and used by the `'admins'` branch, but this branch ignores it. So `can_upload_video(some_user)` under the `'users'` policy returns `True` for **any** user, because `user is None` is already False.

Write the test that documents actual behaviour, and **report the discrepancy**. Do not fix it. State in the test's docstring what the behaviour is and that it is under review, so nobody reads the test as an endorsement.

- [ ] **Step 3: Prove discrimination**

Change `upload_access == 'no'` to `== 'never'`; confirm the no-policy test fails. Restore and record.

- [ ] **Step 4: Commit**

```bash
git add tests/test_utils_upload_video.py
git commit -m "test: cover can_upload_video's four policies"
```

---

### Task 8: `authorise_api_user`

**Files:** Create `tests/test_utils_api_auth.py`

The API authentication gate, with 138 call sites. Every rejection path matters.

- [ ] **Step 1: Write the tests**

Cover: no auth; auth not starting with `Bearer `; an undecodable token; a valid token whose `jti` is in `RevokedToken`; a token for a user id that does not exist; a remote user (`ap_id is not None`); an unverified user; a banned user; a deleted user; a token issued before `password_updated_at`; a token issued after it; `id_match` matching and not matching; and the three return types (`None` → id, `'model'` → User, `'dict'` → the dict).

Build tokens with the app's own encoder so the test proves the real contract. Read how `encode_jwt_token` is defined on `User` before writing them.

The password-rotation case is the subtlest and the most valuable: a token minted before a password change must be refused. **Write both sides** — before and after — since only the pair proves the comparison direction is right. A `>` where `<` was meant would accept exactly the tokens it should reject, and a single-sided test would not notice.

- [ ] **Step 2: The `dict` return type**

It executes six queries. Assert the shape and that each list reflects seeded data — a user banned from one community, following another, with a bookmarked reply, and so on. That is a heavy fixture; build only what each assertion needs and say so if some list is left empty deliberately.

- [ ] **Step 3: Prove discrimination**

Delete `if user.banned is True` from the compound rejection; confirm only the banned test fails. Reverse the password-rotation comparison; confirm the before/after pair catches it. Restore after each and record both.

- [ ] **Step 4: Commit**

```bash
git add tests/test_utils_api_auth.py
git commit -m "test: cover authorise_api_user's rejection paths"
```

---

### Task 9: The inverse call-site audit, and the floor

**Files:** Create `docs/superpowers/specs/2026-08-25-permission-callsite-audit.md`; Modify `coverage_floors.ini`, `tests/README.md`

**This task is the one most likely to find something.** The functions are now covered; the question is whether anything skips them.

- [ ] **Step 1: Enumerate the gated actions and their entry points**

For each of: post creation, reply creation, vote recording, video upload, and state-mutating API endpoints — find EVERY entry point. Web routes (`app/*/routes.py`), API routes (`app/api/`), **ActivityPub inbox handlers** (`app/activitypub/`), CLI commands (`app/cli.py`), and Celery tasks (`app/shared/tasks/`).

Search by what the action DOES, not by what it calls — `Post(`, `PostReply(`, `PostVote(`, `db.session.add` of those models — because a path that never calls the permission function will not appear in a search for the function's name. That is the whole point.

- [ ] **Step 2: Record the table**

| action | entry point | guard | notes |
|---|---|---|---|

One row per entry point. `guard` names the permission function called, or **NONE**.

- [ ] **Step 3: For every NONE, establish whether something upstream gated it**

A route may be protected by a decorator (`@login_required`, `@permission_required`), by an earlier check in the same function, or by only being reachable from an already-authorised path. Follow it and record what you find. **A row that is genuinely unguarded is a finding — report it, do not fix it.**

The ActivityPub inbox is the highest-risk surface: its input is remote-controlled, and this campaign has already found four remote-triggerable defects there. Give it the most attention.

- [ ] **Step 4: Measure and raise the floor**

```bash
./run_tests.sh tests/ -q --ignore=tests/test_activitypub_util.py --cov=app --cov-report=json && \
podman-compose -f compose.test.yaml exec -T test-runner \
    python tests/check_coverage_floors.py coverage.json coverage_floors.ini
```

Set `app/utils.py` to the measured figure rounded DOWN. It is currently 50.

- [ ] **Step 5: Prove the new floor bites**

Raise it one point, re-run, confirm non-zero exit naming the module, restore. Report both outputs. A floor nobody has seen fail is not a floor.

- [ ] **Step 6: Commit**

```bash
git add docs/superpowers/specs/2026-08-25-permission-callsite-audit.md coverage_floors.ini tests/README.md
git commit -m "docs: permission call-site audit; raise app/utils.py floor"
```

---

## Verification

1. The eight functions reach 100% statement and branch coverage, or carry a documented reason.
2. Every sub-condition of every guard has an independent pair of cases, or a recorded reason why it cannot be isolated.
3. The suite stays green — 1482 passed, 3 skipped, 0 failed at the time of writing — and stays fast.
4. `app/utils.py`'s floor has risen and has been seen to fail.
5. The audit table exists, is committed, and every NONE row carries a verdict.
6. Every suspected defect from the spec has a recorded verdict: confirmed, refuted, or deliberate policy.
