"""The user API's actions: blocking, following, noting, flairing, banning,
verifying credentials and logging out.

Sub-project 84, slice C -- the action half of `app/api/alpha/utils/user.py`.
Five defects, all measured:

* `post_user_set_flair` and `post_user_set_note` carried the caller's id
  straight into a foreign key: `ForeignKeyViolation` for a community or a
  person that does not exist (D1175, D1176);
* `post_user_ban` and its twin let `ban_user` read `.banned` off a
  `db.session.get` that answers None (D1177);
* `post_user_verify_credentials` answered 200 for a BANNED account, which
  the API's own login refuses -- and the pair of answers distinguished a
  banned account from a wrong password (D1179);
* `/user/register` and `/user/get_captcha` were registered with a second
  `/api/alpha` in front of them, so the documented paths were 404 (D1180),
  and both utils were `...` stubs whose route then loads a schema from None
  (D1181).

Registered and pinned: an account with `ban users` may ban an administrator,
including the founder (D1178); and a token carrying no `jti` cannot be
revoked, while logout answers success (D1182).
"""
import jwt
import pytest
from flask import current_app, g
from unittest.mock import patch

from app import db
from app.models import (Role, RolePermission, RevokedToken, Site, User,
                        UserBlock, UserFlair, UserNote, user_role)
from tests.factories import make_community, make_user


def token(user):
    return f'Bearer {user.encode_jwt_token()}'


def give(user, permission):
    """`user_access(permission, id)` reads role_permission joined to the
    `user_role` association table; there is no UserRole model."""
    role = Role(name=f'role-{user.id}-{permission}', weight=10)
    db.session.add(role)
    db.session.commit()
    db.session.add(RolePermission(role_id=role.id, permission=permission))
    db.session.execute(user_role.insert().values(user_id=user.id,
                                                 role_id=role.id))
    db.session.commit()
    return user


@pytest.fixture
def env(app, api_baseline):
    """`actor` acts; `target` is acted upon. Neither is user 1, whose
    `user_access` answers True for every permission (fact 549)."""
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    actor = api_baseline.user2
    target = api_baseline.user3
    return actor, target


# --------------------------------------------------------------------------
# D1175, D1176 -- ids that go straight into a foreign key
# --------------------------------------------------------------------------


def test_flair_is_set_for_a_community(app, env):
    from app.api.alpha.utils.user import post_user_set_flair

    actor, target = env
    community = make_community('general')
    db.session.commit()

    post_user_set_flair(token(actor), {'community_id': community.id,
                                       'flair_text': 'regular'})

    assert UserFlair.query.filter_by(user_id=actor.id,
                                     community_id=community.id).one().flair \
        == 'regular'


def test_flair_is_replaced_when_it_is_set_again(app, env):
    from app.api.alpha.utils.user import post_user_set_flair

    actor, target = env
    community = make_community('general')
    db.session.commit()
    post_user_set_flair(token(actor), {'community_id': community.id,
                                       'flair_text': 'regular'})

    post_user_set_flair(token(actor), {'community_id': community.id,
                                       'flair_text': 'veteran'})

    assert UserFlair.query.filter_by(user_id=actor.id,
                                     community_id=community.id).one().flair \
        == 'veteran'


def test_flair_is_removed_when_none_is_sent(app, env):
    from app.api.alpha.utils.user import post_user_set_flair

    actor, target = env
    community = make_community('general')
    db.session.commit()
    post_user_set_flair(token(actor), {'community_id': community.id,
                                       'flair_text': 'regular'})

    post_user_set_flair(token(actor), {'community_id': community.id})

    assert UserFlair.query.filter_by(user_id=actor.id,
                                     community_id=community.id).first() is None


def test_removing_flair_nobody_set_is_not_an_error(app, env):
    from app.api.alpha.utils.user import post_user_set_flair

    actor, target = env
    community = make_community('general')
    db.session.commit()

    post_user_set_flair(token(actor), {'community_id': community.id})

    assert UserFlair.query.count() == 0


