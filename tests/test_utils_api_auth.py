"""Covers app/utils.py's authorise_api_user: the API authentication gate, with
138 call sites. Every rejection path matters -- a missed one means a request
that should be refused gets served.

Guard order, as read from app/utils.py (~3345-3406):

    not auth                                                   -> raise
    not auth.startswith('Bearer ')                              -> raise
    jwt.decode(...) raises InvalidTokenError                     -> raise
    RevokedToken.query.filter_by(jti=decoded['jti']).first()    -> raise
    User.query.get(decoded['sub']) is None                      -> raise
    user.ap_id is not None or user.verified is False or          -> raise
        user.banned is True or user.deleted is True
    user.password_updated_at and iat < password_updated_time    -> raise
    id_match and user.id != id_match                            -> raise
    otherwise: return_type == 'model'  -> the User
               return_type == 'dict'   -> a dict built from six queries
               otherwise               -> user.id

Coverage: every branch above is exercised except one -- the `if decoded:` false
side (app/utils.py:3389). It is not pragma'd, and it is left undocumented
nowhere else: `jwt.decode` *raises* rather than returning something falsy for
every malformed, expired, or tampered token, so in practice `decoded` is always
truthy whenever `jwt.decode` returns at all, and no test here can drive the
false side through a hostile token. It is not, however, strictly impossible --
`jwt.decode` of a token whose payload is the empty object returns `{}`, which
IS falsy, and the function would then fall through to `return None` implicitly
rather than raising. Minting such a token requires this server's own
`SECRET_KEY`, which is not attacker-reachable, so the branch is untested by
design rather than by oversight. A caller that received `None` back from
`authorise_api_user` instead of an exception would fail far from this cause.

Every rejection raises a bare `Exception('incorrect_login')` -- `pytest.raises`
alone would match almost anything, so every test here also asserts on the
message, via `match=` where any 'incorrect_login' will do and via
`str(exc_info.value) == 'incorrect_login'` where the message must be that
string EXACTLY. The exact form matters for the jwt.decode failures:
app/api/alpha/__init__.py's error handler logs, Sentry-captures and echoes
back anything whose message differs, so a more descriptive message there
turns a routine rejection into a reported incident. `type(...) is Exception`
accompanies those, because `pytest.raises(Exception)` would happily catch a
PyJWT error that escaped uncaught.

`password_updated_at` is stamped by `User.set_password()` itself (see
TestAPasswordResetRevokesExistingApiTokens), so every password change revokes
existing tokens; the one deliberate exception is `check_password()`'s
legacy-bcrypt rehash, which passes `revoke_sessions=False`.

That covers every genuine password change EXCEPT the offline `lemmy-import`
CLI command, which writes `password_hash` directly rather than through
`set_password()`: `app/cli.py:377` (new-user construction) is harmless
because the column's `default=utcnow` covers a fresh row, but
`app/cli.py:360` (overwriting an existing row's hash during a re-import)
is not stamped at all. An earlier count of "eight password-changing sites"
included this line among the stampers; it does not belong there. Re-derived
mechanically rather than by hand: `grep -rn '\\.set_password(' app/` finds
nine callers, of which eight pass the default `revoke_sessions=True` (and so
stamp) -- `app/cli.py:252`, `app/cli.py:271`, `app/cli.py:1206`,
`app/admin/routes.py:1898`, `app/auth/util.py:271`, `app/auth/util.py:297`,
`app/auth/routes.py:160`, `app/user/routes.py:240` -- and one,
`app/models.py:1149`, deliberately does not. Separately,
`grep -rn 'password_hash\\s*=' app/` (filtered to direct assignments outside
`set_password`'s own definition) finds exactly the two `app/cli.py` lines
above, neither of which is a `set_password()` call and only one of which
stamps by accident of the column default.

Tokens are minted through `User.encode_jwt_token()` (app/models.py:1576)
wherever the payload's `iat` doesn't need to be pinned to a specific instant.
The two password-rotation tests below need an `iat` other than "now", so they
build the JWT with `jwt.encode` directly -- `_encode_token_with_iat` mirrors
`encode_jwt_token`'s payload exactly (`sub`, `iss`, `iat`, `exp`, `jti`, same
secret, same algorithm) except for the `iat`/`exp` values being varied
explicitly instead of `int(time())`.
"""
import uuid
from datetime import datetime, timezone
from time import time

import jwt
import pytest
from flask import current_app

