"""find_flair_or_create resolves a peer-supplied flair dict to an existing
CommunityFlair, or creates one. DB-backed, no network -- unlike find_community
this works at the COMMUNITY level only and never looks at Post or post_flair.

Lookup order, first match wins:

1. 'id' -- CommunityFlair.ap_id == flair['id']
2. 'preferredUsername' -- CommunityFlair.flair == flair['preferredUsername'].strip(),
   scoped to community_id -- only tried if step 1 found nothing
3. 'display_name' -- CommunityFlair.flair == flair['display_name'].strip(),
   scoped to community_id -- only tried if steps 1 and 2 found nothing

When a row is found, several properties are written from the dict, and when
none is found a new CommunityFlair is built from the same properties. Every
property below accepts two spellings; the choice of NAME used to construct or
rename the flair also has two sources, but they are not spelling variants of
each other (a peer's 'display_name' and 'preferredUsername' are two distinct
input fields, not two names for one field) -- both are enumerated below for
completeness since both have the same if/elif shape.

Enumeration, derived fresh against this checkout with:

    podman-compose -f compose.test.yaml exec -T -w /app test-runner python -c "
    import ast
    src = open('app/activitypub/util.py').read()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.FunctionDef) and n.name == 'find_flair_or_create':
            func = n
    def key_of(test):
        if isinstance(test, ast.Compare) and isinstance(test.ops[0], ast.In):
            left = test.left
            if isinstance(left, ast.Constant):
                return left.value
        return None
    for node in ast.walk(func):
        if isinstance(node, ast.If):
            k1 = key_of(node.test)
            if k1 is None:
                continue
            body = node.orelse
            k2 = key_of(body[0].test) if len(body) == 1 and isinstance(body[0], ast.If) else None
            print(f'line {node.lineno}: if {k1!r} ... elif {k2!r}')
    "

Note that the command is reproduced above with the indentation this docstring
gives it; dedent it before feeding it to `python -c`, which rejects a leading
indent.

Output -- NINETEEN rows, not the ten this transcript used to carry. Both the
'found' update block and the 'not found' create block hold their own copy of
each pair, so each of the three genuine spelling pairs appears twice; two
further rows are the id-lookup guard and the preferredUsername/display_name
lookup priority, which are shape-alike but not spelling pairs. That accounts
for the first ten rows. The remaining nine are the SAME `elif`s seen a second
time in their own right: an `elif` is an `ast.If` sitting in its parent's
`orelse`, so `ast.walk` yields it as a node of its own, and it prints again
as an `if ... elif None` row. Nine and not ten because the id-lookup guard is
the one row of the ten whose elif is None already -- it has no nested `if` to
be revisited. The earlier transcript silently dropped all nine, which made
the output look like a clean one-row-per-pair listing when it is not.

`ast.walk` is breadth-first, so the ten parents print before the nine
children rather than interleaved with them. The script prints each row's line
number as `node.lineno`; those are omitted below since they move on the next
edit -- re-run the command to see them.

    if 'id' ... elif None                             -- ap_id lookup guard
    if 'preferredUsername' ... elif 'display_name'    -- lookup priority (not a spelling pair)
    if 'text_color' ... elif 'textColor'              -- update block
    if 'background_color' ... elif 'backgroundColor'  -- update block
    if 'blur_images' ... elif 'blurImages'            -- update block
    if 'display_name' ... elif 'preferredUsername'    -- update block: which name wins
    if 'text_color' ... elif 'textColor'              -- create block
    if 'background_color' ... elif 'backgroundColor'  -- create block
    if 'blur_images' ... elif 'blurImages'            -- create block
    if 'display_name' ... elif 'preferredUsername'    -- create block: which name wins
    if 'display_name' ... elif None                   -- the lookup priority's own elif
    if 'textColor' ... elif None                      -- update block elif
    if 'backgroundColor' ... elif None                -- update block elif
    if 'blurImages' ... elif None                     -- update block elif
    if 'preferredUsername' ... elif None              -- update block elif
    if 'textColor' ... elif None                      -- create block elif
    if 'backgroundColor' ... elif None                -- create block elif
    if 'blurImages' ... elif None                     -- create block elif
    if 'preferredUsername' ... elif None              -- create block elif

Three genuine dual-spelling property pairs, each needing both halves tested
in BOTH the update block and the create block (six coverage points, not
three): text_color/textColor, background_color/backgroundColor,
blur_images/blurImages. A test that supplies both spellings of one property
in the same call never exercises the elif at all (the if already matched),
so each half is tested with ONLY that spelling present.

Every Community factory call here is preceded by seed_community_owner(),
which builds the Instance and then the local User, per tests/README.md and
make_community's hardcoded instance_id=1 / user_id=1.

Coverage can be re-derived at any time with `coverage.json`'s per-file
`missing_lines` / `missing_branches`, restricted to find_flair_or_create's
own statements (its body ends where the sibling find_flair's `def` begins,
a few lines below) rather than read off the whole module. Doing so against
this file alone currently reports both empty for find_flair_or_create's
statements. (An earlier pass here mistakenly attributed a real gap -- the
create block's preferredUsername branch, `flair_text =
flair['preferredUsername']`, reached only when no display_name key is
present -- to the unrelated sibling function find_flair a few lines below
it; that gap is now closed by
TestNotFoundCreatesNewFlair.test_no_match_creates_new_flair_from_preferred_username_alone.)
"""
from app import db
from app.activitypub.util import find_flair_or_create
from app.models import CommunityFlair
from app.utils import get_task_session
from tests.factories import make_community, make_community_flair, seed_community_owner


