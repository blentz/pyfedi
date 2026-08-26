"""One-case-per-rule coverage for get_instance_stickies (app/utils.py:3990-4043)
and its query helper instance_sticky_posts (app/utils.py:3972-3987).

get_instance_stickies is a Python filter CHAIN, not a SQL WHERE clause like
tasks 4/5's get_deduped_post_ids: a sequence of `continue` statements over
posts already fetched by instance_sticky_posts(), each one a distinct
"this post should not be visible" rule. The failure mode the task-6 brief
calls out is specific: a single post that trips the FIRST rule in a loop
reaches full branch coverage for the whole loop (every `if` has been seen
True and False across the suite) while rules 2..N are never independently
exercised -- their `continue` could be deleted and nothing would fail. So
this file gives each rule its own post, tripping ONLY that rule, with every
other rule held out of the way.

Rule count, derived with the following command against this checkout
(reproduce with `sed -n '3990,4043p' app/utils.py | grep -n continue`, or
the AST walk below -- both give the same seven line numbers):

    python3 -c "
    import ast
    tree = ast.parse(open('app/utils.py').read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == 'get_instance_stickies':
            for c in ast.walk(node):
                if isinstance(c, ast.Continue):
                    print(c.lineno)
    "

Output: 4004, 4007, 4026, 4029, 4032, 4035, 4038 -- seven `continue`
statements, split 2/5 across the two current_user.is_anonymous arms:

Anonymous (current_user.is_anonymous is True), in order:
  1. :4002-4004 -- CONTENT_WARNING disabled AND (nsfw OR nsfl)
  2. :4006-4007 -- community not in community_ids AND NOT all_communities

Authenticated (the else arm), in order:
  3. :4025-4026 -- community not in community_ids AND NOT all_communities
  4. :4028-4029 -- hide_nsfl == 1 AND post.nsfl
  5. :4031-4032 -- hide_nsfw == 1 AND post.nsfw
  6. :4034-4035 -- post.id in read_post_ids (populated only when hide_read_posts)
  7. :4037-4038 -- post.id in hidden_post_ids

Plus the `all_communities` flag itself (:3993-3996): `len(community_ids) ==
1 and community_ids[0] < 0`, which gates rules 2 and 3's community check on
both paths.

Both Task-4/5 traps are inherited but do not apply here: this function does
not call dedupe_post_ids, and there is no LIMIT -- every fixture below seeds
one or two stickies, so neither matters. What DOES carry over is the
hide_nsfw/hide_nsfl column-default coupling (both default to 1,
app/models.py:984-985): every authenticated viewer below sets both to 0
explicitly unless the test is about one of them.

instance_sticky_posts() is exercised incidentally by every test here (it is
the only source of the `posts` list get_instance_stickies filters) but is
otherwise pure query assembly -- an `elif` chain choosing an ORDER BY, no
visibility logic. It is not given its own rule-by-rule treatment; sort='new'
is used throughout since ordering is irrelevant to the presence/absence
assertions.

No test here takes redis_double. Unlike get_deduped_post_ids (tasks 4/5),
get_instance_stickies takes no result_id and neither it nor
instance_sticky_posts touches app.redis_client or get_redis_connection on
any path exercised by a plain query -- confirmed by reading both functions
and grepping app/utils.py for redis references near them. Nothing here
writes to the real test Redis to guard against.

Every mutation direction claimed in this file's class docstrings was run
twice, once as a delete (remove the rule's `continue`/condition -- the
matching absence test must fail, and only it) and once as an over-broaden
(make the rule fire unconditionally -- the matching presence test must
fail, and only it). Results are in task-6-report.md, not restated per class
here beyond the single line each docstring already carries.
"""
from flask_login import login_user

from app import db
from app.utils import get_instance_stickies
from tests.factories import (hide_post, make_community, make_instance, make_post,
                             make_user, mark_post_read)


def make_sticky(community, user, ap_id, **kwargs):
    """A Post that instance_sticky_posts() will fetch: instance_sticky=True,
    deleted=False (factory default), status=1 (factory default, > 0). Those
    three are exactly instance_sticky_posts()'s WHERE clause
    (app/utils.py:3966-3967, 3974), so every fixture below passes it
    uncontested -- what get_instance_stickies then does with the post is
    the thing under test.
    """
    post = make_post(community, user, ap_id, **kwargs)
    post.instance_sticky = True
    db.session.commit()
    return post


