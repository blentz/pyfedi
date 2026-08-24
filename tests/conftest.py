import os
import re

import pytest

# Import app before config. config.py does `import app.constants`, which starts
# loading the app package; app/__init__.py in turn does `from config import
# Config` before Config is defined if config.py is the entry point, causing a
# circular ImportError. Every other test module in this repo sidesteps this by
# importing something from app.* first (e.g. `from app.utils import ...`); do
# the same here so `from config import Config` below sees a fully-initialised
# config module.
import app  # noqa: F401
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
    - Any UserBlock with blocker_id=1. One test's own query for "a user who
      has blocked me" is written as `WHERE blocker_id = :user_id` (it should
      read `blocked_id`), which -- given any row at all -- would resolve to
      user1's own id and misfire a self-subscribe error instead of the "this
      user has blocked you" error it is testing for. That guarded block is
      designed to no-op when no such data exists; seeding it would make an
      already-buggy query trip a wrong assertion instead of skipping cleanly.
      See the test file for the corresponding note.
    """
    from types import SimpleNamespace

    from app import db
    from app.constants import NOTIF_COMMUNITY, NOTIF_POST, NOTIF_USER
    from app.models import (Community, CommunityBan, CommunityMember, NotificationSubscription, Post, PostReply,
                            User, utcnow)
    from datetime import datetime

    from tests.factories import make_community, make_instance, make_post, make_site, make_user

    instance_local = make_instance(app.config['SERVER_NAME'], software='piefed')
    instance_remote = make_instance('remote.piefed.test')
    site = make_site()

    user1 = make_user(instance_local, 'user1', local=True)
    user1.password_updated_at = datetime(2000, 1, 1)
    user2 = make_user(instance_local, 'user2', local=True)
    user3 = make_user(instance_local, 'user3', local=True)
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

    return SimpleNamespace(
        instance_local=instance_local, instance_remote=instance_remote, site=site,
        user1=user1, user2=user2, user3=user3, banned_user=banned_user,
        community1=community1, community2=community2, community3=community3, banned_community=banned_community,
        post1=post1, post2=post2,
        reply1=reply1,
    )
