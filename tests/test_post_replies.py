"""Comment threads, and the routes that moderate a single reply.

Sub-project 82, slice A -- the first of `app/post/routes.py`. Two defects,
both measured:

* every route that carries `/post/<post_id>/comment/<comment_id>` fetched the
  two objects independently and never asked whether they belong together,
  while making its authorization test against `post.community`. So a
  moderator of ANY community could restore a deleted reply in another one,
  undoing that community's moderators' removal (D1077). Eight routes carry
  that pair; all eight now check it;
* `continue_discussion` and its ajax twin rendered a PRIVATE community's
  conversation to an anonymous reader, because the private-community check
  `show_post` makes was never made on the routes that show the same content
  one comment at a time (D1078). Measured: `PROBE ae4 anon status: 200 |
  private body visible: True`. The reply options page and the report form had
  the same hole, and the report form named the private post's title.
"""
from unittest.mock import patch

import pytest

from app import db
from app.constants import POST_TYPE_EVENT, POST_TYPE_POLL
from app.models import (Event, Instance, Language, PostReplyBookmark, Report,
                        Site, User, UserFlair)
from app.utils import utcnow
from tests.factories import (make_community, make_community_member,
                             make_instance, make_poll, make_poll_choice,
                             make_post, make_post_reply, make_user)

pytestmark = pytest.mark.usefixtures('site')


def instance(domain='test.piefed.local', software='piefed'):
    """Fact 394."""
    existing = Instance.query.filter_by(domain=domain).first()
    return existing if existing is not None else make_instance(domain,
                                                               software=software)


def csrf(app, client):
    """Fact 355."""
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf

    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as session:
        session['csrf_token'] = raw
    return token


def rendered(app):
    """`continue_discussion` sets cache headers on what `render_template`
    returns, so a patch that answers a bare string is
    `AttributeError: 'str' object has no attribute 'headers'`. Answer a real
    response instead."""
    return app.response_class('rendered')


def as_user(app, user):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return client


@pytest.fixture
def env(app, db_session):
    """`mod` moderates `community`; `author` wrote the reply; `outsider` has no
    standing anywhere -- which is what the two defects are about."""
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    local = instance()
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1  # fact 347
    mod = make_user(local, 'mod', local=True)
    author = make_user(local, 'author', local=True)
    outsider = make_user(local, 'outsider', local=True)
    community = make_community('general')
    # `languages_for_form` skips code 'und', so a form row that has to pass
    # `language_id`'s DataRequired needs a real language as well.
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.add(Language(code='en', name='English'))
    db.session.commit()
    make_community_member(mod, community, is_moderator=True)
    make_community_member(author, community)
    post = make_post(community, author, 'https://test.piefed.local/p/1',
                     title='a post')
    db.session.commit()
    return as_user(app, author), community, post, mod, author, outsider


def a_reply(post, user, body='a reply', **columns):
    """`make_post_reply` leaves `body_html` unset, and the reply teaser
    template runs the stored HTML through `community_link_to_href` -- which
    answers `TypeError: expected string or bytes-like object, got 'NoneType'`.
    Any row that renders a thread has to set it."""
    reply = make_post_reply(post, user, body=body)
    reply.body_html = f'<p>{body}</p>'
    for column, value in columns.items():
        setattr(reply, column, value)
    db.session.commit()
    return reply


def elsewhere(app, name='elsewhere'):
    """A second community, with its own post and its own author -- the other
    half of every id-mismatch row."""
    community = make_community(name)
    db.session.commit()
    author = make_user(instance(), f'{name}_author', local=True)
    db.session.commit()
    make_community_member(author, community)
    post = make_post(community, author, f'https://test.piefed.local/p/{name}',
                     title=f'a post in {name}')
    db.session.commit()
    return community, post, author


def private_community(app, name='secret'):
    community, post, author = elsewhere(app, name)
    community.private = True
    db.session.commit()
    return community, post, author


# --------------------------------------------------------------------------
# D1078 -- the private-community check the comment pages never made
# --------------------------------------------------------------------------


def test_a_private_communitys_thread_is_refused_to_a_non_member(app, env):
    """D1078. `show_post` refuses a private community's post to anyone who is
    not a member; `continue_discussion` shows the same content one comment at
    a time and made no such check. Measured: `PROBE ae1 status: 200 | private
    body visible: True`."""
    client, community, post, mod, author, outsider = env
    secret, secret_post, member = private_community(app)
    reply = a_reply(secret_post, member, body='PRIVATEBODY')

    response = as_user(app, outsider).get(
        f'/post/{secret_post.id}/comment/{reply.id}')

    assert response.status_code == 403
    assert b'PRIVATEBODY' not in response.data


def test_a_private_communitys_thread_is_refused_to_an_anonymous_reader(app,
                                                                       env):
    """The worse half of D1078: no account at all was needed. Measured:
    `PROBE ae4 anon status: 200 | private body visible: True`."""
    client, community, post, mod, author, outsider = env
    secret, secret_post, member = private_community(app)
    reply = a_reply(secret_post, member, body='PRIVATEBODY')

    response = app.test_client().get(
        f'/post/{secret_post.id}/comment/{reply.id}')

    assert response.status_code == 403
    assert b'PRIVATEBODY' not in response.data


