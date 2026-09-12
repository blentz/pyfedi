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

  - `s.author` LANDS ON id 1, and WHETHER THAT MAKES IT AN ADMIN DEPENDS ON
    WHICH OF THREE PREDICATES YOU ASK -- they disagree for a role-less id-1
    user, which `s.author` is (`make_user`, tests/factories.py:41-67, never
    inserts a `user_role` row). Two say yes, one says no:

    - `User.is_admin()` (app/models.py:1259-1261): `if self.id == 1: return
      True` -- checked before it ever looks at `self.roles`. Admin.
    - `g.admin_ids` (app/request_hooks.py:100-106): a `UNION` of
      `SELECT u.id FROM "user" u WHERE u.id = 1` with a second `SELECT`
      joined to `user_role` for `ROLE_ADMIN`. The id-1 branch is a separate
      `SELECT` with no join, so it needs no `user_role` row. Admin.
    - `Site.admins()` (app/models.py:3999-4000): `.filter_by(deleted=False,
      banned=False).join(user_role).filter(or_(user_role.c.role_id ==
      ROLE_ADMIN, User.id == 1))`. Here the `id == 1` disjunct sits on the
      far side of an INNER join to `user_role`, so a user with zero
      `user_role` rows is dropped before that `WHERE` clause is ever
      evaluated. NOT admin -- the outlier of the three, and, per
      app/request_hooks.py's UNION, arguably a defect rather than a policy:
      the other two predicates treat id 1 as admin unconditionally.

    Task 1's Probe A exercised `Site.admins()` specifically and confirmed
    `[]` even though `s.author.id == 1`. Any test in this file that drives
    `report_post`'s `notify_admins` branch (`:892-900`, which calls
    `Site.admins()`) must therefore seed an explicit admin (a real
    `user_role` row with `ROLE_ADMIN`, or a `g.admin_ids` override) --
    `s.author` alone will not reach `:894`'s loop body. This is specific to
    `Site.admins()`; do not generalise it to `is_admin()` or `g.admin_ids`,
    which both already treat `s.author` as an admin.

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


def test_restoring_increments_both_counters(db_session):
    """`:809`'s author.post_count and `:810`'s community.post_count.

    Catches a regression dropping either increment, which the deleted flag
    alone would not reveal.
    """
    s = seed_post_context(community_name='lifecycle')
    s.post.deleted = True
    s.post.author.post_count = 4
    s.community.post_count = 6
    db.session.commit()

    restore_post(s.post.id, SRC_API, bearer(s.author))

    db.session.refresh(s.post.author)
    db.session.refresh(s.community)
    assert s.post.author.post_count == 5
    assert s.community.post_count == 7


def test_restoring_a_post_with_a_url_links_it_to_its_cross_posts(db_session):
    """`:804`'s true arm and `:805`'s `calculate_cross_posts()`.

    `make_post` leaves `url` unset, so every other test here takes the false
    arm. The assertions are on cross_posts rather than on `deleted`, because
    `deleted` is set at `:807` on EVERY path: a test asserting only that would
    pass with `:805` deleted outright and witness nothing.

    `calculate_cross_posts` (app/models.py:2371-2388) finds other published,
    undeleted posts sharing the url and links them both ways, so a sibling post
    is what makes the call observable. Catches a regression dropping `:805`,
    after which a restored post would come back unlinked from every cross-post
    it belongs with.
    """
    s = seed_post_context(community_name='lifecycle')
    sibling = make_post(s.community, s.voter, 'https://local.example/p/2')
    sibling.url = 'https://example.com/article'
    s.post.url = 'https://example.com/article'
    s.post.deleted = True
    db.session.commit()

    user_id, post = restore_post(s.post.id, SRC_API, bearer(s.author))

    assert user_id == s.author.id
    db.session.refresh(s.post)
    db.session.refresh(sibling)
    assert s.post.cross_posts == [sibling.id]
    assert sibling.cross_posts == [s.post.id]


def test_the_web_arm_reads_current_user_and_returns_none(db_session, app):
    """`:797`'s false arm, `:801`'s `current_user.id`, and `:817`'s bare return.

    Catches a regression making `:816`'s two-tuple unconditional, which would
    change the contract for `app/post/routes.py`'s caller, and one making
    `:797` read the bearer token unconditionally, which would raise with
    auth=None.
    """
    s = seed_post_context(community_name='lifecycle')
    s.post.deleted = True
    db.session.commit()

    with web_ctx(app, s.author):
        result = restore_post(s.post.id, SRC_WEB, None)

    assert result is None
    db.session.refresh(s.post)
    assert s.post.deleted is False


def test_restoring_federates_unconditionally(db_session):
    """`:813`'s task_selector, which has NO guard.

    `delete_post:778` guards its federation on
    `federate_deletion and post.status == POST_STATUS_PUBLISHED`; this function
    has neither parameter nor check, so a never-published post federates a
    restore anyway. That asymmetry is REGISTERED, NOT FIXED by this round --
    this test pins the behaviour as it is, so a later round that decides to
    guard it will see this test fail and know it is changing a recorded
    decision rather than fixing an oversight.
    """
    calls = []
    s = seed_post_context(community_name='lifecycle')
    s.post.deleted = True
    s.post.status = 0
    db.session.commit()

    import app.shared.post as post_module
    original = post_module.task_selector

    def recorder(task_key, **kwargs):
        calls.append(task_key)
        return original(task_key, **kwargs)

    post_module.task_selector = recorder
    try:
        restore_post(s.post.id, SRC_API, bearer(s.author))
    finally:
        post_module.task_selector = original

    assert calls == ['restore_post']
