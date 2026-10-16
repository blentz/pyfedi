"""Spec §3.3, §10 A17.5: a podcast's Apple Podcasts category."""
import importlib.util
import io
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from flask import current_app, g, url_for
from flask import session as flask_session
from flask_wtf.csrf import generate_csrf
from sqlalchemy import inspect

from app import db
from app.constants import APPLE_PODCAST_CATEGORIES
from app.models import Community, Language, Site, utcnow
from app.shared.community import apply_podcast_category
from tests.factories import (a_keypair, make_community, make_community_member, make_instance, make_user)

REFUSAL = 'podcast_category must be one of the Apple Podcasts categories'
MIGRATION = (Path(__file__).resolve().parent.parent / 'migrations' / 'versions'
             / 'e5b8d2f0a4c7_community_podcast_category.py')


def test_the_list_is_apples_19(db_session):
    assert len(APPLE_PODCAST_CATEGORIES) == 19
    assert 'Technology' in APPLE_PODCAST_CATEGORIES and 'True Crime' in APPLE_PODCAST_CATEGORIES


def test_apply_sets_clears_and_refuses(db_session):
    make_instance('test.piefed.local')
    make_user(None, 'alice', local=True)
    community = make_community('pod')
    assert apply_podcast_category(community, 'Technology') is None
    assert community.podcast_category == 'Technology'
    assert apply_podcast_category(community, 'Podcasting') == REFUSAL   # a subcategory, not offered
    assert community.podcast_category == 'Technology'
    assert apply_podcast_category(community, '') is None
    assert community.podcast_category is None
    assert apply_podcast_category(community, None) is None


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def csrf(app, client):
    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as session:
        session['csrf_token'] = raw
    return token


def url(app, endpoint, **values):
    with app.test_request_context():
        return url_for(endpoint, **values)


@pytest.fixture
def web(app, db_session, site):
    """User 1, an admin who clears every gate before `add_local`, who also owns `general`."""
    founder = make_user(make_instance('test.piefed.local'), 'founder', local=True)
    assert founder.id == 1
    founder.verified = True
    founder.private_key = 'a private key'
    founder.created = utcnow() - timedelta(days=30)
    db.session.add(Language(code='und', name='Undetermined'))
    english = Language(code='en', name='English')
    db.session.add(english)
    db.session.get(Site, 1).community_creation_admin_only = False
    community = make_community('general')
    membership = make_community_member(founder, community, is_moderator=True)
    membership.is_owner = True
    db.session.commit()
    client = app.test_client()
    login(client, founder)
    return SimpleNamespace(app=app, client=client, token=csrf(app, client), founder=founder, english=english,
                           community=community)


def _post(web, endpoint, payload, **values):
    payload = {'csrf_token': web.token, 'icon_file': (io.BytesIO(b''), ''), 'banner_file': (io.BytesIO(b''), ''),
               **payload}
    with patch('app.community.routes.render_template', return_value='rendered') as render:
        with patch('app.community.routes.task_selector'), patch('app.shared.community.task_selector'):
            response = web.client.post(url(web.app, endpoint, **values), data=payload,
                                       content_type='multipart/form-data')
    return response, render


def _add(web, **overrides):
    payload = {'community_name': 'A New Community', 'url': 'brandnew', 'description': '', 'posting_warning': '',
               'theme': 'disabled', 'invitations': '0', 'submit': 'Create', **overrides}
    return _post(web, 'community.add_local', payload)


def _edit(web, **overrides):
    payload = {'title': 'General', 'description': '', 'theme': 'disabled', 'posting_warning': '',
               'invitations': '0', 'languages': str(web.english.id), 'default_layout': '',
               'default_post_type': 'link', 'downvote_accept_mode': '0', 'post_url_type': 'friendly',
               'submit': 'Save', **overrides}
    return _post(web, 'community.community_edit', payload, community_id=web.community.id)


def _stored(community_id):
    db.session.expire_all()
    return db.session.get(Community, community_id)



