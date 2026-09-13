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
