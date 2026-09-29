"""D1402: `$` matches before a trailing newline, so three validators were one character wide.

`re.match(r'^...$', value)` and `re.fullmatch(r'^...$', value)` differ for exactly one
input shape: a value ending in a single newline. `$` in Python's `re` matches at the end
of the string **and** immediately before a trailing newline, so `match` accepts
`'0101\\n'` where `fullmatch` does not.

    PROBE  BINARY_RE.match('0101\\n')      True
           BINARY_RE.fullmatch('0101\\n')  False
           BINARY_RE.match('0101\\n\\n')    False   (only ONE trailing newline)
           BINARY_RE.match('0101x')       False

Three validators in `app/` were written with `match` and an anchored pattern, and all
three are boundaries whose whole job is to be exact.

**THE ONE THAT WAS REACHABLE AND MEASURED.** `hash_matches_blocked_image` interpolates
its argument straight into SQL, and says so:

    # only accept a string with 0 and 1 in it. This makes it safe to use sql
    # injection-prone code below, which greatly simplifies the conversion of
    # binary strings
    if not BINARY_RE.match(hash):
    ...
    sql = f\"\"\"SELECT id FROM blocked_image WHERE length(replace((hash # B'{hash}')::text, '0', '')) < 15;\"\"\"

A trailing newline passes the guard and reaches Postgres inside the bit-string literal:

    PROBE  hash_matches_blocked_image('0'*256)        False
           hash_matches_blocked_image('0'*256 + '\\n') DataError:
               (psycopg2.errors.InvalidTextRepresentation)

Not an injection -- the newline stays inside the quotes -- but a `DataError` that
poisons the transaction. `Post.new` (app/models.py:2685) calls this, so a federated post
was lost and the aborted transaction took the rest of the inbox request with it;
`app/shared/post.py:570` calls it on a local upload, where it is a 500. Reachable
because `retrieve_image_hash` returned the hashing endpoint's `pdq_hash_binary`
verbatim with only an `isinstance(str)` check, and an HTTP service emitting a trailing
newline is entirely ordinary.

**THE ONE THAT REACHED THE DATABASE.** `validate_user_name_charset` is the shared
charset rule for self-registration and admin user creation. `'alice\\n'` satisfied it,
and `app/admin/routes.py:2125` stores `form.user_name.data` **unstripped** -- so the
newline reached the `user_name` column and every actor url built from it. Self-
registration strips before calling the helper; the admin path did not, which is the
producer/consumer divergence this campaign keeps finding, and is why the boundary
itself has to be exact rather than relying on its callers.

**THE ONE THAT WAS HARMLESS AND IS STILL WRONG.** `AddCommunityForm`'s url check
accepted `'books\\n'`, and the normalisation a few lines below slugifies it away before
storage -- so nothing was stored wrong. The guard still did not mean what its message
says, and a later edit moving the normalisation would have made it matter.

THE SWEEP. Every `.match(...)` in `app/` whose pattern ends in `$`, found by reading the
compiled patterns out of the AST: exactly these three. Five other anchored checks
already use `fullmatch` (`app/post/routes.py:2918`, `app/utils.py:5734`, `:6210`), and
two `match` calls are deliberately prefix tests on unanchored patterns
(`app/utils.py:465`'s `\\A`-anchored scheme check, `app/nntp/server.py:77`).
"""
import re

import pytest
from flask import g

from app import db
from app.models import Site
from app.utils import BINARY_RE, USER_NAME_CHARSET_RE, hash_matches_blocked_image

pytestmark = pytest.mark.usefixtures('site')

A_HASH = '0' * 256


# --------------------------------------------------------------------------
# The character itself
# --------------------------------------------------------------------------


