"""The modlog endpoint, which closes `app/api/alpha/utils/misc.py`.

Sub-project 84, slice G. The load-bearing rule here is one line:

    if not is_admin:
        base_q = base_q.filter(ModLog.public == True)

-- a moderation log entry is private until somebody says otherwise, and this
endpoint is the public face of it. Every filter, every category and every
view below it is built on that query, so the rows here pin it first.
"""
import pytest
from flask import g

from app import db
from app.models import (Community, ModLog, Post, PostReply, Role,
                        RolePermission, Site, User, user_role)
from tests.factories import (make_community, make_post, make_post_reply,
                             make_user)


def token(user):
    return f'Bearer {user.encode_jwt_token()}'


def with_role(user, name):
    """`is_admin()` and `is_staff()` read the role's NAME, not a permission
    (app/models.py:1264, 1271)."""
    role = Role(name=name, weight=10)
    db.session.add(role)
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=user.id,
                                                 role_id=role.id))
    db.session.commit()
    return user


def an_entry(action, moderator, public=True, **columns):
    entry = ModLog(user_id=moderator.id, action=action, public=public,
                   type='mod', reason='because')
    for column, value in columns.items():
        setattr(entry, column, value)
    db.session.add(entry)
    db.session.commit()
    return entry


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    moderator = api_baseline.user2
    target = api_baseline.user3
    community = api_baseline.community1
    post = api_baseline.post1
    reply = make_post_reply(post, target, body='a reply')
    reply.body_html = '<p>a reply</p>'
    db.session.commit()
    return moderator, target, community, post, reply, api_baseline


# --------------------------------------------------------------------------
# Who sees what
# --------------------------------------------------------------------------


def test_a_private_entry_is_not_shown_to_an_ordinary_account(app, env):
    """`ModLog.public` is False by default (app/models.py:3948), and this
    endpoint is the public face of the log."""
    from app.api.alpha.utils.misc import get_modlog

    moderator, target, community, post, reply, baseline = env
    an_entry('delete_post', moderator, public=False, post_id=post.id)

    answer = get_modlog(token(target), {})

    assert answer['removed_posts'] == []


def test_a_private_entry_is_not_shown_to_an_anonymous_reader_either(app, env):
    from app.api.alpha.utils.misc import get_modlog

    moderator, target, community, post, reply, baseline = env
    an_entry('delete_post', moderator, public=False, post_id=post.id)

    assert get_modlog(None, {})['removed_posts'] == []


def test_a_public_entry_is_shown_to_anybody(app, env):
    from app.api.alpha.utils.misc import get_modlog

    moderator, target, community, post, reply, baseline = env
    entry = an_entry('delete_post', moderator, post_id=post.id,
                     community_id=community.id)

    for auth in (None, token(target)):
        answer = get_modlog(auth, {})
        assert [e['mod_remove_post']['id']
                for e in answer['removed_posts']] == [entry.id]


def test_an_administrator_sees_the_private_entries(app, env):
    from app.api.alpha.utils.misc import get_modlog

    moderator, target, community, post, reply, baseline = env
    admin = with_role(make_user(baseline.instance_local, 'theadmin',
                                local=True), 'Admin')
    entry = an_entry('delete_post', moderator, public=False, post_id=post.id)

    answer = get_modlog(token(admin), {})

    assert [e['mod_remove_post']['id']
            for e in answer['removed_posts']] == [entry.id]


def test_staff_see_them_too(app, env):
    """`user.is_admin() or user.is_staff()`."""
    from app.api.alpha.utils.misc import get_modlog

    moderator, target, community, post, reply, baseline = env
    staff = with_role(make_user(baseline.instance_local, 'thestaff',
                                local=True), 'Staff')
    entry = an_entry('delete_post', moderator, public=False, post_id=post.id)

    answer = get_modlog(token(staff), {})

    assert len(answer['removed_posts']) == 1


# --------------------------------------------------------------------------
# The categories
# --------------------------------------------------------------------------


def test_a_removed_post_is_reported_as_removed_or_restored(app, env):
    from app.api.alpha.utils.misc import get_modlog

    moderator, target, community, post, reply, baseline = env
    removed = an_entry('delete_post', moderator, post_id=post.id,
                       community_id=community.id)
    restored = an_entry('restore_post', moderator, post_id=post.id,
                        community_id=community.id)

    answer = get_modlog(None, {'type_': 'ModRemovePost'})

    by_id = {e['mod_remove_post']['id']: e for e in answer['removed_posts']}
    assert by_id[removed.id]['mod_remove_post']['removed'] is True
    assert by_id[restored.id]['mod_remove_post']['removed'] is False
    assert by_id[removed.id]['moderator']['id'] == moderator.id
    assert by_id[removed.id]['post']['id'] == post.id
    assert by_id[removed.id]['community']['id'] == community.id


