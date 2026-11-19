"""Fixtures from tests/test_onion_hidden_on_clearnet.py that other test modules request (registered in tests/conftest.py)."""
import pytest

from app import db
from app.models import CommunityMember, Post, Role, Topic, utcnow
from tests.factories import grant_permission, make_post, make_post_reply
from tests.test_onion_hidden_on_clearnet import TOKEN, stamped
from tests.two_domains import FIRST, ONION


@pytest.fixture
def onion_hidden_on_clearnet_alpha(app, onion_hidden_on_clearnet_onion, monkeypatch):
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    return onion_hidden_on_clearnet_onion


@pytest.fixture
def onion_hidden_on_clearnet_onion(app, local_only_actors_hidden_world, instance_visible_here_scene, two_domains):
    """The onion's bob and art, his post ONIONPOST in art (from `scene`) with a reply, and his post ONIONINFIRST in the
    first domain's art; the first domain's bob, its admin, and the first domain's own post FIRSTPOST."""
    first = two_domains.domains[0]
    local_only_actors_hidden_world.post = Post.query.filter_by(title='ONIONPOST').one()
    local_only_actors_hidden_world.reply = stamped(make_post_reply(local_only_actors_hidden_world.post, local_only_actors_hidden_world.onion_bob), local_only_actors_hidden_world.onion_bob, local_only_actors_hidden_world.onion_art, None,
                          local_only_actors_hidden_world.post)
    local_only_actors_hidden_world.reply.body_html = '<p>ONIONREPLY</p>'
    local_only_actors_hidden_world.in_first = stamped(make_post(local_only_actors_hidden_world.first_art, local_only_actors_hidden_world.onion_bob, f'http://{ONION}/post/in-first',
                                       title='ONIONINFIRST'), local_only_actors_hidden_world.onion_bob, local_only_actors_hidden_world.first_art)
    local_only_actors_hidden_world.first_post = make_post(local_only_actors_hidden_world.first_art, local_only_actors_hidden_world.first_bob, f'https://{FIRST}/post/first', title='FIRSTPOST')
    local_only_actors_hidden_world.admin = two_domains.local_user('boss', first)
    local_only_actors_hidden_world.admin.private_key = 'a key'
    local_only_actors_hidden_world.admin.roles.append(Role.query.filter_by(name='Admin').first() or Role(name='Admin', built_in=True))
    for user in (local_only_actors_hidden_world.first_bob, local_only_actors_hidden_world.admin):
        user.rss_token = f'{TOKEN}-{user.id}'
    db.session.commit()
    grant_permission(local_only_actors_hidden_world.admin, 'administer all users')
    for post in (local_only_actors_hidden_world.post, local_only_actors_hidden_world.in_first, local_only_actors_hidden_world.first_post):
        post.posted_at = post.created_at = post.last_active = utcnow()
        post.score = post.up_votes = 50
        post.ranking = post.ranking_scaled = 1000
    topic = Topic(machine_name='arts', name='Arts')
    db.session.add(topic)
    db.session.flush()
    for community in (local_only_actors_hidden_world.first_art, local_only_actors_hidden_world.onion_art):   # in every feed: all, popular, a topic, the subscribed
        community.show_all = community.show_popular = True
        community.topic_id = topic.id
        for user in (local_only_actors_hidden_world.first_bob, local_only_actors_hidden_world.admin):
            db.session.add(CommunityMember(user_id=user.id, community_id=community.id))
    db.session.commit()
    return local_only_actors_hidden_world
