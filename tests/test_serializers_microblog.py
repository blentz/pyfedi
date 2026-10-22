"""microblog_note (spec 2026-10-14 §4.2, amended §10 A2): a local microblogs post as a Mastodon-native Note."""
import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from app import db
from app.activitypub.serializers import (microblog_note, note_addressing, note_attachments, note_content,
                                         uses_microblog_note)
from app.activitypub.serializers.microblog import body_hashtags
from app.constants import (COMMUNITY_KIND_MICROBLOGS, POST_TYPE_ARTICLE, POST_TYPE_EVENT, POST_TYPE_IMAGE,
                           POST_TYPE_LINK, POST_TYPE_POLL, POST_TYPE_VIDEO, VISIBILITY_FOLLOWERS)
from app.models import File
from tests.factories import make_community, make_instance, make_post, make_user

PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'
FIXTURE = Path(__file__).parent / 'fixtures' / 'mastodon_note_v4_5.json'


def _local(community_name='microblogs', title='Hello there', body=None, body_html=None, post_type=POST_TYPE_ARTICLE):
    local = make_instance('test.piefed.local', software='piefed')
    alice = make_user(local, 'alice', local=True)
    community = make_community(community_name, kind=COMMUNITY_KIND_MICROBLOGS if community_name == 'microblogs' else None)
    post = make_post(community, alice, ap_id='https://test.piefed.local/post/1', title=title)
    post.type = post_type
    post.body = body
    post.body_html = body_html
    db.session.commit()
    return post


def test_uses_microblog_note_only_in_local_microblogs(db_session):
    post = _local()
    assert uses_microblog_note(post)


def test_other_local_community_does_not_use_it(db_session):
    assert not uses_microblog_note(_local(community_name='c1'))


def test_remote_microblogs_community_does_not_use_it(db_session):
    post = _local()
    post.community.ap_profile_id = 'https://peer.example/c/microblogs'
    post.community.ap_id = 'microblogs@peer.example'
    db.session.commit()
    assert not uses_microblog_note(post)


@pytest.mark.parametrize('post_type', [POST_TYPE_POLL, POST_TYPE_EVENT])
def test_polls_and_events_keep_their_own_builder(db_session, post_type):
    assert not uses_microblog_note(_local(post_type=post_type))


def test_content_is_title_paragraph_then_body(db_session):
    post = _local(title='Hello there', body='more', body_html='<p>more</p>')
    assert note_content(post) == '<p>Hello there</p><p>more</p>'


def test_title_only_post_is_its_title(db_session):
    assert note_content(_local(title='Just a title')) == '<p>Just a title</p>'


def test_title_already_leading_body_is_not_repeated(db_session):
    post = _local(title='Fish & chips today', body='Fish & chips today, yes',
                  body_html='<p>Fish &amp; chips today, yes</p>')
    assert note_content(post) == '<p>Fish &amp; chips today, yes</p>'


def test_empty_title_is_body(db_session):
    post = _local(title='', body='b', body_html='<p>b</p>')
    assert note_content(post) == '<p>b</p>'


@pytest.mark.parametrize('post_type', [POST_TYPE_LINK, POST_TYPE_VIDEO])
def test_link_title_links_the_url(db_session, post_type):
    post = _local(title='A site', post_type=post_type)
    post.url = 'https://example.com/x'
    assert note_content(post) == '<p><a href="https://example.com/x">A site</a></p>'


def test_link_post_without_url_has_plain_title(db_session):
    assert note_content(_local(title='A site', post_type=POST_TYPE_LINK)) == '<p>A site</p>'


def test_title_and_url_are_escaped(db_session):
    post = _local(title='<b>x</b> & "y"', post_type=POST_TYPE_LINK)
    post.url = 'https://example.com/?a=1&b="2"'
    assert note_content(post) == ('<p><a href="https://example.com/?a=1&amp;b=&#34;2&#34;">'
                                  '&lt;b&gt;x&lt;/b&gt; &amp; &#34;y&#34;</a></p>')


