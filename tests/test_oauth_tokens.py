"""OAuth 2 apps, codes and tokens: stored hashed, found by their raw value, revocable (plan W0.2; spec
docs/superpowers/specs/2026-11-07-mastodon-client-api-design.md §3.1, §5)."""
from datetime import timedelta
from hashlib import sha256

import pytest
from sqlalchemy import text

from app import db
from app.models import OAuthApp, OAuthCode, OAuthToken, utcnow
from app.oauth_scopes import covers, parse_scope
from app.oauth_server import tokens
from tests.factories import make_user


@pytest.fixture
def world(app, api_baseline):
    return {'user': make_user(None, 'oauthuser', local=True)}


def _hash(value):
    return sha256(value.encode()).hexdigest()


def test_an_app_keeps_only_a_hash_of_its_secret(world):
    registered, secret = tokens.register_app('Tusky', ['oauth2redirect://com.keylesspalace.tusky/'],
                                             'read write follow push', 'https://tusky.app')
    assert registered.client_secret_hash == _hash(secret)
    assert secret not in (registered.client_secret_hash, registered.client_id)
    assert registered.check_client_secret(secret)
    assert not registered.check_client_secret(secret + 'x')
    assert not registered.check_client_secret('')
    assert registered.redirect_uri_list() == ['oauth2redirect://com.keylesspalace.tusky/']
    assert registered.get_client_id() == registered.client_id
    assert registered.scope == 'read write follow push'


def test_two_apps_get_different_ids_and_secrets(world):
    first, first_secret = tokens.register_app('A', ['https://a.example/cb'], 'read', None)
    second, second_secret = tokens.register_app('B', ['https://b.example/cb'], 'read', None)
    assert first.client_id != second.client_id and first_secret != second_secret


@pytest.mark.parametrize('scope', ['read', 'read write follow push', 'read:statuses write:media profile',
                                   'read  write'])
def test_known_scopes_are_accepted(scope):
    assert parse_scope(scope) == ' '.join(scope.split())


def test_no_scope_means_read():
    assert parse_scope('') == 'read'
    assert parse_scope(None) == 'read'


@pytest.mark.parametrize('scope', ['admin:everything', 'read:everything', 'delete', 'read,write'])
def test_unknown_scopes_are_refused(scope):
    with pytest.raises(ValueError):
        parse_scope(scope)


def test_a_token_is_found_by_its_raw_value_only(world):
    registered, _secret = tokens.register_app('A', ['https://a.example/cb'], 'read write', None)
    access, refresh = tokens.issue_token(registered, world['user'], 'read')
    stored = OAuthToken.query.one()
    assert stored.access_token_hash == _hash(access) and stored.refresh_token_hash == _hash(refresh)
    assert tokens.token_for(access) is stored
    assert tokens.token_for(refresh) is None
    assert tokens.token_for(access + 'x') is None
    assert tokens.token_for('') is None
    assert tokens.refresh_token_for(refresh) is stored
    assert tokens.refresh_token_for(access) is None


def test_an_app_token_has_no_user_and_no_refresh(world):
    registered, _secret = tokens.register_app('A', ['https://a.example/cb'], 'read', None)
    access, refresh = tokens.issue_token(registered, None, 'read', with_refresh=False)
    assert refresh is None
    stored = tokens.token_for(access)
    assert stored.user_id is None and stored.refresh_token_hash is None


def test_a_revoked_token_is_not_found(world):
    registered, _secret = tokens.register_app('A', ['https://a.example/cb'], 'read', None)
    access, refresh = tokens.issue_token(registered, world['user'], 'read')
    tokens.revoke(tokens.token_for(access))
    assert tokens.token_for(access) is None
    assert tokens.refresh_token_for(refresh) is None
    assert OAuthToken.query.one().revoked_at is not None


def test_a_code_is_found_once_by_its_raw_value(world):
    registered, _secret = tokens.register_app('A', ['https://a.example/cb'], 'read', None)
    raw = tokens.issue_code(registered, world['user'], 'https://a.example/cb', 'read', 'challenge', 'S256')
    stored = OAuthCode.query.one()
    assert stored.code_hash == _hash(raw) and raw != stored.code_hash
    assert tokens.code_for(registered, raw) is stored
    assert tokens.code_for(registered, raw + 'x') is None
    other, _secret = tokens.register_app('B', ['https://b.example/cb'], 'read', None)
    assert tokens.code_for(other, raw) is None


def test_an_expired_code_is_not_found(world, monkeypatch):
    registered, _secret = tokens.register_app('A', ['https://a.example/cb'], 'read', None)
    raw = tokens.issue_code(registered, world['user'], 'https://a.example/cb', 'read', None, None)
    monkeypatch.setattr(tokens, 'CODE_LIFETIME_SECONDS', -1)
    assert tokens.code_for(registered, raw) is None


