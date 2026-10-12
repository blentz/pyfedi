"""C2: inbound quote posts (docs/superpowers/specs/2026-10-12-ap-interop-c2-quote-posts-design.md)."""
import contextlib

import pytest

from app import db
from app.activitypub.util import (create_post_reply, quote_id_from, update_post_from_activity,
                                  update_post_reply_from_activity)
from app.constants import POST_TYPE_EVENT
from app.models import Post, PostReply
from app.utils import microblog_content_to_link, strip_quote_fallback, utcnow
from tests.factories import make_community, make_community_member, make_instance, make_post, make_site, make_user

OWN = 'https://remote.test/notes/1'
QUOTED = 'https://other.test/notes/9'


@pytest.mark.parametrize('key', ['quote', 'quoteUrl', 'quoteUri', '_misskey_quote'])
def test_each_key_is_read(key):
    assert quote_id_from({'id': OWN, key: QUOTED}) == QUOTED


def test_quote_wins_over_the_legacy_keys():
    obj = {'id': OWN, '_misskey_quote': 'https://a.test/1', 'quoteUri': 'https://b.test/1',
           'quoteUrl': 'https://c.test/1', 'quote': QUOTED}
    assert quote_id_from(obj) == QUOTED


def test_an_invalid_first_key_falls_through():
    assert quote_id_from({'id': OWN, 'quote': 42, 'quoteUrl': QUOTED}) == QUOTED


def test_dict_form_uses_its_id():
    assert quote_id_from({'id': OWN, 'quote': {'id': QUOTED, 'type': 'Note'}}) == QUOTED


@pytest.mark.parametrize('value', [
    None, '', 42, [QUOTED], {'id': 7}, {}, 'ftp://other.test/1', 'https:///nohost', 'javascript:alert(1)',
    'https://other.test/' + 'a' * 240, 'https://ötherr.test/1', 'https://other.test/a b',
    'https://other.test/\x00', 'https://[::1/', 'other.test/1',
])
def test_hostile_values_are_refused(value):
    assert quote_id_from({'id': OWN, 'quote': value}) is None


def test_exactly_255_characters_is_accepted():
    url = 'https://other.test/' + 'a' * (255 - len('https://other.test/'))
    assert quote_id_from({'id': OWN, 'quote': url}) == url


def test_a_self_quote_is_none():
    assert quote_id_from({'id': OWN, 'quote': OWN}) is None


def test_no_quote_is_none():
    assert quote_id_from({'id': OWN}) is None


def test_columns_exist(app, db_session):
    from app.models import Post, PostReply
    assert Post.quote_ap_id.type.length == 255
    assert PostReply.quote_ap_id.type.length == 255


PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'
PEER = 'remote.test'


class _RedisLockOnlyDouble:
    """`app.redis_client` stand-in: only `.lock(...)`, as tests/test_ap_create_reply.py explains."""

    def lock(self, *args, **kwargs):
        return contextlib.nullcontext()


@pytest.fixture
def redis_lock_only_double(monkeypatch):
    monkeypatch.setattr('app.redis_client', _RedisLockOnlyDouble())


@pytest.fixture
def world(app, db_session):
    make_site()
    instance = make_instance(PEER)
    make_user(instance, 'community_owner')
    community = make_community(host=PEER)
    community.ap_fetched_at = utcnow()
    author = make_user(instance, 'quoter')
    author.ap_profile_id = author.ap_public_url = f'https://{PEER}/u/quoter'
    make_community_member(author, community)
    parent = make_post(community, author, ap_id=f'https://{PEER}/post/1')
    db.session.commit()
    return community, author, parent


def _create(obj):
    return {'id': obj['id'] + '/create', 'type': 'Create', 'to': [PUBLIC], 'object': obj}


def _note(n, **fields):
    obj = {'id': f'https://{PEER}/notes/{n}', 'type': 'Note', 'attributedTo': f'https://{PEER}/u/quoter',
           'to': [PUBLIC], 'cc': [], 'content': '<p>hello there friends</p>', 'published': '2026-01-01T00:00:00Z'}
    obj.update(fields)
    return obj


def _new_reply(world, n, **fields):
    community, author, parent = world
    return create_post_reply(store_ap_json=False, community=community, in_reply_to=parent.ap_id,
                             request_json=_create(_note(n, inReplyTo=parent.ap_id, **fields)), user=author)


def test_post_new_stores_the_quote(world):
    community, author, _ = world
    post = Post.new(author, community, _create(_note(2, quote=QUOTED)))
    assert post.quote_ap_id == QUOTED


def test_post_new_without_a_quote_stores_none(world):
    community, author, _ = world
    assert Post.new(author, community, _create(_note(3))).quote_ap_id is None


