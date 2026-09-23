"""The user API's settings and notifications, which close
`app/api/alpha/utils/user.py`.

Sub-project 84, slice E. Three defects, all measured:

* `put_user_save_user_settings` read `data['default_sort_type']` and tested
  for `'default_sort'` -- two different keys, and the schema declares the
  first, so **both sort settings were unchangeable through the API**,
  silently (D1186);
* `put_user_notification_state` built its response before writing, so
  marking a notification read answered `'Unread'` (D1187);
* `put_user_mark_all_notifications_read` updated the rows and left
  `user.unread_notifications` alone -- and that column is what the unread
  count answers with, so the badge kept its number (D1188).
"""
import pytest
from flask import g

from app.constants import (NOTIF_COMMUNITY, NOTIF_FEED, NOTIF_MENTION,
                           NOTIF_POST, NOTIF_REPLY, NOTIF_REPORT, NOTIF_TOPIC,
                           NOTIF_USER)
from app import db
from app.models import (File, Notification, Site, User, UserExtraField,
                        utcnow)
from tests.factories import make_post_reply, make_user


def token(user):
    return f'Bearer {user.encode_jwt_token()}'


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    user = api_baseline.user2
    author = api_baseline.user3
    return user, author, api_baseline


def settings(user, **data):
    from app.api.alpha.utils.user import put_user_save_user_settings

    return put_user_save_user_settings(token(user), data)


def a_notification(reader, author, notif_type, subtype, targets, read=False):
    notification = Notification(title='something', url='/', user_id=reader.id,
                                author_id=author.id, notif_type=notif_type,
                                subtype=subtype, read=read, targets=targets)
    db.session.add(notification)
    db.session.commit()
    return notification


# --------------------------------------------------------------------------
# D1186 -- two settings nobody could change
# --------------------------------------------------------------------------


def test_the_default_sort_can_be_changed(app, env):
    """D1186. The code read `default_sort_type` and tested for
    `default_sort`, and marshmallow passes only the declared field -- so the
    test could never be true. Measured: `PROBE bh1 outcome: accepted |
    default_sort now: 'hot'` after asking for 'New'."""
    user, author, baseline = env
    user.default_sort = 'hot'
    db.session.commit()

    settings(user, default_sort_type='New')

    assert user.default_sort == 'new'


def test_the_default_comment_sort_can_be_changed(app, env):
    """D1186's twin. Measured: `PROBE bh3 default_comment_sort now: 'hot'`."""
    user, author, baseline = env
    user.default_comment_sort = 'hot'
    db.session.commit()

    settings(user, default_comment_sort_type='New')

    assert user.default_comment_sort == 'new'


def test_the_sorts_are_left_alone_when_they_are_not_sent(app, env):
    user, author, baseline = env
    user.default_sort = 'top'
    user.default_comment_sort = 'top'
    db.session.commit()

    settings(user, bot=True)

    assert user.default_sort == 'top'
    assert user.default_comment_sort == 'top'


# --------------------------------------------------------------------------
# The rest of the settings
# --------------------------------------------------------------------------


@pytest.mark.parametrize('visibility,stored', [('Show', 0), ('Hide', 1),
                                               ('Blur', 2), ('Transparent', 3)])
def test_the_nsfw_visibility_is_written(app, env, visibility, stored):
    user, author, baseline = env

    settings(user, nsfw_visibility=visibility)

    assert user.hide_nsfw == stored


@pytest.mark.parametrize('visibility,stored', [('Show', 0), ('Hide', 1),
                                               ('Blur', 2), ('Transparent', 3)])
def test_the_nsfl_visibility_is_written(app, env, visibility, stored):
    user, author, baseline = env

    settings(user, nsfl_visibility=visibility)

    assert user.hide_nsfl == stored


@pytest.mark.parametrize('visibility,stored', [('Show', 0), ('Hide', 1),
                                               ('Label', 2), ('Transparent', 3)])
def test_the_ai_visibility_is_written(app, env, visibility, stored):
    user, author, baseline = env

    settings(user, genai_visibility=visibility)

    assert user.hide_gen_ai == stored


