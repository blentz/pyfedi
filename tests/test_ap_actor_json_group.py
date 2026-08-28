"""actor_json_to_model's `Group` branch, which turns a peer's actor document
into a Community row.

The `Person`/`Service` branch and the two guards that run before the type
dispatch (`'type' not in activity_json` and the id-host-versus-server
comparison)
are covered in tests/test_ap_actor_json_person.py and are deliberately not
repeated here. The `Feed` branch is covered elsewhere again. Every test in this
file therefore asserts on the Community that came back (or on None, or on the
exception that escaped), never on a User or a Feed.

The peer document is built by tests.factories.peer_actor_json, shared with the
Person/Service and Feed files. Its Group baseline holds only the keys this
branch reads unconditionally -- type, id, preferredUsername,
publicKey.publicKeyPem, name, inbox and outbox -- so every optional key is
opted into by name and the "absent" side of each guard is what the baseline
already gives you. `inbox` is unconditional in effect rather than in form: it
is reached through `activity_json['endpoints']['sharedInbox'] if 'endpoints' in
activity_json else activity_json['inbox']`, whose else-arm has no further
fallback (unlike the Person branch's, which ends in `else ''`), so a document
carrying neither key raises KeyError. That asymmetry is pinned by
TestInboxResolution.

Optional-field enumeration
--------------------------

Derived fresh against this checkout, restricted to the Group branch (from the
`elif activity_json['type'] == 'Group':` down to its `return community`, whose
line numbers the script prints and this docstring deliberately does not repeat):

    podman-compose -f compose.test.yaml exec -T -w /app test-runner python -c "
    import ast
    src = open('app/activitypub/util.py').read()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.FunctionDef) and n.name == 'actor_json_to_model':
            func = n
    def find_group(stmts):
        for s in stmts:
            if isinstance(s, ast.If):
                if 'Group' in ast.unparse(s.test):
                    return s
                r = find_group(s.orelse)
                if r: return r
        return None
    branch = find_group(func.body)
    lo, hi = branch.lineno, branch.body[-1].lineno
    ifs    = [s for s in ast.walk(func) if isinstance(s, ast.If)    and lo <= s.lineno <= hi]
    ifexps = [s for s in ast.walk(func) if isinstance(s, ast.IfExp) and lo <= s.lineno <= hi]
    print('If total:', len(ifs), ' IfExp:', len(ifexps))
    for s in sorted(ifs, key=lambda s: s.lineno):
        print('  If   ', ast.unparse(s.test)[:95])
    for s in sorted(ifexps, key=lambda s: s.lineno):
        print('  IfExp', ast.unparse(s.test)[:95])
    "

That prints `If total: 34  IfExp: 15`. Two of the thirty-four are not
optional-field guards: the type dispatch itself (`== 'Group'`) and the
`if community:` early return for a community already in the database. So the
branch holds **15 conditional expressions + 32 optional-field `if` statements =
47 conditional sites**, or 49 counting the dispatch and the early return. Both
of those two are covered as well, by TestGroupDispatch and
TestExistingCommunity.

The fifteen conditional expressions are all inside the Community() constructor
call, and are the scalar optional fields:

    'sensitive' in activity_json                -> nsfw, else False
    'genAI' in activity_json                    -> ai_generated, else False
    'postingRestrictedToMods' in activity_json  -> restricted_to_mods, else False
    'newModsWanted' in activity_json            -> new_mods_wanted, else False
    'privateMods' in activity_json              -> private_mods, else False
    'questionAnswer' in activity_json           -> question_answer, else False
    'defaultPostType' in activity_json          -> default_post_type, else 'link'
    'published' in activity_json                -> created_at, else utcnow()
    'updated' in activity_json                  -> last_active, else utcnow()
    'postingWarning' in activity_json           -> posting_warning, else None
    address.startswith('!')                     -> ap_id drops the '!', else not
    'followers' in activity_json                -> ap_followers_url, else None
    'endpoints' in activity_json                -> ap_inbox_url from
                                                   endpoints.sharedInbox,
                                                   else activity_json['inbox']
    'featured' in activity_json                 -> ap_featured_url, else ''
    'postUrlType' in activity_json              -> post_url_type, else None

and thirty-two `if` statements, which are the block-shaped ones (the two
excluded above, `== 'Group'` and `if community:`, are not in this list):

    'attributedTo' ... and isinstance(attributedTo, str)   -> mods_url
    'moderators' in activity_json                          (elif; else None)
    'sensitive' ... and sensitive and not site.enable_nsfw (returns None)
    'nsfl' ... and nsfl and not site.enable_nsfl           (returns None)
    get_setting('meme_comms_low_quality', False)           -> low_quality
    'summary' in activity_json                             -> description_html
    'content' in activity_json                             (elif; else '')
    description_html is not None and description_html != ''
      not description_html.startswith('<')                 (PeerTube wrap)
      'source' ... and source['mediaType'] == 'text/markdown'
    'theme' in activity_json and activity_json['theme']
    'icon' in activity_json and activity_json['icon'] is not None
      isinstance(icon, dict) and 'url' in icon
      isinstance(icon, list) and 'url' in icon[-1]
      isinstance(icon, str)
      icon_entry                                (the else leaves it None)
    'image' in activity_json and activity_json['image'] is not None
      isinstance(image, dict) and 'url' in image
      isinstance(image, list) and 'url' in image[0]
      image_entry
    'language' in activity_json and isinstance(language, list)
    'tag' in activity_json and isinstance(tag, list)       (new-style flair)
      flair['type'] == 'CommunityPostTag'
      flair_obj
    'lemmy:tagsForPosts' ... and isinstance(..., list)     (legacy flair)
      'text_color' in flair
      'background_color' in flair
      'blur_images' in flair
      'id' in flair
      flair_obj
    community.icon_id                           -> make_image_sizes
    community.image_id                          -> make_image_sizes

Every test builds the peer's Instance row first with peer_instance. Without it
find_instance_id inserts a sparse Instance and then calls new_instance_profile,
which fetches the peer's nodeinfo over HTTP. The Instance is also read back
twice by this branch, for show_popular and show_all -- see
TestInstanceDerivedVisibility.

Tests that reach either nsfw guard, or that supply 'sensitive'/'nsfl' at all,
request the `site` fixture: the branch loads `db.session.query(Site).get(1)`
unconditionally and both guards dereference it. A document with neither key
short-circuits before the dereference, which is why most tests here do not
need it.

FINDING -- unlike the Person/Service branch, whose whole User() construction
sits inside `try: ... except KeyError: ... return None`, the Group branch wraps
nothing. A peer document missing preferredUsername, name, outbox, publicKey or
inbox raises KeyError straight out of actor_json_to_model instead of returning
None. TestRequiredFieldsMissing pins that, and its docstring records the
asymmetry. Reported, not fixed.
"""
from datetime import datetime

import pytest

from app import db
from app.activitypub import util as activitypub_util
from app.activitypub.util import actor_json_to_model
from app.models import Community, CommunityFlair, File, Language, User, utcnow
from app.utils import set_setting
from tests.factories import peer_actor_json, peer_instance