def test_a_member_still_reads_their_private_communitys_thread(app, env):
    """The guard has to let the members through, or it has broken the feature
    it was added to protect."""
    client, community, post, mod, author, outsider = env
    secret, secret_post, member = private_community(app)
    reply = a_reply(secret_post, member, body='PRIVATEBODY')

    response = as_user(app, member).get(
        f'/post/{secret_post.id}/comment/{reply.id}')

    assert response.status_code == 200
    assert b'PRIVATEBODY' in response.data


def test_the_ajax_thread_refuses_a_private_community(app, env):
    """D1078's second site. Measured: `PROBE ae2 status: 200 | private body
    visible: True`."""
    client, community, post, mod, author, outsider = env
    secret, secret_post, member = private_community(app)
    reply = a_reply(secret_post, member, body='PRIVATEBODY')
    stranger = as_user(app, outsider)

    response = stranger.post(
        f'/post/{secret_post.id}/comment/{reply.id}/ajax/abc',
        data={'csrf_token': csrf(app, stranger)})

    assert response.status_code == 403
    assert b'PRIVATEBODY' not in response.data


def test_the_reply_options_page_refuses_a_private_community(app, env):
    client, community, post, mod, author, outsider = env
    secret, secret_post, member = private_community(app)
    reply = a_reply(secret_post, member)

    response = as_user(app, outsider).get(
        f'/post/{secret_post.id}/comment/{reply.id}/options_menu')

    assert response.status_code == 403


def test_the_report_form_refuses_a_private_community(app, env):
    """The report page names the post it is about, so a non-member learned a
    private community's post title from it. Measured: `PROBE ae5 status: 200 |
    title visible: True`."""
    client, community, post, mod, author, outsider = env
    secret, secret_post, member = private_community(app)
    reply = a_reply(secret_post, member)

    response = as_user(app, outsider).get(
        f'/post/{secret_post.id}/comment/{reply.id}/report')

    assert response.status_code == 403
    assert b'a post in secret' not in response.data


def test_a_public_communitys_thread_is_not_refused(app, env):
    """The guard reads `post.community.private`, so it must not fire on the
    ordinary case -- a mutation that drops the early return would otherwise
    pass unnoticed."""
    client, community, post, mod, author, outsider = env
    a_reply(post, author, body='a public reply')

    response = app.test_client().get(
        f'/post/{post.id}/comment/{post.replies[0].id}')

    assert response.status_code == 200
    assert b'a public reply' in response.data


# --------------------------------------------------------------------------
# The comment thread
# --------------------------------------------------------------------------


def test_a_comment_from_another_post_is_not_found(app, env):
    """D1077's read side: two ids in one URL with nothing tying them
    together, so the page answered for a reply that is not part of this
    conversation at all."""
    client, community, post, mod, author, outsider = env
    other_community, other_post, other_author = elsewhere(app)
    other_reply = a_reply(other_post, other_author, body='somewhere else')

    response = client.get(f'/post/{post.id}/comment/{other_reply.id}')

    assert response.status_code == 404
    assert b'somewhere else' not in response.data


def test_a_polls_thread_carries_the_poll(app, env):
    """The thread page renders the post above the comment, so a poll post has
    to arrive with its choices and its totals -- and whether the reader has
    already voted."""
    client, community, post, mod, author, outsider = env
    post.type = POST_TYPE_POLL
    db.session.commit()
    make_poll(post)
    # Inserted out of order on purpose: a list that is inserted sorted looks
    # sorted whether or not the query orders it (fact 499).
    make_poll_choice(post, 'no', sort_order=1)
    make_poll_choice(post, 'yes', sort_order=0)
    reply = a_reply(post, author)

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        client.get(f'/post/{post.id}/comment/{reply.id}')

    kwargs = render.call_args.kwargs
    assert [choice.choice_text for choice in kwargs['poll_choices']] == \
        ['yes', 'no']
    assert kwargs['poll_total_votes'] == 0
    assert kwargs['has_voted'] is False


def test_a_poll_post_with_no_poll_row_still_renders(app, env):
    """`post.type` and the `Poll` row are separate, so a post that claims to
    be a poll and has no row must not be an exception on the thread page."""
    client, community, post, mod, author, outsider = env
    post.type = POST_TYPE_POLL
    db.session.commit()
    reply = a_reply(post, author)

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        response = client.get(f'/post/{post.id}/comment/{reply.id}')

    assert response.status_code == 200
    assert render.call_args.kwargs['poll_choices'] == []


def test_an_events_thread_carries_the_event(app, env):
    client, community, post, mod, author, outsider = env
    post.type = POST_TYPE_EVENT
    db.session.add(Event(post_id=post.id, start=utcnow(), end=utcnow(),
                         timezone='UTC'))
    db.session.commit()
    reply = a_reply(post, author)

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        client.get(f'/post/{post.id}/comment/{reply.id}')

    assert render.call_args.kwargs['event'].post_id == post.id


def test_a_nested_comment_carries_its_parent(app, env):
    """`parent_id` is what the page uses to offer the way back up the thread."""
    client, community, post, mod, author, outsider = env
    parent = a_reply(post, author, body='parent')
    child = a_reply(post, author, body='child', parent_id=parent.id)

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        client.get(f'/post/{post.id}/comment/{child.id}')

    assert render.call_args.kwargs['parent_id'] == parent.id


