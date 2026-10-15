"""Spec 2026-10-14 §1 row 1: a post in PieFed's local microblogs shows on Mastodon as alice's own status, and
Mastodon's replies, favourites and boosts land in its PieFed thread."""
import uuid

import psycopg2
import pytest

from interop.clients import wait_for

pytestmark = pytest.mark.interop


def _in_home(mastodon, ap_id):
    def find():
        return next((s for s in mastodon.home() if s['uri'] == ap_id), None)
    find.__name__ = f'home has {ap_id}'
    return wait_for(find)


@pytest.fixture
def note(piefed, mastodon, bob_follows_alice):
    marker = uuid.uuid4().hex[:8]
    post = piefed.post(f'Title {marker}', f'Body {marker} #interop', piefed.microblogs_id())
    ap_id = post['post']['ap_id']
    return post, _in_home(mastodon, ap_id), marker


def test_status_reaches_home_timeline_as_alices_own(note):
    post, status, marker = note
    assert status['account']['acct'] == 'alice@piefed.test'
    assert status['reblog'] is None


def test_status_text_has_title_once_and_body(note):
    post, status, marker = note
    assert status['content'].count(f'Title {marker}') == 1
    assert f'Body {marker}' in status['content']


def test_status_is_not_a_link_card_and_hashtag_is_live(note, mastodon, piefed):
    post, status, marker = note
    assert status['card'] is None
    # PieFed hashtags are the post's tags, not words parsed out of the body, and POST /post takes no tags: set them
    # by an edit, which pushes an Update carrying the Hashtag.
    piefed.edit(post['post']['id'], tags='interop')
    wait_for(lambda: any(tag['name'] == 'interop' for tag in (mastodon.status(status['id']) or {}).get('tags', [])))


def test_fetch_by_url_resolves_to_the_same_status(note, mastodon):
    post, status, marker = note
    found = mastodon.search_status(post['post']['ap_id'])
    assert found['id'] == status['id'] and found['content'] == status['content']


def test_mastodon_reply_lands_in_piefed_thread(note, mastodon, piefed):
    post, status, marker = note
    mastodon.reply(status['id'], f'@alice@piefed.test reply {marker}')
    wait_for(lambda: any(f'reply {marker}' in c['comment']['body'] for c in piefed.replies(post['post']['id'])))


def test_mastodon_favourite_is_an_upvote(note, mastodon, piefed):
    post, status, marker = note
    before = piefed.get_post(post['post']['id'])['counts']['upvotes']
    mastodon.favourite(status['id'])
    wait_for(lambda: piefed.get_post(post['post']['id'])['counts']['upvotes'] > before)


def _boost_count(post_id):
    """PieFed records an inbound Announce of a local post as a post_boost row (app/models.py PostBoost); the alpha
    API does not expose it, so the harness reads the harness database directly."""
    with psycopg2.connect(host='piefed-db', dbname='piefed', user='piefed', password='piefed') as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM post_boost WHERE post_id = %s', (post_id,))
            return cur.fetchone()[0]


def test_mastodon_boost_reaches_piefed(note, mastodon, piefed):
    post, status, marker = note
    assert _boost_count(post['post']['id']) == 0
    mastodon.reblog(status['id'])
    wait_for(lambda: _boost_count(post['post']['id']) == 1)


def test_edit_updates_the_status(note, mastodon, piefed):
    post, status, marker = note
    piefed.edit(post['post']['id'], body=f'Edited {marker}')
    wait_for(lambda: f'Edited {marker}' in (mastodon.status(status['id']) or {}).get('content', ''))


def test_delete_removes_the_status(note, mastodon, piefed):
    post, status, marker = note
    piefed.delete(post['post']['id'])
    wait_for(lambda: mastodon.status(status['id']) is None)
