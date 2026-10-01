"""Community membership: joining, leaving and inviting.

Sub-project 80, slice D. Three defects:

* `do_subscribe`'s direct `CommunityBan` read flashed "You cannot join this
  community" and then joined the user anyway (D991);
* an unresolvable remote handle dereferenced `None` (D992);
* `InviteCommunityForm` had no cap, so any account on a default community could
  make the instance send unlimited email (D993).
"""
from unittest.mock import patch

from flask import render_template

import pytest

from flask_login import login_user

from app import cache, db
from app.auth.onboarding import join_topic
from app.models import (Community, CommunityBan, CommunityJoinRequest,
                        CommunityMember, InstanceBan, Topic, User)
from app.utils import user_banned_from_community
from tests.factories import (make_community, make_community_member,
                             make_instance, make_user)

pytestmark = pytest.mark.usefixtures('site')

SUBSCRIPTION_MEMBER = 1


def instance(domain='test.piefed.local', software='piefed'):
    """Fact 394."""
    from app.models import Instance

    existing = Instance.query.filter_by(domain=domain).first()
    return existing if existing is not None else make_instance(domain,
                                                               software=software)


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


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


def url(app, endpoint, **values):
    from flask import url_for

    with app.test_request_context():
        return url_for(endpoint, **values)


