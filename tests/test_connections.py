"""Connected accounts P1: the framework every service plugs into (spec 2026-11-13-connected-accounts-design.md).

The service here is tests/connections_fake.py's: each of its calls is an HTTP request respx answers, so these tests run
the real paths, from the post routes through dispatch and tokens down to app.connections.http.
"""
import gzip
import os
import re
import zlib
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import update
from flask import g

from app import cli, db
from app.api.alpha.utils.post import post_post
from app.clock import utcnow
from app.connections import (KEY, SERVICES, AuthFailed, MaybeSent, Permanent, Progress, QuotaWait, Service,
                             ServiceError, Transient, Unconfirmed, publishing, service_label, setting_name)
from app.connections.apps import connections_offered, enabled, enabled_services
from app import connections
from app.connections import dispatch, register
from app.connections.dispatch import (form_choice, offered_connections, perform, post_edited, post_gone,
                                      post_published, post_restored, purge_user, record_choices, retry, sweep)
from app.connections.eligibility import DROP, image_files, refusal, uploaded_video
from app.connections.http import _BoundedInflater, call, check
from app.connections.tokens import needs_reconnecting, open_tokens, seal_tokens, usable_tokens
from app.constants import POST_STATUS_SCHEDULED, POST_TYPE_ARTICLE, SRC_WEB
from app.models import File, Notification, Post, Site, SocialConnection, SocialCrosspost, User
from app.shared.post import delete_post, restore_post
from app.sealing import SealUnavailable, generate_key, keyring, seal_with, unseal_with
from app.utils import set_setting, user_agent
from tests.connections_fake import BASE, FakeService, fake_service  # noqa: F401 (fake_service is a fixture)
from tests.factories import make_community, make_community_member, make_instance, make_post, make_user
from tests.test_cli_publish_scheduled_posts import _publish
from tests.test_community_add_post import _post_payload, login, poster, url  # noqa: F401 (poster is a fixture)

pytestmark = pytest.mark.usefixtures('site')

SENT = {'id': 'r1', 'url': f'{BASE}/r1'}


@pytest.fixture
def world(app, poster, fake_service):
    client, token, community, author, english = poster
    connection = SocialConnection(user_id=author.id, service='fake', remote_id='acct-1', remote_name='@me@fake',
                                  sealed_tokens=seal_tokens({'access': 'tok-1', 'expires_at': None}))
    db.session.add(connection)
    db.session.commit()
    return SimpleNamespace(client=client, token=token, community=community, author=author, english=english,
                           connection=connection, service=fake_service, app=app)


_numbers = iter(range(1, 10_000))


def a_post(world, **columns) -> Post:
    number = next(_numbers)
    post = make_post(world.community, world.author, f'https://test.piefed.local/post/{number}', title=f'hello {number}')
    for name, value in columns.items():
        setattr(post, name, value)
    db.session.commit()
    return post


def a_row(world, post, state='queued', **columns) -> SocialCrosspost:
    row = SocialCrosspost(post_id=post.id, connection_id=world.connection.id, service='fake', state=state,
                          next_attempt_at=utcnow() if state in dispatch.WORK else None, **columns)
    db.session.add(row)
    db.session.commit()
    return row


def fresh(row) -> SocialCrosspost:
    db.session.expire_all()
    return db.session.get(SocialCrosspost, row.id)


# --------------------------------------------------------------------------
# sealing
# --------------------------------------------------------------------------

def test_a_sealed_value_opens_under_any_key_of_the_ring(app, monkeypatch):
    old, new = generate_key(), generate_key()
    monkeypatch.setitem(app.config, 'TEST_RING', old)
    token = seal_with('TEST_RING', {'a': 1})
    monkeypatch.setitem(app.config, 'TEST_RING', f'{new}, {old}')
    assert unseal_with('TEST_RING', token) == {'a': 1}
    monkeypatch.setitem(app.config, 'TEST_RING', new)
    with pytest.raises(SealUnavailable):
        unseal_with('TEST_RING', token)


@pytest.mark.parametrize('value', ['', ' , ', 'not-a-fernet-key'], ids=['empty', 'commas', 'malformed'])
def test_no_usable_key_means_no_keyring(app, monkeypatch, value):
    monkeypatch.setitem(app.config, 'TEST_RING', value)
    assert keyring('TEST_RING') is None
    with pytest.raises(SealUnavailable):
        seal_with('TEST_RING', {})
    with pytest.raises(SealUnavailable):
        unseal_with('TEST_RING', 'x')


# --------------------------------------------------------------------------
# the registry
# --------------------------------------------------------------------------

def test_a_service_is_on_with_its_key_credentials_and_switch(app, fake_service, monkeypatch):
    assert enabled(fake_service) and enabled_services() == [fake_service]
    fake_service.has_credentials = False
    assert not enabled(fake_service)
    fake_service.has_credentials = True
    set_setting(setting_name('fake'), False)
    assert not enabled(fake_service) and enabled_services() == []
    set_setting(setting_name('fake'), True)
    monkeypatch.setitem(app.config, KEY, '')
    assert not enabled(fake_service)
    assert not enabled(None)


def test_a_service_registers_under_its_name(monkeypatch):
    monkeypatch.setattr('app.connections.SERVICES', {})
    service = FakeService()
    assert register(service) is service and connections.SERVICES == {'fake': service}


def test_labels_and_whether_the_menu_offers_connections(world):
    assert service_label('fake') == 'Fake' and service_label('fake', 'x') == 'Fake (x)'
    assert service_label('nowhere') == 'nowhere'
    assert connections_offered(world.author)
    set_setting(setting_name('fake'), False)
    assert connections_offered(world.author)   # a connection of theirs still shows
    db.session.delete(world.connection)
    db.session.commit()
    assert not connections_offered(world.author)


def test_the_base_service_defaults(app):
    service = Service()
    assert service.paused_until(None) is None
    assert service.expired({}) is False
    assert service.expired({'expires_at': datetime.now().timestamp() - 10}) is True
    assert service.expired({'expires_at': datetime.now().timestamp() + 3600}) is False
    assert service.fits_type(1) and service.can_delete(None) and service.variant_label(None) == ''
    assert service.revoke({}) is None
    with pytest.raises(AuthFailed):
        service.refresh({})
    with pytest.raises(Permanent):
        service.edit(None, {}, 'x')
    for method, args in (('configured', ()), ('fits', (None,)), ('authorize_url', (None, '', '', {})),
                         ('finish', (None, {}, '', {})), ('send', (None, {}, None)), ('delete', ({}, ''))):
        with pytest.raises(NotImplementedError):
            getattr(service, method)(*args)
    assert len(ServiceError('x' * 300).message) == 255


# --------------------------------------------------------------------------
# HTTP answers
# --------------------------------------------------------------------------

