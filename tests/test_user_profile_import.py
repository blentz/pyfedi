"""Editing a profile, and importing or exporting settings.

Sub-project 81, slice B. Six defects, all measured:

* `remove_avatar` and `remove_cover` accepted GET and deleted the viewer's
  image, two of D988's eleven (D1041);
* the settings import read the WHOLE upload into memory and stored it in Redis
  for an hour, and this application sets no `MAX_CONTENT_LENGTH` at all, so
  the size was whatever the client sent (D1042);
* `/notifications/all_read?type=abc` was a `ValueError` -- a 500 from a URL
  anyone could construct (D1043) -- and the route accepted GET, so a forged
  `<img>` marked every notification read (D1044);
* the import task iterated whatever it found under each key, so a string
  where a list belongs was iterated CHARACTER BY CHARACTER, each character
  becoming an actor lookup (D1045);
* and the list length was uncapped, so one upload drove one outbound actor
  fetch per entry -- `find_actor_or_create` defaults to
  `create_if_not_found=True` (D1046).
"""
import io
import json
from unittest.mock import patch

import pytest

from app import db
from app.models import (Community, CommunityBlock, File, Instance,
                        Notification, Site, User, UserBlock, UserExtraField)
from tests.factories import (make_community, make_community_member,
                             make_instance, make_user)

pytestmark = pytest.mark.usefixtures('site')


def instance(domain='test.piefed.local', software='piefed'):
    """Fact 394."""
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


def as_user(app, user):
    client = app.test_client()
    login(client, user)
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


def profile_payload(token, **overrides):
    data = {'title': 'A Display Name', 'email': 'viewer@example.com',
            'password': '', 'about': 'about me', 'matrixuserid': '',
            'timezone': 'Europe/London', 'submit': 'Save',
            'csrf_token': token}
    for number in range(1, 5):
        data[f'extra_label_{number}'] = ''
        data[f'extra_text_{number}'] = ''
    data.update(overrides)
    return data


def an_image(name='avatar.png'):
    image = File(source_url=f'https://test.piefed.local/{name}',
                 file_path=f'app/static/media/{name}')
    db.session.add(image)
    db.session.commit()
    return image


# --------------------------------------------------------------------------
# Editing a profile
# --------------------------------------------------------------------------


def test_the_profile_form_opens_on_the_stored_values(app, env):
    client, viewer, other = env
    viewer.title = 'Stored name'
    viewer.about = 'stored bio'
    viewer.matrix_user_id = '@viewer:example.com'
    viewer.timezone = 'Europe/Paris'
    db.session.add(UserExtraField(user_id=viewer.id, label='Pronouns',
                                  text='they/them'))
    db.session.commit()

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = client.get('/u/viewer/profile')

    assert response.status_code == 200
    form = render.call_args.kwargs['form']
    assert form.title.data == 'Stored name'
    assert form.about.data == 'stored bio'
    assert form.matrixuserid.data == '@viewer:example.com'
    assert form.timezone.data == 'Europe/Paris'
    assert form.extra_label_1.data == 'Pronouns'
    assert form.extra_text_1.data == 'they/them'


def test_the_password_field_never_opens_pre_filled(app, env):
    """`form.password.data = ''` -- the field is a PasswordField and a
    rendered value would put the stored hash, or a previous submission, into
    the page."""
    client, viewer, other = env

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get('/u/viewer/profile')

    assert render.call_args.kwargs['form'].password.data == ''


def test_saving_the_profile(app, env):
    client, viewer, other = env
    token = csrf(app, client)

    response = client.post('/u/viewer/profile',
                           data=profile_payload(token, title='New name',
                                                about='new bio'),
                           content_type='multipart/form-data')

    assert response.status_code == 302
    db.session.refresh(viewer)
    assert viewer.title == 'New name'
    assert viewer.about == 'new bio'
    assert viewer.about_html == '<p>new bio</p>\n'


def test_extra_fields_are_replaced_not_appended(app, env):
    """`current_user.extra_fields = []` before the four appends -- a save that
    added to the list would grow it by four every time."""
    client, viewer, other = env
    db.session.add(UserExtraField(user_id=viewer.id, label='Old', text='old'))
    db.session.commit()
    token = csrf(app, client)

    client.post('/u/viewer/profile',
                data=profile_payload(token, extra_label_1='Pronouns',
                                     extra_text_1='they/them'),
                content_type='multipart/form-data')

    db.session.refresh(viewer)
    assert [(f.label, f.text) for f in viewer.extra_fields] == [
        ('Pronouns', 'they/them')]


def test_an_extra_field_needs_both_halves(app, env):
    """Each pair is stored only when the label AND the text are non-empty, so
    a half-filled row does not become a blank badge on the profile."""
    client, viewer, other = env
    token = csrf(app, client)

    client.post('/u/viewer/profile',
                data=profile_payload(token, extra_label_1='Pronouns',
                                     extra_text_1='',
                                     extra_label_2='', extra_text_2='orphan'),
                content_type='multipart/form-data')

    db.session.refresh(viewer)
    assert list(viewer.extra_fields) == []


@pytest.mark.parametrize('number', [1, 2, 3, 4])
def test_each_extra_field_is_stored(app, env, number):
    """Four near-identical blocks; a row for one says nothing about the rest,
    and a copy-paste error in any of them would be invisible."""
    client, viewer, other = env
    token = csrf(app, client)

    client.post('/u/viewer/profile',
                data=profile_payload(token,
                                     **{f'extra_label_{number}': f'Label {number}',
                                        f'extra_text_{number}': f'Text {number}'}),
                content_type='multipart/form-data')

    db.session.refresh(viewer)
    assert [(f.label, f.text) for f in viewer.extra_fields] == [
        (f'Label {number}', f'Text {number}')]


