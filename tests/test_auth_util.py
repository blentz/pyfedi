"""The rest of `app/auth/util.py`: where a visitor is from, who is told about
a registration, and the steps a registration goes through.

Sub-project 83, slice F. Five defects, all measured:

* `ip2location` let `httpx.HTTPError` escape, and `get_country` -- its only
  caller -- runs on every registration, every login and every OAuth callback,
  so an ipinfo.io outage took the authentication surface down with it
  (D1159);
* the same function read `data['city']` off an answer that carries no city:
  ipinfo replies `{"ip": ..., "bogon": true}` for any private address
  (D1160);
* `no_admins_logged_in_recently` compared a null `last_seen` with a datetime
  (D1161);
* somebody holding both the admin and the staff role was told TWICE about one
  application, and their unread counter moved by two (D1162);
* and every registration wrote the LDAP directory twice, because
  `finalize_user_registration` repeated the call `register_new_user` had
  already made (D1163).
"""
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app import cache, db
from app.auth.util import (configure_bandwidth_cookies, create_new_user_from_ldap,
                           create_registration_application, get_country,
                           get_font_preference, handle_user_application,
                           ip2location, no_admins_logged_in_recently,
                           normalize_username, notify_admins_of_registration,
                           random_token, render_registration_form,
                           send_email_verification)
from app.models import (Instance, Notification, Role, Site, User,
                        UserRegistration, user_role)
from tests.factories import make_community, make_community_member, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')


def instance(domain='test.piefed.local', software='piefed'):
    """Fact 394."""
    existing = Instance.query.filter_by(domain=domain).first()
    return existing if existing is not None else make_instance(domain,
                                                               software=software)


@pytest.fixture
def env(app, db_session):
    site = db.session.get(Site, 1)
    site.private_instance = False
    site.registration_mode = 'Open'
    site.application_question = ''
    site.tos_url = None
    db.session.commit()
    local = instance()
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1  # fact 347
    db.session.commit()
    cache.delete('ip_1.2.3.4')
    return app.test_client(), founder


def with_token(app, **overrides):
    config = {'IPINFO_TOKEN': 'a-token', 'COUNTRY_SOURCE_HEADER': ''}
    config.update(overrides)
    return patch.dict(app.config, config)


def answering(payload, status_code=200):
    response = MagicMock(status_code=status_code)
    response.json.return_value = payload
    return patch('app.auth.util.get_request', return_value=response)


def give_role(user, role_id, name):
    role = db.session.get(Role, role_id) or Role(id=role_id, name=name)
    db.session.add(role)
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=user.id,
                                                 role_id=role.id))
    db.session.commit()
    return role


# --------------------------------------------------------------------------
# D1159, D1160 -- where the visitor is from
# --------------------------------------------------------------------------


def test_no_address_is_no_location(app, env):
    client, founder = env

    assert ip2location(None) == {}
    assert ip2location('') == {}


def test_an_instance_with_no_token_looks_nothing_up(app, env):
    """The service is optional, and without a token there is nothing to ask."""
    client, founder = env

    with patch.dict(app.config, {'IPINFO_TOKEN': ''}):
        with patch('app.auth.util.get_request') as asked:
            assert ip2location('1.2.3.4') == {}

    asked.assert_not_called()


def test_a_location_comes_back_whole(app, env):
    client, founder = env

    with with_token(app):
        with answering({'city': 'Leeds', 'region': 'England', 'country': 'GB',
                        'postal': 'LS1', 'timezone': 'Europe/London'}):
            assert ip2location('1.2.3.4') == {
                'city': 'Leeds', 'region': 'England', 'country': 'GB',
                'postal': 'LS1', 'timezone': 'Europe/London'}


def test_an_answer_with_no_postcode_is_still_an_answer(app, env):
    client, founder = env

    with with_token(app):
        with answering({'city': 'Leeds', 'region': 'England', 'country': 'GB',
                        'timezone': 'Europe/London'}):
            assert ip2location('1.2.3.4')['postal'] == ''


