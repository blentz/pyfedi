"""Single-direction coverage for get_deduped_post_ids' DISPLAY PREFERENCE
filters (app/utils.py:3880-3924): hide_nsfw, hide_nsfl, hide_read_posts,
hide_gen_ai, ignore_bots, read_language_ids, hide_low_quality. Plus the
anonymous CONTENT_WARNING branch at 3866-3857.

Per the task-5 brief, this is deliberately LIGHTER than task 4's visibility
filters (tests/test_feed_visibility_filters.py). Those gate OTHER PEOPLE's
content -- a defect there is a safety failure. These gate the viewer's OWN
taste settings -- a defect here is an annoyance. The spec's allocation is
single-direction: for each preference, assert the hidden thing is absent
when the preference is on. Do not expand this into task 4's paired shape.

The one exception taken here: every one of these seven preferences turned
out to have a presence half that costs nothing extra -- the SAME viewer,
SAME community, SAME post-construction call, with exactly one attribute on
the post (or community, for hide_low_quality) left at its default instead of
flipped to the value that trips the filter. No second user, community, or
instance is built for it. That is cheap insurance against a filter that
excludes EVERYTHING regardless of the flag it claims to key off -- a single
absence test cannot tell "this filter targets nsfw posts" apart from "this
filter hides all posts once hide_nsfw is set". Each class' docstring names
the exact clause whose deletion fails only its absence test, and (where a
presence half exists) the over-broadening that fails only its presence half.

`hide_nsfw` and `hide_nsfl` are the two column defaults that are already ON
(`default=1`, app/models.py:984-985) -- every OTHER class below explicitly
sets both to 0 on its viewer, even though that class is not testing either
preference. Without that, every other class' viewer would carry the column
default of 1 into a run of this file, so `hide_nsfw`'s and `hide_nsfl`'s own
`if ... == 1:` conditions would only ever be seen True across the whole
file, never False -- a real partial-branch gap, and (worse) it would mean an
over-broadened `hide_nsfw`/`hide_nsfl` clause failed six unrelated presence
tests as collateral rather than only its own, discovered while running the
Step-3 discrimination checks below. The other five preferences' column
defaults are already 0/False/None, so their own conditions get the False
arm for free from every other class and needed no such override.

Two traps carried from task 4 (see its docstring and the task-4/5 briefs for
the full reasoning):

**LIMIT 1000** -- fixtures below seed at most two posts per test, nowhere
near that cutoff.

**dedupe_post_ids runs AFTER this query** and only merges rows sharing a
non-empty `Post.cross_posts` (app/utils.py: the merge branch fires only
`if post_id[1]`). `make_post` never sets `cross_posts` -- it stays NULL --
and nothing here calls `Post.calculate_cross_posts()`. So, exactly as task 4
established, a post's absence from a result here is always attributable to a
WHERE clause, never to deduping.

`read_language_ids` is list-valued, not boolean, so its filter (an inclusive
`IN (...) OR IS NULL`) is covered on its own terms rather than forced into
the True/False shape the other six share.
"""
import uuid

from app import db
from app.models import Language
from app.utils import get_deduped_post_ids
from tests.factories import feed_ids, make_community, make_instance, make_post, make_user, mark_post_read


def anon_feed_ids(app, community_ids, **kwargs):
    """The post ids get_deduped_post_ids returns for an ANONYMOUS viewer --
    no login_user call, so current_user is Flask-Login's AnonymousUserMixin
    and the function's `current_user.is_anonymous` branch is taken.
    """
    with app.test_request_context('/'):
        return get_deduped_post_ids(uuid.uuid4().hex, community_ids, 'new', **kwargs)