@pytest.mark.parametrize('visibility,stored', [('Show', 0), ('Hide', 1),
                                               ('Blur', 2), ('Transparent', 3)])
def test_the_bot_visibility_is_written(app, env, visibility, stored):
    user, author, baseline = env

    settings(user, bot_visibility=visibility)

    assert user.ignore_bots == stored


@pytest.mark.parametrize('shown,stored', [(True, 0), (False, 1)])
def test_the_older_boolean_still_works_for_nsfw(app, env, shown, stored):
    """`show_nsfw` is the pre-visibility field, and it is only consulted when
    no `nsfw_visibility` was sent."""
    user, author, baseline = env

    settings(user, show_nsfw=shown)

    assert user.hide_nsfw == stored


@pytest.mark.parametrize('shown,stored', [(True, 0), (False, 1)])
def test_the_older_boolean_still_works_for_nsfl(app, env, shown, stored):
    user, author, baseline = env

    settings(user, show_nsfl=shown)

    assert user.hide_nsfl == stored


@pytest.mark.parametrize('shown,stored', [(True, 0), (False, 1)])
def test_the_older_boolean_still_works_for_ai(app, env, shown, stored):
    user, author, baseline = env

    settings(user, show_genai=shown)

    assert user.hide_gen_ai == stored


def test_the_visibility_wins_over_the_older_boolean(app, env):
    user, author, baseline = env

    settings(user, nsfw_visibility='Blur', show_nsfw=True)

    assert user.hide_nsfw == 2


def test_a_restricted_country_hides_it_whatever_was_asked_for(app, env):
    """The instance's own rule overrides the account's preference."""
    from unittest.mock import patch

    user, author, baseline = env

    with patch('app.api.alpha.utils.user.user_in_restricted_country',
               return_value=True):
        settings(user, nsfw_visibility='Show', nsfl_visibility='Show')

    assert user.hide_nsfw == 1
    assert user.hide_nsfl == 1


@pytest.mark.parametrize('shown,hidden', [(True, False), (False, True)])
def test_whether_read_posts_are_shown(app, env, shown, hidden):
    user, author, baseline = env

    settings(user, show_read_posts=shown)

    assert user.hide_read_posts is hidden


def test_the_biography_is_rendered(app, env):
    user, author, baseline = env

    settings(user, bio='hello **world**')

    assert user.about == 'hello **world**'
    assert '<strong>world</strong>' in user.about_html


def test_an_empty_biography_is_still_a_biography(app, env):
    """`isinstance(about, str)`, not truthiness: clearing it is a change."""
    user, author, baseline = env
    settings(user, bio='something')

    settings(user, bio='')

    assert user.about == ''


@pytest.mark.parametrize('setting,column,value', [
    ('hide_low_quality', 'hide_low_quality', True),
    ('hide_low_quality', 'hide_low_quality', False),
    ('newsletter', 'newsletter', True),
    ('newsletter', 'newsletter', False),
    ('email_unread', 'email_unread', True),
    ('email_unread', 'email_unread', False),
    ('searchable', 'searchable', True),
    ('searchable', 'searchable', False),
    ('indexable', 'indexable', True),
    ('indexable', 'indexable', False),
    ('feed_auto_follow', 'feed_auto_follow', True),
    ('feed_auto_follow', 'feed_auto_follow', False),
    ('feed_auto_leave', 'feed_auto_leave', True),
    ('feed_auto_leave', 'feed_auto_leave', False),
    ('bot', 'bot', True),
    ('bot', 'bot', False),
])
def test_a_boolean_setting_is_written_both_ways(app, env, setting, column,
                                                value):
    user, author, baseline = env
    setattr(user, column, not value)
    db.session.commit()

    settings(user, **{setting: value})

    assert getattr(user, column) is value


@pytest.mark.parametrize('federate,privately', [(True, False), (False, True)])
def test_federating_votes_is_stored_the_other_way_round(app, env, federate,
                                                        privately):
    """The API asks whether votes are federated; the column records whether
    they are private."""
    user, author, baseline = env

    settings(user, federate_votes=federate)

    assert user.vote_privately is privately


