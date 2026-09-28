"""`admin_reports`: the queue of open reports, and its filters.

17 statements with no test at all, behind
`permission_required('administer all users')`.

D1384. `search` was read from the query string and handed to the template, which
renders

    <input type="search" name="search" placeholder="Query" value="{{ search }}">

and no filter ever used it. An admin typed a term, the page reloaded with the box
still filled, and the list was unchanged. Six sibling listings in the same file
implement the same parameter with `ilike` -- `admin_communities` four times,
`admin_users`, `admin_instances` -- so this one looked as though it did too.

It searches `reasons` and `description`, the report's own text, which is what the
box sits above. The suspect's name is deliberately not searched: it lives on five
different relationships depending on `Report.type`, and joining all of them is a
separate feature rather than this control's missing half. That decision is pinned
below so a later round does not read the narrower scope as an oversight.

WHAT ELSE THIS FILE PINS. The queue shows only `REPORT_STATE_NEW` and
`REPORT_STATE_ESCALATED` -- a resolved report is not an open one -- the
`local_remote` split, and the `report_types` multi-select whose `[-1]` sentinel
means "all". That sentinel is the reason `len(report_types) > 0 and -1 not in
report_types` guards the filter, and both halves are exercised.
"""
import pytest
from flask import g

from app import db
from app.constants import (REPORT_STATE_APPEALED, REPORT_STATE_DISCARDED,
                           REPORT_STATE_ESCALATED, REPORT_STATE_NEW,
                           REPORT_STATE_RESOLVED)
from app.models import Report, Site
from tests.factories import make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def admin_client(app, db_session):
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    local = make_instance('test.piefed.local')
    admin = make_user(local, 'theadmin', local=True)
    assert admin.id == 1  # fact 347
    remote = make_instance('peer.example')
    db.session.commit()
    g.admin_ids = [admin.id]
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(admin.id)
        sess['_fresh'] = True
    return client, local, remote


def a_report(reasons='spam', description='DESCRIPTIONTEXT', status=REPORT_STATE_NEW,
             type=1, source_instance_id=1):
    report = Report(reasons=reasons, description=description, status=status,
                    type=type, source_instance_id=source_instance_id,
                    reporter_id=1)
    db.session.add(report)
    db.session.commit()
    return report


def page(client, **params):
    return client.get('/admin/reports', query_string=params)


# --------------------------------------------------------------------------
# D1384: the search box that did nothing
# --------------------------------------------------------------------------


