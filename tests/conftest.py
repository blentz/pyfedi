import json
import os
import re

import boto3
import fakeredis
import httpx
import pytest
import respx
from moto import mock_aws
from pyld import jsonld
from werkzeug.http import http_date

# Import app before config. config.py does `import app.constants`, which starts
# loading the app package; app/__init__.py in turn does `from config import
# Config` before Config is defined if config.py is the entry point, causing a
# circular ImportError. Every other test module in this repo sidesteps this by
# importing something from app.* first (e.g. `from app.utils import ...`); do
# the same here so `from config import Config` below sees a fully-initialised
# config module.
import app  # noqa: F401

# app.community.routes and app.activitypub.routes import from each other
# (community.routes needs RsaKeys/send_post_request from activitypub.signature;
# activitypub.routes needs show_community from community.routes). Whichever
# package's __init__.py starts running first finishes cleanly; if
# app.community starts first, it is still mid-import (show_community not yet
# defined) when app.activitypub.routes asks for it, raising a circular
# ImportError. Priming app.activitypub here, before pytest collects any test
# module, fixes the winning order once for the whole session -- otherwise it
# depends on which test file pytest happens to alphabetically collect first
# (e.g. a bare `from app.instance.util import ...` at module level hits this,
# because app.instance.routes reaches app.community before app.activitypub
# does).
import app.activitypub.signature  # noqa: F401
from app import celery
from config import Config

TEST_DATABASE_URL = os.environ.get('TEST_DATABASE_URL')


def is_disposable_database_url(url):
    """True if `url` names a database whose name marks it as disposable.

    The db_session fixture truncates every table in this database after every
    test, so this check must not be defeatable by a name that merely contains
    "test" as a substring (e.g. "attestation", "contest_archive",
    "latest_snapshot", "posttest_analytics") or by a query string/fragment
    appended after the real name (e.g.
    "...pyfedi_prod?application_name=pytest_test", whose real database name
    is "pyfedi_prod"). The database name is the last "/"-separated path
    segment, with any trailing "?query" or "#fragment" stripped, and it must
    END WITH "_test" — a bare substring match is not enough.
    """
    if not url:
        return False
    stripped_url = re.split(r'[?#]', url, maxsplit=1)[0]
    segment = stripped_url.rsplit('/', 1)[-1]
    return segment.endswith('_test')


class TestConfig(Config):
    """Test configuration. Inherits the real Config so tests exercise real settings."""
    TESTING = True
    WTF_CSRF_ENABLED = False
    MAIL_SUPPRESS_SEND = True
    SQLALCHEMY_DATABASE_URI = TEST_DATABASE_URL
    CACHE_TYPE = 'NullCache'
    SERVER_NAME = 'test.piefed.local'


@pytest.fixture(scope='session')
def app():
    """A Flask app bound to the test database.

    Skips rather than fails when TEST_DATABASE_URL is unset, so a bare checkout
    can still run the pure-function tests. Use ./run_tests.sh for the full suite.
    """
    if not TEST_DATABASE_URL:
        pytest.skip('TEST_DATABASE_URL is not set; run ./run_tests.sh instead')

    # The db_session fixture truncates every table. Refuse to point that at a
    # database whose name does not mark it as disposable.
    if not is_disposable_database_url(TEST_DATABASE_URL):
        pytest.fail(f'TEST_DATABASE_URL must name a disposable test database '
                    f'(name ending in "_test"), got {TEST_DATABASE_URL!r}')

    from app import create_app
    application = create_app(TestConfig)

    # Written in the OLD key format, because config.py already puts old-format
    # keys (CELERY_BROKER_URL et al.) into celery.conf via
    # app/__init__.py:175's celery.conf.update(app.config). Celery's Settings
    # object picks its defaults layer from whichever format is dominant, then
    # refuses to mix: writing only the modern `task_always_eager` raises
    # celery.exceptions.ImproperlyConfigured("Cannot mix new setting names with
    # old setting names") on the first READ of the setting, at finalization,
    # not at the update() itself.
    #
    # The new-style spellings are here for readability only. detect_settings
    # explicitly tolerates a setting supplied under both names, and converts the
    # new name to the old one before storing it, so they are redundant rather
    # than load-bearing -- verified against Celery 5.3.6, where `changes` ends
    # up holding only CELERY_ALWAYS_EAGER and CELERY_EAGER_PROPAGATES_EXCEPTIONS.
    celery.conf.update(
        task_always_eager=True,
        CELERY_ALWAYS_EAGER=True,
        task_eager_propagates=True,
        CELERY_EAGER_PROPAGATES_EXCEPTIONS=True,
    )

    with application.app_context():
        yield application


_TEARDOWN_SQL_CACHE = []


def _teardown_sql(db):
    """The per-test cleanup statement, built once from the model metadata.

    Uses db.metadata rather than pg_tables so alembic_version is left alone --
    wiping it would strand `flask db upgrade`.
    """
    if not _TEARDOWN_SQL_CACHE:
        deletes = ' '.join(f'DELETE FROM "{table.name}";'
                           for table in db.metadata.sorted_tables)
        _TEARDOWN_SQL_CACHE.append(
            'SET LOCAL session_replication_role = replica; '
            + deletes
            + " SELECT setval(c.oid, 1, false) FROM pg_class c"
              " WHERE c.relkind = 'S' AND c.relnamespace = 'public'::regnamespace;")
    return _TEARDOWN_SQL_CACHE[0]


