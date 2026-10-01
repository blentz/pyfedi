"""The rest of `app/user/routes.py`: reading history, OAuth, feeds, follows.

Sub-project 81, slice H -- the last slice of this module. One finding
registered then, since fixed:

* an account created through OAuth never gets a password (`initialize_new_user`
  in `app/auth/oauth_util.py` does not call `set_password`), and
  `connect_oauth` will disconnect its only provider -- leaving an account with
  no password and no provider, reachable only through an emailed password
  reset. Measured: `PROBE ab1 google id now: None | password_hash: None`.
  D1073, fixed (owner ruling): the last login method can no longer be
  disconnected.
"""
from datetime import timedelta
from unittest.mock import patch

import pytest

from app import db
from app.constants import POST_STATUS_SCHEDULED
from app.models import (Community, Feed, FeedMember, Instance, Post, PostVote,
                        Site, User, UserFollower, read_posts)
from app.utils import utcnow
from tests.factories import (grant_permission, make_community,
                             make_community_member, make_instance, make_post,
                             make_user)

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
    viewer = make_user(local, 'viewer', local=True)
    other = make_user(local, 'other', local=True)
    db.session.commit()
    for user in (viewer, other):
        user.ap_profile_id = f'https://test.piefed.local/u/{user.user_name}'
    db.session.commit()
    community = make_community('general')
    db.session.commit()
    return as_user(app, viewer), viewer, other, community


def a_post(community, author, number=1, **columns):
    post = make_post(community, author,
                     f'https://test.piefed.local/p/{number}',
                     title=f'post {number}')
    for column, value in columns.items():
        setattr(post, column, value)
    db.session.commit()
    return post


def mark_read(user, post):
    db.session.execute(read_posts.insert().values(user_id=user.id,
                                                  read_post_id=post.id,
                                                  interacted_at=utcnow()))
    db.session.commit()


# --------------------------------------------------------------------------
# Reading history
# --------------------------------------------------------------------------


def test_the_reading_history_lists_what_you_have_read(app, env):
    client, viewer, other, community = env
    read = a_post(community, other, 1, title='one I read')
    a_post(community, other, 2, title='one I did not')
    mark_read(viewer, read)

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = client.get('/read-posts')

    assert response.status_code == 200
    assert [p.title for p in render.call_args.kwargs['posts'].items] == ['one I read']


def test_the_reading_history_is_yours_alone(app, env):
    client, viewer, other, community = env
    theirs = a_post(community, other, 1, title='they read it')
    mark_read(other, theirs)

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/read-posts')

    assert render.call_args.kwargs['posts'].items == []


@pytest.mark.parametrize('preference, column', [
    ('ignore_bots', 'from_bot'),
    ('hide_nsfl', 'nsfl'),
    ('hide_nsfw', 'nsfw'),
])
def test_the_reading_history_respects_the_content_preferences(app, env,
                                                              preference,
                                                              column):
    """Three separate filters over the same query, and each needs both
    directions -- `hide_nsfw` and `hide_nsfl` default to 1 (fact 443)."""
    client, viewer, other, community = env
    plain = a_post(community, other, 1, title='plain')
    flagged = a_post(community, other, 2, title='flagged', **{column: True})
    mark_read(viewer, plain)
    mark_read(viewer, flagged)
    for name in ('ignore_bots', 'hide_nsfl', 'hide_nsfw'):
        setattr(viewer, name, 0)
    db.session.commit()

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/read-posts')
    assert len(render.call_args.kwargs['posts'].items) == 2

    setattr(viewer, preference, 1)
    db.session.commit()

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/read-posts')
    assert [p.title for p in render.call_args.kwargs['posts'].items] == ['plain']


def test_a_deleted_post_is_not_in_the_reading_history(app, env):
    client, viewer, other, community = env
    gone = a_post(community, other, 1, deleted=True)
    mark_read(viewer, gone)

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/read-posts')

    assert render.call_args.kwargs['posts'].items == []


