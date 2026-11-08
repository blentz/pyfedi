"""The alpha API's person reads answer no more than the web UI shows.

Found by the shared read layer's step 4 security sweep (API-completeness plan W0.1) and proved by probes on the code
of a83d40a87: a person's `moderates` named communities whose moderators are private; a community's own moderator list
ignored `private_mods`. Each gate lives in app/reads/users.py, so every later client API inherits it."""
import pytest
from flask import g

from app import db
from app.api.alpha.utils.community import get_community
from app.api.alpha.utils.post import get_post
from app.api.alpha.utils.user import get_user, get_user_list
from app.constants import VISIBILITY_FOLLOWERS
from app.models import Site, UserFollower
import app.reads.users as reads_users
from tests.factories import make_community, make_community_member, make_post, make_post_reply, make_user


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


# --- private communities in `moderates`: membership as app/reads asks it (Restrictions.private_community_ids) ---

@pytest.fixture
def secret(world):
    """A private community `mod` moderates, beside `world`'s two."""
    community = make_community('secretclub')
    community.private = True
    db.session.commit()
    make_community_member(world[3], community, is_moderator=True)
    return community


@pytest.mark.parametrize('who', ['anonymous', 'reader'])
def test_moderates_leaves_out_a_private_community_to_a_non_member(world, secret, who):
    assert moderated(world, who) == ['openmods']


@pytest.mark.parametrize('who', ['self', 'admin'])
def test_moderates_names_a_private_community_to_the_person_and_admins(world, secret, who):
    assert sorted(moderated(world, who)) == ['hushmods', 'openmods', 'secretclub']


def test_moderates_names_a_private_community_to_its_member(world, secret):
    make_community_member(world[0].user3, secret)
    assert sorted(moderated(world, 'reader')) == ['openmods', 'secretclub']


def test_moderates_leaves_out_a_private_community_to_a_member_banned_from_it(world, secret):
    member = make_community_member(world[0].user3, secret)
    member.is_banned = True
    db.session.commit()
    assert moderated(world, 'reader') == ['openmods']


# --- searchable: the web people directory (app/instance/routes.py:159-165) lists an account that opted out of the
# profile list to an admin ('administer all users') only ---

@pytest.fixture
def unlisted(world):
    mod = world[3]
    mod.searchable = False
    db.session.commit()
    return mod


def people(world, who, data):
    return [u['person']['user_name'] for u in get_user_list(viewer(world, who), data)['users']]


@pytest.mark.parametrize('who', ['anonymous', 'reader', 'self'])
@pytest.mark.parametrize('data', [{'q': 'quietmod'}, {'limit': 50}], ids=['search', 'list'])
def test_the_person_list_leaves_out_an_account_that_opted_out(world, unlisted, who, data):
    assert 'quietmod' not in people(world, who, data)


@pytest.mark.parametrize('data', [{'q': 'quietmod'}, {'limit': 50}], ids=['search', 'list'])
def test_the_person_list_shows_an_admin_an_account_that_opted_out(world, unlisted, data):
    assert 'quietmod' in people(world, 'admin', data)


@pytest.mark.parametrize('who', ['anonymous', 'reader'])
def test_an_account_that_opted_out_is_still_found_by_id_and_name(world, unlisted, who):
    assert get_user(viewer(world, who), {'person_id': unlisted.id})['person_view']['person']['user_name'] == 'quietmod'
    assert get_user(viewer(world, who), {'username': 'quietmod'})['person_view']['person']['id'] == unlisted.id


# --- counts: a person's post and comment counts are what the viewer could list, but to the person and admins ---

@pytest.fixture
def activity(world, secret):
    """`mod`'s posts and comments: one each that anyone may list (in `hush`, the fixture's post), and one each in a
    private community (`secret`), followers-only and local-only. The stored counts are 4 and 4."""
    _, hush, public, mod, post = world
    make_post_reply(post, mod, body='open')
    in_secret = make_post(secret, mod, 'https://test.piefed.local/post/secret1', title='in secret')
    make_post_reply(in_secret, mod, body='secret')
    followers = make_post(public, mod, 'https://test.piefed.local/post/followers1', title='followers only')
    followers.visibility = VISIBILITY_FOLLOWERS
    make_post_reply(post, mod, body='followers').visibility = VISIBILITY_FOLLOWERS
    make_post(public, mod, 'https://test.piefed.local/post/local1', title='local only').local_only = True
    make_post_reply(post, mod, body='local').local_only = True
    mod.recalculate_post_stats()
    db.session.commit()
    return mod


def counted(world, who):
    counts = get_user(viewer(world, who), {'person_id': world[3].id})['person_view']['counts']
    return counts['post_count'], counts['comment_count']


@pytest.mark.parametrize('who, expected', [('anonymous', (1, 1)), ('reader', (2, 2)), ('self', (4, 4)),
                                           ('admin', (4, 4))])
def test_a_person_s_counts_are_what_the_viewer_could_list(world, activity, who, expected):
    """Anonymous: the open post and comment only. A signed-in reader: local-only ones too (the posts and comments
    listings show them a signed-in reader). The person and an admin: the stored counts."""
    assert counted(world, who) == expected


def test_a_member_counts_the_private_community_s_posts_and_comments(world, secret, activity):
    make_community_member(world[0].user3, secret)
    assert counted(world, 'reader') == (3, 3)


def test_a_follower_counts_the_followers_only_posts_and_comments(world, activity):
    db.session.add(UserFollower(local_user_id=world[0].user3.id, remote_user_id=activity.id, is_inward=False,
                                is_accepted=True))
    db.session.commit()
    assert counted(world, 'reader') == (3, 3)


@pytest.mark.parametrize('who, expected', [('anonymous', (1, 1)), ('admin', (4, 4))])
def test_the_person_list_counts_the_same_way(world, activity, who, expected):
    listed = get_user_list(viewer(world, who), {'q': 'quietmod'})['users']
    assert [(u['counts']['post_count'], u['counts']['comment_count']) for u in listed] == [expected]