PEER = 'peer.example'


def _group(name='memes', **kwargs):
    """peer_actor_json for a Group, with this file's server."""
    return peer_actor_json('Group', name=name, server=PEER, **kwargs)


class TestGroupDispatch:
    """`elif activity_json['type'] == 'Group':`, and what happens to documents
    that reach it without matching.

    Both mutation directions are exercised:

    - narrowed, e.g. `== 'Group_'`: a Group document then falls through to the
      Feed branch, which dereferences the owners collection over HTTP and dies
      long before returning a Community. test_group_document_creates_a_community
      catches it.
    - broadened to `!= 'Feed'` (or to a bare `True`): an actor type this
      function does not recognise is built as a Community instead of falling off
      the end of the if/elif chain and returning None.
      test_unrecognised_actor_type_is_not_built_as_a_community catches both.

    One broadening is NOT caught here and cannot be: `in ('Group', 'Feed')`
    leaves Group dispatch correct and only mis-routes Feed documents, so it is
    the Feed file's to fail -- a Feed document would come back as a Community
    there. Naming it here without a test that fails on it is exactly the trap
    the Person/Service file fell into, so it is named as out of scope instead.
    """

    def test_group_document_creates_a_community(self, app, db_session):
        peer_instance(PEER)
        community = actor_json_to_model(_group('memes'), '!memes', PEER)
        assert isinstance(community, Community)
        assert community.name == 'memes'
        assert community.title == 'Memes'
        assert community.ap_id == f'memes@{PEER}'
        assert community.ap_domain == PEER
        assert community.ap_profile_id == f'https://{PEER}/c/memes'
        assert community.ap_public_url == f'https://{PEER}/c/memes'
        assert community.ap_outbox_url == f'https://{PEER}/c/memes/outbox'
        assert community.public_key.startswith('-----BEGIN PUBLIC KEY-----')
        assert db.session.query(Community).count() == 1

    def test_unrecognised_actor_type_is_not_built_as_a_community(self, app, db_session):
        """An 'Organization' actor matches none of the four branches and falls
        off the end of the if/elif chain, so the function returns None
        implicitly. Broadening the Group test to `!= 'Feed'` builds a Community
        out of it instead."""
        peer_instance(PEER)
        document = _group('memes', fields={'type': 'Organization'})
        assert actor_json_to_model(document, '!memes', PEER) is None
        assert db.session.query(Community).count() == 0

    def test_a_person_document_is_not_handled_here(self, app, db_session):
        """The Person/Service test runs first, so a Person never reaches this
        branch. Asserting it comes back as a User keeps the Group tests honest
        about which branch built the row they are looking at."""
        peer_instance(PEER)
        result = actor_json_to_model(peer_actor_json(name='alice', server=PEER), 'alice', PEER)
        assert isinstance(result, User)
        assert db.session.query(Community).count() == 0


class TestExistingCommunity:
    """`community = db.session.query(Community).filter(...).first()` then
    `if community: return community`.

    The lookup is by `ap_profile_id == activity_json['id'].lower()`, and the
    return happens before ANY other key of the document is read. Deleting the
    early return is not caught by a same-id call on its own -- the insert would
    collide on the unique ap_profile_id and the IntegrityError handler would
    hand back the very same row -- so
    test_existing_community_is_returned_before_any_other_key_is_read supplies a
    document that is too incomplete to rebuild, and would raise KeyError rather
    than return. test_lookup_of_an_existing_community_lowercases_the_id strips
    the same key for the same reason: a lookup that failed to match would fall
    into the insert, collide on the unique ap_profile_id (which is written
    lower-cased regardless) and be handed the very same row back by the
    IntegrityError handler, so a bare `result.id == existing.id` would pass
    either way.

    Mutation that fails test_lookup_of_an_existing_community_lowercases_the_id:
    dropping the `.lower()` from the filter, which stops the stored row matching
    an id the peer re-published with a different case.
    """

    def test_a_second_call_with_the_same_document_returns_the_same_row(self, app, db_session):
        peer_instance(PEER)
        document = _group('memes')
        first = actor_json_to_model(document, '!memes', PEER)
        second = actor_json_to_model(document, '!memes', PEER)
        assert second.id == first.id
        assert db.session.query(Community).count() == 1

    def test_existing_community_is_returned_before_any_other_key_is_read(self, app, db_session):
        """The early return precedes every unconditional read in the branch, so
        a document stripped of preferredUsername, name and outbox still yields
        the stored row. Without the early return the same document raises
        KeyError -- see TestRequiredFieldsMissing."""
        peer_instance(PEER)
        existing = actor_json_to_model(_group('memes'), '!memes', PEER)
        stripped = _group('memes', omit=('preferredUsername', 'name', 'outbox'))
        result = actor_json_to_model(stripped, '!memes', PEER)
        assert result.id == existing.id
        assert db.session.query(Community).count() == 1

    def test_lookup_of_an_existing_community_lowercases_the_id(self, app, db_session):
        """A peer that upper-cases the path of its own id on a later fetch still
        matches the stored row. Only the path is varied, to keep this test
        about the lookup: the guard ahead of it lowercases the host on both
        sides, so varying the host would exercise that guard and not this
        lookup. The Person file's
        test_upper_cased_host_in_the_id_is_accepted covers the host.

        'outbox' is stripped so a failed match cannot masquerade as a hit --
        see this class's docstring."""
        peer_instance(PEER)
        existing = actor_json_to_model(_group('memes'), '!memes', PEER)
        document = _group('memes', fields={'id': f'https://{PEER}/c/MEMES'},
                          omit=('outbox',))
        result = actor_json_to_model(document, '!memes', PEER)
        assert result.id == existing.id
        assert db.session.query(Community).count() == 1


class TestRequiredFieldsMissing:
    """FINDING -- the Group branch has no `except KeyError` at all.

    The Person/Service branch builds its User inside
    `try: ... except KeyError: current_app.logger.error(...); return None`, so a
    malformed peer document there becomes a logged None. The Group branch's
    Community() construction is not wrapped, so the same malformation escapes to
    the caller as a KeyError. These tests pin today's behaviour rather than the
    intended one; a fix that adds the handler is expected to rewrite them into
    `is None` assertions.

    Five keys are read unconditionally, and each is tested separately because
    any one of them alone is enough to raise:

      preferredUsername, name, outbox, publicKey (for ['publicKeyPem']), and
      inbox -- the last only when 'endpoints' is absent, which is what makes it
      a required key in practice rather than in form.

    Mutation that fails all of these: adding the Person branch's try/except
    KeyError around the Community() call, which turns each raise into None.
    """

    @pytest.mark.parametrize('missing', ['preferredUsername', 'name', 'outbox', 'inbox'])
    def test_missing_unconditional_key_raises_key_error(self, app, db_session, missing):
        peer_instance(PEER)
        document = _group('memes', omit=(missing,))
        with pytest.raises(KeyError) as excinfo:
            actor_json_to_model(document, '!memes', PEER)
        assert excinfo.value.args[0] == missing
        assert db.session.query(Community).count() == 0

    def test_missing_public_key_raises_key_error(self, app, db_session):
        peer_instance(PEER)
        document = _group('memes', omit=('publicKey',))
        with pytest.raises(KeyError):
            actor_json_to_model(document, '!memes', PEER)
        assert db.session.query(Community).count() == 0

    def test_public_key_without_a_pem_raises_key_error(self, app, db_session):
        peer_instance(PEER)
        document = _group('memes', fields={'publicKey': {'id': 'x'}})
        with pytest.raises(KeyError) as excinfo:
            actor_json_to_model(document, '!memes', PEER)
        assert excinfo.value.args[0] == 'publicKeyPem'
        assert db.session.query(Community).count() == 0


