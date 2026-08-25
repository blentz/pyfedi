import pytest

from app.instance.util import bulk_follow, is_fedi_handle
from app.models import UserFollower
from tests.factories import make_instance, make_site, make_user


class TestIsFediHandle:

    def test_bare_handle(self):
        assert is_fedi_handle('wakko@mastodon.cloud')

    def test_leading_at_is_accepted(self):
        assert is_fedi_handle('@wakko@mastodon.cloud')

    def test_not_a_handle(self):
        assert not is_fedi_handle('not a handle')


def test_bulk_follow_follows_a_resolved_remote_account(app, db_session, federation_peer):
    """The end-to-end path this fixture exists to enable: a handle becomes a follow."""
    make_instance('test.piefed.local', software='piefed')
    make_site()
    local = make_user(None, 'localuser', local=True)
    federation_peer('wakko@mastodon.cloud')

    bulk_follow(local.id, ['wakko@mastodon.cloud'])

    assert UserFollower.query.filter_by(local_user_id=local.id,
                                        is_inward=False).count() == 1


def test_bulk_follow_skips_an_account_already_followed(app, db_session, federation_peer):
    """Re-running an import must not create a second follow."""
    make_instance('test.piefed.local', software='piefed')
    make_site()
    local = make_user(None, 'localuser', local=True)
    federation_peer('wakko@mastodon.cloud')

    bulk_follow(local.id, ['wakko@mastodon.cloud'])
    bulk_follow(local.id, ['wakko@mastodon.cloud'])

    assert UserFollower.query.filter_by(local_user_id=local.id,
                                        is_inward=False).count() == 1


def test_bulk_follow_rolls_back_and_reraises_on_failure(app, db_session, federation_peer):
    """The except arm: a failure must roll back rather than leave a partial import."""
    make_instance('test.piefed.local', software='piefed')
    make_site()

    with pytest.raises(Exception):
        bulk_follow(999999, ['wakko@mastodon.cloud'])

    assert UserFollower.query.count() == 0
