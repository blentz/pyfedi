"""Interop D24: the community list can show only PeerTube or Castopod communities, and badges them."""
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.models import Site
from tests.factories import make_community, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture(autouse=True)
def founder(db_session):
    make_user(make_instance('test.piefed.local', software='piefed'), 'founder', local=True)


@pytest.fixture
def listed(app, db_session):
    g.site = db.session.get(Site, 1)
    g.site.private_instance = False   # the factory's Site leaves it unset, which sends anonymous callers to the login
    for software, host in (('peertube', 'tube.example'), ('castopod', 'pod.example'), ('lemmy', 'lemmy.example')):
        instance = make_instance(host, software=software)
        community = make_community(f'c_{software}', host=host)
        community.instance_id = instance.id
        community.ap_id = f'c_{software}@{host}'
    db.session.commit()
    return app


def names(app, **args):
    seen = {}
    with patch('app.main.routes.render_template', side_effect=lambda t, **k: seen.update(k) or 'ok'):
        assert app.test_client().get('/communities', query_string=args).status_code == 200
    return sorted(c.name for c in seen['communities'].items), seen


@pytest.mark.parametrize('platform, expected', [
    ('peertube', ['c_peertube']), ('castopod', ['c_castopod'])])
def test_a_platform_keeps_only_its_communities(listed, platform, expected):
    assert [n for n in names(listed, platform=platform)[0] if n.startswith('c_')] == expected


@pytest.mark.parametrize('platform', ['', 'lemmy', 'x'])
def test_no_or_unknown_platform_filters_nothing(listed, platform):
    found, seen = names(listed, platform=platform)
    assert {'c_peertube', 'c_castopod', 'c_lemmy'} <= set(found)
    assert seen['platform'] == ''


def test_the_page_badges_media_communities_and_offers_the_filter(listed):
    html = listed.test_client().get('/communities').get_data(as_text=True)
    assert 'name="platform"' in html
    assert 'platform_badge' in html and 'PeerTube' in html and 'Castopod' in html
