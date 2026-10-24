"""Nothing is POSTed to, or fetched over ActivityPub from, a domain we host (plan C4 task 1).

One server hosts every SiteDomain row and SERVER_NAME. An activity for one of them is stored directly, never sent
over the network to ourselves, and an ActivityPub object on one of them is never fetched over HTTP. Every test
watches the HTTP layer itself: a respx route that answers every outbound request, so "nothing was sent" means no
request reached httpx, not that a function returned early.
"""
import json
from datetime import timedelta
import logging
import socket
from types import SimpleNamespace

import httpx
import pytest
import respx

from app import cli, db
from app.activitypub.signature import post_request, publish_async_posts, signed_get_capped, signed_get_request
from app.activitypub.util import fetch_object_capped, webfinger_self_link_allowed
from app.constants import AP_FETCH_ACCEPT
from app.models import ActivityPubLog, SendQueue, SiteDomain, utcnow
from app.storefront import clear_domain_cache, is_hosted_uri
from app.utils import HOSTED_FETCH_STATUS, get_request, get_request_capped
from tests.factories import make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')

SERVER = 'test.piefed.local'   # SERVER_NAME: hell.cloud in production
SHOP_INBOX = 'https://shop.example/inbox'
ONION_INBOX = 'http://abc.onion/inbox'
PEER_INBOX = 'https://peer.example/inbox'
HOSTED_INBOXES = [SHOP_INBOX, f'https://{SERVER}/inbox', ONION_INBOX, 'HTTPS://Shop.Example:443/u/a/inbox']
# The host httpx connects to is ours, however the URL spells it: userinfo, the Unicode or punycode spelling of an IDN
# sister, upper case with a trailing dot and the default port (review I1, M1)
EGRESS_SPELLINGS = ['https://x@shop.example/u/alice', f'https://x:y@{SERVER}/inbox', 'https://bücher.example/inbox',
                    'https://xn--caf-dma.example/inbox', 'HTTPS://SHOP.EXAMPLE.:443/']
HOSTED_INBOXES += EGRESS_SPELLINGS

_REAL_GETADDRINFO = socket.getaddrinfo


def _getaddrinfo_without_the_network(host, *args, **kwargs):
    """`.example` hosts resolve to a public address without a resolver, as in test_local_only_outbound.py."""
    if isinstance(host, str) and host.endswith('.example'):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 0))]
    return _REAL_GETADDRINFO(host, *args, **kwargs)


@pytest.fixture
def wire(monkeypatch):
    """Every outbound HTTP request, answered 202. `wire.calls` is what left the server."""
    monkeypatch.setattr(socket, 'getaddrinfo', _getaddrinfo_without_the_network)
    with respx.mock(assert_all_called=False) as router:
        yield router.route().mock(return_value=httpx.Response(202, html='<html></html>'))


@pytest.fixture
def env(db_session):
    local = make_instance(SERVER, software='piefed')
    if db.session.query(SiteDomain).filter_by(is_default=True).first() is None:
        db.session.add(SiteDomain(domain=SERVER, is_default=True))
    db.session.add(SiteDomain(domain='shop.example'))
    db.session.add(SiteDomain(domain='abc.onion', scheme='http', federates=False))
    db.session.add(SiteDomain(domain='xn--bcher-kva.example'))   # an IDN sister stored as punycode
    db.session.add(SiteDomain(domain='café.example'))            # and one stored in Unicode
    db.session.commit()
    clear_domain_cache()
    make_instance('shop.example', software='piefed')
    make_instance('peer.example', software='lemmy')
    hal = make_user(local, 'hal', local=True, with_keys=True)
    db.session.commit()
    yield SimpleNamespace(hal=hal, key_id=f'https://{SERVER}/u/hal#main-key')
    clear_domain_cache()


def _like(n=1):
    return {'id': f'https://{SERVER}/activities/like/{n}', 'type': 'Like', 'actor': f'https://{SERVER}/u/hal',
            'object': 'https://peer.example/post/1'}


def _out_logs():
    return db.session.query(ActivityPubLog).filter_by(direction='out').all()


# -- is_hosted_uri ----------------------------------------------------------------------------------------------------

def test_is_hosted_uri_names_every_domain_we_host_in_any_spelling(env):
    for uri in HOSTED_INBOXES + ['https://shop.example./x', 'http://ABC.onion:80/c/oc']:
        assert is_hosted_uri(uri), uri
    for uri in (PEER_INBOX, 'https://shop.example.evil/inbox', 'https://evil.shop.example/inbox',
                'https://shop.example:8443/inbox', '//shop.example/inbox', 'ftp://shop.example/x', '', None, 12345,
                ['https://shop.example/inbox'], 'https://shop.example@peer.example/inbox', 'https:shop.example/x',
                'https://[shop.example/x', 'https://shop.example:abc/'):
        assert not is_hosted_uri(uri), uri