class TestHideNsfw:
    """app/utils.py:3884-3885: `if current_user.hide_nsfw == 1:
    post_id_where.append('p.nsfw is false ')`. Mutation that fails only the
    absence test: deleting that `if` block. Mutation that fails only the
    presence test: replacing the appended fragment with `1=0 ` inside the
    same `if` -- the filter would then exclude every post once hide_nsfw is
    set, nsfw or not.
    """

    def test_an_nsfw_post_is_absent_when_hide_nsfw_is_set(self, app, db_session, redis_double):
        make_instance('nsfwhide.example')
        viewer = make_user(None, 'nsfwhideviewer', local=True)
        author = make_user(None, 'nsfwhideauthor', local=True)
        community = make_community('nsfwhidecomm')
        post = make_post(community, author, 'https://nsfwhide.example/posts/1')
        post.nsfw = True
        viewer.hide_nsfw = 1
        viewer.hide_nsfl = 0  # isolate from TestHideNsfl's own clause
        db.session.commit()

        ids = feed_ids(app, viewer, [community.id])

        assert post.id not in ids

    def test_a_non_nsfw_post_is_present_when_hide_nsfw_is_set(self, app, db_session, redis_double):
        """Same viewer preference, same community; only post.nsfw flips (to
        its factory default, False). Proves the filter targets nsfw posts
        rather than hiding everything once hide_nsfw is set.
        """
        make_instance('nsfwokhome.example')
        viewer = make_user(None, 'nsfwokviewer', local=True)
        author = make_user(None, 'nsfwokauthor', local=True)
        community = make_community('nsfwokcomm')
        post = make_post(community, author, 'https://nsfwokhome.example/posts/1')
        viewer.hide_nsfw = 1
        viewer.hide_nsfl = 0  # isolate from TestHideNsfl's own clause
        db.session.commit()

        ids = feed_ids(app, viewer, [community.id])

        assert post.id in ids


class TestHideNsfl:
    """app/utils.py:3882-3883: `if current_user.hide_nsfl == 1:
    post_id_where.append('p.nsfl is false ')`. Mutations mirror TestHideNsfw.
    """

    def test_an_nsfl_post_is_absent_when_hide_nsfl_is_set(self, app, db_session, redis_double):
        make_instance('nsflhide.example')
        viewer = make_user(None, 'nsflhideviewer', local=True)
        author = make_user(None, 'nsflhideauthor', local=True)
        community = make_community('nsflhidecomm')
        post = make_post(community, author, 'https://nsflhide.example/posts/1')
        post.nsfl = True
        viewer.hide_nsfl = 1
        viewer.hide_nsfw = 0  # isolate from TestHideNsfw's own clause
        db.session.commit()

        ids = feed_ids(app, viewer, [community.id])

        assert post.id not in ids

    def test_a_non_nsfl_post_is_present_when_hide_nsfl_is_set(self, app, db_session, redis_double):
        make_instance('nsflokhome.example')
        viewer = make_user(None, 'nsflokviewer', local=True)
        author = make_user(None, 'nsflokauthor', local=True)
        community = make_community('nsflokcomm')
        post = make_post(community, author, 'https://nsflokhome.example/posts/1')
        viewer.hide_nsfl = 1
        viewer.hide_nsfw = 0  # isolate from TestHideNsfw's own clause
        db.session.commit()

        ids = feed_ids(app, viewer, [community.id])

        assert post.id in ids


class TestHideReadPosts:
    """app/utils.py:3886-3887: `if current_user.hide_read_posts:
    post_id_where.append('p.id NOT IN (SELECT read_post_id FROM
    "read_posts" WHERE user_id = :user_id) ')`. Mutation that fails only the
    absence test: deleting the `if` block. Mutation that fails only the
    presence test: replacing the subquery's body with `SELECT p2.id FROM
    "post" as p2` inside the same `if` -- every post becomes "read" once
    hide_read_posts is set.
    """

    def test_a_read_post_is_absent_when_hide_read_posts_is_set(self, app, db_session, redis_double):
        make_instance('readhide.example')
        viewer = make_user(None, 'readhideviewer', local=True)
        author = make_user(None, 'readhideauthor', local=True)
        community = make_community('readhidecomm')
        post = make_post(community, author, 'https://readhide.example/posts/1')
        viewer.hide_read_posts = True
        # hide_nsfw/hide_nsfl default to 1 (on) for a fresh user -- turned off
        # here so this test isn't incidentally gated by them too, and so the
        # file exercises their own preference-off branch somewhere.
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 0
        db.session.commit()
        mark_post_read(viewer, post)

        ids = feed_ids(app, viewer, [community.id])

        assert post.id not in ids

    def test_an_unread_post_is_present_when_hide_read_posts_is_set(self, app, db_session, redis_double):
        """Same viewer preference, same community; only the mark_post_read
        call is omitted. Proves the filter targets READ posts rather than
        hiding everything once hide_read_posts is set.
        """
        make_instance('readokhome.example')
        viewer = make_user(None, 'readokviewer', local=True)
        author = make_user(None, 'readokauthor', local=True)
        community = make_community('readokcomm')
        post = make_post(community, author, 'https://readokhome.example/posts/1')
        viewer.hide_read_posts = True
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 0
        db.session.commit()
        # no mark_post_read call -- post stays unread

        ids = feed_ids(app, viewer, [community.id])

        assert post.id in ids


