"""Community moderation authority: who may ban, unban, promote and demote.

Sub-project 80, slice A. `app/community/routes.py` is the second-densest
authorization surface in the repository (D891) and this is the group that
decides what a moderator can do to other accounts.

Three defects were found by reading, before a line of test was written:

* `community_unban_user` accepted GET and had no form, and `login_required`
  validates CSRF only for POST -- so a moderator who loaded an `<img>` tag
  anywhere on the web unbanned that user (D955);
* five routes in this group never checked `current_user.banned`, while every
  neighbouring route did -- so an instance-banned account kept its power to ban
  and unban other people and to promote and demote owners (D956);
* `community_moderate_subscribers` ran its find-and-ban form BEFORE any
  authorization check, so any logged-in account could make the instance fetch
  an arbitrary remote actor (D957).

The durable artefact is `test_no_state_changing_route_answers_a_banned_user`,
in the shape D901 established for the admin blueprint.
"""
import re
from unittest.mock import patch

import pytest

from app import db
from app.models import (Community, CommunityBan, CommunityBlock,
                        CommunityMember, User)
from tests.factories import (make_community, make_community_member,
                             make_instance, make_user)

pytestmark = pytest.mark.usefixtures('site')


def instance(domain='test.piefed.local', software='piefed'):
    """Fact 394: `make_instance` always INSERTs and `Instance.domain` is
    unique, so a second call for the local instance is a UniqueViolation."""
    from app.models import Instance

    existing = Instance.query.filter_by(domain=domain).first()
    if existing is not None:
        return existing
    return make_instance(domain, software=software)


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def csrf(app, client):
    """Fact 355: `login_required` validates CSRF itself for POST, regardless of
    WTF_CSRF_ENABLED."""
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf

    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as session:
        session['csrf_token'] = raw
    return token


def url(app, endpoint, **values):
    from flask import url_for

    with app.test_request_context():
        return url_for(endpoint, **values)


@pytest.fixture
def community_world(app, db_session):
    """One community, four accounts: the instance owner, a moderator who is
    also an owner, a second moderator, and an ordinary member to act on."""
    local = instance()
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1  # fact 347
    moderator = make_user(local, 'themod', local=True)
    second = make_user(local, 'secondmod', local=True)
    member = make_user(local, 'member', local=True)
    community = make_community('general')
    db.session.commit()

    owner_membership = make_community_member(moderator, community, is_moderator=True)
    owner_membership.is_owner = True
    make_community_member(second, community, is_moderator=True)
    make_community_member(member, community)
    db.session.commit()

    return community, moderator, second, member


@pytest.fixture
def moderator_client(app, community_world):
    community, moderator, second, member = community_world
    client = app.test_client()
    login(client, moderator)
    return client, csrf(app, client)


def _ban(community, user, by, flag_membership=True):
    """`flag_membership=False` matters: `Community.moderators()` filters
    `is_banned == False` (app/models.py:722), so flipping the membership flag
    also removes the person from the moderator list -- and the routes' "you may
    not act on a moderator" guard reads that list. A row that wants somebody to
    stay a moderator has to ban them without it."""
    row = CommunityBan(community_id=community.id, user_id=user.id,
                       banned_by=by.id, reason='spam')
    db.session.add(row)
    if flag_membership:
        membership = CommunityMember.query.filter_by(community_id=community.id,
                                                     user_id=user.id).first()
        if membership:
            membership.is_banned = True
    db.session.commit()
    return row


# The tables this blueprint writes. A fingerprint over their CONTENTS, not
# their row counts: these routes update as much as they insert, and a ratchet
# that counted rows would miss `report.status`, `community_member.is_owner` and
# every wiki edit.
#
# `user` is excluded and named here because app/request_hooks.py:110 sets
# `current_user.last_seen` on every request, so it changes whatever the route
# does. Nothing this blueprint does to a `user` row is invisible elsewhere:
# the community-level effects all land in community_member or community_ban.
_WATCHED_TABLES = (
    'community', 'community_member', 'community_ban', 'community_block',
    'community_wiki_page', 'community_wiki_page_revision', 'community_flair',
    'community_flair_block', 'community_join_request', 'report', 'modlog',
    'post', 'post_reply', 'post_flair', 'notification', 'rss_feed',
)


def _state_fingerprint():
    """Every row of every table this blueprint can write, as comparable text."""
    from sqlalchemy import inspect as sa_inspect, text

    fingerprint = {}
    existing = set(sa_inspect(db.engine).get_table_names())
    for table in _WATCHED_TABLES:
        if table not in existing:
            continue
        rows = db.session.execute(
            text(f'SELECT * FROM "{table}" ORDER BY 1')).mappings().all()
        fingerprint[table] = [tuple(sorted((k, str(v)) for k, v in row.items()))
                              for row in rows]
    return fingerprint


def _is_banned(community, user):
    return CommunityBan.query.filter_by(community_id=community.id,
                                        user_id=user.id).first() is not None


def _is_owner(community, user):
    membership = CommunityMember.query.filter_by(community_id=community.id,
                                                 user_id=user.id).first()
    return bool(membership and membership.is_owner)


# --------------------------------------------------------------------------
# D955: the unban must not happen on a GET
# --------------------------------------------------------------------------


def test_an_unban_cannot_be_driven_by_a_bare_get(app, community_world,
                                                 moderator_client):
    """D955's pin, inverted.

    The route accepted GET, had no form, and unbanned on whichever method
    arrived -- and `login_required` validates CSRF only for POST, so a GET
    carried no token at all. A moderator who loaded

        <img src="https://instance/community/community/1/3/unban_user_community">

    on any page anywhere unbanned user 3. Measured before the fix:

        PROBE c1 GET with NO csrf token, status: 302
        PROBE c1 ban row still there? False
    """
    community, moderator, second, member = community_world
    client, token = moderator_client
    _ban(community, member, moderator)

    target = url(app, 'community.community_unban_user',
                 community_id=community.id, user_id=member.id)
    with patch('app.community.routes.task_selector'):
        response = client.get(target)

    assert response.status_code == 405, 'the unban still answers a GET'
    assert _is_banned(community, member), 'a bare GET removed the ban'


