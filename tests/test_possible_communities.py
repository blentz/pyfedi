r"""One-case-per-rule coverage for possible_communities (app/utils.py:4330-4362).

possible_communities builds a grouped community picker for the "new post"
community selector: three optional keys -- 'Moderating', 'Joined
communities', 'Others' -- each a list of (id, display_name) tuples, plus a
running `already_added` set so a community that qualifies for more than one
group is only listed once, under its highest-priority group (Moderating >
Joined > Others, in the order the three loops run).

Unlike get_instance_stickies (task 6), this function has no `continue`
chain -- the equivalent failure mode here is a chain of `if c.id not in
already_added` dedup guards plus three `if len(comms) > 0` omission guards
plus one display-name if/else, any of which could be deleted or
over-broadened while a 2-item fixture still reports full branch coverage.

Rule count, derived with the following command against this checkout
(cross-checked with plain grep on the same line range):

    python3 -c "
    import ast
    tree = ast.parse(open('app/utils.py').read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == 'possible_communities':
            ifs = [n.lineno for n in ast.walk(node) if isinstance(n, ast.If)]
            print('if statements:', ifs, 'count=', len(ifs))
            calls = [(n.lineno, n.func.attr) for n in ast.walk(node)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                     and n.func.attr in ('filter', 'join')]
            print('filter/join calls:', calls)
    "

Output: `if statements: [4340, 4347, 4360, 4337, 4344, 4353, 4354] count= 7`
and `filter/join calls: [(4350, 'filter'), (4350, 'join'), (4350, 'filter')]`
-- confirmed against ``sed -n '4330,4362p' app/utils.py | grep -n '^[ ]*if \|\.filter(\|\.join('``,
which lists the same seven `if` lines plus the two `.filter(` calls (one
before the join with one predicate, one after with two comma-joined
predicates -- SQLAlchemy ANDs positional filter args, so that single call
carries two independent predicates).

Seven Python-level rules, in execution order:
  1. :4337 -- `if c.id not in already_added` inside the Moderating loop
  2. :4340 -- `if len(comms) > 0` -- omit 'Moderating' when empty
  3. :4344 -- `if c.id not in already_added` inside the Joined loop
  4. :4347 -- `if len(comms) > 0` -- omit 'Joined communities' when empty
  5. :4353 -- `if c.id not in already_added` inside the Others loop
  6. :4354 -- `if c.ap_id is None` -- display_name branch (title vs title@ap_domain)
  7. :4360 -- `if len(comms) > 0` -- omit 'Others' when empty

Plus three predicates the Others query pushes into SQL, invisible to
coverage.py as Python branches and exercised only through the rows the
query returns:
  8. `Community.banned == False`
  9. `Instance.gone_forever == False`
  10. `Community.name != 'microblogs'`

None of these three is a compound `a or b`/`a and b` in the sense that
tripped Task 5 (an OR inside a SQL string) or Task 6 (`sort == '' or sort
== 'hot'`): they are AND-combined but each is independently falsifiable by
its own dedicated test below (turn one false while holding the other two
true), so there is no untested operand to split out.

current_user.is_anonymous / current_user.get_id(): possible_communities()
itself has no explicit branch on viewer kind -- there is no `if
current_user.is_anonymous` anywhere in its body, unlike get_instance_
stickies. The dependence is indirect and lives in the two functions it
calls: joined_communities(user_id) and moderating_communities(user_id)
both open with `if user_id is None or user_id == 0: return []`
(app/utils.py:2557-2559, 2618-2620). flask_login's default
AnonymousUserMixin.get_id() returns None (app/__init__.py configures no
custom anonymous_user), so an anonymous viewer gets both lists empty for
free and only the Others query -- which has no user/membership filter at
all -- can populate anything. That means rules 8-10 do not NEED an
authenticated viewer to isolate them, but the tests below (TestOthers
Exclusions) use one anyway -- an ordinary as_user() viewer with no
CommunityMember rows, which reaches Rules 8-10 exactly the same way an
anonymous one would, since an authenticated viewer with no memberships
also gets both lists empty. That was not a deliberate choice; it is
plain inconsistency with this docstring's original plan, most likely
from copying the make_user/login setup out of the TestGrouping methods
above rather than switching to anon(). The one test in this file that
actually uses anon() is test_no_eligible_communities_at_all_returns_an_
empty_dict, which needs it for a different reason -- to prove rule 7's
omission with NOTHING in the database rather than an Others-only viewer.
Rules 1-7 need an AUTHENTICATED viewer WITH the relevant membership rows,
since moderating_communities/joined_communities return [] for anyone
else.

A finding, not a defect: Step 1 of the brief asks for "a community both
moderated and joined appears ONCE, under Moderating." That literal state is
unreachable. CommunityMember's primary key is the (user_id, community_id)
pair (app/models.py:3367-3368) -- one row per user per community, at most
-- and moderating_communities' filter (is_moderator == True OR is_owner ==
True) and joined_communities' filter (is_moderator == False AND is_owner ==
False) partition that single row's boolean space with no overlap: a row
satisfying one can never satisfy the other. So the SAME user can never have
one community show up in both `moderating` and `joined` in the same call.
The dedup this task is actually built to prove -- and the one Step 4's
prescribed mutation targets -- is between a MODERATED (or JOINED) community
and the raw 'Others' query, which carries no membership filter at all and
would otherwise return that same community a second time. TestDedup below
tests exactly that overlap, once for Moderating and once for Joined, and
the moderating case is the one Step 4 mutates.

A second finding, from running the over-broaden direction on rule 1
(:4337): replacing `if c.id not in already_added:` with `if True:` inside
the MODERATING loop changes NOTHING -- 0 test failures -- because
`already_added` is a freshly-created empty set at that point
(app/utils.py:4335) and `moderating_communities()`'s query is a plain join
against a single user's CommunityMember rows, which cannot return the same
community twice. Rule 1's condition can therefore never be False in
practice; it is dead code as a within-loop duplicate filter. Its only
observable effect is on LATER loops, through the `.add()` calls it gates --
which is what TestDedup's moderating-vs-Others test, and Step 4's mutation
on the `.add()` call rather than the `if`, actually exercise.

Ordering: the Others query ends `.order_by(Community.title)`
(app/utils.py:4352). TestOrdering below seeds two Others-eligible
communities whose natural (insertion/id) order is the REVERSE of their
title order, so a removed ORDER BY would produce the wrong sequence rather
than coincidentally passing -- the trap Task 6 hit on its first attempt.

No test here takes redis_double: possible_communities(), joined_
communities() and moderating_communities() touch only Community,
CommunityMember and Instance tables -- confirmed by reading all three
functions and grepping app/utils.py for redis references near them.

Every mutation direction claimed in this file's class docstrings was run
against this file with `./run_tests.sh tests/test_possible_communities.py
-q`. Results are recorded in task-7-report.md.
"""
from flask_login import login_user

