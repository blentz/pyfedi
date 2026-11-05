"""A local author is told when a moderator or admin acts on their content (spec 2026-11-03-rbac-design.md, follow-up 3,
owner ruling 2026-11-05): the content, the community, the reason, and "a moderator" or "an admin", never who.

Everything goes through `add_to_modlog`, so the web, the API and an activity from another server are one path.
"""
import pytest

from app import db
from app.api.alpha.utils.user import get_user_notifications
from app.constants import NOTIF_MODERATION, SRC_API
from app.models import Notification, User
from app.shared.post import lock_post, mod_remove_post, mod_restore_post
from app.shared.reply import mod_remove_reply, mod_restore_reply
from app.utils import add_to_modlog, notif_id_to_string
from tests.factories import (bearer, make_admin, make_community_member, make_instance, make_post_reply, make_user,
                             seed_post_context)


@pytest.fixture
def s(db_session):
    s = seed_post_context(community_name='moderation')
    s.community.title = 'The Moderation Room'
    s.post.title = 'What the author wrote'
    make_community_member(s.voter, s.community, is_moderator=True)
    s.voter.user_name = 'themoderator'
    db.session.commit()
    return s


def notices(user):
    return Notification.query.filter_by(user_id=user.id, notif_type=NOTIF_MODERATION).order_by(Notification.id).all()


def test_removing_a_post_tells_its_author(s):
    mod_remove_post(s.post.id, 'off topic', SRC_API, bearer(s.voter))

    notice, = notices(s.author)
    assert notice.title.startswith('A moderator removed your post')
    assert 'The Moderation Room' in notice.title and 'What the author wrote' in notice.title
    assert notice.targets['reason'] == 'off topic'
    assert notice.targets['post_id'] == s.post.id and notice.targets['community_id'] == s.community.id
    assert notice.subtype == 'delete_post'
    assert db.session.get(User, s.author.id).unread_notifications == 1


def test_a_notice_about_a_removed_post_goes_to_its_community_which_the_author_can_open(s):
    """The post page is a 404 for its author once it has been removed."""
    mod_remove_post(s.post.id, 'off topic', SRC_API, bearer(s.voter))

    assert notices(s.author)[0].url == f'/c/{s.community.link()}'


def test_the_notice_names_no_one(s):
    mod_remove_post(s.post.id, 'off topic', SRC_API, bearer(s.voter))

    notice, = notices(s.author)
    assert notice.author_id is None
    assert 'themoderator' not in notice.title and 'themoderator' not in str(notice.targets)
    assert s.voter.id not in notice.targets.values()


def test_an_admin_is_called_an_admin(s):
    make_admin(s.voter)

    mod_remove_post(s.post.id, 'off topic', SRC_API, bearer(s.voter))

    assert notices(s.author)[0].title.startswith('An admin removed your post')


def test_restoring_a_post_tells_its_author_and_links_to_it(s):
    mod_remove_post(s.post.id, 'off topic', SRC_API, bearer(s.voter))

    mod_restore_post(s.post.id, 'our mistake', SRC_API, bearer(s.voter))

    restored = notices(s.author)[-1]
    assert restored.title.startswith('A moderator restored your post')
    assert restored.targets['reason'] == 'our mistake'
    assert restored.url == f'/post/{s.post.id}'


@pytest.mark.parametrize('locked, words', [(True, 'turned comments off for your post'),
                                           (False, 'turned comments back on for your post')])
def test_turning_comments_off_or_on_tells_the_author(s, locked, words):
    lock_post(s.post.id, locked, SRC_API, bearer(s.voter))

    notice, = notices(s.author)
    assert words in notice.title
    assert notice.url == f'/post/{s.post.id}'


