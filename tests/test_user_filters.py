"""Keyword filters, user notes, and the remote-follow redirect.

Sub-project 81, slice E. One defect, measured:

* `RemoteFollowForm.instance_url` was validated only for length, and the route
  interpolates it straight into the redirect target -- so
  `evil.example/x?a=` produced
  `https://evil.example/x?a=/@author@test.piefed.local` and the route
  redirected there (D1064). The value is also stored in a cookie that expires
  in 2099 and pre-fills the form afterwards, so one bad value persists.

The three filter routes that take an id already check ownership
(`current_user.id != content_filter.user_id: abort(401)`), and the rows below
pin that rather than report it.
"""
from datetime import timedelta
from unittest.mock import patch

import pytest

from app import db
from app.models import Filter, Instance, Site, User, UserNote
from app.utils import utcnow
from tests.factories import make_instance, make_user

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
    viewer = make_user(local, 'viewer', local=True)
    other = make_user(local, 'other', local=True)
    db.session.commit()
    for user in (viewer, other):
        user.ap_profile_id = f'https://test.piefed.local/u/{user.user_name}'
    db.session.commit()
    return as_user(app, viewer), viewer, other


def a_filter(user, title='Politics', keywords='election\nballot', **columns):
    content_filter = Filter(title=title, keywords=keywords, user_id=user.id,
                            filter_home=True, filter_posts=True,
                            filter_replies=False, hide_type=0)
    for column, value in columns.items():
        setattr(content_filter, column, value)
    db.session.add(content_filter)
    db.session.commit()
    return content_filter


def filter_payload(token, **overrides):
    data = {'title': 'Politics', 'keywords': 'election\nballot',
            'filter_home': 'y', 'filter_posts': 'y', 'hide_type': '0',
            'expire_after': '', 'submit': 'Save', 'csrf_token': token}
    data.update(overrides)
    return data


# --------------------------------------------------------------------------
# The filter list
# --------------------------------------------------------------------------


def test_the_filter_list_shows_your_filters(app, env):
    client, viewer, other = env
    a_filter(viewer, title='Mine')

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = client.get('/user/settings/filters')

    assert response.status_code == 200
    assert [f.title for f in render.call_args.kwargs['filters']] == ['Mine']


def test_the_filter_list_shows_nobody_elses(app, env):
    client, viewer, other = env
    a_filter(viewer, title='Mine')
    a_filter(other, title='Theirs')

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/user/settings/filters')

    assert [f.title for f in render.call_args.kwargs['filters']] == ['Mine']


# --------------------------------------------------------------------------
# Adding, editing and deleting a filter
# --------------------------------------------------------------------------


def test_adding_a_filter(app, env):
    client, viewer, other = env
    token = csrf(app, client)

    response = client.post('/user/settings/filters/add',
                           data=filter_payload(token, title='Sport',
                                               keywords='football'))

    assert response.status_code == 302
    content_filter = Filter.query.filter_by(user_id=viewer.id).one()
    assert content_filter.title == 'Sport'
    assert content_filter.keywords == 'football'
    assert content_filter.filter_home is True


def test_a_filter_can_expire(app, env):
    client, viewer, other = env
    token = csrf(app, client)
    expires = (utcnow() + timedelta(days=7)).date()

    client.post('/user/settings/filters/add',
                data=filter_payload(token, expire_after=expires.isoformat()))

    assert Filter.query.one().expire_after == expires


def test_adding_a_filter_clears_the_filter_caches(app, env):
    """The filters are memoized per user in four places, and a new filter that
    does not clear them does nothing until the cache expires."""
    client, viewer, other = env
    token = csrf(app, client)

    with patch('app.user.routes.cache.delete_memoized') as cleared:
        client.post('/user/settings/filters/add', data=filter_payload(token))

    assert len(cleared.call_args_list) >= 4


def test_the_add_form_renders(app, env):
    client, viewer, other = env

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = client.get('/user/settings/filters/add')

    assert response.status_code == 200
    assert render.call_args.kwargs['title'] == 'Add filter'


def test_editing_a_filter(app, env):
    client, viewer, other = env
    content_filter = a_filter(viewer, title='Before')
    token = csrf(app, client)

    response = client.post(f'/user/settings/filters/{content_filter.id}/edit',
                           data=filter_payload(token, title='After',
                                               keywords='changed'))

    assert response.status_code == 302
    db.session.refresh(content_filter)
    assert content_filter.title == 'After'
    assert content_filter.keywords == 'changed'


