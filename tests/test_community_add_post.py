"""`add_post` -- the posting surface.

Sub-project 80, slice E. Three defects:

* the cross-post form copied a source post's title, body, url and tags into the
  new-post form with **no visibility check**, so a post in a private community
  could be read by anyone who knew its id (D998);
* that same block dereferenced a `None` source post, and `add_post` itself
  dereferenced a `None` community -- D992's shape, fourth and fifth instances
  in this file (D999);
* the failure path flashed `str(ex)` from `make_post`, which reaches image
  processing, remote fetches and plugin hooks (D1000).

A correction worth recording: `if not (community.is_moderator() or ...):
form.sticky.render_kw = {'disabled': True}` looks like an authorization bypass,
because `render_kw` is only a rendering hint and a client can submit the field
anyway. It is not one -- `make_post` guards the assignment itself at
`app/shared/post.py:398`. The probe came before the claim.
"""
import io
from unittest.mock import patch

import pytest

from app import db
from app.models import Community, Language, Post, Site
from tests.factories import (make_community, make_community_member,
                             make_instance, make_user)

pytestmark = pytest.mark.usefixtures('site')

POST_TYPE_LINK = 1


def instance(domain='test.piefed.local', software='piefed'):
    """Fact 394."""
    from app.models import Instance

    existing = Instance.query.filter_by(domain=domain).first()
    return existing if existing is not None else make_instance(domain,
                                                               software=software)


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


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


def url(app, endpoint, **values):
    from flask import url_for

    with app.test_request_context():
        return url_for(endpoint, **values)


def able_to_post(user):
    """`add_post` carries validation_required and approval_required behind
    login_required, so an unverified account with no private_key is redirected
    before the route runs -- a 302 that looks like whatever refusal the row was
    testing for. Same trap as slice C's `able_to_create`."""
    user.verified = True
    user.private_key = 'a private key'
    db.session.commit()
    return user


@pytest.fixture
def poster(app, db_session):
    local = instance()
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1  # fact 347
    author = make_user(local, 'author', local=True)
    able_to_post(author)
    community = make_community('general')
    db.session.add(Language(code='und', name='Undetermined'))
    english = Language(code='en', name='English')
    db.session.add(english)
    db.session.commit()
    make_community_member(author, community)
    db.session.commit()

    client = app.test_client()
    login(client, author)
    return client, csrf(app, client), community, author, english


def _post_payload(token, english, community, **overrides):
    """`communities` is a DataRequired SelectField fed from
    `possible_communities()`, and `timezone` is one fed from `get_timezones()`
    -- which groups by region, so 'UTC' is not a member and a bare 'UTC' is
    `Not a valid choice.`"""
    data = {'title': 'A new post', 'body': 'the body',
            'communities': str(community.id),
            'language_id': str(english.id), 'notify_author': 'y',
            'timezone': 'Europe/London', 'submit': 'Save', 'csrf_token': token}
    data.update(overrides)
    return data


# --------------------------------------------------------------------------
# D998: the cross-post source must be visible to the caller
# --------------------------------------------------------------------------


def _private_post(author_id, title='Private plans', body='the secret body'):
    """A post in a private, local-only community the caller is not in."""
    secret = make_community('secret')
    secret.private = True
    secret.local_only = True
    db.session.commit()
    hidden = Post(user_id=author_id, community_id=secret.id, title=title,
                  body=body, url='https://secret.example/x',
                  ap_id='https://test.piefed.local/post/secret')
    db.session.add(hidden)
    db.session.commit()
    return secret, hidden


def test_a_private_posts_content_is_not_leaked_through_the_crosspost_form(
        app, poster):
    """D998's pin, inverted.

    The cross-post block loads `db.session.get(Post, request.args.get('source'))`
    and copies its title, body, url and tags into the form. Nothing checked who
    was asking, so a post in a private community could be read by anyone who
    knew its id, from the ordinary new-post page of a community they CAN post
    to. Measured:

        PROBE q1 nosy is a member of the private community? []
        PROBE q1 title leaked: 'Private plans'
        PROBE q1 body leaked: 'the secret body'
        PROBE q1 url leaked: 'https://secret.example/x'

    The condition now used is the one `app/post/routes.py:102` already applies
    to the post page itself -- the check existed, one blueprint over, and this
    path never called it.
    """
    client, token, community, author, english = poster
    secret, hidden = _private_post(author_id=1)
    assert not secret.is_member(author)

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        response = client.get(url(app, 'community.add_post',
                                  actor=community.name, type='link')
                              + f'?source={hidden.id}')

    assert response.status_code == 403
    assert render.call_args_list == []


