"""ActivityPub interop C1, item 9: the WebFinger reverse check, and JSON host-meta.

A handle `name@server` resolves through `server`'s WebFinger to an actor
document. That document names its own canonical handle,
`preferredUsername@<host of its id>`.

- The canonical handle equals the looked-up one (case-insensitively): the
  actor is stored as before, with no second request.
- Otherwise ONE capped WebFinger lookup of the canonical handle must resolve
  to the same actor id. The actor is then stored under the canonical handle,
  never under the alias.
- Otherwise the actor is refused and the refusal logged.

Every WebFinger resolver asks the same helper (spec Amendment A): the webfinger
arm of create_actor_from_remote, search_for_user, search_for_community and
search_for_feed.

Before this, a handle whose server differed from the id's host was refused
outright by actor_json_to_model's host gate (D32), and a same-host alias
(`nick@host` answered by `preferredUsername: alice`) was stored under the
alias. The reverse lookup uses get_request_capped, not get_request: the
latter sleeps and retries on a transport error, which D738/D775 forbid on a
request path.

preferredUsername is chosen by the peer. Before it goes into the reverse
lookup's URL or a stored handle it must be a plain name, and the canonical
host must pass the same instance gate (ban list or allowlist) the looked-up
host passed: an alias must not carry a banned instance's actor in.

`/.well-known/host-meta.json` is the JRD twin of the XML host-meta.

Spec: docs/superpowers/specs/2026-10-11-ap-interop-c1-design.md, item 9.
"""
import httpx
import pytest
from flask import g

from app import cache, db
from app.activitypub import routes as activitypub_routes
from app.activitypub.actor import create_actor_from_remote
from app.activitypub.util import canonical_handle, webfinger_self_href
from app.community.util import search_for_community
from app.feed.util import search_for_feed
from app.models import User
from app.user.utils import search_for_user
from tests.factories import PEER_ACTOR_PUBLIC_KEY_PEM, make_banned_instance, make_instance

pytestmark = pytest.mark.usefixtures('site')

ALICE = 'https://social.example/users/alice'


def actor_document(actor_id, name, **fields):
    document = {'@context': 'https://www.w3.org/ns/activitystreams', 'type': 'Person', 'id': actor_id,
                'preferredUsername': name, 'inbox': f'{actor_id}/inbox',
                'publicKey': {'id': f'{actor_id}#main-key', 'owner': actor_id,
                              'publicKeyPem': PEER_ACTOR_PUBLIC_KEY_PEM}}
    document.update(fields)
    return document


def serve_webfinger(http_mock, server, href):
    """`server`'s WebFinger, answering every resource with one self link to `href`."""
    return http_mock.get(f'https://{server}/.well-known/webfinger').respond(
        200, json={'links': [{'rel': 'self', 'type': 'application/activity+json', 'href': href}]})


def serve_actor(http_mock, document):
    return http_mock.get(document['id']).respond(200, json=document)


# --- the reverse check -----------------------------------------------------


def test_a_handle_that_is_already_canonical_is_stored_without_a_second_lookup(app, db_session, http_mock):
    make_instance('social.example')
    webfinger = serve_webfinger(http_mock, 'social.example', ALICE)
    serve_actor(http_mock, actor_document(ALICE, 'alice'))

    user = create_actor_from_remote('alice@social.example')

    assert user.ap_id == 'alice@social.example'
    assert webfinger.call_count == 1


def test_the_handle_comparison_ignores_case(app, db_session, http_mock):
    make_instance('social.example')
    webfinger = serve_webfinger(http_mock, 'social.example', ALICE)
    serve_actor(http_mock, actor_document(ALICE, 'Alice'))

    user = create_actor_from_remote('ALICE@Social.Example')

    assert user.ap_id == 'alice@social.example'
    assert webfinger.call_count == 1