def test_a_deleted_parent_is_not_offered(app, env):
    """A removed comment is not a place to navigate to, so the link up is
    dropped rather than pointing at a 404."""
    client, community, post, mod, author, outsider = env
    parent = a_reply(post, author, body='parent', deleted=True)
    child = a_reply(post, author, body='child', parent_id=parent.id)

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        client.get(f'/post/{post.id}/comment/{child.id}')

    assert render.call_args.kwargs['parent_id'] is None


def test_the_thread_carries_the_communitys_user_flair(app, env):
    client, community, post, mod, author, outsider = env
    db.session.add(UserFlair(user_id=author.id, community_id=community.id,
                             flair='bronze'))
    db.session.commit()
    reply = a_reply(post, author)

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        client.get(f'/post/{post.id}/comment/{reply.id}')

    assert render.call_args.kwargs['user_flair'] == {author.id: 'bronze'}


def test_a_deleted_comment_is_hidden_from_ordinary_readers(app, env):
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author, deleted=True)

    response = as_user(app, outsider).get(
        f'/post/{post.id}/comment/{reply.id}')

    assert response.status_code == 404


def test_a_deleted_comment_is_visible_to_an_admin_with_a_warning(app, env):
    """Staff need the page to see what was removed, and the flash says the
    page is not what an ordinary reader sees."""
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author, deleted=True)
    admin = as_user(app, db.session.get(User, 1))

    with patch('app.post.routes.flash') as flashed:
        response = admin.get(f'/post/{post.id}/comment/{reply.id}')

    assert response.status_code == 200
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'only visible to staff and admins' in messages


@pytest.mark.parametrize('deleted_by_author, expected', [
    (True, 'deleted by the author'),
    (False, 'has been deleted and is only visible'),
])
def test_a_deleted_post_says_who_deleted_it(app, env, deleted_by_author,
                                            expected):
    """Two messages, and which one an admin sees is the difference between an
    author withdrawing a post and a moderator removing it."""
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    post.deleted = True
    post.deleted_by = author.id if deleted_by_author else mod.id
    db.session.commit()
    admin = as_user(app, db.session.get(User, 1))

    with patch('app.post.routes.flash') as flashed:
        admin.get(f'/post/{post.id}/comment/{reply.id}')

    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert expected in messages


def test_a_banned_communitys_thread_is_not_found(app, env):
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    community.banned = True
    db.session.commit()

    response = as_user(app, outsider).get(
        f'/post/{post.id}/comment/{reply.id}')

    assert response.status_code == 404


def test_an_unknown_post_or_comment_is_not_found(app, env):
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author)

    assert client.get(f'/post/9999/comment/{reply.id}').status_code == 404
    assert client.get(f'/post/{post.id}/comment/9999').status_code == 404


def test_the_thread_names_the_moderators(app, env):
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author)

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        client.get(f'/post/{post.id}/comment/{reply.id}')

    assert [user.user_name for user in render.call_args.kwargs['mods']] == \
        ['mod']


def test_private_mods_are_not_named(app, env):
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    community.private_mods = True
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        client.get(f'/post/{post.id}/comment/{reply.id}')

    assert render.call_args.kwargs['mods'] == []


def test_the_ajax_thread_refuses_a_foreign_comment(app, env):
    """D1077's family: the same page, fetched by the front end."""
    client, community, post, mod, author, outsider = env
    other_community, other_post, other_author = elsewhere(app)
    other_reply = a_reply(other_post, other_author, body='somewhere else')

    response = client.post(
        f'/post/{post.id}/comment/{other_reply.id}/ajax/abc',
        data={'csrf_token': csrf(app, client)})

    assert response.status_code == 404


def test_the_ajax_thread_renders_the_branch(app, env):
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author, body='the conversation')

    response = client.post(f'/post/{post.id}/comment/{reply.id}/ajax/abc',
                           data={'csrf_token': csrf(app, client)})

    assert response.status_code == 200
    assert b'the conversation' in response.data


def test_the_ajax_thread_hides_private_mods(app, env):
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    community.private_mods = True
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        client.post(f'/post/{post.id}/comment/{reply.id}/ajax/abc',
                    data={'csrf_token': csrf(app, client)})

    assert render.call_args.kwargs['mods'] == []


def test_the_ajax_thread_carries_the_user_flair(app, env):
    client, community, post, mod, author, outsider = env
    db.session.add(UserFlair(user_id=author.id, community_id=community.id,
                             flair='bronze'))
    db.session.commit()
    reply = a_reply(post, author)

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        client.post(f'/post/{post.id}/comment/{reply.id}/ajax/abc',
                    data={'csrf_token': csrf(app, client)})

    assert render.call_args.kwargs['user_flair'] == {author.id: 'bronze'}


def test_an_anonymous_ajax_thread_has_no_voting_history(app, env):
    """An anonymous reader has no votes to mark and no collapse setting, so
    the branch substitutes empty lists and a fixed threshold."""
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author)

    with patch('app.post.routes.render_template',
               return_value=rendered(app)) as render:
        response = app.test_client().post(
            f'/post/{post.id}/comment/{reply.id}/ajax/abc')

    assert response.status_code == 200
    kwargs = render.call_args.kwargs
    assert kwargs['recently_upvoted_replies'] == []
    assert kwargs['recently_downvoted_replies'] == []
    assert kwargs['reply_collapse_threshold'] == -10


# --------------------------------------------------------------------------
# D1077 -- deleting and restoring a reply
# --------------------------------------------------------------------------


