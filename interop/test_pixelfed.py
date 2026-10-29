"""Pixelfed both ways (spec 2026-10-28 §3.6): PieFed alice's image posts and albums show natively in Pixelfed erin's
home feed, and erin's albums come into PieFed whole. Replies, likes, boosts, edits and deletes cross in both
directions. Pixelfed fans a post out to home feeds from its own queues, which can take over 45 s, so those waits are
150 s; every wait is bounded (spec §8.1)."""
import io
import uuid

import psycopg2
import pytest
from PIL import Image

from interop.clients import wait_for

pytestmark = pytest.mark.interop

FEED_WAIT = 150


def picture(colour, fmt='JPEG'):
    buffer = io.BytesIO()
    Image.new('RGB', (320, 240), colour).save(buffer, fmt)
    return buffer.getvalue()


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


@pytest.fixture(scope='module')
def erin_follows_alice(pixelfed):
    account = wait_for(lambda: pixelfed.resolve_account('alice@piefed.test'), timeout=60)
    pixelfed.follow(account['id'])
    wait_for(lambda: pixelfed.relationship(account['id'])['following'], timeout=90, interval=2)
    return account


@pytest.fixture(scope='module')
def alice_follows_erin(piefed, db):
    erin = piefed.resolve('@erin@pixelfed.test')['person']['person']
    piefed.follow_user(erin['id'])
    return erin


def upload(piefed, colour, fmt='JPEG'):
    ext = {'JPEG': 'jpg'}.get(fmt, fmt.lower())
    return piefed.http.post('/upload/image', files={'file': (f'p.{ext}', picture(colour, fmt), f'image/{ext}')}) \
        .raise_for_status().json()['url']


def in_home(pixelfed, ap_id):
    def find():
        return next((status for status in pixelfed.home() if status['uri'] == ap_id), None)
    find.__name__ = f'erin\'s home has {ap_id}'
    return wait_for(find, timeout=FEED_WAIT, interval=3)


def pixelfed_media(status):
    return [(media['url'], media['description']) for media in status['media_attachments']]


# --- PieFed to Pixelfed ---

def test_an_album_shows_in_the_pixelfed_home_feed_whole(piefed, pixelfed, erin_follows_alice):
    """Every picture, in order, with its alt text; the WebP as the JPEG Pixelfed takes (R3, R5)."""
    marker = uuid.uuid4().hex[:6]
    urls = [upload(piefed, (200, 0, 0)), upload(piefed, (0, 200, 0), 'WEBP'), upload(piefed, (0, 0, 200), 'PNG')]
    post = piefed.post(f'Album {marker}', f'Album {marker}', piefed.microblogs_id(), url=urls[0], alt_text=f'red {marker}',
                       gallery=[{'url': urls[1], 'alt_text': f'green {marker}'},
                                {'url': urls[2], 'alt_text': f'blue {marker}'}])

    media = pixelfed_media(in_home(pixelfed, post['post']['ap_id']))

    assert [alt for _, alt in media] == [f'red {marker}', f'green {marker}', f'blue {marker}']
    assert media[0][0] == urls[0] and media[2][0] == urls[2]
    assert '/media/jpeg/' in media[1][0] and media[1][0].endswith('.jpg')


def test_an_edit_and_a_delete_reach_pixelfed(piefed, pixelfed, erin_follows_alice):
    """The delivery reaches Pixelfed's shared inbox (P1) and the Delete names a Tombstone (P2)."""
    marker = uuid.uuid4().hex[:6]
    post = piefed.post(f'Edit {marker}', f'Before {marker}', piefed.microblogs_id(), url=upload(piefed, (9, 99, 9)),
                       alt_text=f'alt {marker}')
    status = in_home(pixelfed, post['post']['ap_id'])

    piefed.edit(post['post']['id'], title=f'Edit {marker}', body=f'After {marker}')
    wait_for(lambda: f'After {marker}' in (pixelfed.status(status['id']) or {}).get('content', ''), timeout=90, interval=2)

    piefed.delete(post['post']['id'])
    wait_for(lambda: pixelfed.status(status['id']) is None, timeout=90, interval=2)