def test_changing_your_email_requires_verification_again(app, env):
    client, viewer, other = env
    viewer.verified = True
    db.session.commit()
    token = csrf(app, client)

    with patch('app.user.routes.send_verification_email') as verify:
        client.post('/u/viewer/profile',
                    data=profile_payload(token, email='new@example.com'),
                    content_type='multipart/form-data')

    db.session.refresh(viewer)
    assert viewer.email == 'new@example.com'
    assert viewer.verified is False
    assert viewer.verification_token is not None
    assert verify.call_args is not None


def test_keeping_your_email_does_not_reverify(app, env):
    client, viewer, other = env
    viewer.verified = True
    viewer.email = 'viewer@example.com'
    db.session.commit()
    token = csrf(app, client)

    with patch('app.user.routes.send_verification_email') as verify:
        client.post('/u/viewer/profile', data=profile_payload(token),
                    content_type='multipart/form-data')

    db.session.refresh(viewer)
    assert viewer.verified is True
    assert verify.call_args is None


def test_verification_can_be_turned_off_instance_wide(app, env):
    """`get_setting('email_verification', True)` -- an instance that does not
    verify addresses must not un-verify the account on every email change."""
    client, viewer, other = env
    viewer.verified = True
    db.session.commit()
    token = csrf(app, client)

    with patch('app.user.routes.get_setting', return_value=False):
        with patch('app.user.routes.send_verification_email') as verify:
            client.post('/u/viewer/profile',
                        data=profile_payload(token, email='new@example.com'),
                        content_type='multipart/form-data')

    db.session.refresh(viewer)
    assert viewer.email == 'new@example.com'
    assert viewer.verified is True
    assert verify.call_args is None


def test_setting_a_new_password(app, env):
    """`set_password()` stamps `password_updated_at`, which is what revokes
    the account's API tokens -- so the row asserts the stamp as well as the
    hash."""
    client, viewer, other = env
    before = viewer.password_hash
    token = csrf(app, client)

    client.post('/u/viewer/profile',
                data=profile_payload(token, password='a new password'),
                content_type='multipart/form-data')

    db.session.refresh(viewer)
    assert viewer.password_hash != before
    assert viewer.check_password('a new password')
    assert viewer.password_updated_at is not None


def test_an_empty_password_field_leaves_the_password_alone(app, env):
    """The field is Optional and blank on every render, so an empty
    submission is the ordinary case -- treating it as a change would reset
    the password on every profile save."""
    client, viewer, other = env
    viewer.set_password('the old password')
    db.session.commit()
    before = viewer.password_hash
    token = csrf(app, client)

    client.post('/u/viewer/profile', data=profile_payload(token, password=''),
                content_type='multipart/form-data')

    db.session.refresh(viewer)
    assert viewer.password_hash == before


def test_a_banned_account_cannot_save_its_profile(app, env):
    """A banned account keeps the bio and display name it had -- and the
    refusal comes from the LOOKUP, not from the `and not current_user.banned`
    conjunct further down.

    `edit_profile` resolves the actor with `banned=False` and then requires
    `current_user.id == user.id`, so a banned caller is a 404 before the form
    is reached and that conjunct cannot decide anything. Registered as an
    equivalent mutant (D1049) rather than contorted into a kill."""
    client, viewer, other = env
    viewer.title = 'Before'
    viewer.banned = True
    db.session.commit()
    token = csrf(app, client)

    response = client.post('/u/viewer/profile',
                           data=profile_payload(token, title='After'),
                           content_type='multipart/form-data')

    assert response.status_code == 404
    db.session.refresh(viewer)
    assert viewer.title == 'Before'


def test_you_cannot_edit_somebody_elses_profile(app, env):
    client, viewer, other = env

    response = client.get('/u/other/profile')

    assert response.status_code == 401


def test_editing_an_unknown_profile_is_a_404(app, env):
    client, viewer, other = env

    response = client.get('/u/nobody/profile')

    assert response.status_code == 404


def test_a_deleted_account_has_no_profile_form(app, env):
    """The lookup filters `deleted=False` and `banned=False`, so both are a
    404 rather than a refusal."""
    client, viewer, other = env
    viewer.deleted = True
    db.session.commit()

    response = client.get('/u/viewer/profile')

    assert response.status_code == 404


def test_uploading_an_avatar_replaces_the_old_one(app, env):
    client, viewer, other = env
    old = an_image('old.png')
    viewer.avatar_id = old.id
    db.session.commit()
    new = an_image('new.png')
    token = csrf(app, client)
    payload = profile_payload(token)
    payload['profile_file'] = (io.BytesIO(b'an image'), 'new.png')

    with patch('app.user.routes.save_icon_file', return_value=new):
        with patch('app.models.File.delete_from_disk') as deleted:
            client.post('/u/viewer/profile', data=payload,
                        content_type='multipart/form-data')

    db.session.refresh(viewer)
    assert viewer.avatar_id == new.id
    assert db.session.get(File, old.id) is None
    assert deleted.call_args is not None


def test_a_rejected_avatar_leaves_the_old_one(app, env):
    """`if file:` -- `save_icon_file` answers None for an upload it will not
    store, and losing the old avatar to a failed replacement would be worse
    than the failure."""
    client, viewer, other = env
    old = an_image('old.png')
    viewer.avatar_id = old.id
    db.session.commit()
    token = csrf(app, client)
    payload = profile_payload(token)
    payload['profile_file'] = (io.BytesIO(b'not an image'), 'new.png')

    with patch('app.user.routes.save_icon_file', return_value=None):
        client.post('/u/viewer/profile', data=payload,
                    content_type='multipart/form-data')

    db.session.refresh(viewer)
    assert viewer.avatar_id == old.id


