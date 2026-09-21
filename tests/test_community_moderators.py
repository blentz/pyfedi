"""Who moderates a community, who owns it, and what a member can set.

Sub-project 80, slice H. Five defects, all measured:

* `get_sidebar` served ANY community's title and description to ANY caller,
  with no login and no membership check, while `show_community` -- the page
  the fragment belongs to -- refuses a banned community and a private one
  (D1017);
* `community_add_moderator` accepted **GET** and promoted a moderator, and did
  not catch the `no_permission` its own helper raises, so a non-owner got a
  500 rather than the 401 the sibling remove route answers (D1018);
* `community_my_flair` returned None for a name that does not resolve, which
  Flask reports as a 500 (D1019);
* the membership form read the viewer's flair blocks across EVERY community,
  so one community's form opened pre-checked with another's flair ids -- which
  are not among its own choices, so the page then silently refused to save
  (D1020);
* `post_sticky` and `post_vote` accepted GET and mutated (D1021). Those two
  live in the post blueprint; they are pinned here because this is the slice
  that found them, and the D989 ratchet is what carries them forward.

The D989 ratchet could not see D1018 or D1021: its detector looks for
`db.session` writes in the view body, and all three write through a helper in
`app/shared/`. It has been taught the helpers' names.
"""
from unittest.mock import patch

import pytest

from app import db
from app.constants import SRC_WEB
from app.models import (Community, CommunityFlair, CommunityFlairBlock,
                        CommunityMember, Instance, Language, Post, Site, User,
                        UserFlair)
from tests.factories import (make_community, make_community_member,
                             make_instance, make_post, make_user)

pytestmark = pytest.mark.usefixtures('site')


def instance(domain='test.piefed.local', software='piefed'):
    """Fact 394."""
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


@pytest.fixture
def env(app, db_session):
    """`owner` owns `first`; `member` merely belongs to it. `second` exists so
    that a row can ask what one community's page does with another's data."""
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    local = instance()
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1  # fact 347
    owner = make_user(local, 'owner', local=True)
    member = make_user(local, 'member', local=True)
    first = make_community('first')
    second = make_community('second')
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.commit()
    ownership = make_community_member(owner, first)
    ownership.is_moderator = True
    ownership.is_owner = True
    make_community_member(member, first)
    make_community_member(member, second)
    db.session.commit()
    client = app.test_client()
    login(client, owner)
    return client, first, second, owner, member


def as_user(app, user):
    client = app.test_client()
    login(client, user)
    return client


def flair_in(community, name):
    flair = CommunityFlair(community_id=community.id, flair=name,
                           text_color='#000000', background_color='#ffffff')
    db.session.add(flair)
    db.session.commit()
    return flair


def moderates(user, community):
    membership = CommunityMember.query.filter_by(user_id=user.id,
                                                 community_id=community.id).first()
    return membership is not None and membership.is_moderator


# --------------------------------------------------------------------------
# D1017 -- the sidebar fragment
# --------------------------------------------------------------------------


def test_the_sidebar_renders_a_public_community(app, env):
    client, first, second, owner, member = env
    first.description_html = '<p>Everyone welcome</p>'
    db.session.commit()

    response = app.test_client().get(f'/community/get_sidebar/{first.id}')

    assert response.status_code == 200
    assert b'Everyone welcome' in response.data


def test_the_sidebar_of_a_private_community_is_refused(app, env):
    """D1017. This served the title and description of any community to any
    caller, with no login and no membership check, while `show_community`
    answers 403 for exactly this. Measured, anonymously:
    `PROBE h2 description leaked: True title leaked: True`."""
    client, first, second, owner, member = env
    first.private = True
    first.title = 'Secret Community'
    first.description_html = '<p>Members only, please</p>'
    db.session.commit()

    response = app.test_client().get(f'/community/get_sidebar/{first.id}')

    assert response.status_code == 403
    assert b'Members only' not in response.data
    assert b'Secret Community' not in response.data