class TestWhatTheAnchorAccepts:
    @pytest.mark.parametrize('pattern', [BINARY_RE, USER_NAME_CHARSET_RE,
                                         re.compile(r'^[a-zA-Z0-9_]+$')])
    def test_match_and_fullmatch_differ_only_for_a_trailing_newline(self, pattern):
        """The premise, stated once for all three patterns. `match` and `fullmatch`
        agree on everything except a value ending in exactly one newline."""
        body = '0101' if pattern is BINARY_RE else 'alice'

        assert bool(pattern.match(body)) is True
        assert bool(pattern.fullmatch(body)) is True
        # The one disagreement, before the repair:
        assert bool(pattern.match(body + '\n')) != bool(pattern.fullmatch(body + '\n'))
        # and two newlines is refused by both, which is why this is one character wide
        assert bool(pattern.match(body + '\n\n')) is False

    @pytest.mark.parametrize('suffix', ['\n', '\r\n'])
    def test_the_binary_guard_now_refuses_a_trailing_newline(self, suffix):
        assert BINARY_RE.fullmatch(A_HASH + suffix) is None

    @pytest.mark.parametrize('value', ['alice\n', 'alice\r\n', 'alice\r'])
    def test_the_user_name_guard_now_refuses_one_too(self, value):
        assert USER_NAME_CHARSET_RE.fullmatch(value) is None

    def test_a_clean_value_is_still_accepted(self, value=None):
        """The control for every row above."""
        assert BINARY_RE.fullmatch(A_HASH) is not None
        assert USER_NAME_CHARSET_RE.fullmatch('alice_99') is not None


# --------------------------------------------------------------------------
# The SQL one, end to end
# --------------------------------------------------------------------------


class TestTheBlockedImageHash:
    def test_a_clean_hash_is_answered_not_raised(self, app, db_session):
        """The control: the statement runs and answers False for a hash matching no
        blocked image."""
        assert hash_matches_blocked_image(A_HASH) is False

    @pytest.mark.parametrize('bad', [A_HASH + '\n', A_HASH + '\r\n', '\n' + A_HASH,
                                     A_HASH + ' ', A_HASH + 'x', '', 'not binary',
                                     A_HASH + "'; DROP TABLE post; --"])
    def test_anything_that_is_not_binary_is_refused_without_touching_postgres(
            self, app, db_session, bad):
        """`A_HASH + '\\n'` is the one this round repaired -- it reached Postgres and
        raised `DataError: InvalidTextRepresentation`, aborting the transaction. The
        others were already refused; they are here because the guard's job is the whole
        set, and a repair narrowing it to newlines alone would pass the first row and
        fail these."""
        assert hash_matches_blocked_image(bad) is False

    def test_the_session_is_still_usable_afterwards(self, app, db_session):
        """What the DataError actually cost. A poisoned transaction takes the rest of
        the request with it -- for `Post.new` that is the federated post being
        ingested, for an upload it is a 500. Asserted by using the session after."""
        assert hash_matches_blocked_image(A_HASH + '\n') is False

        assert db.session.get(Site, 1) is not None

    def test_a_hash_matching_a_blocked_image_answers_true(self, app, db_session):
        """The control the refusal rows need. Every other row here answers False, and
        so does a guard that refuses everything -- a blocked image is the only input
        that tells "the statement ran and found nothing" apart from "the statement
        never ran".

        The column is `BIT(256)`, so the row is inserted with an explicit cast; the
        distance the query allows is 15 differing bits, and an identical hash is 0.
        """
        from sqlalchemy import text
        from app.models import BlockedImage

        db.session.execute(
            text("INSERT INTO blocked_image (file_name, note, hash) "
                 "VALUES ('x.png', 'n', CAST(:h AS BIT(256)))"), {'h': A_HASH})
        db.session.commit()
        assert BlockedImage.query.count() == 1

        assert hash_matches_blocked_image(A_HASH) is True

    def test_a_hash_far_from_every_blocked_image_answers_false(self, app,
                                                               db_session):
        """The other side of the same comparison: 256 differing bits is well past the
        15 the query allows, so the statement runs and finds nothing."""
        from sqlalchemy import text

        db.session.execute(
            text("INSERT INTO blocked_image (file_name, note, hash) "
                 "VALUES ('x.png', 'n', CAST(:h AS BIT(256)))"), {'h': A_HASH})
        db.session.commit()

        assert hash_matches_blocked_image('1' * 256) is False

    def test_a_hash_of_the_wrong_length_is_still_answered(self, app, db_session):
        """Length is not this guard's business -- only the alphabet is -- and a short
        binary string is a legitimate question for Postgres to answer."""
        assert hash_matches_blocked_image('0101') is False


