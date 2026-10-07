"""The Live view of /c/microblogs: new posts arrive without a reload.

Spec: docs/superpowers/specs/2026-10-07-microblog-live-feed-design.md

Fixture notes, from tests/test_community_show.py: `Site.private_instance`
defaults to True, so every test here makes the instance public; and a local
community is reached at /c/<name>.
"""
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app import db
from app.constants import POST_STATUS_REVIEWING
from app.models import Instance, Language, Post, Site
from app.utils import utcnow
from tests.factories import (make_community, make_instance, make_instance_block,
                             make_post, make_user, make_user_block)

pytestmark = pytest.mark.usefixtures('site')


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def local_instance():
    existing = Instance.query.filter_by(domain='test.piefed.local').first()
    return existing if existing is not None else make_instance('test.piefed.local', software='piefed')


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
    microblogs = make_community('microblogs')
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


def teaser_ids(html):
    return [int(found) for found in re.findall(r'id="post_(\d+)"', html)]


class TestCommunityPostQuery:
    """The filter chain show_community applied inline, now shared with the Live fragment."""

    def test_a_logged_in_viewer_does_not_see_a_blocked_authors_post(self, app, live):
        from flask_login import login_user

        from app.community.routes import community_post_query

        kept = live.toot()
        other = make_user(live.remote, 'pest')
        blocked = make_post(live.microblogs, other, 'https://mastodon.example/statuses/pest', microblog=True)
        make_user_block(live.viewer, other)
        with app.test_request_context():
            login_user(live.viewer)
            posts, content_filters = community_post_query(live.microblogs, 'posts')
            ids = {post.id for post in posts}

        assert kept.id in ids and blocked.id not in ids
        assert isinstance(content_filters, dict)

    def test_an_anonymous_viewer_gets_no_content_filters(self, app, live):
        from app.community.routes import community_post_query

        live.toot()
        with app.test_request_context():
            posts, content_filters = community_post_query(live.microblogs, 'posts')
            assert posts.count() == 1

        assert content_filters == {}
