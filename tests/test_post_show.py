"""`show_post` -- the page every other post route leads to.

Sub-project 82, slice B. `show_post` is not a route of its own: it is called
by `activitypub.post_ap` (`/post/<id>`), `post_nice`
(`/c/<community>/p/<id>/<slug>`) and the community post routes, so an access
check missing here is missing on every one of those URLs.

* **D1084 (fixed).** A post whose `status` is `POST_STATUS_SCHEDULED` -- not
  yet published -- rendered in full to an anonymous visitor, although the
  scheduled-posts page is scoped to its author and the ActivityPub
  representation of the same post answers 403 for `status <
  POST_STATUS_PUBLISHED`. Ids are sequential, so an embargoed post was
  readable ahead of its time by walking them. Measured: `PROBE af2 scheduled
  status: 200 | body visible: True`.
* **D1085 (fixed, owner ruling).** A post its AUTHOR deleted renders a
  'deleted by author' placeholder for its title and body, its comments still
  shown; moderators and admins see the original.
"""
from unittest.mock import patch

import pytest

from app import db
from app.constants import (POST_STATUS_PUBLISHED, POST_STATUS_SCHEDULED,
                           POST_TYPE_ARTICLE, POST_TYPE_EVENT, POST_TYPE_IMAGE,
                           POST_TYPE_LINK, POST_TYPE_POLL)
from app.models import (CommunityBan, Event, File, Instance, Language, Post,
                        Site, Topic, User, UserFlair)
from app.utils import utcnow
from tests.factories import (make_community, make_community_ban,
                             make_community_member, make_instance, make_poll,
                             make_poll_choice, make_post, make_post_reply,
                             make_user)

pytestmark = pytest.mark.usefixtures('site')


def instance(domain='test.piefed.local', software='piefed'):
    """Fact 394."""
    existing = Instance.query.filter_by(domain=domain).first()
    return existing if existing is not None else make_instance(domain,
                                                               software=software)


def as_user(app, user):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return client


def rendered(app):
    """`show_post` sets Link, ETag and Cache-Control headers on what
    `render_template` returns, so a patch that answers a bare string is
    `AttributeError: 'str' object has no attribute 'headers'` (fact 502)."""
    return app.response_class('rendered')


@pytest.fixture
def env(app, db_session):
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    local = instance()
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1  # fact 347
    mod = make_user(local, 'mod', local=True)
    author = make_user(local, 'author', local=True)
    outsider = make_user(local, 'outsider', local=True)
    community = make_community('general')
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.add(Language(code='en', name='English'))
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
    """Fact 501: `make_post_reply` leaves `body_html` unset."""
    reply = make_post_reply(post, user, body=body)
    reply.body_html = f'<p>{body}</p>'
    for column, value in columns.items():
        setattr(reply, column, value)
    db.session.commit()
    return reply


# --------------------------------------------------------------------------
# D1084 -- a post that has not been published yet
# --------------------------------------------------------------------------


def test_a_scheduled_post_is_not_shown_to_a_stranger(app, env):
    """D1084. `status` is `POST_STATUS_SCHEDULED` until its publication time
    arrives, and both the scheduled-posts page and the ActivityPub
    representation treat that as the author's alone. This page did not.
    Measured: `PROBE af2 scheduled status: 200 | body visible: True`."""
    anon, community, post, mod, author, outsider = env
    post.status = POST_STATUS_SCHEDULED
    post.scheduled_for = utcnow()
    db.session.commit()

    response = as_user(app, outsider).get(f'/post/{post.id}')

    assert response.status_code == 404
    assert b'THEBODY' not in response.data
    assert b'THETITLE' not in response.data


def test_a_scheduled_post_is_not_shown_to_an_anonymous_visitor(app, env):
    """Post ids are sequential, so without the guard an embargoed post was
    readable ahead of its time by anyone walking them. Measured: `PROBE af2b
    title visible: True`."""
    anon, community, post, mod, author, outsider = env
    post.status = POST_STATUS_SCHEDULED
    post.scheduled_for = utcnow()
    db.session.commit()

    response = anon.get(f'/post/{post.id}')

    assert response.status_code == 404
    assert b'THEBODY' not in response.data


def test_the_author_still_sees_their_own_scheduled_post(app, env):
    """The guard must not take the queue away from the person who filled it:
    the flash naming the publication time is only reachable from here."""
    anon, community, post, mod, author, outsider = env
    post.status = POST_STATUS_SCHEDULED
    post.scheduled_for = utcnow()
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        with patch('app.post.routes.flash') as flashed:
            response = as_user(app, author).get(f'/post/{post.id}')

    assert response.status_code == 200
    assert render.call_args.kwargs['post'].id == post.id
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'scheduled to be published' in messages


def test_a_moderator_still_sees_a_scheduled_post(app, env):
    anon, community, post, mod, author, outsider = env
    post.status = POST_STATUS_SCHEDULED
    post.scheduled_for = utcnow()
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)):
        response = as_user(app, mod).get(f'/post/{post.id}')

    assert response.status_code == 200


