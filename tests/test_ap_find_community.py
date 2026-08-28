"""find_community decides which Community an incoming activity belongs to, by
three strategies checked in order:

1. Addressing -- 'audience', 'cc', 'to', 'target', checked on the outer
   activity first and then the inner 'object', matching a value (string or
   list element) against Community.ap_profile_id. Two values are excluded
   from ever being looked up: anything starting 'https://www.w3.org' (the
   Public collection) and anything ending '/followers'.
2. inReplyTo -- resolve the parent Post, else the parent PostReply, and
   return its community.
3. PeerTube Video -- 'attributedTo' as a list, matching either a bare string
   or a dict with type == 'Group'.

The function returns on the first hit; nothing later fires once something
earlier has matched. DB-backed, no network.

Every Community factory call here is preceded by seed_community_owner(),
which builds the Instance and the local User in that order: make_community
hardcodes instance_id=1 and user_id=1, so both foreign keys must already
exist, and the Instance must precede the User -- the ordering tests/README.md
and the task context both call out.

Branch enumeration, derived fresh against this checkout (not carried
forward):

    podman-compose -f compose.test.yaml exec -T -w /app test-runner python -c "
    import ast
    src = open('app/activitypub/util.py').read()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.FunctionDef) and n.name == 'find_community':
            print('span', n.lineno, n.end_lineno)
            for s in ast.walk(n):
                if isinstance(s, ast.If):
                    print('  If at', s.lineno, 'orelse=', len(s.orelse))
    "

Output: span covers 17 `if` statements -- 4 for the addressing loop's
structure (object-is-dict check, location-present check, string-shape check,
list-shape check) times roughly the string/list branches each carrying their
own exclusion-and-match pair, 2 for the inReplyTo strategy (presence check,
Post-found check, with a 2-branch else for the PostReply fallback), and
3 for the Video strategy (type check, attributedTo-is-list check, per-item
str-vs-Group dict check). Every one of them is exercised below.

Compound `and` conditions, and checks repeated in two code locations, need a
test on EACH operand and EACH location -- coverage.py branch coverage is
satisfied once both outcomes of the whole expression are observed, so a
single test can report full branch coverage while leaving one operand (or
one of two textually-duplicated copies of the same check) never
independently falsified. This file was reviewed and found short on that
twice: the Public-collection and `/followers` exclusions each appear once in
the addressing loop's string branch and once in its list branch, and the
original test set covered only one branch per exclusion (Public via string,
`/followers` via list) -- proved live by the reviewer, who mutated the
string branch's `/followers` check alone and got 19/19 still passing. Both
gaps are closed below, each exclusion now tested via both branches. The same
sweep found two more real two-operand gaps and closed them: the inReplyTo
guard's `is not None` operand (`'inReplyTo' in rj and rj['inReplyTo'] is not
None`) and the Video guard's `isinstance(..., list)` operand (`'attributedTo'
in rj and isinstance(rj['attributedTo'], list)`) were each previously
exercised on only one operand. One further instance of the same SHAPE exists
-- `'object' in request_json and isinstance(request_json['object'], dict)`
-- and was deliberately NOT closed with a test: a non-dict 'object' value
also reaches the unguarded `rj = request_json['object'] if 'object' in
request_json else request_json` reassignment a few lines later, which has no
isinstance guard of its own, so any input built to isolate THIS operand
crashes there regardless of what the addressing loop's own guard does --
there is no clean input that discriminates this operand in isolation without
first resolving that separate, unguarded access. Reported, not fixed, and
not synthesized into a misleading test.
"""
from app import db
from app.activitypub.util import find_community
from tests.factories import (make_community, make_post, make_post_reply, make_user,
                            seed_community_owner)