@pytest.fixture
def db_session(app):
    """Give each test a clean database (and a clean flask.g).

    Deletes every row rather than rolling back a nested transaction: the code
    under test calls db.session.commit() in several places, which a
    rollback-based fixture would have to fight.

    The `app` fixture pushes one app context for the whole test session (see
    above), so flask.g is not reset between tests the way it would be for
    separate requests -- anything a test stashes on g (g.site, g.user,
    g.admin_ids, ...) is otherwise still there for the next test. Combined
    with objects becoming genuinely detached once this fixture closes the
    session below, a leftover g.user/g.site from an earlier test raises
    DetachedInstanceError in a later one the moment code touches one of its
    attributes. Clearing g before each test removes that cross-test coupling.
    """
    from app import db
    from flask import g

    g.__dict__.clear()

    yield db.session

    db.session.rollback()

    # DELETE, not TRUNCATE. TRUNCATE allocates a fresh relfilenode for every
    # table it touches, so ~90 of them per test churn the catalog; measured at
    # 124ms per teardown, which is most of the suite's runtime. DELETE on
    # already-tiny tables touches no catalog and measures ~6ms. fsync,
    # synchronous_commit and full_page_writes are all off in the test container,
    # so durability is not what either one is paying for.
    #
    # session_replication_role = replica disables FK triggers for the duration
    # of the transaction, which is what TRUNCATE ... CASCADE was buying: with
    # the constraints live there is no single safe deletion order, because the
    # schema's foreign keys are not acyclic. SET LOCAL ends at the COMMIT below,
    # so the next test sees constraints enforced normally.
    #
    # The setval sweep replaces RESTART IDENTITY. Tests depend on ids starting
    # at 1 (fixtures hardcode instance_id=1), so resetting is not optional.
    db.session.connection().exec_driver_sql(_teardown_sql(db))
    db.session.commit()
    # Resetting the sequences means the next test's rows reuse these same
    # primary keys. commit() alone leaves this scoped session's identity map
    # holding this test's now-stale objects at those keys; the next test's
    # fixture, constructing fresh rows at the same keys, then trips
    # "Identity map already had an identity for ... replacing it" and can
    # leave orphaned instances that raise DetachedInstanceError when something
    # still holds a reference to them. close() discards the session (and its
    # identity map) outright; the scoped_session transparently opens a new one
    # on next use, so every test starts from a truly clean slate.
    db.session.close()


@pytest.fixture
def site(db_session):
    """The Site row (id 1) that before_request (registered by the app factory)
    requires to populate g.site for every request except /inbox and /static/ --
    see make_site()'s docstring in tests/factories.py.

    Not autouse across the whole suite: several other tests (test_backfill_
    reply_visibility.py, test_process_microblog_announce.py, test_visibility_
    ingest.py, and the api_baseline fixture below) create their own Site row via
    make_site(), and a second row with id 1 would collide with theirs. Modules
    whose tests all issue requests through the app opt in with
    `pytestmark = pytest.mark.usefixtures('site')` instead.
    """
    from tests.factories import make_site
    return make_site()


@pytest.fixture(scope='session', autouse=True)
def disable_rate_limiter():
    """Turn Flask-Limiter off for the suite. It counts across RUNS, not tests.

    `limiter`'s storage is the test Redis (`CACHE_REDIS_URL`, db 1), and the
    test Redis is only destroyed by `./run_tests.sh --down`. Its counters carry
    a one-day TTL. So a route with a real limit -- /auth/login is "30 per day;
    10 per 5 minutes" -- accumulates hits from every run of the suite on the
    same day, and once a bucket passes 30 the route starts returning
    "429 - Too Many Requests" to every subsequent run until the TTL expires.

    That is not hypothetical and it is not a flake in the ordinary sense: at
    commit a144f5ed, with no change to app/ or tests/, the eight
    TestLoginRouteEndToEnd tests in tests/test_redirect_targets.py failed
    exactly this way -- the bucket for 198.51.100.201 held 37 with 82,896
    seconds still to run, and the response body was the 25 bytes of
    "\\n429 - Too Many Requests\\n". A green suite in the morning and a red one
    in the afternoon, from developing on it.

    tests/test_redirect_targets.py already knew about this and tried to dodge it
    by giving each request its own REMOTE_ADDR (`from_a_fresh_ip`). That only
    spreads the accumulation over a handful of fixed addresses; it delays the
    problem by a few dozen runs rather than removing it, which is what happened
    here. The counter, not the address, is the shared state.

    `limiter.enabled` is Flask-Limiter's own switch, so this suppresses the
    limit the way the library intends rather than by patching anything. Nothing
    in the suite asserts rate-limiting behaviour (no test expects a 429), so
    nothing loses coverage; tests/test_client_ip.py exercises the limiter's KEY
    FUNCTION directly, which is unaffected by `enabled`.

    Restored afterwards so the flag does not leak out of the session.
    """
    from app import limiter
    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