@pytest.mark.parametrize('sort', ['hot', 'top', 'new', 'oldest', 'active'])
def test_the_reading_history_sorts(app, env, sort):
    """Five arms; `top` also filters to the last week, so a row for it has to
    use a recent post."""
    client, viewer, other, community = env
    post = a_post(community, other, 1, posted_at=utcnow())
    mark_read(viewer, post)

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = client.get(f'/read-posts/{sort}')

    assert response.status_code == 200
    assert [p.title for p in render.call_args.kwargs['posts'].items] == ['post 1']


def test_the_top_sort_drops_older_posts(app, env):
    """`top` is the one arm that filters as well as ordering."""
    client, viewer, other, community = env
    old = a_post(community, other, 1, title='old',
                 posted_at=utcnow() - timedelta(days=30))
    mark_read(viewer, old)

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/read-posts/top')

    assert render.call_args.kwargs['posts'].items == []


def test_deleting_the_reading_history(app, env):
    client, viewer, other, community = env
    mine = a_post(community, other, 1)
    theirs = a_post(community, other, 2)
    mark_read(viewer, mine)
    mark_read(other, theirs)
    token = csrf(app, client)

    response = client.post('/read-posts/delete', data={'csrf_token': token})

    assert response.status_code == 302
    remaining = db.session.execute(
        db.text('SELECT user_id FROM "read_posts"')).all()
    assert [row[0] for row in remaining] == [other.id]


def test_deleting_the_reading_history_is_not_a_get(app, env):
    """The delete route is POST-only, and a GET to the same path does NOT 405:
    `/read-posts/<sort>` is registered on the same prefix, so the GET is read
    as a sort named "delete" and renders the history unsorted. Ugly, but the
    history survives, which is what this row is for."""
    client, viewer, other, community = env
    post = a_post(community, other, 1)
    mark_read(viewer, post)

    with patch('app.user.routes.render_template', return_value='rendered'):
        response = client.get('/read-posts/delete')

    assert response.status_code == 200
    assert db.session.execute(db.text('SELECT count(*) FROM "read_posts"')).scalar() == 1


# --------------------------------------------------------------------------
# Hidden posts, scheduled posts, upvotes
# --------------------------------------------------------------------------


def test_the_hidden_posts_list(app, env):
    from app.models import hidden_posts

    client, viewer, other, community = env
    hidden = a_post(community, other, 1, title='hidden')
    a_post(community, other, 2, title='not hidden')
    db.session.execute(hidden_posts.insert().values(user_id=viewer.id,
                                                    hidden_post_id=hidden.id))
    db.session.commit()

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = client.get('/hidden_posts')

    assert response.status_code == 200
    assert [p.title for p in render.call_args.kwargs['posts'].items] == ['hidden']


def test_the_hidden_posts_list_is_yours_alone(app, env):
    from app.models import hidden_posts

    client, viewer, other, community = env
    theirs = a_post(community, other, 1)
    db.session.execute(hidden_posts.insert().values(user_id=other.id,
                                                    hidden_post_id=theirs.id))
    db.session.commit()

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/hidden_posts')

    assert render.call_args.kwargs['posts'].items == []


def test_the_scheduled_posts_list(app, env):
    client, viewer, other, community = env
    scheduled = a_post(community, viewer, 1, title='scheduled',
                       status=POST_STATUS_SCHEDULED,
                       scheduled_for=utcnow() + timedelta(days=1))
    a_post(community, viewer, 2, title='published')

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = client.get('/scheduled_posts')

    assert response.status_code == 200
    assert [p.title for p in render.call_args.kwargs['entities']] == ['scheduled']


def test_the_scheduled_posts_list_is_yours_alone(app, env):
    client, viewer, other, community = env
    a_post(community, other, 1, status=POST_STATUS_SCHEDULED,
           scheduled_for=utcnow() + timedelta(days=1))

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/scheduled_posts')

    assert render.call_args.kwargs['entities'] == []


def test_your_own_upvotes_are_visible_to_you(app, env):
    """`_get_user_upvoted_posts` answers only for the account itself or an
    admin -- an upvote history is a reading history by another name."""
    client, viewer, other, community = env
    post = a_post(community, other, 1, title='upvoted')
    db.session.add(PostVote(user_id=viewer.id, post_id=post.id, effect=1,
                            author_id=other.id))
    db.session.commit()

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = client.get('/u/viewer/upvotes')

    assert response.status_code == 200
    assert [p.title for p in render.call_args.kwargs['upvoted']] == ['upvoted']


