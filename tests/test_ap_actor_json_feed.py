"""actor_json_to_model's `Feed` branch, which turns a peer's actor document
into a Feed row.

The `Person`/`Service` branch and the two guards that run before the type
dispatch (`'type' not in activity_json` and `server not in activity_json['id']`)
are covered in tests/test_ap_actor_json_person.py; the `Group` branch is
covered in tests/test_ap_actor_json_group.py. Neither is repeated here. Every
test in this file asserts on the Feed that came back (or on None, or on the
exception that escaped), never on a User or a Community.

The unrecognised-`type` fallthrough -- a document whose type matches none of
Person, Service, Group or Feed -- is covered here, in TestFeedDispatch, because
this branch is the last link in the if/elif chain and the fallthrough is what
lies immediately past it.

The peer document is built by tests.factories.peer_actor_json, shared with the
Person/Service and Group files. Its Feed baseline holds only the keys this
branch reads unconditionally -- type, id, preferredUsername,
publicKey.publicKeyPem, name, inbox, outbox and following -- so every optional
key is opted into by name and the "absent" side of each guard is what the
baseline already gives you.

Two entries in that baseline are unconditional in effect rather than in form,
and both were checked against the source rather than assumed:

  inbox      reached through `activity_json['endpoints']['sharedInbox'] if
             'endpoints' in activity_json else activity_json['inbox']`, whose
             else-arm has no further fallback (unlike the Person branch's,
             which ends in `else ''`). A document carrying neither key raises
             KeyError. Pinned by TestInboxResolution.
  following  read unconditionally by the get_request that fetches the feed's
             /following collection, which runs BEFORE the Feed() call. Pinned
             by TestRequiredFieldsMissing.

`attributedTo` is deliberately NOT in that baseline: it is the first of three
arms and putting it in the baseline would make the other two unreachable. This
file's own `_owned_feed` helper adds it, and TestOwnersUrl uses the bare
`_feed` helper to reach the `moderators` elif and the `else owners_url = None`.

Optional-field enumeration
--------------------------

Derived fresh against this checkout, restricted to the Feed branch (from the
`elif activity_json['type'] == 'Feed':` down to its final `return feed`, whose
line numbers the script prints and this docstring deliberately does not
repeat):

    podman-compose -f compose.test.yaml exec -T -w /app test-runner python -c "
    import ast
    src = open('app/activitypub/util.py').read()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.FunctionDef) and n.name == 'actor_json_to_model':
            func = n
    def find_feed(stmts):
        for s in stmts:
            if isinstance(s, ast.If):
                if 'Feed' in ast.unparse(s.test):
                    return s
                r = find_feed(s.orelse)
                if r: return r
        return None
    branch = find_feed(func.body)
    lo, hi = branch.lineno, branch.body[-1].lineno
    ifs    = [s for s in ast.walk(func) if isinstance(s, ast.If)    and lo <= s.lineno <= hi]
    ifexps = [s for s in ast.walk(func) if isinstance(s, ast.IfExp) and lo <= s.lineno <= hi]
    loops  = [s for s in ast.walk(func) if isinstance(s, ast.For)   and lo <= s.lineno <= hi]
    print('If total:', len(ifs), ' IfExp:', len(ifexps), ' For:', len(loops))
    for s in sorted(ifs, key=lambda s: s.lineno):
        print('  If   ', ast.unparse(s.test)[:95])
    for s in sorted(ifexps, key=lambda s: s.lineno):
        print('  IfExp', ast.unparse(s.test)[:95])
    "

That prints `If total: 26  IfExp: 9  For: 5`. Two of the twenty-six are not
optional-field guards -- the type dispatch itself (`== 'Feed'`) and the
`if feed:` early return for a feed already in the database -- so the branch
holds **9 conditional expressions + 24 `if` statements = 33 conditional
sites**, or 35 counting the dispatch and the early return. Both of those two
are covered as well, by TestFeedDispatch and TestExistingFeed.

The nine conditional expressions are all inside the Feed() constructor call:

    'sensitive' in activity_json     -> nsfw, else False
    'summary' in activity_json       -> description_html, else ''
    'source' in activity_json        -> description via
                                        piefed_markdown_to_lemmy_markdown,
                                        else ''
    'published' in activity_json     -> created_at, else utcnow()
    'updated' in activity_json       -> last_edit, else utcnow()
    address.startswith('~')          -> ap_id drops the '~', else not
    'followers' in activity_json     -> ap_followers_url, else None
    'following' in activity_json     -> ap_following_url, else None
    'endpoints' in activity_json     -> ap_inbox_url from endpoints.sharedInbox,
                                        else activity_json['inbox']

and twenty-four `if` statements (the two excluded above are not in this list):

    'attributedTo' ... and isinstance(attributedTo, str)   -> owners_url
    'moderators' in activity_json                          (elif; else None)
    'sensitive' ... and sensitive and not site.enable_nsfw (returns None)
    'nsfl' ... and nsfl and not site.enable_nsfl           (returns None)
    owners_data.status_code == 200
    following_data.status_code == 200
    'summary' in activity_json                             -> description_html
    'content' in activity_json                             (elif; else '')
    description_html is not None and description_html != ''
      not description_html.startswith('<')                 (PeerTube wrap)
      'source' ... and source['mediaType'] == 'text/markdown'
    'icon' in activity_json and activity_json['icon'] is not None
      isinstance(icon, dict) and 'url' in icon
      isinstance(icon, list) and 'url' in icon[-1]
      isinstance(icon, str)
      icon_entry                                (the else leaves it None)
    'image' in activity_json and activity_json['image'] is not None
      isinstance(image, dict) and 'url' in image
      isinstance(image, list) and 'url' in image[0]
      image_entry
    feed                             (the re-fetch after commit; see
                                      TestFeedRefetchAfterCommit)
    feed.icon_id                     -> make_image_sizes
    feed.image_id                    -> make_image_sizes
    'childFeeds' in activity_json

and five `for` loops: the owners collection, the /following collection, the
FeedMember inserts, the FeedItem inserts, and the childFeeds loop. Every one of
the five is entered at least once and left at least once by the tests below,
which is what both of a `for`'s branch arcs need.

Why every creating test needs http_mock
---------------------------------------

Unlike the Group branch, this one dereferences two remote collections
UNCONDITIONALLY before it builds the row: the owners/moderators collection and
the feed's own /following collection. So a test that expects a Feed back must
register both routes. http_mock's assert_all_called=True then also works in the
test's favour: a registered route that is never called fails the test, which is
what pins that the fetch happened at all.

Tests that expect no Feed back (the two nsfw guards) register nothing, because
the guards run before the first fetch.

The owners in the collection are resolved through find_actor_or_create. Every
test here pre-creates the owner's User row with a recent ap_fetched_at, so that
function takes its found-in-the-database path: without the row it would fetch
the actor over HTTP, and without the timestamp schedule_actor_refresh would
fire a profile refresh that fetches it anyway.

Every test also builds the peer's Instance row first with make_instance, for
the same reason the Group file does: otherwise find_instance_id inserts a
sparse Instance and calls new_instance_profile, which fetches the peer's
nodeinfo.

FINDINGS pinned by this file, reported and not fixed -- each has its own class
docstring saying more:

  1. The Feed branch has no `except KeyError`, exactly like the Group branch
     and unlike the Person/Service branch. TestRequiredFieldsMissing.
  2. `owner_users[0].id` is unguarded. An owners collection that does not
     return 200, or returns 200 with an empty orderedItems, raises IndexError;
     an entry find_actor_or_create rejects puts None in the list and raises
     AttributeError. TestOwnersCollection.
  3. The same shape in the /following loop: an entry find_actor_or_create
     rejects reaches `c.id` as None. TestFollowingCollection.
  4. A feed with neither attributedTo nor moderators reaches get_request(None),
     which raises httpx.HTTPError out of the function. TestOwnersUrl.
  5. `ap_following_url=... if 'following' in activity_json else None` can never
     take its else arm: the same key is read unconditionally, earlier, by the
     get_request that fetches the /following collection. TestScalarOptionalFields.
"""
from datetime import datetime