@pytest.fixture(scope='session', autouse=True)
def block_outbound_http():
    """Block outbound HTTPX. Nothing else.

    SCOPE, precisely: respx patches httpx's transports and nothing else, so this
    fixture covers exactly the traffic that goes through httpx -- which is all of
    app/activitypub/ and everything else built on app.utils.get_request /
    post_request. A request no http_mock route matched raises instead of leaving
    the process.

    NOT YET BLOCKED -- three transports in app/ bypass httpx entirely and DO
    reach the real internet under this harness. Verified by probe, not inferred:

    - urllib (urllib.request.urlopen) -- app/nntp/server.py:767 fetches images
      with it. A probe against https://example.com/ returned status 200 with
      this fixture active.
    - botocore/urllib3 (boto3) -- ten app modules use it (app/cli.py,
      app/email.py, app/admin/util.py, app/community/util.py, app/utils.py,
      app/activitypub/util.py, app/main/routes.py, app/shared/post.py,
      app/shared/tasks/maintenance.py, app/shared/upload.py). A probe made a
      real S3 call. The `s3_bucket` fixture covers this, but it is
      function-scoped and opt-in: a test that drives S3 code WITHOUT requesting
      `s3_bucket` calls real AWS.
    - smtplib -- app/email.py:164-166 opens smtplib.SMTP/SMTP_SSL directly.
      TestConfig's MAIL_SUPPRESS_SEND governs Flask-Mail, not this code path.
    - pyld/requests -- found by coverage-inbox-gate. pyld's default JSON-LD
      document loader (`_default_document_loader = requests_document_loader()`,
      pyld/jsonld.py:6547) reaches the network through `requests`, which respx
      never touches. LD-signature verification (`jsonld.normalize`) resolves
      `@context` URLs through this loader, so a test exercising that path
      reaches the real internet in this fixture's presence, silently, unless
      it arranges its own isolation. See `no_network_ld_signing` in
      tests/test_inbox_gate_signatures.py for the worked example: a static
      `jsonld.set_document_loader` override serving frozen local copies of
      the two `@context` documents that path needs, plus a `requests.get`
      trip-wire that fails loudly if the static loader is ever bypassed.

    So if you are writing the harness for app/nntp/, for an S3-using module,
    for app/email.py, or for anything that calls jsonld.normalize, you must
    arrange your own isolation (monkeypatch the urlopen/smtplib name in the
    module under test; request `s3_bucket` for boto3 code; install a static
    document loader per no_network_ld_signing for pyld). Do not assume this
    fixture has you covered. Closing the gap properly means a socket-level
    block, which is a design change nobody has ruled on.

    This became load-bearing when Celery went eager. Before that, outbound
    federation went through .delay() and sat in a broker with no worker, so it
    never left the process. Now .delay() runs inline, so app/activitypub/
    signature.py's post_request really does attempt the POST -- and against
    non-resolving test domains that cost ~37s per federating test in real
    timeouts, on top of making the suite depend on DNS and egress.

    An empty respx router raises on any unmatched request instead, which
    post_request records as an ActivityPubLog failure exactly as it would a
    real one -- same code path, no network. assert_all_called=False because
    this router deliberately registers nothing.

    Precedence runs the other way round from what you might expect: respx
    consults routers in REGISTRATION order, so this session-scoped one is
    routers[0] and is asked first, with http_mock second. The arrangement is
    safe only because this router registers ZERO routes -- it can never match,
    so every request falls through to http_mock. Do NOT add a route here, not
    even a catch-all that logs: it would silently take precedence over every
    http_mock route in the suite.

    A test that wants to observe a transport error should register a route with
    side_effect=httpx.ConnectError(...) rather than reach for a real failed
    connection.
    """
    with respx.mock(assert_all_called=False):
        yield


@pytest.fixture
def http_mock():
    """A respx router intercepting all outbound httpx traffic.

    assert_all_called=True means a test that registers a route it never exercises
    fails, rather than passing while silently testing less than it claims.
    """
    with respx.mock(assert_all_called=True) as router:
        yield router


@pytest.fixture
def federation_peer(http_mock):
    """Serve webfinger and actor responses for a remote handle.

    Returns a callable: federation_peer('wakko@mastodon.cloud') -> actor dict.

    The domain must not end in '.local' -- get_request() rejects those via
    is_invalid_get_request_uri() before respx ever sees the request.

    The payload shape follows docs/activitypub_examples/users.md rather than being
    invented, so a test passing here means the code handles what real servers send.

    By default this registers only webfinger and actor -- resolving an actor is
    the common case, and most callers never assert on delivery. A route
    registered here but never called is a hard failure under http_mock's
    assert_all_called=True, so leaving inbox out by default avoids handing
    every future test a foot-gun for the common case.

    Pass include_inbox=True to also register a POST route for the actor's
    inbox, for any test that asserts an activity was actually delivered.

    IMPORTANT -- forgetting include_inbox does NOT fail loudly. Since eager
    Celery landed, delivery runs inline: task_selector calls .delay(), which
    executes in-process, and send_post_request in turn calls
    post_request.delay(), which also executes in-process. But
    app.activitypub.signature.post_request wraps the send in `except
    Exception`, recording the failure as an ActivityPubLog row rather than
    propagating it -- so whatever went wrong is swallowed and the test still
    passes.

    A test that means to prove delivery must therefore pass include_inbox=True
    AND build its sending actor with make_user(..., with_keys=True). Signing
    dereferences the sender's private key, so a keyless sender dies before any
    HTTP request is attempted and the inbox route is never called; the symptom
    is an opaque "RESPX: some routes were not called!" at teardown. Asserting
    on the ActivityPubLog row works too, and distinguishes a failed send from
    no send at all. See test_delivery_can_be_proved_when_the_sender_has_keys.
    """
    def register(handle, include_inbox=False):
        name, domain = handle.lstrip('@').split('@')
        actor_url = f'https://{domain}/users/{name}'
        actor = {
            '@context': 'https://www.w3.org/ns/activitystreams',
            'type': 'Person',
            'id': actor_url,
            'preferredUsername': name,
            'name': name,
            'inbox': f'{actor_url}/inbox',
            'outbox': f'{actor_url}/outbox',
            'followers': f'{actor_url}/followers',
            'publicKey': {
                'id': f'{actor_url}#main-key',
                'owner': actor_url,
                'publicKeyPem': '-----BEGIN PUBLIC KEY-----\nnot-a-real-key\n-----END PUBLIC KEY-----\n',
            },
        }

        http_mock.get(f'https://{domain}/.well-known/webfinger').mock(
            return_value=httpx.Response(200, json={
                'subject': f'acct:{name}@{domain}',
                'links': [{'rel': 'self',
                           'type': 'application/activity+json',
                           'href': actor_url}],
            }))
        http_mock.get(actor_url).mock(return_value=httpx.Response(200, json=actor))
        if include_inbox:
            http_mock.post(f'{actor_url}/inbox').mock(return_value=httpx.Response(202))

        return actor

    return register