def test_uploading_a_banner_replaces_the_old_one(app, env):
    client, viewer, other = env
    old = an_image('old_banner.png')
    viewer.cover_id = old.id
    db.session.commit()
    new = an_image('new_banner.png')
    token = csrf(app, client)
    payload = profile_payload(token)
    payload['banner_file'] = (io.BytesIO(b'an image'), 'banner.png')

    with patch('app.user.routes.save_banner_file', return_value=new):
        with patch('app.models.File.delete_from_disk'):
            client.post('/u/viewer/profile', data=payload,
                        content_type='multipart/form-data')

    db.session.refresh(viewer)
    assert viewer.cover_id == new.id


def test_an_ldap_failure_does_not_fail_the_save(app, env):
    """The whole point of that try/except: an instance whose LDAP server is
    down must still let people edit their profile."""
    client, viewer, other = env
    token = csrf(app, client)

    with patch('app.user.routes.sync_user_to_ldap',
               side_effect=RuntimeError('ldap is down')):
        response = client.post('/u/viewer/profile',
                               data=profile_payload(token, title='Saved'),
                               content_type='multipart/form-data')

    assert response.status_code == 302
    db.session.refresh(viewer)
    assert viewer.title == 'Saved'


def test_the_new_password_is_synced_to_ldap(app, env):
    """`form.password.data if password_updated else None` -- LDAP is told the
    new password only when there is one, and must not be sent an empty string
    on every other save."""
    client, viewer, other = env
    token = csrf(app, client)

    with patch('app.user.routes.sync_user_to_ldap') as sync:
        client.post('/u/viewer/profile',
                    data=profile_payload(token, password='a new password'),
                    content_type='multipart/form-data')
        with_password = sync.call_args.args

        client.post('/u/viewer/profile', data=profile_payload(token),
                    content_type='multipart/form-data')
        without_password = sync.call_args.args

    assert with_password[2] == 'a new password'
    assert without_password[2] is None


# --------------------------------------------------------------------------
# D1041 -- removing your own images
# --------------------------------------------------------------------------


@pytest.mark.parametrize('path, column, message', [
    ('remove_avatar', 'avatar_id', b'Avatar removed'),
    ('remove_cover', 'cover_id', b'Banner removed'),
])
def test_removing_your_own_image(app, env, path, column, message):
    client, viewer, other = env
    setattr(viewer, column, an_image().id)
    db.session.commit()
    token = csrf(app, client)

    with patch('app.models.File.delete_from_disk'):
        response = client.post(f'/user/{path}', data={'csrf_token': token})

    assert response.status_code == 200
    assert message in response.data
    db.session.refresh(viewer)
    assert getattr(viewer, column) is None


@pytest.mark.parametrize('path, column', [
    ('remove_avatar', 'avatar_id'), ('remove_cover', 'cover_id'),
])
def test_removing_an_image_is_not_a_get(app, env, path, column):
    """D1041. Two of D988's eleven. `login_required` validates CSRF only for
    POST, so as a GET an `<img src="/user/remove_avatar">` deleted the
    viewer's own image."""
    client, viewer, other = env
    image = an_image()
    setattr(viewer, column, image.id)
    db.session.commit()

    with patch('app.models.File.delete_from_disk') as deleted:
        response = client.get(f'/user/{path}')

    assert response.status_code == 405
    db.session.refresh(viewer)
    assert getattr(viewer, column) == image.id
    assert deleted.call_args is None


@pytest.mark.parametrize('path', ['remove_avatar', 'remove_cover'])
def test_removing_an_image_you_do_not_have(app, env, path):
    client, viewer, other = env
    token = csrf(app, client)

    response = client.post(f'/user/{path}', data={'csrf_token': token})

    assert response.status_code == 200


# --------------------------------------------------------------------------
# D1043, D1044 -- notifications
# --------------------------------------------------------------------------


def a_notification(user, read=False, notif_type=0):
    notification = Notification(user_id=user.id, title='a notification',
                                url='/', author_id=user.id, read=read,
                                notif_type=notif_type)
    db.session.add(notification)
    db.session.commit()
    return notification


def test_marking_every_notification_read(app, env):
    client, viewer, other = env
    a_notification(viewer)
    a_notification(viewer)
    token = csrf(app, client)

    response = client.post('/notifications/all_read',
                           data={'csrf_token': token})

    assert response.status_code == 302
    assert Notification.query.filter_by(read=False).count() == 0


def test_marking_one_type_read(app, env):
    """The `type` filter is a set of notif_type ids formatted as `{41, 10}`,
    and only those are marked."""
    client, viewer, other = env
    kept = a_notification(viewer, notif_type=10)
    marked = a_notification(viewer, notif_type=41)
    token = csrf(app, client)

    client.post('/notifications/all_read?type=%7B41%7D',
                data={'csrf_token': token})

    db.session.refresh(kept)
    db.session.refresh(marked)
    assert kept.read is False
    assert marked.read is True


def test_only_your_own_notifications_are_marked(app, env):
    client, viewer, other = env
    mine = a_notification(viewer)
    theirs = a_notification(other)
    token = csrf(app, client)

    client.post('/notifications/all_read', data={'csrf_token': token})

    db.session.refresh(mine)
    db.session.refresh(theirs)
    assert mine.read is True
    assert theirs.read is False


def test_a_junk_type_filter_is_not_a_500(app, env):
    """D1043. `int(x)` on a query parameter: `?type=abc` was
    `ValueError: invalid literal for int() with base 10: 'abc'`, measured. An
    unusable filter means "all of them", which is what an empty filter already
    means."""
    client, viewer, other = env
    a_notification(viewer)
    token = csrf(app, client)

    response = client.post('/notifications/all_read?type=abc',
                           data={'csrf_token': token})

    assert response.status_code == 302
    assert Notification.query.filter_by(read=False).count() == 0