def test_somebody_elses_upvotes_are_not(app, env):
    client, viewer, other, community = env
    post = a_post(community, viewer, 1, title='upvoted')
    db.session.add(PostVote(user_id=other.id, post_id=post.id, effect=1,
                            author_id=viewer.id))
    db.session.commit()

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/u/other/upvotes')

    assert render.call_args.kwargs['upvoted'] == []


def test_an_admin_can_see_anybodys_upvotes(app, env):
    client, viewer, other, community = env
    post = a_post(community, viewer, 1, title='upvoted')
    db.session.add(PostVote(user_id=other.id, post_id=post.id, effect=1,
                            author_id=viewer.id))
    db.session.commit()
    admin = as_user(app, db.session.get(User, 1))

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        admin.get('/u/other/upvotes')

    assert [p.title for p in render.call_args.kwargs['upvoted']] == ['upvoted']


def test_the_upvotes_of_an_unknown_account_are_a_404(app, env):
    """D1074. `_get_user_upvoted_posts` reads `user.id` and was called BEFORE
    the `if user is not None` check three lines below, so an actor this
    instance cannot resolve was `AttributeError: 'NoneType' object has no
    attribute 'id'` rather than the 404 the function goes on to produce.
    D992's shape: the guard existed, in the wrong place."""
    client, viewer, other, community = env

    response = client.get('/u/nobody/upvotes')

    assert response.status_code == 404


# --------------------------------------------------------------------------
# Feeds, preview, lookup
# --------------------------------------------------------------------------


def test_a_users_public_feeds_page(app, env):
    client, viewer, other, community = env
    public = Feed(user_id=other.id, title='Public', name='public',
                  public=True,
                  ap_profile_id='https://test.piefed.local/f/public')
    private = Feed(user_id=other.id, title='Private', name='private',
                   public=False,
                   ap_profile_id='https://test.piefed.local/f/private')
    db.session.add_all([public, private])
    db.session.commit()

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = client.get('/u/other/feeds')

    assert response.status_code == 200
    assert render.call_args.kwargs['user_has_public_feeds'] is True
    assert [f.title for f in render.call_args.kwargs['user_feeds_list']] == ['Public']


def test_a_user_with_no_public_feeds(app, env):
    client, viewer, other, community = env

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/u/other/feeds')

    assert render.call_args.kwargs['user_has_public_feeds'] is False


def test_the_feeds_of_an_unknown_account_are_a_404(app, env):
    client, viewer, other, community = env

    response = client.get('/u/nobody/feeds')

    assert response.status_code == 404


def test_your_own_feeds_page(app, env):
    """`myfeeds` lists the feeds you own and the ones you have joined, and the
    joined list excludes the ones you own."""
    client, viewer, other, community = env
    mine = Feed(user_id=viewer.id, title='Mine', name='mine',
                ap_profile_id='https://test.piefed.local/f/mine')
    theirs = Feed(user_id=other.id, title='Theirs', name='theirs',
                  ap_profile_id='https://test.piefed.local/f/theirs')
    db.session.add_all([mine, theirs])
    db.session.commit()
    db.session.add_all([
        FeedMember(user_id=viewer.id, feed_id=mine.id, is_owner=True),
        FeedMember(user_id=viewer.id, feed_id=theirs.id, is_owner=False),
    ])
    db.session.commit()

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = client.get('/u/viewer/myfeeds')

    assert response.status_code == 200
    assert render.call_args.kwargs['user_has_feeds'] is True
    assert [f.title for f in render.call_args.kwargs['user_feeds_list']] == ['Mine']
    assert [f.title for f in render.call_args.kwargs['subbed_feeds']] == ['Theirs']


def test_your_feeds_page_with_nothing_on_it(app, env):
    client, viewer, other, community = env

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/u/viewer/myfeeds')

    assert render.call_args.kwargs['user_has_feeds'] is False
    assert render.call_args.kwargs['user_has_feed_subscriptions'] is False


def test_the_profile_preview(app, env):
    client, viewer, other, community = env

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = client.get(f'/user/{other.id}/preview?return_to=/c/general')

    assert response.status_code == 200
    assert render.call_args.kwargs['user'].id == other.id
    assert render.call_args.kwargs['return_to'] == '/c/general'