def test_localhost_is_looked_up_as_a_real_address(app, env):
    """'127.0.0.1' is rewritten, because the service has nothing to say about
    it and a developer still wants an answer."""
    client, founder = env

    with with_token(app):
        with answering({'city': 'Leeds', 'region': 'England',
                        'country': 'GB', 'timezone': 'Europe/London'}) as asked:
            ip2location('127.0.0.1')

    assert '208.97.120.117' in asked.call_args.args[0]


def test_a_lookup_service_that_is_down_is_not_a_failed_login(app, env):
    """D1159. `get_request` raises httpx.HTTPError for every transport
    failure, and nothing caught it. `get_country` runs on every registration,
    every login and every OAuth callback. Measured: `PROBE az1 outcome:
    HTTPError: boom`."""
    client, founder = env

    with with_token(app):
        with patch('app.auth.util.get_request',
                   side_effect=httpx.HTTPError('boom')):
            assert ip2location('1.2.3.4') == {}


def test_a_body_that_is_not_json_is_not_a_failed_login_either(app, env):
    client, founder = env
    response = MagicMock(status_code=200)
    response.json.side_effect = ValueError('not json')

    with with_token(app):
        with patch('app.auth.util.get_request', return_value=response):
            assert ip2location('1.2.3.4') == {}


def test_a_refusal_from_the_service_is_no_location(app, env):
    client, founder = env

    with with_token(app):
        with answering({}, status_code=429):
            assert ip2location('1.2.3.4') == {}


def test_a_private_address_has_no_location(app, env):
    """D1160. ipinfo answers `{"ip": ..., "bogon": true}` for a private or
    reserved address -- no city, region, country or timezone -- and every LAN
    address reaches this, since only 127.0.0.1 is rewritten. Measured: `PROBE
    az2 outcome: KeyError: 'city'`."""
    client, founder = env

    with with_token(app):
        with answering({'ip': '192.168.1.1', 'bogon': True}):
            assert ip2location('192.168.1.1') == {}


def test_a_location_already_known_is_not_asked_for_again(app, env):
    """The answer is cached for a day, which keeps a busy instance from paying
    for a lookup per request. The test app's cache is a null one, so the store
    has to be stood in for (fact 545)."""
    client, founder = env
    store = {'ip_1.2.3.4': {'city': 'Leeds', 'region': 'England',
                            'country': 'GB', 'timezone': 'Europe/London'}}

    with with_token(app):
        with patch('app.auth.util.cache') as cached:
            cached.get.side_effect = store.get
            with patch('app.auth.util.get_request') as asked:
                again = ip2location('1.2.3.4')

    asked.assert_not_called()
    assert again['country'] == 'GB'


def test_a_location_just_looked_up_is_remembered(app, env):
    client, founder = env

    with with_token(app):
        with patch('app.auth.util.cache') as cached:
            cached.get.return_value = None
            with answering({'city': 'Leeds', 'region': 'England',
                            'country': 'GB', 'timezone': 'Europe/London'}):
                ip2location('1.2.3.4')

    assert cached.set.call_args.args[0] == 'ip_1.2.3.4'
    assert cached.set.call_args.kwargs['timeout'] == 86400


def test_the_country_header_is_taken_when_it_is_configured(app, env):
    """A proxy that already knows the country says so in a header, and then
    there is nothing to look up."""
    client, founder = env

    with with_token(app, COUNTRY_SOURCE_HEADER='CF-IPCountry'):
        with app.test_request_context('/', headers={'CF-IPCountry': 'GB'}):
            with patch('app.auth.util.get_request') as asked:
                assert get_country('1.2.3.4') == 'GB'

    asked.assert_not_called()


def test_no_address_answers_with_the_fallback(app, env):
    client, founder = env

    with with_token(app):
        with app.test_request_context('/'):
            assert get_country('   ', fallback='XX') == 'XX'
            assert get_country(None, fallback='XX') == 'XX'


