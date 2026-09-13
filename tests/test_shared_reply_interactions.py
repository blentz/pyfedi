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
CLEARS THE KEY IN A `finally`. No test in this file does that yet -- Task 1
writes none that reach `vote_for_reply` -- so `_clear_votes_cast` below is
placed for the later tasks of this round rather than used by the four tests
here. The pattern and the reason are
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

from app import db
from app.constants import SRC_API, SRC_WEB
from app.models import NotificationSubscription, PostReplyBookmark
from app.shared.reply import (
    bookmark_reply, delete_reply, extra_rate_limit_check, remove_bookmark_reply,
    restore_reply, subscribe_reply, vote_for_reply,
)
from tests.factories import (
    bearer, make_community, make_instance, make_post, make_post_reply,
    make_post_reply_bookmark, make_user, web_ctx,
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
