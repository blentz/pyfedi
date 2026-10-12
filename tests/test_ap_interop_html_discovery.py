"""ActivityPub interop C1, item 1: HTML discovery.

Every HTML page whose subject has an ActivityPub id -- a post, a user profile
(local or remote), a community, a feed -- names that id in its head with
`<link rel="alternate" type="application/activity+json" href="...">`, in the
default theme's base.html and in the dillo theme's. The user profile and the
community page also send it as an HTTP `Link` header, with `Vary: Accept`, as
the post page has since D1086. The alternate is the id itself: our
public_url() for a local actor, the actor's ap_profile_id for a remote one --
never a remote ap_public_url, which Mastodon sets to its HTML page.

A private or local-only community still emits the alternate: the page is the
same for every viewer (no ETag change), and the ActivityPub side already
answers 403 for both (community_profile).

`fediverse:creator` (fep-2345) is also offered for a plain Page post -- image
and video, the types the old list left out -- when its author is local.

Spec: docs/superpowers/specs/2026-10-11-ap-interop-c1-design.md, item 1.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import db
from app.constants import POST_TYPE_IMAGE, POST_TYPE_VIDEO
from app.utils import ap_alternate_url
from app.models import Feed, Instance, Language, Site
from tests.factories import (make_community, make_community_member, make_instance, make_post,
                             make_user)

pytestmark = pytest.mark.usefixtures('site')

AP_LINK = '<link rel="alternate" type="application/activity+json" href="{}"'
AP_HEADER = '<{}>; rel="alternate"; type="application/activity+json"'


def instance(domain='test.piefed.local', software='piefed'):
    """The local Instance row, created once (tests/test_post_show.py's helper)."""
    existing = Instance.query.filter_by(domain=domain).first()
    return existing if existing is not None else make_instance(domain, software=software)


def as_user(app, user):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return client


def ap_link_headers(response):
    """The ActivityPub `Link` values only: render_template sets a preload Link and after_request may add more."""
    return [link for link in response.headers.get_all('Link') if 'application/activity+json' in link]


def vary_tokens(response):
    """`Vary` split into names: a substring test would find 'Accept' inside 'Accept-Language'."""
    return [token.strip() for token in response.headers.get('Vary', '').split(',')]


def show_community(client, path):
    """GET a community page with render_template doubled; return the response and the template context."""
    with patch('app.community.routes.render_template', return_value='rendered') as render:
        response = client.get(path)
    return response, render.call_args.kwargs


@pytest.fixture
def env(app, db_session):
    """A public instance, a local viewer and author, and a local community holding one post by the author.

    Site.private_instance defaults to True, which would redirect every anonymous request before the route runs."""
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    local = instance()
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1  # make_community hardcodes user_id=1
    viewer = make_user(local, 'viewer', local=True)
    author = make_user(local, 'author', local=True)
    community = make_community('general')
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.add(Language(code='en', name='English'))
    db.session.commit()
    make_community_member(author, community)
    post = make_post(community, author, 'https://test.piefed.local/post/1', title='THETITLE')
    post.body = 'the body'
    post.body_html = '<p>the body</p>'
    db.session.commit()
    return viewer, author, community, post


# --- the post page ---------------------------------------------------------


def test_the_post_page_links_its_activitypub_id(app, env):
    viewer, author, community, post = env
    post_id, ap_id = post.id, post.profile_id()

    html = app.test_client().get(f'/post/{post_id}').get_data(as_text=True)

    assert AP_LINK.format(ap_id) in html


def test_the_dillo_theme_links_the_activitypub_id_too(app, env, monkeypatch):
    """The dillo base template had none of the discovery links (spec item 1)."""
    viewer, author, community, post = env
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # dillo's post page renders form.csrf_token()
    viewer.theme = 'dillo'
    db.session.commit()
    post_id, ap_id = post.id, post.profile_id()

    html = as_user(app, viewer).get(f'/post/{post_id}').get_data(as_text=True)

    assert 'themes/dillo/styles.css' in html
    assert AP_LINK.format(ap_id) in html


@pytest.mark.parametrize('post_type', [POST_TYPE_IMAGE, POST_TYPE_VIDEO])
def test_a_local_authors_plain_page_post_names_its_creator(app, env, post_type):
    viewer, author, community, post = env
    post.type = post_type
    db.session.commit()
    post_id = post.id

    with patch('app.post.routes.render_template', return_value=app.response_class('rendered')) as render:
        app.test_client().get(f'/post/{post_id}')

    assert render.call_args.kwargs['creator'] == '@author@test.piefed.local'


def test_a_remote_authors_plain_page_post_names_no_creator(app, env):
    viewer, author, community, post = env
    remote = make_user(make_instance('peer.example'), 'faraway')
    remote_post = make_post(community, remote, 'https://peer.example/post/9')
    remote_post.type = POST_TYPE_IMAGE
    db.session.commit()
    post_id = remote_post.id

    with patch('app.post.routes.render_template', return_value=app.response_class('rendered')) as render:
        app.test_client().get(f'/post/{post_id}')

    assert render.call_args.kwargs['creator'] is None


# --- the user profile ------------------------------------------------------


def test_a_local_profile_links_its_activitypub_id_in_the_page_and_the_headers(app, env):
    viewer, author, community, post = env
    expected = 'https://test.piefed.local/u/author'   # public_url(), the id user_profile serves

    response = as_user(app, viewer).get('/u/author')

    assert response.status_code == 200
    assert AP_LINK.format(expected) in response.get_data(as_text=True)
    assert ap_link_headers(response) == [AP_HEADER.format(expected)]
    assert 'Accept' in vary_tokens(response)


def test_a_remote_profile_links_the_actors_own_id_not_its_html_page(app, env):
    viewer, author, community, post = env
    remote = make_user(make_instance('peer.example'), 'wakko')
    remote.ap_public_url = 'https://peer.example/@wakko'   # the HTML page, as Mastodon publishes it
    db.session.commit()
    expected = remote.ap_profile_id                         # https://peer.example/users/wakko

    response = as_user(app, viewer).get('/u/wakko@peer.example')

    assert response.status_code == 200
    assert AP_LINK.format(expected) in response.get_data(as_text=True)
    assert ap_link_headers(response) == [AP_HEADER.format(expected)]


# --- the community page ----------------------------------------------------


def test_the_community_page_passes_and_sends_its_activitypub_id(app, env):
    viewer, author, community, post = env
    expected = 'https://test.piefed.local/c/general'

    response, context = show_community(as_user(app, viewer), '/c/general')

    assert response.status_code == 200
    assert context['ap_alternate'] == expected
    assert ap_link_headers(response) == [AP_HEADER.format(expected)]
    assert 'Accept' in vary_tokens(response)


def test_a_remote_community_page_names_the_groups_own_id(app, env):
    viewer, author, community, post = env
    other = make_instance('other.example', software='lemmy')
    remote = make_community('remote', host='other.example')
    remote.ap_id = 'remote@other.example'
    remote.instance_id = other.id
    remote.ap_public_url = 'https://other.example/groups/remote'   # an HTML page, not the id
    db.session.commit()

    response, context = show_community(as_user(app, viewer), '/c/remote@other.example')

    assert response.status_code == 200
    assert context['ap_alternate'] == 'https://other.example/c/remote'
    assert ap_link_headers(response) == [AP_HEADER.format('https://other.example/c/remote')]


def test_a_local_only_community_still_advertises_its_alternate(app, env):
    """The ActivityPub side answers 403 for it (community_profile); the page link is the same for every viewer."""
    viewer, author, community, post = env
    community.local_only = True
    db.session.commit()

    response, context = show_community(as_user(app, viewer), '/c/general')

    assert response.status_code == 200
    assert context['ap_alternate'] == 'https://test.piefed.local/c/general'
    assert ap_link_headers(response) == [AP_HEADER.format('https://test.piefed.local/c/general')]


def test_a_private_community_still_advertises_its_alternate_to_a_member(app, env):
    viewer, author, community, post = env
    community.private = True
    db.session.commit()
    make_community_member(viewer, community)   # show_community answers 403 to a non-member

    response, context = show_community(as_user(app, viewer), '/c/general')

    assert response.status_code == 200
    assert context['ap_alternate'] == 'https://test.piefed.local/c/general'


# --- the feed page ---------------------------------------------------------


def test_the_feed_page_passes_its_activitypub_id(app, env):
    viewer, author, community, post = env
    feed = Feed(user_id=viewer.id, title='news', name='news', machine_name='news', instance_id=1, public=True,
                ap_profile_id='https://test.piefed.local/f/news', ap_public_url='https://test.piefed.local/f/news')
    db.session.add(feed)
    db.session.commit()

    with patch('app.feed.routes.render_template', return_value='rendered') as render:
        response = as_user(app, viewer).get('/f/news')

    assert response.status_code == 200
    assert render.call_args.kwargs['ap_alternate'] == 'https://test.piefed.local/f/news'


# --- an id a remote server chose is advertised only when it is a clean URL --


UNCLEAN_IDS = [
    'https://peer.example/u/x>; rel="x"',
    'https://peer.example/u/x,<https://evil.example/>',
    'https://peer.example/u/x\r\nSet-Cookie: a=b',
    'https://peer.example/u/x y',
    'https://peer.example/u/x"y',
    'ftp://peer.example/u/x',
    'https:///u/x',
    'https://peer.example/users/日本',
]


def assert_no_alternate(response):
    assert response.status_code == 200
    assert ap_link_headers(response) == []
    assert '<link rel="alternate" type="application/activity+json"' not in response.get_data(as_text=True)


@pytest.mark.parametrize('bad_id', UNCLEAN_IDS)
def test_a_remote_profile_with_an_unclean_id_advertises_nothing(app, env, bad_id):
    viewer, author, community, post = env
    remote = make_user(make_instance('peer.example'), 'wakko')
    remote.ap_profile_id = bad_id
    db.session.commit()

    assert_no_alternate(as_user(app, viewer).get('/u/wakko@peer.example'))


@pytest.mark.parametrize('bad_id', UNCLEAN_IDS)
def test_a_remote_community_with_an_unclean_id_advertises_nothing(app, env, bad_id):
    viewer, author, community, post = env
    other = make_instance('other.example', software='lemmy')
    remote = make_community('remote', host='other.example')
    remote.ap_id = 'remote@other.example'
    remote.instance_id = other.id
    remote.ap_profile_id = bad_id
    db.session.commit()

    client = as_user(app, viewer)
    response, context = show_community(client, '/c/remote@other.example')
    assert response.status_code == 200
    assert context['ap_alternate'] is None
    assert ap_link_headers(response) == []


def test_a_remote_post_with_an_unclean_id_advertises_nothing(app, env):
    viewer, author, community, post = env
    remote = make_user(make_instance('peer.example'), 'faraway')
    remote_post = make_post(community, remote, 'https://peer.example/post/9>; rel="x"')
    db.session.commit()

    response = app.test_client().get(f'/post/{remote_post.id}')

    assert response.status_code == 200
    assert '<link rel="alternate" type="application/activity+json"' not in response.get_data(as_text=True)
    # the header names our own /post/<id>, not the peer's id, so it stays (D1086)
    assert ap_link_headers(response) == [AP_HEADER.format(f'https://test.piefed.local/post/{remote_post.id}')]


def test_a_remote_actors_ap_public_url_is_never_advertised(app, env):
    viewer, author, community, post = env
    remote = make_user(make_instance('peer.example'), 'wakko')
    remote.ap_public_url = 'https://peer.example/@wakko>; rel="x"'
    db.session.commit()

    response = as_user(app, viewer).get('/u/wakko@peer.example')

    assert response.status_code == 200
    assert ap_link_headers(response) == [AP_HEADER.format(remote.ap_profile_id)]
    assert 'rel="x"' not in str(response.headers)


def test_a_remote_feed_with_an_unclean_id_advertises_nothing(app, env):
    viewer, author, community, post = env
    feed = Feed(user_id=viewer.id, title='news', name='news', machine_name='news', instance_id=1, public=True,
                ap_id='news@peer.example', ap_profile_id='https://peer.example/f/news>; rel="x"')
    db.session.add(feed)
    db.session.commit()

    response = as_user(app, viewer).get('/f/news@peer.example')

    # the rendered page itself, not the template's arguments (Task 11 review): no tag, and no header either
    assert 'news' in response.get_data(as_text=True)
    assert_no_alternate(response)


def test_a_remote_actor_without_an_id_advertises_nothing():
    actor = SimpleNamespace(is_local=lambda: False, ap_profile_id=None)

    assert ap_alternate_url(actor) is None