def test_a_post_without_a_csrf_token_is_refused(app, community_world,
                                                moderator_client):
    """The other half of D955: POST-only is only a fix because POST is where
    `login_required` validates the token. A POST with no token must fail."""
    community, moderator, second, member = community_world
    client, token = moderator_client
    _ban(community, member, moderator)

    target = url(app, 'community.community_unban_user',
                 community_id=community.id, user_id=member.id)
    with patch('app.community.routes.task_selector'):
        response = client.post(target, data={})

    # 400, not 500. `validate_csrf` raises wtforms' ValidationError, and there
    # is no CSRFProtect registered on this app to turn that into a response, so
    # until D958 a missing or stale token answered 500 with a traceback on
    # every route using this decorator.
    assert response.status_code == 400
    assert _is_banned(community, member)


def test_a_moderator_can_unban(app, community_world, moderator_client):
    """The feature itself, through the method the template now uses. Both the
    CommunityBan row and the membership flag are asserted: the row is what
    moderators control, the flag is what keeps the community out of the
    person's feed, and leaving either behind is a different kind of
    half-unbanned."""
    community, moderator, second, member = community_world
    client, token = moderator_client
    _ban(community, member, moderator)

    target = url(app, 'community.community_unban_user',
                 community_id=community.id, user_id=member.id)
    with patch('app.community.routes.task_selector') as task:
        response = client.post(target, data={'csrf_token': token})

    assert response.status_code == 302
    assert not _is_banned(community, member)
    membership = CommunityMember.query.filter_by(community_id=community.id,
                                                 user_id=member.id).first()
    assert membership.is_banned is False
    assert task.call_args.args == ('unban_from_community',)


def test_an_unban_notifies_a_local_user(app, community_world, moderator_client):
    """`if user.is_local():` -- the person is told, and their unread count goes
    up. A remote user is told by the federated activity instead."""
    from app.models import Notification

    community, moderator, second, member = community_world
    client, token = moderator_client
    _ban(community, member, moderator)
    before = member.unread_notifications or 0

    target = url(app, 'community.community_unban_user',
                 community_id=community.id, user_id=member.id)
    with patch('app.community.routes.task_selector'):
        client.post(target, data={'csrf_token': token})

    db.session.expire_all()
    notification = Notification.query.filter_by(user_id=member.id).one()
    assert 'un-banned' in notification.title
    assert db.session.get(User, member.id).unread_notifications == before + 1


def test_an_unban_of_a_remote_user_creates_no_local_notification(
        app, community_world, moderator_client):
    """The false arm. A remote account has no notifications page here."""
    from app.models import Notification

    community, moderator, second, member = community_world
    client, token = moderator_client
    remote = make_user(instance('remote.example', 'lemmy'), 'theirs', local=False)
    db.session.commit()
    make_community_member(remote, community)
    _ban(community, remote, moderator)

    target = url(app, 'community.community_unban_user',
                 community_id=community.id, user_id=remote.id)
    with patch('app.community.routes.task_selector'):
        client.post(target, data={'csrf_token': token})

    assert Notification.query.filter_by(user_id=remote.id).count() == 0


def test_unbanning_someone_who_is_not_banned_is_harmless(
        app, community_world, moderator_client):
    """`if existing_ban:` -- the link is rendered from a list that may be
    stale, and a second click must not raise."""
    community, moderator, second, member = community_world
    client, token = moderator_client

    target = url(app, 'community.community_unban_user',
                 community_id=community.id, user_id=member.id)
    with patch('app.community.routes.task_selector'):
        response = client.post(target, data={'csrf_token': token})

    assert response.status_code == 302


def test_a_moderator_cannot_be_unbanned_through_this_route(
        app, community_world, moderator_client):
    """`and not community.is_moderator(user)` -- the guard that stops a
    moderator acting on another moderator."""
    community, moderator, second, member = community_world
    client, token = moderator_client
    _ban(community, second, moderator, flag_membership=False)

    target = url(app, 'community.community_unban_user',
                 community_id=community.id, user_id=second.id)
    response = client.post(target, data={'csrf_token': token})

    assert response.status_code == 403
    assert _is_banned(community, second)


def test_an_ordinary_member_cannot_unban(app, community_world):
    """The route's own authorization, separate from the banned check."""
    community, moderator, second, member = community_world
    client = app.test_client()
    login(client, member)
    token = csrf(app, client)
    _ban(community, member, moderator)

    target = url(app, 'community.community_unban_user',
                 community_id=community.id, user_id=member.id)
    response = client.post(target, data={'csrf_token': token})

    assert response.status_code == 403
    assert _is_banned(community, member)


# --------------------------------------------------------------------------
# D956: a banned account keeps no authority
# --------------------------------------------------------------------------


@pytest.mark.parametrize('endpoint, extra, target', [
    ('community.community_ban_user',
     {'reason': 'because', 'ban_until': '', 'submit': 'Ban'}, 'member'),
    ('community.community_unban_user', {}, 'banned_member'),
    ('community.community_make_owner', {}, 'second'),
    ('community.community_remove_owner', {}, 'self_standing_down'),
])
def test_a_banned_moderator_cannot_use_its_authority(app, community_world,
                                                     endpoint, extra, target):
    """D956's pin, inverted.

    `current_user.banned` is checked by `add_post`, `community_edit`,
    `community_delete`, `community_add_moderator`, `community_find_moderator`,
    `community_moderate`, the RSS routes and `community_moderate_comments` --
    and was checked by none of these. So an instance-banned account that held
    community authority could not post or edit the community, but could still
    ban and unban people and promote and demote owners. Measured:

        PROBE c2 banned moderator ban POST status: 302
        PROBE c2 victim now banned from community? True
        PROBE c3 banned owner make_owner status: 302
        PROBE c3 other is now an owner? True

    `banned` is the instance's primary sanction; one that leaves the person's
    power over other accounts intact is not a sanction.
    """
    community, moderator, second, member = community_world

    # Fact 350: each case is set up so the action WOULD change something. Three
    # of these four were vacuous when first written -- unbanning someone who is
    # not banned, promoting someone who is not a moderator and demoting someone
    # who is not an owner all do nothing whether or not the guard is there, and
    # the mutation of the guard survived them.
    if target == 'member':
        subject, before = member, lambda: _is_banned(community, member)
    elif target == 'banned_member':
        _ban(community, member, moderator)
        subject, before = member, lambda: not _is_banned(community, member)
    elif target == 'second':
        subject, before = second, lambda: _is_owner(community, second)
    else:
        # The banned owner stands down -- the only remove_owner clause an owner
        # can satisfy against themselves. `second` is made an owner too, or
        # num_owners() == 1 refuses it for an unrelated reason and the row goes
        # vacuous a second way.
        membership = CommunityMember.query.filter_by(community_id=community.id,
                                                     user_id=second.id).first()
        membership.is_owner = True
        db.session.commit()
        subject, before = moderator, lambda: not _is_owner(community, moderator)

    assert not before(), 'the row is set up so the action would be a no-op'

    moderator.banned = True
    db.session.commit()
    client = app.test_client()
    login(client, moderator)
    token = csrf(app, client)

    with patch('app.community.routes.task_selector'):
        client.post(url(app, endpoint, community_id=community.id,
                        user_id=subject.id),
                    data={'csrf_token': token, **extra})

    assert not before(), f'a banned moderator completed {endpoint}'


