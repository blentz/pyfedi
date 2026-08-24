import pytest

from tests.factories import make_community, make_follow, make_instance, make_post, make_user


@pytest.fixture
def boosted(db_session):
    """A post, and a remote user who can boost it."""
    from app.models import User
    instance = make_instance('m.example')
    author = make_user(instance, 'author')
    booster = make_user(instance, 'booster')
    community = make_community()
    post = make_post(community, author, 'https://m.example/notes/1')
    return post, booster


def test_record_boost_creates_one_row(db_session, boosted):
    """Recording a boost writes a PostBoost row"""
    from app.activitypub.util import record_boost
    from app.models import PostBoost
    post, booster = boosted

    record_boost(post, booster)

    assert PostBoost.query.filter_by(post_id=post.id, user_id=booster.id).count() == 1


def test_record_boost_is_idempotent(db_session, boosted):
    """Redelivery of the same Announce does not double-count"""
    from app.activitypub.util import record_boost
    from app.models import PostBoost
    post, booster = boosted

    record_boost(post, booster)
    record_boost(post, booster)

    assert PostBoost.query.filter_by(post_id=post.id).count() == 1


def test_record_boost_populates_cache(db_session, boosted):
    """The post_boosts cache is refreshed with the booster's details"""
    from app.activitypub.util import record_boost
    post, booster = boosted

    record_boost(post, booster)

    assert len(post.post_boosts) == 1
    assert post.post_boosts[0]['user_id'] == booster.id
    assert post.post_boosts[0]['display_name'] == 'booster'


def test_remove_boost_deletes_the_row(db_session, boosted):
    """Un-boosting removes the row and empties the cache"""
    from app.activitypub.util import record_boost, remove_boost
    from app.models import PostBoost
    post, booster = boosted

    record_boost(post, booster)
    remove_boost(post, booster)

    assert PostBoost.query.filter_by(post_id=post.id).count() == 0
    assert post.post_boosts == []


def test_remove_boost_when_absent_is_a_no_op(db_session, boosted):
    """A repeated Undo does not raise; remote instances re-send"""
    from app.activitypub.util import remove_boost
    post, booster = boosted

    remove_boost(post, booster)
    remove_boost(post, booster)


def test_two_boosters_both_recorded(db_session, boosted):
    """Idempotency is per user, not per post"""
    from app.activitypub.util import record_boost
    from app.models import PostBoost
    post, booster = boosted
    other = make_user(make_instance('other.example'), 'other')

    record_boost(post, booster)
    record_boost(post, other)

    assert PostBoost.query.filter_by(post_id=post.id).count() == 2
