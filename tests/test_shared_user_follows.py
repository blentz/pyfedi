"""`follow_user` and `unfollow_user` (app/shared/user.py:231-300), plus
`subscribe_user`'s two flash statements at :107 and :115.

follow_user and unfollow_user were nearly closed already -- reached
incidentally by tests/test_api_user_subscriptions.py and
tests/test_ap_notify_post.py -- so this file targets only the residual arcs
a full-suite measurement showed still missing at commit fd5b9bcd:

    follow_user     lines 242, 253, 256   arcs [241,242] [252,253]
    unfollow_user   lines 280, 295        arcs [279,280] [294,295]

CORRECTED. That measurement also listed `subscribe_user` lines 107 and 115
as missing, and an earlier version of this docstring said this file did not
attempt them because they were "structurally unreachable". THAT WAS WRONG,
and the paragraph is retracted here rather than quietly deleted.

What the original derivation actually established is narrower than what it
claimed, and the narrow half is true: subscribe_user has exactly two
PRODUCTION callers (app/api/alpha/utils/user.py:315 on SRC_API,
app/user/routes.py:758 on SRC_WEB), and NEITHER of them can reach :107 or
:115. On SRC_WEB, :94 recomputes `subscribe` from
`person.notify_new_posts(user_id)` (app/models.py:1601-1603, filtered on
entity_id/user_id/type == NOTIF_USER), and :96's `existing_notification` is
looked up with the IDENTICAL filter -- the same row. So on SRC_WEB,
`subscribe == False` if and only if `existing_notification is not None`:
:98 true forces :99 true (reaching :100, never :102-107), and :98 false
forces :110 false (reaching :116, never :111-115). On SRC_API, :104 and
:112 both take their `raise` arm before reaching :107/:115.

WHAT DOES NOT FOLLOW is "no test can reach them". `src` is an ordinary
parameter of a module-level function, not a value the two callers get to
constrain, and app/constants.py:94-95 defines SRC_PLD = 4 and SRC_PLG = 5.
Pass a third source value and :93's `if src == SRC_WEB:` is skipped, so the
caller's `subscribe` argument survives; :104 and :112's `if src == SRC_API:`
then take their ELSE arms, and :107/:115 execute. A LINE NO INPUT CAN REACH
IS NOT A LINE NO TEST CAN REACH (tests/README.md fact 241, and fact 250 for
this recurrence).

The remedy was already in the tree when the original claim was written:
subscribe_user is a line-for-line twin of subscribe_post
(app/shared/post.py:127-164) and subscribe_reply, and BOTH twins had these
same two statements closed by exactly this technique --
tests/test_shared_post_interactions.py:577 and
tests/test_shared_reply_interactions.py:1018. Both modules sit at floor 100.
test_a_third_source_reaches_the_flash_branches_the_web_arm_cannot below is
modelled on the post twin.

THE id-1 ADMIN TRAP applies here too, though neither function under test
reads is_admin(). _seed_followers still burns the seat as a matter of this
module's seeding convention (tests/test_shared_user_bans.py:73 and
tests/test_shared_reply_make.py:247 are the precedent) so that the id-1
user is never silently the follower or target of any assertion here.

THE COUNTER ASYMMETRY (D557, fixed):
follow_user's manually-approving arm at :241-242 sets `is_accepted = None`
and skips the counter increments at :245-246 entirely, so neither
`user.num_following` nor `to_follow.num_followers` moves. unfollow_user
used to decrement both counters UNCONDITIONALLY, so following and then
unfollowing a manually-approving user drove both negative. It now
decrements only when it deletes an accepted follow row.
test_follow_then_unfollow_a_manually_approving_target_leaves_counters_at_zero
asserts both halves -- the follow alone would be consistent with the
counters simply never being implemented for that arm.

What each test below closes:

- test_follow_then_unfollow_a_manually_approving_target_leaves_counters_at_zero
  follow_user lines 242, 253, 256 and arcs [241,242], [252,253] (the
  manually-approving arm and its "someone wants to follow you" notification);
  unfollow_user line 280 and arc [279,280] (the SRC_API auth arm). This
  test's unfollow call also deliberately logs in the WRONG user via
  `web_ctx(app, s.target)` -- see its docstring -- so a branch-flip mutant
  at :280 has a non-crashing variant to resolve to, rather than only ever
  crashing on an anonymous `current_user` (a crash kill is not a kill
  unless a non-crashing variant of the same fault also dies). It also
  binds unfollow's task-selector recorder and asserts `calls == []`, the
  negative control proving a LOCAL target dispatches no task at :294-295.
- test_unfollow_user_dispatches_a_task_for_a_remote_target
  unfollow_user line 295 and arc [294,295] (the remote-target task
  dispatch) -- the positive control the negative control above needs to be
  meaningful; together the two rule out a mutant that deletes or inverts
  the :294 is_local() guard so task_selector fires unconditionally.
- test_a_third_source_reaches_the_flash_branches_the_web_arm_cannot
  subscribe_user lines 107 and 115 and arcs [104,107], [112,115] -- the two
  flash statements no production caller reaches, retracting this file's
  original "structurally unreachable" claim above.
"""
import contextlib
import importlib.util
import pathlib
from types import SimpleNamespace

