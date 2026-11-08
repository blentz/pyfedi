"""PATCH /api/v1/accounts/update_credentials: display name, bio, avatar, banner and profile fields from a Mastodon
app (plan W1.1; spec docs/superpowers/specs/2026-11-07-mastodon-client-api-design.md §3.2); and GET
/api/v1/apps/verify_credentials, the app's own check of its token."""
import io
from unittest.mock import patch

import pytest
from werkzeug.exceptions import BadRequest

from app import db
from app.models import File, User, UserExtraField
from app.oauth_server import tokens
from tests.factories import make_user

PATH = '/api/v1/accounts/update_credentials'


@pytest.fixture
def world(app, api_baseline):
    registered, _secret = tokens.register_app('Tusky', ['https://app.example/cb'], 'read write follow',
                                              'https://tusky.app')
    me = make_user(None, 'mastoedit', local=True)
    access, _refresh = tokens.issue_token(registered, me, 'read write')
    return {'app': registered, 'me': me, 'token': access, 'client': app.test_client()}


def _patch(world, token=None, **kwargs):
    return world['client'].patch(PATH, headers={'Authorization': f"Bearer {token or world['token']}"}, **kwargs)


def _me(world):
    db.session.expire_all()
    return db.session.get(User, world['me'].id)


def test_a_form_changes_the_name_bio_and_flags(world):
    response = _patch(world, data={'display_name': '  New Name ', 'note': 'I like **pie**', 'bot': 'true',
                                   'discoverable': 'false', 'indexable': '0'})
    assert response.status_code == 200, response.get_data(as_text=True)
    account = response.get_json()
    me = _me(world)
    assert me.title == 'New Name' and account['display_name'] == 'New Name'
    assert me.about == 'I like **pie**' and '<strong>pie</strong>' in me.about_html
    assert account['note'] == me.about_html and account['source']['note'] == 'I like **pie**'
    assert me.bot is True and me.searchable is False and me.indexable is False


def test_json_works_too_and_fields_replace_the_old_ones(world):
    response = _patch(world, json={'display_name': 'Json', 'bot': False, 'fields_attributes': [
        {'name': 'Site', 'value': 'https://me.example'}, {'name': '', 'value': ''}, {'name': 'Pie', 'value': 'apple'}]})
    assert response.status_code == 200, response.get_data(as_text=True)
    assert [(field.label, field.text) for field in _me(world).extra_fields] == [('Site', 'https://me.example'),
                                                                                 ('Pie', 'apple')]
    assert response.get_json()['fields'][1] == {'name': 'Pie', 'value': 'apple', 'verified_at': None}


def test_form_fields_come_numbered_as_mastodon_sends_them(world):
    response = _patch(world, data={'fields_attributes[1][name]': 'Second', 'fields_attributes[1][value]': 'two',
                                   'fields_attributes[0][name]': 'First', 'fields_attributes[0][value]': 'one'})
    assert response.status_code == 200
    assert [field.label for field in _me(world).extra_fields] == ['First', 'Second']


def test_a_json_object_of_numbered_fields_is_taken(world):
    response = _patch(world, json={'fields_attributes': {'0': {'name': 'A', 'value': 'a'}}})
    assert response.status_code == 200
    assert [field.label for field in _me(world).extra_fields] == ['A']


def test_what_is_left_out_is_left_alone(world):
    me = world['me']
    me.title, me.about = 'Kept', 'Kept bio'
    db.session.commit()
    assert _patch(world, data={'bot': 'true'}).status_code == 200
    me = _me(world)
    assert me.title == 'Kept' and me.about == 'Kept bio'


def test_an_empty_display_name_falls_back_to_the_username(world):
    assert _patch(world, data={'display_name': ''}).get_json()['display_name'] == 'mastoedit'


@pytest.mark.parametrize('fields', [
    {'display_name': 'x' * 256},
    {'note': 'x' * 5001},
    {'fields_attributes[0][name]': 'x' * 1025, 'fields_attributes[0][value]': 'v'},
    {**{f'fields_attributes[{i}][name]': f'n{i}' for i in range(5)},
     **{f'fields_attributes[{i}][value]': f'v{i}' for i in range(5)}},
], ids=['long-name', 'long-note', 'long-field', 'five-fields'])
def test_too_much_is_refused_and_changes_nothing(world, fields):
    response = _patch(world, data=fields)
    assert response.status_code == 422
    assert response.get_json()['error'].startswith('Validation failed')
    assert _me(world).title is None and list(_me(world).extra_fields) == []


