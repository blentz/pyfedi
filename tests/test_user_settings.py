"""The settings page, and the four "block something" forms behind it.

Sub-project 81, slice C. Three defects, all measured:

* a corrupt `max_hours_restriction_date` cookie was
  `ValueError: Invalid isoformat string` (D1051) and a corrupt
  `max_hours_per_day` cookie was `ValueError: invalid literal for int()`
  (D1052) -- both 500s on the settings page, from cookies this application
  sets to expire in 2099, so one bad value locked the account out of its own
  settings for good;
* typing this instance's own domain into the "block instance" box blocked it,
  hiding every local post, comment and community from the caller (D1053) --
  D1035's shape at the other end of the same feature.
"""
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from app import db
from app.models import (Community, CommunityBlock, Domain, DomainBlock,
                        Instance, InstanceBlock, Language, Site, User,
                        UserBlock)
from tests.factories import make_community, make_instance, make_user

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
    db.session.add(Language(code='en', name='English'))
    db.session.commit()
    for user in (viewer, other):
        user.ap_profile_id = f'https://test.piefed.local/u/{user.user_name}'
    db.session.commit()
    return as_user(app, viewer), viewer, other


def settings_payload(token, **overrides):
    """Every field the form refuses without. `theme` is fed from
    `theme_list()`, and `accept_private_messages` coerces to int."""
    data = {'theme': 'piefed', 'interface_language': 'en',
            'default_sort': 'hot', 'default_comment_sort': 'hot',
            'default_filter': 'subscribed', 'font': '',
            'code_style': 'fruity', 'additional_css': '',
            'page_length': '20', 'compaction': '',
            'max_hours_per_day': '', 'max_hours_change_restriction': 'anytime',
            'accept_private_messages': '3', 'submit': 'Save',
            'csrf_token': token}
    data.update(overrides)
    return data


def save_settings(app, client, **overrides):
    token = csrf(app, client)
    with patch('app.user.routes.render_template', return_value='rendered') as render:
        response = client.post('/user/settings',
                               data=settings_payload(token, **overrides))
    return response, render


# --------------------------------------------------------------------------
# The settings form
# --------------------------------------------------------------------------


def test_the_settings_form_opens_on_the_stored_values(app, env):
    client, viewer, other = env
    viewer.default_sort = 'new'
    viewer.theme = 'piefed'
    viewer.markdown_editor = True
    viewer.indexable = False
    viewer.page_length = 15
    db.session.commit()

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = client.get('/user/settings')

    assert response.status_code == 200
    form = render.call_args.kwargs['form']
    assert form.default_sort.data == 'new'
    assert form.theme.data == 'piefed'
    assert form.markdown_editor.data is True
    assert form.indexable.data is False
    assert form.page_length.data == 15


def test_saving_the_settings(app, env):
    client, viewer, other = env

    response, _render = save_settings(app, client, default_sort='new',
                                      markdown_editor='y', page_length='15')

    assert response.status_code == 302
    db.session.refresh(viewer)
    assert viewer.default_sort == 'new'
    assert viewer.markdown_editor is True
    assert viewer.page_length == 15


def test_the_page_length_is_capped_by_the_instance(app, env):
    """`if current_user.page_length > current_app.config['PAGE_LENGTH']` --
    the preference may shorten the page, never lengthen it past what the
    instance is willing to render."""
    client, viewer, other = env

    save_settings(app, client, page_length='9999')

    db.session.refresh(viewer)
    assert viewer.page_length == app.config['PAGE_LENGTH']


def test_turning_off_indexing_rewrites_the_existing_posts(app, env):
    """`propagate_indexable` -- the setting is stored on every post as well as
    on the account, because that is the column the search index reads."""
    from tests.factories import make_post

    client, viewer, other = env
    community = make_community('general')
    db.session.commit()
    post = make_post(community, viewer, 'https://test.piefed.local/p/1',
                     title='a post')
    post.indexable = True
    viewer.indexable = True
    db.session.commit()

    save_settings(app, client)  # indexable unchecked == False

    db.session.refresh(post)
    db.session.refresh(viewer)
    assert viewer.indexable is False
    assert post.indexable is False


