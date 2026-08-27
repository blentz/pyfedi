"""Five `validate()` overrides in app/community/forms.py threw away
`super().validate()`'s verdict, so no base-field validation -- CSRF included --
could reject a submission.

The shape of the defect:

    def validate(self, extra_validators=None) -> bool:
        super().validate(extra_validators)          # <-- boolean discarded
        ...
        return <this override's own check>

`super().validate()` is where WTForms runs every field's own validator chain:
`DataRequired` on `title` and `communities`, `Length`, `Regexp`, the
`validate_<field>` inline hooks -- and, because Flask-WTF installs `csrf_token`
as an ordinary field, the CSRF check. Running it and discarding the answer
populates `form.<field>.errors` for the template while telling the route the
form is fine. Routes gate on `form.validate_on_submit()`, which is
`is_submitted() and validate()`, so the route saw True.

**Why CSRF is the sharp end of it.** There is no `CSRFProtect` anywhere in
`app/` (`grep -rn 'CSRFProtect' app/` is empty), so nothing enforces CSRF at
the request level. The per-form `csrf_token` field validated inside
`super().validate()` was the only enforcement these five forms had, and its
verdict was thrown away. `TestCsrfIsEnforcedOnceTheResultIsGuarded` drives
that with CSRF genuinely switched on, a real Flask session and a real
Flask-WTF signed token -- both directions, so it discriminates a fix from a
form that rejects everything.

The two overrides in the same file that were already correct
(`AddCommunityForm`, `SearchRemoteCommunity`) guard the result:

    if not super().validate():
        return False

The fix makes all five match, keeping the `extra_validators` argument they
already passed -- dropping it would silently ignore caller-supplied
validators, which is a second defect, not a tidy-up.

**A deliberate behaviour change: errors no longer accumulate across the
boundary.** Before the fix a submission with both a blank title and, say, a
banned video domain collected both errors in one round trip. After it, the
title error is reported and the override's own check never runs, so the domain
error surfaces on resubmit. That is exactly how the two already-correct
overrides in this file behave, and how `CreateLinkForm` (whose check is an
inline `validate_link_url` hook, hence inside `super().validate()`) behaves;
`TestErrorsNoLongerAccumulateAcrossTheGuard` pins it so it is a recorded
decision rather than a surprise.

**Why every submission here is built complete.** Pre-fix, an incomplete
submission returns True for the wrong reason and a test asserting False would
pass against a fix that rejected everything. Each class below therefore either
varies exactly one thing away from a fully valid submission, or is paired with
the matching valid-submission test in `TestAValidSubmissionStillValidates`.
"""

from contextlib import contextmanager
from datetime import timedelta
from io import BytesIO

import pytest
from flask import g, request
from flask_wtf.csrf import generate_csrf
from werkzeug.datastructures import MultiDict

from app import db
from app.community.forms import (CreateEventForm, CreateImageForm, CreatePollForm, CreateVideoForm,
                                 EditImageForm)
from app.models import Language, Site, utcnow
from tests.factories import make_community, make_domain, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')

# get_timezones() drops every zone without a '/', so plain 'UTC' is not a
# selectable choice and the SelectField would reject it.
TZ = 'Europe/London'

# A one-pixel PNG is enough: the chan filter is off by default
# (Site.enable_chan_image_filter defaults to False), so nothing decodes it.
PNG_BYTES = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
             b'\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01'
             b'\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82')


@pytest.fixture
def post_env(db_session, site):
    """A community and a Language row, the two rows a CreatePostForm
    submission has to name before any of its own validators can pass.

    make_community hardcodes instance_id=1 and user_id=1, so the local
    instance and one user have to exist first or the insert fails a foreign
    key.
    """
    instance = make_instance('test.piefed.local', software='piefed')
    make_user(instance, 'formowner', local=True)
    community = make_community('formguard')
    language = Language(code='en', name='English')
    db.session.add(language)
    db.session.commit()
    return community, language


