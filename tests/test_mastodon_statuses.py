"""Mastodon's Status for a post or comment (plan W1.2; rulings M3, M5; the fields apps require are in plan W1.0)."""
from itertools import count

import pytest

import app.mastodon.statuses as statuses_module
from app import db
from app.constants import POST_TYPE_GALLERY, POST_TYPE_IMAGE, POST_TYPE_LINK, VISIBILITY_FOLLOWERS, \
    VISIBILITY_UNLISTED
from app.mastodon import ids
from app.mastodon.statuses import Page, status
from app.models import PostBookmark, PostBoost, post_file
from tests.factories import make_community, make_file, make_post, make_post_reply, make_post_reply_bookmark, \
    make_post_reply_vote, make_post_vote, make_user

_SERIAL = count()
# Every app decodes these (Mastodon Android's @RequiredField, Tusky's non-null fields, Elk's strict null checks)
ALWAYS = ('id', 'uri', 'url', 'created_at', 'account', 'content', 'visibility', 'sensitive', 'spoiler_text',
          'media_attachments', 'mentions', 'tags', 'emojis', 'reblogs_count', 'favourites_count', 'replies_count',
          'in_reply_to_id', 'in_reply_to_account_id', 'reblog', 'poll', 'card', 'language', 'edited_at')


@pytest.fixture
def world(app, api_baseline):
    author = make_user(None, 'statusauthor', local=True)
    viewer = make_user(None, 'statusviewer', local=True)
    community = make_community('statuses')
    return {'author': author, 'viewer': viewer, 'community': community}


def _post(world, **columns):
    post = make_post(world['community'], world['author'], ap_id=f'https://test.piefed.local/post/s{next(_SERIAL)}',
                     title=columns.pop('title', 'A title'))
    post.body_html = columns.pop('body_html', '<p>The body</p>')
    for name, value in columns.items():
        setattr(post, name, value)
    db.session.commit()
    return post


def _one(row, viewer=None):
    return status(row, Page(viewer, [row]))


def test_every_field_an_app_decodes_is_there_and_nulls_are_null(world):
    entity = _one(_post(world))
    for field in ALWAYS:
        assert field in entity, field
    assert entity['reblog'] is None and entity['in_reply_to_id'] is None and entity['poll'] is None
    assert entity['mentions'] == [] and entity['emojis'] == [] and entity['media_attachments'] == []
    assert entity['account']['username'] == 'statusauthor'
    assert entity['created_at'].endswith('Z')


def test_a_titled_community_post_is_bold_title_body_link_and_community(world):
    post = _post(world, title='Big <news>', type=POST_TYPE_LINK, url='https://news.example/a?b=1&c=2')
    content = _one(post)['content']
    assert content.startswith('<p><strong>Big &lt;news&gt;</strong></p><p>The body</p>')
    assert '<a href="https://news.example/a?b=1&amp;c=2">' in content
    assert 'class="u-url mention"' in content and '@<span>statuses</span>' in content
    assert content.index('news.example') < content.index('mention')


def test_a_link_post_has_a_card_and_a_discussion_none(world):
    link = _one(_post(world, type=POST_TYPE_LINK, url='https://news.example/a'))
    assert link['card']['url'] == 'https://news.example/a' and link['card']['type'] == 'link'
    assert link['card']['title'] == 'A title' and link['card']['description'] == ''
    assert _one(_post(world))['card'] is None


def test_a_javascript_link_is_never_linked_or_carded(world):
    entity = _one(_post(world, type=POST_TYPE_LINK, url='javascript:alert(1)'))
    assert 'javascript' not in entity['content'] and entity['card'] is None


def test_a_body_that_starts_with_its_title_is_not_titled_twice(world):
    content = _one(_post(world, title='Hello there', body_html='<p>Hello there, all</p>'))['content']
    assert '<strong>' not in content and content.startswith('<p>Hello there, all</p>')


def test_a_microblog_post_is_its_body_with_no_community_mention(world):
    microblogs = make_community('microblogs')
    post = make_post(microblogs, world['author'], ap_id='https://test.piefed.local/post/mb', microblog=True)
    content = _one(post)['content']
    assert content == '<p>a short toot</p>'


def test_hashtags_in_the_body_link_to_this_domains_tag_page(world):
    content = _one(_post(world, body_html='<p>about #pie</p>'))['content']
    assert '/tag/pie' in content and 'class="mention hashtag"' in content


def test_a_comment_answers_its_post_or_its_parent_comment(world):
    post = _post(world)
    top = make_post_reply(post, world['author'], body='top')
    nested = make_post_reply(post, world['viewer'], body='nested')
    nested.parent_id = top.id
    db.session.commit()
    top_entity, nested_entity = _one(top), _one(nested)
    assert top_entity['in_reply_to_id'] == ids.status_id(post)
    assert top_entity['in_reply_to_account_id'] == str(world['author'].id)
    assert nested_entity['in_reply_to_id'] == ids.status_id(top)
    assert nested_entity['content'] == nested.body_html and nested_entity['card'] is None


