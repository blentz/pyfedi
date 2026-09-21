"""The profile page, the notification list and the alert lists.

Sub-project 81, slice F. One defect, measured:

* `/notifications?type=abc` was `ValueError: invalid literal for int() with
  base 10: 'abc'` -- **D1043's shape at its second site** (D1068). Fixing
  `notifications_all_read` one slice earlier did not touch this one, which is
  fact 478's lesson repeating: the same feature has two ends.
"""
from unittest.mock import patch

import pytest

from app import db
from app.constants import NOTIF_COMMUNITY, NOTIF_DEFAULT, NOTIF_REPLY
from app.models import (Community, CommunityMember, Feed, Instance,
                        Notification, NotificationSubscription, Post,
                        PostReply, Site, User, UserFollower)
from tests.factories import (make_community, make_community_member,
                             make_instance, make_post, make_post_reply,
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


@pytest.fixture
def env(app, db_session):
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    local = instance()
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1  # fact 347
    viewer = make_user(local, 'viewer', local=True)
    subject = make_user(local, 'subject', local=True)
    db.session.commit()
    for user in (viewer, subject):
        user.ap_profile_id = f'https://test.piefed.local/u/{user.user_name}'
    db.session.commit()
    return as_user(app, viewer), viewer, subject


def a_notification(user, read=False, notif_type=NOTIF_REPLY, title='a notification'):
    notification = Notification(user_id=user.id, title=title, url='/',
                                author_id=user.id, read=read,
                                notif_type=notif_type)
    db.session.add(notification)
    db.session.commit()
    return notification


# --------------------------------------------------------------------------
# The profile page
# --------------------------------------------------------------------------


def show(app, client, actor='subject', query=''):
    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = client.get(f'/u/{actor}{query}')
    return response, render


def test_a_profile_renders(app, env):
    client, viewer, subject = env
    community = make_community('general')
    db.session.commit()
    make_post(community, subject, 'https://test.piefed.local/p/1',
              title='a post')
    db.session.commit()

    response, render = show(app, client)

    assert response.status_code == 200
    assert render.call_args.kwargs['user'].id == subject.id
    assert [p.title for p in render.call_args.kwargs['posts'].items] == ['a post']


@pytest.mark.parametrize('column', ['deleted', 'banned'])
def test_an_anonymous_visitor_cannot_see_a_removed_profile(app, env, column):
    """`if (user.deleted or user.banned) and current_user.is_anonymous` -- a
    moderator needs the page to see what was removed; a passer-by does not.
    By id, for the reason given below."""
    client, viewer, subject = env
    setattr(subject, column, True)
    db.session.commit()

    response = app.test_client().get(f'/user/{subject.id}')

    assert response.status_code == 404


@pytest.mark.parametrize('column, message', [
    ('deleted', 'has been deleted'),
    ('banned', 'has been banned'),
])
def test_a_logged_in_visitor_is_told_why_a_profile_is_odd(app, env, column,
                                                          message):
    """Two separate flashes; a row for one says nothing about the other.

    Reached by ID, not by name: `activitypub.user_profile` -- the `/u/<name>`
    route -- filters `deleted=False, banned=False` before it calls
    `show_profile`, so these two flashes are unreachable through it.
    `show_profile_by_id` uses `db.session.get` and does not filter, which is
    the path a notification link takes."""
    client, viewer, subject = env
    setattr(subject, column, True)
    db.session.commit()

    with patch('app.user.routes.flash') as flashed:
        with patch('app.user.routes.render_template', return_value='rendered'):
            client.get(f'/user/{subject.id}')

    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert message in messages


def test_the_profile_lists_the_communities_they_moderate(app, env):
    client, viewer, subject = env
    moderated = make_community('moderated')
    ordinary = make_community('ordinary')
    db.session.commit()
    membership = make_community_member(subject, moderated)
    membership.is_moderator = True
    make_community_member(subject, ordinary)
    db.session.commit()

    _response, render = show(app, client)

    assert [c.name for c in render.call_args.kwargs['moderates']] == ['moderated']


def test_the_profile_lists_their_public_feeds(app, env):
    client, viewer, subject = env
    public = Feed(user_id=subject.id, title='Public', name='public',
                  public=True,
                  ap_profile_id='https://test.piefed.local/f/public')
    private = Feed(user_id=subject.id, title='Private', name='private',
                   public=False,
                   ap_profile_id='https://test.piefed.local/f/private')
    db.session.add_all([public, private])
    db.session.commit()

    _response, render = show(app, client)

    assert render.call_args.kwargs['user_has_public_feeds'] is True
    assert [f.title for f in render.call_args.kwargs['user_public_feeds']] == ['Public']


def test_a_profile_with_no_public_feeds_says_so(app, env):
    client, viewer, subject = env

    _response, render = show(app, client)

    assert render.call_args.kwargs['user_has_public_feeds'] is False


def test_the_profile_lists_followers_and_following(app, env):
    """Two queries that differ only by `is_inward`, and the followers one also
    requires `is_accepted` -- a pending request is not a follower."""
    client, viewer, subject = env
    follower = make_user(instance(), 'follower', local=True)
    followed = make_user(instance(), 'followed', local=True)
    pending = make_user(instance(), 'pending', local=True)
    db.session.commit()
    db.session.add_all([
        UserFollower(local_user_id=subject.id, remote_user_id=follower.id,
                     is_inward=True, is_accepted=True),
        UserFollower(local_user_id=subject.id, remote_user_id=pending.id,
                     is_inward=True, is_accepted=None),
        UserFollower(local_user_id=subject.id, remote_user_id=followed.id,
                     is_inward=False),
    ])
    db.session.commit()

    _response, render = show(app, client)

    assert [u.user_name for u in render.call_args.kwargs['followers']] == ['follower']
    assert [u.user_name for u in render.call_args.kwargs['following']] == ['followed']


def test_a_banned_follower_is_not_listed(app, env):
    client, viewer, subject = env
    follower = make_user(instance(), 'follower', local=True)
    db.session.commit()
    db.session.add(UserFollower(local_user_id=subject.id,
                                remote_user_id=follower.id,
                                is_inward=True, is_accepted=True))
    follower.banned = True
    db.session.commit()

    _response, render = show(app, client)

    assert render.call_args.kwargs['followers'] == []


def test_the_profile_shows_posting_patterns_to_a_logged_in_visitor(app, env):
    """Two raw SQL summaries behind `if current_user.is_authenticated`, so an
    anonymous visitor gets empty lists rather than the queries."""
    client, viewer, subject = env
    community = make_community('general')
    db.session.commit()
    post = make_post(community, subject, 'https://test.piefed.local/p/1',
                     title='a post')
    make_post_reply(post, subject, body='a reply')
    db.session.commit()

    _response, render = show(app, client)

    context = render.call_args.kwargs
    assert context['posting_pattern_labels'] != [] or context['posting_pattern_values'] == []
    assert isinstance(context['posting_pattern_labels'], list)
    assert isinstance(context['comment_pattern_labels'], list)


def test_an_anonymous_visitor_gets_no_patterns_and_no_quota(app, env):
    client, viewer, subject = env

    _response, render = show(app, app.test_client())

    context = render.call_args.kwargs
    assert context['posting_pattern_labels'] == []
    assert context['comment_pattern_labels'] == []
    assert context['vote_quota_used'] == 0


def test_the_profile_pages_its_posts(app, env):
    client, viewer, subject = env
    community = make_community('general')
    db.session.commit()
    for number in range(30):
        make_post(community, subject,
                  f'https://test.piefed.local/p/{number}',
                  title=f'post {number}')
    db.session.commit()

    _response, render = show(app, client)

    assert render.call_args.kwargs['post_next_url'] is not None
    assert render.call_args.kwargs['post_prev_url'] is None


def test_the_second_page_of_posts_links_back(app, env):
    client, viewer, subject = env
    community = make_community('general')
    db.session.commit()
    for number in range(30):
        make_post(community, subject,
                  f'https://test.piefed.local/p/{number}',
                  title=f'post {number}')
    db.session.commit()

    _response, render = show(app, client, query='?post_page=2')

    assert render.call_args.kwargs['post_prev_url'] is not None


def test_a_profile_carries_its_canonical_url(app, env):
    client, viewer, subject = env
    subject.ap_public_url = 'https://test.piefed.local/u/subject'
    db.session.commit()

    _response, render = show(app, client)

    assert render.call_args.kwargs['canonical'] == 'https://test.piefed.local/u/subject'


def test_a_profile_without_a_public_url_has_no_canonical(app, env):
    client, viewer, subject = env
    subject.ap_public_url = None
    db.session.commit()

    _response, render = show(app, client)

    assert render.call_args.kwargs['canonical'] is None


def test_a_profiles_bio_becomes_its_description(app, env):
    client, viewer, subject = env
    subject.about = 'A ' + 'very ' * 60 + 'long bio'
    db.session.commit()

    _response, render = show(app, client)

    description = render.call_args.kwargs['description']
    assert description.startswith('A very')
    assert len(description) <= 160


def test_a_profile_without_a_bio_has_no_description(app, env):
    client, viewer, subject = env
    subject.about = None
    db.session.commit()

    _response, render = show(app, client)

    assert render.call_args.kwargs['description'] is None


def test_a_profile_is_reachable_by_id(app, env):
    """`show_profile_by_id` is the route the notification links use, and it
    hands off to the same function."""
    client, viewer, subject = env

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = client.get(f'/user/{subject.id}')

    assert response.status_code == 200
    assert render.call_args.kwargs['user'].id == subject.id


def test_an_unknown_id_is_a_404(app, env):
    client, viewer, subject = env

    response = client.get('/user/9999')

    assert response.status_code == 404


# --------------------------------------------------------------------------
# D1068 -- the notification list
# --------------------------------------------------------------------------


def test_the_notification_list_shows_your_notifications(app, env):
    client, viewer, subject = env
    a_notification(viewer, title='mine')
    a_notification(subject, title='theirs')

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = client.get('/notifications')

    assert response.status_code == 200
    assert [n.title for n in render.call_args.kwargs['notifications']] == ['mine']


def test_the_unread_count_is_recalculated(app, env):
    """The page corrects the counter as well as reading it, because the
    counter is maintained by hand everywhere else."""
    client, viewer, subject = env
    a_notification(viewer, read=False)
    a_notification(viewer, read=True)
    viewer.unread_notifications = 99
    db.session.commit()

    client.get('/notifications')

    db.session.refresh(viewer)
    assert viewer.unread_notifications == 1


def test_the_type_summary_counts_only_the_unread(app, env):
    """`notification_types[...] += 0` for a read one -- the type still has to
    appear in the list of filters, with a count of zero."""
    client, viewer, subject = env
    a_notification(viewer, read=True, notif_type=NOTIF_REPLY)
    a_notification(viewer, read=False, notif_type=NOTIF_COMMUNITY)

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/notifications')

    types = render.call_args.kwargs['notification_types']
    assert len(types) == 2
    assert sum(types.values()) == 1
    assert render.call_args.kwargs['unread'] == 1


def test_a_default_notification_is_not_summarised(app, env):
    """`if notification.notif_type != NOTIF_DEFAULT` -- the default type has
    no filter of its own, so it would summarise as an unnamed bucket."""
    client, viewer, subject = env
    a_notification(viewer, notif_type=NOTIF_DEFAULT)

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/notifications')

    assert render.call_args.kwargs['notification_types'] == {}
    assert render.call_args.kwargs['has_notifications'] is True


def test_an_empty_list_says_so(app, env):
    client, viewer, subject = env

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/notifications')

    assert render.call_args.kwargs['has_notifications'] is False


def test_the_unread_filter(app, env):
    client, viewer, subject = env
    a_notification(viewer, read=False, title='unread')
    a_notification(viewer, read=True, title='read')

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/notifications?type=Unread')

    assert [n.title for n in render.call_args.kwargs['notifications']] == ['unread']


def test_the_type_filter(app, env):
    client, viewer, subject = env
    a_notification(viewer, notif_type=NOTIF_REPLY, title='a reply')
    a_notification(viewer, notif_type=NOTIF_COMMUNITY, title='a community')

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/notifications?type=%7B{NOTIF_REPLY}%7D')

    assert [n.title for n in render.call_args.kwargs['notifications']] == ['a reply']


def test_a_junk_type_filter_shows_everything(app, env):
    """D1068. `?type=abc` was `ValueError: invalid literal for int() with base
    10: 'abc'` -- D1043's shape at its SECOND site, and fixing
    `notifications_all_read` one slice earlier did not touch this one. An
    unusable filter shows everything, which is what an absent filter already
    does, and the filter is cleared so the page does not claim to be
    filtered."""
    client, viewer, subject = env
    a_notification(viewer, title='still here')

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = client.get('/notifications?type=abc')

    assert response.status_code == 200
    assert [n.title for n in render.call_args.kwargs['notifications']] == ['still here']
    assert render.call_args.kwargs['current_filter'] == ''


# --------------------------------------------------------------------------
# The alert lists
# --------------------------------------------------------------------------


def subscribe(user, entity_id, notif_type):
    subscription = NotificationSubscription(user_id=user.id,
                                            entity_id=entity_id,
                                            type=notif_type, name='alert')
    db.session.add(subscription)
    db.session.commit()
    return subscription


def test_the_reply_alerts_list(app, env):
    client, viewer, subject = env
    community = make_community('general')
    db.session.commit()
    post = make_post(community, subject, 'https://test.piefed.local/p/1',
                     title='a post')
    mine = make_post_reply(post, viewer, body='my reply')
    theirs = make_post_reply(post, subject, body='their reply')
    subscribe(viewer, mine.id, NOTIF_REPLY)
    subscribe(viewer, theirs.id, NOTIF_REPLY)

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = client.get('/alerts/comments/all')

    assert response.status_code == 200
    bodies = {e.body for e in render.call_args.kwargs['entities'].items}
    assert bodies == {'my reply', 'their reply'}


@pytest.mark.parametrize('which, expected', [
    ('mine', 'my reply'),
    ('others', 'their reply'),
])
def test_the_reply_alerts_can_be_narrowed(app, env, which, expected):
    """Three arms over the same join, differing only by whose reply it is."""
    client, viewer, subject = env
    community = make_community('general')
    db.session.commit()
    post = make_post(community, subject, 'https://test.piefed.local/p/1',
                     title='a post')
    mine = make_post_reply(post, viewer, body='my reply')
    theirs = make_post_reply(post, subject, body='their reply')
    subscribe(viewer, mine.id, NOTIF_REPLY)
    subscribe(viewer, theirs.id, NOTIF_REPLY)

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/alerts/comments/{which}')

    bodies = [e.body for e in render.call_args.kwargs['entities'].items]
    assert bodies == [expected]


def test_a_deleted_reply_is_not_alerted_on(app, env):
    client, viewer, subject = env
    community = make_community('general')
    db.session.commit()
    post = make_post(community, subject, 'https://test.piefed.local/p/1',
                     title='a post')
    gone = make_post_reply(post, viewer, body='a deleted reply')
    subscribe(viewer, gone.id, NOTIF_REPLY)
    gone.deleted = True
    db.session.commit()

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/alerts/comments/all')

    assert render.call_args.kwargs['entities'].items == []


def test_the_community_alerts_list(app, env):
    client, viewer, subject = env
    community = make_community('general')
    db.session.commit()
    make_community_member(viewer, community)
    subscribe(viewer, community.id, NOTIF_COMMUNITY)

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = client.get('/alerts/communities/all')

    assert response.status_code == 200
    assert [c.name for c in render.call_args.kwargs['entities'].items] == ['general']


@pytest.mark.parametrize('which, moderator, expected', [
    ('mine', True, ['moderated']),
    ('others', False, ['moderated']),
])
def test_the_community_alerts_split_on_moderation(app, env, which, moderator,
                                                  expected):
    """`is_moderator=True` for 'mine' and False for 'others' -- the same join
    with the flag flipped, and a row for one arm cannot see the other."""
    client, viewer, subject = env
    community = make_community('moderated')
    db.session.commit()
    membership = make_community_member(viewer, community)
    membership.is_moderator = moderator
    db.session.commit()
    subscribe(viewer, community.id, NOTIF_COMMUNITY)

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/alerts/communities/{which}')

    assert [c.name for c in render.call_args.kwargs['entities'].items] == expected


def test_somebody_elses_alerts_are_not_listed(app, env):
    client, viewer, subject = env
    community = make_community('general')
    db.session.commit()
    make_community_member(subject, community)
    subscribe(subject, community.id, NOTIF_COMMUNITY)

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/alerts/communities/all')

    assert render.call_args.kwargs['entities'].items == []


def test_the_post_alerts_list(app, env):
    """The `else` arm -- the default type, which is what `/alerts` with no
    path lands on."""
    from app.constants import NOTIF_POST

    client, viewer, subject = env
    community = make_community('general')
    db.session.commit()
    mine = make_post(community, viewer, 'https://test.piefed.local/p/1',
                     title='my post')
    theirs = make_post(community, subject, 'https://test.piefed.local/p/2',
                       title='their post')
    db.session.commit()
    subscribe(viewer, mine.id, NOTIF_POST)
    subscribe(viewer, theirs.id, NOTIF_POST)

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = client.get('/alerts')

    assert response.status_code == 200
    titles = {e.title for e in render.call_args.kwargs['entities'].items}
    assert titles == {'my post', 'their post'}


@pytest.mark.parametrize('which, expected', [
    ('mine', 'my post'),
    ('others', 'their post'),
])
def test_the_post_alerts_can_be_narrowed(app, env, which, expected):
    from app.constants import NOTIF_POST

    client, viewer, subject = env
    community = make_community('general')
    db.session.commit()
    mine = make_post(community, viewer, 'https://test.piefed.local/p/1',
                     title='my post')
    theirs = make_post(community, subject, 'https://test.piefed.local/p/2',
                       title='their post')
    db.session.commit()
    subscribe(viewer, mine.id, NOTIF_POST)
    subscribe(viewer, theirs.id, NOTIF_POST)

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/alerts/posts/{which}')

    titles = [e.title for e in render.call_args.kwargs['entities'].items]
    assert titles == [expected]


def test_a_post_under_review_is_not_in_somebody_elses_alerts(app, env):
    """The `others` arm is the only one that filters `status >
    POST_STATUS_REVIEWING` -- somebody else's post that is still in the
    review queue has not been published to this reader."""
    from app.constants import NOTIF_POST, POST_STATUS_REVIEWING

    client, viewer, subject = env
    community = make_community('general')
    db.session.commit()
    pending = make_post(community, subject, 'https://test.piefed.local/p/1',
                        title='a pending post')
    pending.status = POST_STATUS_REVIEWING
    db.session.commit()
    subscribe(viewer, pending.id, NOTIF_POST)

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/alerts/posts/others')

    assert render.call_args.kwargs['entities'].items == []


@pytest.mark.parametrize('kind, notif_type, title', [
    ('topics', 'NOTIF_TOPIC', 'Topic Alerts'),
    ('feeds', 'NOTIF_FEED', 'Feed Alerts'),
    ('users', 'NOTIF_USER', 'User Alerts'),
])
def test_the_other_alert_types(app, env, kind, notif_type, title):
    """Three arms that ignore the filter entirely, each over its own table."""
    import app.constants as constants

    client, viewer, subject = env
    if kind == 'topics':
        from app.models import Topic

        entity = Topic(name='Science', machine_name='science')
        db.session.add(entity)
    elif kind == 'feeds':
        entity = Feed(user_id=viewer.id, title='News', name='news',
                      public=True,
                      ap_profile_id='https://test.piefed.local/f/news')
        db.session.add(entity)
    else:
        entity = subject
    db.session.commit()
    subscribe(viewer, entity.id, getattr(constants, notif_type))

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = client.get(f'/alerts/{kind}/all')

    assert response.status_code == 200
    assert render.call_args.kwargs['title'] == title
    assert len(render.call_args.kwargs['entities'].items) == 1


def test_a_deleted_account_is_not_in_the_user_alerts(app, env):
    from app.constants import NOTIF_USER

    client, viewer, subject = env
    subscribe(viewer, subject.id, NOTIF_USER)
    subject.deleted = True
    db.session.commit()

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/alerts/users/all')

    assert render.call_args.kwargs['entities'].items == []


def test_the_alerts_page_pages(app, env):
    from app.constants import NOTIF_POST

    client, viewer, subject = env
    community = make_community('general')
    db.session.commit()
    for number in range(120):
        post = make_post(community, viewer,
                         f'https://test.piefed.local/p/{number}',
                         title=f'post {number}')
        subscribe(viewer, post.id, NOTIF_POST)

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/alerts/posts/all')

    assert render.call_args.kwargs['next_url'] is not None
    assert render.call_args.kwargs['prev_url'] is None


def test_the_alerts_page_pages_in_smaller_blocks_on_low_bandwidth(app, env):
    client, viewer, subject = env
    client.set_cookie('low_bandwidth', '1', domain='test.piefed.local')

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/alerts/posts/all')

    assert render.call_args.kwargs['entities'].per_page == 50


def test_only_this_persons_public_feeds_are_listed(app, env):
    """`filter_by(public=True).filter_by(user_id=user.id)` -- two filters, and
    a row whose only public feed belongs to the profile's owner cannot see the
    second one go."""
    client, viewer, subject = env
    theirs = Feed(user_id=subject.id, title='Theirs', name='theirs',
                  public=True,
                  ap_profile_id='https://test.piefed.local/f/theirs')
    somebody_elses = Feed(user_id=viewer.id, title='Mine', name='mine',
                          public=True,
                          ap_profile_id='https://test.piefed.local/f/mine')
    db.session.add_all([theirs, somebody_elses])
    db.session.commit()

    _response, render = show(app, client)

    assert [f.title for f in render.call_args.kwargs['user_public_feeds']] == ['Theirs']


def test_a_banned_account_they_follow_is_not_listed(app, env):
    """The `following` query has its own `User.banned == False`, and the
    followers row cannot see it -- they are two queries."""
    client, viewer, subject = env
    followed = make_user(instance(), 'followed', local=True)
    db.session.commit()
    db.session.add(UserFollower(local_user_id=subject.id,
                                remote_user_id=followed.id,
                                is_inward=False))
    followed.banned = True
    db.session.commit()

    _response, render = show(app, client)

    assert render.call_args.kwargs['following'] == []


def test_somebody_elses_post_alerts_are_not_listed(app, env):
    """The subscription join is scoped by `user_id` on EVERY arm, and the
    community row cannot see the post arm's copy go."""
    from app.constants import NOTIF_POST

    client, viewer, subject = env
    community = make_community('general')
    db.session.commit()
    post = make_post(community, subject, 'https://test.piefed.local/p/1',
                     title='their post')
    db.session.commit()
    subscribe(subject, post.id, NOTIF_POST)

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/alerts/posts/all')

    assert render.call_args.kwargs['entities'].items == []
