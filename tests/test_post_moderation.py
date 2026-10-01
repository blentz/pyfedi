"""Deleting, purging, moving, reporting and blocking, from the post routes.

Sub-project 82, slice D. Seven defects, all measured:

* `post_mea_culpa` had **no authorization at all** -- any logged-in account
  could mark anybody's post as a mistake and turn its comments off (D1097);
* `move_post` consulted only the SOURCE community, so a moderator of any
  community could move a post into a PRIVATE one they do not belong to
  (D1102);
* `post_delete` had no `else`, so an unauthorized caller was a 500 (D1098);
* `post_block_domain` on a post with no domain was a not-null violation
  (D1099);
* `HX-Current-Url` was read straight into `in` tests at six more sites
  (D1100);
* `post_block_instance` contradicted the refusal it had just triggered, and
  fell over on a post with no instance (D1101);
* `post_reply_purge` and `post_reply_block_user` carry two ids and related
  them nowhere -- D1077's tenth and eleventh sites (D1103).
"""
from unittest.mock import patch

import pytest

from app import db
from app.constants import POST_TYPE_LINK
from app.models import (CommunityBlock, DomainBlock, Instance, InstanceBlock,
                        Language, Post, PostReply, Report, Site, User,
                        UserBlock)
from app.utils import utcnow
from tests.factories import (grant_permission, make_community, make_community_member,
                             make_domain, make_instance, make_post,
                             make_post_reply, make_user)

pytestmark = pytest.mark.usefixtures('site')


def instance(domain='test.piefed.local', software='piefed'):
    """Fact 394."""
    existing = Instance.query.filter_by(domain=domain).first()
    return existing if existing is not None else make_instance(domain,
                                                               software=software)


def csrf(app, client):
    """Facts 355, 515."""
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf

    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as session:
        session['csrf_token'] = raw
    return token


def as_user(app, user):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return client


@pytest.fixture
def env(app, db_session):
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    local = instance()
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1  # fact 347
    mod = make_user(local, 'mod', local=True, with_keys=True)
    mod.verified = True
    author = make_user(local, 'author', local=True, with_keys=True)
    author.verified = True
    outsider = make_user(local, 'outsider', local=True, with_keys=True)
    outsider.verified = True
    community = make_community('general')
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.commit()
    make_community_member(mod, community, is_moderator=True)
    make_community_member(author, community)
    post = make_post(community, author, 'https://test.piefed.local/p/1',
                     title='THETITLE')
    post.body_html = '<p>THEBODY</p>'
    db.session.commit()
    return app.test_client(), community, post, mod, author, outsider


def a_reply(post, user, body='a reply', **columns):
    """Fact 501."""
    reply = make_post_reply(post, user, body=body)
    reply.body_html = f'<p>{body}</p>'
    for column, value in columns.items():
        setattr(reply, column, value)
    db.session.commit()
    return reply


def elsewhere(app, name='elsewhere', private=False):
    community = make_community(name)
    community.private = private
    db.session.commit()
    author = make_user(instance(), f'{name}_author', local=True,
                       with_keys=True)
    author.verified = True
    db.session.commit()
    make_community_member(author, community)
    post = make_post(community, author, f'https://test.piefed.local/p/{name}',
                     title=f'a post in {name}')
    post.body_html = f'<p>body of {name}</p>'
    db.session.commit()
    return community, post, author


# --------------------------------------------------------------------------
# D1097 -- "I changed my mind"
# --------------------------------------------------------------------------


def test_an_author_can_admit_a_mistake(app, env):
    """The feature: the post is marked, and its comments close."""
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/mea_culpa',
                           data={'submit': 'I changed my mind',
                                 'csrf_token': token})

    assert response.status_code == 302
    db.session.refresh(post)
    assert post.mea_culpa is True
    assert post.comments_enabled is False


def test_nobody_else_can_admit_a_mistake_on_your_behalf(app, env):
    """D1097. There was no authorization here at all, so any logged-in account
    could mark anybody's post as a mistake and **turn its comments off**.
    Measured: `PROBE al1 stranger mea_culpa: 302 | mea_culpa now=True
    comments_enabled=False`."""
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/mea_culpa',
                           data={'submit': 'I changed my mind',
                                 'csrf_token': token})

    assert response.status_code == 401
    db.session.refresh(post)
    assert post.mea_culpa is False
    assert post.comments_enabled is True