def test_a_member_still_sees_a_private_communitys_sidebar(app, env):
    """The other side of the same condition -- the refusal must not cost the
    members the fragment."""
    client, first, second, owner, member = env
    first.private = True
    first.description_html = '<p>Members only, please</p>'
    db.session.commit()

    response = as_user(app, member).get(f'/community/get_sidebar/{first.id}')

    assert response.status_code == 200
    assert b'Members only' in response.data


def test_the_sidebar_of_a_banned_community_is_not_found(app, env):
    client, first, second, owner, member = env
    first.banned = True
    db.session.commit()

    response = app.test_client().get(f'/community/get_sidebar/{first.id}')

    assert response.status_code == 404


def test_the_sidebar_of_an_unknown_community_is_not_found(app, env):
    """This used to render the template with `community=None`, which the
    template happened to survive -- a 200 describing nothing."""
    response = app.test_client().get('/community/get_sidebar/9999')

    assert response.status_code == 404


# --------------------------------------------------------------------------
# The moderator list
# --------------------------------------------------------------------------


def test_the_owner_sees_the_moderator_list(app, env):
    client, first, second, owner, member = env

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        response = client.get(f'/community/community/{first.id}/moderators')

    assert response.status_code == 200
    assert [m.id for m in render.call_args.kwargs['moderators']] == [owner.id]
    assert render.call_args.kwargs['is_owner'] is True


def test_an_owner_who_is_not_a_moderator_still_sees_the_list(app, env):
    """`if is_owner or current_user.is_admin() or
    community.is_moderator(current_user):`.

    The first arm cannot decide anything, and this row is what proves it
    rather than what covers it. `Community.is_moderator()` (app/models.py:736)
    does NOT read the `is_moderator` column -- it asks whether the user is in
    `moderators()`, which selects on `is_owner OR is_moderator`. So every
    owner satisfies the third arm too, with or without the column, and
    deleting `is_owner or` changes no answer. Registered as an equivalent
    mutant (D1023). The row stays because owner-without-the-column is a state
    the model expects and nothing else here builds it."""
    client, first, second, owner, member = env
    ownership = CommunityMember.query.filter_by(user_id=owner.id,
                                                community_id=first.id).first()
    ownership.is_moderator = False
    db.session.commit()

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        response = client.get(f'/community/community/{first.id}/moderators')

    assert response.status_code == 200
    assert render.call_args.kwargs['is_owner'] is True


def test_a_moderator_sees_the_list_without_being_the_owner(app, env):
    client, first, second, owner, member = env
    membership = CommunityMember.query.filter_by(user_id=member.id,
                                                 community_id=first.id).first()
    membership.is_moderator = True
    db.session.commit()

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        response = as_user(app, member).get(
            f'/community/community/{first.id}/moderators')

    assert response.status_code == 200
    assert render.call_args.kwargs['is_owner'] is False


def test_an_ordinary_member_is_refused_the_moderator_list(app, env):
    client, first, second, owner, member = env

    response = as_user(app, member).get(
        f'/community/community/{first.id}/moderators')

    assert response.status_code == 401


def test_a_banned_moderator_is_not_listed(app, env):
    """`User.banned == False` is part of the query, so an account banned from
    the instance stops appearing as a moderator."""
    client, first, second, owner, member = env
    membership = CommunityMember.query.filter_by(user_id=member.id,
                                                 community_id=first.id).first()
    membership.is_moderator = True
    member.banned = True
    db.session.commit()

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/community/community/{first.id}/moderators')

    assert member.id not in [m.id for m in render.call_args.kwargs['moderators']]


def test_an_unknown_community_has_no_moderator_list(app, env):
    client, first, second, owner, member = env

    response = client.get('/community/community/9999/moderators')

    assert response.status_code == 404


def test_a_banned_user_cannot_read_the_moderator_list(app, env):
    client, first, second, owner, member = env
    owner.banned = True
    db.session.commit()

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/community/community/{first.id}/moderators')

    assert render.call_args is None


# --------------------------------------------------------------------------
# D1018 -- adding a moderator
# --------------------------------------------------------------------------


