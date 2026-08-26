"""Covers app/utils.py's can_create_post: the guard chain deciding who may post
to a community.

Task 6 (can_create_post_reply, a near-identical guard chain) adds to this same
file, so class names below are prefixed CanCreatePost, not left generic.

Guard order, as read from app/utils.py:

    content is None                                          -> False
    user is None or content is None or user.banned            -> False
    user.ban_posts                                             -> False
    local:  verified is False or private_key is None          -> False
    remote: allowlist-or-instance-ban, then a new-account rate limit
    content.banned                                             -> False
    content.is_moderator(user) or user.is_admin()              -> True  (early return)
    content.restricted_to_mods                                 -> False
    content.local_only and not user.is_local()                 -> False
    content.id in communities_banned_from(user.id)              -> False
    content.instance_id in banned_instances(user.id)             -> False
    otherwise                                                  -> True

Every test isolates ONE sub-condition, everything else held eligible, per the
project's condition-coverage convention (branch coverage alone would pass with
a sub-condition of a compound guard deleted).
"""
from datetime import timedelta

from flask import g

from app import db
from app.constants import ALLOWLIST_INTENSE
from app.models import AllowedInstances, BannedInstances, Role, user_role, utcnow
from app.utils import can_create_post, get_setting, set_setting
from tests.factories import (ban_user_from_community, make_community, make_community_member,
                             make_instance, make_instance_ban, make_user)


def _make_admin(user):
    """Assign a Role named 'Admin' to `user`. User.is_admin() (app/models.py)
    checks `role.name == 'Admin'` for any id other than 1 -- a real role, not
    grant_permission's `role-<permission>` naming, is needed to prove the
    is_admin() path without relying on the id==1 special case.
    """
    role = Role(name='Admin', weight=0)
    db.session.add(role)
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=user.id, role_id=role.id))
    db.session.commit()
    return role


class TestCanCreatePostContentAndUserGuards:
    """The two leading guards: `if content is None: return False`, then
    `if user is None or content is None or user.banned: return False`.

    The brief's suspected-defect list flags `content is None` as checked
    twice -- once alone, then again inside the compound. The first check
    already returns before the second is ever reached with content actually
    None, so the compound's own `content is None` sub-condition is dead in
    practice. Reported here, not fixed.
    """

    def test_content_is_none_is_refused(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'nocontent', local=True, with_keys=True)
        assert can_create_post(user, None) is False

    def test_user_is_none_is_refused(self, app, db_session):
        """Reaches the compound guard's `user is None` sub-condition -- content
        is not None here, so this exercises a different line than the test
        above."""
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        make_user(None, 'owner', local=True)  # occupies id 1, make_community()'s owner FK
        community = make_community('postland1')
        assert can_create_post(None, community) is False

    def test_a_banned_user_is_refused(self, app, db_session):
        """Fails if `or user.banned` is deleted from the compound guard."""
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'banneduser', local=True, with_keys=True)
        user.banned = True
        community = make_community('postland2')
        assert can_create_post(user, community) is False


class TestCanCreatePostBanPosts:
    def test_ban_posts_is_refused(self, app, db_session):
        """Fails if `if user.ban_posts: return False` is deleted."""
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'banpost', local=True, with_keys=True)
        user.ban_posts = True
        community = make_community('postland3')
        assert can_create_post(user, community) is False


class TestCanCreatePostLocalBranch:
    """`if user.is_local(): if user.verified is False or user.private_key is
    None: return False` -- two sub-conditions, each needing its own case."""

    def test_an_ordinary_verified_local_user_may_post(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'localposter', local=True, with_keys=True)
        community = make_community('postland4')
        assert can_create_post(user, community) is True

    def test_an_unverified_local_user_is_refused(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'unverified', local=True, with_keys=True)
        user.verified = False
        community = make_community('postland5')
        assert can_create_post(user, community) is False

    def test_a_local_user_with_no_private_key_is_refused(self, app, db_session):
        """with_keys=False (the default) leaves private_key None."""
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'nokey', local=True, with_keys=False)
        community = make_community('postland6')
        assert can_create_post(user, community) is False


