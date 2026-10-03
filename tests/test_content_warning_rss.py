"""A post with a content warning is syndicated as its warning, never its body or image.

The page collapses the body under the warning and the teasers show the warning in
its place; an RSS reader has no <details>, so the feed carries the warning text
alone, and no enclosure or media content for the image."""
import pytest

from app import db
from app.models import Language, Post, Tag, utcnow
from tests.factories import make_community, make_post, make_site
from tests.test_domain_routes import _domain, _post_on, _seed

BODY = '<p>the secret body</p>'
IMAGE = 'https://example.test/pic.jpg'


@pytest.fixture
def seeded(app, db_session):
    make_site()
    instance, alice, bob = _seed()
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.commit()
    return app.test_client(), make_community('microblogs'), alice


def warned_post(community, author, domain=None, warning='look away', **columns):
    if domain is None:
        post = make_post(community, author, 'https://test.piefed.local/post/cw', title='warned')
        post.url = IMAGE
    else:
        post = _post_on(domain, community, author, title='warned', url=IMAGE)
    post.body_html = BODY
    post.content_warning = warning
    for column, value in columns.items():
        setattr(post, column, value)
    db.session.commit()
    return post


def assert_only_the_warning(text):
    assert 'look away' in text
    assert 'the secret body' not in text
    assert 'pic.jpg' not in text


def test_the_domain_feed_carries_the_warning_not_the_body(seeded):
    client, community, alice = seeded
    domain = _domain()
    warned_post(community, alice, domain)

    assert_only_the_warning(client.get(f'/d/{domain.id}/feed').get_data(as_text=True))


def test_the_domain_feed_keeps_the_body_and_image_without_a_warning(seeded):
    client, community, alice = seeded
    domain = _domain()
    warned_post(community, alice, domain, warning=None)

    text = client.get(f'/d/{domain.id}/feed').get_data(as_text=True)
    assert 'the secret body' in text
    assert 'pic.jpg' in text


def test_the_tag_feed_carries_the_warning_not_the_body(seeded):
    client, community, alice = seeded
    post = warned_post(community, alice)
    post.status = 2
    tag = Tag(name='things', display_as='things')
    db.session.add(tag)
    db.session.commit()
    post.tags.append(tag)
    db.session.commit()

    assert_only_the_warning(client.get('/tag/things/feed').get_data(as_text=True))


def test_the_community_feed_carries_the_warning_not_the_body(seeded):
    client, community, alice = seeded
    warned_post(community, alice)

    assert_only_the_warning(client.get(f'/community/{community.name}/feed').get_data(as_text=True))


def test_the_community_feed_keeps_the_body_without_a_warning(seeded):
    client, community, alice = seeded
    warned_post(community, alice, warning=None)

    assert 'the secret body' in client.get(f'/community/{community.name}/feed').get_data(as_text=True)


def test_the_home_feed_carries_the_warning_not_the_body(seeded):
    client, community, alice = seeded
    warned_post(community, alice)

    assert_only_the_warning(client.get('/index/feed/all').get_data(as_text=True))


def test_a_warning_is_escaped_in_the_feed(seeded):
    client, community, alice = seeded
    warned_post(community, alice, warning='<b>x</b> & y')

    text = client.get(f'/community/{community.name}/feed').get_data(as_text=True)
    assert '<b>x</b>' not in text


def _feed_author(alice):
    """The profile feed finds a local account by its profile id (as tests/test_user_profile_feed.py's author)."""
    alice.ap_profile_id = f'https://test.piefed.local/u/{alice.user_name}'
    db.session.commit()


def test_the_profile_feed_carries_the_warning_not_the_body(seeded):
    client, community, alice = seeded
    _feed_author(alice)
    warned_post(community, alice, status=2)

    response = client.get(f'/u/{alice.user_name}/feed')
    assert response.status_code == 200
    assert '<item>' in response.get_data(as_text=True)
    assert_only_the_warning(response.get_data(as_text=True))


def test_the_profile_feed_keeps_the_body_without_a_warning(seeded):
    client, community, alice = seeded
    _feed_author(alice)
    warned_post(community, alice, warning=None, status=2)

    assert 'the secret body' in client.get(f'/u/{alice.user_name}/feed').get_data(as_text=True)
