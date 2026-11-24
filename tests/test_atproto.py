"""ATProto P1 groundwork (spec 2026-11-23-atproto-federation-design.md §4, §5, §6, §12 P1).

The protocol layer both Bluesky variants share (app.atproto): the Session interface, a card for a post outside the
microblogs, deleting a record by its URI; the post form's rule that a microblog post ticks Bluesky (A5); and the
migration that gives connections their variant and identity, records and the cursor their tables.
"""
import importlib.util
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect, text

from app import db
from app.atproto import Session, records
from app.atproto.records import build_card, delete_record, goes_whole, web_url
from app.connections import Progress, Service, setting_name
from app.connections import bluesky
from app.connections.bluesky import OAuthSession
from app.connections.content import body_text
from app.connections.dispatch import record_choices
from app.constants import COMMUNITY_KIND_MICROBLOGS, POST_TYPE_ARTICLE
from app.models import AtprotoCursor, AtprotoRecord, SocialConnection, SocialCrosspost
from app.sealing import generate_key
from app.utils import set_setting
from tests.connections_fake import BASE
from tests.test_community_add_post import url
from tests.test_connections import a_post as a_db_post

PDS = 'https://pds.example'
DID = 'did:plc:alice123'
MIGRATION = Path(__file__).resolve().parent.parent / 'migrations' / 'versions' / 'a95a82e17b01_atproto_groundwork.py'
BLOB = {'$type': 'blob', 'ref': {'$link': 'bafk'}, 'mimeType': 'image/jpeg', 'size': 99}
MICROBLOGS = SimpleNamespace(kind=COMMUNITY_KIND_MICROBLOGS, is_local=lambda: True)
ELSEWHERE = SimpleNamespace(kind='community', is_local=lambda: True)


@pytest.fixture
def bsky(app, db_session, monkeypatch):
    for name, value in (('BLUESKY_OAUTH_KEY', bluesky.new_key_jwk()), ('CONNECTIONS_SECRETS_KEY', generate_key())):
        monkeypatch.setitem(app.config, name, value)
    monkeypatch.setattr(bluesky, '_nonces', {})
    set_setting(setting_name('bluesky'), True)
    return bluesky.SERVICE


def tokens() -> dict:
    return {'access_token': 'at-1', 'refresh_token': 'rt-1', 'expires_at': time.time() + 3600,
            'dpop_jwk': bluesky.new_key_jwk(), 'pds': PDS, 'issuer': PDS, 'token_endpoint': f'{PDS}/oauth/token',
            'revocation_endpoint': f'{PDS}/oauth/revoke', 'did': DID}


def a_post(**changes):
    post = SimpleNamespace(title='A long read', body='It is [here](https://x.example/y) now.',
                           ap_id='https://piefed.example/post/7', content_warning=None, nsfw=False, nsfl=False,
                           gallery_count=0, gallery=[], image_id=None, image=None, extensions=None, language=None,
                           type=POST_TYPE_ARTICLE, url=None, community=ELSEWHERE)
    for name, value in changes.items():
        setattr(post, name, value)
    return post


def _png(tmp_path) -> str:
    from PIL import Image
    path = tmp_path / 'p.png'
    Image.new('RGB', (40, 30), 'red').save(path)
    return str(path)


# --------------------------------------------------------------------------
# the Session interface
# --------------------------------------------------------------------------

def test_a_session_is_an_interface():
    with pytest.raises(NotImplementedError):
        Session().xrpc('GET', 'com.atproto.server.describeServer')
    assert (Session.did, Session.pds) == ('', '')


def test_an_oauth_connection_is_a_session(app, bsky, http_mock):
    route = http_mock.get(f'{PDS}/xrpc/com.atproto.server.getSession').respond(200, json={'did': DID})
    session = OAuthSession(tokens())
    assert isinstance(session, Session) and (session.did, session.pds) == (DID, PDS)
    assert session.xrpc('GET', 'com.atproto.server.getSession').json() == {'did': DID}
    assert route.calls.last.request.headers['Authorization'] == 'DPoP at-1'


# --------------------------------------------------------------------------
# which shape a post takes (A5)
# --------------------------------------------------------------------------

