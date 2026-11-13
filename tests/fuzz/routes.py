"""Every external-facing route, fuzzed through Flask (spec docs/superpowers/specs/2026-11-13-fuzz-every-route-design.md).

A family is a set of blueprints (a rule's blueprint is its endpoint's prefix; `static` is the app's own static route).
Every rule in `app.url_map` belongs to exactly one family, and every family has an atheris target, `routes_<family>`
(tests/fuzz/targets/routes.py), driven by tests/fuzz/run_campaign.py like every other target:
tests/test_fuzz_routes_registry.py fails when a rule has no family or no seed.

One input is one request. Its first field names the rule by its key (`<endpoint> <rule>`), so a committed seed stays on
its route whatever routes are added; a key that names no rule picks one by its hash, so a mutated input still lands on
some rule of the family. The rest of the input chooses the method, who asks (FR2), a value for each part of the path,
the query string, the body and some headers.

The request runs against a seeded world (`prepare`), restored from its snapshot before every input, so each meets the
same state; nothing leaves the process (outbound connections are refused while it runs), and Celery runs inline, as the
suite has it. The oracle (FR1): an unhandled exception or a 5xx other than 502-504, a request slower than its budget,
and for an API with an error format of its own a 4xx in another shape.
"""
from __future__ import annotations

import base64
import re
import socket
import time
import urllib.parse
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from io import BytesIO

from flask import current_app, g, session as flask_session
from flask_wtf.csrf import generate_csrf
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app import db
import app as app_package   # the module, not the name: redis_client is set when the app is built
from app.constants import POST_TYPE_VIDEO
from app.oauth_server import tokens
from app.utils import set_setting
from tests.conftest import _teardown_sql
from tests.factories import (grant_permissions, make_community, make_community_member, make_instance, make_post,
                             make_post_reply, make_site, make_user)

# family -> the blueprints whose rules it fuzzes
FAMILIES: dict[str, frozenset[str]] = {
    'auth': frozenset({'auth'}),
    'oauth': frozenset({'oauth_server'}),
    'gpodder': frozenset({'gpodder'}),
    'activitypub': frozenset({'activitypub'}),
    'static': frozenset({'static', 'bootstrap', 'onion_media'}),
    'alpha': frozenset({'api_alpha', 'Admin', 'Comment', 'Community', 'Feed', 'Misc', 'Post', 'Private Message', 'Site',
                        'Topic', 'Upload', 'User'}),
    'mastodon': frozenset({'mastodon'}),
    'peertube': frozenset({'peertube'}),
    'lemmy': frozenset({'lemmy'}),
    'web': frozenset({'main', 'post', 'community', 'user', 'feed', 'chat', 'search', 'tag', 'domain', 'topic',
                      'instance'}),
    'admin': frozenset({'admin'}),
    'dev': frozenset({'dev'}),
    'payments': frozenset({'payments'}),
}
IGNORED_METHODS = {'HEAD', 'OPTIONS'}
# A request slower than this, in wall-clock time on a machine running at nominal speed, is a finding (FR1)
REQUEST_BUDGET_MS = 4000
UPSTREAM_STATUSES = {502, 503, 504}   # an upstream refused or down: deliberate, not a finding


def blueprint_of(endpoint: str) -> str:
    """A rule's blueprint: its endpoint's prefix, or the app itself (`static`) for an endpoint with none."""
    return endpoint.rsplit('.', 1)[0] if '.' in endpoint else endpoint


def family_of(endpoint: str) -> str | None:
    found = [name for name, blueprints in FAMILIES.items() if blueprint_of(endpoint) in blueprints]
    return found[0] if len(found) == 1 else None


def rule_key(rule) -> str:
    return f'{rule.endpoint} {rule.rule}'


def rules_of(app, family: str) -> list:
    """The family's rules, in their keys' order: the same list on every run."""
    return sorted((rule for rule in app.url_map.iter_rules() if family_of(rule.endpoint) == family), key=rule_key)


# ---------------------------------------------------------------------------------------------------------------------
# The input

class Draw:
    """The input read as a series of choices. Past its end every read is zero or empty, so any input is a request."""

    def __init__(self, data: bytes):
        self.data, self.at = data, 0

    def byte(self) -> int:
        value = self.data[self.at] if self.at < len(self.data) else 0
        self.at += 1
        return value

    def below(self, bound: int) -> int:
        return (self.byte() << 8 | self.byte()) % bound if bound > 1 else 0

    def choose(self, options):
        return options[self.below(len(options))]

    def chunk(self, limit: int = 2048) -> bytes:
        size = min(self.byte() << 8 | self.byte(), limit)
        found = self.data[self.at:self.at + size]
        self.at += size
        return found

    def text(self, limit: int = 2048) -> str:
        return self.chunk(limit).decode('utf-8', errors='replace')


