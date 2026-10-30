"""Loops both ways (spec 2026-10-30 §3.6): Loops lucy's videos reach PieFed with their thumbnail, alt text and title, and
through the relay a video of mia's, whom nobody here follows; comments cross both ways; a delete arrives. PieFed
alice's video-channel upload reaches lucy's Following feed as a Loops video, and a like and an edit follow it. A first
Follow of a Loops account, which Loops drops (U1), ends accepted. Every wait is bounded (spec 2026-10-14 §8.1)."""
import os
import re
import uuid

import psycopg2
import pytest

from interop.clients import Loops, PieFed, PieFedWeb, form_errors, tokens, wait_for

pytestmark = pytest.mark.interop

CLIP = os.path.join(os.path.dirname(__file__), '.loops_clip.mp4')   # run.sh makes it with Loops' ffmpeg
ARRIVAL = 120   # Loops transcodes before it federates; the U1 re-send waits LOOPS_FOLLOW_RESEND_SECONDS (10 here)


@pytest.fixture(scope='module')
def db():
    connection = psycopg2.connect('dbname=piefed user=piefed password=piefed host=piefed-db')
    connection.autocommit = True
    yield connection
    connection.close()


def query(db, sql, *args):
    with db.cursor() as cursor:
        cursor.execute(sql, args)
        return cursor.fetchall()


def follow_accepted(db, piefed, follower, handle):
    """`follower` follows the Loops account `handle` and waits for Loops' Accept: the re-send covers a first Follow
    Loops dropped (U1)."""
    target = piefed.resolve(handle)['person']
    target_id = target['person']['id'] if 'person' in target else target['id']
    piefed.follow_user(target_id)
    wait_for(lambda: query(db, 'SELECT f.is_accepted FROM user_follower f JOIN "user" u ON u.id = f.local_user_id '
                               'WHERE u.user_name = %s AND u.ap_id IS NULL AND f.remote_user_id = %s AND NOT f.is_inward',
                           follower, target_id) == [(True,)], timeout=ARRIVAL, interval=2)


@pytest.fixture(scope='module')
def alice_follows_lucy(db, piefed):
    follow_accepted(db, piefed, 'alice', '@lucy@loops.test')


def arrived(db, video_id):
    """The PieFed post a Loops video became: (id, type, url, title, image source, alt text, deleted, relay)."""
    rows = query(db, 'SELECT p.id, p.type, p.url, p.title, f.source_url, f.alt_text, p.deleted, p.relay_id FROM post p '
                     'LEFT JOIN file f ON f.id = p.image_id WHERE p.ap_id LIKE %s', f'%/video/{video_id}')
    return rows[0] if rows else None


@pytest.fixture(scope='module')
def lucy_video(db, loops, alice_follows_lucy):
    tag = uuid.uuid4().hex[:8]
    caption = f'Loops interop {tag} #interop'
    video_id = loops.upload(CLIP, caption, alt_text='a test clip')
    post = wait_for(lambda: arrived(db, video_id), timeout=ARRIVAL, interval=2)
    return video_id, caption, post


def test_a_loops_video_arrives_with_its_thumbnail_alt_text_and_title(lucy_video):
    _, caption, (_, post_type, url, title, image, alt_text, deleted, _) = lucy_video
    assert post_type == 4 and url.startswith('https://s3.test/') and url.endswith('.mp4')   # POST_TYPE_VIDEO
    assert image and image.startswith('https://s3.test/') and image.endswith('.jpg')       # I1
    assert alt_text == 'a test clip'                                                        # I2
    assert title == caption and not deleted                                                 # I3


