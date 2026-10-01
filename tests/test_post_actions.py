"""The rest of `app/post/routes.py`: votes, bookmarks, emoji, reminders, and
the small actions a post page offers.

Sub-project 82, slice F -- the last of the module, which is why its floor is
taken in this round. Six defects, all measured:

* `/post/<id>/oembed` -- what a chat client or a link preview fetches, and so
  the widest audience any of these routes has -- carried a PRIVATE community's
  post title and its author's name to anyone (D1119);
* `post_reply_distinguish` marked a comment as speaking for the moderators of
  a community its author does not moderate (D1120), D1077's twelfth site;
* `ShareMastodonForm.domain` went straight into the host part of a redirect,
  so anything typed became the destination (D1121);
* a poll vote with nothing ticked was `int(None)` (D1122);
* `post_options` never made the private-community check its comment-level twin
  got in slice A (D1123);
* the three translate routes hand back the text they translate, and send it to
  the configured endpoint, without asking whose community it is (D1124).
"""
from datetime import datetime
from unittest.mock import patch

import pytest
from flask import get_template_attribute, render_template
from flask_login import login_user

from app.constants import (POST_TYPE_ARTICLE, POST_TYPE_IMAGE, POST_TYPE_POLL,
                           SUBSCRIPTION_OWNER)
from app import db
from app.models import (BlockedImage, Emoji, File, Instance, Language,
                        NotificationSubscription, Poll, PollChoice, Post,
                        PostBookmark, PostReply, PostReplyBookmark,
                        PostReplyVote, PostVote, Reminder, Site, User)
from app.utils import utcnow
from tests.factories import (grant_permission, make_community,
                             make_community_ban,
                             make_community_member, make_instance, make_poll,
                             make_poll_choice, make_post, make_post_reply,
                             make_user)

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


def a_reply(post, user, body='a reply', **columns):
    """Fact 501."""
    reply = make_post_reply(post, user, body=body)
    reply.body_html = f'<p>{body}</p>'
    for column, value in columns.items():
        setattr(reply, column, value)
    db.session.commit()
    return reply


def private_place(app, name='secret'):
    community = make_community(name)
    community.private = True
    db.session.commit()
    member = make_user(instance(), f'{name}_member', local=True,
                       with_keys=True)
    member.verified = True
    db.session.commit()
    make_community_member(member, community)
    post = make_post(community, member, f'https://test.piefed.local/p/{name}',
                     title='SECRETTITLE')
    post.body = 'secret body'
    post.body_html = '<p>SECRETBODY</p>'
    db.session.commit()
    return community, post, member


# --------------------------------------------------------------------------
# D1119 -- the oembed document
# --------------------------------------------------------------------------


def test_the_oembed_document(app, env):
    anon, community, post, mod, author, outsider = env

    response = anon.get(f'/post/{post.id}/oembed')

    assert response.status_code == 200
    body = response.get_json()
    assert body['title'] == 'THETITLE'
    assert body['author_name'] == 'author'
    assert body['type'] == 'rich'
    assert f'/post/{post.id}/embed' in body['html']


def test_a_private_communitys_post_has_no_oembed(app, env):
    """D1119. oEmbed is what a chat client or a link preview fetches, so this
    JSON is the widest audience any of these routes has. Measured: `PROBE aq1
    oembed of a private post: 200 | title: True`."""
    anon, community, post, mod, author, outsider = env
    secret, secret_post, member = private_place(app)

    response = anon.get(f'/post/{secret_post.id}/oembed')

    assert response.status_code == 403
    assert b'SECRETTITLE' not in response.data


def test_an_unpublished_post_has_no_oembed(app, env):
    anon, community, post, mod, author, outsider = env
    post.status = -2
    post.scheduled_for = utcnow()
    db.session.commit()

    assert anon.get(f'/post/{post.id}/oembed').status_code == 404


def test_an_unknown_post_has_no_oembed(app, env):
    anon, community, post, mod, author, outsider = env

    assert anon.get('/post/999999/oembed').status_code == 404


# --------------------------------------------------------------------------
# D1123 -- the options menu
# --------------------------------------------------------------------------


def test_the_post_options_menu(app, env):
    anon, community, post, mod, author, outsider = env

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        response = as_user(app, author).get(f'/post/{post.id}/options_menu')

    assert response.status_code == 200
    assert render.call_args.kwargs['post'].id == post.id
    assert render.call_args.kwargs['offer_markdown_source'] == 'False'


def test_the_options_menu_can_offer_the_markdown_source(app, env):
    anon, community, post, mod, author, outsider = env

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        as_user(app, author).get(f'/post/{post.id}/True/options_menu')

    assert render.call_args.kwargs['offer_markdown_source'] == 'True'


def test_the_options_menu_refuses_a_private_community(app, env):
    """D1123. `post_reply_options` got this check in slice A and its
    post-level twin did not."""
    anon, community, post, mod, author, outsider = env
    secret, secret_post, member = private_place(app)

    response = as_user(app, outsider).get(
        f'/post/{secret_post.id}/options_menu')

    assert response.status_code == 403


def test_the_options_menu_knows_about_a_bookmark(app, env):
    anon, community, post, mod, author, outsider = env
    db.session.add(PostBookmark(user_id=author.id, post_id=post.id))
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        as_user(app, author).get(f'/post/{post.id}/options_menu')

    assert render.call_args.kwargs['existing_bookmark'] is not None


def test_the_options_menu_knows_a_post_is_hidden(app, env):
    anon, community, post, mod, author, outsider = env

    with patch('app.models.User.has_hidden_post', return_value=True):
        with patch('app.post.routes.render_template',
                   return_value='rendered') as render:
            as_user(app, author).get(f'/post/{post.id}/options_menu')

    assert render.call_args.kwargs['hidden'] is True


def test_a_deleted_post_has_no_options_for_an_anonymous_visitor(app, env):
    anon, community, post, mod, author, outsider = env
    post.deleted = True
    db.session.commit()

    assert anon.get(f'/post/{post.id}/options_menu').status_code == 404


def test_a_deleted_post_still_has_options_for_a_logged_in_reader(app, env):
    anon, community, post, mod, author, outsider = env
    post.deleted = True
    db.session.commit()

    with patch('app.post.routes.render_template', return_value='rendered'):
        response = as_user(app, author).get(f'/post/{post.id}/options_menu')

    assert response.status_code == 200


def test_an_unknown_post_has_no_options(app, env):
    anon, community, post, mod, author, outsider = env

    assert anon.get('/post/999999/options_menu').status_code == 404


# --------------------------------------------------------------------------
# D1120 -- distinguishing a comment
# --------------------------------------------------------------------------