class TestHideGenAi:
    """app/utils.py:3888-3889: `if current_user.hide_gen_ai == 1:
    post_id_where.append('p.ai_generated is false ')`. hide_gen_ai's column
    default is 2 ("label"), not 1 ("hide") -- 0=show, 1=hide, 2=label,
    3=semi-transparent (app/models.py:986) -- so both tests below set it
    explicitly rather than relying on a fresh user's default. Mutations
    mirror TestHideNsfw.
    """

    def test_an_ai_generated_post_is_absent_when_hide_gen_ai_is_set(self, app, db_session, redis_double):
        make_instance('genaihide.example')
        viewer = make_user(None, 'genaihideviewer', local=True)
        author = make_user(None, 'genaihideauthor', local=True)
        community = make_community('genaihidecomm')
        post = make_post(community, author, 'https://genaihide.example/posts/1')
        post.ai_generated = True
        viewer.hide_gen_ai = 1
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 0
        db.session.commit()

        ids = feed_ids(app, viewer, [community.id])

        assert post.id not in ids

    def test_a_non_ai_generated_post_is_present_when_hide_gen_ai_is_set(self, app, db_session, redis_double):
        make_instance('genaiokhome.example')
        viewer = make_user(None, 'genaiokviewer', local=True)
        author = make_user(None, 'genaiokauthor', local=True)
        community = make_community('genaiokcomm')
        post = make_post(community, author, 'https://genaiokhome.example/posts/1')
        viewer.hide_gen_ai = 1
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 0
        db.session.commit()

        ids = feed_ids(app, viewer, [community.id])

        assert post.id in ids


class TestIgnoreBots:
    """app/utils.py:3880-3881: `if current_user.ignore_bots == 1:
    post_id_where.append('p.from_bot is false ')`. This is the brief's named
    Step-3 discrimination target. Mutations mirror TestHideNsfw. Measured
    counts are in task-5-report.md.
    """

    def test_a_bots_post_is_absent_when_ignore_bots_is_set(self, app, db_session, redis_double):
        make_instance('botshide.example')
        viewer = make_user(None, 'botshideviewer', local=True)
        author = make_user(None, 'botshideauthor', local=True)
        community = make_community('botshidecomm')
        post = make_post(community, author, 'https://botshide.example/posts/1')
        post.from_bot = True
        viewer.ignore_bots = 1
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 0
        db.session.commit()

        ids = feed_ids(app, viewer, [community.id])

        assert post.id not in ids

    def test_a_non_bots_post_is_present_when_ignore_bots_is_set(self, app, db_session, redis_double):
        make_instance('botsokhome.example')
        viewer = make_user(None, 'botsokviewer', local=True)
        author = make_user(None, 'botsokauthor', local=True)
        community = make_community('botsokcomm')
        post = make_post(community, author, 'https://botsokhome.example/posts/1')
        viewer.ignore_bots = 1
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 0
        db.session.commit()

        ids = feed_ids(app, viewer, [community.id])

        assert post.id in ids


