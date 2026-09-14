"""Group B of app/shared/reply.py -- creating and editing a reply.

    make_reply   :156   42 statements / 22 arcs
    edit_reply   :216   29 / 12

Both were at ZERO coverage when this file was created. Every line number here
was re-derived with numbered output at the commit each task's report names.

WHERE THE HARNESS COMES FROM. `tests/test_shared_reply_interactions.py`
(Groups A and C) and `tests/test_shared_reply_moderation.py` (Groups E and F)
cover the rest of this module; `tests/test_shared_post_make.py` covers the
twins `make_post` (app/shared/post.py:175) and `edit_post` (`:262`).

MAKE_REPLY'S WEIGHT IS `PostReply.new`, NOT `make_reply`. The function itself
is plain control flow over rows -- no upload pipeline, no URL classification.
`PostReply.new` runs the blocked-phrase, gif-reaction and low-effort filters,
builds the `path` array, moves three counters, recomputes
`reply_count_cross_posted`, and raises `PostReplyValidationError` from SEVEN
places, not the five the plan named -- re-enumerated against app/models.py at
the commit this task's report names:

    :2983  'Comments are disabled on this post'  (not post.comments_enabled)
    :2986  'Banned from commenting'               (user.ban_comments)
    :3025  'Blocked phrase in comment'
    :3036  'Replier blocked'
    :3039  'Duplicate reply'
    :3046  'Gif comment ignored'   (gated on site.enable_gif_reply_rep_decrease)
    :3049  'Low quality reply'     (gated on site.enable_this_comment_filter)

PROBE A's finding, for Task 4: both `enable_gif_reply_rep_decrease` and
`enable_this_comment_filter` are `db.Column(db.Boolean, default=False)`
(app/models.py:3967,3969) -- NEITHER defaults True. And when no Site row
exists, `:3041-3043` builds `site = Site()` WITHOUT adding or flushing it, so
its boolean columns hold Python `None` (the ORM applies a column `default=`
at flush time, not at bare construction) -- also falsy. So the Gif and
Low-quality raises are unreachable from a factory seed unless a test builds
a Site row and explicitly sets the flag True; every other one of the seven
is reachable with plain factory rows.

PROBE B's finding: `make_reply` calls `user_ip_banned()` at `:174` and
`ip_address()` directly at `:201`. Both resolve to the SAME function --
app/shared/reply.py imports `ip_address` from `app.utils`, which is
`app.utils.ip_address = get_ip_address` (app/utils.py:2308), the identical
function `user_ip_banned` calls internally. Group A already established that
`get_ip_address` catches `RuntimeError` (no app/request context) and returns
`''`. So SRC_API needs no request context at `:174` OR at `:201` -- same
function, same guard, confirmed rather than assumed.

PROBE C's finding: `reply_already_exists` (app/utils.py:2599) counts
non-deleted `post_reply` rows matching `user_id`, `post_id`, `parent_id` AND
`body`. A fixture seeding two replies with different bodies (or different
authors, or different posts) does not trip it by accident; only an identical
resubmission does.

NO `Language` ROW IS NEEDED ANYWHERE IN THIS FILE. `make_user` leaves
`language_id` and `interface_language` as `None`, so `get_recipient_language`
takes its `'en'` branch and `Language.query.get(...)` is structurally
unreachable on that path -- established in sub-project 41 and re-verified by
its reviewer.

PLAN ASSUMPTION FALSIFIED, FIXED HERE: the plan's `edit_reply` payload/form
used `language_id=2`. `edit_reply:238` writes that value straight onto
`reply.language_id`, an FK to `language.id` (app/models.py:2926), and no row
2 exists -- the run failed both tests with
`ForeignKeyViolation: Key (language_id)=(2) is not present in table
"language"`. `language_id` is nullable (no `nullable=False` at :2926), so
both tests below pass `language_id=None` instead; neither asserts on
`language_id`, only on `body` and `edited_at`, so nothing is lost.

EVERY SEEDED ID IS OFFSET, AND THAT IS LOAD-BEARING RATHER THAN TIDY.
Register entry D533 records that sub-project 41's fixture left
`community.id == post.id == author.id == 1`, which hid at least four mutants:
several call sites resolve objects to ids, so a wrong-object-right-id mutation
is invisible. `_seed_for_reply` below burns rows to force the three sequences
apart and asserts they differ.

TASK 4's PLAN NAMED THE WRONG LEVER FOR `:192`/`:193`. Its Step 2 draft set
`s.actor.bot = True` to trip `can_create_post_reply`'s denial. `bot` is never
read anywhere in that function's body (app/utils.py:2546-2578, confirmed by
grep) -- that test would sail straight through `:192` into a real
`PostReply.new` call and never raise. `user.ban_comments`
(app/utils.py:2550) is the lever used instead: checked ahead of the
`is_local()`/`private_key` branch (no `_clear_creation_guards` needed), and
read by neither `authorise_api_user` (app/utils.py:3628-3629, which checks
`ap_id`/`verified`/`banned`/`deleted`, never `ban_comments`) nor `:174`
(`user.banned or user_ip_banned()`), so it cannot also trip the token check
or `:174` the way `user.banned` did for Task 3.

TASK 4's FINDING ON THE SEVEN `PostReplyValidationError` RAISES, via
`make_reply` specifically (not `PostReply.new` in the abstract):

    :2983  REACHABLE, covered here (`test_a_reply_is_rejected_when_the_
           post_has_comments_disabled`).
    :2986  STRUCTURALLY UNREACHABLE from `make_reply`. `can_create_post_
           reply` (app/utils.py:2550) already returns `False` for
           `user.ban_comments` -- by the time `PostReply.new` could run,
           `:192` has already refused the same condition. Not a factory
           limitation; no seed can reach this line through `make_reply`.
    :3025  REACHABLE, covered here (`test_a_reply_containing_a_blocked_
           phrase_is_rejected`).
    :3039  REACHABLE, covered here (`test_an_identical_resubmission_is_
           rejected_as_a_duplicate`).
    :3036  Reachable in principle, but only through a THIRD reply level:
           `notification_target` (app/models.py:3030-3033) is `post` when
           `in_reply_to` is a top-level reply (already covered by `:189`)
           or the immediate parent's OWN author when replying to a
           second-level reply -- a shape `:182` does not check and `:189`
           cannot either. Building that (4 users: post author, a
           first-level reply author, a second-level reply author, and the
           blocked actor) earns comparatively little here: it does not
           change `make_reply`'s own missing set (Group B's target), and
           `tests/README.md`'s fact 56 already documents this exact raise
           reached through `create_post_reply` elsewhere in the suite.
           Skipped.
    :3046  UNREACHABLE from a plain factory seed -- see PROBE A above.
    :3049  Same as `:3046`.

`:2983`/`:3025`/`:3039` close the reachable-and-worth-it slice of
`PostReply.new`'s surface for this task; `:3036` is left to whichever task
next touches `PostReply.new` directly, and the two flag-gated ones remain
PROBE A's territory. None of the seven statements are in `make_reply`'s own
`:156-213` range, so covering or skipping them does not move this task's
measured target either way.
"""