def base_data(post_env):
    """A complete, valid CreatePostForm submission.

    Every SelectField is supplied even where its validator is Optional():
    `pre_validate` runs ahead of the validator chain, so a SelectField left out
    of the form data has `data is None`, matches no choice, and fails with 'Not
    a valid choice' regardless of Optional.
    """
    community, language = post_env
    return {
        'communities': str(community.id),
        'title': 'An entirely ordinary post title',
        'language_id': str(language.id),
        'timezone': TZ,
        'repeat': 'none',
    }


def video_data(post_env, url='https://videoguard.example/watch'):
    return {**base_data(post_env), 'video_url': url}


def image_data(post_env, filename='photo.png', payload=None):
    return {**base_data(post_env),
            'image_file': (BytesIO(PNG_BYTES if payload is None else payload), filename)}


def event_data(post_env, **overrides):
    start = utcnow() + timedelta(days=1)
    end = start + timedelta(hours=2)
    data = {**base_data(post_env),
            'start_datetime': start.strftime('%Y-%m-%dT%H:%M'),
            'end_datetime': end.strftime('%Y-%m-%dT%H:%M'),
            'event_timezone': TZ,
            'join_mode': 'free',
            'max_attendees': '10',
            'irl_address': '1 Test Street',
            'irl_city': 'Testville',
            'irl_country': 'Testland'}
    data.update(overrides)
    return data


def poll_data(post_env, **overrides):
    data = {**base_data(post_env), 'mode': 'single', 'finish_in': '30m'}
    # CreatePollForm.validate reads choice_1..choice_9 unconditionally and
    # calls .strip() on each, so a field missing from the form data (data is
    # None) would raise AttributeError rather than validate.
    for i in range(1, 16):
        data[f'choice_{i}'] = ''
    data['choice_1'] = 'First option'
    data['choice_2'] = 'Second option'
    data.update(overrides)
    return data


# The five overrides under test, each with a builder for a fully valid
# submission to that form.
FORMS = {
    'CreateVideoForm': (CreateVideoForm, video_data),
    'CreateImageForm': (CreateImageForm, image_data),
    'EditImageForm': (EditImageForm, image_data),
    'CreateEventForm': (CreateEventForm, event_data),
    'CreatePollForm': (CreatePollForm, poll_data),
}


@contextmanager
def submitted(app, form_cls, data, post_env, formdata=None):
    """Bind `data` as a POST submission and yield the constructed form.

    The context stays open for the caller's `validate()` call: CreateImageForm
    and CreateEventForm read `request.files` inside their overrides.

    `g.site` is set by hand because `before_request`, which normally populates
    it for CreatePostForm.validate_nsfw, does not run for a bare
    test_request_context. The three choice lists are what the route sets from
    possible_communities() / languages_for_form() / flair_for_form().
    """
    community, language = post_env
    with app.test_request_context('/', method='POST', data=data):
        g.site = db.session.query(Site).get(1)
        form = form_cls() if formdata is None else form_cls(formdata=formdata)
        form.communities.choices = [(community.id, community.title)]
        form.language_id.choices = [(language.id, language.name)]
        form.flair.choices = []
        yield form


@contextmanager
def csrf_on(app):
    """Turn CSRF on for one test and put it back.

    TestConfig sets WTF_CSRF_ENABLED False and the `app` fixture is
    session-scoped, so leaking this would change every later test in the run.
    Flask-WTF reads the flag through `FlaskForm.Meta.csrf`, which consults
    `current_app.config` at form-construction time, so flipping it at runtime
    is enough -- the csrf_token field is added per instance.
    """
    original = app.config['WTF_CSRF_ENABLED']
    app.config['WTF_CSRF_ENABLED'] = True
    try:
        yield
    finally:
        app.config['WTF_CSRF_ENABLED'] = original


def errors_on(field):
    return [str(e) for e in field.errors]