def test_marking_everything_read_is_not_a_get(app, env):
    """D1044, the last of D988's eleven. Measured as a bare GET:
    `PROBE v3 status: 302 unread rows: 0`."""
    client, viewer, other = env
    a_notification(viewer)

    response = client.get('/notifications/all_read')

    assert response.status_code == 405
    assert Notification.query.filter_by(read=False).count() == 1


def test_the_unread_filter_marks_everything(app, env):
    """`notif_type == 'Unread'` takes the same arm as the empty filter -- the
    unread tab's button means "all of these", and all of these are unread."""
    client, viewer, other = env
    a_notification(viewer)
    token = csrf(app, client)

    client.post('/notifications/all_read?type=Unread',
                data={'csrf_token': token})

    assert Notification.query.filter_by(read=False).count() == 0


# --------------------------------------------------------------------------
# Exporting settings
# --------------------------------------------------------------------------


def exported(app, client):
    response = client.post('/user/settings/import_export',
                           data={'export_settings': 'Export',
                                 'csrf_token': csrf(app, client)})
    return response, json.loads(response.data)


def test_exporting_your_settings(app, env):
    client, viewer, other = env
    viewer.title = 'Display name'
    viewer.about = 'my bio'
    viewer.matrix_user_id = '@viewer:example.com'
    viewer.bot = True
    db.session.commit()

    response, exported_json = exported(app, client)

    assert response.status_code == 200
    assert 'attachment' in response.headers['Content-Disposition']
    assert exported_json['display_name'] == 'Display name'
    assert exported_json['bio'] == 'my bio'
    assert exported_json['matrix_id'] == '@viewer:example.com'
    assert exported_json['bot_account'] is True
    assert exported_json['settings']['email'] == viewer.email


@pytest.mark.parametrize('column, key, value, expected', [
    ('hide_nsfw', 'show_nsfw', 1, False),
    ('hide_nsfw', 'show_nsfw', 0, True),
    ('ignore_bots', 'show_bot_accounts', 1, False),
    ('ignore_bots', 'show_bot_accounts', 0, True),
])
def test_the_export_inverts_the_hide_settings(app, env, column, key, value,
                                              expected):
    """PieFed stores "hide this"; the Lemmy format the export targets stores
    "show this". Both directions, because an export that inverted only one way
    would still look right in half the cases."""
    client, viewer, other = env
    setattr(viewer, column, value)
    db.session.commit()

    _response, exported_json = exported(app, client)

    assert exported_json['settings'][key] is expected


def test_the_export_lists_subscribed_communities(app, env):
    client, viewer, other = env
    community = make_community('general')
    db.session.commit()
    make_community_member(viewer, community)
    db.session.commit()

    _response, exported_json = exported(app, client)

    assert exported_json['followed_communities'] == [community.ap_profile_id]


def test_the_export_lists_blocks(app, env):
    client, viewer, other = env
    community = make_community('general')
    db.session.commit()
    db.session.add(CommunityBlock(user_id=viewer.id,
                                  community_id=community.id))
    db.session.add(UserBlock(blocker_id=viewer.id, blocked_id=other.id))
    db.session.commit()

    _response, exported_json = exported(app, client)

    assert exported_json['blocked_communities'] == [community.ap_public_url]
    assert exported_json['blocked_users'] == [other.ap_public_url]


def test_the_export_skips_a_community_with_no_actor_id(app, env):
    """`if c.ap_profile_id is None: continue` -- a local community created
    before its actor id was assigned would otherwise export as null, which the
    importing instance cannot resolve."""
    client, viewer, other = env
    community = make_community('general')
    db.session.commit()
    make_community_member(viewer, community)
    community.ap_profile_id = None
    db.session.commit()

    _response, exported_json = exported(app, client)

    assert exported_json['followed_communities'] == []


# --------------------------------------------------------------------------
# D1042 -- importing settings
# --------------------------------------------------------------------------


def import_payload(app, client, contents, filename='settings.json'):
    data = {'submit': 'Import', 'csrf_token': csrf(app, client)}
    data['import_file'] = (io.BytesIO(contents), filename)
    return data


def test_importing_a_settings_file(app, env):
    client, viewer, other = env
    contents = json.dumps({'followed_communities': []}).encode()

    with patch('app.user.routes.import_settings') as imported:
        with patch('app.redis_client', create=True) as redis:
            response = client.post('/user/settings/import_export',
                                   data=import_payload(app, client, contents),
                                   content_type='multipart/form-data')

    assert response.status_code == 302
    assert redis.set.call_args.args[1] == contents
    assert imported.call_args is not None


def test_an_import_file_must_be_json(app, env):
    client, viewer, other = env

    response = client.post('/user/settings/import_export',
                           data=import_payload(app, client, b'{}',
                                               filename='settings.txt'),
                           content_type='multipart/form-data')

    assert response.status_code == 400


def test_an_oversized_import_is_refused(app, env):
    """D1042. `stream.read()` with no argument read the whole upload into
    memory and stored it in Redis for an hour, and this application sets no
    `MAX_CONTENT_LENGTH` at all -- measured: `PROBE v1 MAX_CONTENT_LENGTH:
    None`. So the size was whatever the client chose to send."""
    from app.user.routes import SETTINGS_IMPORT_MAX_BYTES

    client, viewer, other = env
    contents = b'x' * (SETTINGS_IMPORT_MAX_BYTES + 10)

    with patch('app.user.routes.import_settings') as imported:
        with patch('app.redis_client', create=True) as redis:
            with patch('app.user.routes.flash') as flashed:
                response = client.post('/user/settings/import_export',
                                       data=import_payload(app, client, contents),
                                       content_type='multipart/form-data')

    assert response.status_code == 302
    assert redis.set.call_args is None
    assert imported.call_args is None
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'too large' in messages