@pytest.mark.parametrize('accepts,stored', [('None', 0), ('Local', 1),
                                            ('Trusted', 2), ('All', 3)])
def test_who_may_send_a_private_message(app, env, accepts, stored):
    user, author, baseline = env

    settings(user, accept_private_messages=accepts)

    assert user.accept_private_messages == stored


def test_the_comment_thresholds_are_written(app, env):
    user, author, baseline = env

    settings(user, reply_collapse_threshold=-5, reply_hide_threshold=-10)

    assert user.reply_collapse_threshold == -5
    assert user.reply_hide_threshold == -10


def test_the_keyword_filter_is_stored_as_one_string(app, env):
    user, author, baseline = env

    settings(user, community_keyword_filter=['spam', 'adverts'])

    assert user.community_keyword_filter == 'spam, adverts'


def test_a_null_keyword_filter_clears_it(app, env):
    user, author, baseline = env
    settings(user, community_keyword_filter=['spam'])

    settings(user, community_keyword_filter=None)

    assert user.community_keyword_filter == ''


def test_the_display_name_is_written(app, env):
    user, author, baseline = env

    settings(user, display_name='A Better Name')

    assert user.title == 'A Better Name'


def test_a_null_display_name_removes_it(app, env):
    user, author, baseline = env
    settings(user, display_name='A Better Name')

    settings(user, display_name=None)

    assert user.title is None


def test_the_reserved_display_name_is_refused(app, env):
    """`[deleted]` is what a deleted account is shown as."""
    user, author, baseline = env

    with pytest.raises(Exception) as refused:
        settings(user, display_name='[deleted]')

    assert str(refused.value) == 'this display name is reserved'


def test_an_avatar_is_set_and_removed(app, env):
    from unittest.mock import patch

    user, author, baseline = env

    with patch('app.api.alpha.utils.user.make_image_sizes') as sized:
        settings(user, avatar='https://example.com/a.png')
    assert db.session.get(File, user.avatar_id).source_url == \
        'https://example.com/a.png'
    sized.assert_called_once()

    with patch.object(File, 'delete_from_disk') as deleted:
        settings(user, avatar=None)
    assert user.avatar_id is None
    deleted.assert_called_once()


def test_a_replaced_avatar_takes_the_old_one_off_the_disk(app, env):
    from unittest.mock import patch

    user, author, baseline = env
    with patch('app.api.alpha.utils.user.make_image_sizes'):
        settings(user, avatar='https://example.com/a.png')

        with patch.object(File, 'delete_from_disk') as deleted:
            settings(user, avatar='https://example.com/b.png')

    assert db.session.get(File, user.avatar_id).source_url == \
        'https://example.com/b.png'
    deleted.assert_called_once()


def test_a_cover_is_set_and_removed(app, env):
    from unittest.mock import patch

    user, author, baseline = env

    with patch('app.api.alpha.utils.user.make_image_sizes') as sized:
        settings(user, cover='https://example.com/c.png')
    assert db.session.get(File, user.cover_id).source_url == \
        'https://example.com/c.png'
    sized.assert_called_once()

    with patch.object(File, 'delete_from_disk') as deleted:
        settings(user, cover=None)
    assert user.cover_id is None
    deleted.assert_called_once()


def test_a_replaced_cover_takes_the_old_one_off_the_disk(app, env):
    from unittest.mock import patch

    user, author, baseline = env
    with patch('app.api.alpha.utils.user.make_image_sizes'):
        settings(user, cover='https://example.com/c.png')

        with patch.object(File, 'delete_from_disk') as deleted:
            settings(user, cover='https://example.com/d.png')

    assert deleted.called


def test_removing_an_image_nobody_set_is_not_an_error(app, env):
    user, author, baseline = env

    settings(user, avatar=None, cover=None)

    assert user.avatar_id is None
    assert user.cover_id is None


