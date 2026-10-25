"""The Following tab on a profile lists the accounts the person follows, not the ones that never agreed to it."""
import pytest

from app import db
from app.models import UserFollower
from tests.test_per_domain_html_routes import signed_in, world  # noqa: F401
from tests.two_domains import FIRST, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')


def following_tab(page: str) -> str:
    start = page.index('id="following_tab"')
    return page[start:page.index('</ul>', start)]


@pytest.mark.parametrize('viewer_is_owner', [True, False])
def test_the_following_tab_lists_only_accepted_follows(app, world, two_domains, viewer_is_owner):
    first = two_domains.domains[0]
    follower = world.viewers[FIRST]
    for name, state in (('accepted', True), ('pending', None), ('declined', False)):
        target = two_domains.local_user(name, first)
        db.session.add(UserFollower(local_user_id=follower.id, remote_user_id=target.id, is_inward=False,
                                    is_accepted=state))
    db.session.commit()
    visitor = signed_in(app, follower if viewer_is_owner else world.bob[FIRST], FIRST)

    tab = following_tab(visitor.get(f'/u/{follower.user_name}').get_data(as_text=True))

    assert 'accepted' in tab and 'pending' not in tab and 'declined' not in tab
