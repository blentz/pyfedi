"""The user API's reading half: one person, the person list, the replies and
mentions feeds, uploaded media, and the unread counters.

Sub-project 84, slice D -- `app/api/alpha/utils/user.py`. One defect,
measured:

* `get_user_replies`'s "everything, read and unread" branch appended
  `result[0]['comment_id']` with none of the membership test its sibling
  makes, so one notification of the right subtype carrying no `comment_id`
  broke the whole endpoint with `KeyError: 'comment_id'` (D1184).
"""
import pytest
from flask import current_app, g

from app import db
from app.constants import (NOTIF_MENTION, NOTIF_POST, NOTIF_REPLY,
                           POST_STATUS_PUBLISHED)
from app.models import (ChatMessage, Conversation, File, Notification,
                        PostReply, Site, User, user_file)
from tests.factories import (make_chat_message, make_community,
                             make_community_member, make_conversation,
                             make_file, make_post, make_post_reply, make_user)


def token(user):
    return f'Bearer {user.encode_jwt_token()}'


def a_notification(reader, author, subtype, targets, read=False,
                   notif_type=NOTIF_REPLY):
    notification = Notification(title='something', url='/', user_id=reader.id,
                                author_id=author.id, notif_type=notif_type,
                                subtype=subtype, read=read, targets=targets)
    db.session.add(notification)
    db.session.commit()
    return notification


@pytest.fixture
def env(app, api_baseline):
    """`reader` is the account asking; `author` wrote the comment it is being
    told about."""
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    reader = api_baseline.user2
    author = api_baseline.user3
    community = api_baseline.community1
    post = api_baseline.post1
    reply = make_post_reply(post, author, body='a reply')
    reply.body_html = '<p>a reply</p>'
    db.session.commit()
    return reader, author, reply, api_baseline


# --------------------------------------------------------------------------
# D1184 -- a notification carrying no comment id
# --------------------------------------------------------------------------


def test_the_replies_feed_carries_what_you_were_told_about(app, env):
    from app.api.alpha.utils.user import get_user_replies

    reader, author, reply, baseline = env
    a_notification(reader, author, 'new_reply_on_followed_comment',
                   {'gen': '0', 'comment_id': reply.id})

    answer = get_user_replies(token(reader), {})

    assert [r['comment']['id'] for r in answer['replies']] == [reply.id]


def test_a_notification_with_no_comment_id_does_not_break_the_feed(app, env):
    """D1184. The read/unread branch appended `result[0]['comment_id']` with
    none of the membership test its sibling makes, and `targets` is a
    free-form JSON dict that several subtypes fill differently. Measured:
    `PROBE bg3 outcome: KeyError: 'comment_id'`."""
    from app.api.alpha.utils.user import get_user_replies

    reader, author, reply, baseline = env
    a_notification(reader, author, 'new_reply_on_followed_comment',
                   {'gen': '0', 'comment_id': reply.id}, read=True)
    a_notification(reader, author, 'new_reply_on_followed_comment',
                   {'gen': '0', 'post_id': 1}, read=True)

    answer = get_user_replies(token(reader), {'unread_only': False})

    assert [r['comment']['id'] for r in answer['replies']] == [reply.id]


def test_a_mention_with_no_comment_id_does_not_break_it_either(app, env):
    """D1184's twin, on the mentions query."""
    from app.api.alpha.utils.user import get_user_replies

    reader, author, reply, baseline = env
    a_notification(reader, author, 'comment_mention',
                   {'gen': '0', 'post_id': 1}, read=True,
                   notif_type=NOTIF_MENTION)

    answer = get_user_replies(token(reader), {'unread_only': False},
                              mentions=True)

    assert answer['replies'] == []


def test_the_unread_feed_leaves_out_what_has_been_read(app, env):
    from app.api.alpha.utils.user import get_user_replies

    reader, author, reply, baseline = env
    a_notification(reader, author, 'new_reply_on_followed_comment',
                   {'gen': '0', 'comment_id': reply.id}, read=True)

    assert get_user_replies(token(reader), {})['replies'] == []
    assert len(get_user_replies(token(reader),
                                {'unread_only': False})['replies']) == 1


