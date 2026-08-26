from flask import g

from app.constants import (DOWNVOTE_ACCEPT_ALL, DOWNVOTE_ACCEPT_INSTANCE,
                           DOWNVOTE_ACCEPT_MEMBERS, DOWNVOTE_ACCEPT_NONE,
                           DOWNVOTE_ACCEPT_TRUSTED)
from app.models import Site
from app.utils import can_downvote, can_upvote
from tests.factories import (ban_user_from_community, make_community, make_community_member,
                             make_instance, make_site, make_user)


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


class TestCanDownvoteBasics:
    """Each test isolates ONE sub-condition of

        if user is None or community is None or user.banned or user.bot:

    can_downvote has its own copy of this guard (a separate function, so a
    separate set of coverage.py branches from can_upvote's identical-looking
    line above) -- each sub-condition needs its own case here too.
    """

    def test_an_ordinary_user_may_downvote(self, app, db_session, site):
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'downvoter', local=True)  # first user in the test: gets
        # id 1, which make_community() below hardcodes as its owner's user_id
        community = make_community('downland')
        assert can_downvote(user, community) is True

    def test_a_none_user_is_refused(self, app, db_session, site):
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        make_user(None, 'downowner', local=True)  # occupies id 1, make_community()'s owner FK
        assert can_downvote(None, make_community('downland1b')) is False

    def test_a_none_community_is_refused(self, app, db_session, site):
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        assert can_downvote(make_user(None, 'downvoter1c', local=True), None) is False

    def test_a_banned_user_is_refused(self, app, db_session, site):
        """Fails if `or user.banned` is deleted."""
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'dbanned', local=True)  # first user in the test: gets id 1,
        # which make_community() below hardcodes as its owner's user_id
        user.banned = True
        assert can_downvote(user, make_community('downland2')) is False

    def test_a_bot_is_refused(self, app, db_session, site):
        """Fails if `or user.bot` is deleted."""
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'dbot', local=True)  # first user in the test: gets id 1,
        # which make_community() below hardcodes as its owner's user_id
        user.bot = True
        assert can_downvote(user, make_community('downland3')) is False


class TestCanDownvoteSiteSetting:
    def test_downvotes_disabled_site_wide_refuses_everyone(self, app, db_session, site):
        """Fails if the enable_downvotes check is removed."""
        original = site.enable_downvotes
        site.enable_downvotes = False
        try:
            make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
            user = make_user(None, 'sitedown', local=True)
            assert can_downvote(user, make_community('downland4')) is False
        finally:
            site.enable_downvotes = original


class TestCanDownvoteReputation:
    """The `(user.attitude is not None and user.attitude < 0.0) or user.reputation < -10`
    guard: three sub-conditions, each needing its own case."""

    def test_a_negative_attitude_is_refused(self, app, db_session, site):
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'sour', local=True)
        user.attitude = -0.5
        assert can_downvote(user, make_community('downland5')) is False

    def test_a_none_attitude_does_not_refuse(self, app, db_session, site):
        """The `attitude is not None` sub-condition. A new user has no attitude
        yet; treating None as negative would silently refuse every new account."""
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'fresh', local=True)
        user.attitude = None
        assert can_downvote(user, make_community('downland6')) is True

    def test_low_reputation_is_refused(self, app, db_session, site):
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'lowrep', local=True)
        user.reputation = -11
        assert can_downvote(user, make_community('downland7')) is False

    def test_reputation_at_the_boundary_is_permitted(self, app, db_session, site):
        """`< -10`, so exactly -10 must pass. Fails if it becomes `<=`."""
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'boundary', local=True)
        user.reputation = -10
        assert can_downvote(user, make_community('downland8')) is True