def auth(user):
    return {'Authorization': f'Bearer {user.encode_jwt_token()}'}


@pytest.fixture
def api(app, api_baseline, monkeypatch):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
    for code, name in [('en', 'English'), ('und', 'Undetermined')]:
        db.session.add(Language(code=code, name=name))
    db.session.commit()
    community = make_community('probeland')
    moderator = api_baseline.user2
    moderator.private_key, moderator.public_key = a_keypair()
    maker = api_baseline.user1
    maker.private_key, maker.public_key = a_keypair()
    db.session.commit()
    membership = make_community_member(moderator, community, is_moderator=True)
    membership.is_owner = True
    db.session.commit()
    return SimpleNamespace(client=app.test_client(), community=community, moderator=moderator, maker=maker)


def _api_create(api, **fields):
    return api.client.post('/api/alpha/community', headers=auth(api.maker),
                           json={'name': 'newland', 'title': 'new land', **fields})


def _api_edit(api, **fields):
    return api.client.put('/api/alpha/community', headers=auth(api.moderator),
                          json={'community_id': api.community.id, **fields})



def test_create_form_sets_category(web):
    _add(web, podcast_category='Technology')

    assert Community.query.filter_by(name='brandnew').one().podcast_category == 'Technology'


def test_create_form_without_category_leaves_none(web):
    _add(web)

    assert Community.query.filter_by(name='brandnew').one().podcast_category is None


def test_edit_form_sets_and_refuses(web):
    _edit(web, podcast_category='Comedy')
    assert _stored(web.community.id).podcast_category == 'Comedy'

    _, render = _edit(web, podcast_category='Nonsense', title='Renamed')

    stored = _stored(web.community.id)
    assert (stored.title, stored.podcast_category) == ('General', 'Comedy')
    assert render.call_args.kwargs['form'].podcast_category.errors


def test_edit_form_get_shows_the_stored_category(web):
    web.community.podcast_category = 'News'
    db.session.commit()

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        web.client.get(url(web.app, 'community.community_edit', community_id=web.community.id))

    assert render.call_args.kwargs['form'].podcast_category.data == 'News'


def test_api_create_and_edit(api):
    response = _api_create(api, podcast_category='News')
    assert response.status_code == 200, response.get_json()
    assert Community.query.filter_by(name='newland').one().podcast_category == 'News'

    response = _api_edit(api, podcast_category='Sports')
    assert response.status_code == 200, response.get_json()
    assert _stored(api.community.id).podcast_category == 'Sports'

    response = _api_edit(api, podcast_category='Nonsense')
    assert response.status_code == 400
    assert _stored(api.community.id).podcast_category == 'Sports'

    response = _api_edit(api, title='Renamed')
    assert response.status_code == 200, response.get_json()
    stored = _stored(api.community.id)
    assert (stored.title, stored.podcast_category) == ('Renamed', 'Sports')

    response = _api_edit(api, podcast_category='')
    assert response.status_code == 200, response.get_json()
    assert _stored(api.community.id).podcast_category is None


def test_migration_adds_and_drops_the_column(app, db_session):
    spec = importlib.util.spec_from_file_location('community_podcast_category_migration', MIGRATION)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    db.session.close()
    with db.engine.connect() as connection:
        def columns():
            return {column['name']: column for column in inspect(connection).get_columns('community')}
        assert columns()['podcast_category']['nullable'] is True
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert 'podcast_category' not in columns()
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert 'podcast_category' in columns()
        connection.rollback()


def test_api_edit_refuses_what_the_shared_rule_refuses(api, monkeypatch):
    """The schema vets the value first; this is the rule's own refusal, should the two lists ever drift."""
    monkeypatch.setattr('app.shared.community.APPLE_PODCAST_CATEGORIES', ())

    response = _api_edit(api, podcast_category='News')

    assert response.status_code == 400
    assert REFUSAL in response.get_json()['message']
    assert _stored(api.community.id).podcast_category is None