def test_an_admin_still_sees_a_scheduled_post(app, env):
    anon, community, post, mod, author, outsider = env
    post.status = POST_STATUS_SCHEDULED
    post.scheduled_for = utcnow()
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)):
        response = as_user(app, db.session.get(User, 1)).get(
            f'/post/{post.id}')

    assert response.status_code == 200


def test_a_published_post_is_shown(app, env):
    """The guard reads `status < POST_STATUS_PUBLISHED`, so it must not fire
    on the ordinary case."""
    anon, community, post, mod, author, outsider = env
    assert post.status == POST_STATUS_PUBLISHED

    response = anon.get(f'/post/{post.id}')

    assert response.status_code == 200
    assert b'THEBODY' in response.data


# --------------------------------------------------------------------------
# D1085 -- deletion
# --------------------------------------------------------------------------


def test_a_post_deleted_by_its_author_shows_a_placeholder_and_keeps_its_comments(app, env):
    """D1085, fixed (owner ruling): a post its author deleted still answers
    200 with its comment thread, but a 'deleted by author' placeholder stands
    in place of its title and body -- the page, its <title> and its link
    preview. The title used to stay on show."""
    anon, community, post, mod, author, outsider = env
    a_reply(post, author, body='THEREPLY')
    post.deleted = True
    post.deleted_by = author.id
    db.session.commit()

    with patch('app.post.routes.flash') as flashed:
        response = anon.get(f'/post/{post.id}')

    assert response.status_code == 200
    assert b'THETITLE' not in response.data
    assert b'THEBODY' not in response.data
    assert b'deleted by author' in response.data
    assert b'THEREPLY' in response.data
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'deleted by the author' in messages


def test_a_moderator_sees_the_original_of_a_post_its_author_deleted(app, env):
    """D1085: moderators and admins still see what the author deleted."""
    anon, community, post, mod, author, outsider = env
    post.deleted = True
    post.deleted_by = author.id
    db.session.commit()

    # The signed-in page renders the reply form's csrf_token, which the test
    # config turns off; turned on here for this GET, which needs no token.
    with patch.dict(app.config, {'WTF_CSRF_ENABLED': True}):
        response = as_user(app, mod).get(f'/post/{post.id}')

    assert response.status_code == 200
    assert b'THETITLE' in response.data
    assert b'THEBODY' in response.data


def test_an_events_more_info_url_is_a_link_on_the_page(app, env):
    """R223, fixed (owner ruling): an event's 'More info' link is rendered."""
    from datetime import datetime
    from app.constants import POST_TYPE_EVENT
    from app.models import Event
    anon, community, post, mod, author, outsider = env
    post.type = POST_TYPE_EVENT
    db.session.add(Event(post_id=post.id, start=datetime(2030, 6, 1, 12, 0),
                         end=datetime(2030, 6, 1, 13, 0), timezone='UTC', online=True,
                         more_info_url='https://info.example/event', location={}))
    db.session.commit()

    response = anon.get(f'/post/{post.id}')

    assert response.status_code == 200
    assert b'href="https://info.example/event"' in response.data
    assert b'More info' in response.data


def test_a_post_deleted_by_a_moderator_is_not_found(app, env):
    anon, community, post, mod, author, outsider = env
    post.deleted = True
    post.deleted_by = mod.id
    db.session.commit()

    response = anon.get(f'/post/{post.id}')

    assert response.status_code == 404


def test_a_moderator_sees_a_post_they_deleted(app, env):
    anon, community, post, mod, author, outsider = env
    post.deleted = True
    post.deleted_by = mod.id
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)):
        with patch('app.post.routes.flash') as flashed:
            response = as_user(app, mod).get(f'/post/{post.id}')

    assert response.status_code == 200
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'only visible to staff and admins' in messages


def test_the_author_sees_a_post_a_moderator_deleted(app, env):
    """`current_user.id == post.user_id` is in that test too: the author can
    still reach what was removed, which is how they learn it was."""
    anon, community, post, mod, author, outsider = env
    post.deleted = True
    post.deleted_by = mod.id
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)):
        response = as_user(app, author).get(f'/post/{post.id}')

    assert response.status_code == 200


def test_a_post_in_a_banned_community_is_not_found(app, env):
    anon, community, post, mod, author, outsider = env
    community.banned = True
    db.session.commit()

    response = as_user(app, outsider).get(f'/post/{post.id}')

    assert response.status_code == 404


def test_an_admin_sees_a_post_in_a_banned_community(app, env):
    anon, community, post, mod, author, outsider = env
    community.banned = True
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)):
        response = as_user(app, db.session.get(User, 1)).get(
            f'/post/{post.id}')

    assert response.status_code == 200


def test_an_unknown_post_is_not_found(app, env):
    anon, community, post, mod, author, outsider = env

    assert anon.get('/post/999999').status_code == 404


