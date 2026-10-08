"""Per-server views of the microblogs community: /c/microblogs@<host>.

Spec: docs/superpowers/specs/2026-10-08-microblog-instance-views-design.md
"""
import re
from datetime import timedelta

import pytest
from flask import render_template_string

from app import db
from app.community.live import is_local_microblogs, microblog_server_view, server_view_actor
from app.models import BannedInstances, Tag
from app.utils import utcnow
from tests.discovery_fixtures import fresh_cache  # noqa: F401
from tests.factories import make_community, make_instance, make_post, make_user
from tests.test_microblog_live import client, live, login, teaser_ids  # noqa: F401

pytestmark = pytest.mark.usefixtures('site', 'fresh_cache')

VIEW = '/c/microblogs@mastodon.example'


def other_toot(live, n=1):
    """A microblog in the same community by an author on another server."""
    other = make_instance(f'other{n}.example')
    author = make_user(other, f'other{n}')
    return make_post(live.microblogs, author, f'https://other{n}.example/statuses/1', microblog=True)


def real_microblogs(live):
    """A real remote community named microblogs on the test server's host."""
    community = make_community('microblogs', host='mastodon.example')
    community.ap_id = 'microblogs@mastodon.example'
    community.instance_id = live.remote.id
    db.session.commit()
    return community


class TestResolution:

    def view(self, app, actor):
        with app.test_request_context():
            found = microblog_server_view(actor)
            return found.id if found is not None else None

    def test_a_known_server_resolves(self, app, live):
        assert self.view(app, 'microblogs@mastodon.example') == live.remote.id

    def test_case_is_ignored(self, app, live):
        assert self.view(app, ' Microblogs@Mastodon.Example ') == live.remote.id

    @pytest.mark.parametrize('actor', ['microblogs@nowhere.example', 'general@mastodon.example',
                                       'microblogs', 'microblogs@', '@mastodon.example'])
    def test_anything_else_does_not(self, app, live, actor):
        assert self.view(app, actor) is None

    def test_this_server_does_not(self, app, live):
        assert self.view(app, 'microblogs@test.piefed.local') is None

    def test_a_banned_server_does_not(self, app, live):
        db.session.add(BannedInstances(domain='mastodon.example'))
        db.session.commit()

        assert self.view(app, 'microblogs@mastodon.example') is None

    def test_a_real_community_wins(self, app, live):
        real_microblogs(live)

        assert self.view(app, 'microblogs@mastodon.example') is None

    def test_server_view_actor_names_the_view(self, app, live):
        with app.test_request_context():
            assert server_view_actor(live.remote.id) == 'microblogs@mastodon.example'

    def test_server_view_actor_is_none_where_there_is_no_view(self, app, live):
        real_microblogs(live)
        with app.test_request_context():
            assert server_view_actor(live.remote.id) is None
            assert server_view_actor(999_999) is None

    def test_only_the_local_microblogs_community_is_local_microblogs(self, app, live):
        with app.test_request_context():
            assert is_local_microblogs(live.microblogs)
            assert not is_local_microblogs(make_community('general'))
            assert not is_local_microblogs(real_microblogs(live))
