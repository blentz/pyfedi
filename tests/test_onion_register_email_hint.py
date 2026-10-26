"""The onion's sign-up page recommends a privacy-preserving email (onion unlinkability spec, goal 8). The sentence is
plain text with no link, and the first domain's page does not carry it."""
import pytest

from app import db
from app.models import Site
from tests.test_onion_guard_limits import a_session, onion, onion_get  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')

FORM_TEXT = 'Create new account'
HINT = 'We recommend registering with ProtonMail or another privacy-preserving email service.'


@pytest.fixture
def open_registration(app, site, onion, monkeypatch):  # noqa: F811
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # the open form renders a csrf_token field
    monkeypatch.setattr('app.auth.routes.handle_abandoned_open_instance', lambda: None)   # no admin has logged in here
    db.session.get(Site, 1).registration_mode = 'Open'
    db.session.commit()


def test_onion_register_page_recommends_a_private_email(app, open_registration):
    client = app.test_client()
    a_session(client)
    page = onion_get(client, '/auth/register').get_data(as_text=True)
    assert FORM_TEXT in page
    assert HINT in page


def test_first_domain_register_page_has_no_recommendation(app, open_registration):
    page = app.test_client().get('/auth/register').get_data(as_text=True)
    assert FORM_TEXT in page        # the form is there, so the missing sentence is not a missing page
    assert HINT not in page
