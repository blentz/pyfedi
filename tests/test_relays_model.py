"""Relay data (spec: Data) and the relay context Post.new reads."""
import pytest

from app import db
from app.models import Post, Relay, Site
from app.relays import PUBLIC, RELAY_PENDING, STYLE_MASTODON, current_relay_id, relay_context
from app.utils import utcnow
from tests.factories import make_community, make_community_member, make_post, make_user

pytestmark = pytest.mark.usefixtures('site')

AUTHOR = 'https://remote.test/u/tooter'


def make_relay(url='https://relay.example/inbox', **columns):
    relay = Relay(url=url, style=STYLE_MASTODON, inbox_url=url, state=RELAY_PENDING,
                  follow_activity_id='https://test.piefed.local/activities/relay-follow/x', created_at=utcnow())
    for name, value in columns.items():
        setattr(relay, name, value)
    db.session.add(relay)
    db.session.commit()
    return relay


def test_the_relay_context_is_scoped(app):
    assert current_relay_id.get() is None
    with relay_context(7):
        assert current_relay_id.get() == 7
    assert current_relay_id.get() is None


def test_deleting_a_relay_keeps_its_posts_and_clears_the_link(app, db_session, api_baseline):
    relay = make_relay()
    author = make_user(api_baseline.instance_remote, 'someone')
    community = make_community('microblogs')
    post = make_post(community, author, 'https://remote.example/p/1')
    post.relay_id = relay.id
    db.session.commit()

    db.session.delete(relay)
    db.session.commit()
    db.session.expire_all()

    assert db.session.get(Post, post.id).relay_id is None


def test_post_new_records_the_relay_it_came_through(app, api_baseline, monkeypatch):
    from flask import g

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('relayed')
    author = make_user(api_baseline.instance_remote, 'tooter')
    author.ap_id = 'tooter@remote.test'
    author.ap_profile_id = AUTHOR
    author.ap_public_url = AUTHOR
    db.session.commit()
    make_community_member(author, community)
    monkeypatch.setattr('app.models.post_stored_hooks', [])
    relay = make_relay()

    def create(number):
        document = {'id': f'https://remote.test/p/{number}', 'type': 'Page', 'name': 'a post',
                    'attributedTo': AUTHOR, 'to': [PUBLIC], 'published': '2026-01-01T00:00:00Z',
                    'content': '<p>body</p>'}
        return Post.new(author, community, {'id': f'https://remote.test/c/{number}', 'type': 'Create',
                                            'to': [PUBLIC], 'object': document})

    with relay_context(relay.id):
        relayed = create(1)
    direct = create(2)

    assert relayed.relay_id == relay.id
    assert direct.relay_id is None
