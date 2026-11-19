"""Fixtures from tests/test_front_page_view_filters.py that other test modules request (registered in tests/conftest.py)."""
import pytest
from flask import g

from app import db
from app.models import Site, utcnow
from tests.factories import make_community, make_community_member, make_post, make_user
from tests.test_front_page_view_filters import HOST, _peer_instance, make_instance_local


@pytest.fixture
def front_page_view_filters_env(app, db_session):
    """Five communities, one post in each, so every filter has something it must
    include and something it must leave out.

    * `local_open`    -- local, public, in All and Popular
    * `local_private` -- local, PRIVATE; the reader is a member, the stranger is not
    * `local_quiet`   -- local, public, `show_popular` off: in All, not in Popular
    * `local_hidden`  -- local, public, `show_all` off: in Popular, not in All
    * `remote_open`   -- a REMOTE community, which `local` must exclude
    """
    from types import SimpleNamespace
    site = db.session.get(Site, 1)
    site.private_instance = False
    site.enable_nsfw = True
    g.site = site
    g.admin_ids = []
    db.session.commit()

    local_instance = make_instance_local()
    author = make_user(local_instance, 'author', local=True)
    reader = make_user(local_instance, 'reader', local=True)
    stranger = make_user(local_instance, 'stranger', local=True)
    db.session.commit()

    communities = {}
    for name in ('local_open', 'local_private', 'local_quiet', 'local_hidden'):
        communities[name] = make_community(name)
    remote = make_community('remote_open', host='peer.test')
    remote.ap_id = 'remote_open@peer.test'
    remote.instance_id = _peer_instance().id
    communities['remote_open'] = remote

    communities['local_private'].private = True
    communities['local_private'].show_all = False
    communities['local_private'].show_popular = False
    communities['local_quiet'].show_popular = False
    communities['local_hidden'].show_all = False
    db.session.commit()

    posts = {}
    for name, community in communities.items():
        post = make_post(community, author, f'https://{HOST}/c/{name}/p/1',
                         title=f'post in {name}')
        post.posted_at = utcnow()
        posts[name] = post
    db.session.commit()

    # The reader belongs to the private community and to local_open; the stranger
    # belongs to nothing, which is what makes `subscribed` discriminate.
    make_community_member(reader, communities['local_private'])
    make_community_member(reader, communities['local_open'])
    db.session.commit()

    return SimpleNamespace(app=app, client=app.test_client(), author=author,
                           reader=reader, stranger=stranger,
                           communities=communities, posts=posts)