def test_leaving_indexing_alone_does_not_rewrite_the_posts(app, env):
    """The other side of `propagate_indexable`: an UPDATE over every post the
    account has ever made is not something a save should do when the setting
    has not changed."""
    client, viewer, other = env
    viewer.indexable = False
    db.session.commit()

    with patch('app.user.routes.db.session.execute') as execute:
        save_settings(app, client)

    statements = [str(call.args[0]) for call in execute.call_args_list]
    assert not any('post' in statement and 'indexable' in statement
                   for statement in statements)


def test_the_interface_language_is_remembered_in_the_session(app, env):
    """`session['ui_language']` is what the next request reads before the
    database is touched."""
    client, viewer, other = env

    save_settings(app, client, interface_language='fr')

    with client.session_transaction() as session:
        assert session['ui_language'] == 'fr'


def test_federating_votes_is_stored_inverted(app, env):
    """The form asks "federate votes"; the column stores "vote privately".
    Both directions, because an inversion that went one way only would look
    right half the time."""
    client, viewer, other = env
    # One token for both saves: `generate_csrf` caches in `g`, so a second
    # call inside a fresh request context does not re-populate the session and
    # `flask_session['csrf_token']` raises KeyError.
    token = csrf(app, client)

    with patch('app.user.routes.render_template', return_value='rendered'):
        client.post('/user/settings',
                    data=settings_payload(token, federate_votes='y'))
    db.session.refresh(viewer)
    assert viewer.vote_privately is False

    with patch('app.user.routes.render_template', return_value='rendered'):
        client.post('/user/settings', data=settings_payload(token))
    db.session.refresh(viewer)
    assert viewer.vote_privately is True


def test_changing_the_content_languages_clears_the_filter_cache(app, env):
    client, viewer, other = env
    english = Language.query.filter_by(code='en').one()

    with patch('app.user.routes.cache.delete_memoized') as cleared:
        save_settings(app, client, read_languages=[str(english.id)])

    db.session.refresh(viewer)
    assert viewer.read_language_ids == [english.id]
    assert cleared.call_args_list != []


@pytest.mark.parametrize('cookie, value, expected', [
    ('compact_level', 'compaction', 'compact-min'),
    ('low_bandwidth', 'low_bandwidth_mode', '1'),
])
def test_the_display_settings_are_cookies(app, env, cookie, value, expected):
    """Compaction and low-bandwidth mode are stored in cookies rather than on
    the account, because the same account can be read on two devices."""
    client, viewer, other = env
    payload = {value: 'y'} if value == 'low_bandwidth_mode' else {value: expected}

    response, _render = save_settings(app, client, **payload)

    cookies = ' '.join(response.headers.getlist('Set-Cookie'))
    assert f'{cookie}={expected}' in cookies


def test_a_deleted_account_has_no_settings_page(app, env):
    client, viewer, other = env
    viewer.deleted = True
    db.session.commit()

    response = client.get('/user/settings')

    assert response.status_code == 404


# --------------------------------------------------------------------------
# D1051, D1052 -- the daily usage limit
# --------------------------------------------------------------------------


def test_setting_a_daily_limit(app, env):
    client, viewer, other = env

    response, _render = save_settings(app, client, max_hours_per_day='3')

    cookies = ' '.join(response.headers.getlist('Set-Cookie'))
    assert 'max_hours_per_day=3' in cookies


def test_clearing_the_daily_limit_clears_its_cookies(app, env):
    client, viewer, other = env
    client.set_cookie('max_hours_per_day', '3', domain='test.piefed.local')

    response, _render = save_settings(app, client, max_hours_per_day='')

    cookies = ' '.join(response.headers.getlist('Set-Cookie'))
    assert 'max_hours_per_day=;' in cookies
    assert 'max_hours_restriction_date=;' in cookies
    assert 'max_hours_change_restriction=;' in cookies


def test_a_limit_with_a_restriction_cannot_be_changed_early(app, env):
    """The whole point of the feature: an account that asked to be held to a
    limit for a month cannot raise it the next day."""
    client, viewer, other = env
    client.set_cookie('max_hours_per_day', '2', domain='test.piefed.local')
    client.set_cookie('max_hours_restriction_date',
                      (datetime.now() + timedelta(days=20)).isoformat(),
                      domain='test.piefed.local')

    response, _render = save_settings(app, client, max_hours_per_day='9')

    cookies = ' '.join(response.headers.getlist('Set-Cookie'))
    assert 'max_hours_per_day=2' in cookies


