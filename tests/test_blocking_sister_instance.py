"""A user can block one of our other domains like any instance, never their own (plan C2, task 5). "The local
instance" is the viewer's home domain's instance, on every storefront."""
from types import SimpleNamespace

import pytest
from flask import g

from app import db
from app.api.alpha.utils.post import get_post_list
from app.api.alpha.utils.site import post_site_block
from app.models import InstanceBlock, Site
from tests.factories import bearer, make_instance, make_post, make_post_reply, make_user
from tests.test_per_domain_html_routes import BASE, Visitor
from tests.two_domains import FIRST, ONION, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')

OWN = 'You cannot block the local instance.'


@pytest.fixture
def world(app, two_domains):
    first, onion = two_domains.domains
    viewers = {FIRST: two_domains.local_user('watcher', first), ONION: two_domains.local_user('viewer', onion)}
    for viewer in viewers.values():
        viewer.private_key = 'a key'
    communities = {FIRST: two_domains.local_community('firstcom', first), ONION: two_domains.local_community('torcom', onion)}
    communities[FIRST].title, communities[ONION].title = 'First community', 'Onion community'
    db.session.get(Site, 1).private_instance = False
    db.session.commit()
    posts = {host: make_post(communities[host], viewers[host], f'{BASE[host]}/post/{host}', title=f'post on {host}')
             for host in (FIRST, ONION)}
    replies = {host: make_post_reply(posts[host], viewers[host]) for host in (FIRST, ONION)}
    for reply in replies.values():
        reply.body_html = '<p>a reply</p>'
    db.session.commit()
    return SimpleNamespace(viewers=viewers, communities=communities, posts=posts, replies=replies,
                           instances={FIRST: first.instance_id, ONION: onion.instance_id})


def blocked(user):
    return {b.instance_id for b in InstanceBlock.query.filter_by(user_id=user.id)}


def feed(viewer, community):
    g.admin_ids = []
    return get_post_list(bearer(viewer), {'community_id': community.id})['posts']


def test_an_onion_user_blocks_the_first_domains_instance_on_the_web(app, world):
    """Follow-up F6: the first domain never shows the onion (to anyone), so the sister block is tried from the onion."""
    viewer = world.viewers[ONION]
    assert len(feed(viewer, world.communities[FIRST])) == 1
    visitor = Visitor(app, viewer, ONION)
    assert 'First community' in visitor.get('/communities').get_data(as_text=True)
    assert visitor.post(f'/instance/{world.instances[FIRST]}/block').status_code == 302
    assert blocked(viewer) == {world.instances[FIRST]}
    assert feed(viewer, world.communities[FIRST]) == []
    page = visitor.get('/communities').get_data(as_text=True)
    assert 'First community' not in page
    assert 'Onion community' in page


def test_a_first_user_blocks_the_onion_instance_through_the_api(app, world):
    viewer = world.viewers[FIRST]
    assert post_site_block(bearer(viewer), {'instance_id': world.instances[ONION], 'block': True}) == {'blocked': True}
    assert feed(viewer, world.communities[ONION]) == []
    assert len(feed(viewer, world.communities[FIRST])) == 1


@pytest.mark.parametrize('host', [FIRST, ONION])
def test_blocking_ones_own_instance_is_refused_on_the_web(app, world, host):
    viewer = world.viewers[host]
    visitor = Visitor(app, viewer, host)
    assert visitor.post(f'/instance/{world.instances[host]}/block').status_code == 302
    assert blocked(viewer) == set()
    with visitor.client.session_transaction(base_url=visitor.base) as session:
        assert OWN in [message for _, message in session['_flashes']]
        assert 'Content from' not in ' '.join(message for _, message in session['_flashes'])


@pytest.mark.parametrize('host', [FIRST, ONION])
def test_blocking_ones_own_instance_is_refused_through_the_api(app, world, host):
    with pytest.raises(Exception) as refused:
        post_site_block(bearer(world.viewers[host]), {'instance_id': world.instances[host], 'block': True})
    assert str(refused.value) == OWN
    assert blocked(world.viewers[host]) == set()


def test_an_onion_user_blocks_first_domains_instance(app, world):
    viewer = world.viewers[ONION]
    assert len(feed(viewer, world.communities[FIRST])) == 1
    assert Visitor(app, viewer, ONION).post('/instance/1/block').status_code == 302
    assert blocked(viewer) == {1}
    assert feed(viewer, world.communities[FIRST]) == []
    assert len(feed(viewer, world.communities[ONION])) == 1


def test_the_onion_user_may_block_first_through_the_api(app, world):
    assert post_site_block(bearer(world.viewers[ONION]), {'instance_id': 1, 'block': True}) == {'blocked': True}


@pytest.mark.parametrize('host', [FIRST, ONION])
def test_a_post_or_reply_on_a_sister_blocks_its_instance(app, world, host):
    sister = ONION if host == FIRST else FIRST
    viewer = world.viewers[host]
    visitor = Visitor(app, viewer, host)
    post, reply = world.posts[sister], world.replies[sister]
    assert visitor.post(f'/post/{post.id}/comment/{reply.id}/block_instance').status_code == 302
    assert blocked(viewer) == {world.instances[sister]}
    assert visitor.post(f'/post/{post.id}/block_instance').status_code == 302


@pytest.mark.parametrize('host', [FIRST, ONION])
def test_a_post_or_reply_from_ones_own_instance_cannot_be_instance_blocked(app, world, host):
    visitor = Visitor(app, world.viewers[host], host)
    post, reply = world.posts[host], world.replies[host]
    assert visitor.post(f'/post/{post.id}/block_instance').status_code == 404
    assert visitor.post(f'/post/{post.id}/comment/{reply.id}/block_instance').status_code == 404
    assert blocked(world.viewers[host]) == set()