@pytest.mark.parametrize('column', ['deleted', 'banned'])
def test_an_anonymous_visitor_gets_no_preview_of_a_removed_account(app, env,
                                                                   column):
    client, viewer, other, community = env
    setattr(other, column, True)
    db.session.commit()

    response = app.test_client().get(f'/user/{other.id}/preview')

    assert response.status_code == 404


def test_previewing_an_unknown_account_is_a_404(app, env):
    client, viewer, other, community = env

    response = client.get('/user/9999/preview')

    assert response.status_code == 404


def test_looking_up_a_local_account_redirects_to_it(app, env):
    client, viewer, other, community = env

    response = client.get('/user/lookup/other/test.piefed.local')

    assert response.status_code == 302
    assert response.headers['Location'] == '/u/other'


def test_looking_up_a_known_remote_account_redirects_to_it(app, env):
    """`if exists:` is the shortcut that avoids a SEARCH for an account this
    instance already holds -- and the row has to assert that no search
    happened, because the search would find the same person and redirect to
    the same place. The Location alone cannot tell the two paths apart."""
    client, viewer, other, community = env
    remote = make_user(make_instance('other.example', software='piefed'),
                       'remote')
    remote.ap_domain = 'other.example'
    db.session.commit()

    with patch('app.user.routes.search_for_user') as search:
        response = client.get('/user/lookup/remote/other.example')

    assert response.status_code == 302
    assert response.headers['Location'] == '/u/remote@other.example'
    assert search.call_args is None


def test_looking_up_an_unknown_account_searches_for_it(app, env):
    client, viewer, other, community = env
    found = make_user(make_instance('other.example', software='piefed'),
                      'found')
    db.session.commit()

    found.ap_id = 'found@other.example'
    db.session.commit()

    with patch('app.user.routes.search_for_user', return_value=found) as search:
        response = client.get('/user/lookup/seeking/other.example')

    assert search.call_args.args == ('@seeking@other.example',)
    assert response.status_code == 302
    assert response.headers['Location'] == '/u/found@other.example'


def test_an_anonymous_lookup_is_sent_to_log_in(app, env):
    """Searching makes this instance fetch a remote actor, so it is not
    something an anonymous visitor can trigger."""
    client, viewer, other, community = env

    with patch('app.user.routes.search_for_user') as search:
        response = app.test_client().get('/user/lookup/seeking/other.example')

    assert response.status_code == 302
    assert search.call_args is None


# --------------------------------------------------------------------------
# Following, and the bot challenge
# --------------------------------------------------------------------------


def test_following_somebody(app, env):
    client, viewer, other, community = env
    token = csrf(app, client)

    with patch('app.user.routes.follow_user') as follow:
        response = client.post('/u/other/follow', data={'csrf_token': token})

    assert response.status_code in (200, 302)
    assert follow.call_args.args[0] == other.id


def test_following_answers_htmx_with_the_new_state(app, env):
    """A profile that manually approves followers gets 'Requested'; one that
    does not gets 'Done'."""
    client, viewer, other, community = env
    other.ap_manually_approves_followers = True
    db.session.commit()
    token = csrf(app, client)

    with patch('app.user.routes.follow_user'):
        response = client.post('/u/other/follow', data={'csrf_token': token},
                               headers={'HX-Request': 'true'})

    assert b'Requested' in response.data


def test_following_an_open_profile_answers_done(app, env):
    client, viewer, other, community = env
    other.ap_manually_approves_followers = False
    db.session.commit()
    token = csrf(app, client)

    with patch('app.user.routes.follow_user'):
        response = client.post('/u/other/follow', data={'csrf_token': token},
                               headers={'HX-Request': 'true'})

    assert b'Done' in response.data


def test_unfollowing_somebody(app, env):
    client, viewer, other, community = env
    token = csrf(app, client)

    with patch('app.user.routes.unfollow_user') as unfollow:
        response = client.post('/u/other/unfollow',
                               data={'csrf_token': token})

    assert response.status_code in (200, 302)
    assert unfollow.call_args.args[0] == other.id


