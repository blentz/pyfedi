"""A private feed answers a reader who may not read it exactly as a feed nobody holds (plan F4, task 7; the oracle parked
in Task 4's review): the same status and the same body, for a page, an ActivityPub client, RSS and the API. Its owner and
its members still open it."""
import pytest

from app import db
from app.models import FeedMember
from tests.factories import make_feed_member
from tests.test_onion_feeds_hidden import answer, signed_get
from tests.test_onion_hidden_api_ids import call
from tests.three_domains import FIRST, three_domains, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')

AP = {'Accept': 'application/activity+json'}


@pytest.fixture(autouse=True)
def public_site(site):
    site.private_instance = False
    db.session.commit()
    return site


@pytest.fixture
def secret(three_domains):  # noqa: F811
    d = three_domains
    owner, member, stranger = (d.account(name, d.first) for name in ('owner', 'member', 'stranger'))
    feed = d.local_feed('secretfeed', d.first, owner, public=False)
    make_feed_member(member, feed)
    return feed, owner, member, stranger


def same(hidden, missing):
    assert hidden.status_code == 404
    assert answer(hidden) == (missing.status_code, missing.get_data().replace(b'nosuchfeed', b'secretfeed'))


@pytest.mark.parametrize('who', [None, 'stranger'])
def test_a_private_feed_page_answers_a_non_member_as_a_missing_feed(app, secret, who):
    user = secret[3] if who else None
    same(signed_get(app, user, '/f/secretfeed', FIRST), signed_get(app, user, '/f/nosuchfeed', FIRST))
    same(signed_get(app, user, '/f/secretfeed.rss', FIRST), signed_get(app, user, '/f/nosuchfeed.rss', FIRST))


@pytest.mark.parametrize('path', ['', '/outbox', '/following', '/followers', '/moderators'])
def test_a_private_feed_answers_an_activitypub_client_as_a_missing_feed(app, secret, path):
    same(signed_get(app, None, f'/f/secretfeed{path}', FIRST, AP), signed_get(app, None, f'/f/nosuchfeed{path}', FIRST, AP))


@pytest.mark.parametrize('who', ['owner', 'member'])
def test_its_owner_and_its_members_still_open_it(app, secret, who):
    user = secret[1] if who == 'owner' else secret[2]
    assert signed_get(app, user, '/f/secretfeed', FIRST).status_code == 200


def test_the_api_answers_a_reader_who_is_not_its_owner_as_for_a_missing_feed(app, secret, monkeypatch):
    """The API shows a private feed to its owner alone (`feed_view`), so a member is refused there too, now with the
    missing feed's answer rather than `access_denied`."""
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    feed, owner, member, stranger = secret
    missing = call(app, stranger, 'GET', '/api/alpha/feed?id=2147483000')
    assert missing.status_code == 400
    for reader in (stranger, member):
        hidden = call(app, reader, 'GET', f'/api/alpha/feed?id={feed.id}')
        assert answer(hidden) == answer(missing), reader.user_name
    shown = call(app, owner, 'GET', f'/api/alpha/feed?id={feed.id}')
    assert shown.status_code == 200 and shown.json['id'] == feed.id
    assert FeedMember.query.filter_by(feed_id=feed.id).count() == 2   # owner and member, as made
