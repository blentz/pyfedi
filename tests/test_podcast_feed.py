"""A local podcast community's RSS feed is a Podcasting 2.0 feed of its episodes (spec §4.5, §10 A17.6)."""
import uuid
import xml.etree.ElementTree as ET

import pytest
from feedgen.util import xml_elem

from app import cache, db
from app.constants import COMMUNITY_KIND_PODCAST, POST_STATUS_PROCESSING, POST_TYPE_LINK, POST_TYPE_ARTICLE, VISIBILITY_FOLLOWERS
from app.models import Community, Language, User
from app.rss_extras import PodcastIndexEntryExtension
from app.shared.media import with_episode, with_media
from app.shared.tasks import media as media_tasks
from app.shared.tasks.media import publish_ready_post
from tests.factories import make_community, make_file, make_post, make_site
from tests.podcast_fixtures import episode_post  # noqa: F401  (a fixture)

NS = {'itunes': 'http://www.itunes.com/dtds/podcast-1.0.dtd', 'podcast': 'https://podcastindex.org/namespace/1.0'}
SERVER = 'https://test.piefed.local'


@pytest.fixture
def pod(episode_post, app):
    make_site().private_instance = False
    community = db.session.get(Community, episode_post.community_id)
    alice = db.session.get(User, episode_post.user_id)
    alice.title = 'Alice'
    community.user_id = alice.id
    db.session.commit()
    return community


def channel_of(client, path='/community/pod/feed'):
    cache.clear()
    response = client.get(path)
    assert response.status_code == 200
    return ET.fromstring(response.data).find('channel')


def items_of(channel):
    return {item.findtext('title'): item for item in channel.findall('item')}


def another_episode(community, author, title, **media):
    post = make_post(community, author, f'https://test.piefed.local/post/{uuid.uuid4()}', title=title)
    post.type = POST_TYPE_LINK
    post.url = f'{SERVER}/static/media/posts/ab/cd/{uuid.uuid4().hex[:15]}.mp3'
    fields = {'status': 'ready', 'uuid': str(uuid.uuid4()), 'mime': 'audio/mpeg', 'bytes': 100, 'duration_s': 3,
              'codec': 'mp3', **media}
    with_media(post, **fields)
    db.session.commit()
    return post


def test_channel_has_podcast_tags(app, pod):
    pod.podcast_category = 'Technology'
    pod.icon_id = make_file(source_url='https://cdn.example/icon.png').id
    db.session.commit()

    channel = channel_of(app.test_client())

    assert channel.find('itunes:category', NS).get('text') == 'Technology'
    assert channel.findtext('itunes:author', namespaces=NS) == 'Alice'
    assert channel.findtext('itunes:explicit', namespaces=NS) == 'no'
    assert channel.findtext('podcast:locked', namespaces=NS) == 'no'
    assert channel.find('itunes:image', NS).get('href') == 'https://cdn.example/icon.png'


def test_channel_falls_back(app, pod):
    client = app.test_client()
    pod.user_id = None
    pod.nsfw = True
    db.session.commit()

    channel = channel_of(client)

    assert channel.find('itunes:category', NS) is None
    assert channel.find('itunes:image', NS).get('href').endswith('/static/images/apple-touch-icon.png')
    assert channel.findtext('itunes:author', namespaces=NS) == 'pod'
    assert channel.findtext('itunes:explicit', namespaces=NS) == 'yes'


def test_a_local_icon_is_served_from_this_server(app, pod):
    pod.icon_id = make_file(file_path='app/static/media/communities/ab/cd/icon.jpg').id
    db.session.commit()

    assert channel_of(app.test_client()).find('itunes:image', NS).get('href') == \
        f'{SERVER}/static/media/communities/ab/cd/icon.jpg'


