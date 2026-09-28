"""actor_json_to_model turns a peer's actor document into a database row.

This file covers the two guards that run before the type branch, and the
`Person`/`Service` branch that builds a User. The `Group` and `Feed` branches
of the same function are covered elsewhere; every test here therefore asserts
on the User row that came back (or on None), never on the other two.

The peer document itself is built by tests.factories.peer_actor_json, which is
shared with the Group and Feed tests. Its baseline carries only the keys this
branch reads unconditionally -- type, id, preferredUsername and
publicKey.publicKeyPem -- so every optional key is opted into by name and the
"absent" side of each guard is the default, not something a test has to
remember to delete.

Optional-field enumeration
--------------------------

Derived fresh against this checkout, restricted to the Person/Service branch
(from the branch's `elif`-free `if activity_json['type'] == 'Person' or ... ==
'Service':` down to its `return user`, whose line numbers the script prints and
this docstring deliberately does not repeat):

    podman-compose -f compose.test.yaml exec -T -w /app test-runner python -c "
    import ast
    src = open('app/activitypub/util.py').read()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.FunctionDef) and n.name == 'actor_json_to_model':
            func = n
    # the Person/Service branch is func.body's first If whose test names Person
    for stmt in func.body:
        if isinstance(stmt, ast.If) and 'Person' in ast.unparse(stmt.test):
            branch = stmt
    lo, hi = branch.lineno, branch.body[-1].lineno
    ifs    = [s for s in ast.walk(func) if isinstance(s, ast.If)    and lo <= s.lineno <= hi]
    ifexps = [s for s in ast.walk(func) if isinstance(s, ast.IfExp) and lo <= s.lineno <= hi]
    print('If total:', len(ifs), ' IfExp:', len(ifexps))
    for s in sorted(ifs, key=lambda s: s.lineno):
        print('  If   ', ast.unparse(s.test)[:88])
    "

That prints `If total: 18  IfExp: 11`. Two of the eighteen are not optional-field
guards: the type dispatch itself (`== 'Person' or == 'Service'`) and the
`if user:` early return for an actor already in the database. So the branch holds
**11 conditional expressions + 16 optional-field `if` statements = 27 conditional
sites**, or 29 counting the dispatch and the early return. Both of those two are
covered as well, by TestPersonAndService.

(An earlier revision of this docstring said "12 `if` statements", which was
wrong: it was a hand count of the bullet list below, and it both undercounted
the list and omitted `if user:` from it. The figures above come from the command
as printed, not from reading.)

The eleven conditional expressions are in the User() constructor call, and are
the scalar optional fields:

    'name' in activity_json and activity_json['name']  -> title, else None
    'matrixUserId' in activity_json                    -> matrix_user_id, else ''
    'indexable' in activity_json                       -> indexable, else True
    'discoverable' in activity_json                    -> searchable, else True
    'published' in activity_json                       -> created, else utcnow()
    'endpoints' in activity_json                       -> ap_inbox_url from
      'inbox' in activity_json                            endpoints.sharedInbox,
                                                          else inbox, else ''
    'followers' in activity_json                       -> ap_followers_url, else None
    'manuallyApprovesFollowers' in activity_json       -> ap_manually_approves_
                                                          followers, else False
    activity_json['type'] == 'Service'                 -> bot, else False
    'acceptPrivateMessages' in activity_json           -> accept_private_messages,
                                                          else 3

and sixteen `if` statements, which are the block-shaped ones (the two excluded
above, `== 'Person' or == 'Service'` and `if user:`, are not in this list):

    'summary' in activity_json                     (else about_html = '')
      about_html is not None and not about_html.startswith('<')   (PeerTube wrap)
    'source' ... and source['mediaType'] == 'text/markdown'  (else html_to_text)
    user.title and user.title.strip().lower() == '[deleted]'
    'icon' in activity_json and activity_json['icon'] is not None
      isinstance(icon, dict) and 'url' in icon
      isinstance(icon, list) and 'url' in icon[-1]
      isinstance(icon, str)
      icon_entry                                   (the else leaves it None)
    'image' ... isinstance(image, dict) and 'url' in image
    'image' ... isinstance(image, list) and len(image) > 0       (bridgy-fed)
    'attachment' in activity_json and isinstance(attachment, list)
      field_data['type'] == 'PropertyValue'
      '<a ' in field_data['value']
    user.avatar_id and get_setting('cache_remote_images_locally', True)
    user.cover_id and get_setting('cache_remote_images_locally', True)

The first two `if`s in the function body -- `'type' not in activity_json` and
the comparison of the id's host against `server` -- are not optional fields but
guards that return None, and are covered by TestTypeGuard and
TestIdHostMatchesServerGuard below.

Every test builds the peer's Instance row first with peer_instance. Without it
find_instance_id inserts a sparse Instance and then calls new_instance_profile,
which fetches the peer's nodeinfo over HTTP.

The IntegrityError fallback around `db.session.add(user);
db.session.commit()` is a concurrency race -- the only unique constraint the
insert can violate is User.ap_profile_id, and the branch's first statement
returns early when a row already holds that value, so reaching the handler
needs a second writer committing the same ap_profile_id between that lookup
and the commit. TestConcurrentInsert simulates exactly that second writer,
without a pragma and without asserting on the simulation; its docstring says
how.
"""
from datetime import datetime