def test_flair_longer_than_the_column_is_refused(app, env):
    from app.api.alpha.utils.user import post_user_set_flair

    actor, target = env
    community = make_community('general')
    db.session.commit()

    with pytest.raises(Exception) as refused:
        post_user_set_flair(token(actor), {'community_id': community.id,
                                           'flair_text': 'x' * 51})

    # The exact message: the column is varchar(50), so Postgres refuses an
    # over-long flair too, with "value too long for type character
    # varying(50)" -- and a row matching only on "too long" passes with the
    # guard removed (fact 553).
    assert str(refused.value) == 'Flair text is too long (50 chars max)'
    assert UserFlair.query.count() == 0


def test_flair_for_a_community_that_does_not_exist(app, env):
    """D1175. `community_id` is whatever the caller sent, and it went into a
    foreign key. Measured: `PROBE bf1 outcome: IntegrityError:
    (psycopg2.errors.ForeignKeyViolation) ... user_flair_community_id_fkey`."""
    from app.api.alpha.utils.user import post_user_set_flair

    actor, target = env

    with pytest.raises(Exception) as refused:
        post_user_set_flair(token(actor), {'community_id': 999999,
                                           'flair_text': 'hello'})

    assert str(refused.value) == 'community not found'


def test_a_note_is_written_about_somebody(app, env):
    from app.api.alpha.utils.user import post_user_set_note

    actor, target = env

    post_user_set_note(token(actor), {'person_id': target.id,
                                      'note': '  a reminder  '})

    assert UserNote.query.filter_by(user_id=actor.id,
                                    target_id=target.id).one().body == \
        'a reminder'


def test_a_note_is_replaced_when_it_is_written_again(app, env):
    from app.api.alpha.utils.user import post_user_set_note

    actor, target = env
    post_user_set_note(token(actor), {'person_id': target.id, 'note': 'first'})

    post_user_set_note(token(actor), {'person_id': target.id, 'note': 'second'})

    assert UserNote.query.filter_by(user_id=actor.id,
                                    target_id=target.id).one().body == 'second'


def test_a_note_is_removed_when_none_is_sent(app, env):
    from app.api.alpha.utils.user import post_user_set_note

    actor, target = env
    post_user_set_note(token(actor), {'person_id': target.id, 'note': 'first'})

    post_user_set_note(token(actor), {'person_id': target.id})

    assert UserNote.query.count() == 0


def test_removing_a_note_nobody_wrote_is_not_an_error(app, env):
    from app.api.alpha.utils.user import post_user_set_note

    actor, target = env

    post_user_set_note(token(actor), {'person_id': target.id})

    assert UserNote.query.count() == 0


def test_a_note_about_somebody_who_does_not_exist(app, env):
    """D1176. `user_note.target_id` is a foreign key. Measured: `PROBE bf3
    outcome: IntegrityError: (psycopg2.errors.ForeignKeyViolation) ...
    user_note_target_id_fkey`."""
    from app.api.alpha.utils.user import post_user_set_note

    actor, target = env

    with pytest.raises(Exception) as refused:
        post_user_set_note(token(actor), {'person_id': 999999, 'note': 'x'})

    assert str(refused.value) == 'person not found'


# --------------------------------------------------------------------------
# D1177, D1178 -- banning
# --------------------------------------------------------------------------


def test_somebody_with_the_permission_bans_an_account(app, env):
    from app.api.alpha.utils.user import post_user_ban

    actor, target = env
    give(actor, 'ban users')

    post_user_ban(token(actor), {'person_id': target.id, 'reason': 'spam'})

    assert target.banned is True


def test_manage_users_also_allows_a_ban(app, env):
    """Either permission: `ban users` or `manage users`."""
    from app.api.alpha.utils.user import post_user_ban

    actor, target = env
    give(actor, 'manage users')

    post_user_ban(token(actor), {'person_id': target.id, 'reason': 'spam'})

    assert target.banned is True