def test_an_author_can_delete_their_own_reply(app, env):
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    token = csrf(app, client)

    with patch('app.post.routes.delete_reply') as deleted:
        response = client.post(
            f'/post/{post.id}/comment/{reply.id}/delete',
            data={'submit': 'Yes', 'csrf_token': token})

    assert response.status_code == 302
    assert deleted.call_args.args[0] == reply.id


def test_a_moderator_removing_a_reply_records_a_reason(app, env):
    """A moderator's removal goes through `mod_remove_reply`, not
    `delete_reply`, and carries the reason into the mod log."""
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    moderator = as_user(app, mod)
    token = csrf(app, moderator)

    with patch('app.post.routes.mod_remove_reply') as removed:
        response = moderator.post(
            f'/post/{post.id}/comment/{reply.id}/delete',
            data={'submit': 'Yes', 'reason': 'off topic',
                  'csrf_token': token})

    assert response.status_code == 302
    assert removed.call_args.args[1] == 'Deleted by mod: off topic'


def test_a_moderator_removing_a_reply_without_a_reason(app, env):
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    moderator = as_user(app, mod)
    token = csrf(app, moderator)

    with patch('app.post.routes.mod_remove_reply') as removed:
        moderator.post(f'/post/{post.id}/comment/{reply.id}/delete',
                       data={'submit': 'Yes', 'csrf_token': token})

    assert removed.call_args.args[1] == 'Deleted by mod'


def test_deleting_a_chain_takes_the_replies_too(app, env):
    """`also_delete_replies` walks `path @> ARRAY[:parent_path]`, which
    matches the reply itself as well as its children."""
    client, community, post, mod, author, outsider = env
    parent = a_reply(post, author, body='parent')
    parent.path = [0, parent.id]
    child = a_reply(post, author, body='child', parent_id=parent.id)
    child.path = [0, parent.id, child.id]
    db.session.commit()
    token = csrf(app, client)

    with patch('app.post.routes.delete_reply') as deleted:
        client.post(f'/post/{post.id}/comment/{parent.id}/delete',
                    data={'submit': 'Yes', 'also_delete_replies': 'y',
                          'csrf_token': token})

    assert sorted(call.args[0] for call in deleted.call_args_list) == \
        sorted([parent.id, child.id])


def test_a_moderator_deleting_a_chain_removes_other_peoples_replies(app, env):
    """The author's own go through `delete_reply`; everyone else's go through
    `mod_remove_reply` with the reason, in the same walk."""
    client, community, post, mod, author, outsider = env
    parent = a_reply(post, mod, body='parent')
    parent.path = [0, parent.id]
    child = a_reply(post, author, body='child', parent_id=parent.id)
    child.path = [0, parent.id, child.id]
    db.session.commit()
    moderator = as_user(app, mod)
    token = csrf(app, moderator)

    with patch('app.post.routes.delete_reply') as deleted:
        with patch('app.post.routes.mod_remove_reply') as removed:
            moderator.post(
                f'/post/{post.id}/comment/{parent.id}/delete',
                data={'submit': 'Yes', 'also_delete_replies': 'y',
                      'reason': 'off topic', 'csrf_token': token})

    assert [call.args[0] for call in deleted.call_args_list] == [parent.id]
    assert removed.call_args.args[0] == child.id
    assert removed.call_args.args[1] == 'Deleted by mod: off topic'


def test_a_moderator_deleting_a_chain_without_a_reason(app, env):
    client, community, post, mod, author, outsider = env
    parent = a_reply(post, mod, body='parent')
    parent.path = [0, parent.id]
    child = a_reply(post, author, body='child', parent_id=parent.id)
    child.path = [0, parent.id, child.id]
    db.session.commit()
    moderator = as_user(app, mod)
    token = csrf(app, moderator)

    with patch('app.post.routes.delete_reply'):
        with patch('app.post.routes.mod_remove_reply') as removed:
            moderator.post(
                f'/post/{post.id}/comment/{parent.id}/delete',
                data={'submit': 'Yes', 'also_delete_replies': 'y',
                      'csrf_token': token})

    assert removed.call_args.args[1] == 'Deleted by mod'


def test_a_stranger_cannot_delete_a_reply(app, env):
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    stranger = as_user(app, outsider)
    token = csrf(app, stranger)

    with patch('app.post.routes.delete_reply') as deleted:
        response = stranger.post(
            f'/post/{post.id}/comment/{reply.id}/delete',
            data={'submit': 'Yes', 'csrf_token': token})

    assert response.status_code == 403
    assert deleted.call_args is None


def test_a_reply_from_another_post_cannot_be_deleted(app, env):
    """D1077's second site. The route authorizes against `post.community` and
    acts on `post_reply`, so without this check the two could disagree -- and
    the mismatch reached `delete_reply`, which raises `Exception: Does not
    have permission`. A 500 rather than a cross-community deletion, and only
    because the helper happened to notice."""
    client, community, post, mod, author, outsider = env
    other_community, other_post, other_author = elsewhere(app)
    other_reply = a_reply(other_post, other_author)
    moderator = as_user(app, mod)
    token = csrf(app, moderator)

    with patch('app.post.routes.delete_reply') as deleted:
        with patch('app.post.routes.mod_remove_reply') as removed:
            response = moderator.post(
                f'/post/{post.id}/comment/{other_reply.id}/delete',
                data={'submit': 'Yes', 'csrf_token': token})

    assert response.status_code == 404
    assert deleted.call_args is None
    assert removed.call_args is None