def anon_stickies(app, community_ids, sort='new'):
    """get_instance_stickies for an ANONYMOUS viewer -- no login_user call,
    so current_user.is_anonymous is True and the anonymous arm runs.
    """
    with app.test_request_context('/'):
        return get_instance_stickies(community_ids, sort)


def auth_stickies(app, viewer, community_ids, sort='new'):
    with app.test_request_context('/'):
        login_user(viewer)
        return get_instance_stickies(community_ids, sort)


def ids_of(posts):
    return [p.id for p in posts]


class TestAllCommunitiesFlag:
    """all_communities (app/utils.py:3993-3996): True iff community_ids is
    exactly [-1]. It short-circuits BOTH community-membership rules (anon
    rule 2 at :4006-4007, authenticated rule 3 at :4025-4026) via `not
    all_communities`. Mutation that fails only these two presence tests:
    hardcoding `all_communities = False` (or deleting the `community_ids[0]
    < 0` half of the condition). The FALSE side of this flag is already
    proven by TestAnonymousCommunityNotInView and
    TestAuthenticatedCommunityNotInView below, whose fixtures pass a real
    community id and get an absent result for a sticky elsewhere -- if
    `all_communities` mutated to always-True, those two absence tests would
    fail as collateral, which is exactly the cross-check this class's
    presence tests are paired against.
    """

    def test_a_sticky_from_any_community_is_present_to_anonymous_with_the_all_communities_marker(
            self, app, db_session):
        make_instance('allcommanon.example')
        author = make_user(None, 'allcommanonauthor', local=True)
        elsewhere = make_community('allcommanonelsewhere')
        post = make_sticky(elsewhere, author, 'https://allcommanon.example/posts/1')

        posts = anon_stickies(app, [-1])

        assert post.id in ids_of(posts)

    def test_a_sticky_from_any_community_is_present_to_an_authenticated_viewer_with_the_all_communities_marker(
            self, app, db_session):
        make_instance('allcommauth.example')
        viewer = make_user(None, 'allcommauthviewer', local=True)
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 0
        author = make_user(None, 'allcommauthauthor', local=True)
        elsewhere = make_community('allcommauthelsewhere')
        post = make_sticky(elsewhere, author, 'https://allcommauth.example/posts/1')
        db.session.commit()

        posts = auth_stickies(app, viewer, [-1])

        assert post.id in ids_of(posts)


class TestAnonymousNsfwFilter:
    """app/utils.py:4002-4004: `if not CONTENT_WARNING: if post.nsfw or
    post.nsfl: continue`. This class trips the nsfw half; TestAnonymousNsfl
    Filter below trips the nsfl half of the same `or`, and
    TestAnonymousContentWarningConfigured proves the outer `not
    CONTENT_WARNING` guard matters on its own. .env.test does not set
    CONTENT_WARNING, so config.py's `int(os.environ.get('CONTENT_WARNING')
    or 0)` leaves it 0/falsy here -- the guard's True arm is the ambient
    condition for every test in this class, not something set up locally.
    Mutation that fails only the absent test: deleting the inner `if
    post.nsfw or post.nsfl: continue`. Mutation that fails only the present
    test: replacing the condition with `True` (or dropping the outer `if
    not CONTENT_WARNING:` guard so the continue always fires).
    """

    def test_an_nsfw_sticky_is_absent_to_anonymous_viewers(self, app, db_session):
        make_instance('anonnsfw.example')
        author = make_user(None, 'anonnsfwauthor', local=True)
        community = make_community('anonnsfwcomm')
        post = make_sticky(community, author, 'https://anonnsfw.example/posts/1')
        post.nsfw = True
        db.session.commit()

        posts = anon_stickies(app, [community.id])

        assert post.id not in ids_of(posts)

    def test_a_non_nsfw_sticky_is_present_to_anonymous_viewers(self, app, db_session):
        make_instance('anonnsfwok.example')
        author = make_user(None, 'anonnsfwokauthor', local=True)
        community = make_community('anonnsfwokcomm')
        post = make_sticky(community, author, 'https://anonnsfwok.example/posts/1')

        posts = anon_stickies(app, [community.id])

        assert post.id in ids_of(posts)