class TestABaseValidatorFailureIsNoLongerDiscarded:
    """Test 1, the core case. One field -- `title`, DataRequired -- is blank;
    everything else is a valid submission.

    Pre-fix each of these returns True: `super().validate()` fails, records the
    error on `form.title`, and the override throws the answer away and reports
    its own check's verdict instead.
    """

    @pytest.mark.parametrize('name', list(FORMS))
    def test_a_blank_required_title_makes_the_form_invalid(self, name, app, post_env):
        form_cls, builder = FORMS[name]
        data = {**builder(post_env), 'title': ''}
        with submitted(app, form_cls, data, post_env) as form:
            valid = form.validate()
            title_errors = errors_on(form.title)
        assert valid is False, f'{name} validated a submission with no title'
        assert title_errors, f'{name} reported no error on the blank title: {title_errors}'

    @pytest.mark.parametrize('name', list(FORMS))
    def test_an_out_of_range_title_makes_the_form_invalid(self, name, app, post_env):
        """A second base validator, `Length(min=3, max=255)`, so the claim does
        not rest on DataRequired alone."""
        form_cls, builder = FORMS[name]
        data = {**builder(post_env), 'title': 'x' * 300}
        with submitted(app, form_cls, data, post_env) as form:
            valid = form.validate()
            title_errors = errors_on(form.title)
        assert valid is False, f'{name} validated a 300-character title'
        assert title_errors

    @pytest.mark.parametrize('name', list(FORMS))
    def test_a_community_that_is_not_a_choice_makes_the_form_invalid(self, name, app, post_env):
        """`communities` is the field the route reads to decide where the post
        goes, and SelectField.pre_validate is the only thing checking the
        submitted id is one the viewer was offered.

        Pre-fix, three of these five returned True and the two image forms
        RAISED instead: `CreateImageForm.validate` does
        `Community.query.get(self.communities.data).is_local()` with no None
        check, so an id naming no row was an AttributeError -- a 500 on
        client-controlled input rather than a rejection. The guard makes
        pre_validate refuse the id first, so that path is no longer reachable
        from a submission. The missing None check itself is untouched;
        reported, not fixed here.
        """
        form_cls, builder = FORMS[name]
        data = {**builder(post_env), 'communities': '999999'}
        with submitted(app, form_cls, data, post_env) as form:
            valid = form.validate()
            community_errors = errors_on(form.communities)
        assert valid is False, f'{name} validated an unoffered community id'
        assert community_errors


class TestCsrfIsEnforcedOnceTheResultIsGuarded:
    """Test 2. The live proof, and the reason this defect is a security fix
    rather than a tidy-up.

    Nothing else in `app/` enforces CSRF -- there is no `CSRFProtect`
    registration anywhere -- so the `csrf_token` field validated inside
    `super().validate()` was these forms' only protection, and its verdict was
    discarded.

    Both directions are asserted. A "fix" that returned False unconditionally
    would pass the missing-token test on its own; the valid-token test is what
    rejects it, and it uses a real signed token minted into the real Flask
    session by `generate_csrf`, not a stub.
    """

    @pytest.mark.parametrize('name', list(FORMS))
    def test_a_submission_with_no_csrf_token_is_refused(self, name, app, post_env):
        form_cls, builder = FORMS[name]
        with csrf_on(app):
            with submitted(app, form_cls, builder(post_env), post_env) as form:
                valid = form.validate()
                csrf_errors = errors_on(form.csrf_token)
        assert valid is False, f'{name} validated a submission carrying no CSRF token'
        assert any('CSRF' in e or 'token' in e.lower() for e in csrf_errors), \
            f'expected a CSRF complaint on {name}, got {csrf_errors}'

    @pytest.mark.parametrize('name', list(FORMS))
    def test_a_submission_carrying_a_valid_csrf_token_is_accepted(self, name, app, post_env):
        """The over-correction guard for the test above.

        `generate_csrf()` stores the raw token in the session and returns the
        signed form of it, so this is the same exchange a rendered page and a
        real browser perform. The form is rebuilt from `request.form` plus that
        token because the request body was already fixed when the context
        opened; `request.files` is untouched, which the image forms need.
        """
        form_cls, builder = FORMS[name]
        with csrf_on(app):
            with app.test_request_context('/', method='POST', data=builder(post_env)):
                g.site = db.session.query(Site).get(1)
                combined = MultiDict(request.form)
                combined['csrf_token'] = generate_csrf()
                # Flask-WTF only merges request.files into the formdata when it
                # supplies the formdata itself, so an explicitly-passed
                # MultiDict has to carry the uploads or FileField's
                # DataRequired reports "This field is required."
                for key in request.files:
                    combined.setlist(key, request.files.getlist(key))
                community, language = post_env
                form = form_cls(formdata=combined)
                form.communities.choices = [(community.id, community.title)]
                form.language_id.choices = [(language.id, language.name)]
                form.flair.choices = []
                valid = form.validate()
                all_errors = {k: [str(e) for e in v] for k, v in form.errors.items()}
        assert valid is True, f'{name} rejected a submission with a valid CSRF token: {all_errors}'


