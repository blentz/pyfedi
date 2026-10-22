"""A link host that is one of our own local-only domains stays off every other storefront (plan C2 final wave). An
onion account sharing an onion link files the link under a `Domain` row named for the onion. Off the onion, `/domains`
leaves it out and `/d/<host>` answers 404, as the onion's instance row is hidden there (owner ruling, task 6). On the
onion itself it is listed and shown."""
import pytest

from app import db
from app.models import Domain, Post
from tests.test_instance_visible_here import get, scene  # noqa: F401 -- scene is a fixture
from tests.test_per_domain_ap_routes import onion_proxy, twins  # noqa: F401
from tests.two_domains import HELL, ONION, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def links(scene):  # noqa: F811
    """The onion's post links to the onion, and a hell.cloud post to an ordinary site, each filed under its host."""
    onion_host, ordinary = Domain(name=ONION, post_count=1), Domain(name='example.com', post_count=1)
    db.session.add_all([onion_host, ordinary])
    db.session.flush()
    Post.query.filter_by(title='ONIONPOST').one().domain_id = onion_host.id
    db.session.commit()
    return scene, onion_host, ordinary


@pytest.mark.parametrize('signed_in', [False, True])
@pytest.mark.parametrize('query', ['', '?search=onion'])
def test_hells_domain_list_names_no_onion_host(app, links, signed_in, query):
    scene, onion_host, ordinary = links
    body = get(app, f'/domains{query}', user=scene.hell_bob if signed_in else None).get_data(as_text=True)
    assert ONION not in body
    if not query:
        assert 'example.com' in body


@pytest.mark.parametrize('signed_in', [False, True])
@pytest.mark.parametrize('path', ['/d/{name}', '/d/{id}', '/d/{name}/feed', '/d/{id}/feed'])
def test_hells_onion_host_page_is_not_found(app, links, signed_in, path):
    scene, onion_host, ordinary = links
    user = scene.hell_bob if signed_in else None
    assert get(app, path.format(name=ONION, id=onion_host.id), user=user).status_code == 404
    assert get(app, path.format(name='example.com', id=ordinary.id), user=user).status_code == 200


def test_the_onion_lists_and_shows_its_own_host(app, links):
    scene, onion_host, ordinary = links
    assert ONION in get(app, '/domains', host=ONION, user=scene.onion_bob).get_data(as_text=True)
    assert get(app, f'/d/{ONION}', host=ONION, user=scene.onion_bob).status_code == 200