# --------------------------------------------------------------------------
# Extra fields
# --------------------------------------------------------------------------


def test_extra_fields_are_added(app, env):
    user, author, baseline = env

    settings(user, extra_fields=[{'label': ' Website ', 'text': ' here '}])

    field = UserExtraField.query.one()
    assert (field.label, field.text) == ('Website', 'here')


def test_an_extra_field_is_edited(app, env):
    user, author, baseline = env
    settings(user, extra_fields=[{'label': 'Website', 'text': 'here'}])
    field = UserExtraField.query.one()

    settings(user, extra_fields=[{'id': field.id, 'label': 'Homepage',
                                  'text': 'there'}])

    assert (field.label, field.text) == ('Homepage', 'there')


def test_an_extra_field_with_nothing_in_it_is_removed(app, env):
    user, author, baseline = env
    settings(user, extra_fields=[{'label': 'Website', 'text': 'here'}])
    field = UserExtraField.query.one()

    settings(user, extra_fields=[{'id': field.id, 'label': '', 'text': ''}])

    assert UserExtraField.query.count() == 0


def test_somebody_elses_extra_field_is_refused(app, env):
    """The id is the caller's to prove: an extra field belongs to one
    account."""
    user, author, baseline = env
    settings(author, extra_fields=[{'label': 'Website', 'text': 'theirs'}])
    theirs = UserExtraField.query.one()

    with pytest.raises(Exception) as refused:
        settings(user, extra_fields=[{'id': theirs.id, 'label': 'mine',
                                      'text': 'mine'}])

    assert 'belongs to different user' in str(refused.value)
    assert theirs.text == 'theirs'


def test_a_fifth_extra_field_is_refused(app, env):
    user, author, baseline = env
    settings(user, extra_fields=[{'label': f'field {n}', 'text': str(n)}
                                 for n in range(4)])

    with pytest.raises(Exception) as refused:
        settings(user, extra_fields=[{'label': 'one too many', 'text': 'x'}])

    assert str(refused.value) == 'Cannot have more than four extra fields'
    assert UserExtraField.query.count() == 4


def test_an_extra_field_with_no_label_is_not_created(app, env):
    user, author, baseline = env

    settings(user, extra_fields=[{'label': '', 'text': 'orphan'}])

    assert UserExtraField.query.count() == 0


def test_the_settings_come_back_as_they_were_saved(app, env):
    user, author, baseline = env

    answer = settings(user, nsfw_visibility='Blur', bot=True)

    assert answer['my_user']['local_user_view']['local_user'][
        'nsfw_visibility'] == 'Blur'


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------


def a_post_notification(reader, author, post, notif_type, subtype, **extra):
    targets = {'gen': '0', 'post_id': post.id}
    targets.update(extra)
    return a_notification(reader, author, notif_type, subtype, targets)


def test_the_notification_list_carries_each_supported_kind(app, env):
    from app.api.alpha.utils.user import get_user_notifications

    user, author, baseline = env
    reply = make_post_reply(baseline.post1, author, body='a reply')
    reply.body_html = '<p>a reply</p>'
    db.session.commit()
    a_post_notification(user, author, baseline.post1, NOTIF_USER, 'new_post')
    a_post_notification(user, author, baseline.post1, NOTIF_COMMUNITY,
                        'new_post_in_followed_community',
                        community_id=baseline.community1.id)
    a_post_notification(user, author, baseline.post1, NOTIF_TOPIC, 'new_post')
    a_post_notification(user, author, baseline.post1, NOTIF_FEED, 'new_post')
    a_post_notification(user, author, baseline.post1, NOTIF_POST,
                        'top_level_comment_on_followed_post',
                        comment_id=reply.id)
    a_post_notification(user, author, baseline.post1, NOTIF_REPLY,
                        'new_reply_on_followed_comment', comment_id=reply.id)
    a_post_notification(user, author, baseline.post1, NOTIF_MENTION,
                        'post_mention')
    a_notification(user, author, NOTIF_MENTION, 'comment_mention',
                   {'gen': '0', 'comment_id': reply.id})

    answer = get_user_notifications(token(user), {'status': 'All'})

    assert len(answer['items']) == 8
    assert answer['counts'] == {'total': 8, 'unread': 8, 'read': 0}
    assert answer['username'] == user.user_name