class TestCanCreatePostRemoteBranch:
    """The remote half of the local/remote split: allowlist-or-instance-ban.

    By default no Settings row exists, so get_setting('use_allowlist') is
    falsy and the `else: instance_banned(...)` arm runs. Separate tests below
    cover the `use_allowlist` arm.

    make_user does not set ap_domain for a remote user (only ap_id/
    ap_profile_id/ap_public_url/ap_inbox_url), so it is set explicitly here --
    instance_banned/instance_allowed both key off it.
    """

    def test_a_remote_user_from_a_banned_instance_is_refused(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')  # id 1: local instance
        remote = make_instance('banned.example')  # id 2
        user = make_user(remote, 'frombanned')
        user.ap_domain = remote.domain
        db.session.add(BannedInstances(domain=remote.domain))
        db.session.commit()
        community = make_community('postland7')
        assert can_create_post(user, community) is False

    def test_a_remote_user_from_a_permitted_instance_is_allowed(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')  # id 1: local instance
        remote = make_instance('permitted.example')  # id 2
        user = make_user(remote, 'frompermitted')
        user.ap_domain = remote.domain
        community = make_community('postland8')
        assert can_create_post(user, community) is True


class TestCanCreatePostGSiteMutation:
    """`if not hasattr(g, 'site'): g.site = db.session.query(Site).get(1)` --
    only reached on the remote branch. This is a SIDE EFFECT: a permission
    check silently populating a request-global as a byproduct of deciding
    whether someone may post. Reported, not changed, per this task's brief.

    Every other remote-branch test in this module starts with no g.site set
    (db_session clears flask.g before each test), so the `not hasattr` arm is
    always True there and `g.site = ...` always runs -- the other arm (g.site
    already present) is never exercised elsewhere in this file. This test
    covers that branch and, in passing, demonstrates the mutation directly:
    g.site is unset beforehand and IS the freshly-queried Site row afterward.
    """

    def test_g_site_already_set_is_left_alone(self, app, db_session, site):
        make_instance('test.piefed.local', software='piefed')  # id 1
        remote = make_instance('gsitealready.example')  # id 2
        user = make_user(remote, 'gsiteuser')
        user.ap_domain = remote.domain
        community = make_community('postland_gsite1')
        with app.test_request_context('/'):
            sentinel = object()
            g.site = sentinel
            assert can_create_post(user, community) is True
            # Unchanged: the hasattr check is a no-op when g.site is already there.
            assert g.site is sentinel

    def test_g_site_is_populated_as_a_side_effect_when_absent(self, app, db_session, site):
        """The `not hasattr` arm: proves can_create_post really does mutate
        flask.g, not merely read a local variable named the same."""
        make_instance('test.piefed.local', software='piefed')  # id 1
        remote = make_instance('gsiteabsent.example')  # id 2
        user = make_user(remote, 'gsiteuser2')
        user.ap_domain = remote.domain
        community = make_community('postland_gsite2')
        with app.test_request_context('/'):
            assert not hasattr(g, 'site')
            assert can_create_post(user, community) is True
            assert hasattr(g, 'site')
            assert g.site.id == 1


class TestCanCreatePostAllowlistMode:
    """`if get_setting('use_allowlist') and g.site.allowlist_mode ==
    ALLOWLIST_INTENSE:` -- both sub-conditions must be True to switch from
    instance_banned() to instance_allowed(); each needs its own case, plus the
    True/False outcome of instance_allowed() itself once inside that arm.
    """

    def test_intense_allowlist_refuses_an_unlisted_instance(self, app, db_session, site):
        original_setting = get_setting('use_allowlist')
        original_mode = site.allowlist_mode
        try:
            set_setting('use_allowlist', True)
            site.allowlist_mode = ALLOWLIST_INTENSE
            db.session.commit()
            make_instance('test.piefed.local', software='piefed')  # id 1
            remote = make_instance('unlisted.example')  # id 2
            user = make_user(remote, 'unlisted')
            user.ap_domain = remote.domain
            community = make_community('postland9')
            assert can_create_post(user, community) is False
        finally:
            set_setting('use_allowlist', original_setting)
            site.allowlist_mode = original_mode
            db.session.commit()

    def test_intense_allowlist_permits_an_allowed_instance(self, app, db_session, site):
        original_setting = get_setting('use_allowlist')
        original_mode = site.allowlist_mode
        try:
            set_setting('use_allowlist', True)
            site.allowlist_mode = ALLOWLIST_INTENSE
            make_instance('test.piefed.local', software='piefed')  # id 1
            remote = make_instance('allowed.example')  # id 2
            db.session.add(AllowedInstances(domain=remote.domain))
            db.session.commit()
            user = make_user(remote, 'allowedone')
            user.ap_domain = remote.domain
            community = make_community('postland10')
            assert can_create_post(user, community) is True
        finally:
            set_setting('use_allowlist', original_setting)
            site.allowlist_mode = original_mode
            db.session.commit()

    def test_use_allowlist_off_ignores_intense_mode(self, app, db_session, site):
        """The `get_setting('use_allowlist')` sub-condition's False outcome
        while allowlist_mode is ALREADY intense -- proves both halves of the
        `and` are independently required, not just the mode half."""
        original_setting = get_setting('use_allowlist')
        original_mode = site.allowlist_mode
        try:
            set_setting('use_allowlist', False)
            site.allowlist_mode = ALLOWLIST_INTENSE
            db.session.commit()
            make_instance('test.piefed.local', software='piefed')  # id 1
            remote = make_instance('unlisted2.example')  # id 2, not on any allowlist
            user = make_user(remote, 'stillallowed')
            user.ap_domain = remote.domain
            community = make_community('postland11')
            # Falls through to instance_banned(), which is False since
            # BannedInstances is empty -- permitted, even though this
            # instance would fail instance_allowed().
            assert can_create_post(user, community) is True
        finally:
            set_setting('use_allowlist', original_setting)
            site.allowlist_mode = original_mode
            db.session.commit()


class TestCanCreatePostNewAccountRateLimit:
    """`if user.created_very_recently() and user.post_count > 3: return False`
    only runs on the remote branch. Two sub-conditions: without the count half
    a new user could never post; without the recency half the limit would
    apply forever. created_very_recently() is `self.created > utcnow() -
    timedelta(days=1)` -- make_user's insert-time `created` default already
    satisfies that, so only the old-user case needs `created` overridden.
    """

    def test_a_new_remote_user_with_three_posts_is_permitted(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')  # id 1
        remote = make_instance('newposter.example')  # id 2
        user = make_user(remote, 'threeposts')
        user.ap_domain = remote.domain
        user.post_count = 3
        community = make_community('postland12')
        assert can_create_post(user, community) is True

    def test_a_new_remote_user_with_four_posts_is_refused(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')  # id 1
        remote = make_instance('overposter.example')  # id 2
        user = make_user(remote, 'fourposts')
        user.ap_domain = remote.domain
        user.post_count = 4
        community = make_community('postland13')
        assert can_create_post(user, community) is False

    def test_an_old_remote_user_with_many_posts_is_permitted(self, app, db_session):
        """The recency sub-condition's False outcome: without it, the limit
        would apply forever regardless of account age."""
        make_instance('test.piefed.local', software='piefed')  # id 1
        remote = make_instance('veteran.example')  # id 2
        user = make_user(remote, 'veteranposter')
        user.ap_domain = remote.domain
        user.post_count = 400
        user.created = utcnow() - timedelta(days=2)
        community = make_community('postland14')
        assert can_create_post(user, community) is True


class TestCanCreatePostContentBanned:
    def test_a_banned_community_is_refused(self, app, db_session):
        """Fails if `if content.banned: return False` is deleted."""
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        user = make_user(None, 'contentbanned', local=True, with_keys=True)
        community = make_community('postland15')
        community.banned = True
        assert can_create_post(user, community) is False


class TestCanCreatePostModeratorAndAdminOrdering:
    """The most important cases in this module: `content.is_moderator(user)
    or user.is_admin()` returns True BEFORE `content.restricted_to_mods` is
    checked. An outcome test alone cannot show that order is right -- a
    moderator (or admin) of a restricted_to_mods community being permitted is
    also exactly what you'd see if the early return were simply missing and
    restricted_to_mods happened not to apply. The proof is the discrimination
    check in this task's report: move the early return below
    restricted_to_mods and this test starts failing.
    """

    def test_a_moderator_of_a_restricted_community_is_permitted(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')  # id 1
        make_user(None, 'owner', local=True)  # occupies id 1: keeps the mod
        # below off id 1, so is_admin()'s id==1 special case cannot explain a
        # pass here -- only content.is_moderator(user) can.
        mod = make_user(None, 'modposter', local=True, with_keys=True)
        community = make_community('postland16')
        community.restricted_to_mods = True
        make_community_member(mod, community, is_moderator=True)
        assert can_create_post(mod, community) is True

    def test_an_admin_of_a_restricted_community_is_permitted(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')  # id 1
        make_user(None, 'owner2', local=True)  # occupies id 1, so the admin
        # below is proved by a real Admin role, not the id==1 special case.
        admin = make_user(None, 'adminposter', local=True, with_keys=True)
        _make_admin(admin)
        community = make_community('postland17')
        community.restricted_to_mods = True
        assert can_create_post(admin, community) is True

    def test_a_non_moderator_is_refused_by_restricted_to_mods(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')  # id 1
        make_user(None, 'owner3', local=True)  # occupies id 1
        user = make_user(None, 'ordinaryposter', local=True, with_keys=True)
        community = make_community('postland18')
        community.restricted_to_mods = True
        assert can_create_post(user, community) is False


class TestCanCreatePostLocalOnly:
    """`if content.local_only and not user.is_local(): return False` -- two
    sub-conditions, each needing its own case with the other held eligible."""

    def test_local_only_refuses_a_remote_user(self, app, db_session):
        """The local_only check runs AFTER the moderator/admin early return
        (`content.is_moderator(user) or user.is_admin()`), and User.is_admin()
        special-cases user id 1 -- so the subject here must NOT be the first
        user created, or the admin bypass would mask this guard entirely."""
        make_instance('test.piefed.local', software='piefed')  # id 1
        make_user(None, 'filler', local=True)  # occupies id 1, off is_admin()'s path
        remote = make_instance('remoteposter.example')  # id 2
        user = make_user(remote, 'remotelocalonly')
        user.ap_domain = remote.domain
        community = make_community('postland19')
        community.local_only = True
        assert can_create_post(user, community) is False

    def test_local_only_permits_a_local_user(self, app, db_session):
        """Same id-1/is_admin() concern as above: without a filler, this user
        would be admin and pass regardless of whether the local_only line
        below is even reached."""
        make_instance('test.piefed.local', software='piefed')  # id 1
        make_user(None, 'filler2', local=True)  # occupies id 1
        user = make_user(None, 'locallocalonly', local=True, with_keys=True)
        community = make_community('postland20')
        community.local_only = True
        assert can_create_post(user, community) is True


class TestCanCreatePostBans:
    """Both checks here run AFTER the moderator/admin early return, so (as in
    TestCanCreatePostLocalOnly above) the subject must not land on user id 1
    -- User.is_admin() special-cases it -- or the ban would never actually be
    consulted."""

    def test_a_community_ban_is_refused(self, app, db_session):
        make_instance('test.piefed.local', software='piefed')  # user.instance_id FK target
        make_user(None, 'filler', local=True)  # occupies id 1, off is_admin()'s path
        user = make_user(None, 'commbanned', local=True, with_keys=True)
        community = make_community('postland21')
        ban_user_from_community(user, community)
        assert can_create_post(user, community) is False

    def test_an_instance_ban_is_refused(self, app, db_session):
        """An InstanceBan on the community's instance is refused -- but NOT,
        it turns out, by can_create_post's own tail check
        (`content.instance_id in banned_instances(user.id)`, app/utils.py
        line ~2360-2361). It is refused by the EARLIER
        `content.id in communities_banned_from(user.id)` check instead:
        communities_banned_from()'s own instance-ban half
        (app/utils.py:1546-1548) joins Community to InstanceBan on
        `Community.instance_id == InstanceBan.instance_id`, so any community
        sitting on a banned instance is already in that list before
        can_create_post ever reaches its own banned_instances() call.

        Verified directly (temporary probe, not committed): for this exact
        setup, `community.id in communities_banned_from(user.id)` is True
        AND `community.instance_id in banned_instances(user.id)` is True --
        the first can never be false when the second is true for the
        `content` community itself, since `content` is by construction one of
        the rows that join would return. This makes can_create_post's own
        tail instance-ban check (line 2361's `return False`) DEAD CODE: no
        input reaches it with a True condition, because
        communities_banned_from() always returns False first for the same
        community. Reported as a defect, not fixed -- see this task's report.
        `content.instance_id in banned_instances(user.id)` is written in the
        obvious style of an independent check but is fully subsumed by the
        line above it. make_community() hardcodes instance_id=1, so it is
        reassigned here to a second instance the user is banned from.
        """
        make_instance('test.piefed.local', software='piefed')  # id 1: user's instance
        remote = make_instance('communityinstance.example')  # id 2: community's instance
        make_user(None, 'filler2', local=True)  # occupies id 1, off is_admin()'s path
        user = make_user(None, 'instancebanned', local=True, with_keys=True)
        community = make_community('postland22')
        community.instance_id = remote.id
        db.session.commit()
        make_instance_ban(user, remote)
        assert can_create_post(user, community) is False