def test_the_edit_form_opens_on_the_stored_filter(app, env):
    client, viewer, other = env
    content_filter = a_filter(viewer, title='Stored', keywords='a\nb')

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/user/settings/filters/{content_filter.id}/edit')

    form = render.call_args.kwargs['form']
    assert form.title.data == 'Stored'
    assert form.keywords.data == 'a\nb'
    assert form.filter_home.data is True


def test_you_cannot_edit_somebody_elses_filter(app, env):
    """A filter is keyed by id in the URL and belongs to one account; without
    the ownership check this is an ordinary IDOR on another person's settings."""
    client, viewer, other = env
    theirs = a_filter(other, title='Theirs')
    token = csrf(app, client)

    response = client.post(f'/user/settings/filters/{theirs.id}/edit',
                           data=filter_payload(token, title='Hijacked'))

    assert response.status_code == 401
    db.session.refresh(theirs)
    assert theirs.title == 'Theirs'


def test_editing_an_unknown_filter_is_a_404(app, env):
    client, viewer, other = env

    response = client.get('/user/settings/filters/9999/edit')

    assert response.status_code == 404


def test_deleting_a_filter(app, env):
    client, viewer, other = env
    content_filter = a_filter(viewer)
    token = csrf(app, client)

    response = client.post(f'/user/settings/filters/{content_filter.id}/delete',
                           data={'csrf_token': token})

    assert response.status_code == 302
    assert Filter.query.count() == 0


def test_you_cannot_delete_somebody_elses_filter(app, env):
    client, viewer, other = env
    theirs = a_filter(other)
    token = csrf(app, client)

    response = client.post(f'/user/settings/filters/{theirs.id}/delete',
                           data={'csrf_token': token})

    assert response.status_code == 401
    assert Filter.query.count() == 1


def test_deleting_an_unknown_filter_is_a_404(app, env):
    client, viewer, other = env
    token = csrf(app, client)

    response = client.post('/user/settings/filters/9999/delete',
                           data={'csrf_token': token})

    assert response.status_code == 404


def test_deleting_a_filter_is_not_a_get(app, env):
    """The route is POST-only, which is what keeps a forged `<img>` from
    deleting somebody's filters."""
    client, viewer, other = env
    content_filter = a_filter(viewer)

    response = client.get(f'/user/settings/filters/{content_filter.id}/delete')

    assert response.status_code == 405
    assert Filter.query.count() == 1


# --------------------------------------------------------------------------
# User notes
# --------------------------------------------------------------------------


def test_writing_a_note_about_somebody(app, env):
    client, viewer, other = env
    token = csrf(app, client)

    response = client.post('/u/other/note',
                           data={'note': 'a note about them',
                                 'submit': 'Save', 'csrf_token': token})

    assert response.status_code == 302
    note = UserNote.query.filter_by(user_id=viewer.id,
                                    target_id=other.id).one()
    assert note.body == 'a note about them'


def test_the_note_form_opens_on_the_stored_note(app, env):
    client, viewer, other = env
    db.session.add(UserNote(user_id=viewer.id, target_id=other.id,
                            body='stored note'))
    db.session.commit()

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/u/other/note')

    assert render.call_args.kwargs['form'].note.data == 'stored note'


def test_a_note_can_be_applied_to_every_account_with_that_name(app, env):
    """The same handle exists on many instances, and a note about a person is
    usually about all of them."""
    client, viewer, other = env
    remote = make_user(make_instance('other.example', software='piefed'),
                       'other')
    db.session.commit()
    token = csrf(app, client)

    client.post('/u/other/note',
                data={'note': 'the same person', 'apply_all': 'y',
                      'submit': 'Save', 'csrf_token': token})

    assert UserNote.query.filter_by(user_id=viewer.id).count() == 2
    assert {n.target_id for n in UserNote.query.all()} == {other.id, remote.id}


def test_a_note_without_apply_all_touches_one_account(app, env):
    client, viewer, other = env
    make_user(make_instance('other.example', software='piefed'), 'other')
    db.session.commit()
    token = csrf(app, client)

    client.post('/u/other/note',
                data={'note': 'just this one', 'submit': 'Save',
                      'csrf_token': token})

    assert UserNote.query.filter_by(user_id=viewer.id).count() == 1