def test_a_banned_moderator_cannot_reach_the_subscriber_list(app,
                                                             community_world):
    community, moderator, second, member = community_world
    moderator.banned = True
    db.session.commit()
    client = app.test_client()
    login(client, moderator)

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        client.get(url(app, 'community.community_moderate_subscribers',
                       actor=community.name))

    assert render.call_args_list == []


def test_no_state_changing_route_answers_a_banned_user(app, community_world):
    """The durable artefact, in the shape D901 established for `app/admin`.

    Every rule on the community blueprint that changes state is requested as an
    instance-banned moderator, and the state this blueprint can write is
    fingerprinted before and after: nothing may change.

    **WHAT THIS ROW DOES AND DOES NOT CATCH.** It drives each rule with a bare
    POST and a plausible id, so it catches a route whose work that is enough to
    trigger -- `community.community_report` was found exactly this way, and its
    check removed again still fails this row. It does NOT catch a route that
    needs a valid form body, a real slug or a resolvable actor to do anything,
    because the request stops before the work either way. Measured: removing
    the banned check from `community_wiki_add`, `community_flair_delete` and
    `community_moderate_report_resolve` leaves this row passing.

    So this is a FLOOR, not a proof. The per-route rows in
    `tests/test_community_wiki_flair_reports.py` are what actually pin those
    eight; this one is what notices a route nobody thought to write a row for.
    Saying so matters: D973 is the finding that its earlier version -- which
    only flagged a 200 -- passed while eight routes were unguarded, and a
    ratchet that cannot fail for its own defect is worse than none, because it
    is also a claim.

    Read-only and self-service rules are excluded BY NAME rather than guessed
    from the method, so adding a route means deciding which side of the line it
    is on.
    """
    community, moderator, second, member = community_world
    moderator.banned = True
    db.session.commit()

    client = app.test_client()
    login(client, moderator)
    token = csrf(app, client)

    # Rules that only read. Each is a deliberate decision, not an oversight:
    # a banned user may still look at a community, its wiki and its modlog,
    # because they can see all of that while logged out too.
    read_only = {
        'community.show_community', 'community.show_community_rss',
        'community.show_community_ical', 'community.community_mod_list',
        'community.community_modlog', 'community.community_wiki_list',
        'community.community_wiki_view', 'community.community_wiki_revisions',
        'community.community_wiki_view_revision', 'community.community_flair',
        'community.community_moderate', 'community.community_moderate_comments',
        'community.community_rss_feeds', 'community.lookup',
        'community.check_url_already_posted', 'community.community_changed',
        'community.community_name_search', 'community.retrieve_metadata_of_url',
        'community.communities', 'community.list_local_communities',
        'community.list_subscribed_communities', 'community.list_not_subscribed_communities',
        'community.community_invite_accept', 'community.get_sidebar',
    }

    # Routes that change state but only the CALLER'S OWN relationship to a
    # community. A banned account may still leave a community or hide it from
    # its own feed -- refusing that would trap somebody in a place they are
    # already barred from taking part in. They are listed rather than inferred,
    # for the same reason as the read-only set.
    self_service = {
        'community.unsubscribe', 'community.community_block',
        'community.community_unblock', 'community.community_leave_all',
        'community.community_notification', 'community.community_my_flair',
        'community.community_membership_manage',
    }

    answered = []
    before = _state_fingerprint()
    for rule in app.url_map.iter_rules():
        if not rule.endpoint.startswith('community.'):
            continue
        if rule.endpoint in read_only or rule.endpoint in self_service:
            continue
        methods = rule.methods - {'HEAD', 'OPTIONS'}
        path = rule.rule
        for argument in rule.arguments:
            replacement = {'community_id': str(community.id),
                           'user_id': str(member.id),
                           'actor': community.name}.get(argument, '1')
            path = path.replace(f'<int:{argument}>', replacement)
            path = path.replace(f'<{argument}>', replacement)
        if '<' in path:
            continue

        method = 'post' if 'POST' in methods else 'get'
        try:
            with patch('app.community.routes.task_selector'):
                with patch('app.community.routes.render_template',
                           return_value='rendered'):
                    response = getattr(client, method)(
                        path, data={'csrf_token': token})
        except Exception:
            # A route that raises has not silently done the work, which is what
            # this row is about; it is a different finding if it happens.
            continue

        # The STATUS IS NOT THE TEST. D973: this row used to flag a rule only
        # when it answered 200, and every route on this blueprint redirects on
        # success -- so it passed while eight state-changing routes had no
        # banned check at all. What matters is whether the work happened.
        after = _state_fingerprint()
        if after != before:
            changed = sorted(k for k in set(before) | set(after)
                             if before.get(k) != after.get(k))
            answered.append(f'{method.upper()} {path} ({rule.endpoint}) '
                            f'changed {changed}')
        before = after

    assert answered == [], (
        'these state-changing community routes did work for an '
        'instance-banned moderator:\n  ' + '\n  '.join(answered))