class TestAValidSubmissionStillValidates:
    """Test 3, the over-correction guard. A fix that early-returned too eagerly
    would satisfy tests 1 and 2 and break every post submission on the site."""

    @pytest.mark.parametrize('name', list(FORMS))
    def test_a_complete_submission_validates(self, name, app, post_env):
        form_cls, builder = FORMS[name]
        with submitted(app, form_cls, builder(post_env), post_env) as form:
            valid = form.validate()
            all_errors = {k: [str(e) for e in v] for k, v in form.errors.items()}
        assert valid is True, f'{name} rejected a valid submission: {all_errors}'


class TestEachOverridesOwnCheckStillWorks:
    """Test 4. The point of the override is its own check; guarding super()'s
    result must not cost it. Every submission here is valid apart from the one
    thing the override is supposed to catch.
    """

    def test_a_video_from_a_banned_domain_is_still_refused(self, app, post_env, db_session):
        domain = make_domain('bannedvideo.example')
        domain.banned = True
        db.session.commit()
        data = video_data(post_env, url='https://bannedvideo.example/watch')
        with submitted(app, CreateVideoForm, data, post_env) as form:
            valid = form.validate()
            url_errors = errors_on(form.video_url)
        assert valid is False
        assert any('are not allowed' in e for e in url_errors), \
            f'expected a banned-domain error, got {url_errors}'

    def test_an_unparseable_video_url_is_still_refused(self, app, post_env, db_session):
        data = video_data(post_env, url='https://youtube.com[abc')
        with submitted(app, CreateVideoForm, data, post_env) as form:
            valid = form.validate()
            url_errors = errors_on(form.video_url)
        assert valid is False
        assert any('could not be understood' in e for e in url_errors), \
            f'expected a parse error, got {url_errors}'

    def test_an_oversized_gif_is_still_refused(self, app, post_env, db_session):
        """CreateImageForm's own 10MB ceiling on .gif uploads."""
        oversized = b'GIF89a' + b'\x00' * (10 * 1024 * 1024)
        data = image_data(post_env, filename='big.gif', payload=oversized)
        with submitted(app, CreateImageForm, data, post_env) as form:
            valid = form.validate()
            file_errors = errors_on(form.image_file)
        assert valid is False
        assert any('filesize is too large' in e for e in file_errors), \
            f'expected a filesize error, got {file_errors}'

    def test_a_gif_under_the_ceiling_is_still_accepted(self, app, post_env, db_session):
        """The other direction, so the ceiling is not just 'reject every gif'."""
        data = image_data(post_env, filename='small.gif', payload=b'GIF89a' + b'\x00' * 1024)
        with submitted(app, CreateImageForm, data, post_env) as form:
            valid = form.validate()
            file_errors = errors_on(form.image_file)
        assert valid is True, f'a small gif was refused: {file_errors}'

    def test_an_online_event_with_no_link_is_still_refused(self, app, post_env, db_session):
        data = event_data(post_env, online='y', online_link='')
        with submitted(app, CreateEventForm, data, post_env) as form:
            valid = form.validate()
            link_errors = errors_on(form.online_link)
        assert valid is False
        assert any('Online link is required' in e for e in link_errors), \
            f'expected the online-link error, got {link_errors}'

    def test_a_physical_event_with_no_city_is_still_refused(self, app, post_env, db_session):
        data = event_data(post_env, irl_city='')
        with submitted(app, CreateEventForm, data, post_env) as form:
            valid = form.validate()
            city_errors = errors_on(form.irl_city)
        assert valid is False
        assert any('City is required' in e for e in city_errors), \
            f'expected the city error, got {city_errors}'

    def test_a_repeating_poll_is_still_refused(self, app, post_env, db_session):
        data = poll_data(post_env, repeat='daily')
        with submitted(app, CreatePollForm, data, post_env) as form:
            valid = form.validate()
            repeat_errors = errors_on(form.repeat)
        assert valid is False
        assert any("scheduled more than once" in e for e in repeat_errors), \
            f'expected the repeat error, got {repeat_errors}'

    def test_a_poll_with_one_choice_is_still_refused(self, app, post_env, db_session):
        data = poll_data(post_env, choice_2='')
        with submitted(app, CreatePollForm, data, post_env) as form:
            valid = form.validate()
            choice_errors = errors_on(form.choice_2)
        assert valid is False
        assert any('at least two choices' in e for e in choice_errors), \
            f'expected the two-choice error, got {choice_errors}'

    def test_a_poll_with_no_choices_is_still_refused(self, app, post_env, db_session):
        data = poll_data(post_env, choice_1='', choice_2='')
        with submitted(app, CreatePollForm, data, post_env) as form:
            valid = form.validate()
            choice_errors = errors_on(form.choice_1)
        assert valid is False
        assert any('need options' in e for e in choice_errors), \
            f'expected the no-choices error, got {choice_errors}'

    def test_the_image_forms_still_report_a_local_image_ban(self, app, post_env, db_session):
        """Both image overrides append an error when the destination community
        is local and the site forbids local image posts.

        Asserted on the error list, not on the return value, because both
        overrides append this error and then `return True` -- a separate,
        pre-existing defect (an invalid submission that reports valid, with a
        message the template will render). Reported, not fixed here; this test
        pins today's behaviour so the change is visible when it is fixed.
        """
        site = db.session.query(Site).get(1)
        site.allow_local_image_posts = False
        db.session.commit()
        for form_cls in (CreateImageForm, EditImageForm):
            with submitted(app, form_cls, image_data(post_env), post_env) as form:
                valid = form.validate()
                community_errors = errors_on(form.communities)
            assert any('cannot be posted to local communities' in e for e in community_errors), \
                f'{form_cls.__name__} lost the local-image check: {community_errors}'
            assert valid is True, \
                f'{form_cls.__name__} now REJECTS a local image post -- update this test'


