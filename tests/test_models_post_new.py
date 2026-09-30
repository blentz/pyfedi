"""Turning another instance's Create activity into a Post row.

Sub-project 103 -- `Post.new` in `app/models.py`. This is where a post that
arrives over ActivityPub becomes a row: the title, the body, the attachments,
the tags, the licence, the language, and which of five post types it is. Every
value in it is JSON somebody else sent, and a raise here means the post never
arrives at all.

Six defects, measured first, all of them a key read without a membership test
(D1294):

* the attachment loop checks `'type' in ...attachment[0]` in its condition and
  then reads `attachment['type']` for EVERY entry, so a list whose second
  entry was shaped differently was a `KeyError`;
* the `Document` and `Audio` branches read `attachment['url']` outright, while
  the `Link` branch beside them tests for both `href` and `url`;
* `json_tag['type']` was a `KeyError` for a tag with none, and
  `TypeError: string indices must be integers` for a tag that was a string;
* `json_tag['name']` a `KeyError` for a Hashtag with none;
* `licence['name']` a `KeyError`;
* and `language['identifier']` a `KeyError`.
"""
from unittest.mock import patch

import httpx
import pytest
from flask import current_app, g

from app import db
from app.constants import (POST_TYPE_ARTICLE, POST_TYPE_IMAGE, POST_TYPE_LINK,
                           POST_TYPE_VIDEO)
from app.models import Language, Post, Site, Tag
from app.utils import site_language_id
from tests.factories import (make_community, make_community_flair,
                             make_community_member, make_user)

AUTHOR = 'https://remote.test/u/someone'
PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'


@pytest.fixture
def env(app, api_baseline, monkeypatch):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('probeland')
    author = make_user(api_baseline.instance_remote, 'someone')
    author.ap_id = 'someone@remote.test'
    author.ap_profile_id = AUTHOR
    author.ap_public_url = AUTHOR
    db.session.commit()
    make_community_member(author, community)
    monkeypatch.setitem(current_app.config, 'IMAGE_HASHING_ENDPOINT', '')
    return SimpleNamespace(community=community, author=author,
                           baseline=api_baseline)


def an_activity(**object_overrides):
    obj = {'id': 'https://remote.test/p/1', 'type': 'Page', 'name': 'a post',
           'attributedTo': AUTHOR, 'to': [PUBLIC],
           'published': '2026-01-01T00:00:00Z'}
    obj.update(object_overrides)
    # `to` on the ACTIVITY is what decides whether a microblog post is
    # private; the copy on the object is not read for that.
    return {'id': 'https://remote.test/c/1', 'type': 'Create', 'to': [PUBLIC],
            'object': obj}


def an_image_head(http_mock, url='https://example.test/photo.png'):
    """`is_image_url` asks the url what it is before deciding."""
    http_mock.head(url).mock(return_value=httpx.Response(
        200, headers={'content-type': 'image/png'}))


def new(env, **overrides):
    return Post.new(env.author, env.community, an_activity(**overrides))


class TestAttachmentsThatAreNotWhatTheyClaim:
    """D1294. Each of these arrives from a peer and used to stop the post."""

    def test_a_second_attachment_with_no_type(self, env, http_mock):
        http_mock.head('https://example.test/a.html').mock(
            return_value=httpx.Response(200,
                                        headers={'content-type': 'text/html'}))
        post = new(env, attachment=[
            {'type': 'Link', 'href': 'https://example.test/a.html'},
            {'href': 'https://example.test/b'}])
        assert post is not None
        assert post.url == 'https://example.test/a.html'

    def test_an_attachment_that_is_not_an_object(self, env):
        post = new(env, attachment=[{'type': 'Link'}, 'a string'])
        assert post is not None

    def test_a_document_with_no_url(self, env):
        post = new(env, attachment=[{'type': 'Document',
                                     'name': 'alt text'}])
        assert post is not None
        assert post.url is None

    def test_an_audio_with_no_url(self, env):
        post = new(env, attachment=[{'type': 'Audio', 'name': 'a podcast'}])
        assert post is not None
        assert post.url is None

    def test_a_link_with_neither_href_nor_url(self, env):
        post = new(env, attachment=[{'type': 'Link'}])
        assert post is not None
        assert post.url is None

    def test_a_gup_pe_attachment_given_as_one_object(self, env, http_mock):
        """a.gup.pe sends `attachment` as a single object rather than a
        list."""
        an_image_head(http_mock)
        post = new(env, attachment={'type': 'Document',
                                    'url': 'https://example.test/photo.png'})
        assert post.url == 'https://example.test/photo.png'

    def test_one_of_those_with_no_url(self, env):
        post = new(env, attachment={'type': 'Document', 'name': 'alt text'})
        assert post is not None
        assert post.url is None

    def test_an_attachment_of_a_type_nobody_here_handles(self, env):
        post = new(env, attachment=[{'type': 'Event',
                                     'url': 'https://example.test/x'}])
        assert post is not None
        assert post.url is None