# --------------------------------------------------------------------------
# Private communities, logged-out readers and content warnings
# --------------------------------------------------------------------------


def test_a_private_communitys_post_is_refused_to_an_anonymous_visitor(app,
                                                                      env):
    anon, community, post, mod, author, outsider = env
    community.private = True
    db.session.commit()

    response = anon.get(f'/post/{post.id}')

    assert response.status_code == 403
    assert b'THEBODY' not in response.data


def test_a_private_communitys_post_is_refused_to_a_non_member(app, env):
    anon, community, post, mod, author, outsider = env
    community.private = True
    db.session.commit()

    response = as_user(app, outsider).get(f'/post/{post.id}')

    assert response.status_code == 403


def test_a_member_reads_their_private_communitys_post(app, env):
    anon, community, post, mod, author, outsider = env
    community.private = True
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        response = as_user(app, author).get(f'/post/{post.id}')

    assert response.status_code == 200
    assert render.call_args.kwargs['post'].id == post.id


@pytest.mark.parametrize('column', ['nsfw', 'nsfl'])
def test_adult_content_asks_an_anonymous_visitor_to_log_in(app, env, column):
    """Anonymous readers are sent to the login page rather than shown the
    post, and the `next` parameter brings them back to it."""
    anon, community, post, mod, author, outsider = env
    setattr(post, column, True)
    db.session.commit()

    response = anon.get(f'/post/{post.id}')

    assert response.status_code == 302
    assert response.headers['Location'] == \
        f'/auth/login?next=/post/{post.id}'


def test_with_a_content_warning_only_nsfl_is_held_back(app, env):
    """With `CONTENT_WARNING` set the instance shows an interstitial for NSFW
    instead of refusing it, so only NSFL still needs an account."""
    anon, community, post, mod, author, outsider = env
    post.nsfw = True
    db.session.commit()

    # Fact 440: with CONTENT_WARNING set, the interstitial redirects every
    # reader who has not accepted it, so the row has to carry the cookie or it
    # measures the interstitial rather than this branch.
    anon.set_cookie('warned', '1', domain=app.config['SERVER_NAME'])
    with patch.dict(app.config, {'CONTENT_WARNING': 1}):
        response = anon.get(f'/post/{post.id}')

    assert response.status_code == 200


def test_with_a_content_warning_nsfl_still_needs_an_account(app, env):
    anon, community, post, mod, author, outsider = env
    post.nsfl = True
    db.session.commit()

    anon.set_cookie('warned', '1', domain=app.config['SERVER_NAME'])
    with patch.dict(app.config, {'CONTENT_WARNING': 1}):
        response = anon.get(f'/post/{post.id}')

    assert response.status_code == 302


def test_adult_content_is_refused_in_a_restricted_country(app, env):
    anon, community, post, mod, author, outsider = env
    post.nsfw = True
    db.session.commit()

    with patch('app.post.routes.user_in_restricted_country',
               return_value=True):
        response = as_user(app, outsider).get(f'/post/{post.id}')

    assert response.status_code == 403


# --------------------------------------------------------------------------
# Caching, slugs and the moderator list
# --------------------------------------------------------------------------


def test_an_unchanged_page_answers_304_to_an_anonymous_visitor(app, env):
    """The conditional-request check sits BELOW every access test, which is
    what D1013 was about: a 304 returned above them answers a caller the
    checks would have refused (fact 449)."""
    anon, community, post, mod, author, outsider = env
    first = anon.get(f'/post/{post.id}')
    etag = first.headers['ETag']

    second = anon.get(f'/post/{post.id}', headers={'If-None-Match': etag})

    assert second.status_code == 304
    # The same string the logged-in row builds by hand, so a typo there cannot
    # make that row pass by never matching.
    assert etag == f'{post.id}hot_{hash(post.last_active)}'


def test_a_logged_in_reader_gets_no_304(app, env):
    """The ETag is only offered to anonymous readers -- a logged-in page
    carries per-account state, so it is `private, max-age=15`."""
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)

    with patch('app.post.routes.render_template',
               return_value=rendered(app)):
        first = client.get(f'/post/{post.id}')
        second = client.get(f'/post/{post.id}',
                            headers={'If-None-Match': f'{post.id}hot_0'})

    assert 'ETag' not in first.headers
    assert second.status_code == 200
    assert second.headers['Cache-Control'] == \
        'private, max-age=15, must-revalidate'


def test_a_logged_in_reader_is_never_answered_304(app, env):
    """The ETag is the same string for everyone -- post id, sort and
    `last_active` -- but the PAGE is not: it carries this account's votes, its
    ban notice and its reply box. So the conditional check is guarded by
    `current_user.is_anonymous`, and a logged-in reader sending the exact
    ETag still gets the whole page. The string is built here rather than read
    from an anonymous response first, because an anonymous request earlier in
    the same test leaves the other client's requests being served as anonymous
    (fact 514)."""
    anon, community, post, mod, author, outsider = env
    etag = f'{post.id}hot_{hash(post.last_active)}'
    client = as_user(app, outsider)

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        response = client.get(f'/post/{post.id}',
                              headers={'If-None-Match': etag})

    assert response.status_code == 200
    assert render.call_args is not None