def test_not_even_a_moderator_can(app, env):
    """A moderator removes a post; they do not put words in its author's
    mouth."""
    anon, community, post, mod, author, outsider = env

    assert as_user(app, mod).get(
        f'/post/{post.id}/mea_culpa').status_code == 401


def test_the_mea_culpa_page_asks_first(app, env):
    anon, community, post, mod, author, outsider = env

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        response = as_user(app, author).get(f'/post/{post.id}/mea_culpa')

    assert response.status_code == 200
    assert render.call_args.kwargs['post'].id == post.id
    db.session.refresh(post)
    assert post.mea_culpa is False


def test_an_unknown_post_has_no_mea_culpa(app, env):
    anon, community, post, mod, author, outsider = env

    assert as_user(app, author).get(
        '/post/999999/mea_culpa').status_code == 404


# --------------------------------------------------------------------------
# D1098 -- deleting, restoring, purging
# --------------------------------------------------------------------------


def test_an_author_deletes_their_own_post(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    token = csrf(app, client)

    with patch('app.post.routes.delete_post') as deleted:
        response = client.post(f'/post/{post.id}/delete',
                               data={'submit': 'Yes', 'csrf_token': token})

    assert response.status_code == 302
    assert deleted.call_args.args[0] == post.id


def test_a_moderator_removes_a_post_with_a_reason(app, env):
    """Somebody else's post goes through `mod_remove_post`, which records the
    reason in the mod log."""
    anon, community, post, mod, author, outsider = env
    client = as_user(app, mod)
    token = csrf(app, client)

    with patch('app.post.routes.mod_remove_post') as removed:
        client.post(f'/post/{post.id}/delete',
                    data={'submit': 'Yes', 'reason': 'off topic',
                          'csrf_token': token})

    assert removed.call_args.args[1] == 'off topic'


def test_a_stranger_cannot_delete_a_post(app, env):
    """D1098. There was no `else`, so this fell off the end of the view and
    returned None: `TypeError: The view function for 'post.post_delete' did
    not return a valid response` -- a 500 in the logs in place of the refusal
    that was meant."""
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.delete_post') as deleted:
        response = client.post(f'/post/{post.id}/delete',
                               data={'submit': 'Yes', 'csrf_token': token})

    assert response.status_code == 401
    assert deleted.call_args is None


def test_an_author_banned_from_the_community_cannot_delete(app, env):
    anon, community, post, mod, author, outsider = env
    from tests.factories import make_community_ban
    make_community_ban(author, community, banned_by=mod)
    client = as_user(app, author)

    assert client.get(f'/post/{post.id}/delete').status_code == 403


def test_the_delete_page_asks_first(app, env):
    anon, community, post, mod, author, outsider = env

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        response = as_user(app, author).get(f'/post/{post.id}/delete')

    assert response.status_code == 200
    assert 'THETITLE' in render.call_args.kwargs['title']


def test_deleting_from_a_listing_goes_back_to_it(app, env):
    """The referrer is user-supplied, so it is origin-checked like every other
    redirect target -- and a referrer pointing at the post itself is dropped,
    because the post is what was just deleted."""
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    token = csrf(app, client)

    with patch('app.post.routes.delete_post'):
        response = client.post(
            f'/post/{post.id}/delete',
            data={'submit': 'Yes', 'referrer': '/c/general',
                  'csrf_token': token})

    assert response.headers['Location'] == '/c/general'


def test_deleting_from_the_post_page_goes_to_the_community(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    token = csrf(app, client)

    with patch('app.post.routes.delete_post'):
        response = client.post(
            f'/post/{post.id}/delete',
            data={'submit': 'Yes', 'referrer': f'/post/{post.id}',
                  'csrf_token': token})

    assert '/c/' in response.headers['Location']


def test_a_foreign_referrer_is_dropped(app, env):
    """Fact: `safe_redirect_target` is what stops a delete link carrying an
    off-site destination."""
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    token = csrf(app, client)

    with patch('app.post.routes.delete_post'):
        response = client.post(
            f'/post/{post.id}/delete',
            data={'submit': 'Yes', 'referrer': 'https://evil.example/x',
                  'csrf_token': token})

    assert 'evil.example' not in response.headers['Location']


def test_an_author_restores_their_own_post(app, env):
    anon, community, post, mod, author, outsider = env
    post.deleted = True
    post.deleted_by = author.id
    db.session.commit()
    client = as_user(app, author)
    token = csrf(app, client)

    with patch('app.post.routes.restore_post') as restored:
        response = client.post(f'/post/{post.id}/restore',
                               data={'csrf_token': token})

    assert response.status_code == 302
    assert restored.call_args.args[0] == post.id


def test_a_moderator_restoring_uses_the_moderation_path(app, env):
    """A post a moderator removed comes back through `mod_restore_post`, which
    records it; the author's own withdrawal does not."""
    anon, community, post, mod, author, outsider = env
    post.deleted = True
    post.deleted_by = mod.id
    db.session.commit()
    client = as_user(app, mod)
    token = csrf(app, client)

    with patch('app.post.routes.mod_restore_post') as restored:
        client.post(f'/post/{post.id}/restore', data={'csrf_token': token})

    assert restored.call_args.args[0] == post.id


def test_a_stranger_cannot_restore_a_post(app, env):
    anon, community, post, mod, author, outsider = env
    post.deleted = True
    post.deleted_by = mod.id
    db.session.commit()
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.restore_post') as restored:
        with patch('app.post.routes.mod_restore_post') as mod_restored:
            client.post(f'/post/{post.id}/restore',
                        data={'csrf_token': token})

    assert restored.call_args is None
    assert mod_restored.call_args is None


def test_an_author_may_not_undo_a_moderators_removal(app, env):
    """D421, fixed. The author passed the route's gate, was routed to
    `mod_restore_post` because a moderator did the removing, and that function's
    own gate raised -- an unhandled 500 on an ordinary click. The author now
    gets a 403 and the post stays removed (owner ruling 2026-09-30)."""
    anon, community, post, mod, author, outsider = env
    post.deleted = True
    post.deleted_by = mod.id
    db.session.commit()
    client = as_user(app, author)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/restore', data={'csrf_token': token})

    assert response.status_code == 403
    db.session.refresh(post)
    assert post.deleted is True


def test_an_administer_all_communities_holder_may_remove_a_post(app, env):
    """D421, fixed. The route admitted the permission and `mod_remove_post`
    did not, so the removal was a 500. Both now ask `can_mod_post`, and the
    permission removes a post as an admin would (owner ruling 2026-09-30)."""
    anon, community, post, mod, author, outsider = env
    grant_permission(outsider, 'administer all communities')
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/delete',
                           data={'submit': 'Yes', 'reason': 'spam', 'csrf_token': token})

    assert response.status_code == 302
    db.session.refresh(post)
    assert post.deleted is True
    assert post.deleted_by == outsider.id


def test_purging_a_deleted_post(app, env):
    """Purge is the deletion that cannot be undone: the row and its
    dependencies go."""
    anon, community, post, mod, author, outsider = env
    post.deleted = True
    post.deleted_by = mod.id
    db.session.commit()
    post_id = post.id
    client = as_user(app, mod)
    token = csrf(app, client)

    response = client.post(f'/post/{post_id}/purge',
                           data={'csrf_token': token})

    assert response.status_code == 302
    assert db.session.get(Post, post_id) is None


def test_a_live_post_cannot_be_purged(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, mod)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/purge',
                           data={'csrf_token': token})

    assert response.status_code == 404
    assert db.session.get(Post, post.id) is not None