class TestCanDownvoteAcceptMode:
    def test_accept_none_refuses(self, app, db_session, site):
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'dm1', local=True)
        community = make_community('dmnone')
        community.downvote_accept_mode = DOWNVOTE_ACCEPT_NONE
        assert can_downvote(user, community) is False

    def test_accept_members_refuses_a_non_member(self, app, db_session, site):
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'dm2', local=True)
        community = make_community('dmmembers')
        community.downvote_accept_mode = DOWNVOTE_ACCEPT_MEMBERS
        assert can_downvote(user, community) is False

    def test_accept_members_permits_a_member(self, app, db_session, site):
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'dm3', local=True)
        community = make_community('dmmembers2')
        community.downvote_accept_mode = DOWNVOTE_ACCEPT_MEMBERS
        make_community_member(user, community)
        assert can_downvote(user, community) is True

    def test_accept_instance_refuses_a_different_instance(self, app, db_session, site):
        make_instance('test.piefed.local', software='piefed')  # id 1: community's instance
        remote = make_instance('other.example')  # id 2: genuinely different from the
        # community's instance -- make_community() below hardcodes instance_id=1, so the
        # local instance above must exist FIRST or this test would accidentally put
        # user and community on the same instance
        user = make_user(remote, 'dm4')
        community = make_community('dminstance')
        community.downvote_accept_mode = DOWNVOTE_ACCEPT_INSTANCE
        assert can_downvote(user, community) is False

    def test_accept_instance_permits_the_same_instance(self, app, db_session, site):
        """The `user.instance_id != community.instance_id` sub-condition's False
        outcome -- without this, only the refusal path of this single-line guard
        is ever exercised."""
        make_instance('test.piefed.local', software='piefed')  # id 1, shared by both
        user = make_user(None, 'dm4b', local=True)
        community = make_community('dminstance2')
        community.downvote_accept_mode = DOWNVOTE_ACCEPT_INSTANCE
        assert can_downvote(user, community) is True

    def test_accept_all_permits(self, app, db_session, site):
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'dm5', local=True)
        community = make_community('dmall')
        community.downvote_accept_mode = DOWNVOTE_ACCEPT_ALL
        assert can_downvote(user, community) is True

    def test_accept_trusted_permits_the_same_instance(self, app, db_session, site):
        """`if community.instance_id == user.instance_id: pass` -- the
        short-circuit branch that never consults trusted_instance_ids() at all."""
        make_instance('test.piefed.local', software='piefed')  # id 1, shared by both
        user = make_user(None, 'dm6', local=True)
        community = make_community('dmtrusted1')
        community.downvote_accept_mode = DOWNVOTE_ACCEPT_TRUSTED
        assert can_downvote(user, community) is True

    def test_accept_trusted_permits_a_trusted_remote_instance(self, app, db_session, site):
        """The `user.instance_id in trusted_instance_ids()` sub-condition's True
        outcome, reached only once instance != instance rules out the
        short-circuit above."""
        make_instance('test.piefed.local', software='piefed')  # id 1: community's instance
        remote = make_instance('trusted.example')  # id 2
        remote.trusted = True
        user = make_user(remote, 'dm7')
        community = make_community('dmtrusted2')
        community.downvote_accept_mode = DOWNVOTE_ACCEPT_TRUSTED
        assert can_downvote(user, community) is True

    def test_accept_trusted_refuses_an_untrusted_remote_instance(self, app, db_session, site):
        make_instance('test.piefed.local', software='piefed')  # id 1: community's instance
        remote = make_instance('untrusted.example')  # id 2, trusted defaults False
        user = make_user(remote, 'dm8')
        community = make_community('dmtrusted3')
        community.downvote_accept_mode = DOWNVOTE_ACCEPT_TRUSTED
        assert can_downvote(user, community) is False

    def test_an_unrecognized_accept_mode_falls_through_permitted(self, app, db_session, site):
        """downvote_accept_mode != ALL but also matches none of NONE/MEMBERS/
        INSTANCE/TRUSTED: the elif chain has no final else, so it falls through
        to the ban check with nothing refused. Closes the one branch
        (`elif ... == DOWNVOTE_ACCEPT_TRUSTED` False, with no prior elif having
        matched either) that the five canonical constants above never reach --
        every one of them either matches an earlier elif or is ALL, which skips
        the chain entirely."""
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'dm9', local=True)
        community = make_community('dmunknown')
        community.downvote_accept_mode = 999
        assert can_downvote(user, community) is True