@pytest.fixture
def signing_peer(db_session):
    """A remote actor with a real keypair, resolvable without an actor fetch.

    For any test that must send a REAL HTTP-signed request through the test
    client (see tests/factories.py's signed_inbox_post): with_keys=True gives
    it a private key to sign with, and ap_fetched_at is stamped for the same
    reason resolvable_remote_author's docstring gives above --
    find_actor_or_create_cached would otherwise call schedule_actor_refresh,
    which fires a real actor fetch inline under eager Celery.
    """
    from app import db
    from app.utils import utcnow
    from tests.factories import make_instance, make_site, make_user
    make_site()
    instance = make_instance('peer.example')
    sender = make_user(instance, 'alice', with_keys=True)
    sender.ap_fetched_at = utcnow()
    db.session.commit()
    return sender


@pytest.fixture
def redis_double(monkeypatch):
    """Patch get_redis_connection so app code reaches a fakeredis instance.

    What matters is WHERE THE NAME IS BOUND, not when it is called. `from
    app.utils import get_redis_connection` creates a NEW name in the importing
    module, bound to the function object at import time; monkeypatching
    `app.utils.get_redis_connection` rebinds only the attribute on app.utils and
    leaves every such copy pointing at the original. So each binding site has to
    be patched separately, and this fixture patches all four that exist today:
    app.utils itself, plus app.main.routes (:42, used at :712), app.cli (:45,
    used at :2092) and app.activitypub.routes (:38, used at :47).

    Before this was fixed, a test covering app/main/routes.py:712 went green
    while talking to the REAL, shared, never-truncated test Redis in the compose
    stack. If you add a fifth `from app.utils import get_redis_connection`
    anywhere in app/, add it to the list below or you will get that silently.

    Also covered, as of the coverage-utils-feed sub-project: `app.redis_client`,
    the module-level global in app/__init__.py assigned by create_app(). Every
    one of the ~14 `from app import redis_client` sites (`grep -rn 'from app
    import.*redis_client' app/`) does that import INSIDE a function body, not at
    module level -- unlike get_redis_connection's four bindings above, which are
    bound once at import time. Because the import is re-executed on every call,
    monkeypatching the single attribute `app.redis_client` is enough to redirect
    all ~14 call sites to this fixture's fakeredis instance; there is no second
    binding problem to solve here. Verified for get_deduped_post_ids
    (app/utils.py:3798-3968), which both checks a cached result and, for an
    authenticated caller, writes one back with a 24-hour TTL -- see
    tests/test_factories_feed.py's Redis-policy tests, which show the real test
    Redis (CACHE_REDIS_URL db 1, the one thing `--down` would otherwise be needed
    to clear) does not grow across repeated runs while this fixture is active.

    Still NOT covered: the rate limiter and Celery app, built from Config at
    import time.

    CAVEAT (sub-project 5a): this fixture's `fakeredis.FakeRedis` instance
    cannot serve a redis-py lock in this environment. fakeredis
    (requirements-test.txt, unpinned; observed as 2.37.1 in this
    environment), with no `lupa` installed, implements no Lua
    scripting at all -- not EVAL, not EVALSHA. `redis.lock.Lock.acquire()`
    needs none (plain SET NX PX) and succeeds, but `Lock.release()` calls a
    Lua script via EVALSHA and raises
    `redis.exceptions.ResponseError: unknown command 'evalsha'` on
    `__exit__`, every time, for any `with redis_client.lock(...):` block
    (there are 34 such call sites under app/, per
    `grep -rn 'redis_client\.lock(' app/`). Do not chase this by changing
    this fixture -- it patches the right attribute; the fakeredis version
    just can't back a lock's release. Use a narrow local double instead,
    e.g. one whose `.lock(...)` returns `contextlib.nullcontext()`; see
    `_RedisLockOnlyDouble` / `redis_lock_only_double` in
    tests/test_inbox_dispatch_votes.py and "The fakeredis lock limitation"
    in tests/README.md.
    """
    server = fakeredis.FakeRedis(decode_responses=True)
    for module in ('app.utils', 'app.main.routes', 'app.cli', 'app.activitypub.routes'):
        monkeypatch.setattr(f'{module}.get_redis_connection', lambda *args, **kwargs: server)
    monkeypatch.setattr('app.redis_client', server)
    return server


@pytest.fixture
def no_real_sleeping(monkeypatch):
    """Neutralise both sleep call sites the ActivityPub fetch helpers reach.

    Two patches, not one. `time.sleep` covers callers that reach the module
    attribute; `app.utils.sleep` covers app/utils.py's `from time import
    sleep` binding, which was resolved at import and does not see a patch to
    the module attribute. A test file that patches only one still waits the
    real seconds through the other.

    Not autouse: it is opted into per module with
    `pytestmark = pytest.mark.usefixtures('no_real_sleeping')`, so a test
    elsewhere that genuinely wants to observe a delay is not silently
    stripped of it.
    """
    monkeypatch.setattr('time.sleep', lambda *a, **k: None)
    monkeypatch.setattr('app.utils.sleep', lambda *a, **k: None)


@pytest.fixture
def s3_bucket():
    """A moto-backed S3 bucket, yielding its name."""
    with mock_aws():
        client = boto3.client('s3', region_name='us-east-1')
        client.create_bucket(Bucket='pyfedi-test')
        yield 'pyfedi-test'


