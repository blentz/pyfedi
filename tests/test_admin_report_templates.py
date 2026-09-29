"""Every `Report.type` the product can store, rendered on `/admin/reports`.

`admin/reports.html` renders each row with

    {% include "admin/reports/" + str(report.type_text()).lower() + "_report.html" %}

so the include path is computed from data, and `Report.type_text()` maps the five
`REPORT_TYPE_*` constants onto

    ('User', 'Post', 'Comment', 'Community', 'Conversation')

D1393. There was no `admin/reports/community_report.html`. `REPORT_TYPE_COMMUNITY`
is 3 and `types[3]` is `'Community'`, so a single community report made the WHOLE
queue fail:

    Report.type=0  200
    Report.type=1  200
    Report.type=2  200
    Report.type=3  TemplateNotFound: admin/reports/community_report.html
    Report.type=4  200

The include is inside `{% for report in reports.items %}`, so it is not one broken
row -- the page an admin opens to triage reports could not be opened at all, and
every other pending report went with it. Any user can report a community
(`app/community/routes.py:1319`), so one report is enough.

WHAT ROUND 189 GOT WRONG, corrected here. That round noticed `type_text` is
`types[self.type]` with no bounds check, reasoned that every writer passes a
`REPORT_TYPE_*` constant, and recorded the unbounded index as an unreachable
robustness gap. The reasoning was right and the conclusion was too narrow: the
index is fine, and the *name it returns* is what had no template. Its own tests
missed it because the only type-3 report they created was excluded by a type
filter, so its row was never rendered.

`REPORT_TYPE_MESSAGE` is 4 and `types[4]` is `'Conversation'`, which reads oddly
and is correct: both message-report producers store `suspect_conversation_id`,
which `conversation_report.html` renders. That is asserted below so the odd
mapping is recorded rather than re-investigated.
"""
import pytest
from flask import g

from app import db
from app.constants import (REPORT_TYPE_COMMUNITY, REPORT_TYPE_MESSAGE,
                           REPORT_TYPE_POST, REPORT_TYPE_REPLY, REPORT_TYPE_USER)
from app.models import Report, Site
from tests.factories import make_community, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')

ALL_TYPES = [REPORT_TYPE_USER, REPORT_TYPE_POST, REPORT_TYPE_REPLY,
             REPORT_TYPE_COMMUNITY, REPORT_TYPE_MESSAGE]


@pytest.fixture
def admin_client(app, db_session):
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    local = make_instance('test.piefed.local')
    admin = make_user(local, 'theadmin', local=True)
    assert admin.id == 1  # fact 347
    db.session.commit()
    g.admin_ids = [admin.id]
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(admin.id)
        sess['_fresh'] = True
    return client


def a_report(rtype, **columns):
    report = Report(reasons='REPORTREASON', description='REPORTDESCRIPTION',
                    status=0, type=rtype, reporter_id=1, source_instance_id=1,
                    **columns)
    db.session.add(report)
    db.session.commit()
    return report


class TestEveryTypeRenders:
    @pytest.mark.parametrize('rtype', ALL_TYPES)
    def test_the_queue_opens(self, app, admin_client, rtype):
        """One report of each type, alone. Before D1393 the type-3 row raised
        TemplateNotFound and took the page with it."""
        a_report(rtype)

        assert admin_client.get('/admin/reports').status_code == 200

    def test_every_type_at_once(self, app, admin_client):
        """The realistic queue: one bad row must not hide the other four."""
        for rtype in ALL_TYPES:
            a_report(rtype)

        response = admin_client.get('/admin/reports')

        assert response.status_code == 200
        assert response.data.count(b'REPORTREASON') >= len(ALL_TYPES)

    @pytest.mark.parametrize('rtype', ALL_TYPES)
    def test_the_reason_and_description_are_shown(self, app, admin_client, rtype):
        """Each template reads `report.reasons` and `report.description` off the
        row, so a template that renders but shows nothing would still pass the
        status check above."""
        a_report(rtype)

        body = admin_client.get('/admin/reports').data

        assert b'REPORTREASON' in body
        assert b'REPORTDESCRIPTION' in body


