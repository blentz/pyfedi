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