def test_an_unread_notification_with_no_comment_id_is_skipped(app, env):
    """The sibling branch, which always had the test."""
    from app.api.alpha.utils.user import get_user_replies

    reader, author, reply, baseline = env
    a_notification(reader, author, 'new_reply_on_followed_comment',
                   {'gen': '0', 'post_id': 1})

    assert get_user_replies(token(reader), {})['replies'] == []


def test_the_mentions_feed_is_its_own_query(app, env):
    from app.api.alpha.utils.user import get_user_replies

    reader, author, reply, baseline = env
    a_notification(reader, author, 'comment_mention',
                   {'gen': '0', 'comment_id': reply.id},
                   notif_type=NOTIF_MENTION)

    assert [r['comment']['id']
            for r in get_user_replies(token(reader), {},
                                      mentions=True)['replies']] == [reply.id]
    assert get_user_replies(token(reader), {})['replies'] == []


def test_a_top_level_comment_on_a_followed_post_is_a_reply_too(app, env):
    from app.api.alpha.utils.user import get_user_replies

    reader, author, reply, baseline = env
    a_notification(reader, author, 'top_level_comment_on_followed_post',
                   {'gen': '0', 'comment_id': reply.id},
                   notif_type=NOTIF_POST)

    assert len(get_user_replies(token(reader), {})['replies']) == 1


@pytest.mark.parametrize('sort,first', [('Hot', 'ranked'), ('Top', 'voted'),
                                        ('Old', 'older'), ('New', 'newer')])
def test_the_replies_feed_sorts(app, env, sort, first):
    """Each sort puts a different comment at the top, so a row asserting only
    the count cannot tell them apart."""
    from datetime import timedelta

    from app.api.alpha.utils.user import get_user_replies
    from app.utils import utcnow

    reader, author, reply, baseline = env
    made = {}
    for name in ('ranked', 'voted', 'older', 'newer'):
        one = make_post_reply(baseline.post1, author, body=name)
        one.body_html = f'<p>{name}</p>'
        one.ranking = 100 if name == 'ranked' else 1
        one.up_votes = 100 if name == 'voted' else 1
        one.down_votes = 0
        one.posted_at = utcnow() - timedelta(days=10 if name == 'older' else 1)
        if name == 'newer':
            one.posted_at = utcnow()
        made[name] = one
        db.session.commit()
        a_notification(reader, author, 'new_reply_on_followed_comment',
                       {'gen': '0', 'comment_id': one.id})

    answer = get_user_replies(token(reader), {'sort': sort})

    assert answer['replies'][0]['comment']['id'] == made[first].id


def test_the_replies_feed_is_paged(app, env):
    from app.api.alpha.utils.user import get_user_replies

    reader, author, reply, baseline = env
    for number in range(3):
        one = make_post_reply(baseline.post1, author, body=f'reply {number}')
        one.body_html = f'<p>reply {number}</p>'
        db.session.commit()
        a_notification(reader, author, 'new_reply_on_followed_comment',
                       {'gen': '0', 'comment_id': one.id})

    answer = get_user_replies(token(reader), {'limit': 2})

    assert len(answer['replies']) == 2
    assert answer['next_page'] == '2'


def test_the_replies_page_length_is_capped(app, env):
    from app.api.alpha.utils.user import get_user_replies

    reader, author, reply, baseline = env
    for number in range(3):
        one = make_post_reply(baseline.post1, author, body=f'reply {number}')
        one.body_html = f'<p>reply {number}</p>'
        db.session.commit()
        a_notification(reader, author, 'new_reply_on_followed_comment',
                       {'gen': '0', 'comment_id': one.id})

    with pytest.MonkeyPatch.context() as patched:
        patched.setitem(app.config, 'PAGE_LENGTH', 2)
        answer = get_user_replies(token(reader), {'limit': 100})

    assert len(answer['replies']) == 2


