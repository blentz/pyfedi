"""app/admin/routes.py, slice A -- the admin entry points, and the guard on them.

MEASUREMENT BASIS. `app/admin/routes.py` stood at 15.930% on the full-suite
--cov=app run at ec192c784, carrying 1400 missing statements and the highest
authorization-construct density in the repository (160). It is taken in slices;
this is the first, covering `admin_home`, `admin_misc` and
`admin_instance_chooser`.

One defect is pinned here and repaired with it:

  P1  `admin_home` carried only @login_required. Every OTHER route on this
      blueprint carries a @permission_required(...), and the page renders the
      host's load averages, core count, disk usage, plugin list and overdue
      cron tasks -- to any registered account.

Facts 347 and 348 govern every authorization row here: user id 1 passes every
`user_access` check, so the refused caller must be a different user; and a
permitted caller needs exactly the permission under test, or the guard's
permission string is not load-bearing.
"""
from datetime import datetime, timedelta

import pytest
from unittest.mock import patch

from app import db
from app.models import CronJobLog, Language, Site, utcnow
from tests.factories import grant_permission, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')


def _seed():
    """A founder (id 1, omnipotent -- fact 347) and an ordinary account."""
    instance = make_instance('test.piefed.local', software='piefed')
    founder = make_user(instance, 'founder', local=True)
    assert founder.id == 1
    ordinary = make_user(instance, 'ordinary', local=True)
    ordinary.verified = True
    db.session.commit()
    return instance, ordinary


def _staff(instance, name='staffer'):
    """A member of staff: what the navigation uses to decide whether the admin
    menu exists at all (app/templates/base.html:268), and therefore what
    `admin_home` now requires.
    """
    from app.models import Role

    user = make_user(instance, name, local=True)
    user.verified = True
    role = Role.query.filter_by(name='Staff').first()
    if role is None:
        role = Role(name='Staff', weight=5)
        db.session.add(role)
        db.session.commit()
    user.roles.append(role)
    db.session.commit()
    return user


def _settings_admin(instance, name='settingsadmin'):
    """Holds ONLY 'change instance settings' and is not user 1 -- fact 348."""
    user = make_user(instance, name, local=True)
    user.verified = True
    db.session.commit()
    assert user.id != 1
    grant_permission(user, 'change instance settings')
    return user


def _valid_misc_payload(language):
    """A SiteMiscForm submission that validates.

    Every field gets a value read off the form object rather than hand-listed,
    so the payload stays valid as the form gains fields.

    `formdata=None`, and NOT a surrounding test_request_context: FlaskForm's
    default `formdata=_Auto` reaches for `request`, and a nested
    test_request_context pushes and then pops an app context, whose
    teardown_appcontext runs db.session.remove(). That closes the session the
    session-scoped `app` fixture is running on, and every later request in the
    test resolves current_user to anonymous -- so the row 302s to
    /auth/permission_denied and any assertion about the view is vacuous.
    Passing formdata=None keeps the whole helper inside the one app context.
    """
    from wtforms import IntegerField, SelectField, StringField, TextAreaField

    from app.admin.forms import SiteMiscForm
    from app.utils import theme_list

    form = SiteMiscForm(formdata=None, meta={'csrf': False})
    form.default_theme.choices = theme_list()
    # Not languages_for_form(): that reads current_user, which needs a request.
    form.language_id.choices = [(language.id, language.name)]
    data = {}
    for field in form:
        if isinstance(field, SelectField):
            # Loud, not skipped. A SelectField whose choices are empty because
            # its table was not seeded leaves the payload missing a required
            # key, validate_on_submit() returns False, and the branch under
            # test silently never runs -- the row would pass its own POST and
            # assert nothing.
            assert field.choices, f'{field.name} has no choices; seed its table'
            data[field.name] = str(field.choices[0][0])
        elif isinstance(field, IntegerField):
            data[field.name] = '0'
        elif isinstance(field, (StringField, TextAreaField)) and field.name != 'csrf_token':
            data[field.name] = ''
    return data


