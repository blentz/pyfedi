"""A video uploaded to a PieFed video_channel shows on PeerTube as a native, playable video under the subscribed
channel; a like comes back as an upvote; an edit renames it; a delete removes it; a restore brings it back (spec
2026-10-14 §4.3, §6.1, §10 A11, A15)."""
import uuid

import httpx
import psycopg2
import pytest

from interop.clients import PieFed, wait_for
from interop.test_media_pipeline import _upload, alice_web  # noqa: F401  (the module-scoped fixture)

pytestmark = pytest.mark.interop

CHANNEL = 'vc_media@piefed.test'


def _subscription_accepted():
    with psycopg2.connect(host='peertube-db', dbname='peertube', user='peertube', password='peertube') as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT f.state FROM "actorFollow" f JOIN actor a ON a.id = f."targetActorId" '
                        'JOIN actor me ON me.id = f."actorId" '
                        "WHERE a.\"preferredUsername\" = 'vc_media' AND me.\"preferredUsername\" = 'carol'")
            row = cur.fetchone()
            return bool(row) and row[0] == 'accepted'


@pytest.fixture(scope='module')
def alice_api():
    return PieFed('alice', 'interop-alice-pw')


@pytest.fixture(scope='module')
def subscribed(peertube):
    peertube.unsubscribe(CHANNEL)    # `up` keeps the data: a repeated run subscribes afresh
    peertube.subscribe(CHANNEL)
    wait_for(_subscription_accepted, timeout=60)


def _upload_titled(alice_web):
    title = f'channel video {uuid.uuid4().hex[:8]}'
    url, post_id, media = _upload(alice_web, 'vc_media', 'video', 'video_h264.mp4', 'video/mp4', title=title)
    return title, url, post_id, media


def _gone(peertube, video_uuid):
    """Whether PeerTube no longer has the video. While PeerTube removes a video's rows, a GET can answer 500 (its
    query finds the video without its files); that is PeerTube's own race, not an answer, so keep waiting."""
    def gone():
        try:
            return peertube.video_or_none(video_uuid) is None
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 500:
                return False
            raise
    return gone


def _listed(peertube, title):
    def listed():
        return next((v for v in peertube.channel_videos(CHANNEL) if v['name'] == title), None)
    return listed


def test_a_video_channel_upload_plays_on_peertube_and_round_trips(peertube, alice_web, alice_api, subscribed):
    title, url, post_id, media = _upload_titled(alice_web)

    video = wait_for(_listed(peertube, title), timeout=60)
    detail = peertube.video(video['uuid'])
    assert detail['duration'] == media['duration_s']   # the ceil of ffprobe's duration (A13)
    assert detail['files'][0]['fileUrl'] == url
    # PeerTube's `thumbnailPath` holds only 16:9 thumbnails and the fixture's is square, so read the list it keeps
    # and fetch the first one: PeerTube downloads a remote thumbnail on first request (`/lazy-static/thumbnails/`)
    assert detail['thumbnails'], 'PeerTube stored no thumbnail'
    r = peertube.http.get(detail['thumbnails'][0]['fileUrl'])
    assert r.status_code == 200 and r.headers['content-type'] == 'image/jpeg'
    assert len(r.content) > 0

    # the author's own upvote is already counted at creation, so a like is proven by the count rising past it
    before = alice_api.get_post(post_id)['counts']['upvotes']
    peertube.rate(video['uuid'], 'like')
    wait_for(lambda: alice_api.get_post(post_id)['counts']['upvotes'] >= before + 1, timeout=30)

    alice_api.edit(post_id, title=title + ' renamed')
    wait_for(lambda: peertube.video(video['uuid'])['name'] == title + ' renamed', timeout=30)

    alice_api.delete(post_id)
    wait_for(_gone(peertube, video['uuid']), timeout=30)


def test_a_restored_video_is_created_again_on_peertube(peertube, alice_web, alice_api, subscribed):
    title, url, post_id, media = _upload_titled(alice_web)
    video = wait_for(_listed(peertube, title), timeout=60)

    alice_api.delete(post_id)
    wait_for(_gone(peertube, video['uuid']), timeout=30)

    alice_api.restore(post_id)
    wait_for(_listed(peertube, title), timeout=30)
