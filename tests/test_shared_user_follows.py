"""`follow_user` and `unfollow_user` (app/shared/user.py:231-300).

Both were nearly closed already -- reached incidentally by
tests/test_api_user_subscriptions.py and tests/test_ap_notify_post.py -- so
this file targets only the residual arcs a full-suite measurement showed
still missing at commit fd5b9bcd:

    follow_user     lines 242, 253, 256   arcs [241,242] [252,253]
    unfollow_user   lines 280, 295        arcs [279,280] [294,295]

That measurement also listed `subscribe_user` lines 107 and 115 as missing,
but this file does NOT attempt them -- they are structurally unreachable.
subscribe_user has exactly two callers (app/api/alpha/utils/user.py:315 on
SRC_API, app/user/routes.py:758 on SRC_WEB). On SRC_WEB, :94 recomputes
`subscribe` from `person.notify_new_posts(user_id)`
(app/models.py:1601-1603, filtered on entity_id/user_id/type == NOTIF_USER),
and :96's `existing_notification` is looked up with the IDENTICAL filter --
the same row. So on SRC_WEB, `subscribe == False` if and only if
`existing_notification is not None`: :98 true forces :99 true (reaching
:100, never :102-107), and :98 false forces :110 false (reaching :116,
never :111-115). On SRC_API, :104 and :112 both take their `raise` arm
before reaching :107/:115. No third source exists to reach the surviving
branches. (The brief's Step 3 worked example targets subscribe_user:93-94,
`if src == SRC_WEB:` discriminating correctly against ban_user's four fixed
`if SRC_WEB:` lines -- that pair is already covered by an earlier task and
is not repeated here.)

THE id-1 ADMIN TRAP applies here too, though neither function under test
reads is_admin(). _seed_followers still burns the seat as a matter of this
module's seeding convention (tests/test_shared_user_bans.py:73 and
tests/test_shared_reply_make.py:247 are the precedent) so that the id-1
user is never silently the follower or target of any assertion here.

THE COUNTER ASYMMETRY (a registered finding, not fixed here):
follow_user's manually-approving arm at :241-242 sets `is_accepted = None`
and skips the counter increments at :245-246 entirely, so neither
`user.num_following` nor `to_follow.num_followers` moves. But
unfollow_user:286-287 decrements both counters UNCONDITIONALLY, with no
matching guard. Following and then unfollowing a manually-approving user
therefore drives both counters negative.
test_follow_then_unfollow_a_manually_approving_target_drives_counters_negative
asserts both halves -- the follow alone would be consistent with the
counters simply never being implemented for that arm.

What each test below closes:

- test_follow_then_unfollow_a_manually_approving_target_drives_counters_negative
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
"""
import contextlib
from types import SimpleNamespace

from app.constants import SRC_API
from app.models import Notification, User
from app.shared.user import follow_user, unfollow_user
from tests.factories import bearer, make_instance, make_user, web_ctx


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


def test_follow_then_unfollow_a_manually_approving_target_drives_counters_negative(app, db_session):
    """PINS AN ASYMMETRY. follow_user:241-242 sets `is_accepted = None` and
    skips :245-246, so neither counter moves for a manually-approving
    target -- but unfollow_user:286-287 decrements both unconditionally,
    with no matching guard. Following and then unfollowing a
    manually-approving user therefore drives both counters NEGATIVE.

    Both halves are asserted here rather than only the follow, because the
    follow alone is consistent with the counters simply not being
    implemented. The drift is the finding; it is registered separately, not
    fixed here.

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
    follower-/target-keyed counter assertions below then catch. Do not
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

    follower = db_session.query(User).get(s.follower.id)
    target = db_session.query(User).get(s.target.id)
    assert follower.num_following == 0
    assert target.num_followers == 0

    notification = db_session.query(Notification).filter_by(user_id=s.target.id).one()
    assert notification.title == 'Someone wants to follow you'

    with web_ctx(app, s.target), _recording_task_selector() as calls:
        unfollow_user(s.target.id, SRC_API, bearer(s.follower))
    assert calls == []
    db_session.expire_all()

    follower = db_session.query(User).get(s.follower.id)
    target = db_session.query(User).get(s.target.id)
    assert follower.num_following == -1
    assert target.num_followers == -1


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
