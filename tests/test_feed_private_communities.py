"""`Community.private` is real access control, and the feed must enforce it for
EVERY viewer -- including the two who used to slip past it.

`Community.private` means invite-only: "only members can view. no federation."
(app/models.py:594). It is not `Post.private`, the microblog marker
(tests/test_post_private_is_only_the_microblog_marker.py) -- do not reason from
one to the other.

The defect these tests pin: `get_deduped_post_ids`' only `c.private` guard lived
inside a walrus, `if private_community_ids := community_membership_private(
current_user.id):`. A viewer who belongs to no private community makes that
falsy, so the clause was never appended and NO private restriction was applied
at all; the anonymous branch appended none either. On the "All" feed
(`community_ids=[-1]`) the community filter is just `c.show_all is true` and
`Community.show_all` defaults to True, so every private community's posts were
visible to any anonymous visitor and to any authenticated viewer with no private
membership.

That walrus shape is correct for the six OTHER filters around it -- blocked
domains, blocked instances, blocked communities, blocked users, communities
banned from -- because those are BLOCKLISTS, where an empty list genuinely means
"block nothing". This one is an ALLOWLIST EXCEPTION: the empty list means "this
viewer has no exceptions", and the base restriction `c.private is false` must
still apply. Same syntax, opposite semantics.

The four tests below are deliberately a matched set. Tests 1, 2 and 4 are
absence tests, and every one of them would also pass for a fix that appended
`c.private is false` unconditionally with no membership widening -- i.e. one that
hid private communities from their own members. Test 3 is the presence test that
rejects that over-broad fix, and is the reason this file has four tests rather
than three.

Each absence test also seeds a PUBLIC community with a post and asserts that post
IS returned by the same call. Without it, a mutation that made the query return
nothing at all would pass every absence assertion here.

`community_ids=[-1]` (the "All" feed) throughout: that is where the exposure is
widest, since it selects on `c.show_all` alone. The `local` and `popular` views
are not affected -- they build their own `(c.private is false OR c.id IN ...)` at
the caller (app/main/routes.py:124,131) and pass it in as `community_sql`.
"""
import uuid

from flask_login import login_user

from app import db
from app.utils import get_deduped_post_ids
from tests.factories import make_community, make_community_member, make_instance, make_post, make_user


def feed_ids(app, viewer):
    """The "All" feed (`community_ids=[-1]`) as `viewer`, logged in.

    A fresh uuid result_id per call keeps the Redis short-circuit
    (`feed:<user id>:<result_id>`) from replaying an earlier call's list, so
    every call really re-runs the query.
    """
    with app.test_request_context('/'):
        login_user(viewer)
        return get_deduped_post_ids(uuid.uuid4().hex, [-1], 'new')


def anon_feed_ids(app):
    """The same feed with no `login_user`, so `current_user` is Flask-Login's
    AnonymousUserMixin and the function's `current_user.is_anonymous` arm runs.
    """
    with app.test_request_context('/'):
        return get_deduped_post_ids(uuid.uuid4().hex, [-1], 'new')


def make_private_community(name: str):
    """A community with `private = True` and `show_all` left at its True default.

    show_all matters: `community_ids=[-1]` reduces the community filter to
    `c.show_all is true`, so a private community that also opted out of the All
    feed would be excluded for a reason that has nothing to do with privacy, and
    the test would pass against the unfixed code.
    """
    community = make_community(name)
    community.private = True
    db.session.commit()
    assert community.show_all is True, 'show_all must stay True or the -1 feed excludes this for the wrong reason'
    return community


def test_a_viewer_in_no_private_community_does_not_see_a_private_communitys_post(
        app, db_session, redis_double):
    """Test 1 -- the authenticated half of the leak.

    Bob belongs to no private community, so `community_membership_private(bob)`
    returns `[]`, the walrus is falsy and the `c.private` clause was never
    appended. Against the unfixed function this fails with the private post's id
    present in `ids` -- an actual leak, not an error.

    The public post pins the other direction: a fix that filtered everything
    would fail this test on its first assertion.
    """
    make_instance('privcomm1.example')
    bob = make_user(None, 'privcomm1bob', local=True)
    author = make_user(None, 'privcomm1author', local=True)
    private_community = make_private_community('privcomm1private')
    public_community = make_community('privcomm1public')
    private_post = make_post(private_community, author, 'https://privcomm1.example/posts/1')
    public_post = make_post(public_community, author, 'https://privcomm1.example/posts/2')

    ids = feed_ids(app, bob)

    assert public_post.id in ids, (
        f'the public community post should be in the All feed; got {ids!r}'
    )
    assert private_post.id not in ids, (
        f'LEAK: a viewer belonging to no private community was shown post '
        f'{private_post.id} from private community {private_community.id}; got {ids!r}'
    )


