"""Editing a post, setting its flair, embedding it, and the inline reply.

Sub-project 82, slice C. Six defects, all measured:

* the embed page and the embed-code page made **none** of the post page's
  access checks -- a private community's post and an unpublished one were
  both readable through them (D1089);
* `GET /post/<id>/set_flair` with an `HX-Request` header **cleared the post's
  flair and its nsfw / nsfl / ai_generated marks**, because the htmx branch
  writes from `request.form` and a GET carries none (D1090);
* `HX-Current-Url` was read straight into an `in` test, so a request without
  the header was a 500 (D1091);
* `add_reply_inline` attached a new reply to a comment in ANOTHER post -- and
  another community -- from the one whose permissions it had just checked
  (D1092);
* `int(request.form.get('language_id'))` was a 500 for a form field that is
  whatever the caller sends (D1093);
* the validation-error fragment was not an f-string, so its id was literal and
  htmx had nothing to swap (D1094).
"""
from unittest.mock import patch

import pytest

from app import db
from app.constants import (POST_STATUS_PUBLISHED, POST_STATUS_SCHEDULED,
                           POST_TYPE_ARTICLE, POST_TYPE_EVENT, POST_TYPE_IMAGE,
                           POST_TYPE_LINK, POST_TYPE_POLL, POST_TYPE_VIDEO)
from app.models import (CommunityFlair, Event, File, Instance, Language, Poll,
                        PollChoice, Post, PostReply, PostReplyValidationError,
                        Site, Topic, User)
from app.utils import utcnow
from tests.factories import (make_community, make_community_ban,
                             make_community_flair, make_community_member,
                             make_instance, make_poll, make_poll_choice,
                             make_post, make_post_reply, make_user)

pytestmark = pytest.mark.usefixtures('site')


def instance(domain='test.piefed.local', software='piefed'):
    """Fact 394."""
    existing = Instance.query.filter_by(domain=domain).first()
    return existing if existing is not None else make_instance(domain,
                                                               software=software)


def csrf(app, client):
    """Fact 355. `login_required` validates the token on every POST even with
    `WTF_CSRF_ENABLED = False`, answering 400 without one (fact 515)."""
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
    mod = make_user(local, 'mod', local=True)
    author = make_user(local, 'author', local=True, with_keys=True)
    author.verified = True
    outsider = make_user(local, 'outsider', local=True)
    community = make_community('general')
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.add(Language(code='en', name='English'))
    db.session.commit()
    make_community_member(mod, community, is_moderator=True)
    make_community_member(author, community)
    post = make_post(community, author, 'https://test.piefed.local/p/1',
                     title='THETITLE')
    post.body = 'the body'
    post.body_html = '<p>THEBODY</p>'
    db.session.commit()
    return app.test_client(), community, post, mod, author, outsider


def english():
    return Language.query.filter_by(code='en').one()


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
    author = make_user(instance(), f'{name}_author', local=True)
    db.session.commit()
    make_community_member(author, community)
    post = make_post(community, author, f'https://test.piefed.local/p/{name}',
                     title=f'a post in {name}')
    post.body_html = f'<p>body of {name}</p>'
    db.session.commit()
    return community, post, author


# --------------------------------------------------------------------------
# D1089 -- the embed pages
# --------------------------------------------------------------------------


def test_the_embed_shows_the_post(app, env):
    anon, community, post, mod, author, outsider = env

    response = anon.get(f'/post/{post.id}/embed')

    assert response.status_code == 200
    assert b'THEBODY' in response.data


def test_the_embed_refuses_a_private_community(app, env):
    """D1089. The embed IS the post, in a frame, and it made none of the post
    page's access checks. Measured: `PROBE ai1 embed anon: 200 | body: True |
    title: True`."""
    anon, community, post, mod, author, outsider = env
    secret, secret_post, member = elsewhere(app, 'secret', private=True)

    response = anon.get(f'/post/{secret_post.id}/embed')

    assert response.status_code == 403
    assert b'body of secret' not in response.data


def test_a_member_still_embeds_their_private_communitys_post(app, env):
    anon, community, post, mod, author, outsider = env
    secret, secret_post, member = elsewhere(app, 'secret', private=True)

    response = as_user(app, member).get(f'/post/{secret_post.id}/embed')

    assert response.status_code == 200
    assert b'body of secret' in response.data


def test_the_embed_refuses_an_unpublished_post(app, env):
    """D1089's other half: slice B guarded `show_post` and this page showed
    the same content. Measured: `PROBE ai3 embed scheduled: 200 | body:
    True`."""
    anon, community, post, mod, author, outsider = env
    post.status = POST_STATUS_SCHEDULED
    post.scheduled_for = utcnow()
    db.session.commit()

    response = anon.get(f'/post/{post.id}/embed')

    assert response.status_code == 404
    assert b'THEBODY' not in response.data


def test_the_author_still_embeds_their_own_unpublished_post(app, env):
    anon, community, post, mod, author, outsider = env
    post.status = POST_STATUS_SCHEDULED
    post.scheduled_for = utcnow()
    db.session.commit()

    response = as_user(app, author).get(f'/post/{post.id}/embed')

    assert response.status_code == 200


def test_the_embed_hides_a_deleted_post(app, env):
    anon, community, post, mod, author, outsider = env
    post.deleted = True
    db.session.commit()

    assert anon.get(f'/post/{post.id}/embed').status_code == 404