def test_a_country_that_cannot_be_looked_up_is_the_fallback(app, env):
    """Which is what D1159 and D1160 both come back as now."""
    client, founder = env

    with with_token(app):
        with app.test_request_context('/'):
            with patch('app.auth.util.get_request',
                       side_effect=httpx.HTTPError('boom')):
                assert get_country('1.2.3.4', fallback='XX') == 'XX'


# --------------------------------------------------------------------------
# D1161 -- who has been here lately
# --------------------------------------------------------------------------


def test_an_instance_nobody_administers(app, env):
    client, founder = env
    give_role(founder, 4, 'Admin')
    founder.last_seen = utcnow_a_month_ago()
    db.session.commit()

    assert no_admins_logged_in_recently() is True


def utcnow_a_month_ago():
    from datetime import timedelta

    from app.utils import utcnow
    return utcnow() - timedelta(days=30)


def test_an_admin_who_was_here_this_week(app, env):
    client, founder = env
    give_role(founder, 4, 'Admin')

    assert no_admins_logged_in_recently() is False


def test_a_staff_member_who_was_here_this_week(app, env):
    """The second loop: staff count as somebody watching the place."""
    client, founder = env
    give_role(founder, 4, 'Admin')
    founder.last_seen = utcnow_a_month_ago()
    staff = make_user(instance(), 'staff', local=True)
    give_role(staff, 3, 'Staff')
    db.session.commit()

    assert no_admins_logged_in_recently() is False


def test_an_admin_who_has_never_logged_in_has_not_logged_in_recently(app, env):
    """D1161. `user.last_seen > a_week_ago` on a column that can be null.
    The caller is `handle_abandoned_open_instance`, on the registration page,
    so the exception closed registration by crashing it. Measured: `PROBE az3
    outcome: TypeError: '>' not supported between instances of 'NoneType' and
    'datetime.datetime'`."""
    client, founder = env
    give_role(founder, 4, 'Admin')
    founder.last_seen = None
    db.session.commit()

    assert no_admins_logged_in_recently() is True


def test_a_staff_member_who_has_never_logged_in_either(app, env):
    client, founder = env
    give_role(founder, 4, 'Admin')
    founder.last_seen = utcnow_a_month_ago()
    staff = make_user(instance(), 'staff', local=True)
    give_role(staff, 3, 'Staff')
    staff.last_seen = None
    db.session.commit()

    assert no_admins_logged_in_recently() is True


# --------------------------------------------------------------------------
# D1162 -- who is told about an application
# --------------------------------------------------------------------------


def an_application(name='applicant', verified=True):
    applicant = make_user(instance(), name, local=True)
    applicant.verified = verified
    db.session.commit()
    application = UserRegistration(user_id=applicant.id, answer='because',
                                   status=0)
    db.session.add(application)
    db.session.commit()
    return application


def test_the_admins_are_told(app, env):
    client, founder = env
    give_role(founder, 4, 'Admin')

    with patch('app.auth.util.role_access', return_value=False):
        notify_admins_of_registration(an_application())
    db.session.commit()

    assert Notification.query.filter_by(user_id=founder.id).count() == 1
    assert founder.unread_notifications == 1


def test_the_staff_are_told_when_the_permission_allows_it(app, env):
    client, founder = env
    give_role(founder, 4, 'Admin')
    staff = make_user(instance(), 'staff', local=True)
    give_role(staff, 3, 'Staff')

    with patch('app.auth.util.role_access', return_value=True):
        notify_admins_of_registration(an_application())
    db.session.commit()

    assert Notification.query.filter_by(user_id=staff.id).count() == 1


def test_the_staff_are_not_told_when_it_does_not(app, env):
    client, founder = env
    give_role(founder, 4, 'Admin')
    staff = make_user(instance(), 'staff', local=True)
    give_role(staff, 3, 'Staff')

    with patch('app.auth.util.role_access', return_value=False):
        notify_admins_of_registration(an_application())
    db.session.commit()

    assert Notification.query.filter_by(user_id=staff.id).count() == 0