def test_a_banned_account_cannot_write_notes(app, env):
    client, viewer, other = env
    viewer.banned = True
    db.session.commit()
    token = csrf(app, client)

    with patch('app.user.routes.render_template', return_value='rendered'):
        client.post('/u/other/note',
                    data={'note': 'a note', 'submit': 'Save',
                          'csrf_token': token})

    assert UserNote.query.count() == 0


def test_a_note_about_an_unknown_account_is_a_404(app, env):
    client, viewer, other = env

    response = client.get('/u/nobody/note')

    assert response.status_code == 404


def test_a_note_returns_to_where_it_came_from(app, env):
    client, viewer, other = env
    token = csrf(app, client)

    response = client.post('/u/other/note?return_to=/c/general',
                           data={'note': 'a note', 'submit': 'Save',
                                 'csrf_token': token})

    assert response.headers['Location'] == '/c/general'


def test_a_note_cannot_be_used_to_send_somebody_off_site(app, env):
    """The `return_to` comes from a query parameter, and `safe_redirect_target`
    REPLACES an unsafe candidate with the default rather than raising -- so the
    `if return_to.startswith('http'): abort(401)` below it can never fire, and
    is registered as dead code (D1066). What protects the visitor is the
    replacement, which this row asserts: the save lands on the profile, not on
    the attacker's site."""
    client, viewer, other = env
    token = csrf(app, client)

    response = client.post('/u/other/note?return_to=https://evil.example/',
                           data={'note': 'a note', 'submit': 'Save',
                                 'csrf_token': token})

    assert response.status_code == 302
    assert response.headers['Location'] == '/u/other'


# --------------------------------------------------------------------------
# D1064 -- the remote-follow redirect
# --------------------------------------------------------------------------


def follow_payload(token, **overrides):
    data = {'instance_url': 'mastodon.social', 'instance_type': 'mastodon',
            'submit': 'View profile on remote instance', 'csrf_token': token}
    data.update(overrides)
    return data


@pytest.mark.parametrize('kind, expected', [
    ('mastodon', 'https://mastodon.social/@viewer@test.piefed.local'),
    ('friendica', 'https://mastodon.social/search?q=viewer@test.piefed.local'),
    ('hubzilla', 'https://mastodon.social/search?q=viewer@test.piefed.local'),
    ('pixelfed', 'https://mastodon.social/i/results?q=viewer@test.piefed.local'),
])
def test_each_remote_software_gets_its_own_url(app, env, kind, expected):
    """Four arms building four different URL shapes; a row for one says
    nothing about the others."""
    client, viewer, other = env
    token = csrf(app, client)

    response = client.post('/u/viewer/fediverse_redirect',
                           data=follow_payload(token, instance_type=kind))

    assert response.status_code == 302
    assert response.headers['Location'] == expected


def test_lemmy_cannot_follow_a_profile(app, env):
    """The one arm that refuses rather than redirecting."""
    client, viewer, other = env
    token = csrf(app, client)

    with patch('app.user.routes.flash') as flashed:
        with patch('app.user.routes.render_template', return_value='rendered'):
            response = client.post('/u/viewer/fediverse_redirect',
                                   data=follow_payload(token,
                                                       instance_type='lemmy'))

    assert response.status_code == 200
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert "Lemmy can't follow profiles" in messages


def test_the_remote_instance_is_remembered(app, env):
    client, viewer, other = env
    token = csrf(app, client)

    response = client.post('/u/viewer/fediverse_redirect',
                           data=follow_payload(token))

    cookies = ' '.join(response.headers.getlist('Set-Cookie'))
    assert 'remote_instance_url=mastodon.social' in cookies


def test_the_remembered_instance_pre_fills_the_form(app, env):
    client, viewer, other = env
    client.set_cookie('remote_instance_url', 'mastodon.social',
                      domain='test.piefed.local')

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/u/viewer/fediverse_redirect')

    assert render.call_args.kwargs['form'].instance_url.data == 'mastodon.social'


