"""A local author is told when a moderator or admin acts on their content (spec 2026-11-03-rbac-design.md, follow-up 3,
owner ruling 2026-11-05): the content, the community, the reason, and "a moderator" or "an admin", never who.

Everything goes through `add_to_modlog`, so the web, the API and an activity from another server are one path.
"""
import pytest
from flask import session
from flask_wtf.csrf import generate_csrf

from app import db
from app.api.alpha.utils.community import post_community_moderate_post_nsfw
from app.api.alpha.utils.post import put_post_set_flair
from app.api.alpha.utils.user import get_user_notifications
from app.constants import NOTIF_MODERATION, SRC_API
from app.models import ModLog, Notification, Post, User
from app.shared.post import lock_post, mod_remove_post, mod_restore_post
from app.shared.reply import mod_remove_reply, mod_restore_reply
from app.utils import add_to_modlog, notif_id_to_string
from tests.factories import (bearer, make_admin, make_community_flair, make_community_member, make_instance,
                             make_notification, make_post_reply, make_user, seed_post_context)


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


def test_removing_a_post_clears_other_notifications_about_it_and_keeps_the_notice(s):
    make_notification(s.author, s.post)

    mod_remove_post(s.post.id, 'off topic', SRC_API, bearer(s.voter))

    assert [n.notif_type for n in Notification.query.filter_by(user_id=s.author.id)] == [NOTIF_MODERATION]


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
        with client.session_transaction() as sess:
            sess['_user_id'] = str(s.author.id)
            sess['_fresh'] = True
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


# --------------------------------------------------------------------------
# Labels (follow-up 2): a moderator keeps them, and a change is recorded and told
# --------------------------------------------------------------------------

def label_entries(post):
    return ModLog.query.filter_by(action='edit_post_labels', post_id=post.id).all()


def csrf(app, client):
    """`login_required` checks the token itself on a POST, whatever WTF_CSRF_ENABLED says."""
    with app.test_request_context():
        token = generate_csrf()
        raw = session['csrf_token']
    with client.session_transaction() as sess:
        sess['csrf_token'] = raw
    return token


def htmx_post(app, client, url, data):
    return client.post(url, data={**data, 'csrf_token': csrf(app, client)},
                       headers={'HX-Request': 'true', 'HX-Current-Url': f'http://localhost{url}'})


def logged_in(app, user):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True
    return client


def test_a_moderator_changing_labels_on_the_web_is_recorded_and_told(app, s):
    flair = make_community_flair(s.community, 'news')
    s.voter.verified = True
    db.session.commit()

    response = htmx_post(app, logged_in(app, s.voter), f'/post/{s.post.id}/set_flair',
                         {'nsfw': '1', f'flair-{flair.id}': 'on'})

    assert response.status_code == 200
    entry, = label_entries(s.post)
    assert entry.user_id == s.voter.id and entry.target_user_id == s.author.id
    assert entry.reason == 'NSFW on; flair: news'
    notice, = notices(s.author)
    assert notice.title.startswith('A moderator changed the labels on your post')
    assert notice.targets['reason'] == 'NSFW on; flair: news'
    assert notice.url == f'/post/{s.post.id}'
    assert notice.author_id is None


def test_the_flair_form_page_records_a_change_too(app, s):
    """The low-bandwidth form, without htmx."""
    s.voter.verified = True
    db.session.commit()

    client = logged_in(app, s.voter)
    response = client.post(f'/post/{s.post.id}/set_flair',
                           data={'nsfl': 'y', 'referrer': '/', 'csrf_token': csrf(app, client)})

    assert response.status_code == 302
    assert label_entries(s.post)[0].reason == 'NSFL on'


def test_marking_a_post_ai_generated_is_recorded_and_told(app, s):
    s.voter.verified = True
    db.session.commit()

    logged_in(app, s.voter).post(f'/post/{s.post.id}/set_ai')

    assert label_entries(s.post)[0].reason == 'AI on'
    assert len(notices(s.author)) == 1


def test_the_api_flair_call_is_recorded_and_told(app, s):
    from flask import g

    g.site = s.site
    g.admin_ids = []
    flair = make_community_flair(s.community, 'news')

    put_post_set_flair(bearer(s.voter), {'post_id': s.post.id, 'flair_id_list': [flair.id]})

    assert label_entries(s.post)[0].reason == 'flair: news'
    assert len(notices(s.author)) == 1


def test_the_api_nsfw_call_is_recorded_and_told(app, s):
    from flask import g

    g.site = s.site
    g.admin_ids = []
    s.community.ap_id = None
    db.session.commit()

    post_community_moderate_post_nsfw(bearer(s.voter), {'post_id': s.post.id, 'nsfw_status': True})

    assert label_entries(s.post)[0].reason == 'NSFW on'
    assert len(notices(s.author)) == 1


def test_an_author_labelling_their_own_post_is_no_moderation(app, s):
    s.author.verified = True
    db.session.commit()

    htmx_post(app, logged_in(app, s.author), f'/post/{s.post.id}/set_flair', {'nsfw': '1'})

    assert db.session.get(Post, s.post.id).nsfw is True
    assert label_entries(s.post) == [] and notices(s.author) == []


def test_saving_labels_unchanged_records_nothing(app, s):
    flair = make_community_flair(s.community, 'news')
    s.post.flair = [flair]
    s.post.nsfw = True
    s.voter.verified = True
    db.session.commit()

    htmx_post(app, logged_in(app, s.voter), f'/post/{s.post.id}/set_flair', {'nsfw': '1', f'flair-{flair.id}': 'on'})

    assert db.session.get(Post, s.post.id).nsfw is True
    assert label_entries(s.post) == [] and notices(s.author) == []


def test_taking_a_label_off_says_so(app, s):
    flair = make_community_flair(s.community, 'news')
    s.post.flair = [flair]
    s.post.ai_generated = True
    s.voter.verified = True
    db.session.commit()

    htmx_post(app, logged_in(app, s.voter), f'/post/{s.post.id}/set_flair', {})

    assert label_entries(s.post)[0].reason == 'AI off; flair: none'