def _language(code='en', name='English'):
    """SiteMiscForm.language_id is a DataRequired SelectField fed from the
    `language` table, which the test database does not seed."""
    language = Language(code=code, name=name)
    db.session.add(language)
    db.session.commit()
    return language


def login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def csrf(app, client):
    """A real CSRF token.

    `login_required(csrf=True)` validates the token ITSELF, independently of
    `WTF_CSRF_ENABLED`, which tests/conftest.py sets False -- so every POST here
    needs one even though WTForms' own validation is off. Without it a POST is
    refused BEFORE any authorization check, which would make an authorization
    row pass while testing CSRF.
    """
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf

    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as sess:
        sess['csrf_token'] = raw
    return token


# --------------------------------------------------------------------------
# P1: the admin landing page
# --------------------------------------------------------------------------


def test_an_ordinary_account_cannot_read_the_admin_home(app, db_session):
    """Before the repair this page answered 200 to any registered account and
    handed it the host's telemetry:

        PROBE n1 has change instance settings: False
        PROBE n1 GET /admin/ status: 200
        PROBE n1 disk_usage leaked: Storage used: 51.18%
        PROBE n1 num_cores leaked: 32

    Anonymous visitors were always redirected to the login page, so the hole
    was "any account", which on an open-registration instance is anyone.
    """
    instance, ordinary = _seed()
    client = app.test_client()
    login(client, ordinary)

    response = client.get('/admin/')

    assert response.status_code == 403


def test_an_anonymous_visitor_is_sent_to_the_login_page(app, db_session):
    """The @login_required arm, which was always correct -- pinned so the
    repair is not credited with it.
    """
    _seed()
    client = app.test_client()

    response = client.get('/admin/')

    assert response.status_code == 302
    assert '/auth/login' in response.headers['Location']


def test_a_member_of_staff_can_read_the_admin_home(app, db_session):
    """The inversion. `is_admin_or_staff()` is what the navigation uses to show
    the link, so a repair that refused staff would take the page away from
    people who can see its menu entry.
    """
    instance, ordinary = _seed()
    staffer = _staff(instance)
    client = app.test_client()
    login(client, staffer)

    with patch('app.admin.routes.render_template', return_value='rendered'):
        response = client.get('/admin/')

    assert response.status_code == 200


def test_an_admin_can_read_the_admin_home(app, db_session):
    """The other half of `is_admin() or is_staff()`, so neither disjunct can be
    dropped without a row failing.
    """
    from app.models import Role

    instance, ordinary = _seed()
    admin = make_user(instance, 'theadmin', local=True)
    role = Role(name='Admin', weight=10)
    db.session.add(role)
    db.session.commit()
    admin.roles.append(role)
    db.session.commit()
    client = app.test_client()
    login(client, admin)

    with patch('app.admin.routes.render_template', return_value='rendered'):
        response = client.get('/admin/')

    assert response.status_code == 200


def test_the_admin_home_reports_the_hosts_resources(app, db_session):
    """What the page is FOR, and what the guard above protects: load averages,
    core count and disk usage all come from the host the instance runs on.
    """
    instance, ordinary = _seed()
    staffer = _staff(instance)
    client = app.test_client()
    login(client, staffer)

    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        client.get('/admin/')

    kwargs = render.call_args.kwargs
    assert kwargs['load1'] is not None
    assert kwargs['num_cores'] > 0
    assert 'Storage used' in kwargs['disk_usage']


@pytest.mark.parametrize('configured, expected_cores', [
    (8, 8),
    (0, None),
    (None, None),
])
def test_the_core_count_prefers_the_configured_value(app, db_session, configured,
                                                     expected_cores):
    """`if current_app.config["NUM_CPU"] and ... != 0` -- both conjuncts. A
    configured 0 and an unset value both fall through to os.cpu_count(), which
    is why the two false rows assert "whatever the host has" rather than a
    number.
    """
    import os

    instance, ordinary = _seed()
    staffer = _staff(instance)
    client = app.test_client()
    login(client, staffer)

    with patch.dict(app.config, {'NUM_CPU': configured}):
        with patch('app.admin.routes.render_template', return_value='rendered') as render:
            client.get('/admin/')

    if expected_cores is None:
        assert render.call_args.kwargs['num_cores'] == os.cpu_count()
    else:
        assert render.call_args.kwargs['num_cores'] == expected_cores