def test_the_owner_can_add_a_moderator(app, env):
    client, first, second, owner, member = env
    token = csrf(app, client)

    response = client.post(
        f'/community/community/{first.id}/moderators/add/{member.id}',
        data={'csrf_token': token})

    assert response.status_code == 302
    assert moderates(member, first)


def test_adding_a_moderator_is_not_a_get(app, env):
    """D1021's shape, at D1018's site: `login_required` validates CSRF only
    for POST, so while this route accepted GET an owner who loaded
    `<img src=".../moderators/add/123">` promoted account 123. The D989
    ratchet could not see it -- the write is inside `add_mod_to_community`."""
    client, first, second, owner, member = env

    response = client.get(
        f'/community/community/{first.id}/moderators/add/{member.id}')

    assert response.status_code == 405
    assert not moderates(member, first)


def test_a_non_owner_cannot_add_a_moderator(app, env):
    """D1018. `add_mod_to_community` raises `Exception('no_permission')` and
    nothing caught it, so this was an unhandled exception -- a 500 where the
    sibling remove route answers 401. Measured:
    `PROBE h4 RAISED: Exception no_permission`."""
    client, first, second, owner, member = env
    outsider = make_user(instance(), 'outsider', local=True)
    db.session.commit()
    other = as_user(app, outsider)
    token = csrf(app, other)

    response = other.post(
        f'/community/community/{first.id}/moderators/add/{member.id}',
        data={'csrf_token': token})

    assert response.status_code == 401
    assert not moderates(member, first)


def test_adding_a_moderator_to_an_unknown_community_is_a_404(app, env):
    """`add_mod_to_community` reaches the database with `.one()`, which raises
    `NoResultFound` -- a different exception meaning a different answer."""
    client, first, second, owner, member = env
    token = csrf(app, client)

    response = client.post(
        f'/community/community/9999/moderators/add/{member.id}',
        data={'csrf_token': token})

    assert response.status_code == 404


def test_adding_an_unknown_user_as_a_moderator_is_a_404(app, env):
    client, first, second, owner, member = env
    token = csrf(app, client)

    response = client.post(
        f'/community/community/{first.id}/moderators/add/9999',
        data={'csrf_token': token})

    assert response.status_code == 404


def test_a_banned_user_cannot_add_a_moderator(app, env):
    client, first, second, owner, member = env
    owner.banned = True
    db.session.commit()
    token = csrf(app, client)

    client.post(f'/community/community/{first.id}/moderators/add/{member.id}',
                data={'csrf_token': token})

    assert not moderates(member, first)


def test_the_new_moderator_is_told(app, env):
    """`add_mod_to_community` raises a Notification for a local account, and
    the notification is the only thing that tells them."""
    from app.models import Notification

    client, first, second, owner, member = env
    token = csrf(app, client)

    client.post(f'/community/community/{first.id}/moderators/add/{member.id}',
                data={'csrf_token': token})

    notifications = Notification.query.filter_by(user_id=member.id).all()
    assert len(notifications) == 1
    assert 'moderator' in notifications[0].title


# --------------------------------------------------------------------------
# Removing a moderator, and ownership
# --------------------------------------------------------------------------


def test_the_owner_can_remove_a_moderator(app, env):
    client, first, second, owner, member = env
    membership = CommunityMember.query.filter_by(user_id=member.id,
                                                 community_id=first.id).first()
    membership.is_moderator = True
    db.session.commit()
    token = csrf(app, client)

    response = client.post(
        f'/community/community/{first.id}/moderators/remove/{member.id}',
        data={'csrf_token': token})

    assert response.status_code == 302
    assert not moderates(member, first)


def test_a_non_owner_cannot_remove_a_moderator(app, env):
    client, first, second, owner, member = env
    membership = CommunityMember.query.filter_by(user_id=member.id,
                                                 community_id=first.id).first()
    membership.is_moderator = True
    db.session.commit()
    outsider = make_user(instance(), 'outsider', local=True)
    db.session.commit()
    other = as_user(app, outsider)
    token = csrf(app, other)

    response = other.post(
        f'/community/community/{first.id}/moderators/remove/{member.id}',
        data={'csrf_token': token})

    assert response.status_code == 401
    assert moderates(member, first)


