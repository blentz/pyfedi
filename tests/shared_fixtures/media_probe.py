"""Fixtures from tests/test_media_probe.py that other test modules request (registered in tests/conftest.py)."""
import os
from types import SimpleNamespace

import boto3
import pytest

from app import db
from app.constants import COMMUNITY_KIND_PODCAST, COMMUNITY_KIND_VIDEO_CHANNEL
from app.shared.tasks import media as media_tasks
from app.utils import set_setting
from tests.factories import (
    make_community,
    make_community_member,
    make_instance,
    make_user,
)


@pytest.fixture
def media_probe_env(app, db_session, site, private_static_tree, monkeypatch):
    local = make_instance('test.piefed.local', software='piefed')
    make_user(local, 'founder', local=True)   # user 1 is the admin
    mod = make_user(local, 'mod', local=True)
    mod.verified = True
    mod.private_key = 'a private key'   # can_create_post wants both of a local user
    chan, cast = make_community('chan'), make_community('cast')
    chan.kind, cast.kind = COMMUNITY_KIND_VIDEO_CHANNEL, COMMUNITY_KIND_PODCAST
    db.session.commit()
    for community in (chan, cast):
        make_community_member(mod, community, is_moderator=True)
    set_setting('allow_video_file_uploads', 'users')   # the author may upload to both, until a test revokes it
    set_setting('allow_audio_file_uploads', 'users')

    tasks, notified, sized = [], [], []
    monkeypatch.setattr(media_tasks, 'task_selector', lambda key, **kw: tasks.append((key, kw)))
    monkeypatch.setattr(media_tasks, 'notify_about_post', lambda post: notified.append(post.id))
    monkeypatch.setattr(media_tasks, 'make_image_sizes', lambda *args: sized.append(args))
    yield SimpleNamespace(app=app, mod=mod, chan=chan, cast=cast, tasks=tasks, notified=notified, sized=sized)
    db.session.rollback()
    for directory, _, files in os.walk('app/static'):
        for name in files:
            os.unlink(os.path.join(directory, name))


@pytest.fixture
def media_probe_s3(media_probe_env, s3_bucket, monkeypatch):
    for key, value in (('S3_ACCESS_KEY', 'key'), ('S3_ACCESS_SECRET', 'secret'),
                       ('S3_ENDPOINT', 'https://s3.us-east-1.amazonaws.com'), ('S3_REGION', 'us-east-1'),
                       ('S3_BUCKET', s3_bucket), ('S3_PUBLIC_URL', 'cdn.probeland.test'),
                       ('S3_STORAGE_CLASS', ''), ('S3_PUBLIC_ACL', '')):
        monkeypatch.setitem(media_probe_env.app.config, key, value)
    return boto3.client('s3', region_name='us-east-1')