def test_a_cross_host_handle_is_stored_under_the_canonical_one_when_it_resolves_back(app, db_session, http_mock):
    """alice@alias.example is answered by an actor on social.example. Before C1 the host gate refused it."""
    make_instance('social.example')
    alias = serve_webfinger(http_mock, 'alias.example', ALICE)
    serve_actor(http_mock, actor_document(ALICE, 'alice'))
    reverse = serve_webfinger(http_mock, 'social.example', ALICE)

    user = create_actor_from_remote('alice@alias.example')

    assert user.ap_id == 'alice@social.example'
    assert user.ap_domain == 'social.example'
    assert alias.call_count == 1 and reverse.call_count == 1
    assert reverse.calls.last.request.url.params['resource'] == 'acct:alice@social.example'
    assert db.session.query(User).filter_by(ap_id='alice@alias.example').count() == 0


def test_a_same_host_alias_is_stored_under_the_preferred_username(app, db_session, http_mock):
    make_instance('social.example')
    webfinger = serve_webfinger(http_mock, 'social.example', ALICE)
    serve_actor(http_mock, actor_document(ALICE, 'alice'))

    user = create_actor_from_remote('nick@social.example')

    assert user.ap_id == 'alice@social.example'
    assert webfinger.call_count == 2     # nick, then the reverse lookup of alice


def test_a_mismatch_the_reverse_lookup_does_not_confirm_is_refused_and_logged(app, db_session, http_mock, caplog):
    make_instance('social.example')
    serve_webfinger(http_mock, 'alias.example', ALICE)
    serve_actor(http_mock, actor_document(ALICE, 'alice'))
    serve_webfinger(http_mock, 'social.example', 'https://social.example/users/somebody-else')

    with caplog.at_level('WARNING', logger=app.logger.name):
        assert create_actor_from_remote('alice@alias.example') is None

    assert db.session.query(User).count() == 0
    assert 'WebFinger reverse check refused alice@alias.example' in caplog.text


def test_a_reverse_lookup_that_fails_refuses_the_actor(app, db_session, http_mock):
    make_instance('social.example')
    serve_webfinger(http_mock, 'alias.example', ALICE)
    serve_actor(http_mock, actor_document(ALICE, 'alice'))
    http_mock.get('https://social.example/.well-known/webfinger').respond(404)

    assert create_actor_from_remote('alice@alias.example') is None
    assert db.session.query(User).count() == 0


def test_a_document_without_a_preferred_username_skips_the_check(app, db_session, http_mock):
    """Nothing to compare: actor_json_to_model refuses it as before (D1372), and no reverse lookup is made."""
    make_instance('social.example')
    webfinger = serve_webfinger(http_mock, 'social.example', ALICE)
    document = actor_document(ALICE, 'alice')
    del document['preferredUsername']
    serve_actor(http_mock, document)

    assert create_actor_from_remote('alice@social.example') is None
    assert webfinger.call_count == 1


def test_a_handle_webfinger_does_not_know_creates_nothing(app, db_session, http_mock):
    http_mock.get('https://social.example/.well-known/webfinger').respond(404)

    assert create_actor_from_remote('alice@social.example') is None


# --- hostile values in the peer's document ---------------------------------


@pytest.mark.parametrize('name', ['alice/../admin', 'alice@evil.example', 'alice?resource=x', 'alice#x',
                                  'al ice', 'alice\nforged log line', 'alice%40evil.example', 'x' * 256])
def test_a_preferred_username_that_is_not_a_plain_name_is_refused_without_a_lookup(app, db_session, http_mock,
                                                                                     caplog, name):
    """It would go into the reverse lookup's URL and into the stored handle."""
    make_instance('social.example')
    alias = serve_webfinger(http_mock, 'alias.example', ALICE)
    serve_actor(http_mock, actor_document(ALICE, name))   # no social.example WebFinger route: a call to it fails

    with caplog.at_level('WARNING', logger=app.logger.name):
        assert create_actor_from_remote('alice@alias.example') is None

    assert alias.call_count == 1
    assert 'social.example' not in [call.request.url.host for call in http_mock.calls if
                                    call.request.url.path == '/.well-known/webfinger']
    assert db.session.query(User).count() == 0
    assert 'WebFinger reverse check refused alice@alias.example' in caplog.text
    assert 'forged log line\n' not in caplog.text