def test_a_member_of_the_private_community_can_still_crosspost_from_it(
        app, poster):
    """The control. A check that refuses everyone is not the fix -- somebody
    who is in the private community is exactly who cross-posting is for."""
    client, token, community, author, english = poster
    secret, hidden = _private_post(author_id=author.id)
    make_community_member(author, secret)
    db.session.commit()

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        response = client.get(url(app, 'community.add_post',
                                  actor=community.name, type='link')
                              + f'?source={hidden.id}')

    assert response.status_code == 200
    form = render.call_args.kwargs['form']
    assert form.title.data == 'Private plans'
    assert form.link_url.data == 'https://secret.example/x'


def test_crossposting_from_a_public_post_still_works(app, poster):
    """The ordinary case, which is most of them: a public community's post is
    copied into the form for another community."""
    client, token, community, author, english = poster
    elsewhere = make_community('elsewhere')
    db.session.commit()
    source = Post(user_id=author.id, community_id=elsewhere.id,
                  title='Worth sharing', body='the body',
                  url='https://example.com/article',
                  ap_id='https://test.piefed.local/post/worth')
    db.session.add(source)
    db.session.commit()

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        response = client.get(url(app, 'community.add_post',
                                  actor=community.name, type='link')
                              + f'?source={source.id}')

    assert response.status_code == 200
    form = render.call_args.kwargs['form']
    assert form.title.data == 'Worth sharing'
    assert form.body.data == 'the body'
    assert form.link_url.data == 'https://example.com/article'


def test_crossposting_a_video_copies_the_url_into_the_video_field(app, poster):
    """`elif post_type == POST_TYPE_VIDEO:` -- the same block fills a different
    field, and a row for only the link type would not notice."""
    client, token, community, author, english = poster
    elsewhere = make_community('elsewhere')
    db.session.commit()
    source = Post(user_id=author.id, community_id=elsewhere.id,
                  title='A video', body='', url='https://example.com/v.mp4',
                  ap_id='https://test.piefed.local/post/video')
    db.session.add(source)
    db.session.commit()

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        client.get(url(app, 'community.add_post', actor=community.name,
                       type='video') + f'?source={source.id}')

    assert render.call_args.kwargs['form'].video_url.data == 'https://example.com/v.mp4'


def test_crossposting_a_deleted_post_is_a_404(app, poster):
    """`if source_post.deleted:` -- a deleted post's content must not come back
    through this door either."""
    client, token, community, author, english = poster
    elsewhere = make_community('elsewhere')
    db.session.commit()
    source = Post(user_id=author.id, community_id=elsewhere.id, title='Gone',
                  body='', url='https://example.com/x', deleted=True,
                  ap_id='https://test.piefed.local/post/gone')
    db.session.add(source)
    db.session.commit()

    response = client.get(url(app, 'community.add_post', actor=community.name,
                              type='link') + f'?source={source.id}')

    assert response.status_code == 404


# --------------------------------------------------------------------------
# D999: two more nil dereferences
# --------------------------------------------------------------------------


def test_a_source_id_that_does_not_exist_is_a_404(app, poster):
    """D992's shape, fifth instance in this file -- and the first on a QUERY
    PARAMETER rather than a path segment. A stale cross-post link was
    `AttributeError: 'NoneType' object has no attribute 'deleted'`."""
    client, token, community, author, english = poster

    response = client.get(url(app, 'community.add_post', actor=community.name,
                              type='link') + '?source=999999')

    assert response.status_code == 404


def test_an_unresolvable_community_is_a_404(app, poster):
    """D992's fourth instance: `actor_to_community` returns None and the next
    lines read `community.default_post_type` and `community.nsfw`. Measured as
    `AttributeError: 'NoneType' object has no attribute 'nsfw'`."""
    client, token, community, author, english = poster

    response = client.get(url(app, 'community.add_post',
                              actor='no-such-community', type='discussion'))

    assert response.status_code == 404


def test_an_unknown_post_type_is_a_404(app, poster):
    """The `else: abort(404)` on the type dispatch -- `type` comes from the
    URL."""
    client, token, community, author, english = poster

    response = client.get(url(app, 'community.add_post', actor=community.name,
                              type='not-a-type'))

    assert response.status_code == 404


# --------------------------------------------------------------------------
# D1000: the failure path must not echo the exception
# --------------------------------------------------------------------------