def test_a_limit_can_be_changed_once_the_restriction_has_passed(app, env):
    client, viewer, other = env
    client.set_cookie('max_hours_per_day', '2', domain='test.piefed.local')
    client.set_cookie('max_hours_restriction_date',
                      (datetime.now() - timedelta(days=1)).isoformat(),
                      domain='test.piefed.local')

    response, _render = save_settings(app, client, max_hours_per_day='9',
                                      max_hours_change_restriction='1day')

    cookies = ' '.join(response.headers.getlist('Set-Cookie'))
    assert 'max_hours_per_day=9' in cookies
    assert 'max_hours_restriction_date=' in cookies


def test_a_corrupt_restriction_date_is_not_a_500(app, env):
    """D1051. `datetime.fromisoformat(restriction_cookie)` on a cookie this
    application sets to expire in 2099. Measured:
    `ValueError: Invalid isoformat string: 'not-a-date'`."""
    client, viewer, other = env
    client.set_cookie('max_hours_per_day', '3', domain='test.piefed.local')
    client.set_cookie('max_hours_restriction_date', 'not-a-date',
                      domain='test.piefed.local')

    response, _render = save_settings(app, client, max_hours_per_day='5')

    assert response.status_code == 302
    cookies = ' '.join(response.headers.getlist('Set-Cookie'))
    assert 'max_hours_per_day=5' in cookies


def test_a_corrupt_hours_cookie_is_not_a_500(app, env):
    """D1052. `int(current_max_hours)`, reached only when the restriction
    cookie is also present -- which is why the first probe, with one cookie,
    answered 302 and told us nothing. Measured with both:
    `ValueError: invalid literal for int() with base 10: 'abc'`."""
    client, viewer, other = env
    client.set_cookie('max_hours_per_day', 'abc', domain='test.piefed.local')
    client.set_cookie('max_hours_restriction_date',
                      (datetime.now() + timedelta(days=20)).isoformat(),
                      domain='test.piefed.local')

    response, _render = save_settings(app, client, max_hours_per_day='5')

    assert response.status_code == 302
    # And the unreadable value is treated as NO limit rather than as some
    # limit: a cookie nobody can parse must not be able to hold the account to
    # a restriction it cannot see.
    cookies = ' '.join(response.headers.getlist('Set-Cookie'))
    assert 'max_hours_per_day=5' in cookies


@pytest.mark.parametrize('setting, days', [
    ('1day', 1), ('1month', 28), ('2months', 55),
])
def test_the_restriction_periods(app, env, setting, days):
    """`_calculate_future_date` has an arm per period, and the month arms do
    their own calendar arithmetic rather than using a library."""
    from app.user.routes import _calculate_future_date

    future = _calculate_future_date(setting)

    assert future > datetime.now() + timedelta(days=days - 1)


def test_a_december_restriction_rolls_into_next_year(app, env):
    """`if current_date.month == 12:` -- the one-month arm adds to the month
    number, so December is the case that needs the year."""
    from app.user import routes

    with patch.object(routes, 'datetime') as fake:
        fake.now.return_value = datetime(2026, 12, 15, 10, 0)
        future = routes._calculate_future_date('1month')

    assert (future.year, future.month) == (2027, 1)


def test_a_november_two_month_restriction_rolls_over(app, env):
    """The two-month arm wraps by subtracting twelve, which is a different
    calculation from the one-month arm's."""
    from app.user import routes

    with patch.object(routes, 'datetime') as fake:
        fake.now.return_value = datetime(2026, 11, 15, 10, 0)
        future = routes._calculate_future_date('2months')

    assert (future.year, future.month) == (2027, 1)


# --------------------------------------------------------------------------
# Blocking, from the settings page
# --------------------------------------------------------------------------


def test_blocking_a_local_user_by_name(app, env):
    client, viewer, other = env
    token = csrf(app, client)

    with patch('app.user.routes.render_template', return_value='rendered'):
        client.post('/user/settings/block/user',
                    data={'username': 'other', 'submit': 'Block user',
                          'csrf_token': token})

    assert UserBlock.query.filter_by(blocker_id=viewer.id,
                                     blocked_id=other.id).count() == 1


