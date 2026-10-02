"""PostReply.new() stores the object-level visibility and nothing reads or writes
the old followers-only `private` marker (the to[0] rule was dead for
followers-only content, which create_post_reply refuses before reaching it).
The column stays; only the reads and the write are retired.
"""
import inspect

from tests.factories import make_community, make_instance, make_post, make_site, make_user

FOLLOWERS = 'https://remote.example/users/alice/followers'


def test_new_stores_followers_visibility_from_object_addressing(db_session):
    from app.models import PostReply

    make_site()
    instance = make_instance('remote.example')
    author = make_user(instance, 'alice')
    community = make_community('microblogs')
    post = make_post(community, author, 'https://remote.example/posts/1', title='', microblog=True)

    request_json = {
        'id': 'https://remote.example/activities/create/9',
        'to': [FOLLOWERS],
        'object': {'id': 'https://remote.example/comments/9', 'type': 'Note',
                   'to': [FOLLOWERS], 'cc': []},
    }
    reply = PostReply.new(author, post, None, 'hi', '<p>hi</p>', True, None, False, False, request_json=request_json)

    assert reply.visibility == 'followers'


def test_new_no_longer_writes_private():
    from app.models import PostReply

    source = inspect.getsource(PostReply.new)
    assert 'private=private' not in source
    assert "endswith('/followers')" not in source