import httpx
import pytest

from app import db
from app.activitypub import util as activitypub_util
from app.activitypub.util import actor_json_to_model
from app.models import Community, Feed, FeedItem, FeedMember, File, User, utcnow
from tests.factories import make_instance, make_user, peer_actor_json

PEER = 'peer.example'
FEED = 'news'


def _feed_id(name=FEED):
    """The id peer_actor_json gives a Feed document on this file's server."""
    return f'https://{PEER}/f/{name}'


def _owners_url(name=FEED):
    """The collection this file points attributedTo/moderators at."""
    return f'{_feed_id(name)}/moderators'


def _following_url(name=FEED):
    """The /following collection peer_actor_json's Feed baseline publishes."""
    return f'{_feed_id(name)}/following'


def _feed(name=FEED, **kwargs):
    """peer_actor_json for a Feed, with this file's server. No attributedTo:
    the baseline deliberately omits it, and TestOwnersUrl needs that."""
    return peer_actor_json('Feed', name=name, server=PEER, **kwargs)


def _owned_feed(name=FEED, fields=None, omit=()):
    """_feed plus an attributedTo pointing at this file's owners collection --
    the first of the three owners_url arms, which is what every test that is
    not about those arms wants."""
    merged = {'attributedTo': _owners_url(name)}
    if fields:
        merged.update(fields)
    return _feed(name=name, fields=merged, omit=omit)


def _peer_instance(domain=PEER):
    """The Instance row find_instance_id looks up, created before the call so
    that function takes its found-existing path instead of inserting a sparse
    row and calling new_instance_profile (which fetches the peer's nodeinfo).
    """
    return make_instance(domain)


def _owner(instance, name='feedowner'):
    """A remote User find_actor_or_create resolves without any HTTP.

    ap_profile_id is rewritten onto the /u/ path find_remote_actor keys off,
    and ap_fetched_at is set so schedule_actor_refresh leaves it alone -- a
    None there means "stale" and fires a profile refresh that fetches the
    actor.
    """
    user = make_user(instance, name)
    user.ap_profile_id = f'https://{PEER}/u/{name}'
    user.ap_public_url = user.ap_profile_id
    user.ap_fetched_at = utcnow()
    db.session.commit()
    return user


def _remote_community(instance, name='memes'):
    """A remote Community find_actor_or_create resolves without any HTTP, for
    the entries of a feed's /following collection."""
    community = Community(name=name, title=name, instance_id=instance.id,
                          ap_domain=PEER,
                          ap_profile_id=f'https://{PEER}/c/{name}',
                          ap_public_url=f'https://{PEER}/c/{name}',
                          ap_fetched_at=utcnow(),
                          subscriptions_count=0, local_only=False, nsfw=False)
    db.session.add(community)
    db.session.commit()
    return community


def _register_owners(http_mock, urls, name=FEED, status=200):
    """The owners/moderators collection.

    A non-200 answer deliberately carries a PLAIN-TEXT body rather than a JSON
    collection, and that choice is load-bearing: the production code only calls
    .json() inside the `status_code == 200` guard, so a 404 whose body happened
    to be a well-formed empty collection would let a mutation that replaces the
    guard with `True` pass unnoticed. With a plain-text body the mutated code
    raises instead. Verified by running that mutation both ways round.
    """
    if status == 200:
        http_mock.get(_owners_url(name)).respond(200, json={'orderedItems': list(urls)})
    else:
        http_mock.get(_owners_url(name)).respond(status, text='not a collection')


def _register_following(http_mock, urls=(), name=FEED, status=200):
    """The feed's own /following collection. Same plain-text-on-failure rule as
    _register_owners above, for the same reason."""
    if status == 200:
        http_mock.get(_following_url(name)).respond(200, json={'items': list(urls)})
    else:
        http_mock.get(_following_url(name)).respond(status, text='not a collection')


def _peer_with_one_owner(http_mock, name=FEED, following=()):
    """The setup all the ordinary creating tests share: an Instance, one owner
    User, and both collections registered. Returns the owner."""
    instance = _peer_instance()
    owner = _owner(instance)
    _register_owners(http_mock, [owner.ap_profile_id], name=name)
    _register_following(http_mock, following, name=name)
    return owner


class TestFeedDispatch:
    """`elif activity_json['type'] == 'Feed':`, and what happens to documents
    that reach it without matching.

    Both mutation directions are exercised:

    - narrowed, e.g. `== 'Feed_'`: a Feed document then falls off the end of
      the if/elif chain and the function returns None instead of a Feed.
      test_feed_document_creates_a_feed catches it.
    - broadened to `!= 'Group'` (or to a bare `True`): an actor type this
      function does not recognise is built as a Feed instead of falling off the
      end of the chain. test_unrecognised_actor_type_returns_none catches both.

    test_feed_document_creates_a_feed is also the test that kills the one
    broadening of the GROUP branch's dispatch that the Group file could not
    kill by itself -- `activity_json['type'] in ('Group', 'Feed')`, which
    leaves Group documents routed correctly and only mis-routes Feed documents.
    Under that mutation a Feed document comes back as a Community and every
    assertion below fails.
    """

    def test_feed_document_creates_a_feed(self, app, db_session, http_mock):
        instance = _peer_instance()
        owner = _owner(instance)
        _register_owners(http_mock, [owner.ap_profile_id])
        _register_following(http_mock)

        feed = actor_json_to_model(_owned_feed(), '~news', PEER)

        assert isinstance(feed, Feed)
        assert feed.name == FEED
        assert feed.machine_name == FEED
        assert feed.title == 'News'
        assert feed.user_id == owner.id
        assert feed.ap_id == f'news@{PEER}'
        assert feed.ap_domain == PEER
        assert feed.ap_profile_id == _feed_id()
        assert feed.ap_public_url == _feed_id()
        assert feed.ap_inbox_url == f'{_feed_id()}/inbox'
        assert feed.ap_outbox_url == f'{_feed_id()}/outbox'
        assert feed.ap_moderators_url == _owners_url()
        assert feed.ap_fetched_at is not None
        assert feed.instance_id == instance.id
        assert feed.public is True
        assert feed.num_communities == 0
        assert feed.public_key.startswith('-----BEGIN PUBLIC KEY-----')
        assert db.session.query(Feed).count() == 1

    def test_unrecognised_actor_type_returns_none(self, app, db_session):
        """A document whose type matches none of Person, Service, Group or Feed
        falls past all three branches. There is no trailing `return` statement
        after the chain, so the function returns None by falling off its end,
        and nothing is written.

        No http_mock: nothing is fetched, because nothing is built.
        """
        _peer_instance()
        document = peer_actor_json('Organization', name=FEED, server=PEER)
        assert actor_json_to_model(document, '~news', PEER) is None
        assert db.session.query(Feed).count() == 0
        assert db.session.query(Community).count() == 0
        assert db.session.query(User).count() == 0