class TestReadLanguageIds:
    """app/utils.py:3894-3896: `if current_user.read_language_ids and
    len(current_user.read_language_ids) > 0: post_id_where.append('(p.
    language_id IN :read_language_ids OR p.language_id is null) ')`. List-
    valued, and inclusive rather than exclusive -- a post whose language
    the viewer did NOT opt into is hidden, but a post with NO language set
    is always shown. Mutation that fails only the absence test: deleting
    the `if` block. Mutation that fails only the presence test: replacing
    the appended fragment with `1=0 ` inside the same `if` -- every post
    would be excluded once read_language_ids is non-empty, including one
    whose language IS in the list.

    `test_a_posts_null_language_is_present` below covers the `OR
    p.language_id is null` escape hatch specifically. That term is a SQL-level
    alternative living inside a query STRING, not a Python branch --
    coverage.py's statement/branch measurement cannot see it, and the other
    two tests in this class never exercise it (their posts always carry a
    language_id). Without it, deleting `OR p.language_id is null` from
    app/utils.py:3895 would go completely unnoticed by this file despite its
    100% statement-and-branch reading. Real production impact
    if it silently broke: any post with no language set would vanish from
    the feed of every user who has chosen specific languages, even though
    "no language" is not "a language I didn't choose".
    """

    def test_a_posts_unselected_language_is_absent(self, app, db_session, redis_double):
        make_instance('langhide.example')
        viewer = make_user(None, 'langhideviewer', local=True)
        author = make_user(None, 'langhideauthor', local=True)
        community = make_community('langhidecomm')
        post = make_post(community, author, 'https://langhide.example/posts/1')
        wanted = Language(code='en', name='English')
        other = Language(code='de', name='German')
        db.session.add_all([wanted, other])
        db.session.commit()
        post.language_id = other.id
        viewer.read_language_ids = [wanted.id]
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 0
        db.session.commit()

        ids = feed_ids(app, viewer, [community.id])

        assert post.id not in ids

    def test_a_posts_selected_language_is_present(self, app, db_session, redis_double):
        """Same viewer, same community, same shape; only post.language_id
        flips from the unselected language to the selected one. Proves the
        filter admits the language the viewer opted into rather than
        excluding everything once read_language_ids is set. No unselected
        `other` Language is needed here -- unlike the absence test, this one
        never has to demonstrate exclusion.
        """
        make_instance('langokhome.example')
        viewer = make_user(None, 'langokviewer', local=True)
        author = make_user(None, 'langokauthor', local=True)
        community = make_community('langokcomm')
        post = make_post(community, author, 'https://langokhome.example/posts/1')
        wanted = Language(code='en', name='English')
        db.session.add(wanted)
        db.session.commit()
        post.language_id = wanted.id
        viewer.read_language_ids = [wanted.id]
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 0
        db.session.commit()

        ids = feed_ids(app, viewer, [community.id])

        assert post.id in ids

    def test_a_posts_null_language_is_present(self, app, db_session, redis_double):
        """A post with NO language set (post.language_id left at make_post's
        default, NULL) is present even though the viewer's read_language_ids
        does not contain (and cannot contain -- it's None) anything matching
        it. This is the `OR p.language_id is null` half of the clause, not
        the `IN :read_language_ids` half the other two tests in this class
        exercise -- the post's language is neither selected nor unselected,
        it's simply unknown, and the filter's job is to let unknowns through
        rather than hide them by default.

        Mutation that fails only this test: deleting `OR p.language_id is
        null` from app/utils.py:3895, leaving `p.language_id IN
        :read_language_ids` as the sole predicate -- a NULL language_id can
        never satisfy an IN list, so the post would vanish.
        """
        make_instance('langnullhome.example')
        viewer = make_user(None, 'langnullviewer', local=True)
        author = make_user(None, 'langnullauthor', local=True)
        community = make_community('langnullcomm')
        post = make_post(community, author, 'https://langnullhome.example/posts/1')
        unrelated = Language(code='fr', name='French')
        db.session.add(unrelated)
        db.session.commit()
        # post.language_id stays unset (NULL) -- never assigned
        viewer.read_language_ids = [unrelated.id]
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 0
        db.session.commit()

        ids = feed_ids(app, viewer, [community.id])

        assert post.id in ids


