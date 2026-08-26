"""Covers app/utils.py's authorise_api_user: the API authentication gate, with
138 call sites. Every rejection path matters -- a missed one means a request
that should be refused gets served.

Guard order, as read from app/utils.py (~3345-3406):

    not auth                                                   -> raise
    not auth.startswith('Bearer ')                              -> raise
    jwt.decode(...) raises DecodeError                          -> raise (with
                                                                    a more specific message)
    RevokedToken.query.filter_by(jti=decoded['jti']).first()    -> raise
    User.query.get(decoded['sub']) is None                      -> raise
    user.ap_id is not None or user.verified is False or          -> raise
        user.banned is True or user.deleted is True
    user.password_updated_at and iat < password_updated_time    -> raise
    id_match and user.id != id_match                            -> raise
    otherwise: return_type == 'model'  -> the User
               return_type == 'dict'   -> a dict built from six queries
               otherwise               -> user.id

Every rejection raises a bare `Exception('incorrect_login')` (the decode
failure carries a longer message with the same prefix) -- `pytest.raises`
alone would match almost anything, so every test here also asserts on the
message via `match=`.

Tokens are minted through `User.encode_jwt_token()` (app/models.py:1537)
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

from app import db
from app.constants import NOTIF_REPLY
from app.models import RevokedToken, User
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
    """`except DecodeError: raise Exception('incorrect_login - problem
    decoding bearer token')` -- a distinct message from every other
    rejection, asserted on exactly."""

    def test_an_undecodable_token_is_refused(self, app, db_session):
        with pytest.raises(Exception, match='incorrect_login - problem decoding bearer token'):
            authorise_api_user('Bearer not-a-real-jwt-token')


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
