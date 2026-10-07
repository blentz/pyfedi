"""app/activitypub/util.py: the arms the per-topic suites never reached (coverage-floor work).

Every test is about what happens to input a remote peer controls: a peer that
cannot be fetched at all, a Note whose content is not text, an episode whose
post vanished between ingest and the queued fetch.
"""
import httpx
import pytest

from app import db
from app.activitypub import util as ap_util
from app.activitypub.util import (castopod_episode_url, fetch_castopod_episode_audio,
                                  fetch_castopod_episode_audio_task, find_instance_id,
                                  podcast_episode_title, refresh_community_profile_task,
                                  refresh_feed_profile_task)
from tests.factories import make_instance, seed_signing_site
from tests.test_ap_refresh_profiles import _remote_community, _remote_feed
from tests.test_podcast_episode_audio import (ANNOUNCEMENT, AUDIO, EPISODE, PEER, _episode, _ingest)


def _both_fetches_fail(monkeypatch):
    """The plain GET fails outside httpx, and so does the signed GET it falls back to."""
    def exploding(uri, params=None, headers=None):
        raise RuntimeError('not an httpx error')

    def exploding_signed(uri, private_key, key_id, **kwargs):
        raise RuntimeError('signed fetch failed too')

    monkeypatch.setattr(ap_util, 'get_request', exploding)
    monkeypatch.setattr(ap_util, 'signed_get_request', exploding_signed)


def test_a_community_that_cannot_be_fetched_even_signed_is_left_as_it_was(app, db_session, http_mock, monkeypatch):
    """A peer that refuses the plain GET and then the signed one must leave the community untouched, not crash the
    worker: the refresh is best-effort and runs again on the next schedule."""
    seed_signing_site()
    community = _remote_community()
    community.title = 'Before'
    db.session.commit()
    _both_fetches_fail(monkeypatch)

    refresh_community_profile_task(community.id, None)

    db.session.refresh(community)
    assert community.title == 'Before'


def test_a_feed_that_cannot_be_fetched_even_signed_is_left_as_it_was(app, db_session, http_mock, monkeypatch):
    seed_signing_site()
    feed = _remote_feed()
    feed.title = 'Before'
    db.session.commit()
    _both_fetches_fail(monkeypatch)

    refresh_feed_profile_task(feed.id)

    db.session.refresh(feed)
    assert feed.title == 'Before'


def test_find_instance_id_of_nothing_is_none(app, db_session):
    """No server at all (an actor id with no host) must not query for an instance named ''."""
    make_instance('known.example')

    assert find_instance_id(None) is None
    assert find_instance_id('') is None
    assert find_instance_id(' KNOWN.example ') is not None


def test_a_note_whose_content_is_not_text_is_not_an_episode_announcement():
    note = {'type': 'Note', 'content': {'en': ANNOUNCEMENT}}

    assert castopod_episode_url(note, f'https://{PEER}/@mypodcast') is None


def test_an_announcement_linking_a_non_http_url_is_not_an_episode():
    """`_as_url` drops anything that is not http(s), so a `mailto:` or `javascript:` first link is no episode."""
    note = {'type': 'Note', 'content': '<a href="mailto:host@peer.example">Episode 1</a>'}

    assert castopod_episode_url(note, f'https://{PEER}/@mypodcast') is None


def test_a_note_without_text_content_has_no_episode_title(app, db_session):
    from tests.factories import make_community, make_user
    peer = make_instance(PEER)
    author = make_user(peer, 'mypodcast')
    author.ap_profile_id = f'https://{PEER}/@mypodcast'
    community = make_community()
    community.user_id = author.id
    community.ap_profile_id = author.ap_profile_id
    db.session.commit()

    assert podcast_episode_title({'type': 'Note', 'content': None}, author, community) is None
    assert podcast_episode_title({'type': 'Note', 'content': ['x']}, author, community) is None


