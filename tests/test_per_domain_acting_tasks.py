"""A Celery task acts as the domain that queued it (plan C3, ruling b: app.celery_acting).

A task runs in a worker, with no request: the domain it fetches as (its User-Agent, the instance actor that signs a
signed fetch) comes from its message headers, stamped when it was queued. Under the suite's eager Celery a task runs
inside its caller's request, which would hide whether the stamp is read at all, so `queued` keeps each task's
published call (ActingTask.apply_async's eager branch hands Task.apply the same headers a broker message carries)
and `run` runs it later with no request of any kind: what it names then came from the headers alone, through the
same task_prerun/task_postrun handlers a worker runs.
"""
import contextlib
import threading
from types import SimpleNamespace

import httpx
import pytest
import respx
from celery import Task
from flask_login import login_user

from app import db
from app.activitypub.util import refresh_user_profile, remote_object_to_json
from app.admin.routes import import_bans_task
from app.admin.util import move_community_images_to_here, process_topic_communities
from app.activitypub.signature import HttpSignature
from app.celery_acting import ACTING_HEADER, ActingTask, _queuing_domain_id, _runs, _stop_acting, queued_domain_id
from app.discovery.credits import fetch_feed
from app.discovery.sync import poll_synced_community, reconcile_sync_task
from app.models import DiscoverySync, Post, SiteDomain
from app.relays.expiry import expire_relayed_posts
from app.relays.inbound import process_relayed_announce, send_relay_follow_accept
from app.shared.tasks import task_selector
from app.shared.tasks.maintenance import add_remote_communities, check_instance_health, monitor_healthy_instances, \
    refresh_instance_chooser, sync_defederation_subscriptions
from app.shared.tasks.pages import make_post
from app.storefront import acting_domain, acting_for, clear_domain_cache, domain_for_host
from app.utils import download_defeds_worker
from tests.factories import inbox_activity, make_community, make_instance, make_user
from tests.test_per_domain_signing import ACTOR_URL, HELL_KEY_ID, REMOTE, SHOP, SHOP_KEY_ID, _key_id, \
    _signing_site, hell_ua, shop_ua
from tests.two_domains import HELL, request_on, two_domains  # noqa: F401 -- two_domains is a fixture


class Domains:
    def __init__(self, two):
        db.session.add(SiteDomain(domain=SHOP, scheme='https', federates=True))
        db.session.commit()
        clear_domain_cache()
        self.hell, self.onion = two.domains
        self.shop = domain_for_host(SHOP)
        self.hell_user = two.local_user('hellbob', self.hell)
        self.shop_user = two.local_user('shopbob', self.shop)
        self.remote = make_user(make_instance('remote.example'), 'carol')
        self.remote.ap_public_url = ACTOR_URL
        db.session.commit()


@pytest.fixture
def domains(app, two_domains):  # noqa: F811 -- two_domains is the imported fixture
    _signing_site()
    yield Domains(two_domains)
    clear_domain_cache()


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def queued(monkeypatch):
    """Each task published, as (task, args, kwargs, options), instead of being run."""
    published = []
    monkeypatch.setattr(ActingTask, 'apply', lambda self, args=None, kwargs=None, **options:
                        published.append((self, args, kwargs, options)))
    return published


def run(entry):
    """Run a published task as a worker would: no request, the message's headers."""
    task, args, kwargs, options = entry
    return Task.apply(task, args, kwargs, **options)


def _signed_in(app, user):
    context = app.test_request_context()
    context.push()
    if user is not None:
        login_user(user)
    return context