def test_a_stranger_cannot_purge_a_post(app, env):
    anon, community, post, mod, author, outsider = env
    post.deleted = True
    post.deleted_by = mod.id
    db.session.commit()
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/purge',
                           data={'csrf_token': token})

    assert response.status_code == 401
    assert db.session.get(Post, post.id) is not None


def test_whoever_deleted_it_may_purge_it(app, env):
    """`post.deleted_by == current_user.id` -- an author who withdrew their
    own post can finish the job without a moderator."""
    anon, community, post, mod, author, outsider = env
    post.deleted = True
    post.deleted_by = author.id
    db.session.commit()
    post_id = post.id
    client = as_user(app, author)
    token = csrf(app, client)

    client.post(f'/post/{post_id}/purge', data={'csrf_token': token})

    assert db.session.get(Post, post_id) is None


# --------------------------------------------------------------------------
# D1103 -- purging and blocking from a comment
# --------------------------------------------------------------------------


def test_purging_a_deleted_reply(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author, deleted=True, deleted_by=mod.id)
    reply_id = reply.id
    client = as_user(app, mod)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/comment/{reply_id}/purge',
                           data={'csrf_token': token})

    assert response.status_code == 302
    assert db.session.get(PostReply, reply_id) is None


def test_a_reply_from_another_post_cannot_be_purged(app, env):
    """D1077's tenth site. Purging is the one deletion that cannot be undone,
    and the permission is tested against `post.community` while the row
    deleted is `post_reply`."""
    anon, community, post, mod, author, outsider = env
    other, other_post, other_author = elsewhere(app)
    their_reply = a_reply(other_post, other_author, deleted=True,
                          deleted_by=other_author.id)
    reply_id = their_reply.id
    client = as_user(app, mod)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/comment/{reply_id}/purge',
                           data={'csrf_token': token})

    assert response.status_code == 404
    assert db.session.get(PostReply, reply_id) is not None