def test_a_moderator_distinguishes_their_own_comment(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, mod)
    client = as_user(app, mod)
    token = csrf(app, client)

    response = client.post(
        f'/post/{post.id}/comment/{reply.id}/distinguish',
        data={'csrf_token': token})

    assert response.status_code == 302
    db.session.refresh(reply)
    assert reply.distinguished is True


def test_distinguishing_is_a_toggle(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, mod, distinguished=True)
    client = as_user(app, mod)
    token = csrf(app, client)

    client.post(f'/post/{post.id}/comment/{reply.id}/distinguish',
                data={'csrf_token': token})

    db.session.refresh(reply)
    assert reply.distinguished is False


def test_a_comment_in_another_community_cannot_be_distinguished(app, env):
    """D1120. D1077's twelfth site: the moderator test is made against
    `post.community` and the flag is set on `post_reply`, so a moderator of
    one community marked their own comment in ANOTHER -- a private one, in
    the measurement -- as speaking for that community's moderators. Measured:
    `PROBE aq2 distinguish across communities: 302 | distinguished now=True`.
    """
    anon, community, post, mod, author, outsider = env
    secret, secret_post, member = private_place(app)
    their_reply = a_reply(secret_post, mod)
    client = as_user(app, mod)
    token = csrf(app, client)

    response = client.post(
        f'/post/{post.id}/comment/{their_reply.id}/distinguish',
        data={'csrf_token': token})

    assert response.status_code == 404
    db.session.refresh(their_reply)
    assert their_reply.distinguished is False


def test_somebody_elses_comment_cannot_be_distinguished(app, env):
    """A distinguished comment speaks in its AUTHOR's name, so a moderator
    cannot apply it to someone else's."""
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    client = as_user(app, mod)
    token = csrf(app, client)

    response = client.post(
        f'/post/{post.id}/comment/{reply.id}/distinguish',
        data={'csrf_token': token})

    assert response.status_code == 401
    db.session.refresh(reply)
    assert reply.distinguished is False


def test_an_ordinary_author_cannot_distinguish_their_own_comment(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    client = as_user(app, author)
    token = csrf(app, client)

    response = client.post(
        f'/post/{post.id}/comment/{reply.id}/distinguish',
        data={'csrf_token': token})

    assert response.status_code == 401


# --------------------------------------------------------------------------
# D1121 -- sharing to Mastodon
# --------------------------------------------------------------------------


def test_the_share_form_remembers_the_last_instance(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    client.set_cookie('mastodon_share', 'fosstodon.org',
                      domain=app.config['SERVER_NAME'])

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        response = client.get(f'/post/{post.id}/share_mastodon')

    assert response.status_code == 200
    assert render.call_args.kwargs['form'].domain.data == 'fosstodon.org'


def test_the_share_form_defaults_to_mastodon_social(app, env):
    anon, community, post, mod, author, outsider = env

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        as_user(app, author).get(f'/post/{post.id}/share_mastodon')

    assert render.call_args.kwargs['form'].domain.data == 'mastodon.social'


def test_sharing_sends_the_reader_to_that_instance(app, env):
    anon, community, post, mod, author, outsider = env
    post.slug = f'/c/general/p/{post.id}/thetitle'
    db.session.commit()
    client = as_user(app, author)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/share_mastodon',
                           data={'domain': 'fosstodon.org',
                                 'submit': 'Share', 'csrf_token': token})

    assert response.status_code == 302
    assert response.headers['Location'].startswith(
        'https://fosstodon.org/share?')
    assert post.slug in response.headers['Location']
    assert 'mastodon_share=fosstodon.org' in response.headers['Set-Cookie']


@pytest.mark.parametrize('domain', [
    'evil.example/phish?x=',
    'https://evil.example',
    'user@evil.example',
    'evil.example:8080',
    'localhost',
    '',
])
def test_the_share_destination_has_to_be_a_domain(app, env, domain):
    """D1121. The field carries a `Length(max=512)` and nothing else, and its
    value went straight into the host part of a redirect. Measured: `PROBE aq3
    share to an arbitrary host: 302 ->
    https://evil.example/phish?x=/share?text=MINE&url=...`."""
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/share_mastodon',
                           data={'domain': domain, 'submit': 'Share',
                                 'csrf_token': token})

    assert 'evil.example' not in response.headers.get('Location', '')
    assert response.headers.get('Location', '') != f'https://{domain}/share'


def test_a_post_with_no_slug_is_still_shareable(app, env):
    """The same measurement showed the other end of that URL: `post.slug` is
    None until the post has one, and an f-string writes it out as the four
    characters "None"."""
    anon, community, post, mod, author, outsider = env
    post.slug = None
    db.session.commit()
    client = as_user(app, author)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/share_mastodon',
                           data={'domain': 'fosstodon.org',
                                 'submit': 'Share', 'csrf_token': token})

    assert 'None' not in response.headers['Location']
    assert f'/post/{post.id}' in response.headers['Location']


def test_an_unknown_post_cannot_be_shared(app, env):
    anon, community, post, mod, author, outsider = env

    assert as_user(app, author).get(
        '/post/999999/share_mastodon').status_code == 404


# --------------------------------------------------------------------------
# D1122 -- voting in a poll
# --------------------------------------------------------------------------


def a_poll(post, mode='single'):
    post.type = POST_TYPE_POLL
    db.session.commit()
    poll = make_poll(post, mode=mode)
    make_poll_choice(post, 'no', sort_order=1)
    make_poll_choice(post, 'yes', sort_order=0)
    return poll


def test_voting_in_a_poll(app, env):
    anon, community, post, mod, author, outsider = env
    a_poll(post)
    choice = PollChoice.query.filter_by(choice_text='yes').one()
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.vote_for_poll') as voted:
        response = client.post(f'/poll/{post.id}/vote',
                               data={'poll_choice': str(choice.id),
                                     'csrf_token': token})

    assert response.status_code == 302
    assert voted.call_args.args[1] == choice.id


def test_voting_in_a_multiple_choice_poll(app, env):
    anon, community, post, mod, author, outsider = env
    a_poll(post, mode='multiple')
    choices = [c.id for c in PollChoice.query.all()]
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.vote_for_poll') as voted:
        client.post(f'/poll/{post.id}/vote',
                    data={'poll_choice[]': [str(c) for c in choices],
                          'csrf_token': token})

    assert sorted(voted.call_args.args[1]) == sorted(
        [str(c) for c in choices])


def test_a_poll_vote_with_nothing_ticked(app, env):
    """D1122. Which is what a poll form sends when nobody ticks anything.
    `TypeError: int() argument must be a string, a bytes-like object or a real
    number, not 'NoneType'`."""
    anon, community, post, mod, author, outsider = env
    a_poll(post)
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.vote_for_poll') as voted:
        with patch('app.post.routes.flash') as flashed:
            response = client.post(f'/poll/{post.id}/vote',
                                   data={'csrf_token': token})

    assert response.status_code == 302
    assert voted.call_args is None
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'Choose an option first' in messages


