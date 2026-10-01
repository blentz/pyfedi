"""`CreateEventForm.validate`: what an event submission is refused for.

Fourteen statements of the last gate between an event form and a database write had
no test. It decides four things, and each has both arms here:

* whether the start and end are in the past, and in the right order -- compared in
  **UTC**, after being read in the submitter's own timezone, which is the part a
  naive comparison gets wrong twice a year;
* whether an ONLINE event carries a link, or a PHYSICAL one an address, a city and a
  country;
* whether an uploaded banner is under 10 MB;
* whether images may be posted to a local community at all -- D1001's third site.

**The size cap was general here and GIF-only on image posts** -- R207, since fixed:
both forms now share one check capping every image upload at 10 MB, and
`MAX_CONTENT_LENGTH` refuses an oversized body before any form sees it.

**`if self.communities:` is a truthiness test on the FIELD OBJECT**, which is always
truthy -- a guard that cannot discriminate (fact 75 CAUSE 9). It appears in both
copies of the block. It is harmless: `communities` carries `DataRequired()` and
`super().validate()` has already run, so `.data` is a valid choice by the time the
line is reached, and `db.session.get` cannot answer None. Pinned as behaviour rather
than repaired, because changing it to `self.communities.data` changes nothing and
removing it changes nothing either.

HARNESS. Bound as formdata inside a request context rather than driven through a
route, for the reason tests/test_forms_validation.py gives: the route drags in
everything other files already cover and what is under test is the refusal. The
`submitted` helper is this file's own copy of the one in
tests/test_form_validate_guards_super.py, kept local because it sets the three choice
lists a route would set and two files needing the same three lines is not duplication
worth sharing.
"""
import io
from contextlib import contextmanager
from datetime import timedelta

import pytest
from flask import g

from app import db
from app.community.forms import CreateEventForm, CreateImageForm
from app.models import Language, Site, utcnow
from tests.factories import make_community, make_instance, make_user

TZ = 'Europe/London'


@pytest.fixture
def post_env(db_session, site):
    """A community and a Language, the two rows any CreatePostForm submission must
    name before its own validators can run."""
    instance = make_instance('test.piefed.local', software='piefed')
    make_user(instance, 'formowner', local=True)
    community = make_community('eventguard')
    language = Language(code='en', name='English')
    db.session.add(language)
    db.session.commit()
    return community, language


def _event_data(post_env, **overrides):
    community, language = post_env
    start = utcnow() + timedelta(days=1)
    end = start + timedelta(hours=2)
    data = {'communities': str(community.id), 'language_id': str(language.id),
            'title': 'A test event', 'body': '', 'tags': '',
            'timezone': TZ, 'repeat': 'none',
            'start_datetime': start.strftime('%Y-%m-%dT%H:%M'),
            'end_datetime': end.strftime('%Y-%m-%dT%H:%M'),
            'event_timezone': TZ, 'join_mode': 'free', 'max_attendees': '10',
            'irl_address': '1 Test Street', 'irl_city': 'Testville',
            'irl_country': 'Testland'}
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not None}


@contextmanager
def submitted(app, post_env, form_cls=CreateEventForm, **overrides):
    """Bind an event submission and yield the form, with the context still open.

    `validate` reads `request.files`, so the context cannot close before the caller
    calls it. `g.site` is set by hand because `before_request` does not run for a
    bare test_request_context and `CreatePostForm.validate_nsfw` reads it.
    """
    community, language = post_env
    files = overrides.pop('files', None)
    data = _event_data(post_env, **overrides)
    if files:
        data.update(files)
    with app.test_request_context('/', method='POST', data=data):
        g.site = db.session.get(Site, 1)
        form = form_cls()
        form.communities.choices = [(community.id, community.title)]
        form.language_id.choices = [(language.id, language.name)]
        form.flair.choices = []
        yield form


def _errors(form):
    """Every message the form produced, flattened -- a refusal that explains nothing
    leaves the submitter with a form that will not submit and will not say why."""
    return [message for field in form for message in (field.errors or [])]


# --------------------------------------------------------------------------
# A valid submission
# --------------------------------------------------------------------------


def test_a_complete_physical_event_validates(app, post_env):
    """The control every refusal below is measured against. Without it, a validator
    that refused everything would pass the whole file."""
    with submitted(app, post_env) as form:
        assert form.validate() is True, _errors(form)


def test_a_complete_online_event_validates(app, post_env):
    """The other arm of the online/physical split, with no address at all -- which
    the physical arm would refuse."""
    with submitted(app, post_env, online='y',
                   online_link='https://meet.example/room',
                   irl_address=None, irl_city=None, irl_country=None) as form:
        assert form.validate() is True, _errors(form)


