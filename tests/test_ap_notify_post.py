"""`create_post`, `notify_about_post` and `notify_about_post_task` -- the
post-side create and notify path, and the mirror of the reply notification
fan-out sub-project 15 covered to zero.

Entry is a direct call. `notify_about_post_task` takes a post id and is
`@celery.task`-decorated (app/activitypub/util.py:2803-2804), so the imported
name is a Task object whose `__call__` runs the body; the tests below invoke it
directly rather than through `.delay()`. The `app` fixture puts celery in eager
mode, which makes the two routes equivalent in this suite anyway.

`notify_about_post_task` runs on `get_task_session()`, whose autoflush is at
SQLAlchemy's default True, unlike `db.session`, which is constructed
`SQLAlchemy(session_options={"autoflush": False}, engine_options={...})` at
app/__init__.py:81 -- module level, not inside `create_app` (app/__init__.py:129).

The task commits on its own session, which does NOT expire the instances
`db.session` holds. Whether a test therefore has to re-read is conditional, and
the condition is easy to get backwards. `expire_on_commit` is absent from those
`session_options`, so it is at SQLAlchemy's default True: any
`db.session.commit()` of the test's own -- and every factory in tests/factories.py
ends in one -- expires every instance in `db.session`, and an expired attribute
re-loads on next access. So a plain attribute read usually already sees what the
task wrote. It goes stale only when something between that commit and the
assertion re-loaded the instance and so un-expired it -- and calling the task
does exactly that to any row the task itself touches. Prefer a fresh query
(`_notifications_for`) or an explicit `db.session.refresh()` rather than
depending on which of those happened; that much is the opposite of what
sub-projects 14 and 15 needed.

`log_incoming_ap` writes an `ActivityPubLog` row only when
`LOG_ACTIVITYPUB_TO_DB` is true, and config.py defaults it False. Every
`create_post` guard logs and returns None, so the tests that pin them turn it
on and assert the message EXACTLY -- a substring can match a different guard's
row.
"""
import contextlib
import json

import pytest
from sqlalchemy.exc import DataError

from app import db
from app.activitypub.util import (create_post, notify_about_post,
                                  notify_about_post_task)
from app.constants import (NOTIF_COMMUNITY, NOTIF_FEED, NOTIF_TOPIC,
                           NOTIF_USER)
from app.models import ActivityPubLog, Instance, Notification, Post, Topic
from app.utils import utcnow
from tests.factories import (make_community, make_community_block, make_domain,
                             make_feed, make_feed_item, make_instance,
                             make_instance_block, make_notification_subscription,
                             make_post, make_site, make_user, make_user_block)

PEER = 'peer.example'


@pytest.fixture
def ap_log(app):
    """Turn on the ActivityPubLog write so a `create_post` guard is attributable.

    Without it every guard returns None and creates nothing, which makes them
    indistinguishable from each other and from a deleted guard. The `app`
    fixture is session-scoped (tests/conftest.py), so the restore is
    load-bearing: a leaked True would change behaviour for every later test in
    the process.
    """
    app.config['LOG_ACTIVITYPUB_TO_DB'] = True
    yield
    app.config['LOG_ACTIVITYPUB_TO_DB'] = False


def _seed_scenario(local_only=False):
    """A local community owned by user 1, a remote author, and one Post.

    `make_community` hardcodes `instance_id=1` and `user_id=1`, so an instance
    and a user are seeded first to occupy those ids -- the pattern
    tests/test_inbox_dispatch_votes.py documents.
    """
    make_site()
    instance = make_instance(PEER)
    make_user(instance, 'community_owner')
    community = make_community(host=PEER)
    community.ap_fetched_at = utcnow()
    community.local_only = local_only
    author = make_user(instance, 'author')
    post = make_post(community, author, ap_id=f'https://{PEER}/post/1')
    db.session.commit()
    return community, post, author


def _post_doc(**fields):
    """A Create activity's envelope plus its `object`.

    `create_post` reads `request_json['id']` for the log and
    `request_json.get('object')` for the visibility check, so both levels
    matter.

    Two departures from the brief's draft of this helper, both to stop a test
    passing for the wrong reason:

    `to` defaults to Public -- the same departure, for the same reason, that
    the sibling `_reply_doc` in tests/test_ap_create_reply.py records against
    its own brief's draft. `create_post`'s visibility guard runs before
    `Post.new` is reached, and `activitypub_visibility` classifies an object
    with no addressing at all as 'direct' -- its last line is a bare
    `return 'direct'`. The brief's draft set no addressing, so every test
    aimed at something PAST the visibility guard would have been refused by
    that guard instead. A test wanting the guard itself overrides `to`/`cc`
    through `**fields`.

    The object's `id` is `/post/2`, not the `/post/1` `_seed_scenario` gives
    the row it seeds. This one has no sibling precedent -- `_reply_doc`'s
    `/comment/1` is the id of the reply its tests CREATE, and that file's
    `_seed_scenario` seeds no `PostReply` at all, so there was no collision
    there to avoid. `Post.new` writes `ap_id=request_json['object']['id']`,
    and `Post.ap_id` is unique, so reusing the seeded row's id would send any
    successful create down `Post.new`'s
    `except IntegrityError: ... return Post.query.filter_by(ap_id=...).one()`
    arm and hand back the SEEDED post as if it had just been created.
    """
    obj = {'id': f'https://{PEER}/post/2', 'type': 'Page',
           'to': ['https://www.w3.org/ns/activitystreams#Public'], 'cc': []}
    obj.update(fields)
    return {'id': f'https://{PEER}/activities/create/1',
            'type': 'Create',
            'object': obj}


def _subscribe(user, entity_id, type_):
    """A NotificationSubscription. The four arms key on different entity ids --
    an author's user id, a community id, a topic id and a feed id -- so the
    caller supplies both halves rather than the helper guessing.
    """
    return make_notification_subscription(user, entity_id, type_)


class _Recorder:
    """Stands in for `notify_about_post_task` and records HOW it was called.

    Same shape and same reason as tests/test_inbox_gate_dispatch.py's
    `Recorder`: under this suite's eager Celery (`task_always_eager=True`,
    `task_eager_propagates=True`, set in tests/conftest.py's `app` fixture)
    `.delay()` runs the task inline, so the two arms of

        if current_app.debug:
            notify_about_post_task(post.id)
        else:
            notify_about_post_task.delay(post.id)

    produce identical effects here. Which ATTRIBUTE was invoked is the only
    thing left that distinguishes them, which is why the brief specifies a spy
    for this one test rather than the campaign's usual persisted-row
    assertion.
    """

    def __init__(self):
        self.inline = []
        self.delayed = []

    def __call__(self, *args, **kwargs):
        self.inline.append((args, kwargs))

    def delay(self, *args, **kwargs):
        self.delayed.append((args, kwargs))


# ---------------------------------------------------------------------------
# create_post -- the local_only guard and the tail except
# ---------------------------------------------------------------------------

def test_a_local_only_community_discards_the_post(app, db_session, ap_log):
    """The first guard:

        if community.local_only:
            log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, 'Community is local only, post discarded')
            return None

    `Community.local_only` is `db.Column(db.Boolean, default=False)`
    (app/models.py), so `local_only=True` here is contrary to the column's
    default rather than the value a fresh row already carries.

    The message is asserted with `==`, not `in`. Sub-project 15 lost two
    mutants to substring assertions: deleting an outer guard let a LATER guard
    fire and write a message that still contained the substring, so the
    assertion passed on the wrong row. `create_post` has a second guard
    logging `f'Non-public post refused: {visibility}'` and a tail handler
    logging `str(ex)`, and only equality tells this row apart from either.

    The Post count is asserted against the seeded baseline of one rather than
    against zero, so a passing assertion says the guard created nothing rather
    than merely that the table is empty.
    """
    community, seeded_post, author = _seed_scenario(local_only=True)

    result = create_post(store_ap_json=True, community=community,
                         request_json=_post_doc(name='a federated post'), user=author)

    assert result is None
    assert Post.query.count() == 1
    assert Post.query.one().id == seeded_post.id
    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Community is local only, post discarded'


def test_a_post_on_an_admin_blocked_domain_is_swallowed_by_the_tail_except(app, db_session, ap_log, http_mock):
    """The tail:

        except Exception as ex:
            log_incoming_ap(id, APLOG_CREATE, APLOG_FAILURE, saved_json, str(ex))
            return None

    Reached through a real failure from `Post.new` rather than an injected
    one. Counted by reading `Post.new`'s whole body -- from
    `def new(cls, user: User, community: Community, request_json: dict, announce_id=None):`
    to the next method definition, `def calculate_cross_posts(...)` -- there is
    exactly ONE explicit `raise` in it:

        if domain.banned or domain.name.endswith('.pages.dev'):
            raise Exception(domain.name + ' is blocked by admin')

    so the plan's count of one is right. It sits inside `if domain:`, which
    `Post.new` enters only once `post.url` is set and `domain_from_url`
    resolves it, so the document carries a Link attachment -- `Post.new`'s
    `if attachment['type'] == 'Link': ... post.url = attachment['href']` -- and
    the Domain row it resolves to is seeded banned. `make_domain` leaves
    `banned` at its `default=False`, so the flip below is a contrary baseline,
    not a restatement of the default.

    The `raise` precedes `db.session.add(post)`, so nothing of the refused post
    reaches the database; the seeded row is the whole expected population.

    `http_mock` is needed because `Post.new` reaches `is_image_url(post.url)`
    on the way to `domain_from_url`, and `is_image_url` calls
    `mime_type_using_head`, which issues a real `httpx_client.head`. Its
    `except (httpx.HTTPError, httpx.InvalidURL)` does NOT catch respx's
    unmatched-request error, so without the route below the tail handler logs
    "RESPX: <Request('HEAD', ...)> not mocked!" instead of the `Post.new`
    exception -- measured, as the first run of this test did exactly that.
    That is the whole of why `http_mock` is here.

    The Content-Type served is NOT load-bearing for reaching the raise.
    `domain = domain_from_url(post.url)` (app/models.py:2060) is a SIBLING of
    the entire `if is_image_url(...)` / `elif` / `else:` classification chain
    (`:2011`, `:2029`, `:2031`, `:2040`, `:2050`), all of it inside
    `if post.url:` (`:2008`), so every post type reaches the domain lookup and
    the raise. A non-image type is served only to keep the run out of the
    `POST_TYPE_IMAGE` arm, which would `db.session.add(image)` (`:2027`) a
    `File` row that nothing here wants and the tail handler's own commit would
    then persist.
    """
    community, seeded_post, author = _seed_scenario()
    http_mock.head('https://blocked.example/article').respond(
        200, headers={'Content-Type': 'text/html'})
    domain = make_domain('blocked.example')
    domain.banned = True
    db.session.commit()
    document = _post_doc(name='a federated post',
                         attachment=[{'type': 'Link',
                                      'href': 'https://blocked.example/article'}])

    result = create_post(store_ap_json=True, community=community,
                         request_json=document, user=author)

    assert result is None
    assert Post.query.count() == 1
    assert Post.query.one().id == seeded_post.id
    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'blocked.example is blocked by admin'


# ---------------------------------------------------------------------------
# notify_about_post -- the two-line dispatcher
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('debug', [True, False])
def test_notify_about_post_dispatches_inline_only_under_debug(app, db_session, monkeypatch, debug):
    """The whole of `notify_about_post`:

        def notify_about_post(post: Post):
            if current_app.debug:
                notify_about_post_task(post.id)
            else:
                notify_about_post_task.delay(post.id)

    Flask's `app.debug` reads `config['DEBUG']`, which is what the parametrize
    flips.

    Asserted by spying rather than by effects, on the brief's instruction and
    for the reason `_Recorder`'s docstring gives: eager Celery makes `.delay()`
    run inline, so both arms leave identical rows and no persisted observable
    can separate them. Both lists are asserted each time, so the spy shows the
    OTHER attribute was not also touched.
    """
    community, post, author = _seed_scenario()
    recorder = _Recorder()
    monkeypatch.setattr('app.activitypub.util.notify_about_post_task', recorder)
    monkeypatch.setitem(app.config, 'DEBUG', debug)

    notify_about_post(post)

    if debug:
        assert recorder.inline == [((post.id,), {})]
        assert recorder.delayed == []
    else:
        assert recorder.delayed == [((post.id,), {})]
        assert recorder.inline == []