import pytest
from flask import get_flashed_messages
from sqlalchemy import text

from app import db
from app.constants import SRC_API, SRC_PLD, SRC_WEB
from app.activitypub.util import announcer_is_followed
from app.models import Notification, NotificationSubscription, User, UserFollower, UserFollowRequest
from app.shared.user import follow_user, subscribe_user, unfollow_user
from tests.factories import bearer, make_instance, make_site, make_user, web_ctx


def _seed_followers(target_local=True):
    """A follower and a target, after burning the id-1 admin seat.

    Mirrors tests/test_shared_user_bans.py:73's _seed_ban_scenario shape:
    an instance, an explicit id-1 burn, then the two actors the test needs.
    `target_local` controls whether `target.is_local()` is True or False --
    follow_user:240/:251 and unfollow_user:294 all branch on it.
    """
    instance = make_instance('remote.example')
    burn = make_user(instance, 'burn-the-id-1-seat', local=True)
    assert burn.id == 1, 'the id-1 admin trap moved; re-derive before trusting this'

    follower = make_user(instance, 'follower', local=True)
    target = make_user(instance, 'target', local=target_local)
    return SimpleNamespace(instance=instance, follower=follower, target=target)


@contextlib.contextmanager
def _recording_task_selector():
    """Collect every task key follow_user/unfollow_user dispatch.

    Both call `task_selector(...)` unqualified, and app/shared/user.py:10's
    `from app.shared.tasks import task_selector` already bound the original
    into this module's globals -- patching app.shared.tasks.task_selector
    would not intercept it. Rebinding the name ON app.shared.user does.
    tests/test_shared_post_moderation.py:142 and
    tests/test_shared_user_bans.py:100 are the precedent; this is a copy,
    not an import, because a fixture/helper defined in one test module is
    not visible from another unless it lives in conftest.py.

    Restores in a finally: app.shared.user is imported once per session, so
    a leaked patch would corrupt every test that ran after this one.
    """
    import app.shared.user as user_module
    calls = []
    original = user_module.task_selector

    def recorder(task_key, **kwargs):
        calls.append((task_key, kwargs))
        return None

    user_module.task_selector = recorder
    try:
        yield calls
    finally:
        user_module.task_selector = original