# -- post_request -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('inbox', HOSTED_INBOXES)
def test_an_activity_for_a_hosted_inbox_is_never_posted(env, wire, inbox):
    post_request(inbox, _like(), env.hal.private_key, env.key_id)
    assert wire.calls.call_count == 0
    logs = _out_logs()
    assert [(log.result, log.exception_message) for log in logs] == [
        ('failure', f'hosted domain, not sent: {inbox}')]
    assert logs[0].activity_id == _like()['id'] and logs[0].activity_type == 'Like'
    assert db.session.query(SendQueue).count() == 0


def test_an_activity_for_a_remote_inbox_is_sent(env, wire):
    post_request(PEER_INBOX, _like(), env.hal.private_key, env.key_id)
    assert [str(call.request.url) for call in wire.calls] == [PEER_INBOX]
    assert [log.result for log in _out_logs()] == ['success']


def test_a_queued_retry_for_a_hosted_inbox_is_refused(env, wire, app, monkeypatch):
    db.session.add(SendQueue(destination=SHOP_INBOX, destination_domain='shop.example', actor=env.key_id,
                             private_key=env.hal.private_key, payload=json.dumps(_like()), retries=1,
                             retry_reason='', send_after=utcnow() - timedelta(days=1)))
    db.session.commit()
    monkeypatch.setattr('app.cli.sleep', lambda seconds: None)
    monkeypatch.setattr('app.cli.requeue_stuck_media', lambda: None)
    cli.register(app)
    result = app.test_cli_runner().invoke(args=['send-queue'])
    assert result.exception is None, result.exception
    assert wire.calls.call_count == 0
    assert [log.exception_message for log in _out_logs()] == [f'hosted domain, not sent: {SHOP_INBOX}']
    assert db.session.query(SendQueue).count() == 0   # sent once, refused, never queued again


# -- ActivityPub fetches ----------------------------------------------------------------------------------------------

HOSTED_OBJECT = 'https://Shop.Example/post/1'


def test_fetch_object_capped_of_a_hosted_object_is_a_miss_without_http(env, wire, caplog):
    with caplog.at_level(logging.INFO):
        assert fetch_object_capped(HOSTED_OBJECT) == (None, 'hosted domain')
    assert wire.calls.call_count == 0
    assert f'hosted domain, not fetched: {HOSTED_OBJECT}' in caplog.text


def test_signed_get_request_of_a_hosted_object_is_a_miss_without_http(env, wire):
    response = signed_get_request(HOSTED_OBJECT, env.hal.private_key, env.key_id)
    assert response.status_code == HOSTED_FETCH_STATUS != 200
    assert wire.calls.call_count == 0


def test_signed_get_capped_of_a_hosted_object_is_a_miss_without_http(env, wire):
    assert signed_get_capped(HOSTED_OBJECT, env.hal.private_key, env.key_id, 1000) == (HOSTED_FETCH_STATUS, None)
    assert wire.calls.call_count == 0


@pytest.mark.parametrize('uri', EGRESS_SPELLINGS)
def test_an_activitypub_get_of_any_spelling_of_a_hosted_host_is_a_miss_without_http(env, wire, uri):
    assert get_request(uri, headers={'Accept': AP_FETCH_ACCEPT}).status_code == HOSTED_FETCH_STATUS
    assert fetch_object_capped(uri) == (None, 'hosted domain')
    assert signed_get_request(uri, env.hal.private_key, env.key_id).status_code == HOSTED_FETCH_STATUS
    assert wire.calls.call_count == 0


@pytest.mark.parametrize('accept', [AP_FETCH_ACCEPT, 'application/activity+json',
                                    'application/ld+json; profile="https://www.w3.org/ns/activitystreams"'])
def test_an_activitypub_get_request_of_a_hosted_url_is_a_miss_without_http(env, wire, accept):
    response = get_request(HOSTED_OBJECT, headers={'accept': accept})
    assert response.status_code == HOSTED_FETCH_STATUS
    assert get_request_capped(HOSTED_OBJECT, 1000, headers={'Accept': accept}) == (HOSTED_FETCH_STATUS, None)
    assert wire.calls.call_count == 0


def test_an_html_get_request_of_a_hosted_url_still_goes_out(env, wire):
    """A link preview or an image is an ordinary web fetch, not federation."""
    assert get_request(HOSTED_OBJECT).status_code == 202
    assert get_request(HOSTED_OBJECT, headers={'Accept': 'text/html'}).status_code == 202
    assert get_request_capped(HOSTED_OBJECT, 1000)[0] == 202
    assert wire.calls.call_count == 3


def test_an_activitypub_get_of_a_remote_url_still_goes_out(env, wire):
    assert get_request('https://peer.example/post/1', headers={'Accept': AP_FETCH_ACCEPT}).status_code == 202
    assert wire.calls.call_count == 1


# -- discovery documents, whatever the Accept (review fix round 1, ruling 2) ------------------------------------------

DISCOVERY_URLS = ['https://shop.example/.well-known/webfinger?resource=acct:alice@shop.example',
                  'https://x@shop.example/.well-known/host-meta', f'https://{SERVER}/.well-known/nodeinfo',
                  'https://shop.example/nodeinfo/2.1', 'https://Shop.Example/.WELL-KNOWN/WebFinger',
                  # the path our routes answer, however it is spelled: percent-encoded or with a dot segment (N1)
                  'https://shop.example/%6eodeinfo/2.0', 'https://shop.example/%2Ewell-known/webfinger',
                  'https://shop.example/x/../nodeinfo/2.0', 'https://shop.example/.well-known/x-nodeinfo2']