@pytest.mark.parametrize('name', ['alice.smith', 'alice-smith', 'alice_smith', 'алиса', 'x' * 100])
def test_names_other_platforms_allow_are_plain(app, db_session, http_mock, name):
    """Dots and hyphens (PeerTube, Friendica) and non-Latin letters (Lemmy) are names, not syntax."""
    reverse = serve_webfinger(http_mock, 'social.example', ALICE)
    serve_actor(http_mock, actor_document(ALICE, name))

    assert canonical_handle(actor_document(ALICE, name), 'nick', 'alias.example')[:2] == (name.lower(),
                                                                                          'social.example')
    assert reverse.call_count == 1


def test_an_actor_id_without_a_host_is_refused_without_a_lookup(app, db_session, http_mock):
    """No host means no server to ask; the reverse lookup is never aimed anywhere but the id's own host."""
    assert canonical_handle(actor_document('https://[@banned.example/users/alice', 'alice'),
                            'alice', 'alias.example') is None
    assert canonical_handle(actor_document('not a url', 'alice'), 'alice', 'alias.example') is None
    assert len(http_mock.calls) == 0


def test_the_reverse_lookup_goes_to_the_id_host_not_one_the_document_names(app, db_session, http_mock):
    """`url`, `inbox` and the alias server are all ignored: only the id's host may confirm the handle."""
    reverse = serve_webfinger(http_mock, 'social.example', ALICE)   # no alias.example route: a call to it fails
    serve_actor(http_mock, actor_document(ALICE, 'alice'))
    document = actor_document(ALICE, 'alice', url='https://alias.example/@alice', inbox='https://alias.example/inbox')

    assert canonical_handle(document, 'alice', 'alias.example')[:2] == ('alice', 'social.example')
    assert [call.request.url.host for call in http_mock.calls] == ['social.example', 'social.example']
    assert reverse.call_count == 1


def test_an_alias_cannot_carry_in_an_actor_of_a_banned_instance(app, db_session, http_mock, caplog):
    """validate_remote_actor checked alias.example. banned.example is checked here, before any request to it."""
    make_banned_instance('banned.example')
    actor_id = 'https://banned.example/users/alice'
    serve_webfinger(http_mock, 'alias.example', actor_id)
    serve_actor(http_mock, actor_document(actor_id, 'alice'))   # no banned.example WebFinger route: a call fails

    with caplog.at_level('WARNING', logger=app.logger.name):
        assert create_actor_from_remote('alice@alias.example') is None

    assert [call.request.url.host for call in http_mock.calls] == ['alias.example', 'banned.example']   # the actor
    assert http_mock.calls.last.request.url.path == '/users/alice'
    assert db.session.query(User).count() == 0
    assert 'WebFinger reverse check refused alice@alias.example' in caplog.text


def test_under_an_allowlist_an_alias_cannot_carry_in_an_actor_of_an_unlisted_instance(app, db_session, http_mock,
                                                                                       monkeypatch):
    monkeypatch.setattr('app.activitypub.util.get_setting',
                        lambda name, default=None: True if name == 'use_allowlist' else default)
    monkeypatch.setattr('app.activitypub.util.instance_allowed', lambda host: host == 'alias.example')

    assert canonical_handle(actor_document(ALICE, 'alice'), 'alice', 'alias.example') is None
    assert len(http_mock.calls) == 0


def test_under_an_allowlist_a_listed_canonical_host_is_asked(app, db_session, http_mock, monkeypatch):
    monkeypatch.setattr('app.activitypub.util.get_setting',
                        lambda name, default=None: True if name == 'use_allowlist' else default)
    monkeypatch.setattr('app.activitypub.util.instance_allowed', lambda host: host == 'social.example')
    serve_webfinger(http_mock, 'social.example', ALICE)
    serve_actor(http_mock, actor_document(ALICE, 'alice'))

    assert canonical_handle(actor_document(ALICE, 'alice'), 'alice', 'alias.example')[:2] == ('alice',
                                                                                              'social.example')


