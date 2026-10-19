"""Hosted-blog platforms keep their users' inboxes on the platform's own host: a WordPress.com blog at
blog.example names https://public-api.wordpress.com/... and a micro.blog custom domain names https://micro.blog/....
The 2026-10-11 ruling trusts an inbox only on the actor's own host, so every such actor was refused (64 WordPress.com
and 4 micro.blog refusals on hell.cloud in the week to 2026-10-19), each one emailed as an ERROR.

Owner ruling 2026-10-19: an inbox on a host in the admin's hosted-inbox list (Federation settings, default
public-api.wordpress.com and micro.blog) is trusted as if it were on the actor's own host. Every other off-host
inbox is still refused, now logged as a WARNING.
"""
import logging

import pytest

from app import cache, db
from app.activitypub.util import actor_json_to_model, delivery_inbox_of, hosted_inbox_hosts, off_host_inbox, \
    shared_inbox_of
from app.models import User
from app.utils import set_setting
from tests.factories import peer_actor_json, peer_instance

BLOG = 'blog.altstore.io'
ACTOR_ID = f'https://{BLOG}/activitypub/adam'
MICRO_INBOX = 'https://micro.blog/activitypub/groosh/inbox'
MICRO_SHARED = 'https://micro.blog/activitypub/shared/inbox'
ELSEWHERE_INBOX = 'https://elsewhere.example/inbox'


@pytest.fixture(autouse=True)
def no_memoized_settings(app):
    cache.clear()
    yield
    cache.clear()


def micro_blog_actor(inbox=MICRO_INBOX, shared=MICRO_SHARED):
    """The actor document blog.altstore.io served on 2026-10-19, cut to the keys under test."""
    return {'id': ACTOR_ID, 'inbox': inbox, 'endpoints': {'sharedInbox': shared}}


def test_the_default_list_names_wordpress_com_and_micro_blog(app, db_session):
    assert hosted_inbox_hosts() == frozenset({'public-api.wordpress.com', 'micro.blog'})


def test_the_admin_list_is_normalised_one_host_per_line(app, db_session):
    set_setting('hosted_inbox_hosts', ' Micro.Blog \n\nhttps://Example.Host/inbox\n')
    assert hosted_inbox_hosts() == frozenset({'micro.blog', 'example.host'})


def test_an_inbox_on_a_listed_platform_is_not_off_host(app, db_session):
    assert off_host_inbox(micro_blog_actor()) is None


def test_an_inbox_on_an_unlisted_host_is_still_off_host(app, db_session):
    assert off_host_inbox(micro_blog_actor(inbox=ELSEWHERE_INBOX)) == ELSEWHERE_INBOX


def test_an_empty_list_trusts_no_platform(app, db_session):
    set_setting('hosted_inbox_hosts', '')
    assert off_host_inbox(micro_blog_actor()) == MICRO_INBOX


def test_a_listed_platform_does_not_vouch_for_an_actor_with_no_host(app, db_session):
    assert off_host_inbox({'id': 'not a url', 'inbox': MICRO_INBOX}) == MICRO_INBOX


def test_a_shared_inbox_on_a_listed_platform_is_used(app, db_session):
    assert shared_inbox_of(micro_blog_actor()) == MICRO_SHARED


def test_a_refresh_stores_a_shared_inbox_on_a_listed_platform(app, db_session):
    assert delivery_inbox_of(micro_blog_actor(), ACTOR_ID) == MICRO_SHARED


def test_a_refresh_still_keeps_the_stored_inbox_for_an_unlisted_host(app, db_session):
    assert delivery_inbox_of(micro_blog_actor(shared=ELSEWHERE_INBOX), ACTOR_ID) is None


def test_a_micro_blog_person_is_created_with_the_platform_shared_inbox(app, db_session):
    peer_instance(BLOG)
    document = peer_actor_json(name='adam', server=BLOG,
                               fields={'id': ACTOR_ID, 'inbox': MICRO_INBOX,
                                       'endpoints': {'sharedInbox': MICRO_SHARED}})

    user = actor_json_to_model(document, 'adam', BLOG)

    assert user is not None
    assert user.ap_inbox_url == MICRO_SHARED
    assert db.session.query(User).filter_by(ap_profile_id=ACTOR_ID).count() == 1


def test_a_refused_off_host_person_is_a_warning_not_an_error(app, db_session, caplog):
    """An ERROR is emailed to the admin; a refusal the instance makes by design is not an incident."""
    peer_instance(BLOG)
    document = peer_actor_json(name='adam', server=BLOG, fields={'id': ACTOR_ID, 'inbox': ELSEWHERE_INBOX})

    with caplog.at_level(logging.WARNING):
        assert actor_json_to_model(document, 'adam', BLOG) is None

    refusals = [r for r in caplog.records if 'An inbox on another host' in r.getMessage()]
    assert [r.levelno for r in refusals] == [logging.WARNING]