class TestAddressingStrategy:
    """Mutation that fails these: neutralizing the addressing loop (e.g.
    `for rj in rjs:` iterating nothing) -- every test in this class expects a
    Community that only the addressing strategy can supply.
    """

    def test_audience_string_matching_a_community_is_found(self, app, db_session):
        instance = seed_community_owner()
        community = make_community('audiencematch')
        assert find_community({'audience': community.ap_profile_id}) == community

    def test_cc_list_containing_a_matching_id_is_found(self, app, db_session):
        seed_community_owner()
        community = make_community('ccmatch')
        result = find_community({'cc': ['https://peer.example/u/someone', community.ap_profile_id]})
        assert result == community

    def test_outer_has_no_addressing_but_object_does(self, app, db_session):
        seed_community_owner()
        community = make_community('innermatch')
        result = find_community({'type': 'Create', 'object': {'to': community.ap_profile_id}})
        assert result == community

    def test_outer_wins_over_inner_when_they_name_different_communities(self, app, db_session):
        """The addressing loop checks the outer activity before the inner
        object and returns the first hit -- this is behaviour, not incident.
        Mutation that fails this: swapping the outer-then-inner order (e.g.
        building rjs as [request_json['object'], request_json] instead)."""
        seed_community_owner()
        outer_community = make_community('outerwins')
        inner_community = make_community('innerloses')
        result = find_community({
            'to': outer_community.ap_profile_id,
            'object': {'to': inner_community.ap_profile_id},
        })
        assert result == outer_community

    def test_audience_string_matching_nothing_falls_through_to_the_next_strategy(self, app, db_session):
        """Mutation that fails this: dropping the `if potential_community:`
        guard around the string-match return, so the function returns
        whatever the (possibly None) query produced instead of continuing on
        to inReplyTo. That mutation is wide -- see the task report's mutation
        section -- because it also forecloses the Video strategy for every
        input whose first present addressing location fails to match."""
        instance = seed_community_owner()
        owner = make_user(instance, 'replyauthor')
        community = make_community('replytarget')
        post = make_post(community, owner, ap_id='https://peer.example/post/1')
        result = find_community({
            'audience': 'https://peer.example/c/doesnotexist',
            'inReplyTo': post.ap_id,
        })
        assert result == community

    def test_a_non_matching_addressing_value_does_not_prevent_a_video_match(self, app, db_session):
        """Review finding 2: the fallthrough test above proves the
        addressing-over-broaden mutation is wide by demonstrating the
        inReplyTo half of the short-circuit; this is the Video half,
        combining a non-matching addressing value with a Video fixture that
        would otherwise match. Mutation that fails this: the same
        `if potential_community:` deletion as the test above."""
        seed_community_owner()
        community = make_community('videoafteraudiencemiss')
        result = find_community({
            'audience': 'https://peer.example/c/doesnotexist',
            'type': 'Video',
            'attributedTo': [community.ap_profile_id],
        })
        assert result == community

    def test_the_public_collection_is_never_looked_up_via_the_string_branch(self, app, db_session):
        """Constructs the case that actually discriminates the exclusion: a
        Community whose ap_profile_id happens to equal the Public collection
        URI. Without the `not potential_id.startswith('https://www.w3.org')`
        guard, this would be found by an ordinary ap_profile_id lookup --
        the exclusion is what prevents the query being attempted at all.
        Mutation that fails this: deleting that guard. Exercises the STRING
        branch's copy of the exclusion (`potential_id.startswith`) -- see the
        list-branch counterpart below. Review finding 1: these two branches
        each check the same two conditions independently, so a mutation to
        either copy alone needs its own branch's test to catch it."""
        seed_community_owner()
        community = make_community('publiclookalike')
        community.ap_profile_id = 'https://www.w3.org/ns/activitystreams#Public'
        db.session.commit()
        assert find_community({'type': 'Note', 'to': 'https://www.w3.org/ns/activitystreams#Public'}) is None

    def test_the_public_collection_is_never_looked_up_via_the_list_branch(self, app, db_session):
        """The list-branch counterpart of the test above (`c.startswith`
        rather than `potential_id.startswith`) -- review finding 1. Before
        this test existed, a mutation deleting only the list branch's
        Public exclusion survived undetected, because the string-branch test
        above never reaches the list branch's code at all."""
        seed_community_owner()
        community = make_community('publiclistlookalike')
        community.ap_profile_id = 'https://www.w3.org/ns/activitystreams#Public'
        db.session.commit()
        assert find_community({'type': 'Note', 'cc': ['https://www.w3.org/ns/activitystreams#Public']}) is None

    def test_a_url_ending_followers_is_never_looked_up_via_the_list_branch(self, app, db_session):
        """Same technique as the Public case, for the `/followers` exclusion:
        a Community whose ap_profile_id itself ends '/followers'. Mutation
        that fails this: deleting the `not c.endswith('/followers')` guard.
        Exercises the LIST branch's copy -- see the string-branch counterpart
        below. Review finding 1: the reviewer proved that mutating the
        STRING branch's `/followers` exclusion alone, with only this
        list-branch test in place, survived all 19 prior tests -- this test
        alone was never going to catch a mutation in a branch it never
        reaches."""
        seed_community_owner()
        community = make_community('followerslookalike')
        community.ap_profile_id = 'https://peer.example/c/followerslookalike/followers'
        db.session.commit()
        assert find_community({'type': 'Note', 'cc': [community.ap_profile_id]}) is None

    def test_a_url_ending_followers_is_never_looked_up_via_the_string_branch(self, app, db_session):
        """The string-branch counterpart -- review finding 1's second
        missing case. Mutation that fails this: deleting the
        `not potential_id.endswith('/followers')` guard in the STRING
        branch specifically, which the list-branch test above cannot catch."""
        seed_community_owner()
        community = make_community('followersstringlookalike')
        community.ap_profile_id = 'https://peer.example/c/followersstringlookalike/followers'
        db.session.commit()
        assert find_community({'type': 'Note', 'to': community.ap_profile_id}) is None

    def test_in_reply_to_present_but_explicitly_none_is_not_treated_as_a_lookup_key(self, app, db_session):
        """Same-shape audit (review finding 1's sweep): the inReplyTo guard,
        `'inReplyTo' in rj and rj['inReplyTo'] is not None`, also has two
        operands, and no earlier test exercised the second one independently
        -- every earlier test either omitted 'inReplyTo' entirely or gave it
        a real string. A Post exists here with ap_id literally None (the
        column carries no NOT NULL constraint); if the `is not None` operand
        were dropped, `Post.get_by_ap_id(None)` would spuriously match it
        instead of falling through to the Video match also present here, so
        the assertion is on WHICH community comes back, not merely that one
        is found."""
        instance = seed_community_owner()
        owner = make_user(instance, 'nonemarker')
        wrong_community = make_community('wrongvianone')
        decoy_post = make_post(wrong_community, owner, ap_id='https://peer.example/post/decoy')
        decoy_post.ap_id = None
        db.session.commit()
        right_community = make_community('rightvianone')
        result = find_community({
            'type': 'Video',
            'inReplyTo': None,
            'attributedTo': [right_community.ap_profile_id],
        })
        assert result == right_community