# --------------------------------------------------------------------------
# One person
# --------------------------------------------------------------------------


def test_a_person_is_read_by_id(app, env):
    from app.api.alpha.utils.user import get_user

    reader, author, reply, baseline = env

    answer = get_user(token(reader), {'person_id': author.id})

    assert answer['person_view']['person']['id'] == author.id
    assert answer['posts'] == []
    assert answer['comments'] == []


def test_a_local_person_is_read_by_bare_name(app, env):
    """A local account's `ap_domain` is NULL, and the filter compares
    `func.lower(User.ap_domain)` against None -- which SQLAlchemy renders as
    IS NULL, so the lookup resolves."""
    from app.api.alpha.utils.user import get_user

    reader, author, reply, baseline = env

    answer = get_user(token(reader), {'username': author.user_name.upper()})

    assert answer['person_view']['person']['id'] == author.id


def test_a_local_person_is_read_by_full_name(app, env):
    """`name@this-instance` is the same person: the domain is dropped when it
    is ours."""
    from app.api.alpha.utils.user import get_user

    reader, author, reply, baseline = env
    name = f"{author.user_name}@{current_app.config['SERVER_NAME']}"

    assert get_user(token(reader), {'username': name})[
        'person_view']['person']['id'] == author.id


def test_a_remote_person_is_read_by_their_full_name(app, env):
    from app.api.alpha.utils.user import get_user

    reader, author, reply, baseline = env
    remote = make_user(baseline.instance_remote, 'faraway')
    remote.ap_domain = baseline.instance_remote.domain
    db.session.commit()

    answer = get_user(token(reader),
                      {'username': f'faraway@{remote.ap_domain}'})

    assert answer['person_view']['person']['id'] == remote.id


def test_a_deleted_account_cannot_be_found_by_name(app, env):
    """`User.deleted == False` in the lookup: a deleted account is gone from
    the by-name path, not merely hidden in the list."""
    from app.api.alpha.utils.user import get_user

    reader, author, reply, baseline = env
    author.deleted = True
    db.session.commit()

    with pytest.raises(Exception):
        get_user(token(reader), {'username': author.user_name})


def test_asking_for_nobody_is_refused(app, env):
    from app.api.alpha.utils.user import get_user

    reader, author, reply, baseline = env

    with pytest.raises(Exception) as refused:
        get_user(token(reader), {})

    assert str(refused.value) == 'person_id or username required'


def test_a_person_can_be_read_without_logging_in(app, env):
    from app.api.alpha.utils.user import get_user

    reader, author, reply, baseline = env

    answer = get_user(None, {'person_id': author.id})

    assert answer['person_view']['person']['id'] == author.id


def test_a_persons_content_comes_with_them_when_it_is_asked_for(app, env):
    from app.api.alpha.utils.user import get_user

    reader, author, reply, baseline = env

    answer = get_user(token(reader), {'person_id': author.id,
                                      'include_content': True})

    assert [c['comment']['id'] for c in answer['comments']] == [reply.id]
    # `author` wrote the comment; the baseline's posts are `reader`'s.
    theirs = get_user(token(author), {'person_id': reader.id,
                                      'include_content': True})
    assert {p['post']['id'] for p in theirs['posts']} == {baseline.post1.id,
                                                          baseline.post2.id}


def test_saved_only_answers_with_what_this_account_saved(app, env):
    """`saved_only` drops the person filter, because what is being asked for
    is the CALLER's bookmarks rather than anybody's authorship."""
    from app.api.alpha.utils.user import get_user

    reader, author, reply, baseline = env

    answer = get_user(token(reader), {'person_id': author.id,
                                      'saved_only': True})

    assert answer['posts'] == []


# --------------------------------------------------------------------------
# The person list
# --------------------------------------------------------------------------


def test_the_person_list_carries_the_local_accounts(app, env):
    from app.api.alpha.utils.user import get_user_list

    reader, author, reply, baseline = env

    remote = make_user(baseline.instance_remote, 'faraway')
    db.session.commit()

    listed = get_user_list(token(reader), {'type_': 'Local'})

    listed_ids = [u['person']['id'] for u in listed['users']]
    assert baseline.user1.id in listed_ids
    assert remote.id not in listed_ids


