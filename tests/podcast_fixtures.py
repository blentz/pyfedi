"""Fixtures for the tests of a local podcast community's episodes: a ready MP3 upload with no cover art, followed
from a Mastodon and a Lemmy instance."""
import uuid
from types import SimpleNamespace

import pytest
from flask import current_app, g

from app import db
from app.constants import COMMUNITY_KIND_PODCAST, POST_TYPE_LINK
from app.models import CommunityMember, Site
from app.shared.media import with_media
from tests.factories import a_keypair, make_community, make_instance, make_post, make_user
from tests.peertube_video_fixtures import _remote, sent  # noqa: F401  (sent is a fixture the tests use)


@pytest.fixture
def episode_post(db_session):
    local = make_instance('test.piefed.local', software='piefed')
    alice = make_user(local, 'alice', local=True, with_keys=True)
    community = make_community('pod', kind=COMMUNITY_KIND_PODCAST)
    community.private_key, community.public_key = a_keypair()
    post = make_post(community, alice, ap_id='https://test.piefed.local/post/1', title='Episode one')
    post.type = POST_TYPE_LINK
    post.body = 'Show notes'
    post.url = f"{current_app.config['SERVER_URL']}/static/media/posts/ab/cd/abcdefghijklmno.mp3"
    with_media(post, status='ready', uuid=str(uuid.uuid4()), mime='audio/mpeg', bytes=4406, duration_s=2,
               codec='mp3')
    post.extensions = {**(post.extensions or {}), 'media_type': 'audio'}
    for domain, software, name in (('mastodon.example', 'mastodon', 'ma'), ('lemmy.example', 'lemmy', 'le')):
        _, follower = _remote(domain, software, name)
        db.session.add(CommunityMember(user_id=follower.id, community_id=community.id))
    db.session.commit()
    return post


@pytest.fixture
def env(request, app, db_session, monkeypatch):
    """A local instance with a founder, a community 'general' and a remote author on the test module's PEER host,
    for tests that feed a peer's activity to `Post.new`. No network: the HEAD request is answered with no type."""
    monkeypatch.setattr('app.utils.mime_type_using_head', lambda url: '')
    site = db.session.get(Site, 1) or Site(name='Test Site', blocked_phrases='')
    db.session.add(site)
    db.session.commit()
    site.private_instance = False
    g.site = site
    g.admin_ids = []
    peer = request.module.PEER
    make_user(make_instance('test.piefed.local', software='piefed'), 'founder', local=True)
    author = make_user(make_instance(peer), 'author')
    author.ap_profile_id = f'https://{peer}/u/author'
    community = make_community('general')
    db.session.commit()
    return SimpleNamespace(author=author, community=community)
