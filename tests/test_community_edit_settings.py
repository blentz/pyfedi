"""D641: the community settings only the web edit form used to set.

`community_edit` (app/community/routes.py) re-implemented
`app/shared/community.py`'s `edit_community` and drifted thirteen fields ahead
of it, so an API edit could not change any of them. Owner ruling: extend
`edit_community` to every field the web route handles, then route the web form
through it. Each test below drives the API (`PUT /api/alpha/community`, which
calls `edit_community` with SRC_API) and asserts the shared path now sets one
of those fields.
"""
from types import SimpleNamespace

import pytest
from flask import current_app, g

from app import db
from app.models import Community, Language, Site, Topic
from tests.factories import a_keypair, make_community, make_community_member


def auth(user):
    return {'Authorization': f'Bearer {user.encode_jwt_token()}'}


@pytest.fixture
def env(app, api_baseline, monkeypatch):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
    for code, name in [('en', 'English'), ('und', 'Undetermined')]:
        db.session.add(Language(code=code, name=name))
    db.session.commit()
    community = make_community('probeland')
    moderator = api_baseline.user2
    moderator.private_key, moderator.public_key = a_keypair()
    db.session.commit()
    membership = make_community_member(moderator, community, is_moderator=True)
    membership.is_owner = True
    db.session.commit()
    return SimpleNamespace(client=app.test_client(), community=community,
                           moderator=moderator)


def edit(env, **fields):
    return env.client.put('/api/alpha/community', headers=auth(env.moderator),
                          json={'community_id': env.community.id, **fields})


def stored(env):
    db.session.expire_all()
    return db.session.get(Community, env.community.id)


@pytest.mark.parametrize('field, value', [
    ('theme', 'dillo'),
    ('ai_generated', True),
    ('new_mods_wanted', True),
    ('default_layout', 'masonry'),
    ('default_post_type', 'discussion'),
    ('downvote_accept_mode', 2),
    ('post_url_type', 'post_id'),
])
def test_an_api_edit_sets_a_plain_setting(env, field, value):
    response = edit(env, **{field: value})

    assert response.status_code == 200, response.get_json()
    assert getattr(stored(env), field) == value


def test_an_api_edit_sets_the_posting_warning_sanitised(env):
    """D1377's sanitiser applies on this path too: the warning is rendered
    `|safe`."""
    response = edit(env, posting_warning='read the rules<script>alert(1)</script>')

    assert response.status_code == 200, response.get_json()
    assert stored(env).posting_warning.startswith('read the rules')
    assert '<script>' not in stored(env).posting_warning


@pytest.mark.parametrize('enable_nsfl, expected', [(True, True), (False, False)])
def test_an_api_edit_sets_nsfl_and_the_site_switch_wins(env, enable_nsfl, expected):
    """R203: as on the web form, a site with NSFL switched off keeps it off."""
    g.site.enable_nsfl = enable_nsfl
    db.session.commit()

    response = edit(env, nsfl=True)

    assert response.status_code == 200, response.get_json()
    assert stored(env).nsfl is expected


def test_an_api_edit_makes_a_community_private_and_local_only(env):
    """`private` brings `local_only`, `show_popular` and `show_all` with it,
    the same coupling the web form applies."""
    response = edit(env, private=True, local_only=False, invitations=2)

    assert response.status_code == 200, response.get_json()
    community = stored(env)
    assert (community.private, community.local_only) == (True, True)
    assert (community.show_popular, community.show_all) == (False, False)
    assert community.invitations == 2


def test_an_api_edit_sets_invitations_only_on_a_private_community(env):
    """The web form zeroes `invitations` for a community that is not private,
    so a public community cannot be made invitation-only by the API either."""
    response = edit(env, private=False, invitations=3)

    assert response.status_code == 200, response.get_json()
    assert (stored(env).private, stored(env).invitations) == (False, 0)


def test_an_api_edit_moves_the_community_between_topics(env):
    old_topic = Topic(name='Old', machine_name='old', num_communities=99)
    new_topic = Topic(name='New', machine_name='new', num_communities=99)
    db.session.add_all([old_topic, new_topic])
    db.session.commit()
    env.community.topic_id = old_topic.id
    db.session.commit()

    response = edit(env, topic_id=new_topic.id)

    assert response.status_code == 200, response.get_json()
    assert stored(env).topic_id == new_topic.id
    assert db.session.get(Topic, new_topic.id).num_communities == 1
    assert db.session.get(Topic, old_topic.id).num_communities == 0


@pytest.mark.parametrize('field, value', [
    ('theme', '../../etc'),
    ('default_layout', 'sideways'),
    ('default_post_type', 'Link'),
    ('downvote_accept_mode', 99),
    ('post_url_type', 'slug'),
    ('invitations', 1),
    ('topic_id', 999999),
])
def test_an_api_edit_refuses_a_value_the_web_form_would_not_offer(env, field, value):
    """The web form only offers its own choices; the API is held to the same
    lists rather than writing whatever it is sent."""
    before = getattr(stored(env), field)

    response = edit(env, **{field: value})

    assert response.status_code == 400
    assert getattr(stored(env), field) == before


def test_an_api_edit_that_omits_the_settings_leaves_them_alone(env):
    env.community.theme = 'dillo'
    env.community.default_layout = 'masonry'
    env.community.new_mods_wanted = True
    db.session.commit()

    response = edit(env, title='Renamed')

    assert response.status_code == 200, response.get_json()
    community = stored(env)
    assert (community.title, community.theme, community.default_layout,
            community.new_mods_wanted) == ('Renamed', 'dillo', 'masonry', True)
