"""actor_json_to_model's `Feed` branch, which turns a peer's actor document
into a Feed row.

The `Person`/`Service` branch and the two guards that run before the type
dispatch (`'type' not in activity_json` and the id-host-versus-server
comparison)
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
             which ends in `else ''`, and unlike the Group branch's, which was
             given that same tail). A document carrying neither key is refused
             by the branch's except KeyError. Pinned by TestInboxResolution.
  following  read unconditionally by the get_request that fetches the feed's
             /following collection, which runs BEFORE the Feed() call, and
             refused there by a handler of its own. Pinned by
             TestRequiredFieldsMissing.

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

That prints `If total: 31  IfExp: 9  For: 5`. Two of the thirty-one are not
optional-field guards -- the type dispatch itself (`== 'Feed'`) and the
`if feed:` early return for a feed already in the database -- so the branch
holds **9 conditional expressions + 29 `if` statements = 38 conditional
sites**, or 40 counting the dispatch and the early return. Both of those two
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

and twenty-nine `if` statements (the two excluded above are not in this list):

    'attributedTo' ... and isinstance(attributedTo, str)   -> owners_url
    'moderators' in activity_json                          (elif; else None)
    'sensitive' ... and sensitive and not site.enable_nsfw (returns None)
    'nsfl' ... and nsfl and not site.enable_nsfl           (returns None)
    owners_url is None                       (the no-owners-collection refusal)
    owners_data.status_code == 200
      owner_user is None                         (the owners-collection skip)
    not owner_users                                (the empty-owners refusal)
    following_data.status_code == 200
      community is None                        (the /following skip guard)
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
      isinstance(childFeeds, list)   (else the value is ignored and logged)

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

Every test also builds the peer's Instance row first with peer_instance, for
the same reason the Group file does: otherwise find_instance_id inserts a
sparse Instance and calls new_instance_profile, which fetches the peer's
nodeinfo.

FINDINGS pinned by this file -- each has its own class docstring saying more.
Those still marked FIXED are kept in the list so the pinning tests that used
to record the defect can be traced back to it:

  1. FIXED, and so no longer a finding: the Feed branch had no `except
     KeyError` at all. It now has two, one on the /following fetch and one on
     the Feed() call, and a malformed document is refused with None instead of
     raising. What remains of the asymmetry is the inbox expression, which
     still has no empty-string fallback where Person's and Group's both do, so
     a document with neither 'endpoints' nor 'inbox' is refused here and
     accepted there. TestRequiredFieldsMissing and TestInboxResolution.
  2. FIXED, and so no longer a finding: `owner_users[0].id` used to be
     unguarded. An owners collection that does not return 200, or returns 200
     with an empty orderedItems, raised IndexError there; an entry
     find_actor_or_create rejects put None in the list and raised
     AttributeError. The rejected entry is now skipped, and an owners list with
     nothing left in it is refused with None before anything is written.
     TestOwnersCollection.
  3. FIXED, and so no longer a finding: an entry of the /following collection
     that find_actor_or_create rejects used to reach `c.id` as None, after the
     Feed had been committed. It is now skipped and logged.
     TestFollowingCollection.
  4. FIXED, and so no longer a finding: a feed with neither attributedTo nor
     moderators used to reach get_request(None), and what came out depended on
     DEBUG -- httpx.HTTPError with it off, TypeError with it on. The None is
     now refused before the call, identically in both modes. TestOwnersUrl.
  5. `ap_following_url=... if 'following' in activity_json else None` can never
     take its else arm: the same key is read unconditionally, earlier, by the
     get_request that fetches the /following collection. TestScalarOptionalFields.
  6. FIXED, and so no longer a finding: `for child_feed in
     activity_json['childFeeds']` was guarded only by the `in` test, and three
     shapes of value got through it. `null` (or any other scalar) raised
     TypeError after the Feed, FeedMember and FeedItem commits. A string
     iterated its own characters and fed each one to populate_child_feed
     WITHOUT raising. A mapping iterated its keys, also without raising, so a
     child feed sent as an object key was linked as if it had been sent in a
     list. The value must now be a list; anything else is ignored and logged.
     TestChildFeeds.
"""
from datetime import datetime

import httpx
import pytest

from app import db
from app.activitypub import util as activitypub_util
from app.activitypub.util import actor_json_to_model
from app.models import Community, Feed, FeedItem, FeedMember, File, User, utcnow
from tests.factories import make_user, peer_actor_json, peer_instance

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


def _child_feed(instance, name='childfeed'):
    """A Feed the childFeeds loop can resolve without any HTTP.

    populate_child_feed_worker hands `~<name>@<server>` to search_for_feed,
    which short-cuts on `Feed.ap_id == '<name>@<server>'` before it reaches
    webfinger. Returns the id rather than the object: the worker ends in
    `db.session.remove()`, which detaches everything the caller still holds.
    """
    child = Feed(name=name, title='Child', instance_id=instance.id,
                 ap_id=f'{name}@{PEER}', ap_domain=PEER,
                 ap_profile_id=f'https://{PEER}/f/{name}',
                 ap_public_url=f'https://{PEER}/f/{name}',
                 ap_fetched_at=utcnow())
    db.session.add(child)
    db.session.commit()
    return child.id