def test_somebody_who_is_both_is_told_once(app, env):
    """D1162. Two loops, one notification each: the founder of a small
    instance is usually both, and was told twice about one application with
    their unread counter moved by two. Measured: `PROBE az4 notifications: 2 |
    unread counter: 2`."""
    client, founder = env
    give_role(founder, 4, 'Admin')
    give_role(founder, 3, 'Staff')

    with patch('app.auth.util.role_access', return_value=True):
        notify_admins_of_registration(an_application())
    db.session.commit()

    assert Notification.query.filter_by(user_id=founder.id).count() == 1
    assert founder.unread_notifications == 1


def test_the_plugins_are_told_too(app, env):
    client, founder = env

    with patch('app.auth.util.plugins.fire_hook') as fired:
        with patch('app.auth.util.role_access', return_value=False):
            application = an_application()
            notify_admins_of_registration(application)

    assert fired.call_args.args == ('new_registration_for_approval',
                                    application)


def test_an_application_waits_for_the_email_to_be_verified(app, env):
    """status -1: an application cannot be approved before the address behind
    it is proved, so nobody is asked to look at it yet."""
    client, founder = env
    give_role(founder, 4, 'Admin')
    applicant = make_user(instance(), 'applicant', local=True)
    applicant.verified = False
    db.session.commit()

    with patch('app.auth.util.get_setting', return_value=True):
        application = create_registration_application(applicant, 'because')
    db.session.commit()

    assert application.status == -1
    assert Notification.query.filter_by(user_id=founder.id).count() == 0


def test_a_verified_application_goes_straight_to_the_queue(app, env):
    client, founder = env
    give_role(founder, 4, 'Admin')
    applicant = make_user(instance(), 'applicant', local=True)
    applicant.verified = True
    db.session.commit()

    with patch('app.auth.util.role_access', return_value=False):
        application = create_registration_application(applicant, 'because')
    db.session.commit()

    assert application.status == 0
    assert Notification.query.filter_by(user_id=founder.id).count() == 1


# --------------------------------------------------------------------------
# The registration steps
# --------------------------------------------------------------------------


def test_a_token_is_the_length_it_was_asked_for(app, env):
    client, founder = env

    assert len(random_token(16)) == 16
    assert random_token() != random_token()


def test_a_name_with_special_letters_is_normalised(app, env):
    """NFKC: `ﬁ` is one codepoint that means two letters, and the account has
    to answer to the name it is given."""
    client, founder = env
    form = MagicMock()
    form.user_name.data = 'ﬁrstperson'

    with app.test_request_context('/'):
        with patch('app.auth.util.flash') as flashed:
            normalize_username(form)

    assert form.user_name.data == 'firstperson'
    assert 'special letters' in str(flashed.call_args.args[0])


def test_an_ordinary_name_is_left_alone(app, env):
    client, founder = env
    form = MagicMock()
    form.user_name.data = 'firstperson'

    with app.test_request_context('/'):
        with patch('app.auth.util.flash') as flashed:
            normalize_username(form)

    assert form.user_name.data == 'firstperson'
    flashed.assert_not_called()


@pytest.mark.parametrize('agent,font', [
    ('Mozilla/5.0 (Windows NT 10.0; Win64; x64)', 'inter'),
    ('Mozilla/5.0 (X11; Linux x86_64)', ''),
])
def test_windows_gets_a_font_that_renders_there(app, env, agent, font):
    client, founder = env

    with app.test_request_context('/', headers={'User-Agent': agent}):
        assert get_font_preference() == font


def test_the_verification_link_is_logged_in_debug(app, env):
    """A developer with no mail server still needs the link."""
    client, founder = env
    founder.verification_token = 'a-token'
    db.session.commit()

    with app.test_request_context('/'):
        with patch('app.auth.util.send_verification_email'):
            with patch.dict(app.config, {'DEBUG': True}):
                with patch.object(app.logger, 'info') as logged:
                    send_email_verification(founder)

    assert 'a-token' in logged.call_args.args[0]