from app import db
from app.activitypub import util as activitypub_util
from app.activitypub.util import actor_json_to_model
from app.models import Community, File, User, UserExtraField, utcnow
from app.utils import set_setting
from tests.factories import make_user, peer_actor_json, peer_instance

PEER = 'peer.example'


class TestTypeGuard:
    """`if 'type' not in activity_json: return None` -- Akkoma has been seen
    serving an actor document with no type at all.

    Mutation that fails these two together: deleting the guard, which turns the
    first test's document into a KeyError on activity_json['type'] at the
    branch test rather than a clean None. Negating it to `if 'type' in
    activity_json` fails the second, which supplies a type and expects a User.
    """

    def test_document_without_a_type_returns_none(self, app, db_session):
        peer_instance(PEER)
        document = peer_actor_json(name='notype', omit=('type',))
        assert actor_json_to_model(document, 'notype', PEER) is None
        assert db.session.query(User).count() == 0

    def test_document_with_a_type_is_not_rejected_by_this_guard(self, app, db_session):
        peer_instance(PEER)
        result = actor_json_to_model(peer_actor_json(name='hastype'), 'hastype', PEER)
        assert isinstance(result, User)


class TestIdHostMatchesServerGuard:
    """`if host_of(activity_json['id']) != host_of(f'//{server}'): return None`.

    The guard asks whether the id's HOST is the server. It used to ask whether
    the `server` string appeared anywhere in the id, which two shapes of id
    satisfied without living on that host at all: a suffix-extended host
    ('good.example' inside 'good.example.attacker.net') and a query parameter
    ('?ref=good.example' on attacker.net). Both are pinned below, now as
    rejections. That substring shape is the same one that produced a stored
    denial of service earlier in this campaign.

    Severity of what was closed, established from the call sites rather than
    from the shape: `server` is never peer-supplied. Every caller derives it
    locally from the address it is resolving --

      app/activitypub/actor.py's create_actor_from_remote takes it from
      extract_domain_and_actor(url) for a URL, or normalise_actor_string(handle)
      for a handle;
      app/community/util.py's and app/feed/util.py's search_for_* and
      app/user/utils.py's remote user lookup take it from the handle they
      webfinger;
      app/api/alpha/utils/misc.py's get_resolve_object takes it from
      urlparse(query).netloc, and additionally re-enters itself with
      ap_json['id'] as the new query whenever query != ap_json['id'], so by the
      time it calls actor_json_to_model the two already agree exactly.

    So a peer could not choose the `server` it was checked against, and could
    not use this to be admitted as an actor of an instance it does not control.
    What it COULD do is mint a row whose ap_public_url/ap_profile_id point at a
    host that is neither the one PieFed fetched from nor the one recorded in
    ap_id, ap_domain and instance_id -- cross-host actor smuggling that needed
    the fetched peer's cooperation. That split row is what the two rejection
    tests below now assert can no longer be created.

    Both sides of the comparison go through host_of, which is what makes it
    symmetric: `server` is an authority and may carry a port, so comparing it
    raw against a host would reintroduce the same class of mistake.

    Mutation, both directions, each measured against this file:

      deleted, and widened to `if False`  -- 3 failures, the three rejection
        tests. Neither says anything about the acceptance tests, whose
        documents the removed code never refused.
      narrowed to `if True`               -- 42 failures. Every other test in
        this file builds its document on the server's own host, so refusing
        everything takes the whole file down with the acceptance tests.
      the '//' dropped from the server side, `host_of(server)` -- 42 failures,
        the same set. urlparse reads an authority-less string as a path, so
        host_of returns '' for every server and nothing can match it.
      `urlparse(...).netloc` on both sides instead of `hostname` -- 1 failure,
        test_upper_cased_host_in_the_id_is_accepted. netloc keeps the port on
        both sides, so the port test below survives it; netloc does not
        lowercase, which is what the upper-cased test catches.
      `host_of(id) != server.lower()`, comparing a host against a raw
        authority -- 1 failure, and it is the port test below. That mutation
        is the reason that test exists: it is the only one whose server
        carries a port, so it is the only one where a host and an authority
        differ.
    """

    def test_id_on_a_different_host_is_rejected(self, app, db_session):
        peer_instance('good.example')
        document = peer_actor_json(name='alice', server='good.example',
                                   fields={'id': 'https://other.example/u/alice'})
        assert actor_json_to_model(document, 'alice', 'good.example') is None
        assert db.session.query(User).count() == 0

    def test_id_on_the_real_host_is_accepted(self, app, db_session):
        peer_instance('good.example')
        document = peer_actor_json(name='alice', server='good.example')
        user = actor_json_to_model(document, 'alice', 'good.example')
        assert user.ap_profile_id == 'https://good.example/u/alice'
        assert user.ap_domain == 'good.example'

    def test_suffix_extended_host_is_rejected(self, app, db_session):
        """'good.example' is a substring of 'good.example.attacker.net', whose
        real host is a subdomain of attacker.net and not good.example at all.
        The old substring gate admitted it and produced a User whose
        ap_profile_id pointed at attacker.net while its ap_domain and ap_id
        said good.example."""
        peer_instance('good.example')
        document = peer_actor_json(
            name='alice', server='good.example',
            fields={'id': 'https://good.example.attacker.net/u/alice'})
        assert actor_json_to_model(document, 'alice', 'good.example') is None
        assert db.session.query(User).count() == 0

    def test_upper_cased_host_in_the_id_is_accepted(self, app, db_session):
        """A host is case-insensitive, so a peer that publishes its own id with
        an upper-cased host is publishing the right host. The old substring
        gate was case-sensitive and refused it; host_of lowercases, so both
        sides now agree.

        The row keeps the id exactly as published in ap_public_url -- the guard
        normalises only for the comparison -- while ap_profile_id is lowercased
        by the branch itself."""
        peer_instance('good.example')
        document = peer_actor_json(name='alice', server='good.example',
                                   fields={'id': 'https://GOOD.EXAMPLE/u/alice'})
        user = actor_json_to_model(document, 'alice', 'good.example')
        assert user is not None
        assert user.ap_public_url == 'https://GOOD.EXAMPLE/u/alice'
        assert user.ap_profile_id == 'https://good.example/u/alice'

    def test_server_name_only_in_the_query_string_is_rejected(self, app, db_session):
        """The server name appears only in the query string; the real host is
        attacker.net. The old substring gate admitted this too."""
        peer_instance('good.example')
        document = peer_actor_json(
            name='alice', server='good.example',
            fields={'id': 'https://attacker.net/u/x?ref=good.example'})
        assert actor_json_to_model(document, 'alice', 'good.example') is None
        assert db.session.query(User).count() == 0

    def test_server_carrying_a_port_matches_an_id_on_that_port(self, app, db_session):
        """`server` is an authority, not a host, and a development or private
        peer reaches these call sites with its port attached. Both sides go
        through host_of, which drops the port from each, so the two agree.

        This is the test that the '//' prefix and the choice of hostname over
        netloc are both load-bearing for: without the prefix the server side
        parses as a path and yields '', and with netloc the two sides would
        only agree while the port text matched character for character."""
        peer_instance('peer.example:8443')
        document = peer_actor_json(name='alice', server='peer.example:8443')
        user = actor_json_to_model(document, 'alice', 'peer.example:8443')
        assert user is not None
        assert user.ap_public_url == 'https://peer.example:8443/u/alice'


