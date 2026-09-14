"""Group E and F of app/shared/reply.py -- the moderator verbs and the
reply-only verbs.

WHAT THIS FILE COVERS. Every line number here was re-derived with numbered
output, never copied from the plan. THE WHOLE TABLE WAS RE-DERIVED AGAIN AT
TASK 7, because Task 7's production fix inserted two lines into
`lock_post_reply` and two into `set_collapse_post_reply` and so moved every
line at or after `:524`:

    mod_remove_reply         :416-451   21 statements / 12 arcs
    mod_restore_reply        :454-489   21 / 12
    lock_post_reply          :492-528   26 / 16
    set_collapse_post_reply  :531-555   17 / 14
    choose_answer            :558-586   15 /  4
    unchoose_answer          :589-602    9 /  4

The counts EXCLUDE each `def` line, which is the convention the table has
carried since Task 1; they are `coverage.parser.PythonParser` statement counts
and branch-arc counts for the range beside them, re-run in the container at
Task 7's commit.

EVERY `:NNN` IN THIS FILE POINTS INTO app/shared/reply.py AS IT IS NUMBERED
NOW, INCLUDING THE NUMBERS INSIDE RETRACTIONS. Sub-project 42 task 7 inserted
six statements into this module -- two into `restore_reply`'s bot guard, two
into `mod_remove_reply`'s and two into `mod_restore_reply`'s -- which moved
every line of the file below `:294` down by two, four or six depending on which
of the three insertions it sits under. Rather than leave a file of citations
that no longer resolve, all of them were re-derived to the post-fix numbering
in that task, and so were the numbers quoted inside sentences that describe
what an EARLIER version of a docstring said. Such a number therefore names the
line a past claim pointed at, as that line is numbered today, and not the
digits the past claim actually contained. That normalisation is deliberate: a
citation's only job here is to resolve against the tree in front of the reader.

TWO ROWS CHANGED AT TASK 7 (OF SUB-PROJECT 41) AND THE RETRACTED FIGURES ARE
NAMED HERE RATHER THAN OVERWRITTEN SILENTLY. The `lock_post_reply` row read
`24 / 14` against the start now written `:492`, and `set_collapse_post_reply`
read `15 / 12` against the start now written `:529`; both were correct for the code
as it stood through Task 6 and both are stale now, because Task 7 added
`elif src == SRC_API: raise Exception('Does not have permission')` to each --
two statements and two arcs apiece. `choose_answer` and `unchoose_answer` did
not change, but Task 7's two insertions sit above them, so their starts moved
from `:554` and `:585` to `:558` and `:589`.

THE TABLE ALSO USED TO GIVE NO END AT ALL, only a start plus a count, and for
`unchoose_answer` that implied a span ending near `:593` -- short of the
function's true last line. Task 6's review caught it and deferred it here.
Ends are now given explicitly for all six rows. `unchoose_answer` really runs
to `:602`: `:601` is `if src == SRC_API:` and `:602` is
`return user.id, post_reply`, the last line of the file.

All six were at ZERO coverage when this file was created -- missing equal to
total for every one, against the pre-Task-7 figures named just above (`24 / 14`
for `lock_post_reply`, `15 / 12` for `set_collapse_post_reply`, and the four
unchanged rows as they stand). There was no partial
coverage to build on and no existing test to read for the conventions, which is
why the harness below is borrowed wholesale from two files rather than derived
here.

WHERE THE HARNESS COMES FROM. `tests/test_shared_post_moderation.py` covers
`lock_post`, `mod_remove_post` and `mod_restore_post` -- the direct twins -- and
`tests/test_shared_reply_interactions.py` covers Groups A and C of this module.
`seed_moderator`, `make_site_admin`, `make_instance_admin` and
`recording_task_selector` are transcribed from the former; `_seed_reply`'s
shape and the `make_site()` rule come from the latter.

THE `make_site()` RULE, in the form sub-project 40 corrected it to: a `Site`
row is needed for `render_template` OR for `can_downvote`, which reads
`Site.query.get(1)` at app/utils.py:2443 and dereferences it at `:2445` before
any source fork. Neither group here calls `can_downvote`, so a `Site` row is
needed only where a template renders. NO FUNCTION IN GROUPS E OR F RENDERS ONE
-- `render_template` appears in this module only at `:65` and `:145`, both in
Group A -- SO NO TEST IN THIS FILE CALLS `make_site()` AND THE NAME IS NOT
IMPORTED. One test did, justified by a rule this docstring does not contain
("a template or A PERMISSION READ"), while its sibling in the next class said
the opposite for the same code shape; the call and the justification were both
dropped in the final fix wave. If a later test here seems to need a `Site` row,
the question to answer first is which of the two real reasons it hits.

THREE PROBES WERE RUN BEFORE ANY TEST WAS WRITTEN (task-1-report.md carries
the raw output). Two held; one falsified the plan's own prediction:

  - Probe A predicted that a factory user's `language_id` might point at a
    nonexistent `Language` row and make `choose_answer:566`'s
    `get_recipient_language` raise on `lang.code`. IT DOES NOT RAISE, and not
    for the reason guarded against: `make_user` (tests/factories.py:41) never
    sets `language_id` at all, so it is `None`, not a dangling foreign key.
    `get_recipient_language` (app/utils.py:4789) tests `if recipient.
    language_id:` first, which is falsy, then `elif recipient.
    interface_language:` (also unset, also falsy), and falls to the `else:
    lang_to_use = 'en'` arm at :4799 -- the Language table is never queried.
    No `Language` row needs to be seeded for any test in this file.

  - Probe B confirmed the `@>` cascade at `lock_post_reply:509` works on a
    manually-seeded path shaped like production's (`app/models.py:3058-3065`):
    a child reply whose `path` is `[0, parent.id, child.id]` has
    `replies_enabled` flip to `False` when the parent is locked. A test
    exercising that line must seed `path` itself -- `make_post_reply` does not.

    THIS CITATION READ `:3053-3060` UNTIL TASK 7 AND WAS WRONG AT BOTH ENDS.
    `:3053` is the bare `session.commit()` inside the `try`, which has nothing
    to do with `path`; and `:3060` stops one line before the `else:
    reply.path = [0, reply.id]` at `:3063-3064` that produces the very
    two-element shape the citation is invoked for. The construction is
    `:3058-3065`: the `if in_reply_to and in_reply_to.path:` arm that appends
    to the parent's path, the `else` arm that opens a root path, and
    `:3065`'s `reply.root_id = reply.path[1]`. Corrected in all three places
    it appeared in this file, and in the two in
    `tests/test_shared_reply_interactions.py` that read `:3053-3059`.

  - Probe C confirmed which seeded user lands on id 1: in
    `_seed_moderated_reply`, `author` is minted before `actor`
    (tests/conftest.py resets sequences every test), so `author.id == 1` and
    `author.is_admin()` is `True` purely from the `self.id == 1` short-circuit
    at app/models.py:1260 -- NOT `actor`. `add_to_modlog:3570` would type any
    action performed by `author` as `'admin'` for that reason alone, with no
    Admin role granted. Every test in this file that cares about `ModLog.type`
    must act through `s.actor` (id 2), not `s.author`, to avoid tripping this
    short-circuit by accident.

THE TASK 8 MUTATION PASS, AND THE THIRTEEN MUTANTS STILL ALIVE. The space is
159 mutants, applied one at a time to `app/shared/reply.py` and judged by this
file alone -- no other test file in the tree calls any of these six functions,
so this file is the whole jury. Six other files under `tests/` match one of the
names, and none of them is a second juror: `test_shared_tasks_locks.py` and
`test_shared_tasks_send_answer.py` call the same-named functions in
`app.shared.tasks`, and the four `test_inbox_dispatch_*.py` files carry the
names as string literals and, in two of them, in test FUNCTION names --
`test_inbox_dispatch_undo_content.py:418`, `:451`, `:487` and
`test_inbox_dispatch_votes.py:256`. Neither form is a call, but neither is a
string literal either, so "only as string literals" was the wrong reason and is
retracted here. THE CHECK THAT SETTLES IT IS A GREP FOR THE IMPORT, NOT FOR THE
NAME, and it settles it outright: `/usr/bin/grep -rn 'app[.]shared[.]reply'
tests/ --include=*.py` returns this file and
`tests/test_shared_reply_interactions.py` and nothing else, so not one of the
six can reach these functions however its own tests are spelled.
THE 159 BREAKS DOWN AS 115 statement lines, 20 continuation
lines inside multi-line statements, one neutralisation on each of the four
permission guards -- FOUR mutants, not eight; the two mandatory ones ran in
opposite directions across the set (`:507` forced true, `:461` forced false),
which is what "both directions" was ever meant to mean, and an earlier wording
of this sentence said "both directions on each of the four" and so implied a
bucket of 8 that the 159 total does not admit -- and 20 operand-drops and
operator-swaps over the six `BoolOp` nodes. Both line lists were derived with `ast.walk` over each
`FunctionDef`: the statement list counts the `def` line and so runs one longer
per function than the table above (115, not 109), and the continuation list is
every non-first physical line of a NON-COMPOUND statement, which is the only
definition that does not sweep in blank lines, comments and bare `else:`.
146 died, 13 live. Those 13 are listed below with reproduction recipes so that
they are findable from the repository rather than only from a planning
directory that does not outlive the round; each is a single-line literal
substitution, so apply it, run this file, and observe green.

THE FIRST PASS UNDERSTATED THE SPACE AND THIS PARAGRAPH SAYS HOW. It reported
151 mutants over 12 continuation lines. BOTH OF THOSE FIGURES ARE WRONG, and
the corrections run in opposite directions, which is why the totals still
reconcile. The 151 was **150 of the 159 plus one supernumerary** -- `A426b`, a
SECOND mutant on statement line `:428` (`else -1` -> `else 1`), which is not a
member of the 159 at all. And the "12 continuation lines" were **11**, so
**nine** of the 20 went unmutated, not eight: `:442`, `:443`, `:444`, `:480`,
`:481`, `:482`, `:513`, `:514` inside the three `add_to_modlog(...)` calls, and
`:578`, `targets=targets_data)`, the last line of the `Notification(...)` call
in `choose_answer`. `:578` is not an `add_to_modlog` line, which is why the
pattern that found the other eight walked past it.
There was no reason for the exclusion; it was
not a judgement that those lines were cosmetic, it was an unexamined narrowing,
and it was disclosed nowhere. Mutating the nine gives **8 survivors and 1 kill**:
the eight `add_to_modlog` lines all lived, because the only thing any test here
read off a `ModLog` row was `action`, while `:578` died at once against the
notification test. An earlier wording of this paragraph carried the superseded
framing "the 12 were the 20 minus the 8", which is the round's own named defect
class -- a claim left standing after its premise was withdrawn -- surviving
inside the correction written for that class. The eight
are closed now, together with six more mutants on the `target_user` and
`reason` arguments that share `:441`, `:479` and `:512` with the action literal
and were equally unread; the three `..._action` tests now assert every field
the call is handed. A fifth survivor category is therefore named and empty
rather than absent, which is the point: a future round reading an inventory of
four categories would otherwise conclude those lines had been mutated and found
safe.

THE SAME PASS ALSO RE-RAN COVERAGE AND FOUND TWO ARCS OPEN, which is why this
file has five new tests rather than three. Tasks 1-6 drove these six functions
to zero missing statements and zero missing arcs, and Task 7's authorization
fix then added two statements and two arcs to each of `lock_post_reply` and
`set_collapse_post_reply` and covered only the true arm of each: against the 46
tests, `missing_branches` for this module was `[[518, 521], [545, 548]]` --
the unprivileged caller who is on the WEB path rather than the API path, and so
is neither served nor refused. Both are closed here. Missing statements and
missing arcs are back to zero for all six functions.

  EQUIVALENT ON EVERY PATH PRODUCTION BUILDS (4). `:434` and `:471`
  `len(reply.path) > 1` -> `> 0`, and the same two lines with the whole guard
  reduced to `if reply.path:`. The length test is unobservable because no
  production writer emits a one-element path: app/models.py:3064 opens a root
  path as `[0, reply.id]` and :3059-3060 appends to a parent's, so every path
  is empty/None or at least two long, and app/api/alpha/views.py:669-686's
  `calculate_path` starts every branch at `[0, ...]` too. The one writer that
  could produce a shorter path is app/post/util.py:79,
  `post_reply.path = reply_data.get('path', [])`, which takes whatever an
  import payload contains. NOT CLOSED DELIBERATELY: a test pinning behaviour
  on a one-element path would assert something the ordinary flow cannot
  produce, and on every input it can produce the mutant computes what the
  original computes.

  THE OTHER OPERAND OF THE SAME GUARD DIES, AND DIES ONLY BY CRASHING.
  Dropping `reply.path` instead -- leaving `if len(reply.path) > 1:` -- fails
  18 tests, and `sort -u` over the `E ` lines gives exactly one reason,
  `TypeError: object of type 'NoneType' has no len()`. The standing rule is
  that a crash kill counts only if a viable NON-crashing variant of the same
  fault also dies, and here the rule is satisfied VACUOUSLY: there is no such
  variant, because crash prevention is the operand's entire contribution. Over
  the five path shapes that reach this line -- `None`, `[]`, `[0]`, `[0, p]`,
  `[0, p, r]` -- the original and the mutant agree on four and differ only on
  `None`, where the mutant raises. An operand whose only effect is to stop an
  exception cannot be dropped non-fatally, so the argument is recorded here
  rather than a non-crashing variant being manufactured.

  UNOBSERVABLE WITHIN ONE SESSION (6). `:437`, `:475`, `:511`, `:541`, `:581`
  and `:597` `db.session.commit()` -> `db.session.flush()`. tests/conftest.py's
  `db_session` deliberately does NOT wrap tests in a rollback (it says so at
  :140-142, because the code under test commits); but every assertion in this
  file reads back through the same `db.session` that ran the call, and a flush
  is already visible there. Distinguishing them needs a second connection
  reading uncommitted data, which nothing here has and which would be a test
  about SQLAlchemy rather than about these six functions.

  VOID OVER THE INPUTS THAT EXIST (2). `:495` and `:534` `.one()` -> `.first()`
  on `filter_by(id=post_reply_id)`. The two differ only at zero rows or more
  than one; more than one is impossible on a primary key, and zero is the
  missing-reply case no test in this file supplies. Closable in principle by a
  call with an unused id asserting `NoResultFound` rather than the
  `AttributeError` a `None` would raise three lines later -- recorded rather
  than written, because the value is in the failure mode of a caller these
  tests do not have.

  MASKED BY THE FIXTURE'S OWN USERS (1). `:566`
  `get_recipient_language(post_reply.user_id)` -> `get_recipient_language(
  user.id)`, the recipient's locale replaced by the actor's. Probe A above
  already established why nothing moves: `make_user` (tests/factories.py:41)
  sets neither `language_id` nor `interface_language`, so
  `get_recipient_language` reaches its `else: lang_to_use = 'en'` at
  app/utils.py:4799 for EVERY user, and two users who both resolve to English
  cannot be told apart by the title `force_locale` then produces. Closing it
  needs a second locale with a compiled catalogue, so that
  `_('Your answer was chosen...')` actually renders differently -- a
  translation-infrastructure dependency this file has none of elsewhere.

  THE MODERATION LOG'S ARGUMENTS (0, AND NAMED BECAUSE IT WAS ONCE 8).
  `:442`-`:444`, `:480`-`:482` and `:513`-`:514`, plus `target_user` and
  `reason` on `:441`, `:479` and `:512`. Every one of the fourteen lived
  against the 51 tests that stood before this category was named, because the
  only field any test read off a `ModLog` row was `action`. All fourteen die
  now. The category is kept with a zero rather than dropped, so that the count
  above reads 4 + 6 + 2 + 1 + 0 = 13 and the reader can see that these lines
  were measured rather than never reached.
"""