def test_an_unsupported_kind_is_left_out(app, env):
    from app.api.alpha.utils.user import get_user_notifications

    user, author, baseline = env
    a_notification(user, author, NOTIF_REPORT, 'new_report', {'gen': '0'})

    answer = get_user_notifications(token(user), {'status': 'All'})

    assert answer['items'] == []
    assert answer['counts']['total'] == 1


def test_a_notification_whose_target_is_gone_is_skipped(app, env):
    """"Something couldn't be fetched from the db, just skip" -- a post that
    has since been deleted leaves the rest of the list readable."""
    from app.api.alpha.utils.user import get_user_notifications

    user, author, baseline = env
    a_notification(user, author, NOTIF_FEED, 'new_post',
                   {'gen': '0', 'post_id': 999999})

    answer = get_user_notifications(token(user), {'status': 'All'})

    assert answer['items'] == []


@pytest.mark.parametrize('status', ['Unread', 'New'])
def test_the_unread_list_leaves_out_what_was_read(app, env, status):
    from app.api.alpha.utils.user import get_user_notifications

    user, author, baseline = env
    unread = a_post_notification(user, author, baseline.post1, NOTIF_FEED,
                                 'new_post')
    read = a_notification(user, author, NOTIF_FEED, 'new_post',
                          {'gen': '0', 'post_id': baseline.post1.id},
                          read=True)

    answer = get_user_notifications(token(user), {'status': status})

    assert [i['notif_id'] for i in answer['items']] == [unread.id]
    assert answer['counts'] == {'total': 2, 'unread': 1, 'read': 1}


def test_the_read_list_is_the_other_half(app, env):
    from app.api.alpha.utils.user import get_user_notifications

    user, author, baseline = env
    a_post_notification(user, author, baseline.post1, NOTIF_FEED, 'new_post')
    read = a_notification(user, author, NOTIF_FEED, 'new_post',
                          {'gen': '0', 'post_id': baseline.post1.id},
                          read=True)

    answer = get_user_notifications(token(user), {'status': 'Read'})

    assert [i['notif_id'] for i in answer['items']] == [read.id]


def test_a_notification_with_no_subtype_is_left_out(app, env):
    """`isinstance(item.subtype, str)`: the column is nullable."""
    from app.api.alpha.utils.user import get_user_notifications

    user, author, baseline = env
    notification = a_post_notification(user, author, baseline.post1,
                                       NOTIF_FEED, 'new_post')
    notification.subtype = None
    db.session.commit()

    assert get_user_notifications(token(user), {'status': 'All'})['items'] == []


def test_the_notification_list_is_paged_and_capped(app, env):
    from app.api.alpha.utils.user import get_user_notifications

    user, author, baseline = env
    for _ in range(3):
        a_post_notification(user, author, baseline.post1, NOTIF_FEED,
                            'new_post')

    with pytest.MonkeyPatch.context() as patched:
        patched.setitem(app.config, 'PAGE_LENGTH', 2)
        answer = get_user_notifications(token(user), {'status': 'All',
                                                      'limit': 100})

    assert len(answer['items']) == 2
    assert answer['next_page'] == '2'


def test_an_unknown_status_lists_nothing(app, env):
    from app.api.alpha.utils.user import get_user_notifications

    user, author, baseline = env
    a_post_notification(user, author, baseline.post1, NOTIF_FEED, 'new_post')

    answer = get_user_notifications(token(user), {'status': 'Nonsense'})

    assert answer['items'] == []
    assert answer['counts']['total'] == 1


def test_marking_one_notification_read_says_so(app, env):
    """D1187. The response was built above the write, so it answered the OLD
    state. Measured: `PROBE bh4 answered status: 'Unread' | stored read:
    True`."""
    from app.api.alpha.utils.user import put_user_notification_state

    user, author, baseline = env
    notification = a_post_notification(user, author, baseline.post1,
                                       NOTIF_FEED, 'new_post')

    answer = put_user_notification_state(token(user),
                                         {'notif_id': notification.id,
                                          'read_state': True})

    assert answer['status'] == 'Read'
    assert notification.read is True