# ---------------------------------------------------------------------------
# notify_about_post_task -- helpers shared by all four notification arms
# ---------------------------------------------------------------------------

def _notifications_for(user):
    """Every Notification row for one recipient, freshly read, oldest first.

    `notify_about_post_task` commits on `get_task_session()`, which does not
    expire objects held by `db.session`, so a query is the reliable read --
    an attribute on a stale ORM instance is not.

    The `order_by` is a deliberate departure from the brief's printed draft,
    which ended `.filter_by(user_id=user.id).all()`. Unordered is harmless for
    the NOTIF_USER tests below, which each read a single row, but this helper is
    the interface the three later arm tasks consume, and the kills they owe for
    `notify_id not in notifications_sent_to` need ONE recipient subscribed to
    TWO entities -- two rows for one user, indexed positionally. PostgreSQL
    gives no ordering guarantee for a SELECT without ORDER BY, so
    `notifications[0]` would be a coin toss there. `Notification.id` is the
    autoincrement primary key (app/models.py:3729), so ordering by it is
    insertion order, which is the order the four arms run in.
    """
    return (db.session.query(Notification)
            .filter_by(user_id=user.id)
            .order_by(Notification.id)
            .all())


def _peer_instance():
    """The Instance row `_seed_scenario` seeds for PEER.

    `_seed_scenario` does not return it, but everything below needs it: it is
    the instance `make_user` hangs a recipient off, and it is the one
    `make_post` copies onto `Post.instance_id` (`instance_id=user.instance_id`
    in tests/factories.py), which is the id the arm's instance filter compares
    against.
    """
    return Instance.query.filter_by(domain=PEER).one()


# ---------------------------------------------------------------------------
# notify_about_post_task -- the NOTIF_USER arm
# ---------------------------------------------------------------------------

def test_a_subscriber_to_the_author_is_notified(app, db_session):
    """The NOTIF_USER arm's happy path:

        user_send_notifs_to = notification_subscribers(post.user_id, NOTIF_USER)
        for notify_id in user_send_notifs_to:
            blocked_senders = blocked_users(notify_id)  # D276
            blocked_comms = blocked_communities(notify_id)
            blocked_ints = blocked_or_banned_instances(notify_id)
            if notify_id != post.user_id and notify_id not in notifications_sent_to and \\
                    post.user_id not in blocked_senders and \\
                    post.community_id not in blocked_comms and \\
                    post.instance_id not in blocked_ints:

    `notification_subscribers(entity_id, entity_type)` is a raw
    `SELECT user_id FROM "notification_subscription" WHERE entity_id = :entity_id
    AND type = :type` (app/utils.py), so the subscription below has to name the
    AUTHOR's user id as its entity_id -- this arm keys on the author, not on
    the community.

    `notif_type` is asserted against `NOTIF_USER`, which is `0`
    (app/constants.py:52), while the column's declared default is
    `NOTIF_DEFAULT`, `999` (`notif_type = db.Column(db.Integer,
    default=NOTIF_DEFAULT, index=True)`, app/models.py) -- so the assertion is
    contrary to the default rather than a restatement of it.

    `community.ap_id` is set here on purpose. Both `targets_data` entries that
    carry a name are ternaries --
    `community.ap_id if community.ap_id else community.name` and
    `author.ap_id if author.ap_id else author.user_name` -- and `make_community`
    sets `ap_profile_id` but never `ap_id`, so left alone the community ternary
    would take its else-arm and a swap of the two arms would be invisible. With
    `ap_id` set to a value that differs from `name`, both ternaries resolve to
    their truthy arm and both arms are distinguishable. (`make_user` already
    gives a non-local user `ap_id=f'{name}@{instance.domain}'`, so the author
    half needed nothing.)

    THREE more posts are seeded and the task is run against the LAST of them,
    so that no two of the ids this `targets` dict carries hold the same number.
    `_seed_scenario` seeds one Community and then one Post, and
    the `db_session` teardown resets every
    sequence to 1 after each test (tests/conftest.py:131-132), so Community and Post both take primary key 1 while its two
    users take 1 and 2 -- the silently-vacuous shape fact 89 in tests/README.md
    records. ONE extra post is not enough in this arm, unlike in the
    NOTIF_COMMUNITY arm's happy path below: `post.id` would then be 2, which is
    the AUTHOR's id, and this dict carries `'author_id': post.user_id`
    alongside `'post_id': post.id`. Post 4 is the first id nothing else the arm
    can reach holds -- the subscriber is user 3 and the arm carries that id as
    `notify_id`. (`post.instance_id` does not hold 4 either; it holds 1, which
    is a different point and is about MUTUAL DISTINCTNESS rather than about
    post 4's id being free. The guard below cannot bring it into the distinct
    set and says why.) MEASURED: with `_seed_scenario`'s single post, mutating
    `'post_id': post.id` (app/activitypub/util.py:2829) to `post.community_id`
    left all 34 tests in this file passing; with the fourth post that mutation
    fails here. The `len({...}) == 4` below fails loudly if factory ordering
    ever changes.
    """
    community, seeded_post, author = _seed_scenario()
    community.ap_id = f'microblogs@{PEER}'
    make_post(community, author, ap_id=f'https://{PEER}/post/2')
    make_post(community, author, ap_id=f'https://{PEER}/post/3')
    post = make_post(community, author, ap_id=f'https://{PEER}/post/4')
    subscriber = make_user(_peer_instance(), 'subscriber', local=True)
    assert len({community.id, post.id, author.id, subscriber.id}) == 4
    # post.instance_id is a FIFTH id reachable from `post` and it is NOT
    # distinct: it equals community.id by construction, so it cannot join the
    # set above. MEASURED, not reasoned -- asserting
    # len({..., post.instance_id}) == 5 fails `assert 4 == 5 / where
    # 4 = len({1, 2, 3, 4})`. The cause is structural: _seed_scenario creates
    # the PEER Instance first so it takes id 1, make_community hardcodes
    # instance_id=1, and make_post copies the author's instance_id
    # (tests/factories.py:337), which is that same instance. Separating them
    # would mean restructuring _seed_scenario's id-occupation pattern, which
    # every test in this file depends on.
    #
    # So the `'post_id': post.id` -> `post.instance_id` mutant is pinned by the
    # line below rather than by the set above, and this is the property that
    # actually kills it. Stated explicitly so the kill is not read as stronger
    # than it is: it dies on post.id being 4, exactly as the `post.community_id`
    # mutant does, not on an independently-witnessed id. MEASURED: applying
    # that mutation to app/activitypub/util.py:2829 and running this file
    # gives 1 failed / 33 passed, a SOLE kill by this test; app/ restored and
    # md5-verified against HEAD afterwards.
    assert post.instance_id == community.id
    assert post.instance_id != post.id
    _subscribe(subscriber, author.id, NOTIF_USER)
    db.session.commit()

    notify_about_post_task(post.id)

    notifications = _notifications_for(subscriber)
    assert len(notifications) == 1
    notification = notifications[0]
    assert notification.notif_type == NOTIF_USER
    assert notification.subtype == 'new_post_from_followed_user'
    assert notification.url == f'/post/{post.id}'
    assert notification.title == 'a post'
    assert notification.author_id == author.id
    assert notification.targets == {'gen': '0',
                                    'post_id': post.id,
                                    'post_title': 'a post',
                                    'community_name': f'microblogs@{PEER}',
                                    'author_id': author.id,
                                    'author_user_name': f'author@{PEER}'}


def test_the_author_is_not_notified_even_when_subscribed_to_themselves(app, db_session):
    """`notify_id != post.user_id`, the arm's first conjunct.

    A second, unrelated subscriber is seeded and IS notified, so the author's
    empty result says "this run created nothing for the author" rather than
    "this run created nothing at all" -- without it a mutant that stopped the
    whole arm from firing would pass this test.

    A self-subscription is a row the schema holds without complaint:
    `class NotificationSubscription` (app/models.py) declares `type`,
    `entity_id` and `user_id` as plain indexed columns and no unique
    constraint over them. The only production writer of a NOTIF_USER
    subscription, `subscribe_user` (app/shared/user.py:89 -- the sole
    implementation for both SRC_WEB and SRC_API), nonetheless refuses one:

        if person.id == user_id:
            msg = 'Target must be a another user.'

    so on this path the conjunct is a defensive guard rather than a filter the
    subscribe route can feed. It is still a real discriminator on stored state:
    the row is insertable, and it is the same conjunct the three sibling arms
    carry, where an author subscribed to their own community, topic or feed is
    entirely ordinary.
    """
    community, post, author = _seed_scenario()
    subscriber = make_user(_peer_instance(), 'subscriber', local=True)
    _subscribe(author, author.id, NOTIF_USER)
    _subscribe(subscriber, author.id, NOTIF_USER)
    db.session.commit()

    notify_about_post_task(post.id)

    assert _notifications_for(author) == []
    assert len(_notifications_for(subscriber)) == 1


def test_a_subscriber_who_blocked_the_community_is_not_notified(app, db_session):
    """`post.community_id not in blocked_comms`, where

        blocked_comms = blocked_communities(notify_id)

    reads `CommunityBlock` rows filtered on `user_id` and returns
    `community_id` (app/utils.py) -- which is what `make_community_block`
    writes. Note the comparison is against `post.community_id`, so the block
    that matters is the recipient's block of the community the post landed in.

    As above, a second subscriber who blocked nothing is notified in the same
    run, so "no rows for the blocker" is distinguishable from "no rows at all".
    """
    community, post, author = _seed_scenario()
    instance = _peer_instance()
    blocker = make_user(instance, 'community_blocker', local=True)
    subscriber = make_user(instance, 'subscriber', local=True)
    _subscribe(blocker, author.id, NOTIF_USER)
    _subscribe(subscriber, author.id, NOTIF_USER)
    make_community_block(blocker, community)
    db.session.commit()

    notify_about_post_task(post.id)

    assert _notifications_for(blocker) == []
    assert len(_notifications_for(subscriber)) == 1


def test_a_subscriber_who_blocked_the_author_is_not_notified(app, db_session):
    """D276, fixed (owner ruling): the NOTIF_USER arm checked community and
    instance blocks but not `blocked_users`, so someone who subscribed to an
    author and later blocked them -- the block path does not delete that
    subscription -- still got every post. A blocked author is now suppressed,
    as the topic and feed arms already did.
    """
    community, post, author = _seed_scenario()
    instance = _peer_instance()
    blocker = make_user(instance, 'author_blocker', local=True)
    subscriber = make_user(instance, 'subscriber', local=True)
    _subscribe(blocker, author.id, NOTIF_USER)
    _subscribe(subscriber, author.id, NOTIF_USER)
    make_user_block(blocker, author)
    db.session.commit()

    notify_about_post_task(post.id)

    assert _notifications_for(blocker) == []
    assert len(_notifications_for(subscriber)) == 1


def test_a_subscriber_who_blocked_the_instance_is_not_notified(app, db_session):
    """`post.instance_id not in blocked_ints`, where

        blocked_ints = blocked_or_banned_instances(notify_id)

    is `[block.instance_id for block in blocks] + banned_instances(user_id)`
    over `InstanceBlock` rows filtered on `user_id` (app/utils.py). This test
    exercises the `InstanceBlock` half, which is what `make_instance_block`
    writes.

    The instance compared is `post.instance_id`, and `make_post` sets
    `instance_id=user.instance_id` -- the author's instance, PEER -- so PEER is
    the instance the blocker has to block.

    As above, a second subscriber who blocked nothing is notified in the same
    run.
    """
    community, post, author = _seed_scenario()
    instance = _peer_instance()
    blocker = make_user(instance, 'instance_blocker', local=True)
    subscriber = make_user(instance, 'subscriber', local=True)
    _subscribe(blocker, author.id, NOTIF_USER)
    _subscribe(subscriber, author.id, NOTIF_USER)
    make_instance_block(blocker, instance)
    db.session.commit()

    notify_about_post_task(post.id)

    assert _notifications_for(blocker) == []
    assert len(_notifications_for(subscriber)) == 1