import pytest
from contextlib import contextmanager
from types import SimpleNamespace

from app import db
from app.api.alpha.utils.reply import post_reply_mark_as_answer
from app.constants import NOTIF_ANSWER, SRC_API, SRC_WEB
from app.models import ModLog, Notification, PostReply
from app.shared.reply import (
    choose_answer, lock_post_reply, mod_remove_reply, mod_restore_reply,
    set_collapse_post_reply, unchoose_answer,
)
from tests.factories import (
    bearer, make_community, make_community_member, make_instance, make_post,
    make_post_reply, make_user, web_ctx,
)
# `make_site` is deliberately NOT imported: no test in this file needs a `Site`
# row -- see THE `make_site()` RULE in the module docstring. The import was
# dropped with the last call site in sub-project 41's final fix wave, so that an
# unused name cannot suggest the rule was merely overlooked somewhere.


def _seed_moderated_reply(*, private=True, community_name='moderation'):
    """One instance, one local user, one community, one post, one reply.

    Modelled on `tests/test_shared_reply_interactions.py:507`'s `_seed_reply`
    and carrying its ordering constraints: `make_community` hardcodes
    `instance_id=1` and `user_id=1` (tests/factories.py:141-142) and
    tests/conftest.py:131 resets every sequence after each test, so the
    instance minted first lands on id 1 and the community resolves to it.

    `author` is the reply's author and `actor` is the user who will moderate
    it. They are DISTINCT, and that is load-bearing rather than tidy:
    `mod_remove_reply:428` is
    `reply.deleted_by = user.id if user.id != reply.user_id else -1`, so a
    fixture where the actor IS the author can never witness the `user.id` arm.

    `private=True` sets `community.private`, the federation lever register
    entry D393(d) identifies: it stops the eager Celery task bodies at their
    first guard so no test issues an outbound request.
    """
    instance = make_instance('local.example', software='piefed')
    author = make_user(instance, 'author', local=True)
    actor = make_user(instance, 'actor', local=True)
    community = make_community(community_name)
    community.private = private
    db.session.commit()
    post = make_post(community, author, 'https://local.example/p/1')
    reply = make_post_reply(post, author)
    db.session.commit()
    return SimpleNamespace(instance=instance, author=author, actor=actor,
                           community=community, post=post, reply=reply)


def seed_moderator(s, user=None):
    """Make `user` (default `s.actor`) a moderator of `s.community`.

    `Community.moderators()` (app/models.py:716-722) filters
    `is_banned == False`, so a banned CommunityMember is NOT a moderator --
    an arm worth pinning separately rather than assuming.
    """
    return make_community_member(user or s.actor, s.community, is_moderator=True)


def make_site_admin(user):
    """Give `user` a role named exactly 'Admin'.

    `User.is_admin()` (app/models.py:1259-1265) checks role NAMES, not
    permissions, so `grant_permission` cannot produce a site admin however it
    is called. The name must be the literal string 'Admin' -- sub-project 39
    lost a task to a helper that named the role 'role-4'.
    """
    from app.models import Role, user_role
    role = Role(name='Admin', weight=0)
    db.session.add(role)
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=user.id, role_id=role.id))
    db.session.commit()
    return role


def make_instance_admin(user, instance):
    """An InstanceRole making `user` an admin of `instance`.

    `Community.is_instance_admin(user)` (app/models.py:769-776) looks up
    InstanceRole by the COMMUNITY's instance_id, not the user's, so the
    instance passed here must be the one the community resolves to -- the
    first instance seeded, id 1.
    """
    from app.models import InstanceRole
    role = InstanceRole(instance_id=instance.id, user_id=user.id, role='admin')
    db.session.add(role)
    db.session.commit()
    return role


@contextmanager
def recording_task_selector():
    """Yield a list collecting every task key `app.shared.reply` federates.

    The six functions call `task_selector(...)` unqualified, so rebinding the
    name ON THE MODULE is what intercepts them -- patching
    `app.shared.tasks.task_selector` would not, because the `from ... import`
    at app/shared/reply.py:12 already bound the original into this module's
    globals.

    The recorder calls through to the original rather than stubbing it, so the
    permitted paths still do whatever they do; `_seed_moderated_reply` passes
    `private=True`, which stops every task body at its first guard.

    Restores in a `finally`: `app.shared.reply` is imported once per session,
    so a leaked patch would corrupt every test that ran after this one.
    """
    import app.shared.reply as reply_module
    calls = []
    original = reply_module.task_selector

    def recorder(task_key, **kwargs):
        calls.append(task_key)
        return original(task_key, **kwargs)

    reply_module.task_selector = recorder
    try:
        yield calls
    finally:
        reply_module.task_selector = original