def test_a_live_reply_cannot_be_purged(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    client = as_user(app, mod)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/comment/{reply.id}/purge',
                           data={'csrf_token': token})

    assert response.status_code == 404


def test_a_reply_that_has_answers_cannot_be_purged(app, env):
    """Purging a comment with replies under it would orphan them, so it is
    refused with a message rather than done."""
    anon, community, post, mod, author, outsider = env
    parent = a_reply(post, author, deleted=True, deleted_by=mod.id)
    child = a_reply(post, author, body='an answer', parent_id=parent.id)
    parent.child_count = 1
    db.session.commit()
    client = as_user(app, mod)
    token = csrf(app, client)

    with patch('app.post.routes.flash') as flashed:
        client.post(f'/post/{post.id}/comment/{parent.id}/purge',
                    data={'csrf_token': token})

    assert db.session.get(PostReply, parent.id) is not None
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'cannot be purged' in messages


def test_a_stranger_cannot_purge_a_reply(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author, deleted=True, deleted_by=mod.id)
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/comment/{reply.id}/purge',
                           data={'csrf_token': token})

    assert response.status_code == 401
    assert db.session.get(PostReply, reply.id) is not None


def test_blocking_a_comments_author(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, mod)
    client = as_user(app, author)
    token = csrf(app, client)

    response = client.post(
        f'/post/{post.id}/comment/{reply.id}/block_user',
        data={'csrf_token': token})

    assert response.status_code == 302
    assert UserBlock.query.filter_by(blocker_id=author.id,
                                     blocked_id=mod.id).count() == 1


def test_blocking_twice_makes_one_row(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, mod)
    client = as_user(app, author)
    token = csrf(app, client)

    client.post(f'/post/{post.id}/comment/{reply.id}/block_user',
                data={'csrf_token': token})
    client.post(f'/post/{post.id}/comment/{reply.id}/block_user',
                data={'csrf_token': token})

    assert UserBlock.query.filter_by(blocker_id=author.id,
                                     blocked_id=mod.id).count() == 1


def test_the_comment_block_refuses_a_foreign_comment(app, env):
    """D1077's eleventh site: the redirect compares `post_reply.author` with
    `post.author`, so a mismatched pair sent the reader somewhere unrelated to
    either."""
    anon, community, post, mod, author, outsider = env
    other, other_post, other_author = elsewhere(app)
    their_reply = a_reply(other_post, other_author)
    client = as_user(app, author)
    token = csrf(app, client)

    response = client.post(
        f'/post/{post.id}/comment/{their_reply.id}/block_user',
        data={'csrf_token': token})

    assert response.status_code == 404
    assert UserBlock.query.count() == 0


@pytest.mark.parametrize('current_url, expected', [
    ('https://test.piefed.local/post/1', 'post'),
    ('https://test.piefed.local/u/mod', 'index'),
    ('https://test.piefed.local/somewhere', 'same'),
])
def test_the_comment_block_sends_htmx_somewhere_sensible(app, env,
                                                         current_url,
                                                         expected):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, mod)
    client = as_user(app, author)
    token = csrf(app, client)

    response = client.post(
        f'/post/{post.id}/comment/{reply.id}/block_user',
        data={'csrf_token': token},
        headers={'HX-Request': 'true', 'HX-Current-Url': current_url})

    location = response.headers['HX-Redirect']
    if expected == 'post':
        assert f'/post/{post.id}' in location
    elif expected == 'index':
        assert location == '/home'
    else:
        assert location == current_url