@pytest.mark.parametrize('body', [{'display_name': 5}, {'note': ['x']}, {'fields_attributes': 'x'},
                                  {'fields_attributes': [5]}, {'fields_attributes': [{'name': 5, 'value': 'v'}]},
                                  ['display_name']],
                         ids=['number-name', 'list-note', 'string-fields', 'number-field', 'number-field-name',
                              'not-an-object'])
def test_a_json_value_of_the_wrong_kind_is_refused(world, body):
    assert _patch(world, json=body).status_code == 422


def test_an_avatar_and_header_upload_replace_the_old(world, monkeypatch):
    saved = []

    def save(upload, directory):
        saved.append((upload.filename, directory))
        made = File(file_path=f'app/static/media/users/xx/{upload.filename}')
        db.session.add(made)
        db.session.flush()
        return made

    with patch('app.user.profile_images.save_icon_file', side_effect=save), \
            patch('app.user.profile_images.save_banner_file', side_effect=save):
        response = _patch(world, data={'avatar': (io.BytesIO(b'png'), 'me.png'),
                                       'header': (io.BytesIO(b'png'), 'banner.png')},
                          content_type='multipart/form-data')
    assert response.status_code == 200, response.get_data(as_text=True)
    assert saved == [('me.png', 'users'), ('banner.png', 'users')]
    me = _me(world)
    assert me.avatar.file_path.endswith('me.png') and me.cover.file_path.endswith('banner.png')
    assert response.get_json()['avatar'].endswith('/static/media/users/xx/me.png')


def test_an_upload_that_is_not_an_image_is_refused_before_anything_changes(world):
    with patch('app.user.profile_images.save_icon_file', side_effect=BadRequest()):
        response = _patch(world, data={'display_name': 'Changed', 'avatar': (io.BytesIO(b'no'), 'x.png')},
                          content_type='multipart/form-data')
    assert response.status_code == 422
    assert _me(world).title is None


def test_an_empty_file_field_is_no_upload(world):
    response = _patch(world, data={'avatar': (io.BytesIO(b''), '')}, content_type='multipart/form-data')
    assert response.status_code == 200
    assert _me(world).avatar_id is None


def test_changing_the_profile_needs_write_accounts(world):
    reader, _refresh = tokens.issue_token(world['app'], world['me'], 'read')
    assert _patch(world, token=reader, data={'display_name': 'No'}).status_code == 403
    narrow, _refresh = tokens.issue_token(world['app'], world['me'], 'write:accounts')
    assert _patch(world, token=narrow, data={'display_name': 'Yes'}).status_code == 200


def test_an_app_checks_its_own_token(world):
    response = world['client'].get('/api/v1/apps/verify_credentials',
                                   headers={'Authorization': f"Bearer {world['token']}"})
    assert response.status_code == 200
    body = response.get_json()
    assert body['name'] == 'Tusky' and body['website'] == 'https://tusky.app'
    assert body['scopes'] == ['read', 'write', 'follow'] and body['redirect_uris'] == ['https://app.example/cb']
    assert 'client_secret' not in body and 'client_id' not in body


def test_an_apps_own_token_checks_out_too(world):
    app_token, _none = tokens.issue_token(world['app'], None, 'read', with_refresh=False)
    response = world['client'].get('/api/v1/apps/verify_credentials', headers={'Authorization': f'Bearer {app_token}'})
    assert response.status_code == 200
    assert world['client'].get('/api/v1/apps/verify_credentials').status_code == 401


def test_json_without_fields_keeps_the_old_ones(world):
    world['me'].extra_fields.append(UserExtraField(label='Kept', text='yes'))
    db.session.commit()
    assert _patch(world, json={'bot': True}).status_code == 200
    assert [field.label for field in _me(world).extra_fields] == ['Kept']