class TestExistingFeed:
    """`feed = ...filter(Feed.ap_profile_id == activity_json['id'].lower())
    .first(); if feed: return feed`.

    A plain "call it twice with the same document" test does not pin this on
    its own: with the early return gone, the second call's insert would collide
    on the unique ap_profile_id and the IntegrityError handler would hand back
    the very same row, so `second.id == first.id` would pass either way. Both
    tests here therefore strip a key the rebuild needs, so a lookup that missed
    raises instead of masquerading as a hit.

    Neither test registers a route for the second call, and neither needs one:
    the early return precedes both get_requests. The routes registered for the
    FIRST call satisfy http_mock's assert_all_called.

    test_a_second_call_with_the_same_document_returns_the_same_row is kept
    anyway, and is what makes that claim about the IntegrityError handler
    checkable rather than merely plausible: with the early return replaced by
    `if False:` it still passes, while the two stripped tests fail. That
    contrast was run, not assumed.

    Mutation that fails test_lookup_of_an_existing_feed_lowercases_the_id:
    dropping the `.lower()` from the filter, which stops the stored row
    matching an id the peer re-published with a different case.
    """

    def test_a_second_call_with_the_same_document_returns_the_same_row(
            self, app, db_session, http_mock):
        _peer_with_one_owner(http_mock)
        document = _owned_feed()
        first = actor_json_to_model(document, '~news', PEER)
        second = actor_json_to_model(document, '~news', PEER)
        assert second.id == first.id
        assert db.session.query(Feed).count() == 1

    def test_existing_feed_is_returned_before_any_other_key_is_read(
            self, app, db_session, http_mock):
        _peer_with_one_owner(http_mock)
        existing = actor_json_to_model(_owned_feed(), '~news', PEER)
        stripped = _owned_feed(omit=('preferredUsername', 'name', 'outbox', 'following'))

        result = actor_json_to_model(stripped, '~news', PEER)

        assert result.id == existing.id
        assert db.session.query(Feed).count() == 1

    def test_lookup_of_an_existing_feed_lowercases_the_id(self, app, db_session, http_mock):
        """A peer that upper-cases the path of its own id on a later fetch
        still matches the stored row. Only the path is varied: the host cannot
        be, because the `server not in activity_json['id']` guard is a
        case-sensitive substring test that would reject the document first.

        'following' is stripped so a failed match cannot masquerade as a hit --
        the rebuild would raise KeyError at the /following fetch.
        """
        _peer_with_one_owner(http_mock)
        existing = actor_json_to_model(_owned_feed(), '~news', PEER)
        document = _owned_feed(fields={'id': f'https://{PEER}/f/NEWS'},
                               omit=('following',))

        result = actor_json_to_model(document, '~news', PEER)

        assert result.id == existing.id
        assert db.session.query(Feed).count() == 1


class TestOwnersUrl:
    """The three arms that choose owners_url:

        if 'attributedTo' in activity_json and isinstance(..., str):
            owners_url = activity_json['attributedTo']     # lemmy, mbin, us
        elif 'moderators' in activity_json:
            owners_url = activity_json['moderators']       # kbin, us
        else:
            owners_url = None

    All three are exercised, and the isinstance operand is falsified
    independently of the `in` operand by a document whose attributedTo is a
    list rather than a string -- the shape Mastodon-family software publishes.

    FINDING (4) -- the third arm is not a graceful default. owners_url = None
    goes straight into get_request, whose is_invalid_get_request_uri returns
    True for it, so the call raises httpx.HTTPError("HTTPError: invalid uri")
    out of actor_json_to_model rather than returning None. Every caller of
    actor_json_to_model would have to catch that. Pinned as today's behaviour.

    Mutation that fails test_attributed_to_that_is_not_a_string_falls_through:
    dropping the isinstance operand, which would put the list itself into
    owners_url and then into ap_moderators_url.
    """

    def test_attributed_to_string_is_used(self, app, db_session, http_mock):
        _peer_with_one_owner(http_mock)
        feed = actor_json_to_model(_owned_feed(), '~news', PEER)
        assert feed.ap_moderators_url == _owners_url()

    def test_moderators_is_used_when_there_is_no_attributed_to(
            self, app, db_session, http_mock):
        _peer_with_one_owner(http_mock)
        document = _feed(fields={'moderators': _owners_url()})
        feed = actor_json_to_model(document, '~news', PEER)
        assert feed.ap_moderators_url == _owners_url()

    def test_moderators_is_used_when_attributed_to_is_not_a_string(
            self, app, db_session, http_mock):
        """Both keys present, attributedTo the wrong shape: the elif wins."""
        _peer_with_one_owner(http_mock)
        document = _feed(fields={'attributedTo': [{'id': f'https://{PEER}/u/x'}],
                                 'moderators': _owners_url()})
        feed = actor_json_to_model(document, '~news', PEER)
        assert feed.ap_moderators_url == _owners_url()

    def test_attributed_to_that_is_not_a_string_falls_through(
            self, app, db_session):
        """attributedTo present but not a string, and no moderators: the else
        arm is taken and owners_url is None, so the fetch raises. No http_mock
        route is registered because no HTTP request is ever made -- get_request
        rejects the uri before the transport sees it."""
        _peer_instance()
        document = _feed(fields={'attributedTo': [{'id': f'https://{PEER}/u/x'}]})
        with pytest.raises(httpx.HTTPError) as excinfo:
            actor_json_to_model(document, '~news', PEER)
        assert 'invalid uri' in str(excinfo.value)
        assert db.session.query(Feed).count() == 0

    def test_neither_attributed_to_nor_moderators_raises(self, app, db_session):
        _peer_instance()
        with pytest.raises(httpx.HTTPError) as excinfo:
            actor_json_to_model(_feed(), '~news', PEER)
        assert 'invalid uri' in str(excinfo.value)
        assert db.session.query(Feed).count() == 0


