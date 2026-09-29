"""`CaptchaField`: the widget that mints a registration captcha and the hook that enforces it.

`create_captcha` and `decode_captcha` are covered (tests/test_utils_redis.py,
tests/test_fixture_proofs.py). The FIELD between them was not, and it is the field
that decides whether a registration is refused:

    def post_validate(self, form, validation_stopped):
        if decode_captcha(request.form.get('captcha_uuid', None), self.data):
            pass
        else:
            raise ValidationError(_l('Wrong Captcha text.'))

Round 204 (D1398) covered whether the captcha is REQUIRED -- `RegistrationForm.__init__`
deletes the field when `captcha_enabled` is off, and the default disagreed with the
admin page. Nothing covered whether a wrong answer is refused, which is the other half
and the half an attacker cares about.

FOUR THINGS ABOUT `post_validate` WORTH PINNING, each measured rather than assumed:

* it runs even when another field has already failed. WTForms calls it inside
  `Field.validate()` regardless of `stop_validation`, and this override ignores its
  `validation_stopped` argument -- so a submission with a bad password AND a bad
  captcha is refused for both, and a solved captcha is CONSUMED either way;
* the uuid comes from `request.form`, not from the field, so a submission naming no
  `captcha_uuid` is refused rather than raising -- `decode_captcha` takes None and
  answers False through its own `except TypeError`;
* the comparison is case-insensitive and the code is single-use, both of which live in
  `decode_captcha` and are asserted here through the field so the two cannot drift;
* `ValidationError` is what a WTForms hook signals with. Round 207's D1400 was the
  opposite mistake in the same codebase -- appending to `field.errors` and returning
  True -- so the mechanism is asserted, not just the message.

THE WIDGET mints a fresh captcha on every render and rewrites `self.data` to `''`.
That second part is why a re-rendered form never shows the answer the submitter typed:
it is a deliberate clearing, not a bug, and a mutant removing it is caught below.

HARNESS. `redis_double` is required: `create_captcha` and `decode_captcha` both reach
Redis, and without the double they reach the real, shared, never-truncated instance in
the compose stack (the fixture's own docstring explains which four bindings it
patches). `decode_captcha` uses GETDEL, so every solved captcha in this file is spent.
"""
import re

import pytest
from flask import g
from markupsafe import Markup
from wtforms import Form, StringField
from wtforms.validators import DataRequired, ValidationError

from app import db
from app.models import Site
from app.utils import CaptchaField, create_captcha

# The `site` fixture puts Site id 1 in the database; `env` below reads it.
pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def env(app, db_session):
    """A public instance, because `RegistrationForm` reads `g.site` when built."""
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.site = site
    g.admin_ids = []
    db.session.commit()
    return app


class _CaptchaOnly(Form):
    """The field on a bare WTForms form, so the assertions are about the field.

    `RegistrationForm` carries nine other fields with their own validators; a refusal
    seen through it could come from any of them. This is a `wtforms.Form` rather than
    a `FlaskForm` so that CSRF plays no part either.
    """
    captcha = CaptchaField('Enter captcha code below', validators=[DataRequired()])


def _solved(app, redis_double, answer=None, uuid=None, extra=None):
    """Submit a captcha answer and return (validated, errors).

    A real captcha is minted first, and its code read back out of the double -- the
    code is not returned by `create_captcha`, so the stored value is the only way a
    test can know the right answer (the same reasoning tests/test_utils_redis.py
    records for its own length assertion).
    """
    captcha = create_captcha()
    code = redis_double.get('captcha_' + captcha['uuid'])
    if isinstance(code, bytes):
        code = code.decode()

    data = {'captcha': code if answer is None else answer}
    if uuid is not False:
        data['captcha_uuid'] = captcha['uuid'] if uuid is None else uuid
    if extra:
        data.update(extra)

    with app.test_request_context('/', method='POST', data=data):
        from werkzeug.datastructures import MultiDict
        form = _CaptchaOnly(formdata=MultiDict(data))
        validated = form.validate()
        return validated, list(form.captcha.errors), code, captcha['uuid']


# --------------------------------------------------------------------------
# The enforcement
# --------------------------------------------------------------------------


