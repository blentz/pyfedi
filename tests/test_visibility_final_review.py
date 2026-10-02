"""Final-review fixes for the visibility plan: a visible reply must not carry its hidden parent post, and the
remaining surfaces obey the predicate."""
from unittest.mock import patch

import pytest
from flask import current_app, g

from app import db
from app.models import Language, Site
from tests.factories import bearer, make_post_reply, make_visibility_world
from tests.test_visibility_single_object import MISSING, client_as

SECRET_TITLE = 'Secret Parent Title'
SECRET_BODY = 'secret parent body'
PUBLIC_REPLY = 'zebrafinch public answer'


@pytest.fixture
def world(app, db_session, monkeypatch):
    w = make_visibility_world()
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.admin_ids = []
    g.site = site
    monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.add(Language(code='en', name='English'))
    db.session.commit()
    site.language_id = Language.query.filter_by(code='en').one().id
    w.post.title = SECRET_TITLE
    w.post.body = SECRET_BODY
    w.post.body_html = f'<p>{SECRET_BODY}</p>'
    # a PUBLIC reply, by a local follower, under the followers-only post
    w.open_reply = make_post_reply(w.post, w.follower, PUBLIC_REPLY)
    w.open_reply.ap_id = 'https://m.example/r/77'
    db.session.commit()
    return w


def api_get(app, path, user, **params):
    headers = {'Authorization': bearer(user)} if user is not None else {}
    return client_as(app, None).get(path, query_string=params, headers=headers)


def assert_no_secret(text, where):
    assert SECRET_TITLE not in text, where
    assert SECRET_BODY not in text, where


# C1
@pytest.mark.parametrize('who', ['stranger', None])
def test_post_replies_of_a_hidden_post_is_not_found(app, world, who):
    w = world
    user = getattr(w, who) if who else None
    hidden = api_get(app, '/api/alpha/post/replies', user, post_id=w.post.id)
    missing = api_get(app, '/api/alpha/post/replies', user, post_id=MISSING)
    assert hidden.status_code == missing.status_code != 200
    assert hidden.get_data(as_text=True) == missing.get_data(as_text=True)
    via_parent = api_get(app, '/api/alpha/post/replies', user, parent_id=w.open_reply.id)
    assert via_parent.status_code != 200
    assert_no_secret(via_parent.get_data(as_text=True), 'parent_id branch')


def test_post_replies_of_a_hidden_post_open_to_a_follower(app, world):
    w = world
    response = api_get(app, '/api/alpha/post/replies', w.follower, post_id=w.post.id)
    assert response.status_code == 200, response.get_data(as_text=True)
    assert PUBLIC_REPLY in response.get_data(as_text=True)