class TestAnonymousNsflFilter:
    """The nsfl half of app/utils.py:4002-4004's `post.nsfw or post.nsfl`.
    Mutation that fails only the absent test: narrowing the condition to
    `post.nsfw` alone (dropping `or post.nsfl`) -- an nsfl-only post would
    then sail through unblocked while TestAnonymousNsfwFilter's tests stay
    green, which is exactly why nsfw and nsfl each get an independent post
    here rather than sharing one.
    """

    def test_an_nsfl_sticky_is_absent_to_anonymous_viewers(self, app, db_session):
        make_instance('anonnsfl.example')
        author = make_user(None, 'anonnsflauthor', local=True)
        community = make_community('anonnsflcomm')
        post = make_sticky(community, author, 'https://anonnsfl.example/posts/1')
        post.nsfl = True
        db.session.commit()

        posts = anon_stickies(app, [community.id])

        assert post.id not in ids_of(posts)

    def test_a_non_nsfl_sticky_is_present_to_anonymous_viewers(self, app, db_session):
        make_instance('anonnsflok.example')
        author = make_user(None, 'anonnsflokauthor', local=True)
        community = make_community('anonnsflokcomm')
        post = make_sticky(community, author, 'https://anonnsflok.example/posts/1')

        posts = anon_stickies(app, [community.id])

        assert post.id in ids_of(posts)


class TestAnonymousContentWarningConfigured:
    """The outer guard at app/utils.py:4002, isolated from the inner nsfw/
    nsfl check the two classes above cover. Both tests restore
    app.config['CONTENT_WARNING'] in a `finally`, since it is process-global
    config, not a per-request value -- the same pattern
    TestAnonymousContentWarningBranch in test_feed_display_preferences.py
    uses for the sibling guard on get_deduped_post_ids. Mutation that fails
    only this class's present test: deleting the `if not
    current_app.config['CONTENT_WARNING']:` guard so the inner nsfw/nsfl
    check always runs.
    """

    def test_an_nsfw_sticky_is_present_to_anonymous_viewers_when_content_warning_is_configured(
            self, app, db_session):
        make_instance('anoncwon.example')
        author = make_user(None, 'anoncwonauthor', local=True)
        community = make_community('anoncwoncomm')
        post = make_sticky(community, author, 'https://anoncwon.example/posts/1')
        post.nsfw = True
        db.session.commit()

        original = app.config['CONTENT_WARNING']
        app.config['CONTENT_WARNING'] = 1
        try:
            posts = anon_stickies(app, [community.id])
        finally:
            app.config['CONTENT_WARNING'] = original

        assert post.id in ids_of(posts)


class TestAnonymousCommunityNotInView:
    """app/utils.py:4006-4007: `if post.community_id not in community_ids
    and not all_communities: continue`. all_communities is False throughout
    (community_ids is a real id, not [-1]), so this isolates the membership
    check from the flag TestAllCommunitiesFlag covers. Mutation that fails
    only the absent test: deleting this `continue` (or the whole `if`).
    Mutation that fails only the present test: dropping the `not
    all_communities` half so the continue fires unconditionally regardless
    of membership.
    """

    def test_a_sticky_from_an_unviewed_community_is_absent_to_anonymous_viewers(
            self, app, db_session):
        make_instance('anoncomm.example')
        author = make_user(None, 'anoncommauthor', local=True)
        viewed = make_community('anoncommviewed')
        elsewhere = make_community('anoncommelsewhere')
        post = make_sticky(elsewhere, author, 'https://anoncomm.example/posts/1')

        posts = anon_stickies(app, [viewed.id])

        assert post.id not in ids_of(posts)

    def test_a_sticky_from_a_viewed_community_is_present_to_anonymous_viewers(
            self, app, db_session):
        make_instance('anoncommok.example')
        author = make_user(None, 'anoncommokauthor', local=True)
        viewed = make_community('anoncommokviewed')
        post = make_sticky(viewed, author, 'https://anoncommok.example/posts/1')

        posts = anon_stickies(app, [viewed.id])

        assert post.id in ids_of(posts)