@pytest.mark.parametrize('icon, banner, expected', [
    ('https://cdn.example/icon.webp', 'https://cdn.example/banner.png', 'https://cdn.example/banner.png'),
    ('https://cdn.example/icon.webp', 'https://cdn.example/banner.gif', '/static/images/apple-touch-icon.png'),
    ('https://cdn.example/icon.JPG', None, '/static/images/apple-touch-icon.png'),
    ('javascript:alert(1)//icon.png', None, '/static/images/apple-touch-icon.png'),
    ('https://cdn.example/icon.png?v=2', None, '/static/images/apple-touch-icon.png'),
    ('https://cdn.example/icon.jpg', 'https://cdn.example/banner.png', 'https://cdn.example/icon.jpg'),
    (None, 'https://cdn.example/banner.jpg', 'https://cdn.example/banner.jpg'),
])
def test_channel_image_is_the_first_that_feedgen_accepts(app, pod, icon, banner, expected):
    if icon:
        pod.icon_id = make_file(source_url=icon).id
    if banner:
        pod.image_id = make_file(source_url=banner).id
    db.session.commit()

    href = channel_of(app.test_client()).find('itunes:image', NS).get('href')

    assert href.endswith(expected)


def test_item_has_enclosure_and_tags(app, pod, episode_post):
    with_episode(episode_post, 4, 2, False)
    db.session.commit()

    item = items_of(channel_of(app.test_client()))['Episode one']

    enclosure = item.find('enclosure')
    assert (enclosure.get('url'), enclosure.get('length'), enclosure.get('type')) == \
        (episode_post.url, '4406', 'audio/mpeg')
    assert item.findtext('itunes:duration', namespaces=NS) == '2'
    assert item.findtext('itunes:episode', namespaces=NS) == '4'
    assert item.findtext('itunes:season', namespaces=NS) == '2'
    assert item.findtext('itunes:explicit', namespaces=NS) == 'no'
    assert item.findtext('podcast:guid', namespaces=NS) == episode_post.extensions['media']['uuid']


def test_item_without_episode_numbers_has_none(app, pod, episode_post):
    item = items_of(channel_of(app.test_client()))['Episode one']

    assert item.find('itunes:episode', NS) is None
    assert item.find('itunes:season', NS) is None


def test_explicit_episode_or_nsfw_post_is_explicit(app, pod, episode_post):
    alice = db.session.get(User, episode_post.user_id)
    other = another_episode(pod, alice, 'Rude one')
    other.nsfw = True
    with_episode(episode_post, None, None, True)
    db.session.commit()

    items = items_of(channel_of(app.test_client()))

    assert items['Episode one'].findtext('itunes:explicit', namespaces=NS) == 'yes'
    assert items['Rude one'].findtext('itunes:explicit', namespaces=NS) == 'yes'


def test_item_without_cover_has_no_itunes_image(app, pod, episode_post):
    item = items_of(channel_of(app.test_client()))['Episode one']

    assert item.find('itunes:image', NS) is None


def test_item_with_cover_has_its_image(app, pod, episode_post):
    episode_post.image_id = make_file(source_url=f'{SERVER}/static/media/posts/ab/cd/cover.jpg').id
    db.session.commit()

    item = items_of(channel_of(app.test_client()))['Episode one']

    assert item.find('itunes:image', NS).get('href') == episode_post.image.source_url


@pytest.mark.parametrize('cover', ['https://cdn.example/cover.webp', 'https://cdn.example/cover.JPG', 'javascript:alert(1)//cover.png',
                                   'https://cdn.example/co\x13ver.jpg'])
def test_item_cover_feedgen_would_refuse_is_left_out(app, pod, episode_post, cover):
    episode_post.image_id = make_file(source_url=cover).id
    db.session.commit()

    item = items_of(channel_of(app.test_client()))['Episode one']

    assert item.find('itunes:image', NS) is None


def test_feed_leaves_out_non_episodes(app, pod, episode_post):
    alice = db.session.get(User, episode_post.user_id)
    text = make_post(pod, alice, 'https://test.piefed.local/post/t', title='Text post')
    text.type = POST_TYPE_ARTICLE
    another_episode(pod, alice, 'Processing', status='processing')
    remote = make_post(pod, alice, 'https://test.piefed.local/post/r', title='Remote audio')
    remote.type = POST_TYPE_LINK
    remote.url = 'https://elsewhere.example/a.mp3'
    followers = another_episode(pod, alice, 'Followers only')
    followers.visibility = VISIBILITY_FOLLOWERS
    another_episode(pod, alice, 'Zero bytes', bytes=0)
    db.session.commit()

    assert list(items_of(channel_of(app.test_client()))) == ['Episode one']