def test_a_looked_up_server_with_a_port_matches_the_id_host(app, db_session, http_mock):
    """The handle's server is an authority, the id host is a host: compared as hosts, as actor_json_to_model does."""
    document = actor_document('https://social.example:8443/users/alice', 'alice')

    assert canonical_handle(document, 'alice', 'social.example:8443') == ('alice', 'social.example:8443', document)
    assert len(http_mock.calls) == 0


# --- an actor found through an alias is taken from its own host ------------
#
# The reverse check proves the canonical handle names actor_id. It proves nothing about the document the ALIAS
# served: evil.example can serve `id: https://social.example/users/alice` with its own publicKey, and
# social.example's WebFinger will truthfully confirm alice. So the actor is refetched from actor_id, and only that
# document is stored.

ALIAS_COPY = 'https://alias.example/users/alice'
ATTACKER_PEM = '-----BEGIN PUBLIC KEY-----\nattacker-key\n-----END PUBLIC KEY-----\n'


def impersonation(http_mock, actor_id=ALICE, name='alice', alias='alias.example', resolve_alias=True,
                  **origin_fields):
    """alias.example's WebFinger points at its own copy of actor_id, carrying the attacker's key and name; the
    origin serves the real document. Returns (the alias copy, the origin document, the origin route).
    resolve_alias=False leaves the alias's routes out, for a test that hands the copy to canonical_handle itself."""
    alias_copy = actor_document(actor_id, name, publicKey={'id': f'{actor_id}#main-key', 'owner': actor_id,
                                                           'publicKeyPem': ATTACKER_PEM})
    alias_copy['name'] = 'Impostor'   # the display name; actor_document's own parameter is `name`
    if resolve_alias:
        serve_webfinger(http_mock, alias, ALIAS_COPY)
        http_mock.get(ALIAS_COPY).respond(200, json=alias_copy)
    serve_webfinger(http_mock, host_of_url(actor_id), actor_id)
    origin = actor_document(actor_id, name, **origin_fields)
    origin['name'] = 'Alice Origin'
    return alias_copy, origin, http_mock.get(actor_id).respond(200, json=origin)


def host_of_url(url):
    return url.split('/')[2]


def test_the_helper_hands_back_the_origin_document_not_the_alias_copy(app, db_session, http_mock):
    alias_copy, origin, origin_route = impersonation(http_mock, resolve_alias=False)

    assert canonical_handle(alias_copy, 'alice', 'alias.example') == ('alice', 'social.example', origin)
    assert origin_route.call_count == 1


def test_create_actor_from_remote_stores_the_origin_key_and_name_not_the_alias_copy(app, db_session, http_mock):
    make_instance('social.example')
    impersonation(http_mock)

    user = create_actor_from_remote('alice@alias.example')

    assert user.ap_profile_id == ALICE
    assert user.public_key == PEER_ACTOR_PUBLIC_KEY_PEM
    assert user.title == 'Alice Origin'


def test_an_origin_refetch_that_fails_stores_nothing(app, db_session, http_mock, caplog):
    make_instance('social.example')
    serve_webfinger(http_mock, 'alias.example', ALIAS_COPY)
    http_mock.get(ALIAS_COPY).respond(200, json=actor_document(ALICE, 'alice'))
    serve_webfinger(http_mock, 'social.example', ALICE)
    http_mock.get(ALICE).respond(503)

    with caplog.at_level('WARNING', logger=app.logger.name):
        assert create_actor_from_remote('alice@alias.example') is None

    assert db.session.query(User).count() == 0
    assert 'WebFinger reverse check refused alice@alias.example' in caplog.text


def test_an_origin_refetch_is_not_retried(app, db_session, http_mock):
    serve_webfinger(http_mock, 'social.example', ALICE)
    origin = http_mock.get(ALICE).mock(side_effect=httpx.ConnectError('down'))

    assert canonical_handle(actor_document(ALICE, 'alice'), 'alice', 'alias.example') is None
    assert origin.call_count == 1