def test_the_episode_fetch_runs_inline_under_debug(app, db_session, http_mock, monkeypatch):
    """Under debug there is no worker, so the audio is fetched here and now; the credits fetch still follows."""
    http_mock.get(EPISODE).respond(404)
    post = _ingest(ANNOUNCEMENT)
    http_mock.get(EPISODE).respond(200, json=_episode())
    http_mock.get(f'https://{PEER}/media/ep1.jpg').respond(404)
    credits = []
    monkeypatch.setattr(ap_util.discovery_credits, 'fetch_episode_credits',
                        lambda p, url, background=False: credits.append((p.id, url)))
    monkeypatch.setitem(app.config, 'DEBUG', True)

    fetch_castopod_episode_audio(post, EPISODE)

    db.session.expire_all()
    assert post.url == AUDIO
    assert credits == [(post.id, EPISODE)]


def test_an_episode_for_a_post_that_no_longer_exists_changes_nothing(app, db_session, http_mock):
    http_mock.get(EPISODE).respond(200, json=_episode())

    assert fetch_castopod_episode_audio_task(987654, EPISODE) is None


def test_an_episode_for_a_deleted_post_does_not_resurrect_its_link(app, db_session, http_mock):
    """A post deleted between ingest and the queued fetch must not be rewritten to the audio link."""
    http_mock.get(EPISODE).respond(404)
    post = _ingest(ANNOUNCEMENT)
    original_url = post.url
    post.deleted = True
    db.session.commit()
    http_mock.get(EPISODE).respond(200, json=_episode())

    fetch_castopod_episode_audio_task(post.id, EPISODE)

    db.session.expire_all()
    assert post.url == original_url


def test_a_failure_while_storing_the_audio_rolls_back_and_propagates(app, db_session, http_mock, monkeypatch):
    """The task's own session is rolled back before the error escapes, so a worker never holds a half-written
    post; the error itself still propagates for the task runner to record."""
    http_mock.get(EPISODE).respond(404)
    post = _ingest(ANNOUNCEMENT)
    original_url = post.url
    http_mock.get(EPISODE).respond(200, json=_episode())

    def boom(url):
        raise RuntimeError('domain lookup failed')

    monkeypatch.setattr(ap_util, 'domain_from_url', boom)

    with pytest.raises(RuntimeError, match='domain lookup failed'):
        fetch_castopod_episode_audio_task(post.id, EPISODE)

    db.session.expire_all()
    assert post.url == original_url


# ---- arcs ---------------------------------------------------------------------------------------

def _local_post_with_image(post_type):
    from app.models import File
    from tests.factories import make_community, make_post, make_user
    from tests.test_ap_content_objects import seed_local_post
    community, author, post = seed_local_post()
    post.type = post_type
    post.image = File(source_url=f'https://{PEER}/media/pic.jpg', alt_text='a picture')
    db.session.commit()
    return post


def test_only_an_image_post_sends_its_picture_as_the_attachment(app, db_session):
    """A link post that also has a thumbnail keeps its Link attachment: the Image attachment replaces it only for a
    post whose type IS an image, otherwise a peer would lose the link the post is about."""
    from app.constants import POST_TYPE_IMAGE, POST_TYPE_LINK
    from app.activitypub.util import post_to_page
    link_post = _local_post_with_image(POST_TYPE_LINK)
    link_post.url = 'https://elsewhere.example/story'
    db.session.commit()

    page = post_to_page(link_post)

    assert page['attachment'] == [{'href': 'https://elsewhere.example/story', 'type': 'Link'}]
    assert page['image']['type'] == 'Image'

    link_post.type = POST_TYPE_IMAGE
    page = post_to_page(link_post)
    assert page['attachment'] == [{'type': 'Image', 'url': f'https://{PEER}/media/pic.jpg', 'name': 'a picture'}]


def test_a_post_that_opted_out_of_search_is_not_marked_searchable(app, db_session):
    from app.activitypub.util import post_to_page
    from tests.test_ap_content_objects import seed_local_post
    _community, _author, post = seed_local_post()

    assert post_to_page(post)['searchableBy'] == 'https://www.w3.org/ns/activitystreams#Public'

    post.indexable = False
    db.session.commit()
    assert 'searchableBy' not in post_to_page(post)


def test_find_flair_on_a_given_session_finds_the_community_flair(app, db_session):
    from app.activitypub.util import find_flair
    from tests.factories import make_community, make_community_flair, make_user
    make_instance('flair.example')
    make_user(None, 'flairowner', local=True)
    community = make_community('flairland')
    flair = make_community_flair(community, 'news')

    assert find_flair('news', community.id, db.session) == flair
    assert find_flair('other', community.id, db.session) is None