@pytest.mark.parametrize('percent, blinks', [
    (96.0, True),
    (95.0, False),
    (10.0, False),
])
def test_a_nearly_full_disk_is_flagged(app, db_session, percent, blinks):
    """`if percent_used > 95` -- the boundary. 95.0 exactly must NOT blink, or
    the warning fires a percentage early and stops meaning anything.
    """
    import shutil

    instance, ordinary = _seed()
    staffer = _staff(instance)
    client = app.test_client()
    login(client, staffer)
    usage = shutil.disk_usage('/')
    total = usage.total
    fake = type('Usage', (), {'total': total, 'used': int(total * percent / 100)})()

    with patch('app.admin.routes.shutil.disk_usage', return_value=fake):
        with patch('app.admin.routes.render_template', return_value='rendered') as render:
            client.get('/admin/')

    assert ('blink red' in render.call_args.kwargs['disk_usage']) is blinks


def test_an_overdue_cron_task_is_reported(app, db_session):
    """The maintenance warning. An instance whose cron has stopped silently
    stops expiring bans and cleaning up content, so this is the only place it
    surfaces.
    """
    from flask import get_flashed_messages

    instance, ordinary = _seed()
    staffer = _staff(instance)
    from datetime import timedelta

    # frequency is a Postgres INTERVAL column, so it takes a timedelta -- a
    # string raises InvalidDatetimeFormat at the driver.
    db.session.add(CronJobLog(name='daily', frequency=timedelta(days=1),
                              last_run=utcnow() - timedelta(days=3)))
    db.session.commit()
    client = app.test_client()
    login(client, staffer)

    with client:
        with patch('app.admin.routes.render_template', return_value='rendered'):
            client.get('/admin/')
            messages = get_flashed_messages()

    assert any('daily' in message for message in messages)


def test_a_cron_task_that_ran_recently_is_not_reported(app, db_session):
    """The other arm of `if diff_last_run > cron_task.get_frequency()`, and of
    `if overdue_tasks:` -- a healthy instance flashes nothing.
    """
    from flask import get_flashed_messages

    instance, ordinary = _seed()
    staffer = _staff(instance)
    from datetime import timedelta

    db.session.add(CronJobLog(name='daily', frequency=timedelta(days=1),
                              last_run=utcnow()))
    db.session.commit()
    client = app.test_client()
    login(client, staffer)

    with client:
        with patch('app.admin.routes.render_template', return_value='rendered'):
            client.get('/admin/')
            messages = get_flashed_messages()

    assert messages == []


@pytest.mark.parametrize('endpoint, languages, expected', [
    ('https://lt.example', [{'code': 'en', 'name': 'English'}], [{'code': 'en', 'name': 'English'}]),
    ('', None, None),
])
def test_the_translation_languages_are_fetched_only_when_configured(app, db_session,
                                                                    endpoint, languages,
                                                                    expected):
    instance, ordinary = _seed()
    staffer = _staff(instance)
    client = app.test_client()
    login(client, staffer)

    with patch.dict(app.config, {'TRANSLATE_ENDPOINT': endpoint, 'TRANSLATE_KEY': 'k'}):
        with patch('app.admin.routes.LibreTranslateAPI') as api:
            api.return_value.languages.return_value = languages
            with patch('app.admin.routes.render_template', return_value='rendered') as render:
                client.get('/admin/')

    assert render.call_args.kwargs['translation_languages'] == expected
    # Not just the result: with no endpoint the client must not be BUILT at
    # all. Dropping the `if` guard leaves LibreTranslateAPI(None) to raise
    # inside the bare `except Exception: pass`, which lands on the same None
    # and hides an outbound call the operator never configured.
    assert api.called is bool(endpoint)