class TestInReplyToStrategy:
    """Mutation that fails these: neutralizing the `if 'inReplyTo' in rj and
    rj['inReplyTo'] is not None:` guard (e.g. `if False:`)."""

    def test_in_reply_to_naming_a_known_post_returns_its_community(self, app, db_session):
        instance = seed_community_owner()
        author = make_user(instance, 'postauthor')
        community = make_community('postcommunity')
        post = make_post(community, author, ap_id='https://peer.example/post/2')
        assert find_community({'inReplyTo': post.ap_id}) == community

    def test_in_reply_to_naming_a_known_post_reply_returns_its_community(self, app, db_session):
        """No Post has this ap_id, so the Post lookup misses and the function
        falls to PostReply -- proving the fallback fires, not just that a
        reply row with a community exists."""
        instance = seed_community_owner()
        author = make_user(instance, 'replyauthor2')
        community = make_community('replycommunity')
        post = make_post(community, author, ap_id='https://peer.example/post/3')
        reply = make_post_reply(post, author)
        reply.ap_id = 'https://peer.example/comment/1'
        db.session.commit()
        assert find_community({'inReplyTo': reply.ap_id}) == community

    def test_in_reply_to_naming_nothing_known_returns_none(self, app, db_session):
        assert find_community({'type': 'Note', 'inReplyTo': 'https://peer.example/post/unknown'}) is None


class TestPeerTubeVideoStrategy:
    def test_attributed_to_a_group_dict_returns_its_community(self, app, db_session):
        seed_community_owner()
        community = make_community('videogroupcommunity')
        result = find_community({
            'type': 'Video',
            'attributedTo': [
                {'type': 'Person', 'id': 'https://peer.example/u/creator'},
                {'type': 'Group', 'id': community.ap_profile_id},
            ],
        })
        assert result == community

    def test_attributed_to_a_bare_string_returns_its_community(self, app, db_session):
        seed_community_owner()
        community = make_community('videostringcommunity')
        result = find_community({'type': 'Video', 'attributedTo': [community.ap_profile_id]})
        assert result == community

    def test_a_non_matching_string_in_attributed_to_continues_the_loop_to_none(self, app, db_session):
        """A string item that doesn't match falls through the `if
        potential_community:` guard back into the loop (and, here, out the
        bottom of it) rather than returning -- distinct from the match case
        above, which never exercises that continuation."""
        result = find_community({'type': 'Video', 'attributedTo': ['https://peer.example/u/nonmatch']})
        assert result is None

    def test_a_non_matching_group_dict_in_attributed_to_continues_the_loop_to_none(self, app, db_session):
        result = find_community({
            'type': 'Video',
            'attributedTo': [{'type': 'Group', 'id': 'https://peer.example/c/nonexistent'}],
        })
        assert result is None

    def test_video_with_no_attributed_to_key_returns_none(self, app, db_session):
        assert find_community({'type': 'Video'}) is None

    def test_video_with_an_empty_attributed_to_list_returns_none(self, app, db_session):
        assert find_community({'type': 'Video', 'attributedTo': []}) is None

    def test_video_with_a_non_list_attributed_to_value_returns_none(self, app, db_session):
        """Same-shape audit (review finding 1's sweep): the Video guard,
        `'attributedTo' in rj and isinstance(rj['attributedTo'], list)`, also
        has two operands. `None` exercises the second independently of the
        first: `'attributedTo' in rj` is True, but `for a in
        rj['attributedTo']:` cannot iterate a None without the isinstance
        guard (TypeError) -- current code's guard is what makes this return
        cleanly instead of crashing."""
        assert find_community({'type': 'Video', 'attributedTo': None}) is None