def test_the_notified_subscribers_unread_counter_is_incremented(app, db_session):
    """The two lines after the `session.add(new_notification)`:

        user = session.query(User).get(notify_id)
        user.unread_notifications += 1

    `User.unread_notifications` is `db.Column(db.Integer, default=0)`
    (app/models.py:1023) and `make_user` never sets it, so the counter is
    seeded to 7 first: asserting 8 afterwards cannot be satisfied by the
    column's default, and `+= 1` is distinguished from an assignment of a
    constant.

    The `db.session.refresh` forces the re-read, but -- MEASURED, by deleting
    the line and re-running -- it is not what makes this test pass here: the
    task's commit is on `get_task_session()`'s own Session and does not expire
    anything held by `db.session`, yet the `db.session.commit()` in the arrange
    block above already did, and SQLAlchemy re-loads an expired attribute on
    next access. The refresh is kept so the assertion does not depend on
    nothing between that commit and the assert having touched -- and so
    un-expired -- the attribute. `_notifications_for`'s query is the same
    precaution for the rows.
    """
    community, post, author = _seed_scenario()
    subscriber = make_user(_peer_instance(), 'subscriber', local=True)
    subscriber.unread_notifications = 7
    _subscribe(subscriber, author.id, NOTIF_USER)
    db.session.commit()

    notify_about_post_task(post.id)

    db.session.refresh(subscriber)
    assert subscriber.unread_notifications == 8
    assert len(_notifications_for(subscriber)) == 1


# ---------------------------------------------------------------------------
# notify_about_post_task -- the NOTIF_COMMUNITY arm
# ---------------------------------------------------------------------------

def test_a_subscriber_to_the_community_is_notified(app, db_session):
    """The NOTIF_COMMUNITY arm's happy path:

        community_send_notifs_to = notification_subscribers(post.community_id, NOTIF_COMMUNITY)
        for notify_id in community_send_notifs_to:
            blocked_senders = blocked_users(notify_id)
            blocked_comms = blocked_communities(notify_id)  # D277
            blocked_ints = blocked_or_banned_instances(notify_id)
            if notify_id != post.user_id and notify_id not in notifications_sent_to and \\
                    post.user_id not in blocked_senders and post.community_id not in blocked_comms and \\
                    post.instance_id not in blocked_ints:

    The entity_id the subscription has to name is `post.community_id`, not the
    author's user id -- that is the whole difference between this arm and the
    NOTIF_USER arm above, whose `notification_subscribers` call passes
    `post.user_id`.

    The `targets` dict is what tells the two arms apart on stored state. This
    arm's, quoted whole from source:

        targets_data = {'gen': '0',
                        'post_id': post.id,
                        'post_title': post.title,
                        'community_name': community.ap_id if community.ap_id else community.name,
                        'community_id': post.community_id}

    It carries `community_id` and carries NO `author_id` / `author_user_name`;
    the NOTIF_USER dict carries those two and no `community_id`. The
    `author_id` COLUMN is still set (`author_id=post.user_id`), so it is
    asserted separately -- it is the dict that discriminates, not the column.

    `notif_type` is asserted against `NOTIF_COMMUNITY`, which is `1`
    (app/constants.py:53), while the column's declared default is
    `NOTIF_DEFAULT`, `999` (app/models.py:3736) -- contrary to the default, not
    a restatement of it. `subtype` has no declared default at all
    (`subtype = db.Column(db.String(50), index=True)`, app/models.py:3737), so
    it is None on an unwritten row.

    `community.ap_id` is set for the reason Task 2's happy-path test records:
    `community.ap_id if community.ap_id else community.name` is a ternary,
    `make_community` sets `ap_profile_id` but never `ap_id`, and left alone the
    ternary would take its else-arm and a swap of its arms would be invisible.

    A SECOND post is seeded and it is that one the task is run against, so that
    `post.id` and `post.community_id` hold different numbers. `_seed_scenario`
    creates exactly one Community and then exactly one Post, so both get
    primary key 1, and the two `targets` entries `'post_id': post.id` and
    `'community_id': post.community_id` would then be indistinguishable --
    MEASURED: with the seeded post, mutating `'community_id': post.community_id`
    to `post.id` left all fifteen tests passing. With the second post the ids
    are 2 and 1 and the same mutation fails this test.
    """
    community, seeded_post, author = _seed_scenario()
    community.ap_id = f'microblogs@{PEER}'
    post = make_post(community, author, ap_id=f'https://{PEER}/post/2')
    assert post.id != community.id
    subscriber = make_user(_peer_instance(), 'subscriber', local=True)
    _subscribe(subscriber, community.id, NOTIF_COMMUNITY)
    db.session.commit()

    notify_about_post_task(post.id)

    notifications = _notifications_for(subscriber)
    assert len(notifications) == 1
    notification = notifications[0]
    assert notification.notif_type == NOTIF_COMMUNITY
    assert notification.subtype == 'new_post_in_followed_community'
    assert notification.url == f'/post/{post.id}'
    assert notification.title == 'a post'
    assert notification.author_id == author.id
    assert notification.targets == {'gen': '0',
                                    'post_id': post.id,
                                    'post_title': 'a post',
                                    'community_name': f'microblogs@{PEER}',
                                    'community_id': community.id}


def test_the_author_is_not_notified_even_when_subscribed_to_their_own_community(app, db_session):
    """`notify_id != post.user_id`, this arm's first conjunct.

    Unlike the NOTIF_USER arm's copy of this conjunct -- whose only production
    writer, `subscribe_user` (app/shared/user.py:89), refuses a
    self-subscription with `if person.id == user_id: msg = 'Target must be a
    another user.'` -- NOTIF_COMMUNITY has TWO production writers and neither
    imposes such a rule. `subscribe_community` (app/shared/community.py:394)
    reaches its `NotificationSubscription(... type=NOTIF_COMMUNITY)`
    (:429-431) for any `user_id` that is not already subscribed and is not in
    `communities_banned_from(user_id)`; nothing there compares the subscriber
    against anyone. The other is the `migrate_community_notifs` CLI command
    (app/cli.py:1757-1759), which builds the row from a `CommunityMember`'s
    `user_id` and `community_id` and compares nothing at all. So an author
    subscribed to a community they then post in is a row production creates,
    and this conjunct is a live filter here rather than the defensive guard it
    is in the arm above.

    A second subscriber to the same community IS notified in the same run, so
    the author's empty result says "this run created nothing for the author"
    rather than "this run created nothing at all".
    """
    community, post, author = _seed_scenario()
    subscriber = make_user(_peer_instance(), 'subscriber', local=True)
    _subscribe(author, community.id, NOTIF_COMMUNITY)
    _subscribe(subscriber, community.id, NOTIF_COMMUNITY)
    db.session.commit()

    notify_about_post_task(post.id)

    assert _notifications_for(author) == []
    assert len(_notifications_for(subscriber)) == 1


def test_a_subscriber_already_notified_by_the_user_arm_is_not_notified_again(app, db_session):
    """`notify_id not in notifications_sent_to`, this arm's second conjunct.

    `notifications_sent_to = set()` is initialised once, above all four arms,
    and each arm ends the body of its `if` with
    `notifications_sent_to.add(notify_id)` -- at app/activitypub/util.py:2843,
    :2866, :2897 and :2931, the last of those put there by commit `0489dc1d`
    (register entry D275). The arms run in file order NOTIF_USER,
    NOTIF_COMMUNITY, NOTIF_TOPIC, NOTIF_FEED -- read from source, where the
    `# NOTIF_USER` comment precedes `# NOTIF_COMMUNITY`, which precedes
    `# NOTIF_TOPIC`, which precedes `# NOTIF_FEED`, all four inside the one
    `with patch_db_session(session):` block. So by the time this arm evaluates
    the conjunct the set can be non-empty, which is exactly what the NOTIF_USER
    arm's identical conjunct could not be: it runs first, and nothing writes to
    the set between `notifications_sent_to = set()` and its own loop.

    `dual` is subscribed BOTH to the author (NOTIF_USER) and to the community
    (NOTIF_COMMUNITY). The user arm wins because it runs first, so `dual` gets
    exactly ONE row and its `targets` is the user arm's shape -- the pair
    `author_id` / `author_user_name` and no `community_id`.

    `community_only`, subscribed to the community alone, is notified in the
    same run and IS given a NOTIF_COMMUNITY row. Without it, a mutant that
    stopped the community arm firing altogether would still leave `dual`
    holding exactly one row and pass.
    """
    community, post, author = _seed_scenario()
    instance = _peer_instance()
    dual = make_user(instance, 'dual_subscriber', local=True)
    community_only = make_user(instance, 'community_subscriber', local=True)
    _subscribe(dual, author.id, NOTIF_USER)
    _subscribe(dual, community.id, NOTIF_COMMUNITY)
    _subscribe(community_only, community.id, NOTIF_COMMUNITY)
    db.session.commit()

    notify_about_post_task(post.id)

    dual_notifications = _notifications_for(dual)
    assert len(dual_notifications) == 1
    assert dual_notifications[0].notif_type == NOTIF_USER
    assert dual_notifications[0].subtype == 'new_post_from_followed_user'
    assert 'community_id' not in dual_notifications[0].targets
    assert dual_notifications[0].targets['author_user_name'] == f'author@{PEER}'

    control_notifications = _notifications_for(community_only)
    assert len(control_notifications) == 1
    assert control_notifications[0].notif_type == NOTIF_COMMUNITY
    assert control_notifications[0].subtype == 'new_post_in_followed_community'


def test_a_community_subscriber_who_blocked_the_author_is_not_notified(app, db_session):
    """`post.user_id not in blocked_senders`, where

        blocked_senders = blocked_users(notify_id)

    is

        blocks = db.session.query(UserBlock).filter_by(blocker_id=user_id)
        return [block.blocked_id for block in blocks]

    (app/utils.py:1746-1747) -- so the recipient is the BLOCKER and the post's
    author is the BLOCKED, which is the order `make_user_block(blocker,
    blocked)` writes.

    The NOTIF_USER arm used to lack this conjunct, and this arm the
    community one; both arms now check all three (D276, D277).

    A second subscriber who blocked nobody is notified in the same run, so
    "no rows for the blocker" is distinguishable from "no rows at all".
    """
    community, post, author = _seed_scenario()
    instance = _peer_instance()
    blocker = make_user(instance, 'author_blocker', local=True)
    subscriber = make_user(instance, 'subscriber', local=True)
    _subscribe(blocker, community.id, NOTIF_COMMUNITY)
    _subscribe(subscriber, community.id, NOTIF_COMMUNITY)
    make_user_block(blocker, author)
    db.session.commit()

    notify_about_post_task(post.id)

    assert _notifications_for(blocker) == []
    assert len(_notifications_for(subscriber)) == 1


def test_a_community_subscriber_who_blocked_the_community_is_not_notified(app, db_session):
    """D277, fixed (owner ruling): the NOTIF_COMMUNITY arm checked user and
    instance blocks but not `blocked_communities`, and blocking a community
    deletes no subscription, so a subscriber who blocked it still got every
    post in it. A blocked community is now suppressed.
    """
    community, post, author = _seed_scenario()
    instance = _peer_instance()
    blocker = make_user(instance, 'community_blocker', local=True)
    subscriber = make_user(instance, 'subscriber', local=True)
    _subscribe(blocker, community.id, NOTIF_COMMUNITY)
    _subscribe(subscriber, community.id, NOTIF_COMMUNITY)
    make_community_block(blocker, community)
    db.session.commit()

    notify_about_post_task(post.id)

    assert _notifications_for(blocker) == []
    assert len(_notifications_for(subscriber)) == 1