def test_an_anonymous_page_is_publicly_cacheable(app, env):
    anon, community, post, mod, author, outsider = env

    response = anon.get(f'/post/{post.id}')

    assert response.headers['Cache-Control'] == 'public, max-age=30'
    # `after_request` appends Accept-Encoding to whatever the view set.
    assert response.headers['Vary'].startswith('Accept, Accept-Language')


def test_the_page_advertises_its_activitypub_and_oembed_alternates(app, env):
    anon, community, post, mod, author, outsider = env

    response = anon.get(f'/post/{post.id}')

    links = response.headers.get_all('Link')
    assert any('application/activity+json' in link for link in links)
    assert any('application/json+oembed' in link for link in links)


def test_a_tampered_community_url_is_not_found(app, env):
    """`/c/<community>/p/<id>/<slug>` reaches the same post, and the route
    refuses a path that does not match the post's own slug -- otherwise any
    community's name could be put in front of any post."""
    anon, community, post, mod, author, outsider = env
    post.slug = f'/c/general/p/{post.id}/thetitle'
    db.session.commit()

    response = anon.get(f'/c/general/p/{post.id}/wrong-slug')

    assert response.status_code == 404


def test_the_posts_own_community_url_is_served(app, env):
    anon, community, post, mod, author, outsider = env
    post.slug = f'/c/general/p/{post.id}/thetitle'
    db.session.commit()

    response = anon.get(post.slug)

    assert response.status_code == 200
    assert b'THEBODY' in response.data


def test_the_page_names_the_moderators(app, env):
    anon, community, post, mod, author, outsider = env

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        anon.get(f'/post/{post.id}')

    assert 'mod' in [user.user_name for user in
                     render.call_args.kwargs['mods']]


def test_private_mods_are_not_named(app, env):
    anon, community, post, mod, author, outsider = env
    community.private_mods = True
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        anon.get(f'/post/{post.id}')

    assert render.call_args.kwargs['mods'] == []


def test_a_moderator_is_told_they_are_one(app, env):
    anon, community, post, mod, author, outsider = env

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        as_user(app, mod).get(f'/post/{post.id}')

    assert render.call_args.kwargs['is_moderator'] is True


# --------------------------------------------------------------------------
# Bans, warnings and the reply form
# --------------------------------------------------------------------------


def test_a_banned_reader_is_told_so(app, env):
    anon, community, post, mod, author, outsider = env
    make_community_ban(outsider, community, banned_by=mod)

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        with patch('app.post.routes.flash') as flashed:
            as_user(app, outsider).get(f'/post/{post.id}')

    assert render.call_args.kwargs['banned_from_community'] is True
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'banned from this community' in messages


def test_a_temporary_ban_names_its_end(app, env):
    anon, community, post, mod, author, outsider = env
    ban = make_community_ban(outsider, community, banned_by=mod)
    ban.ban_until = utcnow()
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)):
        with patch('app.post.routes.flash') as flashed:
            as_user(app, outsider).get(f'/post/{post.id}')

    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'banned from this community until' in messages


def test_a_mea_culpa_is_flagged(app, env):
    anon, community, post, mod, author, outsider = env
    post.mea_culpa = True
    db.session.commit()

    with patch('app.post.routes.flash') as flashed:
        anon.get(f'/post/{post.id}')

    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'made a mistake in this post' in messages


def test_the_distinguished_box_is_for_moderators(app, env):
    anon, community, post, mod, author, outsider = env

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        as_user(app, outsider).get(f'/post/{post.id}')

    assert render.call_args.kwargs['form'].distinguished.render_kw == {
        'disabled': True}


def test_a_moderators_reply_box_may_be_distinguished(app, env):
    anon, community, post, mod, author, outsider = env

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        as_user(app, mod).get(f'/post/{post.id}')

    assert render.call_args.kwargs['form'].distinguished.render_kw is None


def test_an_anonymous_reader_is_offered_no_languages(app, env):
    """`languages_for_form` reads `current_user`, so the choices are empty
    rather than an exception for a reader with no account."""
    anon, community, post, mod, author, outsider = env

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        anon.get(f'/post/{post.id}')

    assert render.call_args.kwargs['form'].language_id.choices == []


def test_replying_from_the_post_page(app, env):
    anon, community, post, mod, author, outsider = env
    author.verified = True
    english = Language.query.filter_by(code='en').one()
    db.session.commit()
    made = a_reply(post, author, body='the new one')

    with patch('app.post.routes.make_reply', return_value=made) as make:
        response = as_user(app, author).post(
            f'/post/{post.id}',
            data={'body': 'the new one', 'notify_author': 'y',
                  'language_id': str(english.id), 'submit': 'Comment'})

    assert response.status_code == 302
    assert response.headers['Location'].endswith(f'#comment_{made.id}')
    assert make.call_args.args[2] is None