def test_the_last_owner_cannot_be_removed(app, env):
    """`remove_mod_from_community` refuses to leave a community with no owner.
    For a web caller it FLASHES and returns rather than raising, so the route
    still redirects -- the status code cannot tell this apart from a
    successful removal, and the message is the only evidence."""
    client, first, second, owner, member = env
    token = csrf(app, client)

    with patch('app.shared.community.flash') as flashed:
        response = client.post(
            f'/community/community/{first.id}/moderators/remove/{owner.id}',
            data={'csrf_token': token})

    assert response.status_code == 302
    assert moderates(owner, first)
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'must have one or more owners' in messages


def test_removing_someone_who_is_not_a_moderator_says_so(app, env):
    """The other flash-and-return arm of the same helper, and the same
    indistinguishable redirect."""
    client, first, second, owner, member = env
    token = csrf(app, client)

    with patch('app.shared.community.flash') as flashed:
        response = client.post(
            f'/community/community/{first.id}/moderators/remove/{member.id}',
            data={'csrf_token': token})

    assert response.status_code == 302
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'not a moderator of this community' in messages


def test_the_owner_can_promote_a_moderator_to_owner(app, env):
    client, first, second, owner, member = env
    membership = CommunityMember.query.filter_by(user_id=member.id,
                                                 community_id=first.id).first()
    membership.is_moderator = True
    db.session.commit()
    token = csrf(app, client)

    response = client.post(
        f'/community/community/{first.id}/make_owner/{member.id}',
        data={'csrf_token': token})

    assert response.status_code == 302
    db.session.refresh(membership)
    assert membership.is_owner is True


def test_someone_who_is_not_a_moderator_cannot_be_made_owner(app, env):
    """`and community.is_moderator(user)` -- ownership is promoted from the
    moderator list, not granted from nowhere."""
    client, first, second, owner, member = env
    token = csrf(app, client)

    response = client.post(
        f'/community/community/{first.id}/make_owner/{member.id}',
        data={'csrf_token': token})

    assert response.status_code == 401


def test_a_non_owner_cannot_grant_ownership(app, env):
    client, first, second, owner, member = env
    membership = CommunityMember.query.filter_by(user_id=member.id,
                                                 community_id=first.id).first()
    membership.is_moderator = True
    db.session.commit()
    other = as_user(app, member)
    token = csrf(app, other)

    response = other.post(
        f'/community/community/{first.id}/make_owner/{member.id}',
        data={'csrf_token': token})

    assert response.status_code == 401
    db.session.refresh(membership)
    assert membership.is_owner is False


# --------------------------------------------------------------------------
# Finding a moderator to add
# --------------------------------------------------------------------------


def test_the_owner_can_search_for_a_moderator(app, env):
    client, first, second, owner, member = env
    token = csrf(app, client)

    with patch('app.community.routes.find_potential_moderators',
               return_value=[member]) as find:
        with patch('app.community.routes.render_template',
                   return_value='rendered') as render:
            response = client.post(
                f'/community/community/{first.id}/moderators/find',
                data={'user_name': 'member', 'submit': 'Find',
                      'csrf_token': token})

    assert response.status_code == 200
    assert find.call_args.args == ('member',)
    assert render.call_args.kwargs['potential_moderators'] == [member]


def test_the_search_page_opens_empty(app, env):
    """`potential_moderators = None` until the form is submitted, so the page
    does not open with a list of everybody."""
    client, first, second, owner, member = env

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/community/community/{first.id}/moderators/find')

    assert render.call_args.kwargs['potential_moderators'] is None


def test_a_non_owner_cannot_search_for_a_moderator(app, env):
    """The search calls `find_potential_moderators`, which is a lookup over
    accounts; the refusal has to be above it."""
    client, first, second, owner, member = env
    other = as_user(app, member)

    response = other.get(f'/community/community/{first.id}/moderators/find')

    assert response.status_code == 401


def test_an_unknown_community_has_no_moderator_search(app, env):
    client, first, second, owner, member = env

    response = client.get('/community/community/9999/moderators/find')

    assert response.status_code == 404


