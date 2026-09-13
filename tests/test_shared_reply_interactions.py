"""`app/shared/reply.py`'s reader interactions and its delete/restore lifecycle.

SCOPE. Sub-project 40, Groups A and C of six: 73 of the module's 304 missing
statements and 46 of its 172 missing arcs. Groups B, D, E and F --
`make_reply`/`edit_reply`, `report_reply`, the two moderator verbs, and the
four reply-only verbs -- are sub-projects 41 onward. The 304/172 module totals
and the 73/46 split are the design document's measurement, not this file's:
`docs/superpowers/specs/2026-09-12-coverage-reply-ac-40-design.md`, the
six-row group table.

THIS MODULE IS `app/shared/post.py`'S TWIN, and that is why this round is
short. Twelve of its sixteen functions mirror functions the campaign closed in
sub-projects 34 through 39, so the harness below is inherited rather than
invented. What is NOT inherited is listed under WHAT IS NEW.

WHAT TRANSFERS FROM tests/test_shared_post_interactions.py UNCHANGED:

  - NO `user=` ESCAPE HATCH. `edit_reply` takes one; no Group A function does.
    `vote_for_reply:21`/`:28`, `bookmark_reply:58`, `remove_bookmark_reply:76`
    and `subscribe_reply:95` read `current_user` or call `authorise_api_user`
    with no way around it, so every test here supplies a real user.

  - THE SRC_API ARM NEEDS NO REQUEST CONTEXT. `get_ip_address`
    (app/__init__.py:68-77) wraps its `request` read in `try/except
    RuntimeError` -- its own comment at `:76` names the case, "no application
    or request context (e.g. a CLI command)" -- and returns `''`.
    `user_ip_banned` then sees a falsy IP and returns None, so `vote_for_reply
    :30`'s guard lets a context-free call through. `web_ctx` is reserved for
    the SRC_WEB arms, which need `flash` and template rendering. This is
    register entry D394, which recorded the same claim being asserted the
    other way round for `app/shared/post.py` and disproved by probe; this
    file's API-arm test re-confirms it for `app/shared/reply.py` by running
    `bookmark_reply(..., SRC_API, ...)` outside any context.

  - WHAT BLOCKS AN API TEST IS AN `ap_id`, NOT CONTEXT. `authorise_api_user`
    requires `ap_id is None`, `verified` true and `banned` false
    (app/utils.py:3628-3629, one compound condition that also rejects
    `deleted`). `tests/factories.py:41`'s `make_user` defaults `local=False`
    and therefore mints a non-None `ap_id`, so `_seed_reply` passes
    `local=True`. Register entry D393(f).

REAL REDIS IS SHARED ACROSS THE WHOLE TEST SESSION. `reply.vote()`
(app/models.py:3311, in `class PostReply` which opens at app/models.py:2887)
sets or increments `votes_cast_{today}_{user_id}` on the real redis the
compose stack shares, and tests/conftest.py:131 resets id sequences after
every test -- so a later test whose user reuses that id inherits a stale
count. THE RULE FOR THIS FILE IS THAT EVERY TEST COMPLETING A REAL VOTE
CLEARS THE KEY IN A `finally`. Ten tests now do -- the five in
`TestVoteForReplySourceAndPermission` that reach `:36` and the five in
`TestVoteForReplyGuardsAndReturns` that do. It was five before task 7's
mutation pass added four vote-completing tests and fix round 1 a fifth
(the emoji-reversal test), and the count is kept current
here rather than left to drift, because the rule this paragraph states is
enforced by nothing but the count. The four that refuse earlier
deliberately do not, because no vote completed and the absence is part of what
they assert: `:23`/`:25`'s permission returns, `:31`'s abort and `:34`'s. The
undo test clears the key ONCE for its two calls, which is correct rather than an
oversight -- see its docstring. The pattern and the reason are
tests/test_shared_post_interactions.py:153-173.

WHAT IS NEW, AND HAS NO POST TWIN:

  - `subscribe_reply:94` JOINS `Post` and filters `deleted=False` on BOTH the
    reply and its parent post. `subscribe_post` had no join. A seeded reply
    needs a live parent, and `.one()` raises rather than returning None.

  - `make_post_reply` (tests/factories.py:455-474) DOES NOT SET `path`. Its
    constructor sets exactly `user_id`, `post_id`, `community_id`,
    `instance_id`, `body`, `posted_at` and `deleted`; `path`
    (app/models.py:2898) is a nullable ARRAY column with no default, so a
    factory reply has `path is None`. So `delete_reply:256`'s `if reply.path:`
    and `restore_reply:282`'s are FALSE by default and their true arms need a
    path seeded explicitly. A single-element path makes `reply.path[:-1]` an
    EMPTY tuple, and THAT IS NOT MERELY A DIFFERENT INPUT TO THE RAW SQL `IN`
    CLAUSE -- it is an illegal one. Measured in the container against the live
    test database (sqlalchemy 2.0.52, `postgresql+psycopg2`) with
    `delete_reply:257`'s own statement text: `(1, 2)` and `(1,)` both run and
    return no rows, `()` raises `ProgrammingError: (psycopg2.errors.SyntaxError)
    syntax error at or near ")"`, because psycopg2 renders it as `where id in
    ()`. AND A ONE-ELEMENT PATH IS REACHABLE IN PRODUCTION, so that raise is a
    live 500 rather than a curiosity -- see the DEFECTS section below, which
    enumerates all five writers of the column. `TestDeleteReply` AND
    `TestRestoreReply` EACH seed the three-element shape `app/models.py` builds,
    and NEITHER tests the empty tuple, because the cli importer that can produce
    one cannot be simulated from a factory reply; `TestDeleteReply`'s class
    docstring carries the full probe and `TestRestoreReply`'s points at it,
    `:283` being `:257` with one character changed. `child_count`
    (app/models.py:2899) is likewise unset by the factory, but has a column
    default of 0 rather than None.

    BOTH COLUMN FACTS WERE LATER RE-DERIVED BY A SECOND METHOD THAT FAILS
    DIFFERENTLY, because reading a `db.Column(...)` line cannot see a later
    override of the mapped attribute. Runtime introspection of the mapped table
    inside the container reports `PostReply.__table__.c['path']` as
    `ARRAY(Integer())` with `nullable=True`, `default=None` and
    `server_default=None`, and `c['child_count']` with `default=0`. It agrees
    with the source and is blind to a different thing: a value some other code
    assigns at insert time. `TestRestoreReply`'s docstring records the run.

  - A `Site` ROW WITH id 1 IS NEEDED FOR TWO UNRELATED REASONS, and
    `_seed_reply` seeds one for neither. The render chain below is the first;
    the second is `can_downvote`'s `Site` read (app/utils.py:2443-2445), which
    binds SRC_API tests that never render -- see `_seed_reply`'s own docstring
    and `TestVoteForReplySourceAndPermission`'s. THE RENDER CHAIN:
    `subscribe_reply:131` is the first
    statement in this round to render, and it fails without the row --
    `AttributeError: 'NoneType' object has no attribute 'default_theme'`, not a
    skipped lookup. The chain is app/utils.py:75 (this codebase's own
    `render_template`, which wraps Flask's) -> `current_theme()`
    (app/utils.py:3228-3240). `:3230`'s `if hasattr(g, 'site')` is FALSE under
    `web_ctx`, because `test_request_context` never runs `before_request`, so
    `:3233` falls back to `Site.query.get(1)`. `:3235` then does NOT rescue it:
    `User.theme` (app/models.py:1031, in `class User` which opens at
    app/models.py:973) is `db.Column(db.String(20), default='')`, so a
    `make_user` user has `theme == ''` and it is the `!= ''` half of `:3235`'s
    compound test that fails, not the `is not None` half. Control reaches
    `:3238`, which dereferences the None from `:3233`.

    WITH a `Site` row the same two steps still do not produce a theme:
    `Site.default_theme` (app/models.py:3975, in `class Site` which opens at
    app/models.py:3942) ALSO defaults to `''`, so `:3238`'s `is not None` guard
    is satisfied and it returns `''` rather than its `'piefed'` literal --
    which makes app/utils.py:76's `if theme != ''` short-circuit before any
    filesystem check and `:79` render the base template. A later test that
    wants a THEMED render must set `default_theme` explicitly; `make_site()`
    alone will not give it one. All of this was measured, not read.

    Tests that render call `make_site()` (tests/factories.py:353) themselves --
    as does any test reaching `can_downvote`, per the two-reasons note above;
    the bookmark tests above do not, because `bookmark_reply` and
    `remove_bookmark_reply` flash and fall off the end of the function without
    rendering anything. THE TWO REASONS ARE NOT ALWAYS SEPARATE: a TEMPLATE can
    be the thing that reaches `can_downvote`. `post/_comment_voting_buttons.html`
    line 10 reads `can_downvote(current_user, community,
    communities_banned_from_list)` -- a Jinja global registered at
    app/request_hooks.py:54 -- and `vote_for_reply:51` passes no
    `can_downvote_here` to short-circuit it, so every test rendering THAT
    template needs the row on both counts at once.
    `post/_reply_notification_toggle.html`, the other template this round
    renders, calls neither gate and needs it for the theme chain alone.
    The OTHER renderer in this module is
    `vote_for_reply:51`, the only other `render_template` call in
    app/shared/reply.py -- `delete_reply` and `restore_reply` never render, and
    neither reaches `can_downvote` (`grep -n "can_upvote\|can_downvote"
    app/shared/reply.py` gives `:15`, the import, and `:22`/`:24` inside
    `vote_for_reply`, nothing else), so BOTH reasons are absent and the
    delete/restore lifecycle tests need no `Site` row -- a prediction that is
    now EXECUTED FOR BOTH HALVES OF THE PAIR rather than merely read:
    `TestDeleteReply`'s five tests and `TestRestoreReply`'s six call
    `make_site()` nowhere and pass. THE GREP ABOVE IS ALSO NO LONGER THE ONLY
    EVIDENCE FOR `restore_reply`. A grep is file-wide and textual and cannot
    tell which function a matching line falls in; `TestRestoreReply` re-derived
    the same conclusion from an `ast.walk` over the `restore_reply` FunctionDef
    node ALONE, which is scope-exact and structurally blind to a call made
    through a string name. The two fail in opposite directions and agree. The
    module's one other
    `Site` touch is `Site.admins()` at `:365`, inside `report_reply`, which is
    a later sub-project's.
    `subscribe_reply` additionally has two statements no production source
    value can reach -- see `TestSubscribeReply`'s docstring for what `:98` does
    to the web arm.

FIVE DEFECTS ARE RECORDED HERE. **DEFECTS 3 AND 5 HAVE SINCE BEEN FIXED** --
the repository's owner asked for both after the coverage round closed, and the
work is register entry D517. This section is kept in the past tense for them
rather than deleted, because the tests below were written against the broken
behaviour and a reader needs to know what they were built to witness. The other
three stand as recorded. THREE OF THE FIVE ARE PINNED BY TESTS IN THIS FILE;
DEFECT 4 IS NOT, AND CANNOT BE FROM A FACTORY REPLY. The fifth, DEFECT 5, was found by task 7's mutation pass and is
stated at the end of this section. Taking the unpinned pair first, because a later reader of the
`path` bullet above is sent here for them.

DEFECT 3 -- `flask lemmy-import` WRITES ONE-ELEMENT PATHS, AND
`delete_reply:257` AND `restore_reply:283` THEN RAISE. The column has FIVE
writers, listed below. THIS ENUMERATION IS THE PRODUCT OF TWO DIFFERENT
METHODS, AND THE FIRST VERSION OF IT -- WHICH USED ONE -- MISSED WRITER 5 AND
STILL CALLED ITSELF COMPLETE. That miss is the reason the method is spelled
out here rather than the result alone:

  METHOD A, an AST walk of every `.py` under `app/`, collecting every
  assignment whose target is an attribute named `path` and every `path=`
  keyword in any call. Finds writers 1-4 and, being attribute-based,
  STRUCTURALLY CANNOT SEE A RAW-SQL WRITER.

  METHOD B, an AST walk collecting every string CONSTANT containing both
  `post_reply` and the word `path`, then filtered for `update`/`insert`.
  Finds writer 5, which method A cannot reach by construction.

  The first attempt used neither: it was `grep -rn "\\.path\\b" --include=*.py
  app/` followed by a hand-written filter `grep -E "\\.path\\s*=|path=|\\.path
  \\["`. app/cli.py:1929 DOES appear in that grep's output, because it contains
  `reply_path.path` -- and the hand filter discarded it, because `SET path =`
  has a space before the `=` and no dot before the `path`. A single filtered
  grep is not an enumeration; two methods that fail differently are closer to
  one.

  1. app/models.py:3053-3059, in `class PostReply` (opens at
     app/models.py:2887) -- the ActivityPub/web reply creator. Top-level gets
     `[0, reply.id]`, nested gets `in_reply_to.path[:] + [reply.id]`. ALWAYS
     >= 2 elements, and `:3060`'s `reply.root_id = reply.path[1]` would raise
     IndexError if it were ever shorter, which corroborates the convention
     independently. INCLUDES THE REPLY'S OWN ID as the last element.
  2. app/cli.py:694 and `:715`, inside `@app.cli.command("lemmy-import")`
     (app/cli.py:275). `piefed_path` is built at `:664-670` from
     `path_parts[1:-1]` -- ANCESTORS ONLY, THE REPLY'S OWN ID EXCLUDED, and
     `'0'` filtered out by `:667` and unmapped ancestors by `:669`. For a
     first-level nested comment whose Lemmy ltree path is `0.<parent>.<self>`
     AND WHOSE PARENT WAS MAPPED, `path_parts[1:-1]` is `['<parent>']`, so
     `path == [parent_id]`, `tuple(path[:-1])` is `()`, and both raw-SQL
     statements raise the `psycopg2.errors.SyntaxError` measured above. THE
     SCOPE MATTERS: a top-level comment (`0.<self>`) yields `[]`, and so does a
     first-level one whose parent is NOT in `lemmy_to_piefed_comment`, because
     `:669` skips it -- both are falsy and merely skip the update. WHAT RAISES
     IS ANY IMPORTED REPLY WITH EXACTLY ONE MAPPED ANCESTOR, which is the
     mapped-parent first-level case but is NOT only that case: for a deeper
     comment `0.A.B.<self>`, `path_parts[1:-1]` is `['A', 'B']` and `:669`
     drops whichever of the two is unmapped, leaving one element. An earlier
     wording of this paragraph said the scope was "exactly" the first-level
     case, which understates the reach; the same correction is carried in D497.
  3. app/api/alpha/views.py:686, written by `calculate_path` (`:669`). Depth 0
     gives `[0, reply.id]`, depth 1 `[0, parent_id, reply.id]`, depth > 1 a
     longer walk, committed at `:687`. ALWAYS >= 2. Cleared, no finding -- but
     see the note on self-healing below, which is about WHEN it runs, not what
     it writes.
  4. app/post/util.py:79, inside `create_real_reply` (`:53`). Its own comment
     at `:54` says "Create a PostReply instance (not persisted to DB)", and
     the file contains no `db.session.add`, `flush` or `commit` at all. A
     display object that never reaches the column. Cleared, no finding. THE
     ONLY NON-PERSISTING WRITER OF THE FIVE.
  5. app/cli.py:1929, `UPDATE post_reply SET path = reply_path.path`, inside
     `@app.cli.command("populate_post_reply_for_api")` (app/cli.py:1906),
     committed at `:1934`. A recursive CTE whose base case is
     `ARRAY[0, id]` for `parent_id IS NULL` and whose step is
     `rp.path || pr.id`, so it emits the app/models.py convention and is
     ALWAYS >= 2. Cleared, no finding -- and it is the REMEDY, see below.
     THIS IS THE WRITER THE FIRST ENUMERATION MISSED.

So on any instance that has run `flask lemmy-import`, the author of an
imported first-level reply with a mapped parent can neither delete nor restore
it. THIS IS REACHABLE, NOT LATENT.

AND IT DOES NOT SELF-HEAL. `calculate_path` (writer 3) would rewrite a broken
row into the right convention, but its ONLY call site is
app/api/alpha/views.py:735, guarded at `:734` by `if not reply.path:`. A
ONE-ELEMENT PATH IS TRUTHY, so the repair never fires on exactly the rows that
need it. `grep -rn "calculate_path(" --include=*.py app/` gives the definition
at `:669` and that one call, nothing else.

THE REMEDY EXISTS AND IS WRITER 5. `flask populate_post_reply_for_api` rebuilds
EVERY path from the `parent_id` chain in the app/models.py convention,
unconditionally, so running it repairs the rows both defects describe. Any
register entry for defect 3 or 4 that names the 500 without naming this
command is worth less than one that names both.

DEFECT 4 -- THE PERSISTING WRITERS DISAGREE ON THE PATH CONVENTION, so
`path[:-1]` means different things depending on who wrote the row. FOUR OF THE
FIVE PERSIST -- 1, 2, 3 and 5; only writer 4 does not -- and three of those
four terminate the array with the reply's own id. app/cli.py:664-670 (writer 2)
does not. Every `tuple(path[:-1])` site in the codebase -- app/shared/reply.py
`:258`, `:284`, `:418`, `:453`, app/activitypub/util.py `:2264`, `:2318`,
`:2340`, `:2381`, and app/post/routes.py:1997 -- assumes the majority
convention. Against a cli-imported row they drop the IMMEDIATE PARENT, a
genuine ancestor: a path of `[grandparent, parent]` yields `(grandparent,)`, so
the parent's `child_count` is never adjusted and the count under-reports by one
level. (An earlier version of this paragraph said "the two persisting writers",
which was the four-writer enumeration's arithmetic, not the five-writer one's.)

NEITHER DEFECT 3 NOR DEFECT 4 IS PINNED BY A TEST HERE, and that is a decision
rather than an oversight: reaching either needs a row shaped by the cli
importer, which no factory in `tests/factories.py` produces and which a unit
test of `delete_reply` has no business simulating. Both go to the campaign
register as REACHABLE, each naming `flask populate_post_reply_for_api` as the
existing remedy.

THE TWO THAT ARE PINNED. `restore_reply:279-280`
increments one counter (`reply.post.reply_count`) where `delete_reply:251-254`
decrements three (`reply.post.reply_count`, `reply.post.reply_count_cross_posted`
and `reply.community.post_reply_count`). THE RANGES STOP WHERE THEY DO ON
PURPOSE: `delete_reply:255` and `restore_reply:281` both adjust
`reply.author.post_reply_count`, both sit at four-space indent OUTSIDE the
`if not reply.author.bot:` guard that opens each block, and they mirror each
other exactly. The asymmetry is confined to the bot-guarded block, so a test
pinning it must read `post.reply_count`, `post.reply_count_cross_posted` and
`community.post_reply_count` and must NOT read `author.post_reply_count`,
which is symmetric and would witness nothing. Separately,
`vote_for_reply:22`/`:24` apply `can_upvote`/`can_downvote` only inside the
`if src == SRC_API:` arm, the `else` at `:26-28` having no equivalent -- and
the twin `vote_for_post` DOES gate its web arm (app/shared/post.py:43-48), so
this is a divergence between mirrored functions rather than a uniform policy.
ALL FIVE ARE NOW IN THE CAMPAIGN REGISTER, ADDED BY TASK 8:
D496 the counter asymmetry, D497 defect 3, D498 defect 4, D499 the API-only
permission check, D500 defect 5. D497 and D500 carry material this docstring
does not. `docs/superpowers/specs/2026-09-12-coverage-reply-ac-40-design.md`
still carries the arguments. The tests that pin today's voting behaviour are
`TestVoteForReplySourceAndPermission` below; the delete/restore asymmetry is
now pinned by `TestRestoreReply`'s
`..._a_delete_restore_cycle_leaves_two_counters_permanently_low`, which asserts
the wrong numbers deliberately so that a fix must edit a test. NO TEST IN THIS
FILE MAKES THE
VOTING ASYMMETRY EXECUTABLE, and that is a decision rather than a gap: the
construction that would -- a web downvote against a `Site` with
`enable_downvotes` False, landing where the API arm refuses -- also makes
`post/_comment_voting_buttons.html` line 10 false, which deletes the
`voted_down` markup that `TestVoteForReplyGuardsAndReturns`'s downvote test
needs as its witness for `:49`. The two cannot be had in one test, and `:49`'s
witness won.

DEFECT 5 -- A 'reversal' VOTE SKIPS BOTH API PERMISSION GATES. `:22` and `:24`
are each conjoined with a direction LITERAL, and the API arm passes a third
value: app/api/alpha/utils/reply.py:435-441 maps `score` 1 to 'upvote', -1 to
'downvote' and EVERYTHING ELSE to 'reversal', and `:444` hands that to
`vote_for_reply`. Under 'reversal' both first conjuncts are false, so neither
`can_upvote` nor `can_downvote` is consulted at all and control reaches `:30`.
A bot -- refused by `can_upvote` at app/utils.py:2481 and by `can_downvote` at
`:2437` -- can therefore still remove its vote through the API. THIS IS A
NARROWER HOLE THAN IT SOUNDS and the narrowing is part of the finding: a
'reversal' only reaches a vote at all when a vote already exists, because
`PostReply.vote` remaps it at app/models.py:3316 only `if existing_vote` and
`:3321` then asserts the direction is 'upvote' or 'downvote'. So the gates are
bypassed for UNDOING a vote, not for casting one. It was found by neutralising
`:22`'s first conjunct, which nothing in the file had killed.

**DEFECT 5 IS NOW FIXED, AND THE TEST THAT PINNED IT HAS BEEN INVERTED AS ITS
OWN DOCSTRING REQUIRED.** `:26`-`:39` gate a 'reversal' on the permission that
would have cast the vote being undone -- `can_upvote` for a positive `effect`,
`can_downvote` for a negative one -- because 'reversal' names no permission of
its own. `app/shared/post.py` took the identical arm in the same change, so the
mirrored pair did not diverge. The pin is now
`..._a_reversal_is_refused_when_the_permission_that_cast_the_vote_is_gone`, with
`..._a_permitted_voter_can_still_reverse_their_own_vote` as its same-mechanism
positive control. Register entry D517.

THREE EXTENSIONS TO DEFECT 5 ARE RECORDED IN D500 -- in substance, not in
these words -- and are repeated
here so they are not lost with a report. (i) The gate-free call also carries `emoji`:
`post_reply_like` forwards `data['emoji']`, and the remapped reversal lands on
app/models.py:3325-3331, so a refused user can also REWRITE THE EMOJI on the
existing vote. Fix round 1's `..._a_web_reversal_that_only_rewrites_the_emoji_
marks_neither` executes that path (through the web arm, for its own reasons).
(ii) `vote_for_post` is the twin: app/shared/post.py:31-37 carries the identical
two-literal gate and `post_like` the identical `score`->'reversal' mapping, so
any fix must land in both or create exactly the mirrored-pair divergence this
file registers as a defect class. (iii) Under `python -O` the character of the
hole changes: app/models.py:3321's assert disappears, so `score: 0` with NO
existing vote falls through to the else-branch, `effect` becomes -1 and a NEW
DOWNVOTE is cast past both gates. Nothing in this repository runs `-O` or sets
`PYTHONOPTIMIZE`, so it is hypothetical here -- but it converts a withdrawal
hole into a vote-CASTING hole and belongs in the entry.

WHAT TASK 7'S MUTATION PASS LEFT OPEN, listed because a survivor that is merely
reported dies with the report. Each of these is a mutation of a line in Groups A
or C that all 34 tests here pass against. BE EXACT ABOUT WHEN THAT WAS MEASURED:
the survivors were RE-RUN at 33 tests, and FIX ROUND 1 -- commit `42106cbc`,
the same one that strengthened them -- then added the 34th (the emoji-reversal
test) and incremented this count in the same commit WITHOUT re-running them.
Fix round 2 was documentation-only and added no test. Nothing was re-executed at 34. What was done
instead is a hand check by the round's final whole-branch review, which read all
seventeen against the 34th test's path and found that none of them touches it --
it takes no bookmark, no subscription and no delete/restore path, and reaches
none of `:30`, `:94` or `:121`. So the list below stands on a 33-test
measurement plus that argument, not on a 34-test measurement. NONE of them is
claimed to be an equivalent mutant -- they are unclosed:

THESE ARE OPEN AT `tests/test_shared_*.py` SCOPE, WHICH IS NOT THE SCOPE THE
CAMPAIGN'S RATCHET USES, and the two halves of this file's evidence do not have
the same unit. The COVERAGE claim is itself two measurements at two different
units: `73` statements and `46` arcs closed IS full-suite; "no coverage added
by the mutation round" is NOT -- it is a single-file measurement (`27` / `33` /
`34 passed`, this file only). The MUTATION evidence
below, and every "N passed" in the docstrings here, is shared-suite: twelve
`tests/test_shared_*.py` files, 520 tests. None of the mutants below was ever
re-run at full-suite scope, so "open" means "open at the narrower scope". Some of
them probably die at the wider one -- `60` and `78` are the obvious
candidates -- so the list is conservative in the direction of reporting too
many, not too few. (`83` is not a candidate: it is closed at this scope by the
strengthened flash test below, whose docstring pins it.)

  - `30s/ or user_ip_banned()//`. No test makes `user_ip_banned()` true.
    Closing it needs a request IP plus a `banned_ip_addresses()` row, and that
    helper is `@cache.memoize`d, which is a cross-test hazard this task did not
    take on.
  - `60s/, user_id=user_id//`, `60s/post_reply_id=reply_id, //`,
    `78s/, user_id=user_id//`, `100s/entity_id=reply_id, user_id=user_id,/
    entity_id=reply_id,/` and its entity-side twin. Single-row fixtures: one
    bookmark, one subscription, one user, so "scoped to this user and this row"
    and "the only row" are the same lookup.
  - all four `:94` variants -- dropping `deleted=False` from the reply, dropping
    the `Post` join entirely, joining on the wrong column, and dropping the
    reply identity. No test seeds a deleted reply or a deleted post, so the
    guard this line exists for has never been executed against a row it should
    refuse.
  - `121`/`122`, the new subscription's `name`. Nothing reads the column.
  - `275s/filter_by(id=reply_id, user_id=user_id, deleted=True)/filter_by(
    user_id=user_id, deleted=True)/` -- `restore_reply`'s REPLY-IDENTITY drop,
    the restore twin of `:20`/`:27`. The AUTHOR-FILTER half of `:275` IS closed
    (`test_a_non_author_cannot_restore_another_users_reply`); the identity half
    is not, because every `TestRestoreReply` test seeds exactly one deleted
    reply, so "this reply" and "the only deleted reply of this author" are the
    same row. THIS BULLET WAS MISSING UNTIL TASK 8, which is why the list below
    it enumerated sixteen survivors against a reconciled count of seventeen.
    The register carries all seventeen as D513; this list is the copy, not the
    original.
  - `261s/task_selector('delete_reply'/task_selector('restore_reply'/` and the
    same swap at `289`. Both task bodies take the same kwargs and both return
    early on `community.private`, so the wrong name is silent here. The twin
    mutant at `38` inside `vote_for_reply` DOES die, because
    `task_selector('vote_for_post', reply_id=...)` is a TypeError -- so this is
    a property of the argument lists, not of the dispatcher being covered.
  - `61s/if not existing_bookmark:/if False:/` and
    `79s/if existing_bookmark:/if False:/`. These are different in kind, and the
    LABEL THEY CARRIED IN TASK 7 -- "not executable at this scope" -- WAS WRONG,
    contradicted by the same report's own coverage paste. The lines missing at
    this scope are `62`, `63`, `72`, `80`, `81`, `85` and `90`; `:61` and `:79`
    are NOT among them, so both lines ARE executed here. What is dead is the
    branch, and in BOTH lines the dead arm is the TRUE arm -- but for OPPOSITE
    fixture reasons. `:61`'s true arm (`not existing_bookmark`, i.e. no bookmark
    found) is never taken because every `bookmark_reply` test here seeds a
    bookmark first, so the condition is always False. `:79`'s true arm
    (`existing_bookmark`, i.e. a bookmark found) is never taken because no test
    here seeds a bookmark before calling `remove_bookmark_reply`, so ITS
    condition is always False too -- see
    `test_removing_a_bookmark_that_does_not_exist_flashes_on_the_web`'s own
    docstring, which says so directly. (That pointer read `:662` until task 8.
    `:662` is a blank line; the reference had drifted and nothing noticed,
    which is why it is by NAME now. A line-number self-reference inside a file
    whose own docstrings keep growing rots silently.) That makes them equivalent-AT-THIS-SCOPE, not unexecutable -- and
    each is closable HERE, today, by a different call: `:61` by one web
    `bookmark_reply` call against a reply with NO bookmark seeded, which
    executes `:62`/`:63`; `:79` by seeding a bookmark first and THEN calling
    `remove_bookmark_reply`, which executes `:80`/`:81`. Neither closure was
    made here. It is left open only because this round's business was mutation,
    not new coverage.
    `tests/test_api_reply_bookmarks.py` covers `62`-`63`/`72`/`80`-`81`/`85`/`90`
    at full-suite scope; a mutation of THOSE SEVEN cannot be killed here.

  `62s/db.session.add(PostReplyBookmark(...))/pass/` is NOT in the list above,
  and task 7's report wrongly counted it there. It is the DELIBERATE LABELLED
  CONTROL for the paragraph above: a mutation that deletes a `db.session.add`
  outright and goes unnoticed, proving the seven dead lines are dead rather than
  weakly guarded. A control that survives by construction is not an open hole,
  and counting it as one inflates the inventory. IT IS ALSO THE ONLY ONE OF THE
  SEVEN THAT WAS MUTATED: `63`, `72`, `80`, `81`, `85` and `90` received no
  mutation at all in task 7's sweep, because all six are dead at this scope and
  the result would have been "survives, dead line" for every one of them. That
  residual is stated rather than left for a reader to difference out of the
  statement list, where it would read as an oversight.

RETRACTION -- THE ONE EQUIVALENCE CLAIM THIS FILE MADE WAS FALSE, AND IS NOW
CLOSED BY A TEST. Task 7 argued that `48s/vote_direction == 'downvote' and //`
is an EQUIVALENT mutant over the reachable domain, on two grounds: that
app/models.py:3321's assert narrows `vote_direction` to 'upvote' or 'downvote'
before `:46`, and that a 'reversal' surviving that assert always yields a
non-None `undo`, making the one differing state ('reversal', None) unreachable.

BOTH GROUNDS ARE WRONG.

  - The assert narrows `vote()`'s OWN LOCAL, not the caller's. `:3317`/`:3319`
    ASSIGN to `vote_direction` inside `vote`; `vote_for_reply`'s variable is
    untouched and is still 'reversal' at `:46` and `:48`.
  - A surviving 'reversal' does NOT always yield a non-None `undo`.
    app/models.py:3325-3331 is an early `return None` -- with an existing vote,
    a non-empty `emoji` and a remapped direction matching the existing vote's
    effect, `vote` rewrites the emoji and returns before any removal branch
    assigns `undo`.

So ('reversal', None) IS reachable, and on it the original `:48` is False while
the mutant is True -- the mutant renders the reply as recently-DOWNVOTED when
nothing was downvoted. It is reachable from the web with no `python -O` and no
API involvement: app/post/routes.py:564 routes
`/comment/<int:comment_id>/<vote_direction>/<federate>/emoji` with the BARE
string converter, no `any(...)` whitelist, and `:575` passes that direction plus
`request.form.get('emoji')` into `vote_for_reply`. The `-O` caveat task 7
attached to this claim was the right instinct aimed at the wrong mutant: the
claim fails without invoking `-O` at all.

`..._a_web_reversal_that_only_rewrites_the_emoji_marks_neither` in
`TestVoteForReplyGuardsAndReturns` is that state, and kills the mutant alone.
The same test also closes `36s/.../reply.vote(user, vote_direction, None)/`,
which task 7 left open because every test then passed `emoji=None`.
"""