class TestTwoFailedParsesDoNotSatisfyTheGate:
    """D32. `host_of` degrades an unparseable string to `''`, and `'' != ''` is
    False, so an id urlparse refuses compares EQUAL to a server urlparse also
    refuses and the gate accepts.

    The class docstring above discharges this by call site: every caller was
    said to derive `server` locally and non-empty. `create_actor_from_remote`
    is the one that does not -- on its `https://` path it takes `server` from
    `extract_domain_and_actor`, which returns `('', '')` on a `urlparse`
    `ValueError`, then fetches with `actor_address`, a different variable, so
    nothing exercises the empty `server` before it arrives here.

    D32 filed that as UNPROVEN because it required httpx to accept and fetch a
    URL urlparse refuses, and nobody had exhibited one. One exists, and it is
    not exotic -- a bracket or an NFKC-confusable in the USERINFO, which
    urlparse rejects as part of the netloc while httpx strips userinfo and
    keeps the real host:

        urlparse('https://[@banned.example/u/alice')  ValueError: Invalid IPv6 URL
        httpx.URL('https://[@banned.example/u/alice').host  'banned.example'

    So httpx fetches the peer's real server while every urlparse-derived value
    in this codebase is ''. That closes D32's open question affirmatively.

    Two independent things now stop it, and both are wanted. `validate_remote_actor`
    refuses the URL before `create_actor_from_remote` is ever called (the D48
    fix, 2026-08-29). This gate is the second, and it is the one that matters
    if a future caller reaches `actor_json_to_model` without passing the first:
    the gate is the last thing standing between a peer-chosen document and a
    row.

    Production change that fails these: restoring the bare
    `if host_of(id) != host_of(f'//{server}')` without the empty-side refusal.
    """

    def test_an_unparseable_server_refuses_rather_than_matching_anything(self, app, db_session):
        """The exact shape create_actor_from_remote would deliver: server ''
        because extract_domain_and_actor hit the ValueError, and an id the peer
        chose. The id here is WELL FORMED and on a host of the attacker's
        choosing -- the failure does not need a matching malformed id, only an
        empty server, because '' != 'attacker.net' is True and would refuse.
        This test is therefore the control that pins WHY the pair below is the
        dangerous one."""
        peer_instance('good.example')
        document = peer_actor_json(name='alice', server='good.example',
                                   fields={'id': 'https://attacker.net/u/alice'})

        assert actor_json_to_model(document, 'alice', '') is None
        assert db.session.query(User).count() == 0

    def test_an_unparseable_id_and_an_unparseable_server_do_not_match(self, app, db_session):
        """Both sides degrade to '' and compared EQUAL, so the gate accepted
        and a User was minted whose ap_profile_id pointed at a host nothing had
        verified. The peer supplies the id; `server` arrives empty from the
        ValueError. Neither string is a host, and 'neither is a host' is not a
        reason to treat them as the same host."""
        peer_instance('good.example')
        document = peer_actor_json(name='alice', server='good.example',
                                   fields={'id': 'https://[@attacker.net/u/alice'})

        assert actor_json_to_model(document, 'alice', '') is None
        assert db.session.query(User).count() == 0

    def test_an_unparseable_id_is_refused_even_when_the_server_is_good(self, app, db_session):
        """The other asymmetry, and it already held: a good server is a real
        host, so an id that degrades to '' compares unequal and is refused.
        Kept because the fix must not be mistaken for what creates this
        behaviour, and because it is what a mutant that refuses only on an
        empty SERVER would still pass."""
        peer_instance('good.example')
        document = peer_actor_json(name='alice', server='good.example',
                                   fields={'id': 'https://[@good.example/u/alice'})

        assert actor_json_to_model(document, 'alice', 'good.example') is None
        assert db.session.query(User).count() == 0


