"""Step 3 media pipeline, task 1: the audio upload switch, the shared
`upload_access_allows` rule and the three admin settings (spec §3.5, §5.1)."""
import pytest
from unittest.mock import patch

from app import db
from app.models import Role, user_role
from app.utils import can_upload_audio, can_upload_video, get_setting, set_setting, upload_access_allows
from tests.factories import make_instance, make_user
from tests.test_admin_routes_entry import (_language, _seed, _settings_admin, _valid_misc_payload, csrf, login)


def _users():
    """'first' is id 1, 'admin' holds a real Admin role, 'plain' is an ordinary local user."""
    instance = make_instance('test.piefed.local', software='piefed')
    first = make_user(instance, 'first', local=True)
    assert first.id == 1
    admin = make_user(instance, 'adminuser', local=True)
    role = Role(name='Admin', weight=0)
    db.session.add(role)
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=admin.id, role_id=role.id))
    db.session.commit()
    plain = make_user(instance, 'plainuser', local=True)
    return {'first': first, 'admin': admin, 'plain': plain}


CASES = [
    ('no', 'admin', False), ('no', 'first', False),
    ('user 1', 'first', True), ('user 1', 'admin', False),
    ('admins', 'admin', True), ('admins', 'plain', False),
    ('users', 'plain', True),
]


@pytest.mark.parametrize('value, who, expected', CASES)
def test_audio_switch_takes_the_video_values(db_session, value, who, expected):
    users = _users()
    set_setting('allow_audio_file_uploads', value)
    assert can_upload_audio(users[who]) is expected


@pytest.mark.parametrize('value, who, expected', CASES)
def test_video_switch_is_unchanged(db_session, value, who, expected):
    users = _users()
    set_setting('allow_video_file_uploads', value)
    assert can_upload_video(users[who]) is expected


def test_the_two_switches_are_independent(db_session):
    users = _users()
    set_setting('allow_video_file_uploads', 'users')
    set_setting('allow_audio_file_uploads', 'no')
    assert can_upload_video(users['plain']) is True
    assert can_upload_audio(users['plain']) is False


def test_audio_switch_defaults_to_no(db_session):
    assert can_upload_audio(make_user(make_instance('test.piefed.local'), 'u', local=True)) is False


def test_upload_access_allows_uses_current_user_when_none_is_given(app, db_session):
    users = _users()
    set_setting('allow_audio_file_uploads', 'users')
    with app.test_request_context('/'):
        assert upload_access_allows('allow_audio_file_uploads') is False
    with app.test_request_context('/'), patch('app.utils.current_user', users['plain']):
        assert upload_access_allows('allow_audio_file_uploads') is True


def test_the_audio_switch_is_a_template_global(app):
    assert app.jinja_env.globals['can_upload_audio'] is can_upload_audio


@pytest.fixture
def csrf_enabled(app, monkeypatch):
    """admin/misc.html renders `form.csrf_token()`, which exists only with CSRF on."""
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)


@pytest.fixture
def misc_admin(app, db_session):
    instance, _ordinary = _seed()
    admin = _settings_admin(instance)
    language = _language()
    client = app.test_client()
    login(client, admin)
    return client, csrf(app, client), language


def test_the_misc_form_saves_the_audio_switch_and_both_limits_in_bytes(app, misc_admin):
    client, token, language = misc_admin
    payload = _valid_misc_payload(language)
    payload.update({'submit': 'Save', 'csrf_token': token, 'allow_audio_file_uploads': 'admins',
                    'max_video_upload_mb': '100', 'max_audio_upload_mb': '50'})
    with patch('app.admin.routes.render_template', return_value='rendered') as rt:
        client.post('/admin/misc', data=payload)
    assert rt.call_args.kwargs['form'].errors == {}
    assert get_setting('allow_audio_file_uploads', 'no') == 'admins'
    assert get_setting('max_video_upload_bytes', 524288000) == 104857600
    assert get_setting('max_audio_upload_bytes', 209715200) == 52428800


def test_the_misc_form_keeps_a_limit_left_blank(app, misc_admin):
    client, token, language = misc_admin
    set_setting('max_video_upload_bytes', 3145728)
    payload = _valid_misc_payload(language)
    payload.update({'submit': 'Save', 'csrf_token': token, 'max_video_upload_mb': '',
                    'max_audio_upload_mb': ''})
    with patch('app.admin.routes.render_template', return_value='rendered') as rt:
        client.post('/admin/misc', data=payload)
    assert rt.call_args.kwargs['form'].errors == {}
    assert get_setting('max_video_upload_bytes', 524288000) == 3145728
    assert get_setting('max_audio_upload_bytes', 209715200) == 209715200


@pytest.mark.parametrize('field, value', [('max_video_upload_mb', '0'), ('max_audio_upload_mb', '10241')])
def test_the_misc_form_refuses_an_out_of_range_limit(app, misc_admin, field, value):
    client, token, language = misc_admin
    payload = _valid_misc_payload(language)
    payload.update({'submit': 'Save', 'csrf_token': token, field: value})
    with patch('app.admin.routes.render_template', return_value='rendered') as rt:
        client.post('/admin/misc', data=payload)
    assert list(rt.call_args.kwargs['form'].errors) == [field]


def test_the_misc_form_prefills_the_audio_switch_and_both_limits(app, misc_admin):
    client, _token, _language_row = misc_admin
    set_setting('allow_audio_file_uploads', 'users')
    set_setting('max_video_upload_bytes', 104857600)
    set_setting('max_audio_upload_bytes', 52428800)
    with patch('app.admin.routes.render_template', return_value='rendered') as rt:
        client.get('/admin/misc')
    form = rt.call_args.kwargs['form']
    assert form.allow_audio_file_uploads.data == 'users'
    assert form.max_video_upload_mb.data == 100
    assert form.max_audio_upload_mb.data == 50


def test_the_misc_form_prefills_the_defaults(app, misc_admin):
    client, _token, _language_row = misc_admin
    with patch('app.admin.routes.render_template', return_value='rendered') as rt:
        client.get('/admin/misc')
    form = rt.call_args.kwargs['form']
    assert form.allow_audio_file_uploads.data == 'no'
    assert form.max_video_upload_mb.data == 500
    assert form.max_audio_upload_mb.data == 200


def test_the_misc_page_renders_the_new_fields(app, misc_admin, csrf_enabled):
    client, _token, _language_row = misc_admin
    body = client.get('/admin/misc').get_data(as_text=True)
    for name in ('allow_audio_file_uploads', 'max_video_upload_mb', 'max_audio_upload_mb'):
        assert f'name="{name}"' in body