def test_a_failed_post_does_not_put_the_exception_on_the_page(app, poster,
                                                              caplog):
    """D1000's pin, inverted.

    `make_post` reaches image processing, remote fetches and the plugin hooks,
    so `str(ex)` can name a path, a relay or a library internal -- and it went
    straight onto the page. Same class as D895 and D950. Fact 393: `caplog`,
    not a patched `current_app`.
    """
    client, token, community, author, english = poster
    secret = '/srv/piefed/media/tmp/upload-4f2a.png: Permission denied'

    with patch('app.community.routes.make_post', side_effect=RuntimeError(secret)):
        with patch('app.community.routes.flash') as flashed:
            with patch('app.community.routes.render_template', return_value='rendered'):
                client.post(url(app, 'community.add_post',
                                actor=community.name, type='discussion'),
                            data=_post_payload(token, english, community),
                            content_type='multipart/form-data')

    flashed_text = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert flashed_text, 'the form did not validate, so make_post was never reached'
    assert secret not in flashed_text
    assert 'server log' in flashed_text
    # The detail is not lost -- the operator can read it.
    assert secret in caplog.text


# --------------------------------------------------------------------------
# The sticky field, which is NOT a bypass
# --------------------------------------------------------------------------


def test_a_non_moderator_cannot_sticky_their_own_post(app, poster):
    """`render_kw={'disabled': True}` is a RENDERING hint -- a client can
    submit the field regardless -- so this row exists to record that the real
    check is elsewhere and works.

    `make_post` guards the assignment itself: `if post.community.is_moderator(
    user) or post.community.is_owner(user) or user.is_admin(): post.sticky =
    ...` (app/shared/post.py:398). Without that row somebody reading
    `add_post` alone would reasonably conclude this was a bypass, which is what
    happened while writing this slice.
    """
    client, token, community, author, english = poster
    assert not community.is_moderator(author)

    with patch('app.community.routes.render_template', return_value='rendered'):
        client.post(url(app, 'community.add_post', actor=community.name,
                        type='discussion'),
                    data=_post_payload(token, english, community, title='Not pinned',
                                       sticky='y'),
                    content_type='multipart/form-data')

    post = Post.query.filter_by(title='Not pinned').first()
    assert post is not None
    assert post.sticky is False


def test_a_moderator_can_sticky_their_own_post(app, poster):
    """The other half, and the reason the field exists at all."""
    from app.models import CommunityMember

    client, token, community, author, english = poster
    # The fixture already made them a member -- promote that row rather than
    # inserting a second, which the (user_id, community_id) primary key
    # refuses.
    membership = CommunityMember.query.filter_by(community_id=community.id,
                                                 user_id=author.id).one()
    membership.is_moderator = True
    db.session.commit()

    with patch('app.community.routes.sticky_post') as sticky:
        with patch('app.community.routes.render_template', return_value='rendered'):
            client.post(url(app, 'community.add_post', actor=community.name,
                            type='discussion'),
                        data=_post_payload(token, english, community, title='Pinned',
                                           sticky='y'),
                        content_type='multipart/form-data')

    post = Post.query.filter_by(title='Pinned').first()
    assert post is not None and post.sticky is True
    # Stickiness federates separately from the post itself.
    assert sticky.call_args.args == (post.id, True, 1)


# --------------------------------------------------------------------------
# The rest of the form
# --------------------------------------------------------------------------


@pytest.mark.parametrize('type_name', ['discussion', 'link', 'image', 'video',
                                       'poll', 'event'])
def test_each_post_type_renders_its_own_form(app, poster, type_name):
    """The six-way type dispatch. Each arm builds a different form class, and
    a row for one says nothing about the others."""
    client, token, community, author, english = poster

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        response = client.get(url(app, 'community.add_post',
                                  actor=community.name, type=type_name))

    assert response.status_code == 200
    assert render.call_args.kwargs['form'] is not None


def test_the_default_post_type_is_used_when_none_is_given(app, poster):
    """`if type is None: type = community.default_post_type or 'link'` -- the
    community chooses what its new-post page opens on."""
    client, token, community, author, english = poster
    community.default_post_type = 'discussion'
    db.session.commit()

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        client.get(url(app, 'community.add_post', actor=community.name))

    from app.constants import POST_TYPE_ARTICLE
    assert render.call_args.kwargs['post_type'] == POST_TYPE_ARTICLE


def test_a_community_with_no_default_falls_back_to_link(app, poster):
    """The `or 'link'` half."""
    client, token, community, author, english = poster
    community.default_post_type = None
    db.session.commit()

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        client.get(url(app, 'community.add_post', actor=community.name))

    from app.constants import POST_TYPE_LINK as LINK
    assert render.call_args.kwargs['post_type'] == LINK


@pytest.mark.parametrize('column, field', [('nsfw', 'nsfw'), ('nsfl', 'nsfl'),
                                           ('ai_generated', 'ai_generated')])