from datetime import date
from types import SimpleNamespace

import pytest
from flask import get_flashed_messages
from werkzeug.exceptions import HTTPException

from app import db
from app.constants import SRC_API, SRC_PLD, SRC_WEB
from app.models import NotificationSubscription, PostReplyBookmark, PostReplyVote
from app.shared.reply import (
    bookmark_reply, delete_reply, extra_rate_limit_check, remove_bookmark_reply,
    restore_reply, subscribe_reply, vote_for_reply,
)
from tests.factories import (
    bearer, make_community, make_instance, make_post, make_post_reply,
    make_post_reply_bookmark, make_site, make_user, web_ctx,
)


def _clear_votes_cast(user_id):
    """Delete the `votes_cast_{today}_{user_id}` key a completed vote wrote.

    The import is inside the function body, not at module level, for the same
    reason `votes_cast_today` (app/models.py:48) does it that way --
    `app.redis_client` is a module-level name assigned by `create_app`, so a
    top-level `from app import redis_client` here would bind `None`, captured
    before `create_app` ever runs.
    """
    from app import redis_client
    redis_client.delete(f'votes_cast_{date.today()}_{user_id}')


def _seed_reply(*, private=True, community_name='replies'):
    """One instance, one local user, one community, one post, one reply.

    Modelled on `tests/test_shared_post_edit.py:189`'s `_seed` and on
    `tests/factories.py:1187`'s `seed_post_context`, and it inherits both of
    their ordering constraints. `make_community` hardcodes `instance_id=1` and
    `user_id=1` (tests/factories.py:141-142) and tests/conftest.py:131 resets
    every sequence after each test, so the instance minted first here lands on
    id 1 and the community resolves to it.

    `user` is minted `local=True` because `authorise_api_user`
    (app/utils.py:3628) rejects any bearer token whose user has a non-None
    `ap_id`; `tests/factories.py:41`'s default is `local=False`. What
    `make_user` leaves unset that matters elsewhere is `private_key`, which
    stays None unless `with_keys=True` (tests/factories.py:51) -- harmless
    here because nothing in Groups A or C signs an outbound activity, and
    recorded because sub-project 37 lost a task to assuming otherwise.

    `private=True` sets `community.private`, which is the federation lever
    register entry D393(d) identifies: it stops the eager Celery task bodies
    at their first guard so no test issues an outbound request.
    `community.local_only` is NOT that lever and is deliberately left at
    `make_community`'s hardcoded False (tests/factories.py:148), because
    `can_downvote` reads it and setting it would silently change which
    permission arm a vote test takes.

    The reply is a plain `make_post_reply`, so `path` is None -- see the
    module docstring's WHAT IS NEW. A test needing `delete_reply:256`'s or
    `restore_reply:282`'s true arm must seed a path itself.

    NO `Site` ROW IS SEEDED HERE, and A TEST NEEDS ONE FOR TWO INDEPENDENT
    REASONS, not just the render. The first is the theme lookup any
    `render_template` triggers -- the module docstring's WHAT IS NEW gives the
    failure and the exact chain. The second is a PERMISSION read on a path that
    never renders at all: `can_downvote` (app/utils.py:2436) does
    `Site.query.get(1)` at `:2443` and dereferences it at `:2445`, and that sits
    BEFORE any source fork, so an SRC_API downvote needs the row as much as a
    web render does. `can_upvote` (app/utils.py:2480) has no such read, which is
    why the API upvote tests here run without one. So "fine for every SRC_API
    arm" would be wrong: it is fine for every SRC_API arm that does not reach
    `can_downvote`.

    It is left out of this helper rather than folded in because a `Site` row is
    read by more than the theme lookup (`blocked_phrases`, for one) and seeding
    it unconditionally would change what the existing tests here exercise -- and
    because `TestVoteForReplySourceAndPermission` needs to set
    `enable_downvotes` on the row it makes.
    """
    instance = make_instance('local.example', software='piefed')
    user = make_user(instance, 'reader', local=True)
    community = make_community(community_name)
    community.private = private
    db.session.commit()
    post = make_post(community, user, 'https://local.example/p/1')
    reply = make_post_reply(post, user)
    return SimpleNamespace(instance=instance, user=user, community=community,
                           post=post, reply=reply)


def _seed_distinct_reply_counters(s):
    """Give the four reply counters four DIFFERENT non-zero values.

    All four default to 0 -- `Post.reply_count` app/models.py:1722,
    `Post.reply_count_cross_posted` `:1723`, `Community.post_reply_count`
    `:571`, `User.post_reply_count` `:1011` -- and the factories do not move
    them. A delete test that left them there reads `(0, 0, 0, 0)` before and
    `(-1, -1, -1, -1)` after, and cannot tell one column from another because
    every slot holds the same number. That is false-witness mechanism 2, a
    fixture coincidence making distinct things produce the same value.

    EXACTLY WHAT THIS BUYS, MEASURED ONE MUTANT AT A TIME. An earlier version of
    this docstring asserted two illustrations and measured neither, and BOTH
    WERE WRONG. The three cases that matter, each run against both fixtures:

      COPY, `:253` -> `reply_count_cross_posted = reply_count`. All-zero
      fixture: 4 passed, THE MUTANT SURVIVES, because `:252` has already made
      `reply_count` equal -1 and -1 is what the slot was going to read anyway.
      Distinct fixture: 1 failed, `assert (30, 30, 7, 2) == (30, 16, 7, 2)`.
      THIS IS THE CLASS DISTINCTNESS IS FOR, and the only one where it is
      necessary rather than merely sufficient.

      OFFSET COPY, `:253` -> `reply_count_cross_posted = reply_count - 1`.
      All-zero fixture: 1 failed, `assert (-1, -2, -1, -1) == (-1, -1, -1, -1)`
      -- ALREADY CAUGHT without distinct seeds, because `:252` runs first and
      the offset compounds. The earlier claim that this one survived an
      all-zero fixture was simply false. Distinct fixture: also 1 failed,
      `assert (30, 29, 7, 2) == (30, 16, 7, 2)`.

      PERMUTATION, `:252` and `:253` swapped. BOTH fixtures: 4 passed.
      Distinctness has ZERO power here and no seeding can give it any:
      `:252`-`:255` are four in-place `-= 1` on four independent columns with
      no reads between them, so ANY permutation of their targets still
      decrements each column exactly once and produces a byte-identical tuple.
      IT IS AN EQUIVALENT MUTANT. The earlier claim that "a swap of positions
      i and j writes `value_j - 1` where `value_i - 1` is expected" described
      assignment, not `-=`, and was the justification offered for the whole
      helper.

    So the honest statement is narrow: DISTINCT SEEDS ADD DETECTION POWER OVER
    EXACT-COPY ASSIGNMENT MUTANTS, are redundant against offset copies, and are
    powerless against permutations. Pairwise distinctness is what that needs,
    and 31/17/8/3 have it. (Distinct pairwise DIFFERENCES would be a stronger
    property and these four do not have it -- 31-17 and 17-3 are both 14 --
    but nothing here needs it.) The values are also large enough that the
    post-delete counts stay positive, which keeps a failure message readable.

    Returns the before-tuple in `delete_reply:252`-`:255` order so a caller can
    assert against it without re-reading.
    """
    s.post.reply_count = 31
    s.post.reply_count_cross_posted = 17
    s.community.post_reply_count = 8
    s.user.post_reply_count = 3
    db.session.commit()
    return (s.post.reply_count, s.post.reply_count_cross_posted,
            s.community.post_reply_count, s.user.post_reply_count)


def test_extra_rate_limit_check_returns_false_for_any_user(db_session):
    """`:139` -- the whole function body. It is a documented stub whose
    docstring describes a plan rather than behaviour, and it returns False
    unconditionally, so the only thing to witness is that it does.

    `app/shared/post.py:155-160` holds a NEAR-duplicate, not a byte-identical
    copy: the two bodies are both `return False` and the two docstrings differ
    in exactly one word -- "posts" at app/shared/post.py:158 against
    "comments" at app/shared/reply.py:137. That duplication is already
    register entry D406, which cites this exact pair of line ranges, so it
    needs no new register line and no test of its own here.
    """
    s = _seed_reply()
    assert extra_rate_limit_check(s.user) is False