@pytest.mark.parametrize('response, failure, message', [
    (httpx.Response(429, headers={'Retry-After': '30'}), Transient, 'HTTP 429'),
    (httpx.Response(503, text='down'), Transient, 'HTTP 503'),
    (httpx.Response(401, json={'error': 'invalid_token'}), AuthFailed, 'HTTP 401: invalid_token'),
    (httpx.Response(403, json={}), AuthFailed, 'HTTP 403'),
    (httpx.Response(400, json={'message': 'too long'}), Permanent, 'HTTP 400: too long'),
    (httpx.Response(422, json={'error': {'message': 'deep'}}), Permanent, 'HTTP 422: deep'),
    (httpx.Response(400, json={'error_description': 'nope'}), Permanent, 'HTTP 400: nope'),
    (httpx.Response(400, json=['a list']), Permanent, 'HTTP 400'),
    (httpx.Response(404, text='<html>'), Permanent, 'HTTP 404'),
], ids=['429', '503', '401', '403', 'message', 'nested', 'description', 'list', 'html'])
def test_an_answer_means_a_failure(response, failure, message):
    with pytest.raises(failure) as raised:
        check(response)
    assert raised.value.message == message
    if response.status_code == 429:
        assert raised.value.retry_after == 30
    elif failure is Transient:
        assert raised.value.retry_after is None


def test_an_answer_past_the_cap_is_refused_unread(app, http_mock):
    """A server the member named cannot fill memory: the answer is read in chunks and dropped past the cap."""
    http_mock.get(f'{BASE}/huge').respond(200, content=b'x' * 5000)
    with pytest.raises(Permanent) as raised:
        call('GET', f'{BASE}/huge', max_bytes=1000)
    assert raised.value.message == 'The answer was larger than 1000 bytes.'
    http_mock.get(f'{BASE}/small').respond(200, content=b'x' * 1000)
    assert call('GET', f'{BASE}/small', max_bytes=1000).content == b'x' * 1000


@pytest.fixture
def server(monkeypatch):
    """A server answering with exactly the bytes and headers a test gives, unread on the way: respx reads (and so
    decodes by its Content-Encoding) every body itself, which a bomb, a corrupt body or br would fail inside respx.
    Returns a function setting the next answer; the requests seen are kept on it."""
    answer = {}
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(answer['status'], headers=answer['headers'], stream=httpx.ByteStream(answer['body']))

    def answer_with(body: bytes, encoding: str = '', status: int = 200):
        answer.update(status=status, body=body, headers={'Content-Encoding': encoding} if encoding else {})
    answer_with.seen = seen
    monkeypatch.setattr('app.connections.http.httpx_client', httpx.Client(transport=httpx.MockTransport(handle)))
    return answer_with


def _raw_deflate(data: bytes) -> bytes:
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return compressor.compress(data) + compressor.flush()


@pytest.mark.parametrize('encoding, body', [
    ('gzip', gzip.compress(b'{"ok": true}')),
    ('x-gzip', gzip.compress(b'{"ok": true}')),
    ('deflate', zlib.compress(b'{"ok": true}')),        # zlib-wrapped, as the HTTP spec means it
    ('deflate', _raw_deflate(b'{"ok": true}')),         # raw, as some servers send it
    ('identity', b'{"ok": true}'),
    ('', b'{"ok": true}'),
], ids=['gzip', 'x-gzip', 'deflate-zlib', 'deflate-raw', 'identity', 'none'])
def test_a_compressed_answer_is_inflated(app, server, encoding, body):
    server(body, encoding)
    response = call('GET', f'{BASE}/z')
    assert response.json() == {'ok': True} and 'content-encoding' not in response.headers
    assert server.seen[-1].headers['Accept-Encoding'] == 'gzip, deflate'


@pytest.mark.parametrize('encoding', ['gzip', 'deflate'])
def test_a_decompression_bomb_is_refused_holding_no_more_than_the_cap(app, server, monkeypatch, encoding):
    """A megabyte of zeros arrives as about a kilobyte: it is refused at the cap, and no inflating step ever made
    more than the room left plus one byte."""
    zeros = b'\0' * 1_000_000
    body = gzip.compress(zeros) if encoding == 'gzip' else zlib.compress(zeros)
    assert len(body) < 2000
    made = []

    class Watched:
        def __init__(self, *args):
            self.inner = zlib.decompressobj(*args)

        def decompress(self, data, max_length=0):
            out = self.inner.decompress(data, max_length)
            made.append(len(out))
            return out

        def __getattr__(self, name):
            return getattr(self.inner, name)
    monkeypatch.setattr('app.connections.http.decompressobj', Watched)
    server(body, encoding)
    with pytest.raises(Permanent) as raised:
        call('GET', f'{BASE}/bomb', max_bytes=64_000)
    assert raised.value.message == 'The answer was larger than 64000 bytes.'
    assert made and max(made) <= 64_001 and sum(made) <= 64_001


def test_more_raw_bytes_than_the_cap_are_refused_however_they_inflate(app, server):
    server(gzip.compress(os.urandom(5000)), 'gzip')   # random bytes do not compress: about 5 KB on the wire
    with pytest.raises(Permanent) as raised:
        call('GET', f'{BASE}/big', max_bytes=1000)
    assert raised.value.message == 'The answer was larger than 1000 bytes.'


@pytest.mark.parametrize('encoding', ['gzip, gzip', 'br', 'zstd', 'compress'])
def test_an_encoding_not_asked_for_is_refused_unread(app, server, encoding):
    """Stacked codings multiply a bomb, and br cannot be inflated with a bound: refused from the headers alone."""
    server(b'not read', encoding)
    with pytest.raises(Permanent) as raised:
        call('GET', f'{BASE}/odd')
    assert raised.value.message == f'The answer came encoded as {encoding}, which PieFed did not ask for.'


@pytest.mark.parametrize('body, message', [
    (b'this is not gzip at all', 'The answer could not be decoded.'),
    (gzip.compress(b'{"ok": true}' * 100)[:40], 'The answer ended part way through.'),
], ids=['corrupt', 'truncated'])
def test_a_broken_compressed_answer_is_refused(app, server, body, message):
    server(body, 'gzip')
    with pytest.raises(Permanent) as raised:
        call('GET', f'{BASE}/broken')
    assert raised.value.message == message


def test_an_empty_chunk_is_no_body(app):
    """A transport may hand over an empty chunk: it starts no stream, so a body of nothing stays a body of nothing."""
    inflater = _BoundedInflater('deflate', 1000)
    inflater.feed(b'')
    assert inflater.decoder is None and inflater.finish() == b''


def test_an_empty_answer_under_a_compressed_header(app, server):
    server(b'', 'gzip', status=204)
    assert call('DELETE', f'{BASE}/gone').content == b''