# --------------------------------------------------------------------------
# D957: the subscriber page must check before it fetches
# --------------------------------------------------------------------------


def test_a_non_moderator_cannot_make_the_server_fetch_an_actor(app,
                                                               community_world):
    """D957's pin, inverted.

    The find-and-ban form was handled BEFORE any authorization check, and
    `find_actor_or_create` reaches `create_actor_from_remote` -- an outbound
    fetch of a handle the submitter chose. Measured, as a user with no
    relationship to the community at all:

        PROBE s1 is nobody a moderator? False
        PROBE s1 find_actor_or_create called by a NON-moderator?
            [call('victim@attacker.example')]

    The ban itself was safe, because the redirect lands on
    `community_ban_user`, which checks. The fetch was not.
    """
    community, moderator, second, member = community_world
    nobody = make_user(instance('elsewhere.example'), 'nobody', local=True)
    db.session.commit()
    client = app.test_client()
    login(client, nobody)
    token = csrf(app, client)

    with patch('app.community.routes.find_actor_or_create') as find:
        with patch('app.community.routes.render_template', return_value='rendered'):
            response = client.post(
                url(app, 'community.community_moderate_subscribers',
                    actor=community.name),
                data={'user_name': 'victim@attacker.example',
                      'submit': 'Find and ban', 'csrf_token': token})

    assert response.status_code == 401
    assert find.call_args_list == [], 'a non-moderator drove an outbound fetch'


def test_a_moderator_can_still_find_and_ban(app, community_world,
                                            moderator_client):
    """The feature the check must not break: a moderator submits a handle, the
    actor is resolved, and the ban page opens for them."""
    community, moderator, second, member = community_world
    client, token = moderator_client

    with patch('app.community.routes.find_actor_or_create',
               return_value=member) as find:
        response = client.post(
            url(app, 'community.community_moderate_subscribers',
                actor=community.name),
            data={'user_name': 'member', 'submit': 'Find and ban',
                  'csrf_token': token})

    assert find.call_args.args == ('member',)
    assert response.status_code == 302
    assert f'/{member.id}/ban_user_community' in response.headers['Location']


def test_a_handle_that_resolves_to_nothing_says_so(app, community_world,
                                                   moderator_client):
    """The `else` of `isinstance(user_to_ban, User)` -- a handle may resolve to
    a community or to nothing at all, and neither can be banned."""
    community, moderator, second, member = community_world
    client, token = moderator_client

    with patch('app.community.routes.find_actor_or_create', return_value=None):
        with patch('app.community.routes.flash') as flashed:
            response = client.post(
                url(app, 'community.community_moderate_subscribers',
                    actor=community.name),
                data={'user_name': 'nobody@nowhere.example',
                      'submit': 'Find and ban', 'csrf_token': token})

    assert 'unable to be found' in flashed.call_args.args[0]
    assert response.status_code == 302


def test_the_subscriber_list_shows_members_and_hides_the_banned(
        app, community_world, moderator_client):
    """The list is what a moderator acts from, so who is on it matters: banned
    members are shown separately, and deleted or instance-banned accounts are
    not shown at all."""
    community, moderator, second, member = community_world
    client, token = moderator_client
    gone = make_user(instance(), 'gone', local=True)
    gone.deleted = True
    db.session.commit()
    make_community_member(gone, community)
    _ban(community, member, moderator)

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        client.get(url(app, 'community.community_moderate_subscribers',
                       actor=community.name))

    kwargs = render.call_args.kwargs
    listed = {row[0].user_name for row in kwargs['subscribers']}
    assert 'member' not in listed, 'a banned member is still in the subscriber list'
    assert 'gone' not in listed, 'a deleted account is in the subscriber list'
    assert 'secondmod' in listed


def test_an_ordinary_member_cannot_read_the_subscriber_list(app,
                                                            community_world):
    community, moderator, second, member = community_world
    client = app.test_client()
    login(client, member)

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        response = client.get(url(app, 'community.community_moderate_subscribers',
                                  actor=community.name))

    assert response.status_code == 401
    assert render.call_args_list == []


# --------------------------------------------------------------------------
# Ownership
# --------------------------------------------------------------------------


def test_an_owner_can_promote_a_moderator(app, community_world, moderator_client):
    community, moderator, second, member = community_world
    client, token = moderator_client

    response = client.post(url(app, 'community.community_make_owner',
                               community_id=community.id, user_id=second.id),
                           data={'csrf_token': token})

    assert response.status_code == 302
    assert _is_owner(community, second)


def test_a_non_moderator_cannot_be_promoted_to_owner(app, community_world,
                                                     moderator_client):
    """`and community.is_moderator(user)` -- ownership is an escalation of
    moderator, not a way to grant authority to someone who has none."""
    community, moderator, second, member = community_world
    client, token = moderator_client

    response = client.post(url(app, 'community.community_make_owner',
                               community_id=community.id, user_id=member.id),
                           data={'csrf_token': token})

    assert response.status_code == 401
    assert not _is_owner(community, member)


def test_an_ordinary_moderator_cannot_promote(app, community_world):
    """`community.is_owner() or current_user.is_admin_or_staff()` -- a plain
    moderator may not make owners."""
    community, moderator, second, member = community_world
    client = app.test_client()
    login(client, second)
    token = csrf(app, client)

    response = client.post(url(app, 'community.community_make_owner',
                               community_id=community.id, user_id=second.id),
                           data={'csrf_token': token})

    assert response.status_code == 401
    assert not _is_owner(community, second)


def _staff(name='staffer'):
    """`is_admin_or_staff()` reads the ROLE NAME -- `is_admin()` looks for a
    role called 'Admin' and `is_staff()` for one called 'Staff'
    (app/models.py:1259-1275). A permission granted through a bespoke role, as
    `grant_permission` makes, does not satisfy it.
    """
    from app.models import Role, user_role

    user = make_user(instance(), name, local=True)
    db.session.commit()
    role = Role.query.filter_by(name='Staff').first()
    if role is None:
        role = Role(name='Staff', weight=2)
        db.session.add(role)
        db.session.commit()
    db.session.execute(user_role.insert().values(user_id=user.id, role_id=role.id))
    db.session.commit()
    assert user.is_admin_or_staff()
    return user