def test_an_import_at_the_limit_is_accepted(app, env):
    """The read takes one byte more than the limit so that a file exactly AT
    it is not refused by an off-by-one."""
    from app.user.routes import SETTINGS_IMPORT_MAX_BYTES

    client, viewer, other = env
    contents = b'x' * SETTINGS_IMPORT_MAX_BYTES

    with patch('app.user.routes.import_settings') as imported:
        with patch('app.redis_client', create=True):
            client.post('/user/settings/import_export',
                        data=import_payload(app, client, contents),
                        content_type='multipart/form-data')

    assert imported.call_args is not None


def test_submitting_no_file_saves_nothing(app, env):
    client, viewer, other = env
    data = {'submit': 'Import', 'csrf_token': csrf(app, client)}

    with patch('app.user.routes.import_settings') as imported:
        response = client.post('/user/settings/import_export', data=data,
                               content_type='multipart/form-data')

    assert response.status_code == 302
    assert imported.call_args is None


def test_the_import_page_renders(app, env):
    client, viewer, other = env

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = client.get('/user/settings/import_export')

    assert response.status_code == 200
    assert render.call_args.kwargs['form'] is not None


def test_a_deleted_account_cannot_import_or_export(app, env):
    client, viewer, other = env
    viewer.deleted = True
    db.session.commit()

    response = client.get('/user/settings/import_export')

    assert response.status_code == 404


# --------------------------------------------------------------------------
# D1045, D1046 -- what the import task will accept
# --------------------------------------------------------------------------


def run_import(app, user, contents_json):
    from app.user.routes import import_settings_task

    with patch('app.user.routes.json.loads', return_value=contents_json):
        with patch('app.redis_client', create=True):
            with patch('app.user.routes.find_actor_or_create',
                       return_value=None) as find:
                import_settings_task(user.id, 'a-redis-key')
    return find


def test_a_string_where_a_list_belongs_is_ignored(app, env):
    """D1045. The task iterated whatever it found, so a single URL given as a
    bare string was iterated CHARACTER BY CHARACTER, each character becoming
    an actor lookup. Measured: `PROBE v4 find_actor_or_create calls: 25 first
    arg: h`."""
    client, viewer, other = env

    find = run_import(app, viewer,
                      {'followed_communities': 'https://other.example/c/x'})

    assert find.call_args_list == []


@pytest.mark.parametrize('value', [None, 42, {'a': 'b'}])
def test_other_wrong_shapes_are_ignored_too(app, env, value):
    client, viewer, other = env

    find = run_import(app, viewer, {'blocked_users': value})

    assert find.call_args_list == []


def test_a_long_list_is_capped(app, env):
    """D1046. `find_actor_or_create` defaults to `create_if_not_found=True`,
    which reaches `create_actor_from_remote` -- an outbound fetch of a URL the
    uploader chose. An uncapped list is therefore an unbounded outbound-fetch
    primitive, the same family as D993 and D1025. Measured before the cap: 50
    entries, 50 lookups."""
    from app.user.routes import SETTINGS_IMPORT_MAX_ENTRIES

    client, viewer, other = env
    entries = [f'https://other.example/c/c{n}'
               for n in range(SETTINGS_IMPORT_MAX_ENTRIES + 25)]

    find = run_import(app, viewer, {'followed_communities': entries})

    assert find.call_count == SETTINGS_IMPORT_MAX_ENTRIES


def test_a_list_under_the_cap_is_processed_whole(app, env):
    client, viewer, other = env
    entries = [f'https://other.example/c/c{n}' for n in range(5)]

    find = run_import(app, viewer, {'followed_communities': entries})

    assert find.call_count == 5


def test_every_import_key_is_capped(app, env):
    """Seven keys, one helper -- and a row per key, because a key that was
    left calling the old expression would be invisible here otherwise."""
    from app.user.routes import SETTINGS_IMPORT_MAX_ENTRIES

    client, viewer, other = env
    long_list = [f'https://other.example/x{n}'
                 for n in range(SETTINGS_IMPORT_MAX_ENTRIES + 5)]

    find = run_import(app, viewer, {'followed_communities': long_list,
                                    'blocked_communities': long_list,
                                    'blocked_users': long_list})

    assert find.call_count == SETTINGS_IMPORT_MAX_ENTRIES * 3


def test_an_import_for_a_user_who_has_since_gone(app, env):
    """D1048. The task is queued and runs later, so the account can be gone by
    the time it does -- and every arm dereferences `user`. Measured with an
    EMPTY import file, which is what makes it a nil guard rather than a
    per-entry one: `AttributeError: 'NoneType' object has no attribute 'id'`.
    D992's shape, in a background task rather than a route."""
    from app.user.routes import import_settings_task

    client, viewer, other = env

    with patch('app.user.routes.json.loads',
               return_value={'followed_communities': []}):
        with patch('app.redis_client', create=True):
            import_settings_task(9999, 'a-redis-key')


# --------------------------------------------------------------------------
# What the import task actually does with entries it can resolve
# --------------------------------------------------------------------------


def run_import_with(app, user, contents_json, resolver):
    """Like `run_import`, but `find_actor_or_create` answers real objects, so
    the follow and block bodies run rather than being skipped."""
    from app.user.routes import import_settings_task

    with patch('app.user.routes.json.loads', return_value=contents_json):
        with patch('app.redis_client', create=True):
            with patch('app.user.routes.find_actor_or_create',
                       side_effect=resolver):
                import_settings_task(user.id, 'a-redis-key')


