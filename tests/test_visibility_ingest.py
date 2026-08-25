import pytest

from tests.factories import make_community, make_instance, make_post, make_site, make_user

FOLLOWERS = 'https://m.example/users/alice/followers'
PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'


@pytest.fixture
def author(db_session):
    return make_user(make_instance('m.example'), 'alice')


@pytest.fixture
def log_spy(monkeypatch):
    """Capture log_incoming_ap calls as (aplog_type, aplog_result, message)."""
    calls = []

    def fake_log(id, aplog_type, aplog_result, saved_json, message=None, session=None):
        calls.append((aplog_type, aplog_result, message))

    monkeypatch.setattr('app.activitypub.util.log_incoming_ap', fake_log)
    return calls


def note_activity(visibility_to, visibility_cc):
    return {
        'id': 'https://m.example/users/alice/statuses/1/activity',
        'type': 'Create',
        'object': {
            'id': 'https://m.example/users/alice/statuses/1',
            'type': 'Note',
            'content': '<p>hello</p>',
            'attributedTo': 'https://m.example/users/alice',
            'to': visibility_to,
            'cc': visibility_cc,
        },
    }


def test_followers_only_post_is_refused(db_session, author, log_spy):
    """A followers-only post is not stored"""
    from app.activitypub.util import create_post
    from app.models import Post
    community = make_community()

    result = create_post(False, community, note_activity([FOLLOWERS], []), author)

    assert result is None
    assert Post.query.count() == 0
    assert 'followers' in log_spy[-1][2]


def test_direct_post_is_refused(db_session, author, log_spy):
    """A direct post is not stored"""
    from app.activitypub.util import create_post
    from app.models import Post
    community = make_community()

    result = create_post(False, community, note_activity(['https://m.example/users/bob'], []), author)

    assert result is None
    assert Post.query.count() == 0
    assert 'direct' in log_spy[-1][2]


def test_public_post_is_accepted(db_session, author, log_spy):
    """A public post is stored"""
    from app.activitypub.util import create_post
    from app.models import Post
    community = make_community()
    make_site()  # Post.new() -> blocked_phrases() looks up Site id 1 unconditionally

    result = create_post(False, community, note_activity([PUBLIC], [FOLLOWERS]), author)

    assert result is not None
    assert Post.query.count() == 1


def test_unlisted_post_is_accepted(db_session, author, log_spy):
    """An unlisted post is stored"""
    from app.activitypub.util import create_post
    from app.models import Post
    community = make_community()
    make_site()  # Post.new() -> blocked_phrases() looks up Site id 1 unconditionally

    result = create_post(False, community, note_activity([FOLLOWERS], [PUBLIC]), author)

    assert result is not None
    assert Post.query.count() == 1


def test_followers_only_reply_is_refused(db_session, author, log_spy):
    """A followers-only reply is not stored"""
    from app.activitypub.util import create_post_reply
    from app.models import PostReply
    community = make_community()
    parent = make_post(community, author, 'https://m.example/users/alice/statuses/9')

    activity = note_activity([FOLLOWERS], [])
    activity['object']['inReplyTo'] = parent.ap_id

    result = create_post_reply(False, community, parent.ap_id, activity, author)

    assert result is None
    assert PostReply.query.count() == 0
    assert 'followers' in log_spy[-1][2]


def test_direct_reply_is_refused(db_session, author, log_spy):
    """A direct reply is not stored"""
    from app.activitypub.util import create_post_reply
    from app.models import PostReply
    community = make_community()
    parent = make_post(community, author, 'https://m.example/users/alice/statuses/9')

    activity = note_activity(['https://m.example/users/bob'], [])
    activity['object']['inReplyTo'] = parent.ap_id

    result = create_post_reply(False, community, parent.ap_id, activity, author)

    assert result is None
    assert PostReply.query.count() == 0
    assert 'direct' in log_spy[-1][2]


def test_post_and_reply_refusal_reasons_differ(db_session, author, log_spy):
    """Refusal reasons distinguish posts from replies, so logs are diagnosable"""
    from app.activitypub.util import create_post, create_post_reply
    community = make_community()
    parent = make_post(community, author, 'https://m.example/users/alice/statuses/9')

    create_post(False, community, note_activity([FOLLOWERS], []), author)
    post_reason = log_spy[-1][2]

    activity = note_activity([FOLLOWERS], [])
    activity['object']['inReplyTo'] = parent.ap_id
    create_post_reply(False, community, parent.ap_id, activity, author)
    reply_reason = log_spy[-1][2]

    assert post_reason != reply_reason


def test_direct_message_path_is_unaffected(db_session, author):
    """DMs are handled by process_chat before create_post, so refusal cannot break them.

    Guards the ordering: if a future change moves the Create dispatch above
    process_chat, direct messages would start being refused instead of delivered.
    """
    import inspect
    from app.activitypub import routes

    source = inspect.getsource(routes.process_inbox_request)
    chat_at = source.index('process_chat')
    content_at = source.index('process_new_content(user')
    assert chat_at < content_at, 'process_chat must be reached before process_new_content'