class TestPersonAndService:
    """`if activity_json['type'] == 'Person' or activity_json['type'] == 'Service'`,
    and the `bot=True if activity_json['type'] == 'Service' else False` inside it.

    Both mutation directions on that type test are exercised here, and the
    broadening direction has TWO shapes, because the dispatch has two siblings:

    - narrowed to `== 'Person'` alone: test_service_document_creates_a_bot_user
      stops getting a User back, because a Service document then falls through
      to the Group branch and dies on activity_json['outbox'].
    - broadened to `!= 'Feed'`: a Group document is built as a User, which
      test_group_document_is_not_handled_here catches by asserting a Community
      comes back.
    - broadened to `!= 'Group'`: a Feed document is built as a User, which
      test_feed_document_is_not_handled_here catches. A bare `True` is caught
      by either.

    Both broadenings are needed. `!= 'Group'` leaves Group dispatch correct and
    so survives every Group test; `!= 'Feed'` leaves Feed dispatch correct and
    survives every Feed test. Naming only one of them would name a mutation the
    other test cannot fail.
    """

    def test_person_document_creates_a_user_that_is_not_a_bot(self, app, db_session):
        peer_instance(PEER)
        user = actor_json_to_model(peer_actor_json(name='alice'), 'alice', PEER)
        assert isinstance(user, User)
        assert user.user_name == 'alice'
        assert user.bot is False
        assert user.email == f'alice@{PEER}'
        assert user.ap_preferred_username == 'alice'

    def test_service_document_creates_a_bot_user(self, app, db_session):
        peer_instance(PEER)
        user = actor_json_to_model(peer_actor_json('Service', name='botty'), 'botty', PEER)
        assert isinstance(user, User)
        assert user.bot is True

    def test_group_document_is_not_handled_here(self, app, db_session):
        """A Group falls to the Group branch, which builds a Community, not a
        User. Broadening the Person/Service test to admit Group (`!= 'Feed'`)
        would make this return a User instead."""
        peer_instance(PEER)
        document = peer_actor_json('Group', name='memes')
        result = actor_json_to_model(document, '!memes', PEER)
        assert isinstance(result, Community)
        assert result.name == 'memes'
        assert db.session.query(User).count() == 0

    def test_feed_document_is_not_handled_here(self, app, db_session, site):
        """A Feed falls to the Feed branch. Broadening the Person/Service test
        to admit Feed (`!= 'Group'`) would build a User out of this document
        instead, which is what the count assertion below catches.

        The document is marked sensitive and the Site has enable_nsfw off, so
        the Feed branch returns None at its own nsfw guard -- before the point
        where it would dereference the owners and following collections over
        HTTP. That keeps this test about dispatch, which is all it claims, and
        leaves the Feed branch's own behaviour to its own tests."""
        peer_instance(PEER)
        document = peer_actor_json('Feed', name='news', fields={'sensitive': True})
        result = actor_json_to_model(document, '~news', PEER)
        assert result is None
        assert db.session.query(User).count() == 0

    def test_existing_user_is_returned_without_creating_a_second(self, app, db_session):
        """The branch's first statement looks the actor up by
        ap_profile_id == activity_json['id'].lower() and returns it. Deleting
        that early return makes the insert collide on the unique
        ap_profile_id."""
        instance = peer_instance(PEER)
        existing = make_user(instance, 'alice')
        document = peer_actor_json(name='alice',
                                   fields={'id': existing.ap_profile_id})
        result = actor_json_to_model(document, 'alice', PEER)
        assert result.id == existing.id
        assert db.session.query(User).count() == 1

    def test_lookup_of_an_existing_user_lowercases_the_id(self, app, db_session):
        """The lookup compares against id.lower(), so a peer that upper-cases
        the path of its own id on a later fetch still matches the stored row.
        Only the path is varied here, to keep this test about the lookup: the
        host guard above lowercases the host on both sides before comparing,
        so an upper-cased host reaches the lookup too and is covered there by
        test_upper_cased_host_in_the_id_is_accepted."""
        instance = peer_instance(PEER)
        existing = make_user(instance, 'alice')
        document = peer_actor_json(name='alice',
                                   fields={'id': f'https://{PEER}/users/ALICE'})
        result = actor_json_to_model(document, 'alice', PEER)
        assert result.id == existing.id
        assert db.session.query(User).count() == 1