def test_a_refused_reply_from_the_post_page_says_why(app, env):
    anon, community, post, mod, author, outsider = env
    author.verified = True
    english = Language.query.filter_by(code='en').one()
    db.session.commit()

    with patch('app.post.routes.make_reply',
               side_effect=Exception('you are banned from this community')):
        with patch('app.post.routes.flash') as flashed:
            response = as_user(app, author).post(
                f'/post/{post.id}',
                data={'body': 'rejected', 'notify_author': 'y',
                      'language_id': str(english.id), 'submit': 'Comment'})

    assert response.status_code == 302
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'you are banned from this community' in messages


def test_an_unverified_account_cannot_reply_from_the_post_page(app, env):
    """`current_user.verified` is in the same test as `validate_on_submit`, so
    an unverified account's submission falls through to the page itself rather
    than becoming a comment."""
    anon, community, post, mod, author, outsider = env
    author.verified = False
    english = Language.query.filter_by(code='en').one()
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)):
        with patch('app.post.routes.make_reply') as make:
            response = as_user(app, author).post(
                f'/post/{post.id}',
                data={'body': 'the new one', 'notify_author': 'y',
                      'language_id': str(english.id), 'submit': 'Comment'})

    assert response.status_code == 200
    assert make.call_args is None


# --------------------------------------------------------------------------
# Replies, cross-posts and lazy loading
# --------------------------------------------------------------------------


def test_the_replies_are_loaded_with_the_post(app, env):
    anon, community, post, mod, author, outsider = env
    a_reply(post, author, body='THEREPLY')

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        anon.get(f'/post/{post.id}')

    kwargs = render.call_args.kwargs
    assert kwargs['lazy_load_replies'] is False
    # `post_replies` returns a TREE: {'comment': PostReply, 'replies': [...]},
    # although its annotation says List[PostReply] (fact 510).
    assert [node['comment'].body for node in kwargs['replies']] == \
        ['THEREPLY']


def test_a_busy_post_defers_its_replies(app, env):
    """Past a hundred comments the page is sent without them and the front end
    fetches them separately, so the first paint does not wait on the thread."""
    anon, community, post, mod, author, outsider = env
    a_reply(post, author)

    with patch('app.post.routes.total_comments_on_post_and_cross_posts',
               return_value=101):
        with patch('app.post.routes.render_template',
                   return_value=rendered(app)) as render:
            anon.get(f'/post/{post.id}')

    kwargs = render.call_args.kwargs
    assert kwargs['lazy_load_replies'] is True
    assert kwargs['replies'] == []


def test_a_cross_posts_replies_are_shown_too(app, env):
    """The same link posted in two communities is one conversation, so the
    other community's comments are offered under it."""
    anon, community, post, mod, author, outsider = env
    other = make_community('elsewhere')
    db.session.commit()
    cross = make_post(other, author, 'https://test.piefed.local/p/2',
                      title='the same link')
    cross.body_html = '<p>cross</p>'
    db.session.commit()
    a_reply(cross, author, body='CROSSREPLY')
    post.cross_posts = [cross.id]
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        anon.get(f'/post/{post.id}')

    more = render.call_args.kwargs['more_replies']
    assert [node['comment'].body for node in more[other]] == ['CROSSREPLY']


def test_a_cross_post_with_no_replies_is_not_listed(app, env):
    anon, community, post, mod, author, outsider = env
    other = make_community('elsewhere')
    db.session.commit()
    cross = make_post(other, author, 'https://test.piefed.local/p/2',
                      title='the same link')
    db.session.commit()
    post.cross_posts = [cross.id]
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        anon.get(f'/post/{post.id}')

    assert render.call_args.kwargs['more_replies'] == {}


def test_a_cross_post_in_a_blocked_community_is_not_shown(app, env):
    """The cross-post list is another surface for content the reader has
    already said they do not want, so it is filtered by the same three lists
    as everything else."""
    anon, community, post, mod, author, outsider = env
    other = make_community('elsewhere')
    db.session.commit()
    cross = make_post(other, author, 'https://test.piefed.local/p/2',
                      title='the same link')
    db.session.commit()
    a_reply(cross, author, body='CROSSREPLY')
    post.cross_posts = [cross.id]
    db.session.commit()

    with patch('app.post.routes.blocked_communities',
               return_value=[other.id]):
        with patch('app.post.routes.render_template',
                   return_value=rendered(app)) as render:
            as_user(app, outsider).get(f'/post/{post.id}')

    assert render.call_args.kwargs['more_replies'] == {}