@pytest.mark.parametrize('uri', DISCOVERY_URLS)
@pytest.mark.parametrize('headers', [None, {'Accept': 'application/jrd+json'}, {'Accept': 'application/json'}])
def test_a_discovery_document_on_a_hosted_host_is_never_fetched(env, wire, uri, headers):
    assert get_request(uri, headers=dict(headers) if headers else None).status_code == HOSTED_FETCH_STATUS
    assert get_request_capped(uri, 1000, headers=headers) == (HOSTED_FETCH_STATUS, None)
    assert wire.calls.call_count == 0


def test_other_paths_and_remote_discovery_still_go_out(env, wire):
    assert get_request('https://shop.example/.well-known/security.txt').status_code == 202
    assert get_request('https://shop.example/nodeinfox').status_code == 202
    assert get_request_capped('https://peer.example/.well-known/webfinger?resource=acct:a@peer.example', 1000)[0] \
        == 202
    assert wire.calls.call_count == 3


def test_a_webfinger_self_link_on_a_hosted_host_is_never_fetched(env, monkeypatch):
    """A peer's WebFinger answer chooses the self link and its Accept: one on our host is refused whatever its type."""
    monkeypatch.setattr('app.activitypub.util._federates_with', lambda host: True)
    assert webfinger_self_link_allowed('https://peer.example/u/alice', 'alice', 'peer.example')
    assert not webfinger_self_link_allowed('https://x@shop.example/u/alice', 'alice', 'peer.example')
    assert not webfinger_self_link_allowed('https://shop.example/u/alice', 'alice', 'shop.example')


def test_a_uri_urlsplit_refuses_is_still_an_httpx_error(env, wire, app, monkeypatch):
    """The guard reads the path before get_request's own validation: a URI httpx cannot parse is left to it."""
    monkeypatch.setitem(app.config, 'DEBUG', False)
    for fetch in (lambda uri: get_request(uri), lambda uri: get_request_capped(uri, 1000)):
        for uri in ('https://[shop.example/.well-known/webfinger', 'https://shop.example:abc/.well-known/webfinger'):
            with pytest.raises(httpx.HTTPError):
                fetch(uri)
    assert wire.calls.call_count == 0


# -- the piefed_notifs path (review M2) -------------------------------------------------------------------------------

def test_publish_async_posts_leaves_out_every_hosted_inbox(env, monkeypatch, caplog):
    published = []

    class _Redis:
        def publish(self, channel, payload):
            published.append((channel, json.loads(payload)))

    monkeypatch.setattr('app.redis_client', _Redis())
    data = json.dumps(_like()).encode()
    publish_async_posts([('https://x@shop.example/inbox', {'h': 'shop'}, data), (PEER_INBOX, {'h': 'peer'}, data)])
    assert published == [('http_posts:activity', {'urls': [PEER_INBOX], 'headers': [{'h': 'peer'}],
                                                  'data': data.decode()})]
    with caplog.at_level(logging.INFO):
        publish_async_posts([(SHOP_INBOX, {}, data)])
        publish_async_posts([])
    assert len(published) == 1
    assert f'hosted domain, not sent: {SHOP_INBOX}' in caplog.text


def test_server_name_is_hosted_when_no_row_names_it(db_session):
    """SERVER_NAME's host is always served (as domain_for_host has it), even when the SiteDomain rows name only
    other domains: a seed made under another SERVER_NAME, or SERVER_NAME changed since."""
    db.session.add(SiteDomain(domain='shop.example'))
    db.session.commit()
    clear_domain_cache()
    try:
        assert db.session.query(SiteDomain).filter_by(domain=SERVER).count() == 0
        assert is_hosted_uri(f'https://{SERVER}/inbox') and is_hosted_uri(SHOP_INBOX)
        assert not is_hosted_uri(PEER_INBOX)
    finally:
        clear_domain_cache()


def test_the_hosted_destinations_are_parsed_once_until_the_domain_cache_is_cleared(env, monkeypatch):
    """Review N3: is_hosted_uri parses the URI it is asked about, not every stored domain on every call."""
    import app.storefront as storefront
    parsed = []
    real = storefront._destination

    def counting(url):
        parsed.append(url)
        return real(url)

    monkeypatch.setattr(storefront, '_destination', counting)
    clear_domain_cache()
    for uri in (SHOP_INBOX, PEER_INBOX, ONION_INBOX):
        is_hosted_uri(uri)
    rows = len(storefront.domain_rows()) + 1   # and the synthesised SERVER_NAME
    assert len(parsed) == 3 + rows
    db.session.add(SiteDomain(domain='later.example'))
    db.session.commit()
    clear_domain_cache()
    assert is_hosted_uri('https://later.example/inbox')
    assert len(parsed) == 3 + rows + 1 + rows + 1