@pytest.fixture(autouse=True)
def no_memoized_answers():
    """`communities_banned_from` is memoized for 86400 seconds and
    `community_membership` for rather less; both are what these rows change."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def world(app, db_session):
    local = instance()
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1  # fact 347
    joiner = make_user(local, 'joiner', local=True)
    community = make_community('general')
    db.session.commit()
    return community, joiner, founder


def _is_member(community, user):
    return CommunityMember.query.filter_by(community_id=community.id,
                                           user_id=user.id).first() is not None


def _ban(community, user, by):
    db.session.add(CommunityBan(community_id=community.id, user_id=user.id,
                                banned_by=by.id, reason='spam'))
    db.session.commit()


# --------------------------------------------------------------------------
# D991: a community ban must prevent joining
# --------------------------------------------------------------------------


def test_a_banned_user_cannot_join_even_when_the_cached_list_is_stale(app,
                                                                      world):
    """D991's pin, inverted.

    `do_subscribe` checks twice. The first -- `community.id in
    communities_banned_from(user.id)` -- is the real gate and reads a list
    memoized for **86400 seconds**. The second reads the `CommunityBan` row
    directly, flashed "You cannot join this community", and then **fell
    through into the join**:

        PROBE n1 flashed: []
        PROBE n1 banned user is now a member? True

    The cached list is patched here to model exactly what a 24-hour cache does
    after a ban that arrives any way other than `community_ban_user` -- which
    is the only place that invalidates it. A ban federated in, or written by a
    tool that does not know to invalidate, leaves the first gate stale and the
    second inert.
    """
    from werkzeug.exceptions import Unauthorized

    from app.community.routes import do_subscribe

    community, joiner, founder = world
    _ban(community, joiner, founder)

    with patch('app.community.routes.communities_banned_from', return_value=[]):
        with patch('app.community.routes.flash'):
            with pytest.raises(Unauthorized):
                do_subscribe(community.name, joiner.id)

    assert not _is_member(community, joiner)


def test_a_banned_user_is_refused_by_the_cached_list_too(app, world):
    """The first gate, unpatched -- the one that normally does the refusing.
    Keeping both rows is what says the two checks are independent."""
    from werkzeug.exceptions import Unauthorized

    from app.community.routes import do_subscribe

    community, joiner, founder = world
    _ban(community, joiner, founder)
    cache.clear()

    with pytest.raises(Unauthorized):
        do_subscribe(community.name, joiner.id)

    assert not _is_member(community, joiner)


def test_an_admin_preload_reports_the_ban_instead_of_raising(app, world):
    """`admin_preload` is the bulk path used by the admin community importers,
    which must not abort the whole run for one banned pairing -- it returns a
    message instead. Asserted because the fix added a `return` to that arm."""
    from app.community.routes import do_subscribe

    community, joiner, founder = world
    _ban(community, joiner, founder)

    with patch('app.community.routes.communities_banned_from', return_value=[]):
        message = do_subscribe(community.name, joiner.id, admin_preload=True)

    assert message['community_banned_by_local_instance'] is True
    assert not _is_member(community, joiner)


def test_an_unbanned_user_joins(app, world):
    """The control. A row asserting only refusals passes against a function
    that refuses everyone."""
    from app.community.routes import do_subscribe

    community, joiner, founder = world

    with patch('app.community.routes.flash'):
        do_subscribe(community.name, joiner.id)

    assert _is_member(community, joiner)
    db.session.expire_all()
    assert db.session.get(Community, community.id).subscriptions_count == 1


def test_joining_twice_does_not_add_a_second_membership(app, world):
    """`if not existing_membership:` -- the join link is rendered from a cached
    membership state, so a second click is ordinary."""
    from app.community.routes import do_subscribe

    community, joiner, founder = world
    make_community_member(joiner, community)

    with patch('app.community.routes.flash'):
        do_subscribe(community.name, joiner.id)

    assert CommunityMember.query.filter_by(community_id=community.id,
                                           user_id=joiner.id).count() == 1


# --------------------------------------------------------------------------
# D992: an unresolvable remote handle
# --------------------------------------------------------------------------


def test_an_unresolvable_remote_community_is_not_found_rather_than_a_500(
        app, world):
    """D992's pin, inverted.

    `search_for_community` returns None for a handle it cannot resolve, and the
    next line read `community.banned`:

        PROBE n2 RAISED: AttributeError 'NoneType' object has no attribute 'banned'

    The function's own "community not found" path is at the bottom and was
    never reached.
    """
    from werkzeug.exceptions import NotFound

    from app.community.routes import do_subscribe

    community, joiner, founder = world

    with patch('app.community.routes.search_for_community', return_value=None):
        with pytest.raises(NotFound):
            do_subscribe('nobody@remote.example', joiner.id)


def test_an_unresolvable_remote_community_reports_itself_in_a_preload(app,
                                                                      world):
    """The `admin_preload` arm of the same path, which returns rather than
    aborting."""
    from app.community.routes import do_subscribe

    community, joiner, founder = world

    with patch('app.community.routes.search_for_community', return_value=None):
        message = do_subscribe('nobody@remote.example', joiner.id,
                               admin_preload=True)

    assert message['status'] == 'community not found'
    assert message['community'] == 'nobody@remote.example'


def test_a_banned_remote_community_is_treated_as_not_found(app, world):
    """`if community is not None and community.banned: community = None` -- a
    community this instance has banned must not be joinable, and the path it
    takes is the not-found one."""
    from werkzeug.exceptions import NotFound

    from app.community.routes import do_subscribe

    community, joiner, founder = world
    remote = make_community('theirs', host='remote.example')
    remote.ap_id = 'theirs@remote.example'
    remote.banned = True
    db.session.commit()

    with pytest.raises(NotFound):
        do_subscribe('theirs@remote.example', joiner.id)

    assert not _is_member(remote, joiner)


def test_joining_a_remote_community_sends_a_follow(app, world):
    """The remote arm: a CommunityJoinRequest is recorded and a Follow is
    posted to the community's inbox. Both matter -- the request is what an
    Accept is matched against later."""
    from app.models import Instance

    community, joiner, founder = world
    remote_instance = instance('remote.example', 'lemmy')
    remote_instance.gone_forever = False
    remote = make_community('theirs', host='remote.example')
    remote.ap_id = 'theirs@remote.example'
    remote.instance_id = remote_instance.id
    remote.ap_inbox_url = 'https://remote.example/c/theirs/inbox'
    joiner.private_key = 'a private key'
    db.session.commit()

    from app.community.routes import do_subscribe

    with patch('app.community.routes.send_post_request') as send:
        with patch.object(Instance, 'online', return_value=True):
            with patch('app.community.routes.flash'):
                do_subscribe('theirs@remote.example', joiner.id)

    assert _is_member(remote, joiner)
    assert CommunityJoinRequest.query.filter_by(user_id=joiner.id,
                                                community_id=remote.id).count() == 1
    assert send.call_args.args[0] == 'https://remote.example/c/theirs/inbox'
    assert send.call_args.args[1]['type'] == 'Follow'


def test_joining_a_remote_community_on_a_dead_instance_sends_nothing(app,
                                                                     world):
    """`if community.instance.online():` -- posting to an instance known to be
    gone costs a timeout, and the membership is recorded locally either way."""
    from app.models import Instance

    community, joiner, founder = world
    remote_instance = instance('remote.example', 'lemmy')
    remote = make_community('theirs', host='remote.example')
    remote.ap_id = 'theirs@remote.example'
    remote.instance_id = remote_instance.id
    joiner.private_key = 'a private key'
    db.session.commit()

    from app.community.routes import do_subscribe

    with patch('app.community.routes.send_post_request') as send:
        with patch.object(Instance, 'online', return_value=False):
            with patch('app.community.routes.flash'):
                do_subscribe('theirs@remote.example', joiner.id)

    assert send.call_args_list == []
    assert _is_member(remote, joiner)


# --------------------------------------------------------------------------
# D993: the invite box is an outbound email primitive
# --------------------------------------------------------------------------


@pytest.fixture
def inviter(app, world):
    """An account old enough to invite. `community_invite` refuses
    `created_very_recently()` accounts that are not admins."""
    from datetime import timedelta

    from app.models import utcnow

    community, joiner, founder = world
    joiner.created = utcnow() - timedelta(days=30)
    db.session.commit()
    assert not joiner.created_very_recently()

    client = app.test_client()
    login(client, joiner)
    return client, csrf(app, client), community, joiner


def _invite(app, client, token, community, to):
    with patch('app.community.routes.render_template', return_value='rendered') as render:
        response = client.post(url(app, 'community.community_invite',
                                   actor=community.name),
                               data={'to': to, 'submit': 'Invite',
                                     'csrf_token': token})
    return response, render


def test_the_invite_box_is_capped(app, inviter):
    """D993's pin, inverted.

    `community_invite` calls `invite_with_email` once per line, from this
    instance's own mail server. `Community.invitations` defaults to 0 and
    `can_invite()` returns True for anyone when it is 0, so before the cap any
    account past `created_very_recently()` could paste ten thousand addresses
    into a default community's invite box and have the instance send ten
    thousand emails under its own reputation.
    """
    client, token, community, joiner = inviter
    addresses = '\n'.join(f'person{index}@example.com' for index in range(50))

    with patch('app.shared.community.invite_with_email') as invite:
        response, render = _invite(app, client, token, community, addresses)

    assert response.status_code == 200
    assert 'no more than 20' in str(render.call_args.kwargs['form'].to.errors[0])
    assert invite.call_args_list == [], 'a refused submission still sent email'


def test_a_submission_at_the_cap_is_accepted(app, inviter):
    """The boundary. A cap that refuses the number it advertises is a
    different bug."""
    client, token, community, joiner = inviter
    addresses = '\n'.join(f'person{index}@example.com' for index in range(20))

    with patch('app.shared.community.invite_with_email', return_value=1) as invite:
        response, _render = _invite(app, client, token, community, addresses)

    assert response.status_code == 302
    assert invite.call_count == 20


def test_blank_lines_do_not_count_towards_the_cap(app, inviter):
    """People paste lists with trailing newlines. Counting those would refuse a
    submission that invites fewer people than the limit."""
    client, token, community, joiner = inviter
    addresses = '\n\n'.join(f'person{index}@example.com' for index in range(20)) + '\n\n\n'

    with patch('app.shared.community.invite_with_email', return_value=1) as invite:
        response, _render = _invite(app, client, token, community, addresses)

    assert response.status_code == 302
    assert invite.call_count == 20


def test_commas_are_still_refused(app, inviter):
    """The validator's existing rule, kept alongside the new one."""
    client, token, community, joiner = inviter

    with patch('app.shared.community.invite_with_email') as invite:
        _response, render = _invite(app, client, token, community,
                                    'a@example.com, b@example.com')

    assert 'Use new lines instead of commas.' in \
        str(render.call_args.kwargs['form'].to.errors[0])
    assert invite.call_args_list == []


