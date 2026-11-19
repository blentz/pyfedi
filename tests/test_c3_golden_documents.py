"""the first domain's ActivityPub documents, byte for byte (plan C3, task 2): what the default domain serves does not change
while its sister domains learn to name themselves.

Each golden copy in tests/golden/c3/ was captured from the code before C3 touched these documents (BASE b94132dda),
for one fixed fixture: every key, timestamp, name and file path below is a constant, so nothing in a document varies
between runs. Only the release is not pinned: a version field holds `{VERSION}`, which `golden_copy` fills with the
running release, so a version bump changes no copy. A sister federating domain (shop.example) and the onion exist
alongside, so the copies show that their presence changes nothing on the default domain.

A missing golden file is written from the served document and the test fails, so a capture is always deliberate:
delete the file, run once, review the diff and commit it."""
import json
from datetime import datetime
from pathlib import Path

import pytest
from flask import current_app

from app import cache, db
from app.models import Community, CommunityMember, Feed, FeedItem, File, Language, Site, SiteDomain, User, UserExtraField
from app.storefront import clear_domain_cache, domain_for_host
from tests.two_domains import FIRST

GOLDEN = Path(__file__).parent / 'golden' / 'c3'
AP = {'Accept': 'application/activity+json'}
SHOP = 'shop.example'

CREATED = datetime(2025, 3, 4, 5, 6, 7)
UPDATED = datetime(2025, 6, 7, 8, 9, 10)


def pem(label: str) -> str:
    """A fixed stand-in for a public key: the documents carry the PEM as an opaque string."""
    return f'-----BEGIN PUBLIC KEY-----\nGOLDEN{label}KEY\n-----END PUBLIC KEY-----\n'


VERSION_MARK = b'"version":"{VERSION}"'


# Mastodon's instance documents name the release inside their version (`4.0.0 (compatible; PieFed 1.8.10)`)
COMPAT_MARK = b'PieFed {VERSION})'


def release_mark(app) -> bytes:
    return f'"version":"{app.config["VERSION"]}"'.encode()


def compat_mark(app) -> bytes:
    return f'PieFed {app.config["VERSION"]})'.encode()


def golden_copy(name: str) -> bytes:
    """The golden copy `name` as the running release serves it: a copy pins the document's structure and identity,
    not the release, so the version field is a placeholder in it (VERSION_MARK, COMPAT_MARK)."""
    return (GOLDEN / name).read_bytes().replace(VERSION_MARK, release_mark(current_app)) \
        .replace(COMPAT_MARK, compat_mark(current_app))


def capture(golden: Path, body: bytes) -> None:
    """A deliberate capture (see the module docstring): written with the release as the placeholder, then failed."""
    golden.parent.mkdir(parents=True, exist_ok=True)
    golden.write_bytes(body.replace(release_mark(current_app), VERSION_MARK).replace(compat_mark(current_app),
                                                                                     COMPAT_MARK))
    pytest.fail(f'captured {golden}; review it and run again')


def served(app, path: str, host: str = FIRST):
    response = app.test_client().get(path, headers=AP, base_url=f'https://{host}')
    assert response.status_code == 200, path
    return response


GOLDEN_PATHS = {
    'user.json': '/u/goldie',
    'user_outbox.json': '/u/goldie/outbox',
    'community.json': '/c/goldart',
    'community_outbox.json': '/c/goldart/outbox',
    'community_followers.json': '/c/goldart/followers',
    'community_moderators.json': '/c/goldart/moderators',
    'community_featured.json': '/c/goldart/featured',
    'feed.json': '/f/goldfeed',
    'feed_outbox.json': '/f/goldfeed/outbox',
    'feed_following.json': '/f/goldfeed/following',
    'feed_followers.json': '/f/goldfeed/followers',
    'feed_moderators.json': '/f/goldfeed/moderators',
    'instance_actor.json': '/actor',
    'instance_actor_outbox.json': '/actor/outbox',
    'application.json': '/',
}


@pytest.mark.usefixtures('golden_site')
@pytest.mark.parametrize('name, path', sorted(GOLDEN_PATHS.items()))
def test_first_domains_document_is_its_golden_copy(app, name, path):
    body = served(app, path).data
    if not (GOLDEN / name).exists():
        capture(GOLDEN / name, body)
    assert body == golden_copy(name)
    json.loads(body)   # the copy is a document, not an error page


@pytest.mark.usefixtures('golden_site')
@pytest.mark.parametrize('path, link', [('/u/goldie', f'<https://{FIRST}/u/goldie>'),
                                        ('/c/goldart', f'<https://{FIRST}/c/goldart>'),
                                        ('/f/goldfeed', f'<https://{FIRST}/f/goldfeed>')])
def test_first_domains_actor_documents_link_to_first_domains_page(app, path, link):
    assert served(app, path).headers['Link'] == f'{link}; rel="alternate"; type="text/html"'


# the first domain's discovery documents (plan C3, task 4): WebFinger, host-meta, NodeInfo and the site documents, captured
# from the code before Task 4 made them per domain (BASE b49a5ca83). Only the release is a placeholder. The fixture
# fixes every count (one account, no posts or comments, one community), and Task 4's count change (the first domain counts only
# its own accounts) does not show here, as no other domain has an account in this fixture.
DISCOVERY_PATHS = {
    'host_meta.xml': '/.well-known/host-meta',
    'host_meta.json': '/.well-known/host-meta.json',
    'nodeinfo_index.json': '/.well-known/nodeinfo',
    'nodeinfo_2.0.json': '/nodeinfo/2.0',
    'nodeinfo_2.1.json': '/nodeinfo/2.1',
    'webfinger_user.json': f'/.well-known/webfinger?resource=acct:goldie@{FIRST}',
    'webfinger_community.json': f'/.well-known/webfinger?resource=acct:goldart@{FIRST}',
    'webfinger_feed.json': f'/.well-known/webfinger?resource=acct:~goldfeed@{FIRST}',
    'webfinger_instance_actor.json': f'/.well-known/webfinger?resource=acct:{FIRST}@{FIRST}',
    'api_v1_instance.json': '/api/v1/instance',
    'api_v3_site.json': '/api/v3/site',
}


@pytest.mark.usefixtures('golden_site')
@pytest.mark.parametrize('name, path', sorted(DISCOVERY_PATHS.items()))
def test_first_domains_discovery_document_is_its_golden_copy(app, name, path):
    response = app.test_client().get(path, base_url=f'https://{FIRST}')
    assert response.status_code == 200, path
    if not (GOLDEN / name).exists():
        capture(GOLDEN / name, response.data)
    assert response.data == golden_copy(name)


@pytest.mark.usefixtures('golden_site')
@pytest.mark.parametrize('name', ['api_v3_site.json', 'nodeinfo_2.0.json', 'nodeinfo_2.1.json'])
def test_a_release_bump_changes_no_golden_copy(app, monkeypatch, name):
    """The copies pin the documents' structure and identity, not the release: VERSION is a placeholder in them."""
    monkeypatch.setitem(app.config, 'VERSION', '99.0.0')
    response = app.test_client().get(DISCOVERY_PATHS[name], base_url=f'https://{FIRST}')
    assert b'"99.0.0"' in response.data
    assert response.data == golden_copy(name)