def test_the_person_list_can_be_searched(app, env):
    from app.api.alpha.utils.user import get_user_list

    reader, author, reply, baseline = env

    listed = get_user_list(token(reader), {'q': author.user_name})

    assert [u['person']['id'] for u in listed['users']] == [author.id]


def test_the_person_list_searches_remote_names_by_their_domain(app, env):
    from app.api.alpha.utils.user import get_user_list

    reader, author, reply, baseline = env
    remote = make_user(baseline.instance_remote, 'faraway')
    db.session.commit()

    listed = get_user_list(token(reader),
                           {'q': f'@{baseline.instance_remote.domain}'})

    assert remote.id in [u['person']['id'] for u in listed['users']]


@pytest.mark.parametrize('sort,first', [('New', 'newest'), ('Old', 'oldest'),
                                        ('TopAll', 'prolific')])
def test_the_person_list_sorts(app, env, sort, first):
    from datetime import timedelta

    from app.api.alpha.utils.user import get_user_list
    from app.utils import utcnow

    reader, author, reply, baseline = env
    made = {}
    for name in ('newest', 'oldest', 'prolific'):
        one = make_user(baseline.instance_local, name, local=True)
        one.created = utcnow() - timedelta(days={'newest': 0, 'oldest': 900,
                                                 'prolific': 30}[name])
        one.post_count = 500 if name == 'prolific' else 0
        made[name] = one
    for existing in (reader, author, baseline.user1):
        existing.created = utcnow() - timedelta(days=100)
        existing.post_count = 1
    db.session.commit()

    listed = get_user_list(token(reader), {'sort': sort})

    assert listed['users'][0]['person']['id'] == made[first].id


def test_an_unknown_sort_falls_back_to_the_oldest_id(app, env):
    from app.api.alpha.utils.user import get_user_list

    reader, author, reply, baseline = env

    listed = get_user_list(token(reader), {'sort': 'Nonsense'})

    assert listed['users'][0]['person']['id'] == baseline.user1.id


def test_the_person_list_is_paged_and_capped(app, env):
    from app.api.alpha.utils.user import get_user_list

    reader, author, reply, baseline = env

    with pytest.MonkeyPatch.context() as patched:
        patched.setitem(app.config, 'PAGE_LENGTH', 2)
        listed = get_user_list(token(reader), {'limit': 100})

    assert len(listed['users']) == 2
    assert listed['next_page'] == '2'


def test_the_person_list_can_be_read_without_logging_in(app, env):
    from app.api.alpha.utils.user import get_user_list

    reader, author, reply, baseline = env

    assert get_user_list(None, {})['users'] != []


def test_a_deleted_account_is_not_listed(app, env):
    from app.api.alpha.utils.user import get_user_list

    reader, author, reply, baseline = env
    author.deleted = True
    db.session.commit()

    listed = get_user_list(token(reader), {})

    assert author.id not in [u['person']['id'] for u in listed['users']]


# --------------------------------------------------------------------------
# Media
# --------------------------------------------------------------------------


def test_the_media_list_carries_this_accounts_uploads(app, env):
    from app.api.alpha.utils.user import get_user_media

    reader, author, reply, baseline = env
    # The stored name differs from the url's last segment, or a row cannot
    # tell `file.file_name` from the fallback that derives one from the url.
    file = make_file(file_path='uploads/a.png',
                     source_url='https://test.piefed.local/uploads/stored.png')
    file.file_name = 'what they called it.png'
    db.session.execute(user_file.insert().values(user_id=reader.id,
                                                 file_id=file.id))
    somebody_elses = make_file(
        source_url='https://test.piefed.local/uploads/theirs.png')
    db.session.execute(user_file.insert().values(user_id=author.id,
                                                 file_id=somebody_elses.id))
    db.session.commit()

    answer = get_user_media(token(reader), {})

    assert answer['media'] == [
        {'name': 'what they called it.png',
         'url': 'https://test.piefed.local/uploads/stored.png'}]