import pytest
from datetime import datetime, timedelta
from types import SimpleNamespace

from app import db
from app.constants import SRC_API, SRC_WEB
from app.models import Language, PostReply, PostReplyValidationError
from app.shared.reply import edit_reply, make_reply
from tests.factories import (
    bearer, make_community, make_community_member, make_instance, make_post,
    make_post_reply, make_site, make_user, web_ctx,
)


def _seed_for_reply(*, private=True, community_name='replies'):
    """One instance, two users, one community, one post, one existing reply.

    `author` owns the seeded reply; `actor` is a second user for the tests
    that need a non-author. They are DISTINCT, which `edit_reply:218`'s
    `id_match=reply.user_id` makes load-bearing.

    THE IDS ARE FORCED APART -- see the module docstring and D533. A spare
    community and a spare post are created and left unused purely to advance
    those sequences past the user sequence, and the assertion below fails
    loudly if a future factory change makes them collide again.

    `private=True` sets `community.private`, register entry D393(d)'s
    federation lever: it stops the eager Celery task bodies at their first
    guard so no test issues an outbound request.
    """
    instance = make_instance('local.example', software='piefed')
    author = make_user(instance, 'author', local=True)
    actor = make_user(instance, 'actor', local=True)
    make_community('sequence-burner-one')
    make_community('sequence-burner-two')
    community = make_community(community_name)
    community.private = private
    db.session.commit()
    make_post(community, author, 'https://local.example/burner')
    post = make_post(community, author, 'https://local.example/p/1')
    reply = make_post_reply(post, author)
    db.session.commit()
    assert len({community.id, post.id, author.id}) == 3, (
        'D533: seeded ids collided again -- a wrong-object-right-id mutation '
        'would be invisible'
    )
    return SimpleNamespace(instance=instance, author=author, actor=actor,
                           community=community, post=post, reply=reply)


def make_moderator(s, user=None):
    """Make `user` (default `s.actor`) a moderator of `s.community`.

    `Community.moderators()` (app/models.py:716-722) filters
    `is_banned == False`, so a banned CommunityMember is NOT a moderator.
    """
    return make_community_member(user or s.actor, s.community, is_moderator=True)


def _clear_creation_guards(user):
    """TWO CORRECTIONS, established while writing Task 3, for every test that
    reaches a REAL `PostReply.new` call rather than raising before it.

    FIFTH CORRECTION: `make_reply:192`'s `can_create_post_reply`
    (app/utils.py:2546-2578) refuses ANY local user whose `private_key` is
    `None` -- `tests/test_shared_post_make.py:15-19` and
    `tests/test_shared_reply_interactions.py:544-550` already document the
    identical trap for `can_create_post`, but nothing in `edit_reply`'s chain
    trips it, since `edit_reply` never calls either function. Without this,
    every `TestMakeReply` test that reaches a real `PostReply.new` call dies
    at `:193` with 'You are not permitted to comment in this community' --
    a message indistinguishable from `:174`'s or `:190`'s.

    A REAL RSA KEYPAIR IS NOT NEEDED: `can_create_post_reply` only checks
    `is None`, and `_seed_for_reply`'s `community.private=True` stops the
    eager Celery task bodies that would otherwise try to USE the key for
    signing (see `_seed_for_reply`'s own docstring) before `make_reply`'s
    `task_selector` call at `:208` is ever reached. `make_user`'s
    `with_keys=True` (tests/factories.py:44-49) costs roughly a second per
    call precisely because it generates a real keypair; a plain sentinel
    string is functionally identical for this guard and free.

    SIXTH CORRECTION: past that guard, `PostReply.new` (app/models.py:3023)
    unconditionally calls `blocked_phrases()` (app/utils.py:1751-1754), which
    does `db.session.query(Site).get(1).blocked_phrases` with no `None`
    guard. `db_session` (tests/conftest.py) only truncates tables, so no
    `Site` row exists unless a test creates one -- exactly the situation
    `tests/factories.py:353-358`'s `make_site()` docstring already documents
    for `Post.new()`; `PostReply.new()` shares the same unconditional call.
    Without a `Site` row, every test here dies with `AttributeError:
    'NoneType' object has no attribute 'blocked_phrases'` instead of
    reaching this task's actual target lines.

    `_seed_for_reply`'s signature is a fixed interface other tasks consume,
    so this sets the attribute and creates the row directly rather than
    adding parameters to it.
    """
    user.private_key = 'test-key-not-a-real-pem'
    db.session.commit()
    make_site()