@pytest.mark.parametrize('who, ua', [('shop_user', shop_ua), ('hell_user', hell_ua), (None, hell_ua)])
def test_a_queued_profile_refresh_names_the_domain_that_queued_it(app, domains, router, queued, who, ua):
    """A shop account's page queues a refresh of a remote profile: the worker's fetch is the shop's."""
    fetched = router.get(ACTOR_URL).respond(404)
    context = _signed_in(app, getattr(domains, who) if who else None)
    try:
        refresh_user_profile(domains.remote.id)
    finally:
        context.pop()
    [entry] = queued
    assert entry[3]['headers'][ACTING_HEADER] == (domains.shop.id if who == 'shop_user' else None)
    assert acting_domain() == domains.hell   # nothing outside the task acts as the shop
    run(entry)
    assert fetched.calls.last.request.headers['User-Agent'] == ua(app)
    assert acting_domain() == domains.hell and not _runs


def test_two_tasks_in_a_row_do_not_carry_a_domain_over(app, domains, router, queued):
    fetched = router.get(ACTOR_URL).respond(404)
    context = _signed_in(app, domains.shop_user)
    try:
        refresh_user_profile(domains.remote.id)
    finally:
        context.pop()
    refresh_user_profile(domains.remote.id)   # queued by nobody: housekeeping
    shop_task, hell_task = queued
    run(shop_task)
    run(hell_task)
    assert [call.request.headers['User-Agent'] for call in fetched.calls] == [shop_ua(app), hell_ua(app)]


def test_a_failing_task_leaves_no_domain_behind(app, domains, queued):
    context = _signed_in(app, domains.shop_user)
    try:
        make_post.delay(send_async=True, post_id=987654)   # no such post: send_post raises
    finally:
        context.pop()
    with pytest.raises(AttributeError):
        run(queued[0])
    assert acting_domain() == domains.hell and not _runs


def _shop_post_mentioning_dave(domains):
    community = make_community('news', 'lemmy.example')
    community.local_only = True   # the post goes nowhere: only the mention lookup is made
    post = Post(user_id=domains.shop_user.id, community_id=community.id, title='hello', body='hi @dave@remote.example',
                instance_id=domains.shop.instance_id)
    db.session.add(post)
    db.session.commit()
    return post


def _mention_lookup_routes(router):
    webfinger = router.get(f'{REMOTE}/.well-known/webfinger').respond(200, json={
        'links': [{'rel': 'self', 'type': 'application/activity+json', 'href': f'{REMOTE}/u/dave'}]})
    actor = router.get(f'{REMOTE}/u/dave').mock(side_effect=lambda request: httpx.Response(
        404 if 'Signature' in request.headers else 401))
    return webfinger, actor


def _seen(webfinger, actor):
    unsigned, signed = (call.request for call in actor.calls)
    return (webfinger.calls.last.request.headers['User-Agent'], unsigned.headers['User-Agent'],
            signed.headers['User-Agent'], _key_id(signed))


def test_a_shop_posts_mention_lookup_is_the_shops_inline_and_queued_alike(app, domains, router, queued, monkeypatch):
    """A shop account's post: its mentions are looked up (WebFinger, then a signed actor fetch) as the shop, whether
    the post task runs in the request (send_async=False) or in a worker."""
    monkeypatch.setattr('app.user.utils.time.sleep', lambda seconds: None)
    post = _shop_post_mentioning_dave(domains)
    expected = (shop_ua(app), shop_ua(app), shop_ua(app), SHOP_KEY_ID)

    webfinger, actor = _mention_lookup_routes(router)
    context = _signed_in(app, domains.shop_user)
    try:
        task_selector('make_post', send_async=False, post_id=post.id)
    finally:
        context.pop()
    assert _seen(webfinger, actor) == expected

    router.reset()
    context = _signed_in(app, domains.shop_user)
    try:
        task_selector('make_post', send_async=True, post_id=post.id)
    finally:
        context.pop()
    run(queued[0])
    assert _seen(webfinger, actor) == expected


