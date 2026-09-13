"""Group E and F of app/shared/reply.py -- the moderator verbs and the
reply-only verbs.

WHAT THIS FILE COVERS, and every line number here was re-derived with
numbered output at the commit named in each task's report, never copied
from the plan:

    mod_remove_reply         :414   21 statements / 12 arcs
    mod_restore_reply        :450   21 / 12
    lock_post_reply          :486   24 / 14
    set_collapse_post_reply  :523   15 / 12
    choose_answer            :548   15 /  4
    unchoose_answer          :579    9 /  4

All six were at ZERO coverage when this file was created -- missing equal to
total for every one. There was no partial coverage to build on and no existing
test to read for the conventions, which is why the harness below is borrowed
wholesale from two files rather than derived here.

WHERE THE HARNESS COMES FROM. `tests/test_shared_post_moderation.py` covers
`lock_post`, `mod_remove_post` and `mod_restore_post` -- the direct twins -- and
`tests/test_shared_reply_interactions.py` covers Groups A and C of this module.
`seed_moderator`, `make_site_admin`, `make_instance_admin` and
`recording_task_selector` are transcribed from the former; `_seed_reply`'s
shape and the `make_site()` rule come from the latter.

THE `make_site()` RULE, in the form sub-project 40 corrected it to: a `Site`
row is needed for `render_template` OR for `can_downvote`, which reads
`Site.query.get(1)` at app/utils.py:2443 and dereferences it at `:2445` before
any source fork. Neither group here calls `can_downvote`, so a `Site` row is
needed only where a template renders.

THREE PROBES WERE RUN BEFORE ANY TEST WAS WRITTEN (task-1-report.md carries
the raw output). Two held; one falsified the plan's own prediction:

  - Probe A predicted that a factory user's `language_id` might point at a
    nonexistent `Language` row and make `choose_answer:556`'s
    `get_recipient_language` raise on `lang.code`. IT DOES NOT RAISE, and not
    for the reason guarded against: `make_user` (tests/factories.py:41) never
    sets `language_id` at all, so it is `None`, not a dangling foreign key.
    `get_recipient_language` (app/utils.py:4789) tests `if recipient.
    language_id:` first, which is falsy, then `elif recipient.
    interface_language:` (also unset, also falsy), and falls to the `else:
    lang_to_use = 'en'` arm at :4799 -- the Language table is never queried.
    No `Language` row needs to be seeded for any test in this file.

  - Probe B confirmed the `@>` cascade at `lock_post_reply:503` works on a
    manually-seeded path shaped like production's (`app/models.py:3053-3060`):
    a child reply whose `path` is `[0, parent.id, child.id]` has
    `replies_enabled` flip to `False` when the parent is locked. A test
    exercising that line must seed `path` itself -- `make_post_reply` does not.

  - Probe C confirmed which seeded user lands on id 1: in
    `_seed_moderated_reply`, `author` is minted before `actor`
    (tests/conftest.py resets sequences every test), so `author.id == 1` and
    `author.is_admin()` is `True` purely from the `self.id == 1` short-circuit
    at app/models.py:1260 -- NOT `actor`. `add_to_modlog:3570` would type any
    action performed by `author` as `'admin'` for that reason alone, with no
    Admin role granted. Every test in this file that cares about `ModLog.type`
    must act through `s.actor` (id 2), not `s.author`, to avoid tripping this
    short-circuit by accident.
"""

import pytest
from contextlib import contextmanager
from types import SimpleNamespace

from app import db
from app.constants import SRC_API, SRC_WEB
from app.models import ModLog, Notification, PostReply
from app.shared.reply import (
    choose_answer, lock_post_reply, mod_remove_reply, mod_restore_reply,
    set_collapse_post_reply, unchoose_answer,
)
from tests.factories import (
    bearer, make_community, make_community_member, make_instance, make_post,
    make_post_reply, make_site, make_user, web_ctx,
)