# --------------------------------------------------------------------------
# Kicking a member
# --------------------------------------------------------------------------


def test_an_admin_can_kick_a_member(app, env):
    client, first, second, owner, member = env
    admin = as_user(app, db.session.get(User, 1))
    token = csrf(app, admin)

    response = admin.post(f'/community/community/{first.id}/{member.id}/kick_user_community',
                          data={'csrf_token': token})

    assert response.status_code == 302
    assert CommunityMember.query.filter_by(user_id=member.id,
                                           community_id=first.id).first() is None


def test_an_owner_cannot_kick_a_member(app, env):
    """This route asks for `current_user.is_admin()` and nothing else -- not
    the owner, not a moderator. Pinned because it is surprising, not because
    it is wrong: kicking removes a membership the user chose, which is a
    different act from banning them."""
    client, first, second, owner, member = env
    token = csrf(app, client)

    response = client.post(f'/community/community/{first.id}/{member.id}/kick_user_community',
                           data={'csrf_token': token})

    assert response.status_code == 401
    assert CommunityMember.query.filter_by(user_id=member.id,
                                           community_id=first.id).first() is not None


def test_kicking_from_an_unknown_community_is_a_404(app, env):
    client, first, second, owner, member = env
    admin = as_user(app, db.session.get(User, 1))
    token = csrf(app, admin)

    response = admin.post(f'/community/community/9999/{member.id}/kick_user_community',
                          data={'csrf_token': token})

    assert response.status_code == 404


def test_kicking_an_unknown_user_is_a_404(app, env):
    client, first, second, owner, member = env
    admin = as_user(app, db.session.get(User, 1))
    token = csrf(app, admin)

    response = admin.post(f'/community/community/{first.id}/9999/kick_user_community',
                          data={'csrf_token': token})

    assert response.status_code == 404


# --------------------------------------------------------------------------
# D1020 -- the membership form
# --------------------------------------------------------------------------


def test_the_membership_form_offers_this_communitys_flair(app, env):
    client, first, second, owner, member = env
    mine = flair_in(first, 'Question')
    flair_in(second, 'Meta')

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        response = as_user(app, member).get(
            f'/community/{first.id}/membership')

    assert response.status_code == 200
    assert render.call_args.kwargs['form'].block_flair.choices == [(mine.id,
                                                                   'Question')]


def test_the_membership_form_shows_only_this_communitys_blocks(app, env):
    """D1020. The pre-fill read the viewer's blocks across EVERY community, so
    this form opened pre-checked with another community's flair id -- which is
    not among its own choices, so WTForms then refused the submission and the
    page silently would not save. Measured: `PROBE h1 second community form
    pre-checked with: [1] (its own flair is 2 ...)`."""
    client, first, second, owner, member = env
    elsewhere = flair_in(second, 'Meta')
    flair_in(first, 'Question')
    db.session.add(CommunityFlairBlock(user_id=member.id,
                                       community_id=second.id,
                                       community_flair_id=elsewhere.id))
    db.session.commit()

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        as_user(app, member).get(f'/community/{first.id}/membership')

    assert render.call_args.kwargs['form'].block_flair.data == []


def test_the_membership_form_shows_the_blocks_that_are_this_communitys(app,
                                                                        env):
    """The other side of the same query."""
    client, first, second, owner, member = env
    mine = flair_in(first, 'Question')
    db.session.add(CommunityFlairBlock(user_id=member.id,
                                       community_id=first.id,
                                       community_flair_id=mine.id))
    db.session.commit()

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        as_user(app, member).get(f'/community/{first.id}/membership')

    assert render.call_args.kwargs['form'].block_flair.data == [mine.id]


def test_saving_the_membership_form_replaces_this_communitys_blocks(app, env):
    client, first, second, owner, member = env
    mine = flair_in(first, 'Question')
    other = flair_in(first, 'Discussion')
    db.session.add(CommunityFlairBlock(user_id=member.id,
                                       community_id=first.id,
                                       community_flair_id=mine.id))
    db.session.commit()
    client = as_user(app, member)
    token = csrf(app, client)

    response = client.post(f'/community/{first.id}/membership',
                           data={'block_flair': [str(other.id)],
                                 'submit': 'Save', 'csrf_token': token})

    assert response.status_code == 302
    blocks = CommunityFlairBlock.query.filter_by(user_id=member.id).all()
    assert [b.community_flair_id for b in blocks] == [other.id]


