"""/statistics (v1.8.10): the instances this one knows that were active in the last month, busiest first, optionally
of one software.

Three defects in v1.8.10's page, fixed here:

* it listed every instance row, a local-only domain's too, so the onion's row was on the clearnet's page (owner ruling,
  plan C2 task 6: off its own storefront an onion's row is never listed);
* its page links went to the moderation log (`main.modlog`), not to the next page of statistics;
* it told shared caches to keep the page an hour whoever asked, though a signed-in reader's page carries their
  account's menu; it is public for anonymous readers only, as the home and post pages are.
"""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app import db
from app.models import Instance, Site, utcnow
from tests.factories import make_instance, make_user
from tests.test_instance_visible_here import get, scene  # noqa: F401 -- scene is a fixture
from tests.test_per_domain_ap_routes import onion_proxy, twins  # noqa: F401
from tests.test_per_domain_html_routes import Visitor
from tests.two_domains import FIRST, ONION, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')


def active(domain, software, users, **columns):
    instance = make_instance(domain, software=software)
    instance.active_1mo = users
    for column, value in columns.items():
        setattr(instance, column, value)
    db.session.commit()
    return instance


@pytest.fixture
def known(app, db_session):
    make_instance('test.piefed.local', software='piefed')
    db.session.get(Site, 1).private_instance = False
    db.session.commit()
    return SimpleNamespace(busy=active('busy.example', 'Lemmy', 900), quiet=active('quiet.example', 'piefed', 5),
                           idle=active('idle.example', 'lemmy', 0),
                           gone=active('gone.example', 'lemmy', 50, gone_forever=True))


def listed(body: bytes) -> list[str]:
    return [domain for domain in ('busy.example', 'quiet.example', 'idle.example', 'gone.example')
            if f'/instance/{domain}"'.encode() in body]


def test_active_instances_are_listed_busiest_first(app, known):
    body = app.test_client().get('/statistics').data

    assert listed(body) == ['busy.example', 'quiet.example']
    assert body.index(b'busy.example') < body.index(b'quiet.example')


@pytest.mark.parametrize('path', ['/statistics/LEMMY', '/statistics?software=lemmy'])
def test_one_softwares_instances(app, known, path):
    body = app.test_client().get(path).data

    assert listed(body) == ['busy.example']
    assert b'Statistics for' in body


def test_this_instances_own_count_is_refreshed_when_a_day_old(app, known, monkeypatch):
    local = db.session.get(Instance, 1)
    local.active_1mo, local.updated_at = 3, utcnow() - timedelta(days=2)
    db.session.commit()
    monkeypatch.setattr('app.main.routes.active_month', lambda: 42)

    app.test_client().get('/statistics')

    db.session.expire_all()
    assert db.session.get(Instance, 1).active_1mo == 42


def test_page_links_name_the_statistics_page(app, known, monkeypatch):
    monkeypatch.setattr('app.main.routes.STATISTICS_PAGE_LENGTH', 1)
    active('more.example', 'lemmy', 3)
    body = app.test_client().get('/statistics?page=2').data.decode()
    lemmy = app.test_client().get('/statistics/lemmy?page=1').data.decode()

    assert 'href="/statistics?page=3"' in body and 'href="/statistics?page=1"' in body and '/modlog?page' not in body
    assert 'href="/statistics/lemmy?page=2"' in lemmy
    assert b'/instance/more.example"' in app.test_client().get('/statistics?page=3').data


def test_an_anonymous_page_is_cached_publicly_and_a_signed_in_one_privately(app, known):
    reader = make_user(db.session.get(Instance, 1), 'reader', local=True)
    reader.private_key = 'a key'
    db.session.commit()

    anonymous = app.test_client().get('/statistics')
    signed_in = Visitor(app, reader, FIRST).get('/statistics')

    assert anonymous.headers['Cache-Control'] == 'public, max-age=3600'
    assert 'public' not in signed_in.headers['Cache-Control']
    assert 'private' in signed_in.headers['Cache-Control']


def test_the_onions_row_is_listed_on_the_onion_only(app, scene):  # noqa: F811
    scene.onion_row.active_1mo = scene.peer.active_1mo = 7
    db.session.commit()

    on_first, on_onion = get(app, '/statistics').data, get(app, '/statistics', host=ONION).data

    assert f'/instance/{ONION}"'.encode() not in on_first and f'/instance/{ONION}"'.encode() in on_onion
    assert b'/instance/peer.example"' in on_first