class TestTheSearchBox:
    """NEVER assert that the search TERM appears in the response: the template
    echoes it into `value="{{ search }}"`, so `b'TERM' in body` is true whether or
    not a row matched. Two mutants -- "search only covers reasons" and "search
    becomes a prefix match" -- survived on exactly that. Each row below searches
    one field and witnesses the match through the OTHER field of the same report.
    """

    def test_it_narrows_by_reason(self, app, admin_client):
        client = admin_client[0]
        a_report(reasons='UNIQUEREASON', description='WITNESSONE')
        a_report(reasons='something else', description='WITNESSTWO')

        body = page(client, search='UNIQUEREASON').data

        assert b'WITNESSONE' in body          # the matching report is listed
        assert b'WITNESSTWO' not in body      # the other one is not

    def test_it_narrows_by_description(self, app, admin_client):
        client = admin_client[0]
        a_report(reasons='WITNESSTHREE', description='UNIQUEDESCRIPTION')
        a_report(reasons='WITNESSFOUR', description='OTHERDESCRIPTION')

        body = page(client, search='UNIQUEDESCRIPTION').data

        assert b'WITNESSTHREE' in body
        assert b'WITNESSFOUR' not in body

    def test_it_is_a_substring_match(self, app, admin_client):
        """`ilike('%term%')`, as the six sibling listings use -- so a term in the
        MIDDLE of the text matches. Witnessed through the description, because
        the term itself is echoed into the form either way."""
        client = admin_client[0]
        a_report(reasons='contains MIDDLEWORD inside', description='WITNESSFIVE')

        assert b'WITNESSFIVE' in page(client, search='MIDDLEWORD').data

    def test_it_ignores_case(self, app, admin_client):
        client = admin_client[0]
        a_report(reasons='MixedCaseReason', description='WITNESSSIX')

        assert b'WITNESSSIX' in page(client, search='mixedcasereason').data

    def test_a_term_matching_nothing_empties_the_list(self, app, admin_client):
        client = admin_client[0]
        a_report(reasons='UNIQUEREASON', description='x')

        assert b'UNIQUEREASON' not in page(client, search='nomatchatall').data

    def test_no_term_shows_everything(self, app, admin_client):
        """`if search:` -- an absent or empty box must not narrow anything, which
        is the state the page is normally in."""
        client = admin_client[0]
        a_report(reasons='UNIQUEREASON', description='WITNESSSEVEN')

        assert b'WITNESSSEVEN' in page(client).data
        assert b'WITNESSSEVEN' in page(client, search='').data

    def test_a_report_with_no_text_is_still_listed_when_not_searching(
            self, app, admin_client):
        """What distinguishes `if search:` from running the filter always:
        `ilike('%%')` matches any string but NOT NULL, so a report whose
        `reasons` and `description` are both null would vanish from the unfiltered
        queue. Federated reports arrive with either field unset -- `Report.new`
        stores what the peer sent -- so this is a row the product really has.
        """
        client = admin_client[0]
        # type=0 ('User'), a real value. An out-of-range `type` is a separate
        # matter: `Report.type_text` is `types[self.type]` with no bounds check,
        # and the template builds an include path from it, so one such row would
        # 500 the whole queue -- but every writer passes a REPORT_TYPE_*
        # constant, so nothing here can produce one, and this file does not
        # pretend otherwise by testing it.
        a_report(reasons=None, description=None, type=0)
        db.session.commit()

        # Witnessed structurally, because the report has no text of its own to
        # search for and the table has no marker of its own: the unfiltered queue
        # must contain one more row than a queue filtered to nothing. `<tr` is
        # emitted by the per-type include, one per listed report.
        assert Report.query.count() == 1

        unfiltered = page(client).data.count(b'<tr')
        filtered = page(client, search='nomatchatall').data.count(b'<tr')

        assert unfiltered == filtered + 1, (unfiltered, filtered)

    def test_the_term_is_echoed_back_into_the_box(self, app, admin_client):
        """It always did this, which is what made the missing filter look like a
        working feature."""
        client = admin_client[0]

        assert b'value="ECHOED"' in page(client, search='ECHOED').data

    def test_a_term_with_a_wildcard_is_not_a_wildcard(self, app, admin_client):
        """`%` is `ilike`'s own wildcard, so a term of `%` would match every row
        if it were interpolated without care. Asserted as behaviour rather than
        as a security claim -- this is an admin-only page -- so a later round
        does not have to rediscover what it does."""
        client = admin_client[0]
        a_report(reasons='ordinary', description='x')

        body = page(client, search='%').data

        assert b'ordinary' in body       # today: the wildcard matches

    @pytest.mark.parametrize('term', ["'", '"', '\\', '_', 'a%b'])
    def test_an_awkward_term_does_not_break_the_page(self, app, admin_client,
                                                    term):
        client = admin_client[0]
        a_report(reasons='ordinary', description='x')

        assert page(client, search=term).status_code == 200

    def test_the_suspects_name_is_not_searched(self, app, admin_client):
        """Pinned as a deliberate limit. The suspect hangs off one of five
        relationships depending on `Report.type`, and joining them all is a
        separate feature -- not something a later reader should mistake for this
        filter being half-written."""
        client, local, remote = admin_client
        suspect = make_user(local, 'SUSPECTNAME', local=True)
        db.session.commit()
        # A distinctive witness: 'spam' also appears in the page's own type
        # dropdown, so asserting its absence tested the chrome (fact 830).
        report = a_report(reasons='SUSPECTWITNESS', description='x')
        report.suspect_user_id = suspect.id
        db.session.commit()

        body = page(client, search='SUSPECTNAME').data
        assert b'SUSPECTWITNESS' not in body
        # ... and the report IS findable by its own text, so the row above is
        # about the scope of the filter rather than the filter being broken.
        assert b'SUSPECTWITNESS' in page(client, search='SUSPECTWITNESS').data


# --------------------------------------------------------------------------
# The queue itself
# --------------------------------------------------------------------------


class TestWhichReportsAreOpen:
    @pytest.mark.parametrize('status', [REPORT_STATE_NEW, REPORT_STATE_ESCALATED])
    def test_an_open_report_is_listed(self, app, admin_client, status):
        client = admin_client[0]
        a_report(reasons='OPENREPORT', status=status)

        assert b'OPENREPORT' in page(client).data

    @pytest.mark.parametrize('status', [REPORT_STATE_RESOLVED,
                                        REPORT_STATE_DISCARDED,
                                        REPORT_STATE_APPEALED])
    def test_a_closed_report_is_not(self, app, admin_client, status):
        client = admin_client[0]
        a_report(reasons='CLOSEDREPORT', status=status)

        assert b'CLOSEDREPORT' not in page(client).data