def test_a_success_and_an_unreachable_service(app, http_mock):
    route = http_mock.get(f'{BASE}/ok').respond(200, json={})
    assert call('GET', f'{BASE}/ok').status_code == 200
    with app.app_context():
        assert route.calls.last.request.headers['User-Agent'] == user_agent()
    http_mock.get(f'{BASE}/down').mock(side_effect=httpx.ConnectError('refused'))
    with pytest.raises(Transient) as raised:
        call('GET', f'{BASE}/down')
    assert raised.value.message == 'Could not reach the service (ConnectError).'
    assert not isinstance(raised.value, MaybeSent)


@pytest.mark.parametrize('failure', [httpx.ConnectTimeout('slow'), httpx.PoolTimeout('busy'),
                                     httpx.UnsupportedProtocol('ftp')], ids=['connect-timeout', 'pool', 'protocol'])
def test_a_request_that_never_left_is_plainly_transient(app, http_mock, failure):
    http_mock.post(f'{BASE}/posts').mock(side_effect=failure)
    with pytest.raises(Transient) as raised:
        call('POST', f'{BASE}/posts')
    assert not isinstance(raised.value, MaybeSent)


@pytest.mark.parametrize('failure', [httpx.ReadTimeout('slow'), httpx.WriteTimeout('slow'), httpx.ReadError('reset'),
                                     httpx.WriteError('reset'), httpx.RemoteProtocolError('cut')],
                         ids=['read-timeout', 'write-timeout', 'read', 'write', 'protocol'])
def test_a_request_that_left_and_got_no_answer_may_have_been_acted_on(app, http_mock, failure):
    http_mock.post(f'{BASE}/posts').mock(side_effect=failure)
    with pytest.raises(MaybeSent) as raised:
        call('POST', f'{BASE}/posts')
    assert raised.value.message == f'No answer came back from the service ({type(failure).__name__}).'


def test_publishing_turns_no_answer_into_unconfirmed_and_lets_the_rest_through():
    with pytest.raises(Unconfirmed) as raised:
        with publishing('Fake'):
            raise MaybeSent('no answer')
    assert raised.value.message == 'Fake may have posted it: no answer came back. Check before trying again.'
    with pytest.raises(Transient) as plain:
        with publishing('Fake'):
            raise Transient('HTTP 503')
    assert not isinstance(plain.value, Unconfirmed)


def test_progress_saves_a_copy_of_its_data():
    saved = []
    progress = Progress({'a': 1}, save=saved.append)
    progress.data['b'] = 2
    progress.save()
    progress.data['c'] = 3
    assert saved == [{'a': 1, 'b': 2}]
    Progress().save()   # none to save to: nothing happens


# --------------------------------------------------------------------------
# tokens
# --------------------------------------------------------------------------

def test_fresh_tokens_are_used_as_they_are(world):
    assert usable_tokens(world.connection.id, world.service) == {'access': 'tok-1', 'expires_at': None}


def test_expired_tokens_are_refreshed_and_stored(world, http_mock):
    world.connection.sealed_tokens = seal_tokens({'access': 'old', 'expires_at': 1})
    db.session.commit()
    http_mock.post(f'{BASE}/refresh').respond(200, json={'token': 'new'})
    assert usable_tokens(world.connection.id, world.service)['access'] == 'new'
    db.session.expire_all()
    assert open_tokens(db.session.get(SocialConnection, world.connection.id))['access'] == 'new'


def test_a_refused_refresh_asks_the_member_to_reconnect_once(world, http_mock):
    world.connection.sealed_tokens = seal_tokens({'access': 'old', 'expires_at': 1})
    db.session.commit()
    http_mock.post(f'{BASE}/refresh').respond(401, json={'error': 'invalid_grant'})
    with pytest.raises(AuthFailed):
        usable_tokens(world.connection.id, world.service)
    with pytest.raises(AuthFailed):   # marked: refused at once, with no second call
        usable_tokens(world.connection.id, world.service)
    connection = db.session.get(SocialConnection, world.connection.id)
    assert connection.state == 'reauth' and connection.last_error == 'HTTP 401: invalid_grant'
    needs_reconnecting(connection, world.service, 'again')
    db.session.commit()
    notes = Notification.query.filter_by(subtype='connection_reauth').all()
    assert len(notes) == 1 and notes[0].user_id == world.author.id
    assert notes[0].url == '/user/settings/connections' and '@me@fake' in notes[0].title


# --------------------------------------------------------------------------
# eligibility (spec §5)
# --------------------------------------------------------------------------

@pytest.mark.parametrize('change, expected', [
    ({}, None),
    ({'deleted': True}, DROP),
    ({'status': POST_STATUS_SCHEDULED}, DROP),
    ({'visibility': 'followers'}, DROP),
    ({'visibility': 'unlisted'}, DROP),
    ({'local_only': True}, DROP),
    ({'ap_id': None}, DROP),
    ({'ap_id': 'http://abcdefghij.onion/post/1'}, DROP),
    ({'nsfw': True}, 'Skipped: NSFW posts are not sent to Fake.'),
    ({'nsfl': True}, 'Skipped: NSFW posts are not sent to Fake.'),
], ids=['public', 'deleted', 'scheduled', 'followers', 'unlisted', 'local-only', 'no-id', 'onion', 'nsfw', 'nsfl'])
def test_which_posts_go(world, change, expected):
    post = a_post(world, **change)
    assert refusal(post, world.connection, world.service) == expected


@pytest.mark.parametrize('where', ['private', 'local_only', 'instance', 'not-theirs', 'unfit'])
def test_private_places_and_other_shapes_never_go(world, where):
    post = a_post(world)
    if where == 'instance':
        db.session.get(Site, 1).private_instance = True
    elif where == 'not-theirs':
        world.connection.user_id = 1
    elif where == 'unfit':
        world.service.fit = False
    else:
        setattr(world.community, where, True)
    db.session.commit()
    assert refusal(post, world.connection, world.service) == DROP


def test_a_takes_nsfw_service_takes_nsfw(world, monkeypatch):
    monkeypatch.setattr(FakeService, 'takes_nsfw', True)
    assert refusal(a_post(world, nsfw=True), world.connection, world.service) is None


def test_videos_and_pictures_of_a_post(world):
    post = a_post(world)
    assert not uploaded_video(post) and image_files(post) == []
    post.extensions = {'media': {'mime': 'video/mp4'}}
    picture = File(file_path='app/static/media/posts/a.jpg')
    bare = File()
    db.session.add_all([picture, bare])
    db.session.commit()
    assert uploaded_video(post)
    post.image_id = bare.id
    db.session.commit()
    assert image_files(post) == []
    post.image_id = picture.id
    db.session.commit()
    assert image_files(post) == [picture]
    second = File(source_url='https://test.piefed.local/static/b.jpg')
    db.session.add(second)
    post.gallery.append(picture)
    post.gallery.append(second)
    post.gallery_count = 2
    db.session.commit()
    assert image_files(post) == [picture, second]


