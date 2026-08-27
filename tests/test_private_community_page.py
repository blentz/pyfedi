"""A private community's HTML page is for its members only.

`Community.private` means invite-only: "only members can view. no federation."
(app/models.py:594). It is real access control, unlike `Post.private`, the
microblog marker (tests/test_post_private_is_only_the_microblog_marker.py) --
do not reason from one to the other.

The defect these tests pin: `show_community` (app/community/routes.py) never
looked at `community.private` at all. The only `.private` matches inside its
body were `community.private_mods`, a different column, and a comment about the
`Post` microblog marker. Meanwhile the RSS view (app/community/routes.py:720)
and the iCal view (:784) of the SAME community both `abort(403)` on
`community.private`. So the two feeds were forbidden while the HTML page --
the one a browser actually reaches, and the one that lists the posts, the
sidebar, the moderators and the description -- rendered in full for anybody.

403, not 404, and that is deliberate. `show_community` aborts 404 for
`community.banned`, so the two statuses now sit three lines apart and the
difference deserves a reason. There is no convention in this codebase of using
404 to avoid confirming a private community exists: every other private-
community refusal is a 403 or its API equivalent --
app/community/routes.py:720 and :784 (the sibling feeds), app/post/routes.py:96
and :102, app/activitypub/routes.py:526, :2124, :2153 and :2756,
app/shared/tasks/pages.py:153, with app/api/alpha/views.py:309, :614 and :640
raising 'Private community - membership required'. Nothing anywhere aborts 404
on `.private`. Matching the two sibling views on the same community was
therefore the only consistent choice; hiding existence would have been a new
convention invented in one route.

Moderators are covered. `community_membership_private` (app/utils.py) selects
CommunityMember rows with `cm.is_banned is false` and no role predicate, while
`moderating_communities` selects the same table with an EXTRA
`is_moderator OR is_owner` predicate (app/utils.py:2565). The moderator set is
therefore a subset of the membership set, and a private community's moderator
cannot be locked out by this check. A community's OWNER, likewise, is an
is_owner CommunityMember row. `test_a_moderator_of_a_private_community_sees_it`
drives that path rather than asserting it.

The viewer is always logged in here: `Site.private_instance` defaults to True
(app/models.py:3855) and `show_community` is wrapped in
`login_required_if_private_instance`, so an anonymous GET is bounced to
/auth/login before any of this runs.
"""

import pytest

from app import db
from tests.factories import make_community, make_community_member, make_instance, make_post, make_user

pytestmark = pytest.mark.usefixtures('site')


def logged_in_client(app, user):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True
    return client


@pytest.fixture
def private_community(db_session):
    """One private community holding one post, plus a viewer who is not a
    member of it. Tests that need a member add their own CommunityMember row.
    """
    make_instance('test.piefed.local', software='piefed')
    author = make_user(make_instance('privpage.example'), 'privpageauthor')
    viewer = make_user(None, 'privpageviewer', local=True)
    community = make_community('privpagecomm')
    community.private = True
    db.session.commit()
    post = make_post(community, author, 'https://privpage.example/notes/1', title='a secret post')
    return viewer, community, post


def test_a_non_member_is_refused_a_private_communitys_page(app, private_community):
    """The disclosure itself: before the fix this was a 200 whose body listed
    the community's posts.
    """
    viewer, community, post = private_community

    response = logged_in_client(app, viewer).get(f'/c/{community.name}')

    assert response.status_code == 403
    assert f'/post/{post.id}' not in response.get_data(as_text=True)


def test_a_member_sees_a_private_communitys_page(app, private_community):
    """The legitimate-access test, and the one that rejects an over-broad fix.
    A check that refused every private community outright would pass every
    other test in this file and fail here.
    """
    viewer, community, post = private_community
    make_community_member(viewer, community)

    response = logged_in_client(app, viewer).get(f'/c/{community.name}')

    assert response.status_code == 200
    assert f'/post/{post.id}' in response.get_data(as_text=True)


def test_a_moderator_of_a_private_community_sees_it(app, private_community):
    """Moderators are members: `community_membership_private` has no role
    predicate, so an is_moderator CommunityMember row satisfies it. Driven
    rather than asserted, because "mods are members" is the assumption this
    fix would be worst to get wrong.
    """
    viewer, community, post = private_community
    make_community_member(viewer, community, is_moderator=True)

    response = logged_in_client(app, viewer).get(f'/c/{community.name}')

    assert response.status_code == 200
    assert f'/post/{post.id}' in response.get_data(as_text=True)


def test_a_non_private_community_is_unaffected(app, db_session, site):
    """The control: same route, same viewer with no membership, `private` left
    at its column default. Still 200.
    """
    make_instance('test.piefed.local', software='piefed')
    author = make_user(make_instance('pubpage.example'), 'pubpageauthor')
    viewer = make_user(None, 'pubpageviewer', local=True)
    community = make_community('pubpagecomm')
    post = make_post(community, author, 'https://pubpage.example/notes/1', title='an open post')

    response = logged_in_client(app, viewer).get(f'/c/{community.name}')

    assert response.status_code == 200
    assert f'/post/{post.id}' in response.get_data(as_text=True)
