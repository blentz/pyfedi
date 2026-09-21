"""Uploaded files, single notifications, unsubscribe links and follow requests.

Sub-project 81, slice G. One defect, measured:

* `/user/files/delete/<id>` rendered `<img src="{{ file.source_url }}">` for
  whatever id was in the URL, and nothing tied the file to the caller -- so
  walking the ids disclosed the URL of every uploaded file on the instance,
  including ones an account uploaded and never posted (D1071). The deletion
  itself was never the hole: `process_file_delete` scopes its DELETE by user.
"""
from unittest.mock import patch

import pytest

from app import db
from app.models import (File, Instance, Notification, Post, PostBookmark,
                        PostReply, PostReplyBookmark, Site, User, UserFollower,
                        user_file)
from tests.factories import (make_community, make_instance, make_post,
                             make_post_reply, make_user)

pytestmark = pytest.mark.usefixtures('site')


def instance(domain='test.piefed.local', software='piefed'):
    """Fact 394."""
    existing = Instance.query.filter_by(domain=domain).first()
    return existing if existing is not None else make_instance(domain,
                                                               software=software)


def csrf(app, client):
    """Fact 355."""
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf

    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as session:
        session['csrf_token'] = raw
    return token


def as_user(app, user):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return client


@pytest.fixture
def env(app, db_session):
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    local = instance()
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1  # fact 347
    owner = make_user(local, 'owner', local=True)
    other = make_user(local, 'other', local=True)
    db.session.commit()
    for user in (owner, other):
        user.ap_profile_id = f'https://test.piefed.local/u/{user.user_name}'
    db.session.commit()
    return as_user(app, owner), owner, other


def a_file(user, name='mine.png', size=10):
    file = File(source_url=f'https://test.piefed.local/media/{name}',
                file_path=f'app/static/media/{name}')
    db.session.add(file)
    db.session.commit()
    db.session.execute(
        db.text('INSERT INTO "user_file" (file_id, user_id, size) '
                'VALUES (:f, :u, :s)'),
        {'f': file.id, 'u': user.id, 's': size})
    db.session.commit()
    return file


def a_notification(user, read=False, url='/somewhere'):
    notification = Notification(user_id=user.id, title='a notification',
                                url=url, author_id=user.id, read=read,
                                notif_type=0)
    db.session.add(notification)
    db.session.commit()
    return notification


# --------------------------------------------------------------------------
# The file list
# --------------------------------------------------------------------------


def test_the_file_list_shows_your_files(app, env):
    client, owner, other = env
    mine = a_file(owner, 'mine.png')
    a_file(other, 'theirs.png')

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = client.get('/user/files')

    assert response.status_code == 200
    assert [f.id for f in render.call_args.kwargs['files'].items] == [mine.id]


def test_the_file_list_totals_your_usage(app, env):
    client, owner, other = env
    a_file(owner, 'one.png', size=100)
    a_file(owner, 'two.png', size=250)
    a_file(other, 'theirs.png', size=9999)

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/user/files')

    assert render.call_args.kwargs['total_size'] == 350


def test_the_file_list_pages(app, env):
    client, owner, other = env
    for number in range(120):
        a_file(owner, f'file{number}.png')

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/user/files')

    assert render.call_args.kwargs['next_url'] is not None
    assert render.call_args.kwargs['prev_url'] is None


# --------------------------------------------------------------------------
# D1071 -- the delete confirmation page
# --------------------------------------------------------------------------


def test_deleting_your_own_file(app, env):
    client, owner, other = env
    mine = a_file(owner)
    token = csrf(app, client)

    with patch('app.user.routes.process_file_delete') as deleted:
        response = client.post(f'/user/files/delete/{mine.id}',
                               data={'submit': 'Delete', 'csrf_token': token})

    assert response.status_code == 302
    assert deleted.call_args.args == (mine.source_url, owner.id)


def test_the_delete_page_asks_first(app, env):
    client, owner, other = env
    mine = a_file(owner)

    with patch('app.user.routes.process_file_delete') as deleted:
        with patch('app.user.routes.render_template',
                   return_value='rendered') as render:
            response = client.get(f'/user/files/delete/{mine.id}')

    assert response.status_code == 200
    assert render.call_args.kwargs['file'].id == mine.id
    assert deleted.call_args is None


def test_you_cannot_see_somebody_elses_file(app, env):
    """D1071. The page renders `<img src="{{ file.source_url }}">` for
    whatever id is in the URL, and nothing tied the file to the caller -- so
    walking the ids disclosed every uploaded file's URL on the instance,
    including ones nobody ever posted. Measured from an unrelated account:
    `PROBE aa1 status: 200 url leaked: True`."""
    client, owner, other = env
    theirs = a_file(other, 'secret.png')

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = client.get(f'/user/files/delete/{theirs.id}')

    assert response.status_code == 403
    assert render.call_args is None