class TestWhereTheUrlComesFrom:
    def test_lemmy_before_nought_point_nineteen_sends_href(self, env,
                                                           http_mock):
        http_mock.head('https://example.test/a.html').mock(
            return_value=httpx.Response(200,
                                        headers={'content-type': 'text/html'}))
        post = new(env, attachment=[{'type': 'Link',
                                     'href': 'https://example.test/a.html'}])
        assert post.url == 'https://example.test/a.html'

    def test_nodebb_sends_url(self, env, http_mock):
        http_mock.head('https://example.test/a.html').mock(
            return_value=httpx.Response(200,
                                        headers={'content-type': 'text/html'}))
        post = new(env, attachment=[{'type': 'Link',
                                     'url': 'https://example.test/a.html'}])
        assert post.url == 'https://example.test/a.html'

    def test_mastodon_sends_a_document(self, env, http_mock):
        an_image_head(http_mock)
        post = new(env, attachment=[{'type': 'Document',
                                     'url': 'https://example.test/photo.png',
                                     'name': 'a photograph'}])
        assert post.url == 'https://example.test/photo.png'
        assert post.type == POST_TYPE_IMAGE
        assert post.image.alt_text == 'a photograph'

    def test_a_wordpress_podcast_names_the_post_after_the_audio(self, env,
                                                                http_mock):
        http_mock.head('https://example.test/episode.mp3').mock(
            return_value=httpx.Response(200,
                                        headers={'content-type': 'audio/mpeg'}))
        post = new(env, attachment=[{'type': 'Audio',
                                     'url': 'https://example.test/episode.mp3',
                                     'name': 'Episode 1'}])
        assert post.url == 'https://example.test/episode.mp3'
        assert post.title == 'Episode 1'

    def test_a_url_urlparse_cannot_read_is_dropped_not_refused(self, env):
        """Refusing the peer's whole post would hand peers a way to make this
        instance drop content."""
        post = new(env, attachment=[{'type': 'Link',
                                     'href': 'https://[::1/broken'}])
        assert post is not None
        assert post.url is None


class TestWhichKindOfPostItIs:
    def test_an_image(self, env, http_mock):
        an_image_head(http_mock)
        post = new(env, attachment=[{'type': 'Document',
                                     'url': 'https://example.test/photo.png'}])
        assert post.type == POST_TYPE_IMAGE
        assert post.image.source_url == 'https://example.test/photo.png'

    def test_a_video_by_its_extension(self, env, http_mock):
        http_mock.head('https://example.test/clip.mp4').mock(
            return_value=httpx.Response(200,
                                        headers={'content-type': 'video/mp4'}))
        post = new(env, attachment=[{'type': 'Link',
                                     'href': 'https://example.test/clip.mp4'}])
        assert post.type == POST_TYPE_VIDEO

    def test_a_video_by_the_site_hosting_it(self, env, http_mock):
        http_mock.head('https://www.youtube.com/watch?v=abc').mock(
            return_value=httpx.Response(200,
                                        headers={'content-type': 'text/html'}))
        post = new(env, attachment=[{
            'type': 'Link', 'href': 'https://www.youtube.com/watch?v=abc'}])
        assert post.type == POST_TYPE_VIDEO

    def test_an_ordinary_link(self, env, http_mock):
        http_mock.head('https://example.test/article').mock(
            return_value=httpx.Response(200,
                                        headers={'content-type': 'text/html'}))
        post = new(env, attachment=[{'type': 'Link',
                                     'href': 'https://example.test/article'}])
        assert post.type == POST_TYPE_LINK

    def test_a_post_with_no_url_at_all(self, env):
        post = new(env)
        assert post.type == POST_TYPE_ARTICLE

    def test_a_pixelfed_post(self, env, http_mock):
        opengraph = {'og:image': 'https://pixelfed.social/storage/photo.jpg',
                     'og:title': 'a photo'}
        http_mock.head('https://pixelfed.social/p/someone/1').mock(
            return_value=httpx.Response(200,
                                        headers={'content-type': 'text/html'}))
        with patch('app.utils.opengraph_parse', return_value=opengraph):
            post = new(env, attachment=[{
                'type': 'Link',
                'href': 'https://pixelfed.social/p/someone/1'}])
        assert post.type == POST_TYPE_IMAGE
        assert post.image.source_url == \
            'https://pixelfed.social/storage/photo.jpg'

    def test_a_pixelfed_post_whose_page_says_nothing(self, env, http_mock):
        http_mock.head('https://pixelfed.social/p/someone/1').mock(
            return_value=httpx.Response(200,
                                        headers={'content-type': 'text/html'}))
        with patch('app.utils.opengraph_parse', return_value=None):
            post = new(env, attachment=[{
                'type': 'Link',
                'href': 'https://pixelfed.social/p/someone/1'}])
        assert post.type == POST_TYPE_IMAGE
        assert post.image is None

    def test_a_loops_video(self, env, http_mock):
        opengraph = {'og:image': 'https://loops.video/storage/thumb.jpg',
                     'og:title': 'a clip'}
        http_mock.head('https://loops.video/v/1').mock(
            return_value=httpx.Response(200,
                                        headers={'content-type': 'text/html'}))
        with patch('app.utils.opengraph_parse', return_value=opengraph):
            post = new(env, attachment=[{
                'type': 'Link', 'href': 'https://loops.video/v/1'}])
        assert post.type == POST_TYPE_VIDEO
        assert post.image.source_url.endswith('.720p.mp4')

    def test_a_loops_video_whose_page_says_nothing(self, env, http_mock):
        http_mock.head('https://loops.video/v/1').mock(
            return_value=httpx.Response(200,
                                        headers={'content-type': 'text/html'}))
        with patch('app.utils.opengraph_parse', return_value={}):
            post = new(env, attachment=[{
                'type': 'Link', 'href': 'https://loops.video/v/1'}])
        assert post.type == POST_TYPE_VIDEO
        assert post.image is None