def test_a_community_subscriber_who_blocked_the_instance_is_not_notified(app, db_session):
    """`post.instance_id not in blocked_ints`, where

        blocked_ints = blocked_or_banned_instances(notify_id)

    is `[block.instance_id for block in blocks] + banned_instances(user_id)`
    over `InstanceBlock` rows filtered on `user_id` (app/utils.py:1730-1731).
    This test exercises the `InstanceBlock` half, which is what
    `make_instance_block` writes.

    The instance compared is `post.instance_id`, and `make_post` sets
    `instance_id=user.instance_id` -- the author's instance, PEER -- so PEER is
    the instance the blocker has to block.

    As above, a second subscriber who blocked nothing is notified in the same
    run.
    """
    community, post, author = _seed_scenario()
    instance = _peer_instance()
    blocker = make_user(instance, 'instance_blocker', local=True)
    subscriber = make_user(instance, 'subscriber', local=True)
    _subscribe(blocker, community.id, NOTIF_COMMUNITY)
    _subscribe(subscriber, community.id, NOTIF_COMMUNITY)
    make_instance_block(blocker, instance)
    db.session.commit()

    notify_about_post_task(post.id)

    assert _notifications_for(blocker) == []
    assert len(_notifications_for(subscriber)) == 1


def test_the_notified_community_subscribers_unread_counter_is_incremented(app, db_session):
    """This arm's copy of the two lines after `session.add(new_notification)`:

        user = session.query(User).get(notify_id)
        user.unread_notifications += 1

    `User.unread_notifications` is `db.Column(db.Integer, default=0)`
    (app/models.py:1023) and `make_user` never sets it, so the counter is
    seeded to 7 first: asserting 8 afterwards cannot be satisfied by the
    column's default, and `+= 1` is distinguished from an assignment of a
    constant.

    The `db.session.refresh` and the fresh query in `_notifications_for` are
    the precautions that module docstring describes: the task commits on
    `get_task_session()`, which does not expire anything `db.session` holds, so
    neither read is allowed to depend on whether the arrange block's own
    `db.session.commit()` left the attribute expired.
    """
    community, post, author = _seed_scenario()
    subscriber = make_user(_peer_instance(), 'subscriber', local=True)
    subscriber.unread_notifications = 7
    _subscribe(subscriber, community.id, NOTIF_COMMUNITY)
    db.session.commit()

    notify_about_post_task(post.id)

    db.session.refresh(subscriber)
    assert subscriber.unread_notifications == 8
    assert len(_notifications_for(subscriber)) == 1


# ---------------------------------------------------------------------------
# notify_about_post_task -- the NOTIF_TOPIC arm
# ---------------------------------------------------------------------------

def _seed_topic(community, name='news'):
    """A Topic, attached to `community`, at an id nothing else in this file reaches.

    tests/factories.py has no Topic factory -- nothing in it names the class --
    so this seeds by hand the two columns the arm's `targets` dict reads,

        machine_name = db.Column(db.String(50), index=True)
        name = db.Column(db.String(50))

    (app/models.py:528-529), and sets the foreign key the arm looks the topic
    up through, `topic_id = db.Column(db.Integer, db.ForeignKey('topic.id'),
    index=True)` (app/models.py:582).

    The two columns are given DIFFERENT strings -- 'news' and 'News' -- because
    the dict reads both, as `'topic_name': topic.name` and
    `'topic_machine_name': topic.machine_name`
    (app/activitypub/util.py:2885-2886). Equal strings would make a swap of
    those two entries invisible.

    The explicit `id` is the point of the helper. tests/conftest.py truncates
    every table with `RESTART IDENTITY`, so each sequence restarts at 1 in each
    test, and `_seed_scenario` gives its Community primary key 1 and its Post
    primary key 1 -- a Topic left to its own sequence collides with both. The
    entity id is this arm's ONLY discriminator from its three siblings: it
    calls `notification_subscribers(post.community.topic_id, NOTIF_TOPIC)`
    (app/activitypub/util.py:2869) where the arm above calls
    `notification_subscribers(post.community_id, NOTIF_COMMUNITY)`
    (app/activitypub/util.py:2846) and the arm above that
    `notification_subscribers(post.user_id, NOTIF_USER)`
    (app/activitypub/util.py:2821). A topic sharing the community's id -- or,
    since the NOTIF_FEED arm below, a feed's -- would leave the substitution of
    one entity id for another alive in every test here.

    9 is this helper's reserved id and nothing else in the file may take it.
    The invariant to hold is NOT a census of the ids the file reaches, which
    every task appending to it falsifies again; it is that no test asserts on an
    entity id equal to another entity id in its own scope. This sentence is not
    what enforces that: each test turning on an entity id asserts the
    distinctness itself, `assert len({community.id, topic.id, feed.id}) == 3` in
    `test_a_subscriber_already_notified_by_the_topic_arm_is_not_notified_again`
    and the wider `len({...})` guards in the two arms' happy paths.

    Call this helper ONCE per test. `Topic(id=9, ...)` inserts an explicit
    primary key, which does not advance `topic_id_seq`, so a second Topic left
    to the sequence takes id 1 -- the Community's -- and reintroduces exactly
    the collision this helper exists to avoid.
    """
    topic = Topic(id=9, machine_name=name, name=name.title())
    db.session.add(topic)
    db.session.commit()
    community.topic_id = topic.id
    db.session.commit()
    return topic


def test_a_subscriber_to_the_communitys_topic_is_notified(app, db_session):
    """The NOTIF_TOPIC arm's happy path, quoted whole from
    app/activitypub/util.py:2869-2880:

        topic_send_notifs_to = notification_subscribers(post.community.topic_id, NOTIF_TOPIC)
        if post.community.topic_id:
            topic = session.query(Topic).get(post.community.topic_id)
        for notify_id in topic_send_notifs_to:
            blocked_senders = blocked_users(notify_id)
            blocked_comms = blocked_communities(notify_id)
            blocked_ints = blocked_or_banned_instances(notify_id)
            if notify_id != post.user_id and \\
                    notify_id not in notifications_sent_to and \\
                    post.user_id not in blocked_senders and \\
                    post.community_id not in blocked_comms and \\
                    post.instance_id not in blocked_ints:

    Three per-recipient block lookups, where the two arms above have two each:
    NOTIF_USER omits `blocked_users` and NOTIF_COMMUNITY omits
    `blocked_communities`. This arm calls all three.

    The subscription's entity_id is the TOPIC's id. The community is attached
    to the topic and the subscriber to the topic, never to the community, so
    nothing here can be notified by the NOTIF_COMMUNITY arm instead.

    Its `targets` dict, quoted whole from app/activitypub/util.py:2881-2887:

        targets_data = {'gen': '0',
                        'post_id': post.id,
                        'post_title': post.title,
                        'community_name': community.ap_id if community.ap_id else community.name,
                        'topic_name': topic.name,
                        'topic_machine_name': topic.machine_name,
                        'author_id': post.user_id}

    `topic_name` and `topic_machine_name` are what tell this arm's stored state
    apart from the other three; `author_id` it shares with the NOTIF_USER arm,
    which additionally carries `author_user_name`.

    **The ids in this arm's scope are made pairwise distinct**, and the
    `len({...}) == 5` below fails loudly if factory ordering ever changes.
    `_seed_scenario` gives its Community and its Post the same primary key, 1,
    and its author user id 2; a Topic left to its own sequence would be 1 as
    well, and a second Post would be 2. So this test seeds its own author
    BEFORE its own post -- which puts the author at 3 and leaves the post at 2
    -- takes the topic id from `_seed_topic`, and seeds its subscriber last, at
    4. Both id-valued entries of the dict, `post_id` and `author_id`, are then
    distinguishable from each other and from `post.community_id`,
    `post.community.topic_id` and `notify_id`.

    `community.ap_id` is set for the reason Tasks 2 and 3 record against their
    own happy paths: `community.ap_id if community.ap_id else community.name`
    is a ternary and `make_community` sets `ap_profile_id` but never `ap_id`,
    so left alone the ternary takes its else-arm and a swap of its arms is
    invisible.

    `notif_type` is asserted against `NOTIF_TOPIC`, which is `2`
    (app/constants.py:54), while the column's declared default is
    `NOTIF_DEFAULT`, `999` (app/models.py:3736) -- contrary to the default, not
    a restatement of it. `subtype` has no declared default at all
    (`subtype = db.Column(db.String(50), index=True)`, app/models.py:3737).

    The unread counter is asserted here rather than in a test of its own, which
    is where the two arms above put theirs. This arm's copy of

        user = session.query(User).get(notify_id)
        user.unread_notifications += 1

    (app/activitypub/util.py:2894-2895) would otherwise go unasserted: the six
    tests this arm gets are all spoken for by its five conjuncts and its third
    block lookup. `User.unread_notifications` is `db.Column(db.Integer,
    default=0)` (app/models.py:1023) and `make_user` never sets it, so it is
    seeded to 7 first -- asserting 8 afterwards cannot be satisfied by the
    column's default, and `+= 1` is distinguished from an assignment of a
    constant.
    """
    community, seeded_post, seeded_author = _seed_scenario()
    community.ap_id = f'microblogs@{PEER}'
    topic = _seed_topic(community)
    instance = _peer_instance()
    author = make_user(instance, 'topic_author')
    post = make_post(community, author, ap_id=f'https://{PEER}/post/2')
    subscriber = make_user(instance, 'subscriber', local=True)
    subscriber.unread_notifications = 7
    _subscribe(subscriber, topic.id, NOTIF_TOPIC)
    db.session.commit()
    assert len({community.id, post.id, author.id, subscriber.id, topic.id}) == 5

    notify_about_post_task(post.id)

    notifications = _notifications_for(subscriber)
    assert len(notifications) == 1
    notification = notifications[0]
    assert notification.notif_type == NOTIF_TOPIC
    assert notification.subtype == 'new_post_in_followed_topic'
    assert notification.url == f'/post/{post.id}'
    assert notification.title == 'a post'
    assert notification.author_id == author.id
    assert notification.targets == {'gen': '0',
                                    'post_id': post.id,
                                    'post_title': 'a post',
                                    'community_name': f'microblogs@{PEER}',
                                    'topic_name': 'News',
                                    'topic_machine_name': 'news',
                                    'author_id': author.id}
    db.session.refresh(subscriber)
    assert subscriber.unread_notifications == 8


def test_the_author_is_not_notified_even_when_subscribed_to_their_communitys_topic(app, db_session):
    """`notify_id != post.user_id`, this arm's first conjunct.

    The row is one production writes. `topic_notification`
    (app/topic/routes.py:291) is the only route that creates a NOTIF_TOPIC
    subscription:

        new_notification = NotificationSubscription(name=topic.name, user_id=current_user.id, entity_id=topic.id,
                                                    type=NOTIF_TOPIC)

    (app/topic/routes.py:301-302) -- reached for any logged-in user with no
    subscription to that topic yet, comparing the subscriber against nobody. So
    an author who follows a topic and then posts into one of its communities is
    ordinary state, and this conjunct is a live filter here rather than a
    defensive guard.

    A second subscriber to the same topic IS notified in the same run, so the
    author's empty result says "this run created nothing for the author" rather
    than "this run created nothing at all".
    """
    community, post, author = _seed_scenario()
    topic = _seed_topic(community)
    subscriber = make_user(_peer_instance(), 'subscriber', local=True)
    _subscribe(author, topic.id, NOTIF_TOPIC)
    _subscribe(subscriber, topic.id, NOTIF_TOPIC)
    db.session.commit()

    notify_about_post_task(post.id)

    assert _notifications_for(author) == []
    assert len(_notifications_for(subscriber)) == 1