def test_somebody_without_the_permission_may_not_ban(app, env):
    from app.api.alpha.utils.user import post_user_ban

    actor, target = env

    with pytest.raises(Exception):
        post_user_ban(token(actor), {'person_id': target.id, 'reason': 'x'})

    assert target.banned is False


def test_nobody_bans_themselves(app, env):
    from app.api.alpha.utils.user import post_user_ban

    actor, target = env
    give(actor, 'ban users')

    with pytest.raises(Exception) as refused:
        post_user_ban(token(actor), {'person_id': actor.id, 'reason': 'x'})

    assert str(refused.value) == 'cannot_ban_self'
    assert actor.banned is False


def test_banning_somebody_who_does_not_exist(app, env):
    """D1177. `ban_user` does `db.session.get(User, person_id)` and then
    `to_ban.banned = True`. Measured: `PROBE bf5 outcome: AttributeError:
    'NoneType' object has no attribute 'banned'`."""
    from app.api.alpha.utils.user import post_user_ban

    actor, target = env
    give(actor, 'ban users')

    with pytest.raises(Exception) as refused:
        post_user_ban(token(actor), {'person_id': 999999, 'reason': 'x'})

    assert str(refused.value) == 'person not found'


def test_an_account_is_unbanned(app, env):
    from app.api.alpha.utils.user import post_user_ban, post_user_unban

    actor, target = env
    give(actor, 'ban users')
    post_user_ban(token(actor), {'person_id': target.id, 'reason': 'spam'})

    post_user_unban(token(actor), {'person_id': target.id})

    assert target.banned is False


def test_somebody_without_the_permission_may_not_unban(app, env):
    from app.api.alpha.utils.user import post_user_unban

    actor, target = env
    target.banned = True
    db.session.commit()

    with pytest.raises(Exception):
        post_user_unban(token(actor), {'person_id': target.id})

    assert target.banned is True


def test_nobody_unbans_themselves(app, env):
    from app.api.alpha.utils.user import post_user_unban

    actor, target = env
    give(actor, 'ban users')

    with pytest.raises(Exception) as refused:
        post_user_unban(token(actor), {'person_id': actor.id})

    assert str(refused.value) == 'cannot_unban_self'


def test_unbanning_somebody_who_does_not_exist(app, env):
    """D1177's twin."""
    from app.api.alpha.utils.user import post_user_unban

    actor, target = env
    give(actor, 'ban users')

    with pytest.raises(Exception) as refused:
        post_user_unban(token(actor), {'person_id': 999999})

    assert str(refused.value) == 'person not found'


def test_an_administrator_can_be_banned_by_staff(app, env):
    """D1178, PINNED as it stands. Nothing here or in `ban_user` protects an
    account that administers the instance -- including user 1, whose
    `user_access` answers True for everything but whose `banned` column still
    stops them logging in. Measured: `PROBE bf4 outcome: accepted | admin
    banned: True`.

    Whether staff may ban an admin is a product decision: an instance may
    genuinely need one admin removed by another, and a rule written here
    would also have to say what happens to the founder. Update this test when
    that decision is made (D1178).
    """
    from app.api.alpha.utils.user import post_user_ban

    actor, target = env
    give(actor, 'ban users')
    give(target, 'administer all users')

    post_user_ban(token(actor), {'person_id': target.id, 'reason': 'because'})

    assert target.banned is True


# --------------------------------------------------------------------------
# D1179 -- verifying credentials
# --------------------------------------------------------------------------


def test_good_credentials_verify(app, env):
    from app.api.alpha.utils.user import post_user_verify_credentials

    actor, target = env
    target.set_password('a-good-password')
    db.session.commit()

    assert post_user_verify_credentials({'username': target.user_name,
                                         'password': 'a-good-password'}) == {}


def test_an_email_address_verifies_too(app, env):
    from app.api.alpha.utils.user import post_user_verify_credentials

    actor, target = env
    target.email = 'target@example.com'
    target.set_password('a-good-password')
    db.session.commit()

    assert post_user_verify_credentials({'username': 'Target@Example.com',
                                         'password': 'a-good-password'}) == {}


