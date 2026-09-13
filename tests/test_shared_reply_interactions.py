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
CLEARS THE KEY IN A `finally`. Five tests now do -- the three in
`TestVoteForReplySourceAndPermission` that reach `:36` and the two web tests in
`TestVoteForReplyGuardsAndReturns` that do. The four that refuse earlier
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
    enumerates all five writers of the column. `TestDeleteReply` seeds the
    three-element shape `app/models.py` builds and does NOT test the empty
    tuple, because the cli importer that can produce one cannot be simulated
    from a factory reply; its class docstring carries the full probe.
    `child_count` (app/models.py:2899) is likewise unset by the factory, but
    has a column default of 0 rather than None.

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
    now EXECUTED for `delete_reply` rather than merely read: `TestDeleteReply`'s
    four tests call `make_site()` nowhere and pass. The module's one other
    `Site` touch is `Site.admins()` at `:365`, inside `report_reply`, which is
    a later sub-project's.
    `subscribe_reply` additionally has two statements no production source
    value can reach -- see `TestSubscribeReply`'s docstring for what `:98` does
    to the web arm.

FOUR DEFECTS ARE RECORDED HERE AND DELIBERATELY NOT FIXED. TWO OF THEM ARE
PINNED BY TESTS IN THIS FILE; THE OTHER TWO ARE NOT, AND CANNOT BE FROM A
FACTORY REPLY. Taking the unpinned pair first, because a later reader of the
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
     `:669` skips it -- both are falsy and merely skip the update. Only the
     mapped-parent first-level case raises.
  3. app/api/alpha/views.py:686, written by `calculate_path` (`:669`). Depth 0
     gives `[0, reply.id]`, depth 1 `[0, parent_id, reply.id]`, depth > 1 a
     longer walk, committed at `:687`. ALWAYS >= 2. Cleared, no finding -- but
     see the note on self-healing below, which is about WHEN it runs, not what
     it writes.
  4. app/post/util.py:79, inside `create_real_reply` (`:53`). Its own comment
     at `:55` says "Create a PostReply instance (not persisted to DB)", and
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
NONE OF THE FOUR IS IN THE CAMPAIGN REGISTER
YET -- all are slated for it at this round's end. Until then the argument for
leaving the two pinned ones unfixed lives in
`docs/superpowers/specs/2026-09-12-coverage-reply-ac-40-design.md`, under the
headings that name them. The tests that pin today's voting behaviour are
`TestVoteForReplySourceAndPermission` below; the delete/restore asymmetry is
still pinned by a later task of this round. NO TEST IN THIS FILE MAKES THE
VOTING ASYMMETRY EXECUTABLE, and that is a decision rather than a gap: the
construction that would -- a web downvote against a `Site` with
`enable_downvotes` False, landing where the API arm refuses -- also makes
`post/_comment_voting_buttons.html` line 10 false, which deletes the
`voted_down` markup that `TestVoteForReplyGuardsAndReturns`'s downvote test
needs as its witness for `:49`. The two cannot be had in one test, and `:49`'s
witness won.
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
    """
    s = _seed_reply()
    make_post_reply_bookmark(s.user, s.reply)

    with web_ctx(app, s.user):
        assert bookmark_reply(s.reply.id, SRC_WEB) is None

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
    """
    s = _seed_reply()
    assert PostReplyBookmark.query.count() == 0

    with web_ctx(app, s.user):
        assert remove_bookmark_reply(s.reply.id, SRC_WEB) is None

    assert PostReplyBookmark.query.count() == 0