def test_an_origin_that_answers_a_different_id_stores_nothing(app, db_session, http_mock, caplog):
    make_instance('social.example')
    impersonation(http_mock, id='https://social.example/users/somebody-else')

    with caplog.at_level('WARNING', logger=app.logger.name):
        assert create_actor_from_remote('alice@alias.example') is None

    assert db.session.query(User).count() == 0
    assert 'WebFinger reverse check refused alice@alias.example' in caplog.text


def test_an_origin_id_that_differs_only_in_case_and_space_is_the_same_actor(app, db_session, http_mock):
    """Normalised as names_actor does: stripped and lowercased."""
    alias_copy, origin, _ = impersonation(http_mock, resolve_alias=False, id=f' {ALICE.upper()} ')

    assert canonical_handle(alias_copy, 'alice', 'alias.example') == ('alice', 'social.example', origin)


def test_an_origin_document_of_another_type_stores_nothing(app, db_session, http_mock):
    """The resolver chose its branch by the alias copy's type; the origin must be the same kind of actor."""
    alias_copy, _, _ = impersonation(http_mock, resolve_alias=False, type='Group')

    assert canonical_handle(alias_copy, 'alice', 'alias.example') is None


@pytest.mark.parametrize('origin_name', ['mallory', None, 42, ''])
def test_an_origin_document_naming_another_user_stores_nothing(app, db_session, http_mock, caplog, origin_name):
    """Final review M7. The handle is read from the origin's own document, never from the alias copy: an origin
    whose preferredUsername differs (or is missing or unusable) does not state the handle that would be stored."""
    alias_copy, _, _ = impersonation(http_mock, resolve_alias=False, preferredUsername=origin_name)

    with caplog.at_level('WARNING', logger=app.logger.name):
        assert canonical_handle(alias_copy, 'alice', 'alias.example') is None

    assert 'WebFinger reverse check refused alice@alias.example' in caplog.text


def test_the_stored_handle_is_the_origin_documents_own_name(app, db_session, http_mock):
    """Compared stripped and case-insensitively, as the reverse lookup compares; the name returned is the
    origin's."""
    alias_copy, origin, _ = impersonation(http_mock, resolve_alias=False, preferredUsername=' ALICE ')

    assert canonical_handle(alias_copy, 'alice', 'alias.example') == ('alice', 'social.example', origin)


def test_a_same_host_alias_is_not_refetched(app, db_session, http_mock):
    """The document came from the looked-up host's own WebFinger answer: the path before C1, unchanged."""
    reverse = serve_webfinger(http_mock, 'social.example', ALICE)
    document = actor_document(ALICE, 'alice')

    assert canonical_handle(document, 'nick', 'social.example') == ('alice', 'social.example', document)
    assert [call.request.url.path for call in http_mock.calls] == ['/.well-known/webfinger']
    assert reverse.call_count == 1


# --- the other three resolvers ask the same helper (Amendment A) -----------
#
# actor_json_to_model is recorded, not run: what these tests prove is the handle each resolver hands it, and
# that a refused actor never reaches it. Row creation is create_actor_from_remote's tests' business above.

GROUP = 'https://social.example/c/memes'
FEED = 'https://social.example/f/news'


stored_as_documents = []   # the documents stored_as saw, in order


@pytest.fixture
def stored_as(app, db_session, site, monkeypatch):
    """Record every actor_json_to_model call of the three resolver modules; answer None (nothing created)."""
    g.admin_ids = []
    g.site = site
    calls = []
    documents = stored_as_documents
    documents.clear()

    def record(activity_json, address, server):
        calls.append((activity_json['id'], address, server))
        documents.append(activity_json)
        return None

    for module in ('app.user.utils', 'app.community.util', 'app.feed.util'):
        monkeypatch.setattr(f'{module}.actor_json_to_model', record)
    return calls


def group_document(actor_id, username):
    document = actor_document(actor_id, username, type='Group', outbox=f'{actor_id}/outbox')
    document['name'] = username.title()   # the display name; not a keyword: actor_document's own parameter is `name`
    return document