def _burn_a_seed():
    """Advance the user/community/post sequences by one full `_seed_for_reply`
    unit -- 2 users, 3 communities, 2 posts -- so the NEXT `_seed_for_reply`
    call's `author` id is never 1.

    `User.is_admin` (app/models.py:1259-1265) special-cases `self.id == 1`
    as an admin regardless of roles or community membership, and
    `_seed_for_reply` mints `author` first -- so WITHOUT this burn, `author`
    is id 1 in EVERY test, deterministically, silently turning it into an
    admin and bypassing `edit_reply:224`'s moderator guard for the wrong
    reason no matter what a test intends to prove.

    CORRECTION: an earlier version of this docstring (and of the two
    call-site docstrings below) described this as order-dependent -- "a
    coin flip" on which test happens to run first against a fresh database,
    on the theory that `db_session`'s DELETE-based teardown (tests/
    conftest.py) leaves id sequences to climb across tests. That is wrong.
    tests/conftest.py:131's teardown SQL ends with
    `SELECT setval(c.oid, 1, false) FROM pg_class c WHERE c.relkind = 'S'
    AND c.relnamespace = 'public'::regnamespace`, which resets every
    sequence in the schema back to 1 after EVERY test (tests/conftest.py:
    189-190 states this is deliberate: fixtures hardcode `instance_id=1`).
    So DELETE-not-TRUNCATE was right, but "sequences are not reset between
    tests" was backwards -- they are reset, every time, and that is exactly
    why `author` is id 1 unconditionally rather than occasionally. A
    reviewer proved this by neutering this helper: the mutation killed
    tests 6 and 8 of 8, not merely an early one, which is what an
    order-dependent effect would have produced instead.

    THIS DOES NOT JUST CALL `_seed_for_reply()` AGAIN: that hardcodes the
    instance domain `'local.example'`, so a second call in the same test
    trips `ix_instance_domain`'s unique constraint -- caught by running this.

    IT ALSO DOES NOT BURN A BARE USER: `_seed_for_reply` creates its two
    users, three communities and two posts in a fixed 2:3:2 ratio per call,
    which is exactly what keeps `community.id`, `post.id` and `author.id`
    pairwise distinct FOR ANY starting point (D533) -- author is always
    `2k+1`, post always `2k+2`, community always `3k+3` for the k-th call,
    and those three formulas never coincide for any `k >= 0`. Burning a
    single bare user instead (breaking that ratio) shifts the next call's
    `author` to `2k+2`, exactly `post`'s formula -- a guaranteed collision,
    not an occasional one, caught by running the two tests below inside the
    full class rather than alone. Reproducing the same 2:3:2 ratio under a
    distinct domain and distinct community names keeps the invariant intact
    without colliding with `_seed_for_reply`'s own rows.
    """
    instance = make_instance('id-burner.example')
    author = make_user(instance, 'id-burner-author', local=True)
    make_user(instance, 'id-burner-actor', local=True)
    make_community('id-burner-sequence-one')
    make_community('id-burner-sequence-two')
    community = make_community('id-burner-community')
    db.session.commit()
    make_post(community, author, 'https://id-burner.example/burner')
    make_post(community, author, 'https://id-burner.example/p/1')
    db.session.commit()