class TestTheLocalRemoteSplit:
    def test_local_shows_only_reports_from_this_instance(self, app, admin_client):
        client = admin_client[0]
        a_report(reasons='LOCALREPORT', source_instance_id=1)
        a_report(reasons='REMOTEREPORT', source_instance_id=2)

        body = page(client, local_remote='local').data

        assert b'LOCALREPORT' in body
        assert b'REMOTEREPORT' not in body

    def test_remote_shows_only_reports_from_elsewhere(self, app, admin_client):
        client = admin_client[0]
        a_report(reasons='LOCALREPORT', source_instance_id=1)
        a_report(reasons='REMOTEREPORT', source_instance_id=2)

        body = page(client, local_remote='remote').data

        assert b'REMOTEREPORT' in body
        assert b'LOCALREPORT' not in body

    def test_neither_shows_both(self, app, admin_client):
        client = admin_client[0]
        a_report(reasons='LOCALREPORT', source_instance_id=1)
        a_report(reasons='REMOTEREPORT', source_instance_id=2)

        body = page(client).data

        assert b'LOCALREPORT' in body and b'REMOTEREPORT' in body

    def test_an_unrecognised_value_shows_both(self, app, admin_client):
        """Two separate `if`s rather than an if/elif/else, so anything else is
        simply no filter."""
        client = admin_client[0]
        a_report(reasons='LOCALREPORT', source_instance_id=1)
        a_report(reasons='REMOTEREPORT', source_instance_id=2)

        body = page(client, local_remote='sideways').data

        assert b'LOCALREPORT' in body and b'REMOTEREPORT' in body


class TestTheTypeFilter:
    def test_one_type_excludes_the_others(self, app, admin_client):
        client = admin_client[0]
        a_report(reasons='POSTREPORT', type=1)
        a_report(reasons='REPLYREPORT', type=2)

        body = page(client, report_types=1).data

        assert b'POSTREPORT' in body
        assert b'REPLYREPORT' not in body

    def test_several_types_are_combined(self, app, admin_client):
        """`getlist(..., type=int)` -- the parameter is a multi-select."""
        client = admin_client[0]
        a_report(reasons='USERREPORT', type=0)
        a_report(reasons='POSTREPORT', type=1)
        a_report(reasons='COMMUNITYREPORT', type=3)

        body = page(client, report_types=[0, 1]).data

        assert b'USERREPORT' in body and b'POSTREPORT' in body
        assert b'COMMUNITYREPORT' not in body

    def test_the_minus_one_sentinel_means_all(self, app, admin_client):
        """`if len(report_types) == 0: report_types = [-1]`, and the filter is
        skipped when `-1` is present. Both halves of that are load-bearing: no
        selection and an explicit "all" must behave the same."""
        client = admin_client[0]
        a_report(reasons='USERREPORT', type=0)
        a_report(reasons='POSTREPORT', type=1)

        explicit = page(client, report_types=-1).data
        absent = page(client).data

        for body in (explicit, absent):
            assert b'USERREPORT' in body and b'POSTREPORT' in body

    def test_minus_one_beside_a_real_type_still_means_all(self, app, admin_client):
        """`-1 not in report_types` -- the sentinel wins over a co-selected
        type."""
        client = admin_client[0]
        a_report(reasons='USERREPORT', type=0)
        a_report(reasons='POSTREPORT', type=1)

        body = page(client, report_types=[-1, 1]).data

        assert b'USERREPORT' in body and b'POSTREPORT' in body

    def test_a_non_numeric_type_is_dropped(self, app, admin_client):
        """`getlist(type=int)` discards what it cannot convert, so the list can
        come back empty and take the sentinel path."""
        client = admin_client[0]
        a_report(reasons='POSTREPORT', type=1)

        assert page(client, report_types='abc').status_code == 200


class TestWhoMaySeeIt:
    def test_an_anonymous_visitor_may_not(self, app, admin_client):
        assert app.test_client().get('/admin/reports').status_code in (302, 401, 403)

    def test_an_ordinary_user_may_not(self, app, admin_client):
        """`permission_required('administer all users')`."""
        local = make_instance('other.piefed.local')
        ordinary = make_user(local, 'ordinaryperson', local=True)
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(ordinary.id)
            sess['_fresh'] = True

        assert client.get('/admin/reports').status_code in (302, 401, 403)