def field_of(text: str) -> bytes:
    """`text` as one length-prefixed field: what `Draw.text` reads back."""
    raw = text.encode()
    return bytes([len(raw) >> 8 & 0xFF, len(raw) & 0xFF]) + raw


def seed(key: str, *choices: int) -> bytes:
    """An input naming the rule `key`, then the given choices (each two bytes, as `Draw.below` reads them)."""
    return field_of(key) + b''.join(bytes([choice >> 8 & 0xFF, choice & 0xFF]) for choice in choices)


# ---------------------------------------------------------------------------------------------------------------------
# The world every input meets

@dataclass
class World:
    ids: dict[str, list[int]] = field(default_factory=dict)       # path values by kind: ints
    names: dict[str, list[str]] = field(default_factory=dict)     # and text
    users: dict[str, int] = field(default_factory=dict)           # role -> user id
    jwts: dict[str, str] = field(default_factory=dict)            # role -> alpha API token
    bearers: dict[str, str] = field(default_factory=dict)         # credential name -> OAuth access token
    passwords: dict[str, tuple[str, str]] = field(default_factory=dict)   # gpodder: role -> (user name, app password)


_STATE: dict = {}
SNAPSHOT = 'fuzz_routes_snapshot'
ROLES = ('member', 'moderator', 'admin', 'banned')


def _seed_world():
    """The records the routes are asked about: an admin, a member, a moderator and a banned account, a community with a
    post, a reply and a video, a remote account and community, and every credential the APIs take."""

    make_site()
    local = make_instance('test.piefed.local', software='piefed')
    remote = make_instance('remote.example', software='mastodon')
    admin = make_user(local, 'fuzzadmin', local=True, with_keys=True)   # user 1, who passes every permission check
    member = make_user(local, 'fuzzmember', local=True, with_keys=True)
    moderator = make_user(local, 'fuzzmod', local=True, with_keys=True)
    banned = make_user(local, 'fuzzbanned', local=True)
    stranger = make_user(remote, 'stranger')
    for user in (admin, member, moderator, banned):
        user.verified = True
    banned.banned = True
    community = make_community('fuzzcomm')
    remote_community = make_community('farcomm', host='remote.example')
    db.session.commit()
    make_community_member(member, community)
    make_community_member(moderator, community, is_moderator=True)
    post = make_post(community, member, ap_id='https://test.piefed.local/post/fuzz1', title='Fuzz post')
    video = make_post(community, member, ap_id='https://test.piefed.local/post/fuzz2', title='Fuzz video')
    video.type = POST_TYPE_VIDEO
    reply = make_post_reply(post, moderator, body='Fuzz reply')
    grant_permissions(admin, 'change instance settings')
    set_setting('allow_video_file_uploads', 'users')
    db.session.commit()

    world = World()
    world.ids = {'user': [member.id, moderator.id, banned.id, stranger.id, admin.id],
                 'community': [community.id, remote_community.id], 'post': [post.id, video.id],
                 'reply': [reply.id], 'instance': [local.id, remote.id]}
    world.names = {'user': [member.user_name, moderator.user_name, f'{stranger.user_name}@remote.example'],
                   'community': [community.name, f'{remote_community.name}@remote.example'],
                   'domain': ['test.piefed.local', 'remote.example']}
    world.users = {'member': member.id, 'moderator': moderator.id, 'admin': admin.id, 'banned': banned.id}
    for role, user in (('member', member), ('moderator', moderator), ('admin', admin), ('banned', banned)):
        world.jwts[role] = user.encode_jwt_token()
    registered, _secret = tokens.register_app('fuzz', ['urn:ietf:wg:oauth:2.0:oob'], 'read write follow push', None)
    for scope in ('read', 'write', 'read write follow push'):
        world.bearers[f'oauth {scope}'] = tokens.issue_token(registered, member, scope)[0]
    world.bearers['oauth admin'] = tokens.issue_token(registered, admin, 'read write follow push admin:read admin:write')[0]
    local_client, _secret = tokens.local_client()
    world.bearers['peertube'] = tokens.issue_token(local_client, member, tokens.LOCAL_CLIENT_SCOPE)[0]
    world.passwords['member'] = (member.user_name, tokens.issue_app_password(member, 'fuzz gpodder'))
    db.session.commit()
    return world