def test_an_admin_sees_a_deleted_posts_embed(app, env):
    anon, community, post, mod, author, outsider = env
    post.deleted = True
    post.deleted_by = mod.id
    db.session.commit()

    with patch('app.post.routes.flash') as flashed:
        response = as_user(app, db.session.get(User, 1)).get(
            f'/post/{post.id}/embed')

    assert response.status_code == 200
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'only visible to staff and admins' in messages


def test_an_admin_sees_who_deleted_it(app, env):
    anon, community, post, mod, author, outsider = env
    post.deleted = True
    post.deleted_by = post.user_id
    db.session.commit()

    with patch('app.post.routes.flash') as flashed:
        as_user(app, db.session.get(User, 1)).get(f'/post/{post.id}/embed')

    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'deleted by the author' in messages


def test_the_embed_flags_a_mea_culpa(app, env):
    anon, community, post, mod, author, outsider = env
    post.mea_culpa = True
    db.session.commit()

    with patch('app.post.routes.flash') as flashed:
        anon.get(f'/post/{post.id}/embed')

    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'made a mistake in this post' in messages


def test_an_unchanged_embed_answers_304(app, env):
    anon, community, post, mod, author, outsider = env
    etag = f'{post.id}_{hash(post.last_active)}'

    response = anon.get(f'/post/{post.id}/embed',
                        headers={'If-None-Match': etag})

    assert response.status_code == 304


def test_an_unknown_post_has_no_embed(app, env):
    anon, community, post, mod, author, outsider = env

    assert anon.get('/post/999999/embed').status_code == 404


def test_the_embed_code_page(app, env):
    anon, community, post, mod, author, outsider = env

    response = anon.get(f'/post/{post.id}/embed_code')

    assert response.status_code == 200
    assert b'THETITLE' in response.data


def test_the_embed_code_page_refuses_a_private_community(app, env):
    """Measured: `PROBE ai2 embed_code anon: 200 | title: True`."""
    anon, community, post, mod, author, outsider = env
    secret, secret_post, member = elsewhere(app, 'secret', private=True)

    response = anon.get(f'/post/{secret_post.id}/embed_code')

    assert response.status_code == 403
    assert b'a post in secret' not in response.data


def test_the_embed_code_page_refuses_an_unpublished_post(app, env):
    anon, community, post, mod, author, outsider = env
    post.status = POST_STATUS_SCHEDULED
    post.scheduled_for = utcnow()
    db.session.commit()

    assert anon.get(f'/post/{post.id}/embed_code').status_code == 404


def test_the_embed_code_breadcrumbs_without_a_topic(app, env):
    anon, community, post, mod, author, outsider = env

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        anon.get(f'/post/{post.id}/embed_code')

    crumbs = render.call_args.kwargs['breadcrumbs']
    assert [crumb.text for crumb in crumbs] == \
        ['Home', 'Communities', 'general', 'THETITLE']


def test_the_embed_code_breadcrumbs_climb_a_topic_tree(app, env):
    anon, community, post, mod, author, outsider = env
    root = Topic(name='Root', machine_name='root', num_communities=1)
    db.session.add(root)
    db.session.commit()
    leaf = Topic(name='Leaf', machine_name='leaf', parent_id=root.id,
                 num_communities=1)
    db.session.add(leaf)
    db.session.commit()
    community.topic_id = leaf.id
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        anon.get(f'/post/{post.id}/embed_code')

    crumbs = render.call_args.kwargs['breadcrumbs']
    assert [crumb.text for crumb in crumbs] == \
        ['Home', 'Topics', 'Root', 'Leaf', 'THETITLE']


def test_an_unknown_post_has_no_embed_code(app, env):
    anon, community, post, mod, author, outsider = env

    assert anon.get('/post/999999/embed_code').status_code == 404


# --------------------------------------------------------------------------
# D1090, D1091 -- flair
# --------------------------------------------------------------------------


def test_a_get_no_longer_clears_the_flair(app, env):
    """D1090. The htmx branch is entered on the `HX-Request` header alone and
    writes `post.flair`, `nsfw`, `nsfl` and `ai_generated` from
    `request.form` -- which a GET does not carry, so all four were reset.
    Measured: `PROBE aj1 GET set_flair status=200 | nsfw now=False | flair
    now=[]`, against a post that went in marked nsfw and flaired."""
    anon, community, post, mod, author, outsider = env
    flair = make_community_flair(community, 'spoiler')
    post.nsfw = True
    db.session.commit()
    post.flair.append(flair)
    db.session.commit()

    response = as_user(app, author).get(
        f'/post/{post.id}/set_flair',
        headers={'HX-Request': 'true',
                 'HX-Current-Url': 'https://test.piefed.local/'})

    assert response.status_code == 200
    db.session.refresh(post)
    assert post.nsfw is True
    assert [f.flair for f in post.flair] == ['spoiler']