def test_blocking_the_posts_own_author_from_a_comment(app, env):
    """When the comment's author IS the post's author there is no post left
    worth returning to, so the reader goes to the community."""
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post(
        f'/post/{post.id}/comment/{reply.id}/block_user',
        data={'csrf_token': token},
        headers={'HX-Request': 'true',
                 'HX-Current-Url': f'https://test.piefed.local/post/{post.id}'})

    assert '/c/' in response.headers['HX-Redirect']


def test_the_comment_block_survives_a_missing_hx_current_url(app, env):
    """D1100."""
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, mod)
    client = as_user(app, author)
    token = csrf(app, client)

    response = client.post(
        f'/post/{post.id}/comment/{reply.id}/block_user',
        data={'csrf_token': token}, headers={'HX-Request': 'true'})

    assert response.status_code == 200


def test_blocking_a_comments_instance(app, env):
    anon, community, post, mod, author, outsider = env
    remote = make_instance('remote.example', software='lemmy')
    db.session.commit()
    stranger = make_user(remote, 'stranger')
    db.session.commit()
    reply = a_reply(post, stranger)
    reply.instance_id = remote.id
    db.session.commit()
    client = as_user(app, author)
    token = csrf(app, client)

    response = client.post(
        f'/post/{post.id}/comment/{reply.id}/block_instance',
        data={'csrf_token': token})

    assert response.status_code == 302
    assert InstanceBlock.query.filter_by(user_id=author.id,
                                         instance_id=remote.id).count() == 1


def test_a_comment_on_this_instance_cannot_be_instance_blocked(app, env):
    """D1101's twin: `block_remote_instance` refuses instance 1 with its own
    flash, and the line after it said the opposite."""
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, mod)
    client = as_user(app, author)
    token = csrf(app, client)

    response = client.post(
        f'/post/{post.id}/comment/{reply.id}/block_instance',
        data={'csrf_token': token})

    assert response.status_code == 404
    assert InstanceBlock.query.count() == 0


def test_a_comment_with_no_instance_cannot_be_instance_blocked(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, mod)
    reply.instance_id = None
    db.session.commit()
    client = as_user(app, author)
    token = csrf(app, client)

    response = client.post(
        f'/post/{post.id}/comment/{reply.id}/block_instance',
        data={'csrf_token': token})

    assert response.status_code == 404


@pytest.mark.parametrize('current_url, expected', [
    ('https://remote.example/x', 'index'),
    ('https://test.piefed.local/post/1', 'same'),
    ('https://test.piefed.local/somewhere', 'same'),
])
def test_the_comment_instance_block_sends_htmx_somewhere_sensible(
        app, env, current_url, expected):
    """Once an instance is blocked there is nothing of it left to look at, so
    a page belonging to it is left behind. A LOCAL post carrying one blocked
    comment is still worth reading, so that one stays put."""
    anon, community, post, mod, author, outsider = env
    remote = make_instance('remote.example', software='lemmy')
    db.session.commit()
    stranger = make_user(remote, 'stranger')
    db.session.commit()
    reply = a_reply(post, stranger)
    reply.instance_id = remote.id
    db.session.commit()
    client = as_user(app, author)
    token = csrf(app, client)

    response = client.post(
        f'/post/{post.id}/comment/{reply.id}/block_instance',
        data={'csrf_token': token},
        headers={'HX-Request': 'true', 'HX-Current-Url': current_url})

    location = response.headers['HX-Redirect']
    assert location == ('/home' if expected == 'index' else current_url)


def test_a_post_on_the_blocked_instance_is_left_behind_too(app, env):
    """When the post itself came from the instance just blocked, staying on
    it would show a page of content the reader has just said they do not want.
    """
    anon, community, post, mod, author, outsider = env
    remote = make_instance('remote.example', software='lemmy')
    db.session.commit()
    stranger = make_user(remote, 'stranger')
    db.session.commit()
    reply = a_reply(post, stranger)
    reply.instance_id = remote.id
    post.instance_id = remote.id
    db.session.commit()
    client = as_user(app, author)
    token = csrf(app, client)

    response = client.post(
        f'/post/{post.id}/comment/{reply.id}/block_instance',
        data={'csrf_token': token},
        headers={'HX-Request': 'true',
                 'HX-Current-Url': f'https://test.piefed.local/post/{post.id}'})

    assert response.headers['HX-Redirect'] == '/home'