def test_only_a_local_microblog_post_goes_whole():
    assert goes_whole(a_post(community=MICROBLOGS))
    assert not goes_whole(a_post())
    assert not goes_whole(a_post(community=SimpleNamespace(kind=COMMUNITY_KIND_MICROBLOGS, is_local=lambda: False)))
    assert not goes_whole(a_post(community=None))
    assert not goes_whole(SimpleNamespace())


def test_the_body_alone_as_text():
    assert body_text(a_post()) == 'It is here https://x.example/y now.'
    assert body_text(a_post(body=None)) == ''


def test_a_card_has_the_title_and_links_back_with_the_first_picture(app, bsky, http_mock, tmp_path):
    upload = http_mock.post(f'{PDS}/xrpc/com.atproto.repo.uploadBlob').respond(200, json={'blob': BLOB})
    files = [SimpleNamespace(file_path=_png(tmp_path), source_url=None, alt_text='one'),
             SimpleNamespace(file_path=_png(tmp_path), source_url=None, alt_text='two')]
    post = a_post(gallery_count=2, gallery=files, language=SimpleNamespace(code='en'), nsfw=True)
    with app.test_request_context():
        record = build_card(post, OAuthSession(tokens()))
    assert upload.call_count == 1   # the thumbnail only
    assert record['text'] == 'A long read' and record['createdAt'].endswith('Z')
    assert record['embed'] == {'$type': 'app.bsky.embed.external', 'external': {
        'uri': 'https://piefed.example/post/7', 'title': 'A long read',
        'description': 'It is here https://x.example/y now.', 'thumb': BLOB}}
    assert record['langs'] == ['en'] and record['labels']['values'] == [{'val': 'sexual'}]
    assert 'facets' not in record


def test_a_card_without_a_usable_picture_or_title(app, bsky, http_mock):
    unreadable = SimpleNamespace(file_path=None, source_url='https://img.example/gone.png', alt_text='')
    http_mock.get('https://img.example/gone.png').respond(404)
    long = '#tag ' + 'w' * 400
    with app.test_request_context():
        record = build_card(a_post(title=long, body='', image_id=1, image=unreadable), OAuthSession(tokens()))
        untitled = build_card(a_post(title=None, body=''), OAuthSession(tokens()))
    external = record['embed']['external']
    assert 'thumb' not in external and len(record['text']) == 300 and len(external['title']) == 300
    assert record['facets'][0]['features'][0] == {'$type': 'app.bsky.richtext.facet#tag', 'tag': 'tag'}
    assert untitled['text'] == '' and untitled['embed']['external']['title'] == ''


def test_a_card_behind_a_warning_shows_only_the_warning(app, bsky, tmp_path):
    picture = SimpleNamespace(file_path=_png(tmp_path), source_url=None, alt_text='')
    post = a_post(content_warning='Spoilers', image_id=1, image=picture, nsfl=True)
    with app.test_request_context():
        record = build_card(post, OAuthSession(tokens()))   # no upload: respx refuses one
    assert record['text'] == 'CW: Spoilers'
    assert record['embed']['external'] == {'uri': 'https://piefed.example/post/7', 'title': 'A long read',
                                           'description': ''}
    assert record['labels']['values'] == [{'val': 'graphic-media'}]


def test_a_post_elsewhere_is_sent_as_a_card(app, bsky, http_mock):
    create = http_mock.post(f'{PDS}/xrpc/com.atproto.repo.createRecord').respond(
        200, json={'uri': f'at://{DID}/app.bsky.feed.post/3kc'})
    video = a_post(extensions={'media': {'mime': 'video/mp4'}})
    progress = Progress()
    with app.test_request_context():
        sent = bsky.send(video, tokens(), None, progress)
    record = json.loads(create.calls.last.request.read())['record']
    assert record['embed']['$type'] == 'app.bsky.embed.external' and 'video' not in progress.data
    assert sent.remote_url == f'https://bsky.app/profile/{DID}/post/3kc'


def test_a_record_is_deleted_by_its_uri(app, bsky, http_mock):
    delete = http_mock.post(f'{PDS}/xrpc/com.atproto.repo.deleteRecord').respond(200, json={})
    with app.test_request_context():
        delete_record(OAuthSession(tokens()), f'at://{DID}/app.bsky.feed.like/3kl')
    assert json.loads(delete.calls.last.request.read()) == {'repo': DID, 'collection': 'app.bsky.feed.like',
                                                           'rkey': '3kl'}
    assert web_url(f'at://{DID}/app.bsky.feed.post/3kp') == f'https://bsky.app/profile/{DID}/post/3kp'


