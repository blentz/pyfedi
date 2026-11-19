"""Fixtures from tests/test_c3_golden_documents.py that other test modules request (registered in tests/conftest.py)."""
import pytest

from app import cache, db
from app.models import (
    Community,
    CommunityMember,
    Feed,
    FeedItem,
    File,
    Language,
    Site,
    SiteDomain,
    User,
    UserExtraField,
)
from app.storefront import clear_domain_cache, domain_for_host
from tests.test_c3_golden_documents import CREATED, SHOP, UPDATED, pem
from tests.two_domains import FIRST


@pytest.fixture
def golden_site(app, two_domains):
    """the first domain's site, account, community and feed, all on fixed values, with shop.example federating beside it."""
    db.session.add(Site(name='Golden Site', description='Fixed for the golden copies', blocked_phrases='',
                        public_key=pem('SITE'), created_at=CREATED, updated=UPDATED))
    db.session.add(SiteDomain(domain=SHOP, scheme='https', federates=True))
    db.session.commit()
    clear_domain_cache()
    first = domain_for_host(FIRST)
    base = f'https://{FIRST}'

    avatar, cover = File(file_path='app/static/media/users/go/ld/avatar.webp'), \
        File(thumbnail_path='app/static/media/users/go/ld/cover.webp')
    icon, banner = File(file_path='app/static/media/communities/go/ld/icon.webp'), \
        File(file_path='app/static/media/communities/go/ld/banner.webp')
    feed_icon, feed_banner = File(file_path='app/static/media/feeds/go/ld/icon.webp'), \
        File(file_path='app/static/media/feeds/go/ld/banner.webp')
    db.session.add_all([avatar, cover, icon, banner, feed_icon, feed_banner])
    db.session.flush()

    user = User(user_name='goldie', title='Goldie', email='goldie@example.com', instance_id=first.instance_id,
                site_domain_id=first.id, verified=True, banned=False, public_key=pem('USER'), created=CREATED,
                about='About *goldie*', about_html='<p>About <em>goldie</em></p>', matrix_user_id='@goldie:matrix.org',
                ap_profile_id=f'{base}/u/goldie', ap_public_url=f'{base}/u/goldie',
                ap_inbox_url=f'{base}/u/goldie/inbox', avatar_id=avatar.id, cover_id=cover.id)
    db.session.add(user)
    db.session.flush()
    db.session.add(UserExtraField(user_id=user.id, label='Site', text='https://goldie.example'))

    english = Language(code='en', name='English')
    community = Community(name='goldart', title='Golden art', description='About *art*',
                          description_html='<p>About <em>art</em></p>', posting_warning='Be kind',
                          instance_id=first.instance_id, site_domain_id=first.id, user_id=user.id,
                          public_key=pem('COMMUNITY'), created_at=CREATED, last_active=UPDATED,
                          ap_profile_id=f'{base}/c/goldart', ap_public_url=f'{base}/c/goldart',
                          ap_followers_url=f'{base}/c/goldart/followers',
                          ap_moderators_url=f'{base}/c/goldart/moderators', ap_domain=FIRST,
                          subscriptions_count=1, local_only=False, nsfw=False, icon_id=icon.id, image_id=banner.id)
    community.languages.append(english)
    db.session.add(community)
    db.session.flush()
    db.session.add(CommunityMember(user_id=user.id, community_id=community.id, is_moderator=True, is_owner=True))

    feed = Feed(name='goldfeed', title='Golden feed', description='About *feeds*',
                description_html='<p>About <em>feeds</em></p>', public=True, user_id=user.id,
                instance_id=first.instance_id, public_key=pem('FEED'), created_at=CREATED, last_edit=UPDATED,
                site_domain_id=first.id, ap_profile_id=f'{base}/f/goldfeed', ap_public_url=f'{base}/f/goldfeed',
                ap_followers_url=f'{base}/f/goldfeed/followers', ap_following_url=f'{base}/f/goldfeed/following',
                ap_outbox_url=f'{base}/f/goldfeed/outbox', icon_id=feed_icon.id, image_id=feed_banner.id)
    db.session.add(feed)
    db.session.flush()
    db.session.add(FeedItem(feed_id=feed.id, community_id=community.id))
    db.session.commit()
    cache.clear()   # icon_image, header_image and the Application document are memoized
    yield
    cache.clear()