def test_a_cross_post_on_a_blocked_instance_is_not_shown(app, env):
    anon, community, post, mod, author, outsider = env
    remote = make_instance('remote.example', software='lemmy')
    db.session.commit()
    other = make_community('elsewhere')
    other.instance_id = remote.id
    db.session.commit()
    cross = make_post(other, author, 'https://test.piefed.local/p/2',
                      title='the same link')
    db.session.commit()
    a_reply(cross, author, body='CROSSREPLY')
    post.cross_posts = [cross.id]
    db.session.commit()

    with patch('app.post.routes.blocked_or_banned_instances',
               return_value=[remote.id]):
        with patch('app.post.routes.render_template',
                   return_value=rendered(app)) as render:
            as_user(app, outsider).get(f'/post/{post.id}')

    assert render.call_args.kwargs['more_replies'] == {}


def test_the_page_carries_the_communitys_user_flair(app, env):
    anon, community, post, mod, author, outsider = env
    db.session.add(UserFlair(user_id=author.id, community_id=community.id,
                             flair='bronze'))
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        anon.get(f'/post/{post.id}')

    assert render.call_args.kwargs['user_flair'] == {author.id: 'bronze'}


# --------------------------------------------------------------------------
# Breadcrumbs and related communities
# --------------------------------------------------------------------------


def test_a_community_with_no_topic_gets_the_communities_breadcrumb(app, env):
    anon, community, post, mod, author, outsider = env

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        anon.get(f'/post/{post.id}')

    kwargs = render.call_args.kwargs
    assert [crumb.text for crumb in kwargs['breadcrumbs']] == \
        ['Home', 'Communities']
    assert kwargs['related_communities'] == []


def test_a_topics_breadcrumbs_climb_to_the_root(app, env):
    """The trail is built by walking `parent_id` upwards and reversing, so a
    nested topic reads root-first rather than leaf-first."""
    anon, community, post, mod, author, outsider = env
    root = Topic(name='Root', machine_name='root', num_communities=1)
    db.session.add(root)
    db.session.commit()
    leaf = Topic(name='Leaf', machine_name='leaf', parent_id=root.id,
                 num_communities=1)
    db.session.add(leaf)
    db.session.commit()
    community.topic_id = leaf.id
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        anon.get(f'/post/{post.id}')

    crumbs = render.call_args.kwargs['breadcrumbs']
    assert [crumb.text for crumb in crumbs] == \
        ['Home', 'Topics', 'Root', 'Leaf']
    assert [crumb.url for crumb in crumbs] == \
        ['/', '/topics', '/topic/root', '/topic/root/leaf']


def test_the_other_communities_in_the_topic_are_offered(app, env):
    """A banned community is not a suggestion, and neither is the one being
    read."""
    anon, community, post, mod, author, outsider = env
    topic = Topic(name='Root', machine_name='root', num_communities=3)
    db.session.add(topic)
    db.session.commit()
    community.topic_id = topic.id
    sibling = make_community('sibling')
    sibling.topic_id = topic.id
    banned = make_community('banned_one')
    banned.topic_id = topic.id
    banned.banned = True
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        anon.get(f'/post/{post.id}')

    assert [c.name for c in render.call_args.kwargs['related_communities']] \
        == ['sibling']


# --------------------------------------------------------------------------
# Post types: polls, events, links and images
# --------------------------------------------------------------------------


def test_a_poll_arrives_with_its_choices_in_order(app, env):
    anon, community, post, mod, author, outsider = env
    post.type = POST_TYPE_POLL
    db.session.commit()
    make_poll(post)
    # Inserted out of order on purpose (facts 499, 506).
    make_poll_choice(post, 'no', sort_order=1)
    make_poll_choice(post, 'yes', sort_order=0)

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        anon.get(f'/post/{post.id}')

    kwargs = render.call_args.kwargs
    assert [choice.choice_text for choice in kwargs['poll_choices']] == \
        ['yes', 'no']
    assert kwargs['poll_total_votes'] == 0


def test_a_poll_post_with_no_poll_row_still_renders(app, env):
    anon, community, post, mod, author, outsider = env
    post.type = POST_TYPE_POLL
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        response = anon.get(f'/post/{post.id}')

    assert response.status_code == 200
    assert render.call_args.kwargs['poll_data'] is None


def test_a_reader_who_has_voted_is_told_so(app, env):
    anon, community, post, mod, author, outsider = env
    post.type = POST_TYPE_POLL
    db.session.commit()
    make_poll(post)
    make_poll_choice(post, 'yes')

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        with patch('app.models.Poll.has_voted', return_value=True):
            as_user(app, outsider).get(f'/post/{post.id}')

    assert render.call_args.kwargs['has_voted'] is True


def test_an_event_arrives_with_the_post(app, env):
    anon, community, post, mod, author, outsider = env
    post.type = POST_TYPE_EVENT
    db.session.add(Event(post_id=post.id, start=utcnow(), end=utcnow(),
                         timezone='UTC'))
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        anon.get(f'/post/{post.id}')

    assert render.call_args.kwargs['event'].post_id == post.id