def test_the_delete_page_asks_first(app, env):
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author)

    with patch('app.post.routes.delete_reply') as deleted:
        response = client.get(f'/post/{post.id}/comment/{reply.id}/delete')

    assert response.status_code == 200
    assert deleted.call_args is None


def test_the_delete_page_relabels_the_chain_box_for_an_ordinary_author(app,
                                                                      env):
    """A moderator's `also_delete_replies` deletes everyone's replies in the
    chain; an author's deletes only their own, so the label has to say so."""
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author)

    response = client.get(f'/post/{post.id}/comment/{reply.id}/delete')

    assert b'Delete all my comments in this chain' in response.data
    # Only the text is observable: bootstrap-flask builds the <label> itself
    # and takes `for` from `field.id`, so the `field_id` handed to `Label()`
    # here reaches nothing (fact 505).
    assert b'for="also_delete_replies"' in response.data


def test_an_author_can_restore_their_own_reply(app, env):
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author, deleted=True, deleted_by=author.id)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/comment/{reply.id}/restore',
                           data={'csrf_token': token})

    assert response.status_code == 302
    db.session.refresh(reply)
    assert reply.deleted is False
    assert reply.deleted_by is None


def test_restoring_a_reply_puts_the_counts_back(app, env):
    """The counters are maintained by hand on both sides of a delete, so a
    restore that does not raise them leaves the post claiming fewer replies
    than it shows."""
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author, deleted=True, deleted_by=author.id)
    post.reply_count = 0
    author.post_reply_count = 0
    db.session.commit()
    token = csrf(app, client)

    client.post(f'/post/{post.id}/comment/{reply.id}/restore',
                data={'csrf_token': token})

    db.session.refresh(post)
    db.session.refresh(author)
    assert post.reply_count == 1
    assert author.post_reply_count == 1


def test_restoring_a_nested_reply_puts_its_parents_child_count_back(app, env):
    """`path` holds every ancestor, and each of them counts this reply among
    its children -- so a restore has to raise all of them, not just the post."""
    client, community, post, mod, author, outsider = env
    parent = a_reply(post, author, body='parent')
    parent.path = [0, parent.id]
    parent.child_count = 0
    child = a_reply(post, author, body='child', parent_id=parent.id,
                    deleted=True, deleted_by=author.id)
    child.path = [0, parent.id, child.id]
    db.session.commit()

    client.post(f'/post/{post.id}/comment/{child.id}/restore',
                data={'csrf_token': csrf(app, client)})

    db.session.refresh(parent)
    assert parent.child_count == 1


def test_restoring_a_top_level_reply_touches_no_parent(app, env):
    """A top-level reply's `path` is `[0]` -- the sentinel and nothing else --
    so there is no ancestor whose `child_count` to raise, and the UPDATE is
    skipped rather than run against an empty tuple."""
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author, deleted=True, deleted_by=author.id)
    reply.path = [0]
    db.session.commit()

    response = client.post(f'/post/{post.id}/comment/{reply.id}/restore',
                           data={'csrf_token': csrf(app, client)})

    assert response.status_code == 302
    db.session.refresh(reply)
    assert reply.deleted is False


def test_a_bots_restored_reply_is_not_counted_on_the_post(app, env):
    """A bot's replies are kept out of `post.reply_count`, so restoring one
    must not raise a count the delete never lowered."""
    client, community, post, mod, author, outsider = env
    author.bot = True
    reply = a_reply(post, author, deleted=True, deleted_by=author.id)
    post.reply_count = 0
    db.session.commit()
    token = csrf(app, client)

    client.post(f'/post/{post.id}/comment/{reply.id}/restore',
                data={'csrf_token': token})

    db.session.refresh(post)
    assert post.reply_count == 0


def test_restoring_in_a_local_community_announces_to_its_remote_members(app,
                                                                        env):
    """A local community announces the Undo to every instance with a member
    in it. `following_instances` is that list, and an instance with no inbox
    is skipped rather than queued."""
    client, community, post, mod, author, outsider = env
    remote = make_instance('remote.example', software='lemmy')
    remote.inbox = 'https://remote.example/inbox'
    silent = make_instance('silent.example', software='lemmy')
    silent.inbox = None
    db.session.commit()
    subscriber = make_user(remote, 'subscriber')
    quiet = make_user(silent, 'quiet')
    db.session.commit()
    make_community_member(subscriber, community)
    make_community_member(quiet, community)
    reply = a_reply(post, author, deleted=True, deleted_by=author.id)

    with patch('app.post.routes.send_to_remote_instance') as announced:
        client.post(f'/post/{post.id}/comment/{reply.id}/restore',
                    data={'csrf_token': csrf(app, client)})

    assert [call.args[0] for call in announced.call_args_list] == [remote.id]


def test_a_reply_in_another_community_cannot_be_restored(app, env):
    """D1077. A moderator of ANY community could restore a reply in another
    one -- undoing that community's moderators' removal -- by pairing their
    own post id with its comment id. Every authorization test in the route is
    made against `post.community`. Measured: a moderator of 'mine' restored a
    deleted reply in 'theirs'."""
    client, community, post, mod, author, outsider = env
    other_community, other_post, other_author = elsewhere(app)
    other_reply = a_reply(other_post, other_author, deleted=True,
                          deleted_by=other_author.id)
    moderator = as_user(app, mod)
    token = csrf(app, moderator)

    response = moderator.post(
        f'/post/{post.id}/comment/{other_reply.id}/restore',
        data={'csrf_token': token})

    assert response.status_code == 404
    db.session.refresh(other_reply)
    assert other_reply.deleted is True