def test_reply_stores_the_quote(world, redis_lock_only_double):
    assert _new_reply(world, 4, quoteUri=QUOTED).quote_ap_id == QUOTED


def test_reply_without_a_quote_stores_none(world, redis_lock_only_double):
    assert _new_reply(world, 7).quote_ap_id is None


@pytest.mark.parametrize('before,after', [(None, QUOTED), (QUOTED, 'https://other.test/notes/10'), (QUOTED, None)])
def test_post_update_sets_changes_and_clears(world, redis_lock_only_double, before, after):
    community, author, _ = world
    post = Post.new(author, community, _create(_note(5, **({'quote': before} if before else {}))))
    db.session.add(post)
    db.session.commit()
    update_post_from_activity(post, {'id': 'u', 'type': 'Update', 'object': _note(5, **({'quote': after} if after else {}))})
    assert post.quote_ap_id == after


@pytest.mark.parametrize('before,after', [(None, QUOTED), (QUOTED, None)])
def test_reply_update_sets_and_clears(world, redis_lock_only_double, before, after):
    _, _, parent = world
    reply = _new_reply(world, 6, **({'quote': before} if before else {}))
    update_post_reply_from_activity(reply, {'id': 'u', 'type': 'Update',
                                            'object': _note(6, inReplyTo=parent.ap_id,
                                                            **({'quote': after} if after else {}))})
    assert reply.quote_ap_id == after


def test_a_local_reply_without_request_json_stores_no_quote(world, redis_lock_only_double):
    _, author, parent = world
    reply = PostReply.new(author, parent, None, body='hi', body_html='<p>hi</p>', notify_author=True,
                          language_id=None, distinguished=False, answer=False)
    assert reply.quote_ap_id is None


def test_quote_inline_element_is_removed():
    html = f'<p>my take</p><p class="quote-inline">RE: <a href="{QUOTED}">{QUOTED}</a></p>'
    assert strip_quote_fallback(html, QUOTED) == '<p>my take</p>'


def test_trailing_re_paragraph_is_removed():
    html = f'<p>my take</p><p>RE: <a href="{QUOTED}">other.test/notes/9</a></p>'
    assert strip_quote_fallback(html, QUOTED) == '<p>my take</p>'


def test_misskey_fallback_keeps_the_text_before_it():
    html = f'<p><span>my take<br><br>RE: <a href="{QUOTED}">{QUOTED}</a></span></p>'
    assert strip_quote_fallback(html, QUOTED) == '<p><span>my take</span></p>'


def test_misskey_fallback_in_a_bare_paragraph_and_whitespace_between_breaks():
    html = f'<p>my take<br> <br>RE: <a href="{QUOTED}">{QUOTED}</a></p>'
    assert strip_quote_fallback(html, QUOTED) == '<p>my take</p>'


def test_a_different_link_is_kept():
    html = '<p>my take</p><p>RE: <a href="https://elsewhere.test/1">x</a></p>'
    assert strip_quote_fallback(html, QUOTED) == html


def test_text_beside_the_link_keeps_the_paragraph():
    html = f'<p>my take</p><p>RE: <a href="{QUOTED}">this</a> is wrong</p>'
    assert strip_quote_fallback(html, QUOTED) == html


def test_not_the_last_paragraph_is_kept():
    html = f'<p>RE: <a href="{QUOTED}">x</a></p><p>my take</p>'
    assert strip_quote_fallback(html, QUOTED) == html


def test_words_before_re_in_the_same_text_node_are_kept():
    html = f'<p>see RE: <a href="{QUOTED}">x</a></p>'
    assert strip_quote_fallback(html, QUOTED) == html


def test_a_last_paragraph_without_links_is_kept():
    html = '<p>my take</p><p>no links here</p>'
    assert strip_quote_fallback(html, QUOTED) == html


def test_a_link_whose_previous_sibling_is_a_tag_is_kept():
    html = f'<p><b>RE:</b><a href="{QUOTED}">x</a></p>'
    assert strip_quote_fallback(html, QUOTED) == html


def test_a_link_with_no_previous_sibling_is_kept():
    html = f'<p><a href="{QUOTED}">x</a></p>'
    assert strip_quote_fallback(html, QUOTED) == html


def test_a_paragraph_left_with_an_image_is_kept():
    html = f'<p><img src="https://remote.test/a.png">RE: <a href="{QUOTED}">x</a></p>'
    assert strip_quote_fallback(html, QUOTED) == '<p><img src="https://remote.test/a.png"/></p>'