# --------------------------------------------------------------------------
# The times
# --------------------------------------------------------------------------


class TestTheTimes:
    def test_a_start_in_the_past_is_refused(self, app, post_env):
        past = utcnow() - timedelta(days=2)
        end = utcnow() + timedelta(days=1)

        with submitted(app, post_env,
                       start_datetime=past.strftime('%Y-%m-%dT%H:%M'),
                       end_datetime=end.strftime('%Y-%m-%dT%H:%M')) as form:
            valid = form.validate()
            messages = form.start_datetime.errors

        assert valid is False
        assert 'This time is in the past.' in messages

    def test_an_end_in_the_past_is_refused(self, app, post_env):
        """Both timestamps are checked, and against `utcnow()` rather than each
        other -- an event that ended yesterday is refused even if its start is
        earlier still."""
        start = utcnow() - timedelta(days=3)
        end = utcnow() - timedelta(days=2)

        with submitted(app, post_env,
                       start_datetime=start.strftime('%Y-%m-%dT%H:%M'),
                       end_datetime=end.strftime('%Y-%m-%dT%H:%M')) as form:
            valid = form.validate()
            messages = form.end_datetime.errors

        assert valid is False
        assert 'This time is in the past.' in messages

    def test_the_end_check_can_never_be_the_only_reason(self, app, post_env):
        """AN EQUIVALENT MUTANT, proved rather than left as a survivor.

        Removing the refusal from the `utc_end < utcnow()` branch alone changes no
        answer, and no input can make it: if the end is in the past then either

        * the start is at or before it, so the start is in the past too and that
          branch refuses; or
        * the start is after it, so the ordering branch refuses.

        Both shapes are submitted here and both are refused, with the end's own
        message present either way -- the message is on a different line from the
        refusal and is what tells the submitter WHICH field is wrong, so it is worth
        keeping even though the refusal beside it is redundant.
        """
        earlier_start = utcnow() - timedelta(days=3)
        past_end = utcnow() - timedelta(days=2)
        later_start = utcnow() + timedelta(days=1)

        with submitted(app, post_env,
                       start_datetime=earlier_start.strftime('%Y-%m-%dT%H:%M'),
                       end_datetime=past_end.strftime('%Y-%m-%dT%H:%M')) as form:
            both_past = form.validate()
            both_past_end = form.end_datetime.errors
            both_past_start = form.start_datetime.errors

        with submitted(app, post_env,
                       start_datetime=later_start.strftime('%Y-%m-%dT%H:%M'),
                       end_datetime=past_end.strftime('%Y-%m-%dT%H:%M')) as form:
            out_of_order = form.validate()
            out_of_order_end = form.end_datetime.errors
            out_of_order_start = form.start_datetime.errors

        assert both_past is False
        assert 'This time is in the past.' in both_past_end
        assert 'This time is in the past.' in both_past_start

        assert out_of_order is False
        assert 'This time is in the past.' in out_of_order_end
        assert 'Start must be less than end.' in out_of_order_start

    def test_an_end_before_the_start_is_refused(self, app, post_env):
        """Both in the future, so only the ordering can refuse this one."""
        start = utcnow() + timedelta(days=3)
        end = utcnow() + timedelta(days=2)

        with submitted(app, post_env,
                       start_datetime=start.strftime('%Y-%m-%dT%H:%M'),
                       end_datetime=end.strftime('%Y-%m-%dT%H:%M')) as form:
            valid = form.validate()
            messages = form.start_datetime.errors

        assert valid is False
        assert 'Start must be less than end.' in messages

    def test_the_times_are_read_in_the_submitters_timezone(self, app, post_env):
        """The reason `ZoneInfo(self.event_timezone.data)` is there at all.

        A wall-clock time an hour into the future in a zone twelve hours AHEAD of UTC
        is in the past in UTC; the same string in a zone twelve hours BEHIND is well
        in the future. Only the conversion can tell them apart, and a naive
        comparison against `utcnow()` would treat both the same.
        """
        from zoneinfo import ZoneInfo

        # ONE wall-clock string, an hour ahead of UTC now, submitted twice. Read in a
        # zone fourteen hours AHEAD of UTC it names an instant thirteen hours in the
        # PAST; read in a zone ten hours BEHIND it is eleven hours in the future. A
        # naive comparison against utcnow() would accept both.
        wall_clock = (utcnow() + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M')
        later = (utcnow() + timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M')

        with submitted(app, post_env, event_timezone='Pacific/Kiritimati',
                       start_datetime=wall_clock, end_datetime=later) as form:
            form.event_timezone.choices = [('Pacific/Kiritimati', 'Kiritimati')]
            valid_ahead = form.validate()
            ahead_messages = form.start_datetime.errors

        with submitted(app, post_env, event_timezone='Pacific/Honolulu',
                       start_datetime=wall_clock, end_datetime=later) as form:
            form.event_timezone.choices = [('Pacific/Honolulu', 'Honolulu')]
            valid_behind = form.validate()

        assert valid_ahead is False
        assert 'This time is in the past.' in ahead_messages
        assert valid_behind is True

        # The discrimination the conversion buys, stated directly: the same naive
        # reading is in the future, and only one of the two zones puts it in the past.
        from datetime import datetime
        naive = datetime.strptime(wall_clock, '%Y-%m-%dT%H:%M')
        assert naive > utcnow()
        assert naive.replace(tzinfo=ZoneInfo('Pacific/Kiritimati')).astimezone(
            ZoneInfo('UTC')) < utcnow(naive=False)
        assert naive.replace(tzinfo=ZoneInfo('Pacific/Honolulu')).astimezone(
            ZoneInfo('UTC')) > utcnow(naive=False)


# --------------------------------------------------------------------------
# Online versus physical
# --------------------------------------------------------------------------


class TestWhereTheEventIs:
    def test_an_online_event_needs_a_link(self, app, post_env):
        with submitted(app, post_env, online='y', online_link=None) as form:
            valid = form.validate()
            messages = form.online_link.errors

        assert valid is False
        assert any('Online link is required' in str(m) for m in messages)

    def test_a_link_of_only_spaces_is_not_a_link(self, app, post_env):
        """`not self.online_link.data.strip()` -- the second operand, which a
        non-empty string of whitespace is the only way to reach."""
        with submitted(app, post_env, online='y', online_link='   ') as form:
            valid = form.validate()
            messages = form.online_link.errors

        assert valid is False
        assert any('Online link is required' in str(m) for m in messages)

    @pytest.mark.parametrize('missing,field,message', [
        ('irl_address', 'irl_address', 'Address is required'),
        ('irl_city', 'irl_city', 'City is required'),
        ('irl_country', 'irl_country', 'Country is required'),
    ])
    def test_a_physical_event_needs_all_three(self, app, post_env, missing, field,
                                              message):
        with submitted(app, post_env, **{missing: None}) as form:
            valid = form.validate()
            messages = getattr(form, field).errors

        assert valid is False
        assert any(message in str(m) for m in messages)

    @pytest.mark.parametrize('field', ['irl_address', 'irl_city', 'irl_country'])
    def test_whitespace_is_not_an_address(self, app, post_env, field):
        """The `.strip()` operand for each of the three."""
        with submitted(app, post_env, **{field: '   '}) as form:
            valid = form.validate()

        assert valid is False

    def test_an_online_event_needs_no_address(self, app, post_env):
        """The branch, not just its arms: an online event must not be asked for the
        three physical fields."""
        with submitted(app, post_env, online='y',
                       online_link='https://meet.example/room',
                       irl_address=None, irl_city=None, irl_country=None) as form:
            assert form.validate() is True, _errors(form)

    def test_a_physical_event_needs_no_link(self, app, post_env):
        with submitted(app, post_env, online_link=None) as form:
            assert form.validate() is True, _errors(form)


# --------------------------------------------------------------------------
# The banner
# --------------------------------------------------------------------------


def _image(size, name='banner.png'):
    return {'image_file': (io.BytesIO(b'x' * size), name)}


class TestTheBanner:
    def test_a_banner_over_ten_megabytes_is_refused(self, app, post_env):
        with submitted(app, post_env,
                       files=_image(10 * 1024 * 1024 + 1)) as form:
            valid = form.validate()
            messages = form.image_file.errors

        assert valid is False
        assert 'This image filesize is too large.' in messages

    def test_a_banner_at_exactly_ten_megabytes_is_accepted(self, app, post_env):
        """`>`, not `>=`. The boundary is worth a row because the constant is
        written as `10 * 1024 * 1024` and read as a limit."""
        with submitted(app, post_env, files=_image(10 * 1024 * 1024)) as form:
            assert form.validate() is True, _errors(form)

    def test_a_small_banner_is_accepted(self, app, post_env):
        with submitted(app, post_env, files=_image(32)) as form:
            assert form.validate() is True, _errors(form)

    def test_the_file_is_rewound_for_whoever_reads_it_next(self, app, post_env):
        """`uploaded_file.seek(0)` after the size check. `len(uploaded_file.read())`
        leaves the stream at EOF, and the route saves the same handle afterwards --
        without the rewind an accepted banner would be written as zero bytes."""
        from flask import request

        with submitted(app, post_env, files=_image(64)) as form:
            assert form.validate() is True, _errors(form)
            assert request.files['image_file'].read() == b'x' * 64

    def test_a_submission_with_no_banner_is_accepted(self, app, post_env):
        """`if 'image_file' in request.files:` -- the banner is Optional, so the
        whole block is skipped and the local-image gate below it with it."""
        with submitted(app, post_env) as form:
            assert form.validate() is True, _errors(form)

    def test_a_banner_cannot_be_posted_to_a_local_community_when_disallowed(
            self, app, post_env):
        """D1001's third site. `allow_local_image_posts` off must refuse AND say so;
        the defect it is named for appended the message and returned True."""
        community, _ = post_env
        assert community.is_local() is True
        site = db.session.get(Site, 1)
        site.allow_local_image_posts = False
        db.session.commit()

        with submitted(app, post_env, files=_image(32)) as form:
            valid = form.validate()
            messages = form.communities.errors

        assert valid is False
        assert any('Images cannot be posted to local communities' in str(m)
                   for m in messages)

    def test_the_gate_does_not_fire_when_local_image_posts_are_allowed(self, app,
                                                                      post_env):
        """The control for the setting."""
        site = db.session.get(Site, 1)
        site.allow_local_image_posts = True
        db.session.commit()

        with submitted(app, post_env, files=_image(32)) as form:
            assert form.validate() is True, _errors(form)

    def test_the_gate_does_not_fire_for_a_remote_community(self, app, post_env):
        """The other operand. `community.is_local()` is
        `self.ap_id is None or self.ap_profile_id.startswith(SERVER_URL)`, so the
        row needs an `ap_id` as well as a remote profile url -- a community built
        with only a remote host is still local."""
        community, language = post_env
        community.ap_id = 'elsewhere@remote.example'
        community.ap_profile_id = 'https://remote.example/c/elsewhere'
        site = db.session.get(Site, 1)
        site.allow_local_image_posts = False
        db.session.commit()
        assert community.is_local() is False

        with submitted(app, post_env, files=_image(32)) as form:
            assert form.validate() is True, _errors(form)

    def test_a_banner_too_large_is_refused_before_the_local_gate(self, app,
                                                                post_env):
        """Order, which is behaviour: the size check returns False first, so an
        oversized banner to a local community with the setting off is reported as
        oversized rather than as disallowed. A reader of the two messages would
        otherwise not know which came first."""
        site = db.session.get(Site, 1)
        site.allow_local_image_posts = False
        db.session.commit()

        with submitted(app, post_env,
                       files=_image(10 * 1024 * 1024 + 1)) as form:
            valid = form.validate()
            size_messages = form.image_file.errors
            gate_messages = form.communities.errors

        assert valid is False
        assert 'This image filesize is too large.' in size_messages
        assert gate_messages == []


# --------------------------------------------------------------------------
# The two divergences: the size cap (fixed) and the communities guard
# --------------------------------------------------------------------------


@pytest.mark.parametrize('name', ['big.png', 'big.gif'])
def test_the_image_form_limits_every_image(app, post_env, name):
    """R207, fixed. The identical ten-megabyte block was general on the event form
    and `.gif`-only on the image form, so a 500 MB PNG passed the image form while a
    12 MB banner was refused. Both forms now share one check capping every image at
    10 MB (owner ruling 2026-09-30).
    """
    with submitted(app, post_env, form_cls=CreateImageForm,
                   files=_image(10 * 1024 * 1024 + 1, name=name)) as form:
        valid = form.validate()
        messages = form.image_file.errors

    assert valid is False
    assert 'This image filesize is too large.' in messages


def test_the_communities_guard_cannot_discriminate(app, post_env):
    """RECORDED, NOT REPAIRED. `if self.communities:` tests the FIELD OBJECT, which
    is always truthy -- fact 75 CAUSE 9, a guard that cannot discriminate. It appears
    in both copies of the block.

    Harmless, and this row is the proof rather than an assertion about the route:
    `communities` carries `DataRequired()` and `super().validate()` has already
    returned, so by the time the line is reached `.data` is one of the choices the
    route set and `db.session.get` cannot answer None. Writing
    `if self.communities.data:` would change nothing, and so would deleting the line.
    """
    community, language = post_env

    with submitted(app, post_env) as form:
        assert bool(form.communities) is True
        assert bool(form.communities.data) is True
        # An unbound field object is truthy too, which is the whole point.
        assert bool(CreateEventForm.communities) is True
