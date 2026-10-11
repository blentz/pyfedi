"""is_local() compares a whole authority: SERVER_URL followed by '/', or nothing. A lookalike host that merely starts
with this instance's name (https://test.piefed.local.evil.com) is remote."""
import pytest
from flask import current_app

from app import db
from app.models import is_local_url
from tests.factories import make_community, make_feed, make_instance, make_post, make_post_reply, make_user


def _lookalike():
    return current_app.config['SERVER_URL'] + '.evil.com'


@pytest.fixture
def evil(db_session):
    instance = make_instance('test.piefed.local.evil.com')
    make_user(instance, 'owner')   # user 1: make_community hardcodes it as the owner
    return instance


def test_a_user_on_a_lookalike_host_is_remote(app, evil):
    user = make_user(evil, 'mallory')
    user.ap_profile_id = f'{_lookalike()}/u/mallory'
    db.session.commit()
    assert user.is_local() is False


def test_a_user_with_this_instances_url_is_local(app, evil):
    user = make_user(evil, 'alice')
    user.ap_profile_id = f"{current_app.config['SERVER_URL']}/u/alice"
    db.session.commit()
    assert user.is_local() is True


def test_a_community_on_a_lookalike_host_is_remote(app, evil):
    community = make_community('books', host='test.piefed.local.evil.com')
    community.ap_id = 'books@test.piefed.local.evil.com'
    db.session.commit()
    assert community.is_local() is False


def test_a_feed_on_a_lookalike_host_is_remote(app, evil):
    feed = make_feed(evil, 'news')
    assert feed.is_local() is False


def test_a_local_feed_with_an_ap_id_is_local(app, evil):
    feed = make_feed(evil, 'mine', local=True)
    assert feed.is_local() is True


def test_a_post_and_reply_on_a_lookalike_host_are_remote(app, evil):
    community = make_community()
    author = make_user(evil, 'mallory')
    post = make_post(community, author, f'{_lookalike()}/post/1')
    reply = make_post_reply(post, author, 'hi')
    reply.ap_id = f'{_lookalike()}/comment/1'
    db.session.commit()
    assert post.is_local() is False
    assert reply.is_local() is False


def test_a_post_with_this_instances_url_is_local(app, evil):
    community = make_community()
    post = make_post(community, make_user(evil, 'x'), f"{current_app.config['SERVER_URL']}/post/9")
    assert post.is_local() is True


def test_exactly_the_server_url_is_local(app):
    assert is_local_url(current_app.config['SERVER_URL'])
    assert not is_local_url(_lookalike())
    assert not is_local_url(None)
