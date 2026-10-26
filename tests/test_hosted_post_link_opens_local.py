"""Follow-up F2: a link to a post on one of our own domains opens the local post on the domain the viewer is browsing.

Our own hosts are never fetched over HTTP. The viewer is held to what the post page itself enforces (not the peer
gate), and a post the viewer may not open gets the same "not found" answer as one that does not exist."""
from unittest.mock import patch

import pytest
from flask import session as flask_session
from flask_wtf.csrf import generate_csrf

from app import db
from app.constants import POST_STATUS_DRAFT, ROLE_ADMIN_NAME, VISIBILITY_FOLLOWERS
from app.discovery import views
from app.models import Role, user_role
from app.utils import set_setting
from tests.discovery_fixtures import fresh_cache  # noqa: F401
from tests.factories import make_user
from tests.test_admin_federation import login
from tests.test_local_only_invisible import _post, world  # noqa: F401

pytestmark = pytest.mark.usefixtures('site', 'fresh_cache')


def _token(app, client, base_url):
    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction(base_url=base_url) as session:
        session['csrf_token'] = raw
    return token


def _search(app, client, address, host=None):
    base_url = f'http://{host}' if host else None
    token = _token(app, client, base_url)
    with patch('app.search.routes.resolve_remote_post_from_search', return_value=None) as resolver:
        response = client.post('/retrieve_remote_post', data={'address': address, 'csrf_token': token},
                               base_url=base_url)
    return response, resolver


def _viewer(client, name='carol', host=None):
    viewer = make_user(None, name, local=True)
    viewer.verified = True
    db.session.commit()
    with client.session_transaction(base_url=f'http://{host}' if host else None) as session:
        session['_user_id'] = str(viewer.id)
        session['_fresh'] = True
    return viewer


def test_a_post_url_on_the_first_domain_opens_the_local_post_here(app, world):
    post = _post(app, world.clearnet, world.bob)
    client = app.test_client()
    _viewer(client)

    response, resolver = _search(app, client, f"https://{app.config['SERVER_NAME']}/post/{post.id}")

    assert response.status_code == 302 and response.headers['Location'].endswith(f'/post/{post.id}')
    assert resolver.call_count == 0


def test_a_post_url_on_the_onion_opens_the_local_post_on_the_onion(app, world):
    post = _post(app, world.hidden, world.onionite)
    client = app.test_client()
    _viewer(client, host='abc.onion')

    response, resolver = _search(app, client, f'http://abc.onion/post/{post.id}', host='abc.onion')

    assert response.status_code == 302
    location = response.headers['Location']
    assert location.endswith(f'/post/{post.id}') and app.config['SERVER_NAME'] not in location
    assert resolver.call_count == 0


def test_a_first_domain_url_viewed_from_the_onion_opens_the_post_on_the_onion(app, world):
    post = _post(app, world.clearnet, world.bob)
    client = app.test_client()
    _viewer(client, host='abc.onion')

    response, _ = _search(app, client, f"https://{app.config['SERVER_NAME']}/c/clearnet/p/{post.id}/a-post",
                          host='abc.onion')

    assert response.status_code == 302 and response.headers['Location'].endswith(f'/post/{post.id}')
    assert app.config['SERVER_NAME'] not in response.headers['Location']


def test_a_remote_url_is_still_resolved_as_before(app, world):
    client = app.test_client()
    _viewer(client)

    response, resolver = _search(app, client, 'https://remote.example/post/1')

    assert response.status_code == 200 and b'Post not found.' in response.data
    assert resolver.call_args.args[0] == 'https://remote.example/post/1'


def test_a_hosted_url_that_names_no_post_is_not_found_and_not_fetched(app, world):
    client = app.test_client()
    _viewer(client)

    response, resolver = _search(app, client, f"https://{app.config['SERVER_NAME']}/u/alice")

    assert response.status_code == 200 and b'Post not found.' in response.data
    assert resolver.call_count == 0


def _followers_only(post, world):
    post.visibility = VISIBILITY_FOLLOWERS


def _draft(post, world):
    post.status = POST_STATUS_DRAFT


def _deleted_by_a_moderator(post, world):
    post.deleted = True
    post.deleted_by = world.alice.id


