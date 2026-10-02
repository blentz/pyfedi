"""Mastodon and Pixelfed send a content warning as `summary` (+ `sensitive`). It is
kept on Post and PostReply, shown as a collapsed <details> on the post page, in
place of the body preview in teasers, and sent back out as `summary`."""
import pytest

from app import db
from app.models import Language, Site
from tests.factories import (make_community, make_community_member, make_instance,
                             make_post, make_site, make_user)

PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'


def activity(object_type='Note', **extra):
    obj = {
        'id': 'https://m.example/users/alice/statuses/1',
        'type': object_type,
        'content': '<p>the secret body</p>',
        'attributedTo': 'https://m.example/users/alice',
        'to': [PUBLIC],
        'cc': [],
        **extra,
    }
    return {'id': 'https://m.example/users/alice/statuses/1/activity', 'type': 'Create', 'object': obj}


@pytest.fixture
def author(db_session):
    return make_user(make_instance('m.example'), 'alice')


@pytest.fixture
def community(db_session, author):
    make_site()
    community = make_community()
    make_community_member(author, community)
    return community


def ingest(community, author, act):
    from app.activitypub.util import create_post
    return create_post(False, community, act, author)


def public_site():
    db.session.get(Site, 1).private_instance = False
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.commit()


def test_note_summary_is_stored_as_the_content_warning(community, author):
    post = ingest(community, author, activity(summary='<p>spoilers &amp; <b>more</b></p>', sensitive=True))
    assert post.content_warning == 'spoilers & more'
    assert post.nsfw is True


def test_note_summary_without_sensitive_is_still_a_content_warning(community, author):
    assert ingest(community, author, activity(summary='politics')).content_warning == 'politics'


def test_page_summary_without_sensitive_is_not_a_content_warning(community, author):
    post = ingest(community, author, activity('Page', name='A title', summary='a description'))
    assert post.content_warning is None


def test_page_summary_with_sensitive_is_a_content_warning(community, author):
    post = ingest(community, author, activity('Page', name='A title', summary='gore', sensitive=True))
    assert post.content_warning == 'gore'


def test_blank_summary_is_no_content_warning(community, author):
    assert ingest(community, author, activity(summary='  <p> </p> ')).content_warning is None


def test_content_warning_is_shortened(community, author):
    assert len(ingest(community, author, activity(summary='x' * 2000)).content_warning) <= 500


def test_update_changes_and_clears_the_content_warning(community, author):
    from app.activitypub.util import update_post_from_activity
    post = ingest(community, author, activity(summary='first'))
    update = activity(summary='second')
    update['type'] = 'Update'
    update_post_from_activity(post, update)
    db.session.refresh(post)
    assert post.content_warning == 'second'
    update = activity()
    update['type'] = 'Update'
    update_post_from_activity(post, update)
    db.session.refresh(post)
    assert post.content_warning is None


def reply_to(community, author, **extra):
    from app.activitypub.util import create_post_reply
    parent = make_post(community, author, 'https://m.example/users/alice/statuses/9')
    act = activity(inReplyTo=parent.ap_id, **extra)
    act['object']['id'] = 'https://m.example/users/alice/statuses/2'
    return parent, create_post_reply(False, community, parent.ap_id, act, author)


def test_reply_summary_is_stored_as_the_content_warning(community, author):
    _, reply = reply_to(community, author, summary='<p>heavy</p>', sensitive=True)
    assert reply.content_warning == 'heavy'


def test_reply_update_changes_the_content_warning(community, author):
    from app.activitypub.util import update_post_reply_from_activity
    _, reply = reply_to(community, author, summary='heavy')
    update = activity(summary='lighter')
    update['type'] = 'Update'
    update_post_reply_from_activity(reply, update)
    db.session.refresh(reply)
    assert reply.content_warning == 'lighter'


def test_post_page_collapses_body_and_image_under_the_warning(app, community, author):
    from tests.test_gallery import a_jpeg
    import respx
    public_site()
    with respx.mock(assert_all_called=False) as router:
        router.route(host='m.example').respond(200, headers={'Content-Type': 'image/jpeg'}, content=a_jpeg())
        post = ingest(community, author, activity(
            summary='look away',
            attachment=[{'type': 'Document', 'mediaType': 'image/jpeg', 'url': 'https://m.example/a.jpg', 'name': 'a'},
                        {'type': 'Document', 'mediaType': 'image/jpeg', 'url': 'https://m.example/b.jpg', 'name': 'b'}]))
    html = app.test_client().get(f'/post/{post.id}', follow_redirects=True).get_data(as_text=True)
    start = html.index('<details class="content_warning"')
    end = html.index('</details>', start)
    inside = html[start:end]
    assert '<summary>look away</summary>' in inside
    assert 'the secret body' in inside
    assert 'post_gallery_image' in inside  # the album's later images are collapsed too


def test_post_without_a_warning_has_no_details(app, community, author):
    public_site()
    post = ingest(community, author, activity())
    html = app.test_client().get(f'/post/{post.id}', follow_redirects=True).get_data(as_text=True)
    assert 'content_warning' not in html


def test_reply_on_the_page_collapses_under_the_warning(app, community, author):
    public_site()
    parent, _ = reply_to(community, author, summary='reply warning')
    html = app.test_client().get(f'/post/{parent.id}', follow_redirects=True).get_data(as_text=True)
    start = html.index('<details class="content_warning"')
    inside = html[start:html.index('</details>', start)]
    assert '<summary>reply warning</summary>' in inside
    assert 'the secret body' in inside


def test_teaser_shows_the_warning_instead_of_the_body(app, community, author):
    public_site()
    post = ingest(community, author, activity(content='<p>the secret body</p><p>more words here</p>', summary='teaser warning'))
    html = app.test_client().get(f'/c/{post.community.link()}').get_data(as_text=True)
    assert 'teaser warning' in html
    assert 'the secret body' not in html


def test_outbound_post_carries_the_summary(community, author):
    from app.activitypub.util import post_to_page
    post = ingest(community, author, activity(summary='outbound'))
    page = post_to_page(post)
    assert page['summary'] == 'outbound'
    assert page['sensitive'] is True


def test_outbound_post_without_a_warning_has_no_summary(community, author):
    from app.activitypub.util import post_to_page
    assert 'summary' not in post_to_page(ingest(community, author, activity()))


def test_outbound_reply_carries_the_summary(community, author):
    from app.activitypub.util import comment_model_to_json
    _, reply = reply_to(community, author, summary='reply out')
    note = comment_model_to_json(reply)
    assert note['summary'] == 'reply out'
    assert note['sensitive'] is True