def feed_document(actor_id, username):
    document = actor_document(actor_id, username, type='Feed', following=f'{actor_id}/following')
    document['name'] = username.title()
    return document


def test_search_for_user_stores_a_confirmed_alias_under_the_canonical_handle(stored_as, http_mock):
    serve_webfinger(http_mock, 'alias.example', ALICE)
    serve_actor(http_mock, actor_document(ALICE, 'alice'))
    reverse = serve_webfinger(http_mock, 'social.example', ALICE)

    search_for_user('alice@alias.example')

    assert stored_as == [(ALICE, 'alice', 'social.example')]
    assert reverse.calls.last.request.url.params['resource'] == 'acct:alice@social.example'


def test_search_for_user_refuses_an_alias_the_reverse_lookup_does_not_confirm(stored_as, http_mock):
    serve_webfinger(http_mock, 'alias.example', ALICE)
    serve_actor(http_mock, actor_document(ALICE, 'alice'))
    serve_webfinger(http_mock, 'social.example', 'https://social.example/users/somebody-else')

    assert search_for_user('alice@alias.example') is None
    assert stored_as == []


def test_search_for_community_stores_a_confirmed_alias_under_the_canonical_handle(stored_as, http_mock):
    serve_webfinger(http_mock, 'alias.example', GROUP)
    serve_actor(http_mock, group_document(GROUP, 'memes'))
    reverse = serve_webfinger(http_mock, 'social.example', GROUP)

    search_for_community('!memes@alias.example')

    assert stored_as == [(GROUP, 'memes', 'social.example')]
    assert reverse.calls.last.request.url.params['resource'] == 'acct:memes@social.example'


def test_search_for_community_refuses_an_alias_the_reverse_lookup_does_not_confirm(stored_as, http_mock):
    serve_webfinger(http_mock, 'alias.example', GROUP)
    serve_actor(http_mock, group_document(GROUP, 'memes'))
    http_mock.get('https://social.example/.well-known/webfinger').respond(404)

    assert search_for_community('!memes@alias.example') is None
    assert stored_as == []


def test_search_for_community_asks_nothing_more_for_a_canonical_handle(stored_as, http_mock):
    webfinger = serve_webfinger(http_mock, 'social.example', GROUP)
    serve_actor(http_mock, group_document(GROUP, 'Memes'))

    search_for_community('!memes@social.example')

    assert stored_as == [(GROUP, 'memes', 'social.example')]
    assert webfinger.call_count == 1


def test_search_for_feed_reverse_looks_up_the_feed_handle_with_its_tilde(stored_as, http_mock):
    serve_webfinger(http_mock, 'alias.example', FEED)
    serve_actor(http_mock, feed_document(FEED, 'news'))
    reverse = serve_webfinger(http_mock, 'social.example', FEED)

    search_for_feed('~news@alias.example')

    assert stored_as == [(FEED, 'news', 'social.example')]
    assert reverse.calls.last.request.url.params['resource'] == 'acct:~news@social.example'


def test_search_for_feed_refuses_an_alias_the_reverse_lookup_does_not_confirm(stored_as, http_mock):
    serve_webfinger(http_mock, 'alias.example', FEED)
    serve_actor(http_mock, feed_document(FEED, 'news'))
    serve_webfinger(http_mock, 'social.example', 'https://social.example/f/other')

    assert search_for_feed('~news@alias.example') is None
    assert stored_as == []


# --- webfinger_self_href ---------------------------------------------------


def test_the_first_usable_self_link_is_the_href(app, db_session, http_mock):
    http_mock.get('https://social.example/.well-known/webfinger').respond(200, json={'links': [
        {'rel': 'http://webfinger.net/rel/profile-page', 'href': 'https://social.example/@alice'},
        {'rel': 'self'},
        {'rel': 'self', 'href': ALICE}]})

    assert webfinger_self_href('alice', 'social.example') == ALICE


