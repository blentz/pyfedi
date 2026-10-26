"""`/community/<id>`: the community page reached by id, for a link that must work on whichever domain the viewer is on
(follow-up F1). `/c/<name>` names a community on the current storefront, so a link built from a name reaches a namesake
on another domain; an id names one community. The page and its visibility gates are `/c/<name>`'s."""
import pytest

from app import db
from app.models import Site
from tests.two_domains import FIRST, ONION, two_domains  # noqa: F401  (the fixture)

pytestmark = pytest.mark.usefixtures('site')

PROXY = '10.89.0.2'
MARKER = {'X-PieFed-Via': 'onion', 'X-Forwarded-For': '10.89.0.7'}
FROM_CADDY = {'REMOTE_ADDR': PROXY}


@pytest.fixture
def public(app, monkeypatch):
    """The site is public (it defaults to private, which redirects an anonymous visitor to log in)."""
    monkeypatch.setitem(app.config, 'ONION_PROXY_IP', PROXY)
    db.session.get(Site, 1).private_instance = False
    db.session.commit()


def on_first(client, path):
    return client.get(path, base_url=f'https://{FIRST}')


def on_onion(client, path):
    return client.get(path, base_url=f'http://{ONION}', headers=MARKER, environ_base=FROM_CADDY)


def test_a_first_domain_community_opens_by_id_on_the_first_domain(app, two_domains, public):
    first, onion = two_domains.domains
    community = two_domains.local_community('alpha', first)
    response = on_first(app.test_client(), f'/community/{community.id}')
    assert response.status_code == 200
    assert b'alpha' in response.data


def test_a_first_domain_community_opens_by_id_on_the_onion(app, two_domains, public):
    """The link stays on the domain the viewer is on and still names the one community."""
    first, onion = two_domains.domains
    community = two_domains.local_community('alpha', first)
    two_domains.local_community('alpha', onion)      # a namesake: /c/alpha on the onion is that one
    response = on_onion(app.test_client(), f'/community/{community.id}')
    assert response.status_code == 200
    assert b'alpha' in response.data


def test_an_onion_community_is_hidden_from_a_visitor_who_is_not_logged_in(app, two_domains, public):
    first, onion = two_domains.domains
    community = two_domains.local_community('hidden', onion)
    assert on_onion(app.test_client(), f'/community/{community.id}').status_code == 404
    assert on_onion(app.test_client(), '/c/hidden').status_code == 404      # the same answer /c/<name> gives


def test_an_onion_community_opens_by_id_for_a_logged_in_visitor(app, two_domains, public):
    first, onion = two_domains.domains
    community = two_domains.local_community('hidden', onion)
    member = two_domains.local_user('visitor', first)
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(member.id)
        session['_fresh'] = True
    assert on_first(client, f'/community/{community.id}').status_code == 200


def test_a_banned_community_is_not_found_by_id(app, two_domains, public):
    first, onion = two_domains.domains
    community = two_domains.local_community('gone', first)
    assert on_first(app.test_client(), f'/community/{community.id}').status_code == 200
    community.banned = True
    db.session.commit()
    assert on_first(app.test_client(), f'/community/{community.id}').status_code == 404
    assert on_onion(app.test_client(), f'/community/{community.id}').status_code == 404


def test_a_private_community_is_forbidden_to_a_non_member(app, two_domains, public):
    first, onion = two_domains.domains
    community = two_domains.local_community('club', first)
    community.private = True
    db.session.commit()
    assert on_first(app.test_client(), f'/community/{community.id}').status_code == 403


def test_a_community_id_that_does_not_exist_is_not_found(app, two_domains, public):
    assert on_first(app.test_client(), '/community/987654').status_code == 404


@pytest.mark.parametrize('path', ['/community/99999999999999999999', '/user/99999999999999999999'])
def test_an_oversized_id_is_not_found_not_a_server_error(app, two_domains, public, path):
    assert on_first(app.test_client(), path).status_code == 404