class TestTheEventFormsUrlFieldsAreCheckedAtAll:
    """`CreateEventForm.validate_link_url` was dead code, and the two URL
    fields it was evidently meant to guard had no check of any kind.

    WTForms resolves an inline hook by FIELD NAME -- it looks for
    `validate_<field>` for each field it holds -- and CreateEventForm has no
    `link_url` field. So the hook was never looked up, never ran, and
    `more_info_url` and `online_link` went to the database with neither a
    domain-ban check nor a parseability check, while the same two checks
    guarded every other user-submitted URL on the site.

    It was rewired to those two fields rather than deleted. That is a
    behaviour change stated plainly: both fields gain validation they have
    never had, so an event naming a banned domain, or a URL urlparse cannot
    read, is now refused where it used to be stored.
    """

    def test_the_form_has_no_link_url_field(self, app, post_env):
        """The fact that made the old hook dead. If a `link_url` field is ever
        added, this fails and the hook should come back with it."""
        with submitted(app, CreateEventForm, event_data(post_env), post_env) as form:
            assert 'link_url' not in form._fields
            assert 'more_info_url' in form._fields and 'online_link' in form._fields

    def test_a_banned_domain_in_more_info_url_is_refused(self, app, post_env, db_session):
        domain = make_domain('bannedevent.example')
        domain.banned = True
        db.session.commit()
        data = event_data(post_env, more_info_url='https://bannedevent.example/details')
        with submitted(app, CreateEventForm, data, post_env) as form:
            valid = form.validate()
            errors = errors_on(form.more_info_url)
        assert valid is False
        assert any('are not allowed' in e for e in errors), \
            f'expected a banned-domain error on more_info_url, got {errors}'

    def test_a_banned_domain_in_online_link_is_refused(self, app, post_env, db_session):
        domain = make_domain('bannedevent.example')
        domain.banned = True
        db.session.commit()
        data = event_data(post_env, online='y',
                          online_link='https://bannedevent.example/call')
        with submitted(app, CreateEventForm, data, post_env) as form:
            valid = form.validate()
            errors = errors_on(form.online_link)
        assert valid is False
        assert any('are not allowed' in e for e in errors), \
            f'expected a banned-domain error on online_link, got {errors}'

    def test_an_unparseable_more_info_url_is_refused(self, app, post_env, db_session):
        data = event_data(post_env, more_info_url='https://youtube.com[abc')
        with submitted(app, CreateEventForm, data, post_env) as form:
            valid = form.validate()
            errors = errors_on(form.more_info_url)
        assert valid is False
        assert any('could not be understood' in e for e in errors), \
            f'expected a parse error on more_info_url, got {errors}'

    def test_an_unparseable_online_link_is_refused(self, app, post_env, db_session):
        data = event_data(post_env, online='y', online_link='https://youtube.com[abc')
        with submitted(app, CreateEventForm, data, post_env) as form:
            valid = form.validate()
            errors = errors_on(form.online_link)
        assert valid is False
        assert any('could not be understood' in e for e in errors), \
            f'expected a parse error on online_link, got {errors}'

    def test_ordinary_event_urls_are_still_accepted(self, app, post_env, db_session):
        """The over-correction guard: a check that refused every URL would pass
        all four tests above."""
        data = event_data(post_env, online='y',
                          more_info_url='https://example.com:8443/details',
                          online_link='https://user@example.com/call')
        with submitted(app, CreateEventForm, data, post_env) as form:
            valid = form.validate()
            all_errors = {k: [str(e) for e in v] for k, v in form.errors.items()}
        assert valid is True, f'ordinary event URLs were refused: {all_errors}'

    def test_leaving_both_url_fields_empty_is_still_accepted(self, app, post_env, db_session):
        """Both fields are Optional, and adding a check must not make either
        one required or report a blank field unparseable."""
        data = event_data(post_env, more_info_url='', online_link='')
        with submitted(app, CreateEventForm, data, post_env) as form:
            valid = form.validate()
            errors = errors_on(form.more_info_url) + errors_on(form.online_link)
        assert valid is True
        assert errors == []

    def test_an_unbanned_domain_in_more_info_url_is_accepted(self, app, post_env, db_session):
        """A Domain row that exists and is NOT banned still passes, so the ban
        check is not just 'refuse every domain we know about'."""
        make_domain('okevent.example')
        data = event_data(post_env, more_info_url='https://okevent.example/details')
        with submitted(app, CreateEventForm, data, post_env) as form:
            valid = form.validate()
            errors = errors_on(form.more_info_url)
        assert valid is True
        assert errors == []