class TestApIdLookup:
    """Mutation that fails this: neutralizing the `if 'id' in flair:` guard
    (e.g. `if 'id' not in flair:`) -- the test below supplies a matching
    'id' and asserts the SAME row comes back rather than a new one being
    created, so both directions of that guard are distinguishable."""

    def test_found_by_ap_id_returns_existing_row_without_creating_a_second(self, app, db_session):
        seed_community_owner()
        community = make_community('apidlookup')
        existing = make_community_flair(community, name='spoiler', ap_id='https://peer.example/tag/1')
        result = find_flair_or_create({'id': existing.ap_id}, community.id)
        db.session.commit()
        assert result.id == existing.id
        rows = CommunityFlair.query.filter_by(community_id=community.id).all()
        assert len(rows) == 1


class TestPreferredUsernameLookup:
    def test_found_by_preferred_username_when_no_id_key_present(self, app, db_session):
        """No 'id' key at all -- the ap_id branch is skipped entirely
        (existing_flair stays None, since the ap_id lookup guard's else
        runs when no 'id' key is present), so this exercises the
        preferredUsername branch in isolation. ap_id is
        pre-set on the factory row so the ap_id-backfill block later in the
        function is skipped -- see TestMissingIdKeyOnTheBackfill for what
        happens when it is not."""
        seed_community_owner()
        community = make_community('usernamelookup')
        existing = make_community_flair(community, name='nsfw', ap_id='https://peer.example/tag/2')
        result = find_flair_or_create({'preferredUsername': 'nsfw'}, community.id)
        assert result.id == existing.id


class TestDisplayNameLookup:
    def test_found_by_display_name_when_no_id_or_preferred_username_present(self, app, db_session):
        seed_community_owner()
        community = make_community('displaynamelookup')
        existing = make_community_flair(community, name='meta', ap_id='https://peer.example/tag/3')
        result = find_flair_or_create({'display_name': 'meta'}, community.id)
        assert result.id == existing.id


class TestLookupPriority:
    """Mutation that fails these: reordering the lookup chain (e.g. trying
    preferredUsername before ap_id, or display_name before
    preferredUsername)."""

    def test_ap_id_lookup_wins_over_preferred_username_when_both_would_match(self, app, db_session):
        seed_community_owner()
        community = make_community('apidwins')
        by_id = make_community_flair(community, name='decoy', ap_id='https://peer.example/tag/byid')
        by_username = make_community_flair(community, name='usernamematch', ap_id='https://peer.example/tag/byusername')
        result = find_flair_or_create({'id': by_id.ap_id, 'preferredUsername': 'usernamematch'}, community.id)
        assert result.id == by_id.id
        assert result.id != by_username.id

    def test_preferred_username_lookup_wins_over_display_name_when_both_would_match(self, app, db_session):
        """No 'id' key, so the ap_id branch is skipped and existing_flair
        starts None -- isolating the preferredUsername-vs-display_name
        choice. Both rows carry a pre-set ap_id so the later ap_id-backfill
        block (which reads flair['id']) is skipped for whichever row is
        found."""
        seed_community_owner()
        community = make_community('usernamewins')
        by_username = make_community_flair(community, name='usernamematch2', ap_id='https://peer.example/tag/u2')
        by_displayname = make_community_flair(community, name='displaymatch2', ap_id='https://peer.example/tag/d2')
        result = find_flair_or_create(
            {'preferredUsername': 'usernamematch2', 'display_name': 'displaymatch2'}, community.id)
        assert result.id == by_username.id
        assert result.id != by_displayname.id