class TestNsfwAndNsflGuards:
    """`if 'sensitive' ... and activity_json['sensitive'] and not
    site.enable_nsfw: return None`, and the identical nsfl guard.

    All three operands of each guard are falsified independently: the key
    absent (every other test in this file), the key present but false
    (test_sensitive_false_is_not_blocked), and the instance opting in
    (test_sensitive_document_is_accepted_when_the_instance_enables_nsfw).

    The rejecting tests register no http_mock routes, which is itself the
    evidence that the guard returns before either collection is fetched: a
    request to an unregistered url raises under the suite-wide respx router.

    Mutation that fails test_sensitive_document_is_rejected_when_nsfw_is_off:
    deleting the guard, which admits the feed. Mutation that fails
    test_sensitive_document_is_accepted_when_the_instance_enables_nsfw:
    dropping the `not site.enable_nsfw` operand, which would reject the
    document even on an instance that allows it.
    """

    def test_sensitive_document_is_rejected_when_nsfw_is_off(
            self, app, db_session, site):
        _peer_instance()
        assert site.enable_nsfw is not True
        document = _owned_feed(fields={'sensitive': True})
        assert actor_json_to_model(document, '~news', PEER) is None
        assert db.session.query(Feed).count() == 0

    def test_sensitive_document_is_accepted_when_the_instance_enables_nsfw(
            self, app, db_session, site, http_mock):
        _peer_with_one_owner(http_mock)
        site.enable_nsfw = True
        db.session.commit()
        document = _owned_feed(fields={'sensitive': True})
        feed = actor_json_to_model(document, '~news', PEER)
        assert feed is not None
        assert feed.nsfw is True

    def test_sensitive_false_is_not_blocked(self, app, db_session, site, http_mock):
        """The middle operand: the key is present, so `'sensitive' in
        activity_json` is true, and only the value stops the guard."""
        _peer_with_one_owner(http_mock)
        document = _owned_feed(fields={'sensitive': False})
        feed = actor_json_to_model(document, '~news', PEER)
        assert feed is not None
        assert feed.nsfw is False

    def test_nsfl_document_is_rejected_when_nsfl_is_off(self, app, db_session, site):
        _peer_instance()
        assert site.enable_nsfl is not True
        document = _owned_feed(fields={'nsfl': True})
        assert actor_json_to_model(document, '~news', PEER) is None
        assert db.session.query(Feed).count() == 0

    def test_nsfl_document_is_accepted_when_the_instance_enables_nsfl(
            self, app, db_session, site, http_mock):
        """nsfl is never copied onto the Feed row -- only the guard reads it --
        so the observable outcome is that a row exists at all, with the column
        left at its default."""
        _peer_with_one_owner(http_mock)
        site.enable_nsfl = True
        db.session.commit()
        document = _owned_feed(fields={'nsfl': True})
        feed = actor_json_to_model(document, '~news', PEER)
        assert feed is not None
        assert feed.nsfl is False
        assert db.session.query(Feed).count() == 1

    def test_nsfl_false_is_not_blocked(self, app, db_session, site, http_mock):
        _peer_with_one_owner(http_mock)
        document = _owned_feed(fields={'nsfl': False})
        feed = actor_json_to_model(document, '~news', PEER)
        assert feed is not None
        assert db.session.query(Feed).count() == 1


class TestOwnersCollection:
    """`owners_data = get_request(owners_url, ...)`, the
    `if owners_data.status_code == 200:` guard, the loop over orderedItems, and
    the `FeedMember(..., is_owner=True)` rows written after the commit.

    FINDING (2) -- `user_id=owner_users[0].id` is unguarded, and owner_users is
    empty whenever the guard's false arm is taken. So a peer whose moderators
    collection 404s, or answers 200 with an empty orderedItems, makes
    actor_json_to_model raise IndexError instead of returning None or a
    feed with no owner. The same line raises AttributeError when
    find_actor_or_create rejects an entry, because the None it returns is
    appended to the list unchecked. Both are pinned below as today's behaviour.

    The rejected-entry test uses the Public collection URI, which
    validate_remote_actor refuses by name -- the cheapest rejection available,
    and one that needs no HTTP and no BannedInstances row.

    Mutation that fails test_a_non_200_owners_collection_raises_index_error:
    replacing the status guard with `True`, which reaches .json() on a body
    that is not JSON. That kill depends on the 404 body being plain text --
    see _register_owners, where the first version of this file served an empty
    JSON collection instead and the mutation SURVIVED. Mutation that fails
    test_two_owners_become_two_feed_members: replacing the guard with `False`,
    which takes the IndexError path instead.
    """

    def test_the_first_owner_becomes_the_feeds_user(self, app, db_session, http_mock):
        owner = _peer_with_one_owner(http_mock)
        feed = actor_json_to_model(_owned_feed(), '~news', PEER)
        assert feed.user_id == owner.id
        members = db.session.query(FeedMember).filter_by(feed_id=feed.id).all()
        assert len(members) == 1
        assert members[0].user_id == owner.id
        assert members[0].is_owner is True

    def test_two_owners_become_two_feed_members(self, app, db_session, http_mock):
        instance = _peer_instance()
        first = _owner(instance, 'firstowner')
        second = _owner(instance, 'secondowner')
        _register_owners(http_mock, [first.ap_profile_id, second.ap_profile_id])
        _register_following(http_mock)

        feed = actor_json_to_model(_owned_feed(), '~news', PEER)

        assert feed.user_id == first.id
        members = db.session.query(FeedMember).filter_by(feed_id=feed.id).all()
        assert sorted(m.user_id for m in members) == sorted([first.id, second.id])
        assert all(m.is_owner is True for m in members)

    def test_a_non_200_owners_collection_raises_index_error(
            self, app, db_session, http_mock):
        instance = _peer_instance()
        _owner(instance)
        _register_owners(http_mock, [], status=404)
        _register_following(http_mock)

        with pytest.raises(IndexError):
            actor_json_to_model(_owned_feed(), '~news', PEER)
        assert db.session.query(Feed).count() == 0

    def test_an_empty_owners_collection_raises_index_error(
            self, app, db_session, http_mock):
        instance = _peer_instance()
        _owner(instance)
        _register_owners(http_mock, [])
        _register_following(http_mock)

        with pytest.raises(IndexError):
            actor_json_to_model(_owned_feed(), '~news', PEER)
        assert db.session.query(Feed).count() == 0

    def test_an_owner_the_resolver_rejects_is_appended_as_none(
            self, app, db_session, http_mock):
        """find_actor_or_create returns None for the Public collection URI, and
        the loop appends it without checking, so the None reaches `.id`."""
        instance = _peer_instance()
        _owner(instance)
        _register_owners(http_mock, ['https://www.w3.org/ns/activitystreams#Public'])
        _register_following(http_mock)

        with pytest.raises(AttributeError) as excinfo:
            actor_json_to_model(_owned_feed(), '~news', PEER)
        assert "'NoneType' object has no attribute 'id'" in str(excinfo.value)
        assert db.session.query(Feed).count() == 0