def _seed_moderated_reply(*, private=True, community_name='moderation'):
    """One instance, one local user, one community, one post, one reply.

    Modelled on `tests/test_shared_reply_interactions.py:507`'s `_seed_reply`
    and carrying its ordering constraints: `make_community` hardcodes
    `instance_id=1` and `user_id=1` (tests/factories.py:141-142) and
    tests/conftest.py:131 resets every sequence after each test, so the
    instance minted first lands on id 1 and the community resolves to it.

    `author` is the reply's author and `actor` is the user who will moderate
    it. They are DISTINCT, and that is load-bearing rather than tidy:
    `mod_remove_reply:426` is
    `reply.deleted_by = user.id if user.id != reply.user_id else -1`, so a
    fixture where the actor IS the author can never witness the `user.id` arm.

    `private=True` sets `community.private`, the federation lever register
    entry D393(d) identifies: it stops the eager Celery task bodies at their
    first guard so no test issues an outbound request.
    """
    instance = make_instance('local.example', software='piefed')
    author = make_user(instance, 'author', local=True)
    actor = make_user(instance, 'actor', local=True)
    community = make_community(community_name)
    community.private = private
    db.session.commit()
    post = make_post(community, author, 'https://local.example/p/1')
    reply = make_post_reply(post, author)
    db.session.commit()
    return SimpleNamespace(instance=instance, author=author, actor=actor,
                           community=community, post=post, reply=reply)


def seed_moderator(s, user=None):
    """Make `user` (default `s.actor`) a moderator of `s.community`.

    `Community.moderators()` (app/models.py:716-722) filters
    `is_banned == False`, so a banned CommunityMember is NOT a moderator --
    an arm worth pinning separately rather than assuming.
    """
    return make_community_member(user or s.actor, s.community, is_moderator=True)


def make_site_admin(user):
    """Give `user` a role named exactly 'Admin'.

    `User.is_admin()` (app/models.py:1259-1265) checks role NAMES, not
    permissions, so `grant_permission` cannot produce a site admin however it
    is called. The name must be the literal string 'Admin' -- sub-project 39
    lost a task to a helper that named the role 'role-4'.
    """
    from app.models import Role, user_role
    role = Role(name='Admin', weight=0)
    db.session.add(role)
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=user.id, role_id=role.id))
    db.session.commit()
    return role


def make_instance_admin(user, instance):
    """An InstanceRole making `user` an admin of `instance`.

    `Community.is_instance_admin(user)` (app/models.py:769-776) looks up
    InstanceRole by the COMMUNITY's instance_id, not the user's, so the
    instance passed here must be the one the community resolves to -- the
    first instance seeded, id 1.
    """
    from app.models import InstanceRole
    role = InstanceRole(instance_id=instance.id, user_id=user.id, role='admin')
    db.session.add(role)
    db.session.commit()
    return role


@contextmanager
def recording_task_selector():
    """Yield a list collecting every task key `app.shared.reply` federates.

    The six functions call `task_selector(...)` unqualified, so rebinding the
    name ON THE MODULE is what intercepts them -- patching
    `app.shared.tasks.task_selector` would not, because the `from ... import`
    at app/shared/reply.py:12 already bound the original into this module's
    globals.

    The recorder calls through to the original rather than stubbing it, so the
    permitted paths still do whatever they do; `_seed_moderated_reply` passes
    `private=True`, which stops every task body at its first guard.

    Restores in a `finally`: `app.shared.reply` is imported once per session,
    so a leaked patch would corrupt every test that ran after this one.
    """
    import app.shared.reply as reply_module
    calls = []
    original = reply_module.task_selector

    def recorder(task_key, **kwargs):
        calls.append(task_key)
        return original(task_key, **kwargs)

    reply_module.task_selector = recorder
    try:
        yield calls
    finally:
        reply_module.task_selector = original