class TestTheCommunityReportRow:
    def test_it_names_the_community(self, app, admin_client):
        """`suspect_community_name`, added to the producer with the template: a
        `Report` has FK columns and no relationships, so a template cannot follow
        `suspect_community_id` to a name."""
        community = make_community('reportedcommunity')
        db.session.commit()
        a_report(REPORT_TYPE_COMMUNITY,
                 suspect_community_id=community.id,
                 targets={'gen': '0', 'suspect_community_id': community.id,
                          'suspect_community_name': community.link(),
                          'reporter_id': 1, 'reporter_user_name': 'theadmin'})

        body = admin_client.get('/admin/reports').data

        assert b'reportedcommunity' in body
        assert b'theadmin' in body

    def test_a_real_report_names_the_community(self, app, admin_client):
        """Through `/community/<id>/report`, so the PRODUCER's key names are what
        the template reads.

        The test above builds `targets` by hand, which means a producer that
        renamed the key would still pass it -- that mutant survived until this row
        existed. Here the dict comes from `community_report`.
        """
        from flask import session as flask_session
        from flask_wtf.csrf import generate_csrf

        community = make_community('realreportedcommunity')
        db.session.commit()
        with app.test_request_context():
            token = generate_csrf()
            raw = flask_session['csrf_token']
        with admin_client.session_transaction() as sess:
            sess['csrf_token'] = raw

        posted = admin_client.post(f'/community/community/{community.id}/report',
                                   data={'csrf_token': token, 'reasons': ['1'],
                                         'description': 'REALDESCRIPTION'})
        assert posted.status_code in (200, 302), posted.data[:300]
        assert Report.query.count() == 1, 'the report was not created'

        body = admin_client.get('/admin/reports').data

        assert b'realreportedcommunity' in body
        assert b'REALDESCRIPTION' in body

    def test_a_row_stored_before_the_name_existed_still_renders(self, app,
                                                                admin_client):
        """Reports written before D1393 have only `suspect_community_id` in their
        targets. The row shows the id as text rather than guessing a url from it,
        and must not raise."""
        community = make_community('oldreportedcommunity')
        db.session.commit()
        a_report(REPORT_TYPE_COMMUNITY,
                 suspect_community_id=community.id,
                 targets={'gen': '0', 'suspect_community_id': community.id,
                          'reporter_id': 1})

        response = admin_client.get('/admin/reports')

        assert response.status_code == 200
        assert str(community.id).encode() in response.data

    def test_a_row_with_no_targets_at_all_still_renders(self, app, admin_client):
        """`targets` is nullable, and `report.targets.anything` on None is
        Undefined in Jinja -- which renders as empty inside `{% if %}` and raises
        on attribute access. Every read in this template is inside an `{% if %}`
        or is a plain column, so the row degrades to its reasons."""
        a_report(REPORT_TYPE_COMMUNITY)

        response = admin_client.get('/admin/reports')

        assert response.status_code == 200
        assert b'REPORTREASON' in response.data


class TestTheTypeToTemplateMapping:
    def test_every_constant_has_a_template(self):
        """The property behind D1393, asserted against the filesystem: a sixth
        `REPORT_TYPE_*` added without its template would fail here instead of on
        an admin's first visit."""
        import pathlib

        from app.models import Report as ReportModel

        for rtype in ALL_TYPES:
            name = ReportModel(type=rtype).type_text().lower()
            path = pathlib.Path(f'app/templates/admin/reports/{name}_report.html')
            assert path.exists(), (rtype, name, str(path))

    def test_the_message_type_maps_to_the_conversation_template(self):
        """`REPORT_TYPE_MESSAGE` is 4 and `types[4]` is 'Conversation'. Odd but
        correct: both message-report producers store `suspect_conversation_id`,
        which is what that template reads. Pinned so the mismatch in NAMES is not
        mistaken for a mismatch in behaviour."""
        from app.models import Report as ReportModel

        assert ReportModel(type=REPORT_TYPE_MESSAGE).type_text() == 'Conversation'

    def test_a_report_with_no_type_renders_nothing_rather_than_raising(self):
        """`if self.type is None: return ''` -- the include path would be
        `_report.html`, which does not exist, so this arm is what keeps a
        type-less row from being another TemplateNotFound. `type` has a default of
        0, so reaching it needs an explicit None."""
        from app.models import Report as ReportModel

        assert ReportModel(type=None).type_text() == ''