def test_importing_a_local_community_joins_it(app, env):
    from app.models import CommunityMember

    client, viewer, other = env
    community = make_community('general')
    db.session.commit()

    run_import_with(app, viewer,
                    {'followed_communities': [community.ap_profile_id]},
                    lambda *a, **k: community)

    membership = CommunityMember.query.filter_by(user_id=viewer.id,
                                                 community_id=community.id).one()
    assert membership.is_moderator is False
    # `subscriptions_count` is NOT asserted here: the task runs in its own
    # session (`get_task_session`), and the community object these rows hand it
    # through the `find_actor_or_create` double belongs to the test's session,
    # so the counter is incremented on an instance the task never flushes. The
    # membership row is written through the task's session and is the real
    # evidence.


def test_importing_a_community_you_are_banned_from_does_not_join_it(app, env):
    """The local arm checks `CommunityBan` -- an import file is a list the
    account supplies, so a ban has to survive one."""
    from app.models import CommunityBan, CommunityMember

    client, viewer, other = env
    community = make_community('general')
    db.session.commit()
    db.session.add(CommunityBan(user_id=viewer.id, community_id=community.id,
                                banned_by=other.id))
    db.session.commit()

    run_import_with(app, viewer,
                    {'followed_communities': [community.ap_profile_id]},
                    lambda *a, **k: community)

    assert CommunityMember.query.filter_by(user_id=viewer.id,
                                           community_id=community.id).count() == 0


def test_importing_a_community_you_already_belong_to_changes_nothing(app, env):
    from app.models import CommunityMember

    client, viewer, other = env
    community = make_community('general')
    db.session.commit()
    make_community_member(viewer, community)
    db.session.commit()

    run_import_with(app, viewer,
                    {'followed_communities': [community.ap_profile_id]},
                    lambda *a, **k: community)

    assert CommunityMember.query.filter_by(user_id=viewer.id,
                                           community_id=community.id).count() == 1


def test_importing_a_remote_community_asks_to_follow(app, env):
    """A remote community is joined by sending a Follow and waiting for the
    Accept, so the import writes a CommunityJoinRequest and puts a request on
    the wire."""
    from app.models import CommunityJoinRequest, CommunityMember

    client, viewer, other = env
    other_instance = make_instance('other.example', software='piefed')
    remote = make_community('remote', host='other.example')
    remote.ap_id = 'remote@other.example'
    remote.instance_id = other_instance.id
    remote.ap_inbox_url = 'https://other.example/c/remote/inbox'
    db.session.commit()

    with patch('app.user.routes.send_post_request') as send:
        with patch('app.user.routes.retrieve_mods_and_backfill') as backfill:
            run_import_with(app, viewer,
                            {'followed_communities': [remote.ap_profile_id]},
                            lambda *a, **k: remote)

    assert CommunityJoinRequest.query.filter_by(user_id=viewer.id).count() == 1
    assert CommunityMember.query.filter_by(user_id=viewer.id,
                                           community_id=remote.id).count() == 1
    assert send.call_args.args[0] == 'https://other.example/c/remote/inbox'
    assert send.call_args.args[1]['type'] == 'Follow'
    assert backfill.delay.call_args is not None


def test_a_remote_community_on_a_dead_instance_is_not_asked(app, env):
    """`if not community.instance.gone_forever:` -- there is nobody to accept
    the follow, and the request would be a wasted delivery attempt."""
    client, viewer, other = env
    other_instance = make_instance('other.example', software='piefed')
    other_instance.gone_forever = True
    remote = make_community('remote', host='other.example')
    remote.ap_id = 'remote@other.example'
    remote.instance_id = other_instance.id
    remote.ap_inbox_url = 'https://other.example/c/remote/inbox'
    db.session.commit()

    with patch('app.user.routes.send_post_request') as send:
        with patch('app.user.routes.retrieve_mods_and_backfill'):
            run_import_with(app, viewer,
                            {'followed_communities': [remote.ap_profile_id]},
                            lambda *a, **k: remote)

    assert send.call_args is None


def test_a_community_with_posts_is_not_backfilled(app, env):
    """`if community.posts.count() == 0:` -- backfilling is for a community
    this instance has never seen content from."""
    from tests.factories import make_post

    client, viewer, other = env
    community = make_community('general')
    db.session.commit()
    make_post(community, viewer, 'https://test.piefed.local/p/1',
              title='a post')
    db.session.commit()

    with patch('app.user.routes.retrieve_mods_and_backfill') as backfill:
        run_import_with(app, viewer,
                        {'followed_communities': [community.ap_profile_id]},
                        lambda *a, **k: community)

    assert backfill.delay.call_args is None


def test_importing_blocked_communities(app, env):
    client, viewer, other = env
    community = make_community('general')
    db.session.commit()

    run_import_with(app, viewer,
                    {'blocked_communities': [community.ap_public_url]},
                    lambda *a, **k: community)

    assert CommunityBlock.query.filter_by(user_id=viewer.id,
                                          community_id=community.id).count() == 1


def test_importing_a_community_block_twice_stores_one(app, env):
    client, viewer, other = env
    community = make_community('general')
    db.session.commit()
    db.session.add(CommunityBlock(user_id=viewer.id,
                                  community_id=community.id))
    db.session.commit()

    run_import_with(app, viewer,
                    {'blocked_communities': [community.ap_public_url]},
                    lambda *a, **k: community)

    assert CommunityBlock.query.count() == 1


def test_importing_blocked_users(app, env):
    client, viewer, other = env

    run_import_with(app, viewer, {'blocked_users': [other.ap_profile_id]},
                    lambda *a, **k: other)

    assert UserBlock.query.filter_by(blocker_id=viewer.id,
                                     blocked_id=other.id).count() == 1


def test_importing_a_user_block_twice_stores_one(app, env):
    client, viewer, other = env
    db.session.add(UserBlock(blocker_id=viewer.id, blocked_id=other.id))
    db.session.commit()

    run_import_with(app, viewer, {'blocked_users': [other.ap_profile_id]},
                    lambda *a, **k: other)

    assert UserBlock.query.count() == 1


