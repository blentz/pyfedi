"""app/reads/post.py: one post as a viewer may read it, and the gate before a community's list of posts.

Each refusal is a bare `Exception` with the alpha API's text (its error handler answers a subclass with a 500), so the
tests pin both the type and the text.
"""
from types import SimpleNamespace

import pytest

from app import db
from app.constants import VISIBILITY_FOLLOWERS
from app.reads.post import refuse_private_community, refuse_unreadable_post
from tests.factories import make_community, make_community_member, make_post, make_user


def members_of(*community_ids):
    """Restrictions as Restrictions answers them, belonging to `community_ids`' private communities."""
    return SimpleNamespace(private_community_ids=list(community_ids))


@pytest.fixture
def world(app, api_baseline):
    here = make_community('here')
    author = make_user(api_baseline.instance_local, 'writer', local=True)
    post = make_post(here, author, 'https://test.piefed.local/p/1')
    return SimpleNamespace(here=here, author=author, post=post, reader=api_baseline.user3)


def refused(call, *args) -> str | None:
    """The text `call(*args)` refuses with, or None; a refusal is always a bare Exception."""
    try:
        call(*args)
    except Exception as e:
        assert type(e) is Exception
        return str(e)
    return None


class TestOnePost:
    @pytest.mark.parametrize('signed_in', [False, True], ids=['anonymous', 'signed-in'])
    def test_a_public_post_is_read(self, world, signed_in):
        user_id = world.reader.id if signed_in else None
        assert refused(refuse_unreadable_post, world.post, user_id, members_of()) is None

    @pytest.mark.parametrize('signed_in', [False, True], ids=['anonymous', 'signed-in'])
    def test_a_post_you_may_not_see_is_not_found(self, world, signed_in):
        world.post.visibility = VISIBILITY_FOLLOWERS
        db.session.commit()
        user_id = world.reader.id if signed_in else None
        assert refused(refuse_unreadable_post, world.post, user_id, members_of()) == 'post not found'

    def test_its_author_sees_a_followers_only_post(self, world):
        world.post.visibility = VISIBILITY_FOLLOWERS
        db.session.commit()
        assert refused(refuse_unreadable_post, world.post, world.author.id, members_of()) is None

    @pytest.mark.parametrize('signed_in', [False, True], ids=['anonymous', 'signed-in'])
    def test_a_private_community_wants_its_members(self, world, signed_in):
        world.here.private = True
        db.session.commit()
        user_id = world.reader.id if signed_in else None
        assert refused(refuse_unreadable_post, world.post, user_id, members_of()) == \
            'Private community - membership required'

    def test_a_member_reads_a_private_communitys_post(self, world):
        world.here.private = True
        db.session.commit()
        assert refused(refuse_unreadable_post, world.post, world.reader.id, members_of(world.here.id)) is None

    def test_a_deleted_post_is_not_refused(self, world):
        world.post.deleted = True
        db.session.commit()
        assert refused(refuse_unreadable_post, world.post, None, members_of()) is None


class TestPrivateCommunityGate:
    def test_a_public_community_is_not_refused(self, world):
        assert refused(refuse_private_community, world.here.id, None, None, None) is None

    def test_a_community_nobody_holds_is_not_refused(self, world):
        assert refused(refuse_private_community, 999999, None, None, None) is None

    @pytest.mark.parametrize('name', ['here', 'here@test.piefed.local'], ids=['bare', 'with-domain'])
    def test_a_name_finds_the_community(self, world, name):
        world.here.private = True
        db.session.commit()
        assert refused(refuse_private_community, None, name, None, None) == \
            'Private community - authentication required'

    def test_a_malformed_name_is_an_invalid_request(self, world):
        assert refused(refuse_private_community, None, '@', None, None) == 'invalid_request'

    def test_a_private_community_wants_a_viewer(self, world):
        world.here.private = True
        db.session.commit()
        assert refused(refuse_private_community, world.here.id, None, None, None) == \
            'Private community - authentication required'

    def test_a_private_community_wants_a_member(self, world):
        world.here.private = True
        db.session.commit()
        assert refused(refuse_private_community, world.here.id, None, world.reader.id, world.reader) == \
            'Private community - membership required'

    def test_a_member_lists_a_private_community(self, world):
        world.here.private = True
        make_community_member(world.reader, world.here)
        db.session.commit()
        assert refused(refuse_private_community, world.here.id, None, world.reader.id, world.reader) is None