def test_a_reply_a_like_and_a_boost_come_back(piefed, pixelfed, erin_follows_alice, alice_follows_erin, db):
    marker = uuid.uuid4().hex[:6]
    post = piefed.post(f'Talk {marker}', f'Talk {marker}', piefed.microblogs_id(), url=upload(piefed, (99, 9, 9)))
    post_id = post['post']['id']
    status = in_home(pixelfed, post['post']['ap_id'])

    pixelfed.reply(status['id'], f'@alice@piefed.test reply {marker}')
    pixelfed.favourite(status['id'])
    pixelfed.reblog(status['id'])

    wait_for(lambda: any(marker in reply['comment']['body'] for reply in piefed.replies(post_id)), timeout=90, interval=2)
    wait_for(lambda: piefed.get_post(post_id)['counts']['upvotes'] >= 2, timeout=90, interval=2)   # alice's own and erin's
    wait_for(lambda: query(db, 'select count(*) from post_boost where post_id = %s', post_id)[0][0] == 1,
             timeout=90, interval=2)


# --- Pixelfed to PieFed ---

def stored(db, uri):
    def find():
        rows = query(db, 'select id from post where ap_id = %s', uri)
        return rows[0][0] if rows else None
    find.__name__ = f'PieFed has {uri}'
    return wait_for(find, timeout=FEED_WAIT, interval=3)


def test_a_pixelfed_album_arrives_whole(pixelfed, alice_follows_erin, db):
    marker = uuid.uuid4().hex[:6]
    media = [pixelfed.upload(picture(colour), f'{n}.jpg', f'alt{n} {marker}')
             for n, colour in enumerate([(1, 1, 200), (1, 200, 1), (200, 1, 1)])]
    status = pixelfed.post(f'Album {marker} #interop', media)

    post_id = stored(db, status['uri'])

    alts = query(db, 'select f.alt_text from post p join file f on f.id = p.image_id where p.id = %s', post_id) + \
        query(db, 'select f.alt_text from post_file pf join file f on f.id = pf.file_id '
                  'where pf.post_id = %s and pf.weight >= 0 order by pf.weight', post_id)
    assert [alt for (alt,) in alts] == [f'alt0 {marker}', f'alt1 {marker}', f'alt2 {marker}']


def test_a_sensitive_pixelfed_post_arrives_behind_its_warning(pixelfed, alice_follows_erin, db):
    """R4: the warning is the title, the caption is collapsed behind it, the image blurred (nsfw)."""
    marker = uuid.uuid4().hex[:6]
    status = pixelfed.post(f'Caption {marker}', [pixelfed.upload(picture((5, 5, 5)), 's.jpg')],
                           sensitive=True, spoiler_text=f'cw {marker}')

    post_id = stored(db, status['uri'])

    assert query(db, 'select title, nsfw, content_warning from post where id = %s', post_id) == \
        [(f'cw {marker}', True, f'cw {marker}')]


def test_a_pixelfed_edit_and_delete_reach_piefed(pixelfed, alice_follows_erin, db):
    marker = uuid.uuid4().hex[:6]
    status = pixelfed.post(f'Before {marker}', [pixelfed.upload(picture((70, 70, 70)), 'e.jpg')])
    post_id = stored(db, status['uri'])

    pixelfed.edit(status['id'], f'After {marker}')
    wait_for(lambda: f'After {marker}' in (query(db, 'select body from post where id = %s', post_id)[0][0] or ''),
             timeout=90, interval=2)

    pixelfed.delete(status['id'])
    wait_for(lambda: query(db, 'select deleted from post where id = %s', post_id) in ([], [(True,)]),
             timeout=90, interval=2)