class TestModeratorsUrl:
    """`if 'attributedTo' ... isinstance(str)` / `elif 'moderators'` / `else
    None`, feeding ap_moderators_url.

    Mutation that fails test_non_string_attributed_to_falls_through_to_moderators:
    dropping the isinstance test, which would store the list itself in a String
    column instead of falling through to the kbin spelling.
    """

    def test_attributed_to_string_becomes_the_moderators_url(self, app, db_session):
        peer_instance(PEER)
        document = _group('memes', fields={'attributedTo': f'https://{PEER}/c/memes/moderators'})
        community = actor_json_to_model(document, '!memes', PEER)
        assert community.ap_moderators_url == f'https://{PEER}/c/memes/moderators'

    def test_moderators_is_used_when_attributed_to_is_absent(self, app, db_session):
        peer_instance(PEER)
        document = _group('memes', fields={'moderators': f'https://{PEER}/c/memes/mods'})
        community = actor_json_to_model(document, '!memes', PEER)
        assert community.ap_moderators_url == f'https://{PEER}/c/memes/mods'

    def test_non_string_attributed_to_falls_through_to_moderators(self, app, db_session):
        """Mastodon-style peers publish attributedTo as a list of actors. The
        isinstance test rejects that shape and the kbin 'moderators' key wins."""
        peer_instance(PEER)
        document = _group('memes', fields={
            'attributedTo': [{'type': 'Person', 'id': f'https://{PEER}/u/mod'}],
            'moderators': f'https://{PEER}/c/memes/mods',
        })
        community = actor_json_to_model(document, '!memes', PEER)
        assert community.ap_moderators_url == f'https://{PEER}/c/memes/mods'

    def test_attributed_to_wins_over_moderators_when_both_are_strings(self, app, db_session):
        peer_instance(PEER)
        document = _group('memes', fields={
            'attributedTo': f'https://{PEER}/c/memes/moderators',
            'moderators': f'https://{PEER}/c/memes/mods',
        })
        community = actor_json_to_model(document, '!memes', PEER)
        assert community.ap_moderators_url == f'https://{PEER}/c/memes/moderators'

    def test_neither_key_leaves_no_moderators_url(self, app, db_session):
        peer_instance(PEER)
        community = actor_json_to_model(_group('memes'), '!memes', PEER)
        assert community.ap_moderators_url is None


class TestNsfwAndNsflGuards:
    """`if 'sensitive' ... and activity_json['sensitive'] and not
    site.enable_nsfw: return None`, and the identical nsfl guard.

    All three operands of each guard are falsified independently: the key
    absent (every other test in this file), the key present but false
    (test_sensitive_false_is_not_blocked), and the instance opting in
    (test_sensitive_document_is_accepted_when_the_instance_enables_nsfw).

    Mutation that fails test_sensitive_document_is_rejected_when_nsfw_is_off:
    deleting the guard, which admits the community. Mutation that fails
    test_sensitive_document_is_accepted_when_the_instance_enables_nsfw:
    dropping the `not site.enable_nsfw` operand, which would reject the
    document even on an instance that allows it.
    """

    def test_sensitive_document_is_rejected_when_nsfw_is_off(self, app, db_session, site):
        peer_instance(PEER)
        assert site.enable_nsfw is not True
        document = _group('memes', fields={'sensitive': True})
        assert actor_json_to_model(document, '!memes', PEER) is None
        assert db.session.query(Community).count() == 0

    def test_sensitive_document_is_accepted_when_the_instance_enables_nsfw(self, app, db_session, site):
        peer_instance(PEER)
        site.enable_nsfw = True
        db.session.commit()
        document = _group('memes', fields={'sensitive': True})
        community = actor_json_to_model(document, '!memes', PEER)
        assert community is not None
        assert community.nsfw is True

    def test_sensitive_false_is_not_blocked(self, app, db_session, site):
        """The middle operand: the key is present, so `'sensitive' in
        activity_json` is true, and only the value stops the guard."""
        peer_instance(PEER)
        document = _group('memes', fields={'sensitive': False})
        community = actor_json_to_model(document, '!memes', PEER)
        assert community is not None
        assert community.nsfw is False

    def test_nsfl_document_is_rejected_when_nsfl_is_off(self, app, db_session, site):
        peer_instance(PEER)
        assert site.enable_nsfl is not True
        document = _group('memes', fields={'nsfl': True})
        assert actor_json_to_model(document, '!memes', PEER) is None
        assert db.session.query(Community).count() == 0

    def test_nsfl_document_is_accepted_when_the_instance_enables_nsfl(self, app, db_session, site):
        """nsfl has no column of its own on Community -- only the guard reads
        it -- so the observable outcome is that a row exists at all."""
        peer_instance(PEER)
        site.enable_nsfl = True
        db.session.commit()
        document = _group('memes', fields={'nsfl': True})
        community = actor_json_to_model(document, '!memes', PEER)
        assert community is not None
        assert db.session.query(Community).count() == 1

    def test_nsfl_false_is_not_blocked(self, app, db_session, site):
        peer_instance(PEER)
        document = _group('memes', fields={'nsfl': False})
        assert actor_json_to_model(document, '!memes', PEER) is not None