class TestErrorsNoLongerAccumulateAcrossTheGuard:
    """The deliberate behaviour change, recorded rather than discovered.

    A submission that is wrong in two ways -- one base-field error, one the
    override would catch -- used to report both at once. It now reports the
    base-field error and stops, which is what the two already-correct
    overrides in this file have always done.
    """

    def test_only_the_base_error_is_reported_for_a_doubly_invalid_submission(
            self, app, post_env, db_session):
        domain = make_domain('bannedvideo.example')
        domain.banned = True
        db.session.commit()
        data = video_data(post_env, url='https://bannedvideo.example/watch')
        data['title'] = ''
        with submitted(app, CreateVideoForm, data, post_env) as form:
            valid = form.validate()
            title_errors = errors_on(form.title)
            url_errors = errors_on(form.video_url)
        assert valid is False
        assert title_errors, 'the base-field error must still be reported'
        assert url_errors == [], \
            f'the override check should not have run after the guard, got {url_errors}'

    def test_the_domain_error_appears_once_the_base_error_is_corrected(
            self, app, post_env, db_session):
        """The resubmit half of the same round trip, so 'you see it on the
        second submission' is asserted rather than assumed."""
        domain = make_domain('bannedvideo.example')
        domain.banned = True
        db.session.commit()
        data = video_data(post_env, url='https://bannedvideo.example/watch')
        with submitted(app, CreateVideoForm, data, post_env) as form:
            valid = form.validate()
            url_errors = errors_on(form.video_url)
        assert valid is False
        assert any('are not allowed' in e for e in url_errors)
