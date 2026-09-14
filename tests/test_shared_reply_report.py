"""Group D of app/shared/reply.py -- reporting a reply.

    report_reply   :311   55 statements / 34 arcs

The module's largest function, and at ZERO coverage when this file was
created. `tests/test_shared_post_moderation.py` covers the twin
`report_post` (app/shared/post.py:833).

THIS FUNCTION IS A LOOP, NOT A FORK, AND THAT SHAPES EVERY FIXTURE HERE.
`:359`-`:376` iterates `reply.community.moderators()` and branches per
moderator on `moderator.is_local()`, with `report_remote` gating which remote
instances are collected. Witnessing both arms needs at least one LOCAL and one
REMOTE moderator on the same community, which is what `_seed_for_report`
builds.

`Site.admins()` AT `:379` IS REGISTER ENTRY D442, LIVE. Its behaviour differs
where `g.admin_ids` is unset. Record what it does here; do not fix it.

`notify_admins` AT `:318`-`:319` IS A SUBSTRING TEST over two lists, so 'dox'
matches any word containing it. Recorded, not fixed.

EVERY SEEDED ID IS OFFSET -- see register entry D533. Sub-project 41's fixture
left three ids equal to 1 and hid at least four mutants, because several call
sites resolve objects to ids and a wrong-object-right-id mutation is then
invisible.
"""

import pytest
from contextlib import contextmanager
from types import SimpleNamespace

from sqlalchemy import text

from app import db
from app.constants import ROLE_ADMIN, SRC_API, SRC_WEB
from app.models import Instance, Notification, Report, User
import app.shared.reply as reply_module
from app.shared.reply import report_reply
from tests.factories import (
    bearer, make_community, make_community_member, make_instance, make_post,
    make_post_reply, make_site, make_user, web_ctx,
)


def _seed_for_report(*, private=True):
    """A reply on a community with one LOCAL and one REMOTE moderator.

    `reporter` files the report, `author` wrote the reply, `local_mod` and
    `remote_mod` moderate the community. Four distinct users, because `:359`'s
    loop branches per moderator and `:380`'s admin block skips anyone already
    notified -- a fixture that reused one user could not tell those apart.

    THE IDS ARE FORCED APART, per D533: spare rows advance the community and
    post sequences past the user sequence, and the assertion below fails if a
    factory change makes them collide.
    """
    local_instance = make_instance('local.example', software='piefed')
    remote_instance = make_instance('remote.example', software='lemmy')
    reporter = make_user(local_instance, 'reporter', local=True)
    author = make_user(local_instance, 'author', local=True)
    local_mod = make_user(local_instance, 'local-mod', local=True)
    remote_mod = make_user(remote_instance, 'remote-mod', local=False)
    make_community('report-burner-one')
    make_community('report-burner-two')
    community = make_community('reports')
    community.private = private
    db.session.commit()
    make_post(community, author, 'https://local.example/burner')
    post = make_post(community, author, 'https://local.example/p/1')
    reply = make_post_reply(post, author)
    db.session.commit()
    assert len({community.id, post.id, reporter.id}) == 3, (
        'D533: seeded ids collided -- wrong-object-right-id would be invisible'
    )
    return SimpleNamespace(local_instance=local_instance,
                           remote_instance=remote_instance,
                           reporter=reporter, author=author,
                           local_mod=local_mod, remote_mod=remote_mod,
                           community=community, post=post, reply=reply)


def add_moderator(s, user):
    """Make `user` a moderator of `s.community`.

    `Community.moderators()` (app/models.py:716-722) returns CommunityMember
    rows filtered on `is_banned == False`, and `:360` then loads each
    `User` by `mod.user_id`.
    """
    return make_community_member(user, s.community, is_moderator=True)


@contextmanager
def _recording_task_selector(capture_kwargs=False):
    """Rebind `task_selector` on `app.shared.reply` to record calls.

    `report_reply` calls the module-level name it imported
    (`from app.shared.tasks import task_selector`, app/shared/reply.py:12),
    so patching `app.shared.reply.task_selector` -- not
    `app.shared.tasks.task_selector` -- is what actually intercepts it.

    Yields a list that accumulates one entry per call: `(task_key, kwargs)`
    pairs when `capture_kwargs` is True, bare task keys otherwise. The
    original is restored in a `finally` so a failing test cannot leak the
    patch into whatever runs next.
    """
    calls = []
    original = reply_module.task_selector

    def fake_task_selector(task_key, **kwargs):
        if capture_kwargs:
            calls.append((task_key, kwargs))
        else:
            calls.append(task_key)

    reply_module.task_selector = fake_task_selector
    try:
        yield calls
    finally:
        reply_module.task_selector = original