def test_deleting_an_app_deletes_its_codes_and_tokens(world):
    registered, _secret = tokens.register_app('A', ['https://a.example/cb'], 'read', None)
    tokens.issue_code(registered, world['user'], 'https://a.example/cb', 'read', None, None)
    tokens.issue_token(registered, world['user'], 'read')
    db.session.execute(text('DELETE FROM oauth_app WHERE id = :id'), {'id': registered.id})
    db.session.commit()
    assert db.session.execute(text('SELECT count(*) FROM oauth_token')).scalar() == 0
    assert db.session.execute(text('SELECT count(*) FROM oauth_code')).scalar() == 0


def test_deleting_a_user_deletes_their_codes_and_tokens(world):
    registered, _secret = tokens.register_app('A', ['https://a.example/cb'], 'read', None)
    tokens.issue_code(registered, world['user'], 'https://a.example/cb', 'read', None, None)
    tokens.issue_token(registered, world['user'], 'read')
    user_id = world['user'].id
    db.session.expunge_all()
    db.session.execute(text('DELETE FROM "user" WHERE id = :id'), {'id': user_id})
    db.session.commit()
    assert db.session.execute(text('SELECT count(*) FROM oauth_token')).scalar() == 0
    assert db.session.execute(text('SELECT count(*) FROM oauth_code')).scalar() == 0


@pytest.mark.parametrize('granted, scope, included', [
    ('read', 'read', True), ('read', 'read:statuses', True), ('read', 'write', False),
    ('read', 'write:statuses', False), ('read:statuses', 'read', False), ('read:statuses', 'read:accounts', False),
    ('write', 'write:media', True), ('follow', 'write:follows', True), ('follow', 'read:mutes', True),
    ('follow', 'write:statuses', False), ('push', 'read', False), ('read', 'readx:y', False),
])
def test_a_scope_includes_its_parts(granted, scope, included):
    assert covers(granted, scope) is included


def test_nothing_is_found_for_an_empty_code_or_refresh_token(world):
    registered, _secret = tokens.register_app('A', ['https://a.example/cb'], 'read', None)
    tokens.issue_code(registered, world['user'], 'https://a.example/cb', 'read', None, None)
    tokens.issue_token(registered, world['user'], 'read')
    assert tokens.code_for(registered, '') is None
    assert tokens.refresh_token_for('') is None
    assert tokens.refresh_token_for(None) is None


def test_a_token_answers_what_authlib_asks_of_it(world):
    registered, _secret = tokens.register_app('A', ['https://a.example/cb'], 'read', None)
    other, _other_secret = tokens.register_app('B', ['https://b.example/cb'], 'read', None)
    access, _refresh = tokens.issue_token(registered, world['user'], 'read write')
    token = tokens.token_for(access)
    assert token.check_client(registered) and not token.check_client(other) and not token.check_client(None)
    assert token.get_scope() == 'read write' and token.get_expires_in() == 0 and not token.is_expired()
    assert token.get_user() == world['user'] and token.get_client() == registered and not token.is_revoked()
    tokens.revoke(token)
    assert token.is_revoked()
    app_access, _none = tokens.issue_token(registered, None, 'read', with_refresh=False)
    assert tokens.token_for(app_access).get_user() is None


def test_an_apps_own_token_has_nobody_to_check(world):
    registered, _secret = tokens.register_app('A', ['https://a.example/cb'], 'read', None)
    access, _none = tokens.issue_token(registered, None, 'read', with_refresh=False)
    assert tokens.usable_token(tokens.token_for(access))


def test_a_token_made_before_a_password_change_stops_acting(world):
    registered, _secret = tokens.register_app('A', ['https://a.example/cb'], 'read', None)
    access, _refresh = tokens.issue_token(registered, world['user'], 'read')
    token = tokens.token_for(access)
    assert tokens.usable_token(token)
    world['user'].password_updated_at = utcnow() + timedelta(seconds=5)
    db.session.commit()
    assert not tokens.usable_token(token)
    world['user'].password_updated_at = None
    db.session.commit()
    assert tokens.usable_token(token)


@pytest.mark.parametrize('change', ['remote', 'unverified', 'banned', 'deleted', 'lapsed', 'gone'])
def test_a_token_stops_acting_for_a_person_who_can_no_longer_sign_in(world, change, monkeypatch):
    registered, _secret = tokens.register_app('A', ['https://a.example/cb'], 'read', None)
    access, _refresh = tokens.issue_token(registered, world['user'], 'read')
    token = tokens.token_for(access)
    user = world['user']
    if change == 'remote':
        user.ap_id = 'https://elsewhere.example/u/oauthuser'
    elif change == 'unverified':
        user.verified = False
    elif change == 'lapsed':
        monkeypatch.setattr(type(user), 'access_has_lapsed', lambda self: True)
    elif change == 'gone':
        assert not tokens.usable_user(None)
        return
    else:
        setattr(user, change, True)
    db.session.commit()
    assert not tokens.usable_token(token)


def test_a_token_is_claimed_once(world):
    registered, _secret = tokens.register_app('A', ['https://a.example/cb'], 'read', None)
    access, _refresh = tokens.issue_token(registered, world['user'], 'read')
    token = tokens.token_for(access)
    assert tokens.claim(token) is True
    assert tokens.claim(token) is False
    assert tokens.token_for(access) is None