def test_visibility_and_warnings(world):
    assert _one(_post(world))['visibility'] == 'public'
    assert _one(_post(world, visibility=VISIBILITY_UNLISTED))['visibility'] == 'unlisted'
    assert _one(_post(world, visibility=VISIBILITY_FOLLOWERS))['visibility'] == 'private'
    warned = _one(_post(world, content_warning='spiders'))
    assert warned['sensitive'] is True and warned['spoiler_text'] == 'spiders'
    assert _one(_post(world, nsfw=True))['sensitive'] is True


def test_counts_are_upvotes_comments_and_boosts(world):
    post = _post(world, up_votes=7, down_votes=3, reply_count=2, post_boosts=[{'user_id': 1}, {'user_id': 2}])
    entity = _one(post)
    assert (entity['favourites_count'], entity['replies_count'], entity['reblogs_count']) == (7, 2, 2)


def test_the_viewer_sees_what_they_favourited_bookmarked_and_boosted(world):
    viewer = world['viewer']
    liked, kept, boosted, disliked = (_post(world) for _ in range(4))
    make_post_vote(viewer, liked, 1)
    make_post_vote(viewer, disliked, -1)
    db.session.add_all([PostBookmark(user_id=viewer.id, post_id=kept.id),
                        PostBoost(user_id=viewer.id, post_id=boosted.id)])
    reply = make_post_reply(liked, world['author'])
    make_post_reply_vote(viewer, reply, 1)
    make_post_reply_bookmark(viewer, reply)
    db.session.commit()
    rows = [liked, kept, boosted, disliked, reply]
    entities = {entity['id']: entity for entity in Page(viewer, rows).statuses(rows)}
    flags = lambda row: tuple(entities[ids.status_id(row)][name] for name in ('favourited', 'bookmarked', 'reblogged'))
    assert flags(liked) == (True, False, False)
    assert flags(kept) == (False, True, False)
    assert flags(boosted) == (False, False, True)
    assert flags(disliked) == (False, False, False)
    assert flags(reply) == (True, True, False)


def test_nobody_signed_in_is_told_no_viewer_flags(world):
    entity = _one(_post(world))
    assert not {'favourited', 'reblogged', 'bookmarked', 'muted', 'pinned'} & set(entity)


def test_a_pinned_post_is_its_communitys_sticky(world):
    assert _one(_post(world, sticky=True), world['viewer'])['pinned'] is True
    assert _one(_post(world), world['viewer'])['pinned'] is False


def test_an_image_post_has_its_picture_as_an_attachment(world):
    picture = make_file(file_path='app/static/media/posts/aa/pic.webp')
    picture.width, picture.height, picture.alt_text = 800, 400, 'a pie'
    db.session.commit()
    entity = _one(_post(world, type=POST_TYPE_IMAGE, image_id=picture.id))
    (attachment,) = entity['media_attachments']
    assert attachment['id'] == str(picture.id) and attachment['type'] == 'image'
    assert attachment['url'].startswith('http') and attachment['url'].endswith('pic.webp')
    assert attachment['preview_url'].startswith('http') and attachment['description'] == 'a pie'
    assert attachment['meta']['original'] == {'width': 800, 'height': 400, 'size': '800x400', 'aspect': 2.0}


def test_a_gallery_has_each_picture_in_order(world):
    first, second = make_file(source_url='https://cdn.example/1.png'), make_file(source_url='https://cdn.example/2.png')
    post = _post(world, type=POST_TYPE_GALLERY, gallery_count=2)
    db.session.execute(post_file.insert(), [{'post_id': post.id, 'file_id': second.id, 'weight': 2},
                                            {'post_id': post.id, 'file_id': first.id, 'weight': 1}])
    db.session.commit()
    urls = [attachment['url'] for attachment in _one(post)['media_attachments']]
    assert urls == ['https://cdn.example/1.png', 'https://cdn.example/2.png']
    assert _one(post)['media_attachments'][0]['meta'] == {}


def test_a_page_counts_every_authors_statuses_once(world, monkeypatch):
    calls = []
    real = statuses_module.listable_counts
    monkeypatch.setattr(statuses_module, 'listable_counts', lambda *args: calls.append(args) or real(*args))
    rows = [_post(world) for _ in range(3)]
    Page(world['viewer'], rows).statuses(rows)
    assert len(calls) == 1


def test_a_comment_whose_parent_is_gone_answers_the_post(world):
    post = _post(world)
    orphan = make_post_reply(post, world['author'])
    orphan.parent_id = 2 ** 31 - 1   # no such comment
    db.session.commit()
    assert _one(orphan)['in_reply_to_id'] == ids.status_id(post)