@pytest.mark.parametrize('html', [
    f'<p>RE: <a href="{QUOTED}">post</a> is a great post</p>',
    f'<p>RE: <a href="{QUOTED}"></a> my whole comment here</p>',
    f'<p>my take</p><p>RE: <a href="{QUOTED}">x</a><span>more author text x</span></p>',
])
def test_text_after_the_link_is_never_removed(html):
    assert strip_quote_fallback(html, QUOTED) == html


def test_whitespace_and_breaks_after_the_link_are_dropped_with_it():
    html = f'<p>my take</p><p><span>RE: <a href="{QUOTED}">x</a></span> <br></p>'
    assert strip_quote_fallback(html, QUOTED) == '<p>my take</p>'


@pytest.mark.parametrize('media', ['<video src="https://remote.test/v.mp4"></video>',
                                   '<audio src="https://remote.test/a.mp3"></audio>',
                                   '<iframe src="https://remote.test/e"></iframe>'])
def test_a_paragraph_left_with_media_is_kept(media):
    html = f'<p>{media}RE: <a href="{QUOTED}">x</a></p>'
    assert strip_quote_fallback(html, QUOTED) == f'<p>{media}</p>'


def test_html_without_a_paragraph_is_kept():
    html = f'<div>RE: <a href="{QUOTED}">x</a></div>'
    assert strip_quote_fallback(html, QUOTED) == html


@pytest.mark.parametrize('quote_id', [None, ''])
def test_no_quote_changes_nothing(quote_id):
    html = f'<p class="quote-inline">RE: <a href="{QUOTED}">x</a></p>'
    assert strip_quote_fallback(html, quote_id) == html


def test_empty_html_is_returned():
    assert strip_quote_fallback('', QUOTED) == ''


def test_link_scan_skips_the_quoted_id():
    html = f'<p><a href="{QUOTED}">q</a> and <a href="https://news.test/a">n</a></p>'
    assert microblog_content_to_link(html, exclude='remote.test', skip=QUOTED) == 'https://news.test/a'


def test_link_scan_with_only_the_quoted_id_is_none():
    assert microblog_content_to_link(f'<p><a href="{QUOTED}">q</a></p>', exclude='remote.test', skip=QUOTED) is None


FALLBACK = f'<p>my take on this</p><p class="quote-inline">RE: <a href="{QUOTED}">{QUOTED}</a></p>'


@pytest.mark.parametrize('media', ['text/html', None])
def test_ingested_microblog_post_has_no_fallback_and_no_quote_url(world, media):
    community, author, _ = world
    extra = {'mediaType': media} if media else {}
    post = Post.new(author, community, _create(_note(7, quote=QUOTED, content=FALLBACK, **extra)))
    assert 'RE:' not in post.body_html and 'RE:' not in post.body
    assert post.url != QUOTED


def test_author_link_to_the_quoted_post_is_not_the_url(world):
    community, author, _ = world
    content = f'<p>my take on <a href="{QUOTED}">this</a></p>'
    post = Post.new(author, community, _create(_note(8, quote=QUOTED, content=content)))
    assert post.url != QUOTED


def test_markdown_source_is_untouched(world):
    community, author, _ = world
    md = f'my take\n\nRE: {QUOTED}'
    post = Post.new(author, community, _create(_note(9, quote=QUOTED, content='<p>x</p>',
                                                     source={'content': md, 'mediaType': 'text/markdown'})))
    assert post.body == md


@pytest.mark.parametrize('media', ['text/html', None])
def test_reply_fallback_is_stripped(world, redis_lock_only_double, media):
    content = '<p>agreed</p>' + FALLBACK.split('</p>', 1)[1]
    reply = _new_reply(world, 10, quote=QUOTED, content=content, **({'mediaType': media} if media else {}))
    assert 'RE:' not in reply.body_html and 'RE:' not in reply.body


@pytest.mark.parametrize('media', ['text/html', None])
def test_post_update_strips_the_fallback(world, redis_lock_only_double, media):
    community, author, _ = world
    post = Post.new(author, community, _create(_note(11)))
    extra = {'mediaType': media} if media else {}
    update_post_from_activity(post, {'id': 'u', 'type': 'Update',
                                     'object': _note(11, quote=QUOTED, content=FALLBACK, **extra)})
    assert 'RE:' not in post.body_html
    assert post.url != QUOTED


@pytest.mark.parametrize('media', ['text/html', None])
def test_reply_update_strips_the_fallback(world, redis_lock_only_double, media):
    _, _, parent = world
    reply = _new_reply(world, 12)
    extra = {'mediaType': media} if media else {}
    update_post_reply_from_activity(reply, {'id': 'u', 'type': 'Update',
                                            'object': _note(12, inReplyTo=parent.ap_id, quote=QUOTED,
                                                            content=FALLBACK, **extra)})
    assert 'RE:' not in reply.body_html