def test_an_unreachable_translation_service_does_not_break_the_page(app, db_session):
    """R1's `except Exception: pass`. The admin page must still render when the
    configured endpoint is down -- registered separately is that it is
    contacted synchronously on every load.
    """
    instance, ordinary = _seed()
    staffer = _staff(instance)
    client = app.test_client()
    login(client, staffer)

    with patch.dict(app.config, {'TRANSLATE_ENDPOINT': 'https://lt.example',
                                 'TRANSLATE_KEY': 'k'}):
        with patch('app.admin.routes.LibreTranslateAPI',
                   side_effect=RuntimeError('unreachable')):
            with patch('app.admin.routes.render_template', return_value='rendered') as render:
                response = client.get('/admin/')

    assert response.status_code == 200
    assert render.call_args.kwargs['translation_languages'] is None


# --------------------------------------------------------------------------
# Every route on the blueprint, refused
# --------------------------------------------------------------------------


def test_no_admin_route_answers_an_ordinary_account(app, db_session):
    """THE ROW THAT WOULD HAVE CAUGHT P1, over the whole blueprint.

    Every rule registered under the admin blueprint is requested as a verified
    account with no roles and no permissions. None may answer 200 -- a refusal
    is a redirect to the login or permission-denied page, a 403, or a 404.

    Parameterised rules get a plausible id substituted; a rule that accepts
    only POST is exercised with POST. The assertion lists the offending routes
    rather than counting them, so a failure names the hole rather than reporting
    a number.

    `admin_home` is the route this would have caught. The point is the ones
    added after today.
    """
    instance, ordinary = _seed()
    client = app.test_client()
    login(client, ordinary)
    token = csrf(app, client)

    reachable = []
    for rule in app.url_map.iter_rules():
        if not rule.rule.startswith('/admin'):
            continue
        methods = rule.methods - {'HEAD', 'OPTIONS'}
        path = rule.rule
        for argument in rule.arguments:
            path = path.replace(f'<int:{argument}>', '1').replace(f'<{argument}>', '1')
        if '<' in path:
            continue
        method = 'GET' if 'GET' in methods else 'POST'
        try:
            if method == 'POST':
                response = client.post(path, data={'csrf_token': token})
            else:
                response = client.get(path)
        except Exception:
            # An exception is not a 200. A route that raises has not served the
            # caller, and several raise rather than returning a refusal.
            continue
        if response.status_code == 200:
            reachable.append(f'{method} {path} -> 200')

    assert reachable == [], (
        'these admin routes answered an account with no permissions: '
        + '; '.join(reachable))


def test_every_admin_route_sends_an_anonymous_visitor_to_the_login_page(app, db_session):
    """D943 (audit F14), fixed: on 55 admin routes `@permission_required` sat
    outside `@login_required`, so it ran first and an anonymous visitor was sent
    to `/auth/permission_denied` -- after a role query -- where the rest of the
    blueprint sent them to log in. `@login_required` is outermost everywhere
    now; this row asks every admin rule, as the one above does, and lists the
    rules that answer otherwise."""
    _seed()
    client = app.test_client()

    elsewhere = []
    for rule in app.url_map.iter_rules():
        if not rule.rule.startswith('/admin'):
            continue
        methods = rule.methods - {'HEAD', 'OPTIONS'}
        path = rule.rule
        for argument in rule.arguments:
            path = path.replace(f'<int:{argument}>', '1').replace(f'<{argument}>', '1')
        if '<' in path:
            continue
        method = 'GET' if 'GET' in methods else 'POST'
        response = client.post(path) if method == 'POST' else client.get(path)
        if response.status_code != 302 or '/auth/login' not in response.headers['Location']:
            elsewhere.append(f'{method} {path} -> {response.status_code} '
                             f'{response.headers.get("Location", "")}')

    assert elsewhere == [], (
        'these admin routes did not send an anonymous visitor to log in: '
        + '; '.join(elsewhere))


# --------------------------------------------------------------------------
# admin_instance_chooser
# --------------------------------------------------------------------------