def _single_character_feed(instance, character):
    """A decoy Feed carrying the ap_id that iterating a childFeeds STRING
    resolves one of its characters to.

    extract_domain_and_actor('a') returns ('', 'a'), so the address
    search_for_feed receives is '~a@' and the ap_id it short-cuts on is 'a@'.
    The decoy is what makes the reparenting a bare string causes into a row
    change a test can read back: without it the character falls through to
    webfinger, whose failure path sleeps for three to ten seconds per attempt.
    """
    decoy = Feed(name='decoy', title='Decoy', instance_id=instance.id,
                 ap_id=f'{character}@', ap_domain=PEER,
                 ap_profile_id=f'https://{PEER}/f/decoy',
                 ap_public_url=f'https://{PEER}/f/decoy',
                 ap_fetched_at=utcnow())
    db.session.add(decoy)
    db.session.commit()
    return decoy.id


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
    instance = peer_instance(PEER)
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
        instance = peer_instance(PEER)
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
        peer_instance(PEER)
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
        still matches the stored row. Only the path is varied, to keep this
        test about the lookup: the guard ahead of it lowercases the host on
        both sides, so varying the host would exercise that guard and not this
        lookup. The Person file's
        test_upper_cased_host_in_the_id_is_accepted covers the host.

        'following' is stripped so a failed match cannot masquerade as a hit --
        the rebuild is refused at the /following fetch and returns None, which
        has no `.id`.
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

    FINDING (4), FIXED and so no longer a finding -- the third arm used not to
    be a graceful default. owners_url = None went straight into get_request,
    and what happened next depended on the app's DEBUG setting:
    is_invalid_get_request_uri short-circuits to False under DEBUG, so with
    DEBUG off get_request refused the uri and raised
    httpx.HTTPError("HTTPError: invalid uri"), while with DEBUG on it got past
    that check and the `'washingtonpost.com' in uri` membership test raised
    TypeError instead. The branch now refuses a None owners_url itself, before
    the call, and returns None. test_neither_attributed_to_nor_moderators_is_
    refused runs under both DEBUG settings and asserts the same outcome from
    each, which is the property the old behaviour did not have.

    Mutations:

    - dropping the isinstance operand, which would put the list itself into
      owners_url and then into ap_moderators_url: fails
      test_attributed_to_that_is_not_a_string_is_refused, which would then get
      a Feed back instead of None (and, with no route registered for a list's
      worth of nonsense, an unmocked request).
    - deleting the `if owners_url is None: return None` guard, or narrowing it
      to a condition no document meets: both of the refusal tests below get an
      exception out instead of None.
    - broadening that guard to refuse unconditionally: every other test in this
      file that expects a Feed back gets None.
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

    def test_attributed_to_that_is_not_a_string_is_refused(
            self, app, db_session):
        """attributedTo present but not a string, and no moderators: the else
        arm is taken and owners_url is None, so the document is refused. No
        http_mock route is registered because no HTTP request is ever made --
        the guard runs before the fetch."""
        peer_instance(PEER)
        document = _feed(fields={'attributedTo': [{'id': f'https://{PEER}/u/x'}]})
        assert actor_json_to_model(document, '~news', PEER) is None
        assert db.session.query(Feed).count() == 0

    @pytest.mark.parametrize('debug', [False, True])
    def test_neither_attributed_to_nor_moderators_is_refused(
            self, app, db_session, monkeypatch, debug):
        """Refused the same way whether or not the app is in DEBUG.

        That parametrisation is the point of this test rather than decoration.
        The behaviour this replaced was DEBUG-dependent -- httpx.HTTPError with
        DEBUG off, TypeError with it on -- because is_invalid_get_request_uri
        short-circuits to False under DEBUG and let the None travel one line
        further into get_request. A guard in actor_json_to_model itself is
        reached before either of those, so both settings now return None.

        monkeypatch.setitem is what restores the setting: the app fixture is
        session-scoped, and current_app.debug reads config['DEBUG'] live.
        is_invalid_get_request_uri is memoized, but the test config's cache is
        a NullCache, so the memoization cannot carry one setting's answer into
        the other's run.
        """
        monkeypatch.setitem(app.config, 'DEBUG', debug)
        peer_instance(PEER)
        assert actor_json_to_model(_feed(), '~news', PEER) is None
        assert db.session.query(Feed).count() == 0

    def test_a_null_moderators_is_refused(self, app, db_session):
        """`moderators` present with a null value.

        This reaches the refusal through the SECOND arm, not the third: the
        elif tests only `'moderators' in activity_json`, which a null value
        satisfies, so owners_url is assigned None by the elif and the else is
        never entered. The guard's own comment names this case as one of the
        two that leave owners_url None; nothing pinned it until here.

        That the elif arm is the one taken was confirmed by mutation rather
        than by reading: making the elif assign a non-None sentinel instead of
        the document's value fails this test (the sentinel is fetched, and
        http_mock has no route for it) while
        test_neither_attributed_to_nor_moderators_is_refused, which reaches
        the same refusal through the else, keeps passing.

        No http_mock route is registered because the guard runs before any
        fetch, exactly as in the two refusal tests above.
        """
        peer_instance(PEER)
        document = _feed(fields={'moderators': None})
        assert actor_json_to_model(document, '~news', PEER) is None
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
        peer_instance(PEER)
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
        peer_instance(PEER)
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

    FINDING (2), FIXED and so no longer a finding -- `user_id=owner_users[0].id`
    used to index this list unguarded. Three different documents left nothing
    usable in it and all three crashed there: a moderators collection that does
    not answer 200 and one that answers 200 with an empty orderedItems both
    left it empty and raised IndexError, and an entry find_actor_or_create
    rejects put the None it returns into the list unchecked and raised
    AttributeError. The rejected entry is now skipped and logged, and an owners
    list left with nothing in it is refused with None.

    All three crashed BEFORE the `db.session.commit()` that writes the Feed, so
    refusing leaves no row rather than half of one. That is not assumed here:
    every one of the three tests below asserted `Feed.count() == 0` alongside
    the exception before the fix and asserts it alongside the None after, and
    the assertion held both times. The same is true of the FeedMember rows,
    which are written after that commit and so were never reached at all.

    The rejected-entry tests use the Public collection URI, which
    validate_remote_actor refuses by name -- the cheapest rejection available,
    and one that needs no HTTP and no BannedInstances row.

    Mutations:

    - replacing the status guard with `True`: reaches .json() on a body that is
      not JSON, failing test_a_non_200_owners_collection_is_refused. That kill
      depends on the 404 body being plain text -- see _register_owners, where
      the first version of this file served an empty JSON collection instead
      and the mutation SURVIVED.
    - replacing the status guard with `False`: no owner is ever appended, so
      test_two_owners_become_two_feed_members gets None instead of a Feed.
    - deleting the `if owner_user is None: continue` skip: the None goes back
      into the list and test_an_owner_the_resolver_rejects_is_skipped_and_the_
      rest_owns_the_feed gets AttributeError instead of a Feed.
    - broadening that skip to `continue` unconditionally: every owner is
      dropped, so test_the_first_owner_becomes_the_feeds_user gets None.
    - deleting the `if not owner_users: return None` refusal, or narrowing it to
      a condition no empty list meets: the three refusal tests stop getting
      None. What they get instead is respx's AllMockedAssertionError for the
      unregistered /following route, which the mutated code reaches before it
      reaches the IndexError the unguarded index used to raise -- the refusal
      sits ahead of that second fetch, which is exactly what the missing route
      pins.
    - broadening that refusal to return unconditionally: every test in this
      file that expects a Feed gets None.

    Counts, each from running one mutation at a time against this file: the two
    directions of the skip fail 2 tests and 56, the two of the refusal fail 3
    and 56.

    The two guards are separately pinned, which is why the rejected entry
    appears twice below -- once alongside an owner that does resolve, where
    only the skip decides the outcome, and once alone, where only the refusal
    does.

    None of the three refusal tests registers a /following route. That is the
    assertion, not an omission: http_mock's assert_all_called would fail the
    test if the route existed and went uncalled, so its absence pins that the
    refusal happens before the second remote fetch rather than after it.
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
        instance = peer_instance(PEER)
        first = _owner(instance, 'firstowner')
        second = _owner(instance, 'secondowner')
        _register_owners(http_mock, [first.ap_profile_id, second.ap_profile_id])
        _register_following(http_mock)

        feed = actor_json_to_model(_owned_feed(), '~news', PEER)

        assert feed.user_id == first.id
        members = db.session.query(FeedMember).filter_by(feed_id=feed.id).all()
        assert sorted(m.user_id for m in members) == sorted([first.id, second.id])
        assert all(m.is_owner is True for m in members)

    def test_an_owner_the_resolver_rejects_is_skipped_and_the_rest_owns_the_feed(
            self, app, db_session, http_mock):
        """find_actor_or_create returns None for the Public collection URI. It
        is skipped, and the owner that does resolve becomes the feed's user and
        its only FeedMember -- so this test turns on the skip alone, with the
        empty-list refusal never reached."""
        instance = peer_instance(PEER)
        owner = _owner(instance)
        _register_owners(http_mock, ['https://www.w3.org/ns/activitystreams#Public',
                                     owner.ap_profile_id])
        _register_following(http_mock)

        feed = actor_json_to_model(_owned_feed(), '~news', PEER)

        assert feed.user_id == owner.id
        members = db.session.query(FeedMember).filter_by(feed_id=feed.id).all()
        assert [m.user_id for m in members] == [owner.id]

    def test_a_non_200_owners_collection_is_refused(
            self, app, db_session, http_mock):
        instance = peer_instance(PEER)
        _owner(instance)
        _register_owners(http_mock, [], status=404)

        assert actor_json_to_model(_owned_feed(), '~news', PEER) is None
        assert db.session.query(Feed).count() == 0
        assert db.session.query(FeedMember).count() == 0

    def test_an_empty_owners_collection_is_refused(
            self, app, db_session, http_mock):
        instance = peer_instance(PEER)
        _owner(instance)
        _register_owners(http_mock, [])

        assert actor_json_to_model(_owned_feed(), '~news', PEER) is None
        assert db.session.query(Feed).count() == 0
        assert db.session.query(FeedMember).count() == 0

    def test_an_owners_collection_of_only_rejected_entries_is_refused(
            self, app, db_session, http_mock):
        """The skip empties the list, and the refusal then turns that into a
        None -- the two guards in series."""
        instance = peer_instance(PEER)
        _owner(instance)
        _register_owners(http_mock, ['https://www.w3.org/ns/activitystreams#Public'])

        assert actor_json_to_model(_owned_feed(), '~news', PEER) is None
        assert db.session.query(Feed).count() == 0
        assert db.session.query(FeedMember).count() == 0