class TestSubscribeReply:
    """`:93-133` -- subscribe and unsubscribe, both source arms.

    `:94` JOINS `Post` and filters `deleted=False` on BOTH rows, which
    `subscribe_post` did not, so every test here needs a live parent post and
    `.one()` raises rather than returning None if either is deleted.
    `_seed_reply` supplies exactly that: both constructors set `deleted=False`
    explicitly rather than leaning on the column default -- tests/factories.py
    :344 for the post, :469 for the reply -- so the join resolves and `.one()`
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
        """
        s = _seed_reply()
        subscribe_reply(s.reply.id, True, SRC_API, auth=bearer(s.user))

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

    def test_the_web_arm_loads_the_reply_and_reads_current_user(self, db_session, app):
        """`:19` false -> `:27`, `:28`. Arc 19->27; statements 27, 28.

        The web arm uses `get_or_404` rather than `.one()` and takes its user
        from `current_user`, so this needs `web_ctx`. It reaches `:36` and
        completes a real vote -- hence the `finally`.

        `make_site()` is required: this is the only test in this class that
        reaches `:51`'s `render_template`, and the module docstring's chain
        ends in `AttributeError: 'NoneType' object has no attribute
        'default_theme'` without the row.

        THE RENDERED BODY IS THE WITNESS THAT `:28` TOOK `current_user`, not the
        bare fact that something was returned. `_comment_voting_buttons.html`
        opens with `{% if current_user.is_authenticated and
        current_user.verified %}`, so the `voted_up`/`fe-arrow-up-circle`
        markup exists only on the authenticated branch -- if `:28` had bound
        anything but the logged-in user the template would fall to its `else`
        and emit the `redirect_login` markup instead. `voted_up` additionally
        requires `:47` to have put this reply id into `recently_upvoted_replies`,
        which only happens after `:36` actually voted.

        NO PERMISSION GATE IS CROSSED HERE, because there is none on this arm.
        That is the asymmetry the class docstring registers, and this test is
        the evidence for it. A test showing a BOT voting successfully through
        this path would make the asymmetry executable rather than documentary;
        that is a scope decision and it was referred to the controller rather
        than taken here.
        """
        make_site()
        s = _seed_reply()
        try:
            with web_ctx(app, s.user):
                result = vote_for_reply(s.reply.id, 'upvote', True, None, SRC_WEB)
                body = result.get_data(as_text=True)

            db.session.refresh(s.reply)
            assert s.reply.up_votes == 1
            assert PostReplyVote.query.filter_by(
                post_reply_id=s.reply.id, user_id=s.user.id).count() == 1
            assert 'redirect_login' not in body
            assert 'voted_up' in body
            assert 'fe-arrow-up-circle' in body
        finally:
            _clear_votes_cast(s.user.id)


