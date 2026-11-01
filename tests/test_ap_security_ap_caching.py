"""Phase S6 groundwork: what a shared cache may do with a public post or comment response.

It used to vary on User-Agent and Signature for an author who blocks instances, as the author-block guard (D194)
refused a blocked instance with a 401. v1.8.10 dropped that guard (449ffac0d) and so does the branch: a public
object is answered alike to every requester and varies on Accept alone. None of these views is cached server-side
either: Flask-Caching's @cache.cached keys on the URL alone, and a non-public object is answered per signer.
"""
import pytest

from app.activitypub import routes as activitypub_routes
from tests.factories import make_instance, make_instance_block, make_post_reply
from tests.test_ap_content_objects import _double_the_delegates, ap_get, seed_local_post

PATHS = ['/post/{post}', '/post/{post}/replies', '/post/{post}/context', '/comment/{reply}']


def _vary(response):
    return [part.strip() for part in response.headers['Vary'].split(',')]


@pytest.mark.parametrize('path', PATHS)
@pytest.mark.parametrize('blocks', [False, True], ids=['blocks-nobody', 'blocks-an-instance'])
def test_a_response_varies_on_accept_only(app, db_session, monkeypatch, path, blocks):
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    if blocks:
        make_instance_block(author, make_instance('other.example'))
    reply = make_post_reply(post, author)

    response = ap_get(app, path.format(post=post.id, reply=reply.id))

    assert response.status_code == 200
    assert _vary(response)[0] == 'Accept'
    assert 'Signature' not in _vary(response) and 'User-Agent' not in _vary(response)
    assert response.headers['Cache-Control'] == 'public, max-age=120'


@pytest.mark.parametrize('view', ['comment_ap', 'post_ap', 'post_ap2', 'post_replies_ap', 'post_ap_context'])
def test_no_post_or_comment_view_is_cached_server_side_by_url(view):
    """Flask-Caching's cached() marks the view it wraps with `uncached`. A signer-dependent answer cached by URL
    would be served to the next requester, whoever signed."""
    assert not hasattr(getattr(activitypub_routes, view), 'uncached')