def test_the_instance_chooser_needs_the_settings_permission(app, db_session):
    """Fact 348: the permitted caller below holds ONLY 'change instance
    settings', so the guard's permission string is load-bearing. This row is
    the refusal.
    """
    instance, ordinary = _seed()
    client = app.test_client()
    login(client, ordinary)

    response = client.get('/admin/instance_chooser')

    assert response.status_code == 302
    assert '/permission_denied' in response.headers['Location']


def test_the_instance_chooser_shows_the_stored_settings(app, db_session):
    """The GET arm. Each value is seeded to a non-default so a form that
    ignored the store would show the defaults and fail.
    """
    from app.utils import set_setting

    instance, ordinary = _seed()
    admin = _settings_admin(instance)
    set_setting('enable_instance_chooser', True)
    set_setting('elevator_pitch', 'a friendly place')
    set_setting('number_of_admins', 3)
    set_setting('financial_stability', True)
    set_setting('daily_backups', True)
    client = app.test_client()
    login(client, admin)

    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        response = client.get('/admin/instance_chooser')

    assert response.status_code == 200
    form = render.call_args.kwargs['form']
    assert form.enable_instance_chooser.data is True
    assert form.elevator_pitch.data == 'a friendly place'
    assert form.number_of_admins.data == 3


def test_the_instance_chooser_saves_what_was_submitted(app, db_session):
    from app.utils import get_setting

    instance, ordinary = _seed()
    admin = _settings_admin(instance)
    client = app.test_client()
    login(client, admin)
    token = csrf(app, client)

    with patch('app.admin.routes.render_template', return_value='rendered'):
        client.post('/admin/instance_chooser',
                    data={'enable_instance_chooser': 'y', 'elevator_pitch': 'we are nice',
                          'number_of_admins': '2', 'financial_stability': 'y',
                          'daily_backups': 'y', 'submit': 'Save', 'csrf_token': token})

    assert get_setting('enable_instance_chooser', False) is True
    assert get_setting('elevator_pitch', '') == 'we are nice'
    assert get_setting('number_of_admins', 0) == 2


def test_an_empty_elevator_pitch_is_stored_as_an_empty_string(app, db_session):
    """`form.elevator_pitch.data or ''` -- a cleared field must become '' and
    not None, because the setting is read into a template.
    """
    from app.utils import get_setting

    instance, ordinary = _seed()
    admin = _settings_admin(instance)
    client = app.test_client()
    login(client, admin)
    token = csrf(app, client)

    with patch('app.admin.routes.render_template', return_value='rendered'):
        client.post('/admin/instance_chooser',
                    data={'elevator_pitch': '', 'number_of_admins': '0',
                          'submit': 'Save', 'csrf_token': token})

    assert get_setting('elevator_pitch', 'unset') == ''

    # OMITTED, not empty. A StringField that is present-but-blank already has
    # data '' and would be stored as '' with or without the `or ''`; only an
    # absent key leaves data None, which is what the default is defending
    # against.
    with patch('app.admin.routes.render_template', return_value='rendered'):
        client.post('/admin/instance_chooser',
                    data={'number_of_admins': '0', 'submit': 'Save',
                          'csrf_token': token})

    assert get_setting('elevator_pitch', 'unset') == ''


# --------------------------------------------------------------------------
# admin_misc
# --------------------------------------------------------------------------


def test_the_misc_settings_need_the_settings_permission(app, db_session):
    instance, ordinary = _seed()
    client = app.test_client()
    login(client, ordinary)

    response = client.get('/admin/misc')

    assert response.status_code == 302
    assert '/permission_denied' in response.headers['Location']


def test_the_misc_settings_page_renders_for_a_settings_admin(app, db_session):
    instance, ordinary = _seed()
    admin = _settings_admin(instance)
    client = app.test_client()
    login(client, admin)

    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        response = client.get('/admin/misc')

    assert response.status_code == 200
    assert render.call_args.kwargs['form'] is not None


