"""Fixtures for the tests of a local podcast community's episodes: a ready MP3 upload with no cover art, followed
from a Mastodon and a Lemmy instance."""
import uuid

import pytest
from flask import current_app

from app import db
from app.constants import COMMUNITY_KIND_PODCAST, POST_TYPE_LINK
from app.models import CommunityMember
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