@pytest.fixture
def api_baseline(app, db_session):
    """The populated-dev-database baseline that app/api/alpha/utils/*.py tests assume.

    tests/test_api_*.py were written against a developer's seeded dev database:
    they do `Site.query.get(1)`, `User.query.get(1)`, and expect real Community/
    Post/PostReply rows and pre-existing subscriptions to already be there. This
    fixture recreates that minimal world from scratch (db_session truncates
    everything after every test, so it runs fresh each time). It returns a
    SimpleNamespace of the rows it created; access them as e.g.
    ``api_baseline.user1.id``.

    Guarantees, once this fixture has run:

    - Instance id 1 (``.instance_local``): the local instance, domain ==
      SERVER_NAME.
    - Instance id 2 (``.instance_remote``): a remote instance.
    - Site id 1 (``.site``).
    - User id 1 (``.user1``): local (ap_id is None), verified, unbanned,
      undeleted -- ``authorise_api_user`` accepts it, and ``encode_jwt_token()``
      works. Its ``password_updated_at`` is pinned to the past (2000-01-01)
      so a JWT minted the same wall-clock second as fixture setup is never
      mistaken by ``authorise_api_user`` for predating a password change
      (comparing whole-second ``iat`` against a sub-second-precision
      timestamp is otherwise a real, if rare, race).
    - User id 2 (``.user2``): local. Member of community2 (so a
      CommunityMember query excluding user1 finds a community user1 has NOT
      joined -- the "NotSubscribed" case). Also the target of a pre-existing
      NOTIF_USER subscription from user1 (the "already subscribed" case).
    - User id 3 (``.user3``): local, unbanned, no relationship to user1 yet
      -- the target used by "subscribe to a new person" flows, so that flow
      never lands on user1 subscribing to themselves.
    - User id 4 (``.user4``): local, unbanned, no NOTIF_USER subscription
      from user1 -- and blocks user1 (UserBlock blocker_id=user4,
      blocked_id=user1), the "someone has blocked me" case. Kept as a
      separate user from user3 deliberately: user3 is the sole candidate an
      unqualified "any unbanned, not-yet-subscribed, non-self user" query
      resolves to, and that query is exactly what the "normal add/remove"
      flow uses. Giving user4 the block relationship instead of user3 means
      that flow's target is unaffected by this fixture's block seeding.
    - ``.banned_user``: banned=True, for negative-path assertions.
    - Three non-banned REMOTE communities (ap_id is not None, so
      ``community_view``'s ``name@ap_domain`` string lookup works):
      ``.community1`` (post_count=10, the highest of any instance_id != 1
      community, so ``order_by(desc(Community.post_count))`` deterministically
      picks it), ``.community2`` (post_count=2), ``.community3``
      (post_count=1, not pre-subscribed, but user1 is CommunityBan'd from it
      -- the "banned from this community" negative path).
    - ``.banned_community``: banned=True, post_count=0 (so it never wins the
      post_count ordering above despite matching instance_id != 1).
    - CommunityMember: user1 is a member of community1 (Subscribed); user2
      is a member of community2 (NotSubscribed, from user1's POV).
    - Posts in community1, authored by user2: ``.post1`` (plain, published,
      untouched -- the "normal add/remove" target), ``.post2`` (published,
      already has a NOTIF_POST subscription from user1 -- the "already
      subscribed" target).
    - PostReply on post1, authored by user2: ``.reply1`` (plain, undeleted).
    - Pre-existing NotificationSubscription rows for user1: community2
      (NOTIF_COMMUNITY), post2 (NOTIF_POST), user2 (NOTIF_USER).
    - CommunityBan: user1 banned from community3 (deliberately not
      community1/2, so it does not interfere with the membership/subscription
      rows above).

    Deliberately NOT seeded:

    - Any deleted Post or PostReply. Several tests have an `if post:` /
      `if reply:` guarded block for "act on deleted content" that expects an
      exception. That holds for the *_subscribe endpoints (subscribe_post /
      subscribe_reply explicitly query `deleted=False` and raise when nothing
      matches) but not for the *_save (bookmark) endpoints -- bookmark_post /
      bookmark_reply never check `deleted` at all, so saving a bookmark on
      deleted content is accepted, not rejected. A single shared "the deleted
      post" would make that guard misfire for the bookmark tests (execute,
      then fail with "DID NOT RAISE"). Leaving no deleted rows in the shared
      baseline makes every one of these guarded blocks no-op consistently,
      which is what an unconditional `if:` guard is for.
    """
    from types import SimpleNamespace

    from app import db
    from app.constants import NOTIF_COMMUNITY, NOTIF_POST, NOTIF_USER
    from app.models import (Community, CommunityBan, CommunityMember, NotificationSubscription, Post, PostReply,
                            User, UserBlock, utcnow)
    from datetime import datetime

    from tests.factories import make_community, make_instance, make_post, make_site, make_user

    instance_local = make_instance(app.config['SERVER_NAME'], software='piefed')
    instance_remote = make_instance('remote.piefed.test')
    site = make_site()

    user1 = make_user(instance_local, 'user1', local=True)
    user1.password_updated_at = datetime(2000, 1, 1)
    user2 = make_user(instance_local, 'user2', local=True)
    user3 = make_user(instance_local, 'user3', local=True)
    user4 = make_user(instance_local, 'user4', local=True)
    banned_user = make_user(instance_local, 'banneduser', local=True)
    banned_user.banned = True
    db.session.commit()

    def remote_community(name, post_count, banned=False):
        community = make_community(name)
        community.instance_id = instance_remote.id
        community.ap_id = f'{name}@{instance_remote.domain}'
        community.ap_domain = instance_remote.domain
        community.ap_profile_id = f'https://{instance_remote.domain}/c/{name}'
        community.post_count = post_count
        community.banned = banned
        db.session.commit()
        return community

    community1 = remote_community('community1', post_count=10)
    community2 = remote_community('community2', post_count=2)
    community3 = remote_community('community3', post_count=1)
    banned_community = remote_community('bannedcommunity', post_count=0, banned=True)

    db.session.add(CommunityMember(user_id=user1.id, community_id=community1.id))
    db.session.add(CommunityMember(user_id=user2.id, community_id=community2.id))
    db.session.add(CommunityBan(user_id=user1.id, community_id=community3.id, banned_by=user1.id))
    db.session.commit()

    post1 = make_post(community1, user2, 'https://remote.piefed.test/posts/1', title='post one')
    post2 = make_post(community1, user2, 'https://remote.piefed.test/posts/2', title='post two')
    db.session.commit()

    reply1 = PostReply(user_id=user2.id, post_id=post1.id, community_id=community1.id, instance_id=user2.instance_id,
                       body='reply one', posted_at=utcnow(), deleted=False)
    db.session.add(reply1)
    db.session.commit()

    db.session.add(NotificationSubscription(name='community2', user_id=user1.id, entity_id=community2.id,
                                            type=NOTIF_COMMUNITY))
    db.session.add(NotificationSubscription(name='post2', user_id=user1.id, entity_id=post2.id, type=NOTIF_POST))
    db.session.add(NotificationSubscription(name='user2', user_id=user1.id, entity_id=user2.id, type=NOTIF_USER))
    db.session.commit()

    db.session.add(UserBlock(blocker_id=user4.id, blocked_id=user1.id))
    db.session.commit()

    return SimpleNamespace(
        instance_local=instance_local, instance_remote=instance_remote, site=site,
        user1=user1, user2=user2, user3=user3, user4=user4, banned_user=banned_user,
        community1=community1, community2=community2, community3=community3, banned_community=banned_community,
        post1=post1, post2=post2,
        reply1=reply1,
    )


