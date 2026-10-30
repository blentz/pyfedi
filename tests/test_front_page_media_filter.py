"""Interop D24: the Media tab shows only PeerTube and Castopod posts, with All's guards."""
import pytest

from app import db
from app.constants import COMMUNITY_KIND_MICROBLOGS, POST_TYPE_IMAGE
from tests.factories import make_community, make_community_member, make_instance, make_post, make_user
from tests.test_front_page_view_filters import _titles, env  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def media(env):
    tube = make_instance('tube.example', software='peertube')
    for name, private, show_all, low_quality in (('tube_open', False, True, False), ('tube_private', True, True, False),
                                                 ('tube_silenced', False, False, False), ('tube_low', False, True, True)):
        community = make_community(name, host='tube.example')
        community.instance_id = tube.id
        community.private, community.show_all, community.low_quality = private, show_all, low_quality
        make_post(community, env.author, f'https://tube.example/{name}/1', title=f'post in {name}')
        env.communities[name] = community
    db.session.commit()
    make_community_member(env.reader, env.communities['tube_private'])
    db.session.commit()
    return env


def test_anonymous_readers_get_public_media_posts_only(media):
    assert _titles(media, 'media') == ['post in tube_open']


def test_a_member_also_gets_their_private_media_community(media):
    assert _titles(media, 'media', user=media.reader) == ['post in tube_low', 'post in tube_open',
                                                         'post in tube_private']


def test_a_reader_hiding_low_quality_loses_it(media):
    media.reader.hide_low_quality = True
    db.session.commit()
    assert 'post in tube_low' not in _titles(media, 'media', user=media.reader)


def test_the_nav_offers_the_tab(media):
    html = media.app.test_client().get('/home/new/media').get_data(as_text=True)
    assert "/home/new/media" in html and ">Media<" in html.replace(" ", "").replace("\n", "") and "Videos & Podcasts<" not in html


# --- galleries (spec 2026-10-30 G2): image posts join the tab, the microblog gate lifted for them alone ---

@pytest.fixture
def galleries(media):
    microblogs = make_community('microblogs', kind=COMMUNITY_KIND_MICROBLOGS)
    microblogs.show_all = microblogs.show_popular = False   # as on hell.cloud: the relays' 38,000 posts a day
    media.communities['local_private'].show_all = True       # in All, so only membership keeps it from anyone
    db.session.commit()
    for name, community, image, microblog in (
            ('pixelfed photo', microblogs, True, True), ('toot', microblogs, False, True),
            ('photo in local_open', media.communities['local_open'], True, False),
            ('photo in local_hidden', media.communities['local_hidden'], True, False),
            ('photo in local_private', media.communities['local_private'], True, False)):
        post = make_post(community, media.author, f'https://test.example/{name}', title=name, microblog=microblog)
        post.title = name
        if image:
            post.type = POST_TYPE_IMAGE
    db.session.commit()
    return media


def test_the_tab_lists_image_posts_from_all_and_microblogs(galleries):
    assert _titles(galleries, 'media') == ['photo in local_open', 'pixelfed photo', 'post in tube_open']


def test_a_member_also_gets_images_from_their_private_community(galleries):
    assert 'photo in local_private' in _titles(galleries, 'media', user=galleries.reader)


@pytest.fixture
def low_quality(galleries):
    galleries.communities['local_open'].low_quality = True
    db.session.commit()
    return galleries


# one reader per test: within a test, a signed-in _titles after an anonymous one still renders for the anonymous reader
def test_low_quality_hides_gallery_posts_from_anonymous_readers(low_quality):
    assert 'photo in local_open' not in _titles(low_quality, 'media')


def test_a_reader_sees_low_quality_gallery_posts_unless_hiding_them(low_quality):
    assert 'photo in local_open' in _titles(low_quality, 'media', user=low_quality.reader)


def test_a_reader_hiding_low_quality_loses_gallery_posts(low_quality):
    low_quality.reader.hide_low_quality = True
    db.session.commit()
    assert 'photo in local_open' not in _titles(low_quality, 'media', user=low_quality.reader)


def test_the_gate_still_holds_everywhere_else(galleries):
    assert 'pixelfed photo' not in _titles(galleries, 'all') and 'pixelfed photo' not in _titles(galleries, 'popular')
