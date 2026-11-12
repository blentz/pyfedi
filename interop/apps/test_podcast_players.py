"""Castopod's listeners: podcast players, which read a community's Podcasting 2.0 feed and nothing else.

AntennaPod is driven on the emulator: it logs in to PieFed's gpodder sync with an app password of alice's (plan W4),
fetches the feed itself, given its URL, lists the episode, and syncs the subscription to PieFed. Pocket Casts and Podcast Addict cannot be pointed at the harness: Pocket Casts' servers fetch the feed, not the
device (an address only the harness resolves never reaches them), and Podcast Addict is closed and Play-only. For them
the feed is checked for what any player needs: an item with an audio enclosure, its length and duration.

The harness instance is private (R219), so the feed URL carries alice's RSS token, as its community page's link does.
"""
import base64
import ssl

import httpx
import pytest

from interop.apps import apks, steps
from interop.apps.conftest import ALICE, EPISODE, episode_items as _items
from interop.apps.matrix import inapplicable, load_matrix, record
from interop.clients import CA, PieFedWeb, wait_for

MATRIX = load_matrix()
NS = {'itunes': 'http://www.itunes.com/dtds/podcast-1.0.dtd'}



@pytest.fixture
def episode(baseline, world):
    """The feed URL of pod_media, which holds EPISODE: uploaded when the session made its world (conftest.py)."""
    return world['podcast_feed']


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


def _synced(password) -> list[str]:
    """What PieFed's gpodder sync holds as alice's subscriptions, asked as any device of hers asks."""
    auth = base64.b64encode(f'{ALICE[0]}:{password}'.encode()).decode()
    with httpx.Client(verify=ssl.create_default_context(cafile=CA), timeout=15) as http:
        r = http.get(f'https://piefed.test/api/2/subscriptions/{ALICE[0]}/harness.json?since=0',
                     headers={'Authorization': f'Basic {auth}'})
    return r.raise_for_status().json()['add']


def test_antennapod_syncs_with_piefed_and_lists_the_episode(episode, device, tmp_results):
    """AntennaPod logs in to PieFed's gpodder sync (Settings > Synchronization > gpodder.net, with alice's app
    password), subscribes by the feed's address (Subscriptions > Add podcast > Add podcast by RSS address > Subscribe),
    lists the episode, and syncs the subscription: PieFed holds the feed in alice's list."""
    name, app = 'antennapod-android', MATRIX['antennapod-android']
    package = app['package']
    with httpx.Client(timeout=300, follow_redirects=True) as http:
        apk, version = apks.fetch(app['apk'], http)
    device.install(apk, package)
    device.launch(package)
    password = PieFedWeb(*ALICE).app_password('AntennaPod')

    def by_id(name):
        return device.ui(resourceId=f'{package}:id/{name}')

    def open_sync_settings():
        device.launch(package)
        if not device.ui(text='Settings').wait(timeout=10):
            device.tap('More', exact=True, timeout=30)
        device.tap('Settings', exact=True, timeout=30)
        device.tap('Synchronization', exact=True, timeout=30)

    def login():
        open_sync_settings()
        device.tap('Choose synchronization provider', exact=True, timeout=30)
        device.tap('gpodder.net', timeout=30)
        device.type_into(by_id('serverUrlText'), 'https://piefed.test')
        by_id('chooseHostButton').click(timeout=30)
        device.type_into(by_id('etxtUsername'), ALICE[0])
        device.type_into(by_id('etxtPassword'), password)
        device.hide_keyboard()
        for _ in range(3):   # a tap as the keyboard closes is lost: tap until its device page shows
            by_id('butLogin').click(timeout=30)
            if by_id('createDeviceButton').wait(timeout=20):
                break
        by_id('createDeviceButton').click(timeout=60)   # a new device, named as AntennaPod names it
        by_id('butSyncNow').click(timeout=60)
        return 'logged in to PieFed\'s gpodder sync as alice, with an app password'

    def view():
        device.launch(package)   # afresh, out of the settings the login left open
        device.tap_tab('Subscriptions', timeout=60)
        device.ui(description='Add podcast').click(timeout=30)
        device.tap('Add podcast by RSS address')
        field = device.ui(className='android.widget.EditText', instance=0)
        field.wait(timeout=30)
        device.type_into(field, episode)
        device.tap('Confirm', 'OK', exact=True)
        device.tap('Subscribe', exact=True, timeout=60)
        device.wait_text(EPISODE, timeout=60)
        try:   # it syncs a new subscription by itself; Synchronize now if it has not yet
            wait_for(lambda: episode in _synced(password), timeout=60, interval=3)
        except AssertionError:
            open_sync_settings()
            device.tap('Synchronize now', exact=True, timeout=30)
            wait_for(lambda: episode in _synced(password), timeout=90, interval=3)
        return f'subscribed by RSS address; it lists {EPISODE!r}, and PieFed holds the feed in alice\'s gpodder list'

    try:
        steps.run(name, app, {'login': login, 'view': view}, version=version,
                  on_failure=lambda step: device.screenshot(tmp_results / f'{name}-{step}.png'))
    finally:
        device.stop(app['package'])
