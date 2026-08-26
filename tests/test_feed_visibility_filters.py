"""Both-direction coverage for get_deduped_post_ids' visibility filters --
the WHERE clauses that keep OTHER PEOPLE'S content out of a viewer's feed
(app/utils.py:3832-3899). A defect here shows a user posts from an account
they blocked, a domain they blocked, or a community they were banned from --
a safety failure, not an annoyance -- which is why every filter below gets a
PAIR of tests rather than one: an absence test alone would also pass for a
filter that excluded EVERYTHING, so it cannot by itself prove the filter is
doing anything targeted. Each class' docstring names the exact production
lines whose deletion fails ONLY that class' absence test, leaving its
presence test green.

Two traps this file works around (see the task-4 brief):

**LIMIT 1000** -- the query truncates at 1000 rows rather than filtering.
Every fixture below seeds one or two posts, nowhere near that cutoff.

**dedupe_post_ids runs AFTER this query** and collapses cross-posts that
share a `Post.cross_posts` entry (app/utils.py:3701-3705: the merge branch
only fires `if post_id[1]`, i.e. a non-empty cross_posts list). `make_post`
(tests/factories.py) never sets `cross_posts` -- it is left at its column
default, NULL -- and nothing in this file calls `Post.calculate_cross_posts()`
or otherwise populates it. So dedupe's merge branch never triggers for any
post seeded here, and a post's absence from a result is therefore always
attributable to a WHERE clause, never to deduping. Distinct `ap_id`/`url`
values per post (via distinct per-test hostnames) rule out the one other way
two rows could look like cross-posts to begin with.
"""
import uuid

from flask_login import login_user

from app import db
from app.utils import get_deduped_post_ids
from tests.factories import (ban_user_from_community, make_community, make_community_block,
                             make_domain, make_domain_block, make_flair_block, make_instance,
                             make_instance_block, make_post, make_post_flair, make_user,
                             make_user_block)


def feed_ids(app, viewer, community_ids, **kwargs):
    """The post ids get_deduped_post_ids returns for `viewer`, logged in.

    A fresh uuid result_id on every call bypasses the Redis short-circuit
    (`if redis_client.exists(result_id): return ...`, app/utils.py), so every
    call genuinely re-runs the query instead of replaying an earlier result --
    load-bearing here since several tests in one file would otherwise risk
    reusing a stale id.
    """
    with app.test_request_context('/'):
        login_user(viewer)
        return get_deduped_post_ids(uuid.uuid4().hex, community_ids, 'new', **kwargs)


class TestFilteredOutCommunities:
    """filtered_out_communities (app/utils.py:3833-3837): the viewer's own
    `community_keyword_filter`, ILIKE-matched against Community.name/title.
    Both tests seed an identical community+post; they differ only in whether
    the viewer's keyword filter matches the community's name. Mutation that
    fails the absent test: deleting the `if len(filtered_out_community_ids):`
    block at 3835-3837 (or the `if current_user.is_authenticated:` guard
    around it). The present test seeds a keyword that matches nothing, so
    that mutation leaves it passing -- there was nothing to filter back in.
    """

    def test_a_filtered_communitys_post_is_absent(self, app, db_session, redis_double):
        make_instance('fltout.example')
        viewer = make_user(None, 'fltoutviewer', local=True)
        author = make_user(None, 'fltoutauthor', local=True)
        community = make_community('fltoutmatch')
        post = make_post(community, author, 'https://fltout.example/posts/1')
        viewer.community_keyword_filter = 'fltoutmatch'
        db.session.commit()

        ids = feed_ids(app, viewer, [community.id])

        assert post.id not in ids

    def test_an_unfiltered_communitys_post_is_present(self, app, db_session, redis_double):
        make_instance('fltoutpresent.example')
        viewer = make_user(None, 'fltoutpviewer', local=True)
        author = make_user(None, 'fltoutpauthor', local=True)
        community = make_community('fltoutpresent')
        post = make_post(community, author, 'https://fltoutpresent.example/posts/1')
        viewer.community_keyword_filter = 'somethingelsequitedifferent'
        db.session.commit()

        ids = feed_ids(app, viewer, [community.id])

        assert post.id in ids