class TestModRemoveReply:
    """`mod_remove_reply` (app/shared/reply.py:416-451)."""

    def test_a_moderator_removes_a_reply_through_the_api(self, db_session):
        """`:423`'s false arm via `is_moderator`, and the writes below it.

        Asserts `deleted` AND `deleted_by`, because `:426` sets the flag on
        every path that gets past the guard and the flag alone cannot say
        which arm of `:428`'s conditional ran.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)

        user_id, reply = mod_remove_reply(s.reply.id, 'spam', SRC_API,
                                          auth=bearer(s.actor))

        assert user_id == s.actor.id
        db.session.refresh(s.reply)
        assert s.reply.deleted is True
        assert s.reply.deleted_by == s.actor.id

    def test_an_unprivileged_user_is_refused_and_changes_nothing(self, db_session):
        """`:423`'s true arm -- all three disjuncts false -- and `:424`'s raise.

        THE RAISE IS NOT THE WITNESS ON ITS OWN. A crash is a weak kill, so
        this also asserts that `deleted` is still False and that no ModLog row
        was written: a mutant that ran the body and then raised would pass a
        `pytest.raises` alone.
        """
        s = _seed_moderated_reply()

        with pytest.raises(Exception, match='Does not have permission'):
            mod_remove_reply(s.reply.id, 'spam', SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.reply)
        assert s.reply.deleted is False
        assert db.session.query(ModLog).count() == 0

    def test_an_instance_admin_may_remove(self, db_session):
        """`:423`'s SECOND disjunct alone -- `is_instance_admin` -- with the
        first and third false.

        `:423` is three disjuncts scored by coverage.py as one arc pair, so
        each needs its own witness or mechanism (e) applies: two conditions
        exercised only in lockstep cannot detect a swap between them.
        """
        s = _seed_moderated_reply()
        make_instance_admin(s.actor, s.instance)

        mod_remove_reply(s.reply.id, 'spam', SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.reply)
        assert s.reply.deleted is True

    def test_a_site_admin_who_is_neither_may_remove(self, db_session):
        """`:423`'s THIRD disjunct alone -- `user.is_admin_or_staff()`.

        This is the disjunct `mod_restore_reply:461` does NOT have, which is
        finding 2 in the spec; the paired test in `TestModRestoreReply` shows
        the same user refused there.
        """
        s = _seed_moderated_reply()
        make_site_admin(s.actor)

        mod_remove_reply(s.reply.id, 'spam', SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.reply)
        assert s.reply.deleted is True

    def test_a_moderator_removing_their_own_reply_records_minus_one(self, db_session):
        """`:428`'s ELSE arm -- `user.id == reply.user_id` gives `-1`.

        The comment at `:427` says this makes the UI show 'removed' rather
        than 'deleted'. The sibling test above takes the other arm with a
        distinct actor, so the two together pin the conditional rather than
        the assignment.
        """
        s = _seed_moderated_reply()
        seed_moderator(s, user=s.author)

        mod_remove_reply(s.reply.id, 'spam', SRC_API, auth=bearer(s.author))

        db.session.refresh(s.reply)
        assert s.reply.deleted_by == -1

    def test_a_bot_authors_reply_does_not_move_the_three_guarded_counters(self, db_session):
        """`:429`'s false arm -- `reply.author.bot` true, so `:430`-`:432` are
        skipped while `:433` still runs.

        THE GUARDED BLOCK GREW FROM ONE STATEMENT TO THREE, AND SO DID THIS
        TEST. It was called
        `test_a_bot_authors_reply_does_not_move_the_post_counter` and read two
        columns, because `:430` was then the guard's only statement. Sub-project
        42 task 7 fixed the counter drift registered as D522/D523 and added
        `reply.post.reply_count_cross_posted -= 1` and
        `reply.community.post_reply_count -= 1` beside it, so a bot author now
        skips three decrements rather than one and the old name understated what
        the false arm does. Holding all three level is the assertion; the
        author's own counter still moving is the witness that the function ran.

        THE COUNTERS MUST BE SEEDED DISTINCT or this witnesses nothing: if they
        start at the same value, a mutant moving the wrong one is invisible.
        They are seeded 7, 11, 5 and 3 here and asserted separately.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)
        s.author.bot = True
        s.post.reply_count = 7
        s.post.reply_count_cross_posted = 11
        s.community.post_reply_count = 5
        s.author.post_reply_count = 3
        db.session.commit()

        mod_remove_reply(s.reply.id, 'spam', SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.post)
        db.session.refresh(s.community)
        db.session.refresh(s.author)
        assert s.post.reply_count == 7
        assert s.post.reply_count_cross_posted == 11
        assert s.community.post_reply_count == 5
        assert s.author.post_reply_count == 2

    def test_a_human_authors_reply_moves_all_four_counters(self, db_session):
        """`:429`'s true arm -- the same-mechanism positive control.

        Without it, a fixture in which no counter could ever move would
        produce the same untouched `reply_count` as the test above. Same
        seed, same distinct values, one lever moved.

        IT WAS `test_a_human_authors_reply_moves_both_counters` AND READ TWO
        COLUMNS. That was accurate while the guard held one statement; after
        sub-project 42 task 7 fixed D522/D523 it moves four, and a name saying
        "both" would have been a stale claim in the pytest output itself. This
        test is the only witness in this file for the two decrements that fix
        added to `mod_remove_reply`: deleting either one from the guard fails
        here and nowhere else, since every other test in the class reads
        `reply_count` and `post_reply_count` alone.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)
        s.post.reply_count = 7
        s.post.reply_count_cross_posted = 11
        s.community.post_reply_count = 5
        s.author.post_reply_count = 3
        db.session.commit()

        mod_remove_reply(s.reply.id, 'spam', SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.post)
        db.session.refresh(s.community)
        db.session.refresh(s.author)
        assert s.post.reply_count == 6
        assert s.post.reply_count_cross_posted == 10
        assert s.community.post_reply_count == 4
        assert s.author.post_reply_count == 2

    def test_a_multi_element_path_decrements_the_ancestors_child_count(self, db_session):
        """`:434`'s true arm and the raw SQL at `:435-436`.

        The guard is D517's shape, `if reply.path and len(reply.path) > 1:`.
        The path seeded here is production's -- app/models.py:3058-3065 gives
        `[0, parent.id, reply.id]` -- so `tuple(reply.path[:-1])` is
        `(0, parent.id)`, a genuine multi-element IN operand.

        THE BYSTANDER WITNESSES THE `where` CLAUSE: a mutant dropping it would
        flip every reply in the table, and only a row the statement should NOT
        have touched catches that.

        THE REPLY'S OWN `child_count` IS ASSERTED TOO, AND THIS PARAGRAPH
        RETRACTS THE CLAIM THAT IT NEED NOT BE. Until Task 8 the docstring here
        read "THE ASSERTION IS ON THE ANCESTOR, NOT THE REPLY. The reply's own
        `child_count` is untouched by this statement, so asserting on it would
        witness nothing". The first sentence is true of the CORRECT code and
        false of the code's neighbourhood, which is the distinction that
        matters for a test: `:436` is `tuple(reply.path[:-1])`, and the whole
        job of the `[:-1]` is to drop the reply's own id off the end of the
        path so the reply is not counted as its own ancestor. Task 8's
        mutation pass changed `:436` to `tuple(reply.path)` and all 46 tests
        stayed green, because nothing looked at the one row that changes. The
        seeded 3 below therefore witnesses the slice, not the UPDATE:
        `parent.child_count == 4` cannot tell `[:-1]` from no slice at all.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)
        parent = make_post_reply(s.post, s.author)
        bystander = make_post_reply(s.post, s.author)
        db.session.commit()
        parent.child_count = 5
        bystander.child_count = 9
        s.reply.child_count = 3
        s.reply.path = [0, parent.id, s.reply.id]
        db.session.commit()

        mod_remove_reply(s.reply.id, 'spam', SRC_API, auth=bearer(s.actor))

        db.session.refresh(parent)
        db.session.refresh(bystander)
        db.session.refresh(s.reply)
        assert parent.child_count == 4
        assert bystander.child_count == 9
        assert s.reply.child_count == 3

    def test_the_web_arm_flashes_and_returns_none(self, db_session, app):
        """`:417`'s false arm, `:438`'s true arm, `:439`'s flash, `:451`.

        NO `make_site()` HERE, and the reason is the sibling's:
        `TestModRestoreReply::test_the_web_arm_flashes_and_returns_none_on_restore`
        drives the structurally identical arm without one. This arm neither
        renders a template nor calls `can_downvote` -- the only two reasons the
        module docstring's rule requires a `Site` row -- it only flashes and
        returns. `web_ctx` opens the request context with
        `app.test_request_context`, which does not fire `before_request` and so
        never populates `g.site` anyway; `flash()` writes to the session and
        flask_babel's `_()` does not touch the database. Nothing on this path
        reads `Site.query.get(1)`.

        THIS TEST DID CALL `make_site()`, justified by a rule the module
        docstring does not contain -- "a Site row is needed wherever a template
        or A PERMISSION READ touches it". The real rule names `render_template`
        or `can_downvote`, and the sibling above said the opposite for the same
        code shape. Two docstrings in one file gave contradictory answers, so
        the call is dropped and the sibling's reason is stated instead.
        """
        from flask import get_flashed_messages
        s = _seed_moderated_reply()
        seed_moderator(s)

        with web_ctx(app, s.actor):
            result = mod_remove_reply(s.reply.id, 'spam', SRC_WEB, auth=None)
            messages = get_flashed_messages()

        assert result is None
        assert 'Comment deleted.' in messages

    def test_the_modlog_row_names_the_delete_action(self, db_session):
        """`:441-444`'s add_to_modlog with the literal 'delete_post_reply'.

        Compared as a SET, never as an ordered list -- the campaign's rule
        about rows a query planner returned. THAT RULE WAS STATED HERE AND
        BROKEN THREE LINES LATER: the row this test then asserts on was picked
        with `rows[0]`, an ordered access, and the set assertion above does not
        constrain `len(rows)` so nothing made it safe except that exactly one
        row can be written today. It now selects by ACTION, the idiom
        `TestModRestoreReply::test_the_modlog_row_names_the_restore_action`
        already used, so no assertion in this test depends on planner order.

        This is also the positive control that
        `test_an_unprivileged_user_is_refused_and_changes_nothing`'s
        `ModLog.count() == 0` needed and did not have: that test's zero
        proves nothing about a mechanism that can never write a row at all,
        so this test is what makes that earlier zero mean something.

        EVERY ARGUMENT IS ASSERTED, NOT JUST THE ACTION. The action was the
        only field this test read until Task 8's fix round, and `:441`-`:444`
        hand `add_to_modlog` eight more: `target_user`, `reason`, `community`,
        `post`, `reply`, `link_text` and `link`. Mutants on all of them lived.
        The two that matter most are `target_user` and `reason` -- a
        moderation log that misattributes an action, or that loses the
        moderator's stated reason, is worse than no log -- but the whole row
        is cheap to assert once the row is in hand, so the whole row is
        asserted. The fields are IDS: `add_to_modlog` (app/utils.py:3574-3581)
        resolves each object to `x.id if x else None` before constructing the
        `ModLog`, so `post=None` shows up as `post_id is None` and nothing
        else.

        A SECOND REPLY IS THE TARGET so that `target.id` is 2 while
        `s.post.id` is 1. `:444`'s link is
        `f'post/{reply.post_id}#comment_{reply.id}'`, and against
        `_seed_moderated_reply`'s single post and single reply -- both id 1 --
        transposing the two halves produces the identical string. This is the
        same sequence-reset collision that hid `lock_post_reply:510` and
        `choose_answer:570`; it is a property of the fixture, not of these
        three tests, and it will hide the next one too.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)
        target = make_post_reply(s.post, s.author)
        db.session.commit()
        assert target.id != s.post.id

        mod_remove_reply(target.id, 'spam', SRC_API, auth=bearer(s.actor))

        rows = db.session.query(ModLog).all()
        assert {row.action for row in rows} == {'delete_post_reply'}
        row = next(r for r in rows if r.action == 'delete_post_reply')
        assert row.user_id == s.actor.id
        assert row.type == 'mod'
        assert row.target_user_id == s.author.id
        assert row.reason == 'spam'
        assert row.community_id == s.community.id
        assert row.post_id == s.post.id
        assert row.reply_id == target.id
        assert row.link == f'post/{s.post.id}#comment_{target.id}'
        assert row.link_text == 'comment on a post'

    def test_the_federation_task_is_selected(self, db_session):
        """`:446`'s task_selector call, intercepted on the module.

        `recording_task_selector` calls through rather than stubbing, so this
        also proves the call is reached on the permitted path rather than
        merely that a name exists.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)

        with recording_task_selector() as calls:
            mod_remove_reply(s.reply.id, 'spam', SRC_API, auth=bearer(s.actor))

        assert 'delete_reply' in calls