def test_a_communitys_content_flags_are_forced_on(app, poster, column, field):
    """A community marked NSFW, NSFL or AI-generated pre-sets and disables the
    matching checkbox, so every post inherits it. The `render_kw` disable is a
    rendering hint only -- `make_post` is what actually applies `post.nsfw =
    nsfw or post.community.nsfw`."""
    client, token, community, author, english = poster
    setattr(community, column, True)
    db.session.commit()

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        client.get(url(app, 'community.add_post', actor=community.name,
                       type='discussion'))

    form = render.call_args.kwargs['form']
    assert getattr(form, field).data is True
    assert getattr(form, field).render_kw == {'disabled': True} or \
        form.nsfw.render_kw == {'disabled': True}


def test_a_community_with_flair_offers_it(app, poster):
    """`if len(flair_choices): ... else: del form.flair` -- a community with no
    flair must not render an empty select, and one with flair must offer it."""
    from app.models import CommunityFlair

    client, token, community, author, english = poster
    db.session.add(CommunityFlair(community_id=community.id, flair='Question',
                                  text_color='#000000',
                                  background_color='#ffffff'))
    db.session.commit()

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        client.get(url(app, 'community.add_post', actor=community.name,
                       type='discussion'))

    form = render.call_args.kwargs['form']
    assert form.flair is not None
    assert form.flair.choices == [(0, ''), (1, 'Question')] or \
        ('Question' in [label for _value, label in form.flair.choices])


def test_a_community_without_flair_drops_the_field(app, poster):
    """The `del form.flair` arm. WTForms' `Form.__delattr__` pops the field out
    of `_fields` and then sets the attribute to None, so the field is gone from
    rendering while `hasattr(form, 'flair')` is still True -- the assertion has
    to be on the value, not on the attribute."""
    client, token, community, author, english = poster

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        client.get(url(app, 'community.add_post', actor=community.name,
                       type='discussion'))

    form = render.call_args.kwargs['form']
    assert form.flair is None
    assert 'flair' not in form._fields


def test_a_poll_opens_with_a_three_day_default(app, poster):
    """`if post_type == POST_TYPE_POLL: form.finish_in.data = '3d'` -- a poll
    with no end date never closes."""
    client, token, community, author, english = poster

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        client.get(url(app, 'community.add_post', actor=community.name,
                       type='poll'))

    assert render.call_args.kwargs['form'].finish_in.data == '3d'


def test_an_event_opens_online_in_the_users_timezone(app, poster):
    """The event arm's two defaults."""
    client, token, community, author, english = poster
    author.timezone = 'Europe/London'
    db.session.commit()

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        client.get(url(app, 'community.add_post', actor=community.name,
                       type='event'))

    form = render.call_args.kwargs['form']
    assert form.online.data is True
    assert form.event_timezone.data == 'Europe/London'


@pytest.mark.parametrize('owner, attribute', [('community', 'posting_warning'),
                                              ('instance', 'posting_warning')])
def test_a_posting_warning_is_shown(app, poster, owner, attribute):
    """Both warnings -- the community's and its instance's. They are separate
    fields and a row for one would not notice the other going missing."""
    client, token, community, author, english = poster
    target = community if owner == 'community' else community.instance
    setattr(target, attribute, 'Read the rules first')
    db.session.commit()

    with patch('app.community.routes.flash') as flashed:
        with patch('app.community.routes.render_template', return_value='rendered'):
            client.get(url(app, 'community.add_post', actor=community.name,
                           type='discussion'))

    assert 'Read the rules first' in [call.args[0] for call in flashed.call_args_list]


@pytest.mark.parametrize('type_name, field', [('link', 'link_url'),
                                              ('video', 'video_url')])
def test_a_link_query_parameter_prefills_the_form(app, poster, type_name,
                                                  field):
    """`request.args.get('link')` -- the bookmarklet and share targets arrive
    this way, with the title alongside."""
    client, token, community, author, english = poster

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        client.get(url(app, 'community.add_post', actor=community.name,
                       type=type_name)
                   + '?link=https://example.com/x&title=Shared')

    form = render.call_args.kwargs['form']
    assert getattr(form, field).data == 'https://example.com/x'
    assert form.title.data == 'Shared'


def test_a_post_in_an_unexpected_language_warns(app, poster):
    """`if form.language_id.data not in community.language_ids()` -- the
    community lists the languages it wants, and the warning is advisory rather
    than a refusal."""
    client, token, community, author, english = poster
    french = Language(code='fr', name='French')
    db.session.add(french)
    db.session.commit()
    community.languages.append(french)
    db.session.commit()
    author.language_id = english.id
    db.session.commit()

    with patch('app.community.routes.flash') as flashed:
        with patch('app.community.routes.render_template', return_value='rendered'):
            client.get(url(app, 'community.add_post', actor=community.name,
                           type='discussion'))

    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'prefers posts in' in messages


