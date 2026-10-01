"""Round 253: the rest of a remote feed's profile refresh.

`tests/test_ap_refresh_profiles.py` covers `refresh_user_profile_task` and
`refresh_community_profile_task` in detail, and it covers `refresh_feed_profile_task`'s own
guards. What it does not cover is the FEED task's body past the fetch:

    the fetch   one failure gives up quietly, with no sleep and no second attempt (D224)
    1177        the PeerTube `<p>` wrap for a summary that is not HTML
    1181-1184   the markdown-source preference, D1346's shape applied to a feed
    1191-1205   replacing the feed's icon and cover, which DELETES the old file from disk

The icon and cover lines are the ones worth the round: the same shape as the community form in
round 239, except here the value comes from a PEER rather than from an admin -- a changed url in
somebody else's document deletes a file on this instance's disk.
"""
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from flask import g

from app import db
from app.activitypub.util import refresh_feed_profile_task
from app.models import Feed, File, Site, utcnow
from tests.factories import make_feed, seed_community_owner

PEER = 'peer.example'


@pytest.fixture
def env(app, db_session):
    g.admin_ids = []
    instance = seed_community_owner(PEER)
    feed = make_feed(instance, 'news')
    feed.ap_public_url = f'https://{PEER}/f/news'
    feed.ap_followers_url = None
    db.session.commit()
    return SimpleNamespace(app=app, instance=instance, feed=feed)


def feed_document(fields=None):
    """The peer's Feed document, carrying only what the task reads unconditionally."""
    document = {
        'type': 'Feed',
        'id': f'https://{PEER}/f/news',
        'preferredUsername': 'news',
        'name': 'News, refreshed',
        'publicKey': {'publicKeyPem': '-----BEGIN PUBLIC KEY-----refreshed'},
    }
    if fields:
        document.update(fields)
    return document


def serve(http_mock, url, document=None, status=200):
    return http_mock.get(url).mock(
        return_value=httpx.Response(status, json=document))


def reread(feed_id):
    """The task commits in its OWN session (`get_task_session`), so this session's identity
    map still holds the row it loaded before the call (fact 976)."""
    db.session.expire_all()
    return db.session.get(Feed, feed_id)


# --------------------------------------------------------------------------
# The retry
# --------------------------------------------------------------------------


class TestWhenThePeerDoesNotAnswerTheFirstTime:

    def test_a_failed_fetch_is_not_retried(self, env, http_mock, no_real_sleeping):
        """D224, fixed (owner ruling): the task used to sleep 3-10 seconds inline in the
        worker and fetch again. `get_request` already retries a read error itself, so one
        `httpx.HTTPError` now ends the refresh -- and the task returns rather than raising,
        because a peer being unreachable is not this instance's error. Two transport calls
        are `get_request`'s own attempts; the task's retry made it four."""
        route = http_mock.get(env.feed.ap_public_url).mock(
            side_effect=httpx.ConnectError('refused'))
        before = env.feed.title

        refresh_feed_profile_task(env.feed.id)

        assert route.call_count == 2
        assert reread(env.feed.id).title == before


# --------------------------------------------------------------------------
# The description
# --------------------------------------------------------------------------


class TestTheDescriptionAFeedPublishes:

    def _refresh(self, env, http_mock, fields):
        serve(http_mock, env.feed.ap_public_url, feed_document(fields))
        refresh_feed_profile_task(env.feed.id)
        return reread(env.feed.id)

    def test_a_summary_that_is_not_html_is_wrapped_in_a_paragraph(self, env, http_mock):
        """`if not description_html.startswith('<')` -- PeerTube sends plain text, and the
        value is rendered as HTML, so unwrapped it would run into whatever follows it."""
        feed = self._refresh(env, http_mock, {'summary': 'a plain sentence'})

        assert feed.description_html == '<p>a plain sentence</p>'

    def test_html_is_left_as_html_and_sanitised(self, env, http_mock):
        """`allowlist_html`. The value comes from a peer and is rendered, so the tags that
        survive are the ones this instance allows -- a script does not."""
        feed = self._refresh(env, http_mock,
                             {'summary': '<p>hello <script>alert(1)</script></p>'})

        assert '<script>' not in feed.description_html
        assert 'hello' in feed.description_html

    def test_html_is_not_wrapped_a_second_time(self, env, http_mock):
        """The other side of the `startswith('<')` test. Wrapping HTML again gives
        `<p><p>...</p></p>`, which is what the guard exists to avoid -- and an assertion that
        merely looks for the text inside survives it, because the sanitiser tidies the
        result."""
        feed = self._refresh(env, http_mock, {'summary': '<p>already markup</p>'})

        assert feed.description_html == '<p>already markup</p>'

    def test_the_peers_own_markdown_is_preferred(self, env, http_mock):
        """D1346's shape on a feed: when the peer supplies `source` markdown, that is stored
        and re-rendered, so an edit here round-trips as markdown rather than as scraped
        HTML."""
        feed = self._refresh(env, http_mock, {
            'summary': '<p>rendered by them</p>',
            'source': {'content': 'written **by them**', 'mediaType': 'text/markdown'}})

        assert feed.description == 'written **by them**'
        assert '<strong>by them</strong>' in feed.description_html

    def test_without_a_source_the_text_is_derived_from_the_html(self, env, http_mock):
        """The `else`: `html_to_text`. The description column is the edit box's value, so it
        has to hold something a person can edit even when the peer sent only HTML."""
        feed = self._refresh(env, http_mock, {'summary': '<p>rendered <b>only</b></p>'})

        assert feed.description == 'rendered **only**' or 'rendered' in feed.description
        assert feed.description_html.startswith('<p>')

    def test_a_feed_with_no_summary_keeps_an_empty_description(self, env, http_mock):
        """`if description_html is not None and description_html != ''`. The key is optional
        and most feeds omit it."""
        feed = self._refresh(env, http_mock, {})

        assert not feed.description_html