class TestTheTagsAPeerSends:
    def test_a_hashtag_is_kept(self, env):
        post = new(env, tag=[{'type': 'Hashtag', 'name': '#news'}])
        assert [tag.name for tag in post.tags] == ['news']

    def test_the_community_s_own_slug_is_ignored(self, env):
        """Lemmy adds it to every post in the community."""
        post = new(env, tag=[{'type': 'Hashtag',
                              'name': '#' + env.community.name}])
        assert post.tags == []

    def test_a_tag_with_no_type(self, env):
        post = new(env, tag=[{'name': '#news'}])
        assert post is not None
        assert post.tags == []

    def test_a_hashtag_with_no_name(self, env):
        post = new(env, tag=[{'type': 'Hashtag'}])
        assert post is not None
        assert post.tags == []

    def test_a_tag_that_is_not_an_object(self, env):
        post = new(env, tag=['#news'])
        assert post is not None
        assert post.tags == []

    def test_a_good_tag_beside_a_malformed_one(self, env):
        post = new(env, tag=[{'name': 'no type here'},
                             {'type': 'Hashtag', 'name': '#news'}])
        assert [tag.name for tag in post.tags] == ['news']

    def test_a_tag_list_that_is_not_a_list(self, env):
        post = new(env, tag={'type': 'Hashtag', 'name': '#news'})
        assert post is not None
        assert post.tags == []

    def test_community_flair_a_peer_names(self, env):
        make_community_flair(env.community, name='Discussion',
                             ap_id='https://remote.test/flair/1')
        db.session.commit()
        post = new(env, tag=[{'type': 'lemmy:CommunityTag',
                              'id': 'https://remote.test/flair/1',
                              'display_name': 'Discussion'}])
        assert [flair.flair for flair in post.flair] == ['Discussion']


class TestTheLicenceAndTheLanguage:
    def test_a_licence_that_is_named(self, env):
        post = new(env, licence={'name': 'CC-BY-4.0'})
        assert post.licence.name == 'CC-BY-4.0'

    def test_a_licence_with_no_name(self, env):
        post = new(env, licence={'type': 'Licence'})
        assert post is not None
        assert post.licence is None

    def test_a_licence_that_is_not_an_object(self, env):
        post = new(env, licence='CC-BY')
        assert post is not None
        assert post.licence is None

    def test_a_language_lemmy_names(self, env):
        post = new(env, language={'identifier': 'fr', 'name': 'French'})
        assert post.language.code == 'fr'

    def test_a_language_with_no_identifier(self, env):
        post = new(env, language={'name': 'French'})
        assert post is not None

    def test_a_language_with_no_name(self, env):
        post = new(env, language={'identifier': 'fr'})
        assert post is not None

    def test_a_language_that_is_not_an_object(self, env):
        post = new(env, language='fr')
        assert post is not None

    def test_the_language_mastodon_sends_instead(self, env):
        db.session.add(Language(code='fr', name='French'))
        db.session.commit()
        post = new(env, contentMap={'fr': 'bonjour'})
        assert post.language.code == 'fr'

    def test_a_content_map_naming_a_language_nobody_here_has(self, env):
        post = new(env, contentMap={'xx': 'hello'})
        assert post.language_id is None

    def test_an_empty_content_map(self, env):
        """Fixed alongside D255/D272. `next(iter({}))` raised StopIteration
        and lost the post; an empty map now names no language, like an
        absent one, so the site language applies."""
        post = new(env, contentMap={})
        assert post is not None
        assert post.language_id == site_language_id()

    def test_a_post_that_names_no_language_at_all(self, env):
        post = new(env)
        assert post is not None


