"""The fragments and side doors of `app/post/routes.py`.

Sub-project 82, slice E. Everything here is a page the post view delegates to,
or an endpoint the front end calls directly -- and that is the theme of the
round: **the checks live on the page, and the fragments it delegates to did
not repeat them**. Eight defects, all measured:

* `/ical` handed a private event's title, description and start time to
  anyone, with no access decorator at all (D1105);
* `/lazy_replies` -- where `show_post` defers the thread past a hundred
  comments -- rendered a private community's whole conversation (D1106);
* `post_check_ai` and `post_reply_check_ai` carried no decorator, so an
  anonymous caller drove one outbound request per call (D1107), and read
  `.ap_id` off a `db.session.get` that can answer None (D1108);
* the cross-post page handed a non-member the post itself (D1109) and read a
  cookie with `int()` (D1110);
* the community-suggestion endpoint named PRIVATE communities, to anyone
  (D1111);
* the markdown-source fragments showed a private community's text, and fell
  over on a post with no body (D1112).
"""
from datetime import datetime
from unittest.mock import patch

import pytest

from app import db
from app.constants import POST_TYPE_EVENT, POST_TYPE_IMAGE, POST_TYPE_LINK
from app.models import (BlockedImage, Community, Event, File, Instance,
                        Language, Post, PostReply, Reminder, Site, User)
from app.utils import utcnow
from tests.factories import (grant_permission, make_community,
                             make_community_ban, make_community_member,
                             make_instance, make_post, make_post_reply,
                             make_user)

pytestmark = pytest.mark.usefixtures('site')


def instance(domain='test.piefed.local', software='piefed'):
    """Fact 394."""
    existing = Instance.query.filter_by(domain=domain).first()
    return existing if existing is not None else make_instance(domain,
                                                               software=software)


def csrf(app, client):
    """Facts 355, 515."""
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
    mod = make_user(local, 'mod', local=True, with_keys=True)
    mod.verified = True
    author = make_user(local, 'author', local=True, with_keys=True)
    author.verified = True
    outsider = make_user(local, 'outsider', local=True, with_keys=True)
    outsider.verified = True
    community = make_community('general')
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.commit()
    make_community_member(mod, community, is_moderator=True)
    make_community_member(author, community)
    post = make_post(community, author, 'https://test.piefed.local/p/1',
                     title='THETITLE')
    post.body = 'the body in markdown'
    post.body_html = '<p>THEBODY</p>'
    db.session.commit()
    return app.test_client(), community, post, mod, author, outsider


def a_reply(post, user, body='a reply', **columns):
    """Fact 501."""
    reply = make_post_reply(post, user, body=body)
    reply.body_html = f'<p>{body}</p>'
    for column, value in columns.items():
        setattr(reply, column, value)
    db.session.commit()
    return reply


def private_place(app, name='secret'):
    community = make_community(name)
    community.private = True
    db.session.commit()
    member = make_user(instance(), f'{name}_member', local=True,
                       with_keys=True)
    member.verified = True
    db.session.commit()
    make_community_member(member, community)
    post = make_post(community, member, f'https://test.piefed.local/p/{name}',
                     title='SECRETTITLE')
    post.body = 'secret markdown'
    post.body_html = '<p>SECRETBODY</p>'
    db.session.commit()
    return community, post, member


# --------------------------------------------------------------------------
# D1105 -- the calendar feed
# --------------------------------------------------------------------------


def an_event(post, start=None, end=None):
    post.type = POST_TYPE_EVENT
    db.session.add(Event(post_id=post.id,
                         start=start or datetime(2026, 6, 1, 12, 0),
                         end=end or datetime(2026, 6, 1, 13, 0),
                         timezone='UTC'))
    db.session.commit()
    return post


def test_an_event_can_be_downloaded_as_a_calendar(app, env):
    anon, community, post, mod, author, outsider = env
    an_event(post)

    response = anon.get(f'/post/{post.id}/ical')

    assert response.status_code == 200
    assert response.mimetype == 'text/calendar'
    assert b'THETITLE' in response.data
    assert response.headers['Content-Disposition'] == \
        'inline; filename="thetitle.ics"'


def test_a_private_communitys_event_is_not_downloadable(app, env):
    """D1105. This route carries no access decorator of its own and made no
    check, so a private community's event handed out its title, its
    description and its start time. Measured: `PROBE an1 ical of a private
    event: 200 | title: True`."""
    anon, community, post, mod, author, outsider = env
    secret, secret_post, member = private_place(app)
    an_event(secret_post)

    response = anon.get(f'/post/{secret_post.id}/ical')

    assert response.status_code == 403
    assert b'SECRETTITLE' not in response.data