def test_staff_can_promote_without_being_a_moderator(app, community_world):
    """The `or current_user.is_admin_or_staff()` arm -- instance staff can
    repair a community whose owners have gone."""
    community, moderator, second, member = community_world
    staffer = _staff()
    client = app.test_client()
    login(client, staffer)
    token = csrf(app, client)

    response = client.post(url(app, 'community.community_make_owner',
                               community_id=community.id, user_id=second.id),
                           data={'csrf_token': token})

    assert response.status_code == 302
    assert _is_owner(community, second)


def test_an_owner_can_stand_down(app, community_world, moderator_client):
    """`community.is_owner() and user.id == current_user.id` -- the only way an
    owner may remove an owner is themselves."""
    community, moderator, second, member = community_world
    client, token = moderator_client
    membership = CommunityMember.query.filter_by(community_id=community.id,
                                                 user_id=second.id).first()
    membership.is_owner = True
    db.session.commit()

    response = client.post(url(app, 'community.community_remove_owner',
                               community_id=community.id, user_id=moderator.id),
                           data={'csrf_token': token})

    assert response.status_code == 302
    assert not _is_owner(community, moderator)


def test_the_last_owner_cannot_stand_down(app, community_world,
                                          moderator_client):
    """`if community.num_owners() == 1` -- a community with no owner cannot be
    administered by anyone but instance staff."""
    community, moderator, second, member = community_world
    client, token = moderator_client

    with patch('app.community.routes.flash') as flashed:
        response = client.post(url(app, 'community.community_remove_owner',
                                   community_id=community.id,
                                   user_id=moderator.id),
                               data={'csrf_token': token})

    assert response.status_code == 302
    assert _is_owner(community, moderator), 'the last owner removed themselves'
    assert 'one or more owners' in flashed.call_args.args[0]


def test_an_owner_cannot_remove_another_owner(app, community_world,
                                              moderator_client):
    """`community.is_owner() and ... and not community.is_owner(user)` -- one
    owner may not depose another; only instance staff can."""
    community, moderator, second, member = community_world
    client, token = moderator_client
    membership = CommunityMember.query.filter_by(community_id=community.id,
                                                 user_id=second.id).first()
    membership.is_owner = True
    db.session.commit()

    response = client.post(url(app, 'community.community_remove_owner',
                               community_id=community.id, user_id=second.id),
                           data={'csrf_token': token})

    assert response.status_code == 401
    assert _is_owner(community, second)


def test_staff_can_remove_an_owner(app, community_world):
    """`current_user.is_admin_or_staff() and community.is_owner(user)`."""
    community, moderator, second, member = community_world
    membership = CommunityMember.query.filter_by(community_id=community.id,
                                                 user_id=second.id).first()
    membership.is_owner = True
    db.session.commit()
    staffer = _staff()
    client = app.test_client()
    login(client, staffer)
    token = csrf(app, client)

    response = client.post(url(app, 'community.community_remove_owner',
                               community_id=community.id, user_id=second.id),
                           data={'csrf_token': token})

    assert response.status_code == 302
    assert not _is_owner(community, second)


@pytest.mark.parametrize('endpoint', ['community.community_make_owner',
                                      'community.community_remove_owner'])
def test_the_ownership_routes_are_404_for_things_that_do_not_exist(
        app, community_world, moderator_client, endpoint):
    community, moderator, second, member = community_world
    client, token = moderator_client

    missing_community = client.post(
        url(app, endpoint, community_id=999999, user_id=member.id),
        data={'csrf_token': token})
    missing_user = client.post(
        url(app, endpoint, community_id=community.id, user_id=999999),
        data={'csrf_token': token})

    assert missing_community.status_code == 404
    assert missing_user.status_code == 404


# --------------------------------------------------------------------------
# Banning
# --------------------------------------------------------------------------


def test_a_moderator_can_ban_a_member(app, community_world, moderator_client):
    """Both rows again: CommunityBan is what moderators control, and
    CommunityMember.is_banned is what keeps the community out of the person's
    home feed. The route's own comment says so."""
    community, moderator, second, member = community_world
    client, token = moderator_client

    with patch('app.community.routes.task_selector') as task:
        with patch('app.community.routes.render_template', return_value='rendered'):
            response = client.post(
                url(app, 'community.community_ban_user',
                    community_id=community.id, user_id=member.id),
                data={'reason': 'spamming', 'ban_until': '', 'submit': 'Ban',
                      'csrf_token': token})

    assert response.status_code == 302
    ban = CommunityBan.query.filter_by(community_id=community.id,
                                       user_id=member.id).one()
    assert ban.reason == 'spamming'
    assert ban.banned_by == moderator.id
    assert ban.ban_until is None
    membership = CommunityMember.query.filter_by(community_id=community.id,
                                                 user_id=member.id).first()
    assert membership.is_banned is True
    assert task.call_args.args == ('ban_from_community',)


def test_a_ban_until_a_future_date_is_stored(app, community_world,
                                             moderator_client):
    """`if form.ban_until.data is not None and form.ban_until.data > utcnow().date()`."""
    from datetime import timedelta

    from app.models import utcnow

    community, moderator, second, member = community_world
    client, token = moderator_client
    expiry = (utcnow() + timedelta(days=30)).date()

    with patch('app.community.routes.task_selector'):
        with patch('app.community.routes.render_template', return_value='rendered'):
            client.post(url(app, 'community.community_ban_user',
                            community_id=community.id, user_id=member.id),
                        data={'reason': 'spamming', 'ban_until': expiry.isoformat(),
                              'submit': 'Ban', 'csrf_token': token})

    stored = CommunityBan.query.filter_by(user_id=member.id).one().ban_until
    assert (stored.date() if hasattr(stored, 'date') else stored) == expiry