class TestConcurrentInsert:
    """`except IntegrityError: db.session.rollback(); return ...one()`.

    The handler is only reachable when a second writer commits the same
    ap_profile_id AFTER this call's early-return lookup found nothing and
    BEFORE its own commit. That window is real but narrow, so the test opens it
    deliberately rather than waiting for it.

    The seam is find_instance_id: `instance_id=find_instance_id(server)` is
    evaluated inside the try block, after the early-return lookup has already
    run and before the insert. Substituting a function that commits the rival
    row and then delegates to the real one puts a genuine, already-committed
    duplicate in the database at exactly the right moment, and the collision
    that follows is a real Postgres unique violation rather than a raised
    stand-in.

    Nothing here asserts on the substitution. The assertions are that the row
    which came back is the rival that won -- identified by a title the peer's
    document does not contain, so a newly built row could not carry it -- and
    that the table holds one row, not two.

    Mutation that fails this: deleting the handler (the IntegrityError escapes
    to the caller), or dropping the db.session.rollback() before the re-query
    (the session is left in a failed transaction and the .one() raises).
    """

    def test_a_rival_commit_during_the_call_returns_the_row_that_won(
            self, app, db_session, monkeypatch):
        instance = peer_instance(PEER)
        document = peer_actor_json(name='alice')
        real_find_instance_id = activitypub_util.find_instance_id

        def commit_the_rival_row_first(server):
            instance_id = real_find_instance_id(server)
            rival = make_user(instance, 'alice')
            rival.ap_profile_id = document['id'].lower()
            rival.title = 'the row that won'
            db.session.commit()
            return instance_id

        monkeypatch.setattr(activitypub_util, 'find_instance_id',
                            commit_the_rival_row_first)
        result = actor_json_to_model(document, 'alice', PEER)

        assert result is not None
        assert result.title == 'the row that won'
        assert result.ap_profile_id == document['id'].lower()
        assert db.session.query(User).count() == 1


class TestRequiredFieldsMissing:
    """`except KeyError: ... return None` around the User() construction.

    Only two keys are read unconditionally there: preferredUsername and
    publicKey.publicKeyPem. Mutation that fails these: narrowing the handler to
    a key that is never missing, or deleting it -- the KeyError then escapes to
    the caller instead of becoming None.
    """

    def test_missing_preferred_username_returns_none(self, app, db_session):
        peer_instance(PEER)
        document = peer_actor_json(name='alice', omit=('preferredUsername',))
        assert actor_json_to_model(document, 'alice', PEER) is None
        assert db.session.query(User).count() == 0

    def test_missing_public_key_returns_none(self, app, db_session):
        peer_instance(PEER)
        document = peer_actor_json(name='alice', omit=('publicKey',))
        assert actor_json_to_model(document, 'alice', PEER) is None
        assert db.session.query(User).count() == 0

    def test_public_key_without_a_pem_returns_none(self, app, db_session):
        peer_instance(PEER)
        document = peer_actor_json(name='alice', fields={'publicKey': {'id': 'x'}})
        assert actor_json_to_model(document, 'alice', PEER) is None
        assert db.session.query(User).count() == 0


class TestScalarOptionalFields:
    """The eleven conditional expressions in the User() call, both ways round.

    The present/absent pair is the point: a suite that always supplies every
    key would leave every `else` of these expressions unexecuted while branch
    coverage reported the whole constructor as covered.

    Mutation that fails test_every_scalar_optional_absent_takes_its_default:
    flipping any one guard to its negation (e.g. `'indexable' not in
    activity_json`) -- the default then stops being applied and the assertion
    on that column fails.
    """

    def test_every_scalar_optional_present_is_copied(self, app, db_session):
        peer_instance(PEER)
        document = peer_actor_json(name='alice', fields={
            'name': '  Alice Liddell  ',
            'matrixUserId': '@alice:matrix.example',
            'indexable': False,
            'discoverable': False,
            'published': '2024-01-02T03:04:05Z',
            'followers': f'https://{PEER}/u/alice/followers',
            'manuallyApprovesFollowers': True,
            'acceptPrivateMessages': 1,
            'endpoints': {'sharedInbox': f'https://{PEER}/inbox'},
        })
        user = actor_json_to_model(document, 'alice', PEER)
        assert user.title == 'Alice Liddell'
        assert user.matrix_user_id == '@alice:matrix.example'
        assert user.indexable is False
        assert user.searchable is False
        assert user.created == datetime(2024, 1, 2, 3, 4, 5)
        assert user.ap_followers_url == f'https://{PEER}/u/alice/followers'
        assert user.ap_manually_approves_followers is True
        assert user.accept_private_messages == 1
        assert user.ap_inbox_url == f'https://{PEER}/inbox'

    def test_every_scalar_optional_absent_takes_its_default(self, app, db_session):
        peer_instance(PEER)
        before = utcnow()
        user = actor_json_to_model(peer_actor_json(name='alice'), 'alice', PEER)
        assert user.title is None
        assert user.matrix_user_id == ''
        assert user.indexable is True
        assert user.searchable is True
        # utcnow(), not the peer's value: distinguishable from the 2024 timestamp
        # the present-side test supplies, which `is not None` would also accept.
        assert user.created > before
        assert user.created <= utcnow()
        assert user.ap_followers_url is None
        assert user.ap_manually_approves_followers is False
        assert user.accept_private_messages == 3
        assert user.ap_inbox_url == ''


