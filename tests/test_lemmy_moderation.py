"""A moderator's and an admin's counts in Lemmy's API for Jerboa (spec 2026-11-10-lemmy-v3-design, ruling L2): zero,
so the badge Jerboa asks for on every start never errs, while moderating itself is not served (W3b)."""
import pytest

from app import cache
from tests import lemmy_app_contract as contract
from tests.factories import make_user


@pytest.fixture
def world(app, api_baseline, monkeypatch):
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    cache.clear()
    person = make_user(None, 'lemmymoderator', local=True)
    return {'client': app.test_client(), 'headers': {'Authorization': f'Bearer {person.encode_jwt_token()}'}}


@pytest.mark.parametrize('path,model', [('/api/v3/user/report_count', 'GetReportCountResponse'),
                                        ('/api/v3/admin/registration_application/count',
                                         'GetUnreadRegistrationApplicationCountResponse')])
def test_the_counts_are_zero_and_need_a_login(world, path, model):
    response = world['client'].get(path, headers=world['headers'])
    body = response.get_json()
    assert response.status_code == 200 and contract.problems(body, model) == []
    assert not any(value for key, value in body.items() if key != 'community_id')
    refused = world['client'].get(path)
    assert refused.status_code == 400 and refused.get_json() == {'error': 'incorrect_login'}


@pytest.mark.parametrize('path', ['/api/v3/post/report/list', '/api/v3/post/lock', '/api/v3/community/ban_user'])
def test_moderating_is_not_served(world, path):
    response = world['client'].post(path, json={}, headers=world['headers'])
    assert response.status_code == 404 and response.get_json() == {'error': 'not_found'}


def test_a_token_is_validated(world):
    response = world['client'].get('/api/v3/user/validate_auth', headers=world['headers'])
    assert response.status_code == 200 and response.get_json() == {'success': True}
    refused = world['client'].get('/api/v3/user/validate_auth', headers={'Authorization': 'Bearer nonsense'})
    assert refused.status_code == 400 and refused.get_json() == {'error': 'incorrect_login'}