def test_the_comment_instance_block_survives_a_missing_header(app, env):
    anon, community, post, mod, author, outsider = env
    remote = make_instance('remote.example', software='lemmy')
    db.session.commit()
    stranger = make_user(remote, 'stranger')
    db.session.commit()
    reply = a_reply(post, stranger)
    reply.instance_id = remote.id
    db.session.commit()
    client = as_user(app, author)
    token = csrf(app, client)

    response = client.post(
        f'/post/{post.id}/comment/{reply.id}/block_instance',
        data={'csrf_token': token}, headers={'HX-Request': 'true'})

    assert response.status_code == 200


# --------------------------------------------------------------------------
# D1099, D1100, D1101 -- blocking from a post
# --------------------------------------------------------------------------


def test_blocking_a_posts_author(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/block_user',
                           data={'csrf_token': token})

    assert response.status_code == 302
    assert UserBlock.query.filter_by(blocker_id=outsider.id,
                                     blocked_id=author.id).count() == 1


@pytest.mark.parametrize('current_url, expected', [
    ('https://test.piefed.local/post/1', 'community'),
    ('https://test.piefed.local/u/author', 'index'),
    ('https://test.piefed.local/somewhere', 'same'),
])
def test_the_post_block_sends_htmx_somewhere_sensible(app, env, current_url,
                                                      expected):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post(
        f'/post/{post.id}/block_user', data={'csrf_token': token},
        headers={'HX-Request': 'true', 'HX-Current-Url': current_url})

    location = response.headers['HX-Redirect']
    if expected == 'community':
        assert '/c/' in location
    elif expected == 'index':
        assert location == '/home'
    else:
        assert location == current_url


def test_the_post_block_survives_a_missing_hx_current_url(app, env):
    """D1100. Measured across six sites: `TypeError: argument of type
    'NoneType' is not iterable`."""
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/block_user',
                           data={'csrf_token': token},
                           headers={'HX-Request': 'true'})

    assert response.status_code == 200


def test_blocking_a_posts_domain(app, env):
    anon, community, post, mod, author, outsider = env
    domain = make_domain('example.com')
    post.type = POST_TYPE_LINK
    post.url = 'https://example.com/an-article'
    post.domain_id = domain.id
    db.session.commit()
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/block_domain',
                           data={'csrf_token': token})

    assert response.status_code == 302
    assert DomainBlock.query.filter_by(user_id=outsider.id,
                                       domain_id=domain.id).count() == 1


def test_a_post_with_no_domain_cannot_have_one_blocked(app, env):
    """D1099. Only a link post has a domain; for anything else `domain_id` is
    None and the INSERT was `psycopg2.errors.NotNullViolation: null value in
    column "domain_id" of relation "domain_block" violates not-null
    constraint`."""
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/block_domain',
                           data={'csrf_token': token})

    assert response.status_code == 404
    assert DomainBlock.query.count() == 0


def test_the_domain_block_survives_a_missing_hx_current_url(app, env):
    anon, community, post, mod, author, outsider = env
    domain = make_domain('example.com')
    post.domain_id = domain.id
    db.session.commit()
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/block_domain',
                           data={'csrf_token': token},
                           headers={'HX-Request': 'true'})

    assert response.status_code == 200


@pytest.mark.parametrize('current_url, expected', [
    ('https://test.piefed.local/post/1', 'index'),
    ('https://test.piefed.local/somewhere', 'same'),
])
def test_the_domain_block_sends_htmx_somewhere_sensible(app, env, current_url,
                                                        expected):
    anon, community, post, mod, author, outsider = env
    domain = make_domain('example.com')
    post.domain_id = domain.id
    db.session.commit()
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post(
        f'/post/{post.id}/block_domain', data={'csrf_token': token},
        headers={'HX-Request': 'true', 'HX-Current-Url': current_url})

    location = response.headers['HX-Redirect']
    assert location == ('/home' if expected == 'index' else current_url)


def test_blocking_a_posts_community(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/block_community',
                           data={'csrf_token': token})

    assert response.status_code == 302
    assert CommunityBlock.query.filter_by(user_id=outsider.id,
                                          community_id=community.id).count() \
        == 1


