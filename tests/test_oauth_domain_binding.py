"""An OAuth code or token works only on the domain it was issued on (owner ruling 2026-11-08; plan W0.2): a token an
app got from one storefront is no credential on another, so neither a sister domain nor the onion can be reached with,
or linked to, a login made elsewhere. Codes are exchanged, tokens refreshed and revoked, and tokens used, only there."""
import pytest

from app import db
from app.models import OAuthToken, SiteDomain
from app.oauth_server import tokens
from app.storefront import clear_domain_cache, domain_for_host
from tests.factories import make_user
from tests.two_domains import FIRST, request_on

SHOP = 'shop.example'
CALLBACK = 'https://app.example/cb'
VERIFY = '/api/v1/accounts/verify_credentials'


@pytest.fixture
def world(app, api_baseline):
    if db.session.query(SiteDomain).filter_by(is_default=True).first() is None:
        db.session.add(SiteDomain(domain=FIRST, scheme='https', is_default=True))
    db.session.add(SiteDomain(domain=SHOP, scheme='https'))
    db.session.commit()
    clear_domain_cache()
    registered, secret = tokens.register_app('Tusky', [CALLBACK], 'read write follow', None)
    user = make_user(None, 'boundtoken', local=True)
    return {'app': registered, 'secret': secret, 'user': user, 'flask': app}


def _on(world, host):
    return request_on(world['flask'], domain_for_host(host))


def _token(world, host):
    with _on(world, host):
        return tokens.issue_token(world['app'], world['user'], 'read write')


def _verify(world, access, host):
    return world['flask'].test_client().get(VERIFY, headers={'Authorization': f'Bearer {access}'},
                                            base_url=f'https://{host}')


def _post(world, path, host, **data):
    return world['flask'].test_client().post(path, data=data, base_url=f'https://{host}')


@pytest.mark.parametrize('issued, used', [(FIRST, SHOP), (SHOP, FIRST)], ids=['first-on-shop', 'shop-on-first'])
def test_a_token_is_refused_on_another_domain(world, issued, used):
    access, _refresh = _token(world, issued)
    assert _verify(world, access, issued).status_code == 200
    assert _verify(world, access, used).status_code == 401


def test_a_token_records_where_it_was_issued(world):
    _token(world, SHOP)
    assert OAuthToken.query.one().domain == SHOP


def test_a_code_is_exchanged_only_where_it_was_given(world):
    with _on(world, FIRST):
        code = tokens.issue_code(world['app'], world['user'], CALLBACK, 'read', None, None)
    fields = {'grant_type': 'authorization_code', 'code': code, 'redirect_uri': CALLBACK,
              'client_id': world['app'].client_id, 'client_secret': world['secret']}
    refused = _post(world, '/oauth/token', SHOP, **fields)
    assert refused.status_code == 400 and refused.get_json()['error'] == 'invalid_grant'
    granted = _post(world, '/oauth/token', FIRST, **fields)
    assert granted.status_code == 200, granted.get_data(as_text=True)
    assert _verify(world, granted.get_json()['access_token'], FIRST).status_code == 200


def test_a_refresh_works_only_where_the_token_was_issued(world):
    _access, refresh = _token(world, FIRST)
    fields = {'grant_type': 'refresh_token', 'refresh_token': refresh, 'client_id': world['app'].client_id,
              'client_secret': world['secret']}
    refused = _post(world, '/oauth/token', SHOP, **fields)
    assert refused.status_code == 400 and refused.get_json()['error'] == 'invalid_grant'
    renewed = _post(world, '/oauth/token', FIRST, **fields)
    assert renewed.status_code == 200, renewed.get_data(as_text=True)
    new_access = renewed.get_json()['access_token']
    assert _verify(world, new_access, FIRST).status_code == 200
    assert _verify(world, new_access, SHOP).status_code == 401


def test_a_token_is_not_revoked_from_another_domain(world):
    access, _refresh = _token(world, FIRST)
    fields = {'token': access, 'client_id': world['app'].client_id, 'client_secret': world['secret']}
    assert _post(world, '/oauth/revoke', SHOP, **fields).status_code == 200   # RFC 7009: unknown tokens too
    assert _verify(world, access, FIRST).status_code == 200
    assert _post(world, '/oauth/revoke', FIRST, **fields).status_code == 200
    assert _verify(world, access, FIRST).status_code == 401