# ---------------------------------------------------------------------------
# Shared inbox-gate helpers
#
# These live here rather than in one of the tests/test_inbox_gate_*.py modules
# because all three of those modules need them. `no_network_ld_signing` was
# originally defined in tests/test_inbox_gate_signatures.py and imported from
# there by tests/test_inbox_gate_dispatch.py; a whole-branch review called that
# cross-file fixture import out, on the same reasoning that moved `signing_peer`
# here earlier in this sub-project. The two recipes below it were each written
# out two or three times across those modules; they had not drifted yet, which
# is the moment to converge them rather than after.
# ---------------------------------------------------------------------------

# Frozen, verbatim copies of the two JSON-LD context documents `LDSignature.
# normalized_hash` resolves via `pyld.jsonld.normalize` -- fetched once from
# the real URLs (`requests.get('https://www.w3.org/ns/activitystreams', ...)`
# / `.../security/v1`) and pasted in as-received, not hand-written, so
# URDNA2015 normalization sees exactly what production would see over the
# network. See the module docstring's "Producing a valid LD signature"
# section for why these exist: pyld's default document loader reaches the
# real internet through `requests`, which this suite's httpx-only network
# block does not cover, and a unit test should not depend on w3.org/w3id.org
# being reachable.
_ACTIVITYSTREAMS_CONTEXT = json.loads(
    '{"@context":{"@vocab":"_:","xsd":"http://www.w3.org/2001/XMLSchema#","as":"https://www.w3.org/ns/activitystreams#","ldp":"http://www.w3.org/ns/ldp#","vcard":"http://www.w3.org/2006/vcard/ns#","id":"@id","type":"@type","Accept":"as:Accept","Activity":"as:Activity","IntransitiveActivity":"as:IntransitiveActivity","Add":"as:Add","Announce":"as:Announce","Application":"as:Application","Arrive":"as:Arrive","Article":"as:Article","Audio":"as:Audio","Block":"as:Block","Collection":"as:Collection","CollectionPage":"as:CollectionPage","Relationship":"as:Relationship","Create":"as:Create","Delete":"as:Delete","Dislike":"as:Dislike","Document":"as:Document","Event":"as:Event","Follow":"as:Follow","Flag":"as:Flag","Group":"as:Group","Ignore":"as:Ignore","Image":"as:Image","Invite":"as:Invite","Join":"as:Join","Leave":"as:Leave","Like":"as:Like","Link":"as:Link","Mention":"as:Mention","Note":"as:Note","Object":"as:Object","Offer":"as:Offer","OrderedCollection":"as:OrderedCollection","OrderedCollectionPage":"as:OrderedCollectionPage","Organization":"as:Organization","Page":"as:Page","Person":"as:Person","Place":"as:Place","Profile":"as:Profile","Question":"as:Question","Reject":"as:Reject","Remove":"as:Remove","Service":"as:Service","TentativeAccept":"as:TentativeAccept","TentativeReject":"as:TentativeReject","Tombstone":"as:Tombstone","Undo":"as:Undo","Update":"as:Update","Video":"as:Video","View":"as:View","Listen":"as:Listen","Read":"as:Read","Move":"as:Move","Travel":"as:Travel","IsFollowing":"as:IsFollowing","IsFollowedBy":"as:IsFollowedBy","IsContact":"as:IsContact","IsMember":"as:IsMember","subject":{"@id":"as:subject","@type":"@id"},"relationship":{"@id":"as:relationship","@type":"@id"},"actor":{"@id":"as:actor","@type":"@id"},"attributedTo":{"@id":"as:attributedTo","@type":"@id"},"attachment":{"@id":"as:attachment","@type":"@id"},"bcc":{"@id":"as:bcc","@type":"@id"},"bto":{"@id":"as:bto","@type":"@id"},"cc":{"@id":"as:cc","@type":"@id"},"context":{"@id":"as:context","@type":"@id"},"current":{"@id":"as:current","@type":"@id"},"first":{"@id":"as:first","@type":"@id"},"generator":{"@id":"as:generator","@type":"@id"},"icon":{"@id":"as:icon","@type":"@id"},"image":{"@id":"as:image","@type":"@id"},"inReplyTo":{"@id":"as:inReplyTo","@type":"@id"},"items":{"@id":"as:items","@type":"@id"},"instrument":{"@id":"as:instrument","@type":"@id"},"orderedItems":{"@id":"as:items","@type":"@id","@container":"@list"},"last":{"@id":"as:last","@type":"@id"},"location":{"@id":"as:location","@type":"@id"},"next":{"@id":"as:next","@type":"@id"},"object":{"@id":"as:object","@type":"@id"},"oneOf":{"@id":"as:oneOf","@type":"@id"},"anyOf":{"@id":"as:anyOf","@type":"@id"},"closed":{"@id":"as:closed","@type":"xsd:dateTime"},"origin":{"@id":"as:origin","@type":"@id"},"accuracy":{"@id":"as:accuracy","@type":"xsd:float"},"prev":{"@id":"as:prev","@type":"@id"},"preview":{"@id":"as:preview","@type":"@id"},"replies":{"@id":"as:replies","@type":"@id"},"result":{"@id":"as:result","@type":"@id"},"audience":{"@id":"as:audience","@type":"@id"},"partOf":{"@id":"as:partOf","@type":"@id"},"tag":{"@id":"as:tag","@type":"@id"},"target":{"@id":"as:target","@type":"@id"},"to":{"@id":"as:to","@type":"@id"},"url":{"@id":"as:url","@type":"@id"},"altitude":{"@id":"as:altitude","@type":"xsd:float"},"content":"as:content","contentMap":{"@id":"as:content","@container":"@language"},"name":"as:name","nameMap":{"@id":"as:name","@container":"@language"},"duration":{"@id":"as:duration","@type":"xsd:duration"},"endTime":{"@id":"as:endTime","@type":"xsd:dateTime"},"height":{"@id":"as:height","@type":"xsd:nonNegativeInteger"},"href":{"@id":"as:href","@type":"@id"},"hreflang":"as:hreflang","latitude":{"@id":"as:latitude","@type":"xsd:float"},"longitude":{"@id":"as:longitude","@type":"xsd:float"},"mediaType":"as:mediaType","published":{"@id":"as:published","@type":"xsd:dateTime"},"radius":{"@id":"as:radius","@type":"xsd:float"},"rel":"as:rel","startIndex":{"@id":"as:startIndex","@type":"xsd:nonNegativeInteger"},"startTime":{"@id":"as:startTime","@type":"xsd:dateTime"},"summary":"as:summary","summaryMap":{"@id":"as:summary","@container":"@language"},"totalItems":{"@id":"as:totalItems","@type":"xsd:nonNegativeInteger"},"units":"as:units","updated":{"@id":"as:updated","@type":"xsd:dateTime"},"width":{"@id":"as:width","@type":"xsd:nonNegativeInteger"},"describes":{"@id":"as:describes","@type":"@id"},"formerType":{"@id":"as:formerType","@type":"@id"},"deleted":{"@id":"as:deleted","@type":"xsd:dateTime"},"inbox":{"@id":"ldp:inbox","@type":"@id"},"outbox":{"@id":"as:outbox","@type":"@id"},"following":{"@id":"as:following","@type":"@id"},"followers":{"@id":"as:followers","@type":"@id"},"streams":{"@id":"as:streams","@type":"@id"},"preferredUsername":"as:preferredUsername","endpoints":{"@id":"as:endpoints","@type":"@id"},"uploadMedia":{"@id":"as:uploadMedia","@type":"@id"},"proxyUrl":{"@id":"as:proxyUrl","@type":"@id"},"liked":{"@id":"as:liked","@type":"@id"},"oauthAuthorizationEndpoint":{"@id":"as:oauthAuthorizationEndpoint","@type":"@id"},"oauthTokenEndpoint":{"@id":"as:oauthTokenEndpoint","@type":"@id"},"provideClientKey":{"@id":"as:provideClientKey","@type":"@id"},"signClientKey":{"@id":"as:signClientKey","@type":"@id"},"sharedInbox":{"@id":"as:sharedInbox","@type":"@id"},"Public":{"@id":"as:Public","@type":"@id"},"source":"as:source","likes":{"@id":"as:likes","@type":"@id"},"shares":{"@id":"as:shares","@type":"@id"},"alsoKnownAs":{"@id":"as:alsoKnownAs","@type":"@id"}}}'
)
_SECURITY_V1_CONTEXT = json.loads(
    '{"@context":{"id":"@id","type":"@type","dc":"http://purl.org/dc/terms/","sec":"https://w3id.org/security#","xsd":"http://www.w3.org/2001/XMLSchema#","EcdsaKoblitzSignature2016":"sec:EcdsaKoblitzSignature2016","Ed25519Signature2018":"sec:Ed25519Signature2018","EncryptedMessage":"sec:EncryptedMessage","GraphSignature2012":"sec:GraphSignature2012","LinkedDataSignature2015":"sec:LinkedDataSignature2015","LinkedDataSignature2016":"sec:LinkedDataSignature2016","CryptographicKey":"sec:Key","authenticationTag":"sec:authenticationTag","canonicalizationAlgorithm":"sec:canonicalizationAlgorithm","cipherAlgorithm":"sec:cipherAlgorithm","cipherData":"sec:cipherData","cipherKey":"sec:cipherKey","created":{"@id":"dc:created","@type":"xsd:dateTime"},"creator":{"@id":"dc:creator","@type":"@id"},"digestAlgorithm":"sec:digestAlgorithm","digestValue":"sec:digestValue","domain":"sec:domain","encryptionKey":"sec:encryptionKey","expiration":{"@id":"sec:expiration","@type":"xsd:dateTime"},"expires":{"@id":"sec:expiration","@type":"xsd:dateTime"},"initializationVector":"sec:initializationVector","iterationCount":"sec:iterationCount","nonce":"sec:nonce","normalizationAlgorithm":"sec:normalizationAlgorithm","owner":{"@id":"sec:owner","@type":"@id"},"password":"sec:password","privateKey":{"@id":"sec:privateKey","@type":"@id"},"privateKeyPem":"sec:privateKeyPem","publicKey":{"@id":"sec:publicKey","@type":"@id"},"publicKeyBase58":"sec:publicKeyBase58","publicKeyPem":"sec:publicKeyPem","publicKeyWif":"sec:publicKeyWif","publicKeyService":{"@id":"sec:publicKeyService","@type":"@id"},"revoked":{"@id":"sec:revoked","@type":"xsd:dateTime"},"salt":"sec:salt","signature":"sec:signature","signatureAlgorithm":"sec:signingAlgorithm","signatureValue":"sec:signatureValue"}}'
)
_STATIC_LD_CONTEXTS = {
    'https://www.w3.org/ns/activitystreams': _ACTIVITYSTREAMS_CONTEXT,
    'https://w3id.org/security/v1': _SECURITY_V1_CONTEXT,
}