def test_closing_the_instance_pauses_federation_and_shuts_registration(app, db_session,
                                                                        redis_double):
    """The CloseInstanceForm arm -- the emergency stop. It pauses federation for
    ten years and closes registration in one action, so both are asserted:
    either alone would leave the instance half-closed.
    """
    instance, ordinary = _seed()
    admin = _settings_admin(instance)
    # The Site factory already leaves registration_mode 'Closed', so asserting
    # 'Closed' after the POST proves nothing unless the instance starts open.
    db.session.get(Site, 1).registration_mode = 'Open'
    db.session.commit()
    client = app.test_client()
    login(client, admin)
    token = csrf(app, client)

    with patch('app.admin.routes.render_template', return_value='rendered'):
        client.post('/admin/misc', data={'announcement': 'we are closing',
                                         'close_submit': 'Close instance',
                                         'csrf_token': token})

    db.session.expire_all()
    assert db.session.get(Site, 1).registration_mode == 'Closed'
    assert redis_double.get('pause_federation') == '666'  # decode_responses=True on the double


def test_closing_the_instance_publishes_the_announcement(app, db_session, redis_double):
    """The announcement is stored twice: raw, and rendered to HTML for the
    banner. Both are asserted -- storing only one leaves the home page either
    blank or showing markdown source.
    """
    from app.utils import get_setting

    instance, ordinary = _seed()
    admin = _settings_admin(instance)
    client = app.test_client()
    login(client, admin)
    token = csrf(app, client)

    with patch('app.admin.routes.render_template', return_value='rendered'):
        client.post('/admin/misc', data={'announcement': 'we are closing',
                                         'close_submit': 'Close instance',
                                         'csrf_token': token})

    assert get_setting('announcement', '') == 'we are closing'
    assert 'we are closing' in get_setting('announcement_html', '')


def test_closing_the_instance_without_an_announcement_says_nothing(app, db_session,
                                                                    redis_double):
    """There is no `close quietly`. CloseInstanceForm.announcement carries
    DataRequired(), so a close with no announcement does not validate and the
    branch does not run at all -- nothing is published AND nothing is closed.
    The route used to guard the two set_setting calls with `if
    close_form.announcement.data:`; that guard could never be false once
    validate() had passed, and it is gone.
    """
    from app.utils import get_setting

    instance, ordinary = _seed()
    admin = _settings_admin(instance)
    # Fact 350: the factory already leaves registration_mode 'Closed', so a
    # row asserting it is 'Closed' -- or that it is unchanged -- proves nothing
    # unless the instance starts open.
    db.session.get(Site, 1).registration_mode = 'Open'
    db.session.commit()
    client = app.test_client()
    login(client, admin)
    token = csrf(app, client)

    with patch('app.admin.routes.render_template', return_value='rendered'):
        client.post('/admin/misc', data={'announcement': '', 'close_submit': 'Close instance',
                                         'csrf_token': token})

    db.session.expire_all()
    assert db.session.get(Site, 1).registration_mode == 'Open'
    assert redis_double.get('pause_federation') is None
    assert get_setting('announcement', 'unset') == 'unset'