@pytest.mark.parametrize('host', [FIRST, ONION])
def test_the_hide_instance_option_shows_on_a_sisters_post_not_ones_own(app, world, host):
    sister = ONION if host == FIRST else FIRST
    visitor = Visitor(app, world.viewers[host], host)
    for who, shown in ((sister, True), (host, False)):
        post, reply = world.posts[who], world.replies[who]
        menu = visitor.get(f'/post/{post.id}/options_menu').get_data(as_text=True)
        assert (f'/post/{post.id}/block_instance' in menu) is shown
        menu = visitor.get(f'/post/{post.id}/comment/{reply.id}/options_menu').get_data(as_text=True)
        assert (f'/comment/{reply.id}/block_instance' in menu) is shown


@pytest.mark.parametrize('host', [FIRST, ONION])
def test_the_settings_box_takes_a_sister_but_not_ones_own_instance(app, world, host, monkeypatch):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # the page renders its form's token
    sister = ONION if host == FIRST else FIRST
    viewer = world.viewers[host]
    visitor = Visitor(app, viewer, host)
    assert visitor.post('/user/settings/block/instance', data={'instance_domain': host}).status_code == 200
    assert blocked(viewer) == set()
    assert visitor.post('/user/settings/block/instance', data={'instance_domain': sister}).status_code == 302
    assert blocked(viewer) == {world.instances[sister]}


def flashes(visitor):
    with visitor.client.session_transaction(base_url=visitor.base) as session:
        return [message for _category, message in session.get('_flashes', [])]


@pytest.mark.parametrize('host', [FIRST, ONION])
def test_a_profile_on_a_sister_blocks_its_instance_but_not_ones_own(app, world, host):
    sister = ONION if host == FIRST else FIRST
    viewer = world.viewers[host]
    visitor = Visitor(app, viewer, host)
    assert visitor.post(f'/u/{viewer.user_name}/block_instance').status_code == 302
    assert blocked(viewer) == set()
    assert flashes(visitor) == ['You cannot block this instance.']
    if host == FIRST:   # follow-up F6: off the onion its accounts are unknown
        assert visitor.post(f'/u/{world.viewers[sister].user_name}@{sister}/block_instance').status_code == 404
        return
    assert visitor.post(f'/u/{world.viewers[sister].user_name}@{sister}/block_instance').status_code == 302
    assert blocked(viewer) == {world.instances[sister]}
    assert f'Content from {sister} will be hidden.' in flashes(visitor)


def test_a_sisters_profile_blocked_over_htmx_goes_home_when_viewed_on_that_domain(app, world):
    """From the onion: the first domain never shows the onion's profiles (follow-up F6)."""
    visitor = Visitor(app, world.viewers[ONION], ONION)
    name = world.viewers[FIRST].user_name
    response = visitor.client.post(f'/u/{name}@{FIRST}/block_instance', data={'csrf_token': visitor.token},
                                   base_url=visitor.base,
                                   headers={'HX-Request': 'true', 'HX-Current-Url': f'https://{FIRST}/u/{name}'})
    assert response.status_code == 200
    assert response.headers['HX-Redirect'] == '/home'


def test_a_remote_profile_blocks_its_instance_naming_the_remote_domain(app, world):
    remote = make_instance('remote.example', software='lemmy')
    stranger = make_user(remote, 'stranger')
    db.session.commit()
    viewer = world.viewers[FIRST]
    visitor = Visitor(app, viewer, FIRST)
    assert visitor.post(f'/u/stranger@remote.example/block_instance').status_code == 302
    assert blocked(viewer) == {remote.id}
    assert 'Content from remote.example will be hidden.' in flashes(visitor)


@pytest.mark.parametrize('host', [FIRST, ONION])
def test_a_chat_blocks_a_sister_instance_but_not_ones_own(app, world, host):
    sister = ONION if host == FIRST else FIRST
    viewer = world.viewers[host]
    visitor = Visitor(app, viewer, host)
    assert visitor.post(f'/chat/{world.instances[host]}/block_instance').status_code == 302
    assert blocked(viewer) == set()
    assert flashes(visitor) == [OWN]
    assert visitor.post(f'/chat/{world.instances[sister]}/block_instance').status_code == 302
    assert blocked(viewer) == {world.instances[sister]}
    assert 'Instance blocked.' in flashes(visitor)


def test_a_viewer_with_no_home_instance_is_refused(app, world):
    viewer = world.viewers[FIRST]
    viewer.instance_id = None
    db.session.commit()
    with pytest.raises(Exception) as refused:
        post_site_block(bearer(viewer), {'instance_id': world.instances[ONION], 'block': True})
    assert str(refused.value) == OWN
    assert blocked(viewer) == set()


@pytest.mark.parametrize('host', [FIRST, ONION])
def test_unblocking_a_sister_instance_brings_its_content_back(app, world, host):
    sister = ONION if host == FIRST else FIRST
    viewer = world.viewers[host]
    visitor = Visitor(app, viewer, host)
    assert visitor.post(f'/instance/{world.instances[sister]}/block').status_code == 302
    assert feed(viewer, world.communities[sister]) == []
    assert visitor.post(f'/instance/{world.instances[sister]}/unblock').status_code == 302
    assert blocked(viewer) == set()
    assert len(feed(viewer, world.communities[sister])) == 1