def test_bookmarking_an_already_bookmarked_reply_raises_through_the_api(db_session):
    """`:61` false -> `:65`, then `:66` true -> `:67`'s raise.
    Arcs 61->65 and 66->67; statements 65, 66, 67.

    The seeded bookmark is the witness: without it `:61` is true and the
    function takes its create path, which is already covered. Asserting only
    that an exception was raised would not distinguish this from any other
    failure, so the message is matched too.

    No `web_ctx`. This call runs entirely outside a request context, which is
    the module docstring's SRC_API claim executing rather than being asserted.
    """
    s = _seed_reply()
    make_post_reply_bookmark(s.user, s.reply)

    with pytest.raises(Exception, match='already been bookmarked'):
        bookmark_reply(s.reply.id, SRC_API, auth=bearer(s.user))

    assert PostReplyBookmark.query.filter_by(post_reply_id=s.reply.id).count() == 1


def test_bookmarking_an_already_bookmarked_reply_flashes_on_the_web(db_session, app):
    """`:66` false -> `:69`'s flash, then `:71` false -> function exit.
    Arcs 66->69 and 71->-57; statement 69.

    TWO ARCS IN ONE TEST, and the second is easy to miss: `71->-57` is the
    FUNCTION-EXIT arc that coverage.py writes with a negative `def` line. After
    the flash the web arm falls off the end of the function without returning,
    which is what that arc records.

    The row count is asserted because `flash` leaving the database untouched is
    the half of the behaviour the exception path shares -- the discriminator
    against the API arm is that NO exception escaped, and that the return is
    None rather than the `user_id` `:72` returns.

    THE FLASH IS ASSERTED ON ITS CONTENT, AND THE TASK-7 MUTATION PASS IS WHY.
    This test previously asserted only the return and the row count, and
    `69s/flash(_(msg))/flash(_('unrelated text'))/` left all 27 tests green --
    so nothing here witnessed WHAT was flashed, only that the web arm did not
    raise. `:65`'s message text was pinned by the API test above, through the
    exception it raises, and this arm had no equivalent. The precedent for the
    construction is `..._a_third_source_reaches_the_flash_branches...` below,
    which makes the same argument about `:111`/`:119`.
    """
    s = _seed_reply()
    make_post_reply_bookmark(s.user, s.reply)

    with web_ctx(app, s.user):
        assert bookmark_reply(s.reply.id, SRC_WEB) is None
        flashed = get_flashed_messages()

    assert len(flashed) == 1
    assert 'already been bookmarked' in flashed[0]
    assert PostReplyBookmark.query.filter_by(post_reply_id=s.reply.id).count() == 1


def test_removing_a_bookmark_that_does_not_exist_flashes_on_the_web(db_session, app):
    """`:84` false -> `:87`'s flash, then `:89` false -> function exit.
    Arcs 84->87 and 89->-75; statement 87.

    No bookmark is seeded, so `:79`'s `if existing_bookmark:` is false and
    control reaches the else. THE POSITIVE CONTROL for "nothing was deleted"
    is `tests/test_api_reply_bookmarks.py:37-41`, which drives the SAME `else`
    on the SRC_API arm through `put_reply_save` and observes the
    `Exception('This comment was not bookmarked.')` that `:85` raises -- so
    `:83`-`:85` are already covered and `:87` is this function's only missing
    statement. A bare count of zero here would otherwise be produced by a
    correct refusal, a broken fixture and a no-op alike.

    THE FLASH IS ASSERTED ON ITS CONTENT, AND IT PINS `:83` AS WELL AS `:87`.
    Both `83s/was not bookmarked/was not flagged/` and
    `87s/flash(_(msg))/flash(_('unrelated text'))/` left all 27 tests green in
    the task-7 mutation pass. The out-of-file positive control cited above
    pins `:83` only at FULL-suite scope: at the scope this file is measured
    and mutated at -- `tests/test_shared_*.py` -- `:85` is not executed at all,
    so `:83`'s text had no witness here of any kind. That is a narrower and
    truer statement than the one this docstring made before.
    """
    s = _seed_reply()
    assert PostReplyBookmark.query.count() == 0

    with web_ctx(app, s.user):
        assert remove_bookmark_reply(s.reply.id, SRC_WEB) is None
        flashed = get_flashed_messages()

    assert len(flashed) == 1
    assert 'was not bookmarked' in flashed[0]
    assert PostReplyBookmark.query.count() == 0


class TestSubscribeReply:
    """`:93-133` -- subscribe and unsubscribe, both source arms.

    `:94` JOINS `Post` and filters `deleted=False` on BOTH rows, which
    `subscribe_post` did not, so every test here needs a live parent post and
    `.one()` raises rather than returning None if either is deleted.
    `_seed_reply` supplies exactly that: both constructors set `deleted=False`
    explicitly rather than leaning on the column default -- tests/factories.py
    :344 for the post, :470 for the reply -- so the join resolves and `.one()`
    returns the `PostReply` (the query is `db.session.query(PostReply)`, so the
    join adds a filter and not a second entity to the result row).

    `:98` MAKES THE WEB ARM'S TWO FLASH BRANCHES UNREACHABLE, which is why the
    fifth test here reaches them through a third source constant instead of
    through SRC_WEB as the brief expected. `:98` reads

        subscribe = False if reply.notify_new_replies(user_id) else True

    and `notify_new_replies` (app/models.py:3305-3309, in `class PostReply`
    which opens at app/models.py:2887) runs the SAME query `:100` runs one line
    later -- `NotificationSubscription` filtered on `entity_id == self.id`,
    `user_id == user_id`, `type == NOTIF_REPLY`, `.first()` -- with no write
    between them. So on the web arm `subscribe` is ALWAYS the negation of
    `bool(existing_notification)`:

      - no subscription -> `subscribe` True -> `:113`'s else -> `:114` FALSE ->
        `:121` creates. `:114` can never be true here, so `:116`, and with it
        `:119`'s flash, is dead on this arm.
      - a subscription  -> `subscribe` False -> `:102` true -> `:103` TRUE ->
        `:104` deletes. `:103` can never be false here, so `:107`, and with it
        `:111`'s flash, is dead on this arm.

    Both flash branches additionally require `src != SRC_API`, so neither of
    the two source values any production caller passes can reach `:111` or
    `:119`.

    THE PROOF OF THAT IS THE DEDUCTION ABOVE, NOT THE TESTS BELOW, and the
    order matters. `:98` and `:100` evaluate the same predicate over the same
    session with no write between them, so the web arm has exactly TWO possible
    states and the deduction rules out the mismatched arms of both. The two web
    tests -- `..._ignores_the_subscribe_argument...` for the empty state,
    `..._deletes_an_existing_subscription...` for the occupied one -- then
    CONFIRM the deduction by landing on the two states it names. Reading them
    as the proof would be the weaker and wrong argument: two passing tests
    cannot by themselves establish that a state space has only two members.
    They are also why the brief's third and fifth tests are not here: both
    asserted an outcome the web arm cannot produce and both were falsified by
    running. What DOES reach the two flash branches is a third source
    constant, and the last test in this class uses one; the twin
    function `subscribe_post` has the identical shape and the identical
    resolution at tests/test_shared_post_interactions.py:577-639.

    `:131`'s RENDER NEEDS A `Site` ROW WITH id 1, which is why the three tests
    here that do not raise call `make_site()` and `_seed_reply` alone is not
    enough. `:128` is false for every source but SRC_API, so the two SRC_WEB
    tests and the SRC_PLD one all render; the two SRC_API tests raise before
    reaching `:128` and need no row. The chain is
    `app/shared/reply.py:131` -> `app/utils.py:75` (this codebase's own
    `render_template`, which wraps Flask's) -> `current_theme()`
    (app/utils.py:3228-3240), whose `:3230` `if hasattr(g, 'site')` is FALSE
    under `web_ctx` -- `test_request_context` never runs `before_request` --
    so `:3233` falls back to `Site.query.get(1)` and `:3238` dereferences it.
    Without the row that is `AttributeError: 'NoneType' object has no attribute
    'default_theme'`, not a skipped theme lookup. The module docstring's WHAT IS
    NEW records this; it is the first thing in this round to render a template
    and it will bind every later web arm that does.
    """

    def test_the_web_arm_ignores_the_subscribe_argument_it_was_given(self, db_session, app):
        """`:97` true -> `:98`. Arcs 97->98 and 128->131; statements 98, 131.

        THE WITNESS IS THE ARGUMENT BEING OVERRIDDEN. `:98` recomputes
        `subscribe` from `reply.notify_new_replies(user_id)` regardless of what
        the caller passed, so passing `subscribe=False` against a reply with NO
        existing subscription must still CREATE one. Asserting on the row alone
        would not witness the override -- passing True would produce the same
        row -- so the deliberately wrong argument is the test.

        The pre-call count of 0 is the positive control for the post-call count
        of 1: without it a fixture that seeded a subscription of its own would
        produce the same final number and the create at `:121` would witness
        nothing.

        The rendered body is read back, and `result.get_data(...)` is itself the
        check that `:131` returned something -- a flash-only path that fell off
        the end of the function returns None and raises AttributeError here
        rather than passing. What the body then adds is discrimination: it is
        `fe-bell` WITHOUT `fe-no-bell` that separates this `:131` from the
        sibling test below, which reaches the same return statement and the same
        template and gets `fe-no-bell`. `_reply_notification_toggle.html`
        picks between the two by calling `notify_new_replies(current_user.id)`
        itself, so the string read back is the subscription state as the
        database now holds it and not as this function computed it.
        """
        make_site()
        s = _seed_reply()
        assert NotificationSubscription.query.count() == 0

        with web_ctx(app, s.user):
            result = subscribe_reply(s.reply.id, False, SRC_WEB)
            body = result.get_data(as_text=True)

        assert NotificationSubscription.query.filter_by(
            entity_id=s.reply.id, user_id=s.user.id).count() == 1
        assert 'fe-no-bell' not in body
        assert 'fe-bell' in body

    def test_the_web_arm_deletes_an_existing_subscription_it_was_told_to_create(self, db_session, app):
        """`:98` again, in the OTHER direction, then `:102` true -> `:103` true
        -> `:104`-`:105`, then `:128` false -> `:131`. Arcs 97->98 and 128->131;
        statements 98, 131.

        THIS IS THE CORRECTION OF THE BRIEF'S FIFTH TEST, which expected
        `:116` false -> `:119`'s flash from this exact construction. It does not
        reach it: `:98` recomputes `subscribe` to False because a subscription
        exists, so control goes to `:102`'s delete arm and never to `:113`'s
        else. See the class docstring. What the construction DOES witness is
        `:98` overriding a `True` argument into a deletion, which is the mirror
        of the sibling test above and the reason both are kept.

        The subscription is created through the API arm so that `:98` cannot
        touch its creation, and the count of 1 asserted BEFORE the web call is
        the positive control for the count of 0 after it -- a deletion and a
        creation that never happened are otherwise the same zero.
        """
        make_site()
        s = _seed_reply()
        subscribe_reply(s.reply.id, True, SRC_API, auth=bearer(s.user))
        assert NotificationSubscription.query.filter_by(entity_id=s.reply.id).count() == 1

        with web_ctx(app, s.user):
            result = subscribe_reply(s.reply.id, True, SRC_WEB)
            body = result.get_data(as_text=True)

        assert NotificationSubscription.query.filter_by(entity_id=s.reply.id).count() == 0
        assert 'fe-no-bell' in body

    def test_unsubscribing_when_none_exists_raises_through_the_api(self, db_session):
        """`:102` true, `:103` false -> `:107`-`:109`'s raise.

        Of the two source values production uses, the API arm is the ONLY one
        that reaches `:107`'s else, because `:98` keeps `subscribe` and
        `existing_notification` in lockstep on the web arm -- see the class
        docstring. `:110`-`:111`, the other half of this same else, is reached
        only by the third-source test at the bottom of this class, which is this
        test's same-mechanism counterpart: same `:107`, opposite arm of `:108`.

        No `web_ctx`: this call runs entirely outside a request context, which
        is the module docstring's SRC_API claim executing rather than being
        asserted. The message is matched so that an unrelated failure -- a bad
        bearer token, the `:94` join finding nothing -- cannot pass as this
        branch.
        """
        s = _seed_reply()
        assert NotificationSubscription.query.count() == 0

        with pytest.raises(Exception, match='did not exist'):
            subscribe_reply(s.reply.id, False, SRC_API, auth=bearer(s.user))

    def test_subscribing_twice_raises_through_the_api(self, db_session):
        """`:114` true -> `:115`, `:116` true -> `:117`'s raise.
        Arcs 114->115 and 116->117; statements 115, 116, 117.

        The first call creates the subscription and is the positive control for
        the second: it proves the create path at `:121` works, so the second
        call's refusal is `:114` finding that row and not a failure to make one.
        Both go through the API arm, so `:97`'s override cannot interfere -- on
        the web arm this construction takes the delete path instead, which is
        what the second test in this class measures.

        The row count of 1 after the raise separates "refused" from "created a
        duplicate and then complained".

        THE FIRST CALL'S RETURN IS ASSERTED, AND THAT IS WHAT PINS `:129`.
        `:129` is executed by this file -- coverage reports it as covered --
        but ONLY by setup calls like this one and by
        `..._a_third_source_reaches_the_flash_branches...:763`, and both threw
        the value away. `129s/return user_id/return None/` accordingly left all
        27 tests green in the task-7 mutation pass. A covered statement whose
        every execution is an unread setup call is not a guarded one, and this
        assertion is the cheapest thing that makes it guarded.
        """
        s = _seed_reply()
        assert subscribe_reply(s.reply.id, True, SRC_API, auth=bearer(s.user)) == s.user.id

        with pytest.raises(Exception, match='already existed'):
            subscribe_reply(s.reply.id, True, SRC_API, auth=bearer(s.user))

        assert NotificationSubscription.query.filter_by(entity_id=s.reply.id).count() == 1

    def test_a_third_source_reaches_the_flash_branches_the_web_arm_cannot(self, db_session, app):
        """`:108`'s false arm and `:111`'s flash, then `:114`'s true arm,
        `:116`'s false arm and `:119`'s flash. Arcs 108->111 and 116->119;
        statements 111, 119.

        NEITHER SRC_WEB NOR SRC_API CAN REACH `:111`/`:119`. The class
        docstring DERIVES that from `:98`; the two web tests above only confirm
        the derivation by landing on the two states it names. In short:
        SRC_WEB keeps `subscribe` and `existing_notification` in lockstep, so
        `:103`'s false arm and `:114`'s true arm are jointly unreachable there,
        and under SRC_API `:108`/`:116` always take the raise. A third source
        value is the only way in -- it is (a) not SRC_WEB, so `:97` skips the
        override and the caller's `subscribe` argument survives, and (b) not
        SRC_API, so `:108`/`:116` take the else. SRC_PLD (app/constants.py:94,
        the admin preload path) is used here as such a value. IT IS NOT HOW
        *THIS* FUNCTION IS CALLED IN PRODUCTION -- app/api/alpha/utils/reply.py
        :462 passes SRC_API and app/post/routes.py:2098 passes SRC_WEB, and
        those are the only two callers -- and this docstring says so rather
        than implying otherwise. It is NOT a made-up value, though: SRC_PLD is
        a real constant the shared layer branches on elsewhere
        (app/shared/community.py:50, app/shared/tasks/follows.py:53, :67, :99),
        and `:108`/`:116` are written as `if src == SRC_API` with an `else`
        over every other source, so the contract these two lines declare
        admits it. The precedent, down to the constant, is
        tests/test_shared_post_interactions.py:577-639 against the twin
        `subscribe_post`.

        `web_ctx` is used even though this is not an SRC_WEB call, because
        `:95`'s else-arm reads `current_user.id` for any non-SRC_API source and
        `:131` renders for any non-SRC_API source; `make_site()` is there for
        the render, per the module docstring's WHAT IS NEW.

        THE ASSERTION IS ON `flashed`'s CONTENT, NOT ON `result`, and that is
        load-bearing. Under SRC_PLD `:128` is False either way, so control
        reaches `:131` whether or not the flash call is there -- deleting
        `flash(_(msg))` outright would still return a normal 200 render and
        pass a result-only assertion silently. The row counts are the second
        half: 0 after the first call and 1 after the second separate "refused
        and flashed" from "flashed and then also wrote", which is what reaching
        `:111` or `:119` from the wrong outer arm would look like.
        """
        make_site()
        s = _seed_reply()

        with web_ctx(app, s.user):
            result = subscribe_reply(s.reply.id, False, SRC_PLD)
            flashed = get_flashed_messages()

        assert result.status_code == 200
        assert len(flashed) == 1
        assert 'did not exist' in flashed[0]
        assert NotificationSubscription.query.filter_by(
            entity_id=s.reply.id, user_id=s.user.id).count() == 0

        subscribe_reply(s.reply.id, True, SRC_API, auth=bearer(s.user))

        with web_ctx(app, s.user):
            result = subscribe_reply(s.reply.id, True, SRC_PLD)
            flashed = get_flashed_messages()

        assert result.status_code == 200
        assert len(flashed) == 1
        assert 'already existed' in flashed[0]
        assert NotificationSubscription.query.filter_by(
            entity_id=s.reply.id, user_id=s.user.id).count() == 1