def test_an_email_address_is_invited_by_email(app, inviter):
    """The `else` of the `@` branch -- a plain address gets an email."""
    client, token, community, joiner = inviter

    with patch('app.shared.community.invite_with_email', return_value=1) as email:
        with patch('app.shared.community.invite_with_chat') as chat:
            _invite(app, client, token, community, 'someone@example.com')

    assert email.call_args.args == (community.id, 'someone@example.com', 1)
    assert chat.call_args_list == []


def test_a_fediverse_handle_is_invited_by_chat(app, inviter):
    """`if line.startswith('@')` -- a handle gets a chat message rather than an
    email, because there is no address to send to."""
    client, token, community, joiner = inviter

    with patch('app.shared.community.invite_with_chat', return_value=1) as chat:
        with patch('app.shared.community.invite_with_email') as email:
            _invite(app, client, token, community, '@someone@remote.example')

    assert chat.call_args.args[1] == '@someone@remote.example'
    assert email.call_args_list == []


def test_a_url_is_invited_by_chat(app, inviter):
    """`if line.startswith('http')` -- a profile URL pasted from a browser."""
    client, token, community, joiner = inviter

    with patch('app.shared.community.invite_with_chat', return_value=1) as chat:
        _invite(app, client, token, community, 'https://remote.example/u/someone')

    assert chat.call_args.args[1] == 'https://remote.example/u/someone'


def test_the_same_address_is_not_emailed_twice(app, inviter):
    """`sent_to` -- a pasted list often repeats, and each duplicate would be
    another email to the same person."""
    client, token, community, joiner = inviter

    with patch('app.shared.community.invite_with_email', return_value=1) as email:
        _invite(app, client, token, community,
                'someone@example.com\nsomeone@example.com')

    assert email.call_count == 1


def test_a_banned_user_cannot_invite(app, inviter):
    client, token, community, joiner = inviter
    joiner.banned = True
    db.session.commit()

    with patch('app.shared.community.invite_with_email') as invite:
        _invite(app, client, token, community, 'someone@example.com')

    assert invite.call_args_list == []


def test_a_very_new_account_cannot_invite(app, world):
    """`created_very_recently() and not is_admin()` -- the only other limit on
    this route, and the reason the cap matters: an account that waits is
    otherwise unconstrained."""
    from app.models import utcnow

    community, joiner, founder = world
    joiner.created = utcnow()
    db.session.commit()
    client = app.test_client()
    login(client, joiner)
    token = csrf(app, client)

    with patch('app.shared.community.invite_with_email') as invite:
        with patch('app.community.routes.flash') as flashed:
            response = client.post(url(app, 'community.community_invite',
                                       actor=community.name),
                                   data={'to': 'someone@example.com',
                                         'submit': 'Invite',
                                         'csrf_token': token})

    assert response.status_code == 302
    assert 'too new' in flashed.call_args.args[0]
    assert invite.call_args_list == []


def test_a_community_that_does_not_allow_invitations_refuses(app, inviter):
    """`community.can_invite()` -- `invitations` of 2, 3 or 4 restrict it to
    members, moderators or owners, and the inviter here is none of those."""
    from app.constants import INVITE_MODS_ONLY

    client, token, community, joiner = inviter
    community.invitations = INVITE_MODS_ONLY
    db.session.commit()

    with patch('app.shared.community.invite_with_email') as invite:
        with patch('app.community.routes.flash') as flashed:
            response = client.post(url(app, 'community.community_invite',
                                       actor=community.name),
                                   data={'to': 'someone@example.com',
                                         'submit': 'Invite',
                                         'csrf_token': token})

    assert response.status_code == 302
    assert 'cannot invite people' in flashed.call_args.args[0]
    assert invite.call_args_list == []


def test_the_invite_form_renders_on_a_get(app, inviter):
    client, token, community, joiner = inviter

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        response = client.get(url(app, 'community.community_invite',
                                  actor=community.name))

    assert response.status_code == 200
    assert render.call_args.kwargs['form'] is not None


def test_inviting_to_a_community_that_does_not_exist_is_handled(app, inviter):
    """`if community is not None:` -- the actor comes from the URL."""
    client, token, community, joiner = inviter

    response = client.get(url(app, 'community.community_invite',
                              actor='no-such-community'))

    assert response.status_code in (302, 404)


# --------------------------------------------------------------------------
# Accepting an invitation
# --------------------------------------------------------------------------


def _invitation(community, user, token='abc123'):
    from app.models import CommunityInvitation

    invitation = CommunityInvitation(community_id=community.id, user_id=user.id,
                                     token=token)
    db.session.add(invitation)
    db.session.commit()
    return invitation


def test_accepting_an_invitation_joins_and_consumes_it(app, world):
    """The invitation is deleted as well as acted on -- a token that still
    works after use is a token that can be shared."""
    from app.models import CommunityInvitation

    community, joiner, founder = world
    community.invitations = 2  # INVITE_MEMBERS_ONLY
    db.session.commit()
    _invitation(community, joiner)
    client = app.test_client()
    login(client, joiner)
    token = csrf(app, client)

    with patch('app.community.routes.render_template', return_value='rendered'):
        with patch('app.community.routes.flash'):
            response = client.post(
                url(app, 'community.community_invite_accept',
                    actor=community.name, token='abc123'),
                data={'submit': 'Accept', 'csrf_token': token})

    assert response.status_code == 302
    assert _is_member(community, joiner)
    assert CommunityInvitation.query.count() == 0