class TestModRestoreReply:
    """`mod_restore_reply` (app/shared/reply.py:454-489).

    THE GUARD HAS TWO DISJUNCTS WHERE `mod_remove_reply`'S HAS THREE. `:461`
    is `is_moderator or is_instance_admin`; `:423` adds
    `user.is_admin_or_staff()`. So a site admin who is not a moderator can
    remove a comment and then cannot restore it. That is finding 2 in the
    spec, registered rather than fixed, and
    `test_a_site_admin_who_is_neither_is_refused` below is its witness --
    paired deliberately with `TestModRemoveReply`'s
    `test_a_site_admin_who_is_neither_may_remove`, which shows the same user
    permitted one line earlier in the file.
    """

    def _removed(self, *, bot=False):
        """A seeded reply already removed through `mod_remove_reply`.

        Routed through the production verb rather than set with the ORM, so
        `deleted_by` arrives holding a real actor id and a mutant clearing it
        is visible.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)
        if bot:
            s.author.bot = True
        db.session.commit()
        mod_remove_reply(s.reply.id, 'spam', SRC_API, auth=bearer(s.actor))
        db.session.refresh(s.reply)
        return s

    def test_a_moderator_restores_a_removed_reply(self, db_session):
        """`:461`'s false arm via `is_moderator`, `:464`-`:465`'s writes.

        Asserts `deleted_by` back to None as well as `deleted` to False: `:464`
        runs on every permitted path, so the flag alone cannot witness `:465`.
        """
        s = self._removed()

        user_id, reply = mod_restore_reply(s.reply.id, 'ok', SRC_API,
                                           auth=bearer(s.actor))

        assert user_id == s.actor.id
        db.session.refresh(s.reply)
        assert s.reply.deleted is False
        assert s.reply.deleted_by is None

    def test_an_instance_admin_may_restore(self, db_session):
        """`:461`'s SECOND disjunct alone, with the first false."""
        s = self._removed()
        other = make_user(s.instance, 'admin-user', local=True)
        db.session.commit()
        make_instance_admin(other, s.instance)

        mod_restore_reply(s.reply.id, 'ok', SRC_API, auth=bearer(other))

        db.session.refresh(s.reply)
        assert s.reply.deleted is False

    def test_a_site_admin_who_is_neither_is_refused(self, db_session):
        """`:461`'s true arm for a user `mod_remove_reply:423` WOULD admit.

        THIS TEST PINS FINDING 2 AND ASSERTS THE DIVERGENCE ON PURPOSE. The
        same user, with the same role, is permitted by
        `TestModRemoveReply::test_a_site_admin_who_is_neither_may_remove`. If
        a later round makes the two guards agree, THE EDIT OWED HERE IS TO
        INVERT THIS TEST: the restore must then succeed and `deleted` must
        read False. Its failure at that point is the fix landing, not a
        regression.
        """
        s = self._removed()
        other = make_user(s.instance, 'staffer', local=True)
        db.session.commit()
        make_site_admin(other)

        with pytest.raises(Exception, match='Does not have permission'):
            mod_restore_reply(s.reply.id, 'ok', SRC_API, auth=bearer(other))

        db.session.refresh(s.reply)
        assert s.reply.deleted is True

    def test_a_bot_authors_reply_does_not_move_the_three_guarded_counters_on_restore(self, db_session):
        """`:466`'s false arm -- `:467`-`:469` skipped, `:470` still runs.

        Named distinctly from `TestModRemoveReply`'s test of the same shape --
        two module-level test methods sharing a name across classes still
        collide in the duplicate-name check this campaign runs.

        IT WAS `..._does_not_move_the_post_counter_on_restore` AND READ TWO
        COLUMNS, which was accurate while `mod_restore_reply`'s bot guard held
        one statement. Sub-project 42 task 7 fixed D522/D523 and added
        `reply.post.reply_count_cross_posted += 1` and
        `reply.community.post_reply_count += 1` to it, so the false arm now
        skips three increments and all three are asserted level here. The
        columns are seeded 7, 11, 5, 3 -- pairwise distinct, so a mutant moving
        the wrong one is visible.
        """
        s = self._removed(bot=True)
        s.post.reply_count = 7
        s.post.reply_count_cross_posted = 11
        s.community.post_reply_count = 5
        s.author.post_reply_count = 3
        db.session.commit()

        mod_restore_reply(s.reply.id, 'ok', SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.post)
        db.session.refresh(s.community)
        db.session.refresh(s.author)
        assert s.post.reply_count == 7
        assert s.post.reply_count_cross_posted == 11
        assert s.community.post_reply_count == 5
        assert s.author.post_reply_count == 4

    def test_a_human_authors_reply_moves_all_four_counters_on_restore(self, db_session):
        """`:466`'s true arm -- the same-mechanism positive control.

        IT WAS `..._moves_both_counters_on_restore`, for the reason its
        `TestModRemoveReply` twin's docstring gives: the guard moved one counter
        until D522/D523 were fixed and now moves three. This test is the only
        witness in this file for the two increments that fix added to
        `mod_restore_reply` -- delete either from the guard and this test fails
        alone.
        """
        s = self._removed()
        s.post.reply_count = 7
        s.post.reply_count_cross_posted = 11
        s.community.post_reply_count = 5
        s.author.post_reply_count = 3
        db.session.commit()

        mod_restore_reply(s.reply.id, 'ok', SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.post)
        db.session.refresh(s.community)
        db.session.refresh(s.author)
        assert s.post.reply_count == 8
        assert s.post.reply_count_cross_posted == 12
        assert s.community.post_reply_count == 6
        assert s.author.post_reply_count == 4

    def test_a_multi_element_path_increments_the_ancestors_child_count(self, db_session):
        """`:471`'s true arm and `:472-473`'s raw SQL.

        The mirror of `TestModRemoveReply`'s path test, with a BYSTANDER for
        the same reason: a mutant dropping the `where` clause is caught only
        by a row the statement should not have touched.

        THE REPLY'S OWN `child_count` IS ASSERTED for the reason the twin's
        docstring now gives at length: `:473`'s `tuple(reply.path[:-1])` drops
        the reply's own id off the path, and Task 8's mutation pass changed it
        to `tuple(reply.path)` with all 46 tests still green. `parent.
        child_count == 6` cannot tell the slice from its absence; the seeded 3
        can.
        """
        s = self._removed()
        parent = make_post_reply(s.post, s.author)
        bystander = make_post_reply(s.post, s.author)
        db.session.commit()
        parent.child_count = 5
        bystander.child_count = 9
        s.reply.child_count = 3
        s.reply.path = [0, parent.id, s.reply.id]
        db.session.commit()

        mod_restore_reply(s.reply.id, 'ok', SRC_API, auth=bearer(s.actor))

        db.session.refresh(parent)
        db.session.refresh(bystander)
        db.session.refresh(s.reply)
        assert parent.child_count == 6
        assert bystander.child_count == 9
        assert s.reply.child_count == 3

    def test_the_web_arm_flashes_and_returns_none_on_restore(self, db_session, app):
        """`:455`'s false arm, `:476`'s true arm, `:477`'s flash, `:489`.

        NO `make_site()` HERE. It once said "unlike the sibling test in
        `TestModRemoveReply`", which was true until the final fix wave dropped
        that sibling's unnecessary call; NO TEST IN THIS FILE CALLS
        `make_site()` now, and the two docstrings agree.
        This arm neither renders a template nor calls `can_downvote` -- the
        only two reasons the module docstring's rule requires a `Site` row --
        it only flashes and returns. `web_ctx` opens the request context with
        `app.test_request_context`, which does not fire `before_request` and
        so never populates `g.site` anyway; `flash()` writes to the session
        and flask_babel's `_()` does not touch the database. Nothing on this
        path reads `Site.query.get(1)`.
        """
        from flask import get_flashed_messages
        s = self._removed()

        with web_ctx(app, s.actor):
            result = mod_restore_reply(s.reply.id, 'ok', SRC_WEB, auth=None)
            messages = get_flashed_messages()

        assert result is None
        assert 'Comment restored.' in messages

    def test_the_modlog_row_names_the_restore_action(self, db_session):
        """`:479-482`'s add_to_modlog with the literal 'restore_post_reply'.

        The set here holds TWO actions, because the removal below wrote the
        delete row first. Asserting the set rather than a count is what makes
        the restore action's presence the witness.

        EVERY ARGUMENT IS ASSERTED for the reason
        `TestModRemoveReply::test_the_modlog_row_names_the_delete_action`
        gives at length, and the seeding is done inline rather than through
        `self._removed()` for the reason it gives too: the restore must act on
        a reply whose id differs from its post's, or `:482`'s link cannot tell
        `post_id` from `id`.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)
        target = make_post_reply(s.post, s.author)
        db.session.commit()
        assert target.id != s.post.id
        mod_remove_reply(target.id, 'spam', SRC_API, auth=bearer(s.actor))

        mod_restore_reply(target.id, 'ok', SRC_API, auth=bearer(s.actor))

        rows = db.session.query(ModLog).all()
        assert {row.action for row in rows} == {'delete_post_reply', 'restore_post_reply'}
        row = next(r for r in rows if r.action == 'restore_post_reply')
        assert row.user_id == s.actor.id
        assert row.type == 'mod'
        assert row.target_user_id == s.author.id
        assert row.reason == 'ok'
        assert row.community_id == s.community.id
        assert row.post_id == s.post.id
        assert row.reply_id == target.id
        assert row.link == f'post/{s.post.id}#comment_{target.id}'
        assert row.link_text == 'comment on a post'

    def test_the_federation_task_is_selected_for_restore(self, db_session):
        """`:484`'s task_selector call."""
        s = self._removed()

        with recording_task_selector() as calls:
            mod_restore_reply(s.reply.id, 'ok', SRC_API, auth=bearer(s.actor))

        assert 'restore_reply' in calls