class TestCaseInsensitiveActorMatching:
    """Every lookup in this function normalises the peer's URI before
    querying, because `ap_profile_id` is stored lower-cased.

    There are four such call sites, one per lookup. Derived against this
    checkout with:

        podman-compose -f compose.test.yaml exec -T -w /app test-runner python -c "
        import ast
        src = open('app/activitypub/util.py').read()
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, ast.FunctionDef) and n.name == 'find_community':
                for c in ast.walk(n):
                    if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                            and c.func.attr == 'lower'):
                        print(ast.unparse(c))
        "

    Output, four lines:

        potential_id.lower()   -- addressing loop, string branch
        c.lower()              -- addressing loop, list branch
        a.lower()              -- Video attributedTo, bare-string item
        a['id'].lower()        -- Video attributedTo, Group-dict item

    Coverage cannot see any of them. Each sits inside a `filter_by(...)`
    argument on a line that every existing test in this file already
    executes, so the whole function reported 100% statement and branch
    coverage while all four could be deleted with the full suite still
    green -- proved live by the reviewer. Nothing tested a mixed-case URI,
    so case-insensitive matching was asserted nowhere.

    Each test below feeds a mixed-case URI to exactly one of the four sites
    and asserts the stored Community comes back. `.upper()` on the whole
    URI would not do: 'HTTPS://...' is not what a peer sends, and the point
    is a document a real peer could serve. The host and the final path
    segment are the two parts a peer varies in practice.

    Only the four addressing and Video lookups are covered here. The
    inReplyTo strategy has no `.lower()` of its own -- it hands the URI to
    Post.get_by_ap_id / PostReply.get_by_ap_id unmodified -- so there is no
    fifth site, and this class does not invent a test for one.
    """

    def test_a_mixed_case_audience_string_still_matches_the_stored_community(self, app, db_session):
        """The addressing loop's STRING branch. Production change that fails
        this: deleting `potential_id.lower()`, after which the query looks up
        the peer's mixed-case URI verbatim, misses the lower-cased stored
        row, and the function falls through to None."""
        seed_community_owner()
        community = make_community('stringcasematch')
        result = find_community({'audience': 'https://Test.PieFed.Local/c/STRINGCASEMATCH'})
        assert result == community

    def test_a_mixed_case_cc_list_entry_still_matches_the_stored_community(self, app, db_session):
        """The addressing loop's LIST branch, whose `c.lower()` is a
        textually separate call site from the string branch's: a mutation to
        either one alone is invisible to the other's test, the same reason
        this file already carries paired tests for the Public-collection and
        `/followers` exclusions."""
        seed_community_owner()
        community = make_community('listcasematch')
        result = find_community({'cc': ['https://Test.PieFed.Local/c/LISTCASEMATCH']})
        assert result == community

    def test_a_mixed_case_bare_string_in_attributed_to_still_matches(self, app, db_session):
        """The Video strategy's bare-string item, `a.lower()`. No addressing
        key is present, so the addressing loop cannot reach a match first
        and the assertion can only be satisfied by this site."""
        seed_community_owner()
        community = make_community('videostringcasematch')
        result = find_community({
            'type': 'Video',
            'attributedTo': ['https://Test.PieFed.Local/c/VIDEOSTRINGCASEMATCH'],
        })
        assert result == community

    def test_a_mixed_case_group_dict_id_in_attributed_to_still_matches(self, app, db_session):
        """The Video strategy's Group-dict item, `a['id'].lower()` -- the
        fourth and last site. The list carries a Person entry first so the
        dict arm is reached through the `elif a['type'] == 'Group'` rather
        than being the only thing in the list."""
        seed_community_owner()
        community = make_community('videodictcasematch')
        result = find_community({
            'type': 'Video',
            'attributedTo': [
                {'type': 'Person', 'id': 'https://peer.example/u/creator'},
                {'type': 'Group', 'id': 'https://Test.PieFed.Local/c/VIDEODICTCASEMATCH'},
            ],
        })
        assert result == community