class TestWhatIsAccepted:
    def test_the_right_answer_validates(self, env, redis_double):
        """The control. Every refusal below is measured against this one."""
        validated, errors, _, _ = _solved(env, redis_double)

        assert validated is True, errors
        assert errors == []

    def test_a_wrong_answer_is_refused(self, env, redis_double):
        validated, errors, code, _ = _solved(env, redis_double, answer='zzzz')

        assert validated is False
        assert 'Wrong Captcha text.' in errors

    def test_the_answer_is_case_insensitive(self, env, redis_double):
        """`code.lower() == saved_code.lower()` in `decode_captcha`. Asserted through
        the field so the two cannot drift apart -- the generated code is digits today,
        so the transform is exercised rather than merely present."""
        captcha = create_captcha()
        stored = redis_double.get('captcha_' + captcha['uuid'])
        code = stored.decode() if isinstance(stored, bytes) else stored

        with env.test_request_context(
                '/', method='POST',
                data={'captcha': code.upper(), 'captcha_uuid': captcha['uuid']}):
            from werkzeug.datastructures import MultiDict
            form = _CaptchaOnly(formdata=MultiDict(
                {'captcha': code.upper(), 'captcha_uuid': captcha['uuid']}))
            assert form.validate() is True, form.captcha.errors

    def test_a_solved_captcha_cannot_be_used_twice(self, env, redis_double):
        """`decode_captcha` reads with GETDEL, so the code is consumed. The field is
        where that matters: without it one solved captcha would open an unlimited
        number of registrations."""
        captcha = create_captcha()
        stored = redis_double.get('captcha_' + captcha['uuid'])
        code = stored.decode() if isinstance(stored, bytes) else stored
        data = {'captcha': code, 'captcha_uuid': captcha['uuid']}

        from werkzeug.datastructures import MultiDict
        with env.test_request_context('/', method='POST', data=data):
            first = _CaptchaOnly(formdata=MultiDict(data)).validate()
        with env.test_request_context('/', method='POST', data=data):
            second_form = _CaptchaOnly(formdata=MultiDict(data))
            second = second_form.validate()

        assert first is True
        assert second is False
        assert 'Wrong Captcha text.' in second_form.captcha.errors

    def test_a_uuid_nobody_issued_is_refused(self, env, redis_double):
        validated, errors, _, _ = _solved(env, redis_double,
                                          uuid='a' * 24)

        assert validated is False
        assert 'Wrong Captcha text.' in errors

    @pytest.mark.parametrize('uuid', ['', 'not-a-uuid', 'a' * 23, 'a' * 25,
                                      'A' * 24 + 'b', 'zzzz' * 6])
    def test_a_uuid_that_is_not_one_is_refused(self, env, redis_double, uuid):
        """`decode_captcha`'s `^([a-fA-F0-9]{24})$` refuses these before it touches
        Redis, which is what stops a crafted key reaching `getdel`."""
        validated, errors, _, _ = _solved(env, redis_double, uuid=uuid)

        assert validated is False
        assert 'Wrong Captcha text.' in errors

    def test_a_submission_with_no_uuid_at_all_is_refused(self, env, redis_double):
        """`request.form.get('captcha_uuid', None)` -- the field does not carry the
        uuid, the form does, so a submission omitting it hands `decode_captcha` None.
        Refused through its `except TypeError`, not raised."""
        validated, errors, _, _ = _solved(env, redis_double, uuid=False)

        assert validated is False
        assert 'Wrong Captcha text.' in errors


# --------------------------------------------------------------------------
# How it signals, which is the part D1400 got wrong elsewhere
# --------------------------------------------------------------------------