def _static_ld_document_loader(url, options=None):
    """A pyld document loader over the two frozen documents above -- never
    the network. Raises the same `jsonld.JsonLdError` pyld's own loaders
    raise for an unresolvable URL, so a test that accidentally needs a THIRD
    context fails loudly (an unhelpful KeyError would do too, but this stays
    in pyld's own error vocabulary, matching what `normalized_hash`'s callers
    already expect to catch).
    """
    if url not in _STATIC_LD_CONTEXTS:
        raise jsonld.JsonLdError(
            f'no static content for {url!r} -- add it to _STATIC_LD_CONTEXTS '
            f'rather than letting this fall through to the network',
            'jsonld.LoadDocumentError')
    return {'contentType': 'application/ld+json', 'contextUrl': None,
            'documentUrl': url, 'document': _STATIC_LD_CONTEXTS[url]}


@pytest.fixture
def no_network_ld_signing(monkeypatch):
    """Makes `LDSignature.create_signature`/`verify_signature` resolve their
    two `@context` URLs from the frozen local copies above instead of the
    real internet, for the duration of one test, restoring whatever loader
    pyld had beforehand afterwards -- a `jsonld.set_document_loader` override,
    the same configuration point `app/main/routes.py:744`'s dead demo code
    already uses (there, to point pyld AT the network on purpose). This is
    NOT a patch of `LDSignature.verify_signature` or `HttpSignature.
    verify_request` -- neither forbidden name is touched, and the
    normalization/signature math both still run as production wrote them;
    only where the two context DOCUMENTS come from changes.

    Yields the list of URLs actually resolved through the static loader, so a
    test can assert it was genuinely exercised (`{activitystreams,
    security-v1}`, per the module docstring's "Verified to add no false
    confidence" section) rather than merely not having failed.

    Also monkeypatches `requests.get` to raise `AssertionError` if called at
    all -- `pyld.documentloader.requests.requests_document_loader`'s inner
    loader (pyld's DEFAULT, unpatched here) is the only place in this
    dependency chain that reaches the network, and it does so with exactly
    that call (confirmed by reading its source). With the static loader
    installed it should never run, so this is a hard failure if it somehow
    does, rather than a silent real network request passing unnoticed.
    """
    resolved = []

    def _recording_loader(url, options=None):
        resolved.append(url)
        return _static_ld_document_loader(url, options)

    previous_loader = jsonld.get_document_loader()
    jsonld.set_document_loader(_recording_loader)

    def _network_forbidden(*args, **kwargs):
        raise AssertionError(
            f'requests.get was called during a test using no_network_ld_signing '
            f'-- the static document loader should have intercepted every '
            f'jsonld.normalize context resolution before this call site '
            f'(pyld.documentloader.requests.requests_document_loader) could ever '
            f'be reached; args={args!r} kwargs={kwargs!r}')

    monkeypatch.setattr('requests.get', _network_forbidden)
    try:
        yield resolved
    finally:
        jsonld.set_document_loader(previous_loader)