def test_a_poll_vote_that_is_not_a_number(app, env):
    anon, community, post, mod, author, outsider = env
    a_poll(post)
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.vote_for_poll') as voted:
        response = client.post(f'/poll/{post.id}/vote',
                               data={'poll_choice': 'banana',
                                     'csrf_token': token})

    assert response.status_code == 302
    assert voted.call_args is None


def test_voting_in_a_poll_that_does_not_exist(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)
    token = csrf(app, client)

    assert client.post(f'/poll/{post.id}/vote',
                       data={'csrf_token': token}).status_code == 404
    assert client.post('/poll/999999/vote',
                       data={'csrf_token': token}).status_code == 404


# --------------------------------------------------------------------------
# D1124 -- translation
# --------------------------------------------------------------------------


def test_translating_a_post(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch.dict(app.config, {'TRANSLATE_ENDPOINT': 'https://lt.example'}):
        with patch('app.post.routes.libretranslate_string',
                   side_effect=['<p>TRANSLATED BODY</p>', 'TRANSLATED TITLE']):
            response = client.post(f'/post/{post.id}/translate',
                                   data={'csrf_token': token})

    assert response.status_code == 200
    assert b'TRANSLATED BODY' in response.data


def test_translating_a_private_communitys_post(app, env):
    """D1124. Translation hands back the text it was given -- and sends it to
    the configured endpoint on the way, so a private community's post both
    reached the caller and LEFT THE INSTANCE."""
    anon, community, post, mod, author, outsider = env
    secret, secret_post, member = private_place(app)
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch.dict(app.config, {'TRANSLATE_ENDPOINT': 'https://lt.example'}):
        with patch('app.post.routes.libretranslate_string') as translated:
            response = client.post(f'/post/{secret_post.id}/translate',
                                   data={'csrf_token': token})

    assert response.status_code == 403
    assert translated.call_args is None


def test_a_teaser_in_the_sites_own_language_is_auto_detected(app, env):
    anon, community, post, mod, author, outsider = env
    english = Language.query.filter_by(code='en').one()
    post.language_id = english.id
    db.session.get(Site, 1).language_id = english.id
    db.session.commit()
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch.dict(app.config, {'TRANSLATE_ENDPOINT': 'https://lt.example'}):
        with patch('app.post.routes.libretranslate_string',
                   return_value='x') as translated:
            client.post(f'/post_teaser/{post.id}/translate',
                        data={'csrf_token': token})

    assert translated.call_args.kwargs['source'] == 'auto'


def test_a_comment_in_the_sites_own_language_is_auto_detected(app, env):
    anon, community, post, mod, author, outsider = env
    english = Language.query.filter_by(code='en').one()
    reply = a_reply(post, author, language_id=english.id)
    db.session.get(Site, 1).language_id = english.id
    db.session.commit()
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch.dict(app.config, {'TRANSLATE_ENDPOINT': 'https://lt.example'}):
        with patch('app.post.routes.libretranslate_string',
                   return_value='x') as translated:
            client.post(f'/post_reply/{reply.id}/translate',
                        data={'csrf_token': token})

    assert translated.call_args.kwargs['source'] == 'auto'


def test_translating_a_posts_teaser(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch.dict(app.config, {'TRANSLATE_ENDPOINT': 'https://lt.example'}):
        with patch('app.post.routes.libretranslate_string',
                   return_value='TRANSLATED TITLE'):
            response = client.post(f'/post_teaser/{post.id}/translate',
                                   data={'csrf_token': token})

    assert b'TRANSLATED TITLE' in response.data
    assert f'/post/{post.id}'.encode() in response.data


def test_translating_a_private_communitys_teaser(app, env):
    anon, community, post, mod, author, outsider = env
    secret, secret_post, member = private_place(app)
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch.dict(app.config, {'TRANSLATE_ENDPOINT': 'https://lt.example'}):
        with patch('app.post.routes.libretranslate_string') as translated:
            response = client.post(f'/post_teaser/{secret_post.id}/translate',
                                   data={'csrf_token': token})

    assert response.status_code == 403
    assert translated.call_args is None


def test_translating_a_comment(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch.dict(app.config, {'TRANSLATE_ENDPOINT': 'https://lt.example'}):
        with patch('app.post.routes.libretranslate_string',
                   return_value='<p>TRANSLATED</p>'):
            response = client.post(f'/post_reply/{reply.id}/translate',
                                   data={'csrf_token': token})

    assert b'TRANSLATED' in response.data


def test_translating_a_private_communitys_comment(app, env):
    anon, community, post, mod, author, outsider = env
    secret, secret_post, member = private_place(app)
    reply = a_reply(secret_post, member)
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch.dict(app.config, {'TRANSLATE_ENDPOINT': 'https://lt.example'}):
        with patch('app.post.routes.libretranslate_string') as translated:
            response = client.post(f'/post_reply/{reply.id}/translate',
                                   data={'csrf_token': token})

    assert response.status_code == 403
    assert translated.call_args is None


@pytest.mark.parametrize('route', [
    '/post/{id}/translate', '/post_teaser/{id}/translate',
])
def test_translation_says_when_it_is_not_configured(app, env, route):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch.dict(app.config, {'TRANSLATE_ENDPOINT': ''}):
        response = client.post(route.format(id=post.id),
                               data={'csrf_token': token})

    assert b'not configured' in response.data.lower()


def test_comment_translation_says_when_it_is_not_configured(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch.dict(app.config, {'TRANSLATE_ENDPOINT': ''}):
        response = client.post(f'/post_reply/{reply.id}/translate',
                               data={'csrf_token': token})

    assert b'not configured' in response.data.lower()


def test_a_post_in_the_sites_own_language_is_auto_detected(app, env):
    """A post whose language matches the site's default may simply never have
    had one set, so the translator is asked to detect it rather than told."""
    anon, community, post, mod, author, outsider = env
    english = Language.query.filter_by(code='en').one()
    post.language_id = english.id
    site = db.session.get(Site, 1)
    site.language_id = english.id
    db.session.commit()
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch.dict(app.config, {'TRANSLATE_ENDPOINT': 'https://lt.example'}):
        with patch('app.post.routes.libretranslate_string',
                   return_value='x') as translated:
            client.post(f'/post/{post.id}/translate',
                        data={'csrf_token': token})

    assert translated.call_args.kwargs['source'] == 'auto'


# --------------------------------------------------------------------------
# Votes, emoji reactions and the voting-activity pages
# --------------------------------------------------------------------------


def test_voting_on_a_post(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.vote_for_post',
               return_value='voted') as voted:
        response = client.post(f'/post/{post.id}/upvote/default',
                               data={'csrf_token': token})

    assert response.status_code == 200
    assert voted.call_args.args[1] == 'upvote'
    assert voted.call_args.args[2] is True


def test_voting_privately(app, env):
    """`vote_privately` means the vote is not federated, and 'default' is what
    the button sends -- so the account's own setting decides."""
    anon, community, post, mod, author, outsider = env
    outsider.vote_privately = True
    db.session.commit()
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.vote_for_post', return_value='voted') as voted:
        client.post(f'/post/{post.id}/upvote/default',
                    data={'csrf_token': token})

    assert voted.call_args.args[2] is False


def test_a_public_vote_overrides_the_setting(app, env):
    anon, community, post, mod, author, outsider = env
    outsider.vote_privately = True
    db.session.commit()
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.vote_for_post', return_value='voted') as voted:
        client.post(f'/post/{post.id}/upvote/public',
                    data={'csrf_token': token})

    assert voted.call_args.args[2] is True


def test_voting_on_a_comment(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.vote_for_reply',
               return_value='voted') as voted:
        response = client.post(f'/comment/{reply.id}/downvote/default',
                               data={'csrf_token': token})

    assert response.status_code == 200
    assert voted.call_args.args[1] == 'downvote'


def test_voting_on_a_comment_privately(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    outsider.vote_privately = True
    db.session.commit()
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.vote_for_reply', return_value='v') as voted:
        client.post(f'/comment/{reply.id}/upvote/default',
                    data={'csrf_token': token})

    assert voted.call_args.args[2] is False


def test_a_public_comment_vote_overrides_the_setting(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    outsider.vote_privately = True
    db.session.commit()
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.vote_for_reply', return_value='v') as voted:
        client.post(f'/comment/{reply.id}/upvote/public',
                    data={'csrf_token': token})

    assert voted.call_args.args[2] is True


def test_the_emoji_chooser(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    db.session.add(Emoji(token=':tada:', url='https://x/t.png',
                         instance_id=1))
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        response = as_user(app, outsider).get(
            f'/comment/{reply.id}/upvote/default/emoji')

    assert response.status_code == 200
    assert [e.token for e in render.call_args.kwargs['emojis']] == [':tada:']


def test_choosing_an_emoji_reaction(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.vote_for_reply', return_value='v') as voted:
        response = client.post(
            f'/comment/{reply.id}/upvote/default/emoji',
            data={'emoji': ':tada:', 'submit': 'Choose',
                  'csrf_token': token})

    assert response.status_code == 302
    assert voted.call_args.args[3] == ':tada:'


def test_an_emoji_reaction_from_htmx_returns_the_fragment(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.vote_for_reply', return_value='fragment'):
        response = client.post(
            f'/comment/{reply.id}/upvote/default/emoji',
            data={'emoji': ':tada:', 'submit': 'Choose',
                  'csrf_token': token},
            headers={'HX-Request': 'true'})

    assert response.data == b'fragment'


def test_an_emoji_reaction_federates_by_default(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    outsider.vote_privately = True
    db.session.commit()
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.vote_for_reply', return_value='v') as voted:
        client.post(f'/comment/{reply.id}/upvote/public/emoji',
                    data={'emoji': ':tada:', 'submit': 'Choose',
                          'csrf_token': token})

    assert voted.call_args.args[2] is True


def test_the_emoji_list(app, env):
    """Local emoji first, then trusted instances, then everyone else -- so a
    remote instance cannot push its emoji to the top of the picker."""
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    trusted = make_instance('trusted.example', software='lemmy')
    trusted.trusted = True
    other = make_instance('other.example', software='lemmy')
    db.session.commit()
    db.session.add(Emoji(token=':zzz:', url='https://x/z.png', instance_id=1))
    db.session.add(Emoji(token=':aaa:', url='https://x/a.png',
                         instance_id=other.id))
    db.session.add(Emoji(token=':mmm:', url='https://x/m.png',
                         instance_id=trusted.id))
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        response = as_user(app, outsider).get(
            f'/comment/{reply.id}/emoji_list')

    assert response.status_code == 200
    assert [e['token'] for e in render.call_args.kwargs['emojis']] == \
        [':zzz:', ':mmm:', ':aaa:']


def test_setting_an_emoji_on_a_post(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.vote_for_post') as voted:
        with patch('app.post.routes.render_template',
                   return_value='rendered') as render:
            response = client.post(f'/post/{post.id}/emoji_set',
                                   data={'emoji': ':tada:',
                                         'csrf_token': token})

    assert response.status_code == 200
    assert voted.call_args.args[3] == ':tada:'
    assert render.call_args.kwargs['post_reply'].id == post.id


def test_setting_an_emoji_on_a_post_needs_an_account(app, env):
    """F10, fixed: the route now carries its vote siblings' decorators, so an
    anonymous visitor is sent to log in rather than answered 403."""
    anon, community, post, mod, author, outsider = env

    response = anon.post(f'/post/{post.id}/emoji_set', data={'emoji': ':tada:'})

    assert response.status_code == 302
    assert '/auth/login' in response.headers['Location']


@pytest.mark.parametrize('target', ['post', 'comment'])
def test_an_unverified_account_cannot_set_an_emoji(app, env, target):
    """F10 (permission audit), fixed: post_emoji_set and comment_emoji_set had
    no decorators, so an account that had not verified its email -- refused by
    the four ordinary vote routes -- could still cast an emoji upvote. They now
    carry @login_required @validation_required @approval_required like those
    routes; @login_required also CSRF-checks the POST."""
    anon, community, post, mod, author, outsider = env
    outsider.verified = False
    db.session.commit()
    target_id = post.id if target == 'post' else a_reply(post, author).id
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.vote_for_post') as voted_post, \
            patch('app.post.routes.vote_for_reply') as voted_reply:
        response = client.post(f'/{target}/{target_id}/emoji_set',
                               data={'emoji': ':tada:', 'csrf_token': token})

    assert response.status_code == 302
    assert '/validation_required' in response.headers['Location']
    assert (voted_post.call_args, voted_reply.call_args) == (None, None)
    assert client.post(f'/{target}/{target_id}/emoji_set',
                       data={'emoji': ':tada:'}).status_code == 400


def test_setting_an_emoji_on_a_comment(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.vote_for_reply') as voted:
        with patch('app.post.routes.render_template',
                   return_value='rendered') as render:
            response = client.post(f'/comment/{reply.id}/emoji_set',
                                   data={'emoji': ':tada:',
                                         'csrf_token': token})

    assert response.status_code == 200
    assert voted.call_args.args[3] == ':tada:'
    assert render.call_args.kwargs['post_reply'].id == reply.id


def test_setting_an_emoji_on_a_comment_needs_an_account(app, env):
    """F10, fixed: as for a post, an anonymous visitor is sent to log in."""
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author)

    response = anon.post(f'/comment/{reply.id}/emoji_set', data={'emoji': ':tada:'})

    assert response.status_code == 302
    assert '/auth/login' in response.headers['Location']


def test_the_voting_activity_of_a_post(app, env):
    """Who voted is moderator-only: it is the one place the instance shows a
    vote with a name attached."""
    anon, community, post, mod, author, outsider = env
    db.session.add(PostVote(user_id=outsider.id, post_id=post.id,
                            author_id=author.id, effect=1.0))
    db.session.add(PostVote(user_id=mod.id, post_id=post.id,
                            author_id=author.id, effect=-1.0))
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        response = as_user(app, mod).get(f'/post/{post.id}/voting_activity')

    assert response.status_code == 200
    kwargs = render.call_args.kwargs
    assert kwargs['post_title'] == 'THETITLE'
    assert [u.id for u, v in kwargs['upvoters']] == [outsider.id]
    assert [u.id for u, v in kwargs['downvoters']] == [mod.id]


def test_a_stranger_cannot_see_who_voted(app, env):
    anon, community, post, mod, author, outsider = env

    assert as_user(app, outsider).get(
        f'/post/{post.id}/voting_activity').status_code == 403


def test_the_voting_activity_of_a_comment(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    db.session.add(PostReplyVote(user_id=outsider.id, post_reply_id=reply.id,
                                 author_id=author.id, effect=1.0))
    db.session.commit()

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        response = as_user(app, mod).get(
            f'/comment/{reply.id}/voting_activity')

    assert response.status_code == 200
    assert [u.id for u, v in render.call_args.kwargs['upvoters']] == \
        [outsider.id]


def test_a_stranger_cannot_see_who_voted_on_a_comment(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author)

    assert as_user(app, outsider).get(
        f'/comment/{reply.id}/voting_activity').status_code == 403


def test_an_unknown_post_has_no_voting_activity(app, env):
    anon, community, post, mod, author, outsider = env

    assert as_user(app, mod).get(
        '/post/999999/voting_activity').status_code == 404
    assert as_user(app, mod).get(
        '/comment/999999/voting_activity').status_code == 404


# --------------------------------------------------------------------------
# Bookmarks, notifications, reminders and the small toggles
# --------------------------------------------------------------------------


def test_bookmarking_a_post(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/bookmark',
                           data={'csrf_token': token})

    assert response.status_code == 200
    assert PostBookmark.query.filter_by(user_id=outsider.id,
                                        post_id=post.id).count() == 1


def test_removing_a_posts_bookmark(app, env):
    anon, community, post, mod, author, outsider = env
    db.session.add(PostBookmark(user_id=outsider.id, post_id=post.id))
    db.session.commit()
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/remove_bookmark',
                           data={'csrf_token': token})

    assert response.status_code == 200
    assert PostBookmark.query.count() == 0


def test_bookmarking_a_post_that_does_not_exist(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)
    token = csrf(app, client)

    assert client.post('/post/999999/bookmark',
                       data={'csrf_token': token}).status_code == 404
    assert client.post('/post/999999/remove_bookmark',
                       data={'csrf_token': token}).status_code == 404


def test_bookmarking_a_comment(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post(
        f'/post/{post.id}/comment/{reply.id}/bookmark',
        data={'csrf_token': token})

    assert response.status_code == 200
    assert PostReplyBookmark.query.filter_by(user_id=outsider.id,
                                             post_reply_id=reply.id).count() \
        == 1


def test_removing_a_comments_bookmark(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    db.session.add(PostReplyBookmark(user_id=outsider.id,
                                     post_reply_id=reply.id))
    db.session.commit()
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post(
        f'/post/{post.id}/comment/{reply.id}/remove_bookmark',
        data={'csrf_token': token})

    assert response.status_code == 200
    assert PostReplyBookmark.query.count() == 0


def test_bookmarking_a_comment_that_does_not_exist(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)
    token = csrf(app, client)

    assert client.post(f'/post/{post.id}/comment/999999/bookmark',
                       data={'csrf_token': token}).status_code == 404
    assert client.post(f'/post/{post.id}/comment/999999/remove_bookmark',
                       data={'csrf_token': token}).status_code == 404


def test_subscribing_to_a_post(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/notification',
                           data={'csrf_token': token})

    assert response.status_code == 200
    assert NotificationSubscription.query.filter_by(
        user_id=outsider.id, entity_id=post.id).count() == 1


def test_subscribing_to_a_post_that_does_not_exist(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)
    token = csrf(app, client)

    assert client.post('/post/999999/notification',
                       data={'csrf_token': token}).status_code == 404


def test_subscribing_to_a_comment(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post(f'/post_reply/{reply.id}/notification',
                           data={'csrf_token': token})

    assert response.status_code == 200
    assert NotificationSubscription.query.filter_by(
        user_id=outsider.id, entity_id=reply.id).count() == 1


def test_subscribing_to_a_comment_that_does_not_exist(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)
    token = csrf(app, client)

    assert client.post('/post_reply/999999/notification',
                       data={'csrf_token': token}).status_code == 404


@pytest.mark.parametrize('which', ['post', 'post_reply'])
def test_toggling_reply_notifications_by_get_is_refused(app, env, which):
    """D994 sibling, fixed (owner ruling 2026-09-30). Both bells accepted GET,
    which login_required never CSRF-checks, so any page could subscribe or
    unsubscribe a signed-in user. They are POST-only now, and the bell is a form."""
    anon, community, post, mod, author, outsider = env
    target = post if which == 'post' else a_reply(post, author)
    client = as_user(app, outsider)

    assert client.get(f'/{which}/{target.id}/notification').status_code == 405
    assert client.post(f'/{which}/{target.id}/notification').status_code == 400
    assert NotificationSubscription.query.filter_by(
        user_id=outsider.id, entity_id=target.id).count() == 0


def test_the_bells_are_forms_carrying_the_token(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author)

    with app.test_request_context('/'):
        login_user(outsider)
        post_bell = render_template('post/_post_notification_toggle.html', post=post)
        reply_bell = render_template('post/_reply_notification_toggle.html',
                                     comment={'comment': reply})
        macro_bell = get_template_attribute('post/reply/_macros.html',
                                            'render_reply_notification_toggle')(
            {'comment': reply}, current_user=outsider)

    assert f'<form method="post" action="/post/{post.id}/notification"' in post_bell
    for html in (reply_bell, macro_bell):
        assert f'<form method="post" action="/post_reply/{reply.id}/notification"' in html
    for html in (post_bell, reply_bell, macro_bell):
        assert 'name="csrf_token"' in html
        assert 'href=' not in html


def test_setting_a_reminder_about_a_post(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/reminder',
                           data={'remind_at': 'in 2 weeks', 'submit': 'Save',
                                 'csrf_token': token})

    assert response.status_code == 302
    reminder = Reminder.query.one()
    assert reminder.user_id == outsider.id
    assert reminder.reminder_type == 1
    assert reminder.reminder_destination == post.id


def test_the_reminder_form_asks_first(app, env):
    anon, community, post, mod, author, outsider = env

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        response = as_user(app, outsider).get(f'/post/{post.id}/reminder')

    assert response.status_code == 200
    assert 'THETITLE' in render.call_args.kwargs['title']
    assert Reminder.query.count() == 0


def test_a_date_that_cannot_be_read_is_refused(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.render_template', return_value='rendered'):
        response = client.post(f'/post/{post.id}/reminder',
                               data={'remind_at': 'whenever', 'submit': 'Save',
                                     'csrf_token': token})

    assert response.status_code == 200
    assert Reminder.query.count() == 0


def test_somebody_banned_from_the_community_gets_no_reminder(app, env):
    anon, community, post, mod, author, outsider = env
    make_community_ban(outsider, community, banned_by=mod)

    assert as_user(app, outsider).get(
        f'/post/{post.id}/reminder').status_code == 403


def test_setting_a_reminder_about_a_comment(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post(f'/post_reply/{reply.id}/reminder',
                           data={'remind_at': 'in 2 weeks', 'submit': 'Save',
                                 'csrf_token': token})

    assert response.status_code == 302
    reminder = Reminder.query.one()
    assert reminder.reminder_type == 2
    assert reminder.reminder_destination == reply.id


def test_the_comment_reminder_form_asks_first(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author)

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        response = as_user(app, outsider).get(
            f'/post_reply/{reply.id}/reminder')

    assert response.status_code == 200
    assert 'author' in render.call_args.kwargs['title']


def test_somebody_banned_gets_no_comment_reminder(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    make_community_ban(outsider, community, banned_by=mod)

    assert as_user(app, outsider).get(
        f'/post_reply/{reply.id}/reminder').status_code == 403


def test_an_unknown_post_or_comment_has_no_reminder(app, env):
    anon, community, post, mod, author, outsider = env

    assert as_user(app, outsider).get(
        '/post/999999/reminder').status_code == 404
    assert as_user(app, outsider).get(
        '/post_reply/999999/reminder').status_code == 404


@pytest.mark.parametrize('mode, expected', [('yes', True), ('no', False)])
def test_locking_a_post(app, env, mode, expected):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, mod)
    token = csrf(app, client)

    with patch('app.post.routes.lock_post') as locked:
        response = client.post(f'/post/{post.id}/lock/{mode}',
                               data={'csrf_token': token})

    assert response.status_code == 302
    assert locked.call_args.args[1] is expected


@pytest.mark.parametrize('mode, expected', [('yes', True), ('no', False)])
def test_locking_a_comment(app, env, mode, expected):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    client = as_user(app, mod)
    token = csrf(app, client)

    with patch('app.post.routes.lock_post_reply') as locked:
        response = client.post(f'/post/{post.id}/{reply.id}/lock/{mode}',
                               data={'csrf_token': token})

    assert response.status_code == 302
    assert locked.call_args.args[1] is expected


@pytest.mark.parametrize('mode, expected', [('yes', True), ('no', False)])
def test_collapsing_a_comment(app, env, mode, expected):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    client = as_user(app, mod)
    token = csrf(app, client)

    with patch('app.post.routes.set_collapse_post_reply') as collapsed:
        response = client.post(f'/post/{post.id}/{reply.id}/collapse/{mode}',
                               data={'csrf_token': token})

    assert response.status_code == 302
    assert collapsed.call_args.args[1] is expected


@pytest.mark.parametrize('mode, expected', [('yes', True), ('no', False)])
def test_hiding_a_post(app, env, mode, expected):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.hide_post') as hidden:
        with patch('app.post.routes.flash') as flashed:
            response = client.post(f'/post/{post.id}/hide/{mode}',
                                   data={'csrf_token': token})

    assert response.status_code == 302
    assert hidden.call_args.args[1] is expected
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert ('has been hidden' if expected else 'has been un-hidden') in messages


def test_hiding_a_post_that_does_not_exist(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)
    token = csrf(app, client)

    assert client.post('/post/999999/hide/yes',
                       data={'csrf_token': token}).status_code == 404


@pytest.mark.parametrize('mode, expected', [('yes', True), ('no', False)])
def test_stickying_a_post(app, env, mode, expected):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, mod)
    token = csrf(app, client)

    with patch('app.post.routes.sticky_post') as stickied:
        response = client.post(f'/post/{post.id}/sticky/{mode}',
                               data={'csrf_token': token})

    assert response.status_code == 302
    assert stickied.call_args.args[1] is expected


@pytest.mark.parametrize('mode, expected', [('yes', True), ('no', False)])
def test_stickying_a_post_to_the_instance(app, env, mode, expected):
    """Instance-wide sticky is an admin's decision, not a moderator's: it puts
    a post on everybody's front page."""
    anon, community, post, mod, author, outsider = env
    client = as_user(app, db.session.get(User, 1))
    token = csrf(app, client)

    with patch('app.post.routes.flash'):
        response = client.post(f'/post/{post.id}/instance_sticky/{mode}',
                               data={'csrf_token': token})

    assert response.status_code == 302
    db.session.refresh(post)
    assert post.instance_sticky is expected


def test_a_moderator_cannot_sticky_to_the_instance(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, mod)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/instance_sticky/yes',
                           data={'csrf_token': token})

    assert response.status_code == 302
    db.session.refresh(post)
    assert post.instance_sticky is False


def test_an_unknown_post_cannot_be_stickied_to_the_instance(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, db.session.get(User, 1))
    token = csrf(app, client)

    assert client.post('/post/999999/instance_sticky/yes',
                       data={'csrf_token': token}).status_code == 404


def test_marking_a_post_read(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.mark_post_read') as marked:
        response = client.post(f'/post/{post.id}/set_read',
                               data={'csrf_token': token})

    assert response.status_code == 200
    assert marked.call_args.args[0] == [post.id]


def test_marking_a_post_read_is_one_way(app, env):
    """There is no un-read here: the route always marks read, whatever the
    query string says, because the front end only ever asks for the one
    direction."""
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.mark_post_read') as marked:
        client.post(f'/post/{post.id}/set_read?read=false',
                    data={'csrf_token': token})

    assert marked.call_args.args[1] is True


def test_flagging_a_post_as_ai(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, mod)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/set_ai',
                           data={'csrf_token': token})

    assert response.status_code == 200
    db.session.refresh(post)
    assert post.ai_generated is True


def test_a_stranger_cannot_flag_a_post_as_ai(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/set_ai',
                           data={'csrf_token': token})

    assert response.status_code == 401
    db.session.refresh(post)
    assert post.ai_generated is False


def test_choosing_an_answer(app, env):
    """A question's author, or a moderator, can mark one comment as the
    answer."""
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, outsider)
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.choose_answer') as chosen:
        response = client.post(f'/post_reply/{reply.id}/choose_answer',
                               data={'csrf_token': token})

    assert response.status_code == 200
    assert chosen.call_args.args[0] == reply.id


def test_unchoosing_an_answer(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, outsider, answer=True)
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.unchoose_answer') as unchosen:
        response = client.post(f'/post_reply/{reply.id}/unchoose_answer',
                               data={'csrf_token': token})

    assert response.status_code == 200
    assert unchosen.call_args.args[0] == reply.id


def test_a_stranger_cannot_choose_an_answer(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.choose_answer') as chosen:
        response = client.post(f'/post_reply/{reply.id}/choose_answer',
                               data={'csrf_token': token})

    assert response.status_code == 403
    assert chosen.call_args is None


def test_a_stranger_cannot_unchoose_an_answer(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author, answer=True)
    client = as_user(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.unchoose_answer') as unchosen:
        response = client.post(f'/post_reply/{reply.id}/unchoose_answer',
                               data={'csrf_token': token})

    assert response.status_code == 403
    assert unchosen.call_args is None


def test_cancelling_an_inline_reply(app, env):
    anon, community, post, mod, author, outsider = env
    reply = a_reply(post, author)

    response = anon.post(f'/comment/{reply.id}/cancel_inline')

    assert response.data == \
        f'<div id="reply_to_{reply.id}" class="hidable"></div>'.encode()


# --------------------------------------------------------------------------
# The admin-only routes: blocked images, and re-fetching a remote post
# --------------------------------------------------------------------------


def an_admin(app, user):
    grant_permission(user, 'change instance settings')
    return as_user(app, user)


def test_blocking_an_image(app, env):
    """Blocking an image stores its hash, so every future upload of the same
    picture is refused as well as the posts already carrying it."""
    anon, community, post, mod, author, outsider = env
    image = File(source_url='https://test.piefed.local/static/media/cat.jpg')
    db.session.add(image)
    db.session.commit()
    post.type = POST_TYPE_IMAGE
    post.url = 'https://test.piefed.local/static/media/cat.jpg'
    post.image_id = image.id
    db.session.commit()
    client = an_admin(app, outsider)
    token = csrf(app, client)

    # `BlockedImage.hash` is `BIT(256)`: anything that is not 256 binary
    # digits is `psycopg2.errors.InvalidTextRepresentation: "a" is not a valid
    # binary digit` (fact 523).
    perceptual_hash = '1010' * 64

    with patch('app.post.routes.retrieve_image_hash',
               return_value=perceptual_hash):
        with patch('app.post.routes.flash'):
            response = client.post(f'/post/{post.id}/block_image',
                                   data={'submit': 'Yes',
                                         'csrf_token': token})

    assert response.status_code == 302
    assert '/block_image_purge_posts' in response.headers['Location']
    blocked = BlockedImage.query.one()
    assert blocked.hash == perceptual_hash
    assert blocked.file_name == 'cat.jpg'


def test_an_image_whose_hash_cannot_be_read_is_not_blocked(app, env):
    """The hash is fetched from the stored file; if that fails there is
    nothing to block, and an empty hash would match everything."""
    anon, community, post, mod, author, outsider = env
    post.type = POST_TYPE_IMAGE
    post.url = 'https://test.piefed.local/static/media/cat.jpg'
    db.session.commit()
    client = an_admin(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.retrieve_image_hash', return_value=None):
        response = client.post(f'/post/{post.id}/block_image',
                               data={'submit': 'Yes', 'csrf_token': token})

    assert response.status_code == 302
    assert BlockedImage.query.count() == 0


def test_the_block_image_page_asks_first(app, env):
    anon, community, post, mod, author, outsider = env
    post.type = POST_TYPE_IMAGE
    db.session.commit()
    client = an_admin(app, outsider)

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        response = client.get(f'/post/{post.id}/block_image')

    assert response.status_code == 200
    assert 'block this image' in render.call_args.kwargs['title']
    assert BlockedImage.query.count() == 0


def test_a_post_that_is_not_an_image_has_nothing_to_block(app, env):
    anon, community, post, mod, author, outsider = env
    client = an_admin(app, outsider)

    response = client.get(f'/post/{post.id}/block_image')

    assert response.status_code == 302
    assert BlockedImage.query.count() == 0


def test_blocking_an_image_needs_the_permission(app, env):
    anon, community, post, mod, author, outsider = env
    post.type = POST_TYPE_IMAGE
    db.session.commit()

    assert as_user(app, outsider).get(
        f'/post/{post.id}/block_image').status_code == 302


def test_the_purge_page_lists_the_posts_carrying_blocked_images(app, env):
    anon, community, post, mod, author, outsider = env
    client = an_admin(app, outsider)

    with patch('app.post.routes.posts_with_blocked_images',
               return_value=[post.id]):
        with patch('app.post.routes.render_template',
                   return_value='rendered') as render:
            response = client.get(
                f'/post/{post.id}/block_image_purge_posts')

    assert response.status_code == 200
    assert [p.id for p in render.call_args.kwargs['posts']] == [post.id]


def test_a_deleted_post_is_not_listed_for_purging(app, env):
    anon, community, post, mod, author, outsider = env
    post.deleted = True
    db.session.commit()
    client = an_admin(app, outsider)

    with patch('app.post.routes.posts_with_blocked_images',
               return_value=[post.id]):
        with patch('app.post.routes.render_template',
                   return_value='rendered') as render:
            client.get(f'/post/{post.id}/block_image_purge_posts')

    assert render.call_args.kwargs['posts'] == []


def test_purging_the_posts(app, env):
    anon, community, post, mod, author, outsider = env
    client = an_admin(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.task_selector') as task:
        with patch('app.post.routes.flash') as flashed:
            response = client.post(
                f'/post/{post.id}/block_image_purge_posts',
                data={'post_ids': [str(post.id)], 'csrf_token': token})

    assert response.status_code == 302
    assert task.call_args.kwargs['post_ids'] == [str(post.id)]
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert '1 posts deleted' in messages


def test_purging_returns_to_where_it_started(app, env):
    """The referrer arrives from the hidden field the block-image form
    forwards, and is origin-checked like every other redirect target."""
    anon, community, post, mod, author, outsider = env
    client = an_admin(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.task_selector'):
        with patch('app.post.routes.flash'):
            response = client.post(
                f'/post/{post.id}/block_image_purge_posts?referrer=/c/general',
                data={'post_ids': [], 'csrf_token': token})

    assert response.headers['Location'] == '/c/general'


def test_purging_from_the_post_itself_goes_to_the_community(app, env):
    anon, community, post, mod, author, outsider = env
    client = an_admin(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.task_selector'):
        with patch('app.post.routes.flash'):
            response = client.post(
                f'/post/{post.id}/block_image_purge_posts?referrer=/post/{post.id}',
                data={'post_ids': [], 'csrf_token': token})

    assert '/c/' in response.headers['Location']


def test_purging_needs_the_permission(app, env):
    anon, community, post, mod, author, outsider = env

    assert as_user(app, outsider).get(
        f'/post/{post.id}/block_image_purge_posts').status_code == 302


def test_refetching_a_post_from_its_home_instance(app, env):
    """A link post that should have been an article arrives with a URL and an
    image; re-fetching it drops both and replays the remote object."""
    anon, community, post, mod, author, outsider = env
    image = File(source_url='https://remote.example/i/1.jpg')
    db.session.add(image)
    db.session.commit()
    post.ap_id = 'https://remote.example/post/1'
    post.url = 'https://remote.example/x'
    post.image_id = image.id
    post.domain_id = None
    db.session.commit()
    client = an_admin(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.get_request') as fetched:
        fetched.return_value.status_code = 200
        fetched.return_value.json.return_value = {'type': 'Page'}
        with patch('app.post.routes.update_post_from_activity') as updated:
            response = client.post(f'/post/{post.id}/fixup_from_remote',
                                   data={'csrf_token': token})

    assert response.status_code == 302
    db.session.refresh(post)
    assert post.url is None
    assert post.image_id is None
    assert File.query.count() == 0
    assert updated.call_args.args[1]['type'] == 'Update'


def test_refetching_a_post_that_has_no_image(app, env):
    anon, community, post, mod, author, outsider = env
    post.ap_id = 'https://remote.example/post/1'
    db.session.commit()
    client = an_admin(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.get_request') as fetched:
        fetched.return_value.status_code = 200
        fetched.return_value.json.return_value = {'type': 'Page'}
        with patch('app.post.routes.update_post_from_activity') as updated:
            response = client.post(f'/post/{post.id}/fixup_from_remote',
                                   data={'csrf_token': token})

    assert response.status_code == 302
    assert updated.call_args is not None


def test_a_remote_object_that_is_not_a_page_is_left_alone(app, env):
    anon, community, post, mod, author, outsider = env
    post.ap_id = 'https://remote.example/post/1'
    post.url = 'https://remote.example/x'
    db.session.commit()
    client = an_admin(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.get_request') as fetched:
        fetched.return_value.status_code = 200
        fetched.return_value.json.return_value = {'type': 'Note'}
        with patch('app.post.routes.update_post_from_activity') as updated:
            client.post(f'/post/{post.id}/fixup_from_remote',
                        data={'csrf_token': token})

    db.session.refresh(post)
    assert post.url == 'https://remote.example/x'
    assert updated.call_args is None


def test_a_remote_instance_that_refuses_leaves_the_post_alone(app, env):
    anon, community, post, mod, author, outsider = env
    post.ap_id = 'https://remote.example/post/1'
    post.url = 'https://remote.example/x'
    db.session.commit()
    client = an_admin(app, outsider)
    token = csrf(app, client)

    with patch('app.post.routes.get_request') as fetched:
        fetched.return_value.status_code = 404
        with patch('app.post.routes.update_post_from_activity') as updated:
            response = client.post(f'/post/{post.id}/fixup_from_remote',
                                   data={'csrf_token': token})

    assert response.status_code == 302
    db.session.refresh(post)
    assert post.url == 'https://remote.example/x'
    assert updated.call_args is None


def test_refetching_needs_the_permission(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, outsider)
    token = csrf(app, client)

    response = client.post(f'/post/{post.id}/fixup_from_remote',
                           data={'csrf_token': token})

    assert response.status_code == 302
    assert '/post/' not in response.headers['Location']


# --------------------------------------------------------------------------
# The preview endpoint
# --------------------------------------------------------------------------


@pytest.mark.parametrize('args, oob, target', [
    ({'type': 'post'}, 'post_preview_btn', '#preview'),
    ({'type': 'wiki'}, 'post_preview_btn', '#preview'),
    ({'type': 'comment', 'top': '1'},
     'top_comment_preview_', '#textarea_comment_to_preview'),
    ({'type': 'comment', 'id': '7'},
     'comment_preview_7', '#textarea_in_reply_to_preview_7'),
])
def test_the_preview_endpoint_addresses_the_right_element(app, env, args, oob,
                                                          target):
    """The preview is swapped in beside whatever asked for it, so the element
    ids are the whole contract."""
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    token = csrf(app, client)
    query = '&'.join(f'{k}={v}' for k, v in args.items())

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        response = client.post(f'/post_preview?{query}',
                               data={'body': '**bold**',
                                     'csrf_token': token})

    assert response.status_code == 200
    kwargs = render.call_args.kwargs
    assert kwargs['oob_target'] == oob
    assert kwargs['target_id'] == target


def test_the_preview_renders_the_markdown(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    token = csrf(app, client)

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        client.post('/post_preview?type=post',
                    data={'body': '**bold**', 'csrf_token': token})

    assert '<strong>bold</strong>' in render.call_args.kwargs['preview']


def test_a_nested_comment_preview_is_spaced(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    token = csrf(app, client)

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        client.post('/post_preview?type=comment&id=7',
                    data={'body': 'x', 'csrf_token': token})

    assert render.call_args.kwargs['additional_classes'] == 'mt-2'


def test_a_top_level_comment_preview_is_not(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    token = csrf(app, client)

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        client.post('/post_preview?type=comment&top=1',
                    data={'body': 'x', 'csrf_token': token})

    assert render.call_args.kwargs['additional_classes'] == ''


def test_a_preview_of_nothing(app, env):
    """The body is a textarea, and an empty one is what the first keystroke
    replaces -- not an error."""
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    token = csrf(app, client)

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        response = client.post('/post_preview?type=post',
                               data={'csrf_token': token})

    assert response.status_code == 200
    assert render.call_args.kwargs['preview'] == ''


def test_a_preview_with_no_type(app, env):
    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    token = csrf(app, client)

    with patch('app.post.routes.render_template',
               return_value='rendered') as render:
        response = client.post('/post_preview',
                               data={'body': 'x', 'csrf_token': token})

    assert response.status_code == 200
    assert render.call_args.kwargs['oob_target'] == ''