# --------------------------------------------------------------------------
# choosing (C3)
# --------------------------------------------------------------------------

def test_what_the_author_ticked(app):
    assert form_choice(SimpleNamespace(crosspost_to=SimpleNamespace(data=[3, '4'])), web=True) == [3, 4]
    assert form_choice(SimpleNamespace(crosspost_to=SimpleNamespace(data=None)), web=True) == []
    assert form_choice(SimpleNamespace(), web=True) == []
    assert form_choice({'crosspost_to': [1, True, 'x', 2]}, web=False) == [1, 2]
    assert form_choice({'crosspost_to': 'all'}, web=False) is None
    assert form_choice({}, web=False) is None
    assert form_choice(None, web=False) is None


def test_choices_wait_for_the_publish(world):
    quiet = SocialConnection(user_id=world.author.id, service='fake', remote_id='acct-2', remote_name='@two',
                             sealed_tokens=seal_tokens({}), default_on=False)
    gone = SocialConnection(user_id=world.author.id, service='gone', remote_id='acct-3', remote_name='@three',
                            sealed_tokens=seal_tokens({}))
    db.session.add_all([quiet, gone])
    db.session.commit()
    first, second = a_post(world), a_post(world)
    record_choices(first, None)
    record_choices(second, [quiet.id, world.connection.id, gone.id])
    db.session.commit()
    assert {(r.connection_id, r.state) for r in first.social_crossposts} == {(world.connection.id, 'waiting')}
    assert {r.connection_id for r in second.social_crossposts} == {world.connection.id, quiet.id}
    assert [c.id for c, _service in offered_connections(world.author, POST_TYPE_ARTICLE)] == [world.connection.id,
                                                                                            quiet.id]
    assert offered_connections(world.author, 99) == []


# --------------------------------------------------------------------------
# sending
# --------------------------------------------------------------------------

def test_a_published_post_is_sent(world, http_mock):
    route = http_mock.post(f'{BASE}/posts').respond(200, json=SENT)
    post = a_post(world)
    a_row(world, post, state='waiting')
    post_published(post)
    row = fresh(SocialCrosspost.query.filter_by(post_id=post.id).one())
    assert (row.state, row.remote_id, row.remote_url, row.attempts) == ('sent', 'r1', f'{BASE}/r1', 1)
    assert row.sent_at is not None and row.next_attempt_at is None
    assert route.calls.last.request.headers['Authorization'] == 'Bearer tok-1'


def test_a_post_that_may_not_go_is_dropped_or_skipped(world, monkeypatch):
    post = a_post(world, nsfw=True)
    a_row(world, post, state='waiting')
    post_published(post)
    row = SocialCrosspost.query.filter_by(post_id=post.id).one()
    assert (row.state, row.last_error) == ('skipped', 'Skipped: NSFW posts are not sent to Fake.')

    world.service.fit = False
    unfit = a_post(world)
    a_row(world, unfit, state='waiting')
    post_published(unfit)
    assert SocialCrosspost.query.filter_by(post_id=unfit.id).count() == 0

    world.service.fit = True
    orphan = a_post(world)
    db.session.add(SocialCrosspost(post_id=orphan.id, connection_id=None, service='fake', state='waiting'))
    db.session.commit()
    post_published(orphan)
    assert SocialCrosspost.query.filter_by(post_id=orphan.id).count() == 0

    set_setting(setting_name('fake'), False)
    off = a_post(world)
    a_row(world, off, state='waiting')
    post_published(off)
    assert SocialCrosspost.query.filter_by(post_id=off.id).count() == 0


def test_a_repeating_occurrence_takes_the_templates_choices(world, http_mock):
    http_mock.post(f'{BASE}/posts').respond(200, json=SENT)
    template, occurrence = a_post(world, status=POST_STATUS_SCHEDULED), a_post(world)
    a_row(world, template, state='waiting')
    post_published(occurrence, template=template)
    row = fresh(SocialCrosspost.query.filter_by(post_id=occurrence.id).one())
    assert row.state == 'sent'
    assert SocialCrosspost.query.filter_by(post_id=template.id).one().state == 'waiting'


def test_a_transient_failure_backs_off_then_fails(world, http_mock):
    http_mock.post(f'{BASE}/posts').respond(503)
    row = a_row(world, a_post(world))
    perform(row.id)
    row = fresh(row)
    assert (row.state, row.attempts, row.last_error) == ('queued', 1, 'HTTP 503')
    assert timedelta(seconds=50) < row.next_attempt_at - utcnow() <= timedelta(seconds=60)
    for attempt in range(2, dispatch.MAX_ATTEMPTS + 1):
        row.next_attempt_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
        perform(row.id)
        row = fresh(row)
        assert row.attempts == attempt
    assert (row.state, row.next_attempt_at) == ('failed', None)


def test_a_retry_after_longer_than_the_backoff_is_honoured(world, http_mock):
    http_mock.post(f'{BASE}/posts').respond(429, headers={'Retry-After': '600'})
    row = a_row(world, a_post(world))
    perform(row.id)
    assert fresh(row).next_attempt_at - utcnow() > timedelta(seconds=590)


def test_a_spent_allowance_waits_without_costing_an_attempt(world, monkeypatch):
    until = utcnow() + timedelta(hours=5)

    def spent(*args):
        raise QuotaWait("Waiting for Fake's daily allowance.", until)
    monkeypatch.setattr(FakeService, 'send', spent)
    row = a_row(world, a_post(world))
    perform(row.id)
    row = fresh(row)
    assert (row.state, row.attempts, row.next_attempt_at, row.last_error) == (
        'queued', 0, until, "Waiting for Fake's daily allowance.")


@pytest.mark.parametrize('status, state, error', [
    (401, 'reauth', 'HTTP 401: expired'),
    (400, 'ok', 'HTTP 400: expired'),
], ids=['auth', 'permanent'])
def test_a_refused_send_fails(world, http_mock, status, state, error):
    http_mock.post(f'{BASE}/posts').respond(status, json={'message': 'expired'})
    row = a_row(world, a_post(world))
    perform(row.id)
    row = fresh(row)
    assert (row.state, row.last_error, row.connection.state) == ('failed', error, state)


def test_an_unexpected_error_fails_the_row_without_its_text(world, monkeypatch):
    def broken(*args):
        raise ValueError('secret detail')
    monkeypatch.setattr(FakeService, 'send', broken)
    row = a_row(world, a_post(world))
    perform(row.id)
    row = fresh(row)
    assert (row.state, row.last_error, row.attempts) == ('failed', 'Something went wrong here (ValueError).', 1)


