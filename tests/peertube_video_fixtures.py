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


def _remote(domain, software, name):
    instance = make_instance(domain, software=software)
    instance.inbox = f'https://{domain}/inbox'
    user = make_user(instance, name)
    user.ap_profile_id = user.ap_public_url = f'https://{domain}/users/{name}'
    db.session.commit()
    return instance, user