def test_follow_then_unfollow_a_manually_approving_target_leaves_counters_at_zero(app, db_session):
    """D557, fixed. follow_user:241-242 sets `is_accepted = None` and
    skips :245-246, so neither counter moves for a manually-approving
    target. unfollow_user used to decrement both unconditionally, driving
    them to -1; it now decrements only when it deletes an accepted follow,
    so both stay at 0.

    Both halves are asserted here rather than only the follow, because the
    follow alone is consistent with the counters simply not being
    implemented.

    Also closes follow_user:252-256 -- :251's is_local() is True for a
    local target and :252's `is_accepted is None` (set at :242) selects the
    "Someone wants to follow you" Notification over :265's "You have a new
    follower". THIS notification.title ASSERTION -- not the counter
    asserts above it -- is what kills a branch-flip mutant at :241/:242:
    :245-246's increments live in the sibling `else` at :243, so an
    `is_accepted = True`/`False` flip at :242 leaves both counters
    untouched either way and a mutant there would survive the counter
    checks. Only :252's `is_accepted is None` routing, surfaced through the
    notification title, distinguishes the mutant.

    The unfollow call is made with SRC_API/bearer, closing unfollow_user's
    :279-280 authorise_api_user(auth, return_type='model') arm -- until now
    only the SRC_WEB arm (:281-282) was exercised. It is wrapped in
    `web_ctx(app, s.target)` -- logging in the WRONG user (the target, not
    the bearer-identified follower) -- deliberately: with no request
    context at all, `current_user` is anonymous, so a branch-flip mutant
    (`user = current_user` at :280) can only crash, and a crash kill is not
    a kill unless a viable non-crashing variant of the same fault also
    dies. Logging in s.target gives that mutant a non-crashing resolution
    (`user` becomes the target instead of the follower), which the
    follower-keyed row assertion below then catches (the counters no longer
    move for this pending follow, so they cannot). Do not
    "tidy away" this web_ctx call thinking it is dead weight for an
    SRC_API path -- it exists solely to give the :279-280 mutant something
    non-crashing to be wrong against.

    The unfollow is also wrapped with a BOUND recorder
    (`as calls`) asserting `calls == []` afterwards: :294's target is
    local, so a correct unfollow_user dispatches no task at all here, and
    a bare empty-list check is the precise statement of that -- the only
    call made inside this particular `with` block is the unfollow itself
    (the preceding follow_user call sits in its own separate recording
    context above), so anything nonempty means the :294 is_local() guard
    was deleted or inverted. A key-filtered check
    (`'unfollow_user' not in {k for k, _ in calls}`) was considered instead,
    but would be strictly weaker here since nothing else can appear in this
    list; plain equality states the intent precisely without hiding that.
    """
    s = _seed_followers(target_local=True)
    s.target.ap_manually_approves_followers = True
    db_session.commit()

    with _recording_task_selector():
        follow_user(s.target.id, SRC_API, bearer(s.follower))
    db_session.expire_all()

    follower = db_session.get(User, s.follower.id)
    target = db_session.get(User, s.target.id)
    assert follower.num_following == 0
    assert target.num_followers == 0

    notification = db_session.query(Notification).filter_by(user_id=s.target.id).one()
    assert notification.title == 'Someone wants to follow you'

    with web_ctx(app, s.target), _recording_task_selector() as calls:
        unfollow_user(s.target.id, SRC_API, bearer(s.follower))
    assert calls == []
    db_session.expire_all()

    assert db_session.query(UserFollower).filter_by(
        local_user_id=s.follower.id, remote_user_id=s.target.id).count() == 0
    follower = db_session.get(User, s.follower.id)
    target = db_session.get(User, s.target.id)
    assert follower.num_following == 0
    assert target.num_followers == 0


def test_unfollow_with_no_follow_row_leaves_counters_alone(app, db_session):
    """D557, fixed. Unfollowing someone never followed deletes nothing, so
    neither counter moves."""
    s = _seed_followers(target_local=True)

    with web_ctx(app, s.follower), _recording_task_selector():
        unfollow_user(s.target.id, SRC_API, bearer(s.follower))
    db_session.expire_all()

    assert db_session.get(User, s.follower.id).num_following == 0
    assert db_session.get(User, s.target.id).num_followers == 0