def test_the_link_is_not_logged_otherwise(app, env):
    client, founder = env

    with app.test_request_context('/'):
        with patch('app.auth.util.send_verification_email') as sent:
            with patch.dict(app.config, {'DEBUG': False}):
                with patch.object(app.logger, 'info') as logged:
                    send_email_verification(founder)

    sent.assert_called_once()
    logged.assert_not_called()


def test_an_application_is_checked_against_the_ban_lists(app, env):
    client, founder = env
    applicant = make_user(instance(), 'applicant', local=True)
    applicant.verified = True
    db.session.commit()
    form = MagicMock()
    form.question.data = 'because'

    with app.test_request_context('/'):
        with patch('app.auth.util.get_setting', return_value='ban.example'):
            with patch('app.shared.tasks.task_selector') as task:
                response = handle_user_application(applicant, form)

    assert response.headers['Location'] == '/auth/please_wait'
    assert task.call_args.args[0] == 'check_application'


def test_an_instance_with_no_ban_lists_asks_nobody(app, env):
    client, founder = env
    applicant = make_user(instance(), 'applicant', local=True)
    applicant.verified = True
    db.session.commit()
    form = MagicMock()
    form.question.data = 'because'

    with app.test_request_context('/'):
        with patch('app.auth.util.get_setting') as setting:
            setting.side_effect = lambda name, default=None: (
                '' if name == 'ban_check_servers' else default)
            with patch('app.shared.tasks.task_selector') as task:
                handle_user_application(applicant, form)

    task.assert_not_called()


def test_an_applicant_whose_email_is_unproved_is_sent_to_check_it(app, env):
    client, founder = env
    applicant = make_user(instance(), 'applicant', local=True)
    applicant.verified = False
    db.session.commit()
    form = MagicMock()
    form.question.data = 'because'

    with app.test_request_context('/'):
        with patch('app.auth.util.get_setting', return_value=True):
            response = handle_user_application(applicant, form)

    assert response.headers['Location'] == '/auth/check_email'


def test_the_application_question_is_asked_as_written(app, env):
    """The question is markdown, and the label carries the rendered form."""
    client, founder = env
    site = db.session.get(Site, 1)
    site.registration_mode = 'RequireApplication'
    site.application_question = 'Why do you want **in**?'
    db.session.commit()
    form = MagicMock()

    with app.test_request_context('/'):
        from flask import g
        g.site = db.session.get(Site, 1)
        with patch('app.auth.util.render_template', return_value='rendered'):
            render_registration_form(form)

    assert '<strong>in</strong>' in str(form.question.label.text)


class AForm:
    """`render_registration_form` does `del form.terms`, which a MagicMock
    swallows silently -- so the row that pins it needs a real object."""

    def __init__(self):
        self.terms = 'the terms box'
        self.question = MagicMock()


def test_the_terms_box_is_dropped_when_there_are_no_terms(app, env):
    client, founder = env
    form = AForm()

    with app.test_request_context('/'):
        from flask import g
        g.site = db.session.get(Site, 1)
        with patch('app.auth.util.render_template', return_value='rendered'):
            render_registration_form(form)

    assert not hasattr(form, 'terms')


def test_the_terms_box_stays_when_there_are_terms(app, env):
    client, founder = env
    db.session.get(Site, 1).tos_url = 'https://example.com/terms'
    db.session.commit()
    form = AForm()

    with app.test_request_context('/'):
        from flask import g
        g.site = db.session.get(Site, 1)
        with patch('app.auth.util.render_template', return_value='rendered'):
            render_registration_form(form)

    assert form.terms == 'the terms box'


def test_the_registration_form_is_handed_the_site(app, env):
    client, founder = env
    form = MagicMock()

    with app.test_request_context('/'):
        from flask import g
        g.site = db.session.get(Site, 1)
        with patch('app.auth.util.render_template',
                   return_value='rendered') as rendered:
            render_registration_form(form)

    assert rendered.call_args.kwargs['site'] is g.site


# --------------------------------------------------------------------------
# Where a login lands
# --------------------------------------------------------------------------