def test_post_update_with_an_author_link_to_the_quote_does_not_make_it_the_url(world, redis_lock_only_double):
    community, author, _ = world
    post = Post.new(author, community, _create(_note(13)))
    content = f'<p>my take on <a href="{QUOTED}">this</a></p>'
    update_post_from_activity(post, {'id': 'u', 'type': 'Update', 'object': _note(13, quote=QUOTED, content=content)})
    assert post.url != QUOTED


NEWS = 'https://news.test/a'


@pytest.fixture
def no_url_probes(monkeypatch):
    """A link post's url is probed for an image or a video; these tests are about which link is chosen."""
    monkeypatch.setattr('app.utils.is_image_url', lambda url: False)
    monkeypatch.setattr('app.utils.is_video_url', lambda url: False)


def _h1(href):
    return f'<h1><a href="{href}">A headline</a></h1><p>my take <a href="{NEWS}">n</a></p>'


def test_new_microblog_post_takes_its_headline_link_as_the_url(world, no_url_probes):
    community, author, _ = world
    post = Post.new(author, community, _create(_note(14, quote=QUOTED, content=_h1(NEWS), mediaType='text/html')))
    assert post.url == NEWS


def test_new_microblog_post_whose_headline_links_the_quote_takes_another_link(world, no_url_probes):
    community, author, _ = world
    post = Post.new(author, community, _create(_note(15, quote=QUOTED, content=_h1(QUOTED), mediaType='text/html')))
    assert post.url == NEWS


def _event_post(world, n):
    """An event with no url: the Update's attachment pass keeps whatever url the microblog branch left on it."""
    community, author, _ = world
    post = Post.new(author, community, _create(_note(n)))
    post.type = POST_TYPE_EVENT
    db.session.commit()
    return post


def test_post_update_takes_its_headline_link_as_the_url(world, redis_lock_only_double):
    post = _event_post(world, 16)
    update_post_from_activity(post, {'id': 'u', 'type': 'Update',
                                     'object': _note(16, quote=QUOTED, content=_h1(NEWS), mediaType='text/html')})
    assert post.url == NEWS


def test_post_update_whose_headline_links_the_quote_does_not_take_it(world, redis_lock_only_double):
    post = _event_post(world, 17)
    update_post_from_activity(post, {'id': 'u', 'type': 'Update',
                                     'object': _note(17, quote=QUOTED, content=_h1(QUOTED), mediaType='text/html')})
    assert post.url != QUOTED


def test_post_view_exposes_the_quote(world):
    from app.api.alpha.views import post_view
    _, _, parent = world
    parent.quote_ap_id = QUOTED
    db.session.commit()
    assert post_view(parent, variant=1)['quote_ap_id'] == QUOTED


def test_post_view_quote_is_none_by_default(world):
    from app.api.alpha.views import post_view
    _, _, parent = world
    assert post_view(parent, variant=1)['quote_ap_id'] is None


def test_a_stub_of_a_hidden_post_does_not_carry_the_quote(world):
    from app.api.alpha.views import post_view
    _, _, parent = world
    parent.quote_ap_id = QUOTED
    parent.visibility = 'followers'
    db.session.commit()
    assert 'quote_ap_id' not in post_view(parent, variant=1, user_id=None)


def test_reply_view_exposes_the_quote(world):
    from app.api.alpha.views import reply_view
    from tests.factories import make_post_reply
    _, author, parent = world
    reply = make_post_reply(parent, author)
    reply.quote_ap_id = QUOTED
    db.session.commit()
    assert reply_view(reply, variant=1)['quote_ap_id'] == QUOTED


def test_reply_view_quote_is_none_by_default(world):
    from app.api.alpha.views import reply_view
    from tests.factories import make_post_reply
    _, author, parent = world
    assert reply_view(make_post_reply(parent, author), variant=1)['quote_ap_id'] is None


def test_a_deleted_posts_view_does_not_carry_the_quote(world):
    from app.api.alpha.views import post_view
    _, _, parent = world
    parent.quote_ap_id = QUOTED
    parent.deleted = True
    db.session.commit()
    view = post_view(parent, variant=1)
    assert view['body'] == '' and view['quote_ap_id'] is None


def test_a_deleted_replys_view_does_not_carry_the_quote(world):
    from app.api.alpha.views import reply_view
    from tests.factories import make_post_reply
    _, author, parent = world
    reply = make_post_reply(parent, author)
    reply.quote_ap_id = QUOTED
    reply.deleted = True
    db.session.commit()
    view = reply_view(reply, variant=1)
    assert view['body'] == '' and view['quote_ap_id'] is None
