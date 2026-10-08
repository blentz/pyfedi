"""Home Live (spec Amendment A): the home feed source, the newer-than cursor, the fragment and the page."""
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest
from flask import g

from app import db
from app.models import Site
from app.utils import utcnow
from tests.factories import (make_community, make_community_member, make_post,
                             make_user_block)


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def teaser_ids(html):
    return [int(found) for found in re.findall(r'id="post_(\d+)"', html)]


@pytest.fixture
def home(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    g.site.private_instance = False
    community = make_community('homeland')
    community.show_all = True
    community.show_popular = True
    db.session.commit()
    reader = api_baseline.user3
    author = api_baseline.user2
    make_community_member(reader, community)
    counter = iter(range(1, 10_000))

    def post(**columns):
        created = make_post(community, author, f'https://test.piefed.local/h/{next(counter)}')
        for name, value in columns.items():
            setattr(created, name, value)
        db.session.commit()
        return created

    return SimpleNamespace(client=app.test_client(), community=community, reader=reader, author=author,
                           post=post, baseline=api_baseline)


class TestNewerThan:

    def ids(self, app, home, after, view_filter='all'):
        from flask_login import login_user

        from app.main.routes import home_feed_source
        from app.utils import get_deduped_post_ids

        with app.test_request_context():
            login_user(home.reader)
            community_ids, community_sql = home_feed_source(view_filter)
            return get_deduped_post_ids('', community_ids, 'new', include_following=view_filter == 'subscribed',
                                        community_sql=community_sql,
                                        newer_than=(after, utcnow() - timedelta(hours=1)))

    def test_only_posts_after_the_cursor_and_inside_the_window(self, app, home):
        seen = home.post()
        newer = home.post()
        home.post(posted_at=utcnow() - timedelta(hours=2))

        assert self.ids(app, home, seen.id) == [newer.id]

    def test_the_viewers_blocks_still_apply(self, app, home):
        kept = home.post()
        make_user_block(home.reader, home.author)

        assert kept.id not in self.ids(app, home, 0)

    @pytest.mark.parametrize('view_filter', ['subscribed', 'local', 'popular', 'all'])
    def test_each_tab_sees_its_own_post(self, app, home, view_filter):
        post = home.post()

        assert post.id in self.ids(app, home, 0, view_filter)

    def test_media_does_not_see_a_local_discussion_post(self, app, home):
        post = home.post()

        assert post.id not in self.ids(app, home, 0, 'media')