def test_saving_does_not_touch_another_communitys_blocks(app, env):
    """The delete is scoped by community, and has to stay that way: a block in
    one community is not a statement about another."""
    client, first, second, owner, member = env
    flair_in(first, 'Question')
    elsewhere = flair_in(second, 'Meta')
    db.session.add(CommunityFlairBlock(user_id=member.id,
                                       community_id=second.id,
                                       community_flair_id=elsewhere.id))
    db.session.commit()
    client = as_user(app, member)
    token = csrf(app, client)

    client.post(f'/community/{first.id}/membership',
                data={'submit': 'Save', 'csrf_token': token})

    remaining = CommunityFlairBlock.query.filter_by(user_id=member.id).all()
    assert [(b.community_id, b.community_flair_id) for b in remaining] == [
        (second.id, elsewhere.id)]


def test_the_membership_page_of_an_unknown_community_is_a_404(app, env):
    client, first, second, owner, member = env

    response = client.get('/community/9999/membership')

    assert response.status_code == 404


# --------------------------------------------------------------------------
# D1019 -- a member's own flair
# --------------------------------------------------------------------------


def test_a_member_can_set_their_flair(app, env):
    client, first, second, owner, member = env
    client = as_user(app, member)
    token = csrf(app, client)

    response = client.post(f'/community/{first.name}/my_flair',
                           data={'my_flair': 'Regular', 'submit': 'Save',
                                 'csrf_token': token})

    assert response.status_code == 302
    flair = UserFlair.query.filter_by(user_id=member.id,
                                      community_id=first.id).one()
    assert flair.flair == 'Regular'


def test_setting_flair_again_replaces_it(app, env):
    client, first, second, owner, member = env
    db.session.add(UserFlair(user_id=member.id, community_id=first.id,
                             flair='Old'))
    db.session.commit()
    client = as_user(app, member)
    token = csrf(app, client)

    client.post(f'/community/{first.name}/my_flair',
                data={'my_flair': 'New', 'submit': 'Save',
                      'csrf_token': token})

    flair = UserFlair.query.filter_by(user_id=member.id,
                                      community_id=first.id).one()
    assert flair.flair == 'New'


def test_clearing_flair_deletes_the_row(app, env):
    """An empty submission is a deletion, not a blank flair -- otherwise the
    template renders an empty badge next to the name."""
    client, first, second, owner, member = env
    db.session.add(UserFlair(user_id=member.id, community_id=first.id,
                             flair='Old'))
    db.session.commit()
    client = as_user(app, member)
    token = csrf(app, client)

    client.post(f'/community/{first.name}/my_flair',
                data={'my_flair': '   ', 'submit': 'Save',
                      'csrf_token': token})

    assert UserFlair.query.filter_by(user_id=member.id,
                                     community_id=first.id).first() is None


def test_the_flair_form_opens_on_the_stored_value(app, env):
    client, first, second, owner, member = env
    db.session.add(UserFlair(user_id=member.id, community_id=first.id,
                             flair='Regular'))
    db.session.commit()

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        as_user(app, member).get(f'/community/{first.name}/my_flair')

    assert render.call_args.kwargs['form'].my_flair.data == 'Regular'


def test_the_flair_form_opens_empty_for_someone_with_none(app, env):
    client, first, second, owner, member = env

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        as_user(app, member).get(f'/community/{first.name}/my_flair')

    assert render.call_args.kwargs['form'].my_flair.data is None


def test_flair_in_an_unknown_community_is_a_404(app, env):
    """D1019. The whole body sat inside `if community is not None:` with no
    else, so the view returned None and Flask answered `TypeError: The view
    function ... did not return a valid response`. Measured."""
    client, first, second, owner, member = env

    response = client.get('/community/nonexistent/my_flair')

    assert response.status_code == 404