class TestVoteForReplyGuardsAndReturns:
    """`:30-34` and `:44-51` -- the ban and quota guards, and the web arm's
    three-way recently-voted fork.

    FOUR TESTS, NOT THE SIX THE BRIEF DRAFTED, because
    `TestVoteForReplySourceAndPermission` above had already closed two of them
    and this was MEASURED before anything was written. Against the class above
    alone, suite-scoped over this file plus
    tests/test_shared_post_interactions.py with `--cov=app.shared.reply
    --cov-branch`, statements 30, 33, 36, 38, 41, 42, 44, 45, 46, 47 and 51 are
    already not-missing and so are arcs 30->33, 33->36, 41->42, 41->44 and
    46->47. What was still missing was statements 31, 34, 48 and 49 and arcs
    30->31, 33->34, 46->48, 48->49 and 48->51 -- exactly what the four tests
    here take. The brief's `..._a_completed_api_vote_returns_the_user_id` and
    `..._a_web_upvote_renders_with_the_reply_marked_recently_upvoted` would have
    duplicated `..._passes_both_gates` and
    `..._the_web_arm_loads_the_reply_and_reads_current_user` call for call while
    asserting strictly less (`result is not None` against that test's markup
    discrimination), so they are deliberately absent rather than overlooked.

    WHY `:46`/`:48` NEED THREE INPUTS AND NOT TWO. `:46` is an `if` and `:48`
    its `elif`, so `:48` is not evaluated at all when `:46` is true, and BOTH
    lines carry `undo is None` as their second conjunct:

      - upvote, `undo` None    -> `:46` true  -> `:47`, then `:51`
      - downvote, `undo` None  -> `46->48`, `:48` true  -> `:49`, then `:51`
      - either direction, `undo` NOT None -> `46->48`, `:48` false -> `:51`
        with both lists left empty

    The upvote case is the class above's web test. The other two are the third
    and fourth tests here.

    `undo` IS NON-None ONLY WHEN A VOTE IS REMOVED, and that was read at source
    rather than assumed: `PostReply.vote` (app/models.py:3311, in `class
    PostReply` which opens at app/models.py:2887) initialises `undo = None` at
    `:3322` and assigns it in exactly two places -- `:3343`'s `undo = 'Like'`,
    when an existing upvote is voted up again and deleted, and `:3357`'s
    `undo = 'Dislike'`, when an existing downvote is voted down again. A
    direction REVERSAL (`:3345`, `:3359`) edits the existing row and leaves
    `undo` None, so reversing is NOT an input that reaches `48->51`; repeating
    the same direction is.

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
      - BOTH web tests need one FOR BOTH REASONS AT ONCE. The first is the
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

        THE THIRD INPUT, and the one neither direction alone can produce. Both
        `:46` and `:48` test `undo is None` as their second conjunct, so a vote
        that UNDOES an existing one fails both and falls to `:51` with both
        lists still at the `[]` `:44`/`:45` gave them.

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

        THIS IS THE ONLY TEST IN THE FILE THAT WITNESSES `undo is None` AT ALL,
        and that was measured rather than claimed: deleting `and undo is None`
        from `:46` in a line-scoped scratch mutant (`app/` restored afterwards
        and the restore confirmed with `git diff --quiet -- app/`) failed this
        test alone -- the other seventeen passed, because every one of them has
        `undo` None anyway and cannot tell the conjunct from its absence.

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


class TestDeleteReply:
    """`delete_reply` (app/shared/reply.py:241-266) -- the author's own soft delete.

    `:247` filters on `id`, `user_id` AND `deleted=False` and calls `.one()`, so
    only the author can delete, only once, and a miss raises rather than
    returning None. That is why no test here asserts "the wrong user got
    nothing": the `.one()` would raise, and the raise is a later task's target.

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
        """
        s = _seed_reply()
        parent = make_post_reply(s.post, s.user, body='parent')
        bystander = make_post_reply(s.post, s.user, body='bystander')
        parent.child_count = 1
        bystander.child_count = 1
        s.reply.path = [0, parent.id, s.reply.id]
        db.session.commit()

        delete_reply(s.reply.id, SRC_API, auth=bearer(s.user))

        db.session.refresh(parent)
        db.session.refresh(bystander)
        assert parent.child_count == 0
        assert bystander.child_count == 1


class TestRestoreReply:
    """`restore_reply` (app/shared/reply.py:269-294) -- the author's own undelete.

    THE EXTENT IS THE AST'S, NOT THE BRIEF'S. The brief named `:269-296`;
    `ast.parse` puts the `def` at 269 and its last statement's `end_lineno` at
    294, and `:295-296` are the blank lines before `report_reply` at `:297`.

    `:275` filters `id`, `user_id` AND `deleted=True` and calls `.one()`, so a
    reply must already be deleted and only its author can restore it. A miss
    raises rather than returning None, which is a later task's target.

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
    made through a string name -- and both say the row is unnecessary. The five
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
    NOR restore. Registered, not fixed, with `flask populate_post_reply_for_api`
    as the existing remedy; not tested here, because reaching it needs a row
    only `lemmy-import` builds.

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

        THREE OF THE FIVE TESTS BELOW get their deleted reply from here, which
        is how production reaches this function. The other two do not:
        `..._restores_their_child_counts` marks the reply deleted through the
        ORM instead, for the reason its docstring gives, and
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
        """
        s = self._deleted()
        before = (s.post.reply_count, s.user.post_reply_count)

        user_id, reply = restore_reply(s.reply.id, SRC_API, auth=bearer(s.user))

        assert (user_id, reply.id) == (s.user.id, s.reply.id)
        db.session.refresh(s.post)
        db.session.refresh(s.user)
        assert (s.post.reply_count, s.user.post_reply_count) == (before[0] + 1, before[1] + 1)

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

        THIS TEST DOES NOT CALL `delete_reply`, AND THAT IS THE POINT. The
        obvious construction -- delete, restore, then assert the ancestor's
        `child_count` came back -- CANNOT CARRY A WORKING NEGATIVE CONTROL
        AGAINST THE ONE MUTANT SHAPE THAT MATTERS MOST HERE. `:283` and
        `delete_reply:257` are the same statement with one character changed, so
        the realistic defect is the MIRRORED PAIR losing its `where` clause
        together, not one half of it. Under that mutant a round trip increments
        every row in `post_reply` after having decremented every row, and a
        bystander seeded at N reads N - 1 + 1 == N at the end: THE ROUND TRIP
        LAUNDERS EXACTLY THE DEFECT THE CONTROL EXISTS TO CATCH. Marking the
        reply `deleted` directly through the ORM is all `:275`'s filter
        requires, and it leaves `:283` as the only statement in the whole test
        that touches `child_count`, so nothing can launder anything.

        MEASURED, NOT ARGUED. ` where id in :parents` was deleted from BOTH
        `:257` and `:283` in a scratch mutant -- `app/` restored afterwards and
        the restore confirmed with `git diff --quiet -- app/` and `wc -l`. This
        test FAILED, `assert 10 == 9` on the bystander. A delete-then-restore
        version run alongside it PASSED, and that version was STRENGTHENED
        rather than copied: it carried a bystander of its own AND an
        intermediate assertion on the parent between the two calls, so it was a
        better test than the one this replaces and the round trip defeated it
        anyway. `TestDeleteReply`'s ancestor test also failed, on the delete
        half. A `:283`-ONLY mutant is the weaker case and both shapes catch it;
        it is the pair mutant that separates them. The runs are in the task
        report.

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
        """
        s = _seed_reply()
        parent = make_post_reply(s.post, s.user, body='parent')
        bystander = make_post_reply(s.post, s.user, body='bystander')
        parent.child_count = 4
        bystander.child_count = 9
        s.reply.path = [0, parent.id, s.reply.id]
        s.reply.deleted = True
        db.session.commit()

        restore_reply(s.reply.id, SRC_API, auth=bearer(s.user))

        db.session.refresh(parent)
        db.session.refresh(bystander)
        assert parent.child_count == 5
        assert bystander.child_count == 9

    def test_a_delete_restore_cycle_leaves_two_counters_permanently_low(self, db_session):
        """PINS A REGISTERED DEFECT, AND CLOSES NO NEW STATEMENT OR ARC. Every
        line it executes is already closed by the API test above; it earns its
        place by a UNIQUE KILL, which is the campaign's standing rule for such a
        test, and the kill is the interesting one: A FUTURE FIX.

        It asserts the CURRENT behaviour, which is WRONG, so that repairing
        `restore_reply` has to change a test rather than silently alter a number
        nobody was watching. `delete_reply:252`-`:254` decrement three counters;
        `restore_reply:280` increments one. After a full cycle `post.reply_count`
        is level and `post.reply_count_cross_posted` and
        `community.post_reply_count` are each one LOW.

        THE UNIQUE KILL, MEASURED. `:280` was rewritten in a line-scoped scratch
        mutant as the three-counter increment a fix would make -- `reply.post
        .reply_count += 1; reply.post.reply_count_cross_posted += 1;
        reply.community.post_reply_count += 1` -- with `app/` restored afterwards
        and the restore confirmed by `git diff --quiet -- app/`. THIS TEST WAS
        THE ONLY ONE IN THE FILE THAT FAILED. The API test above passes against
        it, because `post.reply_count` and `author.post_reply_count` still move
        by exactly +1; the bot test passes because the guard skips the whole
        statement; `TestDeleteReply`'s four pass because they never restore. The
        run is in the task report.

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
