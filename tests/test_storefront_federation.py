"""Federation identity stays canonical on every storefront, and a link to any registered domain is local
(onion storefront spec §3.4, Task 4a).

The site answers on test.piefed.local (SERVER_NAME, the default row) and on abc.onion (an alias). Anything
federation reads -- actor documents, WebFinger -- names test.piefed.local whatever Host it was asked on. Anything
that decides "is this ours" accepts both.
"""
import json
from unittest.mock import patch

import pytest

import app.storefront as storefront
from app import db
from app.activitypub import signature_rfc9421
from app.activitypub.signature import HttpSignature
from app.api.alpha.utils.misc import get_resolve_object
from app.community.live import server_view_instance
from app.constants import SRC_API
from app.models import Domain, SiteDomain, UserFollower
from app.shared.domain import block_domain
from app.storefront import clear_domain_cache
from app.utils import display_back_button, is_local_image_url, is_safe_redirect_target
from tests.factories import cavage_signed_headers, inbox_activity, make_instance, make_site, make_user
from tests.test_api_search_resolve import env, token  # noqa: F401 -- env is a fixture

ALIAS = 'abc.onion'
CANONICAL = 'https://test.piefed.local'


def _register_domains():
    db.session.query(SiteDomain).delete()   # the migration's seed row survives until the first truncate
    db.session.add(SiteDomain(domain='test.piefed.local', scheme='https', is_default=True))
    db.session.add(SiteDomain(domain=ALIAS, scheme='http'))
    db.session.commit()
    clear_domain_cache()


@pytest.fixture
def onion(db_session):
    _register_domains()
    yield ALIAS
    clear_domain_cache()


def _local_user(name='alice', manually_approves=False):
    user = make_user(make_instance('local.example'), name, local=True, with_keys=True)
    user.ap_profile_id = f'{CANONICAL}/u/{name}'
    user.ap_manually_approves_followers = manually_approves
    db.session.commit()
    return user


def _strings(document):
    return json.dumps(document)


# --------------------------------------------------------------------------
# Identity on the alias is canonical
# --------------------------------------------------------------------------


def test_actor_json_on_alias_is_canonical(app, onion):
    make_site()
    _local_user('alice')
    response = app.test_client().get('/u/alice', headers={'Host': ALIAS, 'Accept': 'application/activity+json'})
    assert response.status_code == 200
    actor = response.get_json()
    for value in (actor['id'], actor['inbox'], actor['outbox'], actor['publicKey']['id']):
        assert value.startswith(f'{CANONICAL}/')
    assert ALIAS not in _strings(actor)
    assert ALIAS not in response.headers.get('Link', '')


@pytest.mark.parametrize('resource', ['acct:alice@test.piefed.local', 'acct:alice@abc.onion',
                                      'http://abc.onion/u/alice'])
def test_webfinger_on_alias_is_canonical(app, onion, resource):
    make_site()
    _local_user('alice')
    response = app.test_client().get(f'/.well-known/webfinger?resource={resource}', headers={'Host': ALIAS})
    assert response.status_code == 200
    answer = response.get_json()
    assert answer['subject'] == 'acct:alice@test.piefed.local'
    assert all(link['href'].startswith(f'{CANONICAL}/') for link in answer['links'] if 'href' in link)
    assert ALIAS not in _strings(answer)


def test_webfinger_for_an_unregistered_domain_is_404(app, onion):
    make_site()
    _local_user('alice')
    response = app.test_client().get('/.well-known/webfinger?resource=acct:alice@evil.onion')
    assert response.status_code == 404


# --------------------------------------------------------------------------
# A signed inbox POST addressed to the alias (Review Focus 5)
# --------------------------------------------------------------------------


def _follow_of(local_user, sender):
    return inbox_activity(sender, activity_type='Follow', object_uri=local_user.public_url())