class TestModRemoveReply:
    """`mod_remove_reply` (app/shared/reply.py:414-447)."""

    def test_a_moderator_removes_a_reply_through_the_api(self, db_session):
        """`:421`'s false arm via `is_moderator`, and the writes below it.

        Asserts `deleted` AND `deleted_by`, because `:424` sets the flag on
        every path that gets past the guard and the flag alone cannot say
        which arm of `:426`'s conditional ran.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)

        user_id, reply = mod_remove_reply(s.reply.id, 'spam', SRC_API,
                                          auth=bearer(s.actor))

        assert user_id == s.actor.id
        db.session.refresh(s.reply)
        assert s.reply.deleted is True
        assert s.reply.deleted_by == s.actor.id

    def test_an_unprivileged_user_is_refused_and_changes_nothing(self, db_session):
        """`:421`'s true arm -- all three disjuncts false -- and `:422`'s raise.

        THE RAISE IS NOT THE WITNESS ON ITS OWN. A crash is a weak kill, so
        this also asserts that `deleted` is still False and that no ModLog row
        was written: a mutant that ran the body and then raised would pass a
        `pytest.raises` alone.
        """
        s = _seed_moderated_reply()

        with pytest.raises(Exception, match='Does not have permission'):
            mod_remove_reply(s.reply.id, 'spam', SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.reply)
        assert s.reply.deleted is False
        assert db.session.query(ModLog).count() == 0

    def test_an_instance_admin_may_remove(self, db_session):
        """`:421`'s SECOND disjunct alone -- `is_instance_admin` -- with the
        first and third false.

        `:421` is three disjuncts scored by coverage.py as one arc pair, so
        each needs its own witness or mechanism (e) applies: two conditions
        exercised only in lockstep cannot detect a swap between them.
        """
        s = _seed_moderated_reply()
        make_instance_admin(s.actor, s.instance)

        mod_remove_reply(s.reply.id, 'spam', SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.reply)
        assert s.reply.deleted is True

    def test_a_site_admin_who_is_neither_may_remove(self, db_session):
        """`:421`'s THIRD disjunct alone -- `user.is_admin_or_staff()`.

        This is the disjunct `mod_restore_reply:457` does NOT have, which is
        finding 2 in the spec; the paired test in `TestModRestoreReply` shows
        the same user refused there.
        """
        s = _seed_moderated_reply()
        make_site_admin(s.actor)

        mod_remove_reply(s.reply.id, 'spam', SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.reply)
        assert s.reply.deleted is True

    def test_a_moderator_removing_their_own_reply_records_minus_one(self, db_session):
        """`:426`'s ELSE arm -- `user.id == reply.user_id` gives `-1`.

        The comment at `:425` says this makes the UI show 'removed' rather
        than 'deleted'. The sibling test above takes the other arm with a
        distinct actor, so the two together pin the conditional rather than
        the assignment.
        """
        s = _seed_moderated_reply()
        seed_moderator(s, user=s.author)

        mod_remove_reply(s.reply.id, 'spam', SRC_API, auth=bearer(s.author))

        db.session.refresh(s.reply)
        assert s.reply.deleted_by == -1

    def test_a_bot_authors_reply_does_not_move_the_post_counter(self, db_session):
        """`:427`'s false arm -- `reply.author.bot` true, so `:428` is skipped
        while `:429` still runs.

        THE TWO COUNTERS MUST BE SEEDED DISTINCT or this witnesses nothing: if
        both start at the same value, a mutant moving the wrong one is
        invisible. They are seeded 7 and 3 here and asserted separately.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)
        s.author.bot = True
        s.post.reply_count = 7
        s.author.post_reply_count = 3
        db.session.commit()

        mod_remove_reply(s.reply.id, 'spam', SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.post)
        db.session.refresh(s.author)
        assert s.post.reply_count == 7
        assert s.author.post_reply_count == 2

    def test_a_human_authors_reply_moves_both_counters(self, db_session):
        """`:427`'s true arm -- the same-mechanism positive control.

        Without it, a fixture in which no counter could ever move would
        produce the same untouched `reply_count` as the test above. Same
        seed, same distinct values, one lever moved.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)
        s.post.reply_count = 7
        s.author.post_reply_count = 3
        db.session.commit()

        mod_remove_reply(s.reply.id, 'spam', SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.post)
        db.session.refresh(s.author)
        assert s.post.reply_count == 6
        assert s.author.post_reply_count == 2

    def test_a_multi_element_path_decrements_the_ancestors_child_count(self, db_session):
        """`:430`'s true arm and the raw SQL at `:431-432`.

        The guard is D517's shape, `if reply.path and len(reply.path) > 1:`.
        The path seeded here is production's -- app/models.py:3053-3060 gives
        `[0, parent.id, reply.id]` -- so `tuple(reply.path[:-1])` is
        `(0, parent.id)`, a genuine multi-element IN operand.

        THE ASSERTION IS ON THE ANCESTOR, NOT THE REPLY. The reply's own
        `child_count` is untouched by this statement, so asserting on it would
        witness nothing; and a mutant dropping the `where` clause is caught
        only by a row the statement should NOT have touched, which is why the
        bystander below is seeded and asserted too.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)
        parent = make_post_reply(s.post, s.author)
        bystander = make_post_reply(s.post, s.author)
        db.session.commit()
        parent.child_count = 5
        bystander.child_count = 9
        s.reply.path = [0, parent.id, s.reply.id]
        db.session.commit()

        mod_remove_reply(s.reply.id, 'spam', SRC_API, auth=bearer(s.actor))

        db.session.refresh(parent)
        db.session.refresh(bystander)
        assert parent.child_count == 4
        assert bystander.child_count == 9

    def test_the_web_arm_flashes_and_returns_none(self, db_session, app):
        """`:415`'s false arm, `:434`'s true arm, `:435`'s flash, `:447`.

        `make_site()` is required: the web arm reaches a flash, and the module
        docstring's rule says a Site row is needed wherever a template or a
        permission read touches it.
        """
        from flask import get_flashed_messages
        make_site()
        s = _seed_moderated_reply()
        seed_moderator(s)

        with web_ctx(app, s.actor):
            result = mod_remove_reply(s.reply.id, 'spam', SRC_WEB, auth=None)
            messages = get_flashed_messages()

        assert result is None
        assert 'Comment deleted.' in messages

    def test_the_modlog_row_names_the_delete_action(self, db_session):
        """`:437-440`'s add_to_modlog with the literal 'delete_post_reply'.

        Compared as a SET, never as an ordered list -- the campaign's rule
        about rows a query planner returned.

        This is also the positive control that
        `test_an_unprivileged_user_is_refused_and_changes_nothing`'s
        `ModLog.count() == 0` needed and did not have: that test's zero
        proves nothing about a mechanism that can never write a row at all,
        so this test is what makes that earlier zero mean something.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)

        mod_remove_reply(s.reply.id, 'spam', SRC_API, auth=bearer(s.actor))

        actions = {row.action for row in db.session.query(ModLog).all()}
        assert actions == {'delete_post_reply'}

    def test_the_federation_task_is_selected(self, db_session):
        """`:442`'s task_selector call, intercepted on the module.

        `recording_task_selector` calls through rather than stubbing, so this
        also proves the call is reached on the permitted path rather than
        merely that a name exists.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)

        with recording_task_selector() as calls:
            mod_remove_reply(s.reply.id, 'spam', SRC_API, auth=bearer(s.actor))

        assert 'delete_reply' in calls


