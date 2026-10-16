"""Fixtures for the tests of what a local video channel sends PeerTube: a ready video, its followers on a PeerTube,
a Mastodon and a Lemmy instance, and a `sent` recorder for the signed requests."""
import copy
import socket
import uuid

import pytest
from flask import current_app

from app import db
from app.constants import COMMUNITY_KIND_VIDEO_CHANNEL, POST_TYPE_VIDEO
from app.models import CommunityMember, File
from app.shared.media import with_media
import app.shared.tasks.deletes as deletes
import app.shared.tasks.pages as pages
from tests.factories import a_keypair, make_community, make_follow, make_instance, make_post, make_user

_REAL_GETADDRINFO = socket.getaddrinfo


def _getaddrinfo_without_the_network(host, *args, **kwargs):
    if isinstance(host, str) and (host == 'example' or host.endswith('.example')):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 0))]
    return _REAL_GETADDRINFO(host, *args, **kwargs)


@pytest.fixture(autouse=True)
def _example_hosts_resolve_without_a_resolver(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', _getaddrinfo_without_the_network)


class Sent(dict):
    """The last (body, key_id) per inbox, plus every call in order, so a duplicate send to one inbox shows."""
    def __init__(self):
        super().__init__()
        self.calls = []


@pytest.fixture
def sent(monkeypatch):
    record = Sent()

    def fake(inbox, message, private_key, key_id, *args, **kwargs):
        message = copy.deepcopy(message)    # the Page path edits its Create after sending it
        record.calls.append((inbox, message, key_id))
        record[inbox] = (message, key_id)

    monkeypatch.setattr(pages, 'send_post_request', fake)
    monkeypatch.setattr(deletes, 'send_post_request', fake)
    return record


def _remote(domain, software, name):
    instance = make_instance(domain, software=software)
    instance.inbox = f'https://{domain}/inbox'
    user = make_user(instance, name)
    user.ap_profile_id = user.ap_public_url = f'https://{domain}/users/{name}'
    db.session.commit()
    return instance, user


@pytest.fixture
def video_post(db_session, monkeypatch):
    local = make_instance('test.piefed.local', software='piefed')
    alice = make_user(local, 'alice', local=True, with_keys=True)
    community = make_community('chan', kind=COMMUNITY_KIND_VIDEO_CHANNEL)
    community.private_key, community.public_key = a_keypair()
    post = make_post(community, alice, ap_id='https://test.piefed.local/post/1', title='A video')
    server = current_app.config['SERVER_URL']
    post.type = POST_TYPE_VIDEO
    post.body = 'About the video'
    post.url = f'{server}/static/media/posts/ab/cd/abcdefghijklmno.mp4'
    with_media(post, status='ready', uuid=str(uuid.uuid4()), mime='video/mp4', bytes=12741, duration_s=10,
               width=640, height=360, fps=30, thumbnail_width=640, thumbnail_height=360)
    post.image = File(source_url=f'{server}/static/media/posts/ef/gh/thumbthumbthumb.jpg', width=640, height=360)
    for domain, software, name in (('peertube.example', 'peertube', 'pt'), ('mastodon.example', 'mastodon', 'ma'),
                                   ('lemmy.example', 'lemmy', 'le')):
        _, follower = _remote(domain, software, name)
        db.session.add(CommunityMember(user_id=follower.id, community_id=community.id))
    _, fan = _remote('follower.example', 'piefed', 'fan')
    make_follow(alice, fan, is_inward=True)
    _, mentioned = _remote('mention.example', 'piefed', 'men')
    db.session.commit()
    monkeypatch.setattr(pages, 'mentioned_users', lambda post: [mentioned])
    return post