class TestBlockedOrBannedInstancesCommunitySide:
    """blocked_or_banned_instances, applied to the COMMUNITY's own
    instance_id (app/utils.py:3839-3843): `c.instance_id NOT IN
    :filtered_out_instance_ids`. The post's own instance is kept UNBLOCKED in
    both tests (author stays local, instance id 1, matching the community's
    default before it is moved) so this isolates the community-side clause
    from the post-side clause the next class covers -- only the COMMUNITY's
    host instance is reassigned and blocked. Mutation that fails the absent
    test: deleting the `if bi := blocked_or_banned_instances(...)` block at
    3839-3843 (both its `c.instance_id` and `p.instance_id` appends -- see the
    post-side class for why that second append is separately exercised).
    """

    def test_a_blocked_communitys_instance_post_is_absent(self, app, db_session, redis_double):
        make_instance('csblkhome.example')  # id 1: viewer's and author's instance
        remote = make_instance('csblkremote.example')  # id 2: community's instance
        viewer = make_user(None, 'csblkviewer', local=True)
        author = make_user(None, 'csblkauthor', local=True)
        community = make_community('csblkcomm')
        community.instance_id = remote.id
        db.session.commit()
        post = make_post(community, author, 'https://csblkhome.example/posts/1')
        make_instance_block(viewer, remote)

        ids = feed_ids(app, viewer, [community.id])

        assert post.id not in ids

    def test_an_unblocked_communitys_instance_post_is_present(self, app, db_session, redis_double):
        make_instance('csokhome.example')
        remote = make_instance('csokremote.example')
        viewer = make_user(None, 'csokviewer', local=True)
        author = make_user(None, 'csokauthor', local=True)
        community = make_community('csokcomm')
        community.instance_id = remote.id
        db.session.commit()
        post = make_post(community, author, 'https://csokhome.example/posts/1')
        # no instance block

        ids = feed_ids(app, viewer, [community.id])

        assert post.id in ids


class TestBlockedOrBannedInstancesPostSide:
    """blocked_or_banned_instances, applied to the POST's own instance_id
    (app/utils.py:3879-3881): `(p.instance_id NOT IN :instance_ids OR
    p.instance_id is null)`. The community's own instance is kept UNBLOCKED
    (community stays on the default local instance, id 1, from
    make_community's hardcoded instance_id=1) so this isolates the post-side
    clause from the previous class' community-side clause -- only the
    AUTHOR's (and so the post's) instance is remote and blocked. Mutation
    that fails the absent test: deleting the `if instance_ids :=
    blocked_or_banned_instances(...)` block at 3879-3881.
    """

    def test_a_blocked_authors_instance_post_is_absent(self, app, db_session, redis_double):
        make_instance('psblkhome.example')  # id 1: viewer's and community's instance
        remote = make_instance('psblkremote.example')  # id 2: author's instance
        viewer = make_user(None, 'psblkviewer', local=True)
        author = make_user(remote, 'psblkauthor', local=False)
        community = make_community('psblkcomm')  # stays on instance 1
        post = make_post(community, author, 'https://psblkremote.example/posts/1')
        make_instance_block(viewer, remote)

        ids = feed_ids(app, viewer, [community.id])

        assert post.id not in ids

    def test_an_unblocked_authors_instance_post_is_present(self, app, db_session, redis_double):
        make_instance('psokhome.example')
        remote = make_instance('psokremote.example')
        viewer = make_user(None, 'psokviewer', local=True)
        author = make_user(remote, 'psokauthor', local=False)
        community = make_community('psokcomm')
        post = make_post(community, author, 'https://psokremote.example/posts/1')
        # no instance block

        ids = feed_ids(app, viewer, [community.id])

        assert post.id in ids


class TestBlockedDomains:
    """blocked_domains (app/utils.py:3876-3878): `(p.domain_id NOT IN
    :domain_ids OR p.domain_id is null)`. Mutation that fails the absent
    test: deleting the `if domains_ids := blocked_domains(...)` block at
    3876-3878.
    """

    def test_a_blocked_domains_post_is_absent(self, app, db_session, redis_double):
        make_instance('domblk.example')
        viewer = make_user(None, 'domblkviewer', local=True)
        author = make_user(None, 'domblkauthor', local=True)
        community = make_community('domblkcomm')
        domain = make_domain('blocked-domain.example')
        post = make_post(community, author, 'https://domblk.example/posts/1')
        post.domain_id = domain.id
        db.session.commit()
        make_domain_block(viewer, domain)

        ids = feed_ids(app, viewer, [community.id])

        assert post.id not in ids

    def test_an_unblocked_domains_post_is_present(self, app, db_session, redis_double):
        make_instance('domokhome.example')
        viewer = make_user(None, 'domokviewer', local=True)
        author = make_user(None, 'domokauthor', local=True)
        community = make_community('domokcomm')
        domain = make_domain('unblocked-domain.example')
        post = make_post(community, author, 'https://domokhome.example/posts/1')
        post.domain_id = domain.id
        db.session.commit()
        # no domain block

        ids = feed_ids(app, viewer, [community.id])

        assert post.id in ids


class TestBlockedCommunities:
    """blocked_communities (app/utils.py:3882-3884): `p.community_id NOT IN
    :blocked_community_ids`. Mutation that fails the absent test: deleting
    the `if blocked_community_ids := blocked_communities(...)` block at
    3882-3884.
    """

    def test_a_blocked_communitys_post_is_absent(self, app, db_session, redis_double):
        make_instance('commblk.example')
        viewer = make_user(None, 'commblkviewer', local=True)
        author = make_user(None, 'commblkauthor', local=True)
        community = make_community('commblkcomm')
        post = make_post(community, author, 'https://commblk.example/posts/1')
        make_community_block(viewer, community)

        ids = feed_ids(app, viewer, [community.id])

        assert post.id not in ids

    def test_an_unblocked_communitys_post_is_present(self, app, db_session, redis_double):
        make_instance('commokhome.example')
        viewer = make_user(None, 'commokviewer', local=True)
        author = make_user(None, 'commokauthor', local=True)
        community = make_community('commokcomm')
        post = make_post(community, author, 'https://commokhome.example/posts/1')
        # no community block

        ids = feed_ids(app, viewer, [community.id])

        assert post.id in ids


