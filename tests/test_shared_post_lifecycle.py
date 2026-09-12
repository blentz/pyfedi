"""`app/shared/post.py`'s author-lifecycle and reporting functions -- Group C of five.

SCOPE. Three functions, 101 uncovered statements and 56 uncovered branch arcs
when this file was started:

  report_post :821-926 (57/36), delete_post :755-795 (28/14),
  restore_post :796-820 (16/6).

`report_post` is 57 of those 101 statements. It is the largest single function
this campaign has covered in one round, which is why Tasks 5-7 split it.

THE HARNESS IS INHERITED from sub-projects 34 and 35 and needs no new
construction. `seed_post_context`, `web_ctx` and `bearer` live in
tests/factories.py; the rules are tests/README.md facts 206-219. Four bind here:

  - NO TEST MAY REQUEST `redis_double`. delete_post:765 does
    `from app import redis_client` INSIDE the function body and :766 locks on
    it. The fixture reaches that import and then breaks the lock's release,
    which goes through EVALSHA -- unimplemented in fakeredis. Without the
    fixture the call binds to the compose stack's real Redis and works.

  - `s.author` LANDS ON id 1 BUT IS **NOT** AN ADMIN under `seed_post_context`.
    `Site.admins()` (app/models.py:3994-4000) is
    `.filter_by(deleted=False, banned=False).join(user_role).filter(or_(
    user_role.c.role_id == ROLE_ADMIN, User.id == 1))`. The `id == 1` arm of
    the `or_` looks like it should catch `s.author`, but the preceding
    `.join(user_role)` is an INNER join: a user with zero rows in `user_role`
    is dropped before the `WHERE` clause is ever evaluated. `make_user`
    (tests/factories.py:41-67) never inserts a `user_role` row, so every
    seeded user -- author included -- is invisible to `Site.admins()`
    regardless of id. Task 1's Probe A confirmed this empirically:
    `Site.admins()` returned `[]` even though `s.author.id == 1`. Any test in
    this file that needs `notify_admins` to actually notify someone must seed
    an explicit admin (a real `user_role` row with `ROLE_ADMIN`, or a
    `g.admin_ids` override) -- `s.author` alone will not do it.

  - SRC_API ARMS NEED NO REQUEST CONTEXT. `get_ip_address` swallows the
    missing-context RuntimeError, and Task 1's Probe B confirmed
    `report_post`'s SRC_API arm -- including its `:877` `force_locale`/
    `gettext` call -- runs cleanly with no request context pushed.

  - `delete_post` IS CALLED FROM CELERY TASKS WITH NO REQUEST CONTEXT.
    app/shared/tasks/maintenance.py:150 and :185 both call it with SRC_WEB and
    auth=None. There `current_user` resolves to None, :760's `if current_user:`
    is False, and :763's `user_id = 1` fallback fires exactly as its comment
    says. That path is LIVE, confirmed by Task 1's Probe C: `deleted_by` came
    back `1`. An earlier draft of the spec called it unreachable and proposed
    "fixing" :760; reading the callers refuted that, and the refutation is
    registered so nobody re-derives it.
"""

import pytest

from app import db
from app.constants import (
    NOTIF_REPORT,
    NOTIF_REPORT_ESCALATION,
    POST_STATUS_PUBLISHED,
    REPORT_TYPE_POST,
    SRC_API,
    SRC_WEB,
)
from app.models import Instance, Notification, Report, User
from app.shared.post import delete_post, report_post, restore_post
from tests.factories import (
    bearer,
    make_community_member,
    make_instance,
    make_post,
    make_user,
    seed_post_context,
    web_ctx,
)


def seed_local_moderator(s, name='localmod'):
    """A LOCAL moderator of `s.community`, for report_post's :876 true arm.

    `Community.moderators()` (app/models.py:716-722) excludes banned members,
    and `moderator.is_local()` at :876 decides whether the moderator gets a
    Notification or lands in `remote_instance_ids`.
    """
    mod = make_user(s.instance, name, local=True)
    make_community_member(mod, s.community, is_moderator=True)
    return mod


def seed_remote_moderator(s, domain='remote.example', name='remotemod'):
    """A moderator on a DIFFERENT instance, for report_post's :876 false arm.

    Returns `(instance, user)`. The instance is distinct from `s.instance` so
    `moderator.is_local()` is False and :886's report_remote fork is reached.
    """
    instance = make_instance(domain, software='lemmy')
    mod = make_user(instance, name)
    make_community_member(mod, s.community, is_moderator=True)
    return instance, mod


def test_restoring_a_deleted_post_clears_the_flag(db_session):
    """`:807`'s `post.deleted = False` and `:808`'s `deleted_by = None`.

    Catches a regression dropping either assignment, which would leave a
    restored post still marked deleted or still attributed to its deleter.
    """
    s = seed_post_context(community_name='lifecycle')
    s.post.deleted = True
    s.post.deleted_by = s.voter.id
    db.session.commit()

    user_id, post = restore_post(s.post.id, SRC_API, bearer(s.author))

    assert user_id == s.author.id
    db.session.refresh(s.post)
    assert s.post.deleted is False
    assert s.post.deleted_by is None