class TestScalarOptionalFields:
    """The fifteen conditional expressions in the Community() call, both ways
    round.

    The present/absent pair is the point: a suite that always supplied every
    key would leave every `else` of these expressions unexecuted while branch
    coverage reported the whole constructor as covered.

    `address.startswith('!')` and the endpoints/inbox expression are exercised
    by TestApIdFromAddress and TestInboxResolution below, which need more than
    one document each; the other thirteen are paired here.

    Mutation pair on one optional-field guard, `'defaultPostType' in
    activity_json`:
      - replaced with `True`: test_every_scalar_optional_absent_takes_its_default
        raises KeyError instead of returning 'link'.
      - replaced with `False`: test_every_scalar_optional_present_is_copied gets
        'link' instead of the peer's 'image'.
    Either replacement is caught, which is what makes the pair worth naming.
    """

    def test_every_scalar_optional_present_is_copied(self, app, db_session, site):
        peer_instance(PEER)
        site.enable_nsfw = True
        db.session.commit()
        document = _group('memes', fields={
            'sensitive': True,
            'genAI': True,
            'postingRestrictedToMods': True,
            'newModsWanted': True,
            'privateMods': True,
            'questionAnswer': True,
            'defaultPostType': 'image',
            'published': '2024-01-02T03:04:05Z',
            'updated': '2024-03-04T05:06:07Z',
            'postingWarning': 'read the rules',
            'followers': f'https://{PEER}/c/memes/followers',
            'featured': f'https://{PEER}/c/memes/featured',
            'postUrlType': 'thumbnail',
        })
        community = actor_json_to_model(document, '!memes', PEER)
        assert community.nsfw is True
        assert community.ai_generated is True
        assert community.restricted_to_mods is True
        assert community.new_mods_wanted is True
        assert community.private_mods is True
        assert community.question_answer is True
        assert community.default_post_type == 'image'
        assert community.created_at == datetime(2024, 1, 2, 3, 4, 5)
        assert community.last_active == datetime(2024, 3, 4, 5, 6, 7)
        assert community.posting_warning == 'read the rules'
        assert community.ap_followers_url == f'https://{PEER}/c/memes/followers'
        assert community.ap_featured_url == f'https://{PEER}/c/memes/featured'
        assert community.post_url_type == 'thumbnail'

    def test_every_scalar_optional_absent_takes_its_default(self, app, db_session):
        peer_instance(PEER)
        before = utcnow()
        community = actor_json_to_model(_group('memes'), '!memes', PEER)
        assert community.nsfw is False
        assert community.ai_generated is False
        assert community.restricted_to_mods is False
        assert community.new_mods_wanted is False
        assert community.private_mods is False
        assert community.question_answer is False
        assert community.default_post_type == 'link'
        # utcnow(), not the peer's value: distinguishable from the 2024
        # timestamps the present-side test supplies, which `is not None` would
        # also accept.
        assert before <= community.created_at <= utcnow()
        assert before <= community.last_active <= utcnow()
        assert community.posting_warning is None
        assert community.ap_followers_url is None
        assert community.ap_featured_url == ''
        assert community.post_url_type is None


class TestWhitespaceInThePeersNames:
    """`name=activity_json['preferredUsername'].strip()` and
    `title=activity_json['name'].strip()` in the Community() call.

    Coverage cannot see inside either. Both sit on lines that every
    Group-creating test in this file already executes, so the branch
    reported full statement and branch coverage while both could be deleted
    with the suite still green -- no test fed a padded value, so the
    stripping was asserted nowhere.

    The Person/Service and Feed branches have the same two calls. All three
    branches now carry a test per call site; before this class and its two
    counterparts, only Person's `name` was pinned, by the padded
    `'name': '  Alice Liddell  '` in its TestScalarOptionalFields.

    A padded name is not a contrived input. `preferredUsername` and `name`
    are free text a remote admin types into a form, and nothing between
    that form and this constructor trims them.
    """

    def test_a_padded_preferred_username_is_stripped_into_the_name_column(self, app, db_session):
        """Production change that fails this: deleting
        `activity_json['preferredUsername'].strip()`'s `.strip()`, after
        which the Community's name column holds ' memes ' and the equality
        fails. The second assertion pins the value that was persisted, not
        just the one the constructor was handed."""
        peer_instance(PEER)
        document = _group('memes', fields={'preferredUsername': ' memes '})
        community = actor_json_to_model(document, '!memes', PEER)
        assert community.name == 'memes'
        assert db.session.query(Community).one().name == 'memes'

    def test_a_padded_name_is_stripped_into_the_title_column(self, app, db_session):
        """The second, textually separate call site. Production change that
        fails this: deleting `activity_json['name'].strip()`'s `.strip()`.

        The padding differs from the preferredUsername test's on purpose --
        a tab and a newline, not two spaces -- so that a `.strip(' ')`
        narrowing (stripping only literal spaces) is caught here rather
        than passing."""
        peer_instance(PEER)
        document = _group('memes', fields={'name': '\t Memes and Such \n'})
        community = actor_json_to_model(document, '!memes', PEER)
        assert community.title == 'Memes and Such'
        assert db.session.query(Community).one().title == 'Memes and Such'


class TestApIdFromAddress:
    """`ap_id = f"{address[1:].lower()}@{server.lower()}" if
    address.startswith('!') else f"{address.lower()}@{server.lower()}"`.

    The '!' is the Lemmy community sigil, and callers pass the address both
    ways. Both arms also lower-case, which the mixed-case tests pin.

    Mutation that fails test_bare_address_keeps_all_of_its_characters:
    inverting the condition to `not address.startswith('!')`, which would eat
    this address's first character.
    """

    def test_sigil_prefixed_address_loses_the_sigil(self, app, db_session):
        peer_instance(PEER)
        community = actor_json_to_model(_group('memes'), '!memes', PEER)
        assert community.ap_id == f'memes@{PEER}'

    def test_bare_address_keeps_all_of_its_characters(self, app, db_session):
        peer_instance(PEER)
        community = actor_json_to_model(_group('memes'), 'memes', PEER)
        assert community.ap_id == f'memes@{PEER}'

    def test_both_arms_lower_case_the_address_and_the_server(self, app, db_session):
        """ap_domain is lower-cased separately from ap_id, so both are asserted.
        The address argument is what varies in case here; the document's id is
        left on the server's own host, which is all the guard ahead of this
        branch looks at."""
        peer_instance(PEER)
        community = actor_json_to_model(_group('memes'), '!MEMES', PEER)
        assert community.ap_id == f'memes@{PEER}'
        assert community.ap_domain == PEER


class TestInboxResolution:
    """`activity_json['endpoints']['sharedInbox'] if 'endpoints' in
    activity_json else activity_json['inbox']`.

    Two arms, and unlike the Person/Service branch there is no third: the
    else-arm has no `if 'inbox' in activity_json` of its own and no `else ''`.
    The KeyError that a document with neither key raises is pinned in
    TestRequiredFieldsMissing.

    Mutation that fails test_inbox_used_when_endpoints_absent: reordering the
    expression to try 'inbox' first, which would make the first test below read
    the per-actor inbox instead of the shared one.
    """

    def test_shared_inbox_wins_when_endpoints_present(self, app, db_session):
        peer_instance(PEER)
        document = _group('memes', fields={'endpoints': {'sharedInbox': f'https://{PEER}/inbox'}})
        community = actor_json_to_model(document, '!memes', PEER)
        assert community.ap_inbox_url == f'https://{PEER}/inbox'

    def test_inbox_used_when_endpoints_absent(self, app, db_session):
        peer_instance(PEER)
        community = actor_json_to_model(_group('memes'), '!memes', PEER)
        assert community.ap_inbox_url == f'https://{PEER}/c/memes/inbox'