class TestBlockedUsers:
    """blocked_users (app/utils.py:3886-3888): `p.user_id NOT IN
    :blocked_accounts`. One of this task's two Step-3 discrimination targets:
    deleting the `if blocked_accounts := blocked_users(...)` block at
    3886-3888 makes ONLY test_a_blocked_authors_post_is_absent fail --
    test_an_unblocked_authors_post_is_present keeps passing, because nothing
    was filtering its post to begin with. Measured counts are in
    task-4-report.md.
    """

    def test_a_blocked_authors_post_is_absent(self, app, db_session, redis_double):
        """Fails if the blocked_accounts clause is removed."""
        make_instance('userblk.example')
        viewer = make_user(None, 'userblkviewer', local=True)
        author = make_user(None, 'userblkauthor', local=True)
        community = make_community('userblkcomm')
        post = make_post(community, author, 'https://userblk.example/posts/1')
        make_user_block(viewer, author)

        ids = feed_ids(app, viewer, [community.id])

        assert post.id not in ids

    def test_an_unblocked_authors_post_is_present(self, app, db_session, redis_double):
        """The other direction. Without it, a filter excluding EVERYTHING
        would also pass -- the absence test alone cannot tell those apart."""
        make_instance('userokhome.example')
        viewer = make_user(None, 'userokviewer', local=True)
        author = make_user(None, 'userokauthor', local=True)
        community = make_community('userokcomm')
        post = make_post(community, author, 'https://userokhome.example/posts/1')
        # no user block

        ids = feed_ids(app, viewer, [community.id])

        assert post.id in ids


class TestCommunitiesBannedFrom:
    """communities_banned_from (app/utils.py:3890-3892): `p.community_id NOT
    IN :banned_from`. The other Step-3 discrimination target: deleting the
    `if banned_from := communities_banned_from(...)` block at 3890-3892 makes
    ONLY test_a_banned_from_communitys_post_is_absent fail --
    test_an_unbanned_communitys_post_is_present keeps passing. Measured
    counts are in task-4-report.md.
    """

    def test_a_banned_from_communitys_post_is_absent(self, app, db_session, redis_double):
        make_instance('banblk.example')
        viewer = make_user(None, 'banblkviewer', local=True)
        author = make_user(None, 'banblkauthor', local=True)
        community = make_community('banblkcomm')
        post = make_post(community, author, 'https://banblk.example/posts/1')
        ban_user_from_community(viewer, community)

        ids = feed_ids(app, viewer, [community.id])

        assert post.id not in ids

    def test_an_unbanned_communitys_post_is_present(self, app, db_session, redis_double):
        make_instance('banokhome.example')
        viewer = make_user(None, 'banokviewer', local=True)
        author = make_user(None, 'banokauthor', local=True)
        community = make_community('banokcomm')
        post = make_post(community, author, 'https://banokhome.example/posts/1')
        # no community ban

        ids = feed_ids(app, viewer, [community.id])

        assert post.id in ids


class TestBlockedFlair:
    """The direct CommunityFlairBlock / post_flair query (app/utils.py:3893-
    3899), reached only when `community_ids[0] != -1` -- both tests below
    pass a specific community id, never [-1], to stay on this branch.
    Mutation that fails the absent test: deleting the `if blocked_flair:`
    block at 3896-3899 (or the query at 3894-3895 that feeds it).
    """

    def test_a_blocked_flairs_post_is_absent(self, app, db_session, redis_double):
        make_instance('flairblk.example')
        viewer = make_user(None, 'flairblkviewer', local=True)
        author = make_user(None, 'flairblkauthor', local=True)
        community = make_community('flairblkcomm')
        post = make_post(community, author, 'https://flairblk.example/posts/1')
        flair = make_post_flair(post, name='spoiler')
        make_flair_block(viewer, flair)

        ids = feed_ids(app, viewer, [community.id])

        assert post.id not in ids

    def test_an_unblocked_flairs_post_is_present(self, app, db_session, redis_double):
        make_instance('flairokhome.example')
        viewer = make_user(None, 'flairokviewer', local=True)
        author = make_user(None, 'flairokauthor', local=True)
        community = make_community('flairokcomm')
        post = make_post(community, author, 'https://flairokhome.example/posts/1')
        make_post_flair(post, name='spoiler')  # flair exists, but is not blocked
        # no flair block

        ids = feed_ids(app, viewer, [community.id])

        assert post.id in ids
