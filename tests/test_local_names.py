"""One place turns a local name or handle into its domain's row (plan C1, spec §3.1)."""
import pytest

from app.local_names import local_named, local_path_name, split_local_name
from app import db
from app.models import Community, User
from tests.two_domains import HELL, ONION, default_request, onion_request, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')


@pytest.mark.parametrize('raw', ['bob', ' bob ', '@bob', '!bob'])
def test_a_bare_name_is_on_the_storefront(app, two_domains, raw):
    hell, onion = two_domains.domains
    with onion_request(app):
        assert split_local_name(raw) == ('bob', onion)
    with default_request(app):
        assert split_local_name(raw) == ('bob', hell)


@pytest.mark.parametrize('raw,name', [(f'bob@{HELL.upper()}', 'bob'), (f'@Bob@{HELL}', 'Bob'),
                                      (f'!bob@{HELL}', 'bob')])
def test_a_hosted_handle_is_on_its_hosts_domain(app, two_domains, raw, name):
    hell, onion = two_domains.domains
    with onion_request(app):
        assert split_local_name(raw) == (name, hell)


@pytest.mark.parametrize('raw', ['bob@mastodon.social', '', '@', 'a@b@c', '@@bob'])
def test_anything_else_is_none(app, two_domains, raw):
    with onion_request(app):
        assert split_local_name(raw) is None


def test_local_named_finds_only_that_domains_row(app, two_domains):
    hell, onion = two_domains.domains
    hb, ob = two_domains.local_user('bob', hell), two_domains.local_user('bob', onion)
    assert User.query.filter(local_named(User, 'BOB', onion)).all() == [ob]
    assert User.query.filter(local_named(User, 'bob', hell)).all() == [hb]
    assert User.query.filter(local_named(User, 'nobody', hell)).all() == []


def test_local_named_finds_communities_and_skips_remote_rows(app, two_domains):
    hell, onion = two_domains.domains
    hc, oc = two_domains.local_community('news', hell), two_domains.local_community('news', onion)
    oc_remote = two_domains.local_community('remote', onion)
    oc_remote.ap_id = 'remote@elsewhere.example'
    db.session.commit()
    assert Community.query.filter(local_named(Community, 'News', onion)).all() == [oc]
    assert Community.query.filter(local_named(Community, 'news', hell)).all() == [hc]
    assert Community.query.filter(local_named(Community, 'remote', onion)).all() == []


@pytest.mark.parametrize('make', ['local_user', 'local_community'])
def test_local_path_name_qualifies_only_off_its_own_domain(app, two_domains, make):
    hell, onion = two_domains.domains
    hell_one = getattr(two_domains, make)('bob', hell)
    onion_one = getattr(two_domains, make)('bob', onion)
    with default_request(app):
        assert local_path_name(hell_one) == 'bob'
        assert local_path_name(onion_one) == f'bob@{ONION}'
    with onion_request(app):
        assert local_path_name(hell_one) == f'bob@{HELL}'
        assert local_path_name(onion_one) == 'bob'
