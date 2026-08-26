"""Proves the role/membership/ban factories write rows the production code
actually reads. Seven later tasks (community post/reply/vote/upload permission
functions) build their fixtures with these factories -- a factory that writes
to the wrong table would make every later permission test pass without
exercising the permission check it names.
"""

from app.utils import communities_banned_from, user_access
from tests.factories import (ban_user_from_community, grant_permission, make_community,
                             make_community_member, make_instance, make_user)


def test_grant_permission_makes_user_access_true(app, db_session):
    """Fails if the factory writes rows user_access does not read."""
    make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
    # user_access() special-cases user_id == 1 as the instance superadmin and always
    # returns True for it (app/utils.py:1508) -- a throwaway user consumes id 1 so
    # 'roleuser' below lands on an ordinary id and the negative assertion is real.
    make_user(None, 'filler', local=True)
    user = make_user(None, 'roleuser', local=True)
    grant_permission(user, 'change instance settings')

    assert user_access('change instance settings', user.id) is True
    assert user_access('some other permission', user.id) is False


def test_community_member_is_visible_to_the_model(app, db_session):
    make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
    user = make_user(None, 'member', local=True)  # first user in the test: gets id 1,
    # which make_community() below hardcodes as its owner's user_id
    community = make_community('memberland')
    make_community_member(user, community)

    # Community.is_member() returns the CommunityMember query's .all() result
    # (app/models.py:707-717), not a bool -- production call sites (e.g.
    # app/community/routes.py:2641) use it truthily, never `is True`, so this
    # test follows the model rather than the brief's assumed boolean shape.
    assert community.is_member(user)


def test_moderator_membership_is_visible_to_the_model(app, db_session):
    make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
    user = make_user(None, 'mod', local=True)  # first user in the test: gets id 1,
    # which make_community() below hardcodes as its owner's user_id
    community = make_community('modland')
    make_community_member(user, community, is_moderator=True)

    assert community.is_moderator(user) is True


def test_community_ban_is_visible_to_communities_banned_from(app, db_session):
    """The ban factory must write what communities_banned_from reads.

    Fails if the factory targets a different table than the query does -- which
    would make every later ban test pass without exercising a ban.
    """
    make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
    user = make_user(None, 'banned', local=True)  # first user in the test: gets id 1,
    # which make_community() below hardcodes as its owner's user_id
    community = make_community('banland')
    ban_user_from_community(user, community)

    assert community.id in communities_banned_from(user.id)


def test_communities_banned_from_is_not_cached_between_calls(app, db_session):
    """A memoized permission lookup that cached across tests would produce
    permission failures indistinguishable from flakes."""
    make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
    user = make_user(None, 'cachecheck', local=True)  # first user in the test: gets id 1,
    # which make_community() below hardcodes as its owner's user_id
    community = make_community('cacheland')

    assert communities_banned_from(user.id) == []
    ban_user_from_community(user, community)
    assert community.id in communities_banned_from(user.id)