@pytest.mark.parametrize('low_bandwidth,expected', [(True, '1'), (False, '0')])
def test_the_bandwidth_choice_is_remembered(app, env, low_bandwidth, expected):
    client, founder = env

    with app.test_request_context('/'):
        response = app.response_class('ok')
        configure_bandwidth_cookies(response, low_bandwidth)

    assert f'low_bandwidth={expected}' in response.headers['Set-Cookie']


# --------------------------------------------------------------------------
# The account a directory login creates
# --------------------------------------------------------------------------


def test_an_ldap_account_is_created_verified(app, env):
    """It has already proved itself to the directory; there is no address
    here to verify."""
    client, founder = env

    with app.test_request_context('/'):
        from flask import g
        g.site = db.session.get(Site, 1)
        with patch('app.auth.util.finalize_user_setup'):
            user = create_new_user_from_ldap('newcomer',
                                             'newcomer@example.com',
                                             'a-password', '127.0.0.1')

    assert user.verified is True
    assert user.banned is False
    assert user.check_password('a-password')


def test_an_ldap_account_on_a_warning_instance_sees_everything(app, env):
    client, founder = env

    with app.test_request_context('/'):
        from flask import g
        g.site = db.session.get(Site, 1)
        with patch.dict(app.config, {'CONTENT_WARNING': True}):
            with patch('app.auth.util.finalize_user_setup'):
                user = create_new_user_from_ldap('newcomer',
                                                 'newcomer@example.com',
                                                 'a-password', '127.0.0.1')

    assert user.hide_nsfw == 0


# --------------------------------------------------------------------------
# The registration itself, end to end
# --------------------------------------------------------------------------


def register(client, **overrides):
    data = {'user_name': 'newperson', 'email': '',
            'real_email': 'new@example.com', 'password': 'a-good-password',
            'password2': 'a-good-password', 'timezone': 'UTC',
            'submit': 'Register'}
    data.update(overrides)
    return client.post('/auth/register', data=data)


def no_captcha():
    """Fact 529."""
    return patch('app.auth.forms.get_setting', return_value=False)


def verification(on):
    def setting(name, default=None):
        return on if name == 'email_verification' else default
    return patch('app.auth.util.get_setting', side_effect=setting)


def test_an_instance_that_verifies_addresses_sends_you_to_your_inbox(app, env):
    client, founder = env

    with no_captcha():
        with verification(True):
            with patch('app.auth.util.send_email_verification') as sent:
                with patch('app.auth.util.sync_user_with_ldap'):
                    response = register(client)

    assert response.headers['Location'] == '/auth/check_email'
    sent.assert_called_once()
    assert User.query.filter_by(user_name='newperson').first().verified is False


def test_an_instance_that_does_not_sends_you_to_onboarding(app, env):
    """And the account is set up and signed in on the way."""
    client, founder = env

    with no_captcha():
        with verification(False):
            with patch('app.auth.util.finalize_user_setup') as setup:
                with patch('app.auth.util.sync_user_with_ldap'):
                    response = register(client)

    assert response.headers['Location'] == '/auth/filter_selection'
    made = User.query.filter_by(user_name='newperson').first()
    assert made.verified is True
    setup.assert_called_once()


def test_the_directory_is_written_once_per_registration(app, env):
    """D1163. `register_new_user` syncs, and `finalize_user_registration` used
    to sync again with the same three arguments -- a second bind, search and
    modify for every registration. Measured: `PROBE az5 sync_user_to_ldap
    calls: 2`."""
    client, founder = env

    with no_captcha():
        with verification(False):
            with patch('app.auth.util.finalize_user_setup'):
                with patch('app.auth.util.sync_user_to_ldap') as wrote:
                    register(client)

    assert wrote.call_count == 1