class TestFollowingCollection:
    """`following_data = get_request(activity_json['following'], ...)`, the
    `if following_data.status_code == 200:` guard, and the FeedItem rows
    written for each community in the collection.

    num_communities is set to 0 in the constructor and incremented once per
    FeedItem, so the count on the returned row is what says how many were
    linked.

    FIXED (was FINDING (3)) -- an entry find_actor_or_create rejects used to be
    appended as None and to reach `c.id` in the FeedItem loop. That crash
    landed AFTER the feed had been committed, so the peer was left with a
    persisted Feed, no FeedItems, and an exception at the caller -- a
    partially applied ingest rather than a clean rejection. The resolver's
    return is now tested before the append and a rejected entry is skipped and
    logged, see
    test_a_followed_community_the_resolver_rejects_is_skipped_and_the_rest_link.

    The owners loop above still has the unguarded shape (FINDING (2), out of
    this task's scope); the two are no longer the same.

    Mutation that fails test_a_non_200_following_collection_links_nothing:
    replacing the status guard with `True`, which reaches .json() on a body
    that is not JSON -- again dependent on _register_following serving plain
    text rather than an empty JSON collection. Mutation that fails
    test_each_followed_community_becomes_a_feed_item: replacing the guard with
    `False`, which links nothing.
    """

    def test_each_followed_community_becomes_a_feed_item(
            self, app, db_session, http_mock):
        instance = peer_instance(PEER)
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
        instance = peer_instance(PEER)
        owner = _owner(instance)
        _remote_community(instance, 'memes')
        _register_owners(http_mock, [owner.ap_profile_id])
        _register_following(http_mock, [], status=410)

        feed = actor_json_to_model(_owned_feed(), '~news', PEER)

        assert feed is not None
        assert db.session.query(FeedItem).count() == 0
        assert feed.num_communities == 0

    def test_a_followed_community_the_resolver_rejects_is_skipped_and_the_rest_link(
            self, app, db_session, http_mock, caplog):
        """The rejected entry is deliberately in the MIDDLE of the collection,
        between two the resolver accepts. The Public collective is the
        rejection: find_actor_or_create returns None for it.

        The counts are what separate 'skipped the bad entry' from 'skipped the
        whole collection': both good communities become FeedItems and
        num_communities is 2, so a guard that dropped everything, or that
        abandoned the loop at the rejected entry, fails here even though
        nothing raised.

        Mutation, both directions, and they are distinct because this guard is
        a `continue` rather than an early return:

        - delete the guard: the None is appended again, the AttributeError on
          `c.id` comes back, and this test fails on the exception.
        - broaden it to `if True:` (or to `if community is not None:`): every
          entry is skipped, nothing raises, and this test fails on the FeedItem
          list and num_communities instead.

        The warning is pinned as well as the rows, because "the skip is
        logged" is half of the argument for skipping rather than refusing the
        document: an entry the peer sent and this instance dropped has to be
        recoverable from the log. The assertion names the entry that was
        dropped, the collection it came from and the reason, so a warning
        reading "error" would not satisfy it. Only one entry is rejected here,
        so the record count is meaningful; the owners collection above has its
        own warning, at its own call site, and this filter excludes it by
        matching on the '/following' wording.
        """
        instance = peer_instance(PEER)
        owner = _owner(instance)
        first = _remote_community(instance, 'memes')
        second = _remote_community(instance, 'news_comm')
        _register_owners(http_mock, [owner.ap_profile_id])
        _register_following(http_mock, [
            first.ap_profile_id,
            'https://www.w3.org/ns/activitystreams#Public',
            second.ap_profile_id,
        ])

        with caplog.at_level('WARNING'):
            feed = actor_json_to_model(_owned_feed(), '~news', PEER)

        assert feed is not None
        assert db.session.query(Feed).count() == 1
        items = db.session.query(FeedItem).filter_by(feed_id=feed.id).all()
        assert sorted(i.community_id for i in items) == sorted([first.id, second.id])
        assert feed.num_communities == 2

        skips = [r for r in caplog.records if '/following' in r.getMessage()]
        assert len(skips) == 1
        assert skips[0].levelname == 'WARNING'
        assert 'https://www.w3.org/ns/activitystreams#Public' in skips[0].getMessage()
        assert _feed_id() in skips[0].getMessage()
        assert 'does not resolve to a community' in skips[0].getMessage()