def test_a_ban_until_a_past_date_is_ignored(app, community_world,
                                            moderator_client):
    """The false arm. A date already gone would make the ban expire the moment
    it was applied, which reads as the ban not working."""
    from datetime import timedelta

    from app.models import utcnow

    community, moderator, second, member = community_world
    client, token = moderator_client
    expiry = (utcnow() - timedelta(days=1)).date()

    with patch('app.community.routes.task_selector'):
        with patch('app.community.routes.render_template', return_value='rendered'):
            client.post(url(app, 'community.community_ban_user',
                            community_id=community.id, user_id=member.id),
                        data={'reason': 'spamming', 'ban_until': expiry.isoformat(),
                              'submit': 'Ban', 'csrf_token': token})

    assert CommunityBan.query.filter_by(user_id=member.id).one().ban_until is None


def test_banning_someone_already_banned_does_not_add_a_second_row(
        app, community_world, moderator_client):
    """`if not existing:` -- two moderators acting on the same report would
    otherwise leave two bans, and unbanning removes one."""
    community, moderator, second, member = community_world
    client, token = moderator_client
    _ban(community, member, moderator)

    with patch('app.community.routes.task_selector'):
        with patch('app.community.routes.render_template', return_value='rendered'):
            client.post(url(app, 'community.community_ban_user',
                            community_id=community.id, user_id=member.id),
                        data={'reason': 'again', 'ban_until': '',
                              'submit': 'Ban', 'csrf_token': token})

    assert CommunityBan.query.filter_by(community_id=community.id,
                                        user_id=member.id).count() == 1


def test_a_moderator_cannot_be_banned_through_this_route(app, community_world,
                                                         moderator_client):
    community, moderator, second, member = community_world
    client, token = moderator_client

    response = client.post(url(app, 'community.community_ban_user',
                               community_id=community.id, user_id=second.id),
                           data={'reason': 'x', 'ban_until': '',
                                 'submit': 'Ban', 'csrf_token': token})

    assert response.status_code == 403
    assert not _is_banned(community, second)


def test_the_ban_form_renders_for_a_moderator(app, community_world,
                                              moderator_client):
    """The GET arm -- the page the moderator fills in."""
    community, moderator, second, member = community_world
    client, token = moderator_client

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        response = client.get(url(app, 'community.community_ban_user',
                                  community_id=community.id, user_id=member.id))

    assert response.status_code == 200
    assert render.call_args.kwargs['form'] is not None


def test_an_ordinary_member_cannot_ban(app, community_world):
    """The CALLER half of `(community.is_moderator() or
    current_user.is_admin_or_staff()) and not community.is_moderator(user)`.

    The target is a second ordinary member, not a moderator: aiming at a
    moderator satisfies neither clause, so the row would pass with the caller
    check deleted. The mutation pass found exactly that -- dropping the whole
    left-hand side survived until this row named a target the guard would
    otherwise allow.
    """
    community, moderator, second, member = community_world
    bystander = make_user(instance(), 'bystander', local=True)
    db.session.commit()
    make_community_member(bystander, community)

    client = app.test_client()
    login(client, member)
    token = csrf(app, client)

    response = client.post(url(app, 'community.community_ban_user',
                               community_id=community.id,
                               user_id=bystander.id),
                           data={'reason': 'x', 'ban_until': '',
                                 'submit': 'Ban', 'csrf_token': token})

    assert response.status_code == 403
    assert not _is_banned(community, bystander)


def test_an_ordinary_member_cannot_ban_a_moderator_either(app, community_world):
    """The TARGET half, kept separate so each clause has a row that only it
    can satisfy."""
    community, moderator, second, member = community_world
    client = app.test_client()
    login(client, member)
    token = csrf(app, client)

    response = client.post(url(app, 'community.community_ban_user',
                               community_id=community.id, user_id=second.id),
                           data={'reason': 'x', 'ban_until': '',
                                 'submit': 'Ban', 'csrf_token': token})

    assert response.status_code == 403
    assert not _is_banned(community, second)


def test_staff_cannot_remove_ownership_from_someone_who_is_not_an_owner(
        app, community_world):
    """`current_user.is_admin_or_staff() and community.is_owner(user)` -- the
    second half.

    A staff member aiming at a plain moderator satisfies no clause of the
    three, so the route refuses. Without the `and community.is_owner(user)` it
    would proceed and clear a flag that is already false -- invisible in the
    database, which is why this row asserts the STATUS. The mutation pass found
    it: dropping that half survived every row that checked rows rather than
    responses.
    """
    community, moderator, second, member = community_world
    # A third owner, so num_owners() != 1 and the refusal cannot be mistaken
    # for the last-owner guard.
    third = make_user(instance(), 'thirdowner', local=True)
    db.session.commit()
    membership = make_community_member(third, community, is_moderator=True)
    membership.is_owner = True
    db.session.commit()

    staffer = _staff()
    client = app.test_client()
    login(client, staffer)
    token = csrf(app, client)

    response = client.post(url(app, 'community.community_remove_owner',
                               community_id=community.id, user_id=second.id),
                           data={'csrf_token': token})

    assert response.status_code == 401
    assert not _is_owner(community, second)


# --------------------------------------------------------------------------
# community_block -- a member's own block, not moderation
# --------------------------------------------------------------------------


def test_blocking_a_community_hides_it(app, community_world):
    """This is the ONE route in the group that is not moderation authority: it
    is a member acting on their own feed, which is why a banned account may
    still do it."""
    community, moderator, second, member = community_world
    client = app.test_client()
    login(client, member)
    token = csrf(app, client)

    response = client.post(url(app, 'community.community_block',
                               community_id=community.id),
                           data={'csrf_token': token})

    assert response.status_code == 302
    assert CommunityBlock.query.filter_by(user_id=member.id,
                                          community_id=community.id).count() == 1


def test_blocking_twice_does_not_add_a_second_row(app, community_world):
    """`if not existing:`."""
    community, moderator, second, member = community_world
    client = app.test_client()
    login(client, member)
    token = csrf(app, client)
    db.session.add(CommunityBlock(user_id=member.id, community_id=community.id))
    db.session.commit()

    client.post(url(app, 'community.community_block', community_id=community.id),
                data={'csrf_token': token})

    assert CommunityBlock.query.filter_by(user_id=member.id,
                                          community_id=community.id).count() == 1


