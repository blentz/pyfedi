"""Community.kind on create and edit, web and API (spec 2026-10-14 §3.2, §10 A15.1-2)."""
import io
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import current_app, g, url_for

from app import db
from app.constants import (COMMUNITY_KIND_DISCUSSION, COMMUNITY_KIND_MICROBLOGS, COMMUNITY_KIND_PODCAST,
                           COMMUNITY_KIND_VIDEO_CHANNEL)
from app.models import Community, CommunityMember, Instance, Language, Site, utcnow
from app.shared.community import apply_kind
from tests.factories import (a_keypair, make_community, make_community_member, make_instance, make_post,
                             make_user)

OWN_REFUSAL = 'kind can change only while the community has no posts'
KIND_REFUSAL = 'kind must be one of discussion, video_channel, podcast'


@pytest.fixture
def owner(app, db_session, site):
    make_instance('test.piefed.local')
    return make_user(None, 'alice', local=True)


def test_an_empty_community_takes_any_offered_kind(owner):
    community = make_community('vc')
    for kind in (COMMUNITY_KIND_VIDEO_CHANNEL, COMMUNITY_KIND_PODCAST, COMMUNITY_KIND_DISCUSSION):
        assert apply_kind(community, kind, owner) is None
        assert community.kind == kind


def test_microblogs_is_never_a_choice(owner):
    community = make_community('vc')
    assert apply_kind(community, COMMUNITY_KIND_MICROBLOGS, owner) == KIND_REFUSAL
    assert community.kind == COMMUNITY_KIND_DISCUSSION


def test_the_microblogs_community_keeps_its_kind(owner):
    community = make_community('microblogs')
    community.kind = COMMUNITY_KIND_MICROBLOGS
    assert apply_kind(community, COMMUNITY_KIND_VIDEO_CHANNEL, owner) == 'the microblogs community keeps its kind'
    assert community.kind == COMMUNITY_KIND_MICROBLOGS


def test_a_community_with_a_post_keeps_its_kind(owner):
    community = make_community('vc')
    post = make_post(community, owner, ap_id='https://test.piefed.local/post/1')
    post.deleted = True                       # deleted posts count too
    db.session.commit()
    assert apply_kind(community, COMMUNITY_KIND_VIDEO_CHANNEL, owner) == OWN_REFUSAL
    assert community.kind == COMMUNITY_KIND_DISCUSSION
    assert apply_kind(community, COMMUNITY_KIND_DISCUSSION, owner) is None   # unchanged is always fine


def test_a_new_video_channel_without_an_owner_takes_its_owner_member(owner):
    community = make_community('vc')
    community.user_id = None
    other = make_user(None, 'bob', local=True)
    db.session.add_all([CommunityMember(user_id=owner.id, community_id=community.id, is_moderator=True, is_owner=True,
                                        created_at=utcnow() - timedelta(days=1)),
                        CommunityMember(user_id=other.id, community_id=community.id, is_moderator=True, is_owner=True)])
    db.session.commit()
    assert apply_kind(community, COMMUNITY_KIND_VIDEO_CHANNEL, other) is None
    assert community.user_id == owner.id      # the earliest owner membership, not the editor


def test_with_no_owner_member_the_editor_becomes_the_owner(owner):
    community = make_community('vc')
    community.user_id = None
    db.session.commit()
    assert apply_kind(community, COMMUNITY_KIND_VIDEO_CHANNEL, owner) is None
    assert community.user_id == owner.id


def test_an_existing_owner_is_kept(owner):
    community = make_community('vc')
    first = make_user(None, 'carol', local=True)
    community.user_id = first.id
    db.session.commit()
    assert apply_kind(community, COMMUNITY_KIND_VIDEO_CHANNEL, owner) is None
    assert community.user_id == first.id


def test_a_podcast_does_not_set_an_owner(owner):
    community = make_community('pod')
    community.user_id = None
    db.session.commit()
    assert apply_kind(community, COMMUNITY_KIND_PODCAST, owner) is None
    assert community.user_id is None


# --------------------------------------------------------------------------
# The web forms
# --------------------------------------------------------------------------


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def csrf(app, client):
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf

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


def test_the_add_form_creates_a_video_channel_owned_by_its_creator(web):
    _add(web, kind='video_channel')

    community = Community.query.filter_by(name='brandnew').one()
    assert community.kind == COMMUNITY_KIND_VIDEO_CHANNEL
    assert community.user_id == web.founder.id


def test_the_add_form_defaults_to_discussion(web):
    _add(web)

    community = Community.query.filter_by(name='brandnew').one()
    assert community.kind == COMMUNITY_KIND_DISCUSSION
    assert community.user_id == web.founder.id


def test_the_add_form_refuses_microblogs(web):
    _response, render = _add(web, kind='microblogs')

    assert 'kind' in render.call_args.kwargs['form'].errors
    assert Community.query.filter_by(name='brandnew').count() == 0


def _get_form(web, endpoint, **values):
    """The form a GET hands its template (the templates need a CSRF token these tests leave off)."""
    with patch('app.community.routes.render_template', return_value='rendered') as render:
        web.client.get(url(web.app, endpoint, **values))
    return render.call_args.kwargs['form']