def test_a_file_with_no_name_is_named_after_its_url(app, env):
    from app.api.alpha.utils.user import get_user_media

    reader, author, reply, baseline = env
    file = make_file(source_url='https://test.piefed.local/uploads/b.png')
    file.file_name = None
    db.session.execute(user_file.insert().values(user_id=reader.id,
                                                 file_id=file.id))
    db.session.commit()

    assert get_user_media(token(reader), {})['media'][0]['name'] == 'b.png'


def test_the_media_list_is_paged(app, env):
    from app.api.alpha.utils.user import get_user_media

    reader, author, reply, baseline = env
    for number in range(3):
        file = make_file(source_url=f'https://test.piefed.local/u/{number}.png')
        db.session.execute(user_file.insert().values(user_id=reader.id,
                                                     file_id=file.id))
    db.session.commit()

    answer = get_user_media(token(reader), {'limit': 2})

    assert len(answer['media']) == 2
    assert answer['next_page'] == '2'


def test_the_media_page_length_is_capped(app, env):
    from app.api.alpha.utils.user import get_user_media

    reader, author, reply, baseline = env
    for number in range(3):
        file = make_file(source_url=f'https://test.piefed.local/u/{number}.png')
        db.session.execute(user_file.insert().values(user_id=reader.id,
                                                     file_id=file.id))
    db.session.commit()

    with pytest.MonkeyPatch.context() as patched:
        patched.setitem(app.config, 'PAGE_LENGTH', 2)
        answer = get_user_media(token(reader), {'limit': 100})

    assert len(answer['media']) == 2


def test_the_media_list_needs_somebody_to_ask_for_it(app, env):
    """A bearer token that does not authorise falls back to the web session,
    and an anonymous caller has neither."""
    from app.api.alpha.utils.user import get_user_media

    reader, author, reply, baseline = env

    with app.test_request_context('/'):
        with pytest.raises(Exception) as refused:
            get_user_media('Bearer not-a-jwt', {})

    assert str(refused.value) == 'incorrect_login'


def test_a_web_session_stands_in_for_a_token(app, env):
    """The fallback: a caller inside a logged-in web session gets their own
    media even when the Authorization header is unusable."""
    from flask_login import login_user

    from app.api.alpha.utils.user import get_user_media

    reader, author, reply, baseline = env
    file = make_file(source_url='https://test.piefed.local/uploads/c.png')
    db.session.execute(user_file.insert().values(user_id=reader.id,
                                                 file_id=file.id))
    db.session.commit()

    with app.test_request_context('/'):
        login_user(reader)
        answer = get_user_media('Bearer not-a-jwt', {})

    assert len(answer['media']) == 1


# --------------------------------------------------------------------------
# The counters
# --------------------------------------------------------------------------


def test_nothing_unread_is_all_zeroes(app, env):
    from app.api.alpha.utils.user import get_user_unread_count

    reader, author, reply, baseline = env

    assert get_user_unread_count(token(reader)) == {'replies': 0,
                                                    'mentions': 0,
                                                    'private_messages': 0,
                                                    'other': 0}


def test_the_counters_count_each_kind(app, env):
    from app.api.alpha.utils.user import get_user_unread_count

    reader, author, reply, baseline = env
    a_notification(reader, author, 'new_reply_on_followed_comment',
                   {'comment_id': reply.id})
    a_notification(reader, author, 'top_level_comment_on_followed_post',
                   {'comment_id': reply.id}, notif_type=NOTIF_POST)
    a_notification(reader, author, 'comment_mention', {'comment_id': reply.id},
                   notif_type=NOTIF_MENTION)
    a_notification(reader, author, 'post_mention', {'post_id': 1},
                   notif_type=NOTIF_MENTION)
    a_notification(reader, author, 'something_else', {}, notif_type=NOTIF_POST)
    reader.unread_notifications = 5
    conversation = make_conversation(author, reader)
    message = make_chat_message(author, reader, 'https://test.piefed.local/m/1')
    message.conversation_id = conversation.id
    message.read = False
    db.session.commit()

    counts = get_user_unread_count(token(reader))

    assert counts['replies'] == 2
    assert counts['mentions'] == 2
    assert counts['private_messages'] == 1
    assert counts['other'] == 0