def test_blocking_from_a_post_page_redirects_the_htmx_caller(app,
                                                             community_world):
    """The HX-Request arm. Blocking from a post page whose post belongs to the
    blocked community must move the reader somewhere that still exists."""
    community, moderator, second, member = community_world
    client = app.test_client()
    login(client, member)
    token = csrf(app, client)

    response = client.post(
        url(app, 'community.community_block', community_id=community.id),
        data={'csrf_token': token},
        headers={'HX-Request': 'true', 'HX-Current-Url': '/'})

    assert response.headers['HX-Redirect'] == url(app, 'main.index')


def test_blocking_a_community_that_does_not_exist_is_a_404(app,
                                                           community_world):
    community, moderator, second, member = community_world
    client = app.test_client()
    login(client, member)
    token = csrf(app, client)

    response = client.post(url(app, 'community.community_block',
                               community_id=999999),
                           data={'csrf_token': token})

    assert response.status_code == 404


# --------------------------------------------------------------------------
# D959: deleting a banned user's contributions
# --------------------------------------------------------------------------


def _post_and_reply(author, community, marker):
    from app.models import Post, PostReply

    post = Post(user_id=author.id, community_id=community.id, title=f'post {marker}',
                ap_id=f'https://test.piefed.local/post/{marker}')
    db.session.add(post)
    db.session.commit()
    reply = PostReply(user_id=author.id, post_id=post.id, community_id=community.id,
                      body=f'reply {marker}',
                      ap_id=f'https://test.piefed.local/comment/{marker}')
    db.session.add(reply)
    db.session.commit()
    return post, reply


def test_deleting_a_banned_users_replies_stays_in_this_community(
        app, community_world, moderator_client):
    """D959's pin, inverted -- the sharpest defect in this slice.

    The query filtered `PostReply.user_id == user.id, Post.community_id ==
    community.id`, and `Post` is not joined. SQLAlchemy puts it in the FROM
    clause on its own, so the condition holds whenever the community has ANY
    post, and every reply the user had ever written anywhere matched. **A
    moderator of one community, ticking "delete replies" while banning
    somebody, destroyed that person's comments across the whole instance.**
    Measured:

        PROBE x1 replies the ban would delete for community "here":
            ['reply in elsewhere', 'reply in here']
        PROBE x1 replies actually in "here": ['reply in here']

    The row bans from `general` and asserts the reply in `elsewhere` survives,
    which is the only thing that separates the two queries.
    """
    community, moderator, second, member = community_world
    elsewhere = make_community('elsewhere')
    db.session.commit()
    _post, here_reply = _post_and_reply(member, community, 'here')
    _post, other_reply = _post_and_reply(member, elsewhere, 'elsewhere')

    with patch('app.community.routes.task_selector'):
        with patch('app.community.routes.delete_post_reply_from_community') as delete:
            with patch('app.community.routes.render_template', return_value='rendered'):
                client, token = moderator_client
                client.post(url(app, 'community.community_ban_user',
                                community_id=community.id, user_id=member.id),
                            data={'reason': 'spam', 'ban_until': '',
                                  'delete_post_replies': 'y', 'submit': 'Ban',
                                  'csrf_token': token})

    deleted = [call.args[0] for call in delete.call_args_list]
    assert deleted == [here_reply.id], (
        'a ban in one community deleted replies from another')


def test_deleting_a_banned_users_posts_stays_in_this_community(
        app, community_world, moderator_client):
    """The posts arm, which was already correct -- `Post.community_id` on a
    `Post` query. It is pinned so the fix to its neighbour cannot be copied
    the wrong way."""
    community, moderator, second, member = community_world
    elsewhere = make_community('elsewhere')
    db.session.commit()
    here_post, _reply = _post_and_reply(member, community, 'here')
    other_post, _reply = _post_and_reply(member, elsewhere, 'elsewhere')

    with patch('app.community.routes.task_selector'):
        with patch('app.community.routes.delete_post_from_community') as delete:
            with patch('app.community.routes.render_template', return_value='rendered'):
                client, token = moderator_client
                client.post(url(app, 'community.community_ban_user',
                                community_id=community.id, user_id=member.id),
                            data={'reason': 'spam', 'ban_until': '',
                                  'delete_posts': 'y', 'submit': 'Ban',
                                  'csrf_token': token})

    assert [call.args[0] for call in delete.call_args_list] == [here_post.id]


def test_a_ban_that_deletes_nothing_leaves_the_content_alone(
        app, community_world, moderator_client):
    """Both checkboxes unticked. Banning without deleting is the common case,
    and `if posts:` / `if post_replies:` also decide whether the operator is
    told anything happened."""
    community, moderator, second, member = community_world
    post, reply = _post_and_reply(member, community, 'here')

    with patch('app.community.routes.task_selector'):
        with patch('app.community.routes.delete_post_from_community') as delete_post:
            with patch('app.community.routes.delete_post_reply_from_community') as delete_reply:
                with patch('app.community.routes.render_template', return_value='rendered'):
                    client, token = moderator_client
                    client.post(url(app, 'community.community_ban_user',
                                    community_id=community.id, user_id=member.id),
                                data={'reason': 'spam', 'ban_until': '',
                                      'submit': 'Ban', 'csrf_token': token})

    assert delete_post.call_args_list == []
    assert delete_reply.call_args_list == []


def test_deleting_when_there_is_nothing_to_delete_says_nothing(
        app, community_world, moderator_client):
    """`if posts:` and `if post_replies:` -- the false arms. A moderator who
    ticks the boxes for a user with no content must not be told content was
    deleted."""
    community, moderator, second, member = community_world

    with patch('app.community.routes.task_selector'):
        with patch('app.community.routes.flash') as flashed:
            with patch('app.community.routes.render_template', return_value='rendered'):
                client, token = moderator_client
                client.post(url(app, 'community.community_ban_user',
                                community_id=community.id, user_id=member.id),
                            data={'reason': 'spam', 'ban_until': '',
                                  'delete_posts': 'y', 'delete_post_replies': 'y',
                                  'submit': 'Ban', 'csrf_token': token})

    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'has been banned' in messages
    assert 'have been deleted' not in messages


# --------------------------------------------------------------------------
# The subscriber list's search and sort
# --------------------------------------------------------------------------