def test_a_locked_post(app, env):
    from app.api.alpha.utils.misc import get_modlog

    moderator, target, community, post, reply, baseline = env
    locked = an_entry('lock_post', moderator, post_id=post.id)
    unlocked = an_entry('unlock_post', moderator, post_id=post.id)

    answer = get_modlog(None, {'type_': 'ModLockPost'})

    by_id = {e['mod_lock_post']['id']: e for e in answer['locked_posts']}
    assert by_id[locked.id]['mod_lock_post']['locked'] is True
    assert by_id[unlocked.id]['mod_lock_post']['locked'] is False


def test_a_featured_post(app, env):
    from app.api.alpha.utils.misc import get_modlog

    moderator, target, community, post, reply, baseline = env
    featured = an_entry('featured_post', moderator, post_id=post.id)
    unfeatured = an_entry('unfeatured_post', moderator, post_id=post.id)

    answer = get_modlog(None, {'type_': 'ModFeaturePost'})

    by_id = {e['mod_feature_post']['id']: e for e in answer['featured_posts']}
    assert by_id[featured.id]['mod_feature_post']['featured'] is True
    assert by_id[unfeatured.id]['mod_feature_post']['featured'] is False


def test_a_removed_comment(app, env):
    from app.api.alpha.utils.misc import get_modlog

    moderator, target, community, post, reply, baseline = env
    removed = an_entry('delete_post_reply', moderator, reply_id=reply.id)
    restored = an_entry('restore_post_reply', moderator, reply_id=reply.id)

    answer = get_modlog(None, {'type_': 'ModRemoveComment'})

    by_id = {e['mod_remove_comment']['id']: e
             for e in answer['removed_comments']}
    assert by_id[removed.id]['mod_remove_comment']['removed'] is True
    assert by_id[restored.id]['mod_remove_comment']['removed'] is False
    assert by_id[removed.id]['comment']['id'] == reply.id


def test_a_removed_community(app, env):
    from app.api.alpha.utils.misc import get_modlog

    moderator, target, community, post, reply, baseline = env
    entry = an_entry('delete_community', moderator, community_id=community.id)

    answer = get_modlog(None, {'type_': 'ModRemoveCommunity'})

    assert [e['mod_remove_community']['id']
            for e in answer['removed_communities']] == [entry.id]


def test_a_ban_from_a_community_and_a_ban_from_the_instance(app, env):
    """One action, split by whether a community is named: `ban_user` with a
    community is a community ban, without one it is a site ban."""
    from app.api.alpha.utils.misc import get_modlog

    moderator, target, community, post, reply, baseline = env
    from_community = an_entry('ban_user', moderator, community_id=community.id,
                              target_user_id=target.id)
    from_site = an_entry('ban_user', moderator, target_user_id=target.id)

    answer = get_modlog(None, {})

    assert [e['mod_ban_from_community']['id']
            for e in answer['banned_from_community']] == [from_community.id]
    assert [e['mod_ban']['id'] for e in answer['banned']] == [from_site.id]


def test_an_unban_is_the_same_category(app, env):
    from app.api.alpha.utils.misc import get_modlog

    moderator, target, community, post, reply, baseline = env
    an_entry('unban_user', moderator, target_user_id=target.id)

    answer = get_modlog(None, {'type_': 'ModBan'})

    assert answer['banned'][0]['mod_ban']['banned'] is False
    assert answer['banned'][0]['banned_person']['id'] == target.id


def test_a_moderator_added_to_a_community_and_to_the_instance(app, env):
    from app.api.alpha.utils.misc import get_modlog

    moderator, target, community, post, reply, baseline = env
    to_community = an_entry('add_mod', moderator, community_id=community.id,
                            target_user_id=target.id)
    to_site = an_entry('add_mod', moderator, target_user_id=target.id)

    answer = get_modlog(None, {})

    assert [e['mod_add_community']['id']
            for e in answer['added_to_community']] == [to_community.id]
    assert [e['mod_add']['id'] for e in answer['added']] == [to_site.id]


def test_a_moderator_removed(app, env):
    from app.api.alpha.utils.misc import get_modlog

    moderator, target, community, post, reply, baseline = env
    an_entry('remove_mod', moderator, target_user_id=target.id)

    answer = get_modlog(None, {'type_': 'ModAdd'})

    assert answer['added'][0]['mod_add']['removed'] is True


def test_the_categories_this_instance_does_not_keep_are_empty(app, env):
    """Purges and transfers are in the response shape and not in the log."""
    from app.api.alpha.utils.misc import get_modlog

    moderator, target, community, post, reply, baseline = env

    answer = get_modlog(None, {})

    for key in ('transferred_to_community', 'admin_purged_persons',
                'admin_purged_communities', 'admin_purged_posts',
                'admin_purged_comments', 'hidden_communities'):
        assert answer[key] == []


# --------------------------------------------------------------------------
# Narrowing it
# --------------------------------------------------------------------------


