"""G2: replies honour mediaType text/markdown (PeerTube comments)."""
from tests.factories import make_community, make_instance, make_post, make_site, make_user

PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'


def _reply_activity(parent, **fields):
    obj = {'id': 'https://m.example/c/1', 'type': 'Note', 'attributedTo': 'https://m.example/users/alice',
           'inReplyTo': parent.ap_id, 'to': [PUBLIC], 'cc': []}
    obj.update(fields)
    return {'id': 'https://m.example/c/1/activity', 'type': 'Create', 'object': obj}


def _setup():
    make_site()
    author = make_user(make_instance('m.example'), 'alice')
    community = make_community()
    parent = make_post(community, author, 'https://m.example/users/alice/statuses/9')
    return community, parent, author


def test_a_markdown_reply_is_rendered(db_session):
    """PeerTube comments: mediaType text/markdown with no `source`"""
    from app.activitypub.util import create_post_reply
    community, parent, author = _setup()
    activity = _reply_activity(parent, content='**thanks**', mediaType='text/markdown')

    reply = create_post_reply(False, community, parent.ap_id, activity, author)

    assert '<strong>thanks</strong>' in reply.body_html
    assert reply.body == '**thanks**'


def test_an_html_reply_without_a_media_type_is_unchanged(db_session):
    from app.activitypub.util import create_post_reply
    community, parent, author = _setup()
    activity = _reply_activity(parent, content='<p>**thanks**</p>')

    reply = create_post_reply(False, community, parent.ap_id, activity, author)

    assert reply.body_html == '<p>**thanks**</p>'


def test_a_markdown_reply_update_is_rendered(db_session):
    from app.activitypub.util import create_post_reply, update_post_reply_from_activity
    community, parent, author = _setup()
    reply = create_post_reply(False, community, parent.ap_id, _reply_activity(parent, content='<p>hi</p>'), author)
    update = _reply_activity(parent, content='*edited*', mediaType='text/markdown')
    update['type'] = 'Update'

    update_post_reply_from_activity(reply, update)

    assert '<em>edited</em>' in reply.body_html