class TestRequiredFieldsMissing:
    """The Feed branch's two `except KeyError:
    current_app.logger.error(...); return None` handlers, the same handler the
    Person/Service branch wraps its User() call in.

    The branch reads the peer document unconditionally at two points, so it
    takes two handlers rather than one:

      - the get_request that fetches the /following collection, which reads
        activity_json['following'] before the constructor runs;
      - the Feed() constructor itself, for preferredUsername, name, outbox,
        publicKey and -- when 'endpoints' is absent -- inbox.

    Each try holds exactly that one statement, which is what keeps the
    committing calls out of them: find_actor_or_create, in the owners loop and
    in the /following loop, writes User, Community and Instance rows, and both
    loops sit outside the try bodies. Nothing inside either try writes any part
    of the Feed: db.session.add(feed) and its commit, the FeedMember loop and
    the FeedItem loop are all below the second handler. The row-count assertion
    in every test here is what pins that -- it separates "refused cleanly" from
    "wrote something and then returned None".

    Mutation. Deleting a handler and narrowing its exception type are the same
    mutation -- both let the KeyError escape again, and the tests it covers
    fail on the uncaught exception rather than on an assertion.

    Broadening to `except Exception` is a distinct direction, and it is caught
    by the last two tests here rather than by the KeyError tests -- the
    KeyError is still caught under that mutation and every test above still
    passes.

    It used to be caught incidentally, by TestOwnersCollection, whose three
    tests needed IndexError and AttributeError to keep escaping the same
    constructor -- verified by running that mutation against the commit before
    the owners fix, where exactly those three tests failed, and against this
    one, where the whole file passed. Once owner_users became guaranteed
    non-empty and free of Nones, nothing in the suite made anything but a
    KeyError arise inside either try, so fixing that defect silently removed
    the only evidence that either handler was narrow. The /following handler
    was never policed even then: the same broadening applied to it survives at
    the earlier commit too.

    The two tests below restore that evidence deliberately instead of
    incidentally, one per handler, by sending a document that raises a
    non-KeyError inside each `try` and asserting it propagates.
    """

    @pytest.mark.parametrize('missing', ['preferredUsername', 'outbox',
                                         'publicKey'])
    def test_a_missing_constructor_key_is_refused(
            self, app, db_session, http_mock, missing):
        _peer_with_one_owner(http_mock)
        document = _owned_feed(omit=(missing,))
        assert actor_json_to_model(document, '~news', PEER) is None
        assert db.session.query(Feed).count() == 0

    def test_a_feed_with_no_name_is_titled_after_its_actor_name(
            self, app, db_session, http_mock):
        """D1372. `name` used to be in the list above, read as
        `activity_json['name'].strip()` with no guard, so a Feed document
        carrying no `name` was refused outright."""
        _peer_with_one_owner(http_mock)
        document = _owned_feed(omit=('name',))

        feed = actor_json_to_model(document, '~news', PEER)

        assert feed is not None
        assert feed.title == 'news'
        assert feed.name == 'news'

    def test_a_missing_following_is_refused_before_the_following_fetch(
            self, app, db_session, http_mock):
        """Only the owners route is registered. http_mock's assert_all_called
        would fail this test if a /following request were somehow made, so the
        refusal is reached without one, which is what places the read ahead of
        the fetch."""
        instance = peer_instance(PEER)
        owner = _owner(instance)
        _register_owners(http_mock, [owner.ap_profile_id])
        document = _owned_feed(omit=('following',))
        assert actor_json_to_model(document, '~news', PEER) is None
        assert db.session.query(Feed).count() == 0

    def test_a_public_key_that_is_not_an_object_is_refused(
            self, app, db_session, http_mock):
        """CORRECTED BY D1372. This test used to require the TypeError from
        `activity_json['publicKey']['publicKeyPem']` to travel out of
        actor_json_to_model, on the reasoning that a handler swallowing it
        "would report a malformed document and a genuinely broken one
        identically". A `publicKey` that is a string IS a malformed peer
        document, and the document is what it came from -- there is nothing of
        this deployment's in that expression. It is read through
        `public_key_pem` now, which refuses all three of the absent, non-object
        and null-PEM shapes the same way, so this asserts the refusal.

        The Feed row count is asserted for the reason it always was: the
        constructor is the last statement before anything is written, so a
        refusal must leave nothing behind.

        The breadth this test used to police is still policed -- the
        owners-collection tests below require IndexError and AttributeError to
        keep escaping the narrow handler.
        """
        _peer_with_one_owner(http_mock)
        document = _owned_feed(fields={'publicKey': 'not an object'})

        assert actor_json_to_model(document, '~news', PEER) is None
        assert db.session.query(Feed).count() == 0

    def test_a_non_key_error_from_the_following_fetch_is_not_swallowed(
            self, app, db_session, http_mock, monkeypatch):
        """The /following handler is narrow too, and it was never policed at
        all -- the same broadening survives even at the commit before D15's
        fix.

        A `following` whose value is not a string is passed straight to
        get_request, which refuses it and raises httpx.HTTPError from inside
        the handler's `try`. Nothing about that is a KeyError, so it must
        escape.

        DEBUG is pinned because the exception's TYPE depends on it, for the
        same reason FINDING (4) records: with DEBUG off
        is_invalid_get_request_uri reaches `furl(uri)`, whose failure its own
        `except Exception` turns into a True, and get_request raises
        httpx.HTTPError; with DEBUG on that function short-circuits to False
        and the `'washingtonpost.com' in uri` membership test raises TypeError
        one line later instead. Either way a non-KeyError escapes, which is
        the property under test; pinning the setting is what makes the type
        nameable in a `pytest.raises`.

        Only the owners route is registered. http_mock's assert_all_called
        requires it to have been used -- so the owners collection was fetched
        -- while the absence of a /following route is not itself evidence,
        since get_request never reaches respx here.
        """
        monkeypatch.setitem(app.config, 'DEBUG', False)
        instance = peer_instance(PEER)
        owner = _owner(instance)
        _register_owners(http_mock, [owner.ap_profile_id])
        document = _owned_feed(fields={'following': 12345})
        with pytest.raises(httpx.HTTPError):
            actor_json_to_model(document, '~news', PEER)
        assert db.session.query(Feed).count() == 0


