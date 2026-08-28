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

Every Community factory call here is preceded by make_instance(...) and
make_user(None, ..., local=True): make_community hardcodes instance_id=1 and
user_id=1, so both foreign keys must already exist -- this is the ordering
tests/README.md and the task context both call out (make_instance before
make_user(None, ...)).

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
"""
import pytest

from app import db
from app.activitypub.util import find_community
from tests.factories import make_community, make_instance, make_post, make_post_reply, make_user


def _seed_owner_and_instance(domain='peer.example'):
    """Creates the Instance (id=1) and local User (id=1) that make_community's
    hardcoded instance_id=1 / user_id=1 columns require to exist first, and
    returns the Instance for tests that also need a remote actor on it.
    """
    instance = make_instance(domain)
    make_user(None, 'communityowner', local=True)
    return instance


class TestAddressingStrategy:
    """Mutation that fails these: neutralizing the addressing loop (e.g.
    `for rj in rjs:` iterating nothing) -- every test in this class expects a
    Community that only the addressing strategy can supply.
    """

    def test_audience_string_matching_a_community_is_found(self, app, db_session):
        instance = _seed_owner_and_instance()
        community = make_community('audiencematch')
        assert find_community({'audience': community.ap_profile_id}) == community

    def test_cc_list_containing_a_matching_id_is_found(self, app, db_session):
        _seed_owner_and_instance()
        community = make_community('ccmatch')
        result = find_community({'cc': ['https://peer.example/u/someone', community.ap_profile_id]})
        assert result == community

    def test_outer_has_no_addressing_but_object_does(self, app, db_session):
        _seed_owner_and_instance()
        community = make_community('innermatch')
        result = find_community({'type': 'Create', 'object': {'to': community.ap_profile_id}})
        assert result == community

    def test_outer_wins_over_inner_when_they_name_different_communities(self, app, db_session):
        """The addressing loop checks the outer activity before the inner
        object and returns the first hit -- this is behaviour, not incident.
        Mutation that fails this: swapping the outer-then-inner order (e.g.
        building rjs as [request_json['object'], request_json] instead)."""
        _seed_owner_and_instance()
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
        instance = _seed_owner_and_instance()
        owner = make_user(instance, 'replyauthor')
        community = make_community('replytarget')
        post = make_post(community, owner, ap_id='https://peer.example/post/1')
        result = find_community({
            'audience': 'https://peer.example/c/doesnotexist',
            'inReplyTo': post.ap_id,
        })
        assert result == community

    def test_the_public_collection_is_never_looked_up_even_if_it_would_match(self, app, db_session):
        """Constructs the case that actually discriminates the exclusion: a
        Community whose ap_profile_id happens to equal the Public collection
        URI. Without the `not potential_id.startswith('https://www.w3.org')`
        guard, this would be found by an ordinary ap_profile_id lookup --
        the exclusion is what prevents the query being attempted at all.
        Mutation that fails this: deleting that guard."""
        _seed_owner_and_instance()
        community = make_community('publiclookalike')
        community.ap_profile_id = 'https://www.w3.org/ns/activitystreams#Public'
        db.session.commit()
        assert find_community({'type': 'Note', 'to': 'https://www.w3.org/ns/activitystreams#Public'}) is None

    def test_a_url_ending_followers_is_never_looked_up_even_if_it_would_match(self, app, db_session):
        """Same technique as the Public case, for the `/followers` exclusion:
        a Community whose ap_profile_id itself ends '/followers'. Mutation
        that fails this: deleting the `not c.endswith('/followers')` guard."""
        _seed_owner_and_instance()
        community = make_community('followerslookalike')
        community.ap_profile_id = 'https://peer.example/c/followerslookalike/followers'
        db.session.commit()
        assert find_community({'type': 'Note', 'cc': [community.ap_profile_id]}) is None


class TestInReplyToStrategy:
    """Mutation that fails these: neutralizing the `if 'inReplyTo' in rj and
    rj['inReplyTo'] is not None:` guard (e.g. `if False:`)."""

    def test_in_reply_to_naming_a_known_post_returns_its_community(self, app, db_session):
        instance = _seed_owner_and_instance()
        author = make_user(instance, 'postauthor')
        community = make_community('postcommunity')
        post = make_post(community, author, ap_id='https://peer.example/post/2')
        assert find_community({'inReplyTo': post.ap_id}) == community

    def test_in_reply_to_naming_a_known_post_reply_returns_its_community(self, app, db_session):
        """No Post has this ap_id, so the Post lookup misses and the function
        falls to PostReply -- proving the fallback fires, not just that a
        reply row with a community exists."""
        instance = _seed_owner_and_instance()
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
        _seed_owner_and_instance()
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
        _seed_owner_and_instance()
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


class TestNothingMatchesAnywhere:
    def test_an_activity_with_no_addressing_no_reply_and_no_video_match_returns_none(self, app, db_session):
        result = find_community({
            'type': 'Create',
            'to': 'https://peer.example/c/unrelated',
            'object': {'type': 'Note', 'cc': ['https://peer.example/u/nobody']},
        })
        assert result is None


class TestSuspectedNonStringAddressingElementCrash:
    """Records a suspected defect; the tests below pin CURRENT behaviour
    (that this raises), they are NOT asserting that raising is intended.

    The list branch of the addressing loop calls `c.startswith(...)` /
    `c.endswith(...)` on every element of a 'cc'/'to'/'audience'/'target'
    list with no per-element isinstance check -- only the list ITSELF is
    type-checked (`isinstance(potential_id, list)`), never its members. A
    non-string element (e.g. a dict, which the Activity Streams vocabulary
    permits in these fields for an embedded object) raises AttributeError.

    Call-site analysis (see the task report for the full trace): every
    caller of find_community passes JSON that reaches it without any
    upstream validation of the addressing lists' element types --
    app/activitypub/routes.py's Create/Update/Add/Remove handling (lines
    named, not numbered, since they move: the `find_community(request_json)`
    and `find_community(core_activity)` call sites inside
    `process_inbox_request`) all pass a directly-inbound peer activity
    verbatim. This IS reachable by an untrusted remote peer's inbox POST.
    `process_inbox_request` wraps its whole body in `except Exception:
    session.rollback(); raise`, so the crash rolls back cleanly and
    propagates out of the Celery task rather than corrupting state or
    crashing the worker process -- an availability/reliability defect (that
    one activity fails processing), not an authentication or authorization
    bypass.
    """

    def test_a_non_string_element_in_a_cc_list_raises_attributeerror(self, app, db_session):
        with pytest.raises(AttributeError):
            find_community({'cc': [{'type': 'Person', 'id': 'https://peer.example/u/mallory'}]})


class TestSuspectedMissingTypeKeyCrash:
    """Records a suspected defect; the test below pins CURRENT behaviour
    (that this raises), it is NOT asserting that raising is intended.

    Once both the addressing and inReplyTo strategies miss, the function
    unconditionally reads `rj['type']` to check for a PeerTube Video, with no
    'type' in rj guard. An object with no 'type' key raises KeyError.

    Call-site analysis (see the task report): app/activitypub/routes.py's
    'Add' and 'Remove' handling calls `find_community(core_activity)`
    directly. Only `core_activity['type']` (the OUTER activity's type, 'Add'
    or 'Remove') is validated before that call -- nothing checks that
    `core_activity['object']` itself carries a 'type' key, and find_community
    resolves its working `rj` to that inner object whenever 'object' is
    present. A peer sending an Add/Remove whose object has no 'type' reaches
    this KeyError. Separately, `resolve_remote_post_from_search` in
    app/activitypub/util.py (triggered from the UI's 'search' / 'Retrieve a
    post from the original server' action) calls `find_community(post_data)`
    where post_data is the raw JSON fetched from whatever URI was searched --
    also with no 'type' guard before that call. Both are genuinely reachable
    by peer-controlled content; like the AttributeError above, the practical
    effect is that one activity's processing fails rather than any bypass.
    """

    def test_an_object_with_no_type_key_raises_keyerror(self, app, db_session):
        with pytest.raises(KeyError):
            find_community({'type': 'Add', 'object': {'id': 'https://peer.example/x'}})
