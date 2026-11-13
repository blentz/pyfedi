"""The alpha API's placeholders for Lemmy V3 paths it does not implement, and user/mention, renamed (ruling FR4,
2026-11-13): a 400 in alpha's own error format, {code, message, status}, keeping the `error` they always had, so an app
reading either still works. Found by the route fuzzing (FR1: a 4xx not in the API's format)."""
import pytest

pytestmark = pytest.mark.usefixtures('site')

PLACEHOLDERS = [('POST', '/api/alpha/site'), ('PUT', '/api/alpha/site'), ('PUT', '/api/alpha/community/hide'),
                ('POST', '/api/alpha/community/remove'), ('POST', '/api/alpha/community/transfer'),
                ('POST', '/api/alpha/user/delete_account'), ('GET', '/api/alpha/user/validate_auth'),
                ('POST', '/api/alpha/admin/add'), ('POST', '/api/alpha/admin/purge/post'),
                ('PUT', '/api/alpha/custom_emoji'), ('POST', '/api/alpha/custom_emoji/delete')]


@pytest.mark.parametrize('method, path', PLACEHOLDERS, ids=[f'{m} {p}' for m, p in PLACEHOLDERS])
def test_a_placeholder_answers_in_alphas_format_and_keeps_error(app, method, path):
    response = app.test_client().open(path, method=method)
    body = response.get_json()
    assert response.status_code == 400
    assert body == {'code': 400, 'message': 'not_yet_implemented', 'status': 'Bad Request',
                    'error': 'not_yet_implemented'}


def test_the_renamed_mention_path_says_so_in_alphas_format(app):
    body = app.test_client().get('/api/alpha/user/mention').get_json()
    assert body == {'code': 400, 'message': 'renamed to user/mentions', 'status': 'Bad Request',
                    'error': 'renamed to user/mentions'}