def test_a_community_that_only_accepts_undetermined_does_not_warn(app, poster):
    """`if not (len(...) == 1 and ...[0] == 1)` -- language id 1 is always
    "Undetermined", and a community that lists only that has expressed no
    preference at all."""
    client, token, community, author, english = poster

    with patch('app.community.routes.flash') as flashed:
        with patch('app.community.routes.render_template', return_value='rendered'):
            client.get(url(app, 'community.add_post', actor=community.name,
                           type='discussion'))

    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'prefers posts in' not in messages


def test_a_banned_user_cannot_post(app, poster):
    client, token, community, author, english = poster
    author.banned = True
    db.session.commit()

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        client.get(url(app, 'community.add_post', actor=community.name,
                       type='discussion'))

    assert render.call_args_list == []


def test_a_user_banned_from_posting_cannot_post(app, poster):
    """`current_user.ban_posts` is a separate sanction from `banned` -- an
    account that may still read and comment but not post."""
    client, token, community, author, english = poster
    author.ban_posts = True
    db.session.commit()

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        client.get(url(app, 'community.add_post', actor=community.name,
                       type='discussion'))

    assert render.call_args_list == []


def test_a_successful_post_clears_the_draft_cookies(app, poster):
    """The route keeps the title and body in cookies while the user switches
    post type, and has to clear them on success or the next new post starts
    pre-filled with the last one."""
    client, token, community, author, english = poster

    with patch('app.community.routes.render_template', return_value='rendered'):
        response = client.post(url(app, 'community.add_post',
                                   actor=community.name, type='discussion'),
                               data=_post_payload(token, english, community,
                                                  title='Draft cleared'),
                               content_type='multipart/form-data')

    assert response.status_code == 302
    cleared = ' '.join(response.headers.getlist('Set-Cookie'))
    for cookie in ('post_title', 'post_description', 'post_tags'):
        assert cookie in cleared


def test_posting_to_a_community_chosen_in_the_form_uses_that_community(app,
                                                                        poster):
    """`if request.form.get('communities')` -- the form has a community
    selector, so the POST target can differ from the actor in the URL."""
    client, token, community, author, english = poster
    elsewhere = make_community('elsewhere')
    db.session.commit()
    make_community_member(author, elsewhere)
    db.session.commit()

    with patch('app.community.routes.render_template', return_value='rendered'):
        client.post(url(app, 'community.add_post', actor=community.name,
                        type='discussion'),
                    data=_post_payload(token, english, elsewhere,
                                       title='Went elsewhere'),
                    content_type='multipart/form-data')

    post = Post.query.filter_by(title='Went elsewhere').first()
    assert post is not None
    assert post.community_id == elsewhere.id


# --------------------------------------------------------------------------
# The upload dispatch and the failure path
# --------------------------------------------------------------------------


class FakePost:
    """`make_post` is patched out for the rows below, which are about what the
    route hands it rather than what it does. The route reads three attributes
    off the result, and a MagicMock's `id` reaches `session.get(Post, ...)` as
    `InvalidRequestError: Incorrect number of values in identifier` -- a
    failure of the double, not of the route."""

    sticky = False
    slug = '/post/1'
    id = 1


def _type_extras(type_name):
    """Each post type's own required fields. Without them the form refuses the
    submission and `make_post` is never reached, which looks exactly like the
    dispatch being wrong."""
    if type_name == 'image':
        return {}
    if type_name == 'video':
        return {'video_url': 'https://example.com/v.mp4'}
    if type_name == 'poll':
        return {'mode': 'single', 'finish_in': '3d',
                'choice_1': 'Yes', 'choice_2': 'No'}
    if type_name == 'link':
        return {'link_url': 'https://example.com/a'}
    if type_name == 'event':
        return {'start_datetime': '2099-01-01T10:00',
                'end_datetime': '2099-01-01T12:00',
                'event_timezone': 'Europe/London', 'online': 'y',
                'online_link': 'https://example.com/call',
                'max_attendees': '10', 'join_mode': 'free'}
    return {}


def test_a_post_without_a_community_field_falls_back_to_the_actor(app, poster):
    """The POST branch reads `request.form.get('communities')` first and only
    falls back to the actor in the URL when it is absent. `communities` carries
    DataRequired, so the form refuses the submission -- but the fallback runs
    before validation, and a `None` community there is D999's shape again."""
    client, token, community, author, english = poster
    payload = _post_payload(token, english, community)
    del payload['communities']

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        response = client.post(url(app, 'community.add_post',
                                   actor=community.name, type='discussion'),
                               data=payload,
                               content_type='multipart/form-data')

    assert response.status_code == 200
    assert render.call_args.kwargs['community'].id == community.id


