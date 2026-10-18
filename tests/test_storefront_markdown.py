"""Markdown and post links are saved canonical (onion storefront spec §3.5; final review Important 2).

`canonicalise_links` makes stored HTML canonical, but the Markdown a body is rendered from is stored and federated
too (`source.content`, which Lemmy and PieFed prefer), and so is `post.url`. A link an onion visitor pastes, or one
the onion UI showed them, names the onion host. `canonicalise_text` rebases such URLs onto the canonical origin
before they are saved; foreign hosts, the canonical host itself and plain text are left alone.
"""
import contextvars
import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from flask import current_app, g

from app import db
from app.activitypub.util import comment_model_to_json, post_to_page
from app.constants import SRC_API
from app.models import ChatMessage, Language, Post, PostReply, Site
from app.shared.community import edit_community
from app.storefront import canonicalise_links, canonicalise_text
from app.utils import markdown_to_html, piefed_markdown_to_lemmy_markdown, utcnow
from tests.factories import a_keypair, make_community, make_community_member, make_post
from tests.factories import bearer
from tests.test_admin_site_profile import _post, admin_client  # noqa: F401  (admin_client is a fixture)
from tests.test_shared_community_lifecycle import _api_input, _seed
from tests.test_storefront_presentation import CANONICAL, ONION, onion  # noqa: F401  (onion is a fixture)

pytestmark = pytest.mark.usefixtures('site')


@pytest.mark.parametrize('text, saved', [
    ('see http://abc.onion/post/1', f'see https://{CANONICAL}/post/1'),
    ('[x](http://abc.onion/post/1?a=1&b=#c)', f'[x](https://{CANONICAL}/post/1?a=1&b=#c)'),
    ('![p](//abc.onion/a.jpg)', f'![p](https://{CANONICAL}/a.jpg)'),
    ('<HTTPS://ABC.onion:80/c/x>', f'<https://{CANONICAL}/c/x>'),
    ('http://abc.onion.', f'https://{CANONICAL}.'),                    # sentence punctuation is not the host's
    ('(http://abc.onion/post/1), http://abc.onion/u/a;', f'(https://{CANONICAL}/post/1), https://{CANONICAL}/u/a;'),
    ('http://abc.onion', f'https://{CANONICAL}'),
    ('link:http://abc.onion/x', f'link:https://{CANONICAL}/x'),
    # a bare host ends at whatever follows it (final re-review M-1)
    ('[http://abc.onion](http://abc.onion)', f'[https://{CANONICAL}](https://{CANONICAL})'),
    ('~~http://abc.onion~~', f'~~https://{CANONICAL}~~'),
    ('_http://abc.onion_', f'_https://{CANONICAL}_'),
    ('http://abc.onion,http://abc.onion', f'https://{CANONICAL},https://{CANONICAL}'),
    ('http://abc.onion;x http://abc.onion&x http://abc.onion=', f'https://{CANONICAL};x https://{CANONICAL}&x https://{CANONICAL}='),
    ('http://abc.onion]x http://abc.onion[x', f'https://{CANONICAL}]x https://{CANONICAL}[x'),
    ('http://me@abc.onion:8080~/x', f'https://{CANONICAL}~/x'),
    ('a\nhttp://abc.onion/x\r\nb', f'a\nhttps://{CANONICAL}/x\r\nb'),
])
def test_canonicalise_text_rebases_our_other_hosts(app, onion, text, saved):
    assert canonicalise_text(text) == saved
    assert canonicalise_text(saved) is saved   # idempotent: what was saved is saved again unchanged


@pytest.mark.parametrize('text', [
    'no links here, just abc.onion in words',
    'https://example.com/abc.onion/x and https://notabc.onion/x and https://abc.onion.example/x',
    f'http://{CANONICAL}/post/1 and //{CANONICAL}/x',   # the canonical host is left as written (clearnet unchanged)
    f'[http://{CANONICAL}](http://{CANONICAL}), ~~http://{CANONICAL}~~ _http://{CANONICAL}_',
    'ftp://abc.onion/x xhttps://abc.onion/x mailto:me@abc.onion',
    'https:\\\\abc.onion/x',                             # a backslash URL names no host we can trust
    '',
])
def test_canonicalise_text_leaves_the_rest_alone(app, onion, text):
    assert canonicalise_text(text) is text


@pytest.mark.parametrize('text, saved', [
    ('run `curl http://abc.onion/x` now', f'run `curl https://{CANONICAL}/x` now'),
    ('```\ncurl http://abc.onion/x\n```', f'```\ncurl https://{CANONICAL}/x\n```'),
    ('https://ex.com/?u=http://abc.onion/x', f'https://ex.com/?u=https://{CANONICAL}/x'),
])
def test_canonicalise_text_rewrites_our_urls_in_code_and_in_foreign_urls(app, onion, text, saved):
    # documented choice: the privacy invariant (nothing stored names the onion) beats fidelity
    assert canonicalise_text(text) == saved


def test_canonicalise_links_rewrites_our_urls_in_a_foreign_url(app, onion):
    html = '<a href="https://ex.com/?u=http://abc.onion/x">x</a><a href="http://abc.onion/y">y</a>'
    assert canonicalise_links(html) == (f'<a href="https://ex.com/?u=http://abc.onion/x">x</a>'
                                        f'<a href="https://{CANONICAL}/y">y</a>')


def test_canonicalise_text_passes_none(app, onion):
    assert canonicalise_text(None) is None


def test_canonicalise_text_outside_an_app_passes_the_text_through(app, onion):
    # no app context (a fuzz campaign): no registry to read, so nothing to rebase. An empty context has none pushed
    text = 'see http://abc.onion/post/1'
    assert contextvars.Context().run(canonicalise_text, text) is text