def test_the_accept_form_renders_before_it_is_submitted(app, world):
    """A GET must not consume the invitation -- the confirmation page is what
    stops a link preview from spending it."""
    from app.models import CommunityInvitation

    community, joiner, founder = world
    _invitation(community, joiner)
    client = app.test_client()
    login(client, joiner)

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        response = client.get(url(app, 'community.community_invite_accept',
                                  actor=community.name, token='abc123'))

    assert response.status_code == 200
    assert render.call_args.kwargs['form'] is not None
    assert CommunityInvitation.query.count() == 1
    assert not _is_member(community, joiner)


def test_an_invitation_belonging_to_someone_else_is_not_accepted(app, world):
    """`CommunityInvitation.user_id == current_user.id` -- the token alone is
    not enough, which is what stops a leaked link admitting anyone."""
    community, joiner, founder = world
    other = make_user(instance(), 'other', local=True)
    db.session.commit()
    _invitation(community, other)
    client = app.test_client()
    login(client, joiner)
    token = csrf(app, client)

    with patch('app.community.routes.render_template', return_value='rendered'):
        with patch('app.community.routes.flash'):
            client.post(url(app, 'community.community_invite_accept',
                            actor=community.name, token='abc123'),
                        data={'submit': 'Accept', 'csrf_token': token})

    assert not _is_member(community, joiner)


def test_an_invitation_for_another_community_is_not_accepted(app, world):
    """`CommunityInvitation.community_id == community.id` -- the other half of
    the same pairing, which is the class D969-D971 were about."""
    community, joiner, founder = world
    elsewhere = make_community('elsewhere')
    db.session.commit()
    _invitation(elsewhere, joiner)
    client = app.test_client()
    login(client, joiner)
    token = csrf(app, client)

    with patch('app.community.routes.render_template', return_value='rendered'):
        with patch('app.community.routes.flash'):
            client.post(url(app, 'community.community_invite_accept',
                            actor=community.name, token='abc123'),
                        data={'submit': 'Accept', 'csrf_token': token})

    assert not _is_member(community, joiner)


def test_an_unresolvable_actor_on_an_invite_link_is_a_404(app, world):
    """D992's shape, second instance in this file: `actor_to_community`
    returns None for an actor it cannot resolve and the next line called
    `community.is_member(...)`. The actor comes from the URL, so a stale or
    mistyped invite link was an AttributeError."""
    community, joiner, founder = world
    client = app.test_client()
    login(client, joiner)

    response = client.get(url(app, 'community.community_invite_accept',
                              actor='no-such-community', token='abc123'))

    assert response.status_code == 404


def test_an_already_joined_member_is_told_so(app, world):
    community, joiner, founder = world
    make_community_member(joiner, community)
    client = app.test_client()
    login(client, joiner)

    with patch('app.community.routes.flash') as flashed:
        response = client.get(url(app, 'community.community_invite_accept',
                                  actor=community.name, token='abc123'))

    assert response.status_code == 302
    assert flashed.call_args.args[0] == 'You are already a member.'


def test_a_handle_pasted_as_a_token_is_explained(app, world):
    """`if '@' in token:` -- people paste the inviter's handle into the URL,
    and the message tells them to ask that person instead."""
    community, joiner, founder = world
    client = app.test_client()
    login(client, joiner)

    with patch('app.community.routes.flash') as flashed:
        response = client.get(url(app, 'community.community_invite_accept',
                                  actor=community.name,
                                  token='someone@remote.example'))

    assert response.status_code == 302
    assert 'send an invite' in flashed.call_args.args[0]


def test_a_banned_user_cannot_accept_an_invitation(app, world):
    community, joiner, founder = world
    _invitation(community, joiner)
    joiner.banned = True
    db.session.commit()
    client = app.test_client()
    login(client, joiner)
    token = csrf(app, client)

    with patch('app.community.routes.render_template', return_value='rendered'):
        client.post(url(app, 'community.community_invite_accept',
                        actor=community.name, token='abc123'),
                    data={'submit': 'Accept', 'csrf_token': token})

    assert not _is_member(community, joiner)


# --------------------------------------------------------------------------
# Leaving
# --------------------------------------------------------------------------


@pytest.fixture
def member_client(app, world):
    community, joiner, founder = world
    make_community_member(joiner, community)
    db.session.commit()
    client = app.test_client()
    login(client, joiner)
    return client, csrf(app, client), community, joiner


def test_leaving_a_local_community_removes_the_membership(app, member_client):
    community, joiner = member_client[2], member_client[3]
    client, token = member_client[0], member_client[1]

    with patch('app.community.routes.flash'):
        response = client.post(url(app, 'community.unsubscribe',
                                   actor=community.name),
                               data={'csrf_token': token})

    assert response.status_code in (200, 302)
    db.session.expire_all()
    membership = CommunityMember.query.filter_by(community_id=community.id,
                                                 user_id=joiner.id).first()
    assert membership is None or membership.is_banned is False


def test_an_owner_cannot_leave_their_own_community(app, world):
    """`if subscription != SUBSCRIPTION_OWNER:` -- a community with no owner
    cannot be administered by anyone but instance staff, which is the same
    invariant `community_remove_owner`'s last-owner guard protects."""
    community, joiner, founder = world
    membership = make_community_member(joiner, community, is_moderator=True)
    membership.is_owner = True
    db.session.commit()
    client = app.test_client()
    login(client, joiner)
    token = csrf(app, client)

    with patch('app.community.routes.flash'):
        client.post(url(app, 'community.unsubscribe', actor=community.name),
                    data={'csrf_token': token})

    db.session.expire_all()
    assert CommunityMember.query.filter_by(community_id=community.id,
                                           user_id=joiner.id).count() == 1