def test_a_directory_that_refuses_does_not_stop_a_registration(app, env):
    """The instance's own account is made whatever the directory says; the
    failure goes to the log."""
    client, founder = env

    with no_captcha():
        with verification(False):
            with patch('app.auth.util.finalize_user_setup'):
                with patch('app.auth.util.sync_user_to_ldap',
                           side_effect=Exception('directory is down')):
                    with patch.object(app.logger, 'error') as logged:
                        response = register(client)

    assert response.headers['Location'] == '/auth/filter_selection'
    assert 'LDAP sync failed' in logged.call_args.args[0]


def test_a_warning_instance_registers_you_seeing_everything(app, env):
    client, founder = env

    with no_captcha():
        with verification(False):
            with patch.dict(app.config, {'CONTENT_WARNING': True}):
                with patch('app.auth.util.finalize_user_setup'):
                    with patch('app.auth.util.sync_user_with_ldap'):
                        register(client)

    assert User.query.filter_by(user_name='newperson').first().hide_nsfw == 0


# --------------------------------------------------------------------------
# Where a login lands, continued
# --------------------------------------------------------------------------


def test_a_safe_next_page_is_taken_as_it_is(app, env):
    """`?next=` is attacker-supplied, so an off-origin one is dropped -- but a
    safe one is used as it is."""
    from app.auth.util import determine_next_page

    client, founder = env

    with app.test_request_context('/auth/login?next=/somewhere'):
        assert determine_next_page() == '/somewhere'


def test_an_off_origin_next_page_is_dropped(app, env):
    from app.auth.util import determine_next_page

    client, founder = env
    founder.finished_onboarding = True
    db.session.commit()

    with app.test_request_context('/auth/login?next=https://evil.example/'):
        with patch('app.auth.util.current_user', founder):
            assert determine_next_page() == '/home'


def test_somebody_who_has_not_onboarded_is_sent_to_do_it(app, env):
    from app.auth.util import determine_next_page

    client, founder = env
    founder.finished_onboarding = False
    db.session.commit()

    with app.test_request_context('/auth/login'):
        with patch('app.auth.util.current_user', founder):
            assert determine_next_page() == '/auth/filter_selection'


def test_a_login_records_where_and_when(app, env):
    from app.auth.util import update_user_session

    client, founder = env
    founder.timezone = None
    db.session.commit()
    form = MagicMock()
    form.timezone.data = 'Europe/London'

    with app.test_request_context('/'):
        update_user_session(founder, form, '1.2.3.4', 'GB')

    assert founder.ip_address == '1.2.3.4'
    assert founder.ip_address_country == 'GB'
    assert founder.timezone == 'Europe/London'
    assert founder.last_seen is not None


def test_a_timezone_already_known_is_not_overwritten(app, env):
    from app.auth.util import update_user_session

    client, founder = env
    founder.timezone = 'Europe/Berlin'
    founder.ip_address_country = 'DE'
    db.session.commit()
    form = MagicMock()
    form.timezone.data = 'Europe/London'

    with app.test_request_context('/'):
        update_user_session(founder, form, '1.2.3.4', '')

    assert founder.timezone == 'Europe/Berlin'
    assert founder.ip_address_country == 'DE'


def test_the_login_form_carries_the_configured_providers(app, env):
    from app.auth.util import render_login_form

    client, founder = env

    with app.test_request_context('/'):
        with patch('app.auth.util.render_template',
                   return_value='rendered') as rendered:
            render_login_form(MagicMock())

    assert set(rendered.call_args.kwargs) >= {'google_oauth', 'mastodon_oauth',
                                              'discord_oauth'}


def test_a_registration_that_needs_approval_waits(app, env):
    """The third arm of `register_new_user`: the account exists, the
    application is filed, and nobody is signed in."""
    client, founder = env
    site = db.session.get(Site, 1)
    site.registration_mode = 'RequireApplication'
    site.application_question = 'Why do you want to join?'
    db.session.commit()

    with no_captcha():
        with verification(False):
            with patch('app.auth.util.sync_user_with_ldap'):
                response = register(client, question='because I would like to')

    assert response.headers['Location'] == '/auth/please_wait'
    made = User.query.filter_by(user_name='newperson').first()
    assert UserRegistration.query.filter_by(user_id=made.id).first() is not None


