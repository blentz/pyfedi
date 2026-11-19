"""Fixtures from tests/test_media_upload.py that other test modules request (registered in tests/conftest.py)."""
from types import SimpleNamespace

import pytest
from flask import g

from app import db
from app.constants import (
    COMMUNITY_KIND_DISCUSSION,
    COMMUNITY_KIND_PODCAST,
    COMMUNITY_KIND_VIDEO_CHANNEL,
)
from app.models import Language, Site
from app.utils import set_setting
from tests.factories import (
    make_community,
    make_community_member,
    make_instance,
    make_user,
)
from tests.test_media_upload import csrf, login, remove_stored_media


@pytest.fixture
def media_upload_env(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # the templates render form.csrf_token()
    site = db.session.get(Site, 1)
    site.private_instance = False
    local = make_instance('test.piefed.local', software='piefed')
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1   # user 1 is the admin
    mod = make_user(local, 'mod', local=True)
    stranger = make_user(local, 'stranger', local=True)
    for user in (mod, stranger):
        user.verified = True
        user.private_key = 'a private key'   # validation_required / approval_required
    english = Language(code='en', name='English')
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.add(english)
    chan, cast, talk = make_community('chan'), make_community('cast'), make_community('talk')
    chan.kind, cast.kind, talk.kind = COMMUNITY_KIND_VIDEO_CHANNEL, COMMUNITY_KIND_PODCAST, COMMUNITY_KIND_DISCUSSION
    db.session.commit()
    for community in (chan, cast, talk):
        make_community_member(mod, community, is_moderator=True)
        make_community_member(stranger, community)
    set_setting('allow_video_file_uploads', 'users')
    set_setting('allow_audio_file_uploads', 'users')
    db.session.commit()

    probes = []
    monkeypatch.setattr('app.shared.media.queue_probe', probes.append)
    monkeypatch.setattr('app.shared.post.queue_probe', probes.append)

    def client_for(user):
        # The test's app context outlives a request, and with it flask_login's cached `g._login_user`: drop it, or
        # every request is answered for whoever asked first
        g.pop('_login_user', None)
        client = app.test_client()
        login(client, user)
        return client, csrf(app, client)

    yield SimpleNamespace(app=app, local=local, founder=founder, mod=mod, stranger=stranger, english=english,
                          chan=chan, cast=cast, talk=talk, probes=probes, client_for=client_for)
    db.session.rollback()
    remove_stored_media()