class TestVoteForReplySourceAndPermission:
    """`:19-28` -- the source fork and the two API-only permission gates.

    A REGISTERED ASYMMETRY IS PINNED HERE AND DELIBERATELY NOT FIXED. `:22` and
    `:24` call `can_upvote`/`can_downvote` INSIDE the `if src == SRC_API:` arm.
    The `else` at `:26-28` has no equivalent, and the web route that reaches it
    -- `app/post/routes.py:552-561`, `comment_vote` -- carries `@login_required`
    (`:553`), `@validation_required` (`:554`) and `@approval_required` (`:555`)
    and no voting-permission check of its own. So a voter the community has
    banned is refused through the API and not through the web UI.

    THE TWIN DOES NOT SHARE THE ASYMMETRY, which the brief did not say and which
    sharpens it: `app/shared/post.py:43-48` puts an `or`-joined
    `can_upvote`/`can_downvote` pair inside `vote_for_post`'s web arm and
    returns a re-rendered voting-buttons template instead of voting. So this is
    a divergence between two functions written as mirrors, not a uniform policy
    that the web UI gates voting elsewhere.

    This round records that and changes neither arm, on the same reasoning that
    registered the pixelfed divergence in sub-project 39: `:26-28` is live
    behaviour rather than dead code, and altering a permission check is a
    product decision a coverage round has no standing to make.

    WHICH TESTS HERE NEED A `Site` ROW, decided per test rather than by reflex:

      - `..._from_a_bot_...` needs none. `can_upvote` (app/utils.py:2480-2491)
        reads no `Site` at all, and `can_downvote`'s `Site` lookup is at
        `:2440-2443`, AFTER the `user.bot` refusal at `:2437`.
      - the two downvote tests need one, because `can_downvote:2445` reads
        `site.enable_downvotes` -- one with the flag off, one with the row as
        `make_site()` leaves it.
      - `..._passes_both_gates` needs none: `:22`'s `can_upvote` reads no
        `Site`, and `:24`'s first conjunct is false for an upvote so
        `can_downvote` is never called. It votes and renders nothing --
        `:41` is true on the API arm, so it returns at `:42` and never reaches
        `:51`.
      - the web test needs one FOR TWO INDEPENDENT REASONS, not the one this
        docstring first gave. It is the only test in this class that reaches
        `:51`'s `render_template`, which is the module docstring's chain
        executing -- but `:51`'s template ALSO calls `can_downvote` itself, at
        `post/_comment_voting_buttons.html` line 10, and that reads
        `Site.query.get(1)` at app/utils.py:2443 exactly as `:24`'s call does.
        `TestVoteForReplyGuardsAndReturns` below derives that and its downvote
        test witnesses it. Removing the row would therefore break this test
        twice over, and "because it renders" names only the first break.
      - the two TASK-7 tests need none, and for the two different reasons
        already on this list. `..._lands_on_the_named_reply_...` is an SRC_API
        upvote: `can_upvote` reads no row and `:24`'s first conjunct is false,
        so it is the `..._passes_both_gates` case exactly.
        `..._a_reversal_is_refused_when_the_permission_that_cast_the_vote_is_gone`
        -- the inverted form of the test that used to record DEFECT 5 -- still
        needs none, but for a reason the fix CHANGED. It used to need none
        because under 'reversal' both `:22` and `:24` were false at their first
        conjunct and NEITHER permission function was called. Now `:26`'s arm
        calls one of them, chosen by the existing vote's sign, and that test's
        existing vote is an UPVOTE, so the function called is `can_upvote`,
        which reads no `Site` row. Its downvote siblings below DO call
        `can_downvote` and therefore DO call `make_site()`. Same conclusion,
        different mechanism, and the difference is exactly the sort a fix
        invalidates silently.

    THE WEB TEST'S VOTER IS NO LONGER `_seed_reply`'s SINGLE USER, and that
    changes nothing on this list: `Site` is read by `can_downvote` and by the
    theme lookup, neither of which depends on which user votes. It is recorded
    here because the entry above says "the web test" as though its fixture were
    the class's default, and since task 7 it is not.
    """

    def test_an_api_upvote_from_a_bot_returns_early_without_voting(self, db_session):
        """`:22` true -> `:23`. Arc 22->23, statement 23.

        `can_upvote` (app/utils.py:2481) refuses a bot, so `:23` returns
        `user.id` before `:36`'s `reply.vote()` ever runs.

        THE WITNESS IS THE ABSENT VOTE, not the return value: `:23` and `:42`
        both return `user.id`, so the returned value alone cannot tell an early
        refusal from a completed vote. The `PostReplyVote` row count is what
        separates them, and no `_clear_votes_cast` is needed precisely because
        no vote completed -- which is itself part of the assertion. The
        same-mechanism positive control is `..._passes_both_gates` below: the
        identical call with `bot` left False, which votes.

        No `web_ctx` and no `make_site()`: the module docstring's SRC_API
        finding covers the first, and `can_upvote` reads no `Site` row.
        """
        s = _seed_reply()
        s.user.bot = True
        db.session.commit()

        assert vote_for_reply(s.reply.id, 'upvote', True, None, SRC_API,
                              auth=bearer(s.user)) == s.user.id

        db.session.refresh(s.reply)
        assert s.reply.up_votes == 0
        assert PostReplyVote.query.filter_by(
            post_reply_id=s.reply.id, user_id=s.user.id).count() == 0

    def test_an_api_downvote_returns_early_when_downvotes_are_disabled(self, db_session):
        """`:22` false -> `:24`, `:24` true -> `:25`. Arcs 22->24 and 24->25;
        statements 24, 25.

        THIS IS THE CORRECTION OF THE BRIEF'S SECOND TEST, which used a bot for
        the downvote too. A bot is refused by `can_upvote:2481` and
        `can_downvote:2437` for the identical reason, so a bot downvote cannot
        tell `:22` from `:24`: a mutation swapping the two direction literals
        leaves `:22` matching 'downvote', `can_upvote` refusing the bot anyway,
        and the call still returning `user.id` with no vote. Two tests that pass
        under a swap of the conditions they are supposed to separate are the
        fifth false-witness mechanism exactly. THAT WAS MEASURED, NOT ARGUED:
        the two literals at `:22`/`:24` were swapped in a scratch mutant and
        BOTH bot tests -- the one above and the brief's bot downvote, restored
        verbatim for the probe -- passed against it.

        The lever here is DIRECTION-SPECIFIC instead. `can_downvote` reads
        `site.enable_downvotes` at app/utils.py:2445 and `can_upvote`
        (app/utils.py:2480-2491) DOES NOT READ IT AT ALL -- that asymmetry
        between the two gates is the whole witness, and it is a narrower claim
        than "only `can_downvote` reads the column", which is false: the flag is
        also read at app/api/alpha/views.py:1232, app/activitypub/util.py:4065
        and app/admin/routes.py:337, and WRITTEN at app/admin/routes.py:291.
        None of those is on this call path. So under the direction swap this
        user's downvote reaches
        `:22`'s `can_upvote`, which returns True for a non-bot, falls past
        `:24`'s now-'upvote' test, and LANDS -- `down_votes` becomes 1 and this
        test fails. That is the discrimination the bot version could not give,
        and the same scratch mutant confirmed it: this test was the one that
        failed.

        `make_site()` is required and is the point of the test: without a `Site`
        row `can_downvote:2443` gets None and `:2445` raises AttributeError
        rather than refusing. The same-mechanism positive control is
        `..._lands_when_downvotes_are_enabled` below -- without it, a fixture in
        which no downvote could ever land would produce this same zero.
        """
        site = make_site()
        site.enable_downvotes = False
        db.session.commit()
        s = _seed_reply()

        assert vote_for_reply(s.reply.id, 'downvote', True, None, SRC_API,
                              auth=bearer(s.user)) == s.user.id

        db.session.refresh(s.reply)
        assert s.reply.down_votes == 0
        assert PostReplyVote.query.filter_by(
            post_reply_id=s.reply.id, user_id=s.user.id).count() == 0

    def test_an_api_downvote_lands_when_downvotes_are_enabled(self, db_session):
        """`:24` false -> `:30`. Arc 24->30, reached through the downvote arm.

        THE SAME-MECHANISM POSITIVE CONTROL for the test above: identical
        except that `enable_downvotes` is left as `make_site()` makes it. It
        establishes that a downvote from this fixture's user CAN land, so the
        zero above is `can_downvote` refusing and not some unrelated property
        of the seed -- `downvote_accept_mode` (app/models.py:586, in
        `class Community` which opens at app/models.py:555), `user.attitude`
        (app/models.py:1009) and `user.reputation` (app/models.py:1008, both in
        `class User` which opens at app/models.py:973) each independently gate
        `can_downvote` -- attitude and reputation together at app/utils.py:2451,
        `downvote_accept_mode` at app/utils.py:2454-2468 -- and each would
        produce the same empty result. app/utils.py:2448, the line this
        docstring previously gave as the start of that range, is
        `community.local_only`, a FOURTH such gate and not one of the three
        named here; `_seed_reply`'s docstring is where that one is recorded.

        `_clear_votes_cast` is mandatory here because this test completes a real
        vote against the session-wide redis.
        """
        make_site()
        s = _seed_reply()
        try:
            assert vote_for_reply(s.reply.id, 'downvote', True, None, SRC_API,
                                  auth=bearer(s.user)) == s.user.id

            db.session.refresh(s.reply)
            assert s.reply.down_votes == 1
            assert PostReplyVote.query.filter_by(
                post_reply_id=s.reply.id, user_id=s.user.id).count() == 1
        finally:
            _clear_votes_cast(s.user.id)

    def test_a_permitted_api_voter_passes_both_gates(self, db_session):
        """`:24` false -> `:30`. Arc 24->30.

        THE POSITIVE CONTROL for the bot refusal, using the same mechanism:
        identical except that the user is not a bot, and the vote lands. `:24`
        is reached and its first conjunct is false for an upvote, so
        `can_downvote` is never called and no `Site` row is needed.

        `_clear_votes_cast` is mandatory here because this test completes a real
        vote against the session-wide redis.
        """
        s = _seed_reply()
        try:
            assert vote_for_reply(s.reply.id, 'upvote', True, None, SRC_API,
                                  auth=bearer(s.user)) == s.user.id

            db.session.refresh(s.reply)
            assert s.reply.up_votes == 1
            assert PostReplyVote.query.filter_by(
                post_reply_id=s.reply.id, user_id=s.user.id).count() == 1
        finally:
            _clear_votes_cast(s.user.id)

    def test_an_api_vote_lands_on_the_named_reply_as_the_bearers_user(self, db_session):
        """CLOSES NO STATEMENT AND NO ARC. It earns its place by unique kills
        against two FAULT-DIRECTION mutants on `:20` and `:21`, both of which
        survived the whole file in the task-7 pass:

          `20s/filter_by(id=reply_id).one()/first()/`      -> 27 passed
          `21s/authorise_api_user(auth, return_type='model')/reply.author/`
                                                           -> 27 passed

        Neither is a `.one()` crash mutant, which is what makes them worth
        running: each returns a PostReply or a User of the right type and the
        function completes normally, just against the wrong row. They survived
        because `_seed_reply` mints exactly one reply and exactly one user, so
        "the reply named by `reply_id`", "the first reply in the table", "the
        bearer's user" and "the reply's author" were four names for two objects.

        THE FIXTURE IS THE WHOLE TEST. `voter` is not the author, so `:21`'s
        mutant writes a `PostReplyVote` owned by `s.user` and the
        `user_id=voter.id` count reads 0. `decoy` is minted AFTER `s.reply` and
        is the row voted on, so `:20`'s unfiltered `.first()` cannot reach it by
        insertion order and `s.reply` takes the vote instead. `PostReplyVote
        .query.count() == 1` rules out a mutant that voted on both.

        THIS IS THE API ARM'S HALF OF THE PAIR;
        `..._the_web_arm_loads_the_reply_and_reads_current_user` below carries
        the same construction against `:27`/`:28`, which had the identical
        weakness. It deliberately does NOT touch `..._passes_both_gates` above,
        whose value is that it is the bot test's same-mechanism control and
        differs from it in `user.bot` alone; giving that test a second user
        would have destroyed the property it exists for.

        No `make_site()`: this is an upvote, so `:22`'s `can_upvote` runs and
        reads no `Site`, and `:24`'s first conjunct is false so `can_downvote`
        is never called. `:41` is true, so nothing renders.
        `_clear_votes_cast` is mandatory because a real vote completes.
        """
        s = _seed_reply()
        voter = make_user(s.instance, 'voter', local=True)
        decoy = make_post_reply(s.post, s.user, body='decoy')
        db.session.commit()
        try:
            assert vote_for_reply(decoy.id, 'upvote', True, None, SRC_API,
                                  auth=bearer(voter)) == voter.id

            db.session.refresh(s.reply)
            db.session.refresh(decoy)
            assert decoy.up_votes == 1
            assert s.reply.up_votes == 0
            assert PostReplyVote.query.filter_by(
                post_reply_id=decoy.id, user_id=voter.id).count() == 1
            assert PostReplyVote.query.count() == 1
        finally:
            _clear_votes_cast(voter.id)

    def test_a_reversal_is_refused_when_the_permission_that_cast_the_vote_is_gone(self, db_session):
        """DEFECT 5 IS FIXED, AND THIS TEST WAS INVERTED TO SAY SO.

        Until the fix, `:22` and `:24` were each gated on a direction LITERAL,
        and 'upvote'/'downvote' are not the only values the API arm passes:
        `app/api/alpha/utils/reply.py:435-441` maps `score` to three
        directions -- 1 to 'upvote', -1 to 'downvote', and ANYTHING ELSE to
        'reversal' -- and hands the result to `vote_for_reply` at `:444`.
        Under 'reversal' both conjuncts were false, neither `can_upvote` nor
        `can_downvote` was consulted, and the vote proceeded to `:30`. A bot,
        whom `can_upvote` (app/utils.py:2481) and `can_downvote` (`:2437`)
        both refuse, could undo its vote through the API with no permission
        check at all.

        THE EARLIER VERSION OF THIS TEST ASSERTED THAT BUG ON PURPOSE and
        carried a fix-edit obligation naming this exact edit. This is that
        obligation being collected: the assertions below are now the inverse
        of what they were. The reversal must be REFUSED, `up_votes` must stay
        1, and the `PostReplyVote` row must survive.

        THE GATE RESOLVES THE EXISTING VOTE RATHER THAN THE DIRECTION
        LITERAL, which is the only way to gate a reversal at all: 'reversal'
        names no permission of its own. `:25`-`:29` load the voter's existing
        `PostReplyVote` and require `can_upvote` for an `effect` above zero
        and `can_downvote` for one below. The existing vote here is an upvote
        with `effect` exactly 1 (app/models.py:3366), so `can_upvote` is the
        function consulted and the bot flag is what makes it refuse.

        THE WITNESS IS THE SURVIVING ROW, NOT THE RETURNED ID. `:26` and the
        permitted path both return `user.id`, so the return value cannot tell
        a refusal from a success -- exactly the false-witness mechanism (a)
        the module docstring names. What discriminates is state: under the
        unfixed code `:36` ran, `PostReply.vote` remapped 'reversal' to
        'upvote' at app/models.py:3316 and deleted the row at `:3341`, and
        both counts below would read 0.

        `s.user.bot` is set BETWEEN the two calls, because the first call must
        pass `:22` to create the row the second one tries to reverse.
        """
        s = _seed_reply()
        try:
            vote_for_reply(s.reply.id, 'upvote', True, None, SRC_API,
                           auth=bearer(s.user))
            db.session.refresh(s.reply)
            assert s.reply.up_votes == 1

            s.user.bot = True
            db.session.commit()

            assert vote_for_reply(s.reply.id, 'reversal', True, None, SRC_API,
                                  auth=bearer(s.user)) == s.user.id

            db.session.refresh(s.reply)
            assert s.reply.up_votes == 1
            assert PostReplyVote.query.filter_by(
                post_reply_id=s.reply.id, user_id=s.user.id).count() == 1
        finally:
            _clear_votes_cast(s.user.id)

    def test_a_downvote_reversal_is_refused_when_downvotes_are_disabled(self, db_session):
        """`:38`'s true arm -- the `effect < 0` half of the reversal gate.

        The gate resolves the existing vote's SIGN and consults the matching
        permission, so the two halves need separate witnesses. This is the
        negative one.

        `site.enable_downvotes` IS THE RIGHT LEVER AND THE BOT FLAG IS NOT.
        `enable_downvotes` False makes `can_downvote` return at
        app/utils.py:2445 while leaving `can_upvote` (app/utils.py:2480,
        which never reads the Site row) TRUE. So a mutant that consulted
        `can_upvote` for a negative effect -- swapping the two functions, or
        collapsing `:38` into `:36` -- would permit this reversal and the row
        below would be gone. The bot flag would refuse both and could not tell
        the two functions apart: false-witness mechanism (e), two conditions
        moved only in lockstep.

        `make_site()` must exist BEFORE the downvote is cast, because
        `can_downvote:2443` reads `Site.query.get(1)` and `:2445` dereferences
        it. The flag is flipped between the two calls so the first one lands.
        """
        site = make_site()
        s = _seed_reply()
        try:
            vote_for_reply(s.reply.id, 'downvote', True, None, SRC_API,
                           auth=bearer(s.user))
            db.session.refresh(s.reply)
            assert s.reply.down_votes == 1

            site.enable_downvotes = False
            db.session.commit()

            assert vote_for_reply(s.reply.id, 'reversal', True, None, SRC_API,
                                  auth=bearer(s.user)) == s.user.id

            db.session.refresh(s.reply)
            assert s.reply.down_votes == 1
            assert PostReplyVote.query.filter_by(
                post_reply_id=s.reply.id, user_id=s.user.id).count() == 1
        finally:
            _clear_votes_cast(s.user.id)

    def test_a_downvote_reversal_lands_when_downvotes_are_enabled(self, db_session):
        """`:38`'s false arm, and the same-mechanism positive control.

        Identical to the test above except that `enable_downvotes` is left as
        `make_site()` makes it, so `can_downvote` permits and the reversal
        must LAND. Without it, a fixture in which no downvote reversal could
        ever succeed would produce the same surviving row and prove nothing.
        """
        make_site()
        s = _seed_reply()
        try:
            vote_for_reply(s.reply.id, 'downvote', True, None, SRC_API,
                           auth=bearer(s.user))
            db.session.refresh(s.reply)
            assert s.reply.down_votes == 1

            assert vote_for_reply(s.reply.id, 'reversal', True, None, SRC_API,
                                  auth=bearer(s.user)) == s.user.id

            db.session.refresh(s.reply)
            assert s.reply.down_votes == 0
            assert PostReplyVote.query.filter_by(
                post_reply_id=s.reply.id, user_id=s.user.id).count() == 0
        finally:
            _clear_votes_cast(s.user.id)

    def test_a_reversal_with_no_existing_vote_is_not_refused_by_the_gate(self, db_session):
        """`:35`'s false arm -- there is no vote to resolve a permission from.

        The gate cannot ask "was this voter allowed to cast the vote they are
        undoing" when no such vote exists, so it falls through rather than
        refusing. Control reaches `:44`, `:47` and then `reply.vote()`, where
        app/models.py:3321 remaps 'reversal' ONLY `if existing_vote` -- so the
        direction arrives still spelled 'reversal' and `:3326` raises.

        THIS PINS A PRE-EXISTING 500, NOT A NEW ONE, and it is here because
        `:35`'s false arm needs a witness. `Post.vote` handles the same state
        differently: app/models.py:2740-2741 returns None for a reversal with
        no existing vote, where `PostReply.vote` has no such arm. That
        divergence is registered, not repaired here.

        THE WITNESS IS THE ABSENT ROW AS MUCH AS THE RAISE. A crash is a weak
        kill on its own, so the assertion below is that no `PostReplyVote` was
        written -- which is what distinguishes falling through the gate from a
        mutant that let the else-branch cast a new downvote.
        """
        s = _seed_reply()
        try:
            with pytest.raises(ValueError):
                vote_for_reply(s.reply.id, 'reversal', True, None, SRC_API,
                               auth=bearer(s.user))

            assert PostReplyVote.query.filter_by(
                post_reply_id=s.reply.id, user_id=s.user.id).count() == 0
        finally:
            _clear_votes_cast(s.user.id)

    def test_a_permitted_voter_can_still_reverse_their_own_vote(self, db_session):
        """The positive control for the gate above, and it is mandatory.

        A refusal test alone cannot distinguish a gate that refuses the right
        voter from one that refuses every voter -- false-witness mechanism
        (c), emptiness with no same-mechanism positive control. This test runs
        the identical two-call sequence with the bot flag never set, so
        `can_upvote` permits, and asserts the reversal LANDS: `up_votes` back
        to 0 and the `PostReplyVote` row gone.

        Same mechanism, same path, one lever moved. If the new gate at
        `:25`-`:29` refused unconditionally, the test above would still pass
        and this one would fail.
        """
        s = _seed_reply()
        try:
            vote_for_reply(s.reply.id, 'upvote', True, None, SRC_API,
                           auth=bearer(s.user))
            db.session.refresh(s.reply)
            assert s.reply.up_votes == 1

            assert vote_for_reply(s.reply.id, 'reversal', True, None, SRC_API,
                                  auth=bearer(s.user)) == s.user.id

            db.session.refresh(s.reply)
            assert s.reply.up_votes == 0
            assert PostReplyVote.query.filter_by(
                post_reply_id=s.reply.id, user_id=s.user.id).count() == 0
        finally:
            _clear_votes_cast(s.user.id)

    def test_the_web_arm_loads_the_reply_and_reads_current_user(self, db_session, app):
        """`:19` false -> `:27`, `:28`. Arc 19->27; statements 27, 28.

        The web arm uses `get_or_404` rather than `.one()` and takes its user
        from `current_user`, so this needs `web_ctx`. It reaches `:36` and
        completes a real vote -- hence the `finally`.

        `make_site()` is required: this is the only test in this class that
        reaches `:51`'s `render_template`, and the module docstring's chain
        ends in `AttributeError: 'NoneType' object has no attribute
        'default_theme'` without the row.

        THE RENDERED BODY IS NOT ON ITS OWN A WITNESS THAT `:28` TOOK
        `current_user`, AND THIS DOCSTRING USED TO SAY IT WAS. The markup
        argument is sound as far as it goes -- `_comment_voting_buttons.html`
        opens with `{% if current_user.is_authenticated and
        current_user.verified %}`, so `voted_up`/`fe-arrow-up-circle` exist only
        on the authenticated branch, and `voted_up` additionally requires `:47`
        to have put this reply id into `recently_upvoted_replies`, which only
        happens after `:36` actually voted. What it could not see is that the
        FIXTURE made the claim vacuous: `_seed_reply` mints ONE user who is both
        the reply's author and the logged-in voter, so every user-valued
        expression in scope had the same value. Measured in the task-7 pass
        against the old fixture, `28s/user = current_user/user = reply.author/`
        left all 27 tests green -- the markup was identical because the vote was
        identical.

        THE VOTER IS THEREFORE NOT THE AUTHOR, AND THE REPLY IS NOT THE ONLY
        ROW. `voter` closes `:28`: under `user = reply.author` the
        `PostReplyVote` row carries the author's id and the `user_id=voter.id`
        count reads 0. `decoy` closes `:27`, which had the same shape --
        `27s/query(PostReply).get_or_404(reply_id)/query(PostReply).first()/`
        also left all 27 tests green, because with one reply in the table "the
        first reply" and "the reply named by `reply_id`" are the same row. The
        vote is cast on `decoy`, the LATER of the two rows, so an unordered
        `.first()` cannot land on it by insertion order; `PostReplyVote.query
        .count() == 1` is there so that a mutant voting on both would not pass
        on the two per-row assertions.

        NO PERMISSION GATE IS CROSSED HERE, because there is none on this arm.
        That is the asymmetry the class docstring registers, and this test is
        the evidence for it -- and the non-author voter sharpens it rather than
        blurring it: `voter` has no relationship to this reply at all. A test
        showing a BOT voting successfully through this path would make the
        asymmetry executable rather than documentary; that is a scope decision
        and it was referred to the controller rather than taken here.
        """
        make_site()
        s = _seed_reply()
        voter = make_user(s.instance, 'voter', local=True)
        decoy = make_post_reply(s.post, s.user, body='decoy')
        db.session.commit()
        try:
            with web_ctx(app, voter):
                result = vote_for_reply(decoy.id, 'upvote', True, None, SRC_WEB)
                body = result.get_data(as_text=True)

            db.session.refresh(s.reply)
            db.session.refresh(decoy)
            assert decoy.up_votes == 1
            assert s.reply.up_votes == 0
            assert PostReplyVote.query.filter_by(
                post_reply_id=decoy.id, user_id=voter.id).count() == 1
            assert PostReplyVote.query.count() == 1
            assert 'redirect_login' not in body
            assert 'voted_up' in body
            assert 'fe-arrow-up-circle' in body
        finally:
            _clear_votes_cast(voter.id)