def test_unfollow_of_an_accepted_follow_undoes_both_counters(app, db_session):
    """D557, fixed. The positive control: an accepted follow was counted,
    so deleting it lowers both counters back to 0."""
    s = _seed_followers(target_local=True)

    with web_ctx(app, s.follower), _recording_task_selector():
        follow_user(s.target.id, SRC_API, bearer(s.follower))
        unfollow_user(s.target.id, SRC_API, bearer(s.follower))
    db_session.expire_all()

    assert db_session.get(User, s.follower.id).num_following == 0
    assert db_session.get(User, s.target.id).num_followers == 0


def test_unfollow_user_dispatches_a_task_for_a_remote_target(app, db_session):
    """Closes unfollow_user:294-295. `to_unfollow.is_local()` is False for
    a remote target (app/models.py:1252 -- `ap_id` is not None and
    `ap_profile_id` does not start with SERVER_URL), so :294's
    `if not to_unfollow.is_local():` takes its True arm and :295 dispatches
    `task_selector('unfollow_user', ...)` instead of the local-target
    delete-follow-request branch at :296-299.

    The preceding follow_user call is SRC_API/bearer too, so this test
    exercises the same :279-280 arm as the test above by construction, but
    that is not its purpose -- it exists to reach the remote branch of
    unfollow_user, which a local target (used above) cannot reach.
    """
    s = _seed_followers(target_local=False)

    with _recording_task_selector() as follow_calls:
        follow_user(s.target.id, SRC_API, bearer(s.follower))
    assert ('follow_user', {'to_follow_id': s.target.id, 'user_id': s.follower.id}) in follow_calls

    with _recording_task_selector() as unfollow_calls:
        unfollow_user(s.target.id, SRC_API, bearer(s.follower))
    assert ('unfollow_user', {'to_follow_id': s.target.id, 'user_id': s.follower.id}) in unfollow_calls


def test_a_follow_of_a_remote_user_is_stored_pending(app, db_session):
    """R265, fixed (owner ruling). follow_user stored a remote follow as
    `is_accepted = False`, the column's REFUSED value, until the peer's Accept
    arrived -- so every unanswered remote follow read as refused, and
    `announcer_is_followed`, which lets pending follows through by its own
    docstring, shut them out. It is stored as None, as the column comment says.
    """
    s = _seed_followers(target_local=False)

    with _recording_task_selector():
        follow_user(s.target.id, SRC_API, bearer(s.follower))

    row = UserFollower.query.filter_by(local_user_id=s.follower.id, remote_user_id=s.target.id).one()
    assert row.is_accepted is None
    assert announcer_is_followed(s.target.id) is True


@pytest.mark.parametrize('is_accepted', [True, None])
def test_following_a_remote_user_already_followed_is_a_no_op(app, db_session, is_accepted):
    """R265 residue (ruling: repeated actions are idempotent). Following a
    user already followed, accepted or pending, added a second row and sent
    another Follow. It now changes nothing and dispatches nothing."""
    s = _seed_followers(target_local=False)
    db.session.add(UserFollower(local_user_id=s.follower.id, remote_user_id=s.target.id,
                                is_inward=False, is_accepted=is_accepted))
    db.session.commit()

    with _recording_task_selector() as calls:
        follow_user(s.target.id, SRC_API, bearer(s.follower))

    rows = UserFollower.query.filter_by(local_user_id=s.follower.id, remote_user_id=s.target.id).all()
    assert [row.is_accepted for row in rows] == [is_accepted]
    assert calls == []


def test_following_a_local_user_already_followed_changes_no_counts(app, db_session):
    """R265 residue: the repeat follow neither adds a row nor counts twice."""
    s = _seed_followers(target_local=True)
    follow_user(s.target.id, SRC_API, bearer(s.follower))
    following, followers = s.follower.num_following, s.target.num_followers
    notifications = Notification.query.filter_by(user_id=s.target.id).count()

    follow_user(s.target.id, SRC_API, bearer(s.follower))

    assert UserFollower.query.filter_by(local_user_id=s.follower.id, remote_user_id=s.target.id).count() == 1
    assert (s.follower.num_following, s.target.num_followers) == (following, followers)
    assert Notification.query.filter_by(user_id=s.target.id).count() == notifications