class TestFollowingCollection:
    """`following_data = get_request(activity_json['following'], ...)`, the
    `if following_data.status_code == 200:` guard, and the FeedItem rows
    written for each community in the collection.

    num_communities is set to 0 in the constructor and incremented once per
    FeedItem, so the count on the returned row is what says how many were
    linked.

    FINDING (3) -- the same unguarded shape as the owners loop: an entry
    find_actor_or_create rejects is appended as None and reaches `c.id`. Here
    the crash lands AFTER the feed has been committed, so the peer is left with
    a persisted Feed and the caller sees an exception -- a partially applied
    ingest rather than a clean rejection.

    Mutation that fails test_a_non_200_following_collection_links_nothing:
    replacing the status guard with `True`, which reaches .json() on a body
    that is not JSON -- again dependent on _register_following serving plain
    text rather than an empty JSON collection. Mutation that fails
    test_each_followed_community_becomes_a_feed_item: replacing the guard with
    `False`, which links nothing.
    """

    def test_each_followed_community_becomes_a_feed_item(
            self, app, db_session, http_mock):
        instance = _peer_instance()
        owner = _owner(instance)
        first = _remote_community(instance, 'memes')
        second = _remote_community(instance, 'news_comm')
        _register_owners(http_mock, [owner.ap_profile_id])
        _register_following(http_mock, [first.ap_profile_id, second.ap_profile_id])

        feed = actor_json_to_model(_owned_feed(), '~news', PEER)

        items = db.session.query(FeedItem).filter_by(feed_id=feed.id).all()
        assert sorted(i.community_id for i in items) == sorted([first.id, second.id])
        assert feed.num_communities == 2

    def test_an_empty_following_collection_links_nothing(
            self, app, db_session, http_mock):
        _peer_with_one_owner(http_mock)
        feed = actor_json_to_model(_owned_feed(), '~news', PEER)
        assert db.session.query(FeedItem).count() == 0
        assert feed.num_communities == 0

    def test_a_non_200_following_collection_links_nothing(
            self, app, db_session, http_mock):
        instance = _peer_instance()
        owner = _owner(instance)
        _remote_community(instance, 'memes')
        _register_owners(http_mock, [owner.ap_profile_id])
        _register_following(http_mock, [], status=410)

        feed = actor_json_to_model(_owned_feed(), '~news', PEER)

        assert feed is not None
        assert db.session.query(FeedItem).count() == 0
        assert feed.num_communities == 0

    def test_a_followed_community_the_resolver_rejects_crashes_after_the_commit(
            self, app, db_session, http_mock):
        _peer_with_one_owner(
            http_mock, following=['https://www.w3.org/ns/activitystreams#Public'])

        with pytest.raises(AttributeError) as excinfo:
            actor_json_to_model(_owned_feed(), '~news', PEER)

        assert "'NoneType' object has no attribute 'id'" in str(excinfo.value)
        # The Feed row survives the exception: the crash is after the commit.
        assert db.session.query(Feed).count() == 1
        assert db.session.query(FeedItem).count() == 0


class TestRequiredFieldsMissing:
    """FINDING (1) -- the Feed branch has no `except KeyError` at all.

    The Person/Service branch builds its User inside
    `try: ... except KeyError: current_app.logger.error(...); return None`, so
    a malformed peer document there becomes a logged None and the caller
    carries on. The Feed branch's Feed() construction is not wrapped, so the
    same malformation escapes to the caller as a KeyError. These tests pin
    today's behaviour, not the desirable behaviour; a fix that adds the handler
    is expected to rewrite them into `is None` assertions.

    'following' is the odd one out and has its own test below: it is read by
    the get_request that fetches the /following collection, which runs before
    the constructor, so it raises earlier than the other four -- early enough
    that the /following route is never requested at all, which is why that test
    registers only the owners route.

    'inbox' raises only when 'endpoints' is also absent -- see
    TestInboxResolution.
    """

    @pytest.mark.parametrize('missing', ['preferredUsername', 'name', 'outbox',
                                         'publicKey'])
    def test_a_missing_constructor_key_raises_key_error(
            self, app, db_session, http_mock, missing):
        _peer_with_one_owner(http_mock)
        document = _owned_feed(omit=(missing,))
        with pytest.raises(KeyError) as excinfo:
            actor_json_to_model(document, '~news', PEER)
        assert excinfo.value.args[0] == missing
        assert db.session.query(Feed).count() == 0

    def test_a_missing_following_raises_before_the_following_fetch(
            self, app, db_session, http_mock):
        """Only the owners route is registered. http_mock's assert_all_called
        would fail this test if a /following request were somehow made, and
        the KeyError proves the read is unconditional."""
        instance = _peer_instance()
        owner = _owner(instance)
        _register_owners(http_mock, [owner.ap_profile_id])
        document = _owned_feed(omit=('following',))
        with pytest.raises(KeyError) as excinfo:
            actor_json_to_model(document, '~news', PEER)
        assert excinfo.value.args[0] == 'following'
        assert db.session.query(Feed).count() == 0


class TestInboxResolution:
    """`ap_inbox_url = activity_json['endpoints']['sharedInbox'] if 'endpoints'
    in activity_json else activity_json['inbox']`.

    The else arm has NO further fallback, unlike the Person/Service branch's,
    which ends `else activity_json['inbox'] if 'inbox' in activity_json else
    ''`. A Feed document carrying neither key is therefore a hard KeyError
    where the same document on the Person branch would store an empty string.
    That asymmetry is why 'inbox' is in peer_actor_json's Feed baseline.

    Mutation that fails test_shared_inbox_wins_when_endpoints_is_present:
    reordering the expression to try 'inbox' first.
    """

    def test_shared_inbox_wins_when_endpoints_is_present(
            self, app, db_session, http_mock):
        _peer_with_one_owner(http_mock)
        document = _owned_feed(fields={'endpoints': {'sharedInbox': f'https://{PEER}/inbox'}})
        feed = actor_json_to_model(document, '~news', PEER)
        assert feed.ap_inbox_url == f'https://{PEER}/inbox'

    def test_the_actors_own_inbox_is_used_without_endpoints(
            self, app, db_session, http_mock):
        _peer_with_one_owner(http_mock)
        feed = actor_json_to_model(_owned_feed(), '~news', PEER)
        assert feed.ap_inbox_url == f'{_feed_id()}/inbox'

    def test_neither_endpoints_nor_inbox_raises_key_error(
            self, app, db_session, http_mock):
        _peer_with_one_owner(http_mock)
        document = _owned_feed(omit=('inbox',))
        with pytest.raises(KeyError) as excinfo:
            actor_json_to_model(document, '~news', PEER)
        assert excinfo.value.args[0] == 'inbox'
        assert db.session.query(Feed).count() == 0


class TestApIdFromAddress:
    """`ap_id = f"{address[1:].lower()}@{server.lower()}" if
    address.startswith('~') else f"{address.lower()}@{server.lower()}"`.

    '~' is PieFed's feed sigil, and callers pass the address both ways. Both
    arms also lower-case, which the mixed-case test pins.

    Mutation that fails test_bare_address_keeps_all_of_its_characters:
    inverting the condition to `not address.startswith('~')`, which would eat
    this address's first character.
    """

    def test_sigil_prefixed_address_loses_the_sigil(self, app, db_session, http_mock):
        _peer_with_one_owner(http_mock)
        feed = actor_json_to_model(_owned_feed(), '~news', PEER)
        assert feed.ap_id == f'news@{PEER}'

    def test_bare_address_keeps_all_of_its_characters(self, app, db_session, http_mock):
        _peer_with_one_owner(http_mock)
        feed = actor_json_to_model(_owned_feed(), 'news', PEER)
        assert feed.ap_id == f'news@{PEER}'

    def test_both_arms_lower_case_the_address_and_the_server(
            self, app, db_session, http_mock):
        _peer_with_one_owner(http_mock)
        feed = actor_json_to_model(_owned_feed(), '~NeWs', PEER)
        assert feed.ap_id == f'news@{PEER}'
        assert feed.ap_domain == PEER