def test_public_addressing(db_session):
    post = _local()
    bob = make_user(make_instance('mastodon.example'), 'bob')
    bob.ap_profile_id = bob.ap_public_url = 'https://mastodon.example/users/bob'
    db.session.commit()
    addressing = note_addressing(post, [bob])
    assert addressing['to'] == [PUBLIC]
    assert addressing['cc'] == [post.author.followers_url(), 'https://mastodon.example/users/bob']
    assert addressing['interactionPolicy'] == {'canQuote': {'automaticApproval': [PUBLIC]}}


def test_followers_only_addressing_is_amendment_b(db_session):
    post = _local()
    post.visibility = VISIBILITY_FOLLOWERS
    db.session.commit()
    assert note_addressing(post, []) == {'to': [post.author.followers_url()], 'cc': [],
                                         'interactionPolicy': {'canQuote': {'automaticApproval':
                                                                            [post.author.public_url()]}}}


def test_image_attachment_is_a_document(db_session):
    post = _local(post_type=POST_TYPE_IMAGE)
    post.image = File(source_url='https://test.piefed.local/static/media/a.webp', alt_text='a cat',
                      width=640, height=480)
    db.session.commit()
    assert note_attachments(post) == [{'type': 'Document', 'mediaType': 'image/webp',
                                       'url': 'https://test.piefed.local/static/media/a.webp',
                                       'name': 'a cat', 'width': 640, 'height': 480}]


def test_image_without_size_or_alt(db_session):
    post = _local(post_type=POST_TYPE_IMAGE)
    post.image = File(source_url='https://test.piefed.local/static/media/a.png')
    db.session.commit()
    assert note_attachments(post) == [{'type': 'Document', 'mediaType': 'image/png',
                                       'url': 'https://test.piefed.local/static/media/a.png', 'name': ''}]


def test_image_post_with_no_image_has_no_attachment(db_session):
    assert note_attachments(_local(post_type=POST_TYPE_IMAGE)) == []


def test_link_post_attaches_a_link(db_session):
    post = _local(post_type=POST_TYPE_LINK)
    post.url = 'https://example.com/x'
    assert note_attachments(post) == [{'type': 'Link', 'href': 'https://example.com/x'}]


def test_text_post_has_no_attachments(db_session):
    assert note_attachments(_local()) == []


def test_note_shape(db_session):
    post = _local(title='Hi', body='#tag body', body_html='<p>#tag body</p>')
    note = microblog_note(post, [])
    assert note['type'] == 'Note'
    assert note['id'] == post.public_url()
    assert note['attributedTo'] == post.author.public_url()
    assert note['content'] == ('<p>Hi</p><p><a class="mention hashtag" href="https://test.piefed.local/tag/tag" '
                               'rel="tag">#<span>tag</span></a> body</p>')
    assert note['inReplyTo'] is None
    assert 'name' not in note and 'audience' not in note
    assert post.community.public_url() not in note['to'] + note['cc']
    assert note['source'] == {'content': '#tag body', 'mediaType': 'text/markdown'}


def test_content_warning_becomes_summary_and_sensitive(db_session):
    post = _local()
    post.content_warning = 'spoilers'
    db.session.commit()
    note = microblog_note(post, [])
    assert note['summary'] == 'spoilers' and note['sensitive'] is True


def test_no_content_warning_no_summary(db_session):
    note = microblog_note(_local(), [])
    assert 'summary' not in note and note['sensitive'] is False


def test_mentions_become_mention_tags(db_session):
    post = _local()
    bob = make_user(make_instance('mastodon.example'), 'bob')
    bob.ap_profile_id = bob.ap_public_url = 'https://mastodon.example/users/bob'
    bob.ap_id = 'bob@mastodon.example'
    db.session.commit()
    note = microblog_note(post, [bob])
    assert {'href': 'https://mastodon.example/users/bob', 'name': bob.mention_tag(), 'type': 'Mention'} in note['tag']


def test_edited_post_has_updated(db_session):
    from datetime import datetime
    post = _local()
    post.edited_at = datetime(2026, 10, 14, 12, 0, 0)
    db.session.commit()
    assert microblog_note(post, [])['updated'] == '2026-10-14T12:00:00+00:00'


def test_unedited_post_has_no_updated(db_session):
    assert 'updated' not in microblog_note(_local(), [])


