"""Followers-only content, ingested through create_post, enforced on every surface."""
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.activitypub.routes import process_new_content
from app.activitypub.util import create_post
from app.models import Language, Site
from tests.factories import feed_ids, make_visibility_world
from tests.test_visibility_ingest import FOLLOWERS, PUBLIC, note_activity
from tests.test_visibility_single_object import client_as


@pytest.fixture
def stored(app, db_session, monkeypatch):
    w = make_visibility_world()
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.admin_ids = []
    g.site = site
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.add(Language(code='en', name='English'))
    db.session.commit()
    site.language_id = Language.query.filter_by(code='en').one().id
    activity = note_activity([FOLLOWERS], [])
    activity['object']['id'] = 'https://m.example/users/alice/statuses/77'
    activity['object']['attributedTo'] = w.author.ap_profile_id
    post = create_post(False, w.community, activity, w.author)
    assert post is not None and post.visibility == 'followers'
    w.stored = post
    return w


def test_stranger_gets_404_and_follower_200_on_the_post_page(app, stored):
    w = stored
    with patch('app.post.routes.render_template', return_value=app.response_class('rendered')):
        assert client_as(app, w.stranger).get(f'/post/{w.stored.id}').status_code == 404
        assert client_as(app, w.follower).get(f'/post/{w.stored.id}').status_code == 200


def test_feed_includes_the_post_for_the_follower_only(app, stored):
    w = stored
    assert w.stored.id in feed_ids(app, w.follower, [w.community.id], include_following=True)
    assert w.stored.id not in feed_ids(app, w.stranger, [w.community.id], include_following=True)


def test_activitypub_url_is_404(app, stored):
    w = stored
    w.stored.ap_id = None  # treat as local so post_ap serves rather than redirects
    db.session.commit()
    response = app.test_client().get(f'/post/{w.stored.id}', headers={'Accept': 'application/activity+json'})
    assert response.status_code == 404


@pytest.mark.parametrize('to, cc, relayed', [([FOLLOWERS], [], False), ([PUBLIC], [FOLLOWERS], True)])
def test_a_followers_only_create_is_not_relayed_to_the_communitys_followers(app, db_session, to, cc, relayed):
    w = make_visibility_world()
    activity = note_activity(to, cc)
    activity['object']['id'] = 'https://m.example/users/alice/statuses/88'
    activity['object']['attributedTo'] = w.author.ap_profile_id
    with patch('app.activitypub.routes.can_create_post', return_value=True), \
            patch('app.activitypub.routes.announce_activity_to_followers') as announce:
        process_new_content(w.author, w.community, False, activity, False)
    assert announce.called is relayed
