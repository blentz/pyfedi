"""Seeding Pixelfed content into microblogs (spec 2026-10-29 §4): with one Pixelfed person per server set on the admin
page, Sync now has the instance actor follow erin; Pixelfed accepts, a post erin made before the Follow is fetched
from her Atom feed, and her next post is pushed into microblogs. No local user follows her."""
import io
import uuid

import psycopg2
import pytest
from PIL import Image

from interop.clients import PieFedWeb, wait_for

pytestmark = pytest.mark.interop

ERIN = 'https://pixelfed.test/users/erin'
SEED_WAIT = 150   # Celery runs the reconcile, then Pixelfed's queues answer the Follow and fan the post out


def picture(colour):
    buffer = io.BytesIO()
    Image.new('RGB', (320, 240), colour).save(buffer, 'JPEG')
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


def execute(db, sql, *args):
    with db.cursor() as cursor:
        cursor.execute(sql, args)


def stored(db, uri):
    def find():
        rows = query(db, 'select c.name from post p join community c on c.id = p.community_id where p.ap_id = %s', uri)
        return rows[0][0] if rows else None
    find.__name__ = f'PieFed has {uri}'
    return wait_for(find, timeout=SEED_WAIT, interval=3)


def erins_followers(pixelfed):
    erin = pixelfed.http.get('/api/v1/accounts/verify_credentials').raise_for_status().json()
    followers = pixelfed.http.get(f"/api/v1/accounts/{erin['id']}/followers").raise_for_status().json()
    return {follower['acct'] for follower in followers}


def test_the_instance_actor_seeds_erins_posts_into_microblogs(piefed, pixelfed, db):
    """test_pixelfed.py has alice follow erin: she unfollows first, so only the instance actor's Follow delivers."""
    marker = uuid.uuid4().hex[:6]
    erin_id = piefed.resolve('@erin@pixelfed.test')['person']['person']['id']
    piefed.unfollow_user(erin_id)
    wait_for(lambda: 'alice@piefed.test' not in erins_followers(pixelfed), timeout=90, interval=3)
    # PieFed refuses a post from a remote account under a day old once it has more than 3 (app.utils.can_create_post),
    # and test_pixelfed.py has stored more: age erin, as conformance/reset_state.py ages bob
    execute(db, "update \"user\" set created = now() - interval '30 days' where ap_profile_id = %s", ERIN)
    before = pixelfed.post(f'Before {marker}', [pixelfed.upload(picture((10, 10, 120)), 'b.jpg', f'b {marker}')])
    execute(db, "insert into discovery_entry (kind, platform, actor_url, name, host, followers, nsfw, source, first_seen, "
              "last_seen) values ('person', 'pixelfed', %s, 'erin', 'pixelfed.test', 1, false, 'interop', now(), now()) "
              "on conflict (actor_url) do nothing", ERIN)

    web = PieFedWeb('alice', 'interop-alice-pw')
    page = '/admin/federation/discovery'
    form = web._defaults(page)
    form.update(sync_per_host='0', sync_people_per_host='1', sync_save='Save')
    assert web.http.post(page, data=form).status_code == 302
    assert web.http.post(page, data={'csrf_token': form['csrf_token'], 'sync_now': 'Sync now'}).status_code == 302

    wait_for(lambda: query(db, 'select follow_state from discovery_person_sync where follow_target = %s', ERIN)
             == [('accepted',)], timeout=SEED_WAIT, interval=3)
    assert stored(db, before['uri']) == 'microblogs'   # from the Atom feed: Pixelfed sends nothing made before a Follow

    after = pixelfed.post(f'After {marker}', [pixelfed.upload(picture((10, 120, 10)), 'a.jpg', f'a {marker}')])
    assert stored(db, after['uri']) == 'microblogs'    # pushed to the instance actor
    assert erins_followers(pixelfed) == {'piefed.test@piefed.test'}   # the instance actor alone
    assert query(db, 'select count(*) from user_follower uf join "user" u on u.id = uf.remote_user_id '
                     'where u.ap_profile_id = %s and uf.is_inward = false', ERIN) == [(0,)]