class TestLockPostReply:
    """`lock_post_reply` (app/shared/reply.py:492-528).

    THIS FUNCTION USED TO FAIL SILENTLY WHERE ITS TWIN RAISED. THAT IS FIXED,
    AND THE PARAGRAPH THAT STOOD HERE THROUGH TASK 6 IS RETRACTED. It read:
    "`:507`'s guard has no `else`, so an unauthorized SRC_API caller falls
    through to `:525-526` and receives `user.id, post_reply` with nothing
    changed -- a 200 carrying the unchanged object." That was true of the code
    as it stood through Task 6 and it is stale now. Task 7 transcribed the
    repair from the twin module -- `app/shared/post.py:968-969` in `lock_post`
    and `:999-1000` in `move_post`, both from PC2 in sub-project 36 -- so
    `:507`'s guard now carries `elif src == SRC_API: raise Exception('Does not
    have permission')` at `:524-525`. An unauthorized API caller is refused.
    The line numbers in this class shifted by two at or after `:524` as a
    result; every one below was re-derived at Task 7's commit.

    THE WEB ARM STILL FALLS THROUGH SILENTLY. The `elif` is guarded on
    `src == SRC_API`, matching the twin exactly, and that faithfulness is the
    whole reason it was left: a coverage round widening a guard beyond its twin
    manufactures a fresh divergence to sit beside D521's. A SRC_WEB caller who
    fails `:507` still reaches `:527` with nothing changed and no flash.

    AN EARLIER WORDING OF THIS PARAGRAPH ADDED "because the web routes do their
    own authorization before calling". THAT IS FALSE AT SOURCE. It is retracted
    here rather than quietly deleted, because a reader who believed it would
    take it as closing a live registered defect as a non-defect, and a later
    round reading this file
    would have had no reason to doubt it. Re-derived with numbered output in
    sub-project 41's final fix wave, from `app/post/routes.py` lines 1666-1680
    printed by an awk that emits `NR` beside an unmodified `$0` (D536 -- an awk
    that assigns to a field would have collapsed the indentation that shows
    these bodies are one line each):

      1668  @bp.route('/post/<int:post_id>/<int:post_reply_id>/lock/<mode>', methods=['POST'])
      1669  @login_required
      1670  def post_reply_lock(post_id: int, post_reply_id: int, mode):
      1671      lock_post_reply(post_reply_id, mode == 'yes', SRC_WEB)
      1675  @bp.route('/post/<int:post_id>/<int:post_reply_id>/collapse/<mode>', methods=['POST'])
      1676  @login_required
      1677  def post_reply_collapse(post_id: int, post_reply_id: int, mode):
      1678      set_collapse_post_reply(post_reply_id, mode == 'yes', SRC_WEB)

    `@login_required` IS THE ONLY DECORATOR AND NEITHER BODY GUARDS. So the
    fall-through is not a redundant belt behind a web guard -- IT IS THE ONLY
    AUTHORIZATION EITHER ROUTE HAS, and it refuses by doing nothing. The
    consequence, stated plainly because the wrong sentence hid it: ANY LOGGED-IN
    USER CAN LOCK OR COLLAPSE ANY COMMENT THROUGH THE WEB, and gets a 302 back
    to the post with no flash and no sign that nothing happened. That is
    register entry D524, and D524 is OPEN.

    `app/post/routes.py` IS MIXED, WHICH IS HOW THE WRONG CLAIM SURVIVED READING:
    `post_reply_choose_answer` at `:2440-2447` DOES guard -- on
    `is_admin_or_staff()` OR authorship OR `community.is_moderator()`, with
    `abort(403)` on the else -- and so does `post_reply_unchoose_answer` at
    `:2450-2457`. A blanket sentence about "the web routes" is therefore unsafe
    in this file whichever way it points. Check the route you actually mean.

    AND THE API HALF DOES NOT ANSWER 403. A bare `Exception` out of an API
    handler reaches `shared_error_handler` (`app/api/alpha/__init__.py:108-114`),
    which returns HTTP **400**, and logs the exception -- plus a Sentry event when
    `SENTRY_DSN` is configured, which guards only the Sentry call and not the log. So the same denial is 400 through the API and 403 through the web. That
    is faithful to the twin and it is register entry D537; no test here asserts a
    status code, because these tests call the shared function directly.

    False-witness mechanism (a) is still why every test below asserts on state
    rather than on the return: THE RETURN VALUE IS IDENTICAL ON EVERY ARM THAT
    RETURNS AT ALL, so `user.id, post_reply` discriminates nothing. The
    refusal is now witnessed by `pytest.raises` PLUS the same state
    assertions, not by the raise alone.

    NO `make_site()` ANYWHERE IN THIS CLASS. The module docstring's rule only
    requires a `Site` row where a template renders or `can_downvote` runs, and
    this function does neither: its only two branches either flash-and-return
    or fall through untouched. `render_template` in this module appears only
    at `:65` and `:145` (Group A), both outside this function, and `web_ctx`
    opens its request context with `app.test_request_context`, which never
    fires `before_request` and so never populates `g.site` regardless.
    """

    def test_a_moderator_locks_a_reply_through_the_api(self, db_session):
        """`:500`'s true arm, `:507`'s true arm, `:508`'s assignment.

        Asserts `replies_enabled` rather than the return, because `:527-528`
        returns `user.id, post_reply` on every arm that returns at all. (This
        docstring cited `:525-526` and "the refused path" through Task 6; the
        lines moved when Task 7 inserted the refusal at `:524-525`, and there
        is no longer a refused path that returns -- it raises.)
        """
        s = _seed_moderated_reply()
        seed_moderator(s)
        s.reply.replies_enabled = True
        db.session.commit()

        user_id, reply = lock_post_reply(s.reply.id, True, SRC_API,
                                         auth=bearer(s.actor))

        assert user_id == s.actor.id
        db.session.refresh(s.reply)
        assert s.reply.replies_enabled is False

    def test_unlocking_sets_replies_enabled_back_to_true(self, db_session):
        """`:500`'s false arm, `:504`'s assignment and `:505`'s modlog_type."""
        s = _seed_moderated_reply()
        seed_moderator(s)
        s.reply.replies_enabled = False
        db.session.commit()

        lock_post_reply(s.reply.id, False, SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.reply)
        assert s.reply.replies_enabled is True

    def test_locking_cascades_to_a_descendant(self, db_session):
        """`:509-510`'s containment query, the module's only `@>`.

        `where path @> ARRAY[:parent_id]` matches every reply whose `path`
        CONTAINS the locked reply's id, which is how a lock reaches a whole
        subtree. The descendant's path is production's shape --
        app/models.py:3058-3065, `[0, parent.id, child.id]` -- because
        `make_post_reply` does not set `path` at all.

        THE BYSTANDER IS THE WITNESS FOR THE `where` CLAUSE. A mutant dropping
        it would flip every reply in the table, and only a row that should NOT
        have changed catches that.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)
        child = make_post_reply(s.post, s.author)
        bystander = make_post_reply(s.post, s.author)
        db.session.commit()
        child.path = [0, s.reply.id, child.id]
        bystander.path = [0, bystander.id]
        child.replies_enabled = True
        bystander.replies_enabled = True
        db.session.commit()

        lock_post_reply(s.reply.id, True, SRC_API, auth=bearer(s.actor))

        db.session.refresh(child)
        db.session.refresh(bystander)
        assert child.replies_enabled is False
        assert bystander.replies_enabled is True

    def test_the_cascade_keys_on_the_replys_own_id_not_the_posts(self, db_session):
        """`:510`'s `'parent_id': post_reply.id` -- the bound value, not `:509`.

        WHY THE CASCADE TEST ABOVE CANNOT WITNESS THIS. It locks `s.reply`,
        and `_seed_moderated_reply` seeds exactly one post and one reply into
        a database whose sequences tests/conftest.py:131 resets between tests,
        so `s.post.id` and `s.reply.id` are BOTH 1. `ARRAY[:parent_id]` selects
        the same subtree whichever of the two is bound, and Task 8's mutation
        pass confirmed it: changing `:510` to `'parent_id': post_reply.post_id`
        left all 46 tests green.

        WHAT THIS TEST DOES INSTEAD. It locks a SECOND reply, so the locked
        reply's id (2) and its `post_id` (1) differ, and it seeds two
        descendants that separate the two keys: `child`'s path contains 2 and
        `decoy`'s contains 1. Under the correct binding `child` locks and
        `decoy` does not; under the mutant exactly the reverse, so both
        assertions flip rather than one. `:508` sets `replies_enabled` on the
        locked reply in Python, so the target itself is no witness at all --
        only a row reached through the raw UPDATE is.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)
        target = make_post_reply(s.post, s.author)
        child = make_post_reply(s.post, s.author)
        decoy = make_post_reply(s.post, s.author)
        db.session.commit()
        assert target.id != target.post_id
        child.path = [0, target.id, child.id]
        decoy.path = [0, s.post.id, decoy.id]
        child.replies_enabled = True
        decoy.replies_enabled = True
        db.session.commit()

        lock_post_reply(target.id, True, SRC_API, auth=bearer(s.actor))

        db.session.refresh(child)
        db.session.refresh(decoy)
        assert child.replies_enabled is False
        assert decoy.replies_enabled is True

    def test_an_instance_admin_may_lock(self, db_session):
        """`:507`'s SECOND disjunct alone, with the first false."""
        s = _seed_moderated_reply()
        make_instance_admin(s.actor, s.instance)
        s.reply.replies_enabled = True
        db.session.commit()

        lock_post_reply(s.reply.id, True, SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.reply)
        assert s.reply.replies_enabled is False

    def test_an_unprivileged_api_caller_is_refused(self, db_session):
        """`:507`'s false arm into `:524-525`'s refusal.

        THIS TEST WAS CALLED `test_an_unprivileged_api_caller_is_not_refused`
        AND ASSERTED THE OPPOSITE ON PURPOSE. Its claim was: "Neither disjunct
        holds, so the whole body is skipped and control reaches `:525`. The
        call returns `user.id, post_reply` normally: no exception, no flash,
        nothing changed. The caller cannot tell this apart from a success."
        That was an accurate description of a live defect when Task 4 wrote
        it. IT IS NO LONGER TRUE, and the name went with the behaviour rather
        than being left to contradict the body: a test name is a claim, and
        `is_not_refused` in green pytest output would assert a defect that no
        longer exists. Registered as finding 3 in the round's spec; closed by
        Task 7.

        THE FIX WAS TRANSCRIBED FROM THE TWIN, not invented here.
        `app/shared/post.py:968-969` has carried
        `elif src == SRC_API: raise Exception('Does not have permission')`
        since PC2 in sub-project 36, and `move_post:999-1000` carries it too;
        `tests/test_shared_post_moderation.py`'s
        `test_an_unprivileged_user_changes_nothing` pins that shape there.
        `lock_post_reply` was simply left behind. Task 7 copied the two lines
        across, which is why `:524-525` reads identically to `:968-969`.

        WHAT THIS TEST NOW WITNESSES. `s.actor` is neither a moderator of the
        community nor an instance admin, so `:507` is false and `:524`'s
        `src == SRC_API` is true: the call raises rather than returning. The
        state assertions are KEPT BELOW THE RAISE AND ARE NOT REDUNDANT WITH
        IT -- the raise alone would still pass against a mutant that performed
        the lock and then raised, so `replies_enabled` staying True and the
        ModLog staying empty are what prove nothing happened before the
        refusal.
        """
        s = _seed_moderated_reply()
        s.reply.replies_enabled = True
        db.session.commit()

        with pytest.raises(Exception, match='Does not have permission'):
            lock_post_reply(s.reply.id, True, SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.reply)
        assert s.reply.replies_enabled is True
        assert db.session.query(ModLog).count() == 0

    def test_an_unprivileged_web_caller_is_ignored_rather_than_refused(self, db_session, app):
        """`:524`'s FALSE arm -- the one arc Task 7's fix left uncovered.

        Task 7 turned `:507`'s bare `if` into an `if`/`elif` pair so an
        unauthorized API caller is refused instead of receiving the unchanged
        object. That added two statements and two arcs, and the test above
        covers `:524`'s true arm. ITS FALSE ARM WAS NEVER REACHED: Task 8's
        coverage re-run over the 46 tests that existed then reported
        `missing_branches` `[[518, 521], [545, 548]]` for this module, the
        only two arcs left anywhere in these six functions.

        WHAT THE ARC IS. The caller here is neither a moderator nor an instance
        admin AND is on the web path, so `:507` is false and `:524` is false
        too, and control falls to `:527`, which is also false: the call returns
        None having done nothing, with no flash and no exception.

        IT IS NOT A SECOND DEFECT BECAUSE IT IS THE REGISTERED ONE -- NOT
        BECAUSE IT IS SAFE. An earlier wording of this docstring said the
        silence was "deliberate rather than the silent fall-through Task 7
        fixed -- the web routes guard before they dispatch, whereas the API
        path reaches this function as its first check". THE "web routes guard
        before they dispatch" CLAUSE IS FALSE, and the "deliberate rather than"
        framing rested entirely on it; both are retracted. (The clause about the
        API path is true and stands.) `post_reply_lock`
        (`app/post/routes.py:1668-1671`)
        carries `@login_required` and nothing else, and its body is one call to
        `lock_post_reply` with `SRC_WEB`; `post_reply_collapse` (`:1675-1678`)
        is the same shape. NO AUTHORIZATION RUNS BEFORE THIS FUNCTION ON THE WEB
        PATH, so what this test drives is not a redundant no-op behind a guard
        -- it is the route's only authorization, and it lets ANY LOGGED-IN USER
        LOCK ANY COMMENT. It is the same defect Task 7 fixed for `SRC_API`, left
        standing on the web half out of faithfulness to the twin
        (`app/shared/post.py:968-969`), and it is registered OPEN as D524. The
        class docstring above carries the full re-derivation and the contrast
        with `post_reply_choose_answer`, which does guard.

        WHAT THE TEST IS STILL FOR, unchanged by that retraction: asserting
        `None` plus an unchanged row plus an empty flash queue is what
        distinguishes "did nothing" from "did something and said nothing", and
        it is what keeps D524 witnessed by a test rather than merely asserted in
        a register. The test's NAME is the honest one --
        `is_ignored_rather_than_refused` -- and it should stay that way until
        the behaviour changes.
        """
        from flask import get_flashed_messages
        s = _seed_moderated_reply()
        s.reply.replies_enabled = True
        db.session.commit()

        with web_ctx(app, s.actor):
            result = lock_post_reply(s.reply.id, True, SRC_WEB, auth=None)
            messages = get_flashed_messages()

        assert result is None
        assert messages == []
        db.session.refresh(s.reply)
        assert s.reply.replies_enabled is True
        assert db.session.query(ModLog).count() == 0

    def test_locking_writes_the_lock_action(self, db_session):
        """`:512-514`'s add_to_modlog with `:502`'s modlog_type.

        EVERY ARGUMENT IS ASSERTED, for the reason
        `TestModRemoveReply::test_the_modlog_row_names_the_delete_action`
        gives at length. `:512-514`'s argument list differs from the two
        mod-delete calls in two ways worth pinning rather than glossing:
        there is NO `post=`, so `post_id` is None rather than the post's id;
        and `reason` is the LITERAL `''` rather than a parameter, because
        locking a comment takes no reason from the caller. `link_text` is the
        reply's own body through `shorten_string`, not the post's title.

        The row is selected by ACTION rather than by `rows[0]`, for the reason
        that test now gives: the set assertion above does not constrain
        `len(rows)`, so an index is an ordered access over planner-returned
        rows and this file's own rule forbids it.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)
        target = make_post_reply(s.post, s.author)
        db.session.commit()
        assert target.id != s.post.id

        lock_post_reply(target.id, True, SRC_API, auth=bearer(s.actor))

        rows = db.session.query(ModLog).all()
        assert {row.action for row in rows} == {'lock_post_reply'}
        row = next(r for r in rows if r.action == 'lock_post_reply')
        assert row.user_id == s.actor.id
        assert row.type == 'mod'
        assert row.target_user_id == s.author.id
        assert row.reason == ''
        assert row.community_id == s.community.id
        assert row.post_id is None
        assert row.reply_id == target.id
        assert row.link == f'post/{s.post.id}#comment_{target.id}'
        assert row.link_text == 'a reply'

    def test_unlocking_writes_the_unlock_action(self, db_session):
        """`:505`'s modlog_type on the false arm of `:500`.

        The counterpart of the test above. Together they prove `:512`'s
        argument is driven by `:500` rather than fixed.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)

        lock_post_reply(s.reply.id, False, SRC_API, auth=bearer(s.actor))

        actions = {row.action for row in db.session.query(ModLog).all()}
        assert actions == {'unlock_post_reply'}

    def test_the_web_arm_flashes_when_locking(self, db_session, app):
        """`:493`'s false arm, `:516`'s true arm, `:517`-`:518`.

        NO `make_site()` HERE -- see the class docstring. This arm only
        flashes; it neither renders a template nor calls `can_downvote`.
        """
        from flask import get_flashed_messages
        s = _seed_moderated_reply()
        seed_moderator(s)

        with web_ctx(app, s.actor):
            lock_post_reply(s.reply.id, True, SRC_WEB, auth=None)
            messages = get_flashed_messages()

        assert 'Comment has been locked.' in messages

    def test_the_web_arm_flashes_a_different_message_when_unlocking(self, db_session, app):
        """`:516`'s false arm, `:521`-`:522`.

        Paired with the test above so `:516`'s two arms are witnessed by
        different message text rather than by the same assertion twice.
        """
        from flask import get_flashed_messages
        s = _seed_moderated_reply()
        seed_moderator(s)

        with web_ctx(app, s.actor):
            lock_post_reply(s.reply.id, False, SRC_WEB, auth=None)
            messages = get_flashed_messages()

        assert 'Comment has been unlocked.' in messages

    def test_the_api_arm_selects_the_lock_task_without_flashing(self, db_session):
        """`:517`'s false arm and `:519`'s task_selector.

        `:517` guards only the flash; `:519` runs on both arms of it. The
        API call reaches `:519` with `:517` false, which is the arc no web
        test can take.
        """
        s = _seed_moderated_reply()
        seed_moderator(s)

        with recording_task_selector() as calls:
            lock_post_reply(s.reply.id, True, SRC_API, auth=bearer(s.actor))

        assert 'lock_post_reply' in calls

    def test_the_api_arm_selects_the_unlock_task(self, db_session):
        """`:521`'s false arm and `:523`'s task_selector."""
        s = _seed_moderated_reply()
        seed_moderator(s)

        with recording_task_selector() as calls:
            lock_post_reply(s.reply.id, False, SRC_API, auth=bearer(s.actor))

        assert 'unlock_post_reply' in calls


