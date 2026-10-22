"""User counts per domain (plan C2 final wave, owner ruling). A count served to other servers or to logged-out callers
counts the default domain's instance row only, as NodeInfo does: a hosted local-only domain's accounts are on its own
row and are not counted. The home page's "This server" counts follow the domain the page is on, and its cached
fragment is one per domain, so one domain's numbers are never served on the other."""
import re

import pytest
from cachelib import SimpleCache
from flask import g

from app import cache, db
from app.api.alpha.views import site_instance_chooser_view, site_view
from app.models import Site, User, utcnow
from app.storefront import clear_domain_cache
from tests.test_per_domain_local_filter import _visit
from tests.two_domains import request_on, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def crowd(app, two_domains):  # noqa: F811
    """101 active accounts on hell.cloud's row and 103 on the onion's, so each domain's home page shows its counts
    (they show above 100 monthly users) and either one's number tells which accounts it counted."""
    hell, onion = two_domains.domains
    now = utcnow()
    for domain, count in ((hell, 101), (onion, 103)):
        db.session.add_all(User(user_name=f'{domain.domain[0]}crowd{n}', email=f'{n}@{domain.domain}', verified=True,
                                banned=False, deleted=False, last_seen=now, instance_id=domain.instance_id,
                                site_domain_id=domain.id, ap_profile_id=f'{domain.scheme}://{domain.domain}/u/x{n}')
                           for n in range(count))
    db.session.get(Site, 1).private_instance = False
    db.session.commit()
    return two_domains


def counted(domain):
    return User.query.filter(User.ap_id.is_(None), User.instance_id == domain.instance_id).count()


@pytest.mark.parametrize('on', [0, 1])
def test_the_site_api_and_the_instance_chooser_count_only_the_default_domains_accounts(app, crowd, on):
    hell = crowd.domains[0]
    with request_on(app, crowd.domains[on]):
        g.site, g.admin_ids = db.session.get(Site, 1), []
        assert site_view(None)['site']['user_count'] == counted(hell) == 101
        assert site_instance_chooser_view()['mau'] == 101


def test_the_home_page_counts_follow_its_domain(app, crowd):
    for domain, expected in zip(crowd.domains, (101, 103)):
        with request_on(app, domain):
            site = db.session.get(Site, 1)
            assert (site.active_daily(), site.active_weekly(), site.active_monthly(), site.active_6monthly()) == \
                (expected,) * 4
    assert db.session.get(Site, 1).active_monthly() == 101   # no request: the default domain's


def monthly_users(response):
    return re.findall(r'(\d+) Monthly Users', response.get_data(as_text=True))[0]


@pytest.fixture
def fragment_cache(app, monkeypatch):
    """A real cache behind the {% cache %} fragment, which the test config's NullCache never keeps."""
    monkeypatch.setitem(app.extensions['cache'], cache, SimpleCache())
    yield
    clear_domain_cache()


def test_the_cached_home_page_counts_are_one_domains_each(app, crowd, fragment_cache):
    assert monthly_users(_visit(app, '/')) == '101'
    assert monthly_users(_visit(app, '/', onion=True)) == '103'
    assert monthly_users(_visit(app, '/')) == '101'