class TestInboxResolution:
    """`ap_inbox_url = activity_json['endpoints']['sharedInbox'] if 'endpoints'
    in activity_json else activity_json['inbox']`.

    The else arm has NO further fallback, unlike the Person/Service branch's,
    which ends `else activity_json['inbox'] if 'inbox' in activity_json else
    ''` -- and unlike the Group branch's, which was given that same tail. A
    Feed document carrying neither key is therefore refused outright by the
    branch's except KeyError, where the same document on either of the other
    two branches stores an empty string and yields a row. That asymmetry is
    still open, reported and not fixed, and it is why 'inbox' is in
    peer_actor_json's Feed baseline.

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

    def test_neither_endpoints_nor_inbox_is_refused(
            self, app, db_session, http_mock):
        """The missing fallback makes this a KeyError inside the constructor,
        which the branch's handler turns into a refusal: no Feed comes back and
        no Feed row is written. On the Person and Group branches the same
        document is accepted with an empty ap_inbox_url."""
        _peer_with_one_owner(http_mock)
        document = _owned_feed(omit=('inbox',))
        assert actor_json_to_model(document, '~news', PEER) is None
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
    been refused by the time the constructor runs (pinned by
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


class TestWhitespaceInThePeersNames:
    """`name=activity_json['preferredUsername'].strip()` and
    `title=activity_json['name'].strip()` in the Feed() call.

    Coverage cannot see inside either. Both sit on lines every
    Feed-creating test in this file already executes, so the branch
    reported full statement and branch coverage while both could be deleted
    with the suite still green -- no test fed a padded value, so the
    stripping was asserted nowhere. The Person/Service and Group branches
    have the same two calls; all three branches now carry a test per call
    site, where before only Person's `name` was pinned.
    """

    def test_a_padded_preferred_username_is_stripped_into_the_name_column(
            self, app, db_session, http_mock):
        """Production change that fails this: deleting
        `activity_json['preferredUsername'].strip()`'s `.strip()`, after
        which the Feed's name column holds ' news ' and the equality fails.

        CORRECTED BY D1372. This test used to assert
        `machine_name == ' news '` and called it "pinning present behaviour, not
        endorsing it". `/f/<name>` and `/f/<name>.rss` both look a feed up by
        `machine_name`, so a peer publishing ` news ` gave this instance a feed it
        could not serve at its own address (fact 781). Both columns come from one
        validated name now.
        """
        _peer_with_one_owner(http_mock)
        document = _owned_feed(fields={'preferredUsername': ' news '})
        feed = actor_json_to_model(document, '~news', PEER)
        assert feed.name == 'news'
        assert db.session.query(Feed).one().name == 'news'
        assert feed.machine_name == 'news', \
            'both columns come from one validated name (D1372)'

    def test_a_padded_name_is_stripped_into_the_title_column(
            self, app, db_session, http_mock):
        """The second, textually separate call site. Production change that
        fails this: deleting `activity_json['name'].strip()`'s `.strip()`.

        The padding is a tab and a newline rather than spaces, so a
        `.strip(' ')` narrowing is caught here rather than passing."""
        _peer_with_one_owner(http_mock)
        document = _owned_feed(fields={'name': '\t The News \n'})
        feed = actor_json_to_model(document, '~news', PEER)
        assert feed.title == 'The News'
        assert db.session.query(Feed).one().title == 'The News'


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

    def test_a_null_summary_leaves_the_description_empty(self, app, db_session, http_mock):
        """CORRECTED BY D1372: `is None` before, `== ''` now.

        The constructor used to pass the peer's `summary` straight into
        `description_html`, so `summary: None` put a NULL in the column while an
        ABSENT summary put '' there -- two spellings of the same nothing,
        decided by the peer. The constructor passes '' and the value is derived
        below, so both shapes now give ''.

        The `description_html is not None` operand this test was written for is
        still the thing being exercised: `_as_text(None)` is None, so the block
        below is skipped and the constructor's value is what remains.
        """
        _peer_with_one_owner(http_mock)
        document = _owned_feed(fields={'summary': None})

        feed = actor_json_to_model(document, '~news', PEER)

        assert feed.description_html == ''

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

    def test_image_as_a_bare_string_is_taken(self, app, db_session, http_mock):
        """This used to assert the opposite. The asymmetry it pinned -- `icon`
        accepting a bare url string while `image` dropped it -- was drift rather
        than a decision, and sub-project 117 gave both keys one reading
        (`image_url_from`), which also stopped four spellings of the same value
        raising inside the three refresh tasks.

        Its twin in tests/test_ap_actor_json_group.py moved with it."""
        _peer_with_one_owner(http_mock)
        document = _owned_feed(fields={'image': f'https://{PEER}/c.png'})
        feed = actor_json_to_model(document, '~news', PEER)
        assert feed.image.source_url == f'https://{PEER}/c.png'

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

    The `isinstance(activity_json['childFeeds'], list)` test inside that `if`
    is a fix, not an original guard -- see the three tests at the end of this
    class. Only ONE of the three malformed values it turns away used to raise;
    the other two were ingested silently, which is why those two are written as
    characterisation tests of what the code did rather than as flipped
    assertions about an exception.
    """

    def test_a_child_feed_is_linked_to_its_parent(self, app, db_session, http_mock):
        instance = peer_instance(PEER)
        owner = _owner(instance)
        child_id = _child_feed(instance)
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

    def test_null_child_feeds_leaves_the_feed_fully_ingested(self, app, db_session, http_mock, caplog):
        """FIXED, and the loudest of the three -- `childFeeds: null` passed the
        `in` test and raised `TypeError: 'NoneType' object is not iterable` out
        of actor_json_to_model, after the Feed, a commit per FeedMember and a
        commit per FeedItem. The caller got an exception and could not know any
        of that had been written.

        The FeedMember and FeedItem counts are the point: they are the rows
        that used to survive the raise with nobody to record them, and they
        must still be there now that nothing raises. A fix that refused the
        whole document instead of ignoring one key would fail on all three
        counts.

        The warning is pinned here, on the null case, because this guard drops
        the peer's whole `childFeeds` value rather than one entry of it, and
        the two silent cases below it (a string and a mapping) would otherwise
        stay silent in exactly the way the guard exists to end. The assertion
        names the key, the document and the type that was refused -- the type
        in particular, because 'NoneType' versus 'str' versus 'dict' is the
        only thing distinguishing the three cases in an operator's log.
        Deleting the logger.warning call fails this test and nothing else: the
        string and mapping tests assert on rows only.
        """
        instance = peer_instance(PEER)
        owner = _owner(instance)
        community = _remote_community(instance, 'memes')
        _register_owners(http_mock, [owner.ap_profile_id])
        _register_following(http_mock, [community.ap_profile_id])
        document = _owned_feed(fields={'childFeeds': None})

        with caplog.at_level('WARNING'):
            feed = actor_json_to_model(document, '~news', PEER)

        assert feed is not None
        assert db.session.query(Feed).filter_by(ap_profile_id=_feed_id()).count() == 1
        assert db.session.query(FeedMember).count() == 1
        assert db.session.query(FeedItem).count() == 1
        assert db.session.query(Feed).filter(Feed.parent_feed_id.isnot(None)).count() == 0

        ignored = [r for r in caplog.records if "'childFeeds'" in r.getMessage()]
        assert len(ignored) == 1
        assert ignored[0].levelname == 'WARNING'
        assert _feed_id() in ignored[0].getMessage()
        assert 'it is a NoneType, not a list' in ignored[0].getMessage()
        assert 'no child feed is linked' in ignored[0].getMessage()

    def test_a_string_of_child_feeds_links_nothing(self, app, db_session, http_mock):
        """FIXED, and this one never raised -- which is why it needed a
        characterisation test rather than a flipped one.

        A string passes the `in` test and is iterable, so the loop iterated its
        CHARACTERS and handed each single character to populate_child_feed.
        extract_domain_and_actor('a') yields ('', 'a'), search_for_feed is
        asked for '~a@', and any feed whose ap_id is 'a@' is reparented onto
        the feed being ingested. The decoy this test pre-creates is exactly
        such a feed, so before the fix its parent_feed_id came back set: a peer
        could rewrite a feed's parentage by sending a bare string. In
        production populate_child_feed dispatches with .delay(), so what the
        operator saw was N failing celery tasks and an ingest that returned
        normally; only under DEBUG did the failure surface inline.

        The assertions are that the decoy is untouched AND that the feed was
        ingested anyway -- a fix that refused the document would leave the
        decoy untouched too.

        Mutation. This guard WRAPS the loop rather than skipping an entry
        inside it, so its two directions sit the opposite way round from a
        `continue`-shaped guard like the Group branch's tag guard: broadening
        it is what removes its effect, and narrowing it is what removes the
        loop. Both were run, both were killed:

        - broaden to `if True:` -- the guard deleted, the loop unconditional
          again. All three malformed values are iterated: the null test fails
          on the TypeError, and this test and the mapping test fail on a
          parent_feed_id that is set again.
        - narrow to `if False:` -- nothing is ever iterated. This test cannot
          see that, because it asserts on a link NOT being made;
          test_a_child_feed_is_linked_to_its_parent is what fails.

        A partial broadening, `isinstance(..., (list, str, dict))`, is the
        interesting middle: it keeps the null case guarded, so only this test
        and the mapping test fail. It was run too, and it is the direction the
        two silent cases exist to catch -- neither of them would have been
        caught by the null case alone.
        """
        instance = peer_instance(PEER)
        owner = _owner(instance)
        decoy_id = _single_character_feed(instance, 'a')
        _register_owners(http_mock, [owner.ap_profile_id])
        _register_following(http_mock)
        document = _owned_feed(fields={'childFeeds': 'a'})

        feed = actor_json_to_model(document, '~news', PEER)

        assert feed is not None
        assert db.session.query(Feed).filter_by(ap_profile_id=_feed_id()).count() == 1
        assert db.session.get(Feed, decoy_id).parent_feed_id is None

    def test_a_mapping_of_child_feeds_links_nothing(self, app, db_session, http_mock):
        """FIXED, and the quietest of the three -- it never raised either, and
        unlike the string case it LOOKED like it worked.

        A JSON object passes the `in` test and iterates its keys, so a peer
        sending `{"<child feed url>": ...}` instead of `["<child feed url>"]`
        got the child linked as though the document had been well formed. That
        is the case the defect register does not mention at all. Accepting it
        by accident is its own problem: the values were never read, so
        whatever the peer put on the right-hand side was silently discarded,
        and the shape this code accepts drifts away from the one it documents.

        The child feed here is the same one
        test_a_child_feed_is_linked_to_its_parent uses through a list, so the
        pair is a matched set: the list links it, the mapping no longer does.
        """
        instance = peer_instance(PEER)
        owner = _owner(instance)
        child_id = _child_feed(instance)
        _register_owners(http_mock, [owner.ap_profile_id])
        _register_following(http_mock)
        document = _owned_feed(
            fields={'childFeeds': {f'https://{PEER}/f/childfeed': 'whatever'}})

        feed = actor_json_to_model(document, '~news', PEER)

        assert feed is not None
        assert db.session.query(Feed).filter_by(ap_profile_id=_feed_id()).count() == 1
        assert db.session.get(Feed, child_id).parent_feed_id is None


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
        instance = peer_instance(PEER)
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
        instance = peer_instance(PEER)
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