def test_a_hell_posts_mention_lookup_is_hells(app, domains, router, queued, monkeypatch):
    monkeypatch.setattr('app.user.utils.time.sleep', lambda seconds: None)
    post = _shop_post_mentioning_dave(domains)
    post.user_id = domains.hell_user.id
    post.instance_id = domains.hell.instance_id
    db.session.commit()
    webfinger, actor = _mention_lookup_routes(router)
    context = _signed_in(app, domains.hell_user)
    try:
        task_selector('make_post', send_async=True, post_id=post.id)
    finally:
        context.pop()
    run(queued[0])
    assert _seen(webfinger, actor) == (hell_ua(app), hell_ua(app), hell_ua(app), HELL_KEY_ID)


@pytest.mark.usefixtures('redis_double')
@pytest.mark.parametrize('host', [SHOP, HELL])
def test_an_activity_queued_from_a_sister_inbox_is_processed_as_that_domain(app, domains, signing_peer, queued,
                                                                           monkeypatch, host):
    """The inbox queues process_inbox_request; queued from shop.example's inbox, its fetches are the shop's."""
    activity = inbox_activity(signing_peer, activity_type='Follow', object_uri=domains.shop_user.public_url())
    monkeypatch.setitem(app.config, 'DEBUG', True)   # signing for a test host needs DEBUG; processing inline does not
    _uri, headers, body = HttpSignature.signed_request(f'https://{host}/inbox', activity, signing_peer.private_key,
                                                       f'{signing_peer.ap_profile_id}#main-key', send_via_async=True)
    monkeypatch.setitem(app.config, 'DEBUG', False)
    response = app.test_client().post('/inbox', data=body, headers=headers, content_type='application/activity+json')
    assert response.status_code == 200
    [entry] = [entry for entry in queued if entry[0].name.endswith('process_inbox_request')]
    assert entry[3]['headers'][ACTING_HEADER] == (domains.shop.id if host == SHOP else None)


def test_the_discovery_credits_stay_the_defaults_inside_a_shop_task(app, domains, router):
    feed = router.get('https://podcast.example/feed.xml').respond(404)
    with acting_for(domains.shop):
        assert fetch_feed('https://podcast.example/feed.xml') is None
    assert feed.calls.last.request.headers['User-Agent'] == hell_ua(app)


def test_a_task_queued_with_no_app_context_is_the_defaults(app, domains):
    """The stamp reads the database; with no app context (a fresh thread has none) there is none to read, and the
    task is the default domain's."""
    found = []
    thread = threading.Thread(target=lambda: found.append(_queuing_domain_id()))
    thread.start()
    thread.join()
    assert found == [None]


def test_a_workers_request_carries_the_domain_as_an_attribute():
    """A broker message's headers are attributes of the worker's request; an eager run's are in `headers`."""
    assert queued_domain_id(SimpleNamespace(**{ACTING_HEADER: 5})) == 5
    assert queued_domain_id(SimpleNamespace(headers={ACTING_HEADER: 6})) == 6
    assert queued_domain_id(SimpleNamespace(headers=None)) is None
    assert queued_domain_id(SimpleNamespace(**{ACTING_HEADER: 'shop.example'})) is None   # only an id names one


def test_a_postrun_for_a_task_that_never_started_acting_is_a_no_op():
    _stop_acting(task_id='never-ran')
    assert 'never-ran' not in _runs


# --------------------------------------------------------------------------
# Fix round 2: instance-level tasks are the default domain's, whoever queued them
# --------------------------------------------------------------------------

PROBE_URL = f'{REMOTE}/objects/probe'


class _Stop(Exception):
    """Ends a task once the probe has fetched: only what it fetched as is under test."""


def _probe(*_args, **_kwargs):
    """A fetch as the task's acting domain (unsigned, then signed on the 401), made where the task begins."""
    remote_object_to_json(PROBE_URL, retry_delay=0)
    raise _Stop()


def _poll_row(domains):
    community = make_community('synced', 'tube.example')
    db.session.add(DiscoverySync(community_id=community.id, follow_target=community.ap_profile_id))
    db.session.commit()
    return (community.id,)