def test_canonicalise_links_outside_an_app_passes_the_html_through(app, onion):
    # sanitise_posting_warning reaches allowlist_html with no app in the posting_warning_html fuzz campaign
    html = '<a href="http://abc.onion/post/1">x</a>'
    assert contextvars.Context().run(canonicalise_links, html) is html


def test_canonicalise_text_with_no_other_host_reads_no_text(app):
    # only SERVER_NAME is ours: there is nothing to move, whatever the text says
    text = 'see http://abc.onion/post/1'
    assert canonicalise_text(text) is text


def test_rendered_markdown_names_no_other_host_even_in_its_text(app, onion):
    html = markdown_to_html('see http://abc.onion/post/1')
    assert f'>https://{CANONICAL}/post/1</a>' in html
    assert ONION not in html


def test_rendered_markdown_in_a_test_env_reads_no_domains():
    # test_env runs without an app (the fuzz targets): the registered domains are not read
    assert 'abc.onion' in markdown_to_html('see http://abc.onion/post/1', test_env={'fn_string': 'fn-test'})


def test_saved_markdown_is_canonical(app, onion):
    assert piefed_markdown_to_lemmy_markdown('a\r\nsee http://abc.onion/post/1') == \
        f'a  \r\nsee https://{CANONICAL}/post/1'


# --- through the API, on the onion --------------------------------------------------------------------------------

PASTED = 'see http://abc.onion/post/1 and [this](//abc.onion/c/x)'
STORED = f'see https://{CANONICAL}/post/1 and [this](https://{CANONICAL}/c/x)'


def auth(user):
    return {'Authorization': f'Bearer {user.encode_jwt_token()}'}


@pytest.fixture
def env(app, onion, api_baseline, monkeypatch):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
    for code, name in [('en', 'English'), ('und', 'Undetermined')]:
        db.session.add(Language(code=code, name=name))
    db.session.commit()
    db.session.get(Site, 1).language_id = Language.query.filter_by(code='en').one().id
    community = make_community('pasteland')
    author = api_baseline.user2
    author.private_key, author.public_key = a_keypair()
    author.verified, author.created = True, utcnow() - timedelta(days=30)   # old enough to send a message
    make_community_member(author, community)
    post = make_post(community, author, ap_id=f'https://{CANONICAL}/post/77')
    db.session.commit()
    return app.test_client(), community, author, post, api_baseline


def on_onion(client, method, path, user, body):
    # the pasted link is not fetched
    with patch('app.shared.post.opengraph_parse', return_value=None), \
            patch('app.shared.post.is_image_url', return_value=False):
        response = getattr(client, method)(path, base_url=f'http://{ONION}', headers=auth(user), json=body)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def assert_canonical(post):
    assert post.body == STORED
    assert post.url == f'https://{CANONICAL}/post/1'
    assert ONION not in json.dumps(post_to_page(post))


def test_post_written_on_the_onion_is_stored_and_federated_canonical(env):
    client, community, author, _, _ = env
    view = on_onion(client, 'post', '/api/alpha/post', author,
                    {'title': 'pasted', 'community_id': community.id, 'body': PASTED,
                     'url': 'http://abc.onion/post/1'})
    assert_canonical(db.session.get(Post, view['post_view']['post']['id']))


def test_post_edited_on_the_onion_is_stored_and_federated_canonical(env):
    client, _, author, post, _ = env
    on_onion(client, 'put', '/api/alpha/post', author,
             {'post_id': post.id, 'title': 'edited', 'body': PASTED, 'url': 'http://abc.onion/post/1'})
    assert_canonical(db.session.get(Post, post.id))


def test_reply_written_and_edited_on_the_onion_is_stored_and_federated_canonical(env):
    client, _, author, post, _ = env
    view = on_onion(client, 'post', '/api/alpha/comment', author, {'post_id': post.id, 'body': PASTED})
    reply = db.session.get(PostReply, view['comment_view']['comment']['id'])
    assert reply.body == STORED
    assert ONION not in json.dumps(comment_model_to_json(reply))
    on_onion(client, 'put', '/api/alpha/comment', author, {'comment_id': reply.id, 'body': PASTED + '!'})
    assert db.session.get(PostReply, reply.id).body == STORED + '!'


def test_profile_about_saved_on_the_onion_is_canonical(env):
    client, _, author, _, _ = env
    on_onion(client, 'put', '/api/alpha/user/save_user_settings', author, {'bio': PASTED})
    assert db.session.get(type(author), author.id).about == STORED


def test_private_message_edited_on_the_onion_is_canonical(env):
    client, _, author, _, baseline = env
    sent = on_onion(client, 'post', '/api/alpha/private_message', author,
                    {'recipient_id': baseline.user3.id, 'content': PASTED})
    message_id = sent['private_message_view']['private_message']['id']
    assert db.session.get(ChatMessage, message_id).body == STORED
    on_onion(client, 'put', '/api/alpha/private_message', author,
             {'private_message_id': message_id, 'content': PASTED + '!'})
    assert db.session.get(ChatMessage, message_id).body == STORED + '!'


# --- the other saves that store Markdown -------------------------------------------------------------------------

def test_community_rules_saved_through_the_api_are_canonical(app, onion, db_session):
    s = _seed()
    edit_community(_api_input(rules=PASTED), s.community, SRC_API, bearer(s.user), from_scratch=True)
    assert s.community.rules == STORED


def test_site_legal_information_saved_by_an_admin_is_canonical(app, onion, admin_client):
    client, token = admin_client
    response, _ = _post(client, token, legal_information=PASTED)
    assert response.status_code == 200
    db.session.expire_all()
    assert db.session.get(Site, 1).legal_information == STORED