def test_a_wrong_password_does_not_verify(app, env):
    from app.api.alpha.utils.user import post_user_verify_credentials

    actor, target = env
    target.set_password('a-good-password')
    db.session.commit()

    with pytest.raises(BlockingIOError):
        post_user_verify_credentials({'username': target.user_name,
                                      'password': 'the-wrong-password'})


def test_a_name_nobody_holds_does_not_verify(app, env):
    from app.api.alpha.utils.user import post_user_verify_credentials

    actor, target = env

    with pytest.raises(BlockingIOError):
        post_user_verify_credentials({'username': 'nobody',
                                      'password': 'a-good-password'})


def test_a_banned_account_does_not_verify(app, env):
    """D1179. The API's own login refuses a banned account with
    `incorrect_login`; this endpoint answered 200 for one, so the pair of
    answers told a caller which accounts are banned. Measured: `PROBE bf6
    outcome: accepted`."""
    from app.api.alpha.utils.user import post_user_verify_credentials

    actor, target = env
    target.set_password('a-good-password')
    target.banned = True
    db.session.commit()

    with pytest.raises(BlockingIOError):
        post_user_verify_credentials({'username': target.user_name,
                                      'password': 'a-good-password'})


def test_the_founder_still_verifies_when_banned(app, env):
    """`user.id != 1`, the same carve-out the login makes: whoever set the
    instance up keeps a way back in."""
    from app.api.alpha.utils.user import post_user_verify_credentials

    actor, target = env
    founder = db.session.get(User, 1)
    founder.set_password('a-good-password')
    founder.banned = True
    db.session.commit()

    assert post_user_verify_credentials({'username': founder.user_name,
                                         'password': 'a-good-password'}) == {}


# --------------------------------------------------------------------------
# Blocking, following, subscribing
# --------------------------------------------------------------------------


def test_an_account_is_blocked_and_unblocked(app, env):
    from app.api.alpha.utils.user import post_user_block

    actor, target = env

    blocked = post_user_block(token(actor), {'person_id': target.id,
                                             'block': True})
    assert blocked['person_view']['person']['id'] == target.id
    assert UserBlock.query.filter_by(blocker_id=actor.id,
                                     blocked_id=target.id).first() is not None

    post_user_block(token(actor), {'person_id': target.id, 'block': False})
    assert UserBlock.query.filter_by(blocker_id=actor.id,
                                     blocked_id=target.id).first() is None


def test_an_account_is_subscribed_to_and_away_from(app, env):
    from app.api.alpha.utils.user import put_user_subscribe

    actor, target = env

    subscribed = put_user_subscribe(token(actor), {'person_id': target.id,
                                                   'subscribe': True})
    assert subscribed['subscribed'] is True

    unsubscribed = put_user_subscribe(token(actor), {'person_id': target.id,
                                                     'subscribe': False})
    assert unsubscribed['subscribed'] is False


def test_an_account_is_followed_and_unfollowed(app, env):
    from app.api.alpha.utils.user import post_user_follow, post_user_unfollow

    actor, target = env

    assert post_user_follow(token(actor), {'user_id': target.id}) == \
        {'ok': 'ok'}
    assert post_user_unfollow(token(actor), {'user_id': target.id}) == \
        {'ok': 'ok'}


# --------------------------------------------------------------------------
# D1182 -- logging out
# --------------------------------------------------------------------------


def test_logging_out_revokes_the_token(app, env):
    from app.api.alpha.utils.user import post_user_logout

    actor, target = env
    auth = token(actor)

    assert post_user_logout(auth) == {'success': True}

    jti = jwt.decode(auth[7:], current_app.config['SECRET_KEY'],
                     algorithms=['HS256'])['jti']
    assert RevokedToken.query.filter_by(jti=jti).first() is not None


