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
      evaluated. NOT admin -- IN THIS HARNESS. See below; that is not
      production's answer.

    THE THIRD ROW IS A HARNESS ARTIFACT. `Site.admins()` has two branches:
    app/models.py:3996-3997 returns `User.id.in_(tuple(g.admin_ids))` whenever
    `g.admin_ids` is set, and only falls through to the join query when it is
    not. app/request_hooks.py:79 is an `@app.before_request` (registered at
    app/__init__.py:366) setting `g.admin_ids` on every request except
    `/inbox` and `/static/` (`:94`). So on a live request path all three
    predicates AGREE. The join branch is reached only with `g.admin_ids`
    unset -- outside a request context, or on `/inbox`. This file never
    dispatches a request: `web_ctx` uses `test_request_context`, which pushes
    a context but does not run `before_request`. Hence the `[]`.

    Task 1's Probe A exercised `Site.admins()` specifically and confirmed
    `[]` even though `s.author.id == 1`. Any test in this file that drives
    `report_post`'s `notify_admins` branch (`:892-900`, which calls
    `Site.admins()`) must therefore seed an explicit admin (a real
    `user_role` row with `ROLE_ADMIN`) -- `s.author` alone will not reach
    `:894`'s loop body HERE. Do not read that as a production defect, and do
    not generalise it to `is_admin()` or `g.admin_ids`, which treat
    `s.author` as an admin everywhere.

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
from sqlalchemy import text