def test_the_htmx_post_sets_the_flair(app, env):
    anon, community, post, mod, author, outsider = env
    flair = make_community_flair(community, 'spoiler')
    client = as_user(app, author)
    token = csrf(app, client)

    with patch('app.post.routes.task_selector') as task:
        response = client.post(
            f'/post/{post.id}/set_flair',
            data={f'flair-{flair.id}': 'on', 'nsfw': '1',
                  'csrf_token': token},
            headers={'HX-Request': 'true',
                     'HX-Current-Url': 'https://test.piefed.local/'})

    assert response.status_code == 200
    db.session.refresh(post)
    assert [f.flair for f in post.flair] == ['spoiler']
    assert post.nsfw is True
    assert task.call_args.args[0] == 'edit_post'


def test_flair_from_another_community_is_ignored(app, env):
    """A flair id is a number in a form field, so it can name a flair that
    belongs to somebody else's community."""
    anon, community, post, mod, author, outsider = env
    other, other_post, other_author = elsewhere(app)
    theirs = make_community_flair(other, 'theirs')
    client = as_user(app, author)
    token = csrf(app, client)

    with patch('app.post.routes.task_selector'):
        client.post(f'/post/{post.id}/set_flair',
                    data={f'flair-{theirs.id}': 'on', 'csrf_token': token},
                    headers={'HX-Request': 'true',
                             'HX-Current-Url': 'https://test.piefed.local/'})

    db.session.refresh(post)
    assert post.flair == []


def test_an_unpublished_post_is_not_federated_on_a_flair_change(app, env):
    """`edit_post` tells the other instances what changed; a post they have
    never seen has nothing to update."""
    anon, community, post, mod, author, outsider = env
    post.status = POST_STATUS_SCHEDULED
    post.scheduled_for = utcnow()
    db.session.commit()
    client = as_user(app, author)
    token = csrf(app, client)

    with patch('app.post.routes.task_selector') as task:
        client.post(f'/post/{post.id}/set_flair',
                    data={'csrf_token': token},
                    headers={'HX-Request': 'true',
                             'HX-Current-Url': 'https://test.piefed.local/'})

    assert task.call_args is None


