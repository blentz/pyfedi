"""Amendment B, S7.1: a local user can publish a followers-only post, in the local `microblogs` community only.

Visibility is chosen at creation and never changes. The web form offers the choice only in local microblogs, the API
accepts `visibility` and refuses `followers` anywhere else, and an edit that tries to change it is refused.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import current_app

from app import db
from app.constants import POST_TYPE_ARTICLE, SRC_API, SRC_WEB, VISIBILITY_FOLLOWERS, VISIBILITY_PUBLIC
from app.models import Language, Post
from app.shared.post import make_post, requested_visibility
from app.utils import is_local_microblogs
from tests.factories import bearer, make_community, make_community_member, make_instance, make_user
from tests.test_community_add_post import _post_payload, able_to_post, csrf, instance, login, url
from tests.test_shared_post_edit import _api_input
from tests.test_shared_post_make import seed_make_context


# --------------------------------------------------------------------------
# is_local_microblogs
# --------------------------------------------------------------------------


def test_the_local_microblogs_community_counts(db_session):
    make_user(make_instance('local.example', software='piefed'), 'founder', local=True)   # community.user_id = 1
    assert is_local_microblogs(make_community('microblogs')) is True


def test_another_local_community_does_not(db_session):
    make_user(make_instance('local.example', software='piefed'), 'founder', local=True)
    assert is_local_microblogs(make_community('general')) is False


def test_a_remote_community_called_microblogs_does_not(db_session):
    make_user(make_instance('local.example', software='piefed'), 'founder', local=True)
    community = make_community('microblogs', host='peer.example')
    community.ap_id = 'microblogs@peer.example'
    db.session.commit()
    assert is_local_microblogs(community) is False


# --------------------------------------------------------------------------
# requested_visibility: the one decision both producers share
# --------------------------------------------------------------------------


@pytest.fixture
def two_communities(db_session):
    make_user(make_instance('local.example', software='piefed'), 'founder', local=True)
    return SimpleNamespace(microblogs=make_community('microblogs'), general=make_community('general'))


@pytest.mark.parametrize('src, input_, expected', [
    (SRC_API, {}, VISIBILITY_PUBLIC),
    (SRC_API, {'visibility': None}, VISIBILITY_PUBLIC),
    (SRC_API, {'visibility': 'public'}, VISIBILITY_PUBLIC),
    (SRC_API, {'visibility': 'followers'}, VISIBILITY_FOLLOWERS),
    (SRC_WEB, SimpleNamespace(visibility=SimpleNamespace(data='followers')), VISIBILITY_FOLLOWERS),
    (SRC_WEB, SimpleNamespace(visibility=SimpleNamespace(data='public')), VISIBILITY_PUBLIC),
    (SRC_WEB, SimpleNamespace(), VISIBILITY_PUBLIC),   # a form built without the field (community/util.py:1180)
], ids=['api-absent', 'api-null', 'api-public', 'api-followers', 'web-followers', 'web-public', 'web-no-field'])
def test_microblogs_accepts_public_and_followers(two_communities, src, input_, expected):
    assert requested_visibility(input_, src, two_communities.microblogs) == expected


@pytest.mark.parametrize('src, input_', [
    (SRC_API, {'visibility': 'followers'}),
    (SRC_WEB, SimpleNamespace(visibility=SimpleNamespace(data='followers'))),
], ids=['api', 'web'])
def test_followers_outside_microblogs_is_invalid(two_communities, src, input_):
    with pytest.raises(Exception, match='invalid visibility'):
        requested_visibility(input_, src, two_communities.general)


@pytest.mark.parametrize('value', ['unlisted', 'direct', 'everyone', ''])
def test_any_other_value_is_invalid(two_communities, value):
    if value == '':
        # '' is falsy, so it reads as "not given" -- public, the default
        assert requested_visibility({'visibility': value}, SRC_API, two_communities.microblogs) == VISIBILITY_PUBLIC
        return
    with pytest.raises(Exception, match='invalid visibility'):
        requested_visibility({'visibility': value}, SRC_API, two_communities.microblogs)


# --------------------------------------------------------------------------
# make_post stores it (SRC_API, as tests/test_local_post_url_scheme.py drives it)
# --------------------------------------------------------------------------


def _api_seed(monkeypatch, community_name):
    s = seed_make_context(community_name)
    monkeypatch.setattr('app.shared.post.task_selector', lambda *a, **k: None)
    monkeypatch.setattr('app.shared.post.make_image_sizes', lambda *a, **k: None)
    monkeypatch.setattr('app.shared.post.authorise_api_user', lambda *a, **k: s.author)
    return s


def test_make_post_stores_followers_in_microblogs(db_session, monkeypatch):
    s = _api_seed(monkeypatch, 'microblogs')
    _, post = make_post(_api_input(visibility='followers'), s.community, POST_TYPE_ARTICLE, SRC_API, auth='t')
    assert db.session.get(Post, post.id).visibility == VISIBILITY_FOLLOWERS


def test_make_post_defaults_to_public(db_session, monkeypatch):
    s = _api_seed(monkeypatch, 'microblogs')
    _, post = make_post(_api_input(), s.community, POST_TYPE_ARTICLE, SRC_API, auth='t')
    assert db.session.get(Post, post.id).visibility == VISIBILITY_PUBLIC


def test_make_post_refuses_followers_elsewhere_before_writing_anything(db_session, monkeypatch):
    s = _api_seed(monkeypatch, 'general')
    with pytest.raises(Exception, match='invalid visibility'):
        make_post(_api_input(visibility='followers'), s.community, POST_TYPE_ARTICLE, SRC_API, auth='t')
    assert Post.query.count() == 0
    assert s.community.post_count == 0


# --------------------------------------------------------------------------
# The HTTP API: create refusal, edit immutability
# --------------------------------------------------------------------------


@pytest.fixture
def api_client(app, db_session, monkeypatch):
    s = seed_make_context('microblogs')
    general = make_community('general')
    monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
    monkeypatch.setattr('app.shared.post.task_selector', lambda *a, **k: None)
    return SimpleNamespace(client=app.test_client(), author=s.author, microblogs=s.community, general=general)


def test_api_create_with_followers_outside_microblogs_is_a_400(api_client):
    a = api_client
    response = a.client.post('/api/alpha/post', headers={'Authorization': bearer(a.author)},
                             json={'title': 'nope', 'community_id': a.general.id, 'visibility': 'followers'})
    assert response.status_code == 400
    assert response.get_json()['message'] == 'invalid visibility'
    assert Post.query.count() == 0


def test_api_create_with_a_bad_value_is_a_400(api_client):
    a = api_client
    response = a.client.post('/api/alpha/post', headers={'Authorization': bearer(a.author)},
                             json={'title': 'nope', 'community_id': a.microblogs.id, 'visibility': 'direct'})
    assert response.status_code == 400
    assert response.get_json()['message'] == 'invalid visibility'


def test_api_create_with_followers_in_microblogs_is_stored(api_client):
    a = api_client
    response = a.client.post('/api/alpha/post', headers={'Authorization': bearer(a.author)},
                             json={'title': 'for my followers', 'community_id': a.microblogs.id,
                                   'visibility': 'followers'})
    assert response.status_code == 200
    post = Post.query.filter_by(title='for my followers').one()
    assert post.visibility == VISIBILITY_FOLLOWERS


def _followers_post(a):
    with patch('app.shared.post.authorise_api_user', return_value=a.author):
        _, post = make_post(_api_input(visibility='followers', title='fixed'), a.microblogs, POST_TYPE_ARTICLE,
                            SRC_API, auth='t')
    return post


def test_api_edit_changing_visibility_is_a_400(api_client):
    a = api_client
    post = _followers_post(a)
    response = a.client.put('/api/alpha/post', headers={'Authorization': bearer(a.author)},
                            json={'post_id': post.id, 'visibility': 'public'})
    assert response.status_code == 400
    assert response.get_json()['message'] == 'visibility cannot be changed'
    assert db.session.get(Post, post.id).visibility == VISIBILITY_FOLLOWERS


def test_api_edit_repeating_the_same_visibility_is_accepted(api_client):
    a = api_client
    post = _followers_post(a)
    response = a.client.put('/api/alpha/post', headers={'Authorization': bearer(a.author)},
                            json={'post_id': post.id, 'visibility': 'followers', 'title': 'still fixed'})
    assert response.status_code == 200
    assert db.session.get(Post, post.id).visibility == VISIBILITY_FOLLOWERS


# --------------------------------------------------------------------------
# The web form
# --------------------------------------------------------------------------

@pytest.fixture
def web(app, db_session, site, monkeypatch):
    # the form templates call form.csrf_token(), which the test config's WTF_CSRF_ENABLED = False leaves undefined
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)
    local = instance()
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1  # fact 347
    author = able_to_post(make_user(local, 'author', local=True))
    microblogs = make_community('microblogs')
    general = make_community('general')
    db.session.add(Language(code='und', name='Undetermined'))
    english = Language(code='en', name='English')
    db.session.add(english)
    db.session.commit()
    make_community_member(author, microblogs)
    make_community_member(author, general)
    db.session.commit()
    client = app.test_client()
    login(client, author)
    return SimpleNamespace(client=client, token=csrf(app, client), author=author, english=english,
                           microblogs=microblogs, general=general)


def test_the_form_offers_visibility_in_microblogs(app, web):
    body = web.client.get(url(app, 'community.add_post', actor='microblogs', type='discussion')).get_data(as_text=True)
    assert 'name="visibility"' in body
    assert 'Followers only' in body


def test_the_form_does_not_offer_it_elsewhere(app, web):
    body = web.client.get(url(app, 'community.add_post', actor='general', type='discussion')).get_data(as_text=True)
    assert 'name="visibility"' not in body


def test_a_web_followers_only_post_is_stored(app, web):
    with patch('app.community.routes.render_template', return_value='rendered'):
        response = web.client.post(url(app, 'community.add_post', actor='microblogs', type='discussion'),
                                   data=_post_payload(web.token, web.english, web.microblogs,
                                                      title='Friends only', visibility='followers'),
                                   content_type='multipart/form-data')
    assert response.status_code == 302
    assert Post.query.filter_by(title='Friends only').one().visibility == VISIBILITY_FOLLOWERS


def test_a_web_followers_only_post_to_another_community_is_refused(app, web):
    """The community can be switched in the form (`communities`), so the check is on the community the post lands in,
    not on the URL the form was opened from."""
    with patch('app.community.routes.render_template', return_value='rendered'):
        web.client.post(url(app, 'community.add_post', actor='microblogs', type='discussion'),
                        data=_post_payload(web.token, web.english, web.general,
                                           title='Wrong place', visibility='followers'),
                        content_type='multipart/form-data')
    assert Post.query.filter_by(title='Wrong place').first() is None


def test_the_edit_form_never_offers_visibility(app, web):
    with patch('app.community.routes.render_template', return_value='rendered'):
        web.client.post(url(app, 'community.add_post', actor='microblogs', type='discussion'),
                        data=_post_payload(web.token, web.english, web.microblogs,
                                           title='Edit me', visibility='followers'),
                        content_type='multipart/form-data')
    post = Post.query.filter_by(title='Edit me').one()
    body = web.client.get(url(app, 'post.post_edit', post_id=post.id)).get_data(as_text=True)
    assert 'name="visibility"' not in body