class TestVoteForReplyGuardsAndReturns:
    """`:30-34` and `:44-51` -- the ban and quota guards, and the web arm's
    three-way recently-voted fork.

    FOUR COVERAGE-CLOSING TESTS, NOT THE SIX THE BRIEF DRAFTED, because
    `TestVoteForReplySourceAndPermission` above had already closed two of them
    and this was MEASURED before anything was written. Against the class above
    alone, suite-scoped over this file plus
    tests/test_shared_post_interactions.py with `--cov=app.shared.reply
    --cov-branch`, statements 30, 33, 36, 38, 41, 42, 44, 45, 46, 47 and 51 are
    already not-missing and so are arcs 30->33, 33->36, 41->42, 41->44 and
    46->47. What was still missing was statements 31, 34, 48 and 49 and arcs
    30->31, 33->34, 46->48, 48->49 and 48->51 -- exactly what the first four
    tests here take. The brief's `..._a_completed_api_vote_returns_the_user_id`
    and `..._a_web_upvote_renders_with_the_reply_marked_recently_upvoted` would
    have duplicated `..._passes_both_gates` and
    `..._the_web_arm_loads_the_reply_and_reads_current_user` call for call while
    asserting strictly less (`result is not None` against that test's markup
    discrimination), so they are deliberately absent rather than overlooked.

    THREE MORE TESTS CLOSE NOTHING, which is why the count above is qualified
    rather than plain, AND THEY DO NOT ALL EARN THEIR PLACE THE SAME WAY. Two
    earn it by a unique kill against a fault-direction mutant the other four
    left alive -- the downvote-undo test against `:48`'s second conjunct (task
    7) and the emoji-reversal test against `:48`'s FIRST conjunct (fix round 1,
    closing a mutant task 7 had wrongly argued equivalent). The third, the
    quota-boundary test, earns it by PINNING A REGISTERED DEFECT, D501: its
    unique kill against `:33`'s comparison operator is real, but `>=` is the
    candidate FIX for the off-by-one D501 registers, so that kill is a
    fix-catcher and cannot be the justification. Its docstring carries the
    fix-edit obligation the pin-a-defect clause requires; all three carry the
    measurements.

    WHY `:46`/`:48` NEED FIVE INPUTS AND NOT THREE. `:46` is an `if` and `:48`
    its `elif`, so `:48` is not evaluated at all when `:46` is true, and BOTH
    lines carry `undo is None` as their second conjunct:

      - upvote, `undo` None      -> `:46` true  -> `:47`, then `:51`
      - downvote, `undo` None    -> `46->48`, `:48` true  -> `:49`, then `:51`
      - upvote, `undo` NOT None  -> `46->48`, `:48` false on its FIRST conjunct
        -> `:51` with both lists left empty
      - downvote, `undo` NOT None -> `46->48`, `:48` false on its SECOND
        conjunct -> `:51` with both lists left empty
      - 'reversal', `undo` None  -> `46->48`, `:48` false on its FIRST conjunct
        -> `:51` with both lists left empty. THE TABLE USED TO HAVE FOUR ROWS
        AND STOP ABOVE THIS ONE, on the assumption that `vote_direction` here is
        always one of the two literals. It is not: `PostReply.vote` remaps
        'reversal' by assigning to ITS OWN local (app/models.py:3317/:3319), so
        the caller's value survives unchanged, and the emoji early return at
        `:3325-3331` reaches `:46` with `undo` None. Falsifying `:48`'s first
        conjunct with `undo` None is what the fourth row cannot do -- there
        `undo` is non-None, so deleting the first conjunct leaves `:48` false
        either way. The sixth test below is this row.

    THE LAST TWO ROWS USED TO BE ONE ROW READING "either direction", AND THAT
    COLLAPSE COST A GUARD. Both reach `48->51`, so coverage cannot tell them
    apart and the arc table said three inputs sufficed -- but they falsify
    `:48` at different conjuncts, and only the downvote row exercises the
    second one. Deleting ` and undo is None` from `:48` accordingly survived
    the whole file until task 7 added the fourth input. This is the arc table
    being a weaker instrument than the statement-and-operand list, recorded
    where it actually bit.

    The upvote-undo case is the fourth test here, the downvote-undo case the
    fifth and the emoji-reversal case the sixth; the first row is the class
    above's web test and the second the third test here.

    `undo` IS NON-None ONLY WHEN A VOTE IS REMOVED, and that was read at source
    rather than assumed: `PostReply.vote` (app/models.py:3311, in `class
    PostReply` which opens at app/models.py:2887) initialises `undo = None` at
    `:3322` and assigns it in exactly two places -- `:3343`'s `undo = 'Like'`,
    when an existing upvote is voted up again and deleted, and `:3357`'s
    `undo = 'Dislike'`, when an existing downvote is voted down again. A
    direction REVERSAL (`:3345`, `:3359`) edits the existing row and leaves
    `undo` None, so reversing is NOT an input that reaches `48->51`; repeating
    the same direction is. THE EMOJI EARLY RETURN AT `:3325-3331` IS A THIRD
    PATH THAT LEAVES `undo` None, and it is the one the fifth row of the table
    uses: it returns before the removal branches, so nothing is undone and
    nothing is recorded, while the caller's direction may be a value neither
    `:46` nor `:48` matches.

    WHICH TESTS NEED A `Site` ROW, decided against BOTH triggers -- the
    `render_template` theme chain and a `can_downvote` call -- rather than by
    reflex:

      - the ban test needs none. It aborts at `:31`, so nothing renders, and
        `:24`'s `can_downvote` is inside the `if src == SRC_API:` arm this test
        does not take.
      - the quota test needs none. It aborts at `:34`, so nothing renders, and
        although it IS an SRC_API call, `:24`'s first conjunct is false for an
        upvote so `can_downvote` is never called; `can_upvote`
        (app/utils.py:2480-2491) reads no `Site` at all.
      - the quota-BOUNDARY test added by task 7 needs none either, and for the
        same two reasons as the quota test above minus the abort: it is an
        SRC_API upvote, so `can_downvote` is never called and `:41` returns at
        `:42` without rendering. The only thing it changes about `:33` is which
        side of the comparison it lands on.
      - ALL FOUR web tests need one FOR BOTH REASONS AT ONCE -- the two
        original ones, the downvote-undo test task 7 added and the
        emoji-reversal test fix round 1 added. The first is the
        theme chain the module docstring records. The second is the template's
        own: `post/_comment_voting_buttons.html` line 10 reads
        `{% if (can_downvote_here or can_downvote(current_user, community,
        communities_banned_from_list)) ... %}`, `vote_for_reply:51` passes no
        `can_downvote_here`, Jinja `Undefined` is falsy, so the `or` evaluates
        `can_downvote` -- which is a Jinja global registered at
        app/request_hooks.py:54 and reads `Site.query.get(1)` at
        app/utils.py:2443, dereferencing it at `:2445`. The downvote test below
        asserts the downvote button's markup, which line 10 emits only when that
        call returns True, so that assertion is also the executable evidence
        that the template read the row.

    THE TEMPLATE'S OWN `can_upvote`/`can_downvote` GET JINJA `Undefined` FOR
    `communities_banned_from_list`, because `:51` passes none. Both functions
    test `if communities_banned_from_list is not None:` (app/utils.py:2470,
    :2484) and `Undefined` is not None, so they take that arm and evaluate
    `community.id in Undefined`, which is False rather than an error. The
    community-ban check in the template is therefore a no-op on this path, and
    no assertion below depends on it.
    """

    def test_a_banned_user_is_refused_with_403(self, db_session, app):
        """`:30` true -> `:31`'s abort(403). Arc 30->31, statement 31.

        THIS IS THE CORRECTION OF THE BRIEF'S FIRST TEST, which drove the ban
        through SRC_API. That construction cannot reach `:30` at all:
        `authorise_api_user` at `:21` rejects the bearer token first, because
        app/utils.py:3628 reads

            if user.ap_id is not None or user.verified is False or user.banned
               is True or user.deleted is True:

        and raises `Exception('incorrect_login')` at `:3629`. Setting
        `banned` is precisely what makes that token unusable, so the API arm
        cannot witness `:30`'s first disjunct by construction. MEASURED, not
        argued: the brief's version was run verbatim in a scratch class in this
        file and failed with

            AssertionError: assert None == 403
             +  where None = getattr(Exception('incorrect_login'), 'code', None)

        -- the `pytest.raises(Exception)` it used was loose enough to catch the
        authorisation refusal and pass it off as the abort.

        The web arm has no such gate: `:28` binds `current_user` directly, and
        `login_user` accepts a banned user because `User` (app/models.py:973,
        `class User(UserMixin, db.Model)`) does not override `UserMixin`'s
        `is_active`.

        `:30`'s SECOND disjunct is False here, so `user.banned` alone decides --
        but NOT for the module docstring's context-free reason, and the
        difference was probed rather than assumed. `web_ctx` DOES supply a
        request context, so the `except RuntimeError` at app/__init__.py:76
        that rescues a context-free call is never taken. What makes
        `ip_address()` (app/utils.py:2308, an alias of `app.get_ip_address`)
        return `''` anyway is that both of its sources are empty under
        `test_request_context`: `TRUSTED_CLIENT_IP_HEADER` is `''` by default
        (config.py:60) so the header read at `:70` is skipped, and
        `request.remote_addr` at `:75` is None -- measured, `flask.Flask(...)
        .test_request_context('/?')` leaves REMOTE_ADDR unset. `user_ip_banned`
        (app/utils.py:2311-2314) therefore returns None at its implicit
        fall-off WITHOUT reaching `banned_ip_addresses()` at all. The same
        empty string by a different route.

        `HTTPException` RATHER THAN `Exception` IN THE `raises`, and that is
        load-bearing. Probed in the container: `flask.abort(403)` raises
        `werkzeug.exceptions.Forbidden` and `abort(429)` raises
        `werkzeug.exceptions.TooManyRequests`, both `HTTPException` subclasses
        carrying `.code`. A bare `Exception` would have caught the
        `incorrect_login` above and, with `getattr(exc.value, 'code', None)`,
        turned a wrong-path failure into a confusing assertion error instead of
        a clear one.

        The vote counts are asserted because raising is also what the quota
        refusal below does -- the STATUS is the discriminator between the two,
        and the same-mechanism positive control that a vote from this fixture
        CAN land is
        `TestVoteForReplySourceAndPermission`'s
        `..._the_web_arm_loads_the_reply_and_reads_current_user`, the identical
        web call with `banned` left False.
        """
        s = _seed_reply()
        s.user.banned = True
        db.session.commit()

        with web_ctx(app, s.user):
            with pytest.raises(HTTPException) as exc:
                vote_for_reply(s.reply.id, 'upvote', True, None, SRC_WEB)

        assert exc.value.code == 403

        db.session.refresh(s.reply)
        assert s.reply.up_votes == 0
        assert PostReplyVote.query.filter_by(
            post_reply_id=s.reply.id, user_id=s.user.id).count() == 0

    def test_a_user_over_the_vote_quota_is_refused_with_429(self, db_session, app, monkeypatch):
        """`:30` false -> `:33`, `:33` true -> `:34`'s abort(429).
        Arcs 30->33 and 33->34; statements 33, 34.

        The quota is `current_app.config['VOTE_QUOTA']` (config.py:203, default
        240). Setting it to -1 makes `votes_cast_today`'s zero
        (app/models.py:47-52, which returns 0 when the redis key is absent)
        exceed it WITHOUT writing a redis key this test would then have to clean
        up -- which is why there is no `finally` here and why there must not be
        one: nothing votes.

        `monkeypatch.setitem` is safe on the session-scoped `app` fixture
        (tests/conftest.py:72-113) precisely because monkeypatch restores it at
        teardown; the fixture pushes one `app_context` for the whole session, so
        `current_app` at `:33` is this same object.

        429 RATHER THAN 403 IS THE DISCRIMINATOR against the test above. Both
        raise an `HTTPException` and both leave the reply unvoted, so the code
        is the only thing that separates them -- and the two are not exercised
        in lockstep: this user is not banned and `user_ip_banned()` is None
        outside a request context, so `:30` is false here, while the banned test
        has `votes_cast_today` at 0 against the default 240 and so takes
        `33->36` if it ever got there. Swapping the two abort codes fails both
        tests.

        SRC_API is used deliberately, and it needs no `Site` row: `:22`'s
        `can_upvote` reads none, and `:24`'s first conjunct is false for an
        upvote so `can_downvote` -- the gate that does read one -- is never
        called. No `web_ctx` either; this is the module docstring's
        context-free API claim executing again.
        """
        s = _seed_reply()
        monkeypatch.setitem(app.config, 'VOTE_QUOTA', -1)

        with pytest.raises(HTTPException) as exc:
            vote_for_reply(s.reply.id, 'upvote', True, None, SRC_API, auth=bearer(s.user))

        assert exc.value.code == 429

        db.session.refresh(s.reply)
        assert s.reply.up_votes == 0
        assert PostReplyVote.query.filter_by(
            post_reply_id=s.reply.id, user_id=s.user.id).count() == 0

    def test_a_web_downvote_takes_the_elif_and_marks_recently_downvoted(self, db_session, app):
        """`:46` false -> `:48`, `:48` true -> `:49`, then `:51`.
        Arcs 46->48 and 48->49; statements 48, 49.

        DIFFERS FROM THE CLASS-ABOVE WEB TEST IN THE DIRECTION ALONE, which is
        what sends control past `:46`'s first conjunct and into the elif.

        THE WITNESS IS THE `voted_down` MARKUP, not the bare fact that
        something rendered. `post/_comment_voting_buttons.html` line 11 emits
        `voted_down` and line 13 `fe-arrow-down-circle` only when
        `in_sorted_list(recently_downvoted_replies, comment.id)` is true, and
        the only statement that can put this id into that list is `:49`. The
        matching absence of `voted_up` is what rules out `:47` having run
        instead -- a mutation swapping the two direction literals at `:46`/`:48`
        would still render a 200 and still vote, and only the pair of assertions
        tells the two arms apart. MEASURED, not argued: the two literals were
        swapped in a line-scoped scratch mutant (`app/` restored afterwards and
        the restore confirmed with `git diff --quiet -- app/`) and exactly two
        tests failed -- this one and
        `..._the_web_arm_loads_the_reply_and_reads_current_user` above, which is
        its opposite-direction twin. Sixteen others passed against it.

        That same markup is the evidence for the second `Site` trigger: line 10
        gates the whole downvote block on `can_downvote(...)`, which returns
        False without `site.enable_downvotes`, so `voted_down` appearing at all
        proves the template's own `can_downvote` ran and read the row. Both
        triggers apply to this test -- the theme chain and that call -- and
        `make_site()` serves both. `enable_downvotes` is left exactly as
        `make_site()` (tests/factories.py:353) leaves it, which is the column
        default True (app/models.py:3954, in `class Site` which opens at
        app/models.py:3942).

        NO PERMISSION GATE STOPS THIS DOWNVOTE INSIDE `vote_for_reply` ITSELF,
        because `:24`'s `can_downvote` is inside the SRC_API arm -- that is the
        asymmetry `TestVoteForReplySourceAndPermission` registers. THIS TEST
        DOES NOT WITNESS IT, and saying so is the point: `enable_downvotes` is
        True here, so `can_downvote` would have permitted this downvote through
        either arm and the vote landing proves nothing about the missing gate.
        The construction that WOULD witness it -- the same call with
        `enable_downvotes` False -- turns line 10 of the template false and
        erases the `voted_down` markup this test needs for `:49`, so the two
        cannot share a test. The module docstring records that trade.

        This completes a real vote, hence the `finally`.
        """
        make_site()
        s = _seed_reply()
        try:
            with web_ctx(app, s.user):
                result = vote_for_reply(s.reply.id, 'downvote', True, None, SRC_WEB)
                body = result.get_data(as_text=True)

            db.session.refresh(s.reply)
            assert s.reply.down_votes == 1
            assert PostReplyVote.query.filter_by(
                post_reply_id=s.reply.id, user_id=s.user.id).count() == 1
            assert 'redirect_login' not in body
            assert 'voted_down' in body
            assert 'fe-arrow-down-circle' in body
            assert 'voted_up' not in body
        finally:
            _clear_votes_cast(s.user.id)

    def test_a_web_vote_that_undoes_an_existing_one_marks_neither(self, db_session, app):
        """`:46` false -> `:48`, `:48` false -> `:51`. Arcs 46->48 and 48->51.

        THE THIRD INPUT. Both `:46` and `:48` test `undo is None` as their
        second conjunct, so a vote that UNDOES an existing one fails both and
        falls to `:51` with both lists still at the `[]` `:44`/`:45` gave them.
        This test takes the UPVOTE half of that, which is the half that falsifies
        `:48` at its FIRST conjunct; the downvote half is a separate test below,
        for the reason the class docstring now records.

        VOTING UP TWICE IS WHAT MAKES `undo` NON-None, and that was read at
        source before it was relied on: `PostReply.vote` deletes the existing
        row and sets `undo = 'Like'` at app/models.py:3343 when
        `existing_vote.effect > 0` and the new direction is 'upvote'. It is also
        why `up_votes` is back to 0 and the `PostReplyVote` row is gone -- that
        pair is the positive control that the second call really was an UNDO and
        not, say, a silently refused duplicate, which would have left the count
        at 1.

        THE EMPTINESS IS ASSERTED WITH TWO SAME-MECHANISM POSITIVE CONTROLS,
        because `voted_up`/`voted_down` being absent is otherwise exactly what a
        template that rendered nothing, or that fell to its unauthenticated
        `else`, would also produce. The controls are
        `..._the_web_arm_loads_the_reply_and_reads_current_user` above, which
        renders the same template through the same helper and gets `voted_up`,
        and the downvote test immediately above, which gets `voted_down`.
        `redirect_login` absent plus `upvote_button` present is the third
        guard: it pins the render to line 1's authenticated branch, so the two
        absences are the empty lists and not the wrong half of the template.

        THIS IS THE ONLY TEST IN THE FILE THAT WITNESSES `:46`'s `undo is None`,
        and that was measured rather than claimed: deleting `and undo is None`
        from `:46` in a line-scoped scratch mutant (`app/` restored afterwards
        and the restore confirmed with `git diff --quiet -- app/`) failed this
        test alone -- the other seventeen passed, because every one of them has
        `undo` None anyway and cannot tell the conjunct from its absence.

        THE CLAIM USED TO READ "`undo is None` AT ALL", WITHOUT THE `:46`, AND
        THAT WAS FALSE. `:48` carries the same conjunct and this test cannot
        reach it: an UPVOTE undo falsifies `:48` at its first conjunct, so the
        second is never evaluated here. Task 7 measured the gap --
        `48s/ and undo is None//` left all 27 tests green -- and the downvote-undo
        test below is what closes it. The sentence is narrowed rather than
        deleted because the `:46` half of it is still true and still measured.

        ONE `_clear_votes_cast` FOR TWO CALLS, and that is correct rather than
        an oversight: the key is per user and per day, and the undo path at
        app/models.py:3337-3343 never reaches the `votes_cast` bookkeeping at
        `:3382-3386`, which sits in the `else` for a first-time vote. So only
        the first call wrote it.
        """
        make_site()
        s = _seed_reply()
        try:
            with web_ctx(app, s.user):
                vote_for_reply(s.reply.id, 'upvote', True, None, SRC_WEB)
                result = vote_for_reply(s.reply.id, 'upvote', True, None, SRC_WEB)
                body = result.get_data(as_text=True)

            db.session.refresh(s.reply)
            assert s.reply.up_votes == 0
            assert PostReplyVote.query.filter_by(
                post_reply_id=s.reply.id, user_id=s.user.id).count() == 0
            assert 'redirect_login' not in body
            assert 'upvote_button' in body
            assert 'voted_up' not in body
            assert 'voted_down' not in body
        finally:
            _clear_votes_cast(s.user.id)

    def test_a_web_downvote_that_undoes_an_existing_one_marks_neither(self, db_session, app):
        """CLOSES NO STATEMENT AND NO ARC -- it walks `46->48` and `48->51`, both
        of which the two tests above already take. It earns its place by a
        unique kill against `48s/ and undo is None//`, which left all 27 tests
        green in the task-7 pass.

        THE MIRROR IMAGE OF THE TEST ABOVE, AND THAT IS THE POINT. `:46` and
        `:48` carry the SAME second conjunct, and the class docstring's third
        input -- "either direction, `undo` NOT None" -- was taken in ONE
        direction only. Deleting the conjunct from `:46` fails the upvote-undo
        test above, measured; deleting it from `:48` failed nothing, because
        `:48`'s first conjunct is false for the upvote that test uses and the
        line never gets as far as its second. So the file pinned `:46`'s
        `undo is None` and not `:48`'s, and an implementation that marked a
        removed DOWNVOTE as recently-downvoted would have shipped.

        VOTING DOWN TWICE IS WHAT MAKES `undo` NON-None on this arm, the mirror
        of the test above's mechanism: `PostReply.vote` sets `undo = 'Dislike'`
        at app/models.py:3357 when `existing_vote.effect < 0` and the new
        direction is 'downvote', and deletes the row. `down_votes` back at 0
        with no `PostReplyVote` row is the positive control that the second call
        really was an undo rather than a silently refused duplicate.

        THE SAME THREE MARKUP GUARDS as the test above, and `voted_down` absent
        is the one that matters: `post/_comment_voting_buttons.html` line 11
        emits it only when `in_sorted_list(recently_downvoted_replies,
        comment.id)`, so the mutant's `:49` would put it there.
        `make_site()` is needed twice over for the reasons the class docstring
        gives, and `enable_downvotes` is left at `make_site()`'s True so
        line 10 renders the downvote block at all.
        """
        make_site()
        s = _seed_reply()
        try:
            with web_ctx(app, s.user):
                vote_for_reply(s.reply.id, 'downvote', True, None, SRC_WEB)
                result = vote_for_reply(s.reply.id, 'downvote', True, None, SRC_WEB)
                body = result.get_data(as_text=True)

            db.session.refresh(s.reply)
            assert s.reply.down_votes == 0
            assert PostReplyVote.query.filter_by(
                post_reply_id=s.reply.id, user_id=s.user.id).count() == 0
            assert 'redirect_login' not in body
            assert 'upvote_button' in body
            assert 'voted_down' not in body
            assert 'voted_up' not in body
        finally:
            _clear_votes_cast(s.user.id)

    def test_a_web_reversal_that_only_rewrites_the_emoji_marks_neither(self, db_session, app):
        """CLOSES NO STATEMENT AND NO ARC -- it walks `46->48` and `48->51`, both
        of which the two tests above already take. It earns its place by a
        unique kill against `48s/vote_direction == 'downvote' and //`, which
        task 7's report argued was the round's ONE EQUIVALENT mutant. THAT
        ARGUMENT WAS FALSE and is retracted in the module docstring; this test
        is the retraction's executable half.

        THE FIFTH INPUT TO `:46`/`:48`, and the one the class docstring's
        four-row table could not see, because that table takes `vote_direction`
        at `:46` to be 'upvote' or 'downvote' by the time control arrives. IT
        NEED NOT BE. The false argument was that `app/models.py:3321`'s assert
        narrows the caller's direction to those two literals. It does not:
        `:3317`/`:3319` remap 'reversal' by ASSIGNING TO `vote()`'s OWN LOCAL,
        which is what the assert then sees. The caller's `vote_direction` is
        untouched and is still 'reversal' at `:46` and `:48`.

        THE REMAINING HALF OF THE FALSE ARGUMENT was that a 'reversal' which
        survives the assert always yields a NON-None `undo`, so the one state
        where mutant and original differ is unreachable. app/models.py:3325-3331
        is the counterexample it walked past: with an existing vote, a non-empty
        `emoji`, and a remapped direction MATCHING the existing vote's effect,
        `vote` rewrites the emoji and `return None` -- before any of the removal
        branches that assign `undo`. So `undo is None` while nothing was undone,
        and the state ('reversal', None) is reached.

        ON THAT STATE THE MUTANT IS NOT EQUIVALENT. Original `:48`:
        `'reversal' == 'downvote'` is False, so both lists stay empty. Mutant
        `:48`: `undo is None` alone is True, so `:49` marks the reply
        recently-DOWNVOTED in a render where nothing was downvoted at all.

        REACHABLE FROM THE WEB, AND WITHOUT `python -O`. app/post/routes.py:564
        routes `/comment/<int:comment_id>/<vote_direction>/<federate>/emoji`
        with the BARE string converter -- there is no `any(...)` whitelist on
        the direction -- and `:575` hands that value together with
        `request.form.get('emoji')` to `vote_for_reply`. A POST to
        `/comment/<id>/reversal/default/emoji` by a user who has already
        upvoted that reply is exactly the second call below.

        THE WITNESS IS THE MARKUP, under the same three guards the two tests
        above use: `redirect_login` absent and `upvote_button` present pin the
        render to line 1's authenticated branch, so `voted_down` absent is the
        empty list rather than the wrong half of the template.
        `post/_comment_voting_buttons.html` line 11 emits `voted_down` only when
        `in_sorted_list(recently_downvoted_replies, comment.id)`, and `:49` is
        the only statement that can put this id there.

        `voted_up` IS ASSERTED ABSENT TOO, and it is NOT this test's unique
        kill: it is the same-input witness for `46s/vote_direction == 'upvote'
        and //`, which the downvote test above already kills. Re-running that
        mutation after this test fails both, and the fix-round report records
        why the two lines are not in the same position -- `:46` is the `if`
        head, evaluated on every web call, so a plain downvote with `undo` None
        already falsifies its first conjunct while leaving its second true;
        `:48` is reached only when `:46` is false, and for a downvote its own
        first conjunct is TRUE, which masks the deletion. Only a direction that
        is neither literal, arriving with `undo` None, separates `:48`'s mutant
        from the original.

        THE POSITIVE CONTROL THAT NOTHING WAS UNDONE is `up_votes == 1` with the
        `PostReplyVote` row still present and its `emoji` now set. Without it,
        both markup absences are also what a REMOVAL would produce -- and a
        removal sets `undo`, which would move this input back onto the table's
        fourth row and kill nothing.

        `make_site()` is needed twice over for the reasons the class docstring
        gives. ONE `_clear_votes_cast` FOR TWO CALLS, for the same reason the
        upvote-undo test gives: the emoji early return at app/models.py:3331
        never reaches the `votes_cast` bookkeeping at `:3382-3386`, so only the
        first call wrote the key.
        """
        make_site()
        s = _seed_reply()
        try:
            with web_ctx(app, s.user):
                vote_for_reply(s.reply.id, 'upvote', True, None, SRC_WEB)
                result = vote_for_reply(s.reply.id, 'reversal', True, '👍', SRC_WEB)
                body = result.get_data(as_text=True)

            db.session.refresh(s.reply)
            assert s.reply.up_votes == 1
            vote = PostReplyVote.query.filter_by(
                post_reply_id=s.reply.id, user_id=s.user.id).one()
            assert vote.emoji == '👍'
            assert 'redirect_login' not in body
            assert 'upvote_button' in body
            assert 'voted_down' not in body
            assert 'voted_up' not in body
        finally:
            _clear_votes_cast(s.user.id)

    def test_a_voter_exactly_at_the_vote_quota_is_still_allowed_through(self, db_session, app, monkeypatch):
        """CLOSES NO STATEMENT AND NO ARC -- `33->36` is taken by four tests
        already. IT EARNS ITS PLACE BY PINNING A REGISTERED DEFECT, D501, AND
        NOT BY CATCHING A REGRESSION. Its unique kill against
        `33s/user.id) > current/user.id) >= current/`, which left all 27 tests
        green in the task-7 pass, is a FIX-CATCHER rather than a
        fault-direction kill, and saying otherwise would contradict this
        round's own register: D501 records the `>` at `:33` AS the off-by-one,
        which makes `>=` the candidate FIX, and killing a semantically better
        mutant measures change-detection, not defect-detection. The clause this
        test qualifies under is therefore the pin-a-defect one, which obliges
        it to state the fix-edit it owes -- stated below, in the terms
        `TestVoteForReplySourceAndPermission`'s reversal test states its own.

        THE QUOTA TEST ABOVE CANNOT PIN THE BOUNDARY, and that is a property of
        its fixture rather than an oversight. It sets `VOTE_QUOTA` to -1 against
        a `votes_cast_today` of 0, and 0 is strictly greater than -1 AND greater
        than or equal to it, so `>` and `>=` agree on that input. The single
        input on which they disagree is equality, and this test is that input:
        `VOTE_QUOTA` 0 against 0 votes cast means `:33` is false under `>` and
        the vote lands, and true under `>=` and the call aborts 429.

        WHAT THIS TEST ALSO PINS, SAID OUT LOUD BECAUSE A BOUNDARY TEST IS WHERE
        IT MUST BE: `:33` is evaluated BEFORE the vote is cast, so `VOTE_QUOTA =
        N` permits N + 1 votes. The assertion below -- `VOTE_QUOTA` 0 and a vote
        LANDS -- is that off-by-one in executable form. It is recorded, not
        fixed: the default is 240 (config.py:203) so nothing is burning, and
        `app/shared/post.py:53` carries the identical comparison -- as do
        `app/activitypub/routes.py:2438` and `:2459`, written as `<=` on the
        permitting side, which is the same boundary -- so this is a consistent
        product decision across all four enforcement sites rather than a
        divergence between the mirrored pair. It is registered as D501, which
        names this test as its pin.

        THIS TEST ASSERTS THE DEFECT, NOT CORRECT BEHAVIOUR, AND WHOEVER FIXES
        D501 MUST EDIT IT. **The fix is one boundary at FOUR enforcement sites,
        not two, and this obligation said two until the round's last review
        caught it.** A whole-repository `/usr/bin/grep -rn "VOTE_QUOTA"
        --include=*.py app/` returns five occurrences:
        `app/shared/reply.py:33` and `app/shared/post.py:53`, both spelled
        `if votes_cast_today(user.id) > VOTE_QUOTA:` as a REFUSAL; and
        `app/activitypub/routes.py:2438` and `:2459`, spelled
        `votes_cast_today(user.id) <= VOTE_QUOTA:` as a PERMISSION inside the
        federation inbox's `process_upvote`/`process_downvote`, each calling
        `liked.vote()` directly. `> N` refusing and `<= N` permitting are the
        SAME boundary written two ways, so all four already agree and all four
        carry the same off-by-one. The fifth, `app/user/routes.py:127`, is a
        division for a display percentage and enforces nothing. When the
        boundary moves, the edit owed here is to INVERT this test:
        `VOTE_QUOTA` 0 against a `votes_cast_today` of 0 must then be REFUSED
        with 429, so the assertions below failing at that point is the fix
        landing, not a regression.

        THE METHOD NOTE, BECAUSE IT IS THE SAME ONE D497 CARRIES AND THIS
        DOCSTRING QUOTED IT WHILE COMMITTING IT: the two-site claim came from a
        grep hand-scoped to four path globs, which is a filtered search that
        called itself an enumeration. Re-deriving it required a method that
        fails differently -- a whole-tree search with no path filter -- and not
        a second grep of the same shape. Note also that the interactive `grep`
        in this environment is a `ugrep` wrapper carrying `--ignore-files`,
        which silently skips paths a `.gitignore` excludes; `/usr/bin/grep`
        does not.

        THE ASSERTION IS THE COMPLETED VOTE, not the absence of an exception. An
        `abort(429)` would fail this test on the raise, but so would any other
        failure, so `up_votes` and the `PostReplyVote` row are what say the call
        got past `:33` rather than merely got past it in some other year.

        `monkeypatch.setitem` on the session-scoped `app` fixture is safe for
        the reason the quota test above records: monkeypatch restores it at
        teardown. `_clear_votes_cast` is mandatory here and was NOT mandatory
        there, and the difference is the whole point -- that test aborts before
        `:36` and writes no redis key, this one votes and writes one.
        """
        s = _seed_reply()
        monkeypatch.setitem(app.config, 'VOTE_QUOTA', 0)
        try:
            assert vote_for_reply(s.reply.id, 'upvote', True, None, SRC_API,
                                  auth=bearer(s.user)) == s.user.id

            db.session.refresh(s.reply)
            assert s.reply.up_votes == 1
            assert PostReplyVote.query.filter_by(
                post_reply_id=s.reply.id, user_id=s.user.id).count() == 1
        finally:
            _clear_votes_cast(s.user.id)


