"""A link host that is one of our own local-only domains stays off every other storefront (plan C2 final wave). An
onion account sharing an onion link files the link under a `Domain` row named for the onion. Off the onion, `/domains`
leaves it out and `/d/<host>` answers 404, as the onion's instance row is hidden there (owner ruling, task 6). On the
onion itself it is listed and shown."""
from unittest.mock import patch

import pytest

from app import db
from app.models import Domain, Post, SiteDomain
from app.storefront import clear_domain_cache, host_hidden_here, host_not_hidden_clause
from tests.test_instance_visible_here import get, scene  # noqa: F401 -- scene is a fixture
from tests.test_per_domain_ap_routes import onion_proxy, twins  # noqa: F401
from tests.two_domains import HELL, ONION, default_request, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def links(scene):  # noqa: F811
    """The onion's post links to the onion, and a first-domain post to an ordinary site, each filed under its host."""
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


def test_hells_banned_domain_list_names_no_onion_host(app, links):
    """/domains/banned filters as /domains does: a banned onion host stays off hell's list."""
    scene, onion_host, ordinary = links
    onion_host.banned = ordinary.banned = True
    db.session.commit()
    body = get(app, '/domains/banned', user=scene.hell_bob).get_data(as_text=True)
    assert ONION not in body and 'example.com' in body


def test_the_onion_lists_and_shows_its_own_host(app, links):
    scene, onion_host, ordinary = links
    assert ONION in get(app, '/domains', host=ONION, user=scene.onion_bob).get_data(as_text=True)
    assert get(app, f'/d/{ONION}', host=ONION, user=scene.onion_bob).status_code == 200


# --- every spelling of the onion's host (security review of the item-4 commit) ---

VARIANTS = [f'x.{ONION}', f'{ONION}.', ONION.upper(), f'www.{ONION}', f'deep.sub.{ONION}', f'X.{ONION.upper()}.']
UNRELATED = ['zyx.onion', f'x{ONION}', 'example.com']   # another onion; a name that only ends in ours


@pytest.fixture
def spellings(scene):  # noqa: F811
    """A Domain row for each spelling of the onion's host a link can produce (Tor ignores an onion's subdomains, and
    a host may end in a dot or be written in capitals), and some that are not ours."""
    rows = {name: Domain(name=name, post_count=1) for name in VARIANTS + UNRELATED}
    db.session.add_all(rows.values())
    db.session.commit()
    return scene, rows


def listed(app, host, user):
    with patch('app.domain.routes.render_template', return_value='rendered') as render:
        assert get(app, '/domains', host=host, user=user).status_code == 200
    return {d.name for d in render.call_args.kwargs['domains'].items}


@pytest.mark.parametrize('signed_in', [False, True])
def test_no_spelling_of_the_onion_host_is_listed_or_shown_on_hell(app, spellings, signed_in):
    scene, rows = spellings
    user = scene.hell_bob if signed_in else None
    assert listed(app, HELL, user) == set(UNRELATED)
    for name in VARIANTS:
        assert get(app, f'/d/{rows[name].id}', user=user).status_code == 404, name
        assert get(app, f'/d/{rows[name].id}/feed', user=user).status_code == 404, name
    for name in UNRELATED:
        assert get(app, f'/d/{rows[name].id}', user=user).status_code == 200, name


def test_the_onion_lists_and_shows_every_spelling_of_its_host(app, spellings):
    scene, rows = spellings
    assert listed(app, ONION, scene.onion_bob) == set(VARIANTS + UNRELATED)
    for name in VARIANTS:
        assert get(app, f'/d/{rows[name].id}', host=ONION, user=scene.onion_bob).status_code == 200, name


def test_a_like_wildcard_in_a_hidden_host_matches_only_itself(app, two_domains):  # noqa: F811
    """The listing filter escapes LIKE's wildcards: a hidden host with `_` in it hides no other name."""
    db.session.add(SiteDomain(domain='a_b.onion', scheme='http', federates=False))
    db.session.add_all([Domain(name='x.a_b.onion'), Domain(name='x.aXb.onion'), Domain(name='a_b.onion')])
    db.session.commit()
    clear_domain_cache()
    with default_request(app):
        shown = {d.name for d in Domain.query.filter(host_not_hidden_clause(Domain.name))}
        assert not host_hidden_here('x.aXb.onion') and host_hidden_here('X.A_B.ONION.')
    assert shown == {'x.aXb.onion'}