def test_leaving_a_remote_community_sends_an_undo(app, world):
    """The remote arm -- the community is on another server, so leaving means
    telling it. An Undo that never goes out leaves the peer sending content
    here forever."""
    from app.models import Instance

    community, joiner, founder = world
    remote_instance = instance('remote.example', 'lemmy')
    remote_instance.gone_forever = False
    remote = make_community('theirs', host='remote.example')
    remote.ap_id = 'theirs@remote.example'
    remote.instance_id = remote_instance.id
    remote.ap_inbox_url = 'https://remote.example/c/theirs/inbox'
    joiner.private_key = 'a private key'
    db.session.commit()
    make_community_member(joiner, remote)

    client = app.test_client()
    login(client, joiner)
    token = csrf(app, client)

    with patch('app.community.routes.send_post_request') as send:
        with patch('app.community.routes.flash'):
            client.post(url(app, 'community.unsubscribe',
                            actor=remote.link()), data={'csrf_token': token})

    assert send.call_args.args[1]['type'] == 'Undo'
    assert send.call_args.args[1]['object']['type'] == 'Follow'


def test_leaving_a_remote_community_on_a_dead_instance_sends_nothing(app,
                                                                     world):
    """`if not community.instance.gone_forever:` -- the local membership still
    goes, but nothing is posted to an instance known to be gone."""
    community, joiner, founder = world
    remote_instance = instance('remote.example', 'lemmy')
    remote_instance.gone_forever = True
    remote = make_community('theirs', host='remote.example')
    remote.ap_id = 'theirs@remote.example'
    remote.instance_id = remote_instance.id
    db.session.commit()
    make_community_member(joiner, remote)

    client = app.test_client()
    login(client, joiner)
    token = csrf(app, client)

    with patch('app.community.routes.send_post_request') as send:
        with patch('app.community.routes.flash'):
            client.post(url(app, 'community.unsubscribe',
                            actor=remote.link()), data={'csrf_token': token})

    assert send.call_args_list == []


def test_leaving_a_community_you_are_not_in_is_harmless(app, world):
    """`if subscription:` -- the leave link is rendered from a cached
    membership state, so a second click is ordinary."""
    community, joiner, founder = world
    client = app.test_client()
    login(client, joiner)
    token = csrf(app, client)

    with patch('app.community.routes.flash'):
        response = client.post(url(app, 'community.unsubscribe',
                                   actor=community.name),
                               data={'csrf_token': token})

    assert response.status_code in (200, 302)


def test_leaving_a_community_that_does_not_exist_is_handled(app, world):
    community, joiner, founder = world
    client = app.test_client()
    login(client, joiner)
    token = csrf(app, client)

    response = client.post(url(app, 'community.unsubscribe',
                               actor='no-such-community'),
                           data={'csrf_token': token})

    assert response.status_code in (302, 404)


def test_leaving_everything_leaves_each_community_but_not_the_ones_you_run(
        app, world):
    """`subscription < SUBSCRIPTION_MODERATOR` -- "leave all" is a user
    clearing their feed, not resigning their moderator posts. Both halves are
    asserted, because a row that only checks the leaves passes against a
    function that leaves everything."""
    community, joiner, founder = world
    moderated = make_community('moderated')
    db.session.commit()
    make_community_member(joiner, community)
    make_community_member(joiner, moderated, is_moderator=True)
    db.session.commit()

    client = app.test_client()
    login(client, joiner)
    token = csrf(app, client)

    with patch('app.shared.community.leave_community') as leave:
        with patch('app.community.routes.flash'):
            response = client.post(url(app, 'community.community_leave_all'),
                                   data={'csrf_token': token})

    assert response.status_code == 302
    left = [call.kwargs['community_id'] for call in leave.call_args_list]
    assert community.id in left
    assert moderated.id not in left, 'leave-all resigned a moderator post'


def test_leaving_everything_also_leaves_subscribed_feeds(app, world):
    """`if joined_feed_ids:` -- feeds are a second kind of subscription and the
    button says "all"."""
    community, joiner, founder = world
    client = app.test_client()
    login(client, joiner)
    token = csrf(app, client)

    with patch('app.community.routes.subscribed_feeds', return_value=[7]):
        with patch('app.community.routes.feed_membership', return_value=1):
            with patch('app.shared.feed.leave_feed') as leave_feed:
                with patch('app.community.routes.flash'):
                    client.post(url(app, 'community.community_leave_all'),
                                data={'csrf_token': token})

    assert leave_feed.call_count == 1


def test_leaving_everything_with_no_feeds_is_fine(app, world):
    """The false arm of the same guard."""
    community, joiner, founder = world
    client = app.test_client()
    login(client, joiner)
    token = csrf(app, client)

    with patch('app.community.routes.subscribed_feeds', return_value=[]):
        with patch('app.shared.feed.leave_feed') as leave_feed:
            with patch('app.community.routes.flash') as flashed:
                response = client.post(url(app, 'community.community_leave_all'),
                                       data={'csrf_token': token})

    assert response.status_code == 302
    assert leave_feed.call_args_list == []
    assert 'unsubscribed from all communities' in flashed.call_args.args[0]


# --------------------------------------------------------------------------
# join_then_add
# --------------------------------------------------------------------------


def _able_to_post(user):
    """`join_then_add` stacks validation_required and approval_required behind
    login_required, so an unverified account with no private_key is redirected
    to /auth/validation_required or /auth/please_wait -- a 302 that looks like
    whatever refusal the row was testing for. Same trap as slice C's
    `able_to_create`, different route."""
    user.verified = True
    user.private_key = 'a private key'
    db.session.commit()
    return user