@pytest.mark.usefixtures('redis_double')
def test_signed_inbox_on_alias(app, onion, signing_peer, monkeypatch):
    """A Tor-capable peer signs for the Host it sent to. The signature verifies, the Follow is stored, and what is
    stored names the canonical actor."""
    monkeypatch.setitem(app.config, 'DEBUG', True)
    bob = _local_user('bob', manually_approves=True)
    activity = _follow_of(bob, signing_peer)
    _uri, headers, body = HttpSignature.signed_request(f'http://{ALIAS}/inbox', activity, signing_peer.private_key,
                                                       f'{signing_peer.ap_profile_id}#main-key', send_via_async=True)
    assert headers['Host'] == ALIAS
    response = app.test_client().post('/inbox', data=body, headers=headers, content_type='application/activity+json')
    assert response.status_code in (200, 202)
    follow = UserFollower.query.filter_by(local_user_id=bob.id, remote_user_id=signing_peer.id).one()
    assert follow.is_inward
    assert db.session.get(type(bob), bob.id).ap_profile_id == f'{CANONICAL}/u/bob'


@pytest.mark.usefixtures('redis_double')
def test_rfc9421_signed_inbox_on_alias(app, onion, signing_peer, monkeypatch):
    """An RFC 9421 signer covers @target-uri: on the alias that is the alias's own origin."""
    monkeypatch.setitem(app.config, 'DEBUG', True)
    bob = _local_user('bob', manually_approves=True)
    body = json.dumps(_follow_of(bob, signing_peer)).encode('utf8')
    headers = signature_rfc9421.sign('post', f'http://{ALIAS}/inbox', body, signing_peer.private_key,
                                     f'{signing_peer.ap_profile_id}#main-key')
    headers['Host'] = ALIAS
    response = app.test_client().post('/inbox', data=body, headers=headers, content_type='application/activity+json')
    assert response.status_code in (200, 202)
    assert UserFollower.query.filter_by(local_user_id=bob.id, remote_user_id=signing_peer.id).count() == 1


@pytest.mark.usefixtures('redis_double')
def test_an_rfc9421_signature_for_the_canonical_origin_does_not_verify_on_the_alias(app, onion, signing_peer,
                                                                                  monkeypatch):
    """R-HOSTPIN holds per storefront: a signature covering https://test.piefed.local/inbox, replayed with Host
    abc.onion, names another target URI than the one it arrived at."""
    monkeypatch.setitem(app.config, 'DEBUG', True)
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    bob = _local_user('bob', manually_approves=True)
    body = json.dumps(_follow_of(bob, signing_peer)).encode('utf8')
    headers = signature_rfc9421.sign('post', f'{CANONICAL}/inbox', body, signing_peer.private_key,
                                     f'{signing_peer.ap_profile_id}#main-key')
    headers['Host'] = ALIAS
    response = app.test_client().post('/inbox', data=body, headers=headers, content_type='application/activity+json')
    assert response.status_code == 400
    assert UserFollower.query.count() == 0


@pytest.mark.usefixtures('redis_double')
@pytest.mark.parametrize('signed_host, accepted', [(ALIAS, True), (f'{ALIAS}:6666', False)])
def test_a_cavage_signature_for_the_alias_on_another_port_is_refused(app, onion, signing_peer, monkeypatch,
                                                                     signed_host, accepted):
    """R-HOSTPIN for the alias (fix-B1): a stored domain is a bare host, so a signed Host naming it with a port is not
    this instance. The request still routes (the storefront ignores the port), but the signature is refused."""
    monkeypatch.setitem(app.config, 'DEBUG', True)
    bob = _local_user('bob', manually_approves=True)
    body = json.dumps(_follow_of(bob, signing_peer)).encode('utf8')
    headers = cavage_signed_headers(signing_peer.private_key, f'{signing_peer.ap_profile_id}#main-key', body=body,
                                    host=signed_host)
    response = app.test_client().post('/inbox', data=body, headers=headers, content_type='application/activity+json')
    assert (response.status_code in (200, 202)) is accepted, response.status_code
    if not accepted:
        assert response.status_code == 400
    assert UserFollower.query.count() == (1 if accepted else 0)


# --------------------------------------------------------------------------
# A link to any registered domain is local
# --------------------------------------------------------------------------


@pytest.mark.parametrize('origin', [CANONICAL, f'http://{ALIAS}', f'https://{ALIAS.upper()}:443'])
def test_local_link_on_alias_is_local(app, env, origin):  # noqa: F811 -- env is the imported fixture
    """get_resolve_object is what turns a pasted URL into a local object without a fetch. An onion link to a
    local post resolves to that post, as the canonical link does."""
    _register_domains()
    user, _author, _community, post, _reply, _baseline = env
    with patch('app.api.alpha.utils.misc.remote_object_to_json') as fetch:
        answer = get_resolve_object(token(user), {'q': f'{origin}/post/{post.id}'})
    assert answer['post']['post']['id'] == post.id
    fetch.assert_not_called()
    clear_domain_cache()