class TestDeleteReply:
    """`delete_reply` (app/shared/reply.py:241-266) -- the author's own soft delete.

    `:247` filters on `id`, `user_id` AND `deleted=False` and calls `.one()`, so
    only the author can delete, only once, and a miss raises rather than
    returning None. THIS CLASS USED TO ARGUE THAT NO TEST NEED ASSERT "the wrong
    user got nothing" BECAUSE THE `.one()` WOULD RAISE, and route the miss-path
    to the register. TASK 7 REFUTED THAT BY MEASUREMENT and the last test here
    is the correction. The argument was about the unmutated function: whether
    the raise happens is exactly what a mutant decides, and
    `247s/, user_id=user_id,/, /` -- neutralising the author conjunct, which
    task 7's brief made a mandatory probe -- left all 27 tests of the day green
    while letting any bearer soft-delete any reply. A conjunct no test ever
    executes against a mismatch is unguarded whatever the surrounding call
    raises on other inputs. Nothing about the TARGET SET changes: a `.one()`
    miss still adds no statement and no arc, and the new test closes none --
    it is there for the unique kill.

    `:248-249` set `deleted` and `deleted_by` on EVERY path through this
    function, so asserting `deleted` alone witnesses nothing about the counter
    arithmetic below it or about which source arm ran. Sub-project 36 shipped
    exactly that test against `delete_post` and had to replace it. Every test
    here asserts on a counter or on the return shape; `deleted` appears only
    where its VALUE is arm-specific (`deleted_by` on the web test, which is the
    id `:245` read out of `current_user`).

    NO `Site` ROW IS SEEDED BY ANY TEST HERE, and that premise was re-derived
    for this task rather than inherited. A test needs `make_site()` if it
    reaches `render_template` or `can_downvote`; `delete_reply` reaches
    neither. `grep -n "can_upvote\\|can_downvote" app/shared/reply.py` returns
    `:15` (the import), `:22` and `:24` -- both inside `vote_for_reply`.
    `grep -n "render_template" app/shared/reply.py` returns `:13` (the import),
    `:51` (`vote_for_reply`) and `:131` (`subscribe_reply`). `grep -n
    "Site\\|g\\.site"` returns `:10` (the import) and `:365`, `Site.admins()`
    inside `report_reply`. Nothing in `:241-266` is in either list.

    NO `_clear_votes_cast` IS NEEDED EITHER: `delete_reply` completes no vote,
    so it never writes the `votes_cast_{today}_{user_id}` key the module
    docstring's REAL REDIS note governs.

    THE EAGER CELERY TASK AT `:261` SENDS NOTHING. `task_selector`
    (app/shared/tasks/__init__.py:4) runs the body inline under
    `current_app.debug`, and `app/shared/tasks/deletes.py:29`'s `delete_reply`
    calls `delete_object` (`:118`), which returns at `:133` on
    `if community.private or not community.instance.online():`. `_seed_reply`
    defaults `private=True`, which is register entry D393(d)'s federation
    lever. The task also opens its own session and re-queries the reply by id,
    which is safe because `:259` commits before `:261` runs.

    THE `path` QUESTION, MEASURED. `:256-258` runs raw SQL keyed on
    `tuple(reply.path[:-1])`, and a SINGLE-element path makes that an EMPTY
    tuple. An empty tuple is NOT a legal `IN` operand for this driver. Probed
    against the live test database inside the container, sqlalchemy 2.0.52 on
    `postgresql+psycopg2`, with the same statement text `:257` uses:

        two-element (1, 2) -> OK []
        one-element (1,)   -> OK []
        empty      ()      -> RAISED ProgrammingError
                              (psycopg2.errors.SyntaxError) syntax error at or near ")"

    psycopg2 renders an empty tuple as `()`, giving `where id in ()`, which
    Postgres rejects outright -- it is not an empty result, it is a syntax
    error that would propagate out of `delete_reply`. PRODUCTION DOES BUILD
    SUCH A PATH, and this sentence formerly said the opposite. The column has
    five writers, enumerated in the module docstring's DEFECTS section;
    app/cli.py:664-670, inside `@app.cli.command("lemmy-import")`
    (app/cli.py:275), builds the array from `path_parts[1:-1]` -- ancestors
    only, the reply's own id EXCLUDED -- so an ordinary first-level nested
    comment imported from Lemmy gets a ONE-ELEMENT path and its author can
    neither delete nor restore it. That is defect 3 there, and it is reachable,
    not latent.

    app/models.py:3053-3059 (in `class PostReply`, which opens at
    app/models.py:2887) is the writer whose convention the raw SQL assumes: a
    top-level reply gets `[0, reply.id]` and a nested one
    `in_reply_to.path[:] + [reply.id]`, always at least two elements with the
    reply's own id last. The ancestor test below seeds THAT shape,
    `[0, parent.id, reply.id]` -- a genuinely multi-element `IN` operand, with a
    real ancestor row so the decrement has a witness.

    THE EMPTY TUPLE IS STILL LEFT UNTESTED, but for a narrower reason than the
    one first given. It is unreachable from any row `tests/factories.py` can
    build, and the only writer that reaches it is a cli import command; making
    it executable would mean simulating `lemmy-import` inside a unit test of
    `delete_reply`, which is the wrong place for it. The probe above is the
    record instead.
    """

    def test_an_api_delete_decrements_all_four_counters(self, db_session):
        """`:242` true -> `:243`; `:251` true -> `:252`-`:254`; `:256` false ->
        `:259`; `:263` true -> `:264`.
        Arcs 242->243, 251->252, 256->259, 263->264; statements 242, 243, 247,
        248, 249, 251, 252, 253, 254, 255, 259, 261, 263, 264.

        FOUR counters move and all four are asserted, because `:252`-`:255` are
        four separate statements. An earlier version said only that a mutation
        can "remove one at a time", which is true for REMOVAL and was silently
        untrue for an EXACT-COPY assignment such as rewriting `:253` as
        `reply_count_cross_posted = reply_count`: with the columns at their
        default 0 that mutant reads `(-1, -1, -1, -1)` like the original and
        SURVIVES, measured. `_seed_distinct_reply_counters` exists for exactly
        that one class of mutant; its docstring carries all three cases,
        measured, INCLUDING the two that a later version of this sentence got
        wrong. Note what distinctness does NOT buy: swapping `:252` and `:253`
        is an equivalent mutant that passes under any seeding, because four
        in-place `-= 1` on four independent columns are permutation-invariant.

        The before-values are still captured rather than hardcoded, so that the
        test states the delta it is testing rather than four magic numbers that
        would have to be edited in two places at once.

        THE RETURN IS ASSERTED BY SHAPE AND IDENTITY, NOT BY `deleted`. `:264`
        returns `(user_id, reply)`; unpacking it into two names is itself the
        witness that `:266`'s bare `return` was not taken, and `reply.id`
        pins which row came back. `reply.deleted` is deliberately NOT the
        witness -- `:248` sets it on both arms.

        THIS TEST ALSO WITNESSES `:256`'s FALSE ARM POSITIVELY rather than by
        absence. `_seed_reply`'s reply has `path is None`, so `if reply.path:`
        is false. Mutate `:256` to `if not reply.path:` and this test does not
        merely stop asserting something -- it ERRORS, because `None[:-1]` is a
        TypeError before the SQL is ever built. That was confirmed by running
        the mutant, not reasoned about; see the task report.
        """
        s = _seed_reply()
        before = _seed_distinct_reply_counters(s)

        user_id, reply = delete_reply(s.reply.id, SRC_API, auth=bearer(s.user))

        assert (user_id, reply.id) == (s.user.id, s.reply.id)
        db.session.refresh(s.post)
        db.session.refresh(s.community)
        db.session.refresh(s.user)
        assert (s.post.reply_count, s.post.reply_count_cross_posted,
                s.community.post_reply_count, s.user.post_reply_count) == \
               (before[0] - 1, before[1] - 1, before[2] - 1, before[3] - 1)

    def test_a_web_delete_reads_current_user_and_returns_none(self, db_session, app):
        """`:242` false -> `:245`; `:263` false -> `:266`.
        Arcs 242->245 and 263->266; statements 245, 266.

        THE RETURN SHAPE IS THE DISCRIMINATOR. The API arm returns a
        `(user_id, reply)` tuple and this arm returns None, so `is None` cannot
        be produced by the other branch -- and the API test above cannot be
        produced by this one, which is what makes the pair able to catch a swap
        between `:242` and `:263` even though both read the same `src`.

        `is None` IS THE ONLY ARM DISCRIMINATOR HERE, and an earlier version of
        this docstring wrongly claimed a second one. It said `deleted_by` was
        "an arm-specific VALUE". IT IS NOT: only the author can delete a reply
        at all (`:247` filters `user_id=user_id`), so `bearer(s.user)` and
        `web_ctx(app, s.user)` necessarily name THE SAME USER and both arms
        write `s.user.id` at `:249`. That is false-witness mechanism 2 --
        a fixture coincidence making two arms produce the same value -- asserted
        inside a paragraph claiming to have defeated mechanism 1.

        THAT `:245` READ `current_user` is witnessed by `:247` instead: it
        filters `user_id=user_id` and calls `.one()`, so an id from anywhere
        else raises `NoResultFound` rather than deleting. The `deleted_by`
        assertion below is kept as a cheap cross-check that `:249` wrote that
        id and not some other column's, and is NOT independent of the `.one()`;
        it is not load-bearing and is not this test's witness for either arc.

        `auth=None` is passed explicitly because `delete_reply`'s signature at
        `:241` is `(reply_id, src, auth)` with no default for `auth`; the web
        arm never reads it.

        A counter is asserted as well, so that a mutant gutting the body but
        keeping the `return` cannot pass on the None alone.
        """
        s = _seed_reply()
        before = _seed_distinct_reply_counters(s)[3]

        with web_ctx(app, s.user):
            assert delete_reply(s.reply.id, SRC_WEB, auth=None) is None

        db.session.refresh(s.reply)
        db.session.refresh(s.user)
        assert s.reply.deleted_by == s.user.id
        assert s.user.post_reply_count == before - 1

    def test_a_bot_authors_reply_skips_the_three_post_and_community_counters(self, db_session):
        """`:251` false -> `:255`. Arc 251->255; statement 255 on its false leg.

        `:251` is `if not reply.author.bot:`, so a bot author skips
        `:252`-`:254` entirely -- but `:255`, the AUTHOR's own counter, sits
        OUTSIDE the guard at four-space indent and still decrements. THAT
        ASYMMETRY IS THE WITNESS: asserting only that the post and community
        counters held would not distinguish this run from the function never
        having been called at all, which is false-witness mechanism 3 --
        emptiness with no same-mechanism positive control. `:255` is that
        control, and it moves through the same commit as the three that did
        not.

        All three guarded counters are read, not just `post.reply_count`,
        because `:252`-`:254` are separable statements and a guard that leaked
        only one of them would otherwise go unseen. They are seeded to distinct
        non-zero values by `_seed_distinct_reply_counters` for the reason that
        helper's docstring gives, which applies here too: a mutant that made
        one of the three guarded statements COPY another's value rather than
        decrement its own would be invisible at the columns' shared default 0.
        It is only the copy class -- not a permutation of the three, which is
        an equivalent mutant under any seeding.

        `User.bot` is app/models.py:1016, in `class User` which opens at
        app/models.py:973; it defaults False, so `_seed_reply`'s user takes the
        true arm and this test is the only one here on the false leg.
        `authorise_api_user` (app/utils.py:3628) rejects on `ap_id`,
        `verified`, `banned` and `deleted` and says nothing about `bot`, so the
        bearer token still authorises.
        """
        s = _seed_reply()
        s.user.bot = True
        before = _seed_distinct_reply_counters(s)

        delete_reply(s.reply.id, SRC_API, auth=bearer(s.user))

        db.session.refresh(s.post)
        db.session.refresh(s.community)
        db.session.refresh(s.user)
        assert (s.post.reply_count, s.post.reply_count_cross_posted,
                s.community.post_reply_count) == before[:3]
        assert s.user.post_reply_count == before[3] - 1

    def test_a_reply_with_ancestors_decrements_their_child_counts(self, db_session):
        """`:256` true -> `:257`. Arc 256->257; statements 256, 257.

        `:257-258` is raw SQL against `post_reply.child_count`
        (app/models.py:2899, in `class PostReply` which opens at
        app/models.py:2887), keyed on `tuple(reply.path[:-1])` -- the reply's
        ancestors, excluding itself. `path` (app/models.py:2898) is a nullable
        ARRAY with no default and `make_post_reply` (tests/factories.py:455)
        does not set it, so the path is seeded here by hand in the shape
        production builds: `[0, parent.id, reply.id]`, per app/models.py:3054
        and `:3059`. `path[:-1]` is then `(0, parent.id)` -- multi-element, so
        the empty-tuple syntax error documented on the class does not arise,
        and id 0 matches no row so only `parent` is hit.

        THE WITNESS IS `parent.child_count`, and nothing else in `delete_reply`
        writes that column, so it cannot be moved by any statement outside
        `:257-258`. It is refreshed rather than read from the session because
        the raw `UPDATE` bypasses the identity map.

        A NEGATIVE CONTROL IS INCLUDED. `bystander` is a reply under the same
        post with the same seeded `child_count` that is NOT in the path, and
        its count must hold. Without it, a mutant that dropped the `where`
        clause and decremented every row in the table would pass -- false
        witness mechanism 1, asserting on state something else could have set
        unconditionally.

        THE REPLY'S OWN `child_count` IS ASSERTED TOO, AND THE BYSTANDER DOES
        NOT COVER THAT CASE. `:258`'s operand is `tuple(reply.path[:-1])`; the
        `[:-1]` is what excludes the reply itself, and `bystander` is not in
        `path` at all, so a mutant that merely widened the slice would leave
        the bystander untouched and pass. Measured in the task-7 pass:
        `258s/tuple(reply.path\\[:-1\\])/tuple(reply.path)/` left all 27 tests
        green. The seed of 6 is distinct from both ancestors' and from 0 and 1,
        so a mutant assigning a literal is visible as well.
        """
        s = _seed_reply()
        parent = make_post_reply(s.post, s.user, body='parent')
        bystander = make_post_reply(s.post, s.user, body='bystander')
        parent.child_count = 1
        bystander.child_count = 1
        s.reply.child_count = 6
        s.reply.path = [0, parent.id, s.reply.id]
        db.session.commit()

        delete_reply(s.reply.id, SRC_API, auth=bearer(s.user))

        db.session.refresh(parent)
        db.session.refresh(bystander)
        db.session.refresh(s.reply)
        assert parent.child_count == 0
        assert bystander.child_count == 1
        assert s.reply.child_count == 6

    def test_a_non_author_cannot_delete_another_users_reply(self, db_session):
        """CLOSES NO STATEMENT AND NO ARC -- a `.one()` miss adds neither. It
        earns its place by a unique kill against the mutant task 7's brief made
        MANDATORY, `247s/, user_id=user_id,/, /`, which left all 27 tests green.

        THIS OVERTURNS THE CLASS DOCSTRING'S ROUTING DECISION, AND THE
        MEASUREMENT IS WHY. That paragraph reasoned that no test need assert
        "the wrong user got nothing" because `.one()` would raise, and routed
        the miss-path to the register. The reasoning describes the UNMUTATED
        function: with `user_id=user_id` deleted from the filter there is no
        miss and no raise, the interloper's call finds the author's reply and
        soft-deletes it, and every assertion in this class still passes because
        no test in it ever calls `delete_reply` as anybody but the author. An
        unexecuted guard is not a guarded one.

        THE WITNESS IS NOT THE RAISE. A crash is a weak kill, so the assertions
        that carry this test are the ones about state: after the refusal the
        reply is still undeleted and `deleted_by` is still None. Under the
        mutant both are false -- `:248`-`:249` have run -- and they would remain
        the discriminator even against a variant that swallowed the exception.
        `pytest.raises` is kept as the outer frame only because the unmutated
        function does raise and a test that let a `NoResultFound` escape would
        error rather than fail.

        `interloper` is minted local and verified so that `authorise_api_user`
        (app/utils.py:3628) accepts its bearer token -- the refusal under test
        must come from `:247`, not from the token check at `:243`.
        """
        s = _seed_reply()
        interloper = make_user(s.instance, 'interloper', local=True)
        db.session.commit()

        with pytest.raises(Exception):
            delete_reply(s.reply.id, SRC_API, auth=bearer(interloper))

        db.session.refresh(s.reply)
        assert s.reply.deleted is False
        assert s.reply.deleted_by is None

    def test_deleting_a_one_element_path_reply_does_not_reach_the_empty_in_operand(self, db_session):
        """The cli-imported one-element path must not raise.

        This is the executable pin the class docstring's `path` section said was
        left untested. It seeds the shape `app/cli.py` produces for a
        first-level nested comment -- ancestors only, the reply's own id
        excluded, so a single element -- and calls `delete_reply`.

        `:256`'s guard is `if reply.path and len(reply.path) > 1:`. Without the
        length half, `tuple(reply.path[:-1])` is the EMPTY tuple, psycopg2
        renders `where id in ()`, and Postgres raises
        `(psycopg2.errors.SyntaxError) syntax error at or near ")"` out of
        `delete_reply` -- a 500 for the reply's own author. The probe is in the
        class docstring.

        THE WITNESS IS NOT THE ABSENCE OF A RAISE. A test that only asserted
        "no exception" would pass against a `delete_reply` whose body had been
        deleted entirely. The assertions are that the delete actually HAPPENED
        -- `deleted` true, `deleted_by` set -- and that the ancestor named by
        the malformed path was NOT decremented, which is the correct outcome
        for a path that does not identify its ancestors: `parent.child_count`
        is seeded to 5 and must still read 5.

        `parent` is a real second reply so the path holds a real id rather than
        a synthetic one; the point is the LENGTH, and a real id makes the
        child_count assertion meaningful rather than vacuous.
        """
        s = _seed_reply()
        parent = make_post_reply(s.post, s.user)
        db.session.commit()
        parent.child_count = 5
        s.reply.path = [parent.id]
        db.session.commit()

        delete_reply(s.reply.id, SRC_API, auth=bearer(s.user))

        db.session.refresh(s.reply)
        db.session.refresh(parent)
        assert s.reply.deleted is True
        assert s.reply.deleted_by == s.user.id
        assert parent.child_count == 5


