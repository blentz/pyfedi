"""send_post for a local microblogs post pushes the Note microblog_note builds (spec §4.2, §6.1, amended §10 A6)."""
import json
import socket
from datetime import datetime

import pytest

from app import db
from app.activitypub.util import post_to_page
from app.constants import POST_TYPE_ARTICLE, VISIBILITY_FOLLOWERS
from app.models import CommunityMember
from app.shared.tasks.pages import send_post
from tests.factories import (a_keypair, make_community, make_follow, make_instance, make_instance_block, make_post,
                             make_user)

_REAL_GETADDRINFO = socket.getaddrinfo


def _getaddrinfo_without_the_network(host, *args, **kwargs):
    """Answer for `.example` hosts in-process, as tests/test_shared_tasks_send_post.py does: signing a delivery
    resolves the inbox host (app/utils.py is_invalid_get_request_uri), and the suite must not depend on a resolver."""
    if isinstance(host, str) and (host == 'example' or host.endswith('.example')):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 0))]
    return _REAL_GETADDRINFO(host, *args, **kwargs)


@pytest.fixture(autouse=True)
def _example_hosts_resolve_without_a_resolver(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', _getaddrinfo_without_the_network)


@pytest.fixture
def deliveries(monkeypatch):
    """Every send_post_request call, recorded as well as performed, so a test that expects no delivery can say so
    even if a failed unmocked request were swallowed downstream."""
    import app.shared.tasks.pages as pages
    calls = []
    real = pages.send_post_request

    def recording(inbox, *args, **kwargs):
        calls.append(inbox)
        return real(inbox, *args, **kwargs)

    monkeypatch.setattr(pages, 'send_post_request', recording)
    return calls


def _seed():
    local = make_instance('test.piefed.local', software='piefed')
    alice = make_user(local, 'alice', local=True, with_keys=True)
    community = make_community('microblogs')
    community.private_key, community.public_key = a_keypair()
    post = make_post(community, alice, ap_id='https://test.piefed.local/post/1', title='Hello')
    post.type = POST_TYPE_ARTICLE
    post.body, post.body_html = 'world', '<p>world</p>'
    db.session.commit()
    return alice, community, post


def _remote(domain, software, name):
    instance = make_instance(domain, software=software)
    instance.inbox = f'https://{domain}/inbox'
    user = make_user(instance, name)
    user.ap_profile_id = user.ap_public_url = f'https://{domain}/users/{name}'
    db.session.commit()
    return instance, user


def _bodies(route):
    return [json.loads(call.request.content) for call in route.calls]


def test_follower_on_mastodon_gets_the_authors_create_note(db_session, http_mock):
    alice, community, post = _seed()
    instance, bob = _remote('mastodon.example', 'mastodon', 'bob')
    make_follow(alice, bob, is_inward=True)
    route = http_mock.post('https://mastodon.example/inbox').respond(202)
    send_post(post.id, session=db.session)
    (create,) = _bodies(route)
    assert create['type'] == 'Create' and create['actor'] == alice.public_url()
    assert create['object']['type'] == 'Note'
    assert 'audience' not in create


def test_pushed_note_equals_fetched_note(db_session, http_mock):
    alice, community, post = _seed()
    instance, bob = _remote('mastodon.example', 'mastodon', 'bob')
    make_follow(alice, bob, is_inward=True)
    route = http_mock.post('https://mastodon.example/inbox').respond(202)
    send_post(post.id, session=db.session)
    (create,) = _bodies(route)
    assert create['object'] == post_to_page(post)


def test_edit_sends_update_of_the_same_note(db_session, http_mock):
    alice, community, post = _seed()
    post.edited_at = datetime(2026, 10, 14, 12, 0)
    db.session.commit()
    instance, bob = _remote('mastodon.example', 'mastodon', 'bob')
    make_follow(alice, bob, is_inward=True)
    route = http_mock.post('https://mastodon.example/inbox').respond(202)
    send_post(post.id, edit=True, session=db.session)
    (update,) = _bodies(route)
    assert update['type'] == 'Update' and update['object'] == post_to_page(post)


def test_threadiverse_community_follower_gets_group_announce_and_follower_still_gets_create(db_session, http_mock):
    alice, community, post = _seed()
    lemmy, carol = _remote('lemmy.example', 'lemmy', 'carol')
    db.session.add(CommunityMember(user_id=carol.id, community_id=community.id))
    make_follow(alice, carol, is_inward=True)
    db.session.commit()
    route = http_mock.post('https://lemmy.example/inbox').respond(202)
    send_post(post.id, session=db.session)
    bodies = _bodies(route)
    kinds = sorted((b['type'], b['actor']) for b in bodies)
    assert kinds == sorted([('Announce', community.public_url()), ('Create', alice.public_url())])
    announce = next(b for b in bodies if b['type'] == 'Announce')
    assert announce['object']['type'] == 'Create' and '@context' not in announce['object']
    # Lemmy and mbin file a post by its audience: the copy sent to a threadiverse follower carries the community
    assert announce['object']['audience'] == community.public_url()
    assert announce['object']['object']['audience'] == community.public_url()
    # the author's own Create to the same instance does not (spec §10 A9), and still equals the fetched Note
    create = next(b for b in bodies if b['type'] == 'Create')
    assert 'audience' not in create and 'audience' not in create['object']
    assert create['object'] == post_to_page(post)


def test_microblog_app_community_follower_gets_announce_of_id_on_create(db_session, http_mock):
    alice, community, post = _seed()
    masto, dave = _remote('mastodon.example', 'mastodon', 'dave')
    db.session.add(CommunityMember(user_id=dave.id, community_id=community.id))
    db.session.commit()
    route = http_mock.post('https://mastodon.example/inbox').respond(202)
    send_post(post.id, session=db.session)
    (announce,) = _bodies(route)
    assert announce['type'] == 'Announce' and announce['object'] == post.ap_id


def test_microblog_app_community_follower_gets_authors_update_on_edit(db_session, http_mock):
    alice, community, post = _seed()
    masto, dave = _remote('mastodon.example', 'mastodon', 'dave')
    db.session.add(CommunityMember(user_id=dave.id, community_id=community.id))
    db.session.commit()
    route = http_mock.post('https://mastodon.example/inbox').respond(202)
    send_post(post.id, edit=True, session=db.session)
    (update,) = _bodies(route)
    assert update['type'] == 'Update' and update['actor'] == alice.public_url()


def test_edit_reaches_an_instance_following_community_and_author_once(db_session, http_mock):
    alice, community, post = _seed()
    post.edited_at = datetime(2026, 10, 14, 12, 0)
    masto, dave = _remote('mastodon.example', 'mastodon', 'dave')
    db.session.add(CommunityMember(user_id=dave.id, community_id=community.id))
    make_follow(alice, dave, is_inward=True)
    db.session.commit()
    route = http_mock.post('https://mastodon.example/inbox').respond(202)
    send_post(post.id, edit=True, session=db.session)
    (update,) = _bodies(route)
    assert update['type'] == 'Update'


def test_mentioned_remote_user_gets_the_create(db_session, http_mock, monkeypatch):
    alice, community, post = _seed()
    instance, bob = _remote('mastodon.example', 'mastodon', 'bob')
    monkeypatch.setattr('app.shared.tasks.pages.mentioned_users', lambda p, allow_fetch=True: [bob])
    route = http_mock.post('https://mastodon.example/inbox').respond(202)
    send_post(post.id, session=db.session)
    (create,) = _bodies(route)
    assert 'https://mastodon.example/users/bob' in create['cc']


def test_mentioned_local_user_is_not_delivered_to(db_session, http_mock, monkeypatch, deliveries):
    alice, community, post = _seed()
    local_bob = make_user(alice.instance, 'bob', local=True)
    monkeypatch.setattr('app.shared.tasks.pages.mentioned_users', lambda p, allow_fetch=True: [local_bob])
    send_post(post.id, session=db.session)
    assert deliveries == [] and len(http_mock.calls) == 0


def test_followers_only_goes_to_accepted_follower_instances_and_never_announces(db_session, http_mock, deliveries):
    alice, community, post = _seed()
    post.visibility = VISIBILITY_FOLLOWERS
    db.session.commit()
    instance, bob = _remote('mastodon.example', 'mastodon', 'bob')
    make_follow(alice, bob, is_inward=True)
    lemmy, carol = _remote('lemmy.example', 'lemmy', 'carol')
    db.session.add(CommunityMember(user_id=carol.id, community_id=community.id))
    db.session.commit()
    route = http_mock.post('https://mastodon.example/inbox').respond(202)
    send_post(post.id, session=db.session)
    (create,) = _bodies(route)
    assert create['object']['to'] == [alice.followers_url()]
    assert deliveries == ['https://mastodon.example/inbox']


def test_blocked_instance_gets_nothing(db_session, http_mock, deliveries):
    alice, community, post = _seed()
    instance, bob = _remote('mastodon.example', 'mastodon', 'bob')
    make_follow(alice, bob, is_inward=True)
    make_instance_block(alice, instance)
    send_post(post.id, session=db.session)
    assert deliveries == [] and len(http_mock.calls) == 0


def test_microblog_edit_with_no_audience_sends_nothing(db_session, http_mock, deliveries):
    alice, community, post = _seed()
    send_post(post.id, edit=True, session=db.session)
    assert deliveries == [] and len(http_mock.calls) == 0


def test_each_instance_gets_one_create_even_with_two_followers(db_session, http_mock):
    alice, community, post = _seed()
    instance, bob = _remote('mastodon.example', 'mastodon', 'bob')
    erin = make_user(instance, 'erin')
    make_follow(alice, bob, is_inward=True)
    make_follow(alice, erin, is_inward=True)
    route = http_mock.post('https://mastodon.example/inbox').respond(202)
    send_post(post.id, session=db.session)
    assert len(route.calls) == 1


def test_non_microblog_community_still_uses_page_path(db_session, http_mock):
    alice, community, post = _seed()
    community.name = 'c1'
    db.session.commit()
    instance, bob = _remote('mastodon.example', 'mastodon', 'bob')
    make_follow(alice, bob, is_inward=True)
    route = http_mock.post('https://mastodon.example/inbox').respond(202)
    send_post(post.id, session=db.session)
    assert any(b['object'].get('type') == 'Note' and b['object']['content'].startswith('<p>Hello</p>')
               for b in _bodies(route) if isinstance(b.get('object'), dict))


def test_community_announce_skips_only_the_instance_the_author_blocked(db_session, http_mock, deliveries):
    alice, community, post = _seed()
    blocked, carol = _remote('blocked.example', 'lemmy', 'carol')
    open_instance, dave = _remote('open.example', 'lemmy', 'dave')
    for member in (carol, dave):
        db.session.add(CommunityMember(user_id=member.id, community_id=community.id))
    db.session.commit()
    make_instance_block(alice, blocked)
    route = http_mock.post('https://open.example/inbox').respond(202)
    send_post(post.id, session=db.session)
    (announce,) = _bodies(route)
    assert announce['type'] == 'Announce' and announce['actor'] == community.public_url()
    assert deliveries == ['https://open.example/inbox']