def test_a_document_without_a_self_link_has_no_href(app, db_session, http_mock):
    http_mock.get('https://social.example/.well-known/webfinger').respond(200, json={'links': [
        {'rel': 'http://webfinger.net/rel/profile-page', 'href': 'https://social.example/@alice'}]})

    assert webfinger_self_href('alice', 'social.example') is None


@pytest.mark.parametrize('body', [b'[]', b'{"links": "self"}', b'"links"'])
def test_a_document_of_the_wrong_shape_has_no_href(app, db_session, http_mock, body):
    http_mock.get('https://social.example/.well-known/webfinger').respond(200, content=body)

    assert webfinger_self_href('alice', 'social.example') is None


def test_a_body_that_is_not_json_has_no_href(app, db_session, http_mock):
    http_mock.get('https://social.example/.well-known/webfinger').respond(200, content=b'<html>nope</html>')

    assert webfinger_self_href('alice', 'social.example') is None


def test_a_body_over_the_cap_has_no_href(app, db_session, http_mock, monkeypatch):
    monkeypatch.setattr('app.activitypub.util.FETCH_MAX_BYTES', 10)
    serve_webfinger(http_mock, 'social.example', ALICE)

    assert webfinger_self_href('alice', 'social.example') is None


def test_a_transport_error_has_no_href_and_is_not_retried(app, db_session, http_mock):
    route = http_mock.get('https://social.example/.well-known/webfinger').mock(
        side_effect=httpx.ConnectError('down'))

    assert webfinger_self_href('alice', 'social.example') is None
    assert route.call_count == 1


# --- host-meta.json --------------------------------------------------------


def test_host_meta_json_is_the_jrd_twin_of_the_xml(app, db_session):
    cache.clear()

    response = app.test_client().get('/.well-known/host-meta.json')

    assert response.status_code == 200
    assert response.mimetype == 'application/jrd+json'
    assert response.get_json() == {'links': [{
        'rel': 'lrdd', 'template': 'https://test.piefed.local/.well-known/webfinger?resource={uri}'}]}


def test_host_meta_json_is_cached_like_the_xml_route():
    """The suite runs NullCache, so the decorator is checked where flask-caching records it."""
    assert activitypub_routes.host_meta_json.cache_timeout == activitypub_routes.host_meta.cache_timeout == 600


@pytest.mark.parametrize('search, address, actor_id, name, kind', [
    (search_for_user, 'alice@alias.example', ALICE, 'alice', 'Person'),
    (search_for_community, '!memes@alias.example', GROUP, 'memes', 'Group'),
    (search_for_feed, '~news@alias.example', FEED, 'news', 'Feed'),
])
def test_each_search_stores_the_origin_document_not_the_alias_copy(stored_as, http_mock, search, address,
                                                                     actor_id, name, kind):
    alias_copy, origin, _ = impersonation(http_mock, actor_id=actor_id, name=name, type=kind)
    alias_copy['type'] = kind
    http_mock.get(ALIAS_COPY).respond(200, json=alias_copy)

    search(address)

    assert stored_as == [(actor_id, name, 'social.example')]
    assert stored_as_documents == [origin]
    assert stored_as_documents[0]['publicKey']['publicKeyPem'] == PEER_ACTOR_PUBLIC_KEY_PEM


@pytest.mark.parametrize('search, address, actor_id, kind', [
    (search_for_user, 'alice@alias.example', ALICE, 'Person'),
    (search_for_community, '!memes@alias.example', GROUP, 'Group'),
    (search_for_feed, '~news@alias.example', FEED, 'Feed'),
])
def test_each_search_stores_nothing_when_the_origin_refetch_fails(stored_as, http_mock, search, address, actor_id,
                                                                    kind):
    name = address.lstrip('!~').split('@')[0]
    serve_webfinger(http_mock, 'alias.example', ALIAS_COPY)
    http_mock.get(ALIAS_COPY).respond(200, json=actor_document(actor_id, name, type=kind))
    serve_webfinger(http_mock, 'social.example', actor_id)
    http_mock.get(actor_id).respond(404)

    assert search(address) is None
    assert stored_as == []