class TestModRestoreReply:
    """`mod_restore_reply` (app/shared/reply.py:450-483).

    THE GUARD HAS TWO DISJUNCTS WHERE `mod_remove_reply`'S HAS THREE. `:457`
    is `is_moderator or is_instance_admin`; `:421` adds
    `user.is_admin_or_staff()`. So a site admin who is not a moderator can
    remove a comment and then cannot restore it. That is finding 2 in the
    spec, registered rather than fixed, and
    `test_a_site_admin_who_is_neither_is_refused` below is its witness --
    paired deliberately with `TestModRemoveReply`'s
    `test_a_site_admin_who_is_neither_may_remove`, which shows the same user
    permitted one line earlier in the file.
    """

    def _removed(self, *, bot=False):
        """A seeded reply already removed through `mod_remove_reply`.

        Routed through the production verb rather than set with the ORM, so
        `deleted_by` arrives holding a real actor id and a mutant clearing it
        is visible.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)
        if bot:
            s.author.bot = True
        db.session.commit()
        mod_remove_reply(s.reply.id, 'spam', SRC_API, auth=bearer(s.actor))
        db.session.refresh(s.reply)
        return s

    def test_a_moderator_restores_a_removed_reply(self, db_session):
        """`:457`'s false arm via `is_moderator`, `:460`-`:461`'s writes.

        Asserts `deleted_by` back to None as well as `deleted` to False: `:460`
        runs on every permitted path, so the flag alone cannot witness `:461`.
        """
        s = self._removed()

        user_id, reply = mod_restore_reply(s.reply.id, 'ok', SRC_API,
                                           auth=bearer(s.actor))

        assert user_id == s.actor.id
        db.session.refresh(s.reply)
        assert s.reply.deleted is False
        assert s.reply.deleted_by is None

    def test_an_instance_admin_may_restore(self, db_session):
        """`:457`'s SECOND disjunct alone, with the first false."""
        s = self._removed()
        other = make_user(s.instance, 'admin-user', local=True)
        db.session.commit()
        make_instance_admin(other, s.instance)

        mod_restore_reply(s.reply.id, 'ok', SRC_API, auth=bearer(other))

        db.session.refresh(s.reply)
        assert s.reply.deleted is False

    def test_a_site_admin_who_is_neither_is_refused(self, db_session):
        """`:457`'s true arm for a user `mod_remove_reply:421` WOULD admit.

        THIS TEST PINS FINDING 2 AND ASSERTS THE DIVERGENCE ON PURPOSE. The
        same user, with the same role, is permitted by
        `TestModRemoveReply::test_a_site_admin_who_is_neither_may_remove`. If
        a later round makes the two guards agree, THE EDIT OWED HERE IS TO
        INVERT THIS TEST: the restore must then succeed and `deleted` must
        read False. Its failure at that point is the fix landing, not a
        regression.
        """
        s = self._removed()
        other = make_user(s.instance, 'staffer', local=True)
        db.session.commit()
        make_site_admin(other)

        with pytest.raises(Exception, match='Does not have permission'):
            mod_restore_reply(s.reply.id, 'ok', SRC_API, auth=bearer(other))

        db.session.refresh(s.reply)
        assert s.reply.deleted is True

    def test_a_bot_authors_reply_does_not_move_the_post_counter_on_restore(self, db_session):
        """`:462`'s false arm -- `:463` skipped, `:464` still runs.

        Named distinctly from `TestModRemoveReply`'s test of the same name --
        two module-level test methods sharing a name across classes still
        collide in the duplicate-name check this campaign runs.
        """
        s = self._removed(bot=True)
        s.post.reply_count = 7
        s.author.post_reply_count = 3
        db.session.commit()

        mod_restore_reply(s.reply.id, 'ok', SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.post)
        db.session.refresh(s.author)
        assert s.post.reply_count == 7
        assert s.author.post_reply_count == 4

    def test_a_human_authors_reply_moves_both_counters_on_restore(self, db_session):
        """`:462`'s true arm -- the same-mechanism positive control."""
        s = self._removed()
        s.post.reply_count = 7
        s.author.post_reply_count = 3
        db.session.commit()

        mod_restore_reply(s.reply.id, 'ok', SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.post)
        db.session.refresh(s.author)
        assert s.post.reply_count == 8
        assert s.author.post_reply_count == 4

    def test_a_multi_element_path_increments_the_ancestors_child_count(self, db_session):
        """`:465`'s true arm and `:466-467`'s raw SQL.

        The mirror of `TestModRemoveReply`'s path test, with a BYSTANDER for
        the same reason: a mutant dropping the `where` clause is caught only
        by a row the statement should not have touched.
        """
        s = self._removed()
        parent = make_post_reply(s.post, s.author)
        bystander = make_post_reply(s.post, s.author)
        db.session.commit()
        parent.child_count = 5
        bystander.child_count = 9
        s.reply.path = [0, parent.id, s.reply.id]
        db.session.commit()

        mod_restore_reply(s.reply.id, 'ok', SRC_API, auth=bearer(s.actor))

        db.session.refresh(parent)
        db.session.refresh(bystander)
        assert parent.child_count == 6
        assert bystander.child_count == 9

    def test_the_web_arm_flashes_and_returns_none_on_restore(self, db_session, app):
        """`:451`'s false arm, `:470`'s true arm, `:471`'s flash, `:483`.

        NO `make_site()` HERE, unlike the sibling test in `TestModRemoveReply`.
        This arm neither renders a template nor calls `can_downvote` -- the
        only two reasons the module docstring's rule requires a `Site` row --
        it only flashes and returns. `web_ctx` opens the request context with
        `app.test_request_context`, which does not fire `before_request` and
        so never populates `g.site` anyway; `flash()` writes to the session
        and flask_babel's `_()` does not touch the database. Nothing on this
        path reads `Site.query.get(1)`.
        """
        from flask import get_flashed_messages
        s = self._removed()

        with web_ctx(app, s.actor):
            result = mod_restore_reply(s.reply.id, 'ok', SRC_WEB, auth=None)
            messages = get_flashed_messages()

        assert result is None
        assert 'Comment restored.' in messages

    def test_the_modlog_row_names_the_restore_action(self, db_session):
        """`:473-476`'s add_to_modlog with the literal 'restore_post_reply'.

        The set here holds TWO actions, because `_removed` wrote the delete
        row first. Asserting the set rather than a count is what makes the
        restore action's presence the witness.
        """
        s = self._removed()

        mod_restore_reply(s.reply.id, 'ok', SRC_API, auth=bearer(s.actor))

        actions = {row.action for row in db.session.query(ModLog).all()}
        assert actions == {'delete_post_reply', 'restore_post_reply'}

    def test_the_federation_task_is_selected_for_restore(self, db_session):
        """`:478`'s task_selector call."""
        s = self._removed()

        with recording_task_selector() as calls:
            mod_restore_reply(s.reply.id, 'ok', SRC_API, auth=bearer(s.actor))

        assert 'restore_reply' in calls