from app import db
from app.constants import (
    NOTIF_REPORT,
    NOTIF_REPORT_ESCALATION,
    POST_STATUS_PUBLISHED,
    REPORT_TYPE_POST,
    ROLE_ADMIN,
    SRC_API,
    SRC_WEB,
)
from app.models import Instance, Notification, Report, Role, User, user_role
from app.shared.post import delete_post, report_post, restore_post
from tests.factories import (
    bearer,
    make_community,
    make_community_member,
    make_instance,
    make_post,
    make_site,
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


def seed_site_admin(s, name='siteadmin'):
    """A user `Site.admins()` actually returns.

    `Site.admins()` (app/models.py:3999-4000) INNER-JOINS user_role before
    applying `or_(role_id == ROLE_ADMIN, User.id == 1)`, so a user with no
    user_role row produces no rows at all and the id-1 disjunct is never
    reached. `s.author` is User id 1 and has no role row, so it is NOT a site
    admin -- Probe A observed `Site.admins() == []`.

    The Role is created with an explicit id because user_role.role_id is a
    foreign key to role.id and the query matches on that id, not on the role's
    name.
    """
    role = db.session.get(Role, ROLE_ADMIN)
    if role is None:
        role = Role(id=ROLE_ADMIN, name='Admin', weight=0)
        db.session.add(role)
        db.session.commit()
    admin = make_user(s.instance, name, local=True)
    db.session.execute(user_role.insert().values(user_id=admin.id,
                                                 role_id=ROLE_ADMIN))
    db.session.commit()
    return admin


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
    """`:797`'s false arm, `:801`'s `current_user.id`, and `:818`'s bare return.

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


def _restore_recording_tasks(s):
    calls = []
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
    return calls


def test_an_unpublished_post_does_not_federate_its_restore(db_session):
    """D439, fixed: restore_post federated unconditionally, so a
    never-published post sent a restore. It now uses delete_post's guard,
    `post.status == POST_STATUS_PUBLISHED`."""
    s = seed_post_context(community_name='lifecycle')
    s.post.deleted = True
    s.post.status = 0
    db.session.commit()

    assert _restore_recording_tasks(s) == []


def test_a_published_post_federates_its_restore(db_session):
    """The other side of the D439 guard."""
    s = seed_post_context(community_name='lifecycle')
    s.post.deleted = True
    s.post.status = POST_STATUS_PUBLISHED
    db.session.commit()

    assert _restore_recording_tasks(s) == ['restore_post']


def test_restoring_touches_the_authors_last_seen(db_session):
    """D439, fixed: delete_post touches `author.last_seen` and restore_post
    did not. It now does."""
    s = seed_post_context(community_name='lifecycle')
    s.post.deleted = True
    s.post.author.last_seen = None
    db.session.commit()

    restore_post(s.post.id, SRC_API, bearer(s.author))

    db.session.refresh(s.post.author)
    assert s.post.author.last_seen is not None


def test_restoring_takes_the_post_lock_the_delete_takes(db_session, monkeypatch):
    """D439, fixed: restore_post mutated the counters under no lock, where
    delete_post holds `lock:post:<id>`, so a restore racing a delete was not
    serialised. It now takes the same lock."""
    import app
    real = app.redis_client
    keys = []

    class _Recording:
        def lock(self, key, *args, **kwargs):
            keys.append(key)
            return real.lock(key, *args, **kwargs)

    monkeypatch.setattr(app, 'redis_client', _Recording())
    s = seed_post_context(community_name='lifecycle')
    s.post.deleted = True
    db.session.commit()

    restore_post(s.post.id, SRC_API, bearer(s.author))

    assert keys == [f'lock:post:{s.post.id}']


def test_restoring_through_the_api_requires_the_posts_own_author(db_session):
    """`:799`'s `id_match=post.user_id` authorisation -- a REFUSAL, not a value.

    Every SRC_API `restore_post` test above passes a bearer token that
    already belongs to the post's author, so the `user_id` `:799` returns is
    identical whether it comes from `authorise_api_user`'s ownership check or
    from a regression that just reads `post.user_id` directly and never
    checks the bearer at all -- the two arms produce the SAME value in the
    success case, and a mutation collapsing `:799` to the latter passes every
    test above silently. Only a call from someone who is NOT the author can
    tell them apart.

    `s.voter` (id 2) did not author `s.post` (`s.author`, id 1), so `:799`
    must reject the call. `app/utils.py:3634`'s `id_match` check raises a
    bare `Exception('incorrect_login')` on mismatch -- the same rejection
    tested directly in `tests/test_utils_api_auth.py`. The post staying
    deleted afterward additionally confirms the rejection happened before any
    of `:807-811`'s mutations, not after.
    """
    s = seed_post_context(community_name='lifecycle')
    s.post.deleted = True
    db.session.commit()

    with pytest.raises(Exception, match='incorrect_login'):
        restore_post(s.post.id, SRC_API, bearer(s.voter))

    db.session.refresh(s.post)
    assert s.post.deleted is True


def test_deleting_through_the_api_sets_the_flag_and_attributes_it(db_session):
    """`:756`'s true arm, `:771`'s flag, `:772`'s deleted_by, `:791`'s return.

    `:758` authorises with `id_match=post.user_id`, so the actor must be the
    post's author. THE POST IS AUTHORED BY `s.voter`, NOT `s.author`, and that
    is the whole point of the fixture: `s.author` is User id 1, which is
    exactly the literal `:763` assigns on the no-context path. A test deleting
    `s.post` and asserting `deleted_by == s.author.id` asserts
    `deleted_by == 1`, which an INVERTED `:756` would also satisfy -- skipping
    `:758`'s authorisation check entirely and still passing. Authoring the post
    as `s.voter` (id 2) makes the two arms produce different values.

    Catches a regression dropping `:772`, and a regression inverting `:756`,
    which would delete a post through the API with no authorisation at all.
    """
    s = seed_post_context(community_name='lifecycle')
    voter_post = make_post(s.community, s.voter, 'https://local.example/p/2')

    user_id, post = delete_post(voter_post.id, False, SRC_API, bearer(s.voter))

    assert user_id == s.voter.id
    assert s.voter.id != 1
    db.session.refresh(voter_post)
    assert voter_post.deleted is True
    assert voter_post.deleted_by == s.voter.id


def test_deleting_decrements_both_counters_and_touches_last_seen(db_session):
    """`:773`'s author.post_count, `:774`'s last_seen, `:775`'s community count.

    Catches a regression dropping any of the three.
    """
    s = seed_post_context(community_name='lifecycle')
    s.post.author.post_count = 5
    s.community.post_count = 7
    s.post.author.last_seen = None
    db.session.commit()

    delete_post(s.post.id, False, SRC_API, bearer(s.author))

    db.session.refresh(s.post.author)
    db.session.refresh(s.community)
    assert s.post.author.post_count == 4
    assert s.community.post_count == 6
    assert s.post.author.last_seen is not None


def test_deleting_an_already_deleted_post_is_a_no_op(db_session, monkeypatch):
    """N2, fixed: a repeated delete decremented both counters again and
    federated a second Delete. It is now an idempotent no-op, as D506 made
    delete_reply."""
    s = seed_post_context(community_name='lifecycle')
    s.post.deleted = True
    s.post.deleted_by = s.author.id
    s.post.status = POST_STATUS_PUBLISHED
    s.post.author.post_count = 5
    s.community.post_count = 7
    db.session.commit()
    calls = []
    monkeypatch.setattr('app.shared.post.task_selector',
                        lambda task_key, **kwargs: calls.append(task_key))

    user_id, post = delete_post(s.post.id, True, SRC_API, bearer(s.author))

    assert (user_id, post.id) == (s.author.id, s.post.id)
    db.session.refresh(s.post.author)
    db.session.refresh(s.community)
    assert s.post.author.post_count == 5
    assert s.community.post_count == 7
    assert calls == []


def test_restoring_a_post_that_is_not_deleted_is_a_no_op(db_session, monkeypatch):
    """N2, fixed: restoring a live post incremented both counters again and
    federated a restore. It is now an idempotent no-op, as D506 made
    restore_reply."""
    s = seed_post_context(community_name='lifecycle')
    s.post.status = POST_STATUS_PUBLISHED
    s.post.author.post_count = 5
    s.community.post_count = 7
    db.session.commit()
    calls = []
    monkeypatch.setattr('app.shared.post.task_selector',
                        lambda task_key, **kwargs: calls.append(task_key))

    user_id, post = restore_post(s.post.id, SRC_API, bearer(s.author))

    assert (user_id, post.id) == (s.author.id, s.post.id)
    db.session.refresh(s.post.author)
    db.session.refresh(s.community)
    assert s.post.author.post_count == 5
    assert s.community.post_count == 7
    assert calls == []


def test_the_celery_path_attributes_the_deletion_to_user_one(db_session):
    """`:760`'s FALSE arm and `:763`'s `user_id = 1`.

    This is the live maintenance-task path. `app/shared/tasks/maintenance.py:150`
    and `:185` call `delete_post` with SRC_WEB and auth=None from inside Celery
    task bodies, where Flask-Login's `current_user` proxy resolves to None. No
    `web_ctx` here -- the absence of a request context IS the condition under
    test.

    Catches a regression changing `:760` to `current_user.is_authenticated`,
    which would raise on a None proxy and break both maintenance tasks.

    The post is authored by `s.voter` (id 2) so that `deleted_by == 1` means
    `:763`'s literal and nothing else. Deleting `s.post`, whose author IS id 1,
    would leave the assertion satisfiable by a regression that attributed the
    deletion to the post's own author instead of to the fallback.
    """
    s = seed_post_context(community_name='lifecycle')
    voter_post = make_post(s.community, s.voter, 'https://local.example/p/2')

    delete_post(voter_post.id, False, SRC_WEB, None)

    db.session.refresh(voter_post)
    assert voter_post.deleted is True
    assert voter_post.deleted_by == 1
    assert voter_post.user_id != 1


def test_the_web_path_with_a_logged_in_user_attributes_to_them(db_session, app):
    """`:760`'s TRUE arm and `:761`'s `current_user.id`.

    The counterpart of the test above, and the reason `:760` is a fork rather
    than dead code. Catches a regression hardcoding `:763`'s fallback.
    """
    s = seed_post_context(community_name='lifecycle')

    with web_ctx(app, s.voter):
        result = delete_post(s.post.id, False, SRC_WEB, None)

    assert result is None
    db.session.refresh(s.post)
    assert s.post.deleted_by == s.voter.id


def test_deleting_a_post_with_a_url_tears_down_its_cross_post_links(db_session):
    """`:768`'s true arm and `:769`'s `calculate_cross_posts(delete_only=True)`.

    `delete_only=True` reaches app/models.py:2350-2356, which clears this
    post's `cross_posts` and removes this post's id from each sibling that
    listed it. The assertions are on cross_posts rather than on `deleted`,
    because `deleted` is set at `:771` on every path: a test asserting only
    that would pass with `:769` deleted outright.

    Catches a regression dropping `:769`, after which a deleted post would stay
    listed in its siblings' cross_posts and keep appearing as a cross-post of a
    post that no longer exists.
    """
    s = seed_post_context(community_name='lifecycle')
    sibling = make_post(s.community, s.voter, 'https://local.example/p/2')
    sibling.url = 'https://example.com/article'
    s.post.url = 'https://example.com/article'
    s.post.cross_posts = [sibling.id]
    sibling.cross_posts = [s.post.id]
    db.session.commit()

    delete_post(s.post.id, False, SRC_API, bearer(s.author))

    db.session.refresh(s.post)
    db.session.refresh(sibling)
    assert s.post.cross_posts == []
    assert sibling.cross_posts == []


def test_a_published_post_federates_its_deletion(db_session):
    """`:778`'s true arm with BOTH conjuncts true, and `:779`'s task_selector.

    Positive control for the two suppression tests below: this is the sibling
    that shows `calls` is non-empty when both conjuncts hold, so an empty
    `calls` in those tests is a real refusal rather than a broken fixture.
    """
    calls = []
    s = seed_post_context(community_name='lifecycle')
    s.post.status = POST_STATUS_PUBLISHED
    db.session.commit()

    import app.shared.post as post_module
    original = post_module.task_selector

    def recorder(task_key, **kwargs):
        calls.append(task_key)
        return original(task_key, **kwargs)

    post_module.task_selector = recorder
    try:
        delete_post(s.post.id, True, SRC_API, bearer(s.author))
    finally:
        post_module.task_selector = original

    assert calls == ['delete_post']


def test_federate_deletion_false_suppresses_the_federation(db_session):
    """`:778`'s FIRST conjunct taken false, with the second true.

    This is the maintenance tasks' call shape --
    `app/shared/tasks/maintenance.py:150` passes `False`. Catches a regression
    dropping the `federate_deletion` conjunct, which would federate every
    retention-policy deletion to every peer. Positive control:
    `test_a_published_post_federates_its_deletion` above, same fixture shape,
    non-empty `calls`.
    """
    calls = []
    s = seed_post_context(community_name='lifecycle')
    s.post.status = POST_STATUS_PUBLISHED
    db.session.commit()

    import app.shared.post as post_module
    original = post_module.task_selector

    def recorder(task_key, **kwargs):
        calls.append(task_key)
        return original(task_key, **kwargs)

    post_module.task_selector = recorder
    try:
        delete_post(s.post.id, False, SRC_API, bearer(s.author))
    finally:
        post_module.task_selector = original

    assert calls == []


def test_an_unpublished_post_does_not_federate_its_deletion(db_session):
    """`:778`'s SECOND conjunct taken false, with the first true.

    A post no peer ever saw must not federate a delete. Catches a regression
    dropping the status conjunct. Positive control:
    `test_a_published_post_federates_its_deletion` above, same fixture shape,
    non-empty `calls`.
    """
    calls = []
    s = seed_post_context(community_name='lifecycle')
    s.post.status = 0
    db.session.commit()

    import app.shared.post as post_module
    original = post_module.task_selector

    def recorder(task_key, **kwargs):
        calls.append(task_key)
        return original(task_key, **kwargs)

    post_module.task_selector = recorder
    try:
        delete_post(s.post.id, True, SRC_API, bearer(s.author))
    finally:
        post_module.task_selector = original

    assert calls == []


def test_deleting_removes_ordinary_notifications_about_the_post(db_session):
    """`:787`'s delete, reached through `:785`'s false arm."""
    from tests.factories import make_notification
    from app.constants import NOTIF_POST

    s = seed_post_context(community_name='lifecycle')
    make_notification(s.voter, s.post, notif_type=NOTIF_POST)

    delete_post(s.post.id, False, SRC_API, bearer(s.author))

    assert db.session.query(Notification).count() == 0


def test_deleting_keeps_report_notifications(db_session):
    """`:785`'s FIRST disjunct and `:786`'s continue.

    A report notification must survive the deletion it reported.
    """
    from tests.factories import make_notification

    s = seed_post_context(community_name='lifecycle')
    make_notification(s.voter, s.post, notif_type=NOTIF_REPORT)

    delete_post(s.post.id, False, SRC_API, bearer(s.author))

    remaining = db.session.query(Notification).all()
    assert len(remaining) == 1
    assert remaining[0].notif_type == NOTIF_REPORT


def test_deleting_keeps_escalated_report_notifications(db_session):
    """`:785`'s SECOND disjunct, with the first false.

    `:785` is one arc pair to coverage.py; this and the test above are the
    only way to distinguish its operands.
    """
    from tests.factories import make_notification

    s = seed_post_context(community_name='lifecycle')
    make_notification(s.voter, s.post, notif_type=NOTIF_REPORT_ESCALATION)

    delete_post(s.post.id, False, SRC_API, bearer(s.author))

    remaining = db.session.query(Notification).all()
    assert len(remaining) == 1
    assert remaining[0].notif_type == NOTIF_REPORT_ESCALATION


def test_deleting_with_no_notifications_takes_the_loops_zero_exit(db_session):
    """`:783`'s zero-iteration exit arc.

    This test's positive signal is weak -- `post.deleted is True` would hold
    on several paths -- and it is recorded as pinning the ARC rather than a
    behavioural difference. Task 10's mutation pass covers the loop directly.
    """
    s = seed_post_context(community_name='lifecycle')

    delete_post(s.post.id, False, SRC_API, bearer(s.author))

    db.session.refresh(s.post)
    assert s.post.deleted is True
    assert db.session.query(Notification).count() == 0


def test_deleting_builds_no_new_cross_post_links(db_session):
    """The `delete_only=True` ARGUMENT at `:769`, not merely the call.

    app/models.py:2360 returns as soon as the teardown is done when
    `delete_only` is set. WITHOUT the argument the function falls through to
    :2371's search and would LINK the post being deleted to every sibling
    sharing its url -- the opposite of what a delete should do.

    This is the only test that distinguishes `calculate_cross_posts(delete_only=True)`
    from a bare `calculate_cross_posts()`; the teardown test above passes under
    both, because the teardown runs first either way. Starts with no links so a
    link appearing can only have come from the search branch.
    """
    s = seed_post_context(community_name='lifecycle')
    sibling = make_post(s.community, s.voter, 'https://local.example/p/2')
    sibling.url = 'https://example.com/article'
    s.post.url = 'https://example.com/article'
    db.session.commit()

    delete_post(s.post.id, False, SRC_API, bearer(s.author))

    db.session.refresh(s.post)
    db.session.refresh(sibling)
    assert not s.post.cross_posts
    assert not sibling.cross_posts


def test_an_api_report_records_the_reason_and_description(db_session):
    """`:822`'s true arm, `:857-866`'s Report construction, `:922`'s return.

    `:857` and `:858` truncate to 255 characters; this pins the ordinary case.
    """
    s = seed_post_context(community_name='lifecycle')

    reporter_id, report = report_post(
        s.post,
        {'reason': 'spam', 'description': 'unsolicited', 'report_remote': False},
        SRC_API,
        auth=bearer(s.voter),
    )

    assert reporter_id == s.voter.id
    rows = db.session.query(Report).all()
    assert len(rows) == 1
    assert rows[0].reasons == 'spam'
    assert rows[0].description == 'unsolicited'
    assert rows[0].type == REPORT_TYPE_POST
    assert rows[0].suspect_post_id == s.post.id


def test_a_doxing_report_notifies_admins_through_the_api(db_session):
    """`:828`'s SECOND needle, the one that works.

    `'doxing'` is already lowercase so it matches a `.lower()`ed haystack.
    `'Minor abuse'` does not -- that is PC1, and Task 8 observes it failing.

    `Site.admins()` is empty without `seed_site_admin`, so without it this test
    would pass for the wrong reason after PC1 lands and fail for the wrong
    reason before it.
    """
    s = seed_post_context(community_name='lifecycle')
    admin = seed_site_admin(s)

    report_post(
        s.post,
        {'reason': 'Sharing personal info - doxing',
         'description': 'unsolicited', 'report_remote': False},
        SRC_API,
        auth=bearer(s.voter),
    )

    admin_notifs = db.session.query(Notification).filter_by(
        title='Suspicious content').all()
    assert len(admin_notifs) == 1
    assert admin_notifs[0].user_id == admin.id


def test_an_ordinary_api_report_does_not_notify_admins(db_session):
    """`:828-830`'s false arm -- all three disjuncts false.

    Catches a regression making `notify_admins` unconditional, which would
    escalate every report to every admin.

    Seeds an admin so the zero is a real refusal rather than an empty
    `Site.admins()`.
    """
    s = seed_post_context(community_name='lifecycle')
    seed_site_admin(s)

    report_post(
        s.post,
        {'reason': 'spam', 'description': 'unsolicited', 'report_remote': False},
        SRC_API,
        auth=bearer(s.voter),
    )

    assert db.session.query(Notification).filter_by(
        title='Suspicious content').count() == 0


def test_an_api_ai_flair_report_notifies_admins_on_a_non_piefed_instance(db_session):
    """`:830`'s THIRD disjunct, with both conjuncts true.

    `:830` uses exact equality and never calls `.lower()`, which is why it
    works where `:828`'s first needle does not.
    """
    s = seed_post_context(community_name='lifecycle')
    seed_site_admin(s)
    s.instance.software = 'lemmy'
    db.session.commit()

    report_post(
        s.post,
        {'reason': 'AI content that needs flair', 'description': 'x',
         'report_remote': False},
        SRC_API,
        auth=bearer(s.voter),
    )

    assert db.session.query(Notification).filter_by(
        title='Suspicious content').count() == 1


def test_an_api_ai_flair_report_does_not_escalate_on_piefed(db_session):
    """`:830`'s SECOND conjunct taken false.

    A PieFed instance handles its own flair, so the escalation is suppressed.
    Catches a regression dropping the software check.
    """
    s = seed_post_context(community_name='lifecycle')
    seed_site_admin(s)
    s.instance.software = 'piefed'
    db.session.commit()

    report_post(
        s.post,
        {'reason': 'AI content that needs flair', 'description': 'x',
         'report_remote': False},
        SRC_API,
        auth=bearer(s.voter),
    )

    assert db.session.query(Notification).filter_by(
        title='Suspicious content').count() == 0


def test_an_api_report_notifies_a_local_moderator(db_session):
    """`:876`'s true arm and `:878-883`'s Notification.

    Catches a regression inverting `:876`, which would route a local moderator
    into `remote_instance_ids` and federate a Flag to the local instance.
    """
    s = seed_post_context(community_name='lifecycle')
    mod = seed_local_moderator(s)

    report_post(
        s.post,
        {'reason': 'spam', 'description': 'x', 'report_remote': False},
        SRC_API,
        auth=bearer(s.voter),
    )

    notifs = db.session.query(Notification).filter_by(
        title='A post has been reported').all()
    assert {n.user_id for n in notifs} == {mod.id}


def test_an_api_report_bumps_the_local_moderators_unread_count(db_session):
    """D443, fixed: the moderator's report notification never bumped
    `unread_notifications`, where the admin one does. It now does."""
    s = seed_post_context(community_name='lifecycle')
    mod = seed_local_moderator(s)
    mod.unread_notifications = 0
    db.session.commit()

    report_post(
        s.post,
        {'reason': 'spam', 'description': 'x', 'report_remote': False},
        SRC_API,
        auth=bearer(s.voter),
    )

    db.session.refresh(mod)
    assert mod.unread_notifications == 1


def test_an_api_report_gives_a_remote_moderator_no_local_notification(db_session):
    """`:876`'s false arm.

    A remote moderator is reached by a federated Flag, not a local
    Notification row. Positive control: `test_an_api_report_notifies_a_local_moderator`
    above, same fixture shape and same notification title, non-empty.
    """
    s = seed_post_context(community_name='lifecycle')
    seed_remote_moderator(s)

    report_post(
        s.post,
        {'reason': 'spam', 'description': 'x', 'report_remote': False},
        SRC_API,
        auth=bearer(s.voter),
    )

    assert db.session.query(Notification).filter_by(
        title='A post has been reported').count() == 0


def test_api_report_remote_true_adds_a_moderator_the_filter_would_exclude(db_session):
    """`:886`'s FALSE arm and `:890`'s UNCONDITIONAL add.

    THE MODERATOR MUST BE ONE `:887` WOULD REJECT. This one is non-local but
    carries the community's own `instance_id`, so `:887`'s second conjunct is
    false and the filtered arm would drop it. Under `report_remote` the add at
    `:890` happens anyway.

    That fixture is the whole test. A moderator on some THIRD instance --
    distinct from both the suspect's and the community's -- passes `:887` too,
    so a test using one would pass identically whether `:886` routed it to
    `:890` or to `:888`, and would witness nothing about which arm ran. Swap
    the arms here and `remote_instance_ids` is empty, `:914` is false, and no
    `task_selector` call happens at all.

    `make_user(..., local=False)` sets `ap_id` (tests/factories.py:60) while
    leaving `instance_id` at the instance passed in, and `User.is_local()`
    (app/models.py:1252) reads only `ap_id`/`ap_profile_id` -- never
    `instance_id`. That is what lets a user be non-local and still sit on the
    local instance.
    """
    calls = []
    s = seed_post_context(community_name='lifecycle')
    oddmod = make_user(s.instance, 'filtermod', local=False)
    make_community_member(oddmod, s.community, is_moderator=True)

    import app.shared.post as post_module
    original = post_module.task_selector

    def recorder(task_key, **kwargs):
        calls.append((task_key, kwargs.get('instance_ids')))
        return original(task_key, **kwargs)

    post_module.task_selector = recorder
    try:
        report_post(
            s.post,
            {'reason': 'spam', 'description': 'x', 'report_remote': True},
            SRC_API,
            auth=bearer(s.voter),
        )
    finally:
        post_module.task_selector = original

    assert len(calls) == 1
    assert calls[0][0] == 'report_post'
    assert set(calls[0][1]) == {s.instance.id}


def test_api_report_remote_false_excludes_the_suspects_own_instance(db_session):
    """`:886`'s TRUE arm and `:887`'s FIRST conjunct taken false.

    Without opt-in, a moderator on the suspect's own instance is excluded --
    the reporter has not consented to their report reaching the instance
    hosting the person they reported. Catches a regression dropping that
    conjunct. Positive control: `test_api_report_remote_true_adds_a_moderator_the_filter_would_exclude`
    above, non-empty `calls`.
    """
    calls = []
    s = seed_post_context(community_name='lifecycle')
    remote_instance, mod = seed_remote_moderator(s)
    s.post.author.instance_id = remote_instance.id
    db.session.commit()

    import app.shared.post as post_module
    original = post_module.task_selector

    def recorder(task_key, **kwargs):
        calls.append(task_key)
        return original(task_key, **kwargs)

    post_module.task_selector = recorder
    try:
        report_post(
            s.post,
            {'reason': 'spam', 'description': 'x', 'report_remote': False},
            SRC_API,
            auth=bearer(s.voter),
        )
    finally:
        post_module.task_selector = original

    assert calls == []


def test_api_report_remote_false_excludes_a_moderator_sharing_the_communitys_instance(db_session):
    """`:887`'s SECOND conjunct taken false, with the first conjunct true.

    `oddmod`'s `instance_id` equals the community's (1, the local instance
    `seed_post_context` seeds first, per `make_community`'s hardcoded
    `instance_id=1`), but its `ap_id` is set (`make_user(..., local=False)`),
    so `User.is_local()` (app/models.py:1251-1252, `self.ap_id is None or
    self.ap_profile_id.startswith(SERVER_URL)`) is False -- `is_local()` reads
    `ap_id`/`ap_profile_id`, never `instance_id`, so this combination is a
    legitimate state under the model's own definitions even though it looks
    contradictory. `:886`'s false arm is therefore reached for oddmod despite
    `instance_id` alone suggesting "local".

    The suspect (`s.post.author`) is moved to a THIRD instance, distinct from
    both moderators', so oddmod's FIRST conjunct
    (`moderator.instance_id != suspect_user.instance_id`, 1 != other_instance.id)
    is true while its SECOND (`moderator.instance_id != post.community.instance_id`,
    1 != 1) is false -- the arrangement `:887`'s second conjunct needs a
    witness for.

    `realmod`, a genuine remote moderator on a FOURTH instance, is the
    positive control: its instance DOES land in `remote_instance_ids`, which
    is what shows oddmod's absence below is a real exclusion enforced by the
    second conjunct rather than an empty/broken fixture. Catches a regression
    dropping that conjunct, which would let oddmod's instance (1) into the
    call too.
    """
    calls = []
    s = seed_post_context(community_name='lifecycle')
    oddmod = make_user(s.instance, 'oddmod', local=False)
    make_community_member(oddmod, s.community, is_moderator=True)
    assert oddmod.instance_id == s.community.instance_id
    assert oddmod.is_local() is False

    remote_instance, _realmod = seed_remote_moderator(
        s, domain='genuine.example', name='realmod')

    other_instance = make_instance('elsewhere.example')
    s.post.author.instance_id = other_instance.id
    db.session.commit()

    import app.shared.post as post_module
    original = post_module.task_selector

    def recorder(task_key, **kwargs):
        calls.append((task_key, kwargs.get('instance_ids')))
        return original(task_key, **kwargs)

    post_module.task_selector = recorder
    try:
        report_post(
            s.post,
            {'reason': 'spam', 'description': 'x', 'report_remote': False},
            SRC_API,
            auth=bearer(s.voter),
        )
    finally:
        post_module.task_selector = original

    assert len(calls) == 1
    assert calls[0][0] == 'report_post'
    assert set(calls[0][1]) == {remote_instance.id}


def test_a_moderator_row_whose_user_is_gone_is_skipped(db_session):
    """`:875`'s false arm.

    `:874` looks the moderator up by id and `:875` guards the result, so a
    CommunityMember row whose User has been deleted is skipped rather than
    raising. Catches a regression dropping the guard. Positive control:
    `test_an_api_report_notifies_a_local_moderator` above, same fixture shape
    (a single local moderator), non-empty Notification count.
    """
    s = seed_post_context(community_name='lifecycle')
    mod = seed_local_moderator(s)
    orphan_id = mod.id
    # community_member.user_id carries a live FOREIGN KEY to user.id (RESTRICT,
    # not CASCADE), so an ordinary delete of `mod` would raise
    # ForeignKeyViolation rather than produce the orphaned row this test
    # needs. `session_replication_role = replica` disables FK triggers for the
    # rest of this transaction -- the same trick tests/conftest.py's own
    # per-test teardown SQL uses for the identical reason -- so the
    # CommunityMember row survives with a user_id no "user" row backs.
    db.session.execute(text('SET LOCAL session_replication_role = replica'))
    db.session.delete(mod)
    db.session.commit()

    reporter_id, report = report_post(
        s.post,
        {'reason': 'spam', 'description': 'x', 'report_remote': False},
        SRC_API,
        auth=bearer(s.voter),
    )

    assert reporter_id == s.voter.id
    assert db.session.query(Notification).filter_by(
        title='A post has been reported').count() == 0


def test_an_unmoderated_local_community_always_notifies_admins_through_the_api(db_session):
    """`:841`'s two-conjunct override and `:842`'s assignment.

    An unmoderated local community has no moderators to notify, so every report
    escalates regardless of reason. Catches a regression dropping the override.

    The community under `seed_post_context` is local -- `make_community` never
    sets `ap_id` and `Community.is_local()` (app/models.py:796) is
    `self.ap_id is None or ...` -- so `:841`'s first conjunct is already true.
    """
    s = seed_post_context(community_name='lifecycle')
    seed_site_admin(s)
    s.community.un_moderated = True
    db.session.commit()

    report_post(
        s.post,
        {'reason': 'spam', 'description': 'x', 'report_remote': False},
        SRC_API,
        auth=bearer(s.voter),
    )

    assert db.session.query(Notification).filter_by(
        title='Suspicious content').count() == 1


def test_an_unmoderated_remote_community_does_not_force_admin_notification(db_session):
    """`:841`'s FIRST conjunct (`is_local()`) taken false, with the second
    (`un_moderated`) true.

    The override at `:842` is scoped to LOCAL unmoderated communities -- a
    remote community's `un_moderated` flag describes moderation on ITS home
    instance, not ours, so it must not force a local admin escalation. Catches
    a regression collapsing the two-conjunct guard down to `un_moderated`
    alone, which would force every report on a remote unmoderated community to
    escalate regardless of reason.

    `reason='spam'` matches none of `:828-830`'s needles, so `notify_admins`
    has no other source here: a True count below could only come from the
    override reaching too far. Positive control:
    `test_an_unmoderated_local_community_always_notifies_admins_through_the_api`
    above, identical fixture shape apart from locality, non-zero count.
    """
    s = seed_post_context(community_name='lifecycle')
    seed_site_admin(s)
    s.community.un_moderated = True
    remote_instance = make_instance('remote.example', software='lemmy')
    s.community.ap_id = f'lifecycle@{remote_instance.domain}'
    s.community.ap_profile_id = f'https://{remote_instance.domain}/c/lifecycle'
    db.session.commit()
    assert s.community.is_local() is False

    report_post(
        s.post,
        {'reason': 'spam', 'description': 'x', 'report_remote': False},
        SRC_API,
        auth=bearer(s.voter),
    )

    assert db.session.query(Notification).filter_by(
        title='Suspicious content').count() == 0


def test_an_admin_who_is_already_a_notified_moderator_is_not_notified_twice(db_session):
    """`:894`'s false arm.

    `already_notified` is populated at `:884` for local moderators only. An
    admin who moderates the community is in that set and must not receive a
    second notification. Catches a regression dropping the guard.

    The admin must be seeded with `seed_site_admin` (Task 5): `Site.admins()`
    is empty otherwise, which would make the `== 0` assertion below pass
    vacuously and witness nothing. `seed_site_admin` mints a LOCAL user, so
    `:876` routes it to the notification branch and `:884` adds it to
    `already_notified`.

    `reason='spam'` matches none of `:828`'s needles, so `notify_admins` is
    forced True here by `s.community.un_moderated = True` and `:842`'s
    override, not by the reason text.
    """
    s = seed_post_context(community_name='lifecycle')
    admin = seed_site_admin(s)
    make_community_member(admin, s.community, is_moderator=True)
    s.community.un_moderated = True
    db.session.commit()

    report_post(
        s.post,
        {'reason': 'spam', 'description': 'x', 'report_remote': False},
        SRC_API,
        auth=bearer(s.voter),
    )

    assert db.session.query(Notification).filter_by(
        title='Suspicious content').count() == 0
    mod_notifs = db.session.query(Notification).filter_by(
        title='A post has been reported').all()
    assert {n.user_id for n in mod_notifs} == {admin.id}


def test_notifying_an_admin_increments_their_unread_counter(db_session):
    """`:900`'s `admin.unread_notifications += 1`.

    The moderator notification at `:883` has NO counterpart increment -- that
    asymmetry is registered, not fixed. Catches a regression dropping `:900`.

    `reason='spam'` matches none of `:828`'s needles, so `notify_admins` is
    forced True here by `s.community.un_moderated = True` and `:842`'s
    override, not by the reason text.
    """
    s = seed_post_context(community_name='lifecycle')
    admin = seed_site_admin(s)
    admin.unread_notifications = 0
    s.community.un_moderated = True
    db.session.commit()

    report_post(
        s.post,
        {'reason': 'spam', 'description': 'x', 'report_remote': False},
        SRC_API,
        auth=bearer(s.voter),
    )

    db.session.refresh(admin)
    assert admin.unread_notifications == 1


def test_a_report_with_no_remote_moderators_federates_nothing(db_session):
    """`:914`'s false arm.

    An empty `remote_instance_ids` must not dispatch a Flag. Catches a
    regression making `:919`'s task_selector unconditional, which would send an
    empty instance list to the federation layer.
    """
    calls = []
    s = seed_post_context(community_name='lifecycle')
    seed_local_moderator(s)

    import app.shared.post as post_module
    original = post_module.task_selector

    def recorder(task_key, **kwargs):
        calls.append(task_key)
        return original(task_key, **kwargs)

    post_module.task_selector = recorder
    try:
        report_post(
            s.post,
            {'reason': 'spam', 'description': 'x', 'report_remote': False},
            SRC_API,
            auth=bearer(s.voter),
        )
    finally:
        post_module.task_selector = original

    assert calls == []


def test_the_federated_summary_joins_reason_and_description(db_session):
    """`:916`'s true arm and `:917`'s concatenation.

    Catches a regression dropping the description, which would federate a Flag
    carrying only the reason.
    """
    calls = []
    s = seed_post_context(community_name='lifecycle')
    seed_remote_moderator(s)

    import app.shared.post as post_module
    original = post_module.task_selector

    def recorder(task_key, **kwargs):
        calls.append(kwargs.get('summary'))
        return original(task_key, **kwargs)

    post_module.task_selector = recorder
    try:
        report_post(
            s.post,
            {'reason': 'spam', 'description': 'unsolicited', 'report_remote': True},
            SRC_API,
            auth=bearer(s.voter),
        )
    finally:
        post_module.task_selector = original

    assert calls == ['spam - unsolicited']


def test_an_empty_description_leaves_the_summary_as_the_reason(db_session):
    """`:916`'s false arm.

    Catches a regression making `:917` unconditional, which would append a bare
    separator to every summary.
    """
    calls = []
    s = seed_post_context(community_name='lifecycle')
    seed_remote_moderator(s)

    import app.shared.post as post_module
    original = post_module.task_selector

    def recorder(task_key, **kwargs):
        calls.append(kwargs.get('summary'))
        return original(task_key, **kwargs)

    post_module.task_selector = recorder
    try:
        report_post(
            s.post,
            {'reason': 'spam', 'description': '', 'report_remote': True},
            SRC_API,
            auth=bearer(s.voter),
        )
    finally:
        post_module.task_selector = original

    assert calls == ['spam']


def test_the_web_arm_returns_none_and_reads_the_form(db_session, app):
    """`:822`'s false arm, `:836-839`'s form reads, and `:924`'s bare return.

    The WEB arm takes a WTForms object, not a dict. Build a minimal stand-in
    exposing `reasons_to_string`, `reasons.data`, `description.data` and
    `report_remote.data`.
    """
    from types import SimpleNamespace

    s = seed_post_context(community_name='lifecycle')
    form = SimpleNamespace(
        reasons=SimpleNamespace(data=['1']),
        description=SimpleNamespace(data='unsolicited'),
        report_remote=SimpleNamespace(data=False),
        reasons_to_string=lambda data: 'spam',
    )

    with web_ctx(app, s.voter):
        result = report_post(s.post, form, SRC_WEB)

    assert result is None
    rows = db.session.query(Report).all()
    assert len(rows) == 1
    assert rows[0].reasons == 'spam'


def test_the_web_arm_escalates_on_reason_five(db_session, app):
    """`:838`'s FIRST disjunct.

    The WEB arm matches reason IDs where the API arm matches text. `'5'` is
    `Minor abuse or sexualization` (app/post/forms.py:36) -- the same policy
    whose API equivalent PC1 fixed (`a06e350f`).
    """
    from types import SimpleNamespace

    s = seed_post_context(community_name='lifecycle')
    seed_site_admin(s)
    form = SimpleNamespace(
        reasons=SimpleNamespace(data=['5']),
        description=SimpleNamespace(data='x'),
        report_remote=SimpleNamespace(data=False),
        reasons_to_string=lambda data: 'Minor abuse or sexualization',
    )

    with web_ctx(app, s.voter):
        report_post(s.post, form, SRC_WEB)

    assert db.session.query(Notification).filter_by(
        title='Suspicious content').count() == 1


def test_the_web_arm_escalates_on_reason_six(db_session, app):
    """`:838`'s SECOND disjunct.

    `'6'` is `Sharing personal info - doxing` (app/post/forms.py:35) -- the WEB
    arm's counterpart to `test_a_doxing_report_notifies_admins_through_the_api`.
    A regression collapsing this disjunct into the first would still pass
    `test_the_web_arm_escalates_on_reason_five` above, so this needs its own
    witness.
    """
    from types import SimpleNamespace

    s = seed_post_context(community_name='lifecycle')
    seed_site_admin(s)
    form = SimpleNamespace(
        reasons=SimpleNamespace(data=['6']),
        description=SimpleNamespace(data='x'),
        report_remote=SimpleNamespace(data=False),
        reasons_to_string=lambda data: 'Sharing personal info - doxing',
    )

    with web_ctx(app, s.voter):
        report_post(s.post, form, SRC_WEB)

    assert db.session.query(Notification).filter_by(
        title='Suspicious content').count() == 1


def test_the_web_arm_does_not_escalate_on_reason_seventeen_on_piefed(db_session, app):
    """`:838`'s THIRD disjunct, software conjunct taken FALSE.

    The WEB arm's counterpart to `test_an_api_ai_flair_report_does_not_escalate_on_piefed`.
    A PieFed instance handles its own flair, so `'17'` alone must not
    escalate. Catches a regression turning `:838`'s `and` into an `or`, or
    dropping the software check outright, either of which would let `'17'`
    escalate unconditionally -- a mutation that the reason-seventeen
    non-piefed test above does not catch, since it never varies the software.

    `seed_site_admin` is required: a zero from an empty `Site.admins()` is not
    a refusal.
    """
    from types import SimpleNamespace

    s = seed_post_context(community_name='lifecycle')
    seed_site_admin(s)
    s.instance.software = 'piefed'
    db.session.commit()
    form = SimpleNamespace(
        reasons=SimpleNamespace(data=['17']),
        description=SimpleNamespace(data='x'),
        report_remote=SimpleNamespace(data=False),
        reasons_to_string=lambda data: 'AI content that needs flair',
    )

    with web_ctx(app, s.voter):
        report_post(s.post, form, SRC_WEB)

    assert db.session.query(Notification).filter_by(
        title='Suspicious content').count() == 0


def test_the_web_arm_escalates_on_reason_seventeen_on_a_non_piefed_instance(db_session, app):
    """`:838`'s THIRD disjunct, with both conjuncts true.

    `'17'` is `AI content that needs flair` (app/post/forms.py:30). The WEB
    arm's counterpart to `test_an_api_ai_flair_report_notifies_admins_on_a_non_piefed_instance`.
    `post.community.instance` is `s.instance` because the community under
    `seed_post_context` is local, so setting `s.instance.software` reaches
    `:838`'s software conjunct directly.
    """
    from types import SimpleNamespace

    s = seed_post_context(community_name='lifecycle')
    seed_site_admin(s)
    s.instance.software = 'lemmy'
    db.session.commit()
    form = SimpleNamespace(
        reasons=SimpleNamespace(data=['17']),
        description=SimpleNamespace(data='x'),
        report_remote=SimpleNamespace(data=False),
        reasons_to_string=lambda data: 'AI content that needs flair',
    )

    with web_ctx(app, s.voter):
        report_post(s.post, form, SRC_WEB)

    assert db.session.query(Notification).filter_by(
        title='Suspicious content').count() == 1


def test_a_report_on_a_remote_authors_post_flags_their_instance(db_session):
    """`:907`'s true arm and `:909`'s add.

    Every other test in this file reports a post by `s.author`, who is local,
    so `:907` is false and this arc never runs. The suspect has to be a REMOTE
    user before the block below `:903` reaches `:909` at all.

    Catches a regression dropping `:909`, after which a report about a remote
    user's post would never reach the instance hosting them.
    """
    calls = []
    s = seed_post_context(community_name='lifecycle')
    suspect_instance = make_instance('suspect.example', software='lemmy')
    suspect = make_user(suspect_instance, 'remoteauthor')
    remote_post = make_post(s.community, suspect, 'https://suspect.example/p/9')

    import app.shared.post as post_module
    original = post_module.task_selector

    def recorder(task_key, **kwargs):
        calls.append(set(kwargs.get('instance_ids') or []))
        return original(task_key, **kwargs)

    post_module.task_selector = recorder
    try:
        report_post(
            remote_post,
            {'reason': 'spam', 'description': 'x', 'report_remote': True},
            SRC_API,
            auth=bearer(s.voter),
        )
    finally:
        post_module.task_selector = original

    assert calls == [{suspect_instance.id}]


def _report_both_ways(s, app, api_post, web_post):
    """One report through each arm; returns their `targets`, API first."""
    from types import SimpleNamespace

    report_post(api_post, {'reason': 'spam', 'description': 'd', 'report_remote': False},
                SRC_API, auth=bearer(s.voter))
    form = SimpleNamespace(reasons=SimpleNamespace(data=['1']), description=SimpleNamespace(data='d'),
                           report_remote=SimpleNamespace(data=False),
                           reasons_to_string=lambda data: 'spam')
    with web_ctx(app, s.voter):
        report_post(web_post, form, SRC_WEB)
    rows = {r.suspect_post_id: r for r in db.session.query(Report).all()}
    return rows[api_post.id].targets, rows[web_post.id].targets


def test_both_arms_name_the_post_author_s_instance(db_session, app):
    """D440, fixed (owner ruling 2026-09-30), mirroring D553 in report_reply.
    The API arm read the POST's instance (`post.instance_id`, via `.one()`) and
    the web arm the SUSPECT USER's, so the Flag carried a different
    `source_instance` depending on the arm whenever the two differed -- as
    `Post.move_to` makes them. Both now read the post author's.
    """
    s = seed_post_context(community_name='lifecycle')
    author_instance = make_instance('suspect.example', software='lemmy')
    moved_to = make_instance('moved.example', software='lemmy')
    suspect = make_user(author_instance, 'remoteauthor')
    api_post = make_post(s.community, suspect, 'https://suspect.example/p/1')
    web_post = make_post(s.community, suspect, 'https://suspect.example/p/2')
    api_post.instance_id = moved_to.id
    web_post.instance_id = moved_to.id
    db.session.commit()

    for targets in _report_both_ways(s, app, api_post, web_post):
        assert targets['source_instance_id'] == author_instance.id
        assert targets['source_instance_domain'] == 'suspect.example'


def test_an_author_with_no_instance_is_reported_without_one(db_session, app):
    """D440, fixed: None-safe. The API arm's `.one()` raised `NoResultFound`
    and the web arm's `.get()` deferred to an `AttributeError`; an author with
    no instance now yields a report whose source instance is None on both.
    """
    s = seed_post_context(community_name='lifecycle')
    api_post = make_post(s.community, s.author, 'https://local.example/p/2')
    web_post = make_post(s.community, s.author, 'https://local.example/p/3')
    s.author.instance_id = None
    db.session.commit()

    for targets in _report_both_ways(s, app, api_post, web_post):
        assert targets['source_instance_id'] is None
        assert targets['source_instance_domain'] is None


def test_a_remote_suspects_instance_is_not_added_twice(db_session):
    """`:908`'s FALSE arm, reached when a moderator already put the suspect's
    instance in the set.

    THIS TEST PINS AN ARC, NOT A BEHAVIOURAL DIFFERENCE, and says so rather
    than pretending otherwise. `:909` adds to a set, so adding an id already
    present is a no-op: both arms of `:908` leave exactly the same state, and
    no assertion can distinguish them. What the test does establish is that the
    guarded path runs and produces no duplicate and no error.

    Compare `test_deleting_with_no_notifications_takes_the_loops_zero_exit`,
    which is weak for the same reason and is labelled the same way.
    """
    calls = []
    s = seed_post_context(community_name='lifecycle')
    suspect_instance = make_instance('suspect.example', software='lemmy')
    suspect = make_user(suspect_instance, 'remoteauthor')
    remote_post = make_post(s.community, suspect, 'https://suspect.example/p/9')
    comod = make_user(suspect_instance, 'comod')
    make_community_member(comod, s.community, is_moderator=True)

    import app.shared.post as post_module
    original = post_module.task_selector

    def recorder(task_key, **kwargs):
        calls.append(set(kwargs.get('instance_ids') or []))
        return original(task_key, **kwargs)

    post_module.task_selector = recorder
    try:
        report_post(
            remote_post,
            {'reason': 'spam', 'description': 'x', 'report_remote': True},
            SRC_API,
            auth=bearer(s.voter),
        )
    finally:
        post_module.task_selector = original

    assert calls == [{suspect_instance.id}]


def test_a_local_moderators_user_id_colliding_with_a_remote_suspects_instance_id_still_adds_it(db_session):
    """`:908` compares `suspect_user.instance_id` against `remote_instance_ids`
    -- NOT `already_notified` -- and the distinction matters even though the
    arc labelled at `:908` above (an id already present in the SAME set) is a
    documented no-op. A mutant swapping the comparison to `already_notified`
    (a set of USER ids, populated at `:884` for LOCAL moderators only) is a
    DIFFERENT fault: it can falsely skip `:909`'s add when a local
    moderator's user id happens to equal the remote suspect's instance id --
    two values from unrelated id spaces.

    THE COLLISION IS BUILT BY ORDERING, the same technique as
    `test_a_remote_communitys_instance_is_flagged_even_when_ids_collide` (PC2),
    and not by reassigning a primary key. `seed_post_context` takes instance
    id 1, author user id 1, voter user id 2. A local moderator seeded next
    takes user id 3. A padding instance (id 2) and the suspect's own instance
    (id 3) are seeded after that, landing `suspect_instance.id` on 3 as well
    -- `already_notified == {3}` (the moderator's USER id) collides with
    `suspect_user.instance_id == 3` (an INSTANCE id). The assertion below
    pins the collision explicitly so a change to seeding order fails loudly
    here instead of silently ceasing to construct its own precondition.

    Against the tree as it stands, the local moderator contributes nothing to
    `remote_instance_ids` (only non-local moderators do, via `:886-890`), so
    it is empty when `:908` is reached; `suspect_user.instance_id (3) not in
    {}` is true, `:909` adds it, and one Flag is dispatched carrying
    `{suspect_instance.id}`. A mutant checking `already_notified` instead
    would see `3 in {3}`, skip the add, leave `remote_instance_ids` empty,
    and `:914` would then suppress the call ENTIRELY -- not the no-op `:908`
    arc produces, a missing federation call.
    """
    calls = []
    s = seed_post_context(community_name='lifecycle')
    localmod = seed_local_moderator(s)
    make_instance('padding.example')  # consumes instance id 2
    suspect_instance = make_instance('suspect.example', software='lemmy')  # id 3
    assert localmod.id == suspect_instance.id == 3
    suspect = make_user(suspect_instance, 'remoteauthor')
    remote_post = make_post(s.community, suspect, 'https://suspect.example/p/9')

    import app.shared.post as post_module
    original = post_module.task_selector

    def recorder(task_key, **kwargs):
        calls.append((task_key, set(kwargs.get('instance_ids') or [])))
        return original(task_key, **kwargs)

    post_module.task_selector = recorder
    try:
        report_post(
            remote_post,
            {'reason': 'spam', 'description': 'x', 'report_remote': True},
            SRC_API,
            auth=bearer(s.voter),
        )
    finally:
        post_module.task_selector = original

    assert calls == [('report_post', {suspect_instance.id})]


def test_a_minor_abuse_report_notifies_admins_through_the_api(db_session):
    """PC1: `:828`'s `'Minor abuse'` needle can never match a `.lower()`ed
    haystack.

    The API arm mirrors the WEB arm's policy: `'Minor abuse'` corresponds to
    reason `'5'`, `Minor abuse or sexualization` (app/post/forms.py:36). But
    `:828` lowercases the haystack and keeps the needle's capital M, so the
    disjunct is ALWAYS False and the escalation has never fired on this arm.

    Failed against the unmodified tree (fixed at `a06e350f`): no admin
    notification was written.

    `seed_site_admin` is load-bearing here. Without it `Site.admins()` is empty
    and this test fails BOTH before and after the fix -- a failing observation
    that proves nothing, and the worst possible foundation for a production
    change.
    """
    s = seed_post_context(community_name='lifecycle')
    seed_site_admin(s)

    report_post(
        s.post,
        {'reason': 'Minor abuse or sexualization', 'description': 'x',
         'report_remote': False},
        SRC_API,
        auth=bearer(s.voter),
    )

    assert db.session.query(Notification).filter_by(
        title='Suspicious content').count() == 1


def test_minor_abuse_in_the_description_also_notifies_admins(db_session):
    """`:829`, the description-side needle, distinct from `:828`'s reason-side.

    `:828` and `:829` are separate disjuncts of one compound; a test exercising
    only the reason leaves `:829` unwitnessed.
    """
    s = seed_post_context(community_name='lifecycle')
    seed_site_admin(s)

    report_post(
        s.post,
        {'reason': 'spam', 'description': 'this is minor abuse',
         'report_remote': False},
        SRC_API,
        auth=bearer(s.voter),
    )

    assert db.session.query(Notification).filter_by(
        title='Suspicious content').count() == 1


def test_doxing_in_the_description_also_notifies_admins(db_session):
    """`:829`'s `'doxing'` needle, witnessed in the DESCRIPTION.

    `:828` (reason) and `:829` (description) each carry the same two-element
    needle list. `'doxing'` is already witnessed in the reason
    (`test_a_doxing_report_notifies_admins_through_the_api`) and `'minor
    abuse'` is witnessed in both positions, but nothing puts `'doxing'` in a
    description. An ordinary reason keeps this test from being satisfied by
    `:828` instead.
    """
    s = seed_post_context(community_name='lifecycle')
    seed_site_admin(s)

    report_post(
        s.post,
        {'reason': 'spam', 'description': 'this is doxing',
         'report_remote': False},
        SRC_API,
        auth=bearer(s.voter),
    )

    assert db.session.query(Notification).filter_by(
        title='Suspicious content').count() == 1


def test_a_remote_communitys_instance_is_flagged_even_when_ids_collide(db_session):
    """PC2: `:905` tests `post.community_id` against a set of INSTANCE ids.

    `:906` adds `post.community.instance_id`, and `:908` gets the identical
    guard right two lines below -- that in-file counterpart is what
    establishes intent. The consequence is a false skip: when a community id
    happens to equal an instance id already in the set, the community's
    instance is never added and its moderators never receive the Flag.

    THIS FIXTURE CONSTRUCTS THE COLLISION DELIBERATELY. Community ids and
    instance ids are drawn from separate sequences, so the coincidence below
    is not realistic -- it is the minimal arrangement that makes the wrong
    comparison observable. The defect is a latent wrong-variable bug, not a
    common failure.

    THE COLLISION IS BUILT BY ORDERING, NOT BY REASSIGNING A PRIMARY KEY.
    Reassigning `community.id` after the row exists is a primary key with
    dependent foreign keys and is not a safe rewrite, so this inlines
    `seed_post_context`'s own steps (tests/factories.py:1187-1213) with one
    extra `make_community('padding')` call spliced in between the local
    instance and the community under test. `tests/conftest.py:131` resets
    every table's sequence to 1 after each test, and `make_community`
    hardcodes `instance_id=1` (tests/factories.py:141), so:

      - the local instance is the first Instance row -> id 1
      - the padding community consumes community id 1 (its hardcoded FK to
        instance id 1 already exists by this point)
      - the community under test becomes the SECOND community -> id 2
      - `seed_remote_moderator` then creates the SECOND Instance row overall
        (the local instance was the first) -> id 2

    That makes `post.community_id` (2) collide with the remote moderator's
    instance id (2), already in `remote_instance_ids` from the loop above
    `:905` because `report_remote=True` is passed below.

    `:904` additionally requires the community to be NON-LOCAL, and setting
    only `ap_id` is not enough to get there: `Community.is_local()`
    (app/models.py:796) is `self.ap_id is None or
    self.profile_id().startswith(SERVER_URL)`, and `make_community`'s
    default `ap_profile_id` (`https://test.piefed.local/c/<name>`) already
    starts with this test environment's `SERVER_URL` (`.env.test`'s
    `SERVER_NAME=test.piefed.local`) -- the second disjunct alone would keep
    the community local regardless of `ap_id`. Both `ap_id` and
    `ap_profile_id` are overridden below to a remote domain, and
    `is_local()` is asserted False before calling `report_post` so a
    fixture that silently fails to reach `:904` announces itself instead of
    passing for the wrong reason.

    Failed against the unmodified tree (fixed at `478ff47a`): the community's
    instance (id 1) was missing from the federated instance list, which came
    back as `{2}` instead of `{1, 2}`.
    """
    calls = []

    # Inlined seed_post_context (tests/factories.py:1187-1213) with one
    # padding community spliced in to force the id collision described above.
    instance = make_instance('local.example', software='piefed')
    make_site()
    author = make_user(instance, 'author', local=True)
    voter = make_user(instance, 'voter', local=True)
    make_community('padding')  # consumes community id 1
    community = make_community('lifecycle')  # lands on community id 2
    community.private = True
    db.session.commit()
    post = make_post(community, author, 'https://local.example/p/1')

    from types import SimpleNamespace
    remote_instance, _remotemod = seed_remote_moderator(
        SimpleNamespace(community=community))

    assert community.id == remote_instance.id == 2

    community.ap_id = f'lifecycle@{remote_instance.domain}'
    community.ap_profile_id = f'https://{remote_instance.domain}/c/lifecycle'
    db.session.commit()
    assert community.is_local() is False

    import app.shared.post as post_module
    original = post_module.task_selector

    def recorder(task_key, **kwargs):
        calls.append(set(kwargs.get('instance_ids') or []))
        return original(task_key, **kwargs)

    post_module.task_selector = recorder
    try:
        report_post(
            post,
            {'reason': 'spam', 'description': 'x', 'report_remote': True},
            SRC_API,
            auth=bearer(voter),
        )
    finally:
        post_module.task_selector = original

    assert calls == [{instance.id, remote_instance.id}]


def test_the_communitys_instance_is_not_re_added_when_already_present(db_session):
    """`:905`'s FALSE arm, reached when a moderator already put the
    community's own instance in the set before `:905` runs.

    THIS TEST PINS AN ARC, NOT A BEHAVIOURAL DIFFERENCE, and says so rather
    than pretending otherwise. `:906` adds to a set, so adding an id already
    present is a no-op: both arms of `:905` leave `remote_instance_ids` in
    exactly the same state, and no assertion can distinguish them. What the
    test does establish is that the guarded path runs and produces no
    duplicate and no error.

    Compare `test_a_remote_suspects_instance_is_not_added_twice`, which pins
    `:908`'s FALSE arm for the identical reason and is labelled the same way.
    An arc being equivalent does not make every mutation of `:905`
    equivalent -- a mutant that swapped which SET `:905` tests against would
    still be a real defect (that is exactly what
    `test_a_remote_communitys_instance_is_flagged_even_when_ids_collide`
    catches); this test only closes the coverage arc for `:905`'s FALSE arm,
    it does not claim to catch a regression there.

    THE SETUP. `:904` needs the community NON-LOCAL. Setting only `ap_id` is
    not enough: `Community.is_local()` (app/models.py:796) is `self.ap_id is
    None or self.profile_id().startswith(SERVER_URL)`, and `make_community`'s
    default `ap_profile_id` already starts with this test environment's
    `SERVER_URL`, so the second disjunct alone would keep the community local
    regardless of `ap_id`. Both `ap_id` and `ap_profile_id` are overridden
    below to a remote-looking value, and `is_local()` is asserted False
    before calling `report_post` so a fixture that silently fails to reach
    `:904` announces itself instead of passing for the wrong reason.

    To make `:905` see the community's own instance already in the set, the
    moderator loop above it (`:886`-`:890`) has to add
    `post.community.instance_id` itself. A moderator's `instance_id` is
    independent of `Community.is_local()` (which reads only `ap_id`/
    `ap_profile_id`), so a moderator can sit on `s.instance` -- the same row
    `post.community.instance_id` points at -- while the community's AP
    identity is overridden to look remote. `make_user(s.instance, name,
    local=False)` (tests/factories.py:41) sets `ap_id` while leaving
    `instance_id` at the instance passed in, and `User.is_local()`
    (app/models.py:1252) reads only `ap_id`/`ap_profile_id`, never
    `instance_id` -- so this moderator is non-local and lands on the
    community's own instance. Under `report_remote=True`, `:890` adds
    `moderator.instance_id` (== `post.community.instance_id`) to
    `remote_instance_ids` unconditionally, before `:905` ever runs.

    The suspect (`s.author`) stays LOCAL (`seed_post_context`'s default), so
    `:907` is false and `:908`-`:909` do not also touch
    `remote_instance_ids` -- keeping the observation scoped to `:905`.
    """
    calls = []
    s = seed_post_context(community_name='lifecycle')

    s.community.ap_id = 'lifecycle@remote.example'
    s.community.ap_profile_id = 'https://remote.example/c/lifecycle'
    db.session.commit()
    assert s.community.is_local() is False

    modmate = make_user(s.instance, 'modmate', local=False)
    make_community_member(modmate, s.community, is_moderator=True)

    import app.shared.post as post_module
    original = post_module.task_selector

    def recorder(task_key, **kwargs):
        calls.append(set(kwargs.get('instance_ids') or []))
        return original(task_key, **kwargs)

    post_module.task_selector = recorder
    try:
        report_post(
            s.post,
            {'reason': 'spam', 'description': 'x', 'report_remote': True},
            SRC_API,
            auth=bearer(s.voter),
        )
    finally:
        post_module.task_selector = original

    assert calls == [{s.instance.id}]