def test_marking_one_notification_unread_says_so_too(app, env):
    from app.api.alpha.utils.user import put_user_notification_state

    user, author, baseline = env
    notification = a_notification(user, author, NOTIF_FEED, 'new_post',
                                  {'gen': '0', 'post_id': baseline.post1.id},
                                  read=True)

    answer = put_user_notification_state(token(user),
                                         {'notif_id': notification.id,
                                          'read_state': False})

    assert answer['status'] == 'Unread'
    assert notification.read is False


def test_somebody_elses_notification_cannot_be_marked(app, env):
    from app.api.alpha.utils.user import put_user_notification_state

    user, author, baseline = env
    theirs = a_post_notification(author, user, baseline.post1, NOTIF_FEED,
                                 'new_post')

    with pytest.raises(Exception):
        put_user_notification_state(token(user), {'notif_id': theirs.id,
                                                  'read_state': True})

    assert theirs.read is False


def test_a_notification_whose_target_is_gone_cannot_be_marked(app, env):
    from app.api.alpha.utils.user import put_user_notification_state

    user, author, baseline = env
    notification = a_notification(user, author, NOTIF_FEED, 'new_post',
                                  {'gen': '0', 'post_id': 999999})

    with pytest.raises(Exception) as refused:
        put_user_notification_state(token(user),
                                    {'notif_id': notification.id,
                                     'read_state': True})

    assert 'problem processing that notification' in str(refused.value)
    assert notification.read is False


def test_an_unsupported_notification_cannot_be_marked(app, env):
    from app.api.alpha.utils.user import put_user_notification_state

    user, author, baseline = env
    notification = a_notification(user, author, NOTIF_REPORT, 'new_report',
                                  {'gen': '0'})

    with pytest.raises(Exception) as refused:
        put_user_notification_state(token(user),
                                    {'notif_id': notification.id,
                                     'read_state': True})

    assert 'currently unsupported in the api' in str(refused.value)
    assert notification.read is False


def test_the_unread_notification_count(app, env):
    from app.api.alpha.utils.user import get_user_notifications_count

    user, author, baseline = env
    a_post_notification(user, author, baseline.post1, NOTIF_FEED, 'new_post')
    a_notification(user, author, NOTIF_FEED, 'new_post',
                   {'gen': '0', 'post_id': baseline.post1.id}, read=True)

    assert get_user_notifications_count(token(user)) == {'count': 1}


def test_marking_everything_read_clears_the_badge_too(app, env):
    """D1188. The rows were updated and `user.unread_notifications` was not
    -- and that column is what the unread count answers with, so the badge
    kept its number. `post_user_mark_all_as_read`, the same intent one
    endpoint away, zeroes it. Measured: `PROBE bh5 counter column: 1 |
    counted unread: {'count': 0}`."""
    from app.api.alpha.utils.user import (get_user_unread_count,
                                          put_user_mark_all_notifications_read)

    user, author, baseline = env
    notification = a_post_notification(user, author, baseline.post1,
                                       NOTIF_FEED, 'new_post')
    user.unread_notifications = 1
    db.session.commit()

    answer = put_user_mark_all_notifications_read(token(user))

    assert answer == {'mark_all_notifications_as_read': 'complete'}
    assert notification.read is True
    assert user.unread_notifications == 0
    assert get_user_unread_count(token(user))['other'] == 0


@pytest.mark.parametrize('status', ['Unread', 'Read'])
def test_a_broken_notification_is_skipped_in_every_list(app, env, status):
    """Each of the three status branches carries its own `except
    AttributeError: continue`."""
    from app.api.alpha.utils.user import get_user_notifications

    user, author, baseline = env
    a_notification(user, author, NOTIF_FEED, 'new_post',
                   {'gen': '0', 'post_id': 999999},
                   read=(status == 'Read'))

    assert get_user_notifications(token(user), {'status': status})['items'] == []


