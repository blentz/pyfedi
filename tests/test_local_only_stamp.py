"""Content by or for a local-only domain is stamped `local_only` when it is made (plan B task 6)."""
from types import SimpleNamespace

import pytest
from flask import g

from app import db
from app.constants import POST_TYPE_ARTICLE, SRC_API
from app.local_only import community_local_only, content_local_only, user_local_only
from app.models import Post, PostReply, SiteDomain
from app.shared.post import make_post as shared_make_post
from app.storefront import clear_domain_cache
from tests.factories import make_community, make_instance, make_post, make_user
from tests.test_shared_post_edit import _api_input
from tests.test_shared_post_make import seed_make_context

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def env(db_session, monkeypatch):
    s = seed_make_context('stamping')
    default = db.session.query(SiteDomain).filter_by(is_default=True).first()
    if default is None:
        default = SiteDomain(domain='default.example', is_default=True)
        db.session.add(default)
    onion = SiteDomain(domain='abc.onion', scheme='http', federates=False)
    db.session.add(onion)
    db.session.commit()
    clear_domain_cache()
    monkeypatch.setattr('app.shared.post.task_selector', lambda *a, **k: None)
    monkeypatch.setattr('app.shared.post.make_image_sizes', lambda *a, **k: None)
    monkeypatch.setattr('app.shared.post.authorise_api_user', lambda *a, **k: s.author)
    onion_community = make_community('onionland')
    onion_community.site_domain_id = onion.id
    remote_community = make_community('memes', host='lemmy.world')
    remote_community.ap_id = 'memes@lemmy.world'
    db.session.commit()
    yield SimpleNamespace(author=s.author, community=s.community, onion=onion, default=default,
                          onion_community=onion_community, remote_community=remote_community)
    clear_domain_cache()


def _post(env, community):
    result = shared_make_post(_api_input(), community, POST_TYPE_ARTICLE, SRC_API, auth='token')
    return result[1] if isinstance(result, tuple) else result


def _reply(env, author, post, parent=None):
    return PostReply.new(author, post, parent, body='hi', body_html='<p>hi</p>', notify_author=False,
                         language_id=None, distinguished=False, answer=False)


def test_an_onion_users_post_in_a_hell_cloud_community_is_local_only(env):
    env.author.site_domain_id = env.onion.id
    assert _post(env, env.community).local_only is True


def test_an_onion_users_post_in_a_remote_community_is_local_only(env):
    env.author.site_domain_id = env.onion.id
    assert community_local_only(env.remote_community) is False
    assert _post(env, env.remote_community).local_only is True


def test_a_hell_cloud_users_post_in_an_onion_community_is_local_only(env):
    assert _post(env, env.onion_community).local_only is True


def test_a_hell_cloud_users_post_in_a_hell_cloud_community_is_not(env):
    assert _post(env, env.community).local_only is False


def test_a_hell_cloud_users_reply_to_local_only_content_is_local_only(env):
    post = make_post(env.community, env.author, 'https://local.example/p/9')
    post.local_only = True
    db.session.commit()
    assert _reply(env, env.author, post).local_only is True


def test_a_reply_to_a_local_only_reply_is_local_only(env):
    post = make_post(env.community, env.author, 'https://local.example/p/9')
    parent = _reply(env, env.author, post)
    assert parent.local_only is False
    parent.local_only = True
    db.session.commit()
    assert _reply(env, env.author, post, parent).local_only is True


def test_an_onion_users_reply_in_a_hell_cloud_community_is_local_only(env):
    post = make_post(env.community, env.author, 'https://local.example/p/9')
    env.author.site_domain_id = env.onion.id
    assert _reply(env, env.author, post).local_only is True


def test_a_hell_cloud_users_reply_to_ordinary_content_is_not(env):
    post = make_post(env.community, env.author, 'https://local.example/p/9')
    assert _reply(env, env.author, post).local_only is False


def test_a_remote_authors_content_is_never_local_only(env):
    remote = make_user(make_instance('far.example'), 'far')
    post = make_post(env.onion_community, remote, 'https://far.example/p/1')
    assert user_local_only(remote) is False
    assert content_local_only(remote, env.onion_community) is False
    assert _reply(env, remote, post).local_only is False


def test_the_stamp_is_not_recomputed(env):
    post = _post(env, env.community)
    assert post.local_only is False
    env.author.site_domain_id = env.onion.id
    env.community.site_domain_id = env.onion.id
    db.session.commit()
    db.session.refresh(post)
    assert post.local_only is False