def test_a_paused_service_holds_its_work_at_no_cost(world, monkeypatch):
    until = utcnow() + timedelta(minutes=30)
    monkeypatch.setattr(FakeService, 'paused_until', lambda self, connection: until)
    row = a_row(world, a_post(world))
    perform(row.id)
    row = fresh(row)
    assert (row.state, row.attempts, row.next_attempt_at) == ('queued', 0, until)
    assert row.last_error == 'Waiting: Fake asked for a pause.'


def test_nothing_is_sent_for_a_gone_connection_or_a_service_switched_off(world):
    post = a_post(world)
    orphan = SocialCrosspost(post_id=post.id, connection_id=None, service='fake', state='queued',
                             next_attempt_at=utcnow())
    db.session.add(orphan)
    db.session.commit()
    perform(orphan.id)
    assert (fresh(orphan).state, fresh(orphan).last_error) == ('failed', 'This account is no longer connected.')
    set_setting(setting_name('fake'), False)
    row = a_row(world, a_post(world))
    perform(row.id)
    assert fresh(row).state == 'failed'


def test_a_connection_needing_reconnecting_sends_nothing(world):
    world.connection.state = 'reauth'
    db.session.commit()
    row = a_row(world, a_post(world))
    perform(row.id)
    assert (fresh(row).state, fresh(row).last_error) == ('failed', 'This account needs reconnecting.')


def test_work_not_due_or_taken_is_left_alone(world):
    later = a_row(world, a_post(world))
    later.next_attempt_at = utcnow() + timedelta(minutes=5)
    db.session.commit()
    done = a_row(world, a_post(world), state='sent')
    perform(later.id)
    perform(done.id)
    perform(999_999)
    assert fresh(later).state == 'queued' and fresh(done).state == 'sent'
    nothing = update(SocialCrosspost).where(SocialCrosspost.id == -1).values(attempts=0)
    due = a_row(world, a_post(world))
    with patch('app.connections.dispatch.update') as racing:   # another worker took it between the read and the update
        racing.return_value.where.return_value.values.return_value = nothing
        perform(due.id)
    assert fresh(due).state == 'queued'


def test_the_sweep_starts_due_work(world, http_mock, app, monkeypatch):
    http_mock.post(f'{BASE}/posts').respond(200, json=SENT)
    due = a_row(world, a_post(world))
    waiting = a_row(world, a_post(world))
    waiting.next_attempt_at = utcnow() + timedelta(minutes=5)
    db.session.commit()
    monkeypatch.setattr(app, 'debug', True)   # run() calls the task directly in debug
    assert sweep() == 1
    assert fresh(due).state == 'sent' and fresh(waiting).state == 'queued'


# --------------------------------------------------------------------------
# edits, deletes, restores (C4, spec §7)
# --------------------------------------------------------------------------

def test_an_edit_reaches_the_copy(world, http_mock):
    route = http_mock.put(f'{BASE}/posts/r1').respond(200, json={})
    post = a_post(world)
    row = a_row(world, post, state='sent', remote_id='r1')
    post.title = 'renamed'
    db.session.commit()
    post_edited(post)
    assert route.calls.last.request.read() == b'{"text":"renamed"}'
    assert fresh(row).state == 'sent' and fresh(row).edited_at is not None


def test_an_edit_that_makes_the_post_private_deletes_the_copy(world, http_mock):
    http_mock.delete(f'{BASE}/posts/r1').respond(204)
    post = a_post(world)
    row = a_row(world, post, state='sent', remote_id='r1')
    post.visibility = 'followers'
    db.session.commit()
    post_edited(post)
    assert fresh(row).state == 'deleted'


def test_a_copy_that_cannot_be_edited_is_left_alone(world, monkeypatch):
    monkeypatch.setattr(FakeService, 'can_edit', False)
    post = a_post(world)
    row = a_row(world, post, state='sent', remote_id='r1')
    orphan = SocialCrosspost(post_id=post.id, connection_id=None, service='fake', state='sent')
    db.session.add(orphan)
    db.session.commit()
    post_edited(post)
    assert fresh(row).state == 'sent' and fresh(row).edited_at is None


def test_a_refused_edit_leaves_the_copy_as_it_was(world, http_mock):
    http_mock.put(f'{BASE}/posts/r1').respond(400, json={'message': 'locked'})
    post = a_post(world)
    row = a_row(world, post, state='sent', remote_id='r1')
    post_edited(post)
    row = fresh(row)
    assert (row.state, row.last_error) == ('sent', 'HTTP 400: locked')


def test_a_deleted_post_takes_its_copies_with_it(world, http_mock):
    http_mock.delete(f'{BASE}/posts/r1').respond(204)
    post = a_post(world)
    sent = a_row(world, post, state='sent', remote_id='r1')
    others = [SocialCrosspost(post_id=post.id, connection_id=None, service='fake', state=state)
              for state in ('waiting', 'queued', 'failed', 'skipped')]
    db.session.add_all(others)
    db.session.commit()
    post_gone(post)
    assert fresh(sent).state == 'deleted'
    assert [fresh(row).state for row in others] == ['deleted', 'deleted', 'deleted', 'skipped']


def test_a_copy_the_service_cannot_delete_is_left_to_the_author(world):
    post = a_post(world)
    row = a_row(world, post, state='sent', remote_id='r1', variant='nodelete')
    post_gone(post)
    row = fresh(row)
    assert (row.state, row.last_error) == ('manual', 'Delete it by hand on Fake.')


def test_a_restored_post_is_sent_again(world, http_mock):
    http_mock.post(f'{BASE}/posts').respond(200, json={'id': 'r2'})
    post = a_post(world)
    row = a_row(world, post, state='deleted', remote_id='r1')
    post_restored(post)
    row = fresh(row)
    assert (row.state, row.remote_id, row.remote_url) == ('sent', 'r2', None)


def test_a_restored_unpublished_post_waits_for_its_publish(world):
    post = a_post(world, status=POST_STATUS_SCHEDULED)
    row = a_row(world, post, state='deleted', remote_id='r1')
    post_restored(post)
    assert fresh(row).state == 'waiting'


def test_a_failed_copy_can_be_retried(world, http_mock):
    http_mock.post(f'{BASE}/posts').respond(200, json=SENT)
    row = a_row(world, a_post(world), state='failed', attempts=5, last_error='HTTP 503')
    retry(row)
    row = fresh(row)
    assert (row.state, row.attempts, row.last_error) == ('sent', 1, None)