def test_following_an_unknown_account_is_a_404(app, env):
    client, viewer, other, community = env
    token = csrf(app, client)

    response = client.post('/u/nobody/follow', data={'csrf_token': token})

    assert response.status_code == 404


def test_sending_a_bot_challenge(app, env):
    """`@permission_required('change instance settings')` -- a bot challenge
    flags somebody's account, so it is an admin action rather than a
    neighbourly one."""
    client, viewer, other, community = env
    grant_permission(viewer, 'change instance settings')
    db.session.commit()
    token = csrf(app, client)

    with patch('app.user.routes.bot_challenge_user') as challenge:
        with patch('app.user.routes.flash') as flashed:
            response = client.post('/u/other/bot_challenge',
                                   data={'csrf_token': token})

    assert response.status_code in (200, 302)
    assert challenge.call_args.args[0] == other.id
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'Bot challenge was sent' in messages


def test_a_bot_challenge_answers_htmx(app, env):
    client, viewer, other, community = env
    grant_permission(viewer, 'change instance settings')
    db.session.commit()
    token = csrf(app, client)

    with patch('app.user.routes.bot_challenge_user'):
        response = client.post('/u/other/bot_challenge',
                               data={'csrf_token': token},
                               headers={'HX-Request': 'true'})

    assert b'Done' in response.data


def test_challenging_an_unknown_account_is_a_404(app, env):
    client, viewer, other, community = env
    grant_permission(viewer, 'change instance settings')
    db.session.commit()
    token = csrf(app, client)

    response = client.post('/u/nobody/bot_challenge',
                           data={'csrf_token': token})

    assert response.status_code == 404


@pytest.mark.parametrize('path', ['follow', 'unfollow', 'bot_challenge'])
def test_these_routes_refuse_an_off_site_return(app, env, path):
    """`return_to_or_401` refuses an absolute url with 401 rather than
    replacing it -- these three are reached from other people's pages, so the
    return target is worth being strict about."""
    client, viewer, other, community = env
    grant_permission(viewer, 'change instance settings')
    db.session.commit()
    token = csrf(app, client)

    with patch('app.user.routes.follow_user'):
        with patch('app.user.routes.unfollow_user'):
            with patch('app.user.routes.bot_challenge_user'):
                response = client.post(
                    f'/u/other/{path}?return_to=https://evil.example/',
                    data={'csrf_token': token})

    assert response.status_code == 401


# --------------------------------------------------------------------------
# OAuth connections -- and D1073, the last login method kept
# --------------------------------------------------------------------------


def test_the_oauth_page_reports_what_is_connected(app, env):
    client, viewer, other, community = env
    viewer.google_oauth_id = 'google-123'
    db.session.commit()

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = client.get('/user/connect_oauth')

    assert response.status_code == 200
    connections = render.call_args.kwargs['oauth_connections']
    assert connections == {'google': True, 'discord': False,
                           'mastodon': False}


def test_the_oauth_page_reports_which_providers_exist(app, env):
    """A provider with no client id configured cannot be connected, so the
    page must not offer it."""
    client, viewer, other, community = env
    app.config['GOOGLE_OAUTH_CLIENT_ID'] = 'something'
    app.config['MASTODON_OAUTH_CLIENT_ID'] = ''
    app.config['DISCORD_OAUTH_CLIENT_ID'] = ''
    try:
        with patch('app.user.routes.render_template',
                   return_value='rendered') as render:
            client.get('/user/connect_oauth')
    finally:
        app.config['GOOGLE_OAUTH_CLIENT_ID'] = ''

    assert render.call_args.kwargs['oauth_providers'] == {
        'google': True, 'mastodon': False, 'discord': False}