@pytest.mark.parametrize('current_url, expected', [
    ('https://test.piefed.local/c/general', 'index'),
    ('https://test.piefed.local/post/1', 'index'),
    ('https://test.piefed.local/somewhere', 'same'),
])
def test_the_community_block_sends_htmx_somewhere_sensible(app, env,
                                                           current_url,
                                                           expected):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post(
        f'/post/{post.id}/block_community', data={'csrf_token': token},
        headers={'HX-Request': 'true', 'HX-Current-Url': current_url})

    location = response.headers['HX-Redirect']
    assert location == ('/home' if expected == 'index' else current_url)


def test_the_community_block_survives_a_missing_hx_current_url(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/block_community',
                           data={'csrf_token': token},
                           headers={'HX-Request': 'true'})

    assert response.status_code == 200


def test_blocking_a_posts_instance(app, env):
    anon, community, post, mod, author, outsider = env
    remote = make_instance('remote.example', software='lemmy')
    db.session.commit()
    post.instance_id = remote.id
    db.session.commit()
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/block_instance',
                           data={'csrf_token': token})

    assert response.status_code == 302
    assert InstanceBlock.query.filter_by(user_id=outsider.id,
                                         instance_id=remote.id).count() == 1


def test_this_instance_cannot_be_blocked(app, env):
    """D1101. `block_remote_instance` refuses instance 1 with its own flash,
    and the line after it flashed "Content from test.piefed.local will be
    hidden." -- two messages contradicting each other, one of them false."""
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/block_instance',
                           data={'csrf_token': token})

    assert response.status_code == 404
    assert InstanceBlock.query.count() == 0


def test_a_post_with_no_instance_cannot_be_instance_blocked(app, env):
    anon, community, post, mod, author, outsider = env
    post.instance_id = None
    db.session.commit()
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/block_instance',
                           data={'csrf_token': token})

    assert response.status_code == 404


@pytest.mark.parametrize('current_url, expected', [
    ('https://remote.example/x', 'index'),
    ('https://test.piefed.local/post/1', 'index'),
    ('https://test.piefed.local/somewhere', 'same'),
])
def test_the_instance_block_sends_htmx_somewhere_sensible(app, env,
                                                          current_url,
                                                          expected):
    anon, community, post, mod, author, outsider = env
    remote = make_instance('remote.example', software='lemmy')
    db.session.commit()
    post.instance_id = remote.id
    db.session.commit()
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post(
        f'/post/{post.id}/block_instance', data={'csrf_token': token},
        headers={'HX-Request': 'true', 'HX-Current-Url': current_url})

    location = response.headers['HX-Redirect']
    assert location == ('/home' if expected == 'index' else current_url)


def test_the_instance_block_survives_a_missing_hx_current_url(app, env):
    anon, community, post, mod, author, outsider = env
    remote = make_instance('remote.example', software='lemmy')
    db.session.commit()
    post.instance_id = remote.id
    db.session.commit()
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/block_instance',
                           data={'csrf_token': token},
                           headers={'HX-Request': 'true'})

    assert response.status_code == 200


# --------------------------------------------------------------------------
# D1102 -- moving a post
# --------------------------------------------------------------------------


def test_a_moderator_moves_a_post(app, env):
    anon, community, post, mod, author, outsider = env
    destination = make_community('destination')
    db.session.commit()
    make_community_member(mod, destination, is_moderator=True)
    client = as_user(app, mod)
    token = csrf(app, client)

    with patch('app.shared.post.task_selector'):
        response = client.post(
            f'/post/{post.id}/move',
            data={'which_community': 'destination', 'submit': 'Move',
                  'csrf_token': token})

    assert response.status_code == 302
    db.session.refresh(post)
    assert post.community_id == destination.id


def test_a_post_cannot_be_moved_into_a_private_community(app, env):
    """D1102. `move_post` consulted only the SOURCE community's moderators, so
    a moderator of any community could move a post into a PRIVATE one they do
    not belong to -- content injected past its membership. Measured: `PROBE
    am1 mod moves into a private community: 302 | post now in theirs
    (private=True)`."""
    anon, community, post, mod, author, outsider = env
    secret, secret_post, member = elsewhere(app, 'secret', private=True)
    client = as_user(app, mod)
    token = csrf(app, client)

    with patch('app.shared.post.task_selector'):
        with patch('app.shared.post.flash') as flashed:
            client.post(f'/post/{post.id}/move',
                        data={'which_community': 'secret', 'submit': 'Move',
                              'csrf_token': token})

    db.session.refresh(post)
    assert post.community_id == community.id
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'cannot post in that community' in messages