def test_marking_everything_read_clears_all_of_it(app, env):
    from app.api.alpha.utils.user import post_user_mark_all_as_read

    reader, author, reply, baseline = env
    notification = a_notification(reader, author,
                                  'new_reply_on_followed_comment',
                                  {'comment_id': reply.id})
    reader.unread_notifications = 1
    conversation = make_conversation(author, reader)
    conversation.read = False
    message = make_chat_message(author, reader, 'https://test.piefed.local/m/1')
    message.conversation_id = conversation.id
    message.read = False
    db.session.commit()

    assert post_user_mark_all_as_read(token(reader)) == {'replies': []}

    assert notification.read is True
    assert reader.unread_notifications == 0
    assert message.read is True
    assert conversation.read is True


def test_the_details_endpoint_answers_about_the_caller(app, env):
    from app.api.alpha.utils.user import get_user_details

    reader, author, reply, baseline = env

    answer = get_user_details(token(reader))

    assert answer['local_user_view']['person']['id'] == reader.id


def test_a_caller_may_name_their_own_page_length(app, env):
    """`limit` defaults to 20 for this endpoint and is passed through to the
    post and comment lists it builds."""
    from app.api.alpha.utils.user import get_user

    reader, author, reply, baseline = env

    answer = get_user(token(author), {'person_id': reader.id,
                                      'include_content': True, 'limit': 1})

    assert len(answer['posts']) == 1


@pytest.mark.parametrize('effect,expected', [(1, 1), (-1, -1)])
def test_the_replies_feed_carries_how_you_voted(app, env, effect, expected):
    from app.api.alpha.utils.user import get_user_replies
    from tests.factories import make_post_reply_vote

    reader, author, reply, baseline = env
    make_post_reply_vote(reader, reply, effect)
    db.session.commit()
    a_notification(reader, author, 'new_reply_on_followed_comment',
                   {'gen': '0', 'comment_id': reply.id})

    answer = get_user_replies(token(reader), {})

    assert answer['replies'][0]['my_vote'] == expected


def test_the_saved_list_is_not_narrowed_to_the_person_asked_about(app, env):
    """`saved_only` drops `person_id` from the query it hands on, because
    what is being asked for is this account's bookmarks -- which are usually
    somebody else's posts."""
    from app.api.alpha.utils.user import get_user
    from app.models import PostBookmark

    reader, author, reply, baseline = env
    db.session.add(PostBookmark(user_id=reader.id, post_id=baseline.post1.id))
    db.session.commit()

    answer = get_user(token(reader), {'person_id': author.id,
                                      'saved_only': True})

    assert [p['post']['id'] for p in answer['posts']] == [baseline.post1.id]


def test_a_reply_already_read_is_marked_as_read_in_the_feed(app, env):
    """The read/unread branch collects the ids whose notification is read, and
    the comment_reply half of each row carries that back."""
    from app.api.alpha.utils.user import get_user_replies

    reader, author, reply, baseline = env
    other = make_post_reply(baseline.post1, author, body='unread one')
    other.body_html = '<p>unread one</p>'
    db.session.commit()
    a_notification(reader, author, 'new_reply_on_followed_comment',
                   {'gen': '0', 'comment_id': reply.id}, read=True)
    a_notification(reader, author, 'new_reply_on_followed_comment',
                   {'gen': '0', 'comment_id': other.id}, read=False)

    answer = get_user_replies(token(reader), {'unread_only': False})

    read_by_id = {r['comment']['id']: r['comment_reply']['read']
                  for r in answer['replies']}
    assert read_by_id[reply.id] is True
    assert read_by_id[other.id] is False