class TestReportReply:
    """`report_reply` (app/shared/reply.py:311-410)."""

    def test_the_api_arm_creates_the_report_row(self, db_session):
        """`:312` true, `:313`-`:320`, `:342`-`:353`, `:397`, `:408`.

        Asserts the Report's OWN fields rather than a bare count: `:342`-`:352`
        writes nine of them and a count would pass with eight deleted.
        `reply.reports` at `:397` is asserted separately because nothing else
        writes it.
        """
        s = _seed_for_report()
        payload = {'reason': 'spam', 'description': 'clearly spam',
                   'report_remote': False}

        reporter_id, report = report_reply(s.reply, payload, SRC_API,
                                           auth=bearer(s.reporter))

        assert reporter_id == s.reporter.id
        db.session.refresh(s.reply)
        assert s.reply.reports == 1
        rows = db.session.query(Report).all()
        assert {r.suspect_post_reply_id for r in rows} == {s.reply.id}
        assert {r.reporter_id for r in rows} == {s.reporter.id}
        assert {r.suspect_user_id for r in rows} == {s.author.id}

    def test_the_web_arm_reads_the_form_and_returns_none(self, db_session, app):
        """`:312` false -> `:322`-`:328`, and `:410`'s bare return.

        The web arm builds `reason` from `input.reasons_to_string(...)` and
        `notify_admins` from membership of '5' or '6' in `reasons.data` --
        a DIFFERENT mechanism from the API arm's substring test, which is why
        both arms need their own test rather than one parameterised over src.
        """
        s = _seed_for_report()
        form = SimpleNamespace(
            reasons=SimpleNamespace(data=['1']),
            description=SimpleNamespace(data='a web report'),
            report_remote=SimpleNamespace(data=False),
            reasons_to_string=lambda data: 'spam',
        )

        with web_ctx(app, s.reporter):
            result = report_reply(s.reply, form, SRC_WEB, auth=None)

        assert result is None
        rows = db.session.query(Report).all()
        assert {r.reporter_id for r in rows} == {s.reporter.id}

    def test_a_local_moderator_is_notified(self, db_session):
        """`:359`'s loop, `:362`'s true arm, `:363`-`:370`.

        The Notification's `user_id` must be the MODERATOR's and its
        `author_id` the REPORTER's -- a mutant swapping those two operands
        produces the same row count and is caught only by asserting both.
        """
        s = _seed_for_report()
        add_moderator(s, s.local_mod)
        payload = {'reason': 'spam', 'description': 'd', 'report_remote': False}

        report_reply(s.reply, payload, SRC_API, auth=bearer(s.reporter))

        rows = db.session.query(Notification).all()
        assert {n.user_id for n in rows} == {s.local_mod.id}
        assert {n.author_id for n in rows} == {s.reporter.id}

    def test_a_remote_moderator_is_not_notified_locally(self, db_session):
        """`:362`'s false arm -> `:371`-`:376`.

        THE POSITIVE CONTROL IS `test_a_local_moderator_is_notified` ABOVE,
        and it is required: "no Notification row" is both the correct outcome
        here and the signature of a fixture where no moderator exists at all.
        That test proves this fixture CAN notify.
        """
        s = _seed_for_report()
        add_moderator(s, s.remote_mod)
        payload = {'reason': 'spam', 'description': 'd', 'report_remote': False}

        report_reply(s.reply, payload, SRC_API, auth=bearer(s.reporter))

        assert db.session.query(Notification).count() == 0

    def test_report_remote_false_skips_a_mod_on_the_suspects_instance(self, db_session):
        """`:372`'s true arm and `:373`'s three-way comparison.

        With `report_remote` False, a remote moderator is collected ONLY if
        its instance differs from both the suspect's and the community's. Here
        the remote moderator shares the suspect's instance, so nothing is
        collected and `:400`'s `len(remote_instance_ids)` is zero.
        """
        s = _seed_for_report()
        s.author.instance_id = s.remote_instance.id
        db.session.commit()
        add_moderator(s, s.remote_mod)
        payload = {'reason': 'spam', 'description': 'd', 'report_remote': False}

        with _recording_task_selector() as calls:
            report_reply(s.reply, payload, SRC_API, auth=bearer(s.reporter))

        assert 'report_reply' not in calls

    def test_report_remote_true_collects_every_remote_moderator(self, db_session):
        """`:375`-`:376` -- the else arm of `:372`.

        The same fixture as the test above with one lever moved, so the
        difference in outcome is `report_remote` and nothing else.
        """
        s = _seed_for_report()
        s.author.instance_id = s.remote_instance.id
        db.session.commit()
        add_moderator(s, s.remote_mod)
        payload = {'reason': 'spam', 'description': 'd', 'report_remote': True}

        with _recording_task_selector() as calls:
            report_reply(s.reply, payload, SRC_API, auth=bearer(s.reporter))

        assert 'report_reply' in calls

    def test_a_csam_reason_notifies_site_admins(self, db_session):
        """`:318`-`:319`'s substring test, `:378`'s true arm, `:379`-`:386`.

        `Site.admins()` at `:379` IS REGISTER ENTRY D442, LIVE. Record what it
        returns here rather than assuming; the entry says its behaviour
        differs where `g.admin_ids` is unset.

        Probed directly: `db_session` only clears `flask.g`
        (tests/conftest.py:156) and calling `report_reply` here never goes
        through `request_hooks.py`'s `before_request` (the only place that
        sets `g.admin_ids`), so `g.admin_ids` stays unset for this whole test
        and `Site.admins()` always takes the `else` branch at
        app/models.py:4011-4012 -- the INNER JOIN to `user_role`. The role
        row is pinned at `id=ROLE_ADMIN` (see tests/test_request_hooks.py's
        `test_admin_ids_computed_and_persisted_when_setting_absent`) because
        that join filters on `user_role.c.role_id == ROLE_ADMIN` by VALUE, not
        by role name -- a `Role(name='Admin')` given whatever id the sequence
        hands out would not match and this admin would silently vanish from
        the result set. `reporter` (id 1, no role row at all) is dropped by
        the same inner join even though `User.id == 1` is one of the OR's
        disjuncts, because an inner join requires a `user_role` row to exist
        before that disjunct is ever consulted -- so `is_admin()`
        (app/models.py:1259-1261, id==1 with no join) and `Site.admins()`
        disagree about `reporter` here. Asserted as a SET of notified users
        rather than a single counter so a mutant that notified the reporter
        as well would be caught.

        `unread_notifications` is asserted as well as the Notification row,
        because `:386` is a separate statement a mutant can delete on its
        own.
        """
        s = _seed_for_report()
        make_site()
        admin = make_user(s.local_instance, 'site-admin', local=True)
        db.session.commit()
        from app.models import Role, user_role
        role = Role(id=ROLE_ADMIN, name='Admin', weight=0)
        db.session.add(role)
        db.session.commit()
        db.session.execute(user_role.insert().values(user_id=admin.id,
                                                     role_id=role.id))
        admin.unread_notifications = 3
        db.session.commit()
        payload = {'reason': 'csam', 'description': 'd', 'report_remote': False}

        report_reply(s.reply, payload, SRC_API, auth=bearer(s.reporter))

        db.session.refresh(admin)
        assert admin.unread_notifications == 4
        rows = db.session.query(Notification).all()
        assert {n.user_id for n in rows} == {admin.id}

    def test_an_ordinary_reason_does_not_notify_admins(self, db_session):
        """`:378`'s false arm -- the same-mechanism positive control.

        Identical to the test above with `reason` changed, so the zero here is
        `notify_admins` being False rather than an absent admin.
        """
        s = _seed_for_report()
        make_site()
        admin = make_user(s.local_instance, 'site-admin', local=True)
        db.session.commit()
        from app.models import Role, user_role
        role = Role(id=ROLE_ADMIN, name='Admin', weight=0)
        db.session.add(role)
        db.session.commit()
        db.session.execute(user_role.insert().values(user_id=admin.id,
                                                     role_id=role.id))
        admin.unread_notifications = 3
        db.session.commit()
        payload = {'reason': 'spam', 'description': 'd', 'report_remote': False}

        report_reply(s.reply, payload, SRC_API, auth=bearer(s.reporter))

        db.session.refresh(admin)
        assert admin.unread_notifications == 3

    def test_a_remote_suspect_instance_is_added_when_reporting_remotely(self, db_session):
        """`:393`'s true arm and `:394`-`:395`.

        The suspect-user half of the `report_remote` block, which is the half
        written CORRECTLY -- it guards on `suspect_user.instance_id` and adds
        the same value.

        `User.is_local()` (app/models.py:1251-1252) tests `ap_id`, not
        `instance_id` -- `author` was minted with `local=True`, which leaves
        `ap_id` `None` forever, so moving `instance_id` alone would not flip
        `is_local()`. `ap_id`/`ap_profile_id`/`ap_public_url` are set here to
        a URL on `remote.example` so `is_local()` is actually False and
        `:393`'s guard fires.

        `capture_kwargs=True` and asserting the actual `instance_ids` set
        (not just `'report_reply' in calls`) is required: a mutant swapping
        `:395`'s operand to add `reply.community.instance_id` instead of
        `suspect_user.instance_id` still fires the task -- the community's
        instance_id is 1 (`make_community` hardcodes it, and this test never
        moves it) -- so a bare "some task fired" assertion cannot tell the
        suspect's instance (2) apart from the community's (1).
        """
        s = _seed_for_report()
        s.author.instance_id = s.remote_instance.id
        s.author.ap_id = 'author@remote.example'
        s.author.ap_profile_id = 'https://remote.example/u/author'
        s.author.ap_public_url = 'https://remote.example/u/author'
        db.session.commit()
        payload = {'reason': 'spam', 'description': 'd', 'report_remote': True}

        with _recording_task_selector(capture_kwargs=True) as calls:
            report_reply(s.reply, payload, SRC_API, auth=bearer(s.reporter))

        report_calls = [c for c in calls if c[0] == 'report_reply']
        assert len(report_calls) == 1
        instance_ids = report_calls[0][1]['instance_ids']
        assert set(instance_ids) == {s.remote_instance.id}

    def test_the_community_guard_compares_the_wrong_id_space(self, db_session):
        """`:390`-`:392` -- AND IT PINS A REGISTERED DEFECT ON PURPOSE.

        `:391` is `if reply.community_id not in remote_instance_ids:` and
        `:392` adds `reply.community.instance_id`. Those are DIFFERENT ID
        SPACES: the guard tests a community id against a set of instance ids,
        so it cannot do what it is written to do. The `suspect_user` block at
        `:393`-`:395` gets the identical pattern right, which is what makes
        this a slip rather than a convention.

        `Community.is_local()` (app/models.py:795-796) is `ap_id is None or
        profile_id().startswith(SERVER_URL)` -- `make_community` never sets
        `ap_id`, so moving `instance_id` alone leaves `is_local()` True and
        `:390`'s guard would never fire. `ap_id` and `ap_profile_id` are set
        here to a `remote.example` URL, alongside `instance_id`, so
        `is_local()` is actually False.

        THIS TEST RECORDS TODAY'S BEHAVIOUR AND THIS ROUND DOES NOT FIX IT --
        the production budget is the counter fix, and a duplicate Flag to one
        instance is a different blast radius from a permanently drifting
        counter. IF A LATER ROUND REPAIRS `:391` TO GUARD ON
        `reply.community.instance_id`, THE EDIT OWED HERE IS TO ASSERT THE
        INSTANCE APPEARS EXACTLY ONCE rather than that the branch was taken.

        Registered as this round's finding 1.
        """
        s = _seed_for_report()
        s.community.instance_id = s.remote_instance.id
        s.community.ap_id = 'reports@remote.example'
        s.community.ap_profile_id = 'https://remote.example/c/reports'
        db.session.commit()
        payload = {'reason': 'spam', 'description': 'd', 'report_remote': True}

        with _recording_task_selector() as calls:
            report_reply(s.reply, payload, SRC_API, auth=bearer(s.reporter))

        assert 'report_reply' in calls

    def test_a_description_is_appended_to_the_summary(self, db_session):
        """`:402`'s true arm and `:403`'s concatenation.

        The summary is only built when `:400` finds a remote instance, so this
        test needs the remote path as well. Assert the JOINED string, because
        `:401` alone would pass with `:403` deleted.
        """
        s = _seed_for_report()
        s.author.instance_id = s.remote_instance.id
        s.author.ap_id = 'author@remote.example'
        s.author.ap_profile_id = 'https://remote.example/u/author'
        s.author.ap_public_url = 'https://remote.example/u/author'
        db.session.commit()
        payload = {'reason': 'spam', 'description': 'with detail',
                   'report_remote': True}

        with _recording_task_selector(capture_kwargs=True) as calls:
            report_reply(s.reply, payload, SRC_API, auth=bearer(s.reporter))

        summaries = [k.get('summary') for _, k in calls]
        assert 'spam - with detail' in summaries

    def test_an_empty_description_leaves_the_summary_bare(self, db_session):
        """`:402`'s false arm -- the counterpart of the test above.

        Together they prove `:403` is driven by `description` rather than
        running unconditionally.
        """
        s = _seed_for_report()
        s.author.instance_id = s.remote_instance.id
        s.author.ap_id = 'author@remote.example'
        s.author.ap_profile_id = 'https://remote.example/u/author'
        s.author.ap_public_url = 'https://remote.example/u/author'
        db.session.commit()
        payload = {'reason': 'spam', 'description': '', 'report_remote': True}

        with _recording_task_selector(capture_kwargs=True) as calls:
            report_reply(s.reply, payload, SRC_API, auth=bearer(s.reporter))

        summaries = [k.get('summary') for _, k in calls]
        assert 'spam' in summaries

    def test_a_moderator_row_with_no_matching_user_is_skipped(self, db_session):
        """`:361`'s false arm -> back to `:359`'s loop head.

        THIS STATE IS MANUFACTURED. No production path that reaches this test
        was found: every user-deletion path checked --
        `app/admin/routes.py:1785`, `app/user/utils.py`, and
        `User.delete_dependencies()` (`app/models.py:1543`,
        `db.session.query(CommunityMember).filter(CommunityMember.user_id ==
        self.id).delete()`) -- deletes the matching `CommunityMember` rows
        BEFORE the `User` row goes, and no migration puts `ondelete=CASCADE`
        on `community_member.user_id` either, so a real delete cannot leave
        this specific row-without-a-user behind. The test forces the state
        directly with a raw SQL `DELETE` and FK enforcement dropped for that
        one statement (the identical technique tests/conftest.py's teardown
        SQL uses), then `db.session.expire_all()` so `Session.get()` (`:360`)
        -- which checks the identity map before the database -- doesn't hand
        back the stale `s.local_mod` object still cached from
        `_seed_for_report` instead of re-querying and finding nothing.

        `:361` is therefore a DEFENSIVE arm, not one exercised by any known
        caller -- the same shape as `:160`, which this round registers as
        dead code rather than quietly counting. The coverage line is real
        (the arm's own behaviour -- skip rather than crash on `None` -- is
        genuinely asserted below) but nothing reaches it through the
        application; only this manufactured row does.
        """
        s = _seed_for_report()
        add_moderator(s, s.local_mod)
        ghost_id = s.local_mod.id
        db.session.execute(text(
            'SET LOCAL session_replication_role = replica'))
        db.session.execute(text('DELETE FROM "user" WHERE id = :id'),
                           {'id': ghost_id})
        db.session.commit()
        db.session.expire_all()
        payload = {'reason': 'spam', 'description': 'd', 'report_remote': False}

        reporter_id, report = report_reply(s.reply, payload, SRC_API,
                                           auth=bearer(s.reporter))

        assert reporter_id == s.reporter.id
        assert db.session.query(Notification).count() == 0

    def test_an_admin_already_notified_as_moderator_is_not_notified_twice(self, db_session):
        """`:380`'s false arm -> back to `:379`'s loop head.

        An admin who is ALSO a local moderator of the community is added to
        `already_notified` by the moderator loop (`:370`) before the admin
        loop ever runs, so `:380` must skip the second Notification and the
        `unread_notifications` increment -- both stay at their single,
        moderator-loop-caused value, not double it.
        """
        s = _seed_for_report()
        make_site()
        admin = make_user(s.local_instance, 'site-admin', local=True)
        add_moderator(s, admin)
        db.session.commit()
        from app.models import Role, user_role
        role = Role(id=ROLE_ADMIN, name='Admin', weight=0)
        db.session.add(role)
        db.session.commit()
        db.session.execute(user_role.insert().values(user_id=admin.id,
                                                     role_id=role.id))
        admin.unread_notifications = 3
        db.session.commit()
        payload = {'reason': 'csam', 'description': 'd', 'report_remote': False}

        report_reply(s.reply, payload, SRC_API, auth=bearer(s.reporter))

        db.session.refresh(admin)
        assert admin.unread_notifications == 3
        rows = db.session.query(Notification).filter_by(user_id=admin.id).all()
        assert len(rows) == 1

    def test_the_community_guard_false_arm_is_coincidence_not_correctness(self, db_session):
        """`:391`'s false arm, reached only because two unrelated id spaces
        happen to collide -- continues finding 1
        (`test_the_community_guard_compares_the_wrong_id_space`).

        `community.id` is forced to 3 by `_seed_for_report`'s D533 guard. A
        THIRD, otherwise-unrelated instance created here also lands on id 3
        (the local and remote instances that fixture already made take 1 and
        2). A moderator on that third instance is collected into
        `remote_instance_ids` unconditionally (`report_remote=True`), so by
        the time `:391` runs, `reply.community_id` (3) is already "in" a set
        that is really a set of instance ids, purely because the two
        sequences happened to reach the same integer. `:391` being False here
        is not the guard working -- it is the exact coincidence the pinned
        finding says the guard is exposed to. This test does not fix `:391`.

        Asserts the FULL set of collected instance ids, not one element's
        count: a bare `.count(third_instance.id) == 1` cannot see an extra,
        wrong id leaking in alongside it (e.g. from an inverted `:391`
        guard that starts adding `reply.community.instance_id` when it
        should not) -- exactly the kind of confusion this pin exists to
        catch, so an assertion blind to it would defeat the pin's own
        purpose.
        """
        s = _seed_for_report()
        third_instance = make_instance('third.example', software='lemmy')
        assert third_instance.id == s.community.id, (
            'this test needs the coincidence: an unrelated instance id equal '
            'to the community id, or :391 is never driven false'
        )
        third_mod = make_user(third_instance, 'third-mod', local=False)
        add_moderator(s, third_mod)
        s.community.ap_id = 'reports@remote.example'
        s.community.ap_profile_id = 'https://remote.example/c/reports'
        db.session.commit()
        payload = {'reason': 'spam', 'description': 'd', 'report_remote': True}

        with _recording_task_selector(capture_kwargs=True) as calls:
            report_reply(s.reply, payload, SRC_API, auth=bearer(s.reporter))

        report_calls = [c for c in calls if c[0] == 'report_reply']
        assert len(report_calls) == 1
        instance_ids = report_calls[0][1]['instance_ids']
        assert set(instance_ids) == {third_instance.id}

    def test_the_suspect_guard_skips_an_instance_already_collected(self, db_session):
        """`:394`'s false arm -> `:397` directly, skipping a redundant add.

        Unlike the community guard above, `:394` tests
        `suspect_user.instance_id` against `remote_instance_ids` -- the SAME
        id space -- so a remote moderator on the suspect's own instance,
        already collected unconditionally by `:376` (`report_remote=True`),
        makes `:394` correctly skip adding the same instance a second time.
        """
        s = _seed_for_report()
        s.author.instance_id = s.remote_instance.id
        s.author.ap_id = 'author@remote.example'
        s.author.ap_profile_id = 'https://remote.example/u/author'
        s.author.ap_public_url = 'https://remote.example/u/author'
        add_moderator(s, s.remote_mod)
        db.session.commit()
        payload = {'reason': 'spam', 'description': 'd', 'report_remote': True}

        with _recording_task_selector(capture_kwargs=True) as calls:
            report_reply(s.reply, payload, SRC_API, auth=bearer(s.reporter))

        report_calls = [c for c in calls if c[0] == 'report_reply']
        assert len(report_calls) == 1
        instance_ids = report_calls[0][1]['instance_ids']
        assert set(instance_ids) == {s.remote_instance.id}