class TestRestoreReply:
    """`restore_reply` (app/shared/reply.py:269-294) -- the author's own undelete.

    THE EXTENT IS THE AST'S, NOT THE BRIEF'S. The brief named `:269-296`;
    `ast.parse` puts the `def` at 269 and its last statement's `end_lineno` at
    294, and `:295-296` are the blank lines before `report_reply` at `:297`.

    `:275` filters `id`, `user_id` AND `deleted=True` and calls `.one()`, so a
    reply must already be deleted and only its author can restore it. THAT MISS
    IS NOW TESTED, by the last test in this class, and the reasoning that
    routed it to the register instead -- inherited here from `TestDeleteReply`
    and corrected there in the same sweep -- was refuted by task 7's mutation
    pass: `275s/, user_id=user_id,/, /` left all 27 tests of the day green,
    because no test in this class had ever called `restore_reply` as anybody but
    the author. The register entry remains accurate about the TARGET SET, which
    is unchanged: a `.one()` miss adds no statement and no arc, and the new test
    closes neither.

    `:276-277` SET `deleted` AND `deleted_by` ON EVERY PATH, so `deleted is
    False` witnesses that the function ran and nothing about which source arm
    ran or what the counters did -- false-witness mechanism 1. `deleted_by` is
    WEAKER STILL here than it was in `delete_reply`: `:277` assigns the constant
    None, so it cannot differ between arms even in principle. (In
    `delete_reply:249` it at least assigns `user_id`, and was still not an arm
    discriminator, because `:247` filters `user_id=user_id` and both arms
    therefore name the same user -- the correction `TestDeleteReply`'s web test
    carries. Neither trap is repeated below.) The arm discrimination here is the
    return SHAPE, the flash, and the counters.

    THIS FUNCTION DOES NOT MIRROR `delete_reply`, AND THE LAST TEST PINS THAT.
    `delete_reply:252`-`:254` decrement three counters inside the bot guard;
    `restore_reply:280` increments ONE. `post.reply_count_cross_posted` and
    `community.post_reply_count` are never restored, so a delete-then-restore
    cycle leaves both permanently one low. That is a rollback that does not undo
    what it did, and it is REGISTERED, NOT FIXED: a coverage round has no
    standing to change a counter, and the divergence is better witnessed by an
    executing test than asserted in a document.

    THE ASYMMETRY IS `:279`-`:280` AND STOPS THERE, re-derived from the AST
    rather than from indentation. The `If` node whose test unparses to `not
    reply.author.bot` opens at `:279`, its `body` has EXACTLY ONE element --
    `reply.post.reply_count += 1` at `:280` -- and its `orelse` is empty.
    `:281`, `reply.author.post_reply_count += 1`, is a top-level statement of
    the function body, so it runs on both legs of the guard and mirrors
    `delete_reply:255` exactly. A test pinning the divergence must therefore
    read `post.reply_count_cross_posted` and `community.post_reply_count`, and
    must NOT lean on `author.post_reply_count`, which is symmetric and witnesses
    nothing. The last test obeys that; the bot test does not pretend to.

    NO `Site` ROW IS SEEDED BY ANY TEST HERE, and the premise was RE-DERIVED BY
    A METHOD THAT FAILS DIFFERENTLY from `TestDeleteReply`'s. That class used
    three file-wide textual greps; this used an `ast.walk` over the
    `restore_reply` FunctionDef node ALONE, collecting every `Call` target and
    every `Name`/`Attribute` identifier in the subtree. The call set is exactly
    `authorise_api_user`, `query`, `filter_by`, `one`, `tuple`, `execute`,
    `text`, `commit`, `flash`, `_` and `task_selector`, and `render_template`,
    `can_upvote`, `can_downvote` and `Site` appear nowhere in the subtree under
    any spelling. The two methods fail in opposite directions -- a grep cannot
    tell which function a matching line is in, and an AST walk cannot see a call
    made through a string name -- and both say the row is unnecessary. The six
    tests below call `make_site()` nowhere and pass, which is the third check.

    NO `_clear_votes_cast` IS NEEDED: `restore_reply` completes no vote, so it
    never writes the `votes_cast_{today}_{user_id}` key the module docstring's
    REAL REDIS note governs.

    THE EAGER CELERY TASK AT `:289` SENDS NOTHING, for the same reason
    `TestDeleteReply` records for `:261` and through the same code.
    `task_selector` (app/shared/tasks/__init__.py:4) runs the body inline under
    `current_app.debug`; `app/shared/tasks/deletes.py:44`'s `restore_reply`
    calls the SAME `delete_object` (`:118`) the delete task calls, only with
    `is_restore=True`, and that returns at `:133` on `if community.private or
    not community.instance.online():`. `_seed_reply` defaults `private=True`,
    register entry D393(d)'s federation lever. The task opens its own session
    and re-queries the reply by id, which is safe because `:285` commits before
    `:289` runs.

    THE `path` QUESTION IS `TestDeleteReply`'S, ONE LINE OVER. `:283` is
    `delete_reply:257`'s statement with `-` changed to `+`, keyed on the same
    `tuple(reply.path[:-1])`, so the empty-tuple `ProgrammingError` probed there
    is `restore_reply`'s too: the cli-imported one-element path the module
    docstring's defect 3 describes makes a reply its author can neither delete
    NOR restore. **FIXED -- see D517.** `:296`'s guard is now
    `if reply.path and len(reply.path) > 1:`, and the importer builds the
    production shape, so neither half raises. It IS tested here now, by
    `..._restoring_a_one_element_path_reply_does_not_reach_the_empty_in_operand`
    in this class and `..._deleting_...` in `TestDeleteReply`; the row the test
    needs is seeded directly rather than through `lemmy-import`.

    `path` IS NULLABLE WITH NO DEFAULT AND `child_count` DEFAULTS TO 0 -- also
    re-derived by a second method, runtime introspection of the mapped table
    rather than reading app/models.py:2898-2899. `PostReply.__table__.c['path']`
    reports `ARRAY(Integer())`, `nullable=True`, `default=None`,
    `server_default=None`; `c['child_count']` reports `default=0`. Reading the
    source cannot see a later override of a mapped column; introspection cannot
    see a value assigned at insert time by other code. Both agree, so `:282` is
    FALSE for a `make_post_reply` reply and its true arm needs a hand-seeded
    path.
    """

    def _deleted(self, *, bot=False):
        """A seeded reply already deleted through `delete_reply`.

        `:275` filters `deleted=True`, so every test here needs one. The four
        counters are put at distinct non-zero values FIRST, by
        `_seed_distinct_reply_counters`, so they are still pairwise distinct
        after the delete has moved them -- 31/17/8/3 becomes 30/16/7/2, and for
        a bot author 31/17/8/2. That distinctness is load-bearing; the tests
        that depend on it say so individually.

        FOUR OF THE SIX TESTS BELOW get their deleted reply from here, which
        is how production reaches this function -- three originally, plus
        `..._cannot_restore_another_users_reply`, which needs the column to
        arrive holding `s.user.id` so that a mutant clearing it is visible. The
        other two do not: `..._restores_their_child_counts` marks the reply
        deleted through the ORM instead, for the reason its docstring gives, and
        `..._leaves_two_counters_permanently_low` needs the delete INSIDE the
        span it measures rather than in a fixture.
        """
        s = _seed_reply()
        if bot:
            s.user.bot = True
        _seed_distinct_reply_counters(s)
        delete_reply(s.reply.id, SRC_API, auth=bearer(s.user))
        return s

    def test_an_api_restore_returns_the_user_and_the_reply(self, db_session):
        """`:270` true -> `:271`; `:279` true -> `:280`; `:282` false -> `:285`;
        `:286` false -> `:289`; `:291` true -> `:292`.
        Arcs 270->271, 279->280, 282->285, 286->289, 291->292; statements 270,
        271, 275, 276, 277, 279, 280, 281, 282, 285, 286, 289, 291, 292.

        THE RETURN IS THE ARM DISCRIMINATOR, BY SHAPE. `:292` returns
        `(user_id, reply)` and `:294` returns None, so unpacking into two names
        is itself the witness that `:294` was not taken, and `reply.id` pins
        which row came back. Neither `deleted` nor `deleted_by` is the witness,
        for the reasons the class docstring gives.

        THIS TEST OWNS THE TWO COUNTERS `restore_reply` MOVES -- `:280`'s
        `post.reply_count` and `:281`'s `author.post_reply_count` -- and the
        last test owns the two it never moves. The split is deliberate: the two
        tests would otherwise assert the same four-tuple twice, and the campaign
        rule is that a test earns its place by closing something or by a unique
        kill, not by restating a sibling.

        `:282`'s FALSE ARM IS WITNESSED POSITIVELY, not by absence:
        `_seed_reply`'s reply has `path is None`, so mutating `:282` to `if not
        reply.path:` does not merely stop asserting something, it ERRORS on
        `None[:-1]` before any SQL is built.

        DISTINCT SEEDS ARE NECESSARY HERE, MEASURED. Rewriting `:281` as
        `reply.author.post_reply_count = reply.post.reply_count` -- an exact-copy
        assignment of the value `:280` has just written -- is invisible at the
        columns' shared default 0, because after the delete both slots hold -1
        and `:280` makes `post.reply_count` 0, which is exactly what
        `author.post_reply_count` was going to read. Against `_deleted()`'s
        30/16/7/2 the mutant writes 31 where 3 is expected. Both runs are in the
        task report. This is the same single class of mutant
        `_seed_distinct_reply_counters`'s docstring isolates, and no more:
        permuting `:280` and `:281` is still an equivalent mutant, because two
        in-place `+= 1` on independent columns are permutation-invariant.

        `deleted_by` IS ASSERTED, AS A STATEMENT WITNESS AND NOT AS AN ARM
        DISCRIMINATOR. The class docstring's ruling stands unchanged -- `:277`
        assigns the constant None, so it cannot tell the two source arms apart
        even in principle -- but that ruling is about DISCRIMINATION, and this
        docstring's statement list claims `:277` itself. It did not guard it:
        `277s/reply.deleted_by = None/reply.deleted_by = 1/` left all 27 tests
        green in the task-7 mutation pass, while the mirror
        `249s/reply.deleted_by = user_id/reply.deleted_by = None/` failed the
        web delete test. The assertion is meaningful only because `_deleted()`
        reaches this test through `delete_reply`, so the column arrives holding
        `s.user.id` and None is a value `:277` had to write.
        """
        s = self._deleted()
        before = (s.post.reply_count, s.user.post_reply_count)
        assert s.reply.deleted_by == s.user.id

        user_id, reply = restore_reply(s.reply.id, SRC_API, auth=bearer(s.user))

        assert (user_id, reply.id) == (s.user.id, s.reply.id)
        db.session.refresh(s.post)
        db.session.refresh(s.user)
        db.session.refresh(s.reply)
        assert (s.post.reply_count, s.user.post_reply_count) == (before[0] + 1, before[1] + 1)
        assert s.reply.deleted_by is None

    def test_a_web_restore_flashes_and_returns_none(self, db_session, app):
        """`:270` false -> `:273`; `:286` true -> `:287`'s flash; `:291` false
        -> `:294`. Arcs 270->273, 286->287, 291->294; statements 273, 287, 294.

        THREE ARCS IN ONE TEST, and `:286` is the only place in this mirrored
        pair of functions where the source decides whether to flash --
        `delete_reply` has no flash at all, on either arm (`grep -c flash` over
        `:241`-`:266` is 0, and `flash` and `_` are both in the AST call set for
        `restore_reply` recorded on the class).

        THE FLASH IS ASSERTED ON ITS CONTENT, NOT INFERRED FROM THE RETURN, and
        that is load-bearing. `:287` is a bare statement between `:285`'s commit
        and `:289`'s task, so DELETING IT OUTRIGHT still falls through to
        `:294`'s bare `return` and would pass a return-only assertion silently.
        The precedent is `..._a_third_source_reaches_the_flash_branches...`
        above, which makes the identical argument about `:111`/`:119`.

        `is None` IS THE ONLY DISCRIMINATOR THE RETURN CARRIES, and this
        docstring claims no second one -- see the class docstring on `:276`-`:277`.

        THAT `:273` READ `current_user` is witnessed by `:275` instead: it
        filters `user_id=user_id` and calls `.one()`, so an id from anywhere
        else raises `NoResultFound` rather than restoring.

        `auth=None` is passed explicitly because `restore_reply`'s signature at
        `:269` is `(reply_id, src, auth)` with no default; the web arm never
        reads it. A counter is asserted as well, so a mutant that gutted the
        body but kept the flash and the bare `return` cannot pass on the None
        and the message alone.
        """
        s = self._deleted()
        before = s.user.post_reply_count

        with web_ctx(app, s.user):
            assert restore_reply(s.reply.id, SRC_WEB, auth=None) is None
            flashed = get_flashed_messages()

        assert len(flashed) == 1
        assert 'restored' in flashed[0]
        db.session.refresh(s.reply)
        db.session.refresh(s.user)
        assert s.reply.deleted is False
        assert s.user.post_reply_count == before + 1

    def test_a_bot_authors_reply_skips_the_post_counter_on_restore(self, db_session):
        """`:279` false -> `:281`. Arc 279->281.

        `:279` is `if not reply.author.bot:`, so a bot author skips `:280` --
        but `:281`, the AUTHOR's own counter, is a top-level statement of the
        function body and increments anyway. THAT ASYMMETRY IS THE WITNESS:
        asserting only that `post.reply_count` held would not distinguish this
        run from the function never having been called, which is false-witness
        mechanism 3. `:281` is the same-mechanism positive control and it moves
        through the same commit as the counter that did not.

        THE BASELINE IS TAKEN AFTER THE DELETE, NOT BEFORE IT, and that matters.
        Measured from before the delete, this bot's `author.post_reply_count`
        goes 3 -> 2 -> 3 and lands back where it started, so `== before` would
        be exactly what a no-op function produced -- mechanism 3 reintroduced by
        the arithmetic. Read from immediately after the delete, the restore's
        own effect is a clean `+1` against a `+0`.

        ONLY THE ONE GUARDED COUNTER IS READ. `delete_reply`'s bot test reads
        three because `:252`-`:254` are three guarded statements; `restore_reply`
        has exactly one. `post.reply_count_cross_posted` and
        `community.post_reply_count` are untouched by this function on BOTH legs
        of `:279`, so asserting they held would witness nothing about the guard.
        THIS TEST THEREFORE DOES NOT PIN THE DELETE/RESTORE ASYMMETRY -- the
        last test does, and it reads those two columns precisely because this
        one cannot.

        DISTINCT SEEDS ARE NECESSARY HERE TOO, and for the same mutant as the
        API test: with the columns at 0 the post-delete pair is (0, -1), `:281`
        rewritten as `= reply.post.reply_count` writes 0 where 0 is expected and
        SURVIVES; against `_deleted(bot=True)`'s (31, 2) it writes 31 where 3 is
        expected and fails.

        `User.bot` (app/models.py:1016, in `class User` which opens at
        app/models.py:973) defaults False, so every other test here takes the
        true arm. `authorise_api_user` (app/utils.py:3628) rejects on `ap_id`,
        `verified`, `banned` and `deleted` and says nothing about `bot`, so the
        bearer token still authorises.
        """
        s = self._deleted(bot=True)
        before = (s.post.reply_count, s.user.post_reply_count)

        restore_reply(s.reply.id, SRC_API, auth=bearer(s.user))

        db.session.refresh(s.post)
        db.session.refresh(s.user)
        assert (s.post.reply_count, s.user.post_reply_count) == (before[0], before[1] + 1)

    def test_a_reply_with_ancestors_restores_their_child_counts(self, db_session):
        """`:282` true -> `:283`. Arc 282->283; statements 282, 283.

        THIS TEST DOES NOT CALL `delete_reply`, AND WHAT THAT BUYS IS
        LOCALIZATION, NOT DETECTION. The claim is deliberately narrow, because
        the wider one is false and was measured to be false.

        `:283` and `delete_reply:257` are the same statement with one character
        changed, so the defect worth designing against is the MIRRORED PAIR
        losing its `where` clause together. A delete-then-restore construction
        has a real trap against it: the round trip decrements every row and then
        increments every row, so a bystander seeded at N reads N - 1 + 1 == N
        and a control asserted only AFTER the cycle is laundered by it. That
        trap is ESCAPABLE, though, and saying otherwise would be false. Measured
        under the pair mutant: a variant asserting the bystander BETWEEN the two
        calls FAILS, `assert 8 == 9`, while the otherwise identical variant
        asserting only the PARENT mid-cycle PASSES. So the round-trip shape can
        carry a working control; it just has to be the bystander, inside the
        cycle. An earlier version of this docstring said the shape "CANNOT CARRY
        A WORKING NEGATIVE CONTROL", which is wrong -- the one placement that
        does not work is the one that had been tried.

        AND AT SUITE SCOPE THE PAIR MUTANT DIES ANYWAY, on
        `TestDeleteReply`'s ancestor test, which carries its own bystander on
        the delete half. So this rewrite adds no detection the file did not
        already have. What it adds is that `:283` is THE ONLY STATEMENT IN THIS
        TEST THAT TOUCHES `child_count`: a failure here names restore's own
        `UPDATE` instead of the pair, and the test depends neither on
        `delete_reply:257` being correct nor on a sibling test continuing to
        exist. Marking the reply `deleted` through the ORM is all `:275`'s
        filter requires, so the isolation is free.

        MEASURED, NOT ARGUED. ` where id in :parents` was deleted from BOTH
        `:257` and `:283` in a scratch mutant -- `app/` restored afterwards and
        the restore confirmed with `git diff --quiet -- app/` and `wc -l`
        reporting 577. This test FAILED, `assert 10 == 9` on the bystander;
        `TestDeleteReply`'s ancestor test also failed, on the delete half. A
        `:283`-ONLY mutant is the weaker case and every shape discussed here
        catches it. The runs are in the task report.

        THE PATH IS SEEDED IN THE SHAPE PRODUCTION BUILDS, `[0, parent.id,
        reply.id]` -- app/models.py:3054-3055 for the nested case and `:3059`
        for the root, in `class PostReply` which opens at app/models.py:2887.
        `path[:-1]` is then `(0, parent.id)`: multi-element, so the empty-tuple
        syntax error the class docstring records cannot arise, and id 0 matches
        no row so only `parent` is hit.

        `parent` and `bystander` are refreshed rather than read from the session
        because the raw `UPDATE` bypasses the identity map. Their seeded counts
        are different from each other and from both 0 and 1, so a mutant
        assigning a literal instead of incrementing is visible as well.

        THE REPLY'S OWN `child_count` IS ASSERTED TOO, for the reason
        `TestDeleteReply`'s ancestor test now records against `:258`: the
        bystander is not in `path` at all, so it cannot witness the `[:-1]`.
        `284s/tuple(reply.path\\[:-1\\])/tuple(reply.path)/` left all 27 tests
        green in the task-7 pass, and 6 is distinct from 4, 9, 0 and 1.
        """
        s = _seed_reply()
        parent = make_post_reply(s.post, s.user, body='parent')
        bystander = make_post_reply(s.post, s.user, body='bystander')
        parent.child_count = 4
        bystander.child_count = 9
        s.reply.child_count = 6
        s.reply.path = [0, parent.id, s.reply.id]
        s.reply.deleted = True
        db.session.commit()

        restore_reply(s.reply.id, SRC_API, auth=bearer(s.user))

        db.session.refresh(parent)
        db.session.refresh(bystander)
        db.session.refresh(s.reply)
        assert parent.child_count == 5
        assert bystander.child_count == 9
        assert s.reply.child_count == 6

    def test_a_delete_restore_cycle_leaves_two_counters_permanently_low(self, db_session):
        """PINS A REGISTERED DEFECT, AND CLOSES NO NEW STATEMENT OR ARC. Its
        covered set is not merely contained in the API test's above, it is
        IDENTICAL to it -- measured per-test with `--cov-branch`, statements
        `[270, 271, 275, 276, 277, 279, 280, 281, 282, 285, 286, 289, 291, 292]`
        and arcs `[(270,271), (279,280), (282,285), (286,289), (291,292)]` for
        both, with `set(cycle) < set(api)` returning False. So it earns its place
        by a UNIQUE KILL and by nothing else.

        It asserts the CURRENT behaviour, which is WRONG, so that repairing
        `restore_reply` has to change a test rather than silently alter a number
        nobody was watching. `delete_reply:252`-`:254` decrement three counters;
        `restore_reply:280` increments one. After a full cycle `post.reply_count`
        is level and `post.reply_count_cross_posted` and
        `community.post_reply_count` are each one LOW.

        THE UNIQUE KILL IS A FAULT-DIRECTION MUTANT, MEASURED. `:280` was
        rewritten in line-scoped scratch mutants so that restore makes a counter
        WORSE instead of leaving it alone -- `reply.post.reply_count += 1;
        reply.post.reply_count_cross_posted -= 1`, and separately the same with
        `reply.community.post_reply_count -= 1` -- with `app/` restored
        afterwards and each restore confirmed by `git diff --quiet -- app/` and
        `wc -l` reporting 577. Each run: THIS TEST WAS THE ONLY ONE IN THE FILE
        THAT FAILED, `assert 15 == (17 - 1)` and `assert 6 == (8 - 1)`
        respectively, 26 others passing. No other test in this file reads either
        column on a path that runs `restore_reply`; `TestDeleteReply`'s four read
        both but never restore.

        A SEMANTICALLY-BETTER MUTANT IS NOT A UNIQUE KILL, and an earlier version
        of this docstring offered one as if it were. Rewriting `:280` as the
        three-counter increment a FIX would make also fails this test alone --
        that run stands, and it is the evidence for the fix-edit obligation in
        the paragraph above. But a mutant better than production measures
        change-detection rather than defect-detection, so it cannot discharge the
        campaign's unique-kill rule; admitting it would empty the rule. The
        fault-direction mutants above are what discharge it. Both runs are in the
        task report.

        `post.reply_count` RETURNING LEVEL IS THE SAME-MECHANISM POSITIVE
        CONTROL. It proves the cycle ran and that this harness can observe a
        counter coming back to its starting value, so the other two being low is
        a real asymmetry and not a fixture artefact or a refresh that read a
        stale row.

        `author.post_reply_count` IS DELIBERATELY NOT READ. `delete_reply:255`
        and `restore_reply:281` sit outside their respective bot guards and
        mirror each other exactly, so it returns level like `post.reply_count`
        and would be a second control rather than a second witness -- and
        including it would blur which columns the divergence actually covers.
        The class docstring derives the boundary from the AST.

        The three columns are seeded to distinct non-zero values so that a
        failure message names which one moved; they stay positive throughout,
        which keeps that message readable.
        """
        s = _seed_reply()
        before = _seed_distinct_reply_counters(s)

        delete_reply(s.reply.id, SRC_API, auth=bearer(s.user))
        restore_reply(s.reply.id, SRC_API, auth=bearer(s.user))

        db.session.refresh(s.post)
        db.session.refresh(s.community)
        assert s.post.reply_count == before[0]
        assert s.post.reply_count_cross_posted == before[1] - 1
        assert s.community.post_reply_count == before[2] - 1

    def test_a_non_author_cannot_restore_another_users_reply(self, db_session):
        """CLOSES NO STATEMENT AND NO ARC. It earns its place by a unique kill
        against `275s/, user_id=user_id,/, /`, which left all 27 tests green in
        the task-7 pass -- the mirror of the mutant task 7's brief made
        mandatory against `delete_reply:247`, and it survived for the mirror
        reason: no test in this class ever calls `restore_reply` as anybody but
        the author, so the `user_id` conjunct was never executed against a
        mismatch.

        THE CLASS DOCSTRING'S ROUTING OF THIS MISS-PATH TO THE REGISTER IS
        OVERTURNED HERE, exactly as `TestDeleteReply`'s is by its own
        non-author test, and for the same reason: "the `.one()` would raise"
        describes the unmutated function and says nothing about whether any
        test would notice if it stopped raising.

        THE WITNESS IS `deleted` STILL TRUE, not the raise. Under the mutant the
        interloper's call finds the author's deleted reply, `:276`-`:277` clear
        the flags and `:280`-`:281` move two counters, so `deleted is True` is
        false and the test fails on state rather than on a missing exception.
        `_deleted()` is used so the reply arrives deleted through the production
        path, which is also what makes `deleted_by` non-None going in.
        """
        s = self._deleted()
        interloper = make_user(s.instance, 'interloper', local=True)
        db.session.commit()

        with pytest.raises(Exception):
            restore_reply(s.reply.id, SRC_API, auth=bearer(interloper))

        db.session.refresh(s.reply)
        assert s.reply.deleted is True
        assert s.reply.deleted_by == s.user.id

    def test_restoring_a_one_element_path_reply_does_not_reach_the_empty_in_operand(self, db_session):
        """`restore_reply`'s half of the cli-imported one-element path.

        `:282` is `delete_reply:256`'s guard with `-` changed to `+` in the
        statement below it, keyed on the same `tuple(reply.path[:-1])`, so the
        empty-tuple `ProgrammingError` `TestDeleteReply` pins is this
        function's too. Both halves are pinned because a fix applied to one
        guard and not the other leaves the reply deletable but not restorable,
        which is a worse state than the symmetric failure it replaces.

        The reply is marked deleted THROUGH THE ORM rather than through
        `delete_reply`, because `delete_reply` is the other half of this same
        defect: routing the fixture through it would make this test depend on
        the fix it is meant to witness, and it would pass vacuously if the
        guard were fixed in `delete_reply` alone.

        THE WITNESS IS THE RESTORE HAPPENING, not the absence of a raise --
        `deleted` false and `deleted_by` None -- plus `parent.child_count`
        unmoved at 5, which is correct for a path that does not identify its
        ancestors.
        """
        s = _seed_reply()
        parent = make_post_reply(s.post, s.user)
        db.session.commit()
        parent.child_count = 5
        s.reply.path = [parent.id]
        s.reply.deleted = True
        s.reply.deleted_by = s.user.id
        db.session.commit()

        restore_reply(s.reply.id, SRC_API, auth=bearer(s.user))

        db.session.refresh(s.reply)
        db.session.refresh(parent)
        assert s.reply.deleted is False
        assert s.reply.deleted_by is None
        assert parent.child_count == 5