class TestNotFoundCreatesNewFlair:
    def test_no_match_creates_new_flair_from_display_name(self, app, db_session):
        seed_community_owner()
        community = make_community('createfromdisplay')
        result = find_flair_or_create({'display_name': 'brand new'}, community.id)
        db.session.commit()
        assert result is not None
        assert result.flair == 'brand new'
        assert result.community_id == community.id
        persisted = CommunityFlair.query.filter_by(community_id=community.id).one()
        assert persisted.id == result.id

    def test_no_match_creates_new_flair_from_preferred_username_alone(self, app, db_session):
        """No 'display_name' key at all, so the create block's
        display_name check is False and control falls to its
        preferredUsername elif -- flair_text is set from
        flair['preferredUsername']. Every other preferredUsername-bearing
        test in this file either also supplies display_name (which wins,
        taking the display_name branch instead) or finds an existing row
        and lands on the update block, not this one. Mutation that fails
        this: deleting or negating this elif."""
        seed_community_owner()
        community = make_community('createfromusername')
        result = find_flair_or_create({'preferredUsername': 'username only'}, community.id)
        db.session.commit()
        assert result is not None
        assert result.flair == 'username only'
        assert result.community_id == community.id

    def test_display_name_takes_priority_over_preferred_username_for_new_flair_text(self, app, db_session):
        """Same priority as the lookup and the update block: in the create
        block, the display_name check is the `if` and the preferredUsername
        check is its `elif`, so display_name names the new row over
        preferredUsername when both are present. Mutation that fails this:
        swapping which branch is the `if` and which is the `elif`."""
        seed_community_owner()
        community = make_community('createpriority')
        result = find_flair_or_create(
            {'display_name': 'display wins', 'preferredUsername': 'username loses'}, community.id)
        assert result.flair == 'display wins'

    def test_neither_display_name_nor_preferred_username_present_returns_none_and_creates_nothing(self, app, db_session):
        """flair_text stays '' (falsy), so the final `if flair_text:` guard
        is False and the function returns None without adding a row --
        pins the else branch of that guard."""
        seed_community_owner()
        community = make_community('createnothing')
        result = find_flair_or_create({'text_color': 'red'}, community.id)
        assert result is None
        assert CommunityFlair.query.filter_by(community_id=community.id).count() == 0


