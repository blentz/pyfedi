"""A web form posted a JSON body that is not an object (fuzz-routes finding, 2026-11-13): Flask-WTF built the form's
data from it with ImmutableMultiDict, and a number or a list there raised TypeError, a 500 from every form of the site.
Such a body is a malformed submission: a 400. A JSON object still fills the form, as Flask-WTF has it fill one."""
import pytest
from flask import request

from app import db
from app.auth.forms import LoginForm
from tests.factories import make_instance, make_user
from tests.test_admin_site_profile import csrf, login

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture(autouse=True)
def csrf_on(app, monkeypatch):
    """Production's forms: the suite's config turns their CSRF field off, which the login page renders."""
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)


@pytest.mark.parametrize('body', ['5', '[1, 2]', '"text"', 'null', 'true', '{not json'],
                         ids=['number', 'list', 'string', 'null', 'bool', 'unreadable'])
def test_a_form_posted_json_that_is_not_an_object_is_a_400(app, body):
    response = app.test_client().post('/auth/login', data=body, content_type='application/json')
    assert response.status_code == 400


def test_a_form_posted_a_json_object_is_still_read(app):
    response = app.test_client().post('/auth/login', json={'user_name': 'nobody-here', 'password': 'x'})
    assert response.status_code == 200 and 'nobody-here' in response.get_data(as_text=True)


# A JSON object's values as a form posts them (fuzz-routes finding, 2026-11-13: POST /user/files/upload with
# {"urls": null} raised AttributeError, form.urls.data being None): text, as no browser sends anything else

def test_a_json_object_fills_a_form_with_text_only(app):
    with app.test_request_context('/auth/login', method='POST',
                                  json={'user_name': None, 'password': 12345678, 'low_bandwidth_mode': [True, None]}):
        form = LoginForm()
        assert form.user_name.data == '' and form.password.data == '12345678'
        assert request.method == 'POST'


def test_a_form_reading_a_null_field_is_no_500(app, db_session):
    user = make_user(make_instance('test.piefed.local', software='piefed'), 'jsonuploader', local=True)
    user.verified = True
    db.session.commit()
    client = app.test_client()
    login(client, user)
    token = csrf(app, client)
    response = client.post('/user/files/upload', json={'csrf_token': token, 'urls': None})
    assert response.status_code < 500


def test_an_upload_posted_without_its_urls_field_is_no_500(app, db_session):
    user = make_user(make_instance('test.piefed.local', software='piefed'), 'formuploader', local=True)
    user.verified = True
    db.session.commit()
    client = app.test_client()
    login(client, user)
    token = csrf(app, client)
    response = client.post('/user/files/upload', data={'csrf_token': token}, content_type='multipart/form-data')
    assert response.status_code < 500