def _in_a_banned_community(post, world):
    post.community.banned = True


def _in_a_private_community(post, world):
    post.community.private = True


def _nsfw_in_a_restricted_country(post, world):
    post.nsfw = True
    set_setting('nsfw_country_restriction', 'XX')


@pytest.mark.parametrize('hide', [_followers_only, _draft, _deleted_by_a_moderator, _in_a_banned_community,
                                  _in_a_private_community, _nsfw_in_a_restricted_country])
def test_a_post_the_viewer_may_not_open_is_not_found(app, world, hide):
    post = _post(app, world.clearnet, world.bob)
    hide(post, world)
    client = app.test_client()
    viewer = _viewer(client)
    if hide is _nsfw_in_a_restricted_country:
        viewer.ip_address_country = 'XX'
    db.session.commit()

    response, resolver = _search(app, client, f"https://{app.config['SERVER_NAME']}/post/{post.id}")

    assert response.status_code == 200 and b'Post not found.' in response.data
    assert resolver.call_count == 0


def test_a_post_its_author_deleted_still_opens(app, world):
    post = _post(app, world.clearnet, world.bob)
    post.deleted = True
    post.deleted_by = post.user_id
    db.session.commit()
    client = app.test_client()
    _viewer(client)

    response, _ = _search(app, client, f"https://{app.config['SERVER_NAME']}/post/{post.id}")

    assert response.status_code == 302


@pytest.mark.parametrize('who', ['author', 'moderator', 'admin'])
def test_staff_and_the_author_may_open_what_is_hidden_from_others(app, world, who):
    post = _post(app, world.clearnet, world.bob)
    post.deleted = True
    post.deleted_by = world.alice.id
    post.status = POST_STATUS_DRAFT
    db.session.commit()
    client = app.test_client()
    viewer = {'author': world.bob, 'moderator': world.alice, 'admin': None}[who]
    if viewer is None:
        viewer = _viewer(client)
        role = Role(name=ROLE_ADMIN_NAME)
        db.session.add(role)
        db.session.flush()
        db.session.execute(user_role.insert().values(user_id=viewer.id, role_id=role.id))
        db.session.commit()
    else:
        login(client, viewer)

    response, _ = _search(app, client, f"https://{app.config['SERVER_NAME']}/post/{post.id}")

    assert response.status_code == 302


# ---- the discovery video result ---------------------------------------------------------------

def _video(app, client, url, host=None):
    base_url = f'http://{host}' if host else None
    token = _token(app, client, base_url)
    with patch.object(views, 'resolve_remote_post_from_search') as resolver:
        response = client.post('/discovery/video/resolve', data={'csrf_token': token, 'url': url, 'q': 'zq'},
                               base_url=base_url)
    return response, resolver


def test_an_own_domain_video_result_opens_the_local_post_here(app, world):
    post = _post(app, world.clearnet, world.bob)
    client = app.test_client()
    _viewer(client)

    response, resolver = _video(app, client, f"https://{app.config['SERVER_NAME']}/post/{post.id}")

    assert response.status_code == 302 and response.headers['Location'].endswith(f'/post/{post.id}')
    assert resolver.call_count == 0


def test_an_own_domain_video_result_on_the_onion_opens_the_post_on_the_onion(app, world):
    post = _post(app, world.hidden, world.onionite)
    client = app.test_client()
    _viewer(client, host='abc.onion')

    response, resolver = _video(app, client, f"https://{app.config['SERVER_NAME']}/post/{post.id}", host='abc.onion')

    assert response.status_code == 302 and response.headers['Location'].endswith(f'/post/{post.id}')
    assert app.config['SERVER_NAME'] not in response.headers['Location']


def test_an_own_domain_video_result_the_viewer_may_not_open_goes_back_to_the_search(app, world):
    post = _post(app, world.clearnet, world.bob)
    post.visibility = VISIBILITY_FOLLOWERS
    db.session.commit()
    client = app.test_client()
    _viewer(client)

    response, resolver = _video(app, client, f"https://{app.config['SERVER_NAME']}/post/{post.id}")

    assert response.status_code == 302 and '/search' in response.headers['Location']
    assert resolver.call_count == 0