def test_the_misc_settings_create_a_site_row_when_there_is_none(app, db_session):
    """`if site is None: site = Site()` and `if site.id is None:
    db.session.add(site)` -- the fresh-instance path.

    CALLED DIRECTLY, not over HTTP, and that is the finding. With no Site row
    the request lifecycle cannot run at all: the hook that populates `g.site`
    goes through `app.utils.get_site_as_dict`, which does
    `db.session.get(Site, 1)` and then dereferences `site.__table__` with no
    nil check (app/utils.py:5431-5433), so every request raises
    `AttributeError: 'NoneType' object has no attribute '__table__'` before
    reaching any view. These two lines are therefore defensive against a state
    no HTTP request can be served in -- registered as R3 -- and the only honest
    way to reach them is to invoke the view with `g.site` already supplied.
    """
    from flask import g, session as flask_session
    from flask_login import login_user
    from flask_wtf.csrf import generate_csrf

    from app.admin.routes import admin_misc

    instance, ordinary = _seed()
    admin = _settings_admin(instance)
    language = _language()
    site_row = db.session.get(Site, 1)

    # The view is wrapped by login_required, which validates CSRF itself, so a
    # direct call needs a token bound to this context's session exactly as an
    # HTTP POST would.
    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']

    # The payload is built from the form's OWN choices rather than hand-listed:
    # SiteMiscForm carries several DataRequired() SelectFields, and a row that
    # named them by hand would fail silently -- validate_on_submit() returns
    # False and the branch under test never runs -- every time the form gained
    # a field.
    payload = _valid_misc_payload(language)
    payload.update({'submit': 'Save', 'csrf_token': token})

    with app.test_request_context('/admin/misc', method='POST', data=payload):
        flask_session['csrf_token'] = raw
        g.site = site_row
        login_user(admin)
        db.session.delete(site_row)
        db.session.commit()
        assert Site.query.count() == 0

        with patch('app.admin.routes.render_template', return_value='rendered') as rt:
            admin_misc()
        # If the payload failed validation the branch under test never ran and
        # the row below would assert nothing, so say so here.
        assert rt.call_args.kwargs['form'].errors == {}

    assert Site.query.count() == 1


def test_a_misc_settings_save_does_not_close_the_instance(app, db_session, redis_double):
    """P2's pin, inverted.

    SiteMiscForm and CloseInstanceForm are both rendered on /admin/misc, and
    admin_misc() binds BOTH to the same request body. While CloseInstanceForm's
    submit button was also named `submit`, an ordinary misc-settings save made
    `close_form.submit.data` true; the only thing standing between a Save and a
    closed instance was CloseInstanceForm.announcement's DataRequired(), so a
    save that carried any announcement text took the close branch instead --
    pausing federation for ten years and setting registration_mode to Closed,
    skipping the Danger Zone confirmation entirely.

    Probed before the fix, from an open instance:

        PROBE q1 mode before: Open
        PROBE q1 mode after SAVE with announcement text: Closed
        PROBE q1 pause_federation: 666

    Revert CloseInstanceForm.close_submit to `submit` and this row fails.
    """
    instance, ordinary = _seed()
    admin = _settings_admin(instance)
    language = _language()
    site = db.session.get(Site, 1)
    site.registration_mode = 'Open'
    db.session.commit()

    client = app.test_client()
    login(client, admin)
    token = csrf(app, client)

    payload = _valid_misc_payload(language)
    # The value a user would have typed into the Danger Zone textarea and not
    # yet sent. It must not be enough, on its own, to close the instance.
    payload.update({'submit': 'Save', 'announcement': 'draft text I typed',
                    'csrf_token': token})
    with patch('app.admin.routes.render_template', return_value='rendered'):
        client.post('/admin/misc', data=payload)

    db.session.expire_all()
    assert db.session.get(Site, 1).registration_mode == 'Open'
    assert redis_double.get('pause_federation') is None


def test_a_misc_settings_save_that_omits_the_cutoff_is_refused_not_a_500(app, db_session):
    """`set_setting('read_posts_cutoff', int(form.read_posts_cutoff.data))`.

    read_posts_cutoff was an IntegerField with no validators, so a submission
    that omitted the key left `.data` None, the form validated, and the route
    raised `TypeError: int() argument must be a string, a bytes-like object or
    a real number, not 'NoneType'` -- a 500 on a route staff can reach. The
    field now carries InputRequired(), so the form refuses instead. 0 is still
    accepted, which DataRequired() would have rejected.
    """
    instance, ordinary = _seed()
    admin = _settings_admin(instance)
    language = _language()
    site = db.session.get(Site, 1)

    client = app.test_client()
    login(client, admin)
    token = csrf(app, client)

    payload = _valid_misc_payload(language)
    del payload['read_posts_cutoff']
    payload.update({'submit': 'Save', 'csrf_token': token})
    with patch('app.admin.routes.render_template', return_value='rendered') as rt:
        response = client.post('/admin/misc', data=payload)

    assert response.status_code == 200
    assert rt.call_args.kwargs['form'].errors == {
        'read_posts_cutoff': ['This field is required.']}