def test_a_moderator_can_restore_a_reply_in_their_own_community(app, env):
    """The other end of D1077: the guard must not cost a moderator the power
    the route exists to give them."""
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author, deleted=True, deleted_by=mod.id)
    moderator = as_user(app, mod)

    moderator.post(f'/post/{post.id}/comment/{reply.id}/restore',
                   data={'csrf_token': csrf(app, moderator)})

    db.session.refresh(reply)
    assert reply.deleted is False


def remote_community(app, name='faraway'):
    """A community this instance does not host, so the restore route takes the
    `send_post_request` branch rather than the announce loop."""
    community = make_community(name, host='remote.example')
    community.ap_id = f'{name}@remote.example'
    community.ap_inbox_url = f'https://remote.example/c/{name}/inbox'
    db.session.commit()
    return community


def test_a_moderator_undoing_a_mod_removal_says_so_to_the_remote_community(
        app, env):
    """`was_mod_deletion` is read from `deleted_by` -- a removal by anyone
    other than the author -- and it puts `summary: "Deleted by mod"` on the
    Undo. Without that the remote community cannot tell an author's change of
    mind from a moderation decision being reversed."""
    client, community, post, mod, author, outsider = env
    faraway = remote_community(app)
    make_community_member(mod, faraway, is_moderator=True)
    remote_post = make_post(faraway, author, 'https://remote.example/p/1',
                            title='a remote post')
    db.session.commit()
    reply = a_reply(remote_post, author, deleted=True, deleted_by=mod.id)
    moderator = as_user(app, mod)

    with patch('app.post.routes.send_post_request') as sent:
        moderator.post(
            f'/post/{remote_post.id}/comment/{reply.id}/restore',
            data={'csrf_token': csrf(app, moderator)})

    assert sent.call_args.args[1]['object']['summary'] == 'Deleted by mod'


def test_an_authors_own_undo_carries_no_moderation_summary(app, env):
    """The other side of `was_mod_deletion`: an author restoring what they
    deleted themselves is not reversing a moderation decision."""
    client, community, post, mod, author, outsider = env
    faraway = remote_community(app)
    remote_post = make_post(faraway, author, 'https://remote.example/p/1',
                            title='a remote post')
    db.session.commit()
    reply = a_reply(remote_post, author, deleted=True, deleted_by=author.id)

    with patch('app.post.routes.send_post_request') as sent:
        client.post(f'/post/{remote_post.id}/comment/{reply.id}/restore',
                    data={'csrf_token': csrf(app, client)})

    assert 'summary' not in sent.call_args.args[1]['object']


def test_a_stranger_cannot_restore_a_reply(app, env):
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author, deleted=True, deleted_by=mod.id)
    stranger = as_user(app, outsider)

    stranger.post(f'/post/{post.id}/comment/{reply.id}/restore',
                  data={'csrf_token': csrf(app, stranger)})

    db.session.refresh(reply)
    assert reply.deleted is True


def test_restoring_is_not_a_get(app, env):
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author, deleted=True, deleted_by=author.id)

    response = client.get(f'/post/{post.id}/comment/{reply.id}/restore')

    assert response.status_code == 405
    db.session.refresh(reply)
    assert reply.deleted is True


# --------------------------------------------------------------------------
# The no-JavaScript reply form
# --------------------------------------------------------------------------


def test_the_reply_form_renders(app, env):
    """`add_reply` is the form served when JavaScript is off, so it carries
    the comment being replied to and defaults to notifying its author."""
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author, body='the parent')

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        response = client.get(f'/post/{post.id}/comment/{reply.id}/reply')

    assert response.status_code == 200
    kwargs = render.call_args.kwargs
    assert kwargs['comment'].id == reply.id
    assert kwargs['form'].notify_author.data is True


def test_the_reply_form_refuses_a_parent_from_another_post(app, env):
    """D1077 on the write side: without this the new reply would be attached
    to a parent in a different post -- and a different community -- from the
    one whose permissions were just checked."""
    client, community, post, mod, author, outsider = env
    other_community, other_post, other_author = elsewhere(app)
    other_reply = a_reply(other_post, other_author)

    response = client.get(
        f'/post/{post.id}/comment/{other_reply.id}/reply')

    assert response.status_code == 404


def test_a_banned_account_cannot_reach_the_reply_form(app, env):
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    author.banned = True
    db.session.commit()

    response = client.get(f'/post/{post.id}/comment/{reply.id}/reply')

    assert response.status_code == 302
    assert '/post/' not in response.headers['Location']


def test_an_account_banned_from_commenting_cannot_reach_the_reply_form(app,
                                                                      env):
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    author.ban_comments = True
    db.session.commit()

    response = client.get(f'/post/{post.id}/comment/{reply.id}/reply')

    assert response.status_code == 302


def test_the_reply_form_is_closed_when_comments_are_disabled(app, env):
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    post.comments_enabled = False
    db.session.commit()

    with patch('app.post.routes.flash') as flashed:
        response = client.get(f'/post/{post.id}/comment/{reply.id}/reply')

    assert response.status_code == 302
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'Comments have been disabled' in messages


def test_the_reply_form_hides_private_mods(app, env):
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    community.private_mods = True
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/post/{post.id}/comment/{reply.id}/reply')

    assert render.call_args.kwargs['mods'] == []