def test_you_cannot_delete_somebody_elses_file(app, env):
    client, owner, other = env
    theirs = a_file(other, 'secret.png')
    token = csrf(app, client)

    with patch('app.user.routes.process_file_delete') as deleted:
        response = client.post(f'/user/files/delete/{theirs.id}',
                               data={'submit': 'Delete', 'csrf_token': token})

    assert response.status_code == 403
    assert deleted.call_args is None


def test_deleting_an_unknown_file_is_a_404(app, env):
    client, owner, other = env

    response = client.get('/user/files/delete/9999')

    assert response.status_code == 404


def test_a_file_delete_lands_back_where_it_started(app, env):
    client, owner, other = env
    mine = a_file(owner)
    token = csrf(app, client)

    with patch('app.user.routes.process_file_delete'):
        response = client.post(f'/user/files/delete/{mine.id}',
                               data={'submit': 'Delete',
                                     'referrer': 'https://evil.example/',
                                     'csrf_token': token})

    assert 'evil.example' not in response.headers['Location']


# --------------------------------------------------------------------------
# A single notification
# --------------------------------------------------------------------------


def test_following_a_notification_marks_it_read(app, env):
    client, owner, other = env
    notification = a_notification(owner, url='/c/general')
    owner.unread_notifications = 1
    db.session.commit()

    response = client.get(f'/notification/{notification.id}/goto')

    assert response.status_code == 302
    assert response.headers['Location'] == '/c/general'
    db.session.refresh(notification)
    db.session.refresh(owner)
    assert notification.read is True
    assert owner.unread_notifications == 0


def test_following_an_already_read_notification_does_not_go_negative(app, env):
    """`if not notification.read:` -- the counter is maintained by hand, and
    following the same link twice would otherwise drive it below zero."""
    client, owner, other = env
    notification = a_notification(owner, read=True)
    owner.unread_notifications = 0
    db.session.commit()

    client.get(f'/notification/{notification.id}/goto')

    db.session.refresh(owner)
    assert owner.unread_notifications == 0


def test_you_cannot_follow_somebody_elses_notification(app, env):
    """The url is theirs, and so is what it says about them."""
    client, owner, other = env
    theirs = a_notification(other)

    response = client.get(f'/notification/{theirs.id}/goto')

    assert response.status_code == 403


def test_deleting_a_notification(app, env):
    client, owner, other = env
    notification = a_notification(owner)
    owner.unread_notifications = 1
    db.session.commit()

    response = client.get(f'/notification/{notification.id}/delete')

    assert response.status_code == 302
    assert Notification.query.count() == 0
    db.session.refresh(owner)
    assert owner.unread_notifications == 0


def test_deleting_a_notification_from_htmx_returns_nothing(app, env):
    """The htmx caller swaps the row out, so the response body is the empty
    string rather than a redirect."""
    client, owner, other = env
    notification = a_notification(owner)

    response = client.get(f'/notification/{notification.id}/delete',
                          headers={'HX-Request': 'true'})

    assert response.status_code == 200
    assert response.data == b''


def test_you_cannot_delete_somebody_elses_notification(app, env):
    """This route does not abort -- it falls through to the redirect -- so the
    row has to assert that the notification survived."""
    client, owner, other = env
    theirs = a_notification(other)

    response = client.get(f'/notification/{theirs.id}/delete')

    assert response.status_code == 302
    assert Notification.query.count() == 1


def test_marking_one_notification_read(app, env):
    client, owner, other = env
    notification = a_notification(owner)
    owner.unread_notifications = 1
    db.session.commit()
    token = csrf(app, client)

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = client.post(f'/notification/{notification.id}/read',
                               data={'csrf_token': token})

    assert response.status_code == 200
    db.session.refresh(notification)
    db.session.refresh(owner)
    assert notification.read is True
    assert owner.unread_notifications == 0
    assert render.call_args.args[0].startswith('user/notifs/')


def test_marking_one_notification_unread(app, env):
    client, owner, other = env
    notification = a_notification(owner, read=True)
    owner.unread_notifications = 0
    db.session.commit()
    token = csrf(app, client)

    with patch('app.user.routes.render_template', return_value='rendered'):
        client.post(f'/notification/{notification.id}/unread',
                    data={'csrf_token': token})

    db.session.refresh(notification)
    db.session.refresh(owner)
    assert notification.read is False
    assert owner.unread_notifications == 1


