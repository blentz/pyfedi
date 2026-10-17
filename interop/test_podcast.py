"""Step 6 (spec §10 A16, A17): an audio episode uploaded to pod_media plays on Mastodon, a Mastodon reply comes back,
and the community's feed is a podcast feed."""
import re
import ssl
import xml.etree.ElementTree as ET

import httpx
import pytest

from interop.clients import CA, PieFedWeb, wait_for
from interop.test_media_pipeline import _upload

pytestmark = pytest.mark.interop
NS = {'itunes': 'http://www.itunes.com/dtds/podcast-1.0.dtd', 'podcast': 'https://podcastindex.org/namespace/1.0'}


@pytest.fixture(scope='module')
def alice_web():
    return PieFedWeb('alice', 'interop-alice-pw')


@pytest.fixture(scope='module')
def bob_follows_pod_media(mastodon):
    account = wait_for(lambda: mastodon.resolve_account('pod_media@piefed.test'))
    mastodon.follow(account['id'])
    wait_for(lambda: mastodon.relationship(account['id'])['following'])
    return account


def test_an_episode_plays_on_mastodon_and_a_reply_comes_back(alice_web, bob_follows_pod_media, mastodon, piefed):
    url, post_id, media = _upload(alice_web, 'pod_media', 'audio', 'audio.mp3', 'audio/mpeg')

    def boosted():
        for status in mastodon.home():
            shown = status.get('reblog') or status
            if any(m.get('remote_url') == url for m in shown.get('media_attachments', [])):
                return shown
        return None
    status = wait_for(boosted, timeout=60)
    audio = next(m for m in status['media_attachments'] if m['remote_url'] == url)
    assert audio['type'] == 'audio'   # Mastodon downloaded it and made a player, not an 'unknown' link
    mastodon.reply(status['id'], '@alice@piefed.test nice episode')
    wait_for(lambda: any('nice episode' in c['comment']['body'] for c in piefed.replies(post_id)), timeout=60)


def test_the_feed_is_a_podcast_feed(alice_web):
    url, post_id, media = _upload(alice_web, 'pod_media', 'audio', 'audio.mp3', 'audio/mpeg')
    # the harness instance is private (R219): a member's reader gets in with the token in the community page's feed link
    link = re.search(r'/community/pod_media/feed\?token=([\w-]+)', alice_web.http.get('/c/pod_media').text)
    assert link, 'the community page has no feed link carrying the member token'
    with httpx.Client(verify=ssl.create_default_context(cafile=CA), timeout=10) as http:
        # the feed is cached 600 s per URL and query string, so the post id makes a fresh key on a reused stack
        feed = ET.fromstring(http.get('https://piefed.test/community/pod_media/feed',
                                      params={'token': link[1], 'limit': 100, 't': post_id}).content)
    items = {item.find('enclosure').get('url'): item for item in feed.iter('item') if item.find('enclosure') is not None}
    item = items[url]
    assert item.find('enclosure').get('length') == str(media['bytes'])
    assert item.find('enclosure').get('type') == 'audio/mpeg'
    assert item.find('itunes:duration', NS).text == str(media['duration_s'])
    assert item.find('podcast:guid', NS).text == media['uuid']
    assert feed.find('channel/podcast:locked', NS).text == 'no'