def test_an_account_still_waiting_is_not_signed_in(app, env):
    """`finalize_user_registration` does nothing for an account that is
    unverified or still waiting on a moderator."""
    from app.auth.util import finalize_user_registration

    client, founder = env
    applicant = make_user(instance(), 'applicant', local=True)
    applicant.verified = False
    db.session.commit()

    with app.test_request_context('/'):
        with patch('app.auth.util.finalize_user_setup') as setup:
            with patch('app.auth.util.login_user') as logged_in:
                finalize_user_registration(applicant, MagicMock())

    setup.assert_not_called()
    logged_in.assert_not_called()


def test_a_door_somebody_else_already_closed_is_left_alone(app, env):
    """D1137's guard: `g.site` is a transient copy, so it can say Open while
    the row says otherwise -- another worker having just closed it, for
    instance. Nothing is written then, and the memoized copy is left alone."""
    from app.auth.util import handle_abandoned_open_instance

    client, founder = env
    db.session.get(Site, 1).registration_mode = 'Closed'
    db.session.commit()

    with app.test_request_context('/'):
        from flask import g
        g.site = db.session.get(Site, 1)
        g.site.registration_mode = 'Open'
        with patch('app.auth.util.no_admins_logged_in_recently',
                   return_value=True):
            with patch('app.auth.util.cache') as cached:
                handle_abandoned_open_instance()

    cached.delete_memoized.assert_not_called()


# --------------------------------------------------------------------------
# The last of app/auth/routes.py
# --------------------------------------------------------------------------


def test_verifying_an_address_for_somebody_already_in_communities(app, env):
    """The other arm of the landing choice: an account that has joined
    something goes to the home page rather than back to onboarding."""
    client, founder = env
    person = make_user(instance(), 'person', local=True)
    person.verified = False
    person.verification_token = 'a-token'
    person.private_key = 'a-key'
    db.session.commit()
    make_community_member(person, make_community('general'))
    db.session.commit()

    response = client.get('/auth/verify_email/a-token')

    assert response.headers['Location'] == '/home'
    assert person.verified is True


def test_a_resend_keeps_the_token_the_account_already_has(app, env):
    """Replacing it would invalidate the link already in somebody's inbox."""
    client, founder = env
    person = make_user(instance(), 'person', local=True)
    person.email = 'person@example.com'
    person.verification_token = 'the-first-token'
    db.session.commit()

    with patch('app.auth.routes.send_email_verification'):
        client.post('/auth/resend_email', data={'email': 'person@example.com',
                                                'submit': 'Resend'})

    assert person.verification_token == 'the-first-token'


def test_an_account_with_no_token_is_given_one(app, env):
    """"or else verification is impossible" -- an OAuth-created account has
    an empty token."""
    client, founder = env
    person = make_user(instance(), 'person', local=True)
    person.email = 'person@example.com'
    person.verification_token = ''
    db.session.commit()

    with patch('app.auth.routes.send_email_verification'):
        client.post('/auth/resend_email', data={'email': 'person@example.com',
                                                'submit': 'Resend'})

    assert len(person.verification_token) == 16


@pytest.mark.parametrize('provider', ['google', 'mastodon', 'discord'])
def test_connecting_a_provider_asks_it_for_an_authorization(app, env,
                                                            provider):
    """The six connect routes' own bodies: the redirect out to the provider,
    naming the callback it should come back to."""
    client, founder = env
    signed_in = app.test_client()
    with signed_in.session_transaction() as session:
        session['_user_id'] = str(founder.id)
        session['_fresh'] = True

    with patch('app.auth.routes.oauth') as fake:
        fake_provider = getattr(fake, provider)
        fake_provider.authorize_redirect.return_value = 'off you go'
        signed_in.get(f'/auth/{provider}_connect')

    assert fake_provider.authorize_redirect.call_args.kwargs['redirect_uri'] \
        .endswith(f'/auth/{provider}_connect_callback')
