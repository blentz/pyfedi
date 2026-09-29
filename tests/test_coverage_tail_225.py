"""Round 225: `app/community/forms.py`, the last eight lines of the coverage tail.

Round 224 took the tail's small files; this is the largest remaining entry at 96.69%.
Eight lines, and as in 224 they are not all reachable -- each row below states which it is
and why, because a refusal that cannot happen and one that nobody tested look identical in
a coverage report.

    53        `return False` when `super().validate()` refuses      reachable
    110, 111  a local FEED already holds the requested name        reachable
    706       'Maximum of 50 at a time.'                           reachable
    397       `site = Site()` when there is no Site row            reachable
    403       `import pillow_avif` for an .avif upload             reachable
    419, 553  `self.image_file.errors = [message]`                 UNREACHABLE (see below)

THE TWO UNREACHABLE ONES ARE THE SAME LINE TWICE, in `CreateImageForm.validate` and
`CreateEventForm.validate`:

    if not isinstance(self.image_file.errors, list):
        self.image_file.errors = [error_message]
    else:
        self.image_file.errors.append(error_message)

Both functions open with `if not super().validate(...): return False`, and WTForms sets
every field's `errors` to a LIST during that call -- it is a tuple only before validation
has run. So by the time either line is reached the `else` is the only branch, and the
`isinstance` arm is dead. It is documented rather than deleted: the evidence is a WTForms
implementation detail rather than a schema constraint, and the same caution round 224
applied to four arms applies here.
"""
import io

import pytest
from werkzeug.datastructures import MultiDict

from flask import g

from app import db
from app.models import Feed, Site
from tests.factories import make_community, make_instance, make_site, make_user

HOST = 'test.piefed.local'


def _png():
    return io.BytesIO(
        b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06'
        b'\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00'
        b'\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82')


# --------------------------------------------------------------------------
# AddCommunityForm.validate: 53, and 110-111
# --------------------------------------------------------------------------


class TestAddCommunityForm:
    """`AddCommunityForm.validate` opens `if not super().validate(): return False` (:53)
    and ends with three name-collision checks, the last of which asks whether a local FEED
    already holds the name (:110-111).

    Every row binds FORMDATA rather than assigning `.data` afterwards, for the reason
    tests/test_anchored_validators.py records: with no formdata the form fails on
    `community_name`'s DataRequired and the url checks never run, so the refusal looks
    right and carries none of the messages under test (fact 898).
    """

    def _form(self, app, url='books', community_name='A Name'):
        from app.community.forms import AddCommunityForm

        data = MultiDict({'community_name': community_name, 'url': url,
                          'description': '', 'posting_warning': '', 'theme': '',
                          'invitations': '0'})
        with app.test_request_context('/', method='POST', data=data):
            form = AddCommunityForm(formdata=data)
            form.theme.choices = [('', 'None')]
            form.languages.choices = []
            accepted = form.validate()
            return accepted, [str(m) for m in form.url.errors]

    def test_a_form_its_own_validators_refuse_never_reaches_the_name_checks(self, app,
                                                                           db_session):
        """:53. `community_name` is DataRequired, so an empty one refuses in
        `super().validate()` and the function returns before any query runs -- which is
        why every other row here supplies a name."""
        accepted, errors = self._form(app, community_name='')

        assert accepted is False
        assert errors == [], 'the url checks ran, so :53 did not return first'

    def test_a_name_a_local_feed_already_holds_is_refused(self, app, db_session):
        """:110-111, the third of the three collision checks and the only one nothing
        reached. `Feed.ap_id == None` restricts it to LOCAL feeds, so the row is seeded
        that way."""
        make_site()
        instance = make_instance(HOST, software='piefed')
        user = make_user(instance, 'feedowner', local=True)
        feed = Feed(name='books', title='Books', user_id=user.id, machine_name='books',
                    ap_id=None)
        db.session.add(feed)
        db.session.commit()

        accepted, errors = self._form(app, url='books')

        assert accepted is False
        assert errors == ['This name is in use already.']

    def test_a_name_no_feed_holds_is_accepted(self, app, db_session):
        """The control: the check is not refusing every name."""
        make_site()
        instance = make_instance(HOST, software='piefed')
        make_user(instance, 'someone', local=True)
        db.session.commit()

        accepted, errors = self._form(app, url='unusedname')

        assert accepted is True, errors

    def test_a_remote_feed_with_the_same_name_does_not_collide(self, app, db_session):
        """`Feed.ap_id == None` is load-bearing: a REMOTE feed called 'books' is a
        different object and must not block a local community of that name."""
        make_site()
        instance = make_instance('peer.example')
        user = make_user(instance, 'remoteowner')
        feed = Feed(name='books', title='Books', user_id=user.id, machine_name='books',
                    ap_id='https://peer.example/f/books')
        db.session.add(feed)
        db.session.commit()

        accepted, errors = self._form(app, url='books')

        assert accepted is True, errors


