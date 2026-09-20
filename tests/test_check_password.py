"""User.check_password must be total: every stored hash, however malformed,
produces True or False and never an exception.

It was not. The method used to be written as

    try:
        return check_password_hash(self.password_hash, password)
    except ValueError:
        result = app_bcrypt.check_password_hash(self.password_hash, password)
        ...
    except Exception:
        return False

and an exception raised INSIDE `except ValueError:` is not caught by the
sibling `except Exception:` -- sibling handlers on the same `try` guard the
try block, not each other. So the moment werkzeug rejected a hash with
ValueError and the bcrypt fallback then raised as well, the exception left
check_password entirely and the login route answered 500 instead of "wrong
password".

The hashes below are the reachable shapes of that: each one makes
werkzeug raise ValueError (routing into the fallback) and then makes bcrypt
raise too, and they cover both ways werkzeug can raise: three where it cannot
resolve the method at all, and one where it resolves a real method and that
method rejects its parameters. They are not hypothetical rows -- a partial
migration from another system, a truncated `password_hash` column, a
hand-edited row or a config that once set impossible scrypt parameters all
produce one.

The rest of the file pins the behaviour that must NOT change while the
fallback is made total, in particular the legacy-bcrypt migration: a correct
password against a real bcrypt hash still returns True, still re-saves the
hash with werkzeug's algorithm, and still leaves `password_updated_at` alone,
because a hash migration on a successful login is not a password change and
must not revoke that user's API tokens (see User.set_password's docstring and
tests/test_utils_api_auth.py).
"""

from datetime import datetime

import pytest
from werkzeug.security import check_password_hash

from app import app_bcrypt, db
from app.models import User
from tests.factories import make_instance, make_user

# A password_updated_at set far in the past, so that "the method did not stamp
# it" is observable: make_user leaves the column at its insert-time default of
# utcnow(), against which a fresh stamp is indistinguishable.
SAFE_PASSWORD_UPDATED_AT = datetime(2000, 1, 1)

# Each of these makes werkzeug's check_password_hash raise ValueError -- it
# splits on '$' and reads the leading empty segment as the hash method -- and
# then makes bcrypt raise as well, which is the combination that used to
# escape. Named rather than inlined so the failure output says which shape
# broke.
ESCAPING_HASHES = {
    # bcrypt-shaped, but the 53-character payload is not bcrypt's base64
    # alphabet, so bcrypt rejects the salt.
    'bcrypt_shaped_with_invalid_payload': '$2b$12$' + ('!' * 53),
    # The right prefix, far too short to hold a salt and a checksum.
    'truncated_bcrypt': '$2b$12$short',
    # Neither werkzeug's nor bcrypt's: an algorithm from some other system.
    'unknown_method_prefix': '$argon9$x$y',
    # The other fallback family, and the only one here that werkzeug recognises:
    # a REAL werkzeug method with impossible parameters. werkzeug parses
    # 'scrypt:0:0:0' as scrypt with n=0, r=0, p=0 and hashlib raises
    # ValueError('n must be a power of 2') from inside the method it selected --
    # not from failing to find one. The three shapes above all fail at method
    # lookup instead, so without this the set covered only one of the two ways
    # werkzeug can raise. bcrypt then rejects it as an invalid salt, same as the
    # rest, and check_password returns False.
    'valid_werkzeug_method_bad_parameters': 'scrypt:0:0:0$s$' + ('a' * 64),
}


@pytest.fixture
def user(app, db_session):
    make_instance('test.piefed.local', software='piefed')
    u = make_user(None, 'checkpassword', local=True)
    u.password_updated_at = SAFE_PASSWORD_UPDATED_AT
    db.session.commit()
    return u