def _subscribers(app, client, community, query=''):
    with patch('app.community.routes.render_template', return_value='rendered') as render:
        client.get(url(app, 'community.community_moderate_subscribers',
                       actor=community.name) + (f'?{query}' if query else ''))
    return render.call_args.kwargs


def test_the_subscriber_search_matches_a_user_name(app, community_world,
                                                   moderator_client):
    community, moderator, second, member = community_world
    client, token = moderator_client

    listed = {row[0].user_name for row in
              _subscribers(app, client, community, 'search=second')['subscribers']}

    assert listed == {'secondmod'}


@pytest.mark.parametrize('sort_by, first', [
    ('joined DESC', 'member'),
    ('joined ASC', 'themod'),
    ('last_seen DESC', 'member'),
    ('last_seen ASC', 'themod'),
    ('local_remote DESC', None),
    ('local_remote ASC', None),
    ('user_name', None),
])
def test_each_subscriber_sort_orders_the_list(app, community_world,
                                              moderator_client, sort_by, first):
    """Every arm of the sort chain, including the `else` that catches an
    unrecognised value. `first` is asserted only where the ordering is
    deterministic -- the two members joined in a known order and were given
    distinct last_seen values."""
    from datetime import timedelta

    from app.models import utcnow

    community, moderator, second, member = community_world
    client, token = moderator_client
    for offset, user in enumerate((moderator, second, member)):
        user.last_seen = utcnow() - timedelta(days=10 - offset)
    db.session.commit()

    kwargs = _subscribers(app, client, community, f'sort_by={sort_by}')
    names = [row[0].user_name for row in kwargs['subscribers']]

    assert set(names) == {'themod', 'secondmod', 'member'}
    if first is not None:
        assert names[0] == first


def test_the_sort_button_redirects_and_keeps_the_search(app, community_world,
                                                        moderator_client):
    """`sort_by_btn` turns a submit button back into a bookmarkable URL, and
    the search must survive it."""
    community, moderator, second, member = community_world
    client, token = moderator_client

    response = client.get(url(app, 'community.community_moderate_subscribers',
                              actor=community.name)
                          + '?sort_by_btn=joined DESC&search=mod&page=2')

    assert response.status_code == 302
    location = response.headers['Location']
    assert 'sort_by=joined' in location
    assert 'search=mod' in location


def test_blocking_from_a_post_in_another_community_stays_put(app,
                                                             community_world):
    """The `"/post/" in curr_url` arm of community_block: a reader blocking a
    community from a post page that belongs to a DIFFERENT community should
    stay where they are, because the post they are reading is still visible."""
    from app.models import Post

    community, moderator, second, member = community_world
    elsewhere = make_community('elsewhere')
    db.session.commit()
    post = Post(user_id=member.id, community_id=elsewhere.id, title='over there',
                ap_id='https://test.piefed.local/post/overthere')
    db.session.add(post)
    db.session.commit()

    client = app.test_client()
    login(client, member)
    token = csrf(app, client)

    response = client.post(
        url(app, 'community.community_block', community_id=community.id)
        + f'?post_id={post.id}',
        data={'csrf_token': token},
        headers={'HX-Request': 'true',
                 'HX-Current-Url': f'/post/{post.id}'})

    assert response.headers['HX-Redirect'] == f'/post/{post.id}'


def test_blocking_from_a_post_in_the_same_community_does_not_redirect_there(
        app, community_world):
    """`if post.community.id != community_id` -- the false arm. The post is in
    the community just blocked, so sending the reader back to it would show
    them the thing they asked to hide."""
    from app.models import Post

    community, moderator, second, member = community_world
    post = Post(user_id=member.id, community_id=community.id, title='right here',
                ap_id='https://test.piefed.local/post/righthere')
    db.session.add(post)
    db.session.commit()

    client = app.test_client()
    login(client, member)
    token = csrf(app, client)

    response = client.post(
        url(app, 'community.community_block', community_id=community.id)
        + f'?post_id={post.id}',
        data={'csrf_token': token},
        headers={'HX-Request': 'true',
                 'HX-Current-Url': f'/post/{post.id}'})

    assert 'HX-Redirect' not in response.headers


def test_blocking_from_a_post_page_with_no_post_id_does_not_redirect(
        app, community_world):
    """`if post_id:` -- the HTMX caller may not have sent one."""
    community, moderator, second, member = community_world
    client = app.test_client()
    login(client, member)
    token = csrf(app, client)

    response = client.post(
        url(app, 'community.community_block', community_id=community.id),
        data={'csrf_token': token},
        headers={'HX-Request': 'true', 'HX-Current-Url': '/post/999'})

    assert 'HX-Redirect' not in response.headers


def test_banning_a_remote_user_creates_no_local_notification(app,
                                                             community_world,
                                                             moderator_client):
    """The `else` of `if user.is_local():` on the ban path -- a remote account
    has no notifications page here, and the ban reaches them through the
    federated activity instead. The branch body is `...` and a `todo`, so the
    only thing to assert is that nothing local was written."""
    from app.models import Notification

    community, moderator, second, member = community_world
    client, token = moderator_client
    remote = make_user(instance('remote.example', 'lemmy'), 'theirs', local=False)
    db.session.commit()
    make_community_member(remote, community)

    with patch('app.community.routes.task_selector') as task:
        with patch('app.community.routes.render_template', return_value='rendered'):
            client.post(url(app, 'community.community_ban_user',
                            community_id=community.id, user_id=remote.id),
                        data={'reason': 'spam', 'ban_until': '', 'submit': 'Ban',
                              'csrf_token': token})

    assert _is_banned(community, remote)
    assert Notification.query.filter_by(user_id=remote.id).count() == 0
    # The ban still federates -- that is how a remote account learns of it.
    assert task.call_args.args == ('ban_from_community',)


def test_an_unknown_community_is_a_404(app, community_world, moderator_client):
    """`if community is None: abort(404)`, hoisted above the form with the
    authorization check."""
    community, moderator, second, member = community_world
    client, token = moderator_client

    response = client.get(url(app, 'community.community_moderate_subscribers',
                              actor='no-such-community'))

    assert response.status_code == 404
