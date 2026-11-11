"""Castopod's listeners: podcast players, which read a community's Podcasting 2.0 feed and nothing else.

AntennaPod is driven on the emulator: it fetches the feed itself, so it is given the feed URL and must list the
episode. Pocket Casts and Podcast Addict cannot be pointed at the harness: Pocket Casts' servers fetch the feed, not the
device (an address only the harness resolves never reaches them), and Podcast Addict is closed and Play-only. For them
the feed is checked for what any player needs: an item with an audio enclosure, its length and duration.

The harness instance is private (R219), so the feed URL carries alice's RSS token, as its community page's link does.
"""
import re
import ssl
import xml.etree.ElementTree as ET

import httpx
import pytest

from interop.apps import apks, steps
from interop.apps.conftest import ALICE
from interop.apps.matrix import inapplicable, load_matrix, record
from interop.clients import CA, PieFedWeb, form_errors, wait_for

MATRIX = load_matrix()
EPISODE = 'Interop apps episode'
FIXTURE = '/app/tests/fixtures/media/audio.mp3'
NS = {'itunes': 'http://www.itunes.com/dtds/podcast-1.0.dtd'}


def _items(http, feed_url):
    feed = ET.fromstring(http.get(feed_url).raise_for_status().content)
    return [item for item in feed.iter('item') if item.findtext('title') == EPISODE and item.find('enclosure') is not None]


@pytest.fixture(scope='module')
def episode():
    """The feed URL of pod_media, holding EPISODE: uploaded once, reused by later runs on the same stack."""
    web = PieFedWeb(*ALICE)
    link = re.search(r'/community/pod_media/feed\?token=([\w-]+)', web.http.get('/c/pod_media').text)
    assert link, 'the pod_media page has no feed link carrying the member token'
    feed_url = f'https://piefed.test/community/pod_media/feed?token={link[1]}'
    with httpx.Client(verify=ssl.create_default_context(cafile=CA), timeout=15) as http:
        if not _items(http, feed_url + '&t=check'):
            r = web.submit_media('pod_media', 'audio', EPISODE, FIXTURE, 'audio/mpeg')
            assert r.status_code == 302, f'upload answered {r.status_code}: {form_errors(r.text)}'
            # the feed is cached per URL and query string: a fresh query each look
            looks = iter(range(10_000))
            wait_for(lambda: _items(http, f'{feed_url}&t=wait{next(looks)}'), timeout=90, interval=2)
    return feed_url


RSS_ONLY = sorted(name for name, app in MATRIX.items() if app.get('rss'))


@pytest.mark.parametrize('name', RSS_ONLY, ids=RSS_ONLY)
def test_feed_for_a_player_that_cannot_be_driven(episode, name):
    for step in inapplicable(MATRIX[name]):
        record(name, step, 'inapplicable', MATRIX[name]['expect'][step]['reason'], version='rss')
    with httpx.Client(verify=ssl.create_default_context(cafile=CA), timeout=15) as http:
        items = _items(http, episode + f'&t={name}')
    if not items:
        record(name, 'view', 'failed', f'the feed has no item {EPISODE!r} with an enclosure')
        return
    enclosure = items[0].find('enclosure')
    problems = [f'enclosure {key} missing' for key in ('url', 'length', 'type') if not enclosure.get(key)]
    if not (enclosure.get('type') or '').startswith('audio/'):
        problems.append(f'enclosure type {enclosure.get("type")!r} is not audio')
    if not items[0].findtext('itunes:duration', namespaces=NS):
        problems.append('no itunes:duration')
    record(name, 'view', 'failed' if problems else 'passed',
           '; '.join(problems) or f'the feed carries {EPISODE!r} with an audio enclosure and a duration')


def test_antennapod_subscribes_and_lists_the_episode(episode, device, tmp_results):
    """AntennaPod fetches the feed on the device: Subscriptions > Add podcast > Add podcast by RSS address > the feed
    URL > Subscribe, and the podcast's page lists the episode."""
    name, app = 'antennapod-android', MATRIX['antennapod-android']
    with httpx.Client(timeout=300, follow_redirects=True) as http:
        apk, version = apks.fetch(app['apk'], http)
    device.reset_shared_state()   # as each Android app: it starts as if it were the first
    device.install(apk, app['package'])
    device.launch(app['package'])

    def view():
        device.tap_tab('Subscriptions', timeout=60)
        device.ui(description='Add podcast').click(timeout=30)
        device.tap('Add podcast by RSS address')
        field = device.ui(className='android.widget.EditText', instance=0)
        field.wait(timeout=30)
        device.type_into(field, episode)
        device.tap('Confirm', 'OK', exact=True)
        device.tap('Subscribe', exact=True, timeout=60)
        device.wait_text(EPISODE, timeout=60)
        return f'subscribed by RSS address; it lists {EPISODE!r}'

    try:
        steps.run(name, app, {'view': view}, version=version,
                  on_failure=lambda step: device.screenshot(tmp_results / f'{name}-{step}.png'))
    finally:
        device.stop(app['package'])
