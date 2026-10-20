"""Local-only posts and comments carry a "Local" badge beside the author (plan B task 11).

The badge follows the `post.local_only` / `post_reply.local_only` columns, not upstream's `Community.local_only`.
It shows in the teaser (main and dillo themes), the full post (title line, byline and event byline) and the comment.
"""
import pytest

from datetime import timedelta

from app import db
from app.constants import POST_TYPE_EVENT
from app.models import Event, utcnow
from tests.factories import make_community, make_instance, make_post, make_post_reply, make_user

pytestmark = pytest.mark.usefixtures('site')

BADGE = '<span class="badge text-bg-secondary" title="Not shared with other servers">Local</span>'


@pytest.fixture
def world(app, db_session):
    make_instance(app.config['SERVER_NAME'], software='piefed')
    viewer = make_user(None, 'badgeviewer', local=True)
    community = make_community('badgecomm')
    return viewer, community


def _post(community, author, n, local_only, **extra):
    post = make_post(community, author, f'https://test.piefed.local/post/badge{n}', title=f'badge post {n}')
    post.local_only = local_only
    for key, value in extra.items():
        setattr(post, key, value)
    db.session.commit()
    return post


def _get(app, viewer, path, csrf=False, monkeypatch=None):
    if csrf:
        monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # the post page renders form.csrf_token()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(viewer.id)
        sess['_fresh'] = True
    response = client.get(path)
    assert response.status_code == 200, f'{path}: {response.status_code}'
    return response.get_data(as_text=True)


def test_a_local_only_post_teaser_carries_the_badge_once(app, world):
    viewer, community = world
    _post(community, viewer, 1, True)
    assert _get(app, viewer, '/home/new/all').count(BADGE) == 1


def test_a_federated_post_teaser_has_no_badge(app, world):
    viewer, community = world
    _post(community, viewer, 1, False)
    assert BADGE not in _get(app, viewer, '/home/new/all')


def test_a_microblog_teaser_carries_the_badge(app, world):
    viewer, _community = world
    community = make_community('microblogs')   # is_microblog() needs the community of that name
    _post(community, viewer, 1, True, microblog=True, title='', private=True, body_html='<p>hi</p>')
    assert _get(app, viewer, '/c/microblogs').count(BADGE) == 1


def test_the_dillo_teaser_carries_the_badge(app, world):
    viewer, community = world
    viewer.theme = 'dillo'
    _post(community, viewer, 1, True)
    body = _get(app, viewer, '/home/new/all')
    assert 'themes/dillo/styles.css' in body
    assert body.count(BADGE) == 1


def test_a_local_only_full_post_carries_the_badge_once(app, world, monkeypatch):
    viewer, community = world
    post = _post(community, viewer, 1, True)
    body = _get(app, viewer, f'/post/{post.id}', True, monkeypatch)
    assert body.count(BADGE) == 1


def test_a_local_only_microblog_full_post_carries_the_badge(app, world, monkeypatch):
    viewer, _community = world
    community = make_community('microblogs')
    post = _post(community, viewer, 1, True, microblog=True, title='', private=True, body_html='<p>hi</p>')
    assert _get(app, viewer, f'/post/{post.id}', True, monkeypatch).count(BADGE) == 1


def test_a_local_only_event_post_carries_the_badge(app, world, monkeypatch):
    viewer, community = world
    post = _post(community, viewer, 1, True, type=POST_TYPE_EVENT)
    db.session.add(Event(post_id=post.id, start=utcnow() + timedelta(days=2), end=utcnow() + timedelta(days=3)))
    db.session.commit()
    assert BADGE in _get(app, viewer, f'/post/{post.id}', True, monkeypatch)


def test_a_federated_full_post_has_no_badge(app, world, monkeypatch):
    viewer, community = world
    post = _post(community, viewer, 1, False)
    assert BADGE not in _get(app, viewer, f'/post/{post.id}', True, monkeypatch)


def test_a_local_only_comment_carries_the_badge(app, world, monkeypatch):
    viewer, community = world
    post = _post(community, viewer, 1, False)
    reply = make_post_reply(post, viewer, 'quiet words')
    reply.local_only = True
    db.session.commit()
    body = _get(app, viewer, f'/post/{post.id}', True, monkeypatch)
    assert 'quiet words' in body
    assert body.count(BADGE) == 1


def test_a_federated_comment_has_no_badge(app, world, monkeypatch):
    viewer, community = world
    post = _post(community, viewer, 1, False)
    make_post_reply(post, viewer, 'plain words')
    body = _get(app, viewer, f'/post/{post.id}', True, monkeypatch)
    assert 'plain words' in body
    assert BADGE not in body