# --------------------------------------------------------------------------
# The hash the endpoint hands us
# --------------------------------------------------------------------------


class TestWhatRetrieveImageHashReturns:
    def _fetch(self, app, monkeypatch, payload, quality=90):
        from unittest.mock import MagicMock
        from app import utils

        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {'quality': quality, **payload}
        monkeypatch.setattr(utils, 'get_request', lambda *a, **k: response)
        monkeypatch.setitem(app.config, 'IMAGE_HASHING_ENDPOINT',
                            'https://hashing.test/')
        return utils.retrieve_image_hash('https://example.test/i.png')

    def test_a_hash_with_a_trailing_newline_is_stripped(self, app, db_session,
                                                        monkeypatch):
        """The other half of the repair. With the guard exact and no strip here, an
        endpoint appending a newline would make every one of its hashes fail the
        check -- the blocklist would silently stop matching rather than raising, which
        is the quieter of the two failures and the harder to notice.
        """
        assert self._fetch(app, monkeypatch,
                           {'pdq_hash_binary': A_HASH + '\n'}) == A_HASH

    def test_surrounding_whitespace_goes_too(self, app, db_session, monkeypatch):
        assert self._fetch(app, monkeypatch,
                           {'pdq_hash_binary': f'  {A_HASH}\r\n'}) == A_HASH

    def test_a_clean_hash_is_unchanged(self, app, db_session, monkeypatch):
        assert self._fetch(app, monkeypatch, {'pdq_hash_binary': A_HASH}) == A_HASH

    def test_a_hash_that_is_not_a_string_is_still_refused(self, app, db_session,
                                                          monkeypatch):
        """The `isinstance` check the strip had to be folded into rather than
        replace."""
        assert self._fetch(app, monkeypatch, {'pdq_hash_binary': 1010}) is None

    def test_a_stripped_hash_is_then_accepted_by_the_guard(self, app, db_session,
                                                           monkeypatch):
        """The two halves together, which is the point: what the endpoint returns now
        reaches Postgres as a valid bit string."""
        hash_value = self._fetch(app, monkeypatch,
                                 {'pdq_hash_binary': A_HASH + '\n'})

        assert BINARY_RE.fullmatch(hash_value) is not None
        assert hash_matches_blocked_image(hash_value) is False


# --------------------------------------------------------------------------
# The user name one
# --------------------------------------------------------------------------


class TestTheUserNameCharset:
    def _validated(self, app, form_cls, value):
        """Run the form's own `validate_user_name` hook and report the outcome."""
        from wtforms import ValidationError

        class _Field:
            def __init__(self, data):
                self.data = data

        field = _Field(value)
        form = form_cls.__new__(form_cls)
        try:
            form_cls.validate_user_name(form, field)
            return True, field.data
        except ValidationError as refusal:
            return False, str(refusal)

    def test_the_shared_helper_refuses_a_trailing_newline(self, app, db_session):
        from wtforms import ValidationError
        from app.utils import validate_user_name_charset

        class _Field:
            data = 'alice\n'

        with pytest.raises(ValidationError):
            validate_user_name_charset(_Field())

    def test_the_shared_helper_accepts_an_ordinary_name(self, app, db_session):
        from app.utils import validate_user_name_charset

        class _Field:
            data = 'alice_99'

        assert validate_user_name_charset(_Field()) is None

    def test_the_admin_form_strips_before_it_validates(self, app, db_session):
        """`app/admin/routes.py:2125` stores `form.user_name.data` verbatim, so
        whatever this hook leaves on the field is what reaches the column. Self-
        registration has always stripped; the admin path now does too, and the two
        create the same kind of row."""
        from app.admin.forms import AddUserForm

        accepted, data = self._validated(app, AddUserForm, '  alice  ')

        assert accepted is True
        assert data == 'alice'

    def test_the_admin_form_still_refuses_a_name_with_a_newline_inside_it(
            self, app, db_session):
        """Stripping handles the edges; the charset rule is what refuses a newline in
        the middle, and it must still do so."""
        from app.admin.forms import AddUserForm

        accepted, _ = self._validated(app, AddUserForm, 'al\nice')

        assert accepted is False


