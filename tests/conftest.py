import os
import re

import boto3
import fakeredis
import httpx
import pytest
import respx
from moto import mock_aws

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


@pytest.fixture
def db_session(app):
    """Give each test a clean database (and a clean flask.g).

    Truncates rather than rolling back a nested transaction: the code under test
    calls db.session.commit() in several places, which a rollback-based fixture
    would have to fight.

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
    from sqlalchemy import text

    g.__dict__.clear()

    yield db.session

    db.session.rollback()
    table_names = ', '.join(f'"{table.name}"' for table in reversed(db.metadata.sorted_tables))
    db.session.execute(text(f'TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE'))
    db.session.commit()
    # TRUNCATE ... RESTART IDENTITY means the next test's rows reuse these same
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

    So if you are writing the harness for app/nntp/, for an S3-using module, or
    for app/email.py, you must arrange your own isolation (monkeypatch the
    urlopen/smtplib name in the module under test; request `s3_bucket` for boto3
    code). Do not assume this fixture has you covered. Closing the gap properly
    means a socket-level block, which is a design change nobody has ruled on.

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
    (app/utils.py:3790-3960), which both checks a cached result and, for an
    authenticated caller, writes one back with a 24-hour TTL -- see
    tests/test_factories_feed.py's Redis-policy tests, which show the real test
    Redis (CACHE_REDIS_URL db 1, the one thing `--down` would otherwise be needed
    to clear) does not grow across repeated runs while this fixture is active.

    Still NOT covered: the rate limiter and Celery app, built from Config at
    import time.
    """
    server = fakeredis.FakeRedis(decode_responses=True)
    for module in ('app.utils', 'app.main.routes', 'app.cli', 'app.activitypub.routes'):
        monkeypatch.setattr(f'{module}.get_redis_connection', lambda *args, **kwargs: server)
    monkeypatch.setattr('app.redis_client', server)
    return server


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