def test_a_subscriber_already_notified_by_the_community_arm_is_not_notified_again(app, db_session):
    """`notify_id not in notifications_sent_to`, this arm's second conjunct.

    `notifications_sent_to = set()` is initialised once above all four arms and
    each arm ends the body of its `if` with
    `notifications_sent_to.add(notify_id)` -- at app/activitypub/util.py:2843,
    :2866, :2897 and :2931, the last of those put there by commit `0489dc1d`
    (register entry D275). The arms run in file order: `# NOTIF_USER` at
    app/activitypub/util.py:2820, `# NOTIF_COMMUNITY` at :2845,
    `# NOTIF_TOPIC` at :2868, `# NOTIF_FEED` at :2899. The filler used here is
    the IMMEDIATELY PRECEDING arm, NOTIF_COMMUNITY; the NOTIF_COMMUNITY arm's
    own copy of this conjunct is killed with the arm before IT, NOTIF_USER,
    and in the NOTIF_USER arm itself the conjunct is unkillable, because
    nothing writes to the set between its initialisation and that arm's loop.

    `dual` is subscribed BOTH to the community (NOTIF_COMMUNITY) and to the
    topic (NOTIF_TOPIC). The community arm wins because it runs first, so
    `dual` gets exactly ONE row and its `targets` is that arm's shape: a
    `community_id` entry and no `topic_name`.

    `topic_only`, subscribed to the topic alone, is notified in the same run
    and IS given a NOTIF_TOPIC row. Without it, a mutant that stopped the topic
    arm firing altogether would still leave `dual` holding exactly one row and
    pass.
    """
    community, post, author = _seed_scenario()
    topic = _seed_topic(community)
    instance = _peer_instance()
    dual = make_user(instance, 'dual_subscriber', local=True)
    topic_only = make_user(instance, 'topic_subscriber', local=True)
    _subscribe(dual, community.id, NOTIF_COMMUNITY)
    _subscribe(dual, topic.id, NOTIF_TOPIC)
    _subscribe(topic_only, topic.id, NOTIF_TOPIC)
    db.session.commit()

    notify_about_post_task(post.id)

    dual_notifications = _notifications_for(dual)
    assert len(dual_notifications) == 1
    assert dual_notifications[0].notif_type == NOTIF_COMMUNITY
    assert dual_notifications[0].subtype == 'new_post_in_followed_community'
    assert 'topic_name' not in dual_notifications[0].targets
    assert 'community_id' in dual_notifications[0].targets

    control_notifications = _notifications_for(topic_only)
    assert len(control_notifications) == 1
    assert control_notifications[0].notif_type == NOTIF_TOPIC
    assert control_notifications[0].subtype == 'new_post_in_followed_topic'


def test_a_topic_subscriber_who_blocked_the_author_is_not_notified(app, db_session):
    """`post.user_id not in blocked_senders`, where

        blocked_senders = blocked_users(notify_id)

    is

        blocks = db.session.query(UserBlock).filter_by(blocker_id=user_id)
        return [block.blocked_id for block in blocks]

    (app/utils.py:1746-1747) -- the recipient is the BLOCKER and the post's
    author is the BLOCKED, which is the order `make_user_block(blocker,
    blocked)` writes.

    A second subscriber who blocked nobody is notified in the same run, so
    "no rows for the blocker" is distinguishable from "no rows at all".
    """
    community, post, author = _seed_scenario()
    topic = _seed_topic(community)
    instance = _peer_instance()
    blocker = make_user(instance, 'author_blocker', local=True)
    subscriber = make_user(instance, 'subscriber', local=True)
    _subscribe(blocker, topic.id, NOTIF_TOPIC)
    _subscribe(subscriber, topic.id, NOTIF_TOPIC)
    make_user_block(blocker, author)
    db.session.commit()

    notify_about_post_task(post.id)

    assert _notifications_for(blocker) == []
    assert len(_notifications_for(subscriber)) == 1


def test_a_topic_subscriber_who_blocked_the_community_is_not_notified(app, db_session):
    """`post.community_id not in blocked_comms`, where

        blocked_comms = blocked_communities(notify_id)

    is

        blocks = db.session.query(CommunityBlock).filter_by(user_id=user_id)
        return [block.community_id for block in blocks]

    (app/utils.py:1722-1723).

    This is the third block lookup, the one that makes this arm's set of three
    a superset of either arm above: NOTIF_USER computes `blocked_communities`
    and `blocked_or_banned_instances`, NOTIF_COMMUNITY computes `blocked_users`
    and `blocked_or_banned_instances`, and this arm computes all three, at
    app/activitypub/util.py:2873-2875.

    Following a topic while blocking one community inside it is the pairing
    that makes this filter matter: the recipient subscribes to the topic, not
    to the community, so nothing but this conjunct can keep the post out.

    A second subscriber who blocked nothing is notified in the same run.
    """
    community, post, author = _seed_scenario()
    topic = _seed_topic(community)
    instance = _peer_instance()
    blocker = make_user(instance, 'community_blocker', local=True)
    subscriber = make_user(instance, 'subscriber', local=True)
    _subscribe(blocker, topic.id, NOTIF_TOPIC)
    _subscribe(subscriber, topic.id, NOTIF_TOPIC)
    make_community_block(blocker, community)
    db.session.commit()

    notify_about_post_task(post.id)

    assert _notifications_for(blocker) == []
    assert len(_notifications_for(subscriber)) == 1


def test_a_topic_subscriber_who_blocked_the_instance_is_not_notified(app, db_session):
    """`post.instance_id not in blocked_ints`, where

        blocked_ints = blocked_or_banned_instances(notify_id)

    is

        blocks = db.session.query(InstanceBlock).filter_by(user_id=user_id)
        return [block.instance_id for block in blocks] + banned_instances(user_id)

    (app/utils.py:1730-1731). This test exercises the `InstanceBlock` half,
    which is what `make_instance_block` writes.

    The instance compared is `post.instance_id`, and `make_post` sets
    `instance_id=user.instance_id` -- the author's instance, PEER -- so PEER is
    the instance the blocker has to block.

    A second subscriber who blocked nothing is notified in the same run.
    """
    community, post, author = _seed_scenario()
    topic = _seed_topic(community)
    instance = _peer_instance()
    blocker = make_user(instance, 'instance_blocker', local=True)
    subscriber = make_user(instance, 'subscriber', local=True)
    _subscribe(blocker, topic.id, NOTIF_TOPIC)
    _subscribe(subscriber, topic.id, NOTIF_TOPIC)
    make_instance_block(blocker, instance)
    db.session.commit()

    notify_about_post_task(post.id)

    assert _notifications_for(blocker) == []
    assert len(_notifications_for(subscriber)) == 1


# ---------------------------------------------------------------------------
# notify_about_post_task -- the NOTIF_FEED arm, and the cross-arm dedup set
# ---------------------------------------------------------------------------

def _seed_feed(community, instance, name, feed_id, title):
    """A Feed at an id nothing else in this file reaches, holding `community`.

    `make_feed` (tests/factories.py:154) builds the row; this wrapper adds the
    two things it cannot express, plus the `FeedItem` the arm's query joins
    through.

    **The explicit primary key.** tests/conftest.py truncates every table with
    `RESTART IDENTITY`, so `feed_id_seq` restarts at 1 in each test, and
    `_seed_scenario` gives its Community primary key 1. This arm's `targets`
    dict carries `'feed_id': feed.id` (app/activitypub/util.py:2919), and its
    guard reads `post.community_id` (app/activitypub/util.py:2913), so a feed
    left on the sequence would make a substitution of the community id for the
    feed id invisible -- the silently-vacuous shape fact 89 in tests/README.md
    records, met first on `targets['community_id']` in the NOTIF_COMMUNITY arm
    and headed off in the same way for the Topic. `make_feed` has no `id`
    parameter, so the key is reassigned after its insert and before any
    `FeedItem` or `NotificationSubscription` names it; nothing points at the
    row yet, so the UPDATE has no dependants.

    The tests below pass 7, 8 and 10. **9 is `_seed_topic`'s reserved id and no
    feed takes it**, so a later test that seeds a Topic and a Feed together
    cannot have its entity-id distinctness guard satisfied by accident -- topic
    id against feed id is the substitution mutation M13 exists to catch.

    **`title` different from `name`.** `make_feed` passes one string to both
    columns (`Feed(name=name, title=name, ...)`, tests/factories.py:176), but
    the model treats them as different things,

        title = db.Column(db.String(256))  # Human name
        name = db.Column(db.String(256), index=True, unique=True)  # url

    (app/models.py:4080-4081), and the arm reads only the first, as
    `'feed_name': feed.title` (app/activitypub/util.py:2920). Equal strings
    would leave a mutation of that entry to `feed.name` alive. `name` is
    `unique=True`, so every feed in a test needs its own.

    `community` is a parameter rather than the post's community by assumption,
    so a caller can seed a feed holding some OTHER community -- the case
    `.filter(FeedItem.community_id == post.community_id)`
    (app/activitypub/util.py:2902) exists to exclude.
    """
    feed = make_feed(instance, name=name)
    feed.title = title
    feed.id = feed_id
    db.session.commit()
    make_feed_item(feed, community)
    return feed


def test_a_subscriber_to_a_feed_containing_the_community_is_notified(app, db_session):
    """The NOTIF_FEED arm's happy path, quoted whole from
    app/activitypub/util.py:2901-2914:

        community_feeds = session.query(Feed).join(FeedItem, FeedItem.feed_id == Feed.id).filter(
            FeedItem.community_id == post.community_id).all()

        for feed in community_feeds:
            feed_send_notifs_to = notification_subscribers(feed.id, NOTIF_FEED)
            for notify_id in feed_send_notifs_to:
                blocked_senders = blocked_users(notify_id)
                blocked_comms = blocked_communities(notify_id)
                blocked_ints = blocked_or_banned_instances(notify_id)
                if notify_id != post.user_id and \\
                        notify_id not in notifications_sent_to and \\
                        post.user_id not in blocked_senders and \\
                        post.community_id not in blocked_comms and \\
                        post.instance_id not in blocked_ints:

    This arm alone finds its recipients through two lookups rather than one:
    a query for the feeds the post's community belongs to, then
    `notification_subscribers` once per feed. The three arms above each call
    `notification_subscribers` a single time --
    `notification_subscribers(post.user_id, NOTIF_USER)`
    (app/activitypub/util.py:2821),
    `notification_subscribers(post.community_id, NOTIF_COMMUNITY)` (:2846) and
    `notification_subscribers(post.community.topic_id, NOTIF_TOPIC)` (:2869)
    -- so this is the only arm with a nested loop, and the only one whose
    subscription entity id comes out of a query rather than off an attribute of
    the post.

    Its three per-recipient block lookups (app/activitypub/util.py:2907-2909)
    are the same three the NOTIF_TOPIC arm computes: NOTIF_USER omits
    `blocked_users` and NOTIF_COMMUNITY omits `blocked_communities`, and both
    of the remaining arms compute all three.

    Its `targets` dict, quoted whole from app/activitypub/util.py:2915-2921:

        targets_data = {'gen': '0',
                        'post_id': post.id,
                        'post_title': post.title,
                        'community_name': community.ap_id if community.ap_id else community.name,
                        'feed_id': feed.id,
                        'feed_name': feed.title
                        }

    `feed_id` and `feed_name` are what tell this arm's stored state apart from
    the other three. It is the only one of the four that carries no `author_id`
    (NOTIF_USER and NOTIF_TOPIC do) and no `community_id` (NOTIF_COMMUNITY
    does).

    `unrelated` subscribes to `other_feed`, whose only `FeedItem` names a
    DIFFERENT community, and receives nothing while `subscriber` receives one
    row: that is the assertion for the query's own filter,
    `.filter(FeedItem.community_id == post.community_id)`
    (app/activitypub/util.py:2902). A feed with no `FeedItem` at all would not
    do -- the inner join alone excludes it, and the filter could then be deleted
    unnoticed. The notified `subscriber` is what makes `unrelated`'s empty
    result mean "the query excluded them" rather than "nothing ran".

    **The ids in scope are made pairwise distinct** and the `len({...}) == 7`
    below fails loudly if factory ordering changes. `_seed_scenario` gives its
    Community and its Post the same primary key, 1, and seeds users 1 and 2, so
    this test seeds its own author BEFORE its own post -- author 3, post 2 --
    then its two recipients, 4 and 5, and takes both feed ids from `_seed_feed`.
    Every id the `targets` dict could be mutated to name -- `post.id`,
    `post.community_id`, `post.user_id`, `notify_id` and the other feed's id --
    is then a different number from `feed.id`. `other_community` is left off
    that set deliberately: no expression in the arm evaluates to it, so it is
    not a value any mutation of the `targets` dict could produce.

    `community.ap_id` is set for the reason Tasks 2, 3 and 4 record against
    their own happy paths: `community.ap_id if community.ap_id else
    community.name` is a ternary and `make_community` sets `ap_profile_id` but
    never `ap_id`, so left alone the ternary takes its else-arm and a swap of
    its arms is invisible.

    `notif_type` is asserted against `NOTIF_FEED`, which is `5`
    (app/constants.py:58), while the column's declared default is
    `NOTIF_DEFAULT`, `999` (app/models.py:3736) -- contrary to the default, not
    a restatement of it. `subtype` has no declared default at all
    (`subtype = db.Column(db.String(50), index=True)`, app/models.py:3737).

    The unread counter is asserted here rather than in a test of its own, for
    the reason recorded against the arm above: six tests are allotted to this
    arm and all six are spoken for by the five conjuncts, the dedup set and the
    pin, so this arm's copy of

        user = session.query(User).get(notify_id)
        user.unread_notifications += 1

    (app/activitypub/util.py:2928-2929) would otherwise go unasserted.
    `User.unread_notifications` is `db.Column(db.Integer, default=0)`
    (app/models.py:1023) and `make_user` never sets it, so it is seeded to 7
    first -- asserting 8 afterwards cannot be satisfied by the column's default,
    and `+= 1` is distinguished from an assignment of a constant.
    """
    community, seeded_post, seeded_author = _seed_scenario()
    community.ap_id = f'microblogs@{PEER}'
    instance = _peer_instance()
    author = make_user(instance, 'feed_author')
    post = make_post(community, author, ap_id=f'https://{PEER}/post/2')
    subscriber = make_user(instance, 'subscriber', local=True)
    subscriber.unread_notifications = 7
    unrelated = make_user(instance, 'unrelated_subscriber', local=True)
    other_community = make_community(name='othercommunity', host=PEER)
    feed = _seed_feed(community, instance, name='afeed', feed_id=8, title='A Feed')
    other_feed = _seed_feed(other_community, instance, name='otherfeed', feed_id=7,
                            title='Another Feed')
    _subscribe(subscriber, feed.id, NOTIF_FEED)
    _subscribe(unrelated, other_feed.id, NOTIF_FEED)
    db.session.commit()
    assert len({community.id, post.id, author.id, subscriber.id, unrelated.id,
                feed.id, other_feed.id}) == 7

    notify_about_post_task(post.id)

    notifications = _notifications_for(subscriber)
    assert len(notifications) == 1
    notification = notifications[0]
    assert notification.notif_type == NOTIF_FEED
    assert notification.subtype == 'new_post_in_followed_feed'
    assert notification.url == f'/post/{post.id}'
    assert notification.title == 'a post'
    assert notification.author_id == author.id
    assert notification.targets == {'gen': '0',
                                    'post_id': post.id,
                                    'post_title': 'a post',
                                    'community_name': f'microblogs@{PEER}',
                                    'feed_id': feed.id,
                                    'feed_name': 'A Feed'}
    assert _notifications_for(unrelated) == []
    db.session.refresh(subscriber)
    assert subscriber.unread_notifications == 8


