from app.utils import can_upvote
from tests.factories import (ban_user_from_community, make_community, make_instance,
                             make_site, make_user)


class TestCanUpvote:
    """Each test isolates ONE sub-condition of

        if user is None or community is None or user.banned or user.bot:

    Branch coverage alone is satisfied by two tests and would pass with any
    single sub-condition deleted.
    """

    def test_an_ordinary_user_may_upvote(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'voter', local=True)  # first user in the test: gets id 1,
        # which make_community() below hardcodes as its owner's user_id
        community = make_community('voteland')
        assert can_upvote(user, community) is True

    def test_a_none_user_is_refused(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        make_user(None, 'owner', local=True)  # occupies id 1, make_community()'s owner FK
        assert can_upvote(None, make_community('voteland2')) is False

    def test_a_none_community_is_refused(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        assert can_upvote(make_user(None, 'voter2', local=True), None) is False

    def test_a_banned_user_is_refused(self, app, db_session):
        """Fails if `or user.banned` is deleted.

        can_upvote never calls user_access/role_access, which are the only
        functions in app/utils.py that special-case user_id == 1 (Task 2) --
        so, unlike those tests, the subject here can safely BE the id-1 user
        that make_community() below hardcodes as its owner, matching the
        convention in tests/test_factories_permissions.py's ban test.
        """
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'bannedvoter', local=True)  # first user in the test: gets
        # id 1, which make_community() below hardcodes as its owner's user_id
        user.banned = True
        community = make_community('voteland3')
        assert can_upvote(user, community) is False

    def test_a_bot_is_refused(self, app, db_session):
        """Fails if `or user.bot` is deleted."""
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'botvoter', local=True)  # first user in the test: gets id 1,
        # which make_community() below hardcodes as its owner's user_id
        user.bot = True
        community = make_community('voteland4')
        assert can_upvote(user, community) is False

    def test_a_user_banned_from_the_community_is_refused(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'commbanned', local=True)  # first user in the test: gets
        # id 1, which make_community() below hardcodes as its owner's user_id
        community = make_community('voteland5')
        ban_user_from_community(user, community)
        assert can_upvote(user, community) is False

    def test_the_precomputed_ban_list_is_honoured(self, app, db_session):
        """The `communities_banned_from_list is not None` branch: callers pass a
        precomputed list to avoid a query per row. Fails if that branch stops
        being consulted, which would silently re-query and ignore the caller."""
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'listbanned', local=True)  # first user in the test: gets
        # id 1, which make_community() below hardcodes as its owner's user_id
        community = make_community('voteland6')
        assert can_upvote(user, community, communities_banned_from_list=[community.id]) is False

    def test_an_empty_precomputed_list_permits(self, app, db_session):
        """Proves the precomputed branch is used INSTEAD of the query, not as
        well as it: the user is banned in the database but the list says no."""
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'listok', local=True)  # first user in the test: gets id 1,
        # which make_community() below hardcodes as its owner's user_id
        community = make_community('voteland7')
        ban_user_from_community(user, community)
        assert can_upvote(user, community, communities_banned_from_list=[]) is True
