"""Fixtures from tests/test_per_domain_actor_documents.py that other test modules request (registered in tests/conftest.py)."""
import pytest

from app import cache, db
from app.models import Community, CommunityMember, File, User, UserExtraField
from app.storefront import domain_for_host
from tests.test_c3_golden_documents import CREATED, SHOP, UPDATED, pem
from tests.test_per_domain_actor_documents import SHOP_BASE


@pytest.fixture
def shop_rows(app):
    """goldie and goldart again, on shop.example: the same names as the first domain's, so a lookup or URL that ignores the
    domain shows."""
    shop = domain_for_host(SHOP)
    avatar, cover = File(file_path='app/static/media/users/sh/op/avatar.webp'), \
        File(thumbnail_path='app/static/media/users/sh/op/cover.webp')
    icon, banner = File(file_path='app/static/media/communities/sh/op/icon.webp'), \
        File(file_path='app/static/media/communities/sh/op/banner.webp')
    db.session.add_all([avatar, cover, icon, banner])
    db.session.flush()
    user = User(user_name='goldie', title='Shop goldie', email='goldie@shop.example', instance_id=shop.instance_id,
                site_domain_id=shop.id, verified=True, banned=False, public_key=pem('SHOPUSER'), created=CREATED,
                ap_profile_id=f'{SHOP_BASE}/u/goldie', ap_public_url=f'{SHOP_BASE}/u/goldie',
                ap_inbox_url=f'{SHOP_BASE}/u/goldie/inbox', avatar_id=avatar.id, cover_id=cover.id)
    db.session.add(user)
    db.session.flush()
    db.session.add(UserExtraField(user_id=user.id, label='Site', text='https://goldie.example'))
    community = Community(name='goldart', title='Shop art', instance_id=shop.instance_id, site_domain_id=shop.id,
                          user_id=user.id, public_key=pem('SHOPCOMMUNITY'), created_at=CREATED, last_active=UPDATED,
                          ap_profile_id=f'{SHOP_BASE}/c/goldart', ap_public_url=f'{SHOP_BASE}/c/goldart',
                          ap_followers_url=f'{SHOP_BASE}/c/goldart/followers',
                          ap_moderators_url=f'{SHOP_BASE}/c/goldart/moderators', ap_domain=SHOP,
                          subscriptions_count=1, local_only=False, nsfw=False, icon_id=icon.id, image_id=banner.id)
    db.session.add(community)
    db.session.flush()
    db.session.add(CommunityMember(user_id=user.id, community_id=community.id, is_moderator=True, is_owner=True))
    db.session.commit()
    cache.clear()
    return shop, user, community