@pytest.mark.parametrize('type_name', ['image', 'event', 'video'])
def test_the_upload_types_pass_the_file_through(app, poster, type_name):
    """`if type == 'image' or type == 'event': uploaded_file =
    request.files['image_file']` -- three of the six types carry an upload and
    the other three must pass None, or `make_post` stores nothing for a post
    the user attached a file to."""
    client, token, community, author, english = poster
    payload = _post_payload(token, english, community, title='With a file',
                            **_type_extras(type_name))
    payload['image_file'] = (io.BytesIO(b'not really a png'), 'x.png')

    with patch('app.community.routes.make_post', return_value=FakePost()) as make:
        with patch('app.community.routes.can_upload_video', return_value=True):
            with patch('app.community.routes.render_template', return_value='rendered') as render:
                client.post(url(app, 'community.add_post',
                                actor=community.name, type=type_name),
                            data=payload, content_type='multipart/form-data')

    assert make.call_args is not None, render.call_args.kwargs['form'].errors
    assert make.call_args.kwargs['uploaded_file'].filename == 'x.png'


def test_a_before_post_create_plugin_can_rewrite_the_title_and_body(app, poster):
    """D811, fixed (owner ruling 2026-09-30): the web call site now uses
    `fire_hook`'s returned title and content for the post it creates, as the
    API's does, rather than discarding them."""
    client, token, community, author, english = poster

    def rewrite(hook_name, data=None, **kwargs):
        if hook_name != 'before_post_create':
            return data
        return {**data, 'title': 'rewritten title', 'content': 'rewritten body'}

    with patch('app.plugins.fire_hook', side_effect=rewrite), \
            patch('app.community.routes.make_post', return_value=FakePost()) as make:
        with patch('app.community.routes.render_template', return_value='rendered') as render:
            client.post(url(app, 'community.add_post', actor=community.name, type='discussion'),
                        data=_post_payload(token, english, community, title='original'),
                        content_type='multipart/form-data')

    assert make.call_args is not None, render.call_args.kwargs['form'].errors
    form = make.call_args.args[0]
    assert form.title.data == 'rewritten title'
    assert form.body.data == 'rewritten body'


def test_a_video_post_without_upload_permission_gets_no_file(app, poster):
    """`elif type == 'video' and can_upload_video()` -- the permission is half
    of the condition, so an account without it posts a video by URL only and
    the file it attached is dropped."""
    client, token, community, author, english = poster
    payload = _post_payload(token, english, community, title='No video upload',
                            **_type_extras('video'))
    payload['image_file'] = (io.BytesIO(b'not really a png'), 'x.png')

    with patch('app.community.routes.make_post', return_value=FakePost()) as make:
        with patch('app.community.routes.can_upload_video', return_value=False):
            with patch('app.community.routes.render_template', return_value='rendered') as render:
                client.post(url(app, 'community.add_post',
                                actor=community.name, type='video'),
                            data=payload, content_type='multipart/form-data')

    assert make.call_args is not None, render.call_args.kwargs['form'].errors
    assert make.call_args.kwargs['uploaded_file'] is None


@pytest.mark.parametrize('type_name', ['discussion', 'link', 'poll'])
def test_the_other_types_pass_no_file(app, poster, type_name):
    """The `else: uploaded_file = None` arm, for each type that reaches it."""
    client, token, community, author, english = poster

    with patch('app.community.routes.make_post', return_value=FakePost()) as make:
        with patch('app.community.routes.render_template', return_value='rendered') as render:
            client.post(url(app, 'community.add_post', actor=community.name,
                            type=type_name),
                        data=_post_payload(token, english, community,
                                           title='No file here',
                                           **_type_extras(type_name)),
                        content_type='multipart/form-data')

    assert make.call_args is not None, render.call_args.kwargs['form'].errors
    assert make.call_args.kwargs['uploaded_file'] is None


def test_a_failure_is_logged_and_the_reason_is_not_shown(app, poster):
    """D1000. `make_post` reaches image processing, remote fetches and the
    plugin hooks, so `str(ex)` can name a filesystem path, a relay host or a
    library internal. The user gets a fixed message; the detail goes to the
    log."""
    client, token, community, author, english = poster

    with patch('app.community.routes.make_post',
               side_effect=RuntimeError('/srv/piefed/media/tmp/secret.png')):
        with patch('app.community.routes.flash') as flashed:
            with patch('app.community.routes.render_template', return_value='rendered'):
                response = client.post(url(app, 'community.add_post',
                                           actor=community.name,
                                           type='discussion'),
                                       data=_post_payload(token, english,
                                                          community),
                                       content_type='multipart/form-data')

    assert response.status_code == 302
    shown = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'secret.png' not in shown
    assert 'server log' in shown