class TestWhitespaceInThePeersFlairName:
    """The three `.strip()` calls, and the one place that has none.

    Derived against this checkout with:

        podman-compose -f compose.test.yaml exec -T -w /app test-runner python -c "
        import ast
        src = open('app/activitypub/util.py').read()
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, ast.FunctionDef) and n.name == 'find_flair_or_create':
                for c in ast.walk(n):
                    if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                            and c.func.attr == 'strip'):
                        print(ast.unparse(c))
        "

    Output, three lines:

        flair['preferredUsername'].strip()  -- lookup, preferredUsername arm
        flair['display_name'].strip()       -- lookup, display_name arm
        flair_text.strip()                  -- create, the new row's name

    Coverage cannot see any of them: each sits inside an expression on a
    line every existing test in this file already executes, so all three
    could be deleted with the full suite still green. Nothing fed a padded
    name, so stripping was asserted nowhere. Each test below constructs the
    input where the transformation decides the outcome.
    """

    def test_a_padded_preferred_username_finds_the_unpadded_row(self, app, db_session):
        """The lookup's preferredUsername arm. The stored row's flair is
        'spoiler' with no padding, so the query only matches if the peer's
        ' spoiler ' is stripped first.

        Production change that fails this: deleting
        `flair['preferredUsername'].strip()`'s `.strip()`, after which the
        lookup misses, the function falls to the create block, and a SECOND
        row is added -- both assertions catch it, the row count as well as
        the identity."""
        seed_community_owner()
        community = make_community('paddedusernamelookup')
        existing = make_community_flair(community, name='spoiler', ap_id='https://peer.example/tag/pad1')
        result = find_flair_or_create({'preferredUsername': ' spoiler '}, community.id)
        db.session.commit()
        assert result.id == existing.id
        assert CommunityFlair.query.filter_by(community_id=community.id).count() == 1

    def test_a_padded_display_name_finds_the_unpadded_row(self, app, db_session):
        """The lookup's display_name arm -- a textually separate `.strip()`
        from the preferredUsername arm's, so a mutation to either alone is
        invisible to the other's test. No 'preferredUsername' key at all,
        which is what makes the elif the arm taken.

        Production change that fails this: deleting
        `flair['display_name'].strip()`'s `.strip()`."""
        seed_community_owner()
        community = make_community('paddeddisplaylookup')
        existing = make_community_flair(community, name='meta', ap_id='https://peer.example/tag/pad2')
        result = find_flair_or_create({'display_name': '  meta\t'}, community.id)
        db.session.commit()
        assert result.id == existing.id
        assert CommunityFlair.query.filter_by(community_id=community.id).count() == 1

    def test_a_new_flair_is_created_with_its_name_stripped(self, app, db_session):
        """The create block's `flair_text.strip()`. Nothing matches, so a
        row is built, and the assertion is on the value that landed in the
        column -- not merely that a row exists.

        Production change that fails this: deleting that `.strip()`, after
        which the row is stored as ' brand new ' and the equality fails.
        The second assertion is what makes this test the create block's and
        not the lookup's: a lookup-arm mutation cannot reach it."""
        seed_community_owner()
        community = make_community('paddedcreate')
        result = find_flair_or_create({'display_name': ' brand new '}, community.id)
        db.session.commit()
        assert result.flair == 'brand new'
        persisted = CommunityFlair.query.filter_by(community_id=community.id).one()
        assert persisted.flair == 'brand new'

    def test_the_update_path_writes_the_name_back_unstripped(self, app, db_session):
        """PINS PRESENT BEHAVIOUR, NOT DESIRED BEHAVIOUR.

        The update block assigns `existing_flair.flair = flair["display_name"]`
        with no `.strip()`, unlike the lookup arms above it and the create
        block below it. So a peer sending a padded name twice gets two
        different outcomes from the same input: the first call creates the
        row as 'spoiler', and the second call finds that row (the lookup
        strips, so it matches) and then overwrites its name with the padded
        ' spoiler '. The stored value flips on the second delivery of an
        identical document.

        This test asserts the padded value, which is what the code does
        today. It is recorded as a defect in
        docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md and
        deliberately NOT fixed here, per this campaign's report-do-not-fix
        rule. If someone later adds the missing `.strip()`, this test fails
        -- which is the intent: the fix should be a deliberate update to
        this assertion, not something that slips through green.
        """
        seed_community_owner()
        community = make_community('unstrippedupdate')
        created = find_flair_or_create({'display_name': ' spoiler '}, community.id)
        db.session.commit()
        assert created.flair == 'spoiler', 'the create path strips'

        updated = find_flair_or_create(
            {'display_name': ' spoiler ', 'id': 'https://peer.example/tag/pad3'}, community.id)
        db.session.commit()
        assert updated.id == created.id, 'the lookup strips, so the same row is found'
        assert updated.flair == ' spoiler ', 'the update path does not strip'
        assert CommunityFlair.query.filter_by(community_id=community.id).one().flair == ' spoiler '

    def test_the_update_paths_preferred_username_arm_is_unstripped_too(self, app, db_session):
        """PINS PRESENT BEHAVIOUR, NOT DESIRED BEHAVIOUR.

        The same missing `.strip()`, on the update block's other arm:
        `existing_flair.flair = flair['preferredUsername']`. Reached by
        omitting 'display_name' entirely so the elif is what runs. Recorded
        and not fixed for the same reason as the test above.
        """
        seed_community_owner()
        community = make_community('unstrippedusernameupdate')
        existing = make_community_flair(community, name='nsfw', ap_id='https://peer.example/tag/pad4')
        result = find_flair_or_create({'id': existing.ap_id, 'preferredUsername': ' nsfw '}, community.id)
        db.session.commit()
        assert result.id == existing.id
        assert result.flair == ' nsfw '


class TestSessionParameter:
    def test_session_parameter_passed_explicitly_works_same_as_default(self, app, db_session):
        """Mutation that fails this: breaking the `if session is None: session
        = db.session` assignment so an explicitly-passed session is ignored
        or replaced."""
        seed_community_owner()
        community = make_community('explicitsession')
        result = find_flair_or_create({'display_name': 'viaexplicitsession'}, community.id, session=db.session)
        db.session.commit()
        assert result is not None
        assert result.flair == 'viaexplicitsession'
        assert CommunityFlair.query.filter_by(community_id=community.id, flair='viaexplicitsession').count() == 1


