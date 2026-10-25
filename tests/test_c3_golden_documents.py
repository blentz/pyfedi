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
from app.models import Community, CommunityMember, Feed, File, Language, Site, SiteDomain, User, UserExtraField
from app.storefront import clear_domain_cache, domain_for_host
from tests.two_domains import HELL, two_domains  # noqa: F401

GOLDEN = Path(__file__).parent / 'golden' / 'c3'
AP = {'Accept': 'application/activity+json'}
SHOP = 'shop.example'

CREATED = datetime(2025, 3, 4, 5, 6, 7)
UPDATED = datetime(2025, 6, 7, 8, 9, 10)


def pem(label: str) -> str:
    """A fixed stand-in for a public key: the documents carry the PEM as an opaque string."""
    return f'-----BEGIN PUBLIC KEY-----\nGOLDEN{label}KEY\n-----END PUBLIC KEY-----\n'


@pytest.fixture
def golden_site(app, two_domains):
    """the first domain's site, account, community and feed, all on fixed values, with shop.example federating beside it."""
    db.session.add(Site(name='Golden Site', description='Fixed for the golden copies', blocked_phrases='',
                        public_key=pem('SITE'), created_at=CREATED, updated=UPDATED))
    db.session.add(SiteDomain(domain=SHOP, scheme='https', federates=True))
    db.session.commit()
    clear_domain_cache()
    hell = domain_for_host(HELL)
    base = f'https://{HELL}'

    avatar, cover = File(file_path='app/static/media/users/go/ld/avatar.webp'), \
        File(thumbnail_path='app/static/media/users/go/ld/cover.webp')
    icon, banner = File(file_path='app/static/media/communities/go/ld/icon.webp'), \
        File(file_path='app/static/media/communities/go/ld/banner.webp')
    feed_icon, feed_banner = File(file_path='app/static/media/feeds/go/ld/icon.webp'), \
        File(file_path='app/static/media/feeds/go/ld/banner.webp')
    db.session.add_all([avatar, cover, icon, banner, feed_icon, feed_banner])
    db.session.flush()

    user = User(user_name='goldie', title='Goldie', email='goldie@example.com', instance_id=hell.instance_id,
                site_domain_id=hell.id, verified=True, banned=False, public_key=pem('USER'), created=CREATED,
                about='About *goldie*', about_html='<p>About <em>goldie</em></p>', matrix_user_id='@goldie:matrix.org',
                ap_profile_id=f'{base}/u/goldie', ap_public_url=f'{base}/u/goldie',
                ap_inbox_url=f'{base}/u/goldie/inbox', avatar_id=avatar.id, cover_id=cover.id)
    db.session.add(user)
    db.session.flush()
    db.session.add(UserExtraField(user_id=user.id, label='Site', text='https://goldie.example'))

    english = Language(code='en', name='English')
    community = Community(name='goldart', title='Golden art', description='About *art*',
                          description_html='<p>About <em>art</em></p>', posting_warning='Be kind',
                          instance_id=hell.instance_id, site_domain_id=hell.id, user_id=user.id,
                          public_key=pem('COMMUNITY'), created_at=CREATED, last_active=UPDATED,
                          ap_profile_id=f'{base}/c/goldart', ap_public_url=f'{base}/c/goldart',
                          ap_followers_url=f'{base}/c/goldart/followers',
                          ap_moderators_url=f'{base}/c/goldart/moderators', ap_domain=HELL,
                          subscriptions_count=1, local_only=False, nsfw=False, icon_id=icon.id, image_id=banner.id)
    community.languages.append(english)
    db.session.add(community)
    db.session.flush()
    db.session.add(CommunityMember(user_id=user.id, community_id=community.id, is_moderator=True, is_owner=True))

    db.session.add(Feed(name='goldfeed', title='Golden feed', description='About *feeds*',
                        description_html='<p>About <em>feeds</em></p>', public=True, user_id=user.id,
                        instance_id=hell.instance_id, public_key=pem('FEED'), created_at=CREATED, last_edit=UPDATED,
                        ap_profile_id=f'{base}/f/goldfeed', ap_public_url=f'{base}/f/goldfeed',
                        icon_id=feed_icon.id, image_id=feed_banner.id))
    db.session.commit()
    cache.clear()   # icon_image, header_image and the Application document are memoized
    yield
    cache.clear()