def test_no_answer_to_a_send_is_tried_again_carrying_its_progress(world, http_mock):
    route = http_mock.post(f'{BASE}/posts')
    route.side_effect = [httpx.ReadTimeout('slow'), httpx.Response(200, json=SENT)]
    world.service.progress_steps = [('key', 'k-1')]
    row = a_row(world, a_post(world))
    perform(row.id)
    row = fresh(row)
    assert (row.state, row.attempts, row.progress) == ('queued', 1, {'key': 'k-1'})
    assert row.last_error == 'No answer came back from the service (ReadTimeout).'
    seen = []
    world.service.progress_steps = []
    original = FakeService.send
    world.service.send = lambda post, tokens, connection, progress=None: (
        seen.append(dict(progress.data)) or original(world.service, post, tokens, connection, progress))
    row.next_attempt_at = utcnow() - timedelta(seconds=1)
    db.session.commit()
    perform(row.id)
    row = fresh(row)
    assert seen == [{'key': 'k-1'}]
    assert (row.state, row.remote_id, row.progress) == ('sent', 'r1', None)


def test_an_unconfirmed_copy_waits_for_the_author(world, monkeypatch):
    def unconfirmed(post, tokens, connection, progress=None):
        raise Unconfirmed('Fake may have posted it: no answer came back. Check before trying again.')
    monkeypatch.setattr(world.service, 'send', unconfirmed)
    row = a_row(world, a_post(world))
    perform(row.id)
    row = fresh(row)
    assert (row.state, row.next_attempt_at, row.attempts) == ('unconfirmed', None, 1)
    assert row.last_error.startswith('Fake may have posted it')
    note = Notification.query.filter_by(subtype='crosspost_unconfirmed', user_id=world.author.id).one()
    assert note.url == f'/post/{row.post_id}' and note.targets == {'crosspost_id': row.id}
    assert 'check @me@fake' in note.title
    perform(row.id)   # never retried by itself
    assert fresh(row).attempts == 1


def test_an_unconfirmed_copy_can_be_retried_by_its_author(world, http_mock):
    http_mock.post(f'{BASE}/posts').respond(200, json=SENT)
    row = a_row(world, a_post(world), state='unconfirmed', last_error='Fake may have posted it')
    response = world.client.post(f'/user/settings/crossposts/{row.id}/retry', data={'csrf_token': world.token})
    assert response.status_code == 302 and fresh(row).state == 'sent'


def test_a_deleted_post_leaves_an_unconfirmed_copy_to_the_author(world):
    post = a_post(world)
    row = a_row(world, post, state='unconfirmed')
    gone = SocialCrosspost(post_id=post.id, connection_id=None, service='removed', state='unconfirmed')
    db.session.add(gone)
    db.session.commit()
    post_gone(post)
    row, gone = fresh(row), fresh(gone)
    assert (row.state, row.last_error) == ('manual', 'If it was posted on Fake, delete it there by hand.')
    assert (gone.state, gone.last_error) == ('manual', 'If it was posted on removed, delete it there by hand.')


def test_a_restored_post_starts_its_copies_afresh(world):
    post = a_post(world, status=POST_STATUS_SCHEDULED)
    row = a_row(world, post, state='deleted', remote_id='r1', progress={'key': 'old'})
    post_restored(post)
    assert fresh(row).progress is None


def test_disconnecting_revokes_and_keeps_the_copies(world, http_mock):
    http_mock.post(f'{BASE}/revoke').respond(500)   # a failed revoke never keeps a member connected
    row = a_row(world, a_post(world), state='sent', remote_id='r1')
    dispatch.disconnect(world.connection)
    assert SocialConnection.query.count() == 0
    assert fresh(row).state == 'sent' and fresh(row).connection_id is None


def test_a_connection_to_a_service_since_removed_is_forgotten_without_a_revoke(world):
    world.connection.service = 'uninstalled'
    db.session.commit()
    dispatch.disconnect(world.connection)
    assert SocialConnection.query.count() == 0


def test_purging_an_account_deletes_its_copies_and_connections(world, http_mock):
    http_mock.delete(f'{BASE}/posts/r1').respond(204)
    http_mock.post(f'{BASE}/revoke').respond(200)
    row = a_row(world, a_post(world), state='sent', remote_id='r1')
    purge_user(world.author)
    assert fresh(row).state == 'deleted' and SocialConnection.query.count() == 0


# --------------------------------------------------------------------------
# the post paths
# --------------------------------------------------------------------------

def test_the_post_form_offers_the_accounts_and_sends_the_ticked_ones(world, http_mock, monkeypatch):
    monkeypatch.setitem(world.app.config, 'WTF_CSRF_ENABLED', True)   # the form renders form.csrf_token
    http_mock.post(f'{BASE}/posts').respond(200, json=SENT)
    page = world.client.get(url(world.app, 'community.add_post', actor=world.community.name, type='discussion'))
    assert b'Also post to' in page.data and b'@me@fake' in page.data
    assert re.search(rb'value="%d"\s+id="crosspost_to_%d" checked' % (world.connection.id, world.connection.id),
                     page.data)
    response = world.client.post(url(world.app, 'community.add_post', actor=world.community.name, type='discussion'),
                                 data=_post_payload(world.token, world.english, world.community, title='Shared',
                                                    crosspost_to=str(world.connection.id)),
                                 content_type='multipart/form-data')
    assert response.status_code == 302
    db.session.expire_all()
    post = Post.query.filter_by(title='Shared').one()
    assert [(row.connection_id, row.state) for row in post.social_crossposts] == [(world.connection.id, 'sent')]
    world.client.post(url(world.app, 'community.add_post', actor=world.community.name, type='discussion'),
                      data=_post_payload(world.token, world.english, world.community, title='Kept here'),
                      content_type='multipart/form-data')
    assert Post.query.filter_by(title='Kept here').one().social_crossposts == []


def test_an_api_post_uses_the_defaults_unless_it_says(world, http_mock):
    http_mock.post(f'{BASE}/posts').respond(200, json=SENT)
    bearer = f'Bearer {world.author.encode_jwt_token()}'
    base = {'title': 'from an app', 'community_id': world.community.id, 'language_id': world.english.id}
    with world.app.test_request_context():
        g.site, g.admin_ids = db.session.get(Site, 1), []
        first = post_post(bearer, dict(base))['post_view']['post']['id']
        second = post_post(bearer, {**base, 'title': 'quiet', 'crosspost_to': []})['post_view']['post']['id']
    db.session.expire_all()
    assert [row.state for row in db.session.get(Post, first).social_crossposts] == ['sent']
    assert db.session.get(Post, second).social_crossposts == []


def test_deleting_and_restoring_a_post_follows_its_copies(world, http_mock):
    http_mock.delete(f'{BASE}/posts/r1').respond(204)
    http_mock.post(f'{BASE}/posts').respond(200, json={'id': 'r2'})
    post = a_post(world)
    row = a_row(world, post, state='sent', remote_id='r1')
    with world.app.test_request_context():
        with patch('app.shared.post.current_user', world.author):
            delete_post(post.id, False, SRC_WEB, None)
            assert fresh(row).state == 'deleted'
            restore_post(post.id, SRC_WEB, None)
    assert (fresh(row).state, fresh(row).remote_id) == ('sent', 'r2')