def test_the_services_defaults(app, bsky):
    assert records.POST == 'app.bsky.feed.post'
    assert bsky.default_on is False and bsky.ticks_microblog is True
    assert Service.default_on is True and Service.ticks_microblog is False


# --------------------------------------------------------------------------
# ticking on the post form (A5)
# --------------------------------------------------------------------------

def test_what_is_ticked_for_a_new_post(bsky):
    quiet, keen = SimpleNamespace(default_on=False), SimpleNamespace(default_on=True)
    assert bsky.ticked(quiet, MICROBLOGS) and not bsky.ticked(quiet, ELSEWHERE) and not bsky.ticked(quiet, None)
    assert bsky.ticked(keen, ELSEWHERE)
    plain = Service()
    assert not plain.ticked(quiet, MICROBLOGS) and plain.ticked(keen, None)


def test_a_microblog_post_ticks_a_service_that_asks_for_it(connections_world, monkeypatch, http_mock):
    monkeypatch.setitem(connections_world.app.config, 'WTF_CSRF_ENABLED', True)   # the form renders form.csrf_token
    connections_world.connection.default_on = False
    db.session.commit()
    monkeypatch.setattr(connections_world.service, 'ticks_microblog', True, raising=False)
    elsewhere = a_db_post(connections_world)
    record_choices(elsewhere, None)
    connections_world.community.kind = COMMUNITY_KIND_MICROBLOGS
    db.session.commit()
    microblog = a_db_post(connections_world)
    record_choices(microblog, None)
    db.session.commit()
    assert SocialCrosspost.query.filter_by(post_id=elsewhere.id).count() == 0
    assert SocialCrosspost.query.filter_by(post_id=microblog.id).one().state == 'waiting'

    page = connections_world.client.get(url(connections_world.app, 'community.add_post',
                                            actor=connections_world.community.name, type='discussion'))
    connection_id = connections_world.connection.id
    assert re.search(rb'value="%d"\s+id="crosspost_to_%d" checked' % (connection_id, connection_id), page.data)


def test_a_post_elsewhere_is_not_ticked_unless_asked(connections_world, monkeypatch):
    monkeypatch.setitem(connections_world.app.config, 'WTF_CSRF_ENABLED', True)   # the form renders form.csrf_token
    connections_world.connection.default_on = False
    db.session.commit()
    monkeypatch.setattr(connections_world.service, 'ticks_microblog', True, raising=False)
    page = connections_world.client.get(url(connections_world.app, 'community.add_post',
                                            actor=connections_world.community.name, type='discussion'))
    connection_id = connections_world.connection.id
    assert b'Also post to' in page.data
    assert not re.search(rb'value="%d"\s+id="crosspost_to_%d" checked' % (connection_id, connection_id), page.data)


def test_a_new_connection_takes_its_services_default(connections_world, http_mock, monkeypatch):
    monkeypatch.setattr(connections_world.service, 'default_on', False, raising=False)
    http_mock.get(f'{BASE}/ping').respond(200)
    response = connections_world.client.post('/user/settings/connections/fake/connect',
                                             data={'csrf_token': connections_world.token, 'variant': ''})
    state = response.location.split('state=')[1]
    http_mock.post(f'{BASE}/token').respond(200, json={'accounts': [{'id': 'acct-9', 'name': '@n', 'token': 't'}]})
    connections_world.client.get(f'/user/settings/connections/fake/callback?state={state}&code=c1')
    assert SocialConnection.query.filter_by(remote_id='acct-9').one().default_on is False
    assert SocialConnection.query.filter_by(remote_id='acct-1').one().default_on is True   # reconnecting keeps it


def test_the_settings_page_says_what_bluesky_ticks(connections_world, monkeypatch):
    monkeypatch.setattr(connections_world.service, 'ticks_microblog', True, raising=False)
    page = connections_world.client.get('/user/settings/connections').data
    assert b'Stop ticking it on posts outside my microblog' in page
    assert b'Microblog posts are always ticked' in page
    connections_world.connection.default_on = False
    db.session.commit()
    assert b'Tick it on posts outside my microblog too' in connections_world.client.get('/user/settings/connections').data
    monkeypatch.setattr(connections_world.service, 'ticks_microblog', False, raising=False)
    page = connections_world.client.get('/user/settings/connections').data
    assert b'Tick it on new posts' in page and b'Microblog posts are always ticked' not in page