# --------------------------------------------------------------------------
# InviteCommunityForm.validate_to: 706
# --------------------------------------------------------------------------


class TestTheInvitationCap:
    """`validate_to` refuses more than `MAX_INVITATIONS` (20) recipients at :700, and then
    refuses more than 50 LINES at :706.

    The second is reachable only because the two count different things: `recipients`
    drops blank lines, `lines` does not. So a paste of sixty newlines with three addresses
    in it is three recipients and sixty lines -- under the first cap, over the second.
    """

    def _validate(self, app, text):
        from app.community.forms import InviteCommunityForm

        with app.test_request_context('/'):
            form = InviteCommunityForm()
            field = form.to
            field.data = text
            return form.validate_to(field)

    def test_sixty_blank_lines_around_three_addresses_is_refused(self, app):
        """:706. Three recipients is under MAX_INVITATIONS, so :700 does not fire."""
        from wtforms.validators import ValidationError

        text = '\n'.join(['a@example.test', '', '', 'b@example.test'] + [''] * 55
                         + ['c@example.test'])
        assert len([line for line in text.split('\n') if line.strip()]) == 3
        assert len(text.split('\n')) > 50

        with pytest.raises(ValidationError, match='Maximum of 50 at a time'):
            self._validate(app, text)

    def test_more_than_twenty_addresses_is_refused_by_the_earlier_cap(self, app):
        """:700, the cap that fires first. Named here so the row above is known to be
        reaching the LATER one rather than this."""
        from wtforms.validators import ValidationError

        text = '\n'.join(f'user{n}@example.test' for n in range(25))

        with pytest.raises(ValidationError, match='no more than 20 people'):
            self._validate(app, text)

    def test_a_short_list_is_accepted(self, app):
        assert self._validate(app, 'a@example.test\nb@example.test') is None


# --------------------------------------------------------------------------
# CreateImageForm.validate: 397 and 403
# --------------------------------------------------------------------------


@pytest.fixture
def image_env(app, db_session):
    from types import SimpleNamespace

    instance = make_instance(HOST, software='piefed')
    user = make_user(instance, 'imager', local=True)
    community = make_community('probeland')
    db.session.commit()
    return SimpleNamespace(app=app, user=user, community=community, instance=instance)


def _image_form(env, filename='photo.png', content=None, **overrides):
    from app.community.forms import CreateImageForm

    data = {'title': 'a title', 'body': '', 'language_id': '1', 'timezone': 'UTC',
            'communities': str(env.community.id)}
    data.update(overrides)
    data['image_file'] = (content or _png(), filename)
    with env.app.test_request_context('/', method='POST', data=data,
                                      content_type='multipart/form-data'):
        # `validate_nsfw` reads `g.site.enable_nsfw`, and `before_request` -- which
        # populates g.site in the real app -- does not run inside a test_request_context.
        # An UNSAVED Site() when there is no row, which is the state :397 is about: g.site
        # and the row the form looks up are separate reads.
        g.site = db.session.get(Site, 1) or Site()
        form = CreateImageForm()
        form.language_id.choices = [(1, 'English')]
        form.timezone.choices = [('UTC', 'UTC')]
        form.communities.choices = [(env.community.id, 'probeland')]
        return form, form.validate()