def test_a_mention_of_an_unknown_kind_is_not_represented(app, env):
    """`_process_notification_item` answers False for a NOTIF_MENTION whose
    subtype is neither of the two it knows, and the caller refuses it."""
    from app.api.alpha.utils.user import put_user_notification_state

    user, author, baseline = env
    notification = a_notification(user, author, NOTIF_MENTION,
                                  'some_other_mention',
                                  {'gen': '0', 'post_id': baseline.post1.id})

    with pytest.raises(Exception) as refused:
        put_user_notification_state(token(user),
                                    {'notif_id': notification.id,
                                     'read_state': True})

    assert 'currently unsupported in the api' in str(refused.value)


@pytest.mark.parametrize('setting', ['nsfw_visibility', 'nsfl_visibility',
                                     'genai_visibility', 'bot_visibility',
                                     'accept_private_messages'])
def test_a_value_outside_the_vocabulary_changes_nothing(app, env, setting):
    """The schema constrains these, so an unknown value only reaches the util
    directly -- and every chain falls through rather than guessing."""
    user, author, baseline = env
    before = (user.hide_nsfw, user.hide_nsfl, user.hide_gen_ai,
              user.ignore_bots, user.accept_private_messages)

    settings(user, **{setting: 'Nonsense'})

    assert (user.hide_nsfw, user.hide_nsfl, user.hide_gen_ai,
            user.ignore_bots, user.accept_private_messages) == before


def test_a_threshold_of_zero_is_left_alone(app, env):
    """`if reply_collapse_threshold:` -- 0 is falsy, so it is not a value this
    endpoint can set."""
    user, author, baseline = env
    user.reply_collapse_threshold = -5
    user.reply_hide_threshold = -10
    db.session.commit()

    settings(user, reply_collapse_threshold=0, reply_hide_threshold=0)

    assert user.reply_collapse_threshold == -5
    assert user.reply_hide_threshold == -10


def test_an_extra_field_edit_and_an_addition_in_one_call(app, env):
    """The loop handles both shapes in a single request, and the four-field
    ceiling counts what is left after the removals."""
    user, author, baseline = env
    settings(user, extra_fields=[{'label': 'Website', 'text': 'here'}])
    field = UserExtraField.query.one()

    settings(user, extra_fields=[{'id': field.id, 'label': 'Homepage',
                                  'text': 'there'},
                                 {'label': 'Fediverse', 'text': '@me'}])

    assert {(f.label, f.text) for f in UserExtraField.query.all()} == {
        ('Homepage', 'there'), ('Fediverse', '@me')}


def test_an_avatar_url_of_nothing_is_not_a_removal(app, env):
    """`"avatar" in data` with a false value removes; an absent key leaves it
    alone. This is the absent-key half."""
    from unittest.mock import patch

    user, author, baseline = env
    with patch('app.api.alpha.utils.user.make_image_sizes'):
        settings(user, avatar='https://example.com/a.png')
    avatar_id = user.avatar_id

    settings(user, bot=True)

    assert user.avatar_id == avatar_id


# `if remove_file:` inside the avatar and cover paths (four arms) cannot be
# false: `user_avatar_id_fkey` and `user_cover_id_fkey` stop a File row being
# deleted while an account points at it, so the id can never dangle. A row
# that tries answers `psycopg2.errors.ForeignKeyViolation: update or delete
# on table "file" violates foreign key constraint "user_avatar_id_fkey"`.
# They are left as partial branches rather than exercised through a state the
# schema forbids -- see coverage_floors.ini for the floor that records it.


def test_a_settings_call_that_says_nothing_keeps_the_display_name(app, env):
    """`display_name` defaults to **False** when the key is absent, not None,
    because None is the value that REMOVES the title. A call about something
    else must not wipe it."""
    user, author, baseline = env
    settings(user, display_name='A Better Name')

    settings(user, bot=True)

    assert user.title == 'A Better Name'