def test_a_post_cannot_be_moved_into_a_banned_community(app, env):
    """`can_create_post` is the destination's own test, so everything it
    refuses -- a banned community, one restricted to its moderators -- is
    refused here too."""
    anon, community, post, mod, author, outsider = env
    destination = make_community('destination')
    destination.banned = True
    db.session.commit()
    client = as_user(app, mod)
    token = csrf(app, client)

    with patch('app.shared.post.task_selector'):
        with patch('app.shared.post.flash'):
            client.post(f'/post/{post.id}/move',
                        data={'which_community': 'destination',
                              'submit': 'Move', 'csrf_token': token})

    db.session.refresh(post)
    assert post.community_id == community.id


def test_a_post_cannot_be_moved_into_a_community_you_are_banned_from(app,
                                                                     env):
    """Being banned from the destination is the same refusal as its being
    private: the actor has no standing there, whatever they moderate
    elsewhere."""
    anon, community, post, mod, author, outsider = env
    from tests.factories import make_community_ban
    destination = make_community('destination')
    db.session.commit()
    make_community_ban(mod, destination, banned_by=author)
    client = as_user(app, mod)
    token = csrf(app, client)

    with patch('app.shared.post.task_selector'):
        with patch('app.shared.post.flash') as flashed:
            client.post(f'/post/{post.id}/move',
                        data={'which_community': 'destination',
                              'submit': 'Move', 'csrf_token': token})

    db.session.refresh(post)
    assert post.community_id == community.id
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'cannot post in that community' in messages


def test_moving_to_a_community_that_does_not_exist(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, mod)
    token = csrf(app, client)

    with patch('app.post.routes.flash') as flashed:
        response = client.post(
            f'/post/{post.id}/move',
            data={'which_community': 'nosuchplace', 'submit': 'Move',
                  'csrf_token': token})

    assert response.status_code == 302
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'Could not find that community' in messages


def test_a_stranger_cannot_move_a_post(app, env):
    anon, community, post, mod, author, outsider = env

    assert as_user(app, outsider).get(
        f'/post/{post.id}/move').status_code == 403


def test_the_move_page_asks_first(app, env):
    anon, community, post, mod, author, outsider = env

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        response = as_user(app, mod).get(f'/post/{post.id}/move')

    assert response.status_code == 200
    assert render.call_args.kwargs['post'].id == post.id


def test_an_unknown_post_cannot_be_moved(app, env):
    anon, community, post, mod, author, outsider = env

    assert as_user(app, mod).get('/post/999999/move').status_code == 404


# --------------------------------------------------------------------------
# Reporting a post
# --------------------------------------------------------------------------


def test_reporting_a_post(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post(
        f'/post/{post.id}/report',
        data={'reasons': ['1'], 'description': 'spam', 'submit': 'Report',
              'csrf_token': token})

    assert response.status_code == 302
    report = Report.query.one()
    assert report.suspect_post_id == post.id
    assert report.reporter_id == outsider.id


def test_a_post_whose_reports_are_ignored_warns_on_the_page(app, env):
    anon, community, post, mod, author, outsider = env
    post.reports = -1
    db.session.commit()

    with patch('app.post.routes.flash') as flashed:
        as_user(app, outsider).get(f'/post/{post.id}/report')

    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'Moderators have already assessed reports' in messages


def test_a_further_report_on_such_a_post_is_dropped(app, env):
    anon, community, post, mod, author, outsider = env
    post.reports = -1
    db.session.commit()
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.flash') as flashed:
        response = client.post(
            f'/post/{post.id}/report',
            data={'reasons': ['1'], 'description': 'spam',
                  'submit': 'Report', 'csrf_token': token})

    assert response.status_code == 302
    assert Report.query.count() == 0
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'already been reported' in messages


def test_the_report_form_defaults_to_reporting_remotely(app, env):
    anon, community, post, mod, author, outsider = env

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        response = as_user(app, outsider).get(f'/post/{post.id}/report')

    assert response.status_code == 200
    assert render.call_args.kwargs['form'].report_remote.data is True


def test_an_unknown_post_cannot_be_reported(app, env):
    anon, community, post, mod, author, outsider = env

    assert as_user(app, outsider).get(
        '/post/999999/report').status_code == 404
