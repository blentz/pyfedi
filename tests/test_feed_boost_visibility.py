"""The feed clause is a SQL string in app/utils.py, so it is tested as SQL.

This keeps the test independent of Flask-Login and the rest of the feed query.
"""

import re

import pytest
from sqlalchemy import text

from tests.factories import make_community, make_follow, make_instance, make_post, make_user

# The boost disjunct exactly as it appears in get_deduped_post_ids (app/utils.py),
# wrapped so it can run standalone against "post" as p.
#
# There is no `p.private is false` gate here: Post.private is an unlisted marker, not
# a followers-only flag (Post.new() sets it for ANY titleless object, which is every
# ingested Mastodon Note), so gating on it excluded every boosted post from the feed.
# Non-public content (followers-only, direct) is kept out by refusal at ingest
# (create_post / create_post_reply), not by this clause.
BOOST_CLAUSE_BODY = """EXISTS (SELECT 1 FROM post_boost pb
                                  INNER JOIN user_follower uf2 ON uf2.remote_user_id = pb.user_id
                                  WHERE pb.post_id = p.id
                                  AND uf2.local_user_id = :local_user_id
                                  AND uf2.is_inward is false)"""

BOOST_CLAUSE = f"""SELECT p.id FROM "post" as p WHERE
{BOOST_CLAUSE_BODY}"""


@pytest.fixture
def scenario(db_session):
    """A local user follows booster. Stranger authors a post. Booster boosts it."""
    from app.activitypub.util import record_boost
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
    from app import db
    local, post, _ = scenario

    ids = [row[0] for row in db.session.execute(text(BOOST_CLAUSE), {'local_user_id': local.id})]

    assert post.id in ids


def test_unboosted_post_is_not_visible(db_session, scenario):
    """A post nobody followed has boosted does not match"""
    from app import db
    local, _, stranger = scenario
    other_post = make_post(make_community('other'), stranger, 'https://other.example/notes/2')

    ids = [row[0] for row in db.session.execute(text(BOOST_CLAUSE), {'local_user_id': local.id})]

    assert other_post.id not in ids


def test_boost_by_unfollowed_account_is_not_visible(db_session, scenario):
    """A post boosted only by an account the viewer does NOT follow does not match.

    Distinct from test_unboosted_post_is_not_visible: that test has no post_boost row
    at all, so it never exercises the `uf2.remote_user_id = pb.user_id` join -- the
    booster-identity predicate itself. This test does: there IS a boost, by a real
    account, just not one the viewer follows.
    """
    from app import db
    from app.activitypub.util import record_boost
    local, _, stranger = scenario
    unfollowed_booster = make_user(make_instance('unfollowed.example'), 'unfollowedbooster')
    other_post = make_post(make_community('unfollowed-booster-community'), stranger,
                            'https://other.example/notes/3')
    record_boost(other_post, unfollowed_booster)

    ids = [row[0] for row in db.session.execute(text(BOOST_CLAUSE), {'local_user_id': local.id})]

    assert other_post.id not in ids


def test_not_visible_to_a_user_who_follows_nobody(db_session, scenario):
    """The clause is scoped to the querying user's own follows"""
    from app import db
    _, post, _ = scenario
    someone_else = make_user(None, 'someoneelse', local=True)

    ids = [row[0] for row in db.session.execute(text(BOOST_CLAUSE),
                                                {'local_user_id': someone_else.id})]

    assert ids == []


def test_inward_follow_does_not_open_visibility(db_session):
    """is_inward=True means the remote account follows US, not the other way round.

    A UserFollower row existing between two users is not itself the entitlement --
    the direction matters. tests/factories.py hardcoded is_inward=False with no way
    to override it, which meant deleting `AND uf2.is_inward is false` from the SQL
    clause would leave every other test in this file green. This test makes that
    predicate load-bearing.
    """
    from app import db
    from app.activitypub.util import record_boost
    instance = make_instance('inward.example')
    booster = make_user(instance, 'inwardbooster')
    stranger = make_user(make_instance('inward-stranger.example'), 'inwardstranger')
    local = make_user(None, 'inwardlocal', local=True)
    make_follow(local, booster, is_inward=True)  # booster follows local -- NOT local following booster
    post = make_post(make_community('inward-test'), stranger, 'https://inward-stranger.example/notes/1')
    record_boost(post, booster)

    ids = [row[0] for row in db.session.execute(text(BOOST_CLAUSE), {'local_user_id': local.id})]

    assert post.id not in ids


def test_boosted_microblog_post_is_visible(db_session):
    """A boosted post in the shape ingestion really creates appears in the feed.

    Regression test: the feed clause gated on p.private is false, but Post.new sets
    private for every titleless object, so every ingested Mastodon post was excluded
    and boosted posts never appeared.
    """
    from app import db
    from app.activitypub.util import record_boost

    instance = make_instance('m.example')
    booster = make_user(instance, 'booster')
    stranger = make_user(make_instance('other.example'), 'stranger')
    local = make_user(None, 'localuser', local=True)
    make_follow(local, booster)
    post = make_post(make_community(), stranger, 'https://other.example/notes/1', microblog=True)
    record_boost(post, booster)

    ids = [row[0] for row in db.session.execute(text(BOOST_CLAUSE), {'local_user_id': local.id})]

    assert post.id in ids


def test_clause_matches_the_one_in_utils():
    """The tested SQL is the SQL the feed actually uses.

    Normalises whitespace on both sides and asserts the FULL boost-disjunct body --
    the join, the post_id correlation, the viewer scoping, and the is_inward filter --
    appears verbatim in get_deduped_post_ids' source, not just a couple of
    loosely-matched substrings. A clause missing the viewer scoping or the is_inward
    filter would have passed the old two-substring version of this test; it cannot
    pass this one. Guards against the test drifting from app/utils.py.

    Containment alone has a directional blind spot: the ungated BOOST_CLAUSE_BODY is
    trivially a substring of a *gated* production clause too, since
    "EXISTS (...)" is a substring of "(p.private is false AND EXISTS (...))". So the
    assertion above would keep passing even if `p.private is false AND` were
    re-added to the boost disjunct -- exactly the regression this task exists to
    prevent. The second assertion below closes that hole: it isolates the boost
    disjunct's own sources.append(...) block (identified by containing
    "post_boost pb", which is unique to it) and asserts the gate text is absent from
    THAT block specifically -- not a blanket absence check against the whole
    function body, which would incorrectly fail on the untouched
    `if not include_following: post_id_where.append('p.private is false')` line
    elsewhere in get_deduped_post_ids.
    """
    import inspect
    from app import utils

    def normalize(sql: str) -> str:
        return re.sub(r'\s+', ' ', sql).strip()

    source = inspect.getsource(utils.get_deduped_post_ids)
    assert normalize(BOOST_CLAUSE_BODY) in normalize(source)

    append_blocks = re.findall(r'sources\.append\("""(.*?)"""\)', source, re.DOTALL)
    boost_blocks = [b for b in append_blocks if 'post_boost pb' in b]
    assert len(boost_blocks) == 1, \
        f'expected exactly one boost sources.append(...) block, found {len(boost_blocks)}'
    boost_block = normalize(boost_blocks[0])
    assert 'p.private is false' not in boost_block, \
        'the p.private gate has been re-added to the boost disjunct'
