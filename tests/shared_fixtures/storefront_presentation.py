"""Fixtures from tests/test_storefront_presentation.py that other test modules request (registered in tests/conftest.py)."""
import pytest

from app import cache, db
from app.constants import POST_TYPE_IMAGE
from app.models import File, Site, SiteDomain
from app.storefront import clear_domain_cache
from tests.factories import (
    make_community,
    make_community_member,
    make_instance,
    make_post,
    make_user,
)
from tests.test_storefront_presentation import CANONICAL, MEDIA, ONION, THUMB


@pytest.fixture
def storefront_presentation_onion(db_session):
    if SiteDomain.query.filter_by(domain=CANONICAL).first() is None:   # the canonical host stays served
        db.session.add(SiteDomain(domain=CANONICAL, scheme='https', is_default=True))
    db.session.add(SiteDomain(domain=ONION, scheme='http', federates=False))
    db.session.commit()
    clear_domain_cache()
    cache.clear()
    yield ONION
    clear_domain_cache()


@pytest.fixture
def storefront_presentation_world(app, storefront_presentation_onion):
    site = db.session.get(Site, 1)
    site.private_instance = False
    local = make_instance(CANONICAL, software='piefed')
    founder = make_user(local, 'founder', local=True)
    author = make_user(local, 'author', local=True)
    author.ap_profile_id = f'https://{CANONICAL}/u/author'
    author.ap_public_url = f'https://{CANONICAL}/u/author'
    # their profiles show every post and comment, as these tests were written for (spec 2026-11-26-profile-privacy)
    author.profile_community_posts = author.profile_comments = True
    community = make_community('general')
    make_community_member(author, community)
    image = File(file_path=MEDIA, thumbnail_path=THUMB, source_url=f'https://{CANONICAL}/static/media/posts/ab/cd/'
                                                                    'abcdefghijklmno.jpg')
    db.session.add(image)
    db.session.commit()
    post = make_post(community, author, f'https://{CANONICAL}/post/1', title='a picture')
    post.type = POST_TYPE_IMAGE
    post.image_id = image.id
    post.url = image.source_url
    post.body_html = '<p>the body</p>'
    author.post_count = 1
    db.session.commit()
    return founder, author, community, post, image
