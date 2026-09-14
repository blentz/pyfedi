"""Group D of app/shared/reply.py -- reporting a reply.

    report_reply   :313   55 statements / 34 arcs

The module's largest function, and at ZERO coverage when this file was
created. `tests/test_shared_post_moderation.py` covers the twin
`report_post` (app/shared/post.py:833).

EVERY `:NNN` HERE POINTS INTO app/shared/reply.py AS IT IS NUMBERED NOW.
Sub-project 42 task 7 added two statements to `restore_reply`'s bot guard,
above this function, so every citation in this file moved down by two and all
of them were re-derived in that task. `report_reply` itself did not change;
only its address did. The row above read `:311  55 / 34` before that fix.

THIS FUNCTION IS A LOOP, NOT A FORK, AND THAT SHAPES EVERY FIXTURE HERE.
`:361`-`:378` iterates `reply.community.moderators()` and branches per
moderator on `moderator.is_local()`, with `report_remote` gating which remote
instances are collected. Witnessing both arms needs at least one LOCAL and one
REMOTE moderator on the same community, which is what `_seed_for_report`
builds.

`Site.admins()` AT `:381` IS REGISTER ENTRY D442, LIVE. Its behaviour differs
where `g.admin_ids` is unset. Record what it does here; do not fix it.

`notify_admins` AT `:320`-`:321` IS A SUBSTRING TEST over two lists, so 'dox'
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
from app.constants import REPORT_TYPE_REPLY, ROLE_ADMIN, SRC_API, SRC_WEB
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
    `remote_mod` moderate the community. Four distinct users, because `:361`'s
    loop branches per moderator and `:382`'s admin block skips anyone already
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
    rows filtered on `is_banned == False`, and `:362` then loads each
    `User` by `mod.user_id`.
    """
    return make_community_member(user, s.community, is_moderator=True)


def make_site_admin(s, name='site-admin'):
    """A user `Site.admins()` will actually return, plus the `Site` row.

    ADDED BY TASK 8'S MUTATION PASS, which needed five more `notify_admins`
    tests and would otherwise have repeated this eight-line preamble in each
    of them. The mechanics are not incidental and are documented at length in
    `test_a_csam_reason_notifies_site_admins`: the `Role` must be pinned at
    `id=ROLE_ADMIN` because `Site.admins()` (app/models.py:4011-4012) joins
    on `user_role.c.role_id == ROLE_ADMIN` BY VALUE, not by the role's name,
    and `g.admin_ids` is never set in these tests so that inner-join branch
    is always the one taken (register entry D442, live).

    `unread_notifications` is seeded at 3 so `:388`'s increment is a
    transition rather than a move off the column default.
    """
    from app.models import Role, user_role
    make_site()
    admin = make_user(s.local_instance, name, local=True)
    db.session.commit()
    role = db.session.query(Role).get(ROLE_ADMIN)
    if role is None:
        role = Role(id=ROLE_ADMIN, name='Admin', weight=0)
        db.session.add(role)
        db.session.commit()
    db.session.execute(user_role.insert().values(user_id=admin.id,
                                                 role_id=role.id))
    admin.unread_notifications = 3
    db.session.commit()
    return admin


