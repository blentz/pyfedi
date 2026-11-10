"""Lemmy 0.19.11's models, made from /api/alpha's (spec 2026-11-10-lemmy-v3-design §3). Alpha's began as Lemmy's, so
most of a model is renamed fields; what Lemmy requires and PieFed has no such thing for gets the value that says "not
here". Every function takes alpha's dict for the model and returns Lemmy's; tests/lemmy_app_contract.py checks each
against the models Jerboa decodes."""
from datetime import datetime

EPOCH = '1970-01-01T00:00:00.000000Z'

# PieFed's stored sorts and listings, as Lemmy names them: one Lemmy has no such name for takes its nearest
SORTS = {'hot': 'Hot', 'top': 'TopDay', 'new': 'New', 'active': 'Active', 'old': 'Old', 'scaled': 'Scaled'}
LISTINGS = {'subscribed': 'Subscribed', 'local': 'Local', 'popular': 'All', 'all': 'All',
            'moderating': 'ModeratorView'}


def timestamp(moment: datetime | None) -> str:
    return moment.isoformat(timespec='microseconds') + 'Z' if moment else EPOCH


def person(alpha: dict) -> dict:
    """Person, from alpha's Person (views.user_view variant 1, or my_user's)."""
    return {'id': alpha['id'], 'name': alpha['user_name'], 'display_name': alpha.get('title'),
            'avatar': alpha.get('avatar'), 'banned': bool(alpha['banned']), 'published': alpha.get('published') or EPOCH,
            'actor_id': alpha['actor_id'], 'bio': alpha.get('about'), 'local': bool(alpha['local']),
            'banner': alpha.get('banner'), 'deleted': bool(alpha['deleted']),
            'matrix_user_id': alpha.get('matrix_user_id') or None, 'bot_account': bool(alpha['bot']),
            'instance_id': alpha['instance_id']}


def person_aggregates(alpha: dict) -> dict:
    return {'person_id': alpha['person_id'], 'post_count': alpha['post_count'] or 0,
            'comment_count': alpha['comment_count'] or 0}


def person_view(alpha: dict) -> dict:
    """PersonView, from alpha's (variant 2)."""
    return {'person': person(alpha['person']), 'counts': person_aggregates(alpha['counts']),
            'is_admin': bool(alpha['is_admin'])}


def community(alpha: dict) -> dict:
    """Community, from alpha's (views.community_view variant 1). Every community is listed publicly to those who may
    see it, which is what Lemmy's Public says."""
    return {'id': alpha['id'], 'name': alpha['name'], 'title': alpha['title'], 'description': alpha.get('description'),
            'removed': bool(alpha['removed']), 'published': alpha['published'], 'updated': alpha.get('updated'),
            'deleted': bool(alpha['deleted']), 'nsfw': bool(alpha['nsfw']), 'actor_id': alpha['actor_id'],
            'local': bool(alpha['local']), 'icon': alpha.get('icon'), 'banner': alpha.get('banner'),
            'hidden': bool(alpha['hidden']), 'posting_restricted_to_mods': bool(alpha['restricted_to_mods']),
            'instance_id': alpha['instance_id'], 'visibility': 'Public'}


def instance(alpha: dict) -> dict:
    return {'id': alpha['id'], 'domain': alpha['domain'], 'published': alpha['published'],
            'updated': alpha.get('updated'), 'software': alpha.get('software'), 'version': alpha.get('version')}


def my_user(alpha: dict, user) -> dict:
    """MyUserInfo, from alpha's (views.user_view variant 6) and the person's own settings."""
    view = alpha['local_user_view']
    return {'local_user_view': {'local_user': local_user(user), 'person': person(view['person']),
                                'counts': person_aggregates(view['counts']),
                                'local_user_vote_display_mode': {'local_user_id': user.id, 'score': True,
                                                                 'upvotes': True, 'downvotes': True,
                                                                 'upvote_percentage': False}},
            'follows': [{'community': community(row['community']), 'follower': person(row['follower'])}
                        for row in alpha['follows']],
            'moderates': [{'community': community(row['community']), 'moderator': person(row['moderator'])}
                          for row in alpha['moderates']],
            'community_blocks': [{'person': person(row['person']), 'community': community(row['community'])}
                                 for row in alpha['community_blocks']],
            'instance_blocks': [{'person': person(row['person']), 'instance': instance(row['instance'])}
                                for row in alpha['instance_blocks']],
            'person_blocks': [{'person': person(row['person']), 'target': person(row['target'])}
                              for row in alpha['person_blocks']],
            'discussion_languages': []}


def local_user(user) -> dict:
    """LocalUser: the person's settings as Lemmy names them. PieFed has one id for an account; Lemmy's local user and
    person ids are both it."""
    return {'id': user.id, 'person_id': user.id, 'email': user.email, 'show_nsfw': user.hide_nsfw != 1,
            'theme': 'browser', 'default_sort_type': SORTS.get((user.default_sort or 'hot').lower(), 'Hot'),
            'default_listing_type': LISTINGS.get((user.default_filter or 'subscribed').lower(), 'Subscribed'),
            'interface_language': user.interface_language or 'browser', 'show_avatars': True,
            'send_notifications_to_email': bool(user.email_unread), 'show_scores': True,
            'show_bot_accounts': user.ignore_bots != 1, 'show_read_posts': not user.hide_read_posts,
            'email_verified': bool(user.verified), 'accepted_application': True, 'open_links_in_new_tab': False,
            'blur_nsfw': user.hide_nsfw == 2, 'auto_expand': False, 'infinite_scroll_enabled': True,
            'admin': user.is_admin(), 'post_listing_mode': 'List', 'totp_2fa_enabled': False,
            'enable_keyboard_navigation': False, 'enable_animated_images': True, 'collapse_bot_comments': False,
            'last_donation_notification': EPOCH}
