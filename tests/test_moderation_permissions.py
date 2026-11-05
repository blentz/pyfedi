"""`remove and restore content` and `turn comments off`, split out of `moderate all communities` so a role may hold one
without the rest (spec 2026-11-03-rbac-design.md, follow-up 4, owner ruling 2026-11-05).
"""
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from flask import session
from flask_wtf.csrf import generate_csrf
from sqlalchemy import text

from app import db
from app.constants import SRC_API
from app.models import Post, PostReply, Role, RolePermission, RolePermissionDomain, SiteDomain
from app.permissions import BY_NAME, SCOPABLE_PERMISSIONS, STAFF_PERMISSIONS
from app.shared.post import lock_post, mod_remove_post, mod_restore_post
from app.shared.reply import lock_post_reply, mod_remove_reply, mod_restore_reply
from tests.factories import bearer, grant_permission, make_community_member, make_post_reply, seed_post_context
from tests.test_branding import load_migration

REMOVE = 'remove and restore content'
COMMENTS = 'turn comments off'


@pytest.fixture
def s(db_session):
    s = seed_post_context(community_name='splitroom')
    s.community.private = False
    s.reply = make_post_reply(s.post, s.author, body='a comment')
    s.voter.verified = True
    db.session.commit()
    return s


def logged_in(app, user):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True
    return client


def csrf(app, client):
    with app.test_request_context():
        token = generate_csrf()
        raw = session['csrf_token']
    with client.session_transaction() as sess:
        sess['csrf_token'] = raw
    return token


def test_both_are_in_the_catalogue_scopable_and_given_to_staff():
    for name in (REMOVE, COMMENTS):
        assert name in BY_NAME and name in SCOPABLE_PERMISSIONS and name in STAFF_PERMISSIONS


def test_a_holder_of_remove_alone_removes_and_restores_posts_and_comments(s):
    grant_permission(s.voter, REMOVE)

    mod_remove_post(s.post.id, 'spam', SRC_API, bearer(s.voter))
    assert db.session.get(Post, s.post.id).deleted is True
    mod_restore_post(s.post.id, 'not spam', SRC_API, bearer(s.voter))
    assert db.session.get(Post, s.post.id).deleted is False

    mod_remove_reply(s.reply.id, 'spam', SRC_API, bearer(s.voter))
    assert db.session.get(PostReply, s.reply.id).deleted is True
    mod_restore_reply(s.reply.id, 'not spam', SRC_API, bearer(s.voter))
    assert db.session.get(PostReply, s.reply.id).deleted is False


def test_a_holder_of_remove_alone_may_not_turn_comments_off(s):
    grant_permission(s.voter, REMOVE)

    with pytest.raises(Exception, match='Does not have permission'):
        lock_post(s.post.id, True, SRC_API, bearer(s.voter))
    with pytest.raises(Exception, match='Does not have permission'):
        lock_post_reply(s.reply.id, True, SRC_API, bearer(s.voter))

    assert db.session.get(Post, s.post.id).comments_enabled is True


def test_a_holder_of_comments_alone_turns_comments_off_and_on(s):
    grant_permission(s.voter, COMMENTS)

    lock_post(s.post.id, True, SRC_API, bearer(s.voter))
    assert db.session.get(Post, s.post.id).comments_enabled is False
    lock_post(s.post.id, False, SRC_API, bearer(s.voter))
    assert db.session.get(Post, s.post.id).comments_enabled is True

    lock_post_reply(s.reply.id, True, SRC_API, bearer(s.voter))
    assert db.session.get(PostReply, s.reply.id).replies_enabled is False


def test_a_holder_of_comments_alone_may_not_remove(s):
    grant_permission(s.voter, COMMENTS)

    with pytest.raises(Exception, match='Does not have permission'):
        mod_remove_post(s.post.id, 'spam', SRC_API, bearer(s.voter))
    with pytest.raises(Exception, match='Does not have permission'):
        mod_remove_reply(s.reply.id, 'spam', SRC_API, bearer(s.voter))

    assert db.session.get(Post, s.post.id).deleted is False


def test_moderate_all_communities_alone_gives_neither(s):
    grant_permission(s.voter, 'moderate all communities')

    with pytest.raises(Exception, match='Does not have permission'):
        mod_remove_post(s.post.id, 'spam', SRC_API, bearer(s.voter))
    with pytest.raises(Exception, match='Does not have permission'):
        lock_post(s.post.id, True, SRC_API, bearer(s.voter))


def test_a_communitys_own_moderator_still_does_both(s):
    make_community_member(s.voter, s.community, is_moderator=True)

    lock_post(s.post.id, True, SRC_API, bearer(s.voter))
    mod_remove_post(s.post.id, 'spam', SRC_API, bearer(s.voter))

    post = db.session.get(Post, s.post.id)
    assert post.comments_enabled is False and post.deleted is True


# The web

