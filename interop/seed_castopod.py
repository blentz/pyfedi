# Seed the harness Castopod: podcast `dave` with one published episode. Idempotent. Run inside piefed-web by run.sh
# after `spark install:*` (it has python, httpx, Pillow, the harness CA and tests/fixtures/media). The podcast has no
# API, so it goes through the admin forms; the episode goes through the REST API (restapi_* in compose.interop.yaml).
# Prints one JSON line {"castopod_podcast": <actor URL>, "castopod_episode": <episode URL>}.
import io
import json
import re

import httpx
from PIL import Image

BASE = 'https://castopod.test'
web = httpx.Client(base_url=BASE, verify='/tmp/harness-ca.crt', timeout=30, follow_redirects=True)
api = httpx.Client(base_url=f'{BASE}/api/rest/v1', verify='/tmp/harness-ca.crt', timeout=30,
                   auth=('interop', 'interop-api-pw'))


def csrf(path):
    return re.search(r'name="csrf_test_name" value="([^"]+)"', web.get(path).raise_for_status().text).group(1)


def podcast_id():
    for p in api.get('/podcasts').raise_for_status().json():
        if p['handle'] == 'dave':
            return p['id']
    return None


web.post('/cp-auth/login', data={'csrf_test_name': csrf('/cp-auth/login'), 'email': 'admin@castopod.test',
                                 'password': 'interop-admin-pw-1'}).raise_for_status()
if podcast_id() is None:
    cover, banner = io.BytesIO(), io.BytesIO()
    Image.new('RGB', (1400, 1400), (40, 90, 160)).save(cover, 'PNG')
    Image.new('RGB', (1500, 500), (40, 90, 160)).save(banner, 'PNG')   # optional, but is_image fails on no file
    form = {'csrf_test_name': csrf('/cp-admin/podcasts/new'), 'title': 'Dave talks', 'handle': 'dave',
            'description': 'A harness podcast.', 'language': 'en', 'category': '1', 'parental_advisory': 'clean',
            'owner_name': 'Dave', 'owner_email': 'dave@castopod.test', 'type': 'episodic', 'medium': 'podcast',
            # a browser sends the empty fields too; a missing one is null and Podcast's typed setters throw
            'location_name': '', 'custom_rss': '', 'verify_txt': '', 'publisher': '', 'copyright': ''}
    web.post('/cp-admin/podcasts/new', data=form,
             files={'cover': ('cover.png', cover.getvalue(), 'image/png'),
                    'banner': ('banner.png', banner.getvalue(), 'image/png')}).raise_for_status()
pid = podcast_id()
podcast = api.get(f'/podcasts/{pid}').raise_for_status().json()
if not podcast.get('published_at'):
    path = f'/cp-admin/podcasts/{pid}/publish'
    web.post(path, data={'csrf_test_name': csrf(path), 'publication_method': 'now',
                         'message': 'Dave talks is live.'}).raise_for_status()
episodes = [e for e in api.get('/episodes').raise_for_status().json() if e['podcast_id'] == pid]
if not episodes:
    with open('/app/tests/fixtures/media/audio.mp3', 'rb') as f:
        audio = f.read()
    created = api.post('/episodes', data={'created_by': '1', 'updated_by': '1', 'title': 'First episode',
                                          'slug': 'first-episode', 'podcast_id': str(pid),
                                          'description': 'The first episode.', 'type': 'full',
                                          'parental_advisory': 'clean'},
                       files={'audio_file': ('audio.mp3', audio, 'audio/mpeg')}).raise_for_status().json()
    api.post(f"/episodes/{created['id']}/publish",
             data={'publication_method': 'now', 'created_by': '1',
                   'message': 'Episode one is out.'}).raise_for_status()
    episodes = [api.get(f"/episodes/{created['id']}").raise_for_status().json()]
print(json.dumps({'castopod_podcast': f'{BASE}/@dave', 'castopod_episode': episodes[0]['guid']}))