class TestSetCollapsePostReply:
    """`set_collapse_post_reply` (app/shared/reply.py:531-555).

    THE SECOND SILENT-FAILURE FUNCTION UNTIL TASK 7, AND THAT LABEL IS NOW
    RETRACTED. The class docstring through Task 6 called this "THE SECOND
    SILENT-FAILURE FUNCTION" on the strength of `:537`'s guard (as it was then
    numbered) having no `else`. Task 7 transcribed the twin module's repair
    here as well, so the guard -- now at `:539` -- carries
    `elif src == SRC_API: raise Exception('Does not have permission')` at
    `:551-552`, and an unauthorized API caller is refused rather than handed
    back an unchanged object. The function opened at `:529` and every line in
    it moved by two when `lock_post_reply` grew; all citations below were
    re-derived at Task 7's commit.

    THAT REFUSAL IS CORRECT BUT UNREACHABLE FROM PRODUCTION TODAY, AND THAT
    DISTINGUISHES IT FROM `lock_post_reply`'s. `/usr/bin/grep -rn
    set_collapse_post_reply app/` finds exactly one caller,
    app/post/routes.py:1678, and it passes `SRC_WEB`; there is no `SRC_API`
    caller anywhere in `app/`. `lock_post_reply` by contrast IS reached with
    `SRC_API`, from app/api/alpha/utils/reply.py:755, so its refusal changes
    live behaviour and this one does not -- yet. It was propagated anyway,
    because the divergence between the twins is the defect, and a guard that
    is right only until someone adds the endpoint is not a guard. The tests
    below reach it directly, which is the only way it is reachable at all.

    ITS GUARD STILL DIFFERS FROM `lock_post_reply`'s BY ONE DISJUNCT, which is
    a separate finding and is NOT fixed: `:539` is
    `is_moderator or is_instance_admin or user.is_admin_or_staff()` where
    `:507` has only the first two. So a site admin who is not a moderator can
    make a comment collapsible but cannot lock it -- finding 2's second
    instance, and `test_a_site_admin_who_is_neither_may_collapse` below is
    paired with `TestLockPostReply.test_an_unprivileged_api_caller_is_refused`
    to witness it: same role, opposite outcome, one line apart in the module.
    (That cross-reference named `..._is_not_refused` until Task 7 renamed it
    with the behaviour; the pairing itself is unchanged, except that the lock
    side now raises instead of returning silently.)

    `:546` and `:550` are COMMENTED-OUT `task_selector` calls, so this
    function federates nothing. A test asserting an empty recorder would
    witness the comment rather than the code; none is written here.

    THE COLUMN DEFAULT IS A FALSE-WITNESS TRAP HERE AND EVERY TEST BELOW
    SEEDS AROUND IT. `PostReply.collapsible` defaults to **True**
    (app/models.py:2931) and `PostReply.new` sets it to
    `user.id != post.user_id` (`:3006`), so a test that asserts
    `collapsible is True` without seeding False first would pass with `:540`
    deleted -- mechanism (a), asserting on state something else set
    unconditionally. Every test here writes the opposite value before acting.

    NO `make_site()` ANYWHERE IN THIS CLASS, and not by cargo-culting the
    module docstring's rule -- verified directly for this function: it
    flashes and returns, calling neither `render_template` nor
    `can_downvote`. This module's only `render_template` calls are at `:65`
    and `:145` and its only `can_downvote` calls at `:24` and `:38`, all in
    Group A and none reachable from here.
    """

    def test_a_moderator_makes_a_reply_collapsible(self, db_session):
        """`:539`'s true arm via `is_moderator`, `:540`'s assignment."""
        s = _seed_moderated_reply()
        seed_moderator(s)
        s.reply.collapsible = False
        db.session.commit()

        user_id, reply = set_collapse_post_reply(s.reply.id, True, SRC_API,
                                                 auth=bearer(s.actor))

        assert user_id == s.actor.id
        db.session.refresh(s.reply)
        assert s.reply.collapsible is True

    def test_clearing_collapsible_sets_it_back_to_false(self, db_session):
        """`:540` with the other argument, and `:547`'s else arm."""
        s = _seed_moderated_reply()
        seed_moderator(s)
        s.reply.collapsible = True
        db.session.commit()

        set_collapse_post_reply(s.reply.id, False, SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.reply)
        assert s.reply.collapsible is False

    def test_an_instance_admin_may_collapse(self, db_session):
        """`:539`'s SECOND disjunct alone."""
        s = _seed_moderated_reply()
        make_instance_admin(s.actor, s.instance)
        s.reply.collapsible = False
        db.session.commit()

        set_collapse_post_reply(s.reply.id, True, SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.reply)
        assert s.reply.collapsible is True

    def test_a_site_admin_who_is_neither_may_collapse(self, db_session):
        """`:539`'s THIRD disjunct -- the one `lock_post_reply:507` lacks.

        Paired with `TestLockPostReply.test_an_unprivileged_api_caller_is_refused`:
        the same role succeeds here and is REFUSED there, which is the
        divergence finding 2 registers. The pairing read "silently does
        nothing there" until Task 7; after Task 7's fix the lock side raises,
        so the divergence is now visible to the caller rather than silent --
        but it is still a divergence, and finding 2 is still open.
        """
        s = _seed_moderated_reply()
        make_site_admin(s.actor)
        s.reply.collapsible = False
        db.session.commit()

        set_collapse_post_reply(s.reply.id, True, SRC_API, auth=bearer(s.actor))

        db.session.refresh(s.reply)
        assert s.reply.collapsible is True

    def test_an_unprivileged_api_caller_is_refused_either(self, db_session):
        """`:539`'s false arm into `:551-552`'s refusal.

        THIS TEST WAS CALLED
        `test_an_unprivileged_api_caller_is_not_refused_either` AND ASSERTED
        THE OPPOSITE ON PURPOSE. Its claim was: "Neither disjunct holds, so
        the whole body is skipped and control reaches `:550`. The call returns
        `user.id, post_reply` normally: no exception, nothing changed. The
        caller cannot tell this apart from a success." That described a live
        defect accurately when Task 5 wrote it; it is stale now, and the name
        was changed with it rather than left to contradict the body.
        Registered as finding 3's other instance; closed by Task 7.

        THE FIX WAS TRANSCRIBED FROM THE TWIN, not invented here.
        `app/shared/post.py:968-969` and `move_post:999-1000` have carried
        `elif src == SRC_API: raise Exception('Does not have permission')`
        since PC2 in sub-project 36. Task 7 turned `:539`'s bare `if` into an
        `if`/`elif` pair by copying those two lines in at `:551-552`, after
        the guarded block and before `:554`'s return.

        WHAT THIS TEST NOW WITNESSES. `s.actor` satisfies none of `:539`'s
        three disjuncts, so `:551`'s `src == SRC_API` is reached and the call
        raises. THE STATE ASSERTION IS KEPT BELOW THE RAISE and is not
        redundant with it: `collapsible` is seeded False and asserted False,
        and `:541`'s commit is the only write in the body, so a mutant that
        collapsed the comment and then raised would satisfy `pytest.raises`
        alone and is caught only here. The returned tuple would discriminate
        nothing -- `:554`-`:555` return the same shape on every arm that
        returns.
        """
        s = _seed_moderated_reply()
        s.reply.collapsible = False
        db.session.commit()

        with pytest.raises(Exception, match='Does not have permission'):
            set_collapse_post_reply(s.reply.id, True, SRC_API,
                                    auth=bearer(s.actor))

        db.session.refresh(s.reply)
        assert s.reply.collapsible is False

    def test_an_unprivileged_web_caller_is_ignored_here_too(self, db_session, app):
        """`:551`'s FALSE arm -- the second of the two arcs Task 7 left open.

        The mirror of
        `TestLockPostReply::test_an_unprivileged_web_caller_is_ignored_rather
        _than_refused`, and uncovered for the same reason: `:551`'s true arm
        has a test and its false arm had none, so Task 8's coverage run
        reported `[[518, 521], [545, 548]]` as this module's only missing
        arcs. `:539` is false for this caller and `src` is `SRC_WEB`, so
        `:554` is false as well and the call returns None having written
        nothing.
        """
        from flask import get_flashed_messages
        s = _seed_moderated_reply()
        s.reply.collapsible = False
        db.session.commit()

        with web_ctx(app, s.actor):
            result = set_collapse_post_reply(s.reply.id, True, SRC_WEB, auth=None)
            messages = get_flashed_messages()

        assert result is None
        assert messages == []
        db.session.refresh(s.reply)
        assert s.reply.collapsible is False

    def test_the_web_arm_flashes_the_collapsible_message(self, db_session, app):
        """`:532`'s false arm, `:543`'s true arm, `:544`-`:545`.

        NO `make_site()` HERE -- see the class docstring. This arm only
        flashes; it neither renders a template nor calls `can_downvote`.
        """
        from flask import get_flashed_messages
        s = _seed_moderated_reply()
        seed_moderator(s)
        s.reply.collapsible = False
        db.session.commit()

        with web_ctx(app, s.actor):
            set_collapse_post_reply(s.reply.id, True, SRC_WEB, auth=None)
            messages = get_flashed_messages()

        assert 'Comment is collapsible.' in messages

    def test_the_web_arm_binds_the_user_it_then_tests_for_admin(self, db_session, app):
        """`:536`'s `user = current_user`, witnessed through `:539`'s THIRD
        disjunct rather than its first.

        WHY THE OTHER WEB TESTS CANNOT WITNESS `:536`. Both of them make the
        caller a MODERATOR, and `Community.is_moderator` (app/models.py:736-740)
        falls back to `current_user.get_id()` when it is handed `None`. So on
        those two tests `user = None` computes the same answer as
        `user = current_user`, and `:536` is executed without being observed.
        Task 8's mutation pass confirmed it: `user = None` at `:536` left all
        46 tests green, while the identical mutation at `lock_post_reply:497`
        died, because that function goes on to read `user.id` for
        `task_selector`. This one never reads `user.id` on the web arm at all
        -- `:555`'s `user.id` is the API arm -- so the binding is observable
        only through the one disjunct that takes the user as a receiver rather
        than an argument.

        A SITE ADMIN IS THE ONLY CALLER THAT REACHES IT. `user.
        is_admin_or_staff()` at `:539` is evaluated only when both community
        checks are false, so the caller must be neither a moderator nor an
        instance admin.

        WHAT THIS TEST UNIQUELY KILLS, since it closes no statement and no arc
        that another test does not. Restricting the third disjunct to the API
        path -- `:539`'s tail rewritten as
        `or (user.is_admin_or_staff() and src == SRC_API)` -- is a plausible
        fault and a real change of behaviour, and it is invisible to every
        other test in this file: with this one deselected, 50 pass against it.
        It is the shape a careless narrowing of Task 7's API-side work would
        take.

        THE `user = None` KILL IS NOT THE UNIQUE ONE, and this paragraph says
        so rather than letting the reader infer otherwise.
        `test_an_unprivileged_web_caller_is_ignored_here_too` kills that mutant
        as well, by crashing in `is_instance_admin`, and it also kills the
        non-crashing variant that binds some other real user -- but only by
        accident of Probe C above: `s.author` is user id 1, and
        `User.is_admin()` short-circuits on `self.id == 1` at
        app/models.py:1260, so binding the author accidentally binds an admin.
        """
        from flask import get_flashed_messages
        s = _seed_moderated_reply()
        make_site_admin(s.actor)
        s.reply.collapsible = False
        db.session.commit()

        with web_ctx(app, s.actor):
            set_collapse_post_reply(s.reply.id, True, SRC_WEB, auth=None)
            messages = get_flashed_messages()

        db.session.refresh(s.reply)
        assert s.reply.collapsible is True
        assert 'Comment is collapsible.' in messages

    def test_the_web_arm_flashes_the_other_message_when_clearing(self, db_session, app):
        """`:543`'s false arm, `:548`-`:549`."""
        from flask import get_flashed_messages
        s = _seed_moderated_reply()
        seed_moderator(s)
        s.reply.collapsible = True
        db.session.commit()

        with web_ctx(app, s.actor):
            set_collapse_post_reply(s.reply.id, False, SRC_WEB, auth=None)
            messages = get_flashed_messages()

        assert 'Comment will not be collapsed when loading the post.' in messages