class TestAuthenticatedCommunityNotInView:
    """The authenticated twin of app/utils.py:4025-4026 -- same predicate
    shape as the anonymous rule above, but a SEPARATE `continue` statement
    on a separate code path, so it needs its own pair rather than inheriting
    coverage from the anonymous class. Mutation directions mirror
    TestAnonymousCommunityNotInView's.
    """

    def test_a_sticky_from_an_unviewed_community_is_absent_to_an_authenticated_viewer(
            self, app, db_session):
        make_instance('authcomm.example')
        viewer = make_user(None, 'authcommviewer', local=True)
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 0
        author = make_user(None, 'authcommauthor', local=True)
        viewed = make_community('authcommviewed')
        elsewhere = make_community('authcommelsewhere')
        post = make_sticky(elsewhere, author, 'https://authcomm.example/posts/1')
        db.session.commit()

        posts = auth_stickies(app, viewer, [viewed.id])

        assert post.id not in ids_of(posts)

    def test_a_sticky_from_a_viewed_community_is_present_to_an_authenticated_viewer(
            self, app, db_session):
        make_instance('authcommok.example')
        viewer = make_user(None, 'authcommokviewer', local=True)
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 0
        author = make_user(None, 'authcommokauthor', local=True)
        viewed = make_community('authcommokviewed')
        post = make_sticky(viewed, author, 'https://authcommok.example/posts/1')
        db.session.commit()

        posts = auth_stickies(app, viewer, [viewed.id])

        assert post.id in ids_of(posts)


class TestAuthenticatedHideNsfl:
    """app/utils.py:4028-4029: `if current_user.hide_nsfl == 1 and
    post.nsfl: continue`. Mutation that fails only the absent test: deleting
    this `continue`. Mutation that fails only the present test: dropping
    the `and post.nsfl` half so the continue fires for every post once
    hide_nsfl is set, nsfl or not.
    """

    def test_an_nsfl_sticky_is_absent_when_the_viewer_hides_nsfl(self, app, db_session):
        make_instance('authnsfl.example')
        viewer = make_user(None, 'authnsflviewer', local=True)
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 1
        author = make_user(None, 'authnsflauthor', local=True)
        community = make_community('authnsflcomm')
        post = make_sticky(community, author, 'https://authnsfl.example/posts/1')
        post.nsfl = True
        db.session.commit()

        posts = auth_stickies(app, viewer, [community.id])

        assert post.id not in ids_of(posts)

    def test_a_non_nsfl_sticky_is_present_when_the_viewer_hides_nsfl(self, app, db_session):
        make_instance('authnsflok.example')
        viewer = make_user(None, 'authnsflokviewer', local=True)
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 1
        author = make_user(None, 'authnsflokauthor', local=True)
        community = make_community('authnsflokcomm')
        post = make_sticky(community, author, 'https://authnsflok.example/posts/1')
        db.session.commit()

        posts = auth_stickies(app, viewer, [community.id])

        assert post.id in ids_of(posts)


class TestAuthenticatedHideNsfw:
    """app/utils.py:4031-4032: `if current_user.hide_nsfw == 1 and
    post.nsfw: continue`. Mutation directions mirror
    TestAuthenticatedHideNsfl's.
    """

    def test_an_nsfw_sticky_is_absent_when_the_viewer_hides_nsfw(self, app, db_session):
        make_instance('authnsfw.example')
        viewer = make_user(None, 'authnsfwviewer', local=True)
        viewer.hide_nsfw = 1
        viewer.hide_nsfl = 0
        author = make_user(None, 'authnsfwauthor', local=True)
        community = make_community('authnsfwcomm')
        post = make_sticky(community, author, 'https://authnsfw.example/posts/1')
        post.nsfw = True
        db.session.commit()

        posts = auth_stickies(app, viewer, [community.id])

        assert post.id not in ids_of(posts)

    def test_a_non_nsfw_sticky_is_present_when_the_viewer_hides_nsfw(self, app, db_session):
        make_instance('authnsfwok.example')
        viewer = make_user(None, 'authnsfwokviewer', local=True)
        viewer.hide_nsfw = 1
        viewer.hide_nsfl = 0
        author = make_user(None, 'authnsfwokauthor', local=True)
        community = make_community('authnsfwokcomm')
        post = make_sticky(community, author, 'https://authnsfwok.example/posts/1')
        db.session.commit()

        posts = auth_stickies(app, viewer, [community.id])

        assert post.id in ids_of(posts)