def _kind_select(web, form):
    with web.app.test_request_context():
        html = str(form.kind())
    return html[:html.index('>')], html


def test_the_add_page_offers_the_three_kinds(web):
    _opening, html = _kind_select(web, _get_form(web, 'community.add_local'))

    assert 'Video channel' in html and 'Podcast' in html and 'Discussion' in html
    assert 'microblogs' not in html


def test_the_edit_form_sets_the_kind_of_an_empty_community(web):
    _edit(web, kind='podcast')

    assert _stored(web.community.id).kind == COMMUNITY_KIND_PODCAST


def test_the_edit_form_refuses_a_kind_change_once_there_are_posts(web):
    make_post(web.community, web.founder, ap_id='https://test.piefed.local/post/1')

    _response, render = _edit(web, kind='video_channel', title='Renamed')

    assert OWN_REFUSAL in render.call_args.kwargs['form'].kind.errors
    stored = _stored(web.community.id)
    assert (stored.title, stored.kind) == ('general', COMMUNITY_KIND_DISCUSSION)


def test_the_edit_form_without_a_kind_keeps_it_once_there_are_posts(web):
    make_post(web.community, web.founder, ap_id='https://test.piefed.local/post/1')

    _edit(web, title='Renamed')

    stored = _stored(web.community.id)
    assert (stored.title, stored.kind) == ('Renamed', COMMUNITY_KIND_DISCUSSION)


def test_the_edit_page_disables_the_kind_once_there_are_posts(web):
    make_post(web.community, web.founder, ap_id='https://test.piefed.local/post/1')

    form = _get_form(web, 'community.community_edit', community_id=web.community.id)

    opening, _html = _kind_select(web, form)
    assert 'disabled' in opening
    assert form.kind.data == COMMUNITY_KIND_DISCUSSION


def test_the_edit_page_leaves_the_kind_open_while_there_are_no_posts(web):
    web.community.kind = COMMUNITY_KIND_PODCAST
    db.session.commit()

    form = _get_form(web, 'community.community_edit', community_id=web.community.id)

    opening, _html = _kind_select(web, form)
    assert 'disabled' not in opening
    assert form.kind.data == COMMUNITY_KIND_PODCAST


def test_the_edit_page_shows_the_microblogs_kind_read_only(web):
    web.community.kind = COMMUNITY_KIND_MICROBLOGS
    db.session.commit()

    form = _get_form(web, 'community.community_edit', community_id=web.community.id)

    opening, html = _kind_select(web, form)
    assert 'disabled' in opening
    assert 'Microblogs' in html and 'video_channel' not in html


def test_the_edit_form_keeps_the_microblogs_kind_when_none_is_sent(web):
    web.community.kind = COMMUNITY_KIND_MICROBLOGS
    db.session.commit()

    _edit(web, title='Renamed')

    stored = _stored(web.community.id)
    assert (stored.title, stored.kind) == ('Renamed', COMMUNITY_KIND_MICROBLOGS)


# --------------------------------------------------------------------------
# The API
# --------------------------------------------------------------------------


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


def test_the_api_creates_a_video_channel_owned_by_its_creator(api):
    response = _api_create(api, kind='video_channel')

    assert response.status_code == 200, response.get_json()
    community = Community.query.filter_by(name='newland').one()
    assert community.kind == COMMUNITY_KIND_VIDEO_CHANNEL
    assert community.user_id == api.maker.id


def test_the_api_refuses_microblogs_on_create(api):
    response = _api_create(api, kind='microblogs')

    assert response.status_code == 400
    assert Community.query.filter_by(name='newland').count() == 0


def test_the_api_create_defaults_to_discussion(api):
    response = _api_create(api)

    assert response.status_code == 200, response.get_json()
    community = Community.query.filter_by(name='newland').one()
    assert community.kind == COMMUNITY_KIND_DISCUSSION
    assert community.user_id == api.maker.id


def test_the_api_edit_sets_the_kind_of_an_empty_community(api):
    response = _api_edit(api, kind='podcast')

    assert response.status_code == 200, response.get_json()
    assert _stored(api.community.id).kind == COMMUNITY_KIND_PODCAST


def test_the_api_edit_refuses_a_kind_change_once_there_are_posts(api):
    make_post(api.community, api.moderator, ap_id='https://test.piefed.local/post/1')

    response = _api_edit(api, kind='video_channel', title='Renamed')

    assert response.status_code == 400
    assert OWN_REFUSAL in response.get_json()['message']
    stored = _stored(api.community.id)
    assert (stored.title, stored.kind) == ('probeland', COMMUNITY_KIND_DISCUSSION)


def test_the_api_edit_without_a_kind_keeps_it(api):
    make_post(api.community, api.moderator, ap_id='https://test.piefed.local/post/1')

    response = _api_edit(api, title='Renamed')

    assert response.status_code == 200, response.get_json()
    stored = _stored(api.community.id)
    assert (stored.title, stored.kind) == ('Renamed', COMMUNITY_KIND_DISCUSSION)