class TestInstanceDerivedVisibility:
    """`community.show_popular = Instance.popular` and `community.show_all =
    not Instance.silenced`, both re-read from the database by primary key after
    find_instance_id has run.

    Mutation that fails test_a_silenced_unpopular_instance_hides_its_communities:
    dropping the `not` from show_all.
    """

    def test_defaults_come_from_an_ordinary_instance(self, app, db_session):
        peer_instance(PEER)
        community = actor_json_to_model(_group('memes'), '!memes', PEER)
        assert community.show_popular is True
        assert community.show_all is True

    def test_a_silenced_unpopular_instance_hides_its_communities(self, app, db_session):
        instance = peer_instance(PEER)
        instance.popular = False
        instance.silenced = True
        db.session.commit()
        community = actor_json_to_model(_group('memes'), '!memes', PEER)
        assert community.show_popular is False
        assert community.show_all is False


class TestLowQualityMemeCommunities:
    """`if get_setting('meme_comms_low_quality', False): community.low_quality =
    'memes' in preferredUsername or 'shitpost' in preferredUsername`.

    Three tests, because the setting's two states and the substring test's two
    operands are independent: with the setting off the assignment never runs at
    all and the column keeps its default.

    Mutation that fails test_setting_off_leaves_a_meme_community_alone:
    deleting the `if`, which would mark the community low quality on an
    instance that never asked for it.
    """

    def test_setting_on_marks_a_memes_community_low_quality(self, app, db_session):
        peer_instance(PEER)
        set_setting('meme_comms_low_quality', True)
        community = actor_json_to_model(_group('memes'), '!memes', PEER)
        assert community.low_quality is True

    def test_setting_on_marks_a_shitpost_community_low_quality(self, app, db_session):
        """The second operand of the `or`, which the 'memes' test alone leaves
        unfalsified."""
        peer_instance(PEER)
        set_setting('meme_comms_low_quality', True)
        community = actor_json_to_model(_group('shitposting'), '!shitposting', PEER)
        assert community.low_quality is True

    def test_setting_on_leaves_an_ordinary_community_alone(self, app, db_session):
        peer_instance(PEER)
        set_setting('meme_comms_low_quality', True)
        community = actor_json_to_model(_group('gardening'), '!gardening', PEER)
        assert community.low_quality is False

    def test_setting_off_leaves_a_meme_community_alone(self, app, db_session):
        peer_instance(PEER)
        community = actor_json_to_model(_group('memes'), '!memes', PEER)
        assert community.low_quality is False


class TestDescription:
    """description_html comes from 'summary', else 'content', else ''; it is
    allowlisted, wrapped in <p> when it does not start with '<', and overwritten
    from 'source' when that declares text/markdown.

    Mutation that fails test_plain_text_summary_is_wrapped_in_a_paragraph:
    dropping the `not description_html.startswith('<')` PeerTube wrap, which
    leaves the bare text unwrapped.
    """

    def test_html_summary_is_allowlisted_not_wrapped(self, app, db_session):
        peer_instance(PEER)
        document = _group('memes', fields={'summary': '<p>hello <script>x</script></p>'})
        community = actor_json_to_model(document, '!memes', PEER)
        assert '<script>' not in community.description_html
        assert 'hello' in community.description_html
        assert community.description_html.startswith('<p>')

    def test_plain_text_summary_is_wrapped_in_a_paragraph(self, app, db_session):
        peer_instance(PEER)
        document = _group('memes', fields={'summary': 'just words'})
        community = actor_json_to_model(document, '!memes', PEER)
        assert community.description_html.strip().startswith('<p>')
        assert 'just words' in community.description_html

    def test_content_is_used_when_summary_is_absent(self, app, db_session):
        peer_instance(PEER)
        document = _group('memes', fields={'content': '<p>from content</p>'})
        community = actor_json_to_model(document, '!memes', PEER)
        assert 'from content' in community.description_html

    def test_summary_wins_over_content_when_both_are_present(self, app, db_session):
        peer_instance(PEER)
        document = _group('memes', fields={'summary': '<p>from summary</p>',
                                           'content': '<p>from content</p>'})
        community = actor_json_to_model(document, '!memes', PEER)
        assert 'from summary' in community.description_html
        assert 'from content' not in community.description_html

    def test_neither_key_leaves_the_description_unset(self, app, db_session):
        peer_instance(PEER)
        community = actor_json_to_model(_group('memes'), '!memes', PEER)
        assert not community.description_html
        assert not community.description

    def test_null_summary_leaves_the_description_unset(self, app, db_session):
        """`description_html is not None` guards the whole block; the key is
        present with a null value, which several peers do send. Unlike the
        Person branch -- which passes the null on to allowlist_html and stores
        the '' that comes back -- this branch skips the block entirely, so the
        column is never assigned at all."""
        peer_instance(PEER)
        document = _group('memes', fields={'summary': None})
        community = actor_json_to_model(document, '!memes', PEER)
        assert community.description_html is None
        assert community.description is None

    def test_empty_summary_leaves_the_description_unset(self, app, db_session):
        """`description_html != ''` -- the second operand of the same guard."""
        peer_instance(PEER)
        document = _group('memes', fields={'summary': ''})
        community = actor_json_to_model(document, '!memes', PEER)
        assert community.description_html is None

    def test_markdown_source_overwrites_the_html_derived_description(self, app, db_session):
        peer_instance(PEER)
        document = _group('memes', fields={
            'summary': '<p>from html</p>',
            'source': {'mediaType': 'text/markdown', 'content': '**from markdown**'},
        })
        community = actor_json_to_model(document, '!memes', PEER)
        assert community.description == '**from markdown**'
        assert '<strong>from markdown</strong>' in community.description_html

    def test_source_with_another_media_type_leaves_the_description_from_the_html(self, app, db_session):
        peer_instance(PEER)
        document = _group('memes', fields={
            'summary': '<p>from html</p>',
            'source': {'mediaType': 'text/html', 'content': '**from markdown**'},
        })
        community = actor_json_to_model(document, '!memes', PEER)
        assert community.description.strip() == 'from html'

    def test_source_is_ignored_when_there_is_no_description_at_all(self, app, db_session):
        """The markdown override sits INSIDE the `description_html != ''`
        block, so a peer that sends a source but no summary or content gets
        neither -- which is a shape the Person/Service branch does not share,
        since its own source handling is outside the summary block."""
        peer_instance(PEER)
        document = _group('memes', fields={
            'source': {'mediaType': 'text/markdown', 'content': '**ignored**'},
        })
        community = actor_json_to_model(document, '!memes', PEER)
        assert community.description is None
        assert community.description_html is None