class TestSpellingPairsOnTheUpdateBlock:
    """The found/update block -- the branch taken when an existing
    CommunityFlair row was matched by the lookup chain, which updates that
    row's text_color/background_color/blur_images from whichever spelling
    is present. Each of the two tests below supplies only one spelling per
    property, alternated between the two tests, so every if AND every elif
    in this block is independently
    exercised and asserted -- not merely covered by branch coverage's
    weaker both-outcomes-of-the-whole-if bar.

    Mutation that fails EITHER test: negating the `in` check on the
    spelling it exercises (e.g. `"text_color" in flair` ->
    `"text_color" not in flair`), which would leave that property at its
    prior (unset) value instead of the dict's value."""

    def test_snake_case_spellings_update_the_existing_flair(self, app, db_session):
        seed_community_owner()
        community = make_community('snakeupdate')
        existing = make_community_flair(community, name='snaketarget', ap_id='https://peer.example/tag/snake')
        result = find_flair_or_create({
            'id': existing.ap_id,
            'text_color': '#111111',
            'background_color': '#222222',
            'blur_images': True,
        }, community.id)
        assert result.text_color == '#111111'
        assert result.background_color == '#222222'
        assert result.blur_images is True

    def test_camel_case_spellings_update_the_existing_flair(self, app, db_session):
        seed_community_owner()
        community = make_community('camelupdate')
        existing = make_community_flair(community, name='cameltarget', ap_id='https://peer.example/tag/camel')
        result = find_flair_or_create({
            'id': existing.ap_id,
            'textColor': '#333333',
            'backgroundColor': '#444444',
            'blurImages': True,
        }, community.id)
        assert result.text_color == '#333333'
        assert result.background_color == '#444444'
        assert result.blur_images is True


class TestSpellingPairsOnTheCreateBlock:
    """The not-found/create block -- the branch taken when no existing row
    was matched, which builds a new CommunityFlair from whichever spelling
    is present; the create-path counterpart of the class above. Coverage.py
    tracks these as separate branches from the update block's
    identically-named guards, since they are a distinct set of if/elif
    statements in the function body; a test set that only exercised the
    update block would leave this block's elifs uncovered."""

    def test_snake_case_spellings_are_used_for_the_new_flair(self, app, db_session):
        seed_community_owner()
        community = make_community('snakecreate')
        result = find_flair_or_create({
            'display_name': 'snakenew',
            'text_color': '#555555',
            'background_color': '#666666',
            'blur_images': True,
        }, community.id)
        db.session.commit()
        assert result.text_color == '#555555'
        assert result.background_color == '#666666'
        assert result.blur_images is True

    def test_camel_case_spellings_are_used_for_the_new_flair(self, app, db_session):
        seed_community_owner()
        community = make_community('camelcreate')
        result = find_flair_or_create({
            'display_name': 'camelnew',
            'textColor': '#777777',
            'backgroundColor': '#888888',
            'blurImages': True,
        }, community.id)
        db.session.commit()
        assert result.text_color == '#777777'
        assert result.background_color == '#888888'
        assert result.blur_images is True


class TestApIdBackfill:
    """The backfill block -- runs only when a row was FOUND (not created)
    and it has no ap_id yet. Reached via the preferredUsername or
    display_name lookups, since a row found via the ap_id lookup already
    has one."""

    def test_ap_id_is_set_from_flairs_id_when_the_existing_flair_has_none(self, app, db_session):
        seed_community_owner()
        community = make_community('apidbackfill')
        existing = make_community_flair(community, name='backfillme', ap_id=None)
        result = find_flair_or_create(
            {'id': 'https://peer.example/tag/backfilled', 'preferredUsername': 'backfillme'}, community.id)
        assert result.id == existing.id
        assert result.ap_id == 'https://peer.example/tag/backfilled'

    def test_ap_id_is_derived_via_get_ap_id_when_flairs_id_is_falsy(self, app, db_session):
        """flair['id'] is present but '' (falsy), so the `if flair['id']:`
        half of the backfill is False and get_ap_id() runs instead --
        pinning both the presence AND the truthiness of that key mattering,
        not merely its presence."""
        seed_community_owner()
        community = make_community('apidderived')
        existing = make_community_flair(community, name='deriveme', ap_id=None)
        result = find_flair_or_create({'id': '', 'preferredUsername': 'deriveme'}, community.id)
        assert result.id == existing.id
        assert result.ap_id == community.local_url() + f"/tag/{existing.id}"