def test_join_then_add_joins_a_local_community_and_goes_to_the_post_form(
        app, world):
    """The route behind "post to a community you have not joined" -- it joins
    and then forwards to the add-post form."""
    community, joiner, founder = world
    _able_to_post(joiner)
    client = app.test_client()
    login(client, joiner)
    token = csrf(app, client)

    with patch('app.community.routes.render_template', return_value='rendered'):
        response = client.post(url(app, 'community.join_then_add', actor=community.name),
                               data={'csrf_token': token})

    assert response.status_code in (200, 302)
    assert _is_member(community, joiner)


def test_join_then_add_by_get_is_refused(app, world):
    """D994, fixed: joining changes state, so it is POST-only with the CSRF
    token. The feed and topic "post to" pickers reach it with a 307, which
    re-posts their own token-carrying form."""
    community, joiner, founder = world
    _able_to_post(joiner)
    client = app.test_client()
    login(client, joiner)

    response = client.get(url(app, 'community.join_then_add', actor=community.name))

    assert response.status_code == 405
    assert not _is_member(community, joiner)


def test_join_then_add_without_the_token_is_refused(app, world):
    community, joiner, founder = world
    _able_to_post(joiner)
    client = app.test_client()
    login(client, joiner)

    response = client.post(url(app, 'community.join_then_add', actor=community.name))

    assert response.status_code == 400
    assert not _is_member(community, joiner)


def test_join_then_add_on_an_unresolvable_actor_is_a_404(app, world):
    """D992's shape, third instance in this file."""
    community, joiner, founder = world
    _able_to_post(joiner)
    client = app.test_client()
    login(client, joiner)

    response = client.post(url(app, 'community.join_then_add', actor='no-such-community'),
                           data={'csrf_token': csrf(app, client)})

    assert response.status_code == 404


def test_join_then_add_does_not_join_twice(app, world):
    """`if not current_user.subscribed(community.id):`."""
    community, joiner, founder = world
    _able_to_post(joiner)
    make_community_member(joiner, community)
    db.session.commit()
    client = app.test_client()
    login(client, joiner)

    with patch('app.community.routes.render_template', return_value='rendered'):
        client.post(url(app, 'community.join_then_add', actor=community.name),
                    data={'csrf_token': csrf(app, client)})

    assert CommunityMember.query.filter_by(community_id=community.id,
                                           user_id=joiner.id).count() == 1


# --------------------------------------------------------------------------
# The last arms
# --------------------------------------------------------------------------


def test_a_bulk_preload_reports_each_outcome(app, world):
    """`admin_preload`'s three status strings. The admin community importers
    (D924's `admin_federation_preload`, and the two remote scans) call this in
    a loop and show the messages, so each outcome has to be distinguishable."""
    from app.community.routes import do_subscribe

    community, joiner, founder = world

    joined = do_subscribe(community.name, joiner.id, admin_preload=True)
    assert joined['status'] == 'joined'
    assert joined['community'] == community.ap_id

    again = do_subscribe(community.name, joiner.id, admin_preload=True)
    assert again['status'] == 'already subscribed, or subscription pending'


def test_a_bulk_preload_reports_an_instance_level_ban(app, world):
    """`pre_load_message['user_banned']` -- the first gate's admin arm.

    It recorded the refusal and then **fell through into the join**, exactly as
    the direct-read check below it did (D991), so a bulk importer subscribed
    the account to a community it is banned from while reporting that it could
    not. Found by writing this row: the message was right and the membership
    was there too.
    """
    from app.community.routes import do_subscribe

    community, joiner, founder = world

    with patch('app.community.routes.communities_banned_from',
               return_value=[community.id]):
        message = do_subscribe(community.name, joiner.id, admin_preload=True)

    assert message['user_banned'] is True
    assert not _is_member(community, joiner)


def test_a_banned_user_joining_themselves_is_told_why(app, world):
    """The flash on the direct-read path, which needs `current_user` to be the
    person being subscribed -- the same call made by an admin importer for
    somebody else says nothing to anybody."""
    from werkzeug.exceptions import Unauthorized

    from app.community.routes import do_subscribe

    community, joiner, founder = world
    _ban(community, joiner, founder)
    client = app.test_client()
    login(client, joiner)

    with app.test_request_context():
        from flask_login import login_user
        login_user(joiner)
        with patch('app.community.routes.communities_banned_from', return_value=[]):
            with patch('app.community.routes.flash') as flashed:
                with pytest.raises(Unauthorized):
                    do_subscribe(community.name, joiner.id)

    assert flashed.call_args.args[0] == 'You cannot join this community'


def test_leaving_a_community_by_get_is_refused(app, member_client):
    """D994, fixed (owner ruling 2026-09-30). Leaving used to accept GET, which
    `login_required` never CSRF-checks, so any page could make a signed-in user
    leave a community. It is POST-only now; the no-JS button is a form."""
    client, token, community, joiner = member_client

    response = client.get(url(app, 'community.unsubscribe', actor=community.name))

    assert response.status_code == 405
    assert _is_member(community, joiner)


def test_leaving_a_community_without_the_token_is_refused(app, member_client):
    client, token, community, joiner = member_client

    response = client.post(url(app, 'community.unsubscribe', actor=community.name))

    assert response.status_code == 400
    assert _is_member(community, joiner)


def test_leaving_a_community_by_form_says_so(app, member_client):
    """The no-JS path, now a plain form POST: it flashes and goes back, because
    there is no HTMX fragment to swap in."""
    client, token, community, joiner = member_client

    with patch('app.community.routes.flash') as flashed:
        response = client.post(url(app, 'community.unsubscribe', actor=community.name),
                               data={'csrf_token': token})

    assert response.status_code == 302
    assert 'You left' in str(flashed.call_args.args[0])