def test_language_gives_content_map(db_session):
    from app.models import Language
    post = _local(title='Hi')
    post.language = Language(code='en', name='English')
    db.session.commit()
    assert microblog_note(post, [])['contentMap'] == {'en': '<p>Hi</p>'}


def test_no_language_no_content_map(db_session):
    assert 'contentMap' not in microblog_note(_local(), [])


def test_indexable_public_post_is_searchable(db_session):
    post = _local()
    post.indexable = True
    db.session.commit()
    assert microblog_note(post, [])['searchableBy'] == PUBLIC


def test_followers_only_post_never_searchable_and_no_count_collections(db_session):
    post = _local()
    post.indexable = True
    post.visibility = VISIBILITY_FOLLOWERS
    db.session.commit()
    note = microblog_note(post, [])
    assert 'searchableBy' not in note and 'likes' not in note


def test_keys_shared_with_mastodon_use_mastodons_names(db_session):
    mastodon = json.loads(FIXTURE.read_text())
    ours = microblog_note(_local(title='Hi', body='b', body_html='<p>b</p>'), [])
    for key in ('id', 'type', 'attributedTo', 'to', 'cc', 'content', 'published', 'url', 'sensitive', 'tag',
                'attachment', 'inReplyTo', 'replies'):
        assert key in mastodon, f'fixture lacks {key}: recapture it'
        assert key in ours


def test_non_indexable_public_post_is_not_searchable(db_session):
    post = _local()
    post.indexable = False
    db.session.commit()
    note = microblog_note(post, [])
    assert 'searchableBy' not in note and 'likes' in note


def test_title_that_is_only_a_prefix_of_a_word_is_kept(db_session):
    post = _local(title='Hi', body='Hiking today', body_html='<p>Hiking today</p>')
    assert note_content(post) == '<p>Hi</p><p>Hiking today</p>'


def test_title_followed_by_a_word_break_is_omitted(db_session):
    post = _local(title='Hi', body='Hi there', body_html='<p>Hi there</p>')
    assert note_content(post) == '<p>Hi there</p>'


def test_title_equal_to_body_is_omitted(db_session):
    post = _local(title='Hi', body='Hi', body_html='<p>Hi</p>')
    assert note_content(post) == '<p>Hi</p>'


@pytest.mark.parametrize('post_type', [POST_TYPE_LINK, POST_TYPE_VIDEO])
def test_non_http_url_is_not_linked(db_session, post_type):
    post = _local(title='A site', post_type=post_type)
    post.url = 'javascript:alert(1)'
    assert note_content(post) == '<p>A site</p>'
    assert note_attachments(post) == []


def test_uppercase_https_scheme_is_linked(db_session):
    post = _local(title='A site', post_type=POST_TYPE_LINK)
    post.url = 'HTTPS://example.com/x'
    assert note_content(post) == '<p><a href="HTTPS://example.com/x">A site</a></p>'
    assert note_attachments(post) == [{'type': 'Link', 'href': 'HTTPS://example.com/x'}]


def test_malformed_url_is_not_linked_and_does_not_crash(db_session):
    from app.activitypub.util import post_to_page
    post = _local(title='A site', post_type=POST_TYPE_LINK)
    post.url = 'http://[x'     # the API accepts any string starting with http
    note = microblog_note(post, [])
    assert note['content'] == '<p>A site</p>'
    assert note['attachment'] == []
    assert post_to_page(post)['type'] == 'Note'


# ----------------------------------------------------------------- hashtags in the body (spec §1: hashtags live)

def _hashtag_link(name, shown=None):
    return (f'<a class="mention hashtag" href="https://test.piefed.local/tag/{name}" rel="tag">'
            f'#<span>{shown or name}</span></a>')


def _hashtags(note):
    return [entry for entry in note['tag'] if entry['type'] == 'Hashtag']


def _names(body_html):
    """The hashtags a body holds, as the post create and edit store them in post.tags (§10 A12)."""
    return list(body_hashtags(body_html))