def test_a_member_still_downloads_their_private_communitys_event(app, env):
    anon, community, post, mod, author, outsider = env
    secret, secret_post, member = private_place(app)
    an_event(secret_post)

    response = as_user(app, member).get(f'/post/{secret_post.id}/ical')

    assert response.status_code == 200
    assert b'SECRETTITLE' in response.data


def test_an_unpublished_event_is_not_downloadable(app, env):
    anon, community, post, mod, author, outsider = env
    an_event(post)
    post.status = -2
    post.scheduled_for = utcnow()
    db.session.commit()

    assert anon.get(f'/post/{post.id}/ical').status_code == 404


def test_a_post_that_is_not_an_event_has_no_calendar(app, env):
    anon, community, post, mod, author, outsider = env

    assert anon.get(f'/post/{post.id}/ical').status_code == 404


def test_an_event_type_with_no_event_row_has_no_calendar(app, env):
    """`post.type` and the Event row are separate, so a post that claims to be
    an event without one was `AttributeError` on `post.event.start`."""
    anon, community, post, mod, author, outsider = env
    post.type = POST_TYPE_EVENT
    db.session.commit()

    assert anon.get(f'/post/{post.id}/ical').status_code == 404


def test_an_event_with_no_end_is_still_a_calendar_entry(app, env):
    """`end` is only written when it is after `start`, so an open-ended event
    -- or one whose end was federated wrong -- still serialises."""
    anon, community, post, mod, author, outsider = env
    post.type = POST_TYPE_EVENT
    db.session.add(Event(post_id=post.id, start=datetime(2026, 6, 1, 12, 0),
                         end=None, timezone='UTC'))
    db.session.commit()

    response = anon.get(f'/post/{post.id}/ical')

    assert response.status_code == 200
    assert b'DTSTART' in response.data


def test_an_event_ending_before_it_starts_drops_the_end(app, env):
    anon, community, post, mod, author, outsider = env
    an_event(post, start=datetime(2026, 6, 1, 12, 0),
             end=datetime(2026, 6, 1, 11, 0))

    response = anon.get(f'/post/{post.id}/ical')

    assert response.status_code == 200
    assert b'DTEND' not in response.data


def test_an_unknown_post_has_no_calendar(app, env):
    anon, community, post, mod, author, outsider = env

    assert anon.get('/post/999999/ical').status_code == 404


# --------------------------------------------------------------------------
# D1106 -- the lazily loaded thread
# --------------------------------------------------------------------------


def test_the_lazy_thread_renders_the_replies(app, env):
    anon, community, post, mod, author, outsider = env
    a_reply(post, author, body='THEREPLY')

    response = anon.get(f'/post/{post.id}/lazy_replies/abc')

    assert response.status_code == 200
    assert b'THEREPLY' in response.data


def test_the_lazy_thread_refuses_a_private_community(app, env):
    """D1106. `show_post` defers the thread to this route past a hundred
    comments, and this route made none of the checks `show_post` makes.
    Measured: `PROBE an2 lazy_replies of a private post: 200 | reply:
    True`."""
    anon, community, post, mod, author, outsider = env
    secret, secret_post, member = private_place(app)
    a_reply(secret_post, member, body='SECRETREPLY')

    response = anon.get(f'/post/{secret_post.id}/lazy_replies/abc')

    assert response.status_code == 403
    assert b'SECRETREPLY' not in response.data


def test_a_member_still_reads_the_lazy_thread(app, env):
    anon, community, post, mod, author, outsider = env
    secret, secret_post, member = private_place(app)
    a_reply(secret_post, member, body='SECRETREPLY')

    response = as_user(app, member).get(
        f'/post/{secret_post.id}/lazy_replies/abc')

    assert response.status_code == 200
    assert b'SECRETREPLY' in response.data


def test_the_lazy_thread_refuses_an_unpublished_post(app, env):
    anon, community, post, mod, author, outsider = env
    post.status = -2
    post.scheduled_for = utcnow()
    db.session.commit()

    assert anon.get(f'/post/{post.id}/lazy_replies/abc').status_code == 404


def test_the_lazy_thread_answers_a_cors_preflight(app, env):
    anon, community, post, mod, author, outsider = env

    response = anon.open(f'/post/{post.id}/lazy_replies/abc',
                         method='OPTIONS')

    assert response.status_code == 200


def test_the_lazy_thread_preflight_returns_nothing(app, env):
    """The OPTIONS arm answers before the post is even looked up, so a
    preflight for a post that does not exist is still a preflight."""
    anon, community, post, mod, author, outsider = env

    response = anon.open('/post/999999/lazy_replies/abc', method='OPTIONS')

    assert response.status_code == 200
    assert response.data == b''


