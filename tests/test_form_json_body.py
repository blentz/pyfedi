"""A web form posted a JSON body that is not an object (fuzz-routes finding, 2026-11-13): Flask-WTF built the form's
data from it with ImmutableMultiDict, and a number or a list there raised TypeError, a 500 from every form of the site.
Such a body is a malformed submission: a 400. A JSON object still fills the form, as Flask-WTF has it fill one."""
import pytest

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