@pytest.mark.parametrize('typed', [
    'evil.example/x?a=',
    'evil.example/@somebody',
    'evil.example#',
    'user@evil.example',
    'evil.example/..',
])
def test_the_remote_instance_must_be_a_hostname(app, env, typed):
    """D1064. The value is interpolated straight into the redirect target, so
    anything carrying a path, a query or an authority sends the visitor
    somewhere else entirely. Measured: `evil.example/x?a=` produced
    `https://evil.example/x?a=/@author@test.piefed.local` and the route
    redirected there."""
    client, viewer, other = env
    token = csrf(app, client)

    with patch('app.user.routes.render_template', return_value='rendered'):
        response = client.post('/u/viewer/fediverse_redirect',
                               data=follow_payload(token,
                                                   instance_url=typed))

    assert response.status_code == 200
    assert 'Location' not in response.headers


def test_a_hostname_with_a_port_is_allowed(app, env):
    """Development and self-hosted instances run on ports; refusing them would
    be a refusal of the ordinary case for those users."""
    client, viewer, other = env
    token = csrf(app, client)

    response = client.post('/u/viewer/fediverse_redirect',
                           data=follow_payload(token,
                                               instance_url='example.com:8443'))

    assert response.status_code == 302
    assert response.headers['Location'].startswith('https://example.com:8443/')


def test_a_remote_account_has_no_remote_follow_page(app, env):
    """`if user and user.is_local()` -- this page exists to send somebody to
    the instance that hosts the account, and a remote account is already
    there."""
    client, viewer, other = env
    remote = make_user(make_instance('other.example', software='piefed'),
                       'remote')
    db.session.commit()

    response = client.get('/u/remote/fediverse_redirect')

    assert response.status_code == 404


# --------------------------------------------------------------------------
# The content-filter preferences on the same page
# --------------------------------------------------------------------------


def preferences_payload(token, **overrides):
    data = {'ignore_bots': '0', 'hide_nsfw': '1', 'hide_nsfl': '1',
            'hide_gen_ai': '2', 'reply_collapse_threshold': '-10',
            'reply_hide_threshold': '-20', 'community_keyword_filter': '',
            'submit': 'Save', 'csrf_token': token}
    data.update(overrides)
    return data


def test_saving_the_content_preferences(app, env):
    client, viewer, other = env
    token = csrf(app, client)

    response = client.post('/user/settings/filters',
                           data=preferences_payload(token, ignore_bots='1',
                                                    hide_gen_ai='3',
                                                    reply_collapse_threshold='-5',
                                                    community_keyword_filter='sport'))

    assert response.status_code == 302
    db.session.refresh(viewer)
    assert viewer.ignore_bots == 1
    assert viewer.hide_gen_ai == 3
    assert viewer.reply_collapse_threshold == -5
    assert viewer.community_keyword_filter == 'sport'


def test_the_preferences_open_on_the_stored_values(app, env):
    client, viewer, other = env
    viewer.ignore_bots = 1
    viewer.hide_nsfw = 2
    viewer.hide_low_quality = True
    viewer.community_keyword_filter = 'sport'
    db.session.commit()

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/user/settings/filters')

    form = render.call_args.kwargs['form']
    assert form.ignore_bots.data == 1
    assert form.hide_nsfw.data == 2
    assert form.hide_low_quality.data is True
    assert form.community_keyword_filter.data == 'sport'


def test_a_restricted_country_forces_adult_content_hidden(app, env):
    """`if current_user.ip_address_country and user_in_restricted_country(...)`
    -- the preference is overridden after it is read, so the account cannot
    turn it off from a country whose law forbids it."""
    client, viewer, other = env
    viewer.ip_address_country = 'GB'
    db.session.commit()
    token = csrf(app, client)

    with patch('app.user.routes.user_in_restricted_country',
               return_value=True):
        client.post('/user/settings/filters',
                    data=preferences_payload(token, hide_nsfw='0',
                                             hide_nsfl='0'))

    db.session.refresh(viewer)
    assert viewer.hide_nsfw == 1
    assert viewer.hide_nsfl == 1


def test_a_restricted_country_says_why(app, env):
    """The flash fires on the SUBMITTED values, before the override, so
    somebody who tried to turn it off is told why it did not take."""
    client, viewer, other = env
    viewer.ip_address_country = 'GB'
    db.session.commit()
    token = csrf(app, client)

    with patch('app.user.routes.user_in_restricted_country',
               return_value=True):
        with patch('app.user.routes.flash') as flashed:
            client.post('/user/settings/filters',
                        data=preferences_payload(token, hide_nsfw='0'))

    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'legal restrictions in your country' in messages