from app import db
from app.utils import possible_communities
from tests.factories import make_community, make_community_member, make_instance, make_user


def make_other_community(name, instance=None, banned=False, ap_id=None, ap_domain=None):
    """A Community reachable only through possible_communities' raw 'Others'
    query. Built from the shared factory (banned=False, ap_id=None by
    column default, instance_id=1) then adjusted for whichever column this
    particular case is about.
    """
    community = make_community(name)
    if instance is not None:
        community.instance_id = instance.id
    community.banned = banned
    if ap_id is not None:
        community.ap_id = ap_id
        community.ap_domain = ap_domain
    db.session.commit()
    return community


def as_user(app, viewer):
    """possible_communities() for an AUTHENTICATED viewer."""
    with app.test_request_context('/'):
        login_user(viewer)
        return possible_communities()


def anon(app):
    """possible_communities() for an ANONYMOUS viewer -- no login_user call,
    so current_user.get_id() is None and joined_communities/moderating_
    communities both short-circuit to [] (see module docstring).
    """
    with app.test_request_context('/'):
        return possible_communities()


def ids_in(group):
    return [entry[0] for entry in group]


def names_in(group):
    return [entry[1] for entry in group]


class TestGrouping:
    """Rules :4337/:4340 (Moderating), :4344/:4347 (Joined communities),
    :4353/:4360 (Others): each community lands in exactly the group its
    membership state predicts, and a group with nothing in it is omitted
    from the dict entirely rather than present as an empty list.
    """

    def test_a_moderated_community_appears_under_moderating(self, app, db_session):
        make_instance('modgroup.example')
        viewer = make_user(None, 'modgroupviewer', local=True)
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 0
        db.session.commit()
        community = make_community('modgroupcomm')
        make_community_member(viewer, community, is_moderator=True)

        result = as_user(app, viewer)

        assert community.id in ids_in(result['Moderating'])

    def test_a_joined_but_not_moderated_community_appears_under_joined_communities(self, app, db_session):
        make_instance('joingroup.example')
        viewer = make_user(None, 'joingroupviewer', local=True)
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 0
        db.session.commit()
        community = make_community('joingroupcomm')
        make_community_member(viewer, community, is_moderator=False)

        result = as_user(app, viewer)

        assert community.id in ids_in(result['Joined communities'])
        assert 'Moderating' not in result

    def test_an_unrelated_community_appears_under_others(self, app, db_session):
        make_instance('othergroup.example')
        viewer = make_user(None, 'othergroupviewer', local=True)
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 0
        db.session.commit()
        community = make_other_community('othergroupcomm')

        result = as_user(app, viewer)

        assert community.id in ids_in(result['Others'])
        assert 'Moderating' not in result
        assert 'Joined communities' not in result

    def test_an_empty_group_is_omitted_entirely(self, app, db_session):
        """The viewer moderates and joins nothing, but an Others-eligible
        community exists -- 'Moderating' and 'Joined communities' must be
        ABSENT keys, not present with an empty list.
        """
        make_instance('emptygroup.example')
        viewer = make_user(None, 'emptygroupviewer', local=True)
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 0
        db.session.commit()
        make_other_community('emptygroupcomm')

        result = as_user(app, viewer)

        assert 'Moderating' not in result
        assert 'Joined communities' not in result
        assert 'Others' in result

    def test_no_eligible_communities_at_all_returns_an_empty_dict(self, app, db_session):
        """Rule :4360's omission with nothing at all in the database --
        confirms the 'Others' key itself is also omitted, not just the
        first two groups.
        """
        result = anon(app)

        assert result == {}


