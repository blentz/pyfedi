"""S3.3: a deleted post's Tombstone names the type the post was federated as (spec 2026-10-10)."""
from datetime import timedelta

from app import db
from app.constants import POST_TYPE_EVENT, POST_TYPE_POLL
from app.models import Event
from app.utils import utcnow
from tests.factories import make_poll, make_post_reply
from tests.test_ap_content_objects import ap_get, seed_local_post


def _deleted_post(post_type=None):
    community, author, post = seed_local_post()
    post.ap_id = f'https://test.piefed.local/post/{post.id}'
    if post_type is not None:
        post.type = post_type
    post.deleted = True
    db.session.commit()
    return post


def test_a_deleted_plain_post_is_a_page_tombstone(app, db_session):
    post = _deleted_post()

    response = ap_get(app, f'/post/{post.id}')

    assert response.status_code == 410
    assert response.json['type'] == 'Tombstone'
    assert response.json['formerType'] == 'Page'
    assert response.json['id'] == post.ap_id


def test_a_deleted_poll_is_a_question_tombstone(app, db_session):
    post = _deleted_post(POST_TYPE_POLL)
    make_poll(post, end_poll=utcnow() + timedelta(days=1))

    assert ap_get(app, f'/post/{post.id}').json['formerType'] == 'Question'


def test_a_deleted_poll_with_no_end_was_federated_as_a_page(app, db_session):
    """post_to_page sends a poll without end_poll as a Page (D1330), so its Tombstone says Page."""
    post = _deleted_post(POST_TYPE_POLL)
    make_poll(post)

    assert ap_get(app, f'/post/{post.id}').json['formerType'] == 'Page'


def test_a_deleted_event_is_an_event_tombstone(app, db_session):
    post = _deleted_post(POST_TYPE_EVENT)
    db.session.add(Event(post_id=post.id, start=utcnow() + timedelta(days=2)))
    db.session.commit()

    assert ap_get(app, f'/post/{post.id}').json['formerType'] == 'Event'


def test_a_deleted_comment_is_still_a_note_tombstone(app, db_session):
    community, author, post = seed_local_post()
    reply = make_post_reply(post, author)
    reply.deleted = True
    db.session.commit()

    response = ap_get(app, f'/comment/{reply.id}')

    assert response.status_code == 410
    assert response.json['formerType'] == 'Note'
