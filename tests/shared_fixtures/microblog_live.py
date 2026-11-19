"""Fixtures from tests/test_microblog_live.py that other test modules request (registered in tests/conftest.py)."""
from types import SimpleNamespace

import pytest

from app import db
from app.constants import COMMUNITY_KIND_MICROBLOGS
from app.models import Language, Site
from tests.factories import make_community, make_instance, make_post, make_user
from tests.test_microblog_live import local_instance


@pytest.fixture
def microblog_live_client(app):
    return app.test_client()


@pytest.fixture
def live(app, db_session):
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    local = local_instance()
    make_user(local, 'founder', local=True)
    viewer = make_user(local, 'viewer', local=True)
    remote = make_instance('mastodon.example')
    author = make_user(remote, 'tooter')
    microblogs = make_community('microblogs', kind=COMMUNITY_KIND_MICROBLOGS)
    microblogs.show_popular = False
    microblogs.show_all = False
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.commit()
    counter = iter(range(1, 10_000))

    def toot(**columns):
        post = make_post(microblogs, author, f'https://mastodon.example/statuses/{next(counter)}', microblog=True)
        for name, value in columns.items():
            setattr(post, name, value)
        db.session.commit()
        return post

    return SimpleNamespace(viewer=viewer, author=author, remote=remote, microblogs=microblogs, toot=toot)
