"""The boost clause is a SQL fragment in app/utils.py, so it is tested as SQL.

This keeps the test independent of Flask-Login and the rest of the feed query;
tests/test_subscribed_feed_microblogs.py drives the whole of get_deduped_post_ids
end to end.

The fragment is IMPORTED, not copied. It used to be a hand-transcribed duplicate
with a separate test asserting the copy still appeared in
inspect.getsource(get_deduped_post_ids) -- which could pin the fragment's own text
but not the structure around it, and by the time the microblog gate moved onto the
community disjunct that test's docstring was describing a line the function no
longer contained while still passing. app/utils.py now exposes FOLLOWED_BOOSTER_SQL
(and FOLLOWED_AUTHOR_SQL, and MICROBLOG_GATE) at module level, so there is nothing
left to diverge: these tests run the production string.
"""

import pytest
from sqlalchemy import text

from app import db
from app.activitypub.util import record_boost
from app.utils import FOLLOWED_AUTHOR_SQL, FOLLOWED_BOOSTER_SQL, MICROBLOG_GATE
from tests.factories import make_community, make_follow, make_instance, make_post, make_user

# Wrapped so the production fragment can run standalone against "post" as p.
BOOST_CLAUSE = f'''SELECT p.id FROM "post" as p WHERE
{FOLLOWED_BOOSTER_SQL}'''


@pytest.fixture
def scenario(db_session):
    """A local user follows booster. Stranger authors a post. Booster boosts it."""
    instance = make_instance('m.example')
    booster = make_user(instance, 'booster')
    stranger = make_user(make_instance('other.example'), 'stranger')
    local = make_user(None, 'localuser', local=True)
    make_follow(local, booster)
    post = make_post(make_community(), stranger, 'https://other.example/notes/1')
    record_boost(post, booster)
    return local, post, stranger


def test_boosted_post_is_visible(db_session, scenario):
    """A post boosted by a followed account matches the clause"""
    local, post, _ = scenario

    ids = [row[0] for row in db.session.execute(text(BOOST_CLAUSE), {'local_user_id': local.id, 'visibility_viewer_id': local.id})]

    assert post.id in ids


def test_unboosted_post_is_not_visible(db_session, scenario):
    """A post nobody followed has boosted does not match"""
    local, _, stranger = scenario
    other_post = make_post(make_community('other'), stranger, 'https://other.example/notes/2')

    ids = [row[0] for row in db.session.execute(text(BOOST_CLAUSE), {'local_user_id': local.id, 'visibility_viewer_id': local.id})]

    assert other_post.id not in ids


def test_boost_by_unfollowed_account_is_not_visible(db_session, scenario):
    """A post boosted only by an account the viewer does NOT follow does not match.

    Distinct from test_unboosted_post_is_not_visible: that test has no post_boost row
    at all, so it never exercises the `uf2.remote_user_id = pb.user_id` join -- the
    booster-identity predicate itself. This test does: there IS a boost, by a real
    account, just not one the viewer follows.
    """
    local, _, stranger = scenario
    unfollowed_booster = make_user(make_instance('unfollowed.example'), 'unfollowedbooster')
    other_post = make_post(make_community('unfollowed-booster-community'), stranger,
                            'https://other.example/notes/3')
    record_boost(other_post, unfollowed_booster)

    ids = [row[0] for row in db.session.execute(text(BOOST_CLAUSE), {'local_user_id': local.id, 'visibility_viewer_id': local.id})]

    assert other_post.id not in ids


def test_not_visible_to_a_user_who_follows_nobody(db_session, scenario):
    """The clause is scoped to the querying user's own follows"""
    _, post, _ = scenario
    someone_else = make_user(None, 'someoneelse', local=True)

    ids = [row[0] for row in db.session.execute(text(BOOST_CLAUSE),
                                                {'local_user_id': someone_else.id, 'visibility_viewer_id': someone_else.id})]

    assert ids == []


def test_inward_follow_does_not_open_visibility(db_session):
    """is_inward=True means the remote account follows US, not the other way round.

    A UserFollower row existing between two users is not itself the entitlement --
    the direction matters. tests/factories.py hardcoded is_inward=False with no way
    to override it, which meant deleting `AND uf2.is_inward is false` from the SQL
    clause would leave every other test in this file green. This test makes that
    predicate load-bearing.
    """
    instance = make_instance('inward.example')
    booster = make_user(instance, 'inwardbooster')
    stranger = make_user(make_instance('inward-stranger.example'), 'inwardstranger')
    local = make_user(None, 'inwardlocal', local=True)
    make_follow(local, booster, is_inward=True)  # booster follows local -- NOT local following booster
    post = make_post(make_community('inward-test'), stranger, 'https://inward-stranger.example/notes/1')
    record_boost(post, booster)

    ids = [row[0] for row in db.session.execute(text(BOOST_CLAUSE), {'local_user_id': local.id, 'visibility_viewer_id': local.id})]

    assert post.id not in ids


def test_boosted_microblog_post_is_visible(db_session):
    """A boosted post in the shape ingestion really creates appears in the feed.

    Regression test: the feed clause gated on p.private is false, but Post.new sets
    private for every titleless object, so every ingested Mastodon post was excluded
    and boosted posts never appeared.
    """

    instance = make_instance('m.example')
    booster = make_user(instance, 'booster')
    stranger = make_user(make_instance('other.example'), 'stranger')
    local = make_user(None, 'localuser', local=True)
    make_follow(local, booster)
    post = make_post(make_community(), stranger, 'https://other.example/notes/1', microblog=True)
    record_boost(post, booster)

    ids = [row[0] for row in db.session.execute(text(BOOST_CLAUSE), {'local_user_id': local.id, 'visibility_viewer_id': local.id})]

    assert post.id in ids


def test_the_boost_disjunct_carries_no_microblog_gate():
    """The boost source must stay ungated on p.private.

    Adding the gate here excluded every ingested Mastodon post from the feed --
    Post.new() sets private for any titleless object -- which is the regression
    test_boosted_microblog_post_is_visible above exists to catch. This asserts the
    same thing structurally, so it fails on the edit rather than on the data, and it
    names the one place the gate does belong.
    """
    assert MICROBLOG_GATE not in FOLLOWED_BOOSTER_SQL
    assert MICROBLOG_GATE not in FOLLOWED_AUTHOR_SQL
    assert 'private' not in FOLLOWED_BOOSTER_SQL
    assert 'private' not in FOLLOWED_AUTHOR_SQL


def test_a_pending_follow_of_the_booster_does_not_surface_the_boost(db_session):
    """M1: only an accepted follow counts, as in the viewer predicate."""
    booster = make_user(make_instance('pending.example'), 'pendingbooster')
    author = make_user(make_instance('pending-author.example'), 'pendingauthor')
    local = make_user(None, 'pendinglocal', local=True)
    make_follow(local, booster, is_accepted=None)
    post = make_post(make_community('pending-test'), author, 'https://pending-author.example/notes/1')
    record_boost(post, booster)

    ids = [row[0] for row in db.session.execute(text(BOOST_CLAUSE), {'local_user_id': local.id, 'visibility_viewer_id': local.id})]

    assert post.id not in ids
