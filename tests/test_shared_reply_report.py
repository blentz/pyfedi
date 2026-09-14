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
from types import SimpleNamespace

from app import db
from app.constants import SRC_API, SRC_WEB
from app.models import Instance, Notification, Report, User
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