def test_the_author_is_not_notified_even_when_subscribed_to_a_feed(app, db_session):
    """`notify_id != post.user_id`, this arm's first conjunct
    (app/activitypub/util.py:2910).

    The row is one production writes. `feed_notification`
    (app/feed/routes.py:316) is the only route that creates a NOTIF_FEED
    subscription:

        new_notification = NotificationSubscription(name=feed.name, user_id=current_user.id, entity_id=feed.id,
                                                    type=NOTIF_FEED)

    (app/feed/routes.py:326-327) -- reached for any logged-in user with no
    subscription to that feed yet, comparing the subscriber against nobody. So
    an author who follows a feed and then posts into one of its communities is
    ordinary state, and this conjunct is a live filter here rather than a
    defensive guard.

    A second subscriber to the same feed IS notified in the same run, so the
    author's empty result says "this run created nothing for the author" rather
    than "this run created nothing at all".
    """
    community, post, author = _seed_scenario()
    instance = _peer_instance()
    subscriber = make_user(instance, 'subscriber', local=True)
    feed = _seed_feed(community, instance, name='afeed', feed_id=8, title='A Feed')
    _subscribe(author, feed.id, NOTIF_FEED)
    _subscribe(subscriber, feed.id, NOTIF_FEED)
    db.session.commit()

    notify_about_post_task(post.id)

    assert _notifications_for(author) == []
    assert len(_notifications_for(subscriber)) == 1


def test_a_subscriber_already_notified_by_the_topic_arm_is_not_notified_again(app, db_session):
    """`notify_id not in notifications_sent_to`, this arm's second conjunct
    (app/activitypub/util.py:2911) -- and the cross-arm de-duplication itself.

    `notifications_sent_to = set()` is initialised once above all four arms.
    The arms run in file order: `# NOTIF_USER` at app/activitypub/util.py:2820,
    `# NOTIF_COMMUNITY` at :2845, `# NOTIF_TOPIC` at :2868, `# NOTIF_FEED` at
    :2899. The filler used here is the IMMEDIATELY PRECEDING arm, NOTIF_TOPIC,
    which completes the chain: Task 3 filled the community arm's set from
    NOTIF_USER and Task 4 filled the topic arm's from NOTIF_COMMUNITY, and in
    the NOTIF_USER arm itself the conjunct is unkillable, because nothing writes
    to the set between its initialisation and that arm's loop.

    `dual` is subscribed BOTH to the community's topic (NOTIF_TOPIC) and to a
    feed the community is in (NOTIF_FEED). The topic arm wins because it runs
    first, so `dual` gets exactly ONE row and its `targets` is that arm's shape:
    a `topic_name` entry and no `feed_id`.

    `feed_only`, subscribed to the feed alone, is notified in the same run and
    IS given a NOTIF_FEED row. Without it, a mutant that stopped the feed arm
    firing altogether would still leave `dual` holding exactly one row and pass.

    This is the arm-crossing half of the set's job. The other half, one
    recipient reached twice by THIS arm's own outer loop over feeds, is asserted
    by `test_a_feed_subscriber_who_blocked_the_instance_is_skipped_for_every_feed`
    below, whose `dual_feed` control subscribes to two feeds at once.

    The three entity ids in play -- the community's, the topic's and the feed's
    -- are asserted distinct because the arms differ from each other only in
    which id they hand `notification_subscribers`: `feed.id` at
    app/activitypub/util.py:2905, `post.community.topic_id` at :2869,
    `post.community_id` at :2846. Two of them equal would let the feed arm read
    the topic's or the community's subscriber list unnoticed.
    """
    community, post, author = _seed_scenario()
    topic = _seed_topic(community)
    instance = _peer_instance()
    dual = make_user(instance, 'dual_subscriber', local=True)
    feed_only = make_user(instance, 'feed_subscriber', local=True)
    feed = _seed_feed(community, instance, name='afeed', feed_id=8, title='A Feed')
    _subscribe(dual, topic.id, NOTIF_TOPIC)
    _subscribe(dual, feed.id, NOTIF_FEED)
    _subscribe(feed_only, feed.id, NOTIF_FEED)
    db.session.commit()
    assert len({community.id, topic.id, feed.id}) == 3

    notify_about_post_task(post.id)

    dual_notifications = _notifications_for(dual)
    assert len(dual_notifications) == 1
    assert dual_notifications[0].notif_type == NOTIF_TOPIC
    assert dual_notifications[0].subtype == 'new_post_in_followed_topic'
    assert 'feed_id' not in dual_notifications[0].targets
    assert 'topic_name' in dual_notifications[0].targets

    control_notifications = _notifications_for(feed_only)
    assert len(control_notifications) == 1
    assert control_notifications[0].notif_type == NOTIF_FEED
    assert control_notifications[0].subtype == 'new_post_in_followed_feed'
    assert control_notifications[0].targets['feed_id'] == feed.id


def test_a_feed_subscriber_who_blocked_the_author_is_not_notified(app, db_session):
    """`post.user_id not in blocked_senders`, this arm's third conjunct
    (app/activitypub/util.py:2912), where

        blocked_senders = blocked_users(notify_id)

    (app/activitypub/util.py:2907) is

        blocks = db.session.query(UserBlock).filter_by(blocker_id=user_id)
        return [block.blocked_id for block in blocks]

    (app/utils.py:1746-1747) -- the recipient is the BLOCKER and the post's
    author is the BLOCKED, which is the order `make_user_block(blocker,
    blocked)` writes.

    A second subscriber to the same feed who blocked nobody is notified in the
    same run, so "no rows for the blocker" is distinguishable from "no rows at
    all".
    """
    community, post, author = _seed_scenario()
    instance = _peer_instance()
    blocker = make_user(instance, 'author_blocker', local=True)
    subscriber = make_user(instance, 'subscriber', local=True)
    feed = _seed_feed(community, instance, name='afeed', feed_id=8, title='A Feed')
    _subscribe(blocker, feed.id, NOTIF_FEED)
    _subscribe(subscriber, feed.id, NOTIF_FEED)
    make_user_block(blocker, author)
    db.session.commit()

    notify_about_post_task(post.id)

    assert _notifications_for(blocker) == []
    assert len(_notifications_for(subscriber)) == 1


def test_a_feed_subscriber_who_blocked_the_community_is_not_notified(app, db_session):
    """`post.community_id not in blocked_comms`, this arm's fourth conjunct
    (app/activitypub/util.py:2913), where

        blocked_comms = blocked_communities(notify_id)

    (app/activitypub/util.py:2908) is

        blocks = db.session.query(CommunityBlock).filter_by(user_id=user_id)
        return [block.community_id for block in blocks]

    (app/utils.py:1722-1723).

    Following a feed while blocking one community inside it is the pairing that
    makes this filter matter: the recipient subscribes to the feed, not to the
    community, so nothing but this conjunct can keep the post out. It is also
    the conjunct that makes the community's own id live in this arm, which is
    why `_seed_feed` keeps the feed id off it.

    A second subscriber to the same feed who blocked nothing is notified in the
    same run.
    """
    community, post, author = _seed_scenario()
    instance = _peer_instance()
    blocker = make_user(instance, 'community_blocker', local=True)
    subscriber = make_user(instance, 'subscriber', local=True)
    feed = _seed_feed(community, instance, name='afeed', feed_id=8, title='A Feed')
    _subscribe(blocker, feed.id, NOTIF_FEED)
    _subscribe(subscriber, feed.id, NOTIF_FEED)
    make_community_block(blocker, community)
    db.session.commit()

    notify_about_post_task(post.id)

    assert _notifications_for(blocker) == []
    assert len(_notifications_for(subscriber)) == 1