@pytest.mark.parametrize('provider, column', [
    ('google', 'google_oauth_id'),
    ('discord', 'discord_oauth_id'),
    ('mastodon', 'mastodon_oauth_id'),
])
def test_disconnecting_a_provider(app, env, provider, column):
    """Three arms, three columns; a copy-paste error would clear the wrong
    one and leave the account connected to a provider it thinks it dropped."""
    client, viewer, other, community = env
    for name in ('google_oauth_id', 'discord_oauth_id', 'mastodon_oauth_id'):
        setattr(viewer, name, f'{name}-123')
    viewer.set_password('a password')
    db.session.commit()
    token = csrf(app, client)

    response = client.post('/user/connect_oauth',
                           data={'disconnect_provider': provider,
                                 'csrf_token': token})

    assert response.status_code == 302
    db.session.refresh(viewer)
    assert getattr(viewer, column) is None
    others = [n for n in ('google_oauth_id', 'discord_oauth_id',
                          'mastodon_oauth_id') if n != column]
    assert all(getattr(viewer, n) is not None for n in others)


def test_an_unknown_provider_disconnects_nothing(app, env):
    client, viewer, other, community = env
    viewer.google_oauth_id = 'google-123'
    db.session.commit()
    token = csrf(app, client)

    with patch('app.user.routes.render_template', return_value='rendered'):
        response = client.post('/user/connect_oauth',
                               data={'disconnect_provider': 'nonsense',
                                     'csrf_token': token})

    assert response.status_code == 200
    db.session.refresh(viewer)
    assert viewer.google_oauth_id == 'google-123'


def test_an_oauth_only_account_cannot_disconnect_its_last_provider(app, env):
    """D1073, fixed (owner ruling): an account created through OAuth has no
    password, so disconnecting its only provider would leave it no way to log
    in. The disconnect is refused and the flash says to set a password first.
    """
    client, viewer, other, community = env
    viewer.password_hash = None
    viewer.google_oauth_id = 'google-123'
    db.session.commit()
    token = csrf(app, client)

    response = client.post('/user/connect_oauth',
                           data={'disconnect_provider': 'google', 'csrf_token': token})

    assert response.status_code == 302
    db.session.refresh(viewer)
    assert viewer.google_oauth_id == 'google-123'
    with client.session_transaction() as session:
        messages = [message for _category, message in session.get('_flashes', [])]
    assert any('Set a password first' in message for message in messages)


def test_an_oauth_only_account_may_disconnect_one_of_two_providers(app, env):
    """D1073: another provider is still a way to log in, so this one may go."""
    client, viewer, other, community = env
    viewer.password_hash = None
    viewer.google_oauth_id = 'google-123'
    viewer.discord_oauth_id = 'discord-123'
    db.session.commit()
    token = csrf(app, client)

    client.post('/user/connect_oauth',
                data={'disconnect_provider': 'google', 'csrf_token': token})

    db.session.refresh(viewer)
    assert viewer.google_oauth_id is None
    assert viewer.discord_oauth_id == 'discord-123'


def test_a_deleted_account_has_no_oauth_page(app, env):
    client, viewer, other, community = env
    viewer.deleted = True
    db.session.commit()

    response = client.get('/user/connect_oauth')

    assert response.status_code == 404


def test_the_people_page_redirects_to_the_instance_list(app, env):
    client, viewer, other, community = env

    response = client.get('/people')

    assert response.status_code == 302
    assert 'test.piefed.local' in response.headers['Location']


# --------------------------------------------------------------------------
# The remote-handle arms, and the last few branches
# --------------------------------------------------------------------------


def a_remote(name='remote', host='other.example'):
    """A remote account addressable as `name@host`, which is the arm every
    one of these routes reaches through `'@' in actor`."""
    other_instance = Instance.query.filter_by(domain=host).first() or \
        make_instance(host, software='piefed')
    user = make_user(other_instance, name)
    user.ap_id = f'{name}@{host}'
    user.ap_domain = host
    user.ap_inbox_url = f'https://{host}/u/{name}/inbox'
    db.session.commit()
    return user


@pytest.mark.parametrize('path, patched', [
    ('follow', 'follow_user'),
    ('unfollow', 'unfollow_user'),
])
def test_following_a_remote_account_by_handle(app, env, path, patched):
    client, viewer, other, community = env
    remote = a_remote()
    token = csrf(app, client)

    with patch(f'app.user.routes.{patched}') as action:
        response = client.post(f'/u/remote@other.example/{path}',
                               data={'csrf_token': token})

    assert response.status_code in (200, 302)
    assert action.call_args.args[0] == remote.id