def expected_targets(s):
    """The nine-key dict `:332`-`:342` builds for an unmodified `_seed_for_report`.

    ADDED BY TASK 8'S MUTATION PASS. `targets_data` is built ONCE at `:332`
    and then handed to three different consumers -- the `Report` row
    (`:354`), every local moderator's Notification (`:370`) and every admin's
    Notification (`:386`) -- so each consumer needs the same expectation and
    a copy-pasted literal in three places would drift.

    It is written out key by key rather than read back off one of the rows,
    because comparing two things the SAME mutated statement produced proves
    nothing: a mutant that corrupts `:335` corrupts it identically in the
    Report and in the Notification, and `notification.targets ==
    report.targets` would still hold.

    `suspect_user_user_name` takes the `user_name` arm of `:336`'s ternary
    here because `_seed_for_report`'s `author` is local and `make_user`
    leaves `ap_id` `None` for local users. The other arm is witnessed by
    `test_a_remote_suspect_is_named_by_its_ap_id`.
    """
    return {
        'gen': '0',
        'suspect_comment_id': s.reply.id,
        'suspect_user_id': s.author.id,
        'suspect_user_user_name': s.author.user_name,
        'source_instance_id': s.local_instance.id,
        'source_instance_domain': s.local_instance.domain,
        'reporter_id': s.reporter.id,
        'reporter_user_name': s.reporter.user_name,
        'orig_comment_body': s.reply.body,
    }


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
    """`report_reply` (app/shared/reply.py:313-412)."""

    def test_the_api_arm_creates_the_report_row(self, db_session):
        """`:314` true, `:315`-`:322`, `:344`-`:355`, `:399`, `:410`.

        Asserts the Report's OWN fields rather than a bare count: `:344`-`:354`
        writes nine of them and a count would pass with eight deleted.
        `reply.reports` at `:399` is asserted separately because nothing else
        writes it.

        TASK 8'S MUTATION PASS FOUND THAT THREE OF ELEVEN WAS NOT ENOUGH.
        `:344`-`:354` passes ELEVEN keyword arguments and `:332`-`:342`
        builds a NINE-key `targets` dict, and only `suspect_post_reply_id`,
        `reporter_id` and `suspect_user_id` were read back. Fifteen
        argument mutants survived behind this one call -- every remaining
        `Report` column and the entire `targets` dict, which no test in
        this file read at all even though it is copied verbatim into every
        Notification the function sends.

        THE ASSERTIONS ARE WHOLE-VALUE, NOT SPOT CHECKS. `targets` is
        compared as a complete dict: nine separate `key: wrong-object`
        mutants (M124-M131) are one assertion this way, and a new key
        added later without a test cannot slip past. The `Report` row is
        compared as a tuple of all eleven fields for the same reason.

        THE ID-DISTINCTNESS GUARD BELOW NAMES PAIRS, NOT A SET, AND THAT IS
        A FINDING RATHER THAN A STYLE CHOICE. The first version of this
        test asserted all five of `reply.id`, `post.id`, `community.id`,
        `reporter.id` and `author.id` were distinct and FAILED on
        `len({1, 2, 3}) == 5`: `_seed_for_report`'s D533 guard separates
        `community.id` (3), `post.id` (2) and `reporter.id` (1), but
        `reply.id` is ALSO 1 and `author.id` is ALSO 2. Those two
        collisions mask nothing here -- no mutation in Task 8's catalogue
        substitutes `reply.id` for `reporter.id` or `post.id` for
        `author.id` -- so the four pairs the swaps below actually depend on
        are asserted by name instead of hiding a false claim behind a
        blanket one. Anyone adding a swap across one of the colliding pairs
        has to widen this guard first.

        `source_instance_id` IS NOT PINNED BY THIS TEST even though it is
        asserted here: `:353` passes `reporter_user.instance_id` and the
        reporter and the suspect are both on the local instance in this
        fixture, so swapping the operand (M141) produces the same 1.
        `test_a_remote_suspect_instance_is_added_when_reporting_remotely`
        is where those two differ and that swap is caught.
        """
        s = _seed_for_report()
        payload = {'reason': 'spam', 'description': 'clearly spam',
                   'report_remote': False}

        reporter_id, report = report_reply(s.reply, payload, SRC_API,
                                           auth=bearer(s.reporter))

        assert reporter_id == s.reporter.id
        db.session.refresh(s.reply)
        assert s.reply.reports == 1
        assert s.reply.id != s.post.id, 'D533: :334/:348/:351 swaps go blind'
        assert s.community.id != s.post.id, 'D533: :349/:352 swaps go blind'
        assert s.reporter.id != s.author.id, 'D533: :335/:339/:350 swaps go blind'
        assert s.local_instance.id != s.community.id, 'D533: :337 swap goes blind'
        rows = db.session.query(Report).all()
        assert len(rows) == 1
        row = rows[0]
        assert (row.reasons, row.description, row.type) == \
               ('spam', 'clearly spam', REPORT_TYPE_REPLY)
        assert (row.reporter_id, row.suspect_post_id, row.suspect_community_id,
                row.suspect_user_id, row.suspect_post_reply_id,
                row.in_community_id, row.source_instance_id) == \
               (s.reporter.id, s.post.id, s.community.id, s.author.id,
                s.reply.id, s.community.id, s.reporter.instance_id)
        assert row.targets == {
            'gen': '0',
            'suspect_comment_id': s.reply.id,
            'suspect_user_id': s.author.id,
            'suspect_user_user_name': s.author.user_name,
            'source_instance_id': s.local_instance.id,
            'source_instance_domain': s.local_instance.domain,
            'reporter_id': s.reporter.id,
            'reporter_user_name': s.reporter.user_name,
            'orig_comment_body': s.reply.body,
        }

    def test_the_web_arm_reads_the_form_and_returns_none(self, db_session, app):
        """`:314` false -> `:324`-`:330`, and `:412`'s bare return.

        The web arm builds `reason` from `input.reasons_to_string(...)` and
        `notify_admins` from membership of '5' or '6' in `reasons.data` --
        a DIFFERENT mechanism from the API arm's substring test, which is why
        both arms need their own test rather than one parameterised over src.

        `reasons` AND `description` ARE READ BACK OFF THE ROW, added by
        Task 8's mutation pass: `:327` and `:328` are the web arm's own two
        value reads and neither was observed, so replacing either with a
        constant (M119, M120) left this test green. `reasons_to_string` is
        a lambda returning a string that differs from `description`, so the
        two columns cannot be satisfied by the same value.
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
        assert len(rows) == 1
        assert rows[0].reasons == 'spam'
        assert rows[0].description == 'a web report'

    def test_a_local_moderator_is_notified(self, db_session):
        """`:361`'s loop, `:364`'s true arm, `:365`-`:372`.

        The Notification's `user_id` must be the MODERATOR's and its
        `author_id` the REPORTER's -- a mutant swapping those two operands
        produces the same row count and is caught only by asserting both.

        THE OTHER FOUR ARGUMENTS ARE ASSERTED TOO, added by Task 8's
        mutation pass. `:366`-`:370` passes SEVEN things and only the two
        ids were read back, so four mutants survived on a line this test
        executes on every run: the title changed from 'A comment has been
        reported' to 'A post has been reported' (M150), the url built from
        `reply.post_id` instead of `reply.id` (M151), the subtype changed
        from 'comment_reported' to 'post_reported' (M153), and `targets`
        dropped entirely (M154). Every one of those is what the recipient
        actually SEES; none of them was observed.

        THE URL IS COMPARED AGAINST `SERVER_URL` FROM THE LIVE CONFIG
        rather than a literal, because `:367` interpolates
        `current_app.config['SERVER_URL']` and hard-coding the test
        environment's value would make this assertion fail for a reason
        that has nothing to do with `report_reply`. The id inside it is the
        part under test: `reply.id` is 1 and `reply.post_id` is 2 in this
        fixture, so the swap is observable.
        """
        from flask import current_app
        s = _seed_for_report()
        add_moderator(s, s.local_mod)
        payload = {'reason': 'spam', 'description': 'd', 'report_remote': False}

        report_reply(s.reply, payload, SRC_API, auth=bearer(s.reporter))

        rows = db.session.query(Notification).all()
        assert {n.user_id for n in rows} == {s.local_mod.id}
        assert {n.author_id for n in rows} == {s.reporter.id}
        assert len(rows) == 1
        notification = rows[0]
        assert notification.title == 'A comment has been reported'
        assert notification.url == \
               f"{current_app.config['SERVER_URL']}/comment/{s.reply.id}"
        assert notification.subtype == 'comment_reported'
        assert notification.targets == expected_targets(s)

    def test_the_notification_locale_is_the_recipients_not_the_reporters(self, db_session):
        """`:365`'s `get_recipient_language(moderator.id)`.

        ADDED BY TASK 8'S MUTATION PASS: swapping that argument for
        `reporter_user.id` (M148) left every test green -- the notification
        for a moderator would be composed in the REPORTER's language, which
        is the sort of bug that is invisible until it reaches someone who
        does not read the reporter's.

        THE ARGUMENT IS THE ONLY AVAILABLE WITNESS, AND THAT IS DELIBERATE
        RATHER THAN A CONVENIENCE. `:365`-`:366` composes the title inside
        `force_locale(...)` with `gettext`, and `gettext` falls back to the
        msgid when no catalogue is loaded -- which is the case under test,
        so BOTH users' locales produce the byte-identical English string
        and `notification.title` cannot distinguish them. Both users here
        also have `language_id` and `interface_language` `None`
        (`make_user` sets neither), which makes `get_recipient_language`
        return `'en'` for each regardless; giving one of them a different
        language would still produce the same title for the same
        catalogue-free reason. Recording which id was passed is therefore
        the only thing that separates the two, not the first thing that
        came to hand.

        The rebinding targets `app.shared.reply.get_recipient_language` --
        the name `:365` calls, imported at app/shared/reply.py:15 -- for
        the same reason `_recording_task_selector` patches
        `app.shared.reply.task_selector`, and restores it in a `finally`
        so a failure cannot leak the patch.

        THE REPORTER AND THE MODERATOR ARE DIFFERENT USERS in
        `_seed_for_report`, asserted below, or the swap would be an
        equivalent mutant.
        """
        s = _seed_for_report()
        add_moderator(s, s.local_mod)
        assert s.local_mod.id != s.reporter.id
        payload = {'reason': 'spam', 'description': 'd', 'report_remote': False}
        seen = []
        original = reply_module.get_recipient_language

        def recorder(user_id):
            seen.append(user_id)
            return original(user_id)

        reply_module.get_recipient_language = recorder
        try:
            report_reply(s.reply, payload, SRC_API, auth=bearer(s.reporter))
        finally:
            reply_module.get_recipient_language = original

        assert seen == [s.local_mod.id]

    def test_a_remote_moderator_is_not_notified_locally(self, db_session):
        """`:364`'s false arm -> `:373`-`:378`.

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
        """`:374`'s true arm and `:375`'s three-way comparison.

        With `report_remote` False, a remote moderator is collected ONLY if
        its instance differs from both the suspect's and the community's. Here
        the remote moderator shares the suspect's instance, so nothing is
        collected and `:402`'s `len(remote_instance_ids)` is zero.
        """
        s = _seed_for_report()
        s.author.instance_id = s.remote_instance.id
        db.session.commit()
        add_moderator(s, s.remote_mod)
        payload = {'reason': 'spam', 'description': 'd', 'report_remote': False}

        with _recording_task_selector() as calls:
            report_reply(s.reply, payload, SRC_API, auth=bearer(s.reporter))

        assert 'report_reply' not in calls

    def test_the_web_arm_honours_report_remote_false(self, db_session, app):
        """`:330` -- the web arm's own `report_remote` read.

        ADDED BY TASK 8'S MUTATION PASS: replacing `:330` with a constant
        `True` (M123) left every test green. The three web tests in this
        file all send `report_remote=False` on a fixture with no remote
        moderator and a local community and suspect, where forcing it True
        collects nothing anyway -- an equivalent mutant against all of
        them.

        This is the web twin of `test_report_remote_false_skips_a_mod_on_
        the_suspects_instance`: a remote moderator sharing the suspect's
        instance, which `:375` excludes while `report_remote` is False and
        `:378` would collect unconditionally if it were True.
        """
        s = _seed_for_report()
        s.author.instance_id = s.remote_instance.id
        add_moderator(s, s.remote_mod)
        db.session.commit()
        form = SimpleNamespace(
            reasons=SimpleNamespace(data=['1']),
            description=SimpleNamespace(data='a web report'),
            report_remote=SimpleNamespace(data=False),
            reasons_to_string=lambda data: 'spam',
        )

        with _recording_task_selector() as calls:
            with web_ctx(app, s.reporter):
                report_reply(s.reply, form, SRC_WEB, auth=None)

        assert 'report_reply' not in calls

    def test_report_remote_false_still_collects_an_unrelated_remote_mod(self, db_session):
        """`:375`'s TRUE arm and `:376`'s add -- the only path that produces
        an effect while `report_remote` is False.

        ADDED BY TASK 8'S MUTATION PASS. `test_report_remote_false_skips_a_
        mod_on_the_suspects_instance` drives `:375` to False and asserts
        NOTHING was collected, which is also what happens when `:376` does
        not exist -- so the add inside the guard was never observed, and
        two mutants survived: `:376` deleted outright (M161) and `:376`
        adding `suspect_user.instance_id` instead of the moderator's own
        (M162).

        THE MODERATOR IS ON A FOURTH INSTANCE, and that number is not
        arbitrary. `_seed_for_report` consumes instance ids 1 (local) and 2
        (remote), and id 3 collides with `community.id`, which
        `test_the_community_guard_false_arm_is_coincidence_not_correctness`
        depends on and this test must stay clear of. One spare instance is
        created to consume id 3 and the moderator goes on id 4, so the
        collected set is unambiguous.

        `:375` needs the moderator's instance to differ from BOTH the
        suspect's and the community's, so the suspect is moved to the
        remote instance (2) while the community stays local (1) and the
        moderator sits on 4.
        """
        s = _seed_for_report()
        s.author.instance_id = s.remote_instance.id
        make_instance('third.example', software='lemmy')
        fourth_instance = make_instance('fourth.example', software='lemmy')
        assert fourth_instance.id not in (s.community.id, s.community.instance_id,
                                          s.remote_instance.id)
        unrelated_mod = make_user(fourth_instance, 'unrelated-mod', local=False)
        add_moderator(s, unrelated_mod)
        db.session.commit()
        payload = {'reason': 'spam', 'description': 'd', 'report_remote': False}

        with _recording_task_selector(capture_kwargs=True) as calls:
            report_reply(s.reply, payload, SRC_API, auth=bearer(s.reporter))

        report_calls = [c for c in calls if c[0] == 'report_reply']
        assert len(report_calls) == 1
        assert set(report_calls[0][1]['instance_ids']) == {fourth_instance.id}

    def test_report_remote_false_skips_a_mod_on_the_communitys_instance(self, db_session):
        """`:375`'s SECOND conjunct,
        `moderator.instance_id != reply.community.instance_id`.

        ADDED BY TASK 8'S MUTATION PASS: deleting that conjunct (M160) left
        every test green. `test_report_remote_false_skips_a_mod_on_the_
        suspects_instance` drives the FIRST conjunct false, and with it
        false the second is never the reason anything is skipped.

        THE MODERATOR IS NON-LOCAL BUT ON THE LOCAL INSTANCE, which is the
        only configuration that isolates the second conjunct.
        `moderator.is_local()` (app/models.py:1251-1252) reads `ap_id`, not
        `instance_id` -- the trap this file already records at
        `test_a_remote_suspect_instance_is_added_when_reporting_remotely`
        -- so `make_user(local_instance, ..., local=False)` produces a user
        that takes `:364`'s remote arm while carrying `instance_id == 1`,
        the community's own instance. With the suspect moved to instance 2,
        `:375` reads `1 != 2 and 1 != 1` -> False and nothing is collected;
        the mutant reads `1 != 2` -> True and sends a Flag to the
        community's own instance.
        """
        s = _seed_for_report()
        s.author.instance_id = s.remote_instance.id
        house_mod = make_user(s.local_instance, 'house-mod', local=False)
        add_moderator(s, house_mod)
        db.session.commit()
        assert house_mod.instance_id == s.community.instance_id
        assert house_mod.instance_id != s.author.instance_id
        payload = {'reason': 'spam', 'description': 'd', 'report_remote': False}

        with _recording_task_selector() as calls:
            report_reply(s.reply, payload, SRC_API, auth=bearer(s.reporter))

        assert 'report_reply' not in calls

    def test_report_remote_false_skips_the_whole_remote_block(self, db_session):
        """`:391`'s FALSE arm, witnessed on a fixture where its TRUE arm
        would have done something.

        ADDED BY TASK 8'S MUTATION PASS: forcing `:391` to `if True:`
        (M174) left every test green. Every `report_remote=False` test in
        this file has a LOCAL community and a LOCAL suspect, so `:392` and
        `:395` are both false and the block is a no-op whether it runs or
        not -- the gate was covered but not decided.

        Here the community is genuinely non-local, so entering the block
        would collect its instance and fire a Flag. `report_remote` is
        False, so nothing may be collected at all.

        `Community.is_local()` (app/models.py:795-796) reads `ap_id` /
        `profile_id()`, not `instance_id` -- see
        `test_the_community_guard_compares_the_wrong_id_space` for the same
        trap -- so `ap_id` and `ap_profile_id` are set alongside
        `instance_id` here.
        """
        s = _seed_for_report()
        s.community.instance_id = s.remote_instance.id
        s.community.ap_id = 'reports@remote.example'
        s.community.ap_profile_id = 'https://remote.example/c/reports'
        db.session.commit()
        assert s.community.is_local() is False
        payload = {'reason': 'spam', 'description': 'd', 'report_remote': False}

        with _recording_task_selector() as calls:
            report_reply(s.reply, payload, SRC_API, auth=bearer(s.reporter))

        assert 'report_reply' not in calls

    def test_report_remote_true_collects_every_remote_moderator(self, db_session):
        """`:377`-`:378` -- the else arm of `:374`.

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
        """`:320`-`:321`'s substring test, `:380`'s true arm, `:381`-`:388`.

        `Site.admins()` at `:381` IS REGISTER ENTRY D442, LIVE. Record what it
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
        because `:388` is a separate statement a mutant can delete on its
        own.

        THE ADMIN NOTIFICATION'S OWN FIELDS ARE ASSERTED, added by Task 8's
        mutation pass: `:383`-`:386` passes six things and only `user_id`
        was read back, so three mutants survived -- title 'Suspicious
        content' -> 'Ordinary content' (M168), url '/admin/reports' ->
        '/admin/' (M169), and `author_id=reporter_user.id` ->
        `author_id=admin.id` (M170). The last is the same
        recipient-vs-actor swap `test_a_local_moderator_is_notified` guards
        against on the other Notification, and it was unguarded here.

        THIS ADMIN NOTIFICATION IS NOT THE MODERATOR ONE. No moderator is
        added in this test, so the single row below is `:383`'s and the
        assertions cannot be satisfied by `:366`'s -- their titles, urls
        and subtypes all differ.
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
        assert len(rows) == 1
        notify = rows[0]
        assert notify.title == 'Suspicious content'
        assert notify.url == '/admin/reports'
        assert notify.author_id == s.reporter.id
        assert notify.author_id != admin.id, (
            'the reporter and the admin must differ or the author_id swap is '
            'an equivalent mutant'
        )
        assert notify.targets == expected_targets(s)

    def test_an_uppercase_dox_reason_notifies_site_admins(self, db_session):
        """`:320`'s FIRST disjunct, both of its operands at once.

        ADDED BY TASK 8'S MUTATION PASS. `:320` is
        `any(x in reason.lower() for x in ['csam', 'dox'])` and
        `test_a_csam_reason_notifies_site_admins` sends the lowercase word
        'csam', which exercises neither the second list entry nor the
        case-fold. Two mutants survived on it:

          `['csam', 'dox']` reduced to `['csam']` (M113) -- 'dox' reports
            silently stopped reaching admins.
          `reason.lower()` reduced to `reason` (M198) -- any report whose
            reason is not already lowercase silently stopped reaching them.

        A reason of `'DOX'` fails against both and passes against the real
        code, so one input witnesses both properties. It is deliberately
        NOT a lowercase 'dox': that would leave the case-fold unwitnessed
        and need a second test.

        THE ADMIN IS NOTIFIED THROUGH `Site.admins()`, whose id-vs-name
        role trap is documented in full at
        `test_a_csam_reason_notifies_site_admins`; `make_site_admin` exists
        so that explanation is written once.
        """
        s = _seed_for_report()
        admin = make_site_admin(s)
        payload = {'reason': 'DOX', 'description': 'd', 'report_remote': False}

        report_reply(s.reply, payload, SRC_API, auth=bearer(s.reporter))

        db.session.refresh(admin)
        assert admin.unread_notifications == 4
        assert {n.user_id for n in db.session.query(Notification).all()} == {admin.id}

    def test_an_uppercase_dox_description_notifies_site_admins(self, db_session):
        """`:321`'s SECOND disjunct -- the description half of `notify_admins`.

        ADDED BY TASK 8'S MUTATION PASS. Every admin test in this file put
        the trigger word in the REASON, so `:321` was executed but never
        decided anything, and three mutants survived:

          the whole disjunct replaced by `False` (M111) -- reports that name
            the offence only in their free-text description stopped
            reaching admins.
          `['csam', 'dox']` reduced to `['csam']` (M114).
          `description.lower()` reduced to `description` (M199).

        `reason` is an ordinary 'spam' here, so `:320`'s disjunct is false
        and `:321` is the only thing that can make `notify_admins` true --
        which is what makes all three of those mutants fail this test. The
        uppercase 'DOX' covers the list entry and the case-fold in the same
        input, exactly as the reason-side test above does.

        THE SUBSTRING BEHAVIOUR IS NOT ASSERTED HERE and is not fixed: the
        module docstring records that 'dox' matches any word containing it.
        This test sends the bare word.
        """
        s = _seed_for_report()
        admin = make_site_admin(s)
        payload = {'reason': 'spam', 'description': 'DOX', 'report_remote': False}

        report_reply(s.reply, payload, SRC_API, auth=bearer(s.reporter))

        db.session.refresh(admin)
        assert admin.unread_notifications == 4
        assert {n.user_id for n in db.session.query(Notification).all()} == {admin.id}

    def test_the_web_arm_notifies_admins_on_reason_five(self, db_session, app):
        """`:329`'s FIRST disjunct, `'5' in input.reasons.data`.

        ADDED BY TASK 8'S MUTATION PASS: `:329` is the web arm's OWN
        `notify_admins` mechanism -- a membership test over the form's
        selected reason codes, not the API arm's substring scan -- and no
        web test ever selected code 5 or 6, so both of its disjuncts could
        be deleted with every test still green (M121, M122).

        `reasons_to_string` is a lambda here as it is in the other web
        tests; the string it returns is irrelevant to `:329`, which reads
        `input.reasons.data` directly.
        """
        s = _seed_for_report()
        admin = make_site_admin(s)
        form = SimpleNamespace(
            reasons=SimpleNamespace(data=['5']),
            description=SimpleNamespace(data='a web report'),
            report_remote=SimpleNamespace(data=False),
            reasons_to_string=lambda data: 'spam',
        )

        with web_ctx(app, s.reporter):
            report_reply(s.reply, form, SRC_WEB, auth=None)

        db.session.refresh(admin)
        assert admin.unread_notifications == 4

    def test_the_web_arm_notifies_admins_on_reason_six(self, db_session, app):
        """`:329`'s SECOND disjunct, `'6' in input.reasons.data`.

        ADDED BY TASK 8'S MUTATION PASS, for the same reason as the reason-5
        test above: code 6 alone is the only input on which deleting the
        second disjunct (M122) is observable, since code 5 satisfies the
        first and masks it.
        """
        s = _seed_for_report()
        admin = make_site_admin(s)
        form = SimpleNamespace(
            reasons=SimpleNamespace(data=['6']),
            description=SimpleNamespace(data='a web report'),
            report_remote=SimpleNamespace(data=False),
            reasons_to_string=lambda data: 'spam',
        )

        with web_ctx(app, s.reporter):
            report_reply(s.reply, form, SRC_WEB, auth=None)

        db.session.refresh(admin)
        assert admin.unread_notifications == 4

    def test_an_ordinary_reason_does_not_notify_admins(self, db_session):
        """`:380`'s false arm -- the same-mechanism positive control.

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
        """`:395`'s true arm and `:396`-`:397`.

        The suspect-user half of the `report_remote` block, which is the half
        written CORRECTLY -- it guards on `suspect_user.instance_id` and adds
        the same value.

        `User.is_local()` (app/models.py:1251-1252) tests `ap_id`, not
        `instance_id` -- `author` was minted with `local=True`, which leaves
        `ap_id` `None` forever, so moving `instance_id` alone would not flip
        `is_local()`. `ap_id`/`ap_profile_id`/`ap_public_url` are set here to
        a URL on `remote.example` so `is_local()` is actually False and
        `:395`'s guard fires.

        `capture_kwargs=True` and asserting the actual `instance_ids` set
        (not just `'report_reply' in calls`) is required: a mutant swapping
        `:397`'s operand to add `reply.community.instance_id` instead of
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
        assert report_calls[0][1]['user_id'] == s.reporter.id
        assert report_calls[0][1]['reply_id'] == s.reply.id
        assert s.reporter.id != s.author.id, (
            'the reporter and the suspect must differ or :407\'s user_id '
            'swap is an equivalent mutant'
        )
        report_row = db.session.query(Report).one()
        assert report_row.source_instance_id == s.reporter.instance_id
        assert s.reporter.instance_id != s.author.instance_id, (
            ':353 passes the REPORTER\'s instance; with both users on one '
            'instance the operand swap is an equivalent mutant'
        )

    def test_a_remote_suspect_is_named_by_its_ap_id(self, db_session):
        """`:316`'s lookup, `:317`'s lookup and `:336`'s ternary, all three
        driven by a suspect who is neither the post's author nor local.

        ADDED BY TASK 8'S MUTATION PASS, WHICH FOUND `_seed_for_report`
        CANNOT SEPARATE THEM. That fixture gives its post and its one reply
        the SAME author, on the SAME instance as the reporter, with `ap_id`
        `None` -- so three distinct mutations were all equivalent against
        every test in this file:

          `:316` resolving the suspect from `reply.post.user_id` instead of
            `reply.user_id` (M105) -- the same user either way.
          `:317` resolving the source instance from
            `reporter_user.instance_id` instead of `reply.instance_id`
            (M107) -- the same instance either way.
          `:336`'s `suspect_user.ap_id if suspect_user.ap_id else
            suspect_user.user_name` reduced to the `user_name` arm (M126) --
            a local user's `ap_id` is `None`, so the ternary already took
            that arm.

        This test builds a reply by a REMOTE user who did not write the
        post, which separates all three at once: the suspect's id differs
        from the post author's, the reply's instance differs from the
        reporter's, and the suspect has a real `ap_id`.

        `make_user(..., local=False)` is what supplies the `ap_id`
        (tests/factories.py:60) and `make_post_reply` copies the author's
        `instance_id` onto the reply (`:467`), so no attribute is set by
        hand here.

        `report.source_instance_id` IS ALSO ASSERTED, and it is a DIFFERENT
        statement from the targets key with the same name: `:353` passes
        `reporter_user.instance_id` (the local instance) while `:337` passes
        `source_instance.id` (the remote one). They disagree by design on
        this input, which is what makes `:353`'s operand swap (M141)
        observable here and nowhere else in this file.
        """
        s = _seed_for_report()
        remote_author = make_user(s.remote_instance, 'remote-author', local=False)
        remote_reply = make_post_reply(s.post, remote_author)
        db.session.commit()
        assert remote_author.id != s.post.user_id
        assert remote_author.ap_id is not None
        payload = {'reason': 'spam', 'description': 'd', 'report_remote': False}

        report_reply(remote_reply, payload, SRC_API, auth=bearer(s.reporter))

        row = db.session.query(Report).one()
        assert row.targets['suspect_user_user_name'] == remote_author.ap_id
        assert row.targets['suspect_user_id'] == remote_author.id
        assert row.targets['source_instance_id'] == s.remote_instance.id
        assert row.targets['source_instance_domain'] == s.remote_instance.domain
        assert row.source_instance_id == s.reporter.instance_id

    def test_the_web_arm_resolves_the_suspect_and_the_source_instance(self, db_session, app):
        """`:325` and `:326` -- the web arm's own two lookups.

        ADDED BY TASK 8'S MUTATION PASS. They are separate statements from
        the API arm's `:316`/`:317`, written with `Query.get` rather than
        `filter_by(...).one()`, and they carried the same two unpinned
        operands: the suspect resolved from `reply.post.user_id` (M117) and
        the source instance from `reporter_user.instance_id` (M118). The
        API-arm test above cannot reach either line.

        Same remote-author fixture and the same reasoning as
        `test_a_remote_suspect_is_named_by_its_ap_id`; see that docstring
        for why `_seed_for_report` alone cannot separate these.
        """
        s = _seed_for_report()
        remote_author = make_user(s.remote_instance, 'remote-author', local=False)
        remote_reply = make_post_reply(s.post, remote_author)
        db.session.commit()
        form = SimpleNamespace(
            reasons=SimpleNamespace(data=['1']),
            description=SimpleNamespace(data='a web report'),
            report_remote=SimpleNamespace(data=False),
            reasons_to_string=lambda data: 'spam',
        )

        with web_ctx(app, s.reporter):
            report_reply(remote_reply, form, SRC_WEB, auth=None)

        row = db.session.query(Report).one()
        assert row.targets['suspect_user_id'] == remote_author.id
        assert row.targets['suspect_user_user_name'] == remote_author.ap_id
        assert row.targets['source_instance_id'] == s.remote_instance.id

    def test_the_community_guard_compares_the_wrong_id_space(self, db_session):
        """`:392`-`:394` -- AND IT PINS A REGISTERED DEFECT ON PURPOSE.

        `:393` is `if reply.community_id not in remote_instance_ids:` and
        `:394` adds `reply.community.instance_id`. Those are DIFFERENT ID
        SPACES: the guard tests a community id against a set of instance ids,
        so it cannot do what it is written to do. The `suspect_user` block at
        `:395`-`:397` gets the identical pattern right, which is what makes
        this a slip rather than a convention.

        `Community.is_local()` (app/models.py:795-796) is `ap_id is None or
        profile_id().startswith(SERVER_URL)` -- `make_community` never sets
        `ap_id`, so moving `instance_id` alone leaves `is_local()` True and
        `:392`'s guard would never fire. `ap_id` and `ap_profile_id` are set
        here to a `remote.example` URL, alongside `instance_id`, so
        `is_local()` is actually False.

        THIS TEST RECORDS TODAY'S BEHAVIOUR AND THIS ROUND DOES NOT FIX IT --
        the production budget is the counter fix, and a duplicate Flag to one
        instance is a different blast radius from a permanently drifting
        counter. IF A LATER ROUND REPAIRS `:393` TO GUARD ON
        `reply.community.instance_id`, THE EDIT OWED HERE IS TO ASSERT THE
        INSTANCE APPEARS EXACTLY ONCE rather than that the branch was taken.

        Registered as this round's finding 1.

        THE ASSERTION IS NOW THE COLLECTED SET, NOT 'A TASK FIRED', after
        Task 8's mutation pass: `:394` adds `reply.community.instance_id`
        (2 here) and swapping it for `reply.community_id` (3) still fired
        the task, so M178 survived against the old `'report_reply' in
        calls`. The two id spaces this test exists to distinguish are
        exactly the two values that swap would confuse, so an assertion
        blind to which one arrived defeated the pin's own purpose -- the
        same correction the sibling coincidence test already carries.
        """
        s = _seed_for_report()
        s.community.instance_id = s.remote_instance.id
        s.community.ap_id = 'reports@remote.example'
        s.community.ap_profile_id = 'https://remote.example/c/reports'
        db.session.commit()
        assert s.community.id != s.community.instance_id, (
            'this test needs the community id and its instance id to differ, '
            'or :394\'s operand swap is invisible'
        )
        payload = {'reason': 'spam', 'description': 'd', 'report_remote': True}

        with _recording_task_selector(capture_kwargs=True) as calls:
            report_reply(s.reply, payload, SRC_API, auth=bearer(s.reporter))

        report_calls = [c for c in calls if c[0] == 'report_reply']
        assert len(report_calls) == 1
        assert set(report_calls[0][1]['instance_ids']) == \
               {s.community.instance_id}

    def test_a_description_is_appended_to_the_summary(self, db_session):
        """`:404`'s true arm and `:405`'s concatenation.

        The summary is only built when `:402` finds a remote instance, so this
        test needs the remote path as well. Assert the JOINED string, because
        `:403` alone would pass with `:405` deleted.
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
        """`:404`'s false arm -- the counterpart of the test above.

        Together they prove `:405` is driven by `description` rather than
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
        """`:363`'s false arm -> back to `:361`'s loop head.

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
        SQL uses), then `db.session.expire_all()` so `Session.get()` (`:362`)
        -- which checks the identity map before the database -- doesn't hand
        back the stale `s.local_mod` object still cached from
        `_seed_for_report` instead of re-querying and finding nothing.

        `:363` is therefore a DEFENSIVE arm, not one exercised by any known
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
        """`:382`'s false arm -> back to `:381`'s loop head.

        An admin who is ALSO a local moderator of the community is added to
        `already_notified` by the moderator loop (`:372`) before the admin
        loop ever runs, so `:382` must skip the second Notification and the
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
        """`:393`'s false arm, reached only because two unrelated id spaces
        happen to collide -- continues finding 1
        (`test_the_community_guard_compares_the_wrong_id_space`).

        `community.id` is forced to 3 by `_seed_for_report`'s D533 guard. A
        THIRD, otherwise-unrelated instance created here also lands on id 3
        (the local and remote instances that fixture already made take 1 and
        2). A moderator on that third instance is collected into
        `remote_instance_ids` unconditionally (`report_remote=True`), so by
        the time `:393` runs, `reply.community_id` (3) is already "in" a set
        that is really a set of instance ids, purely because the two
        sequences happened to reach the same integer. `:393` being False here
        is not the guard working -- it is the exact coincidence the pinned
        finding says the guard is exposed to. This test does not fix `:393`.

        Asserts the FULL set of collected instance ids, not one element's
        count: a bare `.count(third_instance.id) == 1` cannot see an extra,
        wrong id leaking in alongside it (e.g. from an inverted `:393`
        guard that starts adding `reply.community.instance_id` when it
        should not) -- exactly the kind of confusion this pin exists to
        catch, so an assertion blind to it would defeat the pin's own
        purpose.
        """
        s = _seed_for_report()
        third_instance = make_instance('third.example', software='lemmy')
        assert third_instance.id == s.community.id, (
            'this test needs the coincidence: an unrelated instance id equal '
            'to the community id, or :393 is never driven false'
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
        """`:396`'s false arm -> `:399` directly, skipping a redundant add.

        Unlike the community guard above, `:396` tests
        `suspect_user.instance_id` against `remote_instance_ids` -- the SAME
        id space -- so a remote moderator on the suspect's own instance,
        already collected unconditionally by `:378` (`report_remote=True`),
        makes `:396` correctly skip adding the same instance a second time.
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
