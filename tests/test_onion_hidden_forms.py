"""What the route gate cannot see (follow-up F6, re-review N6 to N9): ids in a form field or query string, a community's
banned list, and the unread-notifications email. Off the onion an onion row answers as one nobody holds there too, and
no `Location` header spells its name."""
from datetime import timedelta
from unittest.mock import patch

import pytest
from flask import current_app

from app import cli, db
from app.constants import NOTIF_POST
from app.models import CommunityBan, CommunityMember, Feed, FeedItem, Notification, utcnow
from tests.test_instance_visible_here import get, scene  # noqa: F401 -- scene is a fixture
from tests.test_local_only_actors_hidden import ONION_ART, world  # noqa: F401 -- world is a fixture
from tests.test_onion_hidden_on_clearnet import near, normalised as strip_volatile, onion  # noqa: F401 -- a fixture
from tests.test_per_domain_ap_routes import onion_proxy, twins  # noqa: F401
from tests.test_per_domain_html_routes import Visitor
from tests.two_domains import FIRST, ONION, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')

NOBODY = 2 ** 30


def same(found, absent):
    assert (found.status_code, found.headers.get('Location'), found.get_data()) == \
        (absent.status_code, absent.headers.get('Location'), absent.get_data()), \
        (found.status_code, found.headers.get('Location'), absent.status_code, absent.headers.get('Location'))


# --- N6: the community's banned list on its moderation page --------------------------------------------------------

def test_a_communitys_banned_list_on_its_page_names_no_onion_account_on_the_first_domain(app, onion):
    db.session.add(CommunityBan(community_id=onion.first_art.id, user_id=onion.onion_bob.id, banned_by=onion.admin.id,
                                reason='BANNEDONION'))
    db.session.add(CommunityMember(community_id=onion.first_art.id, user_id=onion.onion_bob.id, is_banned=True))
    db.session.commit()
    body = get(app, '/community/art/moderate/subscribers', user=onion.admin).get_data(as_text=True)
    assert not near(body, ONION, 'BANNEDONION'), near(body, ONION, 'BANNEDONION')
    on_onion = get(app, f'/community/art@{FIRST}/moderate/subscribers', host=ONION, user=onion.admin)
    assert 'bob' in on_onion.get_data(as_text=True)