def test_a_failure_in_debug_mode_raises(app, poster):
    """`if current_app.debug: raise ex` -- a developer wants the traceback, and
    the redirect below would swallow it."""
    client, token, community, author, english = poster
    app.debug = True
    try:
        with patch('app.community.routes.make_post',
                   side_effect=RuntimeError('boom')):
            with patch('app.community.routes.render_template', return_value='rendered'):
                with pytest.raises(RuntimeError, match='boom'):
                    client.post(url(app, 'community.add_post',
                                    actor=community.name, type='discussion'),
                                data=_post_payload(token, english, community),
                                content_type='multipart/form-data')
    finally:
        app.debug = False


# --------------------------------------------------------------------------
# D1001-D1003
# --------------------------------------------------------------------------


def test_an_instance_that_refuses_local_image_posts_refuses_them(app, poster):
    """D1001. `CreateImageForm.validate` appended 'Images cannot be posted to
    local communities.' to `communities.errors` and then returned True, so the
    site setting recorded a complaint and accepted the image anyway. Measured
    before the fix: `make_post called? True`."""
    client, token, community, author, english = poster
    site = db.session.get(Site, 1)
    site.allow_local_image_posts = False
    db.session.commit()
    assert community.is_local()

    payload = _post_payload(token, english, community, title='An image post')
    payload['image_file'] = (io.BytesIO(b'x' * 32), 'photo.png')

    with patch('app.community.routes.make_post', return_value=FakePost()) as make:
        with patch('app.community.routes.render_template', return_value='rendered') as render:
            response = client.post(url(app, 'community.add_post',
                                       actor=community.name, type='image'),
                                   data=payload,
                                   content_type='multipart/form-data')

    assert response.status_code == 200
    assert make.call_args is None
    assert render.call_args.kwargs['form'].communities.errors != []


def test_an_instance_that_allows_local_image_posts_accepts_them(app, poster):
    """The other side of D1001's condition. A `return False` that fires
    unconditionally would pass the row above."""
    client, token, community, author, english = poster
    site = db.session.get(Site, 1)
    site.allow_local_image_posts = True
    db.session.commit()

    payload = _post_payload(token, english, community, title='An image post')
    payload['image_file'] = (io.BytesIO(b'x' * 32), 'photo.png')

    with patch('app.community.routes.make_post', return_value=FakePost()) as make:
        with patch('app.community.routes.render_template', return_value='rendered'):
            client.post(url(app, 'community.add_post', actor=community.name,
                            type='image'), data=payload,
                        content_type='multipart/form-data')

    assert make.call_args is not None


@pytest.mark.parametrize('field, submitted, stored', [
    ('language_id', 'french', 'english'),
    ('timezone', 'Europe/London', 'Europe/Paris'),
])
def test_a_refused_post_keeps_what_was_typed(app, poster, field, submitted,
                                             stored):
    """D1002 -- D907's shape, the fifth instance. The POST branch ended with
    `else:` rather than `elif request.method == 'GET':`, so a submission the
    form refused was overwritten from the database before being redisplayed."""
    client, token, community, author, english = poster
    french = Language(code='fr', name='French')
    db.session.add(french)
    db.session.commit()
    author.language_id = english.id
    author.timezone = 'Europe/Paris'
    db.session.commit()

    payload = _post_payload(token, english, community, title='x')  # too short
    payload['language_id'] = str(french.id)
    payload['timezone'] = 'Europe/London'

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        response = client.post(url(app, 'community.add_post',
                                   actor=community.name, type='discussion'),
                               data=payload,
                               content_type='multipart/form-data')

    assert response.status_code == 200
    form = render.call_args.kwargs['form']
    expected = french.id if submitted == 'french' else submitted
    assert getattr(form, field).data == expected


def test_a_refused_post_keeps_a_cleared_checkbox(app, poster):
    """The same overwrite, on the field where it is least visible: the GET
    branch sets `notify_author` to True, so clearing it and having the form
    refused turned notifications back on."""
    client, token, community, author, english = poster
    payload = _post_payload(token, english, community, title='x')
    del payload['notify_author']

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        client.post(url(app, 'community.add_post', actor=community.name,
                        type='discussion'),
                    data=payload, content_type='multipart/form-data')

    assert render.call_args.kwargs['form'].notify_author.data is False


