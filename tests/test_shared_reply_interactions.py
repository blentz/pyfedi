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
CLEARS THE KEY IN A `finally`. No test in this file does that yet -- nothing
written so far reaches `vote_for_reply` -- so `_clear_votes_cast` below is
placed for the later tasks of this round rather than used by any test
currently here. The pattern and the reason are
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
    EMPTY tuple, which is a different input to the raw SQL `IN` clause from a
    populated one. `child_count` (app/models.py:2899) is likewise unset by the
    factory, but has a column default of 0 rather than None.

  - ANY WEB ARM THAT RENDERS A TEMPLATE NEEDS A `Site` ROW WITH id 1, and
    `_seed_reply` does not seed one. `subscribe_reply:131` is the first
    statement in this round to render, and it fails without the row --
    `AttributeError: 'NoneType' object has no attribute 'default_theme'`, not a
    skipped lookup. The chain is app/utils.py:75 (this codebase's own
    `render_template`, which wraps Flask's) -> `current_theme()`
    (app/utils.py:3228-3240). `:3230`'s `if hasattr(g, 'site')` is FALSE under
    `web_ctx`, because `test_request_context` never runs `before_request`, so
    `:3233` falls back to `Site.query.get(1)` and `:3238` dereferences the None
    it gets. Tests that render call `make_site()` (tests/factories.py:353)
    themselves; the bookmark tests above do not, because `bookmark_reply` and
    `remove_bookmark_reply` flash and fall off the end of the function without
    rendering anything. `subscribe_reply` additionally has two statements no
    production source value can reach -- see `TestSubscribeReply`'s docstring
    for what `:98` does to the web arm.

TWO DEFECTS ARE PINNED HERE AND DELIBERATELY NOT FIXED. `restore_reply:279-280`
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
`if src == SRC_API:` arm, the `else` at `:26-28` having no equivalent. NEITHER IS IN THE CAMPAIGN REGISTER
YET -- both are slated for it at this round's end. Until then the argument for
leaving each unfixed lives in
`docs/superpowers/specs/2026-09-12-coverage-reply-ac-40-design.md`, under the
headings that name them, and the tests that pin today's behaviour arrive with
the later tasks of this round rather than in this file.
"""

from datetime import date
from types import SimpleNamespace

import pytest
from flask import get_flashed_messages

from app import db
from app.constants import SRC_API, SRC_PLD, SRC_WEB
from app.models import NotificationSubscription, PostReplyBookmark
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

    NO `Site` ROW IS SEEDED HERE. That is fine for every SRC_API arm and for
    the web arms that only flash, but any test whose call reaches a
    `render_template` must call `make_site()` itself first -- the module
    docstring's WHAT IS NEW gives the failure and the exact chain. It is left
    out of this helper rather than folded in because a `Site` row is read by
    more than the theme lookup (`blocked_phrases`, for one) and seeding it
    unconditionally would change what the existing tests here exercise.
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
    `:119`. The two tests below that exhaust the web arm's state space --
    `..._ignores_the_subscribe_argument...` for the empty state,
    `..._deletes_an_existing_subscription...` for the occupied one -- are
    jointly the evidence for that, and they are why the brief's third and fifth
    tests are not here: both asserted an outcome the web arm cannot produce and
    both were falsified by running. What DOES reach the two flash branches is a
    third source constant, and the last test in this class uses one; the twin
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

        NEITHER SRC_WEB NOR SRC_API CAN REACH `:111`/`:119`, which the class
        docstring derives from `:98` and the two web tests above demonstrate:
        SRC_WEB keeps `subscribe` and `existing_notification` in lockstep, so
        `:103`'s false arm and `:114`'s true arm are jointly unreachable there,
        and under SRC_API `:108`/`:116` always take the raise. A third source
        value is the only way in -- it is (a) not SRC_WEB, so `:97` skips the
        override and the caller's `subscribe` argument survives, and (b) not
        SRC_API, so `:108`/`:116` take the else. SRC_PLD (app/constants.py:94,
        the admin preload path) is used here purely as such a value. IT IS NOT
        HOW THE CODE IS USED IN PRODUCTION -- app/api/alpha/utils/reply.py:462
        passes SRC_API and app/post/routes.py:2098 passes SRC_WEB, and those are
        the only two callers -- and this docstring says so rather than implying
        otherwise. The precedent, down to the constant, is
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
