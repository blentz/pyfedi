"""Phase S6 groundwork: a post or comment response whose answer depends on who signed the request says so.

The author-blocks-instances guard (D194) already answers differently per signer: a blocked instance gets 401,
everyone else 200. Vary named only User-Agent, so a shared cache could hand one signer's 200 to another. None
of these views is cached server-side either: Flask-Caching's @cache.cached keys on the URL alone.
"""
import pytest

from app.activitypub import routes as activitypub_routes
from tests.factories import make_instance, make_instance_block, make_post_reply
from tests.test_ap_content_objects import _double_the_delegates, ap_get, seed_local_post

PATHS = ['/post/{post}', '/post/{post}/replies', '/post/{post}/context', '/comment/{reply}']


def _vary(response):
    return [part.strip() for part in response.headers['Vary'].split(',')]


@pytest.mark.parametrize('path', PATHS)
def test_a_response_from_an_author_who_blocks_instances_varies_on_the_signature(app, db_session, monkeypatch, path):
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
    make_instance_block(author, make_instance('other.example'))
    make_instance('peer2.example')
    reply = make_post_reply(post, author)

    response = ap_get(app, path.format(post=post.id, reply=reply.id), user_agent='Test (+https://peer2.example)')

    assert response.status_code == 200
    assert _vary(response)[:3] == ['Accept', 'User-Agent', 'Signature']
    assert response.headers['Cache-Control'] == 'public, max-age=120'


@pytest.mark.parametrize('path', PATHS)
def test_a_response_from_an_author_who_blocks_nobody_varies_on_accept_only(app, db_session, monkeypatch, path):
    _double_the_delegates(monkeypatch)
    community, author, post = seed_local_post()
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