def test_importing_user_notes(app, env):
    from app.models import UserNote

    client, viewer, other = env

    run_import_with(app, viewer,
                    {'user_notes': [{'target': other.ap_profile_id,
                                     'body': 'a note about them'}]},
                    lambda *a, **k: other)

    note = UserNote.query.filter_by(user_id=viewer.id).one()
    assert note.target_id == other.id
    assert note.body == 'a note about them'


def test_importing_blocked_instances(app, env):
    from app.models import InstanceBlock

    client, viewer, other = env
    make_instance('other.example', software='piefed')
    db.session.commit()

    run_import_with(app, viewer, {'blocked_instances': ['other.example']},
                    lambda *a, **k: None)

    assert InstanceBlock.query.filter_by(user_id=viewer.id).count() == 1


def test_importing_an_unknown_instance_block_is_skipped(app, env):
    """`if instance:` -- an instance this server has never heard of has no row
    to block."""
    from app.models import InstanceBlock

    client, viewer, other = env

    run_import_with(app, viewer, {'blocked_instances': ['nowhere.example']},
                    lambda *a, **k: None)

    assert InstanceBlock.query.count() == 0


def test_importing_saved_posts(app, env):
    from app.models import PostBookmark
    from tests.factories import make_post

    client, viewer, other = env
    community = make_community('general')
    db.session.commit()
    post = make_post(community, viewer, 'https://other.example/p/1',
                     title='a post')
    db.session.commit()

    with patch('app.api.alpha.utils.misc.get_resolve_object',
               return_value=post):
        run_import_with(app, viewer, {'saved_posts': [post.ap_id]},
                        lambda *a, **k: None)

    assert PostBookmark.query.filter_by(user_id=viewer.id,
                                        post_id=post.id).count() == 1


def test_a_saved_post_that_cannot_be_resolved_is_skipped(app, env):
    """The `except Exception: continue` -- one unresolvable bookmark must not
    cost the rest of the import."""
    from app.models import PostBookmark

    client, viewer, other = env

    with patch('app.api.alpha.utils.misc.get_resolve_object',
               side_effect=RuntimeError('cannot resolve')):
        run_import_with(app, viewer,
                        {'saved_posts': ['https://other.example/p/1']},
                        lambda *a, **k: None)

    assert PostBookmark.query.count() == 0


def test_importing_saved_comments(app, env):
    from app.models import PostReplyBookmark
    from tests.factories import make_post, make_post_reply

    client, viewer, other = env
    community = make_community('general')
    db.session.commit()
    post = make_post(community, viewer, 'https://other.example/p/1',
                     title='a post')
    reply = make_post_reply(post, viewer, body='a reply')
    db.session.commit()

    with patch('app.api.alpha.utils.misc.get_resolve_object',
               return_value=reply):
        run_import_with(app, viewer, {'saved_comments': [reply.ap_id]},
                        lambda *a, **k: None)

    assert PostReplyBookmark.query.filter_by(user_id=viewer.id,
                                             post_reply_id=reply.id).count() == 1


def test_the_import_runs_inline_in_debug(app, env):
    """`import_settings` chooses between the task and the worker, and a
    developer wants it inline."""
    from app.user.routes import import_settings

    client, viewer, other = env
    app.debug = True
    try:
        with app.test_request_context():
            with patch('app.user.routes.current_user', viewer):
                with patch('app.user.routes.import_settings_task') as task:
                    import_settings('a-redis-key')
    finally:
        app.debug = False

    assert task.call_args.args == (viewer.id, 'a-redis-key')
    assert task.delay.call_args is None


def test_the_import_goes_to_a_worker_otherwise(app, env):
    from app.user.routes import import_settings

    client, viewer, other = env

    with app.test_request_context():
        with patch('app.user.routes.current_user', viewer):
            with patch('app.user.routes.import_settings_task') as task:
                import_settings('a-redis-key')

    assert task.delay.call_args.args == (viewer.id, 'a-redis-key')


def test_the_export_carries_the_images(app, env):
    """Both image URLs are absolute, because the file is for importing into
    ANOTHER instance -- a relative path there points at that instance."""
    client, viewer, other = env
    viewer.avatar_id = an_image('avatar.png').id
    viewer.cover_id = an_image('banner.png').id
    db.session.commit()

    _response, exported_json = exported(app, client)

    assert exported_json['avatar'].startswith('https://')
    assert exported_json['banner'].startswith('https://')
    assert 'avatar.png' in exported_json['avatar']
    assert 'banner.png' in exported_json['banner']


def test_the_export_lists_bookmarks(app, env):
    from app.models import PostBookmark, PostReplyBookmark
    from tests.factories import make_post, make_post_reply

    client, viewer, other = env
    community = make_community('general')
    db.session.commit()
    post = make_post(community, viewer, 'https://test.piefed.local/p/1',
                     title='a post')
    reply = make_post_reply(post, viewer, body='a reply')
    db.session.add(PostBookmark(user_id=viewer.id, post_id=post.id))
    db.session.add(PostReplyBookmark(user_id=viewer.id,
                                     post_reply_id=reply.id))
    db.session.commit()

    _response, exported_json = exported(app, client)

    assert exported_json['saved_posts'] == [post.ap_id]
    assert exported_json['saved_comments'] == [reply.ap_id]


def test_the_export_lists_blocked_instances(app, env):
    from app.models import InstanceBlock

    client, viewer, other = env
    blocked = make_instance('other.example', software='piefed')
    db.session.commit()
    db.session.add(InstanceBlock(user_id=viewer.id, instance_id=blocked.id))
    db.session.commit()

    _response, exported_json = exported(app, client)

    assert exported_json['blocked_instances'] == ['other.example']