def unsigned_but_precheck_clean_headers(body_bytes: bytes) -> dict:
    """Digest and Date headers for a hand-built request carrying NO Signature
    header, which still clears `HttpSignature.precheck`.

    Computed the same way `tests.factories.signed_inbox_post` computes them
    internally, so a test that wants to reach a branch sitting AFTER precheck
    but BEFORE (or independent of) `HttpSignature.verify_request` is not
    refused early for an unrelated reason. Every inbox-gate module needs this;
    it was written out three times before being converged here.
    """
    from app.activitypub.signature import HttpSignature
    return {'Digest': HttpSignature.calculate_digest(body_bytes), 'Date': http_date()}


def ld_signed_body(actor, *, signing_key=None, **fields) -> bytes:
    """A JSON-encoded activity body carrying an LD signature, ready to POST.

    `LDSignature.create_signature` is production's own signer, the counterpart
    of the `verify_signature` the gate runs; nothing here is faked. The key id
    is always `actor`'s (`<ap_profile_id>#main-key`), because that is what the
    gate resolves the actor from -- but `signing_key` may be a DIFFERENT
    private key, which is how a test produces a signature that genuinely fails
    against `actor.public_key` rather than one hand-corrupted into failing.
    Defaults to `actor.private_key`, i.e. a signature that genuinely verifies.

    Requires the `no_network_ld_signing` fixture to be active: signing
    normalizes the document with `pyld`, which resolves the two `@context`
    URLs over the real internet unless that fixture's static loader is
    installed.
    """
    from app.activitypub.signature import LDSignature, default_context
    from tests.factories import inbox_activity
    activity = inbox_activity(actor, **fields)
    activity['@context'] = default_context()
    activity['signature'] = LDSignature.create_signature(
        activity, signing_key if signing_key is not None else actor.private_key,
        f'{actor.ap_profile_id}#main-key')
    return json.dumps(activity).encode('utf8')