@pytest.mark.parametrize('url, expected', [
    ('http://abc.onion/c/x', True),
    ('https://abc.onion./c/x', True),
    ('https://test.piefed.local/c/x', True),
    ('https://evil.onion/c/x', False),
    ('https://abc.onion.evil.example/', False),
])
def test_a_redirect_to_a_registered_domain_is_safe(app, onion, url, expected):
    with app.test_request_context():
        assert is_safe_redirect_target(url) is expected


def test_an_image_on_a_registered_domain_is_local(app, onion, monkeypatch):
    monkeypatch.setattr('app.utils.mime_type_using_head', lambda url: 'image/png')
    with app.test_request_context():
        assert is_local_image_url(f'http://{ALIAS}/static/media/a.png')
        assert not is_local_image_url('http://evil.onion/static/media/a.png')


@pytest.mark.parametrize('referrer, expected', [
    (f'http://{ALIAS}/c/x', 'display_back_button'),
    (f'{CANONICAL}/c/x', 'display_back_button'),
    ('https://evil.example/c/x', ''),
])
def test_the_back_button_shows_for_a_referrer_on_any_storefront(app, onion, referrer, expected):
    with app.test_request_context(headers={'User-Agent': 'Mozilla/5.0 (iPhone)', 'Referer': referrer}):
        assert display_back_button() == expected


@pytest.mark.parametrize('path, location', [
    ('/user/lookup/bob/abc.onion', '/u/bob'),
    ('/community/lookup/general/abc.onion', '/c/general'),
    ('/feed/lookup/news/abc.onion', '/f/news'),
])
def test_a_lookup_on_a_registered_domain_is_local(app, onion, path, location):
    make_site()
    response = app.test_client().get(path)
    assert response.status_code == 302
    assert response.headers['Location'] == location


def test_nobody_blocks_a_registered_domain(app, onion):
    make_site()
    user = _local_user('carol')
    db.session.add(Domain(name=ALIAS))
    db.session.commit()
    with app.test_request_context(), pytest.raises(Exception, match='cannot_block_own_domain'):
        block_domain(ALIAS, SRC_API, f'Bearer {user.encode_jwt_token()}')


def test_a_server_view_of_a_registered_domain_is_none(app, onion):
    assert server_view_instance(ALIAS) is None


# --------------------------------------------------------------------------
# The helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize('host, expected', [
    ('abc.onion', True), ('ABC.onion.', True), ('abc.onion:80', True),
    ('test.piefed.local', True), ('evil.onion', False), ('', False), (None, False), ('[::1', False),
])
def test_is_local_host(app, onion, host, expected):
    assert storefront.is_local_host(host) is expected


def test_the_canonical_host_is_local_with_no_rows(app, db_session):
    clear_domain_cache()
    assert storefront.is_local_host('test.piefed.local')
    assert not storefront.is_local_host('abc.onion')


@pytest.mark.parametrize('url, expected', [
    ('http://abc.onion/post/1', True), ('//abc.onion/x', True), ('https://test.piefed.local', True),
    ('https://abc.onion@evil.example/', False), ('/post/1', False), ('http://[::1/', False), (None, False),
    ('https://evil.example\\@abc.onion/', False), ('https://abc.onion\\x', False),
])
def test_is_local_link(app, onion, url, expected):
    assert storefront.is_local_link(url) is expected


@pytest.mark.parametrize('server_name', [None, ''])
def test_an_unset_server_name_makes_no_host_local(app, db_session, monkeypatch, server_name):
    """With no rows and no SERVER_NAME nothing is ours: 'none' (the text of None) is not a host this site has."""
    monkeypatch.setitem(app.config, 'SERVER_NAME', server_name)
    monkeypatch.setattr(storefront, 'is_registered_host', lambda host: False)
    assert not storefront.is_local_host('none')
    assert not storefront.is_local_host('test.piefed.local')