from app import app_bcrypt, db
from app.constants import NOTIF_REPLY
from app.models import RevokedToken, User, utcnow
from app.utils import authorise_api_user
from tests.factories import (ban_user_from_community, make_community, make_community_member,
                             make_instance, make_notification_subscription, make_post,
                             make_post_reply, make_post_reply_bookmark, make_post_reply_vote,
                             make_user, make_user_block)

# A safely-in-the-past password_updated_at for tests that need the
# password-rotation check to pass without being the thing under test --
# make_user leaves password_updated_at at its column default (utcnow() at
# insert time), and a token minted the same wall-clock second could otherwise
# be mistaken for predating a password change (comparing whole-second `iat`
# against a sub-second-precision timestamp is a real, if rare, race).
SAFE_PASSWORD_UPDATED_AT = datetime(2000, 1, 1)


def _encode_token_with_iat(user: User, iat: int, exp: int = None) -> str:
    """Mint a JWT matching User.encode_jwt_token()'s payload shape exactly,
    except iat/exp are given explicitly instead of int(time()). Needed only
    for the password-rotation pair below, where the test must control
    exactly when the token claims to have been issued relative to
    password_updated_at -- encode_jwt_token() always uses "now".
    """
    payload = {
        'sub': str(user.id),
        'iss': current_app.config['SERVER_NAME'],
        'iat': iat,
        'exp': exp if exp is not None else iat + 86400,
        'jti': str(uuid.uuid4()),
    }
    return jwt.encode(payload, current_app.config['SECRET_KEY'], algorithm='HS256')


def _eligible_user(name: str, instance) -> User:
    """A user that clears every guard up to and including the password-rotation
    check: local, verified, unbanned, undeleted, password_updated_at pinned
    safely in the past.
    """
    user = make_user(instance, name, local=True, with_keys=True)
    user.password_updated_at = SAFE_PASSWORD_UPDATED_AT
    db.session.commit()
    return user


class TestAuthoriseApiUserNoAuth:
    """`if not auth: raise Exception('incorrect_login')`"""

    def test_none_auth_is_refused(self, app, db_session):
        with pytest.raises(Exception, match='incorrect_login'):
            authorise_api_user(None)

    def test_empty_string_auth_is_refused(self, app, db_session):
        """Empty string is also falsy -- a different input than None hitting
        the same branch."""
        with pytest.raises(Exception, match='incorrect_login'):
            authorise_api_user('')


class TestAuthoriseApiUserBearerPrefix:
    """`if not auth.startswith('Bearer '): raise Exception('incorrect_login')`"""

    def test_auth_without_bearer_prefix_is_refused(self, app, db_session):
        with pytest.raises(Exception, match='incorrect_login'):
            authorise_api_user('Basic sometoken')

    def test_bearer_without_trailing_space_is_refused(self, app, db_session):
        """'Bearer' with no space and no token still fails the exact-prefix
        check -- startswith('Bearer ') requires the space."""
        with pytest.raises(Exception, match='incorrect_login'):
            authorise_api_user('Bearersometoken')


class TestAuthoriseApiUserDecodeFailure:
    """`except InvalidTokenError: raise Exception('incorrect_login')`.

    This used to raise 'incorrect_login - problem decoding bearer token'. That
    message is now logged instead of raised: app/api/alpha/__init__.py's error
    handler puts `str(e)` straight into the response body and treats anything
    other than the exact string 'incorrect_login' as an application error, so
    the longer message both leaked which check failed and turned a garbled
    token into a logged, Sentry-captured incident. See
    TestAuthoriseApiUserJwtValidationFailures below for the siblings that used
    to escape entirely.
    """

    def test_an_undecodable_token_is_refused(self, app, db_session):
        with pytest.raises(Exception) as exc_info:
            authorise_api_user('Bearer not-a-real-jwt-token')
        assert type(exc_info.value) is Exception
        assert str(exc_info.value) == 'incorrect_login'


class TestAuthoriseApiUserRevokedToken:
    """`if RevokedToken.query.filter_by(jti=decoded.get('jti')).first():
    raise Exception('incorrect_login')`"""

    def test_a_revoked_tokens_jti_is_refused(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')
        user = _eligible_user('revokeduser', None)
        token = user.encode_jwt_token()
        decoded = jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=['HS256'])
        db.session.add(RevokedToken(jti=decoded['jti']))
        db.session.commit()
        with pytest.raises(Exception, match='incorrect_login'):
            authorise_api_user(f'Bearer {token}')

    def test_an_unrevoked_tokens_jti_is_accepted(self, app, db_session):
        """The False side of the same branch: a token whose jti is NOT in
        RevokedToken proceeds past this check."""
        make_instance('test.piefed.local', software='piefed')
        user = _eligible_user('notrevokeduser', None)
        token = user.encode_jwt_token()
        assert authorise_api_user(f'Bearer {token}') == user.id


