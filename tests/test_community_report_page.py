"""The community report page (fuzz-routes finding, 2026-11-13): community_report rendered
community/community_report.html, a template PieFed never had, so the page itself and every submission that failed
validation was TemplateNotFound, a 500. It renders as its siblings, the post and user report pages, do."""
import pytest

from app import db
from tests.factories import make_community, make_instance, make_user
from tests.test_admin_site_profile import csrf, login

pytestmark = pytest.mark.usefixtures('site')


def _signed_in(app):
    user = make_user(make_instance('test.piefed.local', software='piefed'), 'commreporter', local=True)
    user.verified = True
    community = make_community('reportedcomm')
    db.session.commit()
    client = app.test_client()
    login(client, user)
    return client, community


def test_the_report_page_renders(app, db_session):
    client, community = _signed_in(app)
    response = client.get(f'/community/community/{community.id}/report')
    assert response.status_code == 200 and 'reportedcomm' in response.get_data(as_text=True)


def test_a_report_that_fails_validation_renders_the_page_again(app, db_session):
    client, community = _signed_in(app)
    token = csrf(app, client)
    response = client.post(f'/community/community/{community.id}/report',
                           data={'csrf_token': token, 'description': 'x' * 300})
    assert response.status_code == 200


# The page names the community, so it shows only a community its reader may see, as the community's own page does
# (security review of the fix above): a banned community is no page, a private one only its members'.

def test_a_banned_community_has_no_report_page(app, db_session):
    client, community = _signed_in(app)
    community.banned = True
    db.session.commit()
    response = client.get(f'/community/community/{community.id}/report')
    assert response.status_code == 404 and 'reportedcomm' not in response.get_data(as_text=True)


def test_a_private_community_has_no_report_page_for_a_non_member(app, db_session):
    client, community = _signed_in(app)
    community.private = True
    db.session.commit()
    response = client.get(f'/community/community/{community.id}/report')
    assert response.status_code == 403 and 'reportedcomm' not in response.get_data(as_text=True)