def test_limit_counts_episodes_only(app, pod, episode_post):
    alice = db.session.get(User, episode_post.user_id)
    for number in range(25):
        make_post(pod, alice, f'https://test.piefed.local/post/n{number}', title=f'Note {number}').type = \
            POST_TYPE_ARTICLE
    db.session.commit()

    assert list(items_of(channel_of(app.test_client(), '/community/pod/feed?limit=20'))) == ['Episode one']


def test_invalid_xml_title_skips_only_that_item(app, pod, episode_post):
    another_episode(pod, db.session.get(User, episode_post.user_id), 'bad \x01 title')

    assert list(items_of(channel_of(app.test_client()))) == ['Episode one']


def test_language_is_the_communitys(app, pod):
    client = app.test_client()
    und, fr = Language(code='und', name='Undetermined'), Language(code='fr', name='French')
    db.session.add_all([und, fr])
    db.session.commit()
    pod.languages.append(und)
    db.session.commit()
    assert channel_of(client).findtext('language') == 'en'

    pod.languages.append(fr)
    db.session.commit()
    assert channel_of(client).findtext('language') == 'fr'


def test_other_kinds_unchanged(app, pod, episode_post):
    pod.kind = 'discussion'
    db.session.commit()

    channel = channel_of(app.test_client())

    assert not [element for element in channel.iter() if element.tag.startswith(('{%s}' % NS['itunes'], '{%s}' % NS['podcast']))]
    assert channel.find('item/enclosure') is None
    assert channel.findtext('language') == 'en'


@pytest.mark.parametrize('kind', ['discussion', COMMUNITY_KIND_PODCAST])
def test_self_link_is_a_url_that_resolves(app, pod, episode_post, kind):
    pod.kind = kind
    db.session.commit()
    client = app.test_client()

    href = channel_of(client).find('{http://www.w3.org/2005/Atom}link[@rel="self"]').get('href')

    assert href == f'{SERVER}/community/pod/feed'
    cache.clear()
    assert client.get(href.removeprefix(SERVER)).status_code == 200


def test_entry_extension_without_a_guid_writes_no_podcast_guid():
    entry = xml_elem('item', xml_elem('channel'))

    PodcastIndexEntryExtension().extend_rss(entry)

    assert list(entry) == []


@pytest.mark.parametrize('media', [{'mime': 'audio/\u0013'}, {'uuid': 'a\u0013b'}])
def test_a_control_character_in_the_media_record_does_not_fail_the_feed(app, pod, episode_post, media):
    episode_post.extensions = {**episode_post.extensions, 'media': {**episode_post.extensions['media'], **media}}
    db.session.commit()

    channel = channel_of(app.test_client())

    assert 'Episode one' not in items_of(channel)


def test_publishing_an_upload_changes_the_feed_etag(app, pod, episode_post, monkeypatch):
    monkeypatch.setattr(media_tasks, 'task_selector', lambda key, **kw: None)
    monkeypatch.setattr(media_tasks, 'notify_about_post', lambda post: None)
    alice = db.session.get(User, episode_post.user_id)
    upload = another_episode(pod, alice, 'Uploaded', status='processing')
    upload.status = POST_STATUS_PROCESSING
    db.session.commit()
    client = app.test_client()
    cache.clear()
    first = client.get('/community/pod/feed')
    etag = first.headers['ETag']
    assert 'Uploaded' not in items_of(ET.fromstring(first.data).find('channel'))

    upload.extensions = {**upload.extensions, 'media': {**upload.extensions['media'], 'status': 'ready'}}
    db.session.commit()
    publish_ready_post(upload)
    cache.clear()
    after = client.get('/community/pod/feed', headers={'If-None-Match': etag})

    assert after.status_code == 200
    assert 'Uploaded' in items_of(ET.fromstring(after.data).find('channel'))


def test_an_owner_name_that_is_not_xml_text_falls_back_to_the_community_title(app, pod):
    db.session.get(User, pod.user_id).title = 'Al\x13ice'
    db.session.commit()

    channel = channel_of(app.test_client())

    assert channel.findtext('itunes:author', namespaces=NS) == pod.title