def _tables(connection) -> list[str]:
    return [row[0] for row in connection.execute(text(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"))]


def _empty_every_table() -> None:
    db.session.rollback()
    with db.engine.begin() as connection:
        connection.exec_driver_sql(_teardown_sql(db))


def _take_snapshot() -> None:
    with db.engine.begin() as connection:
        connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS {SNAPSHOT} CASCADE')
        connection.exec_driver_sql(f'CREATE SCHEMA {SNAPSHOT}')
        for table in _tables(connection):
            connection.exec_driver_sql(f'CREATE TABLE {SNAPSHOT}."{table}" AS TABLE public."{table}"')
        connection.exec_driver_sql(
            f'CREATE TABLE {SNAPSHOT}._sequences AS SELECT sequencename AS name, last_value AS value '
            f"FROM pg_sequences WHERE schemaname = 'public'")


def restore() -> None:
    """Every table as the snapshot holds it, in one transaction with triggers off; flask.g and Redis's counters
    cleared, as the suite's db_session does between tests."""
    db.session.rollback()
    db.session.remove()
    with db.engine.begin() as connection:
        connection.exec_driver_sql('SET LOCAL session_replication_role = replica')
        tables = _tables(connection)
        connection.exec_driver_sql('DELETE FROM ' + '; DELETE FROM '.join(f'public."{table}"' for table in tables))
        for table in tables:
            columns = [row[0] for row in connection.exec_driver_sql(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = "
                f"'{table}' AND is_generated = 'NEVER' ORDER BY ordinal_position")]
            names = ', '.join(f'"{column}"' for column in columns)
            connection.exec_driver_sql(f'INSERT INTO public."{table}" ({names}) OVERRIDING SYSTEM VALUE '
                                       f'SELECT {names} FROM {SNAPSHOT}."{table}"')
        for name, value in connection.exec_driver_sql(f'SELECT name, value FROM {SNAPSHOT}._sequences').all():
            connection.exec_driver_sql(f"SELECT setval('public.\"{name}\"', {value or 1}, {value is not None})")
    g.__dict__.clear()
    redis_client = app_package.redis_client
    if redis_client is not None:
        stale = [key for pattern in ('votes_cast_*', 'honeypot:*', 'ban:*', 'gpodder_login_fail:*', '*limiter*')
                 for key in redis_client.keys(pattern)]
        if stale:
            redis_client.delete(*stale)


def prepare(force: bool = False) -> World:
    """The world, seeded once per process (or again with `force`): every table emptied, the world written, and the
    snapshot every input is restored from taken."""
    if force or 'world' not in _STATE:
        _empty_every_table()
        _STATE['world'] = _seed_world()
        _take_snapshot()
        _STATE['clean'] = False   # the session the seeding used is still open: restore before the first request
    return _STATE['world']


# ---------------------------------------------------------------------------------------------------------------------
# Nothing leaves the process

@contextmanager
def _offline(app):
    """Outbound connections refused while a request runs: only the database and Redis are reachable."""
    allowed = {'localhost', '127.0.0.1', '::1'}
    for setting in ('SQLALCHEMY_DATABASE_URI', 'CACHE_REDIS_URL'):
        value = app.config.get(setting)
        if value:
            host = make_url(value).host if setting == 'SQLALCHEMY_DATABASE_URI' else urllib.parse.urlsplit(value).hostname
            if host:
                allowed.add(host)
                with _quiet():
                    allowed.update(info[4][0] for info in socket.getaddrinfo(host, None))
    real_connect, real_lookup = socket.socket.connect, socket.getaddrinfo

    def connect(sock, address):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(address, tuple) and host not in allowed:
            raise ConnectionRefusedError(f'fuzzing: no connection to {host}')
        return real_connect(sock, address)

    def lookup(host, *args, **kwargs):
        if host not in allowed:
            raise socket.gaierror(f'fuzzing: no lookup of {host}')
        return real_lookup(host, *args, **kwargs)
    socket.socket.connect, socket.getaddrinfo = connect, lookup
    try:
        yield
    finally:
        socket.socket.connect, socket.getaddrinfo = real_connect, real_lookup


@contextmanager
def _quiet():
    try:
        yield
    except OSError:
        pass


# ---------------------------------------------------------------------------------------------------------------------
# Building the request

IDENTITIES = ('nobody', 'session member', 'session member csrf', 'session moderator csrf', 'session admin csrf',
              'session banned csrf', 'jwt member', 'jwt admin', 'jwt banned', 'oauth read', 'oauth write',
              'oauth read write follow push', 'oauth admin', 'peertube', 'gpodder member', 'onion')
USER_AGENTS = ('Mozilla/5.0', 'Tusky/30.0', 'Jerboa', 'PixelDroid', 'P2play/0.5.3', 'AntennaPod/3.0',
               'http.rb/5.0 (Mastodon/4.3.0; +https://remote.example/)', '')
ACCEPTS = ('*/*', 'text/html', 'application/json', 'application/activity+json',
           'application/ld+json; profile="https://www.w3.org/ns/activitystreams"', 'application/xml', '')
WILD = ('', '0', '-1', '99999999999999999999', '..', '../../etc/passwd', '%00', 'null', ' ', 'é', '‮',
        'a' * 300, '{}', '[]', '<script>', "'", '1.5', 'true')


def _value(draw: Draw, world: World, name: str, converter: str):
    """A path value for the argument `name`: a seeded record's (by the name's kind), a wild one, or the input's own."""
    mode = draw.below(4)
    kind = next((key for key in ('reply', 'post', 'community', 'user', 'instance', 'domain') if key in name), None)
    if mode < 2 and converter in ('IntegerConverter', 'NumberConverter') :
        pool = world.ids.get(kind) or [id_ for ids in world.ids.values() for id_ in ids]
        return draw.choose(pool)
    if mode < 2:
        pool = world.names.get(kind) or [name_ for names in world.names.values() for name_ in names]
        ids = world.ids.get(kind) or []
        return str(draw.choose(pool + [str(id_) for id_ in ids]))
    if mode == 2:
        return draw.choose(WILD)
    return draw.text(512)


def _json_value(draw: Draw, depth: int = 0):
    kind = draw.below(7 if depth < 3 else 4)
    if kind == 0:
        return draw.text(256)
    if kind == 1:
        return draw.choose((0, -1, 1, 2 ** 31, 2 ** 63, 1.5))
    if kind == 2:
        return draw.choose((True, False))
    if kind == 3:
        return None
    if kind in (4, 5):
        return {draw.text(64): _json_value(draw, depth + 1) for _ in range(draw.below(5))}
    return [_json_value(draw, depth + 1) for _ in range(draw.below(5))]


@dataclass
class Request:
    rule: object
    method: str
    path: str
    identity: str
    query: list
    body: dict
    headers: dict


def decode(app, family: str, data: bytes, world: World) -> Request:
    """The request `data` asks for, on one of `family`'s rules."""
    draw = Draw(data)
    rules = rules_of(app, family)
    key = draw.text(1024)
    by_key = {rule_key(rule): rule for rule in rules}
    rule = by_key.get(key) or rules[zlib.crc32(key.encode()) % len(rules)]
    method = draw.choose(sorted(rule.methods - IGNORED_METHODS) or ['GET'])
    identity = draw.choose(IDENTITIES)
    values = {name: _value(draw, world, name, type(converter).__name__)
              for name, converter in sorted(rule._converters.items())}
    path = _path(app, rule, method, values)
    query = [(draw.text(64), draw.text(256)) for _ in range(draw.below(5))]
    body = _body(draw)
    headers = {'Accept': draw.choose(ACCEPTS), 'User-Agent': draw.choose(USER_AGENTS)}
    if draw.below(4) == 0:
        headers['Signature'] = header_value(draw.text(512))
    return Request(rule, method, path, identity, query, body, headers)


def _path(app, rule, method: str, values: dict) -> str:
    """The rule's path with `values`, through the router when they fit its converters, else written in by hand."""
    try:
        return app.url_map.bind('test.piefed.local').build(rule.endpoint, values, method=method,
                                                          append_unknown=False)
    except Exception:
        def fill(match):
            name = match.group(2)
            safe = '/' if match.group(1) == 'path' else ''
            return urllib.parse.quote(str(values.get(name, '')), safe=safe)
        return re.sub(r'<(?:(\w+)(?:\([^)]*\))?:)?(\w+)>', fill, rule.rule)


def header_value(text: str) -> str:
    """`text` as a header can carry it: no CR, LF or NUL, which no HTTP request can hold in a value (the test client
    refuses to send one, and that refusal is not a finding)."""
    return re.sub(r'[\r\n\x00]', ' ', text)


def _body(draw: Draw) -> dict:
    kind = draw.below(5)
    if kind == 0:
        return {}
    if kind == 1:
        return {'data': {draw.text(64): draw.text(512) for _ in range(draw.below(6))}}
    if kind == 2:
        return {'json': _json_value(draw)}
    if kind == 3:
        fields = {draw.text(64): draw.text(256) for _ in range(draw.below(4))}
        fields[draw.choose(('file', 'image', 'upload', 'avatar', 'header'))] = (BytesIO(draw.chunk(4096)),
                                                                                  draw.text(64) or 'f.png')
        return {'data': fields, 'content_type': 'multipart/form-data'}
    return {'data': draw.chunk(4096), 'content_type': draw.text(128) or 'application/octet-stream'}


def _sign_in(app, client, world: World, identity: str, request: Request) -> None:
    """Give the request `identity`'s credentials."""
    words = identity.split(' ')
    if words[0] == 'session':
        with client.session_transaction() as session:
            session['_user_id'] = str(world.users[words[1]])
            session['_fresh'] = True
        if 'csrf' in words:
            with app.test_request_context():
                token = generate_csrf()
                raw = flask_session['csrf_token']
            with client.session_transaction() as session:
                session['csrf_token'] = raw
            request.headers['X-CSRFToken'] = token
            if isinstance(request.body.get('data'), dict):
                request.body['data']['csrf_token'] = token
    elif words[0] == 'jwt':
        request.headers['Authorization'] = f'Bearer {world.jwts[words[1]]}'
    elif words[0] in ('oauth', 'peertube'):
        request.headers['Authorization'] = f'Bearer {world.bearers[identity]}'
    elif words[0] == 'gpodder':
        name, password = world.passwords[words[1]]
        request.headers['Authorization'] = 'Basic ' + base64.b64encode(f'{name}:{password}'.encode()).decode()
    elif words[0] == 'onion':
        request.headers.update({'X-PieFed-Via': 'onion', 'X-Forwarded-For': '10.89.0.7'})


# ---------------------------------------------------------------------------------------------------------------------
# The oracle

def _mastodon_shape(body) -> bool:
    return isinstance(body, dict) and isinstance(body.get('error'), str)


def _peertube_shape(body) -> bool:
    return isinstance(body, dict) and isinstance(body.get('status'), int) and isinstance(
        body.get('title') or body.get('detail'), str)


def _alpha_shape(body) -> bool:
    return isinstance(body, dict) and 'code' in body and isinstance(body.get('message'), str)


# family -> what its 4xx bodies are (FR1)
ERROR_SHAPES = {'mastodon': _mastodon_shape, 'lemmy': _mastodon_shape, 'oauth': _mastodon_shape,
                'peertube': _peertube_shape, 'alpha': _alpha_shape}


def check(family: str, data: bytes) -> tuple[str, float]:
    """The property every route holds, for the request `data` asks for: what was asked and how long the request
    took in milliseconds, which the target holds to REQUEST_BUDGET_MS (tests/fuzz/targets/routes.py)."""
    app = current_app._get_current_object()
    world = prepare()
    if not _STATE.get('clean'):   # the last input's restore is this one's: only the first, or after a failed one
        restore()
    _STATE['clean'] = False
    request = decode(app, family, data, world)
    client = app.test_client()
    _sign_in(app, client, world, request.identity, request)
    label = f'{request.method} {request.path} as {request.identity} ({rule_key(request.rule)})'
    started = time.monotonic()
    csrf_setting = app.config.get('WTF_CSRF_ENABLED')
    app.config['WTF_CSRF_ENABLED'] = True   # production's forms: the suite's config turns their CSRF field off
    try:
        with _offline(app):
            response = client.open(request.path, method=request.method, query_string=request.query,
                                   headers=request.headers, base_url='https://test.piefed.local', **request.body)
    except Exception as error:   # TESTING propagates a view's exception: the 500 a visitor would have got
        raise AssertionError(f'{label}: raised {error!r}') from error
    finally:
        app.config['WTF_CSRF_ENABLED'] = csrf_setting
        restore()
        _STATE['clean'] = True
    elapsed_ms = (time.monotonic() - started) * 1000
    status = response.status_code
    assert not (status >= 500 and status not in UPSTREAM_STATUSES), f'{label}: answered {status}'
    shape = ERROR_SHAPES.get(family)
    if shape is not None and 400 <= status < 500:
        body = response.get_json(silent=True)
        assert shape(body), f'{label}: a {status} not in {family}\'s error format: {response.get_data()[:200]!r}'
    return label, elapsed_ms


def seeds_for(app, family: str) -> dict[str, bytes]:
    """One seed per rule of `family`: its key, its first method, nobody asking, seeded path values, nothing else.
    Named by the key's checksum, so each rule's seed keeps its file."""
    found = {}
    for rule in rules_of(app, family):
        name = f'rule-{zlib.crc32(rule_key(rule).encode()):08x}'
        found[name] = seed(rule_key(rule), 0, 0) + b'\x00\x00' * len(rule._converters)
    return found