class TestWhitespaceInThePeersNames:
    """`user_name=activity_json['preferredUsername'].strip()` and
    `title=activity_json['name'].strip() if ...` in the User() call.

    Only the second of the two was pinned before this class existed, by
    TestScalarOptionalFields's `'name': '  Alice Liddell  '`. The first was
    not: deleting `activity_json['preferredUsername'].strip()`'s `.strip()`
    left the entire suite green, because no test had ever fed a padded
    preferredUsername to any branch of this function. The review that
    prompted these tests recorded the Person branch as the one that pinned
    its stripping properly, and named three tests failing on removal;
    re-derived by mutating each call site on its own against the full
    suite, it is one test for `name` and none at all for
    `preferredUsername`. The test below closes that.
    """

    def test_a_padded_preferred_username_is_stripped_into_the_user_name_column(self, app, db_session):
        """Production change that fails this: deleting
        `activity_json['preferredUsername'].strip()`'s `.strip()`, after
        which user_name holds ' alice ' and the equality fails.

        CORRECTED BY D1372. This test used to assert
        `ap_preferred_username == ' alice '`, and said so as pinning present
        behaviour rather than endorsing it -- "a `.strip()` later added to
        ap_preferred_username would change what the column holds while leaving
        this test green". Both columns are filled from one validated name now, so
        the split is gone. The column is `String(255)` and the raw value was
        written to it unbounded, which was a DataError at commit for a peer
        publishing a longer one.
        """
        peer_instance(PEER)
        document = peer_actor_json(name='alice', fields={'preferredUsername': ' alice '})
        user = actor_json_to_model(document, 'alice', PEER)
        assert user.user_name == 'alice'
        assert db.session.query(User).one().user_name == 'alice'
        assert user.ap_preferred_username == 'alice', \
            'both columns come from one validated name (D1372)'

    def test_present_but_empty_name_falls_back_to_no_title(self, app, db_session):
        """`'name' in activity_json and activity_json['name']` -- the second
        operand is what an empty display name falsifies, and it is the only
        one of the eleven that tests the VALUE rather than the key."""
        peer_instance(PEER)
        document = peer_actor_json(name='alice', fields={'name': ''})
        user = actor_json_to_model(document, 'alice', PEER)
        assert user.title is None


class TestInboxResolution:
    """`endpoints.sharedInbox`, else `inbox`, else ''. Three outcomes, so
    three tests: the middle one is unreachable from either of the others.

    Mutation that fails test_inbox_used_when_endpoints_absent: reordering the
    expression to try 'inbox' first, which would make the first test below
    read the per-actor inbox instead of the shared one.
    """

    def test_shared_inbox_wins_when_endpoints_present(self, app, db_session):
        peer_instance(PEER)
        document = peer_actor_json(name='alice', fields={
            'endpoints': {'sharedInbox': f'https://{PEER}/inbox'},
            'inbox': f'https://{PEER}/u/alice/inbox',
        })
        user = actor_json_to_model(document, 'alice', PEER)
        assert user.ap_inbox_url == f'https://{PEER}/inbox'

    def test_inbox_used_when_endpoints_absent(self, app, db_session):
        peer_instance(PEER)
        document = peer_actor_json(name='alice',
                                   fields={'inbox': f'https://{PEER}/u/alice/inbox'})
        user = actor_json_to_model(document, 'alice', PEER)
        assert user.ap_inbox_url == f'https://{PEER}/u/alice/inbox'

    def test_empty_string_when_neither_present(self, app, db_session):
        peer_instance(PEER)
        user = actor_json_to_model(peer_actor_json(name='alice'), 'alice', PEER)
        assert user.ap_inbox_url == ''


class TestSummaryAndSource:
    """about_html comes from 'summary' (allowlisted), and 'source' overwrites
    both about and about_html when it declares text/markdown.

    Mutation that fails test_plain_text_summary_is_wrapped_in_a_paragraph:
    dropping the `not about_html.startswith('<')` PeerTube wrap, which leaves
    the bare text unwrapped.
    """

    def test_html_summary_is_allowlisted_not_wrapped(self, app, db_session):
        peer_instance(PEER)
        document = peer_actor_json(name='alice',
                                   fields={'summary': '<p>hello <script>x</script></p>'})
        user = actor_json_to_model(document, 'alice', PEER)
        assert '<script>' not in user.about_html
        assert 'hello' in user.about_html
        assert user.about_html.startswith('<p>')

    def test_plain_text_summary_is_wrapped_in_a_paragraph(self, app, db_session):
        peer_instance(PEER)
        document = peer_actor_json(name='alice', fields={'summary': 'just words'})
        user = actor_json_to_model(document, 'alice', PEER)
        assert user.about_html.strip().startswith('<p>')
        assert 'just words' in user.about_html

    def test_null_summary_is_not_wrapped(self, app, db_session):
        """`about_html is not None` guards the wrap; the key is present with a
        null value, which several peers do send. The null reaches
        allowlist_html, which returns '' for it, so the outcome is an empty
        about -- and specifically NOT the '<p>None</p>' that dropping the
        `is not None` half of the guard would produce."""
        peer_instance(PEER)
        document = peer_actor_json(name='alice', fields={'summary': None})
        user = actor_json_to_model(document, 'alice', PEER)
        assert user.about_html == ''
        assert user.about == ''

    def test_absent_summary_gives_an_empty_about(self, app, db_session):
        peer_instance(PEER)
        user = actor_json_to_model(peer_actor_json(name='alice'), 'alice', PEER)
        assert user.about_html == ''
        assert user.about == ''

    def test_markdown_source_overwrites_the_html_derived_about(self, app, db_session):
        peer_instance(PEER)
        document = peer_actor_json(name='alice', fields={
            'summary': '<p>from html</p>',
            'source': {'mediaType': 'text/markdown', 'content': '**from markdown**'},
        })
        user = actor_json_to_model(document, 'alice', PEER)
        assert user.about == '**from markdown**'
        assert '<strong>from markdown</strong>' in user.about_html

    def test_source_with_another_media_type_leaves_about_from_the_html(self, app, db_session):
        peer_instance(PEER)
        document = peer_actor_json(name='alice', fields={
            'summary': '<p>from html</p>',
            'source': {'mediaType': 'text/html', 'content': '**from markdown**'},
        })
        user = actor_json_to_model(document, 'alice', PEER)
        assert user.about.strip() == 'from html'