def test_an_unrestricted_country_leaves_the_choice_alone(app, env):
    """The other side of both conditions, which an override that fired
    unconditionally would pass."""
    client, viewer, other = env
    token = csrf(app, client)

    with patch('app.user.routes.user_in_restricted_country',
               return_value=False):
        with patch('app.user.routes.flash') as flashed:
            client.post('/user/settings/filters',
                        data=preferences_payload(token, hide_nsfw='0',
                                                 hide_nsfl='0'))

    db.session.refresh(viewer)
    assert viewer.hide_nsfw == 0
    assert viewer.hide_nsfl == 0
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'legal restrictions' not in messages


def test_the_filters_page_lists_every_kind_of_block(app, env):
    """Five separate queries feed the page, and a copied one that read the
    wrong table would still render something."""
    from app.models import (Community, CommunityBlock, CommunityFlair,
                            CommunityFlairBlock, Domain, DomainBlock,
                            InstanceBlock, UserBlock)
    from tests.factories import make_community

    client, viewer, other = env
    community = make_community('general')
    db.session.commit()
    flair = CommunityFlair(community_id=community.id, flair='Question',
                           text_color='#000000', background_color='#ffffff')
    domain = Domain(name='example.com')
    blocked_instance = make_instance('other.example', software='piefed')
    db.session.add_all([flair, domain])
    db.session.commit()
    db.session.add_all([
        UserBlock(blocker_id=viewer.id, blocked_id=other.id),
        CommunityBlock(user_id=viewer.id, community_id=community.id),
        DomainBlock(user_id=viewer.id, domain_id=domain.id),
        InstanceBlock(user_id=viewer.id, instance_id=blocked_instance.id),
        CommunityFlairBlock(user_id=viewer.id, community_id=community.id,
                            community_flair_id=flair.id),
    ])
    db.session.commit()

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/user/settings/filters')

    context = render.call_args.kwargs
    assert [u.user_name for u in context['blocked_users']] == ['other']
    assert [c.name for c in context['blocked_communities']] == ['general']
    assert [d.name for d in context['blocked_domains']] == ['example.com']
    assert [i.domain for i in context['blocked_instances']] == ['other.example']
    assert [f.flair for f in context['blocked_flair']] == ['Question']


def test_the_filters_page_shows_nobody_elses_blocks(app, env):
    from app.models import UserBlock

    client, viewer, other = env
    db.session.add(UserBlock(blocker_id=other.id, blocked_id=viewer.id))
    db.session.commit()

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/user/settings/filters')

    assert render.call_args.kwargs['blocked_users'] == []


def test_a_deleted_account_is_not_listed_as_blocked(app, env):
    from app.models import UserBlock

    client, viewer, other = env
    db.session.add(UserBlock(blocker_id=viewer.id, blocked_id=other.id))
    other.deleted = True
    db.session.commit()

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/user/settings/filters')

    assert render.call_args.kwargs['blocked_users'] == []


def test_a_note_about_a_remote_account(app, env):
    """`if '@' in actor` -- the handle arm of the note route's lookup."""
    client, viewer, other = env
    remote = make_user(make_instance('other.example', software='piefed'),
                       'remote')
    remote.ap_id = 'remote@other.example'
    db.session.commit()
    token = csrf(app, client)

    response = client.post('/u/remote@other.example/note',
                           data={'note': 'about a remote person',
                                 'submit': 'Save', 'csrf_token': token})

    assert response.status_code == 302
    assert UserNote.query.filter_by(user_id=viewer.id,
                                    target_id=remote.id).count() == 1


def test_a_restricted_country_says_nothing_when_nothing_was_attempted(app, env):
    """`(form.hide_nsfw.data != 1 or form.hide_nsfl.data != 1) and ...` -- the
    message explains why a CHANGE did not take, so somebody in a restricted
    country who leaves both settings hidden should not be told anything."""
    client, viewer, other = env
    viewer.ip_address_country = 'GB'
    db.session.commit()
    token = csrf(app, client)

    with patch('app.user.routes.user_in_restricted_country',
               return_value=True):
        with patch('app.user.routes.flash') as flashed:
            client.post('/user/settings/filters',
                        data=preferences_payload(token, hide_nsfw='1',
                                                 hide_nsfl='1'))

    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'legal restrictions' not in messages