def test_you_cannot_reply_to_somebody_who_blocked_you(app, env):
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, mod)
    from app.models import UserBlock
    db.session.add(UserBlock(blocker_id=mod.id, blocked_id=author.id))
    db.session.commit()

    with patch('app.post.routes.flash') as flashed:
        response = client.get(f'/post/{post.id}/comment/{reply.id}/reply')

    assert response.status_code == 302
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'You cannot reply to' in messages


def test_posting_a_reply(app, env):
    client, community, post, mod, author, outsider = env
    parent = a_reply(post, author, body='the parent')
    english = Language.query.filter_by(code='en').one()
    token = csrf(app, client)
    made = a_reply(post, author, body='the new one', parent_id=parent.id)
    made.depth = 1

    with patch('app.post.routes.make_reply', return_value=made) as make:
        response = client.post(
            f'/post/{post.id}/comment/{parent.id}/reply',
            data={'body': 'the new one', 'notify_author': 'y',
                  'language_id': str(english.id), 'submit': 'Comment',
                  'csrf_token': token})

    assert response.status_code == 302
    assert make.call_args.args[2] == parent.id


def test_a_deep_reply_redirects_to_its_own_thread_page(app, env):
    """Past `THREAD_CUTOFF_DEPTH` the post page no longer renders the comment,
    so the redirect goes to the continue-discussion page of its parent."""
    client, community, post, mod, author, outsider = env
    parent = a_reply(post, author, body='the parent')
    english = Language.query.filter_by(code='en').one()
    token = csrf(app, client)
    made = a_reply(post, author, body='deep', parent_id=parent.id)
    made.depth = 99

    with patch('app.post.routes.make_reply', return_value=made):
        response = client.post(
            f'/post/{post.id}/comment/{parent.id}/reply',
            data={'body': 'deep', 'notify_author': 'y',
                  'language_id': str(english.id), 'submit': 'Comment',
                  'csrf_token': token})

    assert response.headers['Location'] == \
        f'/post/{post.id}/comment/{parent.id}'


def test_a_refused_reply_says_why(app, env):
    """`make_reply` raises for a refusal -- a banned account, a closed
    community -- and the reason is put in front of the person rather than
    becoming a 500."""
    client, community, post, mod, author, outsider = env
    parent = a_reply(post, author, body='the parent')
    parent.depth = 1
    english = Language.query.filter_by(code='en').one()
    db.session.commit()
    token = csrf(app, client)

    with patch('app.post.routes.make_reply',
               side_effect=Exception('you are banned from this community')):
        with patch('app.post.routes.flash') as flashed:
            response = client.post(
                f'/post/{post.id}/comment/{parent.id}/reply',
                data={'body': 'rejected', 'notify_author': 'y',
                      'language_id': str(english.id), 'submit': 'Comment',
                      'csrf_token': token})

    assert response.status_code == 302
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'you are banned from this community' in messages


def test_a_refused_deep_reply_goes_back_to_the_thread_page(app, env):
    client, community, post, mod, author, outsider = env
    grandparent = a_reply(post, author, body='grandparent')
    parent = a_reply(post, author, body='the parent',
                     parent_id=grandparent.id)
    parent.depth = 99
    english = Language.query.filter_by(code='en').one()
    db.session.commit()
    token = csrf(app, client)

    with patch('app.post.routes.make_reply', side_effect=Exception('nope')):
        response = client.post(
            f'/post/{post.id}/comment/{parent.id}/reply',
            data={'body': 'rejected', 'notify_author': 'y',
                  'language_id': str(english.id), 'submit': 'Comment',
                  'csrf_token': token})

    assert response.headers['Location'] == \
        f'/post/{post.id}/comment/{grandparent.id}'


# --------------------------------------------------------------------------
# The options page, the report form and the edit form
# --------------------------------------------------------------------------


def test_the_reply_options_page(app, env):
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author)

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        response = client.get(
            f'/post/{post.id}/comment/{reply.id}/options_menu')

    assert response.status_code == 200
    assert render.call_args.kwargs['post_reply'].id == reply.id


def test_the_options_page_knows_about_a_bookmark(app, env):
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    db.session.add(PostReplyBookmark(user_id=author.id,
                                     post_reply_id=reply.id))
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/post/{post.id}/comment/{reply.id}/options_menu')

    assert render.call_args.kwargs['existing_bookmark'] is not None


def test_somebody_elses_bookmark_is_not_shown(app, env):
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    db.session.add(PostReplyBookmark(user_id=outsider.id,
                                     post_reply_id=reply.id))
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/post/{post.id}/comment/{reply.id}/options_menu')

    assert not render.call_args.kwargs['existing_bookmark']


def test_the_options_page_refuses_a_foreign_comment(app, env):
    client, community, post, mod, author, outsider = env
    other_community, other_post, other_author = elsewhere(app)
    other_reply = a_reply(other_post, other_author)

    response = client.get(
        f'/post/{post.id}/comment/{other_reply.id}/options_menu')

    assert response.status_code == 404


def test_a_deleted_reply_has_no_options_for_an_anonymous_visitor(app, env):
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author, deleted=True)

    response = app.test_client().get(
        f'/post/{post.id}/comment/{reply.id}/options_menu')

    assert response.status_code == 404