def test_unfollowing_answers_htmx(app, env):
    client, viewer, other, community = env
    token = csrf(app, client)

    with patch('app.user.routes.unfollow_user'):
        response = client.post('/u/other/unfollow',
                               data={'csrf_token': token},
                               headers={'HX-Request': 'true'})

    assert b'Done' in response.data


def test_unfollowing_an_unknown_account_is_a_404(app, env):
    client, viewer, other, community = env
    token = csrf(app, client)

    response = client.post('/u/nobody/unfollow', data={'csrf_token': token})

    assert response.status_code == 404


def test_challenging_a_remote_account_by_handle(app, env):
    client, viewer, other, community = env
    remote = a_remote()
    grant_permission(viewer, 'change instance settings')
    db.session.commit()
    token = csrf(app, client)

    with patch('app.user.routes.bot_challenge_user') as challenge:
        client.post('/u/remote@other.example/bot_challenge',
                    data={'csrf_token': token})

    assert challenge.call_args.args[0] == remote.id


@pytest.mark.parametrize('path', ['upvotes', 'feeds'])
def test_a_remote_accounts_pages_are_found_by_handle(app, env, path):
    """Both routes resolve through `find_actor_or_create`, whose `'@' in
    actor` arm is a separate line from the local-URL one."""
    client, viewer, other, community = env
    a_remote()

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = client.get(f'/u/remote@other.example/{path}')

    assert response.status_code == 200
    assert render.call_args is not None


def test_toggling_notifications_for_a_user(app, env):
    client, viewer, other, community = env
    token = csrf(app, client)

    with patch('app.user.routes.subscribe_user',
               return_value='done') as subscribe:
        response = client.post(f'/user/{other.id}/notification',
                               data={'csrf_token': token})

    assert response.status_code == 200
    assert subscribe.call_args.args[0] == other.id


def test_toggling_notifications_for_an_unknown_user_is_a_404(app, env):
    """N4, fixed: the 404 now comes from subscribe_user itself (D559), so the
    route's `except NoResultFound` was dead and is gone; the real call is
    made here rather than a patched one raising what it no longer raises."""
    client, viewer, other, community = env
    token = csrf(app, client)

    response = client.post('/user/9999/notification',
                           data={'csrf_token': token})

    assert response.status_code == 404


@pytest.mark.parametrize('sort', ['oldest', 'old'])
def test_the_reading_history_sorts_oldest_first(app, env, sort):
    """D1076. The arm was `'oldest'` only, and the page's nav links exactly
    that -- but `sort` DEFAULTS to `current_user.default_sort`, whose choices
    call the same order `'old'` (`SettingsForm.sorts`, and every other listing
    in the application). So an account whose default sort is "Old" matched no
    arm and got the list in whatever order the database chose.

    Found because a mutation on the `asc()` in this arm SURVIVED: the row was
    asking for `/read-posts/old`, which reached no `order_by` at all."""
    client, viewer, other, community = env
    # The NEWER post is created first on purpose. An arm that reaches no
    # `order_by` returns the rows in insertion order, so a row that inserts
    # oldest-first passes whether or not the sort was applied -- which is how
    # D1076 survived its own mutation until this was turned around.
    newer = a_post(community, other, 1, title='newer', posted_at=utcnow())
    older = a_post(community, other, 2, title='older',
                   posted_at=utcnow() - timedelta(days=2))
    mark_read(viewer, newer)
    mark_read(viewer, older)

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/read-posts/{sort}')

    # The order of the two posts this row created, filtered out of whatever
    # else the listing holds: asserting the WHOLE list made the row depend on
    # nothing in the rest of the suite having left a readable post behind, and
    # it failed that way in the full run while passing alone.
    titles = [p.title for p in render.call_args.kwargs['posts'].items
              if p.title in ('older', 'newer')]
    assert titles == ['older', 'newer']


def test_a_lookup_of_a_blocked_instance_says_so(app, env):
    """`if 'is blocked.' in str(e)` -- the one exception this function reads
    rather than letting through."""
    client, viewer, other, community = env

    with patch('app.user.routes.search_for_user',
               side_effect=Exception('that instance is blocked.')):
        with patch('app.user.routes.flash') as flashed:
            client.get('/user/lookup/seeking/blocked.example')

    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'blocked' in messages