def test_poll_choices_past_the_ninth_are_counted(app, poster):
    """D1003. `CreatePollForm.validate` counted `range(1, 10)` of fifteen
    fields while `make_post` reads `range(1, 16)`, so a poll whose options
    were typed into choices 10-15 was refused for having none."""
    client, token, community, author, english = poster
    payload = _post_payload(token, english, community, title='A poll',
                            mode='single', finish_in='3d')
    for i in range(10, 16):
        payload[f'choice_{i}'] = f'Option {i}'

    with patch('app.community.routes.make_post', return_value=FakePost()) as make:
        with patch('app.community.routes.render_template', return_value='rendered') as render:
            client.post(url(app, 'community.add_post', actor=community.name,
                            type='poll'), data=payload,
                        content_type='multipart/form-data')

    assert make.call_args is not None, render.call_args.kwargs['form'].errors


def test_a_poll_with_one_choice_is_still_refused(app, poster):
    """The counting fix must not turn the minimum off. One choice, in a field
    the old count could not see, is still one choice."""
    client, token, community, author, english = poster
    payload = _post_payload(token, english, community, title='A poll',
                            mode='single', finish_in='3d', choice_12='Only')

    with patch('app.community.routes.make_post', return_value=FakePost()) as make:
        with patch('app.community.routes.render_template', return_value='rendered') as render:
            client.post(url(app, 'community.add_post', actor=community.name,
                            type='poll'), data=payload,
                        content_type='multipart/form-data')

    assert make.call_args is None
    assert render.call_args.kwargs['form'].choice_2.errors != []


def test_a_poll_with_no_choices_is_refused(app, poster):
    """The `choices_made == 0` arm, which reports a different field."""
    client, token, community, author, english = poster
    payload = _post_payload(token, english, community, title='A poll',
                            mode='single', finish_in='3d')

    with patch('app.community.routes.make_post', return_value=FakePost()) as make:
        with patch('app.community.routes.render_template', return_value='rendered') as render:
            client.post(url(app, 'community.add_post', actor=community.name,
                            type='poll'), data=payload,
                        content_type='multipart/form-data')

    assert make.call_args is None
    assert render.call_args.kwargs['form'].choice_1.errors != []


def test_a_poll_submission_that_omits_a_choice_field_is_not_a_500(app, poster):
    """D1003's second half. WTForms leaves an unsubmitted StringField at None,
    not '', so `.data.strip()` was `AttributeError: 'NoneType' object has no
    attribute 'strip'` for any submission that did not carry all fifteen
    fields -- which the browser form does and nothing else has to."""
    client, token, community, author, english = poster
    payload = _post_payload(token, english, community, title='A poll',
                            mode='single', finish_in='3d',
                            choice_1='Yes', choice_2='No')

    with patch('app.community.routes.make_post', return_value=FakePost()) as make:
        with patch('app.community.routes.render_template', return_value='rendered'):
            client.post(url(app, 'community.add_post', actor=community.name,
                            type='poll'), data=payload,
                        content_type='multipart/form-data')

    assert make.call_args is not None


def test_a_poll_may_not_repeat(app, poster):
    """`if self.repeat.data in ['daily', 'weekly', 'monthly']` -- the one arm
    of CreatePollForm.validate that is not about choices."""
    client, token, community, author, english = poster
    payload = _post_payload(token, english, community, title='A poll',
                            mode='single', finish_in='3d', repeat='weekly',
                            choice_1='Yes', choice_2='No')

    with patch('app.community.routes.make_post', return_value=FakePost()) as make:
        with patch('app.community.routes.render_template', return_value='rendered') as render:
            client.post(url(app, 'community.add_post', actor=community.name,
                            type='poll'), data=payload,
                        content_type='multipart/form-data')

    assert make.call_args is None
    assert render.call_args.kwargs['form'].repeat.errors != []


def test_a_remote_community_still_accepts_images(app, poster):
    """D1001's condition says *local* communities, and the fix has to leave
    the rest alone: an instance that hosts no images of its own still carries
    posts whose images live on the remote community's server. Without this row
    dropping `community.is_local()` from the condition changed nothing that
    any assertion could see."""
    client, token, community, author, english = poster
    site = db.session.get(Site, 1)
    site.allow_local_image_posts = False
    db.session.commit()

    remote = make_community('remote', host='other.example')
    remote.ap_id = 'remote@other.example'
    db.session.commit()
    make_community_member(author, remote)
    db.session.commit()
    assert not remote.is_local()

    payload = _post_payload(token, english, remote, title='A remote image')
    payload['image_file'] = (io.BytesIO(b'x' * 32), 'photo.png')

    with patch('app.community.routes.make_post', return_value=FakePost()) as make:
        with patch('app.community.routes.render_template', return_value='rendered') as render:
            client.post(url(app, 'community.add_post', actor=remote.name,
                            type='image'), data=payload,
                        content_type='multipart/form-data')

    assert make.call_args is not None, render.call_args.kwargs['form'].errors