class TestLockPostReply:
    """`lock_post_reply` (app/shared/reply.py:486-520).

    THIS FUNCTION FAILS SILENTLY AND ITS TWIN DOES NOT. `:501`'s guard has no
    `else`, so an unauthorized SRC_API caller falls through to `:519-520` and
    receives `user.id, post_reply` with nothing changed -- a 200 carrying the
    unchanged object. `app/shared/post.py:968-969` is the same guard WITH
    `elif src == SRC_API: raise Exception('Does not have permission')`, and
    `move_post:999-1000` carries it too; both came from PC2 in sub-project 36.
    These two reply functions were left behind.

    That makes false-witness mechanism (a) acute here: THE RETURN VALUE IS THE
    SAME ON BOTH ARMS, so every test below asserts on state and never on the
    return.

    NO `make_site()` ANYWHERE IN THIS CLASS. The module docstring's rule only
    requires a `Site` row where a template renders or `can_downvote` runs, and
    this function does neither: its only two branches either flash-and-return
    or fall through untouched. `render_template` in this module appears only
    at `:65` and `:145` (Group A), both outside this function, and `web_ctx`
    opens its request context with `app.test_request_context`, which never
    fires `before_request` and so never populates `g.site` regardless.
    """

    def test_a_moderator_locks_a_reply_through_the_api(self, db_session):
        """`:494`'s true arm, `:501`'s true arm, `:502`'s assignment.

        Asserts `replies_enabled` rather than the return, because `:519-520`
        returns the same shape on the refused path.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)
        s.reply.replies_enabled = True
        db.session.commit()

        user_id, reply = lock_post_reply(s.reply.id, True, SRC_API,
                                         auth=bearer(s.actor))

        assert user_id == s.actor.id
        db.session.refresh(s.reply)
        assert s.reply.replies_enabled is False

    def test_unlocking_sets_replies_enabled_back_to_true(self, db_session):
        """`:494`'s false arm, `:498`'s assignment and `:499`'s modlog_type."""
        s = _seed_moderated_reply()
        seed_moderator(s)
        s.reply.replies_enabled = False
        db.session.commit()

        lock_post_reply(s.reply.id, False, SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.reply)
        assert s.reply.replies_enabled is True

    def test_locking_cascades_to_a_descendant(self, db_session):
        """`:503-504`'s containment query, the module's only `@>`.

        `where path @> ARRAY[:parent_id]` matches every reply whose `path`
        CONTAINS the locked reply's id, which is how a lock reaches a whole
        subtree. The descendant's path is production's shape --
        app/models.py:3053-3060, `[0, parent.id, child.id]` -- because
        `make_post_reply` does not set `path` at all.

        THE BYSTANDER IS THE WITNESS FOR THE `where` CLAUSE. A mutant dropping
        it would flip every reply in the table, and only a row that should NOT
        have changed catches that.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)
        child = make_post_reply(s.post, s.author)
        bystander = make_post_reply(s.post, s.author)
        db.session.commit()
        child.path = [0, s.reply.id, child.id]
        bystander.path = [0, bystander.id]
        child.replies_enabled = True
        bystander.replies_enabled = True
        db.session.commit()

        lock_post_reply(s.reply.id, True, SRC_API, auth=bearer(s.actor))

        db.session.refresh(child)
        db.session.refresh(bystander)
        assert child.replies_enabled is False
        assert bystander.replies_enabled is True

    def test_an_instance_admin_may_lock(self, db_session):
        """`:501`'s SECOND disjunct alone, with the first false."""
        s = _seed_moderated_reply()
        make_instance_admin(s.actor, s.instance)
        s.reply.replies_enabled = True
        db.session.commit()

        lock_post_reply(s.reply.id, True, SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.reply)
        assert s.reply.replies_enabled is False

    def test_an_unprivileged_api_caller_is_not_refused(self, db_session):
        """`:501`'s false arm -- AND IT ASSERTS THE DEFECT ON PURPOSE.

        Neither disjunct holds, so the whole body is skipped and control
        reaches `:519`. The call returns `user.id, post_reply` normally: no
        exception, no flash, nothing changed. The caller cannot tell this
        apart from a success.

        THE TWIN RAISES. `app/shared/post.py:968-969` is
        `elif src == SRC_API: raise Exception('Does not have permission')`,
        and `tests/test_shared_post_moderation.py`'s
        `test_an_unprivileged_user_changes_nothing` pins that shape with
        `pytest.raises`.

        WHOEVER PROPAGATES THAT FIX MUST EDIT THIS TEST. The edit owed is to
        INVERT it: wrap the call in
        `pytest.raises(Exception, match='Does not have permission')` and keep
        both state assertions. Its failure at that point is the fix landing,
        not a regression. Registered as finding 3 in the round's spec.

        THE STATE ASSERTIONS CARRY THE TEST, not the returned tuple. A mutant
        that ran the body and then returned the same tuple would pass a
        return-value assertion and is caught only by `replies_enabled` and the
        empty ModLog.
        """
        s = _seed_moderated_reply()
        s.reply.replies_enabled = True
        db.session.commit()

        user_id, reply = lock_post_reply(s.reply.id, True, SRC_API,
                                         auth=bearer(s.actor))

        assert user_id == s.actor.id
        db.session.refresh(s.reply)
        assert s.reply.replies_enabled is True
        assert db.session.query(ModLog).count() == 0

    def test_locking_writes_the_lock_action(self, db_session):
        """`:506-508`'s add_to_modlog with `:496`'s modlog_type."""
        s = _seed_moderated_reply()
        seed_moderator(s)

        lock_post_reply(s.reply.id, True, SRC_API, auth=bearer(s.actor))

        actions = {row.action for row in db.session.query(ModLog).all()}
        assert actions == {'lock_post_reply'}

    def test_unlocking_writes_the_unlock_action(self, db_session):
        """`:499`'s modlog_type on the false arm of `:494`.

        The counterpart of the test above. Together they prove `:506`'s
        argument is driven by `:494` rather than fixed.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)

        lock_post_reply(s.reply.id, False, SRC_API, auth=bearer(s.actor))

        actions = {row.action for row in db.session.query(ModLog).all()}
        assert actions == {'unlock_post_reply'}

    def test_the_web_arm_flashes_when_locking(self, db_session, app):
        """`:487`'s false arm, `:510`'s true arm, `:511`-`:512`.

        NO `make_site()` HERE -- see the class docstring. This arm only
        flashes; it neither renders a template nor calls `can_downvote`.
        """
        from flask import get_flashed_messages
        s = _seed_moderated_reply()
        seed_moderator(s)

        with web_ctx(app, s.actor):
            lock_post_reply(s.reply.id, True, SRC_WEB, auth=None)
            messages = get_flashed_messages()

        assert 'Comment has been locked.' in messages

    def test_the_web_arm_flashes_a_different_message_when_unlocking(self, db_session, app):
        """`:510`'s false arm, `:515`-`:516`.

        Paired with the test above so `:510`'s two arms are witnessed by
        different message text rather than by the same assertion twice.
        """
        from flask import get_flashed_messages
        s = _seed_moderated_reply()
        seed_moderator(s)

        with web_ctx(app, s.actor):
            lock_post_reply(s.reply.id, False, SRC_WEB, auth=None)
            messages = get_flashed_messages()

        assert 'Comment has been unlocked.' in messages

    def test_the_api_arm_selects_the_lock_task_without_flashing(self, db_session):
        """`:511`'s false arm and `:513`'s task_selector.

        `:511` guards only the flash; `:513` runs on both arms of it. The
        API call reaches `:513` with `:511` false, which is the arc no web
        test can take.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)

        with recording_task_selector() as calls:
            lock_post_reply(s.reply.id, True, SRC_API, auth=bearer(s.actor))

        assert 'lock_post_reply' in calls

    def test_the_api_arm_selects_the_unlock_task(self, db_session):
        """`:515`'s false arm and `:517`'s task_selector."""
        s = _seed_moderated_reply()
        seed_moderator(s)

        with recording_task_selector() as calls:
            lock_post_reply(s.reply.id, False, SRC_API, auth=bearer(s.actor))

        assert 'unlock_post_reply' in calls