def test_an_archived_post_always_sorts_hot(app, env):
    """An archived thread is rebuilt from stored data, which is only kept in
    one order."""
    anon, community, post, mod, author, outsider = env
    post.archived = 'https://archive.example/p/1'
    db.session.commit()

    with patch('app.post.routes.post_replies', return_value=[]) as replies:
        anon.get(f'/post/{post.id}/lazy_replies/abc?sort=new')

    assert replies.call_args.args[1] == 'hot'


def test_the_lazy_thread_honours_the_requested_sort(app, env):
    anon, community, post, mod, author, outsider = env

    with patch('app.post.routes.post_replies', return_value=[]) as replies:
        anon.get(f'/post/{post.id}/lazy_replies/abc?sort=new')

    assert replies.call_args.args[1] == 'new'


def test_the_lazy_thread_carries_the_user_flair(app, env):
    anon, community, post, mod, author, outsider = env
    from app.models import UserFlair
    db.session.add(UserFlair(user_id=author.id, community_id=community.id,
                             flair='bronze'))
    db.session.commit()
    a_reply(post, author)

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        as_user(app, author).get(f'/post/{post.id}/lazy_replies/abc')

    assert render.call_args.kwargs['user_flair'] == {author.id: 'bronze'}


def test_an_anonymous_lazy_thread_has_no_per_account_state(app, env):
    anon, community, post, mod, author, outsider = env
    a_reply(post, author)

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        anon.get(f'/post/{post.id}/lazy_replies/abc')

    kwargs = render.call_args.kwargs
    assert kwargs['recently_upvoted_replies'] == []
    assert kwargs['reply_collapse_threshold'] == -10
    assert kwargs['user_flair'] == {}
    assert kwargs['communities_banned_from_list'] == []


def test_a_readers_collapse_setting_reaches_the_lazy_thread(app, env):
    anon, community, post, mod, author, outsider = env
    outsider.reply_collapse_threshold = -5
    db.session.commit()
    a_reply(post, author)

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        as_user(app, outsider).get(f'/post/{post.id}/lazy_replies/abc')

    assert render.call_args.kwargs['reply_collapse_threshold'] == -5


def test_no_collapse_setting_means_collapse_nothing_here_too(app, env):
    anon, community, post, mod, author, outsider = env
    outsider.reply_collapse_threshold = 0
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        as_user(app, outsider).get(f'/post/{post.id}/lazy_replies/abc')

    assert render.call_args.kwargs['reply_collapse_threshold'] == -1000


def test_the_lazy_thread_offers_a_cross_posts_replies(app, env):
    anon, community, post, mod, author, outsider = env
    other = make_community('elsewhere')
    db.session.commit()
    cross = make_post(other, author, 'https://test.piefed.local/p/2',
                      title='the same link')
    db.session.commit()
    a_reply(cross, author, body='CROSSREPLY')
    post.cross_posts = [cross.id]
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        anon.get(f'/post/{post.id}/lazy_replies/abc')

    more = render.call_args.kwargs['more_replies']
    assert [node['comment'].body for node in more[other]] == ['CROSSREPLY']


def test_a_blocked_communitys_cross_post_is_left_out(app, env):
    anon, community, post, mod, author, outsider = env
    other = make_community('elsewhere')
    db.session.commit()
    cross = make_post(other, author, 'https://test.piefed.local/p/2',
                      title='the same link')
    db.session.commit()
    a_reply(cross, author, body='CROSSREPLY')
    post.cross_posts = [cross.id]
    db.session.commit()

    with patch('app.post.routes.blocked_communities', return_value=[other.id]):
        with patch('app.post.routes.render_template',
                   return_value='rendered') as render:
            as_user(app, outsider).get(f'/post/{post.id}/lazy_replies/abc')

    assert render.call_args.kwargs['more_replies'] == {}


def test_an_unknown_post_has_no_lazy_thread(app, env):
    anon, community, post, mod, author, outsider = env

    assert anon.get('/post/999999/lazy_replies/abc').status_code == 404


# --------------------------------------------------------------------------
# D1107, D1108 -- the AI check
# --------------------------------------------------------------------------


def ai_says(result='ai', confidence=0.9):
    response = patch('app.post.routes.get_request').start()
    patch.stopall()
    return result, confidence


