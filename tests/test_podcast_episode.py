"""Spec 2026-10-14 §10 A17.1-A17.3: what a podcast episode is, the Document both Page builders carry, and how an
inbound audio Document is marked."""
from types import SimpleNamespace

import pytest
from flask import current_app, g

from app import db
from app.activitypub.serializers.podcast import NAME_MAX, episode_attachment, uses_podcast_episode
from app.activitypub.util import post_to_page, update_post_from_activity
from app.constants import COMMUNITY_KIND_DISCUSSION, POST_TYPE_LINK
from app.models import File, Post, Site
from app.shared.media import with_media
import app.shared.tasks.pages as pages
from app.shared.tasks.pages import send_post
from tests.factories import make_community, make_instance, make_user
from tests.peertube_video_fixtures import _remote
from tests.podcast_fixtures import episode_post, sent  # noqa: F401


def test_episode_attachment_is_an_audio_document(episode_post):
    assert uses_podcast_episode(episode_post)
    assert episode_attachment(episode_post) == {'type': 'Document', 'mediaType': 'audio/mpeg',
                                                'url': episode_post.url, 'name': 'Episode one'}


def test_episode_without_cover_has_no_icon(episode_post):
    assert 'icon' not in episode_attachment(episode_post)
    assert 'image' not in post_to_page(episode_post)


def test_episode_with_cover_has_an_icon(episode_post):
    server = current_app.config['SERVER_URL']
    episode_post.image = File(source_url=f'{server}/static/media/posts/ef/gh/coverartcoverar.jpg', width=500,
                              height=500)
    with_media(episode_post, thumbnail_width=500, thumbnail_height=500)
    db.session.commit()
    assert episode_attachment(episode_post)['icon'] == {'type': 'Image', 'url': episode_post.image.source_url}


def test_name_is_cut_to_1500(episode_post):
    episode_post.title = 'x' * 2000
    assert len(episode_attachment(episode_post)['name']) == NAME_MAX == 1500


def test_fetch_carries_the_document(episode_post):
    assert post_to_page(episode_post)['attachment'] == [episode_attachment(episode_post)]


def test_push_carries_the_same_document(episode_post, sent):
    send_post(episode_post.id, session=db.session)
    lemmy = sent['https://lemmy.example/inbox'][0]   # the community's Announce of the author's Create(Page)
    assert lemmy['object']['object']['attachment'] == [episode_attachment(episode_post)]
    mastodon = sent['https://mastodon.example/inbox'][0]   # an Announce of the id: Mastodon fetches the Page
    assert mastodon['object'] == episode_post.ap_id


def test_an_edit_carries_the_document(episode_post, sent):
    send_post(episode_post.id, edit=True, session=db.session)
    lemmy = sent['https://lemmy.example/inbox'][0]   # the community's Announce of the author's Update(Page)
    assert lemmy['object']['type'] == 'Update'
    assert lemmy['object']['object']['attachment'] == [episode_attachment(episode_post)]


def test_the_mentions_note_carries_the_document(episode_post, sent, monkeypatch):
    _, mentioned = _remote('mention.example', 'piefed', 'men')
    monkeypatch.setattr(pages, 'mentioned_users', lambda post: [mentioned])
    send_post(episode_post.id, session=db.session)
    note = sent['https://mention.example/inbox'][0]['object']
    assert note['type'] == 'Note'
    assert note['attachment'] == [episode_attachment(episode_post)]


def test_not_ready_or_linked_audio_is_not_an_episode(episode_post):
    for media in ({'status': 'processing'}, {'status': 'failed'}, {'mime': 'video/mp4'}, {'bytes': 0},
                  {'duration_s': True}, {'duration_s': 2.5}, {'uuid': ''}):
        with_media(episode_post, **{'status': 'ready', 'mime': 'audio/mpeg', 'bytes': 4406, 'duration_s': 2,
                                    'uuid': 'u', **media})
        assert not uses_podcast_episode(episode_post), media
        assert post_to_page(episode_post)['attachment'] == [{'href': episode_post.url, 'type': 'Link'}]
    episode_post.extensions = {'media_type': 'audio'}   # an audio_url link: no media record
    assert not uses_podcast_episode(episode_post)


