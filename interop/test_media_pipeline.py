"""Media uploads end to end (spec 2026-10-14 §5.1, §5.2, §5.4, §10 A13): alice, a moderator, uploads a video to a
video_channel community and an audio file to a podcast community through the web form; the background worker probes
each with ffprobe and publishes it; Caddy serves the file to a ranged request as 206 with CORS open. A file whose bytes
are not media makes no post."""
import os
import re
import ssl
import uuid

import httpx
import psycopg2
import pytest

from interop.clients import CA, PieFedWeb, form_errors, wait_for

pytestmark = pytest.mark.interop

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'tests', 'fixtures', 'media')
POST_STATUS_PUBLISHED = 1   # app/constants.py


@pytest.fixture(scope='module')
def alice_web():
    return PieFedWeb('alice', 'interop-alice-pw')


def _query(sql, *args):
    """Rows from the harness database: the media record lives in Post.extensions, which the alpha API does not
    expose."""
    with psycopg2.connect(host='piefed-db', dbname='piefed', user='piefed', password='piefed') as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()


def _upload_and_wait_ready(alice_web, community, kind, fixture, mime):
    return _upload(alice_web, community, kind, fixture, mime)[0]


def _upload(alice_web, community, kind, fixture, mime, title=None):
    """Upload, wait until the probe is done: (url, post id, media record)."""
    title = title or f'{kind} upload {uuid.uuid4().hex[:8]}'
    r = alice_web.submit_media(community, kind, title, os.path.join(FIXTURES, fixture), mime)
    assert r.status_code == 302, f'upload answered {r.status_code}, not a redirect: {form_errors(r.text)}'
    # the new post's page (its slug, app/models.py Post), not the community page a failed create redirects to
    created = re.search(r'/p(?:ost)?/(\d+)', r.headers['location'])
    assert created, f'upload redirected to {r.headers["location"]}, not to a post'
    post_id = int(created[1])

    def ready():
        status, media, url = _query("SELECT status, extensions -> 'media', url FROM post WHERE id = %s", post_id)[0]
        return (status, media, url) if media['status'] in ('ready', 'failed') else None
    status, media, url = wait_for(ready, timeout=60)
    assert media['status'] == 'ready', f'probe failed: {media.get("error")}'
    assert status == POST_STATUS_PUBLISHED
    assert media['mime'] == mime
    return url, post_id, media


def _ranged_get(url):
    with httpx.Client(verify=ssl.create_default_context(cafile=CA), timeout=10) as http:
        return http.get(url, headers={'Range': 'bytes=0-99'})


def _assert_partial(r):
    assert r.status_code == 206
    assert r.headers['content-range'].startswith('bytes 0-99/')
    assert r.headers['access-control-allow-origin'] == '*'
    assert len(r.content) == 100


def test_an_uploaded_video_is_probed_published_and_served_in_ranges(alice_web):
    url = _upload_and_wait_ready(alice_web, 'vc_media', 'video', 'video_h264.mp4', 'video/mp4')
    assert url.startswith('https://piefed.test/static/media/posts/')
    _assert_partial(_ranged_get(url))


def test_an_uploaded_audio_file_is_probed_published_and_served_in_ranges(alice_web):
    url = _upload_and_wait_ready(alice_web, 'pod_media', 'audio', 'audio.mp3', 'audio/mpeg')
    assert url.startswith('https://piefed.test/static/media/posts/')
    _assert_partial(_ranged_get(url))


def test_a_file_that_is_not_media_makes_no_post(alice_web):
    title = f'not media {uuid.uuid4().hex[:8]}'
    r = alice_web.submit_media('vc_media', 'video', title, os.path.join(FIXTURES, 'not_media.mp4'), 'video/mp4')
    assert r.status_code == 200   # the form again, with the refusal on the file field
    assert form_errors(r.text) == ['This is not a supported video file.']
    assert _query('SELECT id FROM post WHERE title = %s', title) == []