def test_a_deleted_reply_still_has_options_for_a_logged_in_reader(app, env):
    """Only the anonymous case is refused -- the author and the moderators
    reach the menu because that is where the restore link lives."""
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author, deleted=True)

    response = client.get(f'/post/{post.id}/comment/{reply.id}/options_menu')

    assert response.status_code == 200


def test_reporting_a_reply(app, env):
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    reporter = as_user(app, outsider)
    token = csrf(app, reporter)

    response = reporter.post(
        f'/post/{post.id}/comment/{reply.id}/report',
        data={'reasons': ['1'], 'description': 'spam', 'submit': 'Report',
              'csrf_token': token})

    assert response.status_code == 302
    report = Report.query.one()
    assert report.suspect_post_reply_id == reply.id
    assert report.reporter_id == outsider.id


def test_a_reply_whose_reports_are_ignored_says_so(app, env):
    """`reports == -1` is how a moderator says the reports on this comment
    have been assessed, so a further one is dropped rather than filed."""
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author, reports=-1)
    reporter = as_user(app, outsider)
    token = csrf(app, reporter)

    with patch('app.post.routes.flash') as flashed:
        response = reporter.post(
            f'/post/{post.id}/comment/{reply.id}/report',
            data={'reasons': ['1'], 'description': 'spam',
                  'submit': 'Report', 'csrf_token': token})

    assert response.status_code == 302
    assert Report.query.count() == 0
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'already been reported' in messages


def test_the_report_page_warns_when_the_reports_are_already_assessed(app,
                                                                    env):
    """`reports == -1` is set when a moderator decides to ignore further
    reports on a comment, and the form says so before it is filled in -- not
    only after it is sent."""
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author, reports=-1)

    with patch('app.post.routes.flash') as flashed:
        client.get(f'/post/{post.id}/comment/{reply.id}/report')

    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'Moderators have already assessed reports' in messages


def test_the_report_page_does_not_warn_about_an_ordinary_comment(app, env):
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author)

    with patch('app.post.routes.flash') as flashed:
        client.get(f'/post/{post.id}/comment/{reply.id}/report')

    assert flashed.call_args_list == []


def test_the_report_form_defaults_to_reporting_remotely(app, env):
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author)

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        response = client.get(f'/post/{post.id}/comment/{reply.id}/report')

    assert response.status_code == 200
    assert render.call_args.kwargs['form'].report_remote.data is True


def test_the_report_form_refuses_a_foreign_comment(app, env):
    """A report is filed against the reply and routed to `post.community`'s
    moderators, so a mismatch sends a community a report about content that is
    not theirs."""
    client, community, post, mod, author, outsider = env
    other_community, other_post, other_author = elsewhere(app)
    other_reply = a_reply(other_post, other_author)

    response = client.get(f'/post/{post.id}/comment/{other_reply.id}/report')

    assert response.status_code == 404


def test_an_author_can_edit_their_own_reply(app, env):
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author, body='before')

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        response = client.get(f'/post/{post.id}/comment/{reply.id}/edit')

    assert response.status_code == 200
    assert render.call_args.kwargs['form'].body.data == 'before'


def test_nobody_else_can_edit_a_reply(app, env):
    """`post_reply.user_id == current_user.id` -- editing is the author's
    alone, even for a moderator, because the text is attributed to them."""
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    moderator = as_user(app, mod)

    response = moderator.get(f'/post/{post.id}/comment/{reply.id}/edit')

    assert response.status_code == 401


def test_the_distinguished_box_is_for_moderators(app, env):
    """A distinguished comment is marked as speaking for the moderators, so
    the box is disabled for everyone else."""
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author)

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/post/{post.id}/comment/{reply.id}/edit')

    assert render.call_args.kwargs['form'].distinguished.render_kw == {
        'disabled': True}


def test_a_moderator_editing_their_own_reply_may_distinguish_it(app, env):
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, mod)
    moderator = as_user(app, mod)

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        moderator.get(f'/post/{post.id}/comment/{reply.id}/edit')

    assert render.call_args.kwargs['form'].distinguished.render_kw is None


def test_the_edit_form_carries_the_parent_comment(app, env):
    """The page shows what is being replied to, so a nested reply has to
    arrive with its parent."""
    client, community, post, mod, author, outsider = env
    parent = a_reply(post, author, body='parent')
    child = a_reply(post, author, body='child', parent_id=parent.id)

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/post/{post.id}/comment/{child.id}/edit')

    assert render.call_args.kwargs['comment'].id == parent.id


def test_a_top_level_reply_has_no_parent_comment(app, env):
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author)

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/post/{post.id}/comment/{reply.id}/edit')

    assert render.call_args.kwargs['comment'] is None


def test_the_edit_form_refuses_a_foreign_comment(app, env):
    client, community, post, mod, author, outsider = env
    other_community, other_post, other_author = elsewhere(app)
    other_reply = a_reply(other_post, other_author)

    response = client.get(f'/post/{post.id}/comment/{other_reply.id}/edit')

    assert response.status_code == 404


def test_saving_an_edit(app, env):
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author, body='before')
    token = csrf(app, client)

    english = Language.query.filter_by(code='en').one()

    with patch('app.post.routes.edit_reply') as edited:
        response = client.post(
            f'/post/{post.id}/comment/{reply.id}/edit',
            data={'body': 'after', 'notify_author': 'y',
                  'language_id': str(english.id), 'submit': 'Save',
                  'csrf_token': token})

    assert response.status_code == 302
    assert edited.call_args.args[1].id == reply.id