class TestHideLowQuality:
    """app/utils.py:3836-3837: `if current_user.is_authenticated and
    current_user.hide_low_quality and community_ids[0] == -1:
    post_id_where.append('c.low_quality is false')`. This is the one
    preference keyed off the COMMUNITY's own flag, not the post's, and it
    only applies on the "all communities" listing (community_ids == [-1]) --
    both tests below query with that, never a specific community id.
    Mutation that fails only the absence test: deleting the `if` block (all
    three of its conditions). Mutation that fails only the presence test:
    replacing the appended fragment with `1=0` inside the same `if` -- every
    community's posts would be excluded from the all-communities listing
    once hide_low_quality is set, low-quality or not.
    """

    def test_a_low_quality_communitys_post_is_absent_when_hide_low_quality_is_set(
            self, app, db_session, redis_double):
        make_instance('lowqhide.example')
        viewer = make_user(None, 'lowqhideviewer', local=True)
        author = make_user(None, 'lowqhideauthor', local=True)
        community = make_community('lowqhidecomm')
        community.show_all = True
        community.low_quality = True
        db.session.commit()
        post = make_post(community, author, 'https://lowqhide.example/posts/1')
        viewer.hide_low_quality = True
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 0
        db.session.commit()

        ids = feed_ids(app, viewer, [-1])

        assert post.id not in ids

    def test_a_normal_communitys_post_is_present_when_hide_low_quality_is_set(
            self, app, db_session, redis_double):
        make_instance('lowqokhome.example')
        viewer = make_user(None, 'lowqokviewer', local=True)
        author = make_user(None, 'lowqokauthor', local=True)
        community = make_community('lowqokcomm')
        community.show_all = True
        db.session.commit()
        post = make_post(community, author, 'https://lowqokhome.example/posts/1')
        viewer.hide_low_quality = True
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 0
        db.session.commit()

        ids = feed_ids(app, viewer, [-1])

        assert post.id in ids


class TestAnonymousContentWarningBranch:
    """app/utils.py:3874-3865. Anonymous viewers take a config-dependent
    branch instead of any per-user preference:

        if current_app.config['CONTENT_WARNING']:
            ... from_bot is false AND nsfl is false AND deleted is false AND status > 0
        else:
            ... from_bot is false AND nsfw is false AND nsfl is false AND deleted is false AND status > 0

    The two arms differ by exactly one predicate: `nsfw is false`. Both
    tests below restore `current_app.config['CONTENT_WARNING']` in a
    `finally`, since it is process-global config, not a per-request value.
    Mutation that fails only the WITH-warning test: adding `p.nsfw is
    false ` back into the True arm (collapsing the two arms). Mutation that
    fails only the WITHOUT-warning test: deleting `AND p.nsfw is false` from
    the False arm.
    """

    def test_an_nsfw_post_is_present_to_anonymous_users_when_content_warning_is_configured(
            self, app, db_session, redis_double):
        make_instance('cwonhome.example')
        author = make_user(None, 'cwonauthor', local=True)
        community = make_community('cwoncomm')
        post = make_post(community, author, 'https://cwonhome.example/posts/1')
        post.nsfw = True
        db.session.commit()

        original = app.config['CONTENT_WARNING']
        app.config['CONTENT_WARNING'] = 1
        try:
            ids = anon_feed_ids(app, [community.id])
        finally:
            app.config['CONTENT_WARNING'] = original

        assert post.id in ids

    def test_an_nsfw_post_is_absent_to_anonymous_users_when_content_warning_is_not_configured(
            self, app, db_session, redis_double):
        make_instance('cwoffhome.example')
        author = make_user(None, 'cwoffauthor', local=True)
        community = make_community('cwoffcomm')
        post = make_post(community, author, 'https://cwoffhome.example/posts/1')
        post.nsfw = True
        db.session.commit()

        original = app.config['CONTENT_WARNING']
        app.config['CONTENT_WARNING'] = 0
        try:
            ids = anon_feed_ids(app, [community.id])
        finally:
            app.config['CONTENT_WARNING'] = original

        assert post.id not in ids