def test_blocking_an_unknown_user_says_so(app, env):
    client, viewer, other = env
    token = csrf(app, client)

    with patch('app.user.routes.flash') as flashed:
        with patch('app.user.routes.render_template', return_value='rendered'):
            client.post('/user/settings/block/user',
                        data={'username': 'nobody', 'submit': 'Block user',
                              'csrf_token': token})

    assert UserBlock.query.count() == 0
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'User not found' in messages


def test_you_cannot_block_yourself_from_the_settings(app, env):
    client, viewer, other = env
    token = csrf(app, client)

    with patch('app.user.routes.flash') as flashed:
        with patch('app.user.routes.render_template', return_value='rendered'):
            client.post('/user/settings/block/user',
                        data={'username': 'viewer', 'submit': 'Block user',
                              'csrf_token': token})

    assert UserBlock.query.count() == 0
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'cannot block yourself' in messages


def test_blocking_someone_already_blocked_says_so(app, env):
    client, viewer, other = env
    db.session.add(UserBlock(blocker_id=viewer.id, blocked_id=other.id))
    db.session.commit()
    token = csrf(app, client)

    with patch('app.user.routes.flash') as flashed:
        with patch('app.user.routes.render_template', return_value='rendered'):
            client.post('/user/settings/block/user',
                        data={'username': 'other', 'submit': 'Block user',
                              'csrf_token': token})

    assert UserBlock.query.count() == 1
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'already blocked' in messages


def test_blocking_a_local_community_by_name(app, env):
    client, viewer, other = env
    community = make_community('general')
    db.session.commit()
    token = csrf(app, client)

    with patch('app.user.routes.render_template', return_value='rendered'):
        client.post('/user/settings/block/community',
                    data={'community_name': 'general',
                          'submit': 'Block community', 'csrf_token': token})

    assert CommunityBlock.query.filter_by(user_id=viewer.id,
                                          community_id=community.id).count() == 1


def test_blocking_an_unknown_community_says_so(app, env):
    client, viewer, other = env
    token = csrf(app, client)

    with patch('app.user.routes.flash') as flashed:
        with patch('app.user.routes.render_template', return_value='rendered'):
            client.post('/user/settings/block/community',
                        data={'community_name': 'nowhere',
                              'submit': 'Block community',
                              'csrf_token': token})

    assert CommunityBlock.query.count() == 0
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'not found' in messages.lower()


def test_blocking_a_remote_community_by_handle(app, env):
    """The `!name@host` arm goes through `search_for_community(...,
    allow_fetch=False)`, which looks locally rather than fetching -- a block
    box must not be a way to make the server fetch arbitrary actors."""
    client, viewer, other = env
    remote = make_community('remote', host='other.example')
    remote.ap_id = 'remote@other.example'
    db.session.commit()
    token = csrf(app, client)

    with patch('app.user.routes.search_for_community',
               return_value=remote) as search:
        with patch('app.user.routes.render_template', return_value='rendered'):
            client.post('/user/settings/block/community',
                        data={'community_name': 'remote@other.example',
                              'submit': 'Block community',
                              'csrf_token': token})

    assert search.call_args.args == ('!remote@other.example',)
    assert search.call_args.kwargs == {'allow_fetch': False}
    assert CommunityBlock.query.filter_by(user_id=viewer.id,
                                          community_id=remote.id).count() == 1


def test_blocking_a_domain(app, env):
    client, viewer, other = env
    token = csrf(app, client)

    with patch('app.user.routes.render_template', return_value='rendered'):
        client.post('/user/settings/block/domain',
                    data={'domain_name': 'Example.COM/',
                          'submit': 'Block domain', 'csrf_token': token})

    domain = Domain.query.filter_by(name='example.com').one()
    assert DomainBlock.query.filter_by(user_id=viewer.id,
                                       domain_id=domain.id).count() == 1


@pytest.mark.parametrize('typed', ['https://example.com', 'http://example.com',
                                   'example.com/'])
def test_a_domain_is_normalised_before_it_is_blocked(app, env, typed):
    """People paste URLs. The scheme and the trailing slash are stripped, and
    the result is lower-cased, so the same site does not become three rows."""
    client, viewer, other = env
    token = csrf(app, client)

    with patch('app.user.routes.render_template', return_value='rendered'):
        client.post('/user/settings/block/domain',
                    data={'domain_name': typed, 'submit': 'Block domain',
                          'csrf_token': token})

    assert Domain.query.filter_by(name='example.com').count() == 1