class TestDedup:
    """already_added (app/utils.py:4335) prevents a community the viewer
    moderates or has joined from ALSO showing up in the raw 'Others' query,
    which carries no membership filter and would otherwise return every
    non-excluded community regardless of the viewer's relationship to it.

    Mutation performed (Step 4 of the task brief): delete
    `already_added.add(c.id)` from the MODERATING loop (app/utils.py:4339).
    Ran `./run_tests.sh tests/test_possible_communities.py -q`: exactly one
    failure, test_a_moderated_community_also_qualifies_for_others_but_
    appears_only_once below (the moderated community now appears a second
    time, under 'Others'). Restored immediately after. See task-7-report.md
    for the full pairing against the over-broaden direction on the same
    line's `if` (see module docstring's second finding: over-broadening
    that `if` to `True` changes nothing, because the condition can never be
    False there in the first place -- 0 failures, confirming rule 1 is dead
    as a within-loop filter and this dedup test is the only thing that
    actually depends on the `.add()` call it gates).
    """

    def test_a_moderated_community_also_qualifies_for_others_but_appears_only_once(self, app, db_session):
        make_instance('deduplicate.example')
        viewer = make_user(None, 'dedupmodviewer', local=True)
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 0
        db.session.commit()
        # Not banned, on an instance that is not gone_forever, name != 'microblogs':
        # this community independently satisfies every 'Others' predicate too.
        community = make_community('dedupmodcomm')
        make_community_member(viewer, community, is_moderator=True)

        result = as_user(app, viewer)

        assert ids_in(result['Moderating']) == [community.id]
        assert 'Others' not in result

    def test_a_joined_community_also_qualifies_for_others_but_appears_only_once(self, app, db_session):
        make_instance('deduplicatejoin.example')
        viewer = make_user(None, 'dedupjoinviewer', local=True)
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 0
        db.session.commit()
        community = make_community('dedupjoincomm')
        make_community_member(viewer, community, is_moderator=False)

        result = as_user(app, viewer)

        assert ids_in(result['Joined communities']) == [community.id]
        assert 'Others' not in result