def test_following_again_after_a_refusal_replaces_the_refused_row(app, db_session):
    """R265 (owner ruling): a refused follow can be retried. The refused row
    is replaced by the new pending one rather than left beside it, where
    `is_following`'s `.first()` could keep reading the refusal."""
    s = _seed_followers(target_local=False)
    db.session.add(UserFollower(local_user_id=s.follower.id, remote_user_id=s.target.id,
                                is_inward=False, is_accepted=False))
    db.session.commit()

    with _recording_task_selector():
        follow_user(s.target.id, SRC_API, bearer(s.follower))

    rows = UserFollower.query.filter_by(local_user_id=s.follower.id, remote_user_id=s.target.id).all()
    assert [row.is_accepted for row in rows] == [None]


def test_the_migration_turns_pending_false_rows_into_null_and_keeps_refusals(app, db_session):
    """R265's data migration (owner ruling). A False row whose follow request
    still exists was never refused -- a Reject deletes the request -- so it
    becomes NULL; a False row with no request left is a refusal and stays."""
    path = pathlib.Path(__file__).resolve().parent.parent / 'migrations' / 'versions' / \
        '23d65cdb8207_pending_follows_are_null.py'
    spec = importlib.util.spec_from_file_location('pending_follows_are_null', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    s = _seed_followers(target_local=False)
    refuser = make_user(s.instance, 'refuser', local=False)
    db.session.add_all([
        UserFollower(local_user_id=s.follower.id, remote_user_id=s.target.id, is_inward=False, is_accepted=False),
        UserFollower(local_user_id=s.follower.id, remote_user_id=refuser.id, is_inward=False, is_accepted=False),
        UserFollowRequest(user_id=s.follower.id, follow_id=s.target.id),
    ])
    db.session.commit()

    db.session.execute(text(migration.PENDING_FOLLOWS_SQL))
    db.session.commit()

    assert s.follower.is_following(s.target) == 'pending'
    assert s.follower.is_following(refuser) == 'refused'


# --- subscribe_user, :98-115's two flash statements ---

def test_a_third_source_reaches_the_flash_branches_the_web_arm_cannot(app, db_session):
    """Closes subscribe_user :107 and :115 -- `:98`'s true arm with `:99`
    false, then `:98`'s false arm with `:110` true -- and with them the arcs
    [104,107] and [112,115].

    NEITHER SRC_WEB NOR SRC_API CAN REACH `:107`/`:115`, and that much of the
    module docstring's original derivation is correct and kept. Under
    SRC_WEB, `:93-94` overwrites `subscribe` from
    `person.notify_new_posts(user_id)`, which runs the SAME query as `:96`'s
    `existing_notification` lookup (entity_id, user_id, type=NOTIF_USER,
    identical on both), so `subscribe == False` if and only if
    `existing_notification` is truthy -- `:98`/`:99` and `:98`/`:110` can
    only ever land in lockstep, never on the "mismatched" arms that lead to
    `:102-107` or `:111-115`. Under SRC_API, `:104`/`:112`'s
    `if src == SRC_API:` always takes the raise at `:105`/`:113` instead of
    the else.

    WHAT THAT DOES NOT ESTABLISH is that no test can reach them, and the
    module docstring records the retraction. `src` is an ordinary parameter,
    so a third source value is the way in: it is (a) not SRC_WEB, so `:93`
    skips the override and this test's `subscribe` argument survives, and
    (b) not SRC_API, so `:104`/`:112` take the else. SRC_PLD
    (app/constants.py:94, the admin preload path) IS USED HERE PURELY AS SUCH
    A VALUE. IT IS NOT HOW THIS FUNCTION IS CALLED IN PRODUCTION --
    app/api/alpha/utils/user.py:315 passes SRC_API and app/user/routes.py:758
    passes SRC_WEB, and those are the only two callers -- and this docstring
    says so rather than implying otherwise. It is not a made-up value either:
    SRC_PLD is a real constant the shared layer branches on elsewhere
    (app/shared/community.py:50, app/shared/tasks/follows.py:53, :67, :99),
    and `:104`/`:112` are written as `if src == SRC_API:` with an `else` over
    every other source, so the contract those two lines declare admits it.
    The precedent, down to the constant, is
    tests/test_shared_post_interactions.py:577 against the line-for-line twin
    `subscribe_post`, and tests/test_shared_reply_interactions.py:1018
    against `subscribe_reply`.

    `web_ctx` is used even though this is not an SRC_WEB call, because `:90`'s
    else-arm reads `current_user.id` for any non-SRC_API source and `:138`
    renders `user/_notification_toggle.html` -- which itself reads
    `current_user.id` -- for any non-SRC_API source. `make_site()` is there
    for the same render: app/utils.py:75's `render_template` calls
    `current_theme()`, which at app/utils.py:3233-3238 falls back to
    `Site.query.get(1)` and then reads `site.default_theme`, so with no Site
    row the call raises AttributeError before it can return. This is the
    reply twin's arrangement (tests/test_shared_reply_interactions.py:1046),
    and it is why this is the only test in this file that needs a Site --
    follow_user and unfollow_user render nothing.

    THE ASSERTIONS ARE ON `flashed`'s CONTENT, NOT ON `result`, and that is
    load-bearing. Under SRC_PLD `:135` is False either way, so control
    reaches `:138`'s render whether or not the flash call is there --
    deleting `flash(_(msg))` outright would still return a normal 200 and
    pass a result-only assertion silently. The row counts are the second
    half: 0 after the first call and 1 after the second separate "refused and
    flashed" from "flashed and then also wrote", which is what reaching
    `:107` or `:115` from the wrong outer arm would look like.

    The seeding call for the second half goes through SRC_API rather than
    inserting a NotificationSubscription by hand, so the row under test is
    the one subscribe_user itself writes at `:130-133` -- a hand-built row
    with a wrong `type` would silently miss `:96`'s filter and send the
    second call down `:116` instead, and the test would then pass its first
    half and fail its second for a reason having nothing to do with `:115`.
    """
    make_site()
    s = _seed_followers(target_local=True)

    with web_ctx(app, s.follower):
        result = subscribe_user(s.target.id, False, SRC_PLD)
        flashed = get_flashed_messages()

    assert result.status_code == 200
    assert flashed == ['A subscription for this user did not exist.']
    assert db_session.query(NotificationSubscription).filter_by(
        entity_id=s.target.id, user_id=s.follower.id).count() == 0

    subscribe_user(s.target.id, True, SRC_API, auth=bearer(s.follower))

    with web_ctx(app, s.follower):
        result = subscribe_user(s.target.id, True, SRC_PLD)
        flashed = get_flashed_messages()

    assert result.status_code == 200
    assert flashed == ['A subscription for this user already existed.']
    assert db_session.query(NotificationSubscription).filter_by(
        entity_id=s.target.id, user_id=s.follower.id).count() == 1


@pytest.mark.parametrize('banned', [True, None])
def test_subscribing_to_a_banned_or_missing_user_is_a_404(app, db_session, banned):
    """D559, fixed: `.one()` on `banned=False` raised NoResultFound (a 500)
    for a missing user or one banned between render and submit. Both are now
    a 404. `banned=None` stands for a missing id."""
    from werkzeug.exceptions import NotFound
    s = _seed_followers()
    target_id = s.target.id
    if banned:
        s.target.banned = True
        db.session.commit()
    else:
        target_id += 1000

    with web_ctx(app, s.follower):
        with pytest.raises(NotFound):
            subscribe_user(target_id, True, SRC_WEB)