def test_leaving_a_community_by_htmx_returns_the_join_button(app, member_client):
    """The HTMX path returns the re-rendered button fragment rather than a
    redirect."""
    client, token, community, joiner = member_client

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        response = client.post(url(app, 'community.unsubscribe',
                                   actor=community.name),
                               data={'csrf_token': token},
                               headers={'HX-Request': 'true'})

    assert response.status_code == 200
    assert render.call_args.args == ('community/_join_button.html',)


def test_the_leave_button_is_a_form_carrying_the_token(app, world):
    """D994: the button every page includes posts with the CSRF token, with
    htmx or without it, instead of linking to a GET."""
    community, joiner, founder = world

    with app.test_request_context('/'):
        html = render_template('community/_leave_button.html', community=community)

    assert f'<form method="post" action="/community/{community.link()}/unsubscribe"' in html
    assert 'name="csrf_token"' in html
    assert 'href=' not in html


def test_an_owner_is_told_to_hand_over_first(app, world):
    """The `else` of the owner guard -- the message is the only thing telling
    them what to do about it."""
    community, joiner, founder = world
    membership = make_community_member(joiner, community, is_moderator=True)
    membership.is_owner = True
    db.session.commit()
    client = app.test_client()
    login(client, joiner)

    with patch('app.community.routes.flash') as flashed:
        client.post(url(app, 'community.unsubscribe', actor=community.name),
                    data={'csrf_token': csrf(app, client)})

    assert 'make someone else the owner' in flashed.call_args.args[0]


def test_unsubscribing_reuses_the_original_follow_id(app, world):
    """D89, fixed (owner ruling). A peer that matches an Undo to the Follow by
    id needs the ORIGINAL request's uuid rather than a fresh one; that was a
    workaround for ovo.st alone, named by domain. Every peer now gets it
    whenever the join request exists -- this one is not ovo.st."""
    community, joiner, founder = world
    peer = instance('remote.example', 'lemmy')
    peer.gone_forever = False
    remote = make_community('theirs', host='remote.example')
    remote.ap_id = 'theirs@remote.example'
    remote.instance_id = peer.id
    remote.ap_inbox_url = 'https://remote.example/c/theirs/inbox'
    joiner.private_key = 'a private key'
    db.session.commit()
    make_community_member(joiner, remote)
    join_request = CommunityJoinRequest(user_id=joiner.id,
                                        community_id=remote.id)
    db.session.add(join_request)
    db.session.commit()
    expected = str(join_request.uuid)

    client = app.test_client()
    login(client, joiner)
    token = csrf(app, client)

    with patch('app.community.routes.send_post_request') as send:
        with patch('app.community.routes.flash'):
            client.post(url(app, 'community.unsubscribe', actor=remote.link()),
                        data={'csrf_token': token})

    assert send.call_args.args[1]['object']['id'].endswith(expected)


def test_join_then_add_on_a_remote_community_sends_a_follow(app, world):
    """`if not community.is_local():` -- the remote arm of join_then_add,
    which records a join request and posts a Follow."""
    from app.models import Instance

    community, joiner, founder = world
    _able_to_post(joiner)
    remote_instance = instance('remote.example', 'lemmy')
    remote_instance.gone_forever = False
    remote = make_community('theirs', host='remote.example')
    remote.ap_id = 'theirs@remote.example'
    remote.instance_id = remote_instance.id
    remote.ap_inbox_url = 'https://remote.example/c/theirs/inbox'
    db.session.commit()

    client = app.test_client()
    login(client, joiner)

    with patch('app.community.routes.send_post_request') as send:
        with patch('app.community.routes.render_template', return_value='rendered'):
            client.post(url(app, 'community.join_then_add', actor=remote.link()),
                        data={'csrf_token': csrf(app, client)})

    assert CommunityJoinRequest.query.filter_by(user_id=joiner.id,
                                                community_id=remote.id).count() == 1
    assert send.call_args.args[1]['type'] == 'Follow'
    assert _is_member(remote, joiner)


def test_join_then_add_on_a_dead_remote_instance_sends_nothing(app, world):
    """`if not community.instance.gone_forever:`."""
    community, joiner, founder = world
    _able_to_post(joiner)
    remote_instance = instance('remote.example', 'lemmy')
    remote_instance.gone_forever = True
    remote = make_community('theirs', host='remote.example')
    remote.ap_id = 'theirs@remote.example'
    remote.instance_id = remote_instance.id
    db.session.commit()

    client = app.test_client()
    login(client, joiner)

    with patch('app.community.routes.send_post_request') as send:
        with patch('app.community.routes.render_template', return_value='rendered'):
            client.post(url(app, 'community.join_then_add', actor=remote.link()),
                        data={'csrf_token': csrf(app, client)})

    assert send.call_args_list == []
    assert _is_member(remote, joiner)


def test_the_invite_page_warns_about_a_local_only_community(app, inviter):
    """`if community.is_local() and community.local_only:` -- inviting people
    from other instances to a community they cannot reach is the mistake this
    message exists to prevent."""
    client, token, community, joiner = inviter
    community.local_only = True
    db.session.commit()

    with patch('app.community.routes.flash') as flashed:
        with patch('app.community.routes.render_template', return_value='rendered'):
            client.get(url(app, 'community.community_invite',
                           actor=community.name))

    assert 'can only be accessed by people who have a' in flashed.call_args.args[0]


def test_join_then_add_refuses_a_user_banned_from_the_community(app, world):
    """`if not user_banned_from_community(...): ... else: abort(401)`.

    The final gate on this route. It used to be a third independent answer to
    "is this user banned here" (`Community.user_is_banned`, its own query);
    D995 put this route, `do_subscribe` and every other caller on the one
    helper.
    """
    community, joiner, founder = world
    _able_to_post(joiner)
    _ban(community, joiner, founder)
    make_community_member(joiner, community)
    db.session.commit()
    client = app.test_client()
    login(client, joiner)

    with patch('app.community.routes.render_template', return_value='rendered'):
        response = client.post(url(app, 'community.join_then_add', actor=community.name),
                               data={'csrf_token': csrf(app, client)})

    assert response.status_code == 401