class TestEditReply:
    """`edit_reply` (app/shared/reply.py:216-251)."""

    def test_the_api_arm_edits_the_body_and_returns_the_pair(self, db_session):
        """`:217` true, `:218`-`:222`, the writes at `:233`-`:238`, `:249`.

        Asserts `body` AND `edited_at`, because `:233`-`:238` write six
        attributes unconditionally and any single one of them would pass with
        the other five deleted.
        """
        s = _seed_for_reply()
        payload = {'body': 'edited through the api', 'notify_author': True,
                   'language_id': None, 'distinguished': False}

        user_id, reply = edit_reply(payload, s.reply, s.post, SRC_API,
                                    auth=bearer(s.author))

        assert user_id == s.author.id
        db.session.refresh(s.reply)
        assert 'edited through the api' in s.reply.body
        assert s.reply.edited_at is not None
        assert s.reply.notify_author is True

    def test_the_web_arm_reads_the_form_and_returns_none(self, db_session, app):
        """`:217` false -> `:227`-`:231`, `:243`-`:244`'s flash, `:251`.

        `web_ctx` takes the app fixture FIRST -- `web_ctx(app, user)`,
        tests/factories.py:1217. Sub-project 41's plan wrote `web_ctx(user)`
        five times and every occurrence was wrong.
        """
        from flask import get_flashed_messages
        s = _seed_for_reply()
        form = SimpleNamespace(
            body=SimpleNamespace(data='edited through the web'),
            notify_author=SimpleNamespace(data=False),
            language_id=SimpleNamespace(data=None),
            distinguished=SimpleNamespace(data=False),
        )

        with web_ctx(app, s.author):
            result = edit_reply(form, s.reply, s.post, SRC_WEB, auth=None)
            messages = get_flashed_messages()

        assert result is None
        assert 'Your changes have been saved.' in messages
        db.session.refresh(s.reply)
        assert 'edited through the web' in s.reply.body

    def test_editing_writes_all_six_attributes_not_just_body(self, db_session):
        """`:233`-`:238` write SIX attributes unconditionally: `body`,
        `body_html`, `notify_author`, `community.last_active`, `edited_at`
        and `language_id`. The other tests in this class already witness
        `body`, `edited_at` and `notify_author`; this one closes the
        remaining three, each of which would survive deletion of its own
        line if only the other five were checked.

        `community.last_active` is pinned to a fixed sentinel FAR in the
        past BEFORE the call (rather than merely captured), and the
        assertion is that it MOVED well past that sentinel -- not merely
        that it is not `None`, which the fixture's `make_community` already
        leaves true regardless. A sentinel decades in the past, rather than
        comparing two `utcnow()` calls a few lines apart, means the
        assertion does not depend on clock resolution between the fixture's
        timestamp and `:236`'s: it is a genuine identity change (sentinel ->
        now), not a timing race.

        `language_id` starts `None` (no factory sets it) and is asserted to
        become a real `Language.id` after the edit -- a value round-trip,
        not just a non-`None` check, since a mutant that deleted `:238`
        would leave it `None` too if the payload's default were `None`.
        A genuine `Language` row is seeded here (nowhere else in this file
        needs one) purely so a non-`None`, FK-satisfying value exists to
        witness the write.
        """
        s = _seed_for_reply()
        language = Language(code='en', name='English')
        db.session.add(language)
        sentinel = datetime(2000, 1, 1)
        s.community.last_active = sentinel
        db.session.commit()
        payload = {'body': 'zzyzx marks the six', 'notify_author': True,
                   'language_id': language.id, 'distinguished': False}

        edit_reply(payload, s.reply, s.post, SRC_API, auth=bearer(s.author))

        db.session.refresh(s.reply)
        db.session.refresh(s.community)
        assert 'zzyzx marks the six' in s.reply.body_html
        assert s.community.last_active > sentinel + timedelta(days=365)
        assert s.reply.language_id == language.id

    def test_a_moderator_may_distinguish_through_the_api(self, db_session):
        """`:223`'s true arm with `:224` false, and `:239`-`:240`.

        `:223` is `(not reply.distinguished and distinguished == True) or
        (reply.distinguished == True and distinguished == False)` -- two
        disjuncts, one arc pair to coverage.py. This takes the FIRST:
        undistinguished becoming distinguished.
        """
        s = _seed_for_reply()
        make_moderator(s, user=s.author)
        s.reply.distinguished = False
        db.session.commit()
        payload = {'body': 'body', 'notify_author': False,
                   'language_id': None, 'distinguished': True}

        edit_reply(payload, s.reply, s.post, SRC_API, auth=bearer(s.author))

        db.session.refresh(s.reply)
        assert s.reply.distinguished is True

    def test_undistinguishing_takes_the_second_disjunct(self, db_session):
        """`:223`'s SECOND disjunct -- distinguished becoming undistinguished.

        The first disjunct is false here (`reply.distinguished` is already
        True), so this is the only test that can witness the second. Without
        it the two move only in lockstep -- false-witness mechanism (e).
        """
        s = _seed_for_reply()
        make_moderator(s, user=s.author)
        s.reply.distinguished = True
        db.session.commit()
        payload = {'body': 'body', 'notify_author': False,
                   'language_id': None, 'distinguished': False}

        edit_reply(payload, s.reply, s.post, SRC_API, auth=bearer(s.author))

        db.session.refresh(s.reply)
        assert s.reply.distinguished is False

    def test_a_non_moderator_changing_distinguished_is_refused_by_the_api(self, db_session):
        """`:224`'s true arm and `:225`'s raise.

        THE RAISE IS NOT THE ONLY WITNESS. A crash is a weak kill, so this
        also asserts the body was NOT written: `:233` runs after the guard, so
        a mutant that performed the edit and then raised would pass a bare
        `pytest.raises`.

        `_burn_a_seed()` runs first: `_seed_for_reply` mints `author` first
        every single test (tests/conftest.py's teardown resets every
        sequence to 1 -- see `_burn_a_seed`'s docstring), and `User.is_admin`
        special-cases id 1 as an admin outright (app/models.py:1259-1261).
        Without the burn `author` is id 1 EVERY time this test runs, not
        occasionally, so `:224` would deterministically be False (admin) and
        nothing would ever raise.
        """
        _burn_a_seed()
        s = _seed_for_reply()
        s.reply.distinguished = False
        db.session.commit()
        original_body = s.reply.body
        payload = {'body': 'should not be saved', 'notify_author': False,
                   'language_id': None, 'distinguished': True}

        with pytest.raises(Exception, match='Not a moderator'):
            edit_reply(payload, s.reply, s.post, SRC_API, auth=bearer(s.author))

        db.session.refresh(s.reply)
        assert s.reply.body == original_body

    def test_leaving_distinguished_unchanged_skips_the_moderator_check(self, db_session):
        """`:223`'s false arm -- both disjuncts false, so `:224` never runs.

        The same non-moderator as the test above succeeds here, which is what
        proves `:223` gates `:224` rather than `:224` refusing unconditionally.
        """
        s = _seed_for_reply()
        s.reply.distinguished = False
        db.session.commit()
        payload = {'body': 'saved fine', 'notify_author': False,
                   'language_id': None, 'distinguished': False}

        edit_reply(payload, s.reply, s.post, SRC_API, auth=bearer(s.author))

        db.session.refresh(s.reply)
        assert 'saved fine' in s.reply.body

    def test_the_web_arm_silently_declines_distinguished(self, db_session, app):
        """`:239`'s false arm -- AND IT ASSERTS A REGISTERED DEFECT ON PURPOSE.

        `edit_reply` checks one permission TWICE, in two spellings, fifteen
        lines apart. `:224` is
        `not is_moderator and not is_owner and not is_staff() and not
        is_admin()`; `:239` is `is_moderator or is_owner or
        is_admin_or_staff()`. `is_admin_or_staff()` is exactly
        `is_admin() or is_staff()` (app/models.py:1274-1275), so the two are
        De Morgan twins over the same set.

        The API arm RAISES at `:225`. The web arm has no equivalent, so a
        non-moderator's `distinguished` is silently dropped at `:239` and the
        caller is told nothing -- the same silent-failure shape sub-project 41
        fixed twice in this module.

        THIS TEST IS NOT INVERTED BY THIS ROUND. The finding is registered,
        not fixed: this round's production budget is the counter fix. If a
        later round adds the refusal, THE EDIT OWED HERE IS TO INVERT THIS
        TEST -- the call must then raise and `distinguished` must stay False.

        The witness is `distinguished` still False AFTER a successful edit, so
        the body assertion is what proves the call was not refused outright.

        `_burn_a_seed()` runs first for the same reason as the API-arm
        refusal test above: `author` is id 1 EVERY time this test runs
        without the burn, deterministically (see `_burn_a_seed`'s
        docstring), which would make `user.is_admin_or_staff()` True at
        `:239`, applying `distinguished` instead of silently dropping it --
        this test would pass for the wrong reason every single time, not
        occasionally.
        """
        _burn_a_seed()
        s = _seed_for_reply()
        s.reply.distinguished = False
        db.session.commit()
        form = SimpleNamespace(
            body=SimpleNamespace(data='edited anyway'),
            notify_author=SimpleNamespace(data=False),
            language_id=SimpleNamespace(data=None),
            distinguished=SimpleNamespace(data=True),
        )

        with web_ctx(app, s.author):
            edit_reply(form, s.reply, s.post, SRC_WEB, auth=None)

        db.session.refresh(s.reply)
        assert 'edited anyway' in s.reply.body
        assert s.reply.distinguished is False