def test_a_holder_of_remove_alone_removes_a_post_and_a_comment_on_the_web(app, s):
    grant_permission(s.voter, REMOVE)
    client = logged_in(app, s.voter)
    token = csrf(app, client)

    client.post(f'/post/{s.post.id}/comment/{s.reply.id}/delete', data={'csrf_token': token, 'reason': 'spam'})
    assert db.session.get(PostReply, s.reply.id).deleted is True

    client.post(f'/post/{s.post.id}/delete', data={'csrf_token': token, 'reason': 'spam'})
    assert db.session.get(Post, s.post.id).deleted is True


def test_a_holder_of_remove_alone_can_open_what_they_removed_to_restore_it(app, s, monkeypatch):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # the page renders its forms' tokens
    grant_permission(s.voter, REMOVE)
    mod_remove_post(s.post.id, 'spam', SRC_API, bearer(s.voter))

    client = logged_in(app, s.voter)

    assert client.get(f'/post/{s.post.id}').status_code == 200
    assert f'/post/{s.post.id}/restore' in client.get(f'/post/{s.post.id}/options_menu').get_data(as_text=True)


def options_page(app, s, user):
    return logged_in(app, user).get(f'/post/{s.post.id}/options_menu').get_data(as_text=True)


def test_the_post_menu_offers_a_holder_of_remove_delete_and_not_lock(app, s):
    grant_permission(s.voter, REMOVE)

    page = options_page(app, s, s.voter)

    assert f'/post/{s.post.id}/delete' in page
    assert f'/post/{s.post.id}/lock/yes' not in page


def test_the_post_menu_offers_a_holder_of_comments_lock_and_not_delete(app, s):
    grant_permission(s.voter, COMMENTS)

    page = options_page(app, s, s.voter)

    assert f'/post/{s.post.id}/lock/yes' in page
    assert f'/post/{s.post.id}/delete' not in page


def test_the_comment_menu_follows_the_two_permissions(app, s):
    grant_permission(s.voter, COMMENTS)
    page = logged_in(app, s.voter).get(f'/post/{s.post.id}/comment/{s.reply.id}/options_menu').get_data(as_text=True)

    assert f'/{s.reply.id}/lock/yes' in page
    assert f'/comment/{s.reply.id}/delete' not in page


# The migration

def upgraded():
    migration = load_migration('e4c8a1d7f3b6_split_moderation_permissions.py')
    with db.engine.connect() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
        connection.commit()


def downgraded():
    migration = load_migration('e4c8a1d7f3b6_split_moderation_permissions.py')
    with db.engine.connect() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.downgrade()
        connection.commit()


def rows(role):
    return {p.permission: p.all_domains for p in RolePermission.query.filter_by(role_id=role.id)}


def test_each_holder_gains_both_at_the_same_reach(db_session):
    domain = SiteDomain(domain='first.example')
    wide = Role(name='Wide')
    limited = Role(name='Limited')
    other = Role(name='Other')
    db.session.add_all([domain, wide, limited, other])
    db.session.commit()
    db.session.add_all([RolePermission(role_id=wide.id, permission='moderate all communities'),
                        RolePermission(role_id=limited.id, permission='moderate all communities', all_domains=False),
                        RolePermission(role_id=other.id, permission='ban users')])
    db.session.flush()
    db.session.add(RolePermissionDomain(role_id=limited.id, permission='moderate all communities',
                                        site_domain_id=domain.id))
    db.session.commit()

    upgraded()
    db.session.expire_all()

    assert rows(wide) == {'moderate all communities': True, REMOVE: True, COMMENTS: True}
    assert rows(limited) == {'moderate all communities': False, REMOVE: False, COMMENTS: False}
    assert rows(other) == {'ban users': True}
    listed = {row.permission for row in RolePermissionDomain.query.filter_by(role_id=limited.id,
                                                                             site_domain_id=domain.id)}
    assert listed == {'moderate all communities', REMOVE, COMMENTS}


def test_upgrading_twice_changes_nothing_and_keeps_what_an_admin_set(db_session):
    role = Role(name='Set by hand')
    db.session.add(role)
    db.session.commit()
    db.session.add_all([RolePermission(role_id=role.id, permission='moderate all communities'),
                        RolePermission(role_id=role.id, permission=REMOVE, all_domains=False)])
    db.session.commit()

    upgraded()
    upgraded()
    db.session.expire_all()

    assert rows(role) == {'moderate all communities': True, REMOVE: False, COMMENTS: True}


def test_the_downgrade_takes_both_away_and_leaves_the_rest(db_session):
    role = Role(name='Wide')
    db.session.add(role)
    db.session.commit()
    db.session.add(RolePermission(role_id=role.id, permission='moderate all communities'))
    db.session.commit()
    upgraded()

    downgraded()
    db.session.expire_all()

    assert rows(role) == {'moderate all communities': True}
    assert db.session.execute(text("SELECT count(*) FROM role_permission WHERE permission = :p"),
                              {'p': REMOVE}).scalar() == 0