def test_a_scheduled_post_sends_its_choices_when_it_publishes(app, scheduled_world, http_mock):
    http_mock.post(f'{BASE}/posts').respond(200, json=SENT)
    _publish(app)
    db.session.expire_all()
    rows = SocialCrosspost.query.all()
    assert [row.state for row in rows] == ['sent']


def test_a_repeating_schedule_sends_each_occurrence(app, scheduled_world, http_mock):
    http_mock.post(f'{BASE}/posts').respond(200, json=SENT)
    scheduled_world.post.repeat = 'daily'
    db.session.commit()
    _publish(app)
    db.session.expire_all()
    occurrence = Post.query.filter(Post.id != scheduled_world.post.id).one()
    assert [row.state for row in occurrence.social_crossposts] == ['sent']
    assert [row.state for row in scheduled_world.post.social_crossposts] == ['waiting']


@pytest.fixture
def scheduled_world(app, db_session, monkeypatch, fake_service):
    """tests/test_cli_publish_scheduled_posts.py's `scheduled`, with a connection the post chose."""
    instance = make_instance('test.piefed.local', software='piefed')
    make_user(instance, 'founder', local=True)
    author = make_user(instance, 'author', local=True, with_keys=True)
    author.verified = True
    community = make_community('general')
    db.session.commit()
    make_community_member(author, community)
    post = Post(user_id=author.id, community_id=community.id, instance_id=instance.id, title='scheduled', body='',
                type=1, status=POST_STATUS_SCHEDULED, timezone='UTC', scheduled_for=utcnow() - timedelta(hours=1),
                posted_at=utcnow(), created_at=utcnow(), ap_id='https://test.piefed.local/post/scheduled')
    db.session.add(post)
    db.session.commit()
    monkeypatch.setattr('app.cli.task_selector', lambda *a, **k: None)
    monkeypatch.setattr('app.cli.notify_about_post', lambda *a, **k: None)
    cli.register(app)
    world = SimpleNamespace(author=author, community=community, post=post)
    connection = SocialConnection(user_id=world.author.id, service='fake', remote_id='acct-1', remote_name='@me',
                                  sealed_tokens=seal_tokens({'access': 'tok'}))
    db.session.add(connection)
    db.session.commit()
    db.session.add(SocialCrosspost(post_id=world.post.id, connection_id=connection.id, service='fake',
                                   state='waiting'))
    db.session.commit()
    return world


# --------------------------------------------------------------------------
# the member's pages
# --------------------------------------------------------------------------

def test_the_connections_page(world, monkeypatch):
    page = world.client.get('/user/settings/connections')
    assert page.status_code == 200
    assert b'@me@fake' in page.data and b'Working' in page.data and b'Connect Fake' in page.data
    world.connection.state = 'reauth'
    world.connection.last_error = 'HTTP 401'
    db.session.commit()
    page = world.client.get('/user/settings/connections')
    assert b'Needs reconnecting' in page.data and b'Reconnect' in page.data and b'HTTP 401' in page.data
    world.connection.state = 'ok'
    db.session.commit()
    set_setting(setting_name('fake'), False)
    page = world.client.get('/user/settings/connections')
    assert b'Switched off by the admins' in page.data and b'Connect Fake' not in page.data
    db.session.delete(world.connection)
    db.session.commit()
    assert b'No services are available here yet.' in world.client.get('/user/settings/connections').data
    set_setting(setting_name('fake'), True)
    monkeypatch.setattr('app.user.connections.current_storefront',
                        lambda: SimpleNamespace(domain='elsewhere.example', id=None, is_default=False))
    page = world.client.get('/user/settings/connections')
    assert b'Accounts are connected on' in page.data and b'Connect Fake' not in page.data


def _connect(world, http_mock, variant=''):
    http_mock.get(f'{BASE}/ping').respond(200)
    response = world.client.post('/user/settings/connections/fake/connect',
                                 data={'csrf_token': world.token, 'variant': variant})
    assert response.status_code == 302 and response.location.startswith(f'{BASE}/authorize?state=')
    return response.location.split('state=')[1]


def test_connecting_one_account(world, http_mock):
    state = _connect(world, http_mock)
    token_route = http_mock.post(f'{BASE}/token').respond(200, json={'accounts': [
        {'id': 'acct-9', 'name': '@new@fake', 'token': 'tok-9'}]})
    response = world.client.get(f'/user/settings/connections/fake/callback?state={state}&code=c1')
    assert response.status_code == 302 and response.location.endswith('/user/settings/connections')
    assert token_route.calls.last.request.read() == b'code=c1&verifier=v-123'
    stored = SocialConnection.query.filter_by(remote_id='acct-9').one()
    assert stored.remote_name == '@new@fake' and open_tokens(stored)['access'] == 'tok-9'
    with world.client.session_transaction() as session:
        assert 'connections_flow' not in session


def test_reconnecting_replaces_the_tokens(world, http_mock):
    world.connection.state, world.connection.last_error = 'reauth', 'HTTP 401'
    db.session.commit()
    state = _connect(world, http_mock)
    http_mock.post(f'{BASE}/token').respond(200, json={'accounts': [
        {'id': 'acct-1', 'name': '@me@fake', 'token': 'tok-new'}]})
    world.client.get(f'/user/settings/connections/fake/callback?state={state}&code=c1')
    db.session.expire_all()
    connection = db.session.get(SocialConnection, world.connection.id)
    assert (connection.state, connection.last_error, open_tokens(connection)['access']) == ('ok', None, 'tok-new')


def test_choosing_among_several_accounts(world, http_mock):
    state = _connect(world, http_mock, variant='')
    http_mock.post(f'{BASE}/token').respond(200, json={'accounts': [
        {'id': 'p1', 'name': 'Page one', 'token': 'secret-token-1'},
        {'id': 'p2', 'name': 'Page two', 'token': 'secret-token-2'}]})
    response = world.client.get(f'/user/settings/connections/fake/callback?state={state}&code=c1')
    assert response.location.endswith('/user/settings/connections/fake/choose')
    page = world.client.get('/user/settings/connections/fake/choose')
    assert b'Page one' in page.data and b'Page two' in page.data and b'secret-token' not in page.data
    assert world.client.post('/user/settings/connections/fake/choose',
                             data={'csrf_token': world.token, 'account': '7'}).status_code == 400
    world.client.post('/user/settings/connections/fake/choose', data={'csrf_token': world.token, 'account': '1'})
    assert SocialConnection.query.filter_by(remote_id='p2').one().remote_name == 'Page two'
    assert SocialConnection.query.filter_by(remote_id='p1').first() is None
    response = world.client.get('/user/settings/connections/fake/choose')   # the choice is used up
    assert response.location.endswith('/user/settings/connections')