@pytest.mark.parametrize('action', ['read', 'unread'])
def test_you_cannot_change_somebody_elses_notification(app, env, action):
    client, owner, other = env
    theirs = a_notification(other, read=(action == 'unread'))
    token = csrf(app, client)

    response = client.post(f'/notification/{theirs.id}/{action}',
                           data={'csrf_token': token})

    assert response.status_code == 403
    db.session.refresh(theirs)
    assert theirs.read is (action == 'unread')


@pytest.mark.parametrize('action', ['read', 'unread'])
def test_changing_an_unknown_notification_is_a_404(app, env, action):
    client, owner, other = env
    token = csrf(app, client)

    response = client.post(f'/notification/9999/{action}',
                           data={'csrf_token': token})

    assert response.status_code == 404


def test_marking_a_read_notification_read_again_changes_no_count(app, env):
    client, owner, other = env
    notification = a_notification(owner, read=True)
    owner.unread_notifications = 5
    db.session.commit()
    token = csrf(app, client)

    with patch('app.user.routes.render_template', return_value='rendered'):
        client.post(f'/notification/{notification.id}/read',
                    data={'csrf_token': token})

    db.session.refresh(owner)
    assert owner.unread_notifications == 5


# --------------------------------------------------------------------------
# The unsubscribe links from emails
# --------------------------------------------------------------------------


@pytest.mark.parametrize('path, column', [
    ('newsletter', 'newsletter'),
    ('email_notifs', 'email_unread'),
])
def test_an_unsubscribe_link_turns_the_setting_off(app, env, path, column):
    """These are GETs by protocol -- they arrive from an email -- and each
    carries the account's verification token instead of a session."""
    client, owner, other = env
    owner.verification_token = 'a-token'
    setattr(owner, column, True)
    db.session.commit()

    response = app.test_client().get(
        f'/user/{path}/{owner.id}/a-token/unsubscribe')

    assert response.status_code == 200
    db.session.refresh(owner)
    assert getattr(owner, column) is False


@pytest.mark.parametrize('path, column', [
    ('newsletter', 'newsletter'),
    ('email_notifs', 'email_unread'),
])
def test_an_unsubscribe_link_with_the_wrong_token_changes_nothing(app, env,
                                                                  path,
                                                                  column):
    """The token is the whole authentication: without this check anyone could
    unsubscribe anyone by walking the ids."""
    client, owner, other = env
    owner.verification_token = 'a-token'
    setattr(owner, column, True)
    db.session.commit()

    response = app.test_client().get(
        f'/user/{path}/{owner.id}/the-wrong-token/unsubscribe')

    assert response.status_code == 200
    db.session.refresh(owner)
    assert getattr(owner, column) is True


# --------------------------------------------------------------------------
# Bookmarks
# --------------------------------------------------------------------------


def test_the_bookmark_list(app, env):
    from app.constants import POST_STATUS_REVIEWING

    client, owner, other = env
    community = make_community('general')
    db.session.commit()
    kept = make_post(community, owner, 'https://test.piefed.local/p/1',
                     title='a bookmarked post')
    pending = make_post(community, owner, 'https://test.piefed.local/p/2',
                        title='a post in review')
    pending.status = POST_STATUS_REVIEWING
    db.session.add_all([
        PostBookmark(user_id=owner.id, post_id=kept.id),
        PostBookmark(user_id=owner.id, post_id=pending.id),
    ])
    db.session.commit()

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = client.get('/bookmarks')

    assert response.status_code == 200
    assert [p.title for p in render.call_args.kwargs['posts'].items] == [
        'a bookmarked post']


def test_the_bookmark_list_shows_nobody_elses(app, env):
    client, owner, other = env
    community = make_community('general')
    db.session.commit()
    post = make_post(community, other, 'https://test.piefed.local/p/1',
                     title='their bookmark')
    db.session.add(PostBookmark(user_id=other.id, post_id=post.id))
    db.session.commit()

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/bookmarks')

    assert render.call_args.kwargs['posts'].items == []


def test_the_comment_bookmark_list(app, env):
    client, owner, other = env
    community = make_community('general')
    db.session.commit()
    post = make_post(community, owner, 'https://test.piefed.local/p/1',
                     title='a post')
    reply = make_post_reply(post, owner, body='a bookmarked reply')
    db.session.add(PostReplyBookmark(user_id=owner.id,
                                     post_reply_id=reply.id))
    db.session.commit()

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = client.get('/bookmarks/comments')

    assert response.status_code == 200
    assert [r.body for r in render.call_args.kwargs['post_replies'].items] == [
        'a bookmarked reply']


def test_the_comment_bookmark_list_shows_nobody_elses(app, env):
    client, owner, other = env
    community = make_community('general')
    db.session.commit()
    post = make_post(community, other, 'https://test.piefed.local/p/1',
                     title='a post')
    reply = make_post_reply(post, other, body='their bookmark')
    db.session.add(PostReplyBookmark(user_id=other.id,
                                     post_reply_id=reply.id))
    db.session.commit()

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/bookmarks/comments')

    assert render.call_args.kwargs['post_replies'].items == []