class TestAuthoriseApiUserUnknownUser:
    """`user = User.query.get(user_id); if user is None: raise
    Exception('incorrect_login')`

    The token is minted through the real encode_jwt_token() -- called on a
    genuine (but never committed) User instance, so the JWT's signature and
    shape are exactly what production code would produce, but the sub it
    names has no row in the database.
    """

    def test_a_token_for_a_nonexistent_user_is_refused(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')
        ghost = User(user_name='ghost', email='ghost@example.com', instance_id=1, verified=True)
        ghost.id = 999999  # never persisted -- User.query.get(999999) is None
        token = ghost.encode_jwt_token()
        with pytest.raises(Exception, match='incorrect_login'):
            authorise_api_user(f'Bearer {token}')


class TestAuthoriseApiUserCompoundRejection:
    """`if user.ap_id is not None or user.verified is False or user.banned is
    True or user.deleted is True: raise Exception('incorrect_login')`

    Each sub-condition gets its own test, everything else held eligible --
    branch coverage alone would pass with any one of the four `or` clauses
    deleted, since the whole expression only needs to be True once. The
    discrimination check in this task's report deletes `user.banned is True`
    and confirms only the banned test below fails.
    """

    def test_a_remote_user_is_refused(self, app, db_session):
        """Fails if `user.ap_id is not None` is deleted. make_user(instance,
        name) without local=True produces a remote user (ap_id set)."""
        instance = make_instance('test.piefed.local', software='piefed')
        remote = make_instance('remote.example')
        user = make_user(remote, 'remoteuser')
        user.password_updated_at = SAFE_PASSWORD_UPDATED_AT
        db.session.commit()
        token = user.encode_jwt_token()
        with pytest.raises(Exception, match='incorrect_login'):
            authorise_api_user(f'Bearer {token}')

    def test_an_unverified_user_is_refused(self, app, db_session):
        """Fails if `user.verified is False` is deleted."""
        make_instance('test.piefed.local', software='piefed')
        user = _eligible_user('unverifieduser', None)
        user.verified = False
        db.session.commit()
        token = user.encode_jwt_token()
        with pytest.raises(Exception, match='incorrect_login'):
            authorise_api_user(f'Bearer {token}')

    def test_a_banned_user_is_refused(self, app, db_session):
        """Fails if `user.banned is True` is deleted."""
        make_instance('test.piefed.local', software='piefed')
        user = _eligible_user('banneduser', None)
        user.banned = True
        db.session.commit()
        token = user.encode_jwt_token()
        with pytest.raises(Exception, match='incorrect_login'):
            authorise_api_user(f'Bearer {token}')

    def test_a_deleted_user_is_refused(self, app, db_session):
        """Fails if `user.deleted is True` is deleted."""
        make_instance('test.piefed.local', software='piefed')
        user = _eligible_user('deleteduser', None)
        user.deleted = True
        db.session.commit()
        token = user.encode_jwt_token()
        with pytest.raises(Exception, match='incorrect_login'):
            authorise_api_user(f'Bearer {token}')

    def test_an_eligible_user_is_not_refused_by_the_compound_guard(self, app, db_session):
        """Baseline: none of the four sub-conditions apply, so the compound
        guard's False outcome is exercised too (branch coverage needs both
        sides, not just the True side each test above hits)."""
        make_instance('test.piefed.local', software='piefed')
        user = _eligible_user('eligibleuser', None)
        token = user.encode_jwt_token()
        assert authorise_api_user(f'Bearer {token}') == user.id


class TestAuthoriseApiUserPasswordRotation:
    """The sharpest test in this task:

        if user.password_updated_at:
            issued_at_time = decoded['iat']
            password_updated_time = int(user.password_updated_at.timestamp())
            if issued_at_time < password_updated_time:
                raise Exception('incorrect_login')

    A token minted BEFORE a password change must be refused -- that is how
    changing a password revokes existing sessions. Only the before/after PAIR
    proves the comparison direction is right: a `>` where `<` was meant would
    accept exactly the tokens it should reject and refuse the legitimate
    ones, and either test alone would still pass against that bug. The
    discrimination check in this task's report reverses the comparison and
    confirms both tests below fail.

    DRIFT, marked rather than silently corrected: reversing `<` to `>` was
    originally measured (module-scoped, this file only) at 10 failures.
    Suite-wide it is now 22 -- `set_password()` began stamping
    `password_updated_at` on every password change once the auth fix
    landed, so far more fixtures across the suite now mint tokens against a
    live stamp instead of a null one, and each becomes collateral under the
    reversed comparison. The property the count was evidence for still
    holds either way: both halves of the rotation pair above fail. Verified
    with `./run_tests.sh tests/ -q --ignore=tests/test_activitypub_util.py`
    against the reversed comparison: `22 failed, 1636 passed`.
    """

    def test_a_token_issued_before_the_password_change_is_refused(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')
        user = make_user(None, 'rotatebefore', local=True, with_keys=True)
        now = int(time())
        # jwt.decode validates both exp (must be in the future) and iat (must
        # not be in the future) with zero leeway by default -- so every iat
        # used below must be <= now, and password_changed_at must land
        # strictly between the "before" and "after" iats while itself never
        # exceeding now.
        password_changed_at_epoch = now - 1800  # 30 minutes ago
        user.password_updated_at = datetime.fromtimestamp(password_changed_at_epoch, tz=timezone.utc).replace(tzinfo=None)
        db.session.commit()
        iat = password_changed_at_epoch - 3600  # one hour before the change
        token = _encode_token_with_iat(user, iat, exp=now + 90000)
        with pytest.raises(Exception, match='incorrect_login'):
            authorise_api_user(f'Bearer {token}')

    def test_a_token_issued_after_the_password_change_is_accepted(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')
        user = make_user(None, 'rotateafter', local=True, with_keys=True)
        now = int(time())
        password_changed_at_epoch = now - 1800  # 30 minutes ago
        user.password_updated_at = datetime.fromtimestamp(password_changed_at_epoch, tz=timezone.utc).replace(tzinfo=None)
        db.session.commit()
        iat = password_changed_at_epoch + 900  # 15 minutes after the change,
        # still <= now so jwt.decode's iat-in-the-future check does not fire
        token = _encode_token_with_iat(user, iat, exp=now + 90000)
        assert authorise_api_user(f'Bearer {token}') == user.id

    def test_a_user_with_no_password_updated_at_skips_the_check(self, app, db_session):
        """The `if user.password_updated_at:` guard's False outcome --
        password_updated_at is nullable (migration
        b15d2a8704cc_password_updated_at_to_user.py), and when it is None the
        whole rotation check is skipped regardless of iat."""
        make_instance('test.piefed.local', software='piefed')
        user = make_user(None, 'nopasswordupdate', local=True, with_keys=True)
        user.password_updated_at = None
        db.session.commit()
        now = int(time())
        # An iat far in the past would fail the rotation check if it ran --
        # still <= now, just old, so jwt.decode's own iat validation accepts it.
        token = _encode_token_with_iat(user, iat=now - 500000, exp=now + 90000)
        assert authorise_api_user(f'Bearer {token}') == user.id


class TestAuthoriseApiUserIdMatch:
    """`if id_match and user.id != id_match: raise Exception('incorrect_login')`"""

    def test_id_match_equal_to_the_users_id_is_accepted(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')
        user = _eligible_user('idmatchok', None)
        token = user.encode_jwt_token()
        assert authorise_api_user(f'Bearer {token}', id_match=user.id) == user.id

    def test_id_match_different_from_the_users_id_is_refused(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')
        user = _eligible_user('idmatchbad', None)
        token = user.encode_jwt_token()
        with pytest.raises(Exception, match='incorrect_login'):
            authorise_api_user(f'Bearer {token}', id_match=user.id + 12345)

    def test_id_match_none_skips_the_check(self, app, db_session):
        """id_match is falsy by default -- the `id_match and ...` short-circuit's
        False outcome, distinct from the two cases above where it's truthy."""
        make_instance('test.piefed.local', software='piefed')
        user = _eligible_user('idmatchnone', None)
        token = user.encode_jwt_token()
        assert authorise_api_user(f'Bearer {token}', id_match=None) == user.id


class TestAuthoriseApiUserReturnTypes:
    """`return_type` dispatch:

        if return_type and return_type == 'model': return user
        elif return_type and return_type == 'dict': return user_dict
        else: return user.id
    """

    def test_default_return_type_yields_the_user_id(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')
        user = _eligible_user('returnidcase', None)
        token = user.encode_jwt_token()
        result = authorise_api_user(f'Bearer {token}')
        assert result == user.id
        assert isinstance(result, int)

    def test_model_return_type_yields_the_user_object(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')
        user = _eligible_user('returnmodelcase', None)
        token = user.encode_jwt_token()
        result = authorise_api_user(f'Bearer {token}', return_type='model')
        assert isinstance(result, User)
        assert result.id == user.id

    def test_an_unrecognized_return_type_falls_through_to_the_user_id(self, app, db_session):
        """Neither `if` nor `elif` matches an unrecognized return_type string,
        so it falls to the `else` branch -- same outcome as the default case,
        exercising the False side of both `return_type == 'model'` and
        `return_type == 'dict'` with return_type itself truthy. Documented
        behaviour, not asserted to be intentional design."""
        make_instance('test.piefed.local', software='piefed')
        user = _eligible_user('returnbogus', None)
        token = user.encode_jwt_token()
        result = authorise_api_user(f'Bearer {token}', return_type='not_a_real_type')
        assert result == user.id


class TestAuthoriseApiUserDictReturn:
    """The 'dict' return type executes eight list-building lookups (the
    brief's prose groups them as "six queries" -- upvoted/downvoted replies
    and followed/moderated communities each in fact produce two of the dict's
    keys from two separately-filtered queries): banned communities, followed
    communities, bookmarked replies, blocked creators, upvoted replies,
    downvoted replies, subscribed replies, and moderated communities.

    Every one of the eight keys is seeded here with data that only the
    correct query would surface -- deliberately not leaving any of them
    empty, since an empty list that nothing seeded proves nothing about the
    query that produced it. Distinct communities/replies/users are used for
    each key precisely so that a query reading the wrong table or the wrong
    filter would be caught by a WRONG value, not just a present one.
    """

    def test_the_dict_shape_reflects_seeded_data(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')
        user = _eligible_user('dictuser', None)

        # 1. user_ban_community_ids: CommunityBan half of communities_banned_from.
        banned_community = make_community('dictbannedcommunity')
        ban_user_from_community(user, banned_community)

        # 2 & 8. followed_community_ids / moderated_community_ids both come from
        # community_member -- a plain member row and a moderator row, kept in
        # separate communities so each key's own filter can be checked exactly.
        followed_community = make_community('dictfollowedcommunity')
        make_community_member(user, followed_community, is_moderator=False)
        moderated_community = make_community('dictmoderatedcommunity')
        make_community_member(user, moderated_community, is_moderator=True)

        # A post to hang replies off of.
        content_community = make_community('dictcontentcommunity')
        author = make_user(None, 'dictreplyauthor', local=True)
        post = make_post(content_community, author, 'https://test.piefed.local/p/dict1')

        # 3. bookmarked_reply_ids
        bookmarked_reply = make_post_reply(post, author, body='bookmarked')
        make_post_reply_bookmark(user, bookmarked_reply)

        # 4. blocked_creator_ids
        blocked_creator = make_user(None, 'dictblockedcreator', local=True)
        make_user_block(user, blocked_creator)

        # 5. upvoted_reply_ids
        upvoted_reply = make_post_reply(post, author, body='upvoted')
        make_post_reply_vote(user, upvoted_reply, effect=1.0)

        # 6. downvoted_reply_ids
        downvoted_reply = make_post_reply(post, author, body='downvoted')
        make_post_reply_vote(user, downvoted_reply, effect=-1.0)

        # 7. subscribed_reply_ids
        subscribed_reply = make_post_reply(post, author, body='subscribed')
        make_notification_subscription(user, subscribed_reply.id, NOTIF_REPLY)

        token = user.encode_jwt_token()
        result = authorise_api_user(f'Bearer {token}', return_type='dict')

        assert result['id'] == user.id
        assert result['user_ban_community_ids'] == [banned_community.id]
        assert sorted(result['followed_community_ids']) == sorted(
            [followed_community.id, moderated_community.id])
        assert result['bookmarked_reply_ids'] == [bookmarked_reply.id]
        assert result['blocked_creator_ids'] == [blocked_creator.id]
        assert result['upvoted_reply_ids'] == [upvoted_reply.id]
        assert result['downvoted_reply_ids'] == [downvoted_reply.id]
        assert result['subscribed_reply_ids'] == [subscribed_reply.id]
        assert result['moderated_community_ids'] == [moderated_community.id]


class TestAPasswordResetRevokesExistingApiTokens:
    """The password-rotation check above is only as good as its wiring.

    `password_updated_at` used to be written in exactly ONE place --
    app/user/routes.py's settings form -- so every other way a password
    changes left it untouched and the rotation check inert. The
    forgot-password reset is the worst of those: it is the path a user takes
    AFTER a compromise, and it left the attacker's bearer token working.

    These tests drive the real route rather than assigning the column, because
    the column being honoured is already covered by
    TestAuthoriseApiUserPasswordRotation. What was broken is the wiring, and
    only the wiring proves the fix.
    """

    def _user_with_a_token_minted_an_hour_ago(self, name):
        """A local, eligible user whose password_updated_at is pinned far in
        the past, plus a bearer token whose iat is an hour old.

        The hour matters: `password_updated_at` is written with second-or-finer
        precision at reset time while `iat` is whole seconds, so a token minted
        in the same wall-clock second as the reset would compare equal, not
        less-than, and the test would pass or fail on timing rather than on the
        fix.
        """
        make_instance('test.piefed.local', software='piefed')
        user = make_user(None, name, local=True)
        user.password_updated_at = SAFE_PASSWORD_UPDATED_AT
        db.session.commit()
        token = _encode_token_with_iat(user, iat=int(time()) - 3600, exp=int(time()) + 90000)
        # Precondition: the token works right now. Without this the test could
        # pass because the token was never valid in the first place.
        assert authorise_api_user(f'Bearer {token}') == user.id
        return user, token

    def test_the_forgot_password_route_revokes_a_token_minted_before_it(self, app, site):
        """app/auth/routes.py reset_password(): the recovery path a compromised
        user actually takes. It calls set_password() and commits, and nothing
        else -- so the revocation has to come from set_password itself.

        The reset token is produced by User.get_reset_password_token(), which is
        the same value send_password_reset_email() puts in the email; the route
        consumes it through the real verify_reset_password_token().
        """
        user, token = self._user_with_a_token_minted_an_hour_ago('resetviaemail')
        reset_token = user.get_reset_password_token()

        client = app.test_client()
        response = client.post(f'/auth/reset_password/{reset_token}',
                               data={'password': 'a-brand-new-password',
                                     'password2': 'a-brand-new-password',
                                     'submit': 'Set password'})
        assert response.status_code == 302

        # The request ran in its own app context, hence its own session.
        db.session.expire_all()
        reloaded = User.query.get(user.id)
        # Proof the route really changed the password, not just redirected.
        assert reloaded.check_password('a-brand-new-password')

        with pytest.raises(Exception) as exc_info:
            authorise_api_user(f'Bearer {token}')
        assert str(exc_info.value) == 'incorrect_login'

    def test_set_password_stamps_password_updated_at(self, app, db_session):
        """The seam itself. Every other caller listed in the finding
        (app/admin/routes.py's admin reset, app/auth/util.py's two
        user-creation paths, app/cli.py's three) goes through this one method,
        so stamping here is what makes them all revoke.
        """
        make_instance('test.piefed.local', software='piefed')
        user = make_user(None, 'stamped', local=True)
        user.password_updated_at = SAFE_PASSWORD_UPDATED_AT
        db.session.commit()

        before = utcnow()
        user.set_password('something-else')
        after = utcnow()

        assert before <= user.password_updated_at <= after

    def test_the_bcrypt_rehash_on_login_does_not_revoke_anything(self, app, db_session):
        """The one caller that must NOT revoke: User.check_password()'s
        fallback re-saves the hash with a stronger algorithm when the stored
        one is a legacy bcrypt hash. The PASSWORD did not change -- only its
        encoding -- and a successful login is not a reason to sign every one of
        that user's API clients out.

        This is a live path, not a hypothetical: werkzeug's check_password_hash
        raises ValueError("Invalid hash method ''") on a `$2b$` bcrypt hash,
        which is exactly what routes the login into the fallback.
        """
        make_instance('test.piefed.local', software='piefed')
        user = make_user(None, 'legacyhash', local=True)
        user.password_hash = app_bcrypt.generate_password_hash('the-old-password').decode()
        user.password_updated_at = SAFE_PASSWORD_UPDATED_AT
        db.session.commit()
        token = _encode_token_with_iat(user, iat=int(time()) - 3600, exp=int(time()) + 90000)
        assert authorise_api_user(f'Bearer {token}') == user.id

        assert user.check_password('the-old-password') is True
        # The fallback ran: the stored hash is no longer bcrypt's.
        assert not user.password_hash.startswith('$2b$')
        # ...and the stamp was left alone, so the token still works.
        assert user.password_updated_at == SAFE_PASSWORD_UPDATED_AT
        assert authorise_api_user(f'Bearer {token}') == user.id


class TestAuthoriseApiUserJwtValidationFailures:
    """`except InvalidTokenError:` -- every way PyJWT can reject a token.

    Only DecodeError used to be caught. ExpiredSignatureError and
    ImmatureSignatureError are siblings of it under InvalidTokenError, not
    subclasses, so both escaped `authorise_api_user` as themselves. That
    matters because app/api/alpha/__init__.py's error handler compares
    `str(e)` against the exact string 'incorrect_login' and logs plus
    Sentry-captures anything else -- so the single commonest legitimate
    rejection there is, an expired token, was reported as an application
    error.

    `type(...) is Exception` is the load-bearing assertion in each test:
    `pytest.raises(Exception)` catches every PyJWT error too, since they all
    descend from Exception.
    """

    def test_an_expired_token_is_refused_as_a_plain_incorrect_login(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')
        user = _eligible_user('expiredtoken', None)
        now = int(time())
        token = _encode_token_with_iat(user, iat=now - 7200, exp=now - 60)
        with pytest.raises(Exception) as exc_info:
            authorise_api_user(f'Bearer {token}')
        assert type(exc_info.value) is Exception
        assert str(exc_info.value) == 'incorrect_login'

    def test_a_token_dated_in_the_future_is_refused_as_a_plain_incorrect_login(self, app, db_session):
        """PyJWT raises ImmatureSignatureError("The token is not yet valid
        (iat)") for an iat ahead of now -- a different sibling of DecodeError,
        and it escaped the same way."""
        make_instance('test.piefed.local', software='piefed')
        user = _eligible_user('futuretoken', None)
        now = int(time())
        token = _encode_token_with_iat(user, iat=now + 3600, exp=now + 90000)
        with pytest.raises(Exception) as exc_info:
            authorise_api_user(f'Bearer {token}')
        assert type(exc_info.value) is Exception
        assert str(exc_info.value) == 'incorrect_login'

    def test_a_token_signed_with_the_wrong_secret_is_refused(self, app, db_session):
        """InvalidSignatureError is a DecodeError subclass, so this one was
        already caught -- kept as the "still works" side of widening the
        except clause."""
        make_instance('test.piefed.local', software='piefed')
        user = _eligible_user('wrongsecret', None)
        now = int(time())
        payload = {'sub': str(user.id), 'iss': current_app.config['SERVER_NAME'],
                   'iat': now, 'exp': now + 90000, 'jti': str(uuid.uuid4())}
        token = jwt.encode(payload, 'not-the-servers-secret', algorithm='HS256')
        with pytest.raises(Exception) as exc_info:
            authorise_api_user(f'Bearer {token}')
        assert type(exc_info.value) is Exception
        assert str(exc_info.value) == 'incorrect_login'


class TestAnExpiredTokenIsNotLoggedAsAnApplicationError:
    """The consequence Finding 2 is actually about, asserted where it happens:
    app/api/alpha/__init__.py's error handler.

    It logs `current_app.logger.exception("API exception")` (and captures to
    Sentry when configured) for every exception whose message is not exactly
    'incorrect_login'. An expired bearer token is a routine client condition;
    it should produce a 400 and nothing in the error log.
    """

    def test_an_expired_bearer_token_yields_a_clean_400(self, app, site, caplog, monkeypatch):
        monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
        make_instance('test.piefed.local', software='piefed')
        user = _eligible_user('apiexpired', None)
        now = int(time())
        token = _encode_token_with_iat(user, iat=now - 7200, exp=now - 60)

        client = app.test_client()
        with caplog.at_level('ERROR'):
            response = client.get('/api/alpha/user/unread_count',
                                  headers={'Authorization': f'Bearer {token}'})

        assert response.status_code == 400
        assert response.json['message'] == 'incorrect_login'
        assert 'API exception' not in caplog.text