def test_a_discussion_community_has_no_episodes(episode_post):
    episode_post.community.kind = COMMUNITY_KIND_DISCUSSION
    assert not uses_podcast_episode(episode_post)


PEER = 'piefed.example'
AUDIO_URL = f'https://{PEER}/static/media/posts/xx/yy/noextension'


@pytest.fixture
def env(app, db_session, monkeypatch):
    monkeypatch.setattr('app.utils.mime_type_using_head', lambda url: '')   # no network
    site = Site(name='Test Site', blocked_phrases='')
    db.session.add(site)
    db.session.commit()
    site.private_instance = False
    g.site = site
    g.admin_ids = []
    make_user(make_instance('test.piefed.local', software='piefed'), 'founder', local=True)
    author = make_user(make_instance(PEER), 'author')
    author.ap_profile_id = f'https://{PEER}/u/author'
    community = make_community('general')
    db.session.commit()
    return SimpleNamespace(author=author, community=community)


def _inbound(env, media_type, kind='Document', url=AUDIO_URL):
    activity = {'id': f'https://{PEER}/activities/1', 'type': 'Create', 'actor': env.author.ap_profile_id,
                'object': {'id': f'https://{PEER}/p/1', 'type': 'Page', 'name': 'Ep',
                           'attributedTo': env.author.ap_profile_id, 'audience': env.community.ap_profile_id,
                           'attachment': [{'type': kind, **({'mediaType': media_type} if media_type else {}),
                                           'url': url, 'name': 'Ep'}]}}
    post = Post.new(env.author, env.community, activity)
    db.session.commit()
    return post


def test_inbound_audio_document_is_marked_audio(env):
    post = _inbound(env, 'audio/mpeg')
    assert post.url == AUDIO_URL
    assert post.type == POST_TYPE_LINK
    assert post.extensions['media_type'] == 'audio'


def test_inbound_image_document_is_not_marked_audio(env):
    post = _inbound(env, 'image/jpeg')
    assert (post.extensions or {}).get('media_type') != 'audio'


def test_inbound_audio_attachment_is_marked_audio(env):
    post = _inbound(env, 'audio/mpeg', kind='Audio', url=AUDIO_URL + '2')
    assert post.url == AUDIO_URL + '2'
    assert post.extensions['media_type'] == 'audio'


def test_inbound_audio_attachment_without_media_type_is_not_marked(env):
    post = _inbound(env, None, kind='Audio', url=AUDIO_URL + '2')
    assert post.url == AUDIO_URL + '2'
    assert (post.extensions or {}).get('media_type') != 'audio'


@pytest.mark.parametrize('kind', ['Document', 'Audio'])
def test_an_inbound_edit_with_an_audio_attachment_marks_the_post_audio(env, kind):
    post = _inbound(env, None, kind=kind)
    assert (post.extensions or {}).get('media_type') != 'audio'

    update_post_from_activity(post, {
        'id': f'{post.ap_id}/update', 'type': 'Update',
        'object': {'id': post.ap_id, 'type': 'Page', 'name': 'Ep', 'attributedTo': env.author.ap_profile_id,
                   'attachment': [{'type': kind, 'mediaType': 'audio/mpeg', 'url': AUDIO_URL, 'name': 'Ep'}]}})

    assert post.extensions['media_type'] == 'audio'


@pytest.mark.parametrize('kind', ['Document', 'Audio'])
def test_an_inbound_edit_with_a_non_audio_attachment_does_not_mark_audio(env, kind):
    post = _inbound(env, None, kind=kind)

    update_post_from_activity(post, {
        'id': f'{post.ap_id}/update', 'type': 'Update',
        'object': {'id': post.ap_id, 'type': 'Page', 'name': 'Ep', 'attributedTo': env.author.ap_profile_id,
                   'attachment': [{'type': kind, 'mediaType': 'image/jpeg', 'url': AUDIO_URL, 'name': 'Ep'}]}})

    assert (post.extensions or {}).get('media_type') != 'audio'