# --------------------------------------------------------------------------
# D1021 -- two routes in the post blueprint
# --------------------------------------------------------------------------


@pytest.fixture
def a_post(app, env):
    client, first, second, owner, member = env
    post = make_post(first, owner, 'https://test.piefed.local/p/1',
                     title='a post')
    db.session.commit()
    return post


def test_stickying_a_post_is_not_a_get(app, env, a_post):
    """D1021. `login_required` validates CSRF only for POST, so while this
    accepted GET a moderator who loaded `<img src="/post/1/sticky/yes">`
    stickied that post. D987 fixed the instance-wide twin and this one was
    left, because the ratchet's detector could not see a write made inside
    `sticky_post()`."""
    client, first, second, owner, member = env

    response = client.get(f'/post/{a_post.id}/sticky/yes')

    assert response.status_code == 405
    db.session.refresh(a_post)
    assert a_post.sticky is False


def test_a_moderator_can_still_sticky_a_post(app, env, a_post):
    client, first, second, owner, member = env
    token = csrf(app, client)

    with patch('app.post.routes.sticky_post') as sticky:
        response = client.post(f'/post/{a_post.id}/sticky/yes',
                               data={'csrf_token': token})

    assert response.status_code == 302
    assert sticky.call_args.args == (a_post.id, True, SRC_WEB)


def test_voting_is_not_a_get(app, env, a_post):
    """D1021's second site, and the one with the largest reach: an
    `<img src="/post/1/upvote/default">` cast the viewer's vote on any page an
    attacker could get them to load. The site's own vote buttons are hx-post
    with no anchor fallback."""
    client, first, second, owner, member = env

    response = as_user(app, member).get(f'/post/{a_post.id}/upvote/default')

    assert response.status_code == 405


def test_voting_by_post_still_works(app, env, a_post):
    client, first, second, owner, member = env
    # `validation_required` and `approval_required` sit above this route, so
    # an unverified account is redirected before it runs -- fact 355's
    # neighbour, and the same trap as slice C's `able_to_create`.
    member.verified = True
    member.private_key = 'a private key'
    db.session.commit()
    voter = as_user(app, member)
    token = csrf(app, voter)

    with patch('app.post.routes.vote_for_post',
               return_value='voted') as vote:
        response = voter.post(f'/post/{a_post.id}/upvote/default',
                              data={'csrf_token': token})

    assert response.status_code == 200
    assert vote.call_args.args[0] == a_post.id
    assert vote.call_args.args[1] == 'upvote'


# --------------------------------------------------------------------------
# Standing down as owner
# --------------------------------------------------------------------------


def owner_of(user, community):
    membership = CommunityMember.query.filter_by(user_id=user.id,
                                                 community_id=community.id).first()
    return membership is not None and membership.is_owner


def make_owner(user, community):
    membership = CommunityMember.query.filter_by(user_id=user.id,
                                                 community_id=community.id).first()
    membership.is_moderator = True
    membership.is_owner = True
    db.session.commit()
    return membership


def test_an_owner_can_stand_down_when_someone_else_owns_it_too(app, env):
    """`(community.is_owner() and user.id == current_user.id)` -- the third
    arm of the condition, and the only one that lets an owner act on
    themselves."""
    client, first, second, owner, member = env
    make_owner(member, first)
    token = csrf(app, client)

    response = client.post(
        f'/community/community/{first.id}/remove_owner/{owner.id}',
        data={'csrf_token': token})

    assert response.status_code == 302
    assert not owner_of(owner, first)
    assert owner_of(member, first)


def test_the_last_owner_cannot_stand_down(app, env):
    """`if community.num_owners() == 1:` -- a community with no owner has
    nobody who can appoint one."""
    client, first, second, owner, member = env
    token = csrf(app, client)

    with patch('app.community.routes.flash') as flashed:
        response = client.post(
            f'/community/community/{first.id}/remove_owner/{owner.id}',
            data={'csrf_token': token})

    assert response.status_code == 302
    assert owner_of(owner, first)
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'must have one or more owners' in messages