class TestTheme:
    """`if 'theme' in activity_json and activity_json['theme']:
    community.theme = activity_json['theme']`.

    Both operands are pinned:

    - dropping `'theme' in activity_json` turns an absent theme into a KeyError,
      which test_absent_theme_leaves_the_default catches.
    - dropping the truthiness test lets a falsy value through to the column,
      which test_falsy_non_string_theme_is_not_copied catches.

    Not every falsy value catches it, and they do not all behave alike: there
    are three outcomes, not one. '' and a JSON null leave theme unchanged (''
    is also the column's default, so both sides of the mutation store the same
    thing); falsy scalars and lists are coerced to a non-empty string and so are
    distinguishable (0 -> '0', false -> 'false', [] -> '{}'); and a falsy dict
    is not stored at all -- psycopg raises ProgrammingError, "can't adapt type
    'dict'", before it reaches the column. The test below uses `false`, which is
    both the likeliest thing a peer sends and one of the two values that pin
    the operand cleanly.
    """

    def test_theme_is_copied(self, app, db_session):
        peer_instance(PEER)
        document = _group('memes', fields={'theme': 'high_contrast'})
        community = actor_json_to_model(document, '!memes', PEER)
        assert community.theme == 'high_contrast'

    def test_empty_theme_is_not_copied(self, app, db_session):
        peer_instance(PEER)
        document = _group('memes', fields={'theme': ''})
        community = actor_json_to_model(document, '!memes', PEER)
        assert community.theme == ''

    def test_falsy_non_string_theme_is_not_copied(self, app, db_session):
        """This is what pins the truthiness operand. A JSON `false` is falsy, so
        the guard skips it and the column keeps its '' default; drop the operand
        and the same value reaches a String column, which stores it as the
        four-character string 'false'. Falsy scalars and lists behave that way
        too (0 becomes '0', [] becomes '{}'), but a falsy dict does NOT: psycopg
        raises ProgrammingError, "can't adapt type 'dict'", so {} never reaches
        the column at all. '' and a JSON null are the two that leave theme
        unchanged, and so cannot pin this operand."""
        peer_instance(PEER)
        document = _group('memes', fields={'theme': False})
        community = actor_json_to_model(document, '!memes', PEER)
        assert community.theme == ''

    def test_absent_theme_leaves_the_default(self, app, db_session):
        peer_instance(PEER)
        community = actor_json_to_model(_group('memes'), '!memes', PEER)
        assert community.theme == ''


class TestIcon:
    """The community icon. Four shapes of 'icon' are recognised (dict with url,
    list whose LAST entry has a url, bare string, and nothing else) plus the
    `icon_entry` guard that skips the File when none matched.

    Mutation that fails test_icon_as_a_list_takes_the_last_entry: changing the
    [-1] index to [0], which picks the wrong url.
    """

    def test_icon_as_a_dict(self, app, db_session):
        peer_instance(PEER)
        document = _group('memes', fields={'icon': {'url': f'https://{PEER}/a.png'}})
        community = actor_json_to_model(document, '!memes', PEER)
        assert community.icon_id is not None
        assert db.session.get(File, community.icon_id).source_url == f'https://{PEER}/a.png'

    def test_icon_as_a_list_takes_the_last_entry(self, app, db_session):
        peer_instance(PEER)
        document = _group('memes', fields={'icon': [
            {'url': f'https://{PEER}/small.png'},
            {'url': f'https://{PEER}/large.png'},
        ]})
        community = actor_json_to_model(document, '!memes', PEER)
        assert db.session.get(File, community.icon_id).source_url == f'https://{PEER}/large.png'

    def test_icon_as_a_bare_string(self, app, db_session):
        peer_instance(PEER)
        document = _group('memes', fields={'icon': f'https://{PEER}/a.png'})
        community = actor_json_to_model(document, '!memes', PEER)
        assert db.session.get(File, community.icon_id).source_url == f'https://{PEER}/a.png'

    def test_icon_of_an_unrecognised_shape_creates_no_file(self, app, db_session):
        """A dict with no 'url' matches none of the three shapes, so icon_entry
        stays None and the File is never built."""
        peer_instance(PEER)
        document = _group('memes', fields={'icon': {'mediaType': 'image/png'}})
        community = actor_json_to_model(document, '!memes', PEER)
        assert community.icon_id is None
        assert db.session.query(File).count() == 0

    def test_null_icon_creates_no_file(self, app, db_session):
        peer_instance(PEER)
        document = _group('memes', fields={'icon': None})
        community = actor_json_to_model(document, '!memes', PEER)
        assert community.icon_id is None

    def test_absent_icon_creates_no_file(self, app, db_session):
        peer_instance(PEER)
        community = actor_json_to_model(_group('memes'), '!memes', PEER)
        assert community.icon_id is None
        assert db.session.query(File).count() == 0


class TestImage:
    """The community banner. Only two shapes are recognised here -- unlike the
    icon block above there is no bare-string arm -- and the list form takes
    entry [0], not [-1].

    Mutation that fails test_image_as_a_list_takes_the_first_entry: changing
    that [0] to [-1].
    """

    def test_image_as_a_dict(self, app, db_session):
        peer_instance(PEER)
        document = _group('memes', fields={'image': {'url': f'https://{PEER}/c.png'}})
        community = actor_json_to_model(document, '!memes', PEER)
        assert db.session.get(File, community.image_id).source_url == f'https://{PEER}/c.png'

    def test_image_as_a_list_takes_the_first_entry(self, app, db_session):
        peer_instance(PEER)
        document = _group('memes', fields={'image': [
            {'url': f'https://{PEER}/first.png'},
            {'url': f'https://{PEER}/second.png'},
        ]})
        community = actor_json_to_model(document, '!memes', PEER)
        assert db.session.get(File, community.image_id).source_url == f'https://{PEER}/first.png'

    def test_image_as_a_bare_string_creates_no_file(self, app, db_session):
        """The shape the icon block accepts and this one does not: neither
        isinstance test matches, so image_entry stays None."""
        peer_instance(PEER)
        document = _group('memes', fields={'image': f'https://{PEER}/c.png'})
        community = actor_json_to_model(document, '!memes', PEER)
        assert community.image_id is None
        assert db.session.query(File).count() == 0

    def test_null_image_creates_no_file(self, app, db_session):
        peer_instance(PEER)
        document = _group('memes', fields={'image': None})
        community = actor_json_to_model(document, '!memes', PEER)
        assert community.image_id is None

    def test_absent_image_creates_no_file(self, app, db_session):
        peer_instance(PEER)
        community = actor_json_to_model(_group('memes'), '!memes', PEER)
        assert community.image_id is None