def test_the_ai_check_reports_what_the_detector_said(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    token = csrf(app, client)

    with patch.dict(app.config, {'DETECT_AI_ENDPOINT': 'https://ai.example/x'}):
        with patch('app.post.routes.get_request') as fetched:
            fetched.return_value.status_code = 200
            fetched.return_value.json.return_value = {
                'detection_result': 'ai', 'confidence': 0.93}
            response = client.post(f'/post/{post.id}/check_ai',
                                   data={'csrf_token': token})

    assert response.status_code == 200
    assert b'AI' in response.data
    assert b'93% confident' in response.data
    assert b'alert-warning' in response.data


def test_a_human_verdict_is_shown_differently(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    token = csrf(app, client)

    with patch.dict(app.config, {'DETECT_AI_ENDPOINT': 'https://ai.example/x'}):
        with patch('app.post.routes.get_request') as fetched:
            fetched.return_value.status_code = 200
            fetched.return_value.json.return_value = {
                'detection_result': 'human', 'confidence': 0.4}
            response = client.post(f'/post/{post.id}/check_ai',
                                   data={'csrf_token': token})

    assert b'alert-success' in response.data


def test_an_anonymous_caller_cannot_drive_the_detector(app, env):
    """D1107. This route carried no decorator, so an anonymous caller made
    this instance issue one outbound request to the configured detector per
    call. Measured: `PROBE an3 anonymous check_ai: 200 | outbound fetch:
    True`. D1025's shape."""
    anon, community, post, mod, author, outsider = env

    with patch.dict(app.config, {'DETECT_AI_ENDPOINT': 'https://ai.example/x'}):
        with patch('app.post.routes.get_request') as fetched:
            response = anon.post(f'/post/{post.id}/check_ai')

    assert response.status_code == 302
    assert fetched.call_args is None


def test_a_private_communitys_post_cannot_be_checked(app, env):
    anon, community, post, mod, author, outsider = env
    secret, secret_post, member = private_place(app)
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch.dict(app.config, {'DETECT_AI_ENDPOINT': 'https://ai.example/x'}):
        with patch('app.post.routes.get_request') as fetched:
            response = client.post(f'/post/{secret_post.id}/check_ai',
                                   data={'csrf_token': token})

    assert response.status_code == 403
    assert fetched.call_args is None


def test_a_detector_that_declines_to_answer(app, env):
    """`'none'` is the detector saying it would not look -- a blocked or
    unreachable URL -- which is not a verdict about the text."""
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    token = csrf(app, client)

    with patch.dict(app.config, {'DETECT_AI_ENDPOINT': 'https://ai.example/x'}):
        with patch('app.post.routes.get_request') as fetched:
            fetched.return_value.status_code = 200
            fetched.return_value.json.return_value = {
                'detection_result': 'none', 'confidence': 0.0}
            response = client.post(f'/post/{post.id}/check_ai',
                                   data={'csrf_token': token})

    assert b'Detection blocked' in response.data


@pytest.mark.parametrize('verdict, expected', [
    ('ai', b'alert-warning'),
    ('human', b'alert-success'),
])
def test_a_link_post_is_judged_twice(app, env, verdict, expected):
    """A link post has a body AND the page it points at, and the detector
    answers about both: the attachment's verdict is reported separately."""
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    token = csrf(app, client)

    with patch.dict(app.config, {'DETECT_AI_ENDPOINT': 'https://ai.example/x'}):
        with patch('app.post.routes.get_request') as fetched:
            fetched.return_value.status_code = 200
            fetched.return_value.json.return_value = {
                'detection_result': 'human', 'confidence': 0.2,
                'attachment': {'detection_result': verdict,
                               'confidence': 0.77}}
            response = client.post(f'/post/{post.id}/check_ai',
                                   data={'csrf_token': token})

    assert b'Link: ' + verdict.upper().encode() in response.data
    assert b'77% confident' in response.data
    assert expected in response.data


def test_checking_an_unknown_post(app, env):
    """D1108. `db.session.get` answers None, and the fetch reads `post.ap_id`:
    `AttributeError: 'NoneType' object has no attribute 'ap_id'`."""
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    token = csrf(app, client)

    with patch.dict(app.config, {'DETECT_AI_ENDPOINT': 'https://ai.example/x'}):
        response = client.post('/post/999999/check_ai',
                               data={'csrf_token': token})

    assert response.status_code == 404


def test_the_ai_check_says_when_it_is_not_configured(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    token = csrf(app, client)

    with patch.dict(app.config, {'DETECT_AI_ENDPOINT': ''}):
        response = client.post(f'/post/{post.id}/check_ai',
                               data={'csrf_token': token})

    assert b'Not configured' in response.data


def test_a_detector_that_fails_says_nothing(app, env):
    """A non-200 from the detector is not an answer, and the button is left as
    it was rather than claiming a verdict."""
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    token = csrf(app, client)

    with patch.dict(app.config, {'DETECT_AI_ENDPOINT': 'https://ai.example/x'}):
        with patch('app.post.routes.get_request') as fetched:
            fetched.return_value.status_code = 502
            response = client.post(f'/post/{post.id}/check_ai',
                                   data={'csrf_token': token})

    assert response.data == b''


def test_checking_a_comment(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author, body='x' * 200)
    client = as_user(app, author)
    token = csrf(app, client)

    with patch.dict(app.config, {'DETECT_AI_ENDPOINT': 'https://ai.example/x'}):
        with patch('app.post.routes.get_request') as fetched:
            fetched.return_value.status_code = 200
            fetched.return_value.json.return_value = {
                'detection_result': 'ai', 'confidence': 0.8}
            response = client.post(f'/post_reply/{reply.id}/check_ai',
                                   data={'csrf_token': token})

    assert b'80% confident' in response.data


def test_a_comment_judged_human(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author, body='x' * 200)
    client = as_user(app, author)
    token = csrf(app, client)

    with patch.dict(app.config, {'DETECT_AI_ENDPOINT': 'https://ai.example/x'}):
        with patch('app.post.routes.get_request') as fetched:
            fetched.return_value.status_code = 200
            fetched.return_value.json.return_value = {
                'detection_result': 'human', 'confidence': 0.6}
            response = client.post(f'/post_reply/{reply.id}/check_ai',
                                   data={'csrf_token': token})

    assert b'alert-success' in response.data
    assert b'HUMAN' in response.data


def test_a_short_comment_is_not_worth_checking(app, env):
    """The floor is a hundred characters: below it the detector's verdict
    means little, so the body is never sent. The row sits just under the
    boundary rather than obviously short, or the boundary itself is untested.
    """
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author, body='x' * 100)
    client = as_user(app, author)
    token = csrf(app, client)

    with patch.dict(app.config, {'DETECT_AI_ENDPOINT': 'https://ai.example/x'}):
        with patch('app.post.routes.get_request') as fetched:
            response = client.post(f'/post_reply/{reply.id}/check_ai',
                                   data={'csrf_token': token})

    assert b'too short to be sure' in response.data
    assert fetched.call_args is None


def test_the_comment_check_says_when_it_is_not_configured(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author, body='x' * 200)
    client = as_user(app, author)
    token = csrf(app, client)

    with patch.dict(app.config, {'DETECT_AI_ENDPOINT': ''}):
        response = client.post(f'/post_reply/{reply.id}/check_ai',
                               data={'csrf_token': token})

    assert b'Not configured' in response.data


def test_an_anonymous_caller_cannot_check_a_comment(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author, body='x' * 200)

    with patch.dict(app.config, {'DETECT_AI_ENDPOINT': 'https://ai.example/x'}):
        with patch('app.post.routes.get_request') as fetched:
            response = anon.post(f'/post_reply/{reply.id}/check_ai')

    assert response.status_code == 302
    assert fetched.call_args is None


def test_checking_an_unknown_comment(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    token = csrf(app, client)

    with patch.dict(app.config, {'DETECT_AI_ENDPOINT': 'https://ai.example/x'}):
        response = client.post('/post_reply/999999/check_ai',
                               data={'csrf_token': token})

    assert response.status_code == 404


def test_a_private_communitys_comment_cannot_be_checked(app, env):
    anon, community, post, mod, author, outsider = env
    secret, secret_post, member = private_place(app)
    reply = a_reply(secret_post, member, body='x' * 200)
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch.dict(app.config, {'DETECT_AI_ENDPOINT': 'https://ai.example/x'}):
        with patch('app.post.routes.get_request') as fetched:
            response = client.post(f'/post_reply/{reply.id}/check_ai',
                                   data={'csrf_token': token})

    assert response.status_code == 403
    assert fetched.call_args is None


def test_a_detector_that_fails_on_a_comment_says_nothing(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author, body='x' * 200)
    client = as_user(app, author)
    token = csrf(app, client)

    with patch.dict(app.config, {'DETECT_AI_ENDPOINT': 'https://ai.example/x'}):
        with patch('app.post.routes.get_request') as fetched:
            fetched.return_value.status_code = 502
            response = client.post(f'/post_reply/{reply.id}/check_ai',
                                   data={'csrf_token': token})

    assert response.data == b''


# --------------------------------------------------------------------------
# D1109, D1110 -- cross-posting
# --------------------------------------------------------------------------


def test_the_cross_post_page(app, env):
    anon, community, post, mod, author, outsider = env

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        response = as_user(app, author).get(f'/post/{post.id}/cross-post')

    assert response.status_code == 200
    kwargs = render.call_args.kwargs
    assert kwargs['post'].id == post.id
    assert [crumb.text for crumb in kwargs['breadcrumbs']] == \
        ['Home', 'Communities']


def test_the_cross_post_page_refuses_a_private_communitys_post(app, env):
    """D1109. D998 fixed the community LIST on this same form; the post itself
    was still handed over. Measured: `PROBE ao1 cross-post of a private post:
    200 | post handed to the template: SECRETTITLE`."""
    anon, community, post, mod, author, outsider = env
    secret, secret_post, member = private_place(app)

    response = as_user(app, outsider).get(
        f'/post/{secret_post.id}/cross-post')

    assert response.status_code == 403


def test_the_cross_post_page_refuses_an_unpublished_post(app, env):
    anon, community, post, mod, author, outsider = env
    post.status = -2
    post.scheduled_for = utcnow()
    db.session.commit()

    assert as_user(app, outsider).get(
        f'/post/{post.id}/cross-post').status_code == 404


def test_the_last_community_is_remembered(app, env):
    anon, community, post, mod, author, outsider = env
    other = make_community('elsewhere')
    db.session.commit()
    client = as_user(app, author)
    client.set_cookie('cross_post_community_id', str(other.id),
                      domain=app.config['SERVER_NAME'])

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/post/{post.id}/cross-post')

    assert 'elsewhere' in render.call_args.kwargs['form'].which_community.data


def test_a_junk_cookie_is_ignored(app, env):
    """D1110. A cookie is whatever the caller sends: `int('banana')` is
    `ValueError: invalid literal for int() with base 10: 'banana'` -- a 500 on
    a page that has a perfectly good answer without the convenience."""
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    client.set_cookie('cross_post_community_id', 'banana',
                      domain=app.config['SERVER_NAME'])

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        response = client.get(f'/post/{post.id}/cross-post')

    assert response.status_code == 200
    assert render.call_args.kwargs['form'].which_community.data is None


def test_a_cookie_naming_a_community_that_is_gone_is_ignored(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    client.set_cookie('cross_post_community_id', '999999',
                      domain=app.config['SERVER_NAME'])

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        response = client.get(f'/post/{post.id}/cross-post')

    assert response.status_code == 200
    assert render.call_args.kwargs['form'].which_community.data is None


def test_cross_posting_carries_the_post_into_the_new_form(app, env):
    anon, community, post, mod, author, outsider = env
    other = make_community('elsewhere')
    db.session.commit()
    client = as_user(app, author)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/cross-post',
                           data={'which_community': 'elsewhere',
                                 'submit': 'Cross post',
                                 'csrf_token': token})

    assert response.status_code == 302
    assert f'source={post.id}' in response.headers['Location']
    assert 'cross_post_community_id' in response.headers['Set-Cookie']


def test_cross_posting_to_a_community_that_does_not_exist(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    token = csrf(app, client)

    with patch('app.post.routes.flash') as flashed:
        response = client.post(f'/post/{post.id}/cross-post',
                               data={'which_community': 'nosuchplace',
                                     'submit': 'Cross post',
                                     'csrf_token': token})

    assert response.status_code == 302
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'Could not find that community' in messages


def test_the_list_of_cross_posts(app, env):
    anon, community, post, mod, author, outsider = env
    other = make_community('elsewhere')
    db.session.commit()
    cross = make_post(other, author, 'https://test.piefed.local/p/2',
                      title='the same link')
    db.session.commit()
    post.cross_posts = [cross.id]
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        response = anon.get(f'/post/{post.id}/cross_posts')

    assert response.status_code == 200
    assert [p.id for p in render.call_args.kwargs['cross_posts']] == [cross.id]


@pytest.mark.parametrize('cross_posts', [None, []])
def test_a_post_with_no_cross_posts_has_no_list(app, env, cross_posts):
    """`cross_posts` is emptied rather than nulled when the last one goes, so
    the test has to be truthiness and not `is not None`."""
    anon, community, post, mod, author, outsider = env
    post.cross_posts = cross_posts
    db.session.commit()

    assert anon.get(f'/post/{post.id}/cross_posts').status_code == 404


# --------------------------------------------------------------------------
# D1111 -- the community suggestions
# --------------------------------------------------------------------------


def test_the_suggestions_name_communities_you_have_joined(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    token = csrf(app, client)

    response = client.post('/post/search_community_suggestions',
                           data={'which_community': 'gene',
                                 'csrf_token': token})

    assert response.status_code == 200
    assert b'general' in response.data


def test_the_suggestions_do_not_name_private_communities(app, env):
    """D1111. D998's shape at the other end of the same form: the fallback
    search matched on name and `ap_id` with only `banned == False`, so it
    named PRIVATE communities. A private community's EXISTENCE is what its
    membership is meant to withhold. Measured: `PROBE ap1 anonymous
    suggestions: 200 | private named: True`."""
    anon, community, post, mod, author, outsider = env
    secret = make_community('secretplace')
    secret.private = True
    db.session.commit()
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post('/post/search_community_suggestions',
                           data={'which_community': 'secret',
                                 'csrf_token': token})

    assert b'secretplace' not in response.data


def test_a_member_is_offered_their_own_private_community(app, env):
    anon, community, post, mod, author, outsider = env
    secret, secret_post, member = private_place(app, 'secretplace')
    client = as_user(app, member)
    token = csrf(app, client)

    response = client.post('/post/search_community_suggestions',
                           data={'which_community': 'secret',
                                 'csrf_token': token})

    assert b'secretplace' in response.data


def test_the_suggestions_need_an_account(app, env):
    anon, community, post, mod, author, outsider = env

    response = anon.post('/post/search_community_suggestions',
                         data={'which_community': 'gene'})

    assert response.status_code == 302


def test_a_community_you_have_not_joined_is_still_suggested(app, env):
    """The joined and moderated lists come first; the database search behind
    them is what makes a community you have never seen findable."""
    anon, community, post, mod, author, outsider = env
    make_community('generalstrangers')
    db.session.commit()
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post('/post/search_community_suggestions',
                           data={'which_community': 'generalstrangers',
                                 'csrf_token': token})

    assert b'generalstrangers' in response.data


def test_a_banned_community_is_not_suggested(app, env):
    anon, community, post, mod, author, outsider = env
    banned = make_community('generalbanned')
    banned.banned = True
    db.session.commit()
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post('/post/search_community_suggestions',
                           data={'which_community': 'generalbanned',
                                 'csrf_token': token})

    assert b'generalbanned' not in response.data


@pytest.mark.parametrize('query', ['', 'a'])
def test_too_short_a_query_suggests_nothing(app, env, query):
    """Two characters is the floor -- below it every community matches and the
    list is noise."""
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post('/post/search_community_suggestions',
                           data={'which_community': query,
                                 'csrf_token': token})

    assert response.data == b''


def test_the_other_field_name_is_accepted_too(app, env):
    """The same endpoint backs two forms, which name the field differently."""
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    token = csrf(app, client)

    response = client.post('/post/search_community_suggestions',
                           data={'community': 'gene', 'csrf_token': token})

    assert b'general' in response.data


def test_a_community_you_moderate_is_suggested_once(app, env):
    """The moderating list and the joined list overlap, and `already_added` is
    what stops a community being offered twice."""
    anon, community, post, mod, author, outsider = env
    client = as_user(app, mod)
    token = csrf(app, client)

    response = client.post('/post/search_community_suggestions',
                           data={'which_community': 'gene',
                                 'csrf_token': token})

    assert response.data.count(b'general') == 1


# --------------------------------------------------------------------------
# D1112 -- the markdown source fragments
# --------------------------------------------------------------------------


def test_the_source_of_a_post(app, env):
    anon, community, post, mod, author, outsider = env

    response = as_user(app, author).get(
        f'/post/{post.id}/source/show', headers={'HX-Request': 'true'})

    assert response.status_code == 200
    assert b'the body in markdown' in response.data


def test_hiding_the_source_again(app, env):
    anon, community, post, mod, author, outsider = env

    response = as_user(app, author).get(
        f'/post/{post.id}/source/hide', headers={'HX-Request': 'true'})

    assert b'THEBODY' in response.data


def test_the_source_fragment_is_for_htmx_only(app, env):
    anon, community, post, mod, author, outsider = env

    assert as_user(app, author).get(
        f'/post/{post.id}/source/show').status_code == 400


def test_a_private_communitys_source_is_not_shown(app, env):
    """D1112. The markdown source of a post IS the post."""
    anon, community, post, mod, author, outsider = env
    secret, secret_post, member = private_place(app)

    response = as_user(app, outsider).get(
        f'/post/{secret_post.id}/source/show', headers={'HX-Request': 'true'})

    assert b'secret markdown' not in response.data
    assert b'could not be found' in response.data


def test_a_post_with_no_body_has_no_source(app, env):
    """Every link post has `body` None, and `'````' in None` is `TypeError:
    argument of type 'NoneType' is not iterable`."""
    anon, community, post, mod, author, outsider = env
    post.body = None
    post.type = POST_TYPE_LINK
    db.session.commit()

    response = as_user(app, author).get(
        f'/post/{post.id}/source/show', headers={'HX-Request': 'true'})

    assert response.status_code == 200
    assert b'could not be found' in response.data


def test_an_unknown_post_has_no_source(app, env):
    anon, community, post, mod, author, outsider = env

    response = as_user(app, author).get(
        '/post/999999/source/show', headers={'HX-Request': 'true'})

    assert b'could not be found' in response.data


def test_a_state_that_is_not_show_or_hide(app, env):
    anon, community, post, mod, author, outsider = env

    response = as_user(app, author).get(
        f'/post/{post.id}/source/sideways', headers={'HX-Request': 'true'})

    assert b'could not be found' in response.data


def test_a_deleted_posts_source_is_withheld(app, env):
    anon, community, post, mod, author, outsider = env
    post.deleted = True
    db.session.commit()

    response = as_user(app, author).get(
        f'/post/{post.id}/source/show', headers={'HX-Request': 'true'})

    assert b'could not be found' in response.data


def test_an_admin_still_reads_a_deleted_posts_source(app, env):
    """Two rows rather than one, because the first request in a test fixes
    `current_user` for every request after it (fact 514)."""
    anon, community, post, mod, author, outsider = env
    post.deleted = True
    db.session.commit()

    response = as_user(app, db.session.get(User, 1)).get(
        f'/post/{post.id}/source/show', headers={'HX-Request': 'true'})

    assert b'the body in markdown' in response.data


@pytest.mark.parametrize('body, escaped', [
    ('plain text', False),
    ('```\nfenced\n```', False),
    ('````\ndouble fenced\n````', True),
])
def test_source_rendering_survives_its_own_fences(app, env, body, escaped):
    """The source is shown inside a fence one backtick longer than anything in
    the body. A body containing FOUR backticks cannot be fenced at all, so it
    is escaped into a literal `<pre><code>` instead -- and that is the only
    thing that distinguishes the two arms, since both render the text."""
    anon, community, post, mod, author, outsider = env
    post.body = body
    db.session.commit()

    response = as_user(app, author).get(
        f'/post/{post.id}/source/show', headers={'HX-Request': 'true'})

    assert body.splitlines()[-1].encode() in response.data
    assert (b'<pre><code>' in response.data) is escaped


def test_the_source_of_a_comment(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author, body='comment markdown')

    response = as_user(app, author).get(
        f'/post/{post.id}/comment/{reply.id}/source/show',
        headers={'HX-Request': 'true'})

    assert b'comment markdown' in response.data


def test_hiding_a_comments_source_again(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author, body='comment markdown')

    response = as_user(app, author).get(
        f'/post/{post.id}/comment/{reply.id}/source/hide',
        headers={'HX-Request': 'true'})

    assert b'<p>comment markdown</p>' in response.data


def test_the_comment_source_fragment_is_for_htmx_only(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author)

    assert as_user(app, author).get(
        f'/post/{post.id}/comment/{reply.id}/source/show').status_code == 400


def test_a_private_communitys_comment_source_is_not_shown(app, env):
    anon, community, post, mod, author, outsider = env
    secret, secret_post, member = private_place(app)
    reply = a_reply(secret_post, member, body='secret comment markdown')

    response = as_user(app, outsider).get(
        f'/post/{secret_post.id}/comment/{reply.id}/source/show',
        headers={'HX-Request': 'true'})

    assert b'secret comment markdown' not in response.data
    assert b'could not be found' in response.data


def test_an_unknown_comment_has_no_source(app, env):
    anon, community, post, mod, author, outsider = env

    response = as_user(app, author).get(
        f'/post/{post.id}/comment/999999/source/show',
        headers={'HX-Request': 'true'})

    assert b'could not be found' in response.data


def test_a_deleted_comments_source_is_withheld(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author, body='comment markdown', deleted=True)

    response = as_user(app, author).get(
        f'/post/{post.id}/comment/{reply.id}/source/show',
        headers={'HX-Request': 'true'})

    assert b'could not be found' in response.data


def test_an_admin_still_reads_a_deleted_comments_source(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author, body='comment markdown', deleted=True)

    response = as_user(app, db.session.get(User, 1)).get(
        f'/post/{post.id}/comment/{reply.id}/source/show',
        headers={'HX-Request': 'true'})

    assert b'comment markdown' in response.data


@pytest.mark.parametrize('body, escaped', [
    ('plain text', False),
    ('```\nfenced\n```', False),
    ('````\ndouble fenced\n````', True),
])
def test_comment_source_rendering_survives_its_own_fences(app, env, body,
                                                          escaped):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author, body=body)

    response = as_user(app, author).get(
        f'/post/{post.id}/comment/{reply.id}/source/show',
        headers={'HX-Request': 'true'})

    assert (b'<pre><code>' in response.data) is escaped