def test_an_owner_can_remove_another_owner(app, env):
    """The second arm requires the target to be a moderator and NOT an owner,
    so an owner removing a co-owner goes through the first arm only if they
    are also staff. Pinned as it stands: an ordinary owner cannot remove a
    co-owner, only themselves."""
    client, first, second, owner, member = env
    make_owner(member, first)
    token = csrf(app, client)

    response = client.post(
        f'/community/community/{first.id}/remove_owner/{member.id}',
        data={'csrf_token': token})

    assert response.status_code == 401
    assert owner_of(member, first)


def test_an_admin_can_remove_an_owner(app, env):
    """The first arm: `current_user.is_admin_or_staff() and
    community.is_owner(user)`."""
    client, first, second, owner, member = env
    make_owner(member, first)
    admin = as_user(app, db.session.get(User, 1))
    token = csrf(app, admin)

    response = admin.post(
        f'/community/community/{first.id}/remove_owner/{member.id}',
        data={'csrf_token': token})

    assert response.status_code == 302
    assert not owner_of(member, first)


def test_a_stranger_cannot_remove_an_owner(app, env):
    client, first, second, owner, member = env
    outsider = make_user(instance(), 'outsider', local=True)
    db.session.commit()
    other = as_user(app, outsider)
    token = csrf(app, other)

    response = other.post(
        f'/community/community/{first.id}/remove_owner/{owner.id}',
        data={'csrf_token': token})

    assert response.status_code == 401
    assert owner_of(owner, first)


def test_removing_the_owner_of_an_unknown_community_is_a_404(app, env):
    client, first, second, owner, member = env
    token = csrf(app, client)

    response = client.post('/community/community/9999/remove_owner/1',
                           data={'csrf_token': token})

    assert response.status_code == 404


def test_removing_an_unknown_owner_is_a_404(app, env):
    client, first, second, owner, member = env
    token = csrf(app, client)

    response = client.post(
        f'/community/community/{first.id}/remove_owner/9999',
        data={'csrf_token': token})

    assert response.status_code == 404


def test_a_banned_user_cannot_grant_ownership(app, env):
    """Every route in this group opens with the same two lines, and each one
    needs its own row -- a shared opening is not a shared test.

    The row has to start from a state the request would CHANGE: `member` is a
    moderator and not an owner, so a ban check that does not fire makes them
    one.
    """
    client, first, second, owner, member = env
    membership = CommunityMember.query.filter_by(user_id=member.id,
                                                 community_id=first.id).first()
    membership.is_moderator = True
    owner.banned = True
    db.session.commit()
    token = csrf(app, client)

    client.post(f'/community/community/{first.id}/make_owner/{member.id}',
                data={'csrf_token': token})

    assert not owner_of(member, first)


def test_a_banned_owner_cannot_stand_down(app, env):
    """The same for `remove_owner`, and the arm it has to use is the one an
    owner can reach on their own account -- `user.id == current_user.id`.
    Aiming the request at somebody else is refused by the authorization check
    whether or not the ban check runs, so such a row cannot see the ban check
    at all."""
    client, first, second, owner, member = env
    make_owner(member, first)  # a second owner, so standing down is allowed
    owner.banned = True
    db.session.commit()
    token = csrf(app, client)

    client.post(f'/community/community/{first.id}/remove_owner/{owner.id}',
                data={'csrf_token': token})

    assert owner_of(owner, first)


def test_a_banned_user_cannot_search_for_a_moderator(app, env):
    client, first, second, owner, member = env
    owner.banned = True
    db.session.commit()

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/community/community/{first.id}/moderators/find')

    assert render.call_args is None


def test_a_banned_user_cannot_remove_a_moderator(app, env):
    client, first, second, owner, member = env
    membership = CommunityMember.query.filter_by(user_id=member.id,
                                                 community_id=first.id).first()
    membership.is_moderator = True
    owner.banned = True
    db.session.commit()
    token = csrf(app, client)

    client.post(
        f'/community/community/{first.id}/moderators/remove/{member.id}',
        data={'csrf_token': token})

    assert moderates(member, first)