def test_an_upload_with_no_site_row_uses_a_blank_site(image_env):
    """:397. The form reads `Site` id 1 to find `enable_chan_image_filter`, and falls back
    to an unsaved `Site()` when there is none -- whose column default leaves the filter
    off. Reached by seeding no Site at all, which the `image_env` fixture deliberately
    does not.

    Worth having rather than dismissing as impossible: `make_site()` is opt-in across this
    suite (tests/conftest.py's `site` fixture is not autouse), and a fresh install reaches
    this code before the Site row is created.
    """
    assert db.session.get(Site, 1) is None

    form, valid = _image_form(image_env)

    assert valid is True


class TestTheChanImageFilter:
    """D1415. Turning `enable_chan_image_filter` on runs the uploaded image through
    `pytesseract.image_to_string`, and the block caught `FileNotFoundError` and
    `UnidentifiedImageError` only.

    `TesseractNotFoundError` -- what pytesseract raises when the tesseract BINARY is
    missing, which is the ordinary state of a machine that installed this application's
    Python dependencies and nothing else -- is an `OSError` but NOT a `FileNotFoundError`,
    so neither arm caught it. Every image upload raised out of form validation until the
    setting was turned off again.

    This test image has no tesseract, which is why these rows can drive the defect
    directly rather than doubling anything.
    """

    @pytest.fixture
    def filtering(self, image_env):
        site = make_site()
        site.enable_chan_image_filter = True
        db.session.commit()
        return image_env

    def test_a_png_upload_survives_a_missing_tesseract(self, filtering):
        """The defect: before the fix this raised `TesseractNotFoundError` out of
        `form.validate()`."""
        form, valid = _image_form(filtering)

        assert valid is True

    def test_an_avif_upload_survives_it_too(self, filtering):
        """:403, the `import pillow_avif` line, which only an `.avif` filename reaches --
        inside the same `try:`, so it shares the arm above."""
        form, valid = _image_form(filtering, filename='photo.avif')

        assert valid is True

    def test_the_filter_is_off_by_default_which_is_why_nobody_hit_this(self, image_env):
        """`enable_chan_image_filter` is off unless an admin turns it on, so the whole
        block -- and the defect -- was invisible to every instance that left it alone."""
        site = make_site()
        db.session.commit()

        assert not site.enable_chan_image_filter
        form, valid = _image_form(image_env)
        assert valid is True

    def test_the_arm_answers_the_empty_string_the_others_answer(self):
        """What the new arm does, read out of the source: the same `image_text = ''` the
        other two give, so the filter degrades to OFF rather than to broken -- an empty
        string carries no chan markers and the check below it passes the image."""
        import pathlib

        source = (pathlib.Path(__file__).resolve().parent.parent
                  / 'app' / 'community' / 'forms.py').read_text()
        block = source[source.index('if site.enable_chan_image_filter:'):]
        block = block[:block.index("if 'Anonymous' in image_text")]

        assert block.count("image_text = ''") == 3
        assert 'except pytesseract.TesseractNotFoundError:' in block


# --------------------------------------------------------------------------
# The two that cannot be reached
# --------------------------------------------------------------------------


def test_a_fields_errors_are_a_list_once_validate_has_run(app, db_session):
    """Why `app/community/forms.py:419` and `:553` are dead.

    Both are `if not isinstance(self.image_file.errors, list): self.image_file.errors =
    [error_message]`, and both sit in a `validate` that opens with
    `if not super().validate(...): return False`. WTForms replaces each field's `errors`
    with a list during that call, so the isinstance test is False by the time either line
    is reached and only the `else` can run.

    This row pins the WTForms behaviour rather than the line: if a future version leaves
    `errors` a tuple after validation, it fails here and both arms become live again.
    """
    from app.community.forms import CreateImageForm

    data = {'title': '', 'body': '', 'language_id': '1', 'timezone': 'UTC',
            'communities': ''}
    with app.test_request_context('/', method='POST', data=data,
                                  content_type='multipart/form-data'):
        g.site = db.session.get(Site, 1) or Site()
        form = CreateImageForm()
        form.language_id.choices = [(1, 'English')]
        form.timezone.choices = [('UTC', 'UTC')]
        form.communities.choices = []
        form.validate()

        assert isinstance(form.image_file.errors, list)
        assert isinstance(form.title.errors, list)
