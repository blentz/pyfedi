"""The settings page listing the apps a person has authorised, each with a button that ends its access (plan W0.2
step 7; spec docs/superpowers/specs/2026-11-07-mastodon-client-api-design.md §3.1)."""
from datetime import datetime

import pytest

from app import db
from app.models import OAuthCode, OAuthToken
from app.oauth_server import tokens
from tests.factories import make_user
from tests.test_admin_routes_entry import csrf, login

PAGE = '/user/settings/apps'


@pytest.fixture
def world(app, api_baseline):
    tusky, _secret = tokens.register_app('Tusky', ['https://tusky.example/cb'], 'read write follow push',
                                         'https://tusky.app')
    phanpy, _secret = tokens.register_app('Phanpy', ['https://phanpy.example/cb'], 'read', None)
    user = make_user(None, 'oauthsettings', local=True)
    other = make_user(None, 'oauthother', local=True)
    client = app.test_client()
    login(client, user)
    return {'tusky': tusky, 'phanpy': phanpy, 'user': user, 'other': other, 'client': client, 'flask': app}


def test_the_page_needs_a_signed_in_person(app, api_baseline):
    response = app.test_client().get(PAGE)
    assert response.status_code == 302 and '/auth/login' in response.headers['Location']


def test_the_page_lists_this_persons_apps_and_what_they_may_do(world):
    tokens.issue_token(world['tusky'], world['user'], 'read write')
    tokens.issue_token(world['tusky'], world['user'], 'read follow')
    response = world['client'].get(PAGE)
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert 'Tusky' in page and 'https://tusky.app' in page
    assert 'read' in page and 'write' in page and 'follow' in page
    assert f"/user/settings/apps/{world['tusky'].id}/revoke" in page


def test_the_page_leaves_out_others_revoked_and_app_only_tokens(world):
    tokens.issue_token(world['phanpy'], world['other'], 'read')
    revoked, _refresh = tokens.issue_token(world['tusky'], world['user'], 'read')
    tokens.revoke(tokens.token_for(revoked))
    tokens.issue_token(world['phanpy'], None, 'read', with_refresh=False)
    page = world['client'].get(PAGE).get_data(as_text=True)
    assert 'Tusky' not in page and 'Phanpy' not in page
    assert 'No apps' in page


def test_the_settings_menu_links_to_the_page(world):
    page = world['client'].get(PAGE).get_data(as_text=True)
    assert page.count(f'value="{PAGE}"') + page.count(f'href="{PAGE}"') >= 2


def test_revoking_ends_this_persons_access_for_that_app_only(world):
    mine, _refresh = tokens.issue_token(world['tusky'], world['user'], 'read')
    also_mine, _refresh = tokens.issue_token(world['tusky'], world['user'], 'write')
    other_app, _refresh = tokens.issue_token(world['phanpy'], world['user'], 'read')
    someone_elses, _refresh = tokens.issue_token(world['tusky'], world['other'], 'read')
    tokens.issue_code(world['tusky'], world['user'], 'https://tusky.example/cb', 'read', None, None)
    tokens.issue_code(world['tusky'], world['other'], 'https://tusky.example/cb', 'read', None, None)
    token = csrf(world['flask'], world['client'])
    response = world['client'].post(f"/user/settings/apps/{world['tusky'].id}/revoke", data={'csrf_token': token})
    assert response.status_code == 302 and response.headers['Location'].endswith(PAGE)
    assert tokens.token_for(mine) is None and tokens.token_for(also_mine) is None
    assert tokens.token_for(other_app) is not None and tokens.token_for(someone_elses) is not None
    assert OAuthCode.query.filter_by(user_id=world['user'].id).count() == 0
    assert OAuthCode.query.filter_by(user_id=world['other'].id).count() == 1


def test_revoking_needs_the_csrf_token(world):
    mine, _refresh = tokens.issue_token(world['tusky'], world['user'], 'read')
    response = world['client'].post(f"/user/settings/apps/{world['tusky'].id}/revoke")
    assert response.status_code == 400
    assert tokens.token_for(mine) is not None


def test_revoking_an_app_with_no_access_changes_nothing(world):
    token = csrf(world['flask'], world['client'])
    response = world['client'].post(f"/user/settings/apps/{world['phanpy'].id}/revoke", data={'csrf_token': token})
    assert response.status_code == 302
    assert OAuthToken.query.count() == 0


def test_an_app_shows_when_any_of_its_tokens_was_last_used(world):
    first, _refresh = tokens.issue_token(world['tusky'], world['user'], 'read')
    second, _refresh = tokens.issue_token(world['tusky'], world['user'], 'write')
    third, _refresh = tokens.issue_token(world['tusky'], world['user'], 'follow')
    tokens.token_for(first).last_used_at = datetime(2026, 3, 4)
    tokens.token_for(second).last_used_at = datetime(2026, 5, 6)
    tokens.token_for(third).last_used_at = datetime(2026, 4, 5)
    db.session.commit()
    [entry] = tokens.authorised_apps(world['user'])
    assert entry['last_used'] == datetime(2026, 5, 6)
    assert entry['scopes'] == {'read', 'write', 'follow'}
    assert '6 May 2026' in world['client'].get(PAGE).get_data(as_text=True)