class TestRemoteImageResizing:
    """`if community.icon_id: make_image_sizes(...)` and the matching image
    guard.

    Unlike the Person/Service branch, these two guards do NOT consult
    get_setting('cache_remote_images_locally', True) -- a remote community's
    icon and banner are always sent for resizing. That difference is worth
    knowing before anyone assumes the setting governs both branches.

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
        peer_instance(PEER)
        http_mock.get(f'https://{PEER}/a.png').respond(404)
        document = _group('memes', fields={'icon': {'url': f'https://{PEER}/a.png'}})
        community = actor_json_to_model(document, '!memes', PEER)
        assert community.icon_id is not None

    def test_banner_is_sent_for_resizing(self, app, db_session, http_mock):
        peer_instance(PEER)
        http_mock.get(f'https://{PEER}/c.png').respond(404)
        document = _group('memes', fields={'image': {'url': f'https://{PEER}/c.png'}})
        community = actor_json_to_model(document, '!memes', PEER)
        assert community.image_id is not None


class TestLanguages:
    """`if 'language' in activity_json and isinstance(..., list)`, then one
    find_language_or_create per entry, appended to community.languages.

    Mutation that fails test_language_that_is_not_a_list_is_ignored: dropping
    the isinstance test, which would iterate a dict's keys and raise TypeError
    on `ap_language['identifier']`.
    """

    def test_languages_are_created_and_linked(self, app, db_session):
        peer_instance(PEER)
        document = _group('memes', fields={'language': [
            {'identifier': 'en', 'name': 'English'},
            {'identifier': 'de', 'name': 'German'},
        ]})
        community = actor_json_to_model(document, '!memes', PEER)
        assert sorted(lang.code for lang in community.languages) == ['de', 'en']
        assert db.session.query(Language).filter_by(code='de').one().name == 'German'

    def test_an_existing_language_is_reused_not_duplicated(self, app, db_session):
        peer_instance(PEER)
        db.session.add(Language(code='en', name='English'))
        db.session.commit()
        document = _group('memes', fields={'language': [{'identifier': 'en', 'name': 'Englisch'}]})
        community = actor_json_to_model(document, '!memes', PEER)
        assert [lang.code for lang in community.languages] == ['en']
        assert db.session.query(Language).filter_by(code='en').count() == 1
        # find_language_or_create returns the stored row untouched, so the
        # peer's spelling of the name does not overwrite ours.
        assert db.session.query(Language).filter_by(code='en').one().name == 'English'

    def test_language_that_is_not_a_list_is_ignored(self, app, db_session):
        peer_instance(PEER)
        document = _group('memes', fields={'language': {'identifier': 'en', 'name': 'English'}})
        community = actor_json_to_model(document, '!memes', PEER)
        assert community.languages.count() == 0

    def test_absent_language_leaves_no_languages(self, app, db_session):
        peer_instance(PEER)
        community = actor_json_to_model(_group('memes'), '!memes', PEER)
        assert community.languages.count() == 0


class TestNewStylePostFlair:
    """`if 'tag' in activity_json and isinstance(activity_json['tag'], list)`,
    which hands each CommunityPostTag straight to find_flair_or_create.

    find_flair_or_create itself is covered by tests/test_ap_find_flair_or_create.py;
    what these tests cover is this branch's own filtering and appending.

    Mutation that fails test_non_community_post_tag_entries_are_skipped:
    replacing the `flair['type'] == 'CommunityPostTag'` test with `True`, which
    stores the sibling lemmy:CommunityTag entry as community flair too.

    That entry has to be one find_flair_or_create would actually accept, or the
    mutation survives: a Hashtag carries 'name' and 'href' and neither
    'display_name' nor 'preferredUsername', so find_flair_or_create returns None
    for it and dropping the type test changes nothing observable. The
    lemmy:CommunityTag shape is the one this very codebase emits for
    flair_ap_json version 1, and it does carry 'display_name'. Both entries are
    in the list below: the Hashtag because it is what peers really send, the
    lemmy:CommunityTag because it is what makes the guard's effect visible.
    """

    def test_community_post_tag_becomes_community_flair(self, app, db_session):
        peer_instance(PEER)
        document = _group('memes', fields={'tag': [{
            'type': 'CommunityPostTag',
            'id': f'https://{PEER}/c/memes/tag/1',
            'preferredUsername': 'Discussion',
            'textColor': '#ffffff',
            'backgroundColor': '#000000',
            'blurImages': True,
        }]})
        community = actor_json_to_model(document, '!memes', PEER)
        assert [f.flair for f in community.flair] == ['Discussion']
        stored = db.session.query(CommunityFlair).one()
        assert stored.community_id == community.id
        assert stored.ap_id == f'https://{PEER}/c/memes/tag/1'
        assert stored.text_color == '#ffffff'
        assert stored.background_color == '#000000'
        assert stored.blur_images is True

    def test_non_community_post_tag_entries_are_skipped(self, app, db_session):
        peer_instance(PEER)
        document = _group('memes', fields={'tag': [
            {'type': 'Hashtag', 'name': '#memes', 'href': f'https://{PEER}/tag/memes'},
            {'type': 'lemmy:CommunityTag', 'id': f'https://{PEER}/c/memes/tag/9',
             'display_name': 'Legacy'},
            {'type': 'CommunityPostTag', 'id': f'https://{PEER}/c/memes/tag/1',
             'preferredUsername': 'Discussion'},
        ]})
        community = actor_json_to_model(document, '!memes', PEER)
        assert [f.flair for f in community.flair] == ['Discussion']
        assert db.session.query(CommunityFlair).count() == 1

    def test_a_tag_find_flair_or_create_rejects_is_not_appended(self, app, db_session):
        """`if flair_obj:` -- find_flair_or_create returns None when the tag
        carries no usable name, and the branch must not append that None."""
        peer_instance(PEER)
        document = _group('memes', fields={'tag': [{
            'type': 'CommunityPostTag',
            'id': f'https://{PEER}/c/memes/tag/1',
            'preferredUsername': '',
        }]})
        community = actor_json_to_model(document, '!memes', PEER)
        assert community.flair == []
        assert db.session.query(CommunityFlair).count() == 0

    def test_tag_that_is_not_a_list_is_ignored(self, app, db_session):
        peer_instance(PEER)
        document = _group('memes', fields={'tag': {'type': 'CommunityPostTag',
                                                   'preferredUsername': 'Discussion'}})
        community = actor_json_to_model(document, '!memes', PEER)
        assert community.flair == []
        assert db.session.query(CommunityFlair).count() == 0

    def test_absent_tag_leaves_no_flair(self, app, db_session):
        peer_instance(PEER)
        community = actor_json_to_model(_group('memes'), '!memes', PEER)
        assert community.flair == []


class TestLegacyPostFlair:
    """`elif 'lemmy:tagsForPosts' in activity_json and isinstance(..., list)`,
    which rebuilds a smaller dict per tag before calling find_flair_or_create --
    'display_name' for every entry that has one (an entry without one is skipped
    and logged, see
    test_a_tag_without_a_display_name_is_skipped_and_the_rest_ingest), and then
    'text_color', 'background_color', 'blur_images' and 'id' each conditionally.

    Those four `if`s are the reason this block exists rather than passing the
    tag through as-is, so each is tested present AND absent: the absent side is
    what proves find_flair_or_create receives a dict WITHOUT the key and applies
    its own default, rather than receiving the key with a None value.

    Two things about this path were established by the find_flair_or_create task
    and are taken as settled rather than re-derived here: this block copies 'id'
    only when the peer supplies it, and it calls find_flair_or_create with no
    session, which defaults to db.session -- whose autoflush is disabled
    app-wide. find_flair_or_create's unguarded `flair['id']` read in its
    ap_id-backfill path is therefore unreachable from here, for two independent
    reasons: this branch returns early for a community that already exists, so
    the block only ever runs on a fresh insert with no pre-existing flair to
    back-fill, and autoflush=False stops a flair added earlier in the same loop
    from being visible to the next iteration's query. That crash is reachable
    from refresh_community_profile_task, which uses get_task_session() -- a
    plain Session with autoflush on -- and is out of scope here.

    Mutation that fails test_a_tag_with_only_a_display_name_takes_every_default:
    replacing any one of the four `if key in flair` guards with `True`, which
    turns the absent key into a KeyError instead of a default.
    """

    def test_a_tag_with_every_optional_key_copies_all_of_them(self, app, db_session):
        peer_instance(PEER)
        document = _group('memes', fields={'lemmy:tagsForPosts': [{
            'display_name': 'Discussion',
            'text_color': '#ffffff',
            'background_color': '#000000',
            'blur_images': True,
            'id': f'https://{PEER}/c/memes/tag/1',
        }]})
        community = actor_json_to_model(document, '!memes', PEER)
        assert [f.flair for f in community.flair] == ['Discussion']
        stored = db.session.query(CommunityFlair).one()
        assert stored.community_id == community.id
        assert stored.text_color == '#ffffff'
        assert stored.background_color == '#000000'
        assert stored.blur_images is True
        assert stored.ap_id == f'https://{PEER}/c/memes/tag/1'

    def test_a_tag_with_only_a_display_name_takes_every_default(self, app, db_session):
        """The absent side of all four optional guards at once. The defaults
        are find_flair_or_create's, not this block's: empty strings for the two
        colours, False for blur_images and a null ap_id."""
        peer_instance(PEER)
        document = _group('memes', fields={'lemmy:tagsForPosts': [{'display_name': 'Discussion'}]})
        community = actor_json_to_model(document, '!memes', PEER)
        stored = db.session.query(CommunityFlair).one()
        assert stored.flair == 'Discussion'
        assert stored.text_color == ''
        assert stored.background_color == ''
        assert stored.blur_images is False
        assert stored.ap_id is None
        assert [f.id for f in community.flair] == [stored.id]

    def test_several_tags_all_become_flair(self, app, db_session):
        peer_instance(PEER)
        document = _group('memes', fields={'lemmy:tagsForPosts': [
            {'display_name': 'Discussion'},
            {'display_name': 'Meta'},
        ]})
        community = actor_json_to_model(document, '!memes', PEER)
        assert sorted(f.flair for f in community.flair) == ['Discussion', 'Meta']
        assert db.session.query(CommunityFlair).count() == 2

    def test_a_tag_with_an_empty_display_name_is_not_appended(self, app, db_session):
        """`if flair_obj:` -- find_flair_or_create returns None for a tag whose
        display_name is empty, and the branch must not append that None."""
        peer_instance(PEER)
        document = _group('memes', fields={'lemmy:tagsForPosts': [{'display_name': ''}]})
        community = actor_json_to_model(document, '!memes', PEER)
        assert community.flair == []
        assert db.session.query(CommunityFlair).count() == 0

    def test_tags_for_posts_that_is_not_a_list_is_ignored(self, app, db_session):
        peer_instance(PEER)
        document = _group('memes', fields={'lemmy:tagsForPosts': {'display_name': 'Discussion'}})
        community = actor_json_to_model(document, '!memes', PEER)
        assert community.flair == []
        assert db.session.query(CommunityFlair).count() == 0

    def test_new_style_tag_wins_when_both_spellings_are_present(self, app, db_session):
        """The two blocks are an if/elif, so a peer publishing both gets only
        the 'tag' one. Turning the elif into a second `if` would apply both and
        leave the legacy flair in place, which the count assertion catches."""
        peer_instance(PEER)
        document = _group('memes', fields={
            'tag': [{'type': 'CommunityPostTag', 'id': f'https://{PEER}/c/memes/tag/1',
                     'preferredUsername': 'New'}],
            'lemmy:tagsForPosts': [{'display_name': 'Legacy'}],
        })
        community = actor_json_to_model(document, '!memes', PEER)
        assert [f.flair for f in community.flair] == ['New']
        assert db.session.query(CommunityFlair).count() == 1

    def test_a_tag_without_a_display_name_is_skipped_and_the_rest_ingest(self, app, db_session):
        """FIXED -- `flair_dict = {'display_name': flair['display_name']}` used
        to be an unguarded read, so a legacy tag omitting the key raised
        KeyError out of actor_json_to_model. The community had already been
        committed by then, so the peer ended up with a community, no flair, and
        an exception at the caller -- a partially-applied ingest. The read now
        sits behind `if 'display_name' not in flair: continue`, matching the
        four optional guards below it, and the skip is logged.

        The malformed entry is deliberately in the MIDDLE of the list. The
        assertions are what separate 'skipped the bad entry' from 'skipped the
        loop': both good entries are present, so a guard that swallowed the
        whole list, or that abandoned the loop at the first bad entry, fails
        here even though nothing raised.

        Mutation, both directions, and they are distinct because this guard is
        a `continue` rather than an early return:

        - delete the guard: the KeyError comes back and this test fails on the
          exception, not on a count.
        - broaden it to `if True:` (or to a key every entry has, e.g.
          `'display_name' in flair`): every entry is skipped, nothing raises,
          and this test fails on the flair list and the row count instead.
        """
        peer_instance(PEER)
        document = _group('memes', fields={'lemmy:tagsForPosts': [
            {'display_name': 'Discussion'},
            {'id': 'https://x/1'},
            {'display_name': 'Meta'},
        ]})
        community = actor_json_to_model(document, '!memes', PEER)
        assert community is not None
        assert db.session.query(Community).count() == 1
        assert sorted(f.flair for f in community.flair) == ['Discussion', 'Meta']
        assert db.session.query(CommunityFlair).count() == 2


class TestConcurrentInsert:
    """`except IntegrityError: db.session.rollback(); return ...one()`.

    The handler is only reachable when a second writer commits the same
    ap_profile_id AFTER this call's early-return lookup found nothing and
    BEFORE its own commit. That window is real but narrow, so the test opens it
    deliberately rather than waiting for it.

    The seam is find_instance_id: `instance_id=find_instance_id(server)` is
    evaluated while the Community() arguments are built, after the early-return
    lookup has already run and before the insert. Substituting a function that
    commits the rival row and then delegates to the real one puts a genuine,
    already-committed duplicate in the database at exactly the right moment, and
    the collision that follows is a real Postgres unique violation on
    ap_profile_id rather than a raised stand-in.

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
        document = _group('memes')
        real_find_instance_id = activitypub_util.find_instance_id

        def commit_the_rival_row_first(server):
            instance_id = real_find_instance_id(server)
            rival = Community(name='memes', title='the row that won',
                              instance_id=instance.id, ap_domain=PEER,
                              ap_profile_id=document['id'].lower(),
                              ap_public_url=document['id'])
            db.session.add(rival)
            db.session.commit()
            return instance_id

        monkeypatch.setattr(activitypub_util, 'find_instance_id',
                            commit_the_rival_row_first)
        result = actor_json_to_model(document, '!memes', PEER)

        assert result is not None
        assert result.title == 'the row that won'
        assert result.ap_profile_id == document['id'].lower()
        assert db.session.query(Community).count() == 1