def test_resubscribing_with_a_flagged_membership_adds_no_second_row(app,
                                                                     world):
    """`if not existing_membership:` inside `do_subscribe`, which is reachable
    only for a membership the outer guard does not recognise as current.

    `User.subscribed()` returns `SUBSCRIPTION_BANNED` for a `CommunityMember`
    with `is_banned=True` (app/models.py:1435), and the outer guard only skips
    `SUBSCRIPTION_MEMBER` and `SUBSCRIPTION_PENDING` -- so a membership flagged
    by a ban that has since been lifted at the `CommunityBan` level reaches the
    inner check. Without it the user gets two `CommunityMember` rows for one
    community, and `subscribed()` answers from whichever it finds first.
    """
    from app.community.routes import do_subscribe

    community, joiner, founder = world
    membership = make_community_member(joiner, community)
    membership.is_banned = True
    db.session.commit()

    with patch('app.community.routes.flash'):
        do_subscribe(community.name, joiner.id)

    assert CommunityMember.query.filter_by(community_id=community.id,
                                           user_id=joiner.id).count() == 1


def test_leaving_everything_does_not_leave_a_feed_you_own(app, world):
    """`if subscription != SUBSCRIPTION_OWNER:` on the feed loop -- the same
    rule as for communities, and it needs its own row because the feed branch
    is a separate loop with its own membership helper."""
    community, joiner, founder = world
    client = app.test_client()
    login(client, joiner)
    token = csrf(app, client)

    from app.constants import SUBSCRIPTION_OWNER

    with patch('app.community.routes.subscribed_feeds', return_value=[7]):
        with patch('app.community.routes.feed_membership',
                   return_value=SUBSCRIPTION_OWNER):
            with patch('app.shared.feed.leave_feed') as leave_feed:
                with patch('app.community.routes.flash'):
                    client.post(url(app, 'community.community_leave_all'),
                                data={'csrf_token': token})

    assert leave_feed.call_args_list == [], 'leave-all abandoned a feed it owns'


def test_leaving_by_htmx_says_nothing(app, member_client):
    """`if not request.headers.get('HX-Request'):` guards the flash -- the HTMX
    path swaps in the re-rendered button instead, and a flash queued there would
    appear unannounced on whatever page the reader loads next."""
    client, token, community, joiner = member_client

    with patch('app.community.routes.render_template', return_value='rendered'):
        with patch('app.community.routes.flash') as flashed:
            client.post(url(app, 'community.unsubscribe',
                            actor=community.name), data={'csrf_token': token},
                        headers={'HX-Request': 'true'})

    assert flashed.call_args_list == []


def test_join_then_add_does_not_claim_you_joined_when_you_already_had(
        app, world):
    """`if not current_user.subscribed(community.id):` guards the flash as well
    as the join. Without it an existing member is told "You joined ..." every
    time they use the post form, which is the only thing that distinguishes
    the guard from the `existing_member` check inside it."""
    community, joiner, founder = world
    _able_to_post(joiner)
    make_community_member(joiner, community)
    db.session.commit()
    client = app.test_client()
    login(client, joiner)

    with patch('app.community.routes.render_template', return_value='rendered'):
        with patch('app.community.routes.flash') as flashed:
            client.post(url(app, 'community.join_then_add', actor=community.name),
                        data={'csrf_token': csrf(app, client)})

    assert flashed.call_args_list == []


# --------------------------------------------------------------------------
# D995: one answer to "is this user banned from this community"
# --------------------------------------------------------------------------


def _instance_ban(community, user):
    """A ban from the whole instance the community lives on, which
    `communities_banned_from` counts and the old `Community.user_is_banned`
    did not."""
    db.session.add(InstanceBan(user_id=user.id, instance_id=community.instance_id))
    db.session.commit()


@pytest.mark.parametrize('ban, expected', [
    (None, False), ('community', True), ('instance', True)])
def test_the_helper_answers_for_community_and_instance_bans(app, world, ban, expected):
    community, joiner, founder = world
    if ban == 'community':
        _ban(community, joiner, founder)
    elif ban == 'instance':
        _instance_ban(community, joiner)

    assert user_banned_from_community(joiner.id, community.id) is expected


def test_the_helper_sees_a_ban_the_cached_list_has_not(app, world):
    """D991's stale-cache protection, kept: a CommunityBan row the memoized
    list does not know about yet still counts."""
    community, joiner, founder = world
    _ban(community, joiner, founder)

    with patch('app.utils.communities_banned_from', return_value=[]):
        assert user_banned_from_community(joiner.id, community.id) is True


def test_join_then_add_refuses_a_user_banned_from_the_whole_instance(app, world):
    """D995, owner ruling: the route's final gate used to ask
    `Community.user_is_banned`, which read only CommunityBan rows, so an
    instance-banned user was sent on to the post form."""
    community, joiner, founder = world
    _able_to_post(joiner)
    _instance_ban(community, joiner)
    make_community_member(joiner, community)
    db.session.commit()
    client = app.test_client()
    login(client, joiner)

    with patch('app.community.routes.render_template', return_value='rendered'):
        response = client.post(url(app, 'community.join_then_add', actor=community.name),
                               data={'csrf_token': csrf(app, client)})

    assert response.status_code == 401


def test_onboarding_does_not_join_an_instance_banned_user_to_a_topic_community(app, world):
    community, joiner, founder = world
    topic = Topic(name='Books', machine_name='books')
    db.session.add(topic)
    db.session.commit()
    community.topic_id = topic.id
    db.session.commit()
    _instance_ban(community, joiner)

    with app.test_request_context('/'):
        login_user(joiner)
        joined = join_topic(topic.id)

    assert joined == 0
    assert not _is_member(community, joiner)