class TestOthersExclusions:
    """The three predicates the Others query pushes into SQL
    (app/utils.py:4351-4352). One case per predicate, each with a
    plain control community present in the same call to prove the
    exclusion is selective rather than emptying the whole query.

    Mutation pairing run on the banned predicate (`Community.banned ==
    False`), reported in full in task-7-report.md:
      - delete: remove the `.filter(Community.banned == False)` clause --
        exactly 1 failure (test_a_banned_community_is_excluded_from_others).
      - over-broaden: change it to `.filter(Community.banned == True)`
        (excludes every ordinary, non-banned community) -- wide failure
        across every test in this file that asserts an Others-group
        community is present.
    """

    def test_a_banned_community_is_excluded_from_others(self, app, db_session):
        make_instance('bannedexcl.example')
        viewer = make_user(None, 'bannedexclviewer', local=True)
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 0
        db.session.commit()
        banned = make_other_community('bannedexclcomm', banned=True)
        control = make_other_community('bannedexclcontrol')

        result = as_user(app, viewer)

        assert banned.id not in ids_in(result['Others'])
        assert control.id in ids_in(result['Others'])

    def test_a_community_on_a_gone_forever_instance_is_excluded_from_others(self, app, db_session):
        make_instance('goneforeverexcl.example')
        viewer = make_user(None, 'goneforeverexclviewer', local=True)
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 0
        db.session.commit()
        dead_instance = make_instance('goneforeverdead.example')
        dead_instance.gone_forever = True
        db.session.commit()
        gone = make_other_community(
            'goneforeverexclcomm', instance=dead_instance, ap_id='https://goneforeverdead.example/c/goneforeverexclcomm',
            ap_domain='goneforeverdead.example')
        control = make_other_community('goneforeverexclcontrol')

        result = as_user(app, viewer)

        assert gone.id not in ids_in(result['Others'])
        assert control.id in ids_in(result['Others'])

    def test_the_local_microblogs_community_is_excluded_from_others(self, app, db_session):
        make_instance('localmicro.example')
        viewer = make_user(None, 'localmicroviewer', local=True)
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 0
        db.session.commit()
        microblogs = make_other_community('microblogs')
        control = make_other_community('localmicrocontrol')

        result = as_user(app, viewer)

        assert microblogs.id not in ids_in(result['Others'])
        assert control.id in ids_in(result['Others'])

    def test_a_remote_microblogs_community_is_also_excluded_from_others(self, app, db_session):
        """The exclusion is BY NAME, not by locality -- confirmed here with
        a remote 'microblogs' community (ap_id set, on a second instance)
        excluded exactly like the local one above.
        """
        make_instance('remotemicro.example')
        viewer = make_user(None, 'remotemicroviewer', local=True)
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 0
        db.session.commit()
        peer = make_instance('remotemicropeer.example')
        remote_microblogs = make_other_community(
            'microblogs', instance=peer, ap_id='https://remotemicropeer.example/c/microblogs',
            ap_domain='remotemicropeer.example')
        control = make_other_community('remotemicrocontrol')

        result = as_user(app, viewer)

        assert remote_microblogs.id not in ids_in(result['Others'])
        assert control.id in ids_in(result['Others'])


class TestDisplayNameBranch:
    """Rule :4354: `display_name = c.title` when `c.ap_id is None`, else
    `f"{c.title}@{c.ap_domain}"`. This branch is specific to the Others
    loop's hand-rolled version of Community.display_name() -- the query only
    selects (id, ap_id, title, ap_domain) columns, not a full Community
    object, so it cannot call the model method.
    """

    def test_a_local_communitys_display_name_is_its_title(self, app, db_session):
        make_instance('localdisplay.example')
        viewer = make_user(None, 'localdisplayviewer', local=True)
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 0
        db.session.commit()
        community = make_other_community('localdisplaycomm')

        result = as_user(app, viewer)

        entry = next(e for e in result['Others'] if e[0] == community.id)
        assert entry[1] == 'localdisplaycomm'

    def test_a_remote_communitys_display_name_is_title_at_ap_domain(self, app, db_session):
        make_instance('remotedisplay.example')
        viewer = make_user(None, 'remotedisplayviewer', local=True)
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 0
        db.session.commit()
        peer = make_instance('remotedisplaypeer.example')
        community = make_other_community(
            'remotedisplaycomm', instance=peer, ap_id='https://remotedisplaypeer.example/c/remotedisplaycomm',
            ap_domain='remotedisplaypeer.example')

        result = as_user(app, viewer)

        entry = next(e for e in result['Others'] if e[0] == community.id)
        assert entry[1] == 'remotedisplaycomm@remotedisplaypeer.example'


class TestOrdering:
    """The Others query ends `.order_by(Community.title)`
    (app/utils.py:4352). Fixture titles are chosen so insertion/id order is
    the REVERSE of title order -- a removed ORDER BY would return
    ['zzzorderlast', 'aaaorderfirst'] here, not coincidentally the right
    sequence.
    """

    def test_others_group_is_ordered_by_title_not_insertion_order(self, app, db_session):
        make_instance('orderexcl.example')
        viewer = make_user(None, 'orderexclviewer', local=True)
        viewer.hide_nsfw = 0
        viewer.hide_nsfl = 0
        db.session.commit()
        make_other_community('zzzorderlast')
        make_other_community('aaaorderfirst')

        result = as_user(app, viewer)

        assert names_in(result['Others']) == ['aaaorderfirst', 'zzzorderlast']