class TestNothingMatchesAnywhere:
    def test_an_activity_with_no_addressing_no_reply_and_no_video_match_returns_none(self, app, db_session):
        result = find_community({
            'type': 'Create',
            'to': 'https://peer.example/c/unrelated',
            'object': {'type': 'Note', 'cc': ['https://peer.example/u/nobody']},
        })
        assert result is None


class TestNonStringAddressingElementIsSkipped:
    """Was `TestSuspectedNonStringAddressingElementCrash`: the tests here
    used to pin the AttributeError this function raised. That defect is
    fixed -- a non-string element in the list branch is now skipped like any
    other non-matching entry, instead of crashing on `.startswith`.

    The list branch of the addressing loop calls `c.startswith(...)` /
    `c.endswith(...)` on every element of a 'cc'/'to'/'audience'/'target'
    list. Only the list ITSELF was type-checked (`isinstance(potential_id,
    list)`), never its members, so a non-string element (e.g. a dict, which
    the Activity Streams vocabulary permits in these fields for an embedded
    object) used to raise AttributeError. The guard now checks
    `isinstance(c, str)` per element before calling either string method,
    mirroring the isinstance check already used one branch up for
    `potential_id` itself.

    Call-site analysis: every caller of find_community passes JSON that
    reaches it without any upstream validation of the addressing lists'
    element types -- app/activitypub/routes.py's Create/Update/Add/Remove
    handling (the `find_community(request_json)` and
    `find_community(core_activity)` call sites inside
    `process_inbox_request`) all pass a directly-inbound peer activity
    verbatim. This IS reachable by an untrusted remote peer's inbox POST.
    `process_inbox_request` wraps its whole body in `except Exception:
    session.rollback(); raise`, so before this fix the crash rolled back
    cleanly and propagated out of the Celery task rather than corrupting
    state or crashing the worker process -- but since `process_inbox_request`
    is invoked with `.delay()`, the HTTP inbox response had already gone out;
    the crash just meant that activity's processing silently failed with no
    retry, not an authentication or authorization bypass.
    """

    def test_a_non_string_element_in_a_cc_list_is_skipped_and_returns_none(self, app, db_session):
        assert find_community({'cc': [{'type': 'Person', 'id': 'https://peer.example/u/mallory'}]}) is None


class TestMissingTypeKeyReturnsNone:
    """Was `TestSuspectedMissingTypeKeyCrash`: the test here used to pin the
    KeyError this function raised. That defect is fixed -- an object with no
    'type' key is now treated as not-a-Video and returns None, instead of
    crashing.

    Once both the addressing and inReplyTo strategies miss, the function
    checks for a PeerTube Video via `rj.get('type') == 'Video'` -- previously
    an unconditional `rj['type']` read, with no 'type' in rj guard, which
    raised KeyError on an object with no 'type' key.

    Call-site analysis: app/activitypub/routes.py's 'Add' and 'Remove'
    handling calls `find_community(core_activity)` directly. Only
    `core_activity['type']` (the OUTER activity's type, 'Add' or 'Remove') is
    validated before that call -- nothing checks that
    `core_activity['object']` itself carries a 'type' key, and find_community
    resolves its working `rj` to that inner object whenever 'object' is
    present. A peer sending an Add/Remove whose object has no 'type' used to
    reach this KeyError. Separately, `resolve_remote_post_from_search` in
    app/activitypub/util.py (triggered from the UI's 'search' / 'Retrieve a
    post from the original server' action) calls `find_community(post_data)`
    where post_data is the raw JSON fetched from whatever URI was searched --
    also with no 'type' guard before that call. Both are genuinely reachable
    by peer-controlled content; like the list-element case above, the
    practical effect was that one activity's processing failed rather than
    any bypass.
    """

    def test_an_object_with_no_type_key_returns_none(self, app, db_session):
        assert find_community({'type': 'Add', 'object': {'id': 'https://peer.example/x'}}) is None