class TestChooseAnswer:
    """`choose_answer` (app/shared/reply.py:558-586) and `unchoose_answer`
    (`:589-602`). Both ranges moved by four at Task 7, which inserted two
    lines into each of the two functions above them in the module; the extents
    read `:554-582` and `:585-598` through Task 6.

    NEITHER FUNCTION CONTAINS A PERMISSION CHECK, AND THAT IS STILL TRUE AND
    STILL DELIBERATE. Both establish `user` from the source fork and then act.
    What changed at Task 7 is the API ENTRY POINT, not these two verbs.

    THE PARAGRAPH THAT STOOD HERE THROUGH TASK 6 IS RETRACTED. It read: "but
    the API path does not: app/api/alpha/routes.py:983 calls
    `post_reply_mark_as_answer` (app/api/alpha/utils/reply.py:687-697), which
    calls `authorise_api_user` and dispatches straight through." That was an
    accurate trace of a live authorization hole. Task 7 closed it.
    `post_reply_mark_as_answer` is now app/api/alpha/utils/reply.py:687-722,
    and at `:694-698` it loads the reply and the caller and refuses unless
    `user.is_admin_or_staff() or reply.user_id == user.id or
    reply.community.is_moderator(user)` -- the web route's three-way guard at
    app/post/routes.py:2443, mirrored. `is_moderator` is passed `user`
    EXPLICITLY: its signature is `is_moderator(self, user=None)`
    (app/models.py:736) and the `None` default reads `current_user`, which
    does not exist on the API path.

    WHAT REMAINS TRUE: `authorise_api_user` establishes WHO the caller is and
    says nothing about what they may do, and `choose_answer` /
    `unchoose_answer` remain plain verbs that check nothing. Calling either
    one directly, as most tests in this class do, still bypasses all
    authorization -- by design, so that both entry points guard in one place
    each rather than the verb guarding twice.

    `force_locale(get_recipient_language(post_reply.user_id))` wraps the
    title at `:566`; no `Language` row is needed for that path -- Task 1's
    Probe A (this file's module docstring) established that `make_user`
    leaves `language_id` and `interface_language` unset, so
    `get_recipient_language` takes the `'en'` default arm and never queries
    `Language`.

    NO `make_site()` HERE, for the same reason as the other classes in this
    file: neither function calls `render_template` or `can_downvote` -- the
    module's only such calls are Group A's, at `:65`/`:145` and `:24`/`:38`.
    """

    def test_choosing_an_answer_sets_the_flag_and_notifies_the_author(self, db_session):
        """`:564`-`:581` -- the flag, the Notification and the unread counter.

        THREE ASSERTIONS BECAUSE `:565` ALONE WITNESSES ALMOST NOTHING: a
        mutant deleting `:574`-`:580` leaves `answer` true and the test
        green. The notification's `user_id` is asserted to be the AUTHOR's
        rather than the actor's, which is what `:575` claims and what a
        mutant swapping the two operands would break.
        """
        s = _seed_moderated_reply()
        s.author.unread_notifications = 4
        db.session.commit()

        user_id, reply = choose_answer(s.reply.id, SRC_API, auth=bearer(s.actor))

        assert user_id == s.actor.id
        db.session.refresh(s.reply)
        db.session.refresh(s.author)
        assert s.reply.answer is True
        assert s.author.unread_notifications == 5
        notifications = db.session.query(Notification).all()
        assert {n.user_id for n in notifications} == {s.author.id}
        assert {n.author_id for n in notifications} == {s.actor.id}

    def test_the_notification_carries_the_title_url_subtype_and_targets(self, db_session):
        """`:566`-`:578` -- everything the Notification is built OUT OF.

        THE TEST ABOVE ASSERTS WHO IS NOTIFIED; THIS ONE ASSERTS WHAT THEY ARE
        SENT, and the two were not the same claim. Task 8's mutation pass put
        one mutation on each of `:567`, `:568`, `:569`, `:570`, `:571`, `:572`,
        `:573`, `:574` and `:577` -- the message text, both `shorten_string`
        limits, `'gen'`, `post_id`, `requestor_id`, `author_user_name`, the
        notification `url` and the `subtype` -- and ALL NINE SURVIVED against
        the 46 tests that existed then. `user_id` and `author_id` were the only
        two fields any test looked at. Nine surviving mutants on one contiguous
        block is what a hole looks like from the inside.

        THE POST TITLE IS SEEDED LONG ON PURPOSE. `shorten_string`
        (app/utils.py:1610-1617) returns its input unchanged when it is no
        longer than the limit, so against `_seed_moderated_reply`'s six-
        character `'a post'` the 100 at `:568` and `:573` is unobservable: 100
        and 10 and 50 all produce `'a post'`. At 134 characters the limit is
        load-bearing, and the expected value is spelled as the slice rather
        than by calling `shorten_string` again, so that a mutant inside
        `shorten_string` could not agree with the test by construction.

        `slug` IS SEEDED because `make_post` does not set it
        (tests/factories.py:331-345 lists the columns it does), and `url=None`
        would let `:574` bind anything else nullable and still pass.

        A SECOND REPLY IS THE ANSWER, not `s.reply`, for the reason
        `TestLockPostReply.test_the_cascade_keys_on_the_replys_own_id_not_the
        _posts` spells out: `_seed_moderated_reply` leaves `s.post.id` and
        `s.reply.id` both 1, so `:570`'s `post_reply.post_id` and a mutant's
        `post_reply.id` agree. That mutant survived the first version of this
        test for exactly that reason. Marking reply 2 separates them, and the
        assertion above the call refuses to let the separation regress
        silently.
        """
        s = _seed_moderated_reply()
        answer = make_post_reply(s.post, s.author)
        title = 'an unusually long question title that must be shortened ' + 'q' * 78
        assert len(title) == 134
        s.post.title = title
        s.post.slug = '/c/moderation@local.example/p/1/an-unusually-long'
        db.session.commit()
        assert answer.id != answer.post_id

        choose_answer(answer.id, SRC_API, auth=bearer(s.actor))

        notify = db.session.query(Notification).one()
        assert notify.title == 'Your answer was chosen as an answer to ' + title[:97] + '…'
        assert notify.url == '/c/moderation@local.example/p/1/an-unusually-long'
        assert notify.notif_type == NOTIF_ANSWER
        assert notify.subtype == 'answer_chosen'
        assert notify.targets == {
            'gen': '0',
            'post_id': s.post.id,
            'requestor_id': s.actor.id,
            'author_user_name': s.author.display_name(),
            'post_title': title[:97] + '…',
        }

    def test_unchoosing_clears_the_flag_and_notifies_nobody(self, db_session):
        """`:595`-`:597`, and the absence of a notification.

        The positive control for the emptiness is the test above: it proves
        a Notification CAN be written by this fixture, so the zero here is
        `unchoose_answer` not writing one rather than a broken seed.
        """
        s = _seed_moderated_reply()
        s.reply.answer = True
        db.session.commit()

        user_id, reply = unchoose_answer(s.reply.id, SRC_API, auth=bearer(s.actor))

        assert user_id == s.actor.id
        db.session.refresh(s.reply)
        assert s.reply.answer is False
        assert db.session.query(Notification).count() == 0

    def test_an_unrelated_api_user_may_not_mark_a_comment_as_the_answer(self, db_session):
        """app/api/alpha/utils/reply.py:694-698's guard, the API side of
        app/post/routes.py:2443.

        THIS TEST WAS CALLED
        `test_any_authenticated_api_user_may_mark_any_comment_as_the_answer`
        AND ASSERTED A LIVE AUTHORIZATION DEFECT ON PURPOSE. Its claim was:
        "The web route would refuse them at app/post/routes.py:2443. The API
        path does not check at all, so the call succeeds and the comment is
        marked as the accepted answer by someone with no relationship to it."
        That was true when Task 6 wrote it. Task 7 closed the hole, so the
        claim is retracted and the name went with the behaviour -- a test
        named `may_mark_any_comment` passing green would assert a defect that
        no longer exists.

        IT ALSO CHANGED WHAT IT CALLS, as its own predecessor required.
        `choose_answer` is still a plain verb with no permission check, by
        design; the refusal lives one level up, in `post_reply_mark_as_answer`
        -- which is what `app/api/alpha/routes.py:983` actually calls. So this
        test exercises the WRAPPER. Calling `choose_answer` directly here
        would witness nothing, because nothing was added to it.

        THE GUARD IS THE WEB ROUTE'S, MIRRORED, not a new policy:
        `user.is_admin_or_staff() or reply.user_id == user.id or
        reply.community.is_moderator(user)`. `user` is passed to
        `is_moderator` EXPLICITLY because its default is `current_user`
        (app/models.py:736) and there is no `current_user` on the API path.

        `stranger` satisfies none of the three disjuncts: not the reply's
        author, not a moderator of its community, not an instance admin, not
        site staff. THE STATE ASSERTION IS KEPT BELOW THE RAISE -- `answer`
        must still be False, which is what catches a mutant that marked the
        answer and then refused. The return value would witness nothing:
        `:585`-`:586` return the same shape whoever calls.

        `answer` IS SEEDED False EXPLICITLY rather than left to
        `PostReply.answer`'s column default (app/models.py:2929,
        `default=False`), so the assertion below witnesses the refusal and not
        a default nobody wrote. The two positive controls beneath this test
        seed it the same way.
        """
        s = _seed_moderated_reply()
        stranger = make_user(s.instance, 'stranger', local=True)
        s.reply.answer = False
        db.session.commit()

        with pytest.raises(Exception, match='Does not have permission'):
            post_reply_mark_as_answer(bearer(stranger),
                                      {'comment_reply_id': s.reply.id,
                                       'answer': True})

        db.session.refresh(s.reply)
        assert s.reply.answer is False

    def test_a_moderator_may_mark_a_comment_as_the_answer(self, db_session):
        """THE POSITIVE CONTROL FOR `reply.community.is_moderator(user)`, the
        third disjunct of app/api/alpha/utils/reply.py:696-697.

        WITHOUT A POSITIVE CONTROL THE GUARD ABOVE IS UNPINNED IN THE
        REGRESSION DIRECTION. Task 7's review rewrote the whole guard as
        `if True: raise` -- refusing every caller and breaking the feature
        outright -- and 168 tests passed, because the refusal test was the
        only caller of `post_reply_mark_as_answer` in the suite and exercises
        all three disjuncts in lockstep, all false. That is false-witness
        mechanism (c): emptiness with no same-mechanism positive control. This
        test and the one below close it, and the mutation was re-run against
        them; see task-7-report.md.

        ONLY THE THIRD DISJUNCT HOLDS HERE, which is what makes it a witness
        for that disjunct rather than for the guard as a lump. `s.actor` is
        user id 2, so `is_admin()`'s `self.id == 1` short-circuit
        (app/models.py:1260) does NOT fire for it and no role is granted, so
        disjunct 1 is false; the reply is authored by `s.author`, not
        `s.actor`, so disjunct 2 is false. `seed_moderator` supplies the
        third. It goes through the WRAPPER, not `choose_answer`, because the
        wrapper is where the guard lives.

        `g.admin_ids` IS SET BECAUSE THE WRAPPER RENDERS A VIEW after it
        dispatches, and `reply_view` reads `g.admin_ids` unconditionally. A
        real request gets that from a `before_request` hook; calling the util
        directly skips it, exactly as `tests/test_api_post_bookmarks.py:13-18`
        records for `post_view`. Empty, because no seeded user here is an
        admin. The refusal test above needs no such setup -- it raises before
        reaching any view.
        """
        from flask import g
        s = _seed_moderated_reply()
        seed_moderator(s)
        s.reply.answer = False
        db.session.commit()
        g.admin_ids = []

        post_reply_mark_as_answer(bearer(s.actor),
                                  {'comment_reply_id': s.reply.id,
                                   'answer': True})

        db.session.refresh(s.reply)
        assert s.reply.answer is True

    def test_the_replys_own_author_may_mark_it_as_the_answer(self, db_session):
        """THE POSITIVE CONTROL FOR `reply.user_id == user.id`, the second
        disjunct of app/api/alpha/utils/reply.py:696.

        A SECOND DISJUNCT IS EXERCISED SEPARATELY so that a later change
        dropping either one is visible. With only one positive control, a
        guard narrowed from three disjuncts to one would still pass.

        THE REPLY IS AUTHORED BY `s.actor`, NOT BY `s.author`, and that is
        load-bearing rather than incidental: `_seed_moderated_reply` mints
        `author` first, so `s.author.id == 1` and `is_admin()` returns True
        from its `self.id == 1` short-circuit (app/models.py:1260, the
        module docstring's Probe C). Marking `s.reply` as its own author
        would therefore satisfy disjunct 1 as well and witness neither
        cleanly. `s.actor` is id 2, is not a moderator here (no
        `seed_moderator` call) and holds no role, so ONLY disjunct 2 holds.

        `g.admin_ids` is set for the reason the test above gives.
        """
        from flask import g
        s = _seed_moderated_reply()
        own_reply = make_post_reply(s.post, s.actor)
        db.session.commit()
        own_reply.answer = False
        db.session.commit()
        g.admin_ids = []

        post_reply_mark_as_answer(bearer(s.actor),
                                  {'comment_reply_id': own_reply.id,
                                   'answer': True})

        db.session.refresh(own_reply)
        assert own_reply.answer is True

    def test_the_web_arm_reads_current_user(self, db_session, app):
        """`:559`'s false arm and `:562`, for both functions.

        Returns None on the web arm because `:585` guards the return. NO
        `make_site()` HERE -- see the class docstring.
        """
        s = _seed_moderated_reply()

        with web_ctx(app, s.actor):
            result = choose_answer(s.reply.id, SRC_WEB, auth=None)

        assert result is None
        db.session.refresh(s.reply)
        assert s.reply.answer is True

    def test_the_web_arm_of_unchoose_reads_current_user(self, db_session, app):
        """`:590`'s false arm and `:593`, and `:601`'s guarded return."""
        s = _seed_moderated_reply()
        s.reply.answer = True
        db.session.commit()

        with web_ctx(app, s.actor):
            result = unchoose_answer(s.reply.id, SRC_WEB, auth=None)

        assert result is None
        db.session.refresh(s.reply)
        assert s.reply.answer is False

    def test_both_verbs_select_their_federation_task(self, db_session):
        """`:583` and `:599`.

        One test for both because the two calls are independent and neither
        has a branch; splitting them would add a test without adding a
        witness.
        """
        s = _seed_moderated_reply()

        with recording_task_selector() as calls:
            choose_answer(s.reply.id, SRC_API, auth=bearer(s.actor))
            unchoose_answer(s.reply.id, SRC_API, auth=bearer(s.actor))

        assert 'choose_answer' in calls
        assert 'unchoose_answer' in calls
