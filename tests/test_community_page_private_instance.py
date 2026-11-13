"""The community page keeps its own guards (security review, 2026-11-13): a helper inserted between show_community and
its decorators took @login_required_if_private_instance and @check_anoobis off the page, so on a private instance a
signed-out visitor read every community's page. A private instance sends them to the login page."""
import pytest

from app import db
from tests.factories import make_community, make_instance, make_user


def _community(name):
    make_user(make_instance('test.piefed.local', software='piefed'), 'guardowner', local=True)
    make_community(name)


def test_a_private_instance_sends_a_signed_out_reader_of_a_community_to_log_in(app, db_session, site):
    site.private_instance = True
    _community('guardedcomm')
    db.session.commit()
    response = app.test_client().get('/c/guardedcomm')
    assert response.status_code == 302 and '/auth/login' in response.headers['Location']


def test_an_open_instance_shows_a_signed_out_reader_the_community(app, db_session, site):
    site.private_instance = False
    _community('opencomm')
    db.session.commit()
    assert app.test_client().get('/c/opencomm').status_code == 200