class TestMissingIdKeyOnTheBackfill:
    """The backfill's absent-'id' arm. FIXED behaviour, not pinned defect.

    Once a row is found via preferredUsername or display_name (not ap_id)
    and that row's ap_id column is falsy, the backfill decides where the new
    ap_id comes from. It used to read `flair['id']` with no `'id' in flair`
    guard -- unlike every other optional-field read in this function -- so a
    flair dict with no 'id' key raised KeyError. The guard is now
    `if 'id' in flair and flair['id']:`, which sends an entry that names no
    usable id down the same else arm an entry with an EMPTY id already took:
    derive the ap_id locally via get_ap_id(). Nothing about the entry is
    dropped -- what is skipped is the peer's id, not the flair -- and the
    skip is logged so the substitution is not silent.

    Why this mattered: refresh_community_profile_task's legacy
    'lemmy:tagsForPosts' handling (the block guarded by
    `if len(community.flair) == 0` inside `'lemmy:tagsForPosts' in
    activity_json and ... "tag" not in activity_json`) builds `flair_dict =
    {'display_name': flair['display_name']}` plus optional
    text_color/background_color/blur_images -- NEVER an 'id' key. That task
    commits the community's refreshed profile fields BEFORE running the
    loop, so the KeyError was a partially-applied ingest: a committed
    community, no flair, and an exception at the caller. Contrast
    actor_json_to_model's OWN 'lemmy:tagsForPosts' handling, which
    conditionally copies 'id' across (`if 'id' in flair: flair_dict['id'] =
    flair['id']`) and so could not reach the KeyError through that key.

    The two callers also differ in the session -- and therefore the
    autoflush behaviour -- they pass, and that difference is the whole
    reason the crash held in one of them and not the other.
    refresh_community_profile_task takes its session from
    get_task_session(), which returns a plain `sqlalchemy.orm.Session(bind=
    db.engine)` (app/utils.py) with no autoflush override, so it defaults to
    autoflush=True. actor_json_to_model's 'lemmy:tagsForPosts' block calls
    find_flair_or_create with no session argument, defaulting to db.session
    -- Flask-SQLAlchemy configured app-wide with
    `session_options={"autoflush": False}` (app/__init__.py). Because
    refresh_community_profile_task's session autoflushes, two entries in the
    SAME peer-supplied 'lemmy:tagsForPosts' list sharing one display_name
    were enough, with no second activity or delivery: the first entry's call
    adds a CommunityFlair with ap_id=None (new_ap_id is None whenever 'id'
    is absent from flair_dict), autoflush makes that pending insert visible
    to the second entry's query, and the second call finds it, sees the
    falsy ap_id, and read the key that was not there.

    test_two_entries_sharing_a_name_under_autoflush_derive_a_local_ap_id
    reproduces exactly that, on a real get_task_session(). Its companion
    test_the_same_two_entries_under_autoflush_off_never_form_the_collision
    is why it has to: run the identical list against db.session and no
    collision forms at all, so a test written only against db.session goes
    green on the defective code and characterises nothing. That companion
    also records a SEPARATE consequence of the same autoflush asymmetry,
    reported and not fixed here: under autoflush=False the two entries
    produce two CommunityFlair rows with the same name for the same
    community, because neither call can see the other's pending insert.

    Is the backfill reachable from actor_json_to_model by some OTHER route,
    given that its own 'lemmy:tagsForPosts' handling copies 'id' when
    present? Two reasons say no, for its current call sites: first,
    actor_json_to_model only reaches its tag-processing block when the
    Community row was JUST newly inserted (the preceding `try:
    db.session.add(community); db.session.commit() except IntegrityError:
    ... return ...` returns immediately for an already-existing community,
    before the tag block), so there is never a pre-existing,
    already-persisted CommunityFlair row for that community_id for a
    name-based lookup to find. Second, even within that one call,
    db.session's autoflush=False means a same-call duplicate-name collision
    does not expose an unflushed insert to a later iteration's query -- so
    no in-loop trigger either, including in the sibling
    'tag'/CommunityPostTag branch (whose entries are copied verbatim as
    flair_dict = flair and so could themselves lack 'id').

    Mutation, both directions, on `if 'id' in flair and flair['id']:`:

    - narrowed back to `if flair['id']:` (deleting the guard): the KeyError
      returns. All three tests below fail.
    - broadened to `if 'id' in flair or flair['id']:` -- or to a bare
      `True`: the else arm becomes unreachable for an entry with an empty
      'id', which is what
      TestApIdBackfill.test_ap_id_is_derived_via_get_ap_id_when_flairs_id_is_falsy
      already pins.
    """

    def test_an_entry_with_no_id_key_derives_the_ap_id_instead_of_raising(self, app, db_session):
        """The guard in isolation, on the default session: an existing row
        with a null ap_id and a dict carrying no 'id' at all. The assertion
        is on the value that landed in the column, not merely that nothing
        raised -- a fix that returned early, or that skipped the whole
        backfill, would leave ap_id None and fail it."""
        seed_community_owner()
        community = make_community('missingidbackfill')
        existing = make_community_flair(community, name='noidkey', ap_id=None)
        result = find_flair_or_create({'preferredUsername': 'noidkey'}, community.id)
        db.session.commit()
        assert result.id == existing.id
        assert result.ap_id == community.local_url() + f"/tag/{existing.id}"
        assert CommunityFlair.query.filter_by(community_id=community.id).count() == 1

    def test_two_entries_sharing_a_name_under_autoflush_derive_a_local_ap_id(self, app, db_session):
        """The reachable path, reproduced on the session the reachable
        caller actually uses.

        The list is the shape refresh_community_profile_task builds: three
        legacy entries, none with an 'id', two of them sharing one
        display_name. On get_task_session()'s autoflush=True session the
        first 'Discussion' entry's pending insert is visible to the second
        entry's query, so the second call lands on the backfill with no
        'id' to read -- the exact collision that used to raise KeyError
        after the caller had already committed.

        The row counts are what make this stronger than 'nothing raised':
        two rows, not one (a fix that abandoned the loop or the second entry
        would give one) and not three (autoflush is genuinely deduplicating
        by name, which is what distinguishes this from the companion test
        below).
        """
        seed_community_owner()
        community = make_community('autoflushbackfill')
        entries = [{'display_name': 'Discussion'},
                   {'display_name': 'Discussion'},
                   {'display_name': 'Meta'}]

        session = get_task_session()
        try:
            returned = [find_flair_or_create(entry, community.id, session) for entry in entries]
            session.commit()
        finally:
            session.close()

        assert all(flair is not None for flair in returned)
        db.session.rollback()
        rows = CommunityFlair.query.filter_by(community_id=community.id) \
            .order_by(CommunityFlair.flair).all()
        assert [row.flair for row in rows] == ['Discussion', 'Meta']
        discussion, meta = rows
        # The colliding entry took the derive arm; the entry that created its
        # own row never reaches the backfill at all, so its ap_id stays null.
        assert discussion.ap_id == community.local_url() + f"/tag/{discussion.id}"
        assert meta.ap_id is None

    def test_the_same_two_entries_under_autoflush_off_never_form_the_collision(self, app, db_session):
        """Why the test above cannot be written against db.session.

        Identical input, default session, autoflush=False: the first
        entry's insert is still pending and invisible, so the second
        entry's query finds nothing and CREATES a second row rather than
        reaching the backfill. This test therefore passed against the
        DEFECTIVE code exactly as it passes now -- it is here to record
        that, not to characterise the guard.

        PINS PRESENT BEHAVIOUR, NOT DESIRED BEHAVIOUR for the count: two
        rows named 'Discussion' for one community is duplicate flair, a
        consequence of the same autoflush asymmetry. Reported in this
        campaign's findings and deliberately not fixed here.
        """
        seed_community_owner()
        community = make_community('noautoflushbackfill')
        entries = [{'display_name': 'Discussion'},
                   {'display_name': 'Discussion'},
                   {'display_name': 'Meta'}]

        returned = [find_flair_or_create(entry, community.id) for entry in entries]
        db.session.commit()

        assert all(flair is not None for flair in returned)
        rows = CommunityFlair.query.filter_by(community_id=community.id).all()
        assert sorted(row.flair for row in rows) == ['Discussion', 'Discussion', 'Meta']
        assert all(row.ap_id is None for row in rows)