class TestWhenThePeerSaysTheFeedWasCreated:
    """D1347's Feed half. `Feed(created_at=..., last_edit=...)` took the peer's
    `published` and `updated` strings straight into two DateTime columns, so a
    document carrying `published: "whenever"` was

        DataError: (psycopg2.errors.InvalidDatetimeFormat) invalid input syntax
        for type timestamp: "whenever"

    at the commit below the constructor -- the feed was never created, and the
    transaction was poisoned with it. Same shape as D1330 (a poll's endTime) and
    D1340 (a resolved post's published).

    Two columns, asserted separately: a readable `published` must not mask an
    unreadable `updated`.
    """

    @pytest.mark.parametrize('published, updated', [
        ('whenever', '2024-01-01T00:00:00Z'),
        ('2024-01-01T00:00:00Z', 'whenever'),
        ('whenever', 'whenever'),
        ('', ''),
        (5, []),
        ('2024-13-45T99:99:99Z', '2024-13-45T99:99:99Z'),
    ])
    def test_the_feed_is_created_whichever_timestamp_is_unreadable(
            self, app, db_session, http_mock, published, updated):
        _peer_with_one_owner(http_mock)
        document = _owned_feed(fields={'published': published, 'updated': updated})

        feed = actor_json_to_model(document, '~news', PEER)

        assert feed is not None
        assert feed.created_at is not None
        assert feed.last_edit is not None

    def test_readable_timestamps_are_converted_to_utc(self, app, db_session,
                                                      http_mock):
        """Not merely accepted: stored as a raw string, PostgreSQL cast an
        offset-bearing value by DISCARDING the offset, so a peer five hours ahead
        got a row five hours wrong."""
        from datetime import datetime

        _peer_with_one_owner(http_mock)
        document = _owned_feed(fields={'published': '2024-01-01T00:00:00+05:00',
                                       'updated': '2024-06-01T12:00:00+02:00'})

        feed = actor_json_to_model(document, '~news', PEER)

        assert feed.created_at == datetime(2023, 12, 31, 19, 0)
        assert feed.last_edit == datetime(2024, 6, 1, 10, 0)

    def test_a_document_with_neither_timestamp_gets_now(self, app, db_session,
                                                        http_mock):
        from app.models import utcnow

        _peer_with_one_owner(http_mock)
        before = utcnow()

        feed = actor_json_to_model(_owned_feed(), '~news', PEER)

        assert feed.created_at >= before
        assert feed.last_edit >= before