class TestDeletedTitle:
    """`if user.title and user.title.strip().lower() == '[deleted]': user.title = ''`

    Mutation that fails this: dropping the .lower(), which lets the
    upper-cased spelling through.
    """

    def test_deleted_display_name_is_blanked(self, app, db_session):
        peer_instance(PEER)
        document = peer_actor_json(name='alice', fields={'name': ' [Deleted] '})
        user = actor_json_to_model(document, 'alice', PEER)
        assert user.title == ''

    def test_an_ordinary_display_name_is_kept(self, app, db_session):
        peer_instance(PEER)
        document = peer_actor_json(name='alice', fields={'name': 'Alice'})
        user = actor_json_to_model(document, 'alice', PEER)
        assert user.title == 'Alice'


class TestIcon:
    """The avatar. Four shapes of 'icon' are recognised (dict with url, list
    whose LAST entry has a url, bare string, and nothing else) plus the
    `icon_entry` guard that skips the File when none matched.

    Mutation that fails test_icon_as_a_list_takes_the_last_entry: changing the
    [-1] index to [0], which picks the wrong url.
    """

    def test_icon_as_a_dict(self, app, db_session):
        peer_instance(PEER)
        document = peer_actor_json(name='alice',
                                   fields={'icon': {'url': f'https://{PEER}/a.png'}})
        user = actor_json_to_model(document, 'alice', PEER)
        assert user.avatar_id is not None
        assert db.session.get(File, user.avatar_id).source_url == f'https://{PEER}/a.png'

    def test_icon_as_a_list_takes_the_last_entry(self, app, db_session):
        peer_instance(PEER)
        document = peer_actor_json(name='alice', fields={'icon': [
            {'url': f'https://{PEER}/small.png'},
            {'url': f'https://{PEER}/large.png'},
        ]})
        user = actor_json_to_model(document, 'alice', PEER)
        assert db.session.get(File, user.avatar_id).source_url == f'https://{PEER}/large.png'

    def test_icon_as_a_bare_string(self, app, db_session):
        peer_instance(PEER)
        document = peer_actor_json(name='alice', fields={'icon': f'https://{PEER}/a.png'})
        user = actor_json_to_model(document, 'alice', PEER)
        assert db.session.get(File, user.avatar_id).source_url == f'https://{PEER}/a.png'

    def test_icon_of_an_unrecognised_shape_creates_no_file(self, app, db_session):
        """A dict with no 'url' matches none of the three shapes, so icon_entry
        stays None and the File is never built."""
        peer_instance(PEER)
        document = peer_actor_json(name='alice', fields={'icon': {'mediaType': 'image/png'}})
        user = actor_json_to_model(document, 'alice', PEER)
        assert user.avatar_id is None
        assert db.session.query(File).count() == 0

    def test_null_icon_creates_no_file(self, app, db_session):
        peer_instance(PEER)
        document = peer_actor_json(name='alice', fields={'icon': None})
        user = actor_json_to_model(document, 'alice', PEER)
        assert user.avatar_id is None

    def test_absent_icon_creates_no_file(self, app, db_session):
        peer_instance(PEER)
        user = actor_json_to_model(peer_actor_json(name='alice'), 'alice', PEER)
        assert user.avatar_id is None
        assert db.session.query(File).count() == 0


class TestImage:
    """The cover banner. Only two shapes are recognised here, and unlike the
    icon block above they are written as an if/elif over two separate
    `'image' in activity_json` tests rather than one nested chain -- the list
    form takes entry [0], not [-1].

    Mutation that fails test_image_as_a_list_takes_the_first_entry: changing
    that [0] to [-1].
    """

    def test_image_as_a_dict(self, app, db_session):
        peer_instance(PEER)
        document = peer_actor_json(name='alice',
                                   fields={'image': {'url': f'https://{PEER}/c.png'}})
        user = actor_json_to_model(document, 'alice', PEER)
        assert db.session.get(File, user.cover_id).source_url == f'https://{PEER}/c.png'

    def test_image_as_a_list_takes_the_first_entry(self, app, db_session):
        peer_instance(PEER)
        document = peer_actor_json(name='alice', fields={'image': [
            {'url': f'https://{PEER}/first.png'},
            {'url': f'https://{PEER}/second.png'},
        ]})
        user = actor_json_to_model(document, 'alice', PEER)
        assert db.session.get(File, user.cover_id).source_url == f'https://{PEER}/first.png'

    def test_empty_image_list_creates_no_file(self, app, db_session):
        """`len(activity_json['image']) > 0` -- the empty list matches neither
        arm."""
        peer_instance(PEER)
        document = peer_actor_json(name='alice', fields={'image': []})
        user = actor_json_to_model(document, 'alice', PEER)
        assert user.cover_id is None

    def test_null_image_creates_no_file(self, app, db_session):
        peer_instance(PEER)
        document = peer_actor_json(name='alice', fields={'image': None})
        user = actor_json_to_model(document, 'alice', PEER)
        assert user.cover_id is None

    def test_absent_image_creates_no_file(self, app, db_session):
        peer_instance(PEER)
        user = actor_json_to_model(peer_actor_json(name='alice'), 'alice', PEER)
        assert user.cover_id is None