def test_one_category_leaves_the_others_empty(app, env):
    from app.api.alpha.utils.misc import get_modlog

    moderator, target, community, post, reply, baseline = env
    an_entry('delete_post', moderator, post_id=post.id)
    an_entry('lock_post', moderator, post_id=post.id)

    answer = get_modlog(None, {'type_': 'ModRemovePost'})

    assert len(answer['removed_posts']) == 1
    assert answer['locked_posts'] == []


def test_a_category_nobody_has_heard_of_answers_nothing(app, env):
    from app.api.alpha.utils.misc import get_modlog

    moderator, target, community, post, reply, baseline = env
    an_entry('delete_post', moderator, post_id=post.id)

    answer = get_modlog(None, {'type_': 'ModNonsense'})

    assert answer == {'removed_posts': [], 'locked_posts': [],
                      'featured_posts': [], 'removed_comments': [],
                      'removed_communities': [], 'banned_from_community': [],
                      'banned': [], 'added_to_community': [],
                      'transferred_to_community': [], 'added': [],
                      'admin_purged_persons': [],
                      'admin_purged_communities': [],
                      'admin_purged_posts': [], 'admin_purged_comments': [],
                      'hidden_communities': []}


def test_the_log_can_be_narrowed_to_one_moderator(app, env):
    from app.api.alpha.utils.misc import get_modlog

    moderator, target, community, post, reply, baseline = env
    somebody_else = make_user(baseline.instance_local, 'othermod', local=True)
    db.session.commit()
    mine = an_entry('delete_post', moderator, post_id=post.id)
    an_entry('delete_post', somebody_else, post_id=post.id)

    answer = get_modlog(None, {'mod_person_id': moderator.id})

    assert [e['mod_remove_post']['id']
            for e in answer['removed_posts']] == [mine.id]


def test_the_log_can_be_narrowed_to_one_community(app, env):
    from app.api.alpha.utils.misc import get_modlog

    moderator, target, community, post, reply, baseline = env
    here = an_entry('delete_post', moderator, community_id=community.id,
                    post_id=post.id)
    an_entry('delete_post', moderator, community_id=baseline.community2.id,
             post_id=post.id)

    answer = get_modlog(None, {'community_id': community.id})

    assert [e['mod_remove_post']['id']
            for e in answer['removed_posts']] == [here.id]


def test_the_log_can_be_narrowed_to_one_person_acted_upon(app, env):
    from app.api.alpha.utils.misc import get_modlog

    moderator, target, community, post, reply, baseline = env
    theirs = an_entry('ban_user', moderator, target_user_id=target.id)
    an_entry('ban_user', moderator, target_user_id=baseline.user4.id)

    answer = get_modlog(None, {'other_person_id': target.id})

    assert [e['mod_ban']['id'] for e in answer['banned']] == [theirs.id]


def test_the_log_can_be_narrowed_to_one_post(app, env):
    from app.api.alpha.utils.misc import get_modlog

    moderator, target, community, post, reply, baseline = env
    here = an_entry('delete_post', moderator, post_id=post.id)
    an_entry('delete_post', moderator, post_id=baseline.post2.id)

    answer = get_modlog(None, {'post_id': post.id})

    assert [e['mod_remove_post']['id']
            for e in answer['removed_posts']] == [here.id]


def test_the_log_can_be_narrowed_to_one_comment(app, env):
    from app.api.alpha.utils.misc import get_modlog

    moderator, target, community, post, reply, baseline = env
    another = make_post_reply(post, target, body='another')
    another.body_html = '<p>another</p>'
    db.session.commit()
    here = an_entry('delete_post_reply', moderator, reply_id=reply.id)
    an_entry('delete_post_reply', moderator, reply_id=another.id)

    answer = get_modlog(None, {'comment_id': reply.id})

    assert [e['mod_remove_comment']['id']
            for e in answer['removed_comments']] == [here.id]


def test_the_log_is_paged(app, env):
    from app.api.alpha.utils.misc import get_modlog

    moderator, target, community, post, reply, baseline = env
    for _ in range(3):
        an_entry('delete_post', moderator, post_id=post.id)

    first = get_modlog(None, {'limit': 2})
    second = get_modlog(None, {'limit': 2, 'page': 2})

    assert len(first['removed_posts']) == 2
    assert len(second['removed_posts']) == 1


def test_an_entry_whose_subjects_are_gone(app, env):
    """Every view is guarded: an entry naming nobody and nothing still
    renders, with nulls where the objects would be."""
    from app.api.alpha.utils.misc import get_modlog

    moderator, target, community, post, reply, baseline = env
    entry = ModLog(action='delete_post', public=True, type='mod')
    db.session.add(entry)
    db.session.commit()

    answer = get_modlog(None, {'type_': 'ModRemovePost'})

    shown = answer['removed_posts'][0]
    assert shown['moderator'] is None
    assert shown['post'] is None
    assert shown['community'] is None
