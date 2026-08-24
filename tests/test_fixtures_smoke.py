def test_database_is_reachable_and_empty(db_session):
    """The fixture gives each test a clean database"""
    from app.models import User
    assert User.query.count() == 0


def test_factories_build_a_followed_remote_user(db_session):
    """A remote user followed by a local user can be constructed"""
    from tests.factories import make_instance, make_user, make_follow
    from app.models import UserFollower

    remote_instance = make_instance('m.example')
    alice = make_user(remote_instance, 'alice')
    local = make_user(None, 'localuser', local=True)
    make_follow(local, alice)

    assert UserFollower.query.filter_by(remote_user_id=alice.id, is_inward=False).count() == 1


def test_truncation_between_tests(db_session):
    """Rows created by the previous test are gone"""
    from app.models import User
    assert User.query.count() == 0