class TestScalarOptionalFields:
    """The nine conditional expressions in the Feed() call, both ways round.

    The present/absent pair is the point: a suite that always supplied every
    key would leave every `else` of these expressions unexecuted while branch
    coverage reported the whole constructor as covered -- and coverage.py does
    not record a conditional expression as a branch arc at all, so nothing in
    the coverage number would have shown the gap.

    `address.startswith('~')` and the endpoints/inbox expression are exercised
    by TestApIdFromAddress and TestInboxResolution above, which need more than
    one document each.

    FINDING (5) -- `ap_following_url=activity_json['following'] if 'following'
    in activity_json else None` can NEVER take its else arm. The same key is
    read unconditionally, earlier in the branch, by the get_request that
    fetches the /following collection, so a document without it has already
    raised KeyError by the time the constructor runs (pinned by
    TestRequiredFieldsMissing). The `else None` is dead code. No test here can
    reach it, and none pretends to.

    Mutation pair on one optional-field guard, `'published' in activity_json`:
      - replaced with `True`: test_every_scalar_optional_absent_takes_its_default
        raises KeyError instead of returning a fresh utcnow().
      - replaced with `False`: test_every_scalar_optional_present_is_copied gets
        a fresh utcnow() instead of the peer's 2024 timestamp.
    Either replacement is caught, which is what makes the pair worth naming.
    """

    def test_every_scalar_optional_present_is_copied(
            self, app, db_session, site, http_mock):
        _peer_with_one_owner(http_mock)
        site.enable_nsfw = True
        db.session.commit()
        document = _owned_feed(fields={
            'sensitive': True,
            'summary': '<p>the summary</p>',
            'source': {'content': 'the *source*'},
            'published': '2024-01-02T03:04:05Z',
            'updated': '2024-03-04T05:06:07Z',
            'followers': f'{_feed_id()}/followers',
        })

        feed = actor_json_to_model(document, '~news', PEER)

        assert feed.nsfw is True
        assert feed.created_at == datetime(2024, 1, 2, 3, 4, 5)
        assert feed.last_edit == datetime(2024, 3, 4, 5, 6, 7)
        assert feed.ap_followers_url == f'{_feed_id()}/followers'
        assert feed.ap_following_url == _following_url()

    def test_every_scalar_optional_absent_takes_its_default(
            self, app, db_session, http_mock):
        _peer_with_one_owner(http_mock)
        before = utcnow()

        feed = actor_json_to_model(_owned_feed(), '~news', PEER)

        assert feed.nsfw is False
        # utcnow(), not the peer's value: distinguishable from the 2024
        # timestamps the present-side test supplies, which `is not None` would
        # also accept.
        assert before <= feed.created_at <= utcnow()
        assert before <= feed.last_edit <= utcnow()
        assert feed.ap_followers_url is None
        # description/description_html are the other two expressions; their
        # absent side is asserted in TestDescription.
        assert feed.description == ''


class TestDescription:
    """The description block: `summary` / `content` / neither into
    description_html, the PeerTube `<p>` wrap, allowlist_html, and the
    Markdown-source override.

    Two of the constructor's conditional expressions feed into this and are
    asserted here rather than in TestScalarOptionalFields:
    `description_html=activity_json['summary'] if 'summary' ... else ''` and
    `description=piefed_markdown_to_lemmy_markdown(activity_json['source']
    ['content']) if 'source' ... else ''`. Whatever the constructor put there
    is then overwritten by this block whenever description_html is neither None
    nor empty, which is why the constructor's value is only observable when it
    is not -- the one test below where neither 'summary' nor 'content' is
    present, so description_html stays '' and the overwrite never runs.

    Mutation that fails test_html_summary_is_not_wrapped: deleting the
    `not description_html.startswith('<')` guard, which would wrap an already
    wrapped summary a second time. Mutation that fails
    test_markdown_source_overrides_the_html: dropping the mediaType operand,
    which would take the markdown arm for a source that is not markdown.
    Mutation that fails test_source_without_summary_or_content_reaches_the_constructors_description:
    forcing the constructor's `description=...` conditional expression's true
    arm to `''`, which every other test in this file cannot catch because their
    non-empty description_html sends the overwrite block back over the same
    'source' key and reproduces the same value regardless of what the
    constructor stored.
    """

    def test_summary_becomes_the_description(self, app, db_session, http_mock):
        _peer_with_one_owner(http_mock)
        document = _owned_feed(fields={'summary': '<p>a summary</p>'})
        feed = actor_json_to_model(document, '~news', PEER)
        assert feed.description_html == '<p>a summary</p>'
        assert 'a summary' in feed.description

    def test_content_is_used_when_there_is_no_summary(self, app, db_session, http_mock):
        _peer_with_one_owner(http_mock)
        document = _owned_feed(fields={'content': '<p>some content</p>'})
        feed = actor_json_to_model(document, '~news', PEER)
        assert feed.description_html == '<p>some content</p>'

    def test_summary_wins_when_both_are_present(self, app, db_session, http_mock):
        """Covers the elif: with the `if` turned into a second `if`, content
        would overwrite summary."""
        _peer_with_one_owner(http_mock)
        document = _owned_feed(fields={'summary': '<p>a summary</p>',
                                       'content': '<p>some content</p>'})
        feed = actor_json_to_model(document, '~news', PEER)
        assert feed.description_html == '<p>a summary</p>'

    def test_neither_leaves_the_description_empty(self, app, db_session, http_mock):
        _peer_with_one_owner(http_mock)
        feed = actor_json_to_model(_owned_feed(), '~news', PEER)
        assert feed.description_html == ''
        assert feed.description == ''

    def test_an_empty_summary_leaves_the_description_empty(
            self, app, db_session, http_mock):
        """The `description_html != ''` operand: the key is present, so the
        `if 'summary'` arm is taken, and only the emptiness stops the block."""
        _peer_with_one_owner(http_mock)
        document = _owned_feed(fields={'summary': ''})
        feed = actor_json_to_model(document, '~news', PEER)
        assert feed.description_html == ''

    def test_a_null_summary_leaves_the_description_none(self, app, db_session, http_mock):
        """The `description_html is not None` operand. Without it the block
        would call .startswith on None and raise AttributeError."""
        _peer_with_one_owner(http_mock)
        document = _owned_feed(fields={'summary': None})
        feed = actor_json_to_model(document, '~news', PEER)
        assert feed.description_html is None

    def test_bare_text_summary_is_wrapped_in_a_paragraph(
            self, app, db_session, http_mock):
        """PeerTube publishes an unwrapped string."""
        _peer_with_one_owner(http_mock)
        document = _owned_feed(fields={'summary': 'plain text'})
        feed = actor_json_to_model(document, '~news', PEER)
        assert feed.description_html == '<p>plain text</p>'

    def test_html_summary_is_not_wrapped(self, app, db_session, http_mock):
        _peer_with_one_owner(http_mock)
        document = _owned_feed(fields={'summary': '<p>already wrapped</p>'})
        feed = actor_json_to_model(document, '~news', PEER)
        assert feed.description_html == '<p>already wrapped</p>'

    def test_markdown_source_overrides_the_html(self, app, db_session, http_mock):
        _peer_with_one_owner(http_mock)
        document = _owned_feed(fields={
            'summary': '<p>the html</p>',
            'source': {'content': 'the **markdown**', 'mediaType': 'text/markdown'},
        })
        feed = actor_json_to_model(document, '~news', PEER)
        assert feed.description == 'the **markdown**'
        assert '<strong>markdown</strong>' in feed.description_html

    def test_a_source_that_is_not_markdown_leaves_the_html_alone(
            self, app, db_session, http_mock):
        """The else arm: description comes from html_to_text, not from the
        source's content."""
        _peer_with_one_owner(http_mock)
        document = _owned_feed(fields={
            'summary': '<p>the html</p>',
            'source': {'content': 'the **markdown**', 'mediaType': 'text/html'},
        })
        feed = actor_json_to_model(document, '~news', PEER)
        assert feed.description_html == '<p>the html</p>'
        assert 'the html' in feed.description
        assert '**markdown**' not in feed.description

    def test_source_without_summary_or_content_reaches_the_constructors_description(
            self, app, db_session, http_mock):
        """The Feed() constructor's own `description=...` conditional expression,
        pinned where its true arm is actually observable.

        Every other test in this class supplies 'summary' (or 'content'), so the
        description-handling block below the constructor call always overwrites
        whatever piefed_markdown_to_lemmy_markdown produced there -- the
        constructor's value is built and then immediately discarded. Here
        'summary' and 'content' are both absent, so description_html stays '',
        the `if description_html is not None and description_html != '':` guard
        is false, the block does nothing, and feed.description is left holding
        exactly what the constructor put there.

        piefed_markdown_to_lemmy_markdown only rewrites a soft-break
        (non-whitespace immediately followed by \\r\\n); this source has none,
        so it is returned unchanged -- confirmed by calling the function
        directly with this input before writing the assertion, not assumed.
        """
        _peer_with_one_owner(http_mock)
        document = _owned_feed(fields={'source': {'content': 'the *source* only'}})
        feed = actor_json_to_model(document, '~news', PEER)
        assert feed.description_html == ''
        assert feed.description == 'the *source* only'