class TestAuthenticatedHideReadPosts:
    """app/utils.py:4034-4035: `if post.id in read_post_ids: continue`,
    where read_post_ids is populated (:4015-4018) only when
    current_user.hide_read_posts is truthy -- otherwise it stays `[]`
    (:4019-4020) and this `continue` can never fire. Both tests set
    hide_read_posts=True so the query that fills read_post_ids actually
    runs; they differ only in whether the post is marked read via the
    mark_post_read factory (which inserts into the same `read_posts` table
    this rule's own query reads, per its docstring in tests/factories.py).
    Mutation that fails only the absent test: deleting this `continue`.
    Mutation that fails only the present test: replacing the membership
    check with something unconditionally true (e.g. `if True:`), or forcing
    read_post_ids to be populated regardless of hide_read_posts.
    """

    def test_a_read_sticky_is_absent_when_the_viewer_hides_read_posts(self, app, db_session):
        make_instance('authread.example')
        viewer = make_user(None, 'authreadviewer', local=True)
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 0
        viewer.hide_read_posts = True
        author = make_user(None, 'authreadauthor', local=True)
        community = make_community('authreadcomm')
        post = make_sticky(community, author, 'https://authread.example/posts/1')
        db.session.commit()
        mark_post_read(viewer, post)

        posts = auth_stickies(app, viewer, [community.id])

        assert post.id not in ids_of(posts)

    def test_an_unread_sticky_is_present_when_the_viewer_hides_read_posts(self, app, db_session):
        make_instance('authreadok.example')
        viewer = make_user(None, 'authreadokviewer', local=True)
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 0
        viewer.hide_read_posts = True
        author = make_user(None, 'authreadokauthor', local=True)
        community = make_community('authreadokcomm')
        post = make_sticky(community, author, 'https://authreadok.example/posts/1')
        db.session.commit()

        posts = auth_stickies(app, viewer, [community.id])

        assert post.id in ids_of(posts)


class TestAuthenticatedHiddenPost:
    """app/utils.py:4037-4038: `if post.id in hidden_post_ids: continue`.
    hidden_post_ids (:4012-4014) is queried unconditionally for every
    authenticated viewer, unlike read_post_ids, so no extra flag is needed
    to reach this rule -- only whether hide_post was called for this post.
    This is the rule the task-6 brief names explicitly for the Step-3
    discrimination proof: delete this `continue` and confirm only
    test_a_hidden_sticky_is_absent_when_the_viewer_has_hidden_it fails.
    Mutation that fails only the present test: replacing the membership
    check with something unconditionally true.
    """

    def test_a_hidden_sticky_is_absent_when_the_viewer_has_hidden_it(self, app, db_session):
        make_instance('authhidden.example')
        viewer = make_user(None, 'authhiddenviewer', local=True)
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 0
        author = make_user(None, 'authhiddenauthor', local=True)
        community = make_community('authhiddencomm')
        post = make_sticky(community, author, 'https://authhidden.example/posts/1')
        db.session.commit()
        hide_post(viewer, post)

        posts = auth_stickies(app, viewer, [community.id])

        assert post.id not in ids_of(posts)

    def test_a_non_hidden_sticky_is_present_when_the_viewer_has_hidden_nothing(
            self, app, db_session):
        make_instance('authhiddenok.example')
        viewer = make_user(None, 'authhiddenokviewer', local=True)
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 0
        author = make_user(None, 'authhiddenokauthor', local=True)
        community = make_community('authhiddenokcomm')
        post = make_sticky(community, author, 'https://authhiddenok.example/posts/1')
        db.session.commit()

        posts = auth_stickies(app, viewer, [community.id])

        assert post.id in ids_of(posts)