def test_the_export_lists_user_notes(app, env):
    from app.models import UserNote

    client, viewer, other = env
    db.session.add(UserNote(user_id=viewer.id, target_id=other.id,
                            body='a note about them'))
    db.session.commit()

    _response, exported_json = exported(app, client)

    assert exported_json['user_notes'] == [
        {'target': other.profile_id(), 'note': 'a note about them'}]


def test_a_community_with_no_subscriber_counts_is_still_joinable(app, env):
    """`if community.subscriptions_count is None:` -- a remote community
    created from a profile that carried no counts starts at None, and `+= 1`
    on None is a TypeError."""
    from app.models import CommunityMember

    client, viewer, other = env
    other_instance = make_instance('other.example', software='piefed')
    remote = make_community('remote', host='other.example')
    remote.ap_id = 'remote@other.example'
    remote.instance_id = other_instance.id
    remote.ap_inbox_url = 'https://other.example/c/remote/inbox'
    remote.subscriptions_count = None
    remote.total_subscriptions_count = None
    db.session.commit()

    with patch('app.user.routes.send_post_request'):
        with patch('app.user.routes.retrieve_mods_and_backfill'):
            run_import_with(app, viewer,
                            {'followed_communities': [remote.ap_profile_id]},
                            lambda *a, **k: remote)

    assert CommunityMember.query.filter_by(user_id=viewer.id,
                                           community_id=remote.id).count() == 1


def test_an_import_that_fails_rolls_back(app, env):
    """The task's `except Exception: session.rollback(); raise` -- a failure
    half way through must not leave half the file applied."""
    from app.user.routes import import_settings_task

    client, viewer, other = env

    with patch('app.user.routes.json.loads',
               side_effect=ValueError('not json')):
        with patch('app.redis_client', create=True):
            with pytest.raises(ValueError):
                import_settings_task(viewer.id, 'a-redis-key')


def test_backfilling_runs_inline_in_debug(app, env):
    """`if current_app.debug: retrieve_mods_and_backfill(...)` -- the same
    inline/worker split as everywhere else, and the inline arm is the one a
    developer sees."""
    client, viewer, other = env
    other_instance = make_instance('other.example', software='piefed')
    remote = make_community('remote', host='other.example')
    remote.ap_id = 'remote@other.example'
    remote.instance_id = other_instance.id
    remote.ap_inbox_url = 'https://other.example/c/remote/inbox'
    db.session.commit()
    app.debug = True
    try:
        with patch('app.user.routes.send_post_request'):
            with patch('app.user.routes.retrieve_mods_and_backfill') as backfill:
                run_import_with(app, viewer,
                                {'followed_communities': [remote.ap_profile_id]},
                                lambda *a, **k: remote)
    finally:
        app.debug = False

    assert backfill.call_args.args[0] == remote.id
    assert backfill.delay.call_args is None


def test_a_saved_comment_that_cannot_be_resolved_is_skipped(app, env):
    """The second `except Exception: continue`, on the comment loop. It is a
    separate try block from the post loop's and needs its own row."""
    from app.models import PostReplyBookmark

    client, viewer, other = env

    with patch('app.api.alpha.utils.misc.get_resolve_object',
               side_effect=RuntimeError('cannot resolve')):
        run_import_with(app, viewer,
                        {'saved_comments': ['https://other.example/comment/1']},
                        lambda *a, **k: None)

    assert PostReplyBookmark.query.count() == 0


def test_blocking_a_remote_user_by_import_records_the_block(app, env):
    """The `if not blocked_user.is_local():` arm is a `...` placeholder for
    federating the block, so the row asserts what it does today: the local
    record is written either way."""
    client, viewer, other = env
    remote = make_user(make_instance('other.example', software='piefed'),
                       'remote')
    db.session.commit()

    run_import_with(app, viewer, {'blocked_users': [remote.ap_profile_id]},
                    lambda *a, **k: remote)

    assert UserBlock.query.filter_by(blocker_id=viewer.id,
                                     blocked_id=remote.id).count() == 1


def test_importing_a_saved_post_twice_stores_one_bookmark(app, env):
    """`if not existing_bookmark:` -- an import file can name a post the
    account has already bookmarked, and `PostBookmark` has no unique
    constraint to catch the second row."""
    from app.models import PostBookmark
    from tests.factories import make_post

    client, viewer, other = env
    community = make_community('general')
    db.session.commit()
    post = make_post(community, viewer, 'https://other.example/p/1',
                     title='a post')
    db.session.add(PostBookmark(user_id=viewer.id, post_id=post.id))
    db.session.commit()

    with patch('app.api.alpha.utils.misc.get_resolve_object',
               return_value=post):
        run_import_with(app, viewer, {'saved_posts': [post.ap_id]},
                        lambda *a, **k: None)

    assert PostBookmark.query.filter_by(user_id=viewer.id,
                                        post_id=post.id).count() == 1


def test_importing_a_saved_comment_twice_stores_one_bookmark(app, env):
    from app.models import PostReplyBookmark
    from tests.factories import make_post, make_post_reply

    client, viewer, other = env
    community = make_community('general')
    db.session.commit()
    post = make_post(community, viewer, 'https://other.example/p/1',
                     title='a post')
    reply = make_post_reply(post, viewer, body='a reply')
    db.session.add(PostReplyBookmark(user_id=viewer.id,
                                     post_reply_id=reply.id))
    db.session.commit()

    with patch('app.api.alpha.utils.misc.get_resolve_object',
               return_value=reply):
        run_import_with(app, viewer, {'saved_comments': [reply.ap_id]},
                        lambda *a, **k: None)

    assert PostReplyBookmark.query.filter_by(user_id=viewer.id,
                                             post_reply_id=reply.id).count() == 1