def test_a_feed_subscriber_who_blocked_the_instance_is_skipped_for_every_feed(app, db_session):
    """`post.instance_id not in blocked_ints`, this arm's fifth conjunct
    (app/activitypub/util.py:2914) -- and the pin on the arm's
    `notifications_sent_to.add`, which commit `0489dc1d` (register entry D275)
    moved inside the `if`.

        blocked_ints = blocked_or_banned_instances(notify_id)

    (app/activitypub/util.py:2909) is

        blocks = db.session.query(InstanceBlock).filter_by(user_id=user_id)
        return [block.instance_id for block in blocks] + banned_instances(user_id)

    (app/utils.py:1730-1731). This test exercises the `InstanceBlock` half,
    which is what `make_instance_block` writes. The instance compared is
    `post.instance_id`, and `make_post` sets `instance_id=user.instance_id` --
    the author's instance, PEER -- so PEER is the instance the blocker has to
    block.

    **Commit `0489dc1d` -- this sub-project's one production change, register
    entry D275 -- changed the code under this test, and the assertion below did
    not change with it.** What that assertion observes is behaviour INVARIANT to
    the change, not behaviour specific to either spelling -- the next paragraph
    proves that, and it is the reason the test was neither inverted nor deleted
    when the fix landed. `notifications_sent_to.add(notify_id)` is now the
    last statement of the `if` body in all four arms: at
    app/activitypub/util.py:2843, :2866 and :2897 it is indented 20 spaces, one
    level in from the `if` that governs it at :2825, :2850 and :2876, each
    indented 16; in this arm it is at :2931, indented 24, one level in from its
    own `if` at :2910, indented 20. This arm sits one level deeper than the
    other three throughout because its subscriber loop is itself nested inside
    `for feed in community_feeds:` (:2904). Before `0489dc1d`, :2931 was indented
    20 -- the SAME indentation as its own `if` -- so it was not in the `if`
    body at all but the last statement of
    `for notify_id in feed_send_notifs_to:` (:2906), and it ran for every
    subscriber the arm looked at, notified or filtered. `blocker` was therefore
    added to `notifications_sent_to` while whichever of the two feeds the query
    returned first was being processed, even though the instance block had kept
    them out of it, and the remaining feed rejected them on
    `notify_id not in notifications_sent_to` (:2911) rather than on the
    instance block.

    **The assertion below is identical on both sides of that fix**, and this
    docstring says so rather than claiming an inversion that never happened.
    Every conjunct of the guard at :2910-2914 is constant across iterations of
    `for feed in community_feeds:` (:2904) -- `notify_id`, `post.user_id`,
    `post.community_id`, `post.instance_id` and the three per-recipient block
    lists at :2907-2909 are all computed without reference to `feed` -- and the
    one conjunct that can change value,
    `notify_id not in notifications_sent_to`, can only go from true to false.
    So a recipient the guard rejects at one feed is rejected at every later
    feed by the same conjunct that rejected them first, on either side of the
    `if`; and NOTIF_FEED is the last arm, its `except Exception:` following at
    :2932, so nothing downstream reads the set either. The `add` itself is live
    -- put it on neither side and `dual_feed` below collects two rows -- but
    the EXTRA executions the old placement bought, the ones for recipients the
    guard rejected, changed no output of the function. What this test pins is
    the OBSERVABLE contract -- filtered out of one feed, notified by none --
    which is what a reader would expect to break if the fix had been made
    wrongly, and which stayed green across it. The gate on that fix was not an
    inverted assertion here but a re-run of the accumulated mutation tables
    after the fix, which confirmed that no mutant any earlier task killed came
    back unkilled.

    Two controls run alongside, and both are load-bearing:

    `dual_feed` is subscribed to BOTH feeds and blocked nothing, so this arm
    reaches them twice and `notifications_sent_to` is the only thing standing
    between them and a second row. That is the kill for
    `notify_id not in notifications_sent_to` by way of a previous iteration of
    this arm's OWN outer loop -- a route no other arm has, since the other three
    walk their subscribers once. Which of the two feeds supplies their single
    row is NOT asserted: the query at :2901-2902 has no `ORDER BY`, so
    PostgreSQL guarantees no order over `community_feeds`.

    `second_only` is subscribed to the second feed alone, so their single row
    can only have come from the iteration that processed `feed_two`, and its
    `targets['feed_id']` names `feed_two` whatever order the query returned.

    Together the two controls make `blocker`'s empty result mean "this run
    created nothing for the blocker" rather than "this run created nothing".
    """
    community, seeded_post, seeded_author = _seed_scenario()
    instance = _peer_instance()
    author = make_user(instance, 'feed_author')
    post = make_post(community, author, ap_id=f'https://{PEER}/post/2')
    blocker = make_user(instance, 'instance_blocker', local=True)
    dual_feed = make_user(instance, 'two_feed_subscriber', local=True)
    second_only = make_user(instance, 'second_feed_subscriber', local=True)
    feed_one = _seed_feed(community, instance, name='firstfeed', feed_id=8,
                          title='First Feed')
    feed_two = _seed_feed(community, instance, name='secondfeed', feed_id=10,
                          title='Second Feed')
    _subscribe(blocker, feed_one.id, NOTIF_FEED)
    _subscribe(blocker, feed_two.id, NOTIF_FEED)
    _subscribe(dual_feed, feed_one.id, NOTIF_FEED)
    _subscribe(dual_feed, feed_two.id, NOTIF_FEED)
    _subscribe(second_only, feed_two.id, NOTIF_FEED)
    make_instance_block(blocker, instance)
    db.session.commit()
    assert len({community.id, post.id, author.id, blocker.id, dual_feed.id,
                second_only.id, feed_one.id, feed_two.id}) == 8

    notify_about_post_task(post.id)

    assert _notifications_for(blocker) == []

    dual_notifications = _notifications_for(dual_feed)
    assert len(dual_notifications) == 1
    assert dual_notifications[0].notif_type == NOTIF_FEED
    assert dual_notifications[0].subtype == 'new_post_in_followed_feed'

    second_notifications = _notifications_for(second_only)
    assert len(second_notifications) == 1
    assert second_notifications[0].notif_type == NOTIF_FEED
    assert second_notifications[0].targets['feed_id'] == feed_two.id
    assert second_notifications[0].targets['feed_name'] == 'Second Feed'


# ---------------------------------------------------------------------------
# The six conditional expressions -- the arms no coverage number can see
# ---------------------------------------------------------------------------
#
# tests/README.md fact 87: coverage.py emits NO arc for a conditional
# expression, so a region at 100% statements and 100% branches can still hide
# an unexercised arm behind every `x if y else z` in it. The six below were
# enumerated by reading the three functions, whose extents come from an
# UNFILTERED `^def ` scan of app/activitypub/util.py -- `create_post` at
# :2777 to the next def, `notify_about_post` at :2796, `notify_about_post_task`
# at :2804, and the def after it, `notify_about_post_reply`, at :2939:
#
#   :2778  saved_json = request_json if store_ap_json else None
#   :2831  community.ap_id if community.ap_id else community.name   (NOTIF_USER)
#   :2833  author.ap_id if author.ap_id else author.user_name       (NOTIF_USER)
#   :2855  community.ap_id if community.ap_id else community.name   (NOTIF_COMMUNITY)
#   :2884  community.ap_id if community.ap_id else community.name   (NOTIF_TOPIC)
#   :2918  community.ap_id if community.ap_id else community.name   (NOTIF_FEED)
#
# `notify_about_post` (:2796-2800) contains none. Six, matching the plan's
# count.
#
# The four tests above whose whole-dict `targets` assertion carries a
# `community_name` key are the four arms' happy paths -- none of them subscripts
# the dict -- and each of them assigns `community.ap_id` deliberately;
# every post above takes its author from `make_user`'s default remote shape,
# which sets `ap_id`. So all five `.ap_id` ternaries already have their IF-side
# pinned, and it is their ELSE sides that are new here, one test each.
# `saved_json`'s if-side is exercised by the two `create_post` tests above but
# its VALUE is asserted by neither, so that one gets both arms in a single test.


def test_store_ap_json_decides_whether_the_log_row_carries_the_activity(app, db_session, ap_log):
    """`create_post`'s only conditional expression, its first line:

        saved_json = request_json if store_ap_json else None

    (app/activitypub/util.py:2778). It is a pure data ternary -- neither arm
    changes control flow -- and its value reaches stored state through
    `log_incoming_ap`'s

        if saved_json:
            activity_log.activity_json = json.dumps(saved_json)

    (app/activitypub/util.py:4577-4578), the only writer of that column on this
    path.

    Both arms are pinned in ONE test, by contrast, because neither is
    assertable alone. `activity_json = db.Column(db.Text)`
    (app/models.py:3692) declares no default, so `is None` on a single row
    restates the unwritten value and would pass with `log_incoming_ap`'s write
    deleted outright. The row from the `store_ap_json=True` call is the
    contrary baseline that makes the None on the second row mean "this call
    chose the else-arm".

    Both calls go through the `local_only` guard, so both log rows are the same
    guard's -- asserted, so a future divergence in which guard fires cannot be
    read as a difference in `saved_json`. Neither call reaches `Post.new`, so
    the seeded post is still the whole Post population.

    The stored side is compared after `json.loads`, not as a string.
    app/activitypub/util.py:18 is `from flask import current_app, request, g,
    url_for, json`, so the `json.dumps` above is Flask's, which sorts keys --
    MEASURED: the first spelling of this assertion compared against
    `json.dumps(document)` from the standard library and failed on key order
    alone. Round-tripping asserts what the column is for, the activity, and
    leaves the serialiser's key ordering unpinned by a test that is not about
    it.
    """
    community, seeded_post, author = _seed_scenario(local_only=True)
    document = _post_doc(name='a federated post')

    stored = create_post(store_ap_json=True, community=community,
                         request_json=document, user=author)
    not_stored = create_post(store_ap_json=False, community=community,
                             request_json=document, user=author)

    assert stored is None
    assert not_stored is None
    assert Post.query.count() == 1
    logs = ActivityPubLog.query.order_by(ActivityPubLog.id).all()
    assert len(logs) == 2
    assert [log.exception_message for log in logs] == [
        'Community is local only, post discarded',
        'Community is local only, post discarded']
    assert json.loads(logs[0].activity_json) == document
    assert logs[1].activity_json is None


def test_a_community_with_no_ap_id_is_named_by_its_name_in_the_user_arm(app, db_session):
    """The else-arm of

        'community_name': community.ap_id if community.ap_id else community.name,

    (app/activitypub/util.py:2831), the NOTIF_USER arm's copy.

    `make_community` (tests/factories.py:122-151) sets `ap_profile_id`,
    `ap_public_url`, `ap_followers_url` and `ap_domain` but never `ap_id`, and
    `ap_id = db.Column(db.String(255), index=True)` (app/models.py:594) declares
    no default, so a community straight out of the factory takes this arm. The
    four arms' happy-path tests above each assign `community.ap_id` precisely
    to escape it; this one does not, and the `assert community.ap_id is None`
    below states that as a precondition rather than leaving it to the factory's
    continued silence.

    `name` is 'microblogs', `make_community`'s default, and the if-side value
    the tests above use is `f'microblogs@{PEER}'` -- different strings, so the
    two arms are distinguishable in either direction.

    `author_user_name` is asserted alongside, at its IF-side value. The two
    ternaries sit in the same dict two lines apart, and asserting both shows
    they resolved DIFFERENTLY on the same call -- which no single-entry
    assertion can show, and which is what rules out a mutation that made both
    read the same attribute.

    No id-valued entry of the dict is asserted here, so the primary-key
    collision fact 89 in tests/README.md records -- `_seed_scenario`'s
    Community and Post both take id 1 under tests/conftest.py's
    `RESTART IDENTITY` -- cannot make any assertion below vacuous. Nothing is
    arranged against it for that reason.
    """
    community, post, author = _seed_scenario()
    assert community.ap_id is None
    assert community.name == 'microblogs'
    subscriber = make_user(_peer_instance(), 'subscriber', local=True)
    _subscribe(subscriber, author.id, NOTIF_USER)
    db.session.commit()

    notify_about_post_task(post.id)

    notifications = _notifications_for(subscriber)
    assert len(notifications) == 1
    assert notifications[0].notif_type == NOTIF_USER
    assert notifications[0].targets['community_name'] == 'microblogs'
    assert notifications[0].targets['author_user_name'] == f'author@{PEER}'