class TestWhatIsRefusedOutright:
    def test_a_post_whose_title_carries_a_blocked_phrase(self, env):
        with patch('app.utils.blocked_phrases', return_value=['buy now']):
            assert new(env, name='buy now, cheap') is None

    def test_one_whose_body_does(self, env):
        with patch('app.utils.blocked_phrases', return_value=['buy now']):
            assert new(env, content='<p>buy now, cheap</p>') is None

    def test_a_microblog_with_no_content(self, env):
        activity = an_activity()
        del activity['object']['name']
        assert Post.new(env.author, env.community, activity) is None

    def test_an_image_whose_hash_is_blocked(self, env, monkeypatch,
                                            http_mock):
        an_image_head(http_mock)
        monkeypatch.setitem(current_app.config, 'IMAGE_HASHING_ENDPOINT',
                            'https://hashing.test')
        with patch('app.utils.retrieve_image_hash', return_value='1' * 256), \
                patch('app.utils.hash_matches_blocked_image',
                      return_value=True):
            post = new(env, attachment=[{
                'type': 'Document',
                'url': 'https://example.test/photo.png'}])
        assert post is None

    def test_an_image_whose_hash_is_not(self, env, monkeypatch, http_mock):
        an_image_head(http_mock)
        monkeypatch.setitem(current_app.config, 'IMAGE_HASHING_ENDPOINT',
                            'https://hashing.test')
        with patch('app.utils.retrieve_image_hash', return_value='1' * 256), \
                patch('app.utils.hash_matches_blocked_image',
                      return_value=False):
            post = new(env, attachment=[{
                'type': 'Document',
                'url': 'https://example.test/photo.png'}])
        assert post is not None
        assert post.image.hash == '1' * 256


class TestAMicroblogPost:
    def a_note(self, env, **overrides):
        activity = an_activity(**overrides)
        del activity['object']['name']
        activity['object']['type'] = 'Note'
        return Post.new(env.author, env.community, activity)

    def test_its_title_comes_from_its_body(self, env):
        post = self.a_note(env, content='<p>something worth reading</p>')
        assert post.title
        assert post.microblog is True

    def test_a_null_name_is_treated_like_a_missing_one(self, env):
        """D258, fixed. `"name": null` skipped the microblog branch and
        `None.strip()` raised; it now means the same as no name at all."""
        post = new(env, name=None, type='Note',
                   content='<p>something worth reading</p>')
        assert post.title
        assert post.microblog is True

    def test_one_addressed_to_the_public_is_not_private(self, env):
        post = self.a_note(env, content='<p>hello</p>')
        assert post.private is False

    def test_one_addressed_to_nobody_in_particular_is(self, env):
        activity = an_activity(content='<p>hello</p>')
        del activity['object']['name']
        activity['to'] = []
        activity['object']['to'] = []
        post = Post.new(env.author, env.community, activity)
        assert post.private is True

    def test_one_public_only_by_its_cc(self, env):
        activity = an_activity(content='<p>hello</p>')
        del activity['object']['name']
        activity['to'] = ['https://remote.test/u/someone/followers']
        activity['cc'] = [PUBLIC]
        post = Post.new(env.author, env.community, activity)
        assert post.private is False

    @pytest.mark.parametrize('marker,attribute', [
        ('[NSFL]', 'nsfl'), ('(NSFL)', 'nsfl'), ('[COMBAT]', 'nsfl'),
        ('[NSFW]', 'nsfw'), ('(NSFW)', 'nsfw'),
    ])
    def test_a_marker_in_the_derived_title(self, env, marker, attribute):
        post = self.a_note(env, content=f'<p>{marker} something graphic</p>')
        assert getattr(post, attribute) is True

    def test_content_that_is_not_wrapped_in_a_paragraph(self, env):
        post = self.a_note(env, content='bare text')
        assert post.body_html.startswith('<p>')