def test_comments_cross_both_ways(db, piefed, loops, lucy_video):
    video_id, _, (post_id, *_) = lucy_video
    piefed.reply(post_id, 'A PieFed comment on a Loops video')
    ours = wait_for(lambda: next((c for c in loops.comments(video_id)
                                  if c['caption'] == 'A PieFed comment on a Loops video'), None), timeout=ARRIVAL)
    loops.comment(video_id, 'A Loops top-level comment')
    loops.comment(video_id, 'A Loops reply to the PieFed comment', parent_id=ours['id'])
    replies = wait_for(lambda: (lambda rows: rows if len(rows) == 3 else None)(
        query(db, 'SELECT id, parent_id, body FROM post_reply WHERE post_id = %s ORDER BY id', post_id)),
        timeout=ARRIVAL, interval=2)
    by_body = {body: (reply_id, parent) for reply_id, parent, body in replies}
    assert by_body['A Loops top-level comment'][1] is None
    assert by_body['A Loops reply to the PieFed comment'][1] == by_body['A PieFed comment on a Loops video'][0]


def test_a_loops_delete_arrives(db, loops, alice_follows_lucy):
    video_id = loops.upload(CLIP, f'Loops delete {uuid.uuid4().hex[:8]}')
    wait_for(lambda: arrived(db, video_id), timeout=ARRIVAL, interval=2)
    loops.delete(video_id)
    wait_for(lambda: arrived(db, video_id)[6], timeout=ARRIVAL, interval=2)


def test_a_video_nobody_here_follows_arrives_through_the_relay(db):
    mia = Loops(tokens()['loops_mia'])
    video_id = mia.upload(CLIP, f'Through the relay {uuid.uuid4().hex[:8]}')
    post = wait_for(lambda: arrived(db, video_id), timeout=ARRIVAL, interval=2)
    assert post[1] == 4 and post[7] is not None   # a video post, stored with the relay it came through


@pytest.fixture(scope='module')
def alice_video(db, loops):
    """alice's upload to vc_media, once lucy follows her: (PieFed post id, title, the Loops video lucy sees)."""
    alice = loops.resolve('@alice@piefed.test')
    loops.follow(alice['id'])
    wait_for(lambda: query(db, 'SELECT 1 FROM user_follower f JOIN "user" u ON u.id = f.remote_user_id '
                               "WHERE u.ap_profile_id LIKE 'https://loops.test/%%' AND f.is_inward"),
             timeout=ARRIVAL, interval=2)
    title = f'Alice for Loops {uuid.uuid4().hex[:8]}'
    response = PieFedWeb('alice', 'interop-alice-pw').submit_media('vc_media', 'video', title, CLIP, 'video/mp4')
    assert response.status_code == 302, form_errors(response.text)
    post_id = int(re.search(r'/p(?:ost)?/(\d+)', response.headers['location'])[1])
    video = wait_for(lambda: next((v for v in loops.following_feed() if title in (v.get('caption') or '')), None),
                     timeout=ARRIVAL, interval=2)
    return post_id, title, video


def test_alices_video_reaches_lucys_following_feed_as_a_loops_video(alice_video):
    _, title, video = alice_video
    assert video['caption'].startswith(title)    # the title, folded into content (L2)
    assert video['media']['src_url'].startswith('https://s3.test/')   # Loops downloaded and transcoded our mp4


def test_a_loops_like_of_alices_video_arrives(db, loops, alice_video):
    post_id, _, video = alice_video
    loops.like(video['id'])
    wait_for(lambda: query(db, 'SELECT 1 FROM post_vote WHERE post_id = %s AND effect > 0', post_id),
             timeout=ARRIVAL, interval=2)


def test_an_edit_of_alices_video_updates_it_on_loops(db, piefed, loops, alice_video):
    post_id, title, video = alice_video
    piefed.edit(post_id, title=title + ' edited')
    wait_for(lambda: (loops.video(video['id']).get('caption') or '').startswith(title + ' edited'),
             timeout=ARRIVAL, interval=2)


def test_a_first_follow_loops_drops_ends_accepted(db):
    """dora is followed by no one and follows no one on Loops: on a fresh stack her first Follow is the first activity
    Loops sees from her, which it drops (U1). It is accepted once PieFed re-sends it."""
    follow_accepted(db, PieFed('dora', 'interop-dora-pw'), 'dora', '@mia@loops.test')
