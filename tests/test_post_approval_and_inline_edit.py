"""Two of v1.8.10's post routes: approving a held post (/post/<id>/approve) and editing a comment in place
(/post/<id>/comment/<id>/edit_inline).

Defects in v1.8.10's versions, fixed here:

* approve published, and federated again, any post it was pointed at -- one already published, or deleted -- and
  answered None, a 500, to a permission holder who neither wrote the post nor moderates it; it approves only a post
  held for review (POST_STATUS_REVIEWING), and only for its community's moderators, staff and admins (or its author);
* the inline edit built its redirect for a request that was not htmx and dropped it, edited a comment through a post
  it does not belong to, and showed a comment hidden here or one the reader may not see -- the full edit page refuses
  all three (D1077, interop D7, follow-up F6).
"""
import pytest

from app import db
from app.constants import POST_STATUS_PUBLISHED, POST_STATUS_REVIEWING
from app.models import Language, Post, PostReply, Role, Site, SiteDomain
from tests.conftest import forget_login
from tests.factories import COMMUNITY_ADMINISTRATION, grant_permissions, grant_role, make_community, make_instance, make_post, make_post_reply, make_user
from tests.test_per_domain_html_routes import Visitor
from tests.two_domains import FIRST

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def held(app, db_session, monkeypatch):
    local = make_instance('test.piefed.local', software='piefed')
    admin, author = make_user(local, 'boss', local=True), make_user(local, 'newcomer', local=True)
    for user in (admin, author):
        user.private_key = 'a key'
    author.ban_posts = True
    db.session.get(Site, 1).private_instance = False
    db.session.commit()
    grant_permissions(admin, *COMMUNITY_ADMINISTRATION)
    community = make_community('talk')
    post = make_post(community, author, None, title='held post')
    post.status = POST_STATUS_REVIEWING
    db.session.commit()
    federated, notified = [], []
    monkeypatch.setattr('app.post.routes.task_selector', lambda name, **kwargs: federated.append((name, kwargs)))
    monkeypatch.setattr('app.post.routes.notify_about_post', lambda post: notified.append(post.id))
    forget_login()
    return admin, author, community, post, federated, notified


def test_a_held_post_is_published_federated_and_its_author_trusted(app, held):
    admin, author, community, post, federated, notified = held

    response = Visitor(app, admin, FIRST).post(f'/post/{post.id}/approve')

    assert response.status_code == 200
    db.session.expire_all()
    post = db.session.get(Post, post.id)
    assert post.status == POST_STATUS_PUBLISHED and post.author.ban_posts is False
    assert federated == [('make_post', {'post_id': post.id})] and notified == [post.id]


@pytest.mark.parametrize('state', ['published', 'deleted'])
def test_a_post_not_held_for_review_is_not_published_again(app, held, state):
    admin, author, community, post, federated, notified = held
    if state == 'published':
        post.status = POST_STATUS_PUBLISHED
    else:
        post.deleted = True
    db.session.commit()

    response = Visitor(app, admin, FIRST).post(f'/post/{post.id}/approve')

    assert response.status_code == 404
    assert federated == [] and notified == []


def test_a_permission_holder_with_no_say_over_the_post_is_refused(app, held):
    """The permission is what lets the route run, and on the post's own domain. A holder of it on another domain only
    has no say over a post homed elsewhere. v1.8.10 answered None, a 500."""
    admin, author, community, post, federated, notified = held
    elsewhere = SiteDomain(domain='elsewhere.approval.example', scheme='https', federates=False)
    shop = SiteDomain(domain='shop.approval.example', scheme='https', federates=False)
    db.session.add_all([elsewhere, shop])
    db.session.commit()
    community.site_domain_id = shop.id
    holder = make_user(author.instance, 'elsewhereadmin', local=True)   # not `admin`, who is user 1 and passes all
    holder.private_key = 'a key'
    db.session.commit()
    grant_role(holder, Role.query.filter_by(name='role-moderate all communities').one(), site_domain_id=elsewhere.id)

    response = Visitor(app, holder, FIRST).post(f'/post/{post.id}/approve')

    assert response.status_code == 302 and '/auth/permission_denied' in response.location
    db.session.expire_all()
    assert db.session.get(Post, post.id).status == POST_STATUS_REVIEWING and federated == []


@pytest.fixture
def comment(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # the form renders form.csrf_token, as in production
    local = make_instance('test.piefed.local', software='piefed')
    author, other = make_user(local, 'writer', local=True), make_user(local, 'other', local=True)
    for user in (author, other):
        user.private_key = 'a key'
    db.session.get(Site, 1).private_instance = False
    db.session.commit()
    db.session.add(Language(code='en', name='English'))
    db.session.commit()
    community = make_community('talk')
    post, elsewhere = make_post(community, author, None, title='a post'), make_post(community, author, None, title='b')
    reply = make_post_reply(post, author, body='first words')
    db.session.commit()
    forget_login()
    return author, other, post, elsewhere, reply


HTMX = {'HX-Request': 'true'}


def test_a_request_that_is_not_htmx_goes_to_the_edit_page(app, comment):
    author, other, post, elsewhere, reply = comment

    response = Visitor(app, author, FIRST).get(f'/post/{post.id}/comment/{reply.id}/edit_inline')

    assert response.status_code == 302
    assert response.headers['Location'].endswith(f'/post/{post.id}/comment/{reply.id}/edit')


def test_the_author_gets_the_form_filled_in(app, comment):
    author, other, post, elsewhere, reply = comment

    response = Visitor(app, author, FIRST).get(f'/post/{post.id}/comment/{reply.id}/edit_inline', headers=HTMX)

    assert response.status_code == 200 and b'first words' in response.data
    assert f'id="reply_body_{reply.id}"'.encode() in response.data


def test_a_cancelled_edit_shows_the_comment_again(app, comment):
    author, other, post, elsewhere, reply = comment

    response = Visitor(app, author, FIRST).get(f'/post/{post.id}/comment/{reply.id}/edit_inline?cancelled=1',
                                               headers=HTMX)

    assert response.status_code == 200 and f'id="reply_body_{reply.id}"'.encode() not in response.data


def test_the_author_saves_an_edit(app, comment):
    author, other, post, elsewhere, reply = comment
    visitor = Visitor(app, author, FIRST)

    response = visitor.client.post(f'/post/{post.id}/comment/{reply.id}/edit_inline', base_url=visitor.base,
                                   headers=HTMX, data={'body': 'second words', 'notify_author': 'y',
                                                       'language_id': str(Language.query.filter_by(code='en').one().id),
                                                       'csrf_token': visitor.token})

    assert response.status_code == 200
    db.session.expire_all()
    assert db.session.get(PostReply, reply.id).body == 'second words'


def test_someone_else_may_not_edit_it(app, comment):
    author, other, post, elsewhere, reply = comment

    response = Visitor(app, other, FIRST).get(f'/post/{post.id}/comment/{reply.id}/edit_inline', headers=HTMX)

    assert response.status_code == 401


def test_a_comment_named_through_another_post_is_a_404(app, comment):
    author, other, post, elsewhere, reply = comment

    response = Visitor(app, author, FIRST).get(f'/post/{elsewhere.id}/comment/{reply.id}/edit_inline', headers=HTMX)

    assert response.status_code == 404