# --------------------------------------------------------------------------
# The icon and the cover
# --------------------------------------------------------------------------


class TestReplacingAFeedsImages:
    """The url comes from the peer's document, so a change on their side deletes a file on
    this instance's disk. `delete_from_disk` is replaced here: what the rows are about is
    WHICH File is deleted and when, not the unlink itself.
    """

    @pytest.fixture
    def deletions(self, monkeypatch):
        deleted = []
        monkeypatch.setattr(File, 'delete_from_disk',
                            lambda self, *args, **kwargs: deleted.append(self.source_url))
        return deleted

    def _existing(self, env, field, url):
        row = File(source_url=url)
        db.session.add(row)
        db.session.commit()
        setattr(env.feed, f'{field}_id', row.id)
        db.session.commit()
        return row

    def _refresh(self, env, http_mock, fields):
        serve(http_mock, env.feed.ap_public_url, feed_document(fields))
        refresh_feed_profile_task(env.feed.id)
        return reread(env.feed.id)

    def test_a_first_icon_is_attached_without_deleting_anything(self, env, http_mock,
                                                               deletions):
        feed = self._refresh(env, http_mock,
                             {'icon': {'type': 'Image',
                                       'url': f'https://{PEER}/icon.png'}})

        assert feed.icon.source_url == f'https://{PEER}/icon.png'
        assert deletions == []

    def test_a_changed_icon_replaces_the_old_one_and_deletes_it(self, env, http_mock,
                                                               deletions):
        """`if feed.icon_id and icon_entry != feed.icon.source_url: delete_from_disk()`. The
        comparison is on the URL, which is the only thing this instance can compare -- and
        the old File's bytes are this instance's copy of the peer's old image."""
        self._existing(env, 'icon', f'https://{PEER}/old-icon.png')

        feed = self._refresh(env, http_mock,
                             {'icon': {'type': 'Image',
                                       'url': f'https://{PEER}/new-icon.png'}})

        assert deletions == [f'https://{PEER}/old-icon.png']
        assert feed.icon.source_url == f'https://{PEER}/new-icon.png'

    def test_an_unchanged_icon_is_left_completely_alone(self, env, http_mock, deletions):
        """The refresh runs daily. An icon whose url has not changed must not be deleted and
        re-downloaded every time, which is what the equality test is for."""
        existing = self._existing(env, 'icon', f'https://{PEER}/icon.png')

        feed = self._refresh(env, http_mock,
                             {'icon': {'type': 'Image',
                                       'url': f'https://{PEER}/icon.png'}})

        assert deletions == []
        assert feed.icon_id == existing.id

    def test_the_cover_is_handled_the_same_way(self, env, http_mock, deletions):
        """A second copy of the block on `image_id`. Two copies is two places to name the
        wrong column, so the row asserts the icon is untouched."""
        self._existing(env, 'icon', f'https://{PEER}/icon.png')
        self._existing(env, 'image', f'https://{PEER}/old-cover.png')

        feed = self._refresh(env, http_mock, {
            'icon': {'type': 'Image', 'url': f'https://{PEER}/icon.png'},
            'image': {'type': 'Image', 'url': f'https://{PEER}/new-cover.png'}})

        assert deletions == [f'https://{PEER}/old-cover.png']
        assert feed.image.source_url == f'https://{PEER}/new-cover.png'
        assert feed.icon.source_url == f'https://{PEER}/icon.png'

    def test_a_document_with_no_images_changes_neither(self, env, http_mock, deletions):
        """`if 'icon' in activity_json` / `if 'image' in ...`. Both keys are optional, and a
        document that omits them must not clear what this instance already has."""
        existing = self._existing(env, 'icon', f'https://{PEER}/icon.png')

        feed = self._refresh(env, http_mock, {})

        assert deletions == []
        assert feed.icon_id == existing.id

    def test_an_unreadable_image_entry_is_ignored(self, env, http_mock, deletions):
        """`if icon_entry:` -- `image_url_from` answers None for a shape it cannot read, and
        a None `source_url` would be a File row pointing at nothing."""
        existing = self._existing(env, 'icon', f'https://{PEER}/icon.png')

        feed = self._refresh(env, http_mock, {'icon': {'type': 'Image'}})

        assert deletions == []
        assert feed.icon_id == existing.id
