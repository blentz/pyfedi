"""`create_post`, `notify_about_post` and `notify_about_post_task` -- the
post-side create and notify path, and the mirror of the reply notification
fan-out sub-project 15 covered to zero.

Entry is a direct call. `notify_about_post_task` takes a post id; the `app`
fixture puts celery in eager mode, so calling the undecorated function is the
path a worker runs.

`notify_about_post_task` runs on `get_task_session()`, whose autoflush is at
SQLAlchemy's default True, unlike `db.session`, which the app factory
configures `autoflush=False`. The task commits on its own session, so a test
holding a row from `db.session` needs `db.session.refresh()` to see what the
task wrote. That is the opposite of what sub-projects 14 and 15 needed.

`log_incoming_ap` writes an `ActivityPubLog` row only when
`LOG_ACTIVITYPUB_TO_DB` is true, and config.py defaults it False. Every
`create_post` guard logs and returns None, so the tests that pin them turn it
on and assert the message EXACTLY -- a substring can match a different guard's
row.
"""
import pytest

from app import db
from app.activitypub.util import (create_post, notify_about_post,
                                  notify_about_post_task)
from app.constants import (NOTIF_COMMUNITY, NOTIF_FEED, NOTIF_TOPIC,
                           NOTIF_USER)
from app.models import ActivityPubLog, Notification, Post, Topic, User
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
    passing for the wrong reason, and both matching what the sibling
    `_reply_doc` in tests/test_ap_create_reply.py already does:

    `to` defaults to Public. `create_post`'s visibility guard runs before
    `Post.new` is reached, and `activitypub_visibility` classifies an object
    with no addressing at all as 'direct' -- its last line is a bare
    `return 'direct'`. The brief's draft set no addressing, so every test
    aimed at something PAST the visibility guard would have been refused by
    that guard instead. A test wanting the guard itself overrides `to`/`cc`
    through `**fields`.

    The object's `id` is `/post/2`, not the `/post/1` `_seed_scenario` gives
    the row it seeds. `Post.new` writes `ap_id=request_json['object']['id']`,
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
    exception -- measured, as the first run of this test did exactly that. A
    non-image Content-Type is served so the url is classified
    `POST_TYPE_LINK`, which is the branch that goes on to resolve the domain.
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