def test_blocking_a_domain_twice_says_so(app, env):
    client, viewer, other = env
    domain = Domain(name='example.com')
    db.session.add(domain)
    db.session.commit()
    db.session.add(DomainBlock(user_id=viewer.id, domain_id=domain.id))
    db.session.commit()
    token = csrf(app, client)

    with patch('app.user.routes.flash') as flashed:
        with patch('app.user.routes.render_template', return_value='rendered'):
            client.post('/user/settings/block/domain',
                        data={'domain_name': 'example.com',
                              'submit': 'Block domain', 'csrf_token': token})

    assert DomainBlock.query.count() == 1
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'already blocked' in messages


def test_you_cannot_block_your_own_domain_from_the_settings(app, env):
    """D581 residue, fixed (owner ruling): the settings page refuses this
    instance's own domain, as `block_domain` and the instance form do, rather
    than creating a row for it and hiding every local link."""
    client, viewer, other = env
    token = csrf(app, client)

    with patch('app.user.routes.flash') as flashed:
        with patch('app.user.routes.render_template', return_value='rendered'):
            client.post('/user/settings/block/domain',
                        data={'domain_name': 'https://Test.PieFed.local/',
                              'submit': 'Block domain', 'csrf_token': token})

    assert DomainBlock.query.count() == 0
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert "cannot block this instance's own domain" in messages


def test_blocking_an_instance_from_the_settings(app, env):
    client, viewer, other = env
    make_instance('other.example', software='piefed')
    db.session.commit()
    token = csrf(app, client)

    with patch('app.user.routes.block_remote_instance') as block:
        with patch('app.user.routes.render_template', return_value='rendered'):
            client.post('/user/settings/block/instance',
                        data={'instance_domain': 'https://Other.example/',
                              'submit': 'Block instance',
                              'csrf_token': token})

    assert block.call_args is not None


def test_blocking_an_unknown_instance_says_so(app, env):
    client, viewer, other = env
    token = csrf(app, client)

    with patch('app.user.routes.block_remote_instance') as block:
        with patch('app.user.routes.flash') as flashed:
            with patch('app.user.routes.render_template',
                       return_value='rendered'):
                client.post('/user/settings/block/instance',
                            data={'instance_domain': 'nowhere.example',
                                  'submit': 'Block instance',
                                  'csrf_token': token})

    assert block.call_args is None
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'Instance not found' in messages


def test_you_cannot_block_your_own_instance_from_the_settings(app, env):
    """D1053. Instance 1 is this server, so blocking it hides every local
    post, comment and community from the caller, with no obvious way back --
    D1035's shape at the other end of the same feature."""
    client, viewer, other = env
    token = csrf(app, client)

    with patch('app.user.routes.block_remote_instance') as block:
        with patch('app.user.routes.flash') as flashed:
            with patch('app.user.routes.render_template',
                       return_value='rendered'):
                client.post('/user/settings/block/instance',
                            data={'instance_domain': 'test.piefed.local',
                                  'submit': 'Block instance',
                                  'csrf_token': token})

    assert block.call_args is None
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'cannot block this instance' in messages


def test_a_failure_to_block_an_instance_is_reported(app, env):
    """`except Exception as e: flash(...)` -- the block goes through a shared
    helper that federates, and a failure there must not be a 500 on the
    settings page."""
    client, viewer, other = env
    make_instance('other.example', software='piefed')
    db.session.commit()
    token = csrf(app, client)

    with patch('app.user.routes.block_remote_instance',
               side_effect=RuntimeError('could not block')):
        with patch('app.user.routes.flash') as flashed:
            with patch('app.user.routes.render_template',
                       return_value='rendered'):
                response = client.post('/user/settings/block/instance',
                                       data={'instance_domain': 'other.example',
                                             'submit': 'Block instance',
                                             'csrf_token': token})

    assert response.status_code in (200, 302)
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'Error blocking instance' in messages


def test_a_restriction_set_when_there_was_no_limit_before(app, env):
    """The `else` arm of the restriction logic: no current limit, so there is
    nothing to be restricted BY, and setting one starts the clock."""
    client, viewer, other = env

    response, _render = save_settings(app, client, max_hours_per_day='4',
                                      max_hours_change_restriction='1month')

    cookies = ' '.join(response.headers.getlist('Set-Cookie'))
    assert 'max_hours_per_day=4' in cookies
    assert 'max_hours_restriction_date=20' in cookies