class TestIcon:
    """The feed icon. Four shapes of 'icon' are recognised (dict with url, list
    whose LAST entry has a url, bare string, and nothing else) plus the
    `icon_entry` guard that skips the File when none matched.

    Mutation that fails test_icon_as_a_list_takes_the_last_entry: changing the
    [-1] index to [0], which picks the wrong url.
    """

    def test_icon_as_a_dict(self, app, db_session, http_mock):
        _peer_with_one_owner(http_mock)
        http_mock.get(f'https://{PEER}/a.png').respond(404)
        document = _owned_feed(fields={'icon': {'url': f'https://{PEER}/a.png'}})
        feed = actor_json_to_model(document, '~news', PEER)
        assert db.session.get(File, feed.icon_id).source_url == f'https://{PEER}/a.png'

    def test_icon_as_a_list_takes_the_last_entry(self, app, db_session, http_mock):
        _peer_with_one_owner(http_mock)
        http_mock.get(f'https://{PEER}/large.png').respond(404)
        document = _owned_feed(fields={'icon': [
            {'url': f'https://{PEER}/small.png'},
            {'url': f'https://{PEER}/large.png'},
        ]})
        feed = actor_json_to_model(document, '~news', PEER)
        assert db.session.get(File, feed.icon_id).source_url == f'https://{PEER}/large.png'

    def test_icon_as_a_bare_string(self, app, db_session, http_mock):
        _peer_with_one_owner(http_mock)
        http_mock.get(f'https://{PEER}/a.png').respond(404)
        document = _owned_feed(fields={'icon': f'https://{PEER}/a.png'})
        feed = actor_json_to_model(document, '~news', PEER)
        assert db.session.get(File, feed.icon_id).source_url == f'https://{PEER}/a.png'

    def test_icon_of_an_unrecognised_shape_creates_no_file(
            self, app, db_session, http_mock):
        """A dict with no 'url' matches none of the three shapes, so icon_entry
        stays None and the File is never built."""
        _peer_with_one_owner(http_mock)
        document = _owned_feed(fields={'icon': {'mediaType': 'image/png'}})
        feed = actor_json_to_model(document, '~news', PEER)
        assert feed.icon_id is None
        assert db.session.query(File).count() == 0

    def test_null_icon_creates_no_file(self, app, db_session, http_mock):
        _peer_with_one_owner(http_mock)
        document = _owned_feed(fields={'icon': None})
        feed = actor_json_to_model(document, '~news', PEER)
        assert feed.icon_id is None

    def test_absent_icon_creates_no_file(self, app, db_session, http_mock):
        _peer_with_one_owner(http_mock)
        feed = actor_json_to_model(_owned_feed(), '~news', PEER)
        assert feed.icon_id is None
        assert db.session.query(File).count() == 0


class TestImage:
    """The feed banner. Only two shapes are recognised here -- unlike the icon
    block above there is no bare-string arm -- and the list form takes entry
    [0], not [-1].

    Mutation that fails test_image_as_a_list_takes_the_first_entry: changing
    that [0] to [-1].
    """

    def test_image_as_a_dict(self, app, db_session, http_mock):
        _peer_with_one_owner(http_mock)
        http_mock.get(f'https://{PEER}/c.png').respond(404)
        document = _owned_feed(fields={'image': {'url': f'https://{PEER}/c.png'}})
        feed = actor_json_to_model(document, '~news', PEER)
        assert db.session.get(File, feed.image_id).source_url == f'https://{PEER}/c.png'

    def test_image_as_a_list_takes_the_first_entry(self, app, db_session, http_mock):
        _peer_with_one_owner(http_mock)
        http_mock.get(f'https://{PEER}/first.png').respond(404)
        document = _owned_feed(fields={'image': [
            {'url': f'https://{PEER}/first.png'},
            {'url': f'https://{PEER}/second.png'},
        ]})
        feed = actor_json_to_model(document, '~news', PEER)
        assert db.session.get(File, feed.image_id).source_url == f'https://{PEER}/first.png'

    def test_image_as_a_bare_string_creates_no_file(self, app, db_session, http_mock):
        """The shape the icon block accepts and this one does not: neither
        isinstance test matches, so image_entry stays None."""
        _peer_with_one_owner(http_mock)
        document = _owned_feed(fields={'image': f'https://{PEER}/c.png'})
        feed = actor_json_to_model(document, '~news', PEER)
        assert feed.image_id is None
        assert db.session.query(File).count() == 0

    def test_null_image_creates_no_file(self, app, db_session, http_mock):
        _peer_with_one_owner(http_mock)
        document = _owned_feed(fields={'image': None})
        feed = actor_json_to_model(document, '~news', PEER)
        assert feed.image_id is None

    def test_absent_image_creates_no_file(self, app, db_session, http_mock):
        _peer_with_one_owner(http_mock)
        feed = actor_json_to_model(_owned_feed(), '~news', PEER)
        assert feed.image_id is None