class TestAMalformedHashDoesNotEscape:
    """The bug. Each of these raised out of check_password before the fix."""

    @pytest.mark.parametrize('shape', sorted(ESCAPING_HASHES))
    def test_a_malformed_hash_returns_false_rather_than_raising(self, user, shape):
        user.password_hash = ESCAPING_HASHES[shape]

        assert user.check_password('any-password-at-all') is False

    @pytest.mark.parametrize('shape', sorted(ESCAPING_HASHES))
    def test_the_malformed_hash_is_left_alone(self, user, shape):
        """A failed check must not migrate anything. Rewriting the hash here
        would let an unauthenticated caller overwrite a row's password_hash
        just by attempting a login against it.
        """
        original = ESCAPING_HASHES[shape]
        user.password_hash = original

        user.check_password('any-password-at-all')

        assert user.password_hash == original

    @pytest.mark.parametrize('shape', sorted(ESCAPING_HASHES))
    def test_these_hashes_really_do_take_the_bcrypt_fallback(self, user, shape):
        """Proof that the parametrised inputs still exercise the path under
        test, and have not silently become a case werkzeug handles quietly.
        Without this, a future werkzeug that returned False instead of raising
        would leave the three tests above passing for the wrong reason.
        """
        hash_value = ESCAPING_HASHES[shape]

        with pytest.raises(ValueError):
            check_password_hash(hash_value, 'any-password-at-all')

        # Deliberately as broad as check_password's own guard: what matters is
        # that bcrypt refuses these by raising something, not which class it
        # picks. (All three currently raise ValueError('Invalid salt').)
        with pytest.raises(Exception):
            app_bcrypt.check_password_hash(hash_value, 'any-password-at-all')


class TestTheBehaviourThatMustNotChange:

    def test_a_correct_password_against_a_werkzeug_hash_returns_true(self, user):
        user.set_password('the-right-password')

        assert user.check_password('the-right-password') is True

    def test_a_wrong_password_returns_false(self, user):
        user.set_password('the-right-password')

        assert user.check_password('the-wrong-password') is False

    def test_an_absent_hash_returns_false(self, user):
        """A row that has never had a password set -- every remote user, and
        any local user created before one was chosen.
        """
        user.password_hash = None

        assert user.check_password('any-password-at-all') is False

    def test_an_empty_hash_returns_false(self, user):
        user.password_hash = ''

        assert user.check_password('any-password-at-all') is False


class TestAValidLegacyBcryptHashStillMigrates:
    """The fallback exists to carry rows forward from PieFed's bcrypt era. A
    correct password against a real bcrypt hash must still authenticate AND
    still be re-saved under werkzeug's algorithm -- and must not stamp
    password_updated_at while doing it.
    """

    def test_it_returns_true_and_rewrites_the_hash(self, user):
        user.password_hash = app_bcrypt.generate_password_hash('the-old-password').decode()
        db.session.commit()

        assert user.check_password('the-old-password') is True

        assert not user.password_hash.startswith('$2b$')
        # The rewritten hash is werkzeug's, and it verifies the same password.
        assert check_password_hash(user.password_hash, 'the-old-password') is True

    def test_the_migration_is_committed(self, user):
        user.password_hash = app_bcrypt.generate_password_hash('the-old-password').decode()
        db.session.commit()

        assert user.check_password('the-old-password') is True

        db.session.expire_all()
        assert not db.session.get(User, user.id).password_hash.startswith('$2b$')

    def test_it_does_not_stamp_password_updated_at(self, user):
        """revoke_sessions=False. The password did not change, only its
        encoding, so the user's API tokens must survive the migration.
        """
        user.password_hash = app_bcrypt.generate_password_hash('the-old-password').decode()
        db.session.commit()

        assert user.check_password('the-old-password') is True

        assert user.password_updated_at == SAFE_PASSWORD_UPDATED_AT

    def test_a_wrong_password_against_a_valid_bcrypt_hash_returns_false(self, user):
        original = app_bcrypt.generate_password_hash('the-old-password').decode()
        user.password_hash = original
        db.session.commit()

        assert user.check_password('the-wrong-password') is False

        # No migration on a failed check.
        assert user.password_hash == original
