"""Interop D24: the Videos & Podcasts tab shows only PeerTube and Castopod posts, with All's guards."""
import pytest

from app import db
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
    assert '/home/new/media' in html and 'Videos & Podcasts' in html