class TestRemoteImageResizing:
    """`if feed.icon_id: make_image_sizes(...)` and the matching image guard.

    Like the Group branch and unlike the Person/Service branch, these two
    guards do NOT consult get_setting('cache_remote_images_locally', True) -- a
    remote feed's icon and banner are always sent for resizing.

    make_image_sizes runs inline under this harness (Celery is eager), and the
    only thing it does that a test can see is fetch the image's source_url. So
    these tests register those urls in http_mock: assert_all_called=True means
    the test fails if the fetch never happens, which is what proves the guard
    was taken. A 404 is served so make_image_sizes_async stops there instead of
    resizing and writing files.

    The false side of both guards is exercised by every test above whose
    document has no icon or no image.

    Mutation that fails these: deleting either `if` -- the registered route is
    then never called and http_mock fails the test at teardown.
    """

    def test_icon_is_sent_for_resizing(self, app, db_session, http_mock):
        _peer_with_one_owner(http_mock)
        http_mock.get(f'https://{PEER}/a.png').respond(404)
        document = _owned_feed(fields={'icon': {'url': f'https://{PEER}/a.png'}})
        feed = actor_json_to_model(document, '~news', PEER)
        assert feed.icon_id is not None

    def test_banner_is_sent_for_resizing(self, app, db_session, http_mock):
        _peer_with_one_owner(http_mock)
        http_mock.get(f'https://{PEER}/c.png').respond(404)
        document = _owned_feed(fields={'image': {'url': f'https://{PEER}/c.png'}})
        feed = actor_json_to_model(document, '~news', PEER)
        assert feed.image_id is not None


class TestChildFeeds:
    """`if 'childFeeds' in activity_json: for child_feed in ...:
    populate_child_feed(feed.id, child_feed)`.

    populate_child_feed runs its worker inline here (Celery is eager), and the
    worker resolves the child through app.feed.util.search_for_feed with a
    `~name@server` address. A Feed row already carrying that ap_id short-cuts
    that lookup before any webfinger request, which is why the child is
    pre-created; the observable effect is the child's parent_feed_id.

    The worker ends with `db.session.remove()` in a finally, which discards the
    scoped session the call was using. Anything the caller still holds is
    detached afterwards, so the assertions below re-query rather than reading
    attributes off the returned object.

    Mutation that fails test_a_child_feed_is_linked_to_its_parent: deleting the
    `if`, which stops the link being made. The absent side is exercised by
    every other test in this file.
    """

    def test_a_child_feed_is_linked_to_its_parent(self, app, db_session, http_mock):
        instance = _peer_instance()
        owner = _owner(instance)
        child = Feed(name='childfeed', title='Child', instance_id=instance.id,
                     ap_id=f'childfeed@{PEER}', ap_domain=PEER,
                     ap_profile_id=f'https://{PEER}/f/childfeed',
                     ap_public_url=f'https://{PEER}/f/childfeed',
                     ap_fetched_at=utcnow())
        db.session.add(child)
        db.session.commit()
        child_id = child.id
        _register_owners(http_mock, [owner.ap_profile_id])
        _register_following(http_mock)
        document = _owned_feed(fields={'childFeeds': [f'https://{PEER}/f/childfeed']})

        actor_json_to_model(document, '~news', PEER)

        parent = db.session.query(Feed).filter_by(ap_profile_id=_feed_id()).one()
        assert db.session.get(Feed, child_id).parent_feed_id == parent.id

    def test_an_empty_child_feed_list_links_nothing(self, app, db_session, http_mock):
        """The `in` operand true, the loop body never entered."""
        _peer_with_one_owner(http_mock)
        document = _owned_feed(fields={'childFeeds': []})
        feed = actor_json_to_model(document, '~news', PEER)
        assert feed is not None
        assert db.session.query(Feed).filter(Feed.parent_feed_id.isnot(None)).count() == 0

    def test_no_child_feeds_key_links_nothing(self, app, db_session, http_mock):
        _peer_with_one_owner(http_mock)
        feed = actor_json_to_model(_owned_feed(), '~news', PEER)
        assert feed is not None
        assert db.session.query(Feed).filter(Feed.parent_feed_id.isnot(None)).count() == 0


class TestFeedRefetchAfterCommit:
    """`feed = db.session.query(Feed).filter_by(ap_profile_id=...).first();
    if feed:` -- the re-fetch that runs between the FeedMember inserts and the
    FeedItem inserts.

    The true arm is taken by every creating test in this file. The FALSE arm is
    unreachable: the query re-reads a row this same session committed moments
    earlier by the same unique ap_profile_id, so `first()` cannot come back
    None. Nothing in this file pretends to reach it, and the very next
    statement (`if feed.icon_id:`) would raise AttributeError if it ever did --
    the guard protects nothing it goes on to use.

    That unreachable arm is the one residue in this branch's branch coverage,
    and it is documented as such rather than pragma'd away.

    The test below is what makes the re-fetch itself observable: it pins that
    the object the function returns is the row in the database, carrying the
    FeedItem-driven num_communities the re-fetched instance accumulated.
    """

    def test_the_returned_feed_is_the_committed_row(self, app, db_session, http_mock):
        instance = _peer_instance()
        owner = _owner(instance)
        community = _remote_community(instance, 'memes')
        _register_owners(http_mock, [owner.ap_profile_id])
        _register_following(http_mock, [community.ap_profile_id])

        feed = actor_json_to_model(_owned_feed(), '~news', PEER)

        stored = db.session.query(Feed).filter_by(ap_profile_id=_feed_id()).one()
        assert feed.id == stored.id
        assert stored.num_communities == 1


class TestConcurrentInsert:
    """`except IntegrityError: db.session.rollback(); return ...one()`.

    The handler is only reachable when a second writer commits the same
    ap_profile_id AFTER this call's early-return lookup found nothing and
    BEFORE its own commit. That window is real but narrow, so the test opens it
    deliberately rather than waiting for it.

    The seam is find_instance_id: `instance_id=find_instance_id(server)` is
    evaluated while the Feed() arguments are built, after the early-return
    lookup has already run and before the insert. Substituting a function that
    commits the rival row and then delegates to the real one puts a genuine,
    already-committed duplicate in the database at exactly the right moment,
    and the collision that follows is a real Postgres unique violation on
    ap_profile_id rather than a raised stand-in.

    Nothing here asserts on the substitution. The assertions are that the row
    which came back is the rival that won -- identified by a title the peer's
    document does not contain, so a newly built row could not carry it -- and
    that the table holds one row, not two. The handler returns immediately, so
    no FeedMember is written either, which the last assertion pins.

    Mutation that fails this: deleting the handler (the IntegrityError escapes
    to the caller), or dropping the db.session.rollback() before the re-query
    (the session is left in a failed transaction and the .one() raises).
    """

    def test_a_rival_commit_during_the_call_returns_the_row_that_won(
            self, app, db_session, http_mock, monkeypatch):
        instance = _peer_instance()
        owner = _owner(instance)
        _register_owners(http_mock, [owner.ap_profile_id])
        _register_following(http_mock)
        document = _owned_feed()
        real_find_instance_id = activitypub_util.find_instance_id

        def commit_the_rival_row_first(server):
            instance_id = real_find_instance_id(server)
            rival = Feed(name='rivalnews', title='the row that won',
                         instance_id=instance.id, ap_domain=PEER,
                         ap_profile_id=document['id'].lower(),
                         ap_public_url=document['id'])
            db.session.add(rival)
            db.session.commit()
            return instance_id

        monkeypatch.setattr(activitypub_util, 'find_instance_id',
                            commit_the_rival_row_first)

        result = actor_json_to_model(document, '~news', PEER)

        assert result is not None
        assert result.title == 'the row that won'
        assert result.ap_profile_id == document['id'].lower()
        assert db.session.query(Feed).count() == 1
        assert db.session.query(FeedMember).count() == 0