def test_a_cutoff_of_zero_is_accepted(app, db_session):
    """NumberRange(min=0) with InputRequired(), not DataRequired(): 0 days is a
    legitimate setting and DataRequired() treats it as absent."""
    from app.utils import get_setting

    instance, ordinary = _seed()
    admin = _settings_admin(instance)
    language = _language()
    site = db.session.get(Site, 1)

    payload = _valid_misc_payload(language)
    client = app.test_client()
    login(client, admin)
    token = csrf(app, client)

    payload.update({'read_posts_cutoff': '0', 'submit': 'Save',
                    'csrf_token': token})
    with patch('app.admin.routes.render_template', return_value='rendered') as rt:
        client.post('/admin/misc', data=payload)

    assert rt.call_args.kwargs['form'].errors == {}
    assert get_setting('read_posts_cutoff', 180) == 0


def test_no_two_forms_on_the_misc_page_share_a_field_name(app, db_session):
    """The durable form of P2, and the reason the fix was a rename.

    admin_misc() instantiates SiteMiscForm and CloseInstanceForm from the SAME
    request body and renders both on /admin/misc. Any name they share means one
    form's submission drives the other's logic -- which is exactly how a misc
    Save came to close the instance. Renaming CloseInstanceForm.submit fixes
    today's collision; this row is what refuses tomorrow's, including a
    collision introduced by adding an innocuous field to either form.

    csrf_token is the one legitimate shared name: Flask-WTF puts it on every
    form and validates it per-request, not per-form.
    """
    from app.admin.forms import CloseInstanceForm, SiteMiscForm

    misc = set(SiteMiscForm(formdata=None, meta={'csrf': False})._fields)
    close = set(CloseInstanceForm(formdata=None, meta={'csrf': False})._fields)

    assert misc & close == set(), (
        f'fields shared between SiteMiscForm and CloseInstanceForm: '
        f'{sorted(misc & close)} -- admin_misc() binds both to one request body')


def test_a_rejected_instance_chooser_post_keeps_what_was_submitted(app, db_session):
    """`elif request.method == 'GET':`.

    The pre-fill arm must not run on a POST. If it did, a submission the form
    refused would come back showing the STORED settings rather than what the
    admin typed, silently discarding their edit -- so this row submits an
    elevator pitch over the Length(max=90) limit and checks the rendered form
    still holds it.
    """
    from app.utils import set_setting

    instance, ordinary = _seed()
    admin = _settings_admin(instance)
    set_setting('elevator_pitch', 'the stored pitch')
    client = app.test_client()
    login(client, admin)
    token = csrf(app, client)

    too_long = 'x' * 91
    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        client.post('/admin/instance_chooser',
                    data={'elevator_pitch': too_long, 'number_of_admins': '0',
                          'submit': 'Save', 'csrf_token': token})

    form = render.call_args.kwargs['form']
    assert 'elevator_pitch' in form.errors
    assert form.elevator_pitch.data == too_long


def test_a_cron_task_due_exactly_now_is_not_yet_overdue(app, db_session):
    """`if diff_last_run > cron_task.get_frequency():` -- strictly greater.

    A task whose last run is exactly one frequency ago is due, not late, so it
    must not be flashed. utcnow() is pinned because the view calls it itself:
    without the patch the few microseconds between the row's arithmetic and the
    view's make the difference strictly greater every time, and `>` and `>=`
    become indistinguishable.
    """
    frequency = timedelta(days=1)
    now = datetime(2026, 9, 20, 12, 0, 0)

    instance, ordinary = _seed()
    staffer = _staff(instance)
    db.session.add(CronJobLog(name='exactly due', frequency=frequency,
                              last_run=now - frequency))
    db.session.commit()

    client = app.test_client()
    login(client, staffer)

    with patch('app.admin.routes.utcnow', return_value=now):
        with patch('app.admin.routes.render_template', return_value='rendered'):
            with patch('app.admin.routes.flash') as flashed:
                client.get('/admin/')

    assert flashed.call_args_list == []