def test_an_unknown_restriction_period_is_now(app, env):
    """`_calculate_future_date`'s fall-through: a value that is not one of the
    three periods returns the current time, so nothing is restricted."""
    from app.user.routes import _calculate_future_date

    before = datetime.now()
    future = _calculate_future_date('nonsense')

    assert before <= future <= datetime.now()


def test_a_mixed_protocol_instance_opens_most_compact(app, env):
    """`if request.cookies.get('compact_level') is None and
    HTTP_PROTOCOL == 'mixed'` -- an instance serving both protocols defaults
    new visitors to the lightest rendering."""
    client, viewer, other = env
    app.config['HTTP_PROTOCOL'] = 'mixed'
    try:
        with patch('app.user.routes.render_template',
                   return_value='rendered') as render:
            client.get('/user/settings')
    finally:
        app.config['HTTP_PROTOCOL'] = 'https'

    assert render.call_args.kwargs['form'].compaction.data == 'compact-min compact-max'


def test_blocking_a_remote_user_by_handle(app, env):
    """The `'@' in username` arm resolves through `find_actor_or_create`, and
    the `isinstance` check below it is what stops a community handle typed
    into the user box becoming a UserBlock on a Community id."""
    client, viewer, other = env
    remote = make_user(make_instance('other.example', software='piefed'),
                       'remote')
    db.session.commit()
    token = csrf(app, client)

    with patch('app.user.routes.find_actor_or_create', return_value=remote):
        with patch('app.user.routes.render_template', return_value='rendered'):
            client.post('/user/settings/block/user',
                        data={'username': 'remote@other.example',
                              'submit': 'Block user', 'csrf_token': token})

    assert UserBlock.query.filter_by(blocker_id=viewer.id,
                                     blocked_id=remote.id).count() == 1


def test_a_community_typed_into_the_user_box_is_refused(app, env):
    """`if user_to_block and not isinstance(user_to_block, User)` --
    `find_actor_or_create` answers Communities and Feeds as well as Users, and
    their ids share no namespace with each other."""
    client, viewer, other = env
    community = make_community('general')
    db.session.commit()
    token = csrf(app, client)

    with patch('app.user.routes.find_actor_or_create', return_value=community):
        with patch('app.user.routes.flash') as flashed:
            with patch('app.user.routes.render_template',
                       return_value='rendered'):
                client.post('/user/settings/block/user',
                            data={'username': 'general@other.example',
                                  'submit': 'Block user',
                                  'csrf_token': token})

    assert UserBlock.query.count() == 0
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'User not found' in messages


def test_a_user_typed_into_the_community_box_is_refused(app, env):
    """The same check on the community form, which has its own copy."""
    client, viewer, other = env
    token = csrf(app, client)

    with patch('app.user.routes.find_actor_or_create', return_value=other):
        with patch('app.user.routes.flash') as flashed:
            with patch('app.user.routes.render_template',
                       return_value='rendered'):
                client.post('/user/settings/block/community',
                            data={'community_name': 'https://other.example/c/x',
                                  'submit': 'Block community',
                                  'csrf_token': token})

    assert CommunityBlock.query.count() == 0
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'not found' in messages.lower()


def test_blocking_a_community_twice_says_so(app, env):
    client, viewer, other = env
    community = make_community('general')
    db.session.commit()
    db.session.add(CommunityBlock(user_id=viewer.id,
                                  community_id=community.id))
    db.session.commit()
    token = csrf(app, client)

    with patch('app.user.routes.flash') as flashed:
        with patch('app.user.routes.render_template', return_value='rendered'):
            client.post('/user/settings/block/community',
                        data={'community_name': 'general',
                              'submit': 'Block community',
                              'csrf_token': token})

    assert CommunityBlock.query.count() == 1
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'already blocked' in messages


@pytest.mark.parametrize('path, template', [
    ('user', 'user/block_user.html'),
    ('community', 'user/block_community.html'),
    ('domain', 'user/block_domain.html'),
    ('instance', 'user/block_instance.html'),
])
def test_each_block_form_renders(app, env, path, template):
    """Four forms, four GET arms. Each renders its own template, and a copied
    route that rendered a sibling's would look identical from the outside."""
    client, viewer, other = env

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = client.get(f'/user/settings/block/{path}')

    assert response.status_code == 200
    assert render.call_args.args[0] == template