@pytest.mark.parametrize('case', ['bad-state', 'refused', 'token-error', 'no-accounts', 'no-flow'])
def test_a_callback_that_does_not_connect(world, http_mock, case):
    state = 'nope' if case == 'no-flow' else _connect(world, http_mock)
    query = f'state={state}&code=c1'
    if case == 'bad-state':
        query = 'state=forged&code=c1'
    elif case == 'refused':
        query = f'state={state}&error=access_denied'
    elif case == 'token-error':
        http_mock.post(f'{BASE}/token').respond(400, json={'message': 'bad code'})
    elif case == 'no-accounts':
        http_mock.post(f'{BASE}/token').respond(200, json={'accounts': []})
    if case == 'no-flow':
        with world.client.session_transaction() as session:
            session.pop('connections_flow', None)
    response = world.client.get(f'/user/settings/connections/fake/callback?{query}')
    assert response.status_code == 302 and response.location.endswith('/user/settings/connections')
    assert SocialConnection.query.count() == 1


def test_connect_refusals(world, http_mock):
    assert world.client.post('/user/settings/connections/nowhere/connect',
                             data={'csrf_token': world.token}).status_code == 404
    assert world.client.post('/user/settings/connections/fake/connect',
                             data={'csrf_token': world.token, 'variant': 'other'}).status_code == 400
    http_mock.get(f'{BASE}/ping').respond(503)
    response = world.client.post('/user/settings/connections/fake/connect', data={'csrf_token': world.token})
    assert response.location.endswith('/user/settings/connections')


def test_a_flow_belongs_to_one_member_and_service(world, http_mock, app):
    state = _connect(world, http_mock)
    with world.client.session_transaction() as session:
        flow = unseal_with(KEY, session['connections_flow'])
        flow['user_id'] = 1
        session['connections_flow'] = seal_with(KEY, flow)
    response = world.client.get(f'/user/settings/connections/fake/callback?state={state}&code=c1')
    assert response.location.endswith('/user/settings/connections') and SocialConnection.query.count() == 1


def test_default_and_disconnect_buttons(world, http_mock, app, monkeypatch):
    world.client.post(f'/user/settings/connections/{world.connection.id}/default',
                      data={'csrf_token': world.token, 'default_on': '0'})
    db.session.expire_all()
    assert db.session.get(SocialConnection, world.connection.id).default_on is False
    assert world.client.post('/user/settings/connections/999999/default',
                             data={'csrf_token': world.token}).status_code == 404
    revoke = http_mock.post(f'{BASE}/revoke').respond(200)
    world.client.post(f'/user/settings/connections/{world.connection.id}/disconnect', data={'csrf_token': world.token})
    assert revoke.called and SocialConnection.query.count() == 0
    again = SocialConnection(user_id=world.author.id, service='fake', remote_id='x', remote_name='x',
                             sealed_tokens=seal_tokens({}))
    db.session.add(again)
    db.session.commit()
    monkeypatch.setitem(app.config, KEY, '')   # the key is gone: no revoke, but the member may still leave
    world.client.post(f'/user/settings/connections/{again.id}/disconnect', data={'csrf_token': world.token})
    assert SocialConnection.query.count() == 0


def test_the_retry_button(world, http_mock):
    http_mock.post(f'{BASE}/posts').respond(200, json=SENT)
    post = a_post(world)
    failed = a_row(world, post, state='failed')
    sent = SocialCrosspost(post_id=post.id, connection_id=None, service='fake', state='sent')
    db.session.add(sent)
    db.session.commit()
    assert world.client.post('/user/settings/crossposts/999999/retry',
                             data={'csrf_token': world.token}).status_code == 404
    assert world.client.post(f'/user/settings/crossposts/{sent.id}/retry',
                             data={'csrf_token': world.token}).status_code == 400
    response = world.client.post(f'/user/settings/crossposts/{failed.id}/retry', data={'csrf_token': world.token})
    assert response.location.endswith(f'/post/{post.id}') and fresh(failed).state == 'sent'
    world.connection.user_id = 1
    post.user_id = 1
    db.session.commit()
    assert world.client.post(f'/user/settings/crossposts/{failed.id}/retry',
                             data={'csrf_token': world.token}).status_code == 404


def test_the_author_sees_where_the_post_went(world, monkeypatch):
    monkeypatch.setitem(world.app.config, 'WTF_CSRF_ENABLED', True)   # the reply form renders form.csrf_token
    post = a_post(world)
    states = [('sent', {'remote_url': f'{BASE}/r1'}), ('sent', {}), ('waiting', {}), ('queued', {'last_error': 'HTTP 503'}),
              ('failed', {'last_error': 'HTTP 400: no'}), ('skipped', {'last_error': 'Skipped: NSFW'}),
              ('manual', {'last_error': 'Delete it by hand', 'remote_url': f'{BASE}/m'}), ('deleted', {}),
              ('unconfirmed', {'last_error': 'Fake may have posted it'})]
    for state, columns in states:
        db.session.add(SocialCrosspost(post_id=post.id, connection_id=world.connection.id if state == 'failed' else None,
                                       service='fake', state=state, **columns))
    db.session.commit()
    page = world.client.get(f'/post/{post.id}').data
    for text in (b'Your connected accounts', f'{BASE}/r1'.encode(), b'Waiting for this post', b'Working on it',
                 b'HTTP 400: no', b'Retry', b'Skipped: NSFW', b'Delete it by hand', b'Deleted',
                 b'Maybe posted: Fake may have posted it'):
        assert text in page, text
    post.user_id = 1   # someone else's post: one client per test, since g keeps its first user
    db.session.commit()
    assert b'Your connected accounts' not in world.client.get(f'/post/{post.id}').data


# --------------------------------------------------------------------------
# the admin page
# --------------------------------------------------------------------------

def test_the_admin_switches_services(world, app, monkeypatch):
    client, token = world.client, world.token   # one client per test: g keeps the first CSRF token
    login(client, db.session.get(User, 1))
    page = client.get('/admin/connections')
    assert page.status_code == 200 and b'Fake' in page.data and b'Switch off' in page.data
    client.post('/admin/connections', data={'csrf_token': token, 'service': 'fake', 'on': '0'})
    assert not enabled(world.service)
    client.post('/admin/connections', data={'csrf_token': token, 'service': 'fake', 'on': '1'})
    assert enabled(world.service)
    assert client.post('/admin/connections', data={'csrf_token': token, 'service': 'nowhere'}).status_code == 400
    world.service.has_credentials = False
    assert client.post('/admin/connections', data={'csrf_token': token, 'service': 'fake'}).status_code == 400
    assert b'Not set' in client.get('/admin/connections').data
    monkeypatch.setitem(app.config, KEY, '')
    assert b'Set CONNECTIONS_SECRETS_KEY first' in client.get('/admin/connections').data
    monkeypatch.setattr('app.admin.connections_routes.SERVICES', {})
    assert b'No services are installed.' in client.get('/admin/connections').data