@pytest.mark.parametrize('post_type, expected', [
    (POST_TYPE_ARTICLE, True),
    (POST_TYPE_LINK, True),
    (POST_TYPE_POLL, True),
    (POST_TYPE_EVENT, True),
    (POST_TYPE_IMAGE, False),
])
def test_the_creator_tag_is_offered_for_written_posts(app, env, post_type,
                                                      expected):
    """fep-2345's `fediverse:creator` names the author to an aggregator. An
    image post carries no text to attribute, so it is left out."""
    anon, community, post, mod, author, outsider = env
    post.type = post_type
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        anon.get(f'/post/{post.id}')

    creator = render.call_args.kwargs['creator']
    assert (creator is not None) is expected
    if expected:
        assert creator.startswith('@author')


def test_a_paywalled_link_is_offered_an_archive(app, env):
    anon, community, post, mod, author, outsider = env
    post.type = POST_TYPE_LINK
    post.url = 'https://www.nytimes.com/an-article'
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        anon.get(f'/post/{post.id}')

    assert render.call_args.kwargs['archive_link'] == \
        'https://www.removepaywall.com/search?url=https://www.nytimes.com/an-article'


def test_a_link_that_already_names_an_archive_is_left_alone(app, env):
    anon, community, post, mod, author, outsider = env
    post.type = POST_TYPE_LINK
    post.url = 'https://www.nytimes.com/an-article'
    post.body_html = '<p>https://archive.ph/abcde</p>'
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        anon.get(f'/post/{post.id}')

    assert render.call_args.kwargs['archive_link'] is None


def test_an_ordinary_link_is_offered_no_archive(app, env):
    anon, community, post, mod, author, outsider = env
    post.type = POST_TYPE_LINK
    post.url = 'https://example.com/an-article'
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        anon.get(f'/post/{post.id}')

    assert render.call_args.kwargs['archive_link'] is None


def test_an_image_post_offers_its_source_as_the_og_image(app, env):
    anon, community, post, mod, author, outsider = env
    image = File(source_url='https://test.piefed.local/i/1.jpg')
    db.session.add(image)
    db.session.commit()
    post.image_id = image.id
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        anon.get(f'/post/{post.id}')

    assert render.call_args.kwargs['og_image'] == \
        'https://test.piefed.local/i/1.jpg'


def test_the_description_is_the_body_shortened(app, env):
    anon, community, post, mod, author, outsider = env

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        anon.get(f'/post/{post.id}')

    assert render.call_args.kwargs['description'] == 'the body in markdown'


def test_a_post_with_no_body_has_no_description(app, env):
    anon, community, post, mod, author, outsider = env
    post.body = None
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        anon.get(f'/post/{post.id}')

    assert render.call_args.kwargs['description'] is None


# --------------------------------------------------------------------------
# Archived posts, read marks, languages and the author's standing
# --------------------------------------------------------------------------


def test_an_archived_posts_body_comes_from_the_archive(app, env):
    """The archived body is put on the object for the template only, AFTER
    the last commit on this path -- `mark_post_read` commits, and the archive
    text must not reach the database."""
    anon, community, post, mod, author, outsider = env
    post.archived = 'https://archive.example/p/1'
    db.session.commit()

    with patch('app.post.routes.retrieve_archived_post',
               return_value={'body_html': '<p>FROMARCHIVE</p>'}):
        with patch('app.post.routes.render_template',
                   return_value=rendered(app)) as render:
            anon.get(f'/post/{post.id}')

    assert render.call_args.kwargs['post'].body_html == '<p>FROMARCHIVE</p>'
    assert render.call_args.kwargs['sort'] == 'hot'
    assert render.call_args.kwargs['disable_voting'] is True
    db.session.expire(post)
    assert db.session.get(Post, post.id).body_html == '<p>THEBODY</p>'


def test_voting_is_offered_on_a_live_post(app, env):
    anon, community, post, mod, author, outsider = env

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        as_user(app, outsider).get(f'/post/{post.id}')

    kwargs = render.call_args.kwargs
    assert kwargs['disable_voting'] is False
    assert kwargs['can_upvote_here'] is True


def test_a_reader_who_hides_read_posts_has_this_one_marked(app, env):
    anon, community, post, mod, author, outsider = env
    outsider.hide_read_posts = True
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)):
        with patch('app.shared.post.mark_post_read') as marked:
            as_user(app, outsider).get(f'/post/{post.id}')

    assert marked.call_args.args[0] == [post.id]


def test_a_read_mark_covers_the_cross_posts_too(app, env):
    """Reading one of a set of cross-posts is reading all of them, or the
    same link comes back in the feed under another community's name."""
    anon, community, post, mod, author, outsider = env
    outsider.hide_read_posts = True
    other = make_community('elsewhere')
    db.session.commit()
    cross = make_post(other, author, 'https://test.piefed.local/p/2',
                      title='the same link')
    db.session.commit()
    post.cross_posts = [cross.id]
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)):
        with patch('app.shared.post.mark_post_read') as marked:
            as_user(app, outsider).get(f'/post/{post.id}')

    assert marked.call_args.args[0] == [post.id, cross.id]