def test_an_anonymous_viewer_does_not_see_a_private_communitys_post(
        app, db_session, redis_double):
    """Test 2 -- the anonymous half.

    The anonymous branch (`if current_user.is_anonymous:`) only ever appended
    nsfw/nsfl/deleted/status predicates; it had no `c.private` clause of any
    kind, so a logged-out visitor hitting the All feed saw every private
    community's posts. Against the unfixed function this fails with the private
    post present.
    """
    make_instance('privcomm2.example')
    author = make_user(None, 'privcomm2author', local=True)
    private_community = make_private_community('privcomm2private')
    public_community = make_community('privcomm2public')
    private_post = make_post(private_community, author, 'https://privcomm2.example/posts/1')
    public_post = make_post(public_community, author, 'https://privcomm2.example/posts/2')

    ids = anon_feed_ids(app)

    assert public_post.id in ids, (
        f'the public community post should be in the anonymous All feed; got {ids!r}'
    )
    assert private_post.id not in ids, (
        f'LEAK: an anonymous viewer was shown post {private_post.id} from private '
        f'community {private_community.id}; got {ids!r}'
    )


def test_a_member_of_the_private_community_still_sees_its_posts(
        app, db_session, redis_double):
    """Test 3 -- the legitimate case, and the one that rejects an over-broad fix.

    Alice is a member of the private community, so
    `community_membership_private(alice)` returns its id and the clause must
    WIDEN to `(c.private is false OR c.id IN :private_community_ids)`. Appending
    a bare `c.private is false` for everyone would pass tests 1, 2 and 4 and fail
    only here -- which is the whole point of this test existing.

    This test passes against the UNFIXED function too (it never filtered private
    communities from anyone), so it is not a regression test for the leak; it is
    the guard rail on the fix.
    """
    make_instance('privcomm3.example')
    alice = make_user(None, 'privcomm3alice', local=True)
    author = make_user(None, 'privcomm3author', local=True)
    private_community = make_private_community('privcomm3private')
    make_community_member(alice, private_community)
    private_post = make_post(private_community, author, 'https://privcomm3.example/posts/1')

    ids = feed_ids(app, alice)

    assert private_post.id in ids, (
        f'a member of private community {private_community.id} must still see its '
        f'post {private_post.id}; got {ids!r} -- the fix is over-broad'
    )


def test_a_member_of_one_private_community_does_not_see_anothers_posts(
        app, db_session, redis_double):
    """Test 4 -- membership widens the restriction only for the communities the
    viewer is actually in.

    Carol is a member of private community A and of no other. Against the
    unfixed function the walrus IS truthy for her, so this one case was already
    filtered correctly -- meaning this test passes before the fix as well. It is
    here to prove the widening is per-community rather than "has any private
    membership at all", which is the shape a careless fix could produce.
    """
    make_instance('privcomm4.example')
    carol = make_user(None, 'privcomm4carol', local=True)
    author = make_user(None, 'privcomm4author', local=True)
    community_a = make_private_community('privcomm4privatea')
    community_b = make_private_community('privcomm4privateb')
    make_community_member(carol, community_a)
    post_a = make_post(community_a, author, 'https://privcomm4.example/posts/1')
    post_b = make_post(community_b, author, 'https://privcomm4.example/posts/2')

    ids = feed_ids(app, carol)

    assert post_a.id in ids, (
        f'Carol is a member of community {community_a.id} and must see its post; got {ids!r}'
    )
    assert post_b.id not in ids, (
        f'LEAK: Carol was shown post {post_b.id} from private community '
        f'{community_b.id}, which she is not a member of; got {ids!r}'
    )