class TestTheActorNameD1372:
    """D1372, the Feed branch. `name`, `machine_name` and `title` all came from
    `preferredUsername`/`name` read by hand, and `machine_name` took the raw
    value while `name` was stripped -- see TestWhitespaceInThePeersNames above.
    tests/test_ap_actor_names.py holds the helper and the other two branches.
    """

    @pytest.mark.parametrize('value', [None, 5, [], {}, True, ['news'], '', '   '])
    def test_an_unusable_preferred_username_is_refused_cleanly(
            self, app, db_session, http_mock, value):
        """Only the absent case reached the `except KeyError`; each of these
        raised AttributeError past it."""
        _peer_with_one_owner(http_mock)
        document = _owned_feed(fields={'preferredUsername': value})

        assert actor_json_to_model(document, '~news', PEER) is None
        assert db.session.query(Feed).count() == 0

    def test_a_name_wider_than_the_columns_is_cut(self, app, db_session, http_mock):
        """`Feed.machine_name` is String(50) where `Feed.name` is String(256), and
        both are written from this one value -- so 50 is the width that fits, and
        a peer publishing a 60-character name was a DataError at the commit
        below. Asserted at 50 rather than 255 because that is the narrower
        column, and the two must agree: `/f/<name>` routes on `machine_name`
        (fact 781)."""
        _peer_with_one_owner(http_mock)
        document = _owned_feed(fields={'preferredUsername': 'n' * 300})

        feed = actor_json_to_model(document, '~news', PEER)

        assert feed.name == 'n' * 50
        assert feed.machine_name == 'n' * 50
        db.session.commit()      # the commit that used to raise DataError

    @pytest.mark.parametrize('value', [5, [], {}, True, ['News'], '   '])
    def test_an_unusable_title_falls_back_to_the_actor_name(
            self, app, db_session, http_mock, value):
        _peer_with_one_owner(http_mock)
        document = _owned_feed(fields={'name': value})

        assert actor_json_to_model(document, '~news', PEER).title == 'news'

    @pytest.mark.parametrize('value', [5, [], {}, True, {'value': 'hi'}])
    def test_an_unusable_summary_never_reaches_the_column(
            self, app, db_session, http_mock, value):
        """D1372's second half. The constructor assigned `activity_json['summary']`
        straight into `description_html`, and the block below only overwrites it
        when `_as_text` accepts the value -- so for each of these the raw object
        stayed on the instance and the caller's commit raised on a value psycopg
        cannot adapt. The constructor passes '' now and the block owns the value.
        """
        _peer_with_one_owner(http_mock)
        document = _owned_feed(fields={'summary': value})

        feed = actor_json_to_model(document, '~news', PEER)

        assert feed.description_html == ''
        db.session.commit()      # the commit that used to raise