def test_removing_and_restoring_a_comment_tells_its_author(s):
    reply = make_post_reply(s.post, s.author, body='a comment')

    mod_remove_reply(reply.id, 'rude', SRC_API, bearer(s.voter))
    mod_restore_reply(reply.id, 'not rude', SRC_API, bearer(s.voter))

    removed, restored = notices(s.author)
    assert removed.title.startswith('A moderator removed your comment')
    assert removed.targets['comment_id'] == reply.id and removed.targets['reason'] == 'rude'
    assert removed.url == f'/post/{s.post.id}#comment_{reply.id}'
    assert restored.title.startswith('A moderator restored your comment')


def test_turning_replies_off_under_a_comment_tells_its_author(s):
    """As an activity from another server does: it names the comment and not its post."""
    reply = make_post_reply(s.post, s.author, body='a comment')

    add_to_modlog('lock_post_reply', actor=s.voter, target_user=s.author, community=s.community, reply=reply)

    notice, = notices(s.author)
    assert 'turned replies off under your comment' in notice.title
    assert notice.targets['post_id'] == s.post.id and notice.targets['comment_id'] == reply.id


def test_a_comment_on_a_removed_post_links_to_the_community(s):
    reply = make_post_reply(s.post, s.author, body='a comment')
    s.post.deleted = True
    db.session.commit()

    add_to_modlog('delete_post_reply', actor=s.voter, target_user=s.author, community=s.community, post=s.post,
                  reply=reply)

    assert notices(s.author)[0].url == f'/c/{s.community.link()}'


def test_nobody_is_told_of_their_own_action(s):
    add_to_modlog('delete_post', actor=s.author, target_user=s.author, community=s.community, post=s.post)

    assert notices(s.author) == []


def test_a_remote_author_is_told_by_their_own_server(s):
    """The federated Delete already carries the reason, and a remote account reads no notifications here."""
    remote = make_user(make_instance('remote.example'), 'remoteauthor')

    add_to_modlog('delete_post', actor=s.voter, target_user=remote, community=s.community, post=s.post)

    assert notices(remote) == []


@pytest.mark.parametrize('action', ['ban_user', 'add_mod', 'featured_post', 'move_post'])
def test_other_actions_send_no_notice(s, action):
    add_to_modlog(action, actor=s.voter, target_user=s.author, community=s.community, post=s.post)

    assert notices(s.author) == []


def test_an_action_with_nothing_to_name_sends_no_notice(s):
    add_to_modlog('delete_post', actor=s.voter, target_user=s.author, community=s.community)

    assert notices(s.author) == []


def test_the_type_has_a_label_and_a_row(app, s):
    assert notif_id_to_string(NOTIF_MODERATION) == 'Admin'


def test_the_notifications_page_shows_the_reason_and_not_the_actor(app, s):

    mod_remove_post(s.post.id, 'off topic', SRC_API, bearer(s.voter))
    s.author.verified = True
    db.session.commit()

    with app.test_client() as client:
        with client.session_transaction() as session:
            session['_user_id'] = str(s.author.id)
            session['_fresh'] = True
        page = client.get('/notifications').get_data(as_text=True)

    assert 'A moderator removed your post' in page
    assert 'off topic' in page
    assert 'themoderator' not in page


def test_the_api_lists_it_with_the_reason_and_the_recipient_as_author(app, s):
    """`author` is required in the API's schema, so a client that decodes strictly has one; it is the content's author,
    which is the recipient, and never the moderator."""
    from flask import g

    g.site = s.site
    g.admin_ids = []
    mod_remove_post(s.post.id, 'off topic', SRC_API, bearer(s.voter))

    item, = get_user_notifications(bearer(s.author), {'status': 'All'})['items']

    assert item['notif_type'] == NOTIF_MODERATION and item['notif_subtype'] == 'delete_post'
    assert item['reason'] == 'off topic'
    assert item['notif_body'].startswith('A moderator removed your post')
    assert item['post_id'] == s.post.id
    assert item['author']['id'] == s.author.id
    assert 'themoderator' not in str(item)
