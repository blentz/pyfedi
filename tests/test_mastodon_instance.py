"""Mastodon's instance documents, which an app reads before anyone signs in, to size its composer and uploads
(plan W1.1; spec docs/superpowers/specs/2026-11-07-mastodon-client-api-design.md §3.2; Tusky needs v2's `domain` and
`version`, plan W1.0)."""
import pytest
from flask import current_app

from app import cache, db
from app.models import Site
from tests.test_login_gate import private  # noqa: F401  the private-instance fixture


@pytest.fixture
def site(app, api_baseline):
    site = db.session.get(Site, 1)
    site.name, site.description, site.contact_email = 'Probeland', 'A place to probe', 'admin@probe.example'
    site.about_html = '<p>All about it</p>'
    site.registration_mode = 'Open'
    db.session.commit()
    cache.clear()
    return site


def _get(app, path):
    response = app.test_client().get(path)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def test_v2_is_what_apps_need_to_log_in_and_compose(app, site):
    document = _get(app, '/api/v2/instance')
    assert document['domain'] == current_app.config['SERVER_NAME']
    assert document['title'] == 'Probeland' and document['description'] == 'A place to probe'
    assert document['version'].startswith('4.0.0 (compatible; PieFed ')
    assert document['source_url'].startswith('https://')
    statuses = document['configuration']['statuses']
    assert statuses['max_characters'] == 5000 and statuses['max_media_attachments'] == 4
    media = document['configuration']['media_attachments']
    assert 'image/jpeg' in media['supported_mime_types'] and 'video/mp4' in media['supported_mime_types']
    assert media['image_size_limit'] == current_app.config['MAX_CONTENT_LENGTH']
    assert document['configuration']['translation'] == {'enabled': False}
    assert document['registrations'] == {'enabled': True, 'approval_required': False, 'message': None}
    assert document['contact'] == {'email': 'admin@probe.example', 'account': None}
    assert document['rules'] == []
    assert document['thumbnail']['url'].startswith('http')
    assert isinstance(document['usage']['users']['active_month'], int)


def test_v2_says_when_registration_is_by_application(app, site):
    site.registration_mode = 'RequireApplication'
    site.application_question = 'Why?'
    db.session.commit()
    cache.clear()
    assert _get(app, '/api/v2/instance')['registrations'] == {'enabled': True, 'approval_required': True,
                                                              'message': None}


def test_v1_keeps_what_it_said_and_says_the_rest(app, site):
    document = _get(app, '/api/v1/instance')
    assert document['uri'] == current_app.config['SERVER_NAME'] and document['title'] == 'Probeland'
    assert document['registrations'] is True and document['approval_required'] is False
    assert {'user_count', 'status_count', 'domain_count'} <= set(document['stats'])
    assert document['short_description'] == 'A place to probe'
    assert document['description'] == '<p>All about it</p>'
    assert document['email'] == 'admin@probe.example'
    assert document['version'].startswith('4.0.0 (compatible; PieFed ')
    assert document['configuration']['statuses']['max_characters'] == 5000
    assert document['invites_enabled'] is False and document['contact_account'] is None and document['rules'] == []
    assert document['thumbnail'].startswith('http')


@pytest.mark.parametrize('path', ['/api/v2/instance', '/api/v1/instance'])
def test_both_are_open_on_a_private_instance(app, private, path):
    """An app reads them before it can send anyone to log in."""
    assert app.test_client().get(path).status_code == 200
