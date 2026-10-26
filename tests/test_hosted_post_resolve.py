"""Follow-up F2 fix round, item 3: `resolve_object` and the `web+ap://` protocol handler, given a URL of a post on one
of our own domains, answer with the local post under the viewer gates the post page applies. A post the caller may not
open is 'No object found.', the answer for a post that does not exist."""
import pytest
from flask import g

from app import db
from app.api.alpha.utils.misc import get_resolve_object
from app.constants import POST_STATUS_DRAFT, VISIBILITY_FOLLOWERS
from app.models import Site
from tests.factories import make_user
from tests.test_local_only_invisible import _post, world  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture(autouse=True)
def api_globals(app, db_session):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)


def caller(name='carol'):
    user = make_user(None, name, local=True)
    user.verified = True
    db.session.commit()
    return user


def resolve(user, url):
    return get_resolve_object(f'Bearer {user.encode_jwt_token()}', {'q': url})


def _followers_only(post, world):
    post.visibility = VISIBILITY_FOLLOWERS


def _draft(post, world):
    post.status = POST_STATUS_DRAFT


def _deleted(post, world):
    post.deleted = True
    post.deleted_by = world.alice.id


def _banned_community(post, world):
    post.community.banned = True


def _private_community(post, world):
    post.community.private = True


HIDDEN = [_followers_only, _draft, _deleted, _banned_community, _private_community]


@pytest.mark.parametrize('path', ['/post/{id}', '/c/clearnet/p/{id}/a-post', '/p/{id}'])
def test_resolve_object_returns_the_local_post_for_a_first_domain_url(app, world, path):
    post = _post(app, world.clearnet, world.bob)
    found = resolve(caller(), f"https://{app.config['SERVER_NAME']}{path.format(id=post.id)}")
    assert found['post']['post']['id'] == post.id


def test_resolve_object_returns_the_local_post_for_an_onion_url(app, world):
    post = _post(app, world.hidden, world.onionite)
    assert resolve(caller(), f'http://abc.onion/post/{post.id}')['post']['post']['id'] == post.id


def test_resolve_object_returns_it_for_the_posts_stored_url_too(app, world):
    post = _post(app, world.clearnet, world.bob)
    assert resolve(caller(), post.ap_id)['post']['post']['id'] == post.id


@pytest.mark.parametrize('hide', HIDDEN)
@pytest.mark.parametrize('how', ['path', 'stored'])
def test_resolve_object_does_not_return_a_post_the_caller_may_not_open(app, world, hide, how):
    post = _post(app, world.clearnet, world.bob)
    hide(post, world)
    db.session.commit()
    url = post.ap_id if how == 'stored' else f"https://{app.config['SERVER_NAME']}/post/{post.id}"
    with pytest.raises(Exception) as refused:
        resolve(caller(), url)
    assert str(refused.value) == 'No object found.'


def test_resolve_object_over_http_returns_the_local_post(app, world, monkeypatch):
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    post = _post(app, world.clearnet, world.bob)
    user = caller()
    response = app.test_client().get('/api/alpha/resolve_object',
                                     query_string={'q': f"https://{app.config['SERVER_NAME']}/post/{post.id}"},
                                     headers={'Authorization': f'Bearer {user.encode_jwt_token()}'})
    assert response.status_code == 200 and response.json['post']['post']['id'] == post.id


# ---- the protocol handler -----------------------------------------------------------------------------------------

def handler(app, user, url, host=None):
    client = app.test_client()
    base_url = f'http://{host}' if host else None
    with client.session_transaction(base_url=base_url) as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return client.get('/protocol_handler', query_string={'to': url}, base_url=base_url)


def test_the_protocol_handler_opens_the_local_post_on_the_current_domain(app, world):
    post = _post(app, world.clearnet, world.bob)
    response = handler(app, caller(), f"web+ap://{app.config['SERVER_NAME']}/post/{post.id}")
    assert response.status_code == 302 and response.headers['Location'].endswith(f'/post/{post.id}')


def test_the_protocol_handler_on_the_onion_stays_on_the_onion(app, world):
    post = _post(app, world.clearnet, world.bob)
    response = handler(app, caller(), f"web+ap://{app.config['SERVER_NAME']}/post/{post.id}", host='abc.onion')
    location = response.headers['Location']
    assert response.status_code == 302 and location.endswith(f'/post/{post.id}')
    assert app.config['SERVER_NAME'] not in location


@pytest.mark.parametrize('hide', HIDDEN)
def test_the_protocol_handler_does_not_open_a_post_the_viewer_may_not_open(app, world, hide):
    post = _post(app, world.clearnet, world.bob)
    hide(post, world)
    db.session.commit()
    response = handler(app, caller(), f"web+ap://{app.config['SERVER_NAME']}/post/{post.id}")
    assert response.status_code == 302 and not response.headers['Location'].endswith(f'/post/{post.id}')
