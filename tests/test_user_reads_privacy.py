"""The alpha API's person reads answer no more than the web UI shows.

Found by the shared read layer's step 4 security sweep (API-completeness plan W0.1) and proved by probes on the code
of a83d40a87: a person's `moderates` named communities whose moderators are private; a community's own moderator list
ignored `private_mods`. Each gate lives in app/reads/users.py, so every later client API inherits it."""
import pytest
from flask import g

from app import db
from app.api.alpha.utils.community import get_community
from app.api.alpha.utils.post import get_post
from app.api.alpha.utils.user import get_user
from app.models import Site
import app.reads.users as reads_users
from tests.factories import make_community, make_community_member, make_post, make_user


def token(user):
    return f'Bearer {user.encode_jwt_token()}'


@pytest.fixture
def world(app, api_baseline):
    """`hush`, a community whose moderators are private, and `open`, one whose are not, both moderated by `mod`; a
    post in `hush`. user1 is the admin; user3 a signed-in reader with no part in either."""
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    hush = make_community('hushmods')
    hush.private_mods = True
    public = make_community('openmods')
    db.session.commit()
    mod = make_user(api_baseline.instance_local, 'quietmod', local=True)
    make_community_member(mod, hush, is_moderator=True)
    make_community_member(mod, public, is_moderator=True)
    post = make_post(hush, mod, 'https://test.piefed.local/post/hush1', title='in hush')
    db.session.commit()
    return api_baseline, hush, public, mod, post


def viewer(world, who):
    baseline, _, _, mod, _ = world
    return {'anonymous': None, 'reader': token(baseline.user3), 'self': token(mod),
            'admin': token(baseline.user1)}[who]


def moderated(world, who):
    mod = world[3]
    return [m['community']['name'] for m in get_user(viewer(world, who), {'person_id': mod.id})['moderates']]


# --- private_mods: app/user/utils.py:417-419 (the profile), app/community/routes.py:522-523 (the community page) ---

@pytest.mark.parametrize('who', ['anonymous', 'reader'])
def test_moderates_leaves_out_a_private_mods_community(world, who):
    assert moderated(world, who) == ['openmods']


@pytest.mark.parametrize('who', ['self', 'admin'])
def test_moderates_names_a_private_mods_community_to_the_person_and_admins(world, who):
    assert sorted(moderated(world, who)) == ['hushmods', 'openmods']


def community_mods(world, who, community):
    return [m['moderator']['user_name'] for m in get_community(viewer(world, who), {'id': community.id})['moderators']]


@pytest.mark.parametrize('who', ['anonymous', 'reader'])
def test_a_private_mods_community_lists_no_moderators(world, who):
    _, hush, public, _, _ = world
    community_mods(world, 'self', hush)   # a moderator's answer first: the cached list must not serve it on
    assert community_mods(world, who, hush) == []
    assert community_mods(world, who, public) == ['quietmod']


@pytest.mark.parametrize('who', ['self', 'admin'])
def test_a_private_mods_community_lists_its_moderators_to_them_and_admins(world, who):
    """The web's moderator page (app/community/routes.py:1838, community_mod_list) shows them to its moderators and
    to an admin of communities."""
    assert community_mods(world, who, world[1]) == ['quietmod']


@pytest.mark.parametrize('who', ['anonymous', 'reader'])
def test_a_post_in_a_private_mods_community_names_no_moderators(world, who):
    post = world[4]
    assert get_post(viewer(world, 'self'), {'id': post.id})['moderators']
    assert get_post(viewer(world, who), {'id': post.id})['moderators'] == []


def test_the_gates_on_their_own(world):
    """What no API call reaches: a community nobody holds hides no moderators, and nothing moderated hides nothing."""
    mod = world[3]
    assert reads_users.moderators_hidden(987654, None) is False
    assert reads_users.moderates_hidden_from(mod, None, []) == set()