class TestHowItRefuses:
    def test_it_raises_validation_error_rather_than_appending(self, env,
                                                              redis_double):
        """The mechanism, not the message. A WTForms hook signals by RAISING;
        appending to `field.errors` and returning would leave `validate()` True --
        which is exactly the defect D1400 repaired in `CreateEventForm`.

        Called directly so the raise is observable: `Field.validate` catches it and
        turns it into an entry in `errors`, which is why the rows above see a message
        rather than an exception.
        """
        captcha = create_captcha()
        from werkzeug.datastructures import MultiDict
        data = {'captcha': 'zzzz', 'captcha_uuid': captcha['uuid']}

        with env.test_request_context('/', method='POST', data=data):
            form = _CaptchaOnly(formdata=MultiDict(data))
            with pytest.raises(ValidationError) as refusal:
                form.captcha.post_validate(form, False)

        assert 'Wrong Captcha text.' in str(refusal.value)

    def test_it_runs_even_when_validation_has_already_stopped(self, env,
                                                             redis_double):
        """`validation_stopped` is accepted and ignored. WTForms calls
        `post_validate` inside `Field.validate()` whatever the validator chain did, so
        a submission that fails DataRequired is still refused for the captcha too --
        and a solved captcha is still consumed.

        The safe direction, and worth pinning: an override that returned early on
        `validation_stopped` would let a caller skip the captcha by deliberately
        failing another field.
        """
        captcha = create_captcha()
        from werkzeug.datastructures import MultiDict
        data = {'captcha': '', 'captcha_uuid': captcha['uuid']}

        with env.test_request_context('/', method='POST', data=data):
            form = _CaptchaOnly(formdata=MultiDict(data))
            validated = form.validate()
            messages = list(form.captcha.errors)

        assert validated is False
        # DataRequired refused it, and post_validate refused it as well.
        assert any('required' in message.lower() for message in messages)
        assert 'Wrong Captcha text.' in messages

    def test_an_empty_answer_with_a_good_uuid_is_still_refused(self, env,
                                                              redis_double):
        """The same shape through `validate()` rather than the hook, since an empty
        answer is the one a bot omitting the field produces."""
        validated, errors, _, _ = _solved(env, redis_double, answer='')

        assert validated is False
        assert 'Wrong Captcha text.' in errors


# --------------------------------------------------------------------------
# The widget
# --------------------------------------------------------------------------


class TestTheWidget:
    def _rendered(self, env):
        from werkzeug.datastructures import MultiDict
        with env.test_request_context('/'):
            form = _CaptchaOnly(formdata=MultiDict({}))
            return form.captcha()

    def test_it_renders_the_uuid_the_image_and_the_audio(self, env, redis_double):
        html = self._rendered(env)

        assert 'name="captcha_uuid"' in html
        assert re.search(r'name="captcha_uuid" value="[0-9a-f]{24}"', html)
        assert 'src="data:image/jpeg;base64,' in html
        assert 'src="data:audio/wav;base64,' in html

    def test_it_renders_the_text_input_too(self, env, redis_double):
        """`super().__call__()` is appended, so the field a submitter types into is
        part of the same markup."""
        html = self._rendered(env)

        assert 'name="captcha"' in html

    def test_the_result_is_markup_and_not_escaped_into_text(self, env,
                                                            redis_double):
        """`Markup(...)` -- the template renders this with no `|safe`, so a plain str
        would arrive as visible angle brackets and no captcha at all."""
        html = self._rendered(env)

        assert isinstance(html, Markup)
        assert '&lt;input' not in html

    def test_every_render_mints_a_new_captcha(self, env, redis_double):
        """Two renders, two uuids, two stored codes. A widget reusing one would make
        the captcha replayable for as long as the page stayed open."""
        first = self._rendered(env)
        second = self._rendered(env)

        uuids = [re.search(r'name="captcha_uuid" value="([0-9a-f]{24})"', html)
                 .group(1) for html in (first, second)]
        assert uuids[0] != uuids[1]
        for uuid in uuids:
            assert redis_double.get('captcha_' + uuid) is not None

    def test_rendering_clears_whatever_was_typed(self, env, redis_double):
        """`self.data = ''` is the first statement. A re-rendered form must not show
        the previous answer beside a NEW captcha image, which would read as though the
        answer still applied.
        """
        from werkzeug.datastructures import MultiDict
        data = {'captcha': 'whatever', 'captcha_uuid': 'a' * 24}

        with env.test_request_context('/', method='POST', data=data):
            form = _CaptchaOnly(formdata=MultiDict(data))
            assert form.captcha.data == 'whatever'
            form.captcha()
            assert form.captcha.data == ''

    def test_the_field_is_a_string_field_with_a_text_input(self, env):
        """What it inherits, asserted because the class body is two lines and both
        matter: a `StringField` so the answer arrives as text, and `TextInput` so the
        browser renders a box rather than the default for some other type."""
        from wtforms.widgets import TextInput

        assert issubclass(CaptchaField, StringField)
        assert isinstance(CaptchaField.widget, TextInput)