INSTANCE_LEVEL = [
    # (task, the name its body calls first, which the probe stands in for, its arguments)
    (reconcile_sync_task, 'app.discovery.sync.reconcile_sync', lambda domains: ()),
    (poll_synced_community, 'app.discovery.sync.run_backfill', _poll_row),
    (import_bans_task, 'app.admin.routes.get_task_session', lambda domains: ('{}',)),
    (download_defeds_worker, 'app.utils.get_task_session', lambda domains: (1, 'lists.example')),
    (process_topic_communities, 'app.admin.util.get_task_session', lambda domains: ({}, 1)),
    (move_community_images_to_here, 'app.admin.util.get_task_session', lambda domains: (1,)),
    (send_relay_follow_accept, 'app.relays.inbound.get_task_session', lambda domains: (1, {})),
    (expire_relayed_posts, 'app.relays.expiry.relay_retention_days', lambda domains: ()),
    (sync_defederation_subscriptions, 'app.shared.tasks.maintenance.get_task_session', lambda domains: ()),
    (check_instance_health, 'app.shared.tasks.maintenance.get_task_session', lambda domains: ()),
    (monitor_healthy_instances, 'app.shared.tasks.maintenance.get_task_session', lambda domains: ()),
    (refresh_instance_chooser, 'app.shared.tasks.maintenance.get_task_session', lambda domains: ()),
    (add_remote_communities, 'app.shared.tasks.maintenance.get_request', lambda domains: ()),
]


@pytest.mark.parametrize('task, hook, arguments', INSTANCE_LEVEL, ids=[row[0].name.rsplit('.', 1)[-1]
                                                                        for row in INSTANCE_LEVEL])
def test_an_instance_level_task_a_shop_admin_queues_is_the_defaults(app, domains, router, queued, monkeypatch, task,
                                                                     hook, arguments):
    """Relays, the discovery sync, ban and defederation imports, topic imports and the maintenance probes are the
    instance's housekeeping: queued from a request a shop-home admin makes (stamped shop), each still fetches as
    The first domain, unsigned and signed."""
    route = router.get(PROBE_URL).mock(side_effect=lambda request: httpx.Response(
        404 if 'Signature' in request.headers else 401))
    monkeypatch.setattr(hook, _probe)
    args = arguments(domains)
    context = _signed_in(app, domains.shop_user)
    try:
        task.delay(*args)
    finally:
        context.pop()
    [entry] = queued
    assert entry[3]['headers'][ACTING_HEADER] == domains.shop.id   # the stamp is the shop's...
    with contextlib.suppress(_Stop):   # a task that records its failures (the sync poll) does not re-raise it
        run(entry)
    unsigned, signed = (call.request for call in route.calls)   # the probe ran: two fetches
    assert unsigned.headers['User-Agent'] == signed.headers['User-Agent'] == hell_ua(app)   # ...the work hell's
    assert _key_id(signed) == HELL_KEY_ID
    assert acting_domain() == domains.hell and not _runs


def test_a_relayed_announce_queued_from_a_shop_inbox_is_fetched_as_the_default(app, domains, router, queued):
    """A relay is the default domain's: an Announce it delivers to a sister's inbox (an anonymous request there acts
    as the sister, and the task is stamped so) is still fetched as the first domain. The task's own flow, unpatched."""
    route = router.get(PROBE_URL).mock(side_effect=lambda request: httpx.Response(
        404 if 'Signature' in request.headers else 401))
    with request_on(app, domains.shop):
        assert acting_domain() == domains.shop
        process_relayed_announce.delay(1, PROBE_URL)
    [entry] = queued
    assert entry[3]['headers'][ACTING_HEADER] == domains.shop.id
    run(entry)
    unsigned, signed = (call.request for call in route.calls)
    assert unsigned.headers['User-Agent'] == signed.headers['User-Agent'] == hell_ua(app)
    assert _key_id(signed) == HELL_KEY_ID