# --------------------------------------------------------------------------
# the tables and the migration (§5)
# --------------------------------------------------------------------------

def test_the_new_columns_and_tables(connections_world):
    connection = connections_world.connection
    assert (connection.is_identity, connection.import_own, connection.slot_state) == (False, False, None)
    post = a_db_post(connections_world)
    assert post.requires_login is False
    record = AtprotoRecord(uri=f'at://{DID}/app.bsky.feed.like/1', did=DID, collection='app.bsky.feed.like',
                           rkey='1', direction='out', connection_id=connection.id, post_id=post.id)
    db.session.add_all([record, AtprotoCursor(seq=42)])
    db.session.commit()
    assert record.created_at is not None and AtprotoCursor.query.one().seq == 42
    db.session.add(AtprotoRecord(uri=record.uri, did=DID, collection='c', rkey='1', direction='in'))
    with pytest.raises(Exception):
        db.session.commit()   # one row per URI
    db.session.rollback()


def test_a_member_has_one_identity(connections_world):
    connections_world.connection.is_identity = True
    db.session.add(SocialConnection(user_id=connections_world.author.id, service='fake', remote_id='acct-2',
                                    remote_name='two', sealed_tokens='x', is_identity=False))
    db.session.commit()
    db.session.add(SocialConnection(user_id=connections_world.author.id, service='fake', remote_id='acct-3',
                                    remote_name='three', sealed_tokens='x', is_identity=True))
    with pytest.raises(Exception):
        db.session.commit()
    db.session.rollback()


def _migration():
    spec = importlib.util.spec_from_file_location('atproto_groundwork_migration', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_revision_chain():
    migration = _migration()
    assert (migration.revision, migration.down_revision) == ('a95a82e17b01', 'f3b7d1a9c5e2')


@pytest.mark.serial   # it alters the post table every worker reads (run_tests.sh runs these alone)
def test_the_migration_reverses_and_reapplies(connections_world):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration = _migration()
    author = connections_world.author.id
    post = a_db_post(connections_world)
    for remote_id, variant in (('o1', 'oauth'), ('h1', 'hosted')):
        db.session.add(SocialConnection(user_id=author, service='bluesky', remote_id=remote_id, remote_name=remote_id,
                                        sealed_tokens='x', variant=variant))
    db.session.commit()
    post_id, oauth_id = post.id, SocialConnection.query.filter_by(remote_id='o1').one().id
    db.session.add(SocialCrosspost(post_id=post_id, connection_id=oauth_id, service='bluesky', variant='oauth',
                                   state='sent'))
    db.session.commit()
    db.session.close()   # no lock of this session's on the tables the migration alters

    def columns(connection, table):
        return {found['name'] for found in inspect(connection).get_columns(table)}

    with db.engine.connect() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            assert 'is_identity' not in columns(connection, 'social_connection')
            assert 'requires_login' not in columns(connection, 'post')
            assert not inspect(connection).has_table('atproto_record')
            variants = connection.execute(text(
                "SELECT remote_id, variant FROM social_connection WHERE service = 'bluesky'")).all()
            assert variants == [('o1', None)]   # a hosted account goes with its columns
            assert connection.execute(text("SELECT variant FROM social_crosspost WHERE service = 'bluesky'")) \
                .scalar() is None
            migration.upgrade()
        assert {'is_identity', 'import_own', 'slot_state'} <= columns(connection, 'social_connection')
        assert inspect(connection).has_table('atproto_record') and inspect(connection).has_table('atproto_cursor')
        assert connection.execute(text(
            "SELECT variant, is_identity, import_own FROM social_connection WHERE remote_id = 'o1'")).one() == \
            ('oauth', False, False)
        assert connection.execute(text("SELECT variant FROM social_crosspost WHERE service = 'bluesky'")) \
            .scalar() == 'oauth'
        assert connection.execute(text('SELECT requires_login FROM post WHERE id = :id'), {'id': post_id}) \
            .scalar() is False
        connection.rollback()

