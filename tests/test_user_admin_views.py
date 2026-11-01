"""v1.8.10's three admin views of an account: /u/<actor>/voting_patterns, /voting_patterns_down and /post_timing
(permission 'manage users').

Defects in v1.8.10's version, fixed here:

* an actor naming nobody was an empty 200 from two of them and a 500 from the third (it returned None);
* they found the account by name alone, so on the clearnet `/u/bob@<onion>/...` showed an admin the onion's account,
  which no clearnet viewer, admins included, may see (follow-up F6 ruling); the downvote list likewise named the
  onion authors a clearnet account had downvoted.

An admin looks at banned accounts too, so a banned one is shown.
"""
from datetime import timedelta

import pytest

from app import db
from app.models import Post, utcnow
from tests.conftest import forget_login
from tests.factories import (grant_permission, make_community, make_instance, make_post, make_post_vote,
                             make_user)
from tests.test_instance_visible_here import scene  # noqa: F401 -- scene is a fixture
from tests.test_per_domain_ap_routes import onion_proxy, twins  # noqa: F401
from tests.test_per_domain_html_routes import Visitor
from tests.two_domains import FIRST, ONION, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')

VIEWS = ['voting_patterns', 'voting_patterns_down', 'post_timing']


@pytest.fixture
def staff(app, db_session):
    local = make_instance('test.piefed.local', software='piefed')
    admin = make_user(local, 'boss', local=True)
    target = make_user(local, 'suspect', local=True)
    author = make_user(make_instance('peer.example', software='lemmy'), 'victim')
    for user in (admin, target):
        user.private_key = 'a key'
        user.ap_profile_id = f'https://test.piefed.local/u/{user.user_name}'   # as a real local account has it
    db.session.commit()
    grant_permission(admin, 'manage users')
    community = make_community('talk')
    now = utcnow()
    times = [now - timedelta(minutes=10), now - timedelta(minutes=10, seconds=20), now - timedelta(hours=3)]
    posts = []
    for n, posted_at in enumerate(times):
        post = make_post(community, target, f'https://test.piefed.local/post/t{n}', title=f'timed {n}')
        post.posted_at = posted_at
        posts.append(post)
    downvoted = make_post(community, author, 'https://peer.example/post/1', title='downvoted')
    db.session.commit()
    make_post_vote(target, downvoted, -1)
    forget_login()
    return admin, target, author, posts


@pytest.mark.parametrize('view', VIEWS)
def test_an_admin_sees_the_view(app, staff, view):
    admin, target, author, posts = staff

    response = Visitor(app, admin, FIRST).get(f'/u/{target.user_name}/{view}')

    assert response.status_code == 200
    assert target.user_name.encode() in response.data


@pytest.mark.parametrize('view', VIEWS)
def test_a_banned_account_is_still_shown(app, staff, view):
    admin, target, author, posts = staff
    target.banned = True
    db.session.commit()

    assert Visitor(app, admin, FIRST).get(f'/u/{target.user_name}/{view}').status_code == 200


@pytest.mark.parametrize('view', VIEWS)
def test_an_actor_naming_nobody_is_a_404(app, staff, view):
    admin, target, author, posts = staff

    assert Visitor(app, admin, FIRST).get(f'/u/nobody/{view}').status_code == 404


@pytest.mark.parametrize('view', VIEWS)
def test_a_reader_without_the_permission_is_refused(app, staff, view):
    admin, target, author, posts = staff

    response = Visitor(app, target, FIRST).get(f'/u/{target.user_name}/{view}')

    assert response.status_code in (302, 403)
    assert b'Voting patterns' not in response.data and b'Post timings' not in response.data


def test_posts_close_together_are_marked(app, staff, monkeypatch):
    admin, target, author, posts = staff
    seen = {}

    def capture(template, **context):
        seen.update(context)
        return 'rendered'
    monkeypatch.setattr('app.user.routes.render_template', capture)

    Visitor(app, admin, FIRST).get(f'/u/{target.user_name}/post_timing')

    assert seen['close_together'] == {posts[0].id, posts[1].id}
    assert [post.id for post in seen['posts']] == [post.id for post in posts]


def test_the_downvotes_name_their_authors(app, staff, monkeypatch):
    admin, target, author, posts = staff
    seen = {}

    def capture(template, **context):
        seen.update(context)
        return 'rendered'
    monkeypatch.setattr('app.user.routes.render_template', capture)

    Visitor(app, admin, FIRST).get(f'/u/{target.user_name}/voting_patterns_down')

    assert [vote.author_id for vote in seen['votes']] == [author.id]
    assert set(seen['authors']) == {author.id}


@pytest.mark.parametrize('view', VIEWS)
def test_the_onions_account_is_not_shown_on_the_clearnet(app, scene, view):  # noqa: F811
    grant_permission(scene.first_bob, 'manage users')

    on_first = Visitor(app, scene.first_bob, FIRST).get(f'/u/{scene.onion_bob.user_name}@{ONION}/{view}')

    assert on_first.status_code == 404


def test_downvotes_on_the_onion_are_left_out_on_the_clearnet(app, scene, monkeypatch):  # noqa: F811
    grant_permission(scene.first_bob, 'manage users')
    onion_post = Post.query.filter_by(title='ONIONPOST').one()
    make_post_vote(scene.first_bob, onion_post, -1)
    seen = {}

    def capture(template, **context):
        seen.update(context)
        return 'rendered'
    monkeypatch.setattr('app.user.routes.render_template', capture)

    Visitor(app, scene.first_bob, FIRST).get(f'/u/{scene.first_bob.user_name}/voting_patterns_down')

    assert seen['votes'] == [] and seen['authors'] == {}