def test_logging_out_twice_revokes_it_once(app, env):
    from app.api.alpha.utils.user import post_user_logout

    actor, target = env
    auth = token(actor)

    post_user_logout(auth)
    post_user_logout(auth)

    assert RevokedToken.query.count() == 1


@pytest.mark.parametrize('auth', [None, '', 'not-a-bearer-token',
                                  'Bearer not-a-jwt'])
def test_logging_out_without_a_usable_token(app, env, auth):
    from app.api.alpha.utils.user import post_user_logout

    actor, target = env

    with pytest.raises(Exception) as refused:
        post_user_logout(auth)

    assert str(refused.value) == 'incorrect_login'


def test_a_token_with_no_jti_cannot_be_revoked(app, env):
    """D1182, PINNED as it stands. `encode_jwt_token` always mints a `jti`,
    so this is only reachable for a token made elsewhere -- but the endpoint
    answers success while revoking nothing, which is a claim it cannot keep.
    Measured: `PROBE bf7 answer: {'success': True} | revoked rows: 0`.
    Update this test if the endpoint is made to refuse (D1182).
    """
    from app.api.alpha.utils.user import post_user_logout

    actor, target = env
    without_jti = jwt.encode({'sub': str(actor.id), 'iss': 'test'},
                             current_app.config['SECRET_KEY'],
                             algorithm='HS256')

    assert post_user_logout(f'Bearer {without_jti}') == {'success': True}
    assert RevokedToken.query.count() == 0


# --------------------------------------------------------------------------
# D1180, D1181 -- two endpoints nobody could reach
# --------------------------------------------------------------------------


@pytest.mark.parametrize('path,method', [('/api/alpha/user/register', 'post'),
                                         ('/api/alpha/user/get_captcha', 'get')])
def test_the_documented_path_resolves(app, env, path, method):
    """D1180. Both routes carried a second `/api/alpha` in front of them,
    while every other route in the file is registered relative to the
    blueprint's prefix -- so the documented paths were 404 and the reachable
    ones were `/api/alpha/api/alpha/...`. Measured: `PROBE bf8
    /api/alpha/user/register: 404`."""
    actor, target = env
    client = app.test_client()

    with patch('app.api.alpha.routes.enable_api', return_value=True):
        response = getattr(client, method)(path, json={})

    assert response.status_code != 404


@pytest.mark.parametrize('path,method',
                         [('/api/alpha/api/alpha/user/register', 'post'),
                          ('/api/alpha/api/alpha/user/get_captcha', 'get')])
def test_the_doubled_path_is_gone(app, env, path, method):
    actor, target = env
    client = app.test_client()

    with patch('app.api.alpha.routes.enable_api', return_value=True):
        response = getattr(client, method)(path, json={})

    assert response.status_code == 404


def test_registering_through_the_api_says_it_is_not_written(app, env):
    """D1181. Both utils were `...`, and the route loads a response schema
    from what they return -- `Schema().load(None)`."""
    from app.api.alpha.utils.user import get_user_captcha, post_user_register

    actor, target = env

    with pytest.raises(Exception) as refused:
        post_user_register({})
    assert str(refused.value) == 'not implemented'

    with pytest.raises(Exception) as refused:
        get_user_captcha()
    assert str(refused.value) == 'not implemented'


def test_a_note_is_mine_alone(app, env):
    """Two people may hold notes about the same person, and editing one must
    not reach the other's."""
    from app.api.alpha.utils.user import post_user_set_note

    actor, target = env
    somebody_else = db.session.get(User, 1)
    post_user_set_note(token(somebody_else), {'person_id': target.id,
                                              'note': 'their note'})

    post_user_set_note(token(actor), {'person_id': target.id,
                                      'note': 'my note'})

    assert UserNote.query.filter_by(user_id=somebody_else.id,
                                    target_id=target.id).one().body == \
        'their note'
    assert UserNote.query.filter_by(user_id=actor.id,
                                    target_id=target.id).one().body == 'my note'
