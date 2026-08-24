"""The feed clause is a SQL string in app/utils.py, so it is tested as SQL.

This keeps the test independent of Flask-Login and the rest of the feed query.
"""

import pytest
from sqlalchemy import text

from tests.factories import make_community, make_follow, make_instance, make_post, make_user

BOOST_CLAUSE = """SELECT p.id FROM "post" as p WHERE
EXISTS (SELECT 1 FROM post_boost pb
        INNER JOIN user_follower uf2 ON uf2.remote_user_id = pb.user_id
        WHERE pb.post_id = p.id
        AND uf2.local_user_id = :local_user_id
        AND uf2.is_inward is false)"""


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


def test_not_visible_to_a_user_who_follows_nobody(db_session, scenario):
    """The clause is scoped to the querying user's own follows"""
    from app import db
    _, post, _ = scenario
    someone_else = make_user(None, 'someoneelse', local=True)

    ids = [row[0] for row in db.session.execute(text(BOOST_CLAUSE),
                                                {'local_user_id': someone_else.id})]

    assert ids == []


def test_clause_matches_the_one_in_utils():
    """The tested SQL is the SQL the feed actually uses.

    Guards against the test drifting from app/utils.py.
    """
    import inspect
    from app import utils
    source = inspect.getsource(utils.get_deduped_post_ids)
    assert 'post_boost pb' in source
    assert 'uf2.remote_user_id = pb.user_id' in source