def test_setting_flair_from_a_post_page_redirects_back_to_it(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    token = csrf(app, client)

    with patch('app.post.routes.task_selector'):
        response = client.post(
            f'/post/{post.id}/set_flair', data={'csrf_token': token},
            headers={'HX-Request': 'true',
                     'HX-Current-Url': f'https://test.piefed.local/post/{post.id}'})

    assert response.headers['HX-Redirect'] == \
        f'https://test.piefed.local/post/{post.id}'


def test_setting_flair_from_a_listing_returns_the_teaser(app, env):
    """From a listing there is no page to reload, so the changed teaser is
    swapped in place."""
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    token = csrf(app, client)

    with patch('app.post.routes.task_selector'):
        with patch('app.post.routes.render_template',
                   return_value='rendered') as render:
            response = client.post(
                f'/post/{post.id}/set_flair', data={'csrf_token': token},
                headers={'HX-Request': 'true',
                         'HX-Current-Url': 'https://test.piefed.local/c/general'})

    assert response.status_code == 200
    assert render.call_args.kwargs['show_post_community'] is False


def test_setting_flair_from_the_home_page_keeps_the_community_name(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    token = csrf(app, client)

    with patch('app.post.routes.task_selector'):
        with patch('app.post.routes.render_template',
                   return_value='rendered') as render:
            client.post(f'/post/{post.id}/set_flair',
                        data={'csrf_token': token},
                        headers={'HX-Request': 'true',
                                 'HX-Current-Url': 'https://test.piefed.local/'})

    assert render.call_args.kwargs['show_post_community'] is True


def test_the_flair_form_survives_a_missing_hx_current_url(app, env):
    """D1091. Measured: `TypeError: argument of type 'NoneType' is not
    iterable` -- a 500 for a request without the header."""
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    token = csrf(app, client)

    with patch('app.post.routes.task_selector'):
        response = client.post(f'/post/{post.id}/set_flair',
                               data={'csrf_token': token},
                               headers={'HX-Request': 'true'})

    assert response.status_code == 200


def test_the_flair_list_survives_a_missing_hx_current_url(app, env):
    """D1091's second site, `app/post/routes.py:1736`."""
    anon, community, post, mod, author, outsider = env
    make_community_flair(community, 'spoiler')

    response = as_user(app, author).get(f'/post/{post.id}/get_flair')

    assert response.status_code == 200


def test_the_flair_list_knows_where_it_was_called_from(app, env):
    anon, community, post, mod, author, outsider = env
    make_community_flair(community, 'spoiler')

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        as_user(app, author).get(
            f'/post/{post.id}/get_flair',
            headers={'HX-Current-Url': f'https://test.piefed.local/post/{post.id}'})

    assert render.call_args.kwargs['post_preview'] is False


def test_a_community_with_no_flair_offers_none(app, env):
    anon, community, post, mod, author, outsider = env

    response = as_user(app, author).get(f'/post/{post.id}/get_flair')

    assert response.status_code == 200
    assert response.data == b''


def test_only_the_author_and_the_moderators_may_set_flair(app, env):
    anon, community, post, mod, author, outsider = env

    assert as_user(app, outsider).get(
        f'/post/{post.id}/set_flair').status_code == 401
    assert as_user(app, outsider).get(
        f'/post/{post.id}/get_flair').status_code == 401


def test_a_moderator_may_set_flair(app, env):
    anon, community, post, mod, author, outsider = env

    with patch('app.post.routes.render_template',
               return_value='rendered'):
        response = as_user(app, mod).get(f'/post/{post.id}/set_flair')

    assert response.status_code == 200


def test_the_flair_form_is_filled_in_from_the_post(app, env):
    anon, community, post, mod, author, outsider = env
    flair = make_community_flair(community, 'spoiler')
    post.nsfw = True
    post.ai_generated = True
    db.session.commit()
    post.flair.append(flair)
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        as_user(app, author).get(f'/post/{post.id}/set_flair')

    form = render.call_args.kwargs['form']
    assert form.flair.data == [flair.id]
    assert form.nsfw.data is True
    assert form.ai_generated.data is True


def test_the_plain_form_saves_the_flair(app, env):
    """The no-JavaScript path: a real form post, not an htmx fragment."""
    anon, community, post, mod, author, outsider = env
    flair = make_community_flair(community, 'spoiler')
    client = as_user(app, author)
    token = csrf(app, client)

    with patch('app.post.routes.task_selector') as task:
        response = client.post(
            f'/post/{post.id}/set_flair',
            data={'flair': [str(flair.id)], 'nsfw': 'y', 'submit': 'Save',
                  'csrf_token': token})

    assert response.status_code == 302
    db.session.refresh(post)
    assert [f.flair for f in post.flair] == ['spoiler']
    assert post.nsfw is True
    assert task.call_args.args[0] == 'edit_post'


def test_an_unknown_post_has_no_flair_form(app, env):
    anon, community, post, mod, author, outsider = env

    assert as_user(app, author).get(
        '/post/999999/set_flair').status_code == 404


# --------------------------------------------------------------------------
# D1092, D1093, D1094 -- the inline reply
# --------------------------------------------------------------------------


def test_the_inline_reply_form_renders(app, env):
    anon, community, post, mod, author, outsider = env
    parent = a_reply(post, author, body='the parent')

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        response = as_user(app, author).get(
            f'/post/{post.id}/comment/{parent.id}/reply_inline/abc')

    assert response.status_code == 200
    assert render.call_args.kwargs['in_reply_to'].id == parent.id
    assert render.call_args.kwargs['nonce'] == 'abc'


def test_the_inline_form_refuses_a_parent_from_another_post(app, env):
    """D1092. Every permission above is tested against `post.community`, and
    the reply is then attached to `in_reply_to`."""
    anon, community, post, mod, author, outsider = env
    other, other_post, other_author = elsewhere(app)
    their_reply = a_reply(other_post, other_author)

    response = as_user(app, author).get(
        f'/post/{post.id}/comment/{their_reply.id}/reply_inline/abc')

    assert response.status_code == 404


def test_a_reply_cannot_be_grafted_onto_another_communitys_thread(app, env):
    """D1092 as it was measured: `PROBE ak1 status=200 | replies made=1 |
    child post_id=1 parent post_id=2` -- a reply created under a PUBLIC post
    whose parent is a comment in a PRIVATE community's post. D1077's family,
    ninth site, and the only one that writes across the boundary rather than
    reading across it."""
    anon, community, post, mod, author, outsider = env
    secret, secret_post, member = elsewhere(app, 'secret', private=True)
    their_reply = a_reply(secret_post, member)
    client = as_user(app, author)
    token = csrf(app, client)
    before = PostReply.query.count()

    response = client.post(
        f'/post/{post.id}/comment/{their_reply.id}/reply_inline/abc',
        data={'body': 'crossed', 'language_id': str(english().id),
              'csrf_token': token})

    assert response.status_code == 404
    assert PostReply.query.count() == before


def test_posting_an_inline_reply(app, env):
    anon, community, post, mod, author, outsider = env
    parent = a_reply(post, author, body='the parent')
    client = as_user(app, author)
    token = csrf(app, client)

    with patch('app.post.routes.task_selector') as task:
        response = client.post(
            f'/post/{post.id}/comment/{parent.id}/reply_inline/abc',
            data={'body': 'the answer', 'language_id': str(english().id),
                  'csrf_token': token})

    assert response.status_code == 200
    made = PostReply.query.filter_by(parent_id=parent.id).one()
    assert made.body == 'the answer'
    assert made.post_id == post.id
    assert task.call_args.args[0] == 'make_reply'


def test_an_inline_reply_remembers_the_language_chosen(app, env):
    anon, community, post, mod, author, outsider = env
    parent = a_reply(post, author)
    client = as_user(app, author)
    token = csrf(app, client)

    with patch('app.post.routes.task_selector'):
        client.post(f'/post/{post.id}/comment/{parent.id}/reply_inline/abc',
                    data={'body': 'the answer',
                          'language_id': str(english().id),
                          'csrf_token': token})

    db.session.refresh(author)
    assert author.language_id == english().id


def test_an_inline_reply_with_no_language_falls_back(app, env):
    """D1093. A form field is whatever the caller sends. Measured:
    `TypeError: int() argument must be a string, a bytes-like object or a
    real number, not 'NoneType'` (app/post/routes.py:1005)."""
    anon, community, post, mod, author, outsider = env
    parent = a_reply(post, author)
    author.language_id = english().id
    db.session.commit()
    client = as_user(app, author)
    token = csrf(app, client)

    with patch('app.post.routes.task_selector'):
        response = client.post(
            f'/post/{post.id}/comment/{parent.id}/reply_inline/abc',
            data={'body': 'the answer', 'csrf_token': token})

    assert response.status_code == 200
    assert PostReply.query.filter_by(parent_id=parent.id).one().language_id \
        == english().id


def test_an_inline_reply_with_a_junk_language_falls_back(app, env):
    anon, community, post, mod, author, outsider = env
    parent = a_reply(post, author)
    author.language_id = english().id
    db.session.commit()
    client = as_user(app, author)
    token = csrf(app, client)

    with patch('app.post.routes.task_selector'):
        response = client.post(
            f'/post/{post.id}/comment/{parent.id}/reply_inline/abc',
            data={'body': 'the answer', 'language_id': 'banana',
                  'csrf_token': token})

    assert response.status_code == 200


def test_an_empty_inline_reply_just_closes_the_form(app, env):
    anon, community, post, mod, author, outsider = env
    parent = a_reply(post, author)
    client = as_user(app, author)
    token = csrf(app, client)
    before = PostReply.query.count()

    response = client.post(
        f'/post/{post.id}/comment/{parent.id}/reply_inline/abc',
        data={'body': '   ', 'language_id': str(english().id),
              'csrf_token': token})

    assert response.status_code == 200
    assert response.data == \
        f'<div id="reply_to_{parent.id}" class="hidable"></div>'.encode()
    assert PostReply.query.count() == before


def test_a_refused_inline_reply_names_the_element_it_replaces(app, env):
    """D1094. The fragment was not an f-string, so its id was the literal
    `reply_to_{comment_id}`, htmx had no element to swap, and the refusal was
    rendered into nothing."""
    anon, community, post, mod, author, outsider = env
    parent = a_reply(post, author)
    client = as_user(app, author)
    token = csrf(app, client)

    with patch('app.models.PostReply.new',
               side_effect=PostReplyValidationError('no good')):
        response = client.post(
            f'/post/{post.id}/comment/{parent.id}/reply_inline/abc',
            data={'body': 'the answer', 'language_id': str(english().id),
                  'csrf_token': token})

    assert f'id="reply_to_{parent.id}"'.encode() in response.data
    assert b'{comment_id}' not in response.data
    assert b'no good' in response.data


def test_a_banned_account_gets_no_inline_form(app, env):
    anon, community, post, mod, author, outsider = env
    parent = a_reply(post, author)
    author.banned = True
    db.session.commit()

    response = as_user(app, author).get(
        f'/post/{post.id}/comment/{parent.id}/reply_inline/abc')

    assert response.data == b'You have been banned.'


def test_an_account_banned_from_commenting_gets_no_inline_form(app, env):
    anon, community, post, mod, author, outsider = env
    parent = a_reply(post, author)
    author.ban_comments = True
    db.session.commit()

    response = as_user(app, author).get(
        f'/post/{post.id}/comment/{parent.id}/reply_inline/abc')

    assert response.data == b'You have been banned.'


def test_somebody_who_may_not_comment_here_gets_no_inline_form(app, env):
    anon, community, post, mod, author, outsider = env
    parent = a_reply(post, author)

    response = as_user(app, outsider).get(
        f'/post/{post.id}/comment/{parent.id}/reply_inline/abc')

    assert response.data == \
        b'You are not permitted to comment in this community'


def test_the_inline_form_is_closed_when_comments_are_disabled(app, env):
    anon, community, post, mod, author, outsider = env
    parent = a_reply(post, author)
    post.comments_enabled = False
    db.session.commit()

    response = as_user(app, author).get(
        f'/post/{post.id}/comment/{parent.id}/reply_inline/abc')

    assert response.data == b'Comments have been disabled.'


def test_you_cannot_reply_inline_to_somebody_who_blocked_you(app, env):
    anon, community, post, mod, author, outsider = env
    from app.models import UserBlock
    parent = a_reply(post, mod)
    db.session.add(UserBlock(blocker_id=mod.id, blocked_id=author.id))
    db.session.commit()

    response = as_user(app, author).get(
        f'/post/{post.id}/comment/{parent.id}/reply_inline/abc')

    assert b'You cannot reply to' in response.data


def test_a_comment_with_replies_turned_off_cannot_be_answered(app, env):
    anon, community, post, mod, author, outsider = env
    parent = a_reply(post, author, replies_enabled=False)

    response = as_user(app, author).get(
        f'/post/{post.id}/comment/{parent.id}/reply_inline/abc')

    assert response.data == b'This comment cannot be replied to.'


def test_the_inline_form_offers_the_parents_language(app, env):
    """The form names the language of what is being answered, so a reply does
    not silently change the language of a thread."""
    anon, community, post, mod, author, outsider = env
    parent = a_reply(post, author, language_id=english().id)
    outsider_language = Language.query.filter_by(code='und').one()
    author.language_id = outsider_language.id
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        as_user(app, author).get(
            f'/post/{post.id}/comment/{parent.id}/reply_inline/abc')

    kwargs = render.call_args.kwargs
    assert kwargs['recipient_language_code'] == 'en'
    assert kwargs['recipient_language_name'] == 'English'


def test_the_inline_form_says_nothing_when_the_languages_agree(app, env):
    """Naming the language only helps when it differs from the reader's."""
    anon, community, post, mod, author, outsider = env
    parent = a_reply(post, author, language_id=english().id)
    author.language_id = english().id
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        as_user(app, author).get(
            f'/post/{post.id}/comment/{parent.id}/reply_inline/abc')

    assert render.call_args.kwargs['recipient_language_code'] is None


def test_the_inline_form_marks_a_banned_author(app, env):
    anon, community, post, mod, author, outsider = env
    parent = a_reply(post, mod)
    mod.banned = True
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        as_user(app, author).get(
            f'/post/{post.id}/comment/{parent.id}/reply_inline/abc')

    assert render.call_args.kwargs['author_banned'] is True


def test_the_inline_form_marks_an_author_banned_from_this_community(app, env):
    anon, community, post, mod, author, outsider = env
    parent = a_reply(post, mod)
    make_community_ban(mod, community, banned_by=author)

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        as_user(app, author).get(
            f'/post/{post.id}/comment/{parent.id}/reply_inline/abc')

    assert render.call_args.kwargs['author_banned'] is True


def test_an_inline_reply_carries_the_authors_flair(app, env):
    anon, community, post, mod, author, outsider = env
    from app.models import UserFlair
    parent = a_reply(post, author)
    db.session.add(UserFlair(user_id=author.id, community_id=community.id,
                             flair='bronze'))
    db.session.commit()
    client = as_user(app, author)
    token = csrf(app, client)

    with patch('app.post.routes.task_selector'):
        with patch('app.post.routes.render_template',
                   return_value='rendered') as render:
            client.post(
                f'/post/{post.id}/comment/{parent.id}/reply_inline/abc',
                data={'body': 'the answer', 'language_id': str(english().id),
                      'csrf_token': token})

    assert render.call_args.kwargs['user_flair'] == {author.id: 'bronze'}


# --------------------------------------------------------------------------
# Editing a post
# --------------------------------------------------------------------------


def test_the_edit_form_is_filled_in_from_the_post(app, env):
    anon, community, post, mod, author, outsider = env
    post.notify_author = True
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        response = as_user(app, author).get(f'/post/{post.id}/edit')

    assert response.status_code == 200
    form = render.call_args.kwargs['form']
    assert form.title.data == 'THETITLE'
    assert form.body.data == 'the body'
    assert form.notify_author.data is True


def test_nobody_else_may_edit_a_post(app, env):
    """`post.user_id == current_user.id` -- editing is the author's alone,
    even for a moderator, because the text is attributed to them."""
    anon, community, post, mod, author, outsider = env

    assert as_user(app, mod).get(f'/post/{post.id}/edit').status_code == 401


def test_an_author_banned_from_the_community_may_not_edit(app, env):
    anon, community, post, mod, author, outsider = env
    make_community_ban(author, community, banned_by=mod)

    assert as_user(app, author).get(
        f'/post/{post.id}/edit').status_code == 403


def test_an_unknown_post_cannot_be_edited(app, env):
    anon, community, post, mod, author, outsider = env

    assert as_user(app, author).get('/post/999999/edit').status_code == 404


def test_a_post_of_an_unknown_type_cannot_be_edited(app, env):
    """The form is chosen by `post.type`, and a type with no form is a 404
    rather than an exception."""
    anon, community, post, mod, author, outsider = env
    post.type = 99
    db.session.commit()

    assert as_user(app, author).get(
        f'/post/{post.id}/edit').status_code == 404


def test_a_link_post_is_edited_with_the_link_form(app, env):
    anon, community, post, mod, author, outsider = env
    post.type = POST_TYPE_LINK
    post.url = 'https://example.com/an-article'
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        as_user(app, author).get(f'/post/{post.id}/edit')

    kwargs = render.call_args.kwargs
    assert kwargs['post_type'] == POST_TYPE_LINK
    assert kwargs['form'].link_url.data == 'https://example.com/an-article'


def test_a_video_post_whose_url_is_not_a_video_becomes_a_link(app, env):
    """The stored type and the URL can disagree -- a federated post, or a URL
    edited since -- and the form has to follow the URL."""
    anon, community, post, mod, author, outsider = env
    post.type = POST_TYPE_VIDEO
    post.url = 'https://example.com/not-a-video'
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        as_user(app, author).get(f'/post/{post.id}/edit')

    assert render.call_args.kwargs['post_type'] == POST_TYPE_LINK


def test_a_video_post_keeps_the_video_form(app, env):
    anon, community, post, mod, author, outsider = env
    post.type = POST_TYPE_VIDEO
    post.url = 'https://example.com/video.mp4'
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        as_user(app, author).get(f'/post/{post.id}/edit')

    kwargs = render.call_args.kwargs
    assert kwargs['post_type'] == POST_TYPE_VIDEO
    assert kwargs['form'].video_url.data == 'https://example.com/video.mp4'


def test_an_image_post_hosted_elsewhere_is_edited_as_a_link(app, env):
    """Only a locally stored image can be replaced through this form, so a
    remote one is offered the link form instead."""
    anon, community, post, mod, author, outsider = env
    image = File(source_url='https://remote.example/i/1.jpg')
    db.session.add(image)
    db.session.commit()
    post.type = POST_TYPE_IMAGE
    post.image_id = image.id
    db.session.commit()

    with patch('app.post.routes.is_local_image_url', return_value=False):
        with patch('app.post.routes.render_template',
                   return_value='rendered') as render:
            as_user(app, author).get(f'/post/{post.id}/edit')

    assert render.call_args.kwargs['post_type'] == POST_TYPE_LINK


def test_a_local_image_post_is_edited_with_the_image_form(app, env):
    anon, community, post, mod, author, outsider = env
    image = File(source_url='https://test.piefed.local/static/media/1.jpg',
                 alt_text='a cat', file_path='app/static/media/missing.jpg')
    db.session.add(image)
    db.session.commit()
    post.type = POST_TYPE_IMAGE
    post.image_id = image.id
    db.session.commit()

    with patch('app.post.routes.is_local_image_url', return_value=True):
        with patch('app.post.routes.render_template',
                   return_value='rendered') as render:
            response = as_user(app, author).get(f'/post/{post.id}/edit')

    assert response.status_code == 200
    assert render.call_args.kwargs['form'].image_alt_text.data == 'a cat'


def test_an_image_with_no_stored_path_is_found_from_its_url(app, env):
    """Older rows have `source_url` and no `file_path`, so the path is derived
    from the URL -- and a file that is no longer on disk is skipped rather
    than raising."""
    anon, community, post, mod, author, outsider = env
    image = File(source_url='https://test.piefed.local/static/media/gone.jpg',
                 alt_text='a cat', file_path=None)
    db.session.add(image)
    db.session.commit()
    post.type = POST_TYPE_IMAGE
    post.image_id = image.id
    db.session.commit()

    with patch('app.post.routes.is_local_image_url', return_value=True):
        with patch('app.post.routes.render_template',
                   return_value='rendered') as render:
            response = as_user(app, author).get(f'/post/{post.id}/edit')

    assert response.status_code == 200
    assert render.call_args.kwargs['form'].image_file.data is None


def test_a_stored_image_is_loaded_into_the_form(app, env, tmp_path):
    """The bytes are read so the form can re-upload the existing image when
    nothing new is chosen."""
    anon, community, post, mod, author, outsider = env
    on_disk = tmp_path / 'cat.jpg'
    on_disk.write_bytes(b'JPEGBYTES')
    image = File(source_url='https://test.piefed.local/static/media/cat.jpg',
                 alt_text='a cat', file_path=str(on_disk))
    db.session.add(image)
    db.session.commit()
    post.type = POST_TYPE_IMAGE
    post.image_id = image.id
    db.session.commit()

    with patch('app.post.routes.is_local_image_url', return_value=True):
        with patch('app.post.routes.render_template',
                   return_value='rendered') as render:
            as_user(app, author).get(f'/post/{post.id}/edit')

    assert render.call_args.kwargs['form'].image_file.data == b'JPEGBYTES'


def test_in_debug_the_refused_edit_raises_instead(app, env):
    """A developer wants the traceback, not the flash -- so in debug the
    original exception is re-raised after the message is set."""
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    token = csrf(app, client)

    # `Flask.debug` is a property over config['DEBUG'], so patch the config:
    # patch.object on the property sets it on the CLASS and leaks into the
    # next test (fact 516).
    with patch.dict(app.config, {'DEBUG': True}):
        with patch('app.post.routes.edit_post',
                   side_effect=Exception('boom')):
            with patch('app.post.routes.flash'):
                with pytest.raises(Exception, match='boom'):
                    client.post(
                        f'/post/{post.id}/edit',
                        data={'title': 'a new title', 'body': 'a new body',
                              'language_id': str(english().id),
                              'timezone': 'Europe/London', 'repeat': 'none',
                              'submit': 'Save', 'csrf_token': token})


def test_a_poll_post_arrives_with_its_choices(app, env):
    anon, community, post, mod, author, outsider = env
    post.type = POST_TYPE_POLL
    db.session.commit()
    poll = make_poll(post, mode='multiple')
    # Inserted out of order on purpose (facts 499, 506).
    make_poll_choice(post, 'no', sort_order=1)
    make_poll_choice(post, 'yes', sort_order=0)

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        as_user(app, author).get(f'/post/{post.id}/edit')

    form = render.call_args.kwargs['form']
    assert form.mode.data == 'multiple'
    assert form.choice_1.data == 'yes'
    assert form.choice_2.data == 'no'


def test_an_event_post_arrives_in_its_own_timezone(app, env):
    """`event.start` is stored naive UTC; the form has to show the time the
    organiser typed, in the timezone they chose."""
    from datetime import datetime
    anon, community, post, mod, author, outsider = env
    post.type = POST_TYPE_EVENT
    db.session.add(Event(post_id=post.id,
                         start=datetime(2026, 6, 1, 12, 0),
                         end=datetime(2026, 6, 1, 13, 0),
                         timezone='Europe/Berlin', online=True,
                         online_link='https://example.com/call',
                         more_info_url='https://example.com/more'))
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        as_user(app, author).get(f'/post/{post.id}/edit')

    kwargs = render.call_args.kwargs
    form = kwargs['form']
    assert form.start_datetime.data == datetime(2026, 6, 1, 14, 0)
    assert form.event_timezone.data == 'Europe/Berlin'
    assert form.online_link.data == 'https://example.com/call'
    assert form.more_info_url.data == 'https://example.com/more'  # R223
    assert kwargs['event_online'] is True


def test_an_event_in_a_place_arrives_with_its_address(app, env):
    from datetime import datetime
    anon, community, post, mod, author, outsider = env
    post.type = POST_TYPE_EVENT
    db.session.add(Event(post_id=post.id,
                         start=datetime(2026, 6, 1, 12, 0),
                         end=datetime(2026, 6, 1, 13, 0),
                         timezone='UTC', online=False,
                         location={'address': '1 Main St', 'city': 'Town',
                                   'country': 'Nowhere'}))
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        as_user(app, author).get(f'/post/{post.id}/edit')

    form = render.call_args.kwargs['form']
    assert form.irl_address.data == '1 Main St'
    assert form.irl_city.data == 'Town'
    assert form.irl_country.data == 'Nowhere'


def test_the_sticky_box_is_for_moderators(app, env):
    anon, community, post, mod, author, outsider = env

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        as_user(app, author).get(f'/post/{post.id}/edit')

    assert render.call_args.kwargs['form'].sticky.render_kw == {
        'disabled': True}


def test_a_moderator_editing_their_own_post_may_make_it_sticky(app, env):
    anon, community, post, mod, author, outsider = env
    their_post = make_post(community, mod, 'https://test.piefed.local/p/9',
                           title='the mods own')
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        as_user(app, mod).get(f'/post/{their_post.id}/edit')

    assert render.call_args.kwargs['form'].sticky.render_kw is None


def test_an_nsfw_community_forces_the_box_on(app, env):
    """A post in an NSFW community is NSFW whatever the author says, so the
    box is set and disabled rather than left to them."""
    anon, community, post, mod, author, outsider = env
    community.nsfw = True
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        as_user(app, author).get(f'/post/{post.id}/edit')

    form = render.call_args.kwargs['form']
    assert form.nsfw.data is True
    assert form.nsfw.render_kw == {'disabled': True}


def test_an_nsfl_community_disables_its_own_box(app, env):
    """D1095. The NSFL arm set `form.nsfw.render_kw` -- the wrong field --
    so an NSFL community left its own box editable and locked the NSFW one
    instead."""
    anon, community, post, mod, author, outsider = env
    community.nsfl = True
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        as_user(app, author).get(f'/post/{post.id}/edit')

    form = render.call_args.kwargs['form']
    assert form.nsfl.data is True
    assert form.nsfl.render_kw == {'disabled': True}
    assert form.nsfw.render_kw is None


def test_an_instance_with_nsfl_off_disables_that_box(app, env):
    anon, community, post, mod, author, outsider = env
    site = db.session.get(Site, 1)
    site.enable_nsfl = False
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        as_user(app, author).get(f'/post/{post.id}/edit')

    assert render.call_args.kwargs['form'].nsfl.render_kw == {
        'disabled': True}


def test_a_community_with_no_flair_drops_the_field(app, env):
    """Fact 432: `del form.flair` pops it from `_fields` and leaves the
    attribute at None, so the template asks `if form.flair` rather than
    `hasattr`."""
    anon, community, post, mod, author, outsider = env

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        as_user(app, author).get(f'/post/{post.id}/edit')

    form = render.call_args.kwargs['form']
    assert form.flair is None
    assert 'flair' not in form._fields


def test_the_flair_a_post_has_is_ticked(app, env):
    anon, community, post, mod, author, outsider = env
    flair = make_community_flair(community, 'spoiler')
    db.session.commit()
    post.flair.append(flair)
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        as_user(app, author).get(f'/post/{post.id}/edit')

    assert render.call_args.kwargs['form'].flair.data == [flair.id]


def test_private_mods_are_not_named_on_the_edit_page(app, env):
    anon, community, post, mod, author, outsider = env
    community.private_mods = True
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        as_user(app, author).get(f'/post/{post.id}/edit')

    assert render.call_args.kwargs['mods'] == []


def test_the_edit_page_names_the_moderators(app, env):
    anon, community, post, mod, author, outsider = env

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        as_user(app, author).get(f'/post/{post.id}/edit')

    assert 'mod' in [user.user_name for user in
                     render.call_args.kwargs['mods']]


def test_saving_an_edit(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    token = csrf(app, client)

    with patch('app.post.routes.edit_post') as edited:
        with patch('app.post.routes.flash') as flashed:
            response = client.post(
                f'/post/{post.id}/edit',
                data={'title': 'a new title', 'body': 'a new body',
                      'language_id': str(english().id),
                      'timezone': 'Europe/London',
                      'repeat': 'none', 'submit': 'Save',
                      'csrf_token': token})

    assert response.status_code == 302
    assert edited.call_args.args[1].id == post.id
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'Your changes have been saved' in messages


def test_a_refused_edit_says_why(app, env):
    """`edit_post` raises for a refusal -- a URL the instance will not store,
    an image it cannot read -- and the reason is put in front of the person."""
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    token = csrf(app, client)

    with patch('app.post.routes.edit_post',
               side_effect=Exception('that domain is blocked')):
        with patch('app.post.routes.flash') as flashed:
            response = client.post(
                f'/post/{post.id}/edit',
                data={'title': 'a new title', 'body': 'a new body',
                      'language_id': str(english().id),
                      'timezone': 'Europe/London',
                      'repeat': 'none', 'submit': 'Save',
                      'csrf_token': token})

    assert response.status_code == 401
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'that domain is blocked' in messages