def test_body_hashtag_is_linked_and_its_tag_comes_from_post_tags(db_session):
    """A12 replaces A8: the Note's tag list is post.tags; the body is not scanned for it. A post stored before A12,
    whose body tags were never stored, links them in content and lists none."""
    post = _local(title='Hi', body='x #interop', body_html='<p>x #interop</p>')
    note = microblog_note(post, [])
    assert _hashtags(note) == []
    assert note['content'] == f'<p>Hi</p><p>x {_hashtag_link("interop")}</p>'


def test_content_map_equals_content_with_hashtags(db_session):
    from app.models import Language
    post = _local(title='Hi', body='#a', body_html='<p>#a</p>')
    post.language = Language(code='en', name='English')
    db.session.commit()
    note = microblog_note(post, [])
    assert note['contentMap'] == {'en': note['content']}
    assert _hashtag_link('a') in note['content']


def test_hashtag_markup_matches_what_mastodon_emits(db_session):
    mastodon = json.loads(FIXTURE.read_text())['content']
    assert '<a href="https://mastodon.test/tags/fixture" class="mention hashtag" rel="tag">#<span>fixture</span></a>' in mastodon
    post = _local(title='Hi', body='#fixture', body_html='<p>#fixture</p>')
    link = BeautifulSoup(microblog_note(post, [])['content'], 'html.parser').find('a')
    assert link['class'] == ['mention', 'hashtag'] and link['rel'] == ['tag']
    assert link.get_text() == '#fixture' and link.span.get_text() == 'fixture'


def test_hashtag_inside_a_link_or_code_is_ignored(db_session):
    html = ('<p><a href="https://e.example/page#frag">see #linked</a> <code>#coded</code></p>'
            '<pre>#pre</pre><p><img alt="#alt" src="https://e.example/i.png#img"></p>')
    note = microblog_note(_local(title='Hi', body='b', body_html=html), [])
    assert _names(html) == []
    assert note['content'] == '<p>Hi</p>' + html


def test_hashtag_glued_to_a_word_is_ignored(db_session):
    assert _names('<p>a#b and 9#c</p>') == []


def test_hashtag_after_punctuation_counts(db_session):
    assert _names('<p>(#one) "#two", #_no #3</p>') == ['one', 'two']


def test_an_all_digit_hashtag_is_not_a_hashtag(db_session):
    note = microblog_note(_local(title='Hi', body='b', body_html='<p>#1 and #2024</p>'), [])
    assert _names('<p>#1 and #2024</p>') == []
    assert note['content'] == '<p>Hi</p><p>#1 and #2024</p>'


def test_a_hashtag_with_a_letter_among_digits_is_one(db_session):
    note = microblog_note(_local(title='Hi', body='b', body_html='<p>#2024cats #café</p>'), [])
    assert _names('<p>#2024cats #café</p>') == ['2024cats', 'café']
    assert _hashtag_link('2024cats') in note['content'] and _hashtag_link('café') in note['content']


def test_hashtags_differing_in_case_are_one_tag_and_both_linked(db_session):
    post = _local(title='Hi', body='b', body_html='<p>#Foo then #foo</p>')
    note = microblog_note(post, [])
    assert _names(post.body_html) == ['foo']
    assert _hashtag_link('foo', 'Foo') in note['content'] and _hashtag_link('foo') in note['content']


def test_unicode_hashtag(db_session):
    note = microblog_note(_local(title='Hi', body='b', body_html='<p>#Café!</p>'), [])
    assert _names('<p>#Café!</p>') == ['café']
    assert _hashtag_link('café', 'Café') + '!' in note['content']


def test_the_tag_list_is_post_tags_only(db_session):
    from app.models import Tag
    post = _local(title='Hi', body='b', body_html='<p>#Interop and #other</p>')
    post.tags.append(Tag(name='interop'))
    db.session.commit()
    note = microblog_note(post, [])
    assert [t['name'] for t in _hashtags(note)] == ['#interop']
    assert note['tag'][0]['href'] == 'https://test.piefed.local/tag/interop'


def test_title_only_post_has_no_derived_tags(db_session):
    note = microblog_note(_local(title='#titletag'), [])
    assert _hashtags(note) == [] and note['content'] == '<p>#titletag</p>'


def test_html_comment_is_not_scanned(db_session):
    assert _names('<p>x</p><!-- #hidden -->') == []