def test_a_local_author_is_named_by_their_user_name(app, db_session):
    """The else-arm of

        'author_user_name': author.ap_id if author.ap_id else author.user_name}

    (app/activitypub/util.py:2833), the file's only author ternary -- the
    NOTIF_USER arm is the only one of the four whose `targets` dict carries
    `author_user_name` at all.

    `make_user`'s local shape is what produces it:
    `ap_id=None if local else f'{name}@{instance.domain}'`
    (tests/factories.py:58), and `ap_id = db.Column(db.String(255), index=True)`
    (app/models.py:1066) declares no default. Every post above takes its author
    from the default remote shape, so all three `author_user_name` assertions
    above -- in `test_a_subscriber_to_the_author_is_notified`, in
    `test_a_subscriber_already_notified_by_the_user_arm_is_not_notified_again`
    and in the test immediately preceding this one -- expect
    `f'author@{PEER}'` and record the IF-side. This one seeds a LOCAL author
    and gives it the post.

    A local author is ordinary state on this path: the arm notifies the
    followers of whoever posted, and PieFed's own users post into their own
    communities. The author is still hung off the peer Instance, because
    `make_post` copies `instance_id=user.instance_id` onto the Post
    (tests/factories.py) and the arm's last conjunct compares
    `post.instance_id` against the recipient's blocked instances -- nothing
    here blocks any instance, so the instance is immaterial and is left where
    the rest of the file puts it.

    `community.ap_id` IS set here, unlike the test above, so exactly one of the
    dict's two ternaries flips between the two tests. `community_name` is
    asserted at its if-side value to show that.
    """
    community, seeded_post, seeded_author = _seed_scenario()
    community.ap_id = f'microblogs@{PEER}'
    instance = _peer_instance()
    author = make_user(instance, 'local_author', local=True)
    assert author.ap_id is None
    post = make_post(community, author, ap_id=f'https://{PEER}/post/2')
    subscriber = make_user(instance, 'subscriber', local=True)
    _subscribe(subscriber, author.id, NOTIF_USER)
    db.session.commit()

    notify_about_post_task(post.id)

    notifications = _notifications_for(subscriber)
    assert len(notifications) == 1
    assert notifications[0].notif_type == NOTIF_USER
    assert notifications[0].targets['author_user_name'] == 'local_author'
    assert notifications[0].targets['community_name'] == f'microblogs@{PEER}'


def test_a_community_with_no_ap_id_is_named_by_its_name_in_the_community_arm(app, db_session):
    """The else-arm of

        'community_name': community.ap_id if community.ap_id else community.name,

    (app/activitypub/util.py:2855), the NOTIF_COMMUNITY arm's copy. It is a
    separate expression on a separate line from the NOTIF_USER arm's at :2831,
    so a mutation of one leaves the other intact and each needs its own test.

    `notif_type` is asserted at `NOTIF_COMMUNITY` so the row is attributable to
    this arm rather than to a sibling: the subscriber's only subscription names
    the community id with type `NOTIF_COMMUNITY`, and
    `notification_subscribers` filters on both columns, but the assertion says
    so on stored state instead of by reasoning about the fixture.

    Only a name is asserted, so this test arranges nothing against the
    Community/Post primary-key collision `_seed_scenario` produces; that
    collision can only make an id-valued assertion vacuous, and there is none
    here.
    """
    community, post, author = _seed_scenario()
    assert community.ap_id is None
    assert community.name == 'microblogs'
    subscriber = make_user(_peer_instance(), 'subscriber', local=True)
    _subscribe(subscriber, community.id, NOTIF_COMMUNITY)
    db.session.commit()

    notify_about_post_task(post.id)

    notifications = _notifications_for(subscriber)
    assert len(notifications) == 1
    assert notifications[0].notif_type == NOTIF_COMMUNITY
    assert notifications[0].targets['community_name'] == 'microblogs'


def test_a_community_with_no_ap_id_is_named_by_its_name_in_the_topic_arm(app, db_session):
    """The else-arm of

        'community_name': community.ap_id if community.ap_id else community.name,

    (app/activitypub/util.py:2884), the NOTIF_TOPIC arm's copy -- again a
    separate expression on a separate line from the two above.

    `_seed_topic` puts the Topic at its reserved id 9 and attaches the
    community to it; the subscription names the TOPIC's id, so nothing here can
    be notified by the NOTIF_COMMUNITY arm instead, and `notif_type` is
    asserted at `NOTIF_TOPIC` to say that on stored state. `topic_name` is
    asserted alongside `community_name` because it is the entry that tells this
    arm's dict apart from the community arm's.

    The topic's id, 9, is distinct from the Community's and the Post's shared
    id of 1, but no id-valued entry of the dict is asserted here, so that
    distinctness is `_seed_topic`'s invariant rather than something this test
    depends on.
    """
    community, post, author = _seed_scenario()
    assert community.ap_id is None
    assert community.name == 'microblogs'
    topic = _seed_topic(community)
    subscriber = make_user(_peer_instance(), 'subscriber', local=True)
    _subscribe(subscriber, topic.id, NOTIF_TOPIC)
    db.session.commit()

    notify_about_post_task(post.id)

    notifications = _notifications_for(subscriber)
    assert len(notifications) == 1
    assert notifications[0].notif_type == NOTIF_TOPIC
    assert notifications[0].targets['community_name'] == 'microblogs'
    assert notifications[0].targets['topic_name'] == 'News'


def test_a_community_with_no_ap_id_is_named_by_its_name_in_the_feed_arm(app, db_session):
    """The else-arm of

        'community_name': community.ap_id if community.ap_id else community.name,

    (app/activitypub/util.py:2918), the NOTIF_FEED arm's copy and the last of
    the four.

    `_seed_feed` builds the Feed and the `FeedItem` that puts the community in
    it, which is what this arm's own lookup joins through:

        community_feeds = session.query(Feed).join(FeedItem, FeedItem.feed_id == Feed.id).filter(
            FeedItem.community_id == post.community_id).all()

    (app/activitypub/util.py:2901-2902 -- the call opens on :2901 and only the
    filter's argument and the `.all()` are on :2902; two lines quoted, two
    lines cited).

    Feed id 8 is one of the three ids the feed tests above use (7, 8 and 10);
    9 is `_seed_topic`'s reserved id and no feed takes it. Only one feed is seeded here, so there is no
    second feed id for a mutation to confuse this one with, and no id-valued
    entry is asserted in any case -- `feed_name` is, because it is this arm's
    discriminator from the other three, alongside `notif_type` at `NOTIF_FEED`.

    This test does not depend on where the arm's `notifications_sent_to.add`
    sits: it has a single recipient, a single feed and a single arm producing a
    row, so the dedup set is never consulted after the add.
    """
    community, post, author = _seed_scenario()
    assert community.ap_id is None
    assert community.name == 'microblogs'
    instance = _peer_instance()
    subscriber = make_user(instance, 'subscriber', local=True)
    feed = _seed_feed(community, instance, name='afeed', feed_id=8, title='A Feed')
    _subscribe(subscriber, feed.id, NOTIF_FEED)
    db.session.commit()

    notify_about_post_task(post.id)

    notifications = _notifications_for(subscriber)
    assert len(notifications) == 1
    assert notifications[0].notif_type == NOTIF_FEED
    assert notifications[0].targets['community_name'] == 'microblogs'
    assert notifications[0].targets['feed_name'] == 'A Feed'


# ---------------------------------------------------------------------------
# notify_about_post_task -- the tail except, and the partial fan-out
# ---------------------------------------------------------------------------

INT_MAX = 2147483647  # PostgreSQL's integer maximum


def test_a_failing_recipient_mid_fan_out_rolls_back_only_that_recipient(app, db_session):
    """The task's tail handler:

        except Exception:
            session.rollback()
            raise

    This used to be reached through a NULL `unread_notifications`, whose
    `+= 1` raised TypeError. D274, fixed: the column is now NOT NULL (see
    tests/test_user_not_null_columns.py), so that state is gone and the
    failure is produced instead by a counter at PostgreSQL's integer maximum,
    whose increment the COMMIT refuses with DataError -- after
    `session.add(new_notification)`, so the failing iteration really has work
    to roll back.

    THE PARTIAL FAN-OUT. Each arm commits per recipient inside its loop, so a
    recipient notified by an earlier arm is already durable when a later arm
    raises. `notified` is subscribed to the author (NOTIF_USER arm, first);
    `broken` to the community (NOTIF_COMMUNITY arm, next), where it dies. Arm
    order is what makes the split deterministic.

    `pytest.raises` makes the zero-rows assertion for `broken` non-vacuous: a
    mutant that skipped `broken` would leave the same zero rows but not raise.
    Replacing `session.rollback()` with `session.commit()` fails too, since
    the commit would hit the same refusal inside the handler; deleting the
    `raise` fails `pytest.raises`.
    """
    community, post, author = _seed_scenario()
    instance = _peer_instance()
    notified = make_user(instance, 'notified', local=True)
    notified.unread_notifications = 7
    broken = make_user(instance, 'broken', local=True)
    broken.unread_notifications = INT_MAX
    _subscribe(notified, author.id, NOTIF_USER)
    _subscribe(broken, community.id, NOTIF_COMMUNITY)
    db.session.commit()
    assert len({author.id, notified.id, broken.id}) == 3
    assert Notification.query.count() == 0

    with pytest.raises(DataError) as excinfo:
        notify_about_post_task(post.id)

    assert 'out of range' in str(excinfo.value)

    survivors = _notifications_for(notified)
    assert len(survivors) == 1
    assert survivors[0].notif_type == NOTIF_USER
    assert survivors[0].subtype == 'new_post_from_followed_user'
    assert survivors[0].author_id == author.id
    assert _notifications_for(broken) == []
    assert Notification.query.count() == 1
    db.session.refresh(notified)
    db.session.refresh(broken)
    assert notified.unread_notifications == 8
    assert broken.unread_notifications == INT_MAX


def test_a_retried_fan_out_skips_recipients_already_notified(app, db_session):
    """D278, fixed (owner ruling): the fan-out commits per recipient, so a
    failure part-way leaves earlier recipients notified, and Celery retries
    the task from the top. The retry used to notify them a second time; a
    recipient who already has a notification for this post's url is now
    skipped, so the retry reaches only the ones the failed attempt missed.
    """
    community, post, author = _seed_scenario()
    instance = _peer_instance()
    notified = make_user(instance, 'notified', local=True)
    notified.unread_notifications = 7
    broken = make_user(instance, 'broken', local=True)
    broken.unread_notifications = INT_MAX
    _subscribe(notified, author.id, NOTIF_USER)
    _subscribe(broken, community.id, NOTIF_COMMUNITY)
    db.session.commit()
    with pytest.raises(DataError):
        notify_about_post_task(post.id)
    db.session.rollback()
    broken.unread_notifications = 0
    db.session.commit()

    notify_about_post_task(post.id)

    assert len(_notifications_for(notified)) == 1
    assert len(_notifications_for(broken)) == 1
    db.session.refresh(notified)
    db.session.refresh(broken)
    assert notified.unread_notifications == 8
    assert broken.unread_notifications == 1


class _RecordingLockDouble:
    """An `app.redis_client` double whose `.lock(...)` records its key and
    returns a no-op context manager."""

    def __init__(self):
        self.keys = []

    def lock(self, key, *args, **kwargs):
        self.keys.append(key)
        return contextlib.nullcontext()


def test_every_arm_increments_the_unread_counter_under_the_users_lock(app, db_session, monkeypatch):
    """D279, fixed: the four arms did `user.unread_notifications += 1` with no
    lock, where notify_about_post_reply takes `lock:user:<id>` around the same
    read-modify-write, so two posts fanning out to one recipient could lose an
    increment. Each arm now takes that lock."""
    community, post, author = _seed_scenario()
    instance = _peer_instance()
    by_user = make_user(instance, 'user_subscriber', local=True)
    by_community = make_user(instance, 'community_subscriber', local=True)
    by_topic = make_user(instance, 'topic_subscriber', local=True)
    by_feed = make_user(instance, 'feed_subscriber', local=True)
    topic = _seed_topic(community)
    feed = _seed_feed(community, instance, name='afeed', feed_id=8, title='A Feed')
    _subscribe(by_user, author.id, NOTIF_USER)
    _subscribe(by_community, community.id, NOTIF_COMMUNITY)
    _subscribe(by_topic, topic.id, NOTIF_TOPIC)
    _subscribe(by_feed, feed.id, NOTIF_FEED)
    db.session.commit()
    double = _RecordingLockDouble()
    monkeypatch.setattr('app.redis_client', double)

    notify_about_post_task(post.id)

    assert double.keys == [f'lock:user:{by_user.id}', f'lock:user:{by_community.id}',
                           f'lock:user:{by_topic.id}', f'lock:user:{by_feed.id}']
    for subscriber in (by_user, by_community, by_topic, by_feed):
        db.session.refresh(subscriber)
        assert subscriber.unread_notifications == 1