# --------------------------------------------------------------------------
# Follow requests
# --------------------------------------------------------------------------


def a_follow_request(target, follower, accepted=None):
    request_row = UserFollower(local_user_id=target.id,
                               remote_user_id=follower.id,
                               is_inward=True, is_accepted=accepted)
    db.session.add(request_row)
    db.session.commit()
    return request_row


def test_the_follow_request_list(app, env):
    client, owner, other = env
    a_follow_request(owner, other)

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = client.get('/user/follow_requests')

    assert response.status_code == 200
    assert [u.id for u in render.call_args.kwargs['follow_requests']] == [other.id]


def test_an_accepted_request_is_not_in_the_list(app, env):
    client, owner, other = env
    a_follow_request(owner, other, accepted=True)

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/user/follow_requests')

    assert render.call_args.kwargs['follow_requests'] == []


def test_accepting_a_follow_request(app, env):
    client, owner, other = env
    request_row = a_follow_request(owner, other)
    token = csrf(app, client)

    response = client.post(f'/user/follow_request/{other.id}/accept',
                           data={'csrf_token': token})

    assert response.status_code in (200, 302)
    db.session.refresh(request_row)
    assert request_row.is_accepted is True


def test_accepting_a_remote_follow_request_sends_an_accept(app, env):
    """A remote follower is waiting on an Accept activity; a local one is
    not."""
    client, owner, other = env
    remote = make_user(make_instance('other.example', software='piefed'),
                       'remote')
    remote.ap_inbox_url = 'https://other.example/u/remote/inbox'
    db.session.commit()
    a_follow_request(owner, remote)
    token = csrf(app, client)

    with patch('app.user.routes.send_post_request') as send:
        client.post(f'/user/follow_request/{remote.id}/accept',
                    data={'csrf_token': token})

    assert send.call_args.args[0] == 'https://other.example/u/remote/inbox'
    assert send.call_args.args[1]['type'] == 'Accept'


def test_accepting_a_local_follow_request_sends_nothing(app, env):
    client, owner, other = env
    a_follow_request(owner, other)
    token = csrf(app, client)

    with patch('app.user.routes.send_post_request') as send:
        client.post(f'/user/follow_request/{other.id}/accept',
                    data={'csrf_token': token})

    assert send.call_args is None


def test_rejecting_a_follow_request(app, env):
    """D1072. This route said `is_accepted = True` -- a copy of the accept
    route that was never changed -- so rejecting a follow request sent the
    remote side a Reject activity AND recorded locally that the follow had
    been ACCEPTED. The rejected person then appeared in the followers list of
    somebody who believed they had turned them away. The column's own comment
    gives the value: "None = request sent. True = accepted. False =
    Rejected"."""
    client, owner, other = env
    request_row = a_follow_request(owner, other)
    token = csrf(app, client)

    response = client.post(f'/user/follow_request/{other.id}/reject',
                           data={'csrf_token': token})

    assert response.status_code in (200, 302)
    db.session.refresh(request_row)
    assert request_row.is_accepted is False


def test_a_rejected_follower_is_not_in_the_followers_list(app, env):
    """What the defect cost: `show_profile` selects `is_accepted == True`, so
    a rejection recorded as an acceptance put the person in the list."""
    client, owner, other = env
    a_follow_request(owner, other)
    token = csrf(app, client)

    client.post(f'/user/follow_request/{other.id}/reject',
                data={'csrf_token': token})

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/user/{owner.id}')

    assert render.call_args.kwargs['followers'] == []


def test_rejecting_a_remote_follow_request_sends_a_reject(app, env):
    client, owner, other = env
    remote = make_user(make_instance('other.example', software='piefed'),
                       'remote')
    remote.ap_inbox_url = 'https://other.example/u/remote/inbox'
    db.session.commit()
    a_follow_request(owner, remote)
    token = csrf(app, client)

    with patch('app.user.routes.send_post_request') as send:
        client.post(f'/user/follow_request/{remote.id}/reject',
                    data={'csrf_token': token})

    assert send.call_args.args[1]['type'] == 'Reject'


def test_you_cannot_accept_a_request_that_is_not_yours(app, env):
    """The lookup is keyed on `local_user_id == current_user.id`, so another
    account's pending request is simply not found."""
    client, owner, other = env
    third = make_user(instance(), 'third', local=True)
    db.session.commit()
    request_row = a_follow_request(other, third)
    token = csrf(app, client)

    client.post(f'/user/follow_request/{third.id}/accept',
                data={'csrf_token': token})

    db.session.refresh(request_row)
    assert request_row.is_accepted is None