# --------------------------------------------------------------------------
# The sweep, pinned so a fourth site cannot appear unnoticed
# --------------------------------------------------------------------------


def test_no_anchored_pattern_in_app_is_checked_with_match():
    """`re.match` against a `$`-anchored pattern is the shape this round repaired, and
    the rule is easier to hold than the three sites.

    Read out of the AST rather than by grep so a compiled pattern assigned to a name is
    resolved to its literal. Prefix checks on UNANCHORED patterns are untouched -- they
    are what `match` is for -- which is why the test keys on the trailing `$` rather
    than on the method alone.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / 'app'
    offenders = []
    for path in sorted(root.glob('**/*.py')):
        tree = ast.parse(path.read_text())
        patterns = {}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Attribute)
                    and node.value.func.attr == 'compile'
                    and node.value.args
                    and isinstance(node.value.args[0], ast.Constant)):
                patterns[node.targets[0].id] = node.value.args[0].value
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == 'match'):
                continue
            base = ast.unparse(node.func.value)
            literal = None
            if base == 're' and node.args and isinstance(node.args[0], ast.Constant):
                literal = node.args[0].value
            pattern = patterns.get(base, literal)
            if isinstance(pattern, str) and pattern.endswith('$'):
                offenders.append(f'{path.name}:{node.lineno} {base} {pattern!r}')

    assert offenders == []


# --------------------------------------------------------------------------
# The community url charset, the third site
# --------------------------------------------------------------------------


class TestTheCommunityUrlCharset:
    """`AddCommunityForm.validate` refuses a url outside `[a-zA-Z0-9_]`, and the
    normalisation a few lines below slugifies whatever it accepts -- so the newline
    this round repaired never reached storage. The guard is still what the message
    claims only with `fullmatch`, and these rows are what stop it being deleted as
    redundant with the slugify.
    """

    def _validated(self, app, url):
        """`AddCommunityForm.validate` calls `super().validate()` first, so the two
        SelectFields the route populates -- `theme` from `community_theme_list()` and
        `languages` from `languages_for_form()` -- have to be given choices here or the
        form is `TypeError: Choices cannot be None` before the url is looked at."""
        from werkzeug.datastructures import MultiDict

        from app.community.forms import AddCommunityForm

        # Bound as FORMDATA, not by assigning `.data` afterwards. `AddCommunityForm.
        # validate` opens with `if not super().validate(): return False`, so a form
        # built with no formdata fails on `community_name`'s DataRequired and the url
        # checks below never run -- the refusal looks right and carries none of the
        # messages this class is about.
        data = MultiDict({'community_name': 'A Name', 'url': url,
                          'description': '', 'posting_warning': '',
                          'theme': '', 'invitations': '0'})
        with app.test_request_context('/', method='POST', data=data):
            form = AddCommunityForm(formdata=data)
            form.theme.choices = [('', 'None')]
            form.languages.choices = []
            accepted = form.validate()
            return accepted, [str(message) for message in form.url.errors]

    @pytest.mark.parametrize('url', ['books\n', 'books ', 'bo oks', 'books!',
                                     'books@host', 'boo/ks', ''])
    def test_a_url_outside_the_charset_is_refused(self, app, db_session, url):
        """`books\n` is the one this round repaired; the rest were already refused and
        are here because the guard's job is the whole set."""
        accepted, messages = self._validated(app, url)

        assert accepted is False
        assert messages != []

    def test_a_hyphen_gets_its_own_message(self, app, db_session):
        """Checked before the charset rule, so a submitter is told to use an
        underscore rather than being given the general complaint."""
        accepted, messages = self._validated(app, 'a-b')

        assert accepted is False
        assert any('Use _ instead' in message for message in messages)

    def test_an_ordinary_url_passes_the_charset_rule(self, app, db_session):
        """The control. The form may still refuse for another reason -- uniqueness,
        a missing field -- so this asserts the charset message is absent rather than
        that the whole form validated."""
        accepted, messages = self._validated(app, 'books_99')

        assert not any('letters, numbers, and underscores' in message
                       for message in messages)