class TestMakeReply:
    """`make_reply` (app/shared/reply.py:156-213).

    THE WEIGHT IS IN `PostReply.new`, WHICH THIS FUNCTION CALLS AT `:196`.
    `make_reply` itself is a source fork, four guards and a commit. The
    filters, the `path` construction, the three counter increments and the
    `reply_count_cross_posted` recompute all live in the model, and five
    `PostReplyValidationError` raises are reachable from there -- Task 1's
    Probe A records which a factory seed can drive.
    """

    def test_the_api_arm_creates_a_reply_and_returns_the_pair(self, db_session):
        """`:157` true, `:158`-`:165`, `:196`'s `PostReply.new`, `:211`.

        Asserts the row exists AND that `user.language_id` was written at
        `:200`, because `:211` returns a pair whose shape a mutant could
        produce without creating anything.

        `_clear_creation_guards(s.actor)` is required: `:192`'s `can_create_post_reply`
        refuses any local user whose `private_key` is `None` -- see
        `_clear_creation_guards`'s docstring.

        A REAL `Language` ROW IS SEEDED AND ITS ID USED, NOT `None`, per a
        fix-round-1 finding: `User.language_id` (app/models.py:1038) has no
        default, so `None` is already every fresh user's value, and an
        assertion of `is None` cannot tell whether `:200` ran at all or was
        deleted outright. The reviewer mutated `:200` out and the `None`-based
        version of this assertion still passed. Using a genuine, non-`None`
        id makes the assertion a value round-trip -- confirmed by the same
        mutate-and-restore cycle below.
        """
        s = _seed_for_reply()
        _clear_creation_guards(s.actor)
        language = Language(code='en', name='English')
        db.session.add(language)
        db.session.commit()
        payload = {'body': 'a new reply', 'notify_author': True,
                   'language_id': language.id, 'distinguished': False}

        user_id, reply = make_reply(payload, s.post, None, SRC_API,
                                    auth=bearer(s.actor))

        assert user_id == s.actor.id
        assert reply.id is not None
        db.session.refresh(s.actor)
        assert s.actor.language_id == language.id

    def test_the_web_arm_reads_the_form_clears_it_and_flashes(self, db_session, app):
        """`:157` false -> `:167`-`:172`, `:204`-`:206`, `:213`.

        `:205` sets `input.body.data = ''`, which is a write BACK INTO the
        form object -- assert it, because nothing else in the function does
        and a mutant deleting `:205` is otherwise invisible.

        `_clear_creation_guards(s.actor)` is required here too -- see
        `_clear_creation_guards`'s docstring; `:192` gates the web arm
        exactly as it does the API arm.
        """
        from flask import get_flashed_messages
        s = _seed_for_reply()
        _clear_creation_guards(s.actor)
        form = SimpleNamespace(
            body=SimpleNamespace(data='a web reply'),
            notify_author=SimpleNamespace(data=True),
            language_id=SimpleNamespace(data=None),
            distinguished=SimpleNamespace(data=False),
        )

        with web_ctx(app, s.actor):
            reply = make_reply(form, s.post, None, SRC_WEB, auth=None)
            messages = get_flashed_messages()

        assert reply.id is not None
        assert form.body.data == ''
        assert 'Your comment has been added.' in messages

    def test_an_ip_banned_user_is_refused(self, db_session, app):
        """`:174`'s true arm (the `user_ip_banned()` disjunct) and `:175`'s
        raise.

        A SEVENTH CORRECTION, established by mutation-testing this test
        rather than by reading. `user.banned` is UNSUITABLE as this guard's
        lever, despite the brief's plan naming it: `can_create_post_reply`
        (app/utils.py:2547) has its OWN unconditional `user.banned` check,
        so a banned user is refused at `:192`-`:193` even if `:174`-`:175`
        are deleted outright -- `:175`'s and `:193`'s messages are also
        byte-for-byte identical ('You are not permitted to comment in this
        community'). Proven by mutating `:174`-`:175` to a no-op: a
        `user.banned`-based version of this test, EVEN WITH
        `_clear_creation_guards` applied, still passed against that mutant,
        with `can_create_post_reply` supplying the same-text raise and the
        row-count assertion holding for the wrong reason -- an equivalent
        mutant with respect to that lever, not a caught one.

        `user_ip_banned()` has no such twin: `can_create_post_reply` never
        calls it or `banned_ip_addresses()`, so it is the ONLY reachable way
        to isolate `:174` from `:192`. `ip_address()` (`user_ip_banned`'s own
        call) resolves `request.remote_addr`, which plain `web_ctx` leaves
        `None` (Werkzeug's `test_request_context` sets no REMOTE_ADDR by
        default) -- confirmed by inspection, since `''` is falsy and
        `user_ip_banned` would short-circuit to `None` regardless of any
        `IpBan` row. This test builds its own request context with
        `environ_overrides` instead of calling `web_ctx`, to supply a
        REMOTE_ADDR an `IpBan` row can match.

        THE STATE ASSERTION CARRIES IT, not the raise: no `PostReply` row may
        be created. `_clear_creation_guards(s.actor)` is load-bearing for the
        kill, confirmed by the same mutation: with it applied, a neutered
        `:174`-`:175` lets the call run to a real, successful
        `PostReply.new`, so a mutant deleting the guard is genuinely caught
        by the row-count assertion rather than passing on `can_create_post_reply`'s
        unrelated say-so.

        `SRC_API` is used, not `SRC_WEB`: the API arm resolves `user` from
        the bearer token via `authorise_api_user`, with no need for
        `login_user`/`current_user` -- only a request context is needed, for
        `ip_address()`'s `request.remote_addr` read.
        """
        from app.models import IpBan
        s = _seed_for_reply()
        _clear_creation_guards(s.actor)
        db.session.add(IpBan(ip_address='203.0.113.5'))
        db.session.commit()
        before = db.session.query(PostReply).count()
        payload = {'body': 'ip banned attempt', 'notify_author': False,
                   'language_id': None, 'distinguished': False}

        with app.test_request_context('/', environ_overrides={'REMOTE_ADDR': '203.0.113.5'}):
            with pytest.raises(Exception, match='not permitted to comment'):
                make_reply(payload, s.post, None, SRC_API, auth=bearer(s.actor))

        assert db.session.query(PostReply).count() == before

    def test_a_non_moderator_cannot_distinguish_a_new_reply(self, db_session):
        """`:177`'s true arm and `:178`'s demotion.

        `:177` is three negated conditions; this takes all three false at
        once, which is the only combination that reaches `:178`. The witness
        is `distinguished` being False on the created row DESPITE the payload
        asking for True -- a mutant deleting `:178` leaves it True.

        THE id-1 TRAP DOES NOT BITE HERE, but only because of who is acting.
        `user.is_admin_or_staff()` is evaluated on the ACTING user, which is
        `s.actor` here (the bearer token belongs to `s.actor`), not
        `s.author`. `_seed_for_reply` mints `author` first and `actor`
        second, and tests/conftest.py resets every sequence before each
        test, so `author` is id 1 and `actor` is id 2 -- deterministically,
        every run. `User.is_admin` (app/models.py:1259-1261) only special-
        cases id 1, so `actor` (id 2) is never accidentally an admin and no
        `_burn_a_seed()` is needed to keep this guard meaningful. Neither
        `post.community.is_moderator(actor)` nor `is_owner(actor)` can be
        true either: `_seed_for_reply` never adds `actor` as a
        `CommunityMember` of `s.community`, so `Community.moderators()`
        (app/models.py:716-722) never returns a row for it.

        `_clear_creation_guards(s.actor)` clears `:192`'s unrelated guard so the demotion
        at `:178` can be witnessed on an actually-created row -- see
        `_clear_creation_guards`'s docstring.
        """
        s = _seed_for_reply()
        _clear_creation_guards(s.actor)
        payload = {'body': 'presumptuous', 'notify_author': False,
                   'language_id': None, 'distinguished': True}

        user_id, reply = make_reply(payload, s.post, None, SRC_API,
                                    auth=bearer(s.actor))

        db.session.refresh(reply)
        assert reply.distinguished is False

    def test_a_moderator_keeps_distinguished_on_a_new_reply(self, db_session):
        """`:177`'s false arm -- the same-mechanism positive control.

        Without it, a fixture in which `distinguished` could never survive
        would produce the same False above. Same payload, one lever moved.

        `_clear_creation_guards(s.actor)` clears `:192`'s unrelated guard -- see
        `_clear_creation_guards`'s docstring.
        """
        s = _seed_for_reply()
        _clear_creation_guards(s.actor)
        make_moderator(s)
        payload = {'body': 'entitled', 'notify_author': False,
                   'language_id': None, 'distinguished': True}

        user_id, reply = make_reply(payload, s.post, None, SRC_API,
                                    auth=bearer(s.actor))

        db.session.refresh(reply)
        assert reply.distinguished is True

    def test_replying_to_a_parent_sets_the_path_and_the_parent_id(self, db_session):
        """`:180`'s true arm, `:181`'s lookup, and `PostReply.new`'s path build.

        The seeded parent has no `path` of its own -- `make_post_reply` does
        not set one -- so this also witnesses the `else` at
        app/models.py:3063-3064, which gives a top-level reply `[0, reply.id]`.
        Assert the SHAPE, not just non-emptiness: a two-element path with the
        sentinel first is what every reader of this column assumes.

        `_clear_creation_guards(s.actor)` clears `:192`'s unrelated guard -- see
        `_clear_creation_guards`'s docstring.
        """
        s = _seed_for_reply()
        _clear_creation_guards(s.actor)
        payload = {'body': 'a child reply', 'notify_author': False,
                   'language_id': None, 'distinguished': False}

        user_id, reply = make_reply(payload, s.post, s.reply.id, SRC_API,
                                    auth=bearer(s.actor))

        db.session.refresh(reply)
        assert reply.parent_id == s.reply.id
        assert reply.path == [0, reply.id]

    def test_a_blocked_replier_is_refused_by_the_parents_author(self, db_session):
        """`:182`'s true arm and `:183`'s raise.

        `has_blocked_user` is the lever. The state assertion is that no new
        `PostReply` row exists -- the raise alone would pass against a mutant
        that created it first.

        `match='parent reply'` IS LOAD-BEARING, not decoration -- proven by
        mutating `:182`-`:183` to a no-op: a bare `pytest.raises(Exception)`
        still passed, because `_seed_for_reply`'s `post` and its lone
        top-level `reply` share ONE author (`s.author`), so the SAME
        `UserBlock` row ALSO satisfies `:189`'s post-author check and the
        call still raised and still created no row -- for the wrong reason.
        `:183`'s message says 'parent reply'; `:190`'s says 'parent post' --
        the two are otherwise identical text, so this is the only way to
        pin the raise to `:183` rather than its downstream twin.
        """
        s = _seed_for_reply()
        from app.models import UserBlock
        db.session.add(UserBlock(blocker_id=s.author.id, blocked_id=s.actor.id))
        db.session.commit()
        before = db.session.query(PostReply).count()
        payload = {'body': 'blocked', 'notify_author': False,
                   'language_id': None, 'distinguished': False}

        with pytest.raises(Exception, match='parent reply'):
            make_reply(payload, s.post, s.reply.id, SRC_API,
                       auth=bearer(s.actor))

        assert db.session.query(PostReply).count() == before

    def test_a_locked_parent_cannot_be_replied_to(self, db_session):
        """`:184`'s true arm and `:185`'s raise.

        `replies_enabled` False is what `lock_post_reply` sets, so this is the
        downstream half of the lock Group F covers.
        """
        s = _seed_for_reply()
        s.reply.replies_enabled = False
        db.session.commit()
        before = db.session.query(PostReply).count()
        payload = {'body': 'locked out', 'notify_author': False,
                   'language_id': None, 'distinguished': False}

        with pytest.raises(Exception, match='cannot be replied to'):
            make_reply(payload, s.post, s.reply.id, SRC_API,
                       auth=bearer(s.actor))

        assert db.session.query(PostReply).count() == before

    def test_a_blocked_replier_is_refused_by_the_posts_author(self, db_session):
        """`:189`'s true arm and `:190`'s raise -- the POST author's block.

        Distinct from the parent-reply block at `:182`-`:183`
        (`test_a_blocked_replier_is_refused_by_the_parents_author`): this
        test passes `parent_id=None`, so `:180` is false and `:187` sets
        `parent_reply` to `None` -- `:182`-`:183` never runs at all here,
        rather than merely not firing. `_seed_for_reply`'s `post` and its
        lone top-level `reply` share ONE author (`s.author`), which is
        exactly what made the OTHER test need `match='parent reply'` to
        isolate itself from this line; here the situation is reversed, so
        `match='parent post'` does the same job in the other direction --
        `:183`'s message says 'parent reply', `:190`'s says 'parent post',
        otherwise byte-identical text.
        """
        s = _seed_for_reply()
        from app.models import UserBlock
        db.session.add(UserBlock(blocker_id=s.author.id, blocked_id=s.actor.id))
        db.session.commit()
        before = db.session.query(PostReply).count()
        payload = {'body': 'blocked at post level', 'notify_author': False,
                   'language_id': None, 'distinguished': False}

        with pytest.raises(Exception, match='parent post'):
            make_reply(payload, s.post, None, SRC_API, auth=bearer(s.actor))

        assert db.session.query(PostReply).count() == before

    def test_a_user_without_permission_to_comment_is_refused(self, db_session):
        """`:192`'s true arm and `:193`'s raise.

        THE BRIEF NAMED `s.actor.bot = True` AS THE LEVER; THAT IS WRONG,
        caught before writing this test rather than after. `can_create_
        post_reply` (app/utils.py:2546-2578) never reads `.bot` anywhere in
        its body (confirmed with `grep -n "\\.bot\\b"` across app/utils.py) --
        a `bot=True` actor sails straight through `:192` into a real
        `PostReply.new` call and this test would never raise, let alone at
        `:193` specifically. See the module docstring's "TASK 4's PLAN
        NAMED THE WRONG LEVER" note.

        `user.ban_comments` (app/utils.py:2550) is the lever used instead.
        It is checked ahead of the `is_local()`/`private_key` branch, so no
        `_clear_creation_guards` call is needed here (nothing past `:192`
        is ever reached). It is also read by neither `authorise_api_user`
        (app/utils.py:3628-3629 checks `ap_id`/`verified`/`banned`/
        `deleted`, never `ban_comments`) nor `make_reply:174`
        (`user.banned or user_ip_banned()`) -- so, unlike `user.banned`
        (Task 3's finding: it trips `authorise_api_user` before `make_reply`
        even runs, AND trips `can_create_post_reply`'s own unconditional
        `user.banned` check), `ban_comments` cannot fire either of those,
        which is what makes `:192`-`:193` the ONLY guard this test's actor
        can possibly hit. `:175` and `:193` share byte-identical message
        text, so isolation here comes from the lever being structurally
        incapable of reaching `:174`, not from the message.

        PROVEN BY MUTATION (see this task's report): neutering `:192`
        (`if not can_create_post_reply(...)` -> `if False:`) makes this
        test fail, because the call then proceeds into a real
        `PostReply.new` and returns a pair instead of raising.
        """
        s = _seed_for_reply()
        s.actor.ban_comments = True
        db.session.commit()
        before = db.session.query(PostReply).count()
        payload = {'body': 'not permitted', 'notify_author': False,
                   'language_id': None, 'distinguished': False}

        with pytest.raises(Exception, match='not permitted to comment'):
            make_reply(payload, s.post, None, SRC_API, auth=bearer(s.actor))

        assert db.session.query(PostReply).count() == before

    def test_the_federation_task_is_selected_with_the_parent_id(self, db_session):
        """`:208`'s task_selector call, including its `parent_id` argument.

        `recording_task_selector` is defined in
        `tests/test_shared_reply_moderation.py`; this file defines its own
        rather than importing across test modules, because the two rebind
        DIFFERENT module globals and sharing one would be a false witness.

        `_clear_creation_guards(s.actor)` IS NEEDED, though the brief's
        given code omitted it: `parent_id=s.reply.id` means this call must
        reach a REAL `PostReply.new` to ever get to `:208` at all, and
        `:192`'s `can_create_post_reply` guard refuses any local actor
        whose `private_key` is `None` -- see `_clear_creation_guards`'s
        docstring. Without it this test dies at `:193` before
        `task_selector` is ever called.
        """
        s = _seed_for_reply()
        _clear_creation_guards(s.actor)
        import app.shared.reply as reply_module
        calls = []
        original = reply_module.task_selector

        def recorder(task_key, **kwargs):
            calls.append((task_key, kwargs.get('parent_id')))
            return original(task_key, **kwargs)

        reply_module.task_selector = recorder
        try:
            payload = {'body': 'federated', 'notify_author': False,
                       'language_id': None, 'distinguished': False}
            make_reply(payload, s.post, s.reply.id, SRC_API,
                       auth=bearer(s.actor))
        finally:
            reply_module.task_selector = original

        assert ('make_reply', s.reply.id) in calls

    def test_a_reply_is_rejected_when_the_post_has_comments_disabled(self, db_session):
        """`PostReply.new`'s FIRST guard, app/models.py:2982-2983
        ('Comments are disabled on this post') -- a callee raise, not one
        of `make_reply`'s own `:156-213` statements, but the reachable one
        cheapest to witness. `_clear_creation_guards` is still required so
        the call reaches `PostReply.new` at all (`:192`'s `can_create_post_
        reply` guard); this guard itself runs before `blocked_phrases()`,
        so the blank `Site` row `_clear_creation_guards` creates is never
        even read on this path.
        """
        s = _seed_for_reply()
        _clear_creation_guards(s.actor)
        s.post.comments_enabled = False
        db.session.commit()
        before = db.session.query(PostReply).count()
        payload = {'body': 'too late, comments are off', 'notify_author': False,
                   'language_id': None, 'distinguished': False}

        with pytest.raises(PostReplyValidationError, match='Comments are disabled'):
            make_reply(payload, s.post, None, SRC_API, auth=bearer(s.actor))

        assert db.session.query(PostReply).count() == before

    def test_a_reply_containing_a_blocked_phrase_is_rejected(self, db_session):
        """`PostReply.new`'s guard at app/models.py:3025 ('Blocked phrase
        in comment'), reached through `make_reply:196` once `:192`'s
        `can_create_post_reply` guard is cleared.

        `_clear_creation_guards` creates a blank `Site` row
        (`blocked_phrases=''`, tests/factories.py:359); this test
        overwrites that column on the SAME row afterward rather than
        constructing a second one -- `blocked_phrases()` (app/utils.py:
        1751-1754) and `can_create_post_reply` both read Site id 1, and a
        second row would just be dead data. `CACHE_TYPE = 'NullCache'`
        under test (tests/conftest.py:68) means `blocked_phrases()`'s
        `@cache.memoize` never serves a stale read back.
        """
        s = _seed_for_reply()
        _clear_creation_guards(s.actor)
        from app.models import Site
        site = db.session.query(Site).get(1)
        site.blocked_phrases = 'forbiddenword'
        db.session.commit()
        before = db.session.query(PostReply).count()
        payload = {'body': 'this has a forbiddenword in it', 'notify_author': False,
                   'language_id': None, 'distinguished': False}

        with pytest.raises(PostReplyValidationError, match='Blocked phrase'):
            make_reply(payload, s.post, None, SRC_API, auth=bearer(s.actor))

        assert db.session.query(PostReply).count() == before

    def test_an_identical_resubmission_is_rejected_as_a_duplicate(self, db_session):
        """`PostReply.new`'s guard at app/models.py:3038-3039 ('Duplicate
        reply'), reached on a SECOND `make_reply` call carrying the same
        user, post, `parent_id` (`None`) and post-conversion body as an
        already-successful first call.

        `reply_already_exists` (app/utils.py:2599) compares the STORED
        `body` column, not the raw payload, so the first call must
        genuinely succeed and the second must send byte-identical `body`
        text through the same `piefed_markdown_to_lemmy_markdown`
        conversion -- a pure function of its input, so sending the same
        payload twice is sufficient.
        """
        s = _seed_for_reply()
        _clear_creation_guards(s.actor)
        before = db.session.query(PostReply).count()
        payload = {'body': 'say it once', 'notify_author': False,
                   'language_id': None, 'distinguished': False}

        make_reply(payload, s.post, None, SRC_API, auth=bearer(s.actor))
        after_first = db.session.query(PostReply).count()
        assert after_first == before + 1

        with pytest.raises(PostReplyValidationError, match='Duplicate reply'):
            make_reply(payload, s.post, None, SRC_API, auth=bearer(s.actor))

        assert db.session.query(PostReply).count() == after_first