VERSION_MARK = b'"version":"{VERSION}"'


def release_mark(app) -> bytes:
    return f'"version":"{app.config["VERSION"]}"'.encode()


def golden_copy(name: str) -> bytes:
    """The golden copy `name` as the running release serves it: a copy pins the document's structure and identity,
    not the release, so the version field is a placeholder in it (VERSION_MARK)."""
    return (GOLDEN / name).read_bytes().replace(VERSION_MARK, release_mark(current_app))


def capture(golden: Path, body: bytes) -> None:
    """A deliberate capture (see the module docstring): written with the release as the placeholder, then failed."""
    golden.parent.mkdir(parents=True, exist_ok=True)
    golden.write_bytes(body.replace(release_mark(current_app), VERSION_MARK))
    pytest.fail(f'captured {golden}; review it and run again')


def served(app, path: str, host: str = HELL):
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
    'instance_actor.json': '/actor',
    'instance_actor_outbox.json': '/actor/outbox',
    'application.json': '/',
}


@pytest.mark.usefixtures('golden_site')
@pytest.mark.parametrize('name, path', sorted(GOLDEN_PATHS.items()))
def test_hells_document_is_its_golden_copy(app, name, path):
    body = served(app, path).data
    if not (GOLDEN / name).exists():
        capture(GOLDEN / name, body)
    assert body == golden_copy(name)
    json.loads(body)   # the copy is a document, not an error page


@pytest.mark.usefixtures('golden_site')
@pytest.mark.parametrize('path, link', [('/u/goldie', f'<https://{HELL}/u/goldie>'),
                                        ('/c/goldart', f'<https://{HELL}/c/goldart>'),
                                        ('/f/goldfeed', f'<https://{HELL}/f/goldfeed>')])
def test_hells_actor_documents_link_to_hells_page(app, path, link):
    assert served(app, path).headers['Link'] == f'{link}; rel="alternate"; type="text/html"'


# the first domain's discovery documents (plan C3, task 4): WebFinger, host-meta, NodeInfo and the site documents, captured
# from the code before Task 4 made them per domain (BASE b49a5ca83). Only the release is a placeholder. The fixture
# fixes every count (one account, no posts or comments, one community), and Task 4's count change (hell counts only
# its own accounts) does not show here, as no other domain has an account in this fixture.
DISCOVERY_PATHS = {
    'host_meta.xml': '/.well-known/host-meta',
    'host_meta.json': '/.well-known/host-meta.json',
    'nodeinfo_index.json': '/.well-known/nodeinfo',
    'nodeinfo_2.0.json': '/nodeinfo/2.0',
    'nodeinfo_2.1.json': '/nodeinfo/2.1',
    'webfinger_user.json': f'/.well-known/webfinger?resource=acct:goldie@{HELL}',
    'webfinger_community.json': f'/.well-known/webfinger?resource=acct:goldart@{HELL}',
    'webfinger_feed.json': f'/.well-known/webfinger?resource=acct:~goldfeed@{HELL}',
    'webfinger_instance_actor.json': f'/.well-known/webfinger?resource=acct:{HELL}@{HELL}',
    'api_v1_instance.json': '/api/v1/instance',
    'api_v3_site.json': '/api/v3/site',
}


@pytest.mark.usefixtures('golden_site')
@pytest.mark.parametrize('name, path', sorted(DISCOVERY_PATHS.items()))
def test_hells_discovery_document_is_its_golden_copy(app, name, path):
    response = app.test_client().get(path, base_url=f'https://{HELL}')
    assert response.status_code == 200, path
    if not (GOLDEN / name).exists():
        capture(GOLDEN / name, response.data)
    assert response.data == golden_copy(name)


@pytest.mark.usefixtures('golden_site')
@pytest.mark.parametrize('name', ['api_v3_site.json', 'nodeinfo_2.0.json', 'nodeinfo_2.1.json'])
def test_a_release_bump_changes_no_golden_copy(app, monkeypatch, name):
    """The copies pin the documents' structure and identity, not the release: VERSION is a placeholder in them."""
    monkeypatch.setitem(app.config, 'VERSION', '99.0.0')
    response = app.test_client().get(DISCOVERY_PATHS[name], base_url=f'https://{HELL}')
    assert b'"99.0.0"' in response.data
    assert response.data == golden_copy(name)