class TestPropertyValueAttachments:
    """`if 'attachment' in activity_json and isinstance(..., list)`, then one
    UserExtraField per entry whose type is PropertyValue.

    Mutation that fails test_non_property_value_attachments_are_skipped:
    dropping the `field_data['type'] == 'PropertyValue'` test, which would
    build an extra field out of an Image attachment that carries no 'name'
    and raise KeyError instead.
    """

    def test_property_value_becomes_an_extra_field(self, app, db_session):
        peer_instance(PEER)
        document = peer_actor_json(name='alice', fields={'attachment': [
            {'type': 'PropertyValue', 'name': '  Website  ', 'value': '  example.org  '},
        ]})
        user = actor_json_to_model(document, 'alice', PEER)
        rows = db.session.query(UserExtraField).filter_by(user_id=user.id).all()
        assert len(rows) == 1
        assert rows[0].label == 'Website'
        assert rows[0].text == 'example.org'

    def test_anchor_valued_field_is_reduced_to_its_href(self, app, db_session):
        """`if '<a ' in field_data['value']` routes the value through
        mastodon_extra_field_link, which returns the first anchor's href."""
        peer_instance(PEER)
        document = peer_actor_json(name='alice', fields={'attachment': [
            {'type': 'PropertyValue', 'name': 'Website',
             'value': '<a href="https://example.org/alice" rel="me">example.org</a>'},
        ]})
        user = actor_json_to_model(document, 'alice', PEER)
        rows = db.session.query(UserExtraField).filter_by(user_id=user.id).all()
        assert rows[0].text == 'https://example.org/alice'

    def test_non_property_value_attachments_are_skipped(self, app, db_session):
        peer_instance(PEER)
        document = peer_actor_json(name='alice', fields={'attachment': [
            {'type': 'Image', 'url': f'https://{PEER}/banner.png'},
            {'type': 'PropertyValue', 'name': 'Pronouns', 'value': 'she/her'},
        ]})
        user = actor_json_to_model(document, 'alice', PEER)
        rows = db.session.query(UserExtraField).filter_by(user_id=user.id).all()
        assert len(rows) == 1
        assert rows[0].label == 'Pronouns'

    def test_attachment_that_is_not_a_list_is_ignored(self, app, db_session):
        """Some peers send a single object rather than an array; the isinstance
        test is what stops the for-loop iterating a dict's keys."""
        peer_instance(PEER)
        document = peer_actor_json(name='alice', fields={
            'attachment': {'type': 'PropertyValue', 'name': 'Website', 'value': 'example.org'}})
        user = actor_json_to_model(document, 'alice', PEER)
        assert db.session.query(UserExtraField).count() == 0

    def test_absent_attachment_leaves_no_extra_fields(self, app, db_session):
        peer_instance(PEER)
        user = actor_json_to_model(peer_actor_json(name='alice'), 'alice', PEER)
        assert db.session.query(UserExtraField).count() == 0


class TestRemoteImageCaching:
    """`if user.avatar_id and get_setting('cache_remote_images_locally', True)`
    and the matching cover guard, which call make_image_sizes.

    make_image_sizes runs inline under this harness (Celery is eager), and the
    only thing it does that a test can see is fetch the image's source_url. So
    the positive case registers that url in http_mock: assert_all_called=True
    means the test fails if the fetch never happens, which is what proves the
    guard was taken. A 404 is served so make_image_sizes_async stops there
    instead of resizing and writing files.

    The negative case turns the setting off. Its absence of a fetch is not
    directly observable -- make_image_sizes_async wraps get_request in a bare
    except, so the harness's own block would be swallowed rather than raised --
    so that test asserts the row state and leaves the fetch unasserted, which
    is the honest limit of what this harness can see. The `user.avatar_id`
    half of each guard IS independently falsified, by every test above whose
    actor has no icon.
    """

    def test_avatar_is_sent_for_resizing_when_caching_is_on(self, app, db_session, http_mock):
        peer_instance(PEER)
        http_mock.get(f'https://{PEER}/a.png').respond(404)
        document = peer_actor_json(name='alice',
                                   fields={'icon': {'url': f'https://{PEER}/a.png'}})
        user = actor_json_to_model(document, 'alice', PEER)
        assert user.avatar_id is not None

    def test_cover_is_sent_for_resizing_when_caching_is_on(self, app, db_session, http_mock):
        peer_instance(PEER)
        http_mock.get(f'https://{PEER}/c.png').respond(404)
        document = peer_actor_json(name='alice',
                                   fields={'image': {'url': f'https://{PEER}/c.png'}})
        user = actor_json_to_model(document, 'alice', PEER)
        assert user.cover_id is not None

    def test_images_are_still_recorded_when_caching_is_off(self, app, db_session):
        peer_instance(PEER)
        set_setting('cache_remote_images_locally', False)
        document = peer_actor_json(name='alice', fields={
            'icon': {'url': f'https://{PEER}/a.png'},
            'image': {'url': f'https://{PEER}/c.png'},
        })
        user = actor_json_to_model(document, 'alice', PEER)
        assert db.session.get(File, user.avatar_id).source_url == f'https://{PEER}/a.png'
        assert db.session.get(File, user.cover_id).source_url == f'https://{PEER}/c.png'