def test_a_reader_who_does_not_hide_read_posts_is_not_marked(app, env):
    anon, community, post, mod, author, outsider = env

    with patch('app.post.routes.render_template',
               return_value=rendered(app)):
        with patch('app.shared.post.mark_post_read') as marked:
            as_user(app, outsider).get(f'/post/{post.id}')

    assert marked.call_args is None


def test_the_reply_language_follows_the_post(app, env):
    """The reply box is pre-set to the language of what is being replied to,
    so a thread does not drift language by accident."""
    anon, community, post, mod, author, outsider = env
    english = Language.query.filter_by(code='en').one()
    post.language_id = english.id
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        anon.get(f'/post/{post.id}')

    kwargs = render.call_args.kwargs
    assert kwargs['recipient_language_id'] == english.id
    assert kwargs['recipient_language_code'] == 'en'
    assert kwargs['recipient_language_name'] == 'English'


def test_a_post_with_no_language_falls_back_to_its_author(app, env):
    anon, community, post, mod, author, outsider = env
    english = Language.query.filter_by(code='en').one()
    post.language_id = None
    author.language_id = english.id
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        anon.get(f'/post/{post.id}')

    assert render.call_args.kwargs['recipient_language_id'] == english.id


def test_a_banned_author_is_marked_as_one(app, env):
    anon, community, post, mod, author, outsider = env
    author.banned = True
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        anon.get(f'/post/{post.id}')

    assert render.call_args.kwargs['author_banned'] is True


def test_an_author_banned_from_this_community_is_marked_too(app, env):
    """Banned here counts, not only banned everywhere -- the reader is being
    told why this person is not answering."""
    anon, community, post, mod, author, outsider = env
    make_community_ban(author, community, banned_by=mod)

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        anon.get(f'/post/{post.id}')

    assert render.call_args.kwargs['author_banned'] is True


def test_an_ordinary_author_is_not_marked(app, env):
    anon, community, post, mod, author, outsider = env

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        anon.get(f'/post/{post.id}')

    assert render.call_args.kwargs['author_banned'] is False


def test_a_dead_remote_instance_is_announced(app, env):
    """Federation is one-way once the other side is gone, so the page says so
    rather than letting comments look like they were delivered."""
    anon, community, post, mod, author, outsider = env
    remote = make_instance('remote.example', software='lemmy')
    remote.gone_forever = True
    db.session.commit()
    community.instance_id = remote.id
    community.ap_id = 'general@remote.example'
    community.ap_profile_id = 'https://remote.example/c/general'
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        with patch('app.post.routes.flash') as flashed:
            anon.get(f'/post/{post.id}')

    assert render.call_args.kwargs['is_dead'] is True
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'no longer online' in messages


def test_a_local_community_is_never_dead(app, env):
    anon, community, post, mod, author, outsider = env

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        anon.get(f'/post/{post.id}')

    assert render.call_args.kwargs['is_dead'] is False


def test_a_reader_who_wants_no_index_is_honoured(app, env):
    anon, community, post, mod, author, outsider = env
    author.indexable = False
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        anon.get(f'/post/{post.id}')

    assert render.call_args.kwargs['noindex'] is True


def test_staff_are_shown_deleted_comments(app, env):
    anon, community, post, mod, author, outsider = env

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        as_user(app, db.session.get(User, 1)).get(f'/post/{post.id}')

    assert render.call_args.kwargs['show_deleted'] is True


def test_an_ordinary_reader_is_not(app, env):
    anon, community, post, mod, author, outsider = env

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        as_user(app, outsider).get(f'/post/{post.id}')

    assert render.call_args.kwargs['show_deleted'] is False


def test_the_microblogs_community_hides_its_community_actions(app, env):
    """`microblogs` is the local catch-all for federated notes, not a
    community anybody joins, so its join and block controls are dropped."""
    anon, community, post, mod, author, outsider = env
    community.name = 'microblogs'
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        anon.get(f'/post/{post.id}')

    assert render.call_args.kwargs['hide_community_actions'] is True


def test_an_anonymous_readers_voting_history_is_empty(app, env):
    anon, community, post, mod, author, outsider = env

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        anon.get(f'/post/{post.id}')

    kwargs = render.call_args.kwargs
    assert kwargs['recently_upvoted'] == []
    assert kwargs['recently_downvoted_replies'] == []
    assert kwargs['reply_collapse_threshold'] == -10


def test_a_readers_collapse_setting_is_used(app, env):
    anon, community, post, mod, author, outsider = env
    outsider.reply_collapse_threshold = -5
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        as_user(app, outsider).get(f'/post/{post.id}')

    assert render.call_args.kwargs['reply_collapse_threshold'] == -5


def test_no_collapse_setting_means_collapse_nothing(app, env):
    """`0` is falsy and would read as "collapse everything", so the fallback
    is a threshold no comment reaches."""
    anon, community, post, mod, author, outsider = env
    outsider.reply_collapse_threshold = 0
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        as_user(app, outsider).get(f'/post/{post.id}')

    assert render.call_args.kwargs['reply_collapse_threshold'] == -1000