class TestCanDownvoteLocalOnly:
    """`if community.local_only and not user.is_local(): return False` -- two
    sub-conditions, each needing its own case with the other held eligible."""

    def test_local_only_refuses_a_remote_user(self, app, db_session, site):
        make_instance('test.piefed.local', software='piefed')  # id 1: community's instance
        remote = make_instance('remote.example')  # id 2
        user = make_user(remote, 'downremote')
        community = make_community('downlocalonly')
        community.local_only = True
        assert can_downvote(user, community) is False

    def test_local_only_permits_a_local_user(self, app, db_session, site):
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'downlocal', local=True)
        community = make_community('downlocalonly2')
        community.local_only = True
        assert can_downvote(user, community) is True


class TestCanDownvoteBan:
    """Mirrors TestCanUpvote's precomputed-list coverage for the same
    community-ban check at the tail of can_downvote."""

    def test_a_user_banned_from_the_community_is_refused(self, app, db_session, site):
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'downbanned', local=True)  # first user in the test: gets
        # id 1, which make_community() below hardcodes as its owner's user_id
        community = make_community('downbanland')
        ban_user_from_community(user, community)
        assert can_downvote(user, community) is False

    def test_the_precomputed_ban_list_is_honoured(self, app, db_session, site):
        """The `communities_banned_from_list is not None` branch: callers pass a
        precomputed list to avoid a query per row. Fails if that branch stops
        being consulted, which would silently re-query and ignore the caller."""
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'downlistbanned', local=True)  # first user in the test:
        # gets id 1, which make_community() below hardcodes as its owner's user_id
        community = make_community('downbanland2')
        assert can_downvote(user, community, communities_banned_from_list=[community.id]) is False

    def test_an_empty_precomputed_list_permits(self, app, db_session, site):
        """Proves the precomputed branch is used INSTEAD of the query, not as
        well as it: the user is banned in the database but the list says no."""
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'downlistok', local=True)  # first user in the test: gets
        # id 1, which make_community() below hardcodes as its owner's user_id
        community = make_community('downbanland3')
        ban_user_from_community(user, community)
        assert can_downvote(user, community, communities_banned_from_list=[]) is True


class TestCanDownvoteSiteLookup:
    """`try: site = g.site / except: site = Site.query.get(1)`.

    Probed directly (app.app_context(), then `try: g.site / except Exception as
    e: print(type(e), e)`): accessing g.site when it was never set raises
    `AttributeError: site`, not KeyError -- flask.g is a plain namespace object,
    not a dict.

    The bare `except:` also catches KeyboardInterrupt and SystemExit. That is a
    finding, reported here, and NOT narrowed by this task -- authorisation code
    changes need the project owner's decision (this is on the spec's
    suspected-defects list).
    """

    def test_missing_g_site_falls_back_to_the_database_row(self, app, db_session, site):
        """Every other test in this file already exercises this branch too --
        db_session's fixture clears flask.g before each test and nothing here
        ever sets g.site -- but this test makes it explicit, with a real
        request context (matching how the exception actually arises in
        production) rather than relying on that as an accident of fixture
        ordering."""
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'fallbacksite', local=True)
        community = make_community('downfallback')
        with app.test_request_context('/'):
            assert not hasattr(g, 'site')
            assert can_downvote(user, community) is True

    def test_a_set_g_site_is_used_instead_of_the_database_row(self, app, db_session, site):
        """Discriminates the try branch from the except fallback. g.site here is
        a DIFFERENT Site object than the database row (id 1, from the `site`
        fixture, enable_downvotes=True by default) that the fallback would
        fetch instead -- this one has enable_downvotes=False. If `try: site =
        g.site` were ever skipped in favour of always falling through to the
        database query, this would wrongly return True."""
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'gsiteused', local=True)
        community = make_community('downgsite')
        with app.test_request_context('/'):
            g.site = Site(name='not the db row', enable_downvotes=False)
            assert can_downvote(user, community) is False