class TestInstanceStickyPostsSortBranches:
    """instance_sticky_posts's elif chain (app/utils.py:3975-3986), which
    picks the ORDER BY. This is query assembly, not a visibility rule, so
    it does not get a rule-by-rule presence/absence pair like the classes
    above -- but every branch this suite's other classes leave unexercised
    (they all pass sort='new') is covered here, with an assertion on the
    actual resulting order rather than just executing the line. 'new'
    itself (:3981-3982) is exercised by every other class in this file and
    is not repeated here.
    """

    def test_hot_sort_orders_by_ranking_then_posted_at(self, app, db_session):
        make_instance('sorthot.example')
        author = make_user(None, 'sorthotauthor', local=True)
        community = make_community('sorthotcomm')
        low = make_sticky(community, author, 'https://sorthot.example/posts/low')
        high = make_sticky(community, author, 'https://sorthot.example/posts/high')
        low.ranking = 1.0
        high.ranking = 2.0
        db.session.commit()

        posts = anon_stickies(app, [community.id], sort='hot')

        assert ids_of(posts) == [high.id, low.id]

    def test_scaled_sort_orders_by_ranking_scaled(self, app, db_session):
        make_instance('sortscaled.example')
        author = make_user(None, 'sortscaledauthor', local=True)
        community = make_community('sortscaledcomm')
        low = make_sticky(community, author, 'https://sortscaled.example/posts/low')
        high = make_sticky(community, author, 'https://sortscaled.example/posts/high')
        low.ranking_scaled = 1.0
        high.ranking_scaled = 2.0
        db.session.commit()

        posts = anon_stickies(app, [community.id], sort='scaled')

        assert ids_of(posts) == [high.id, low.id]

    def test_top_sort_orders_by_net_votes(self, app, db_session):
        make_instance('sorttop.example')
        author = make_user(None, 'sorttopauthor', local=True)
        community = make_community('sorttopcomm')
        low = make_sticky(community, author, 'https://sorttop.example/posts/low')
        high = make_sticky(community, author, 'https://sorttop.example/posts/high')
        low.up_votes, low.down_votes = 1, 0
        high.up_votes, high.down_votes = 10, 0
        db.session.commit()

        posts = anon_stickies(app, [community.id], sort='top')

        assert ids_of(posts) == [high.id, low.id]

    def test_old_sort_orders_by_posted_at_ascending(self, app, db_session):
        make_instance('sortold.example')
        author = make_user(None, 'sortoldauthor', local=True)
        community = make_community('sortoldcomm')
        earlier = make_sticky(community, author, 'https://sortold.example/posts/earlier')
        later = make_sticky(community, author, 'https://sortold.example/posts/later')
        earlier.posted_at = earlier.posted_at.replace(year=earlier.posted_at.year - 1)
        db.session.commit()

        posts = anon_stickies(app, [community.id], sort='old')

        assert ids_of(posts) == [earlier.id, later.id]

    def test_active_sort_orders_by_last_active(self, app, db_session):
        make_instance('sortactive.example')
        author = make_user(None, 'sortactiveauthor', local=True)
        community = make_community('sortactivecomm')
        stale = make_sticky(community, author, 'https://sortactive.example/posts/stale')
        fresh = make_sticky(community, author, 'https://sortactive.example/posts/fresh')
        stale.last_active = stale.last_active.replace(year=stale.last_active.year - 1)
        db.session.commit()

        posts = anon_stickies(app, [community.id], sort='active')

        assert ids_of(posts) == [fresh.id, stale.id]

    def test_an_unrecognized_sort_still_returns_every_matching_sticky(self, app, db_session):
        """None of the six `if`/`elif` conditions at :3975-3986 match, so
        the query falls through with no ORDER BY applied at all (the
        3985->3987 branch coverage.py reports as otherwise unexercised).
        This does not raise and does not drop posts -- it just leaves their
        order unspecified, which is why this test asserts membership rather
        than a sequence, unlike the five branch tests above it.
        """
        make_instance('sortbogus.example')
        author = make_user(None, 'sortbogusauthor', local=True)
        community = make_community('sortboguscomm')
        one = make_sticky(community, author, 'https://sortbogus.example/posts/1')
        two = make_sticky(community, author, 'https://sortbogus.example/posts/2')

        posts = anon_stickies(app, [community.id], sort='not-a-real-sort')

        assert {one.id, two.id} == set(ids_of(posts))
