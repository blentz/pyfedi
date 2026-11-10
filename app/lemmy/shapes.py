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


def community_aggregates(alpha: dict) -> dict:
    """CommunityAggregates, from alpha's counts (views.community_view variant 2): `subscribers` is everyone's,
    `subscribers_local` this server's."""
    return {'community_id': alpha['id'], 'subscribers': alpha.get('total_subscriptions_count') or 0,
            'posts': alpha.get('post_count') or 0, 'comments': alpha.get('post_reply_count') or 0,
            'published': alpha['published'], 'users_active_day': alpha.get('active_daily') or 0,
            'users_active_week': alpha.get('active_weekly') or 0, 'users_active_month': alpha.get('active_monthly') or 0,
            'users_active_half_year': alpha.get('active_6monthly') or 0,
            'subscribers_local': alpha.get('subscriptions_count') or 0}


def community_view(alpha: dict) -> dict:
    """CommunityView, from alpha's (variant 2). A ban from the community is not shown on its view in alpha either."""
    return {'community': community(alpha['community']), 'subscribed': alpha['subscribed'],
            'blocked': bool(alpha['blocked']), 'counts': community_aggregates(alpha['counts']),
            'banned_from_community': False}


def moderator_view(alpha: dict) -> dict:
    return {'community': community(alpha['community']), 'moderator': person(alpha['moderator'])}


def _poll(poll: dict, link: str) -> str:
    """A poll as Jerboa can show it (ruling L3): its choices and their votes, and where to vote."""
    choices = '\n'.join(f"- {choice['choice_text']} ({choice['num_votes']})" for choice in poll.get('choices', []))
    return f'{choices}\n\n[Vote on the web]({link})'


def _event(event: dict) -> str:
    """When and where an event is, as the first lines of its body (ruling L3)."""
    when = event['start'] + (f" – {event['end']}" if event.get('end') else '') + f" ({event['timezone']})"
    lines = [f'**When:** {when}']
    place = event.get('location') or {}
    where = ', '.join(part for part in (place.get('address'), place.get('city'), place.get('country')) if part)
    if where or event.get('online_link'):
        lines.append(f"**Where:** {where or event['online_link']}")
    return '\n\n'.join(lines)


def _gallery(images: list[dict]) -> str:
    return '\n\n'.join(f"![{image.get('alt_text') or ''}]({image['url']})" for image in images)


def post(alpha: dict) -> dict:
    """Post, from alpha's (views.post_view variant 1). Lemmy has links, images and text only; a poll, a gallery or an
    event is shown as the nearest of them, with the rest in its body (ruling L3)."""
    body, url = alpha.get('body'), alpha.get('url')
    gallery = alpha.get('extensions', {}).get('gallery', [])
    if alpha.get('post_type') == 'Gallery' and gallery:
        url, body = gallery[0]['url'], '\n\n'.join(part for part in (body, _gallery(gallery[1:])) if part)
    if alpha.get('event'):
        body = '\n\n'.join(part for part in (_event(alpha['event']), body) if part)
    if alpha.get('poll'):
        body = '\n\n'.join(part for part in (body, _poll(alpha['poll'], alpha['ap_id'])) if part)
    return {'id': alpha['id'], 'name': alpha['title'], 'url': url, 'body': body or None,
            'creator_id': alpha['user_id'], 'community_id': alpha['community_id'], 'removed': bool(alpha['removed']),
            'locked': bool(alpha['locked']), 'published': alpha['published'], 'updated': alpha.get('updated'),
            'deleted': bool(alpha['deleted']), 'nsfw': bool(alpha['nsfw']), 'thumbnail_url': alpha.get('thumbnail_url'),
            'ap_id': alpha['ap_id'], 'local': bool(alpha['local']), 'language_id': alpha.get('language_id') or 0,
            'featured_community': bool(alpha['sticky']), 'featured_local': bool(alpha['instance_sticky']),
            'alt_text': alpha.get('alt_text')}


def post_aggregates(alpha: dict) -> dict:
    return {'post_id': alpha['post_id'], 'comments': alpha['comments'] or 0, 'score': alpha['score'] or 0,
            'upvotes': alpha['upvotes'] or 0, 'downvotes': alpha['downvotes'] or 0, 'published': alpha['published'],
            'newest_comment_time': alpha.get('newest_comment_time') or alpha['published']}


def post_view(alpha: dict) -> dict:
    """PostView, from alpha's (variant 2). A blocked author's posts are not listed, so `creator_blocked` is false."""
    return {'post': post(alpha['post']), 'creator': person(alpha['creator']), 'community': community(alpha['community']),
            'creator_banned_from_community': bool(alpha['creator_banned_from_community']),
            'banned_from_community': bool(alpha['banned_from_community']),
            'creator_is_moderator': bool(alpha['creator_is_moderator']),
            'creator_is_admin': bool(alpha['creator_is_admin']), 'counts': post_aggregates(alpha['counts']),
            'subscribed': alpha['subscribed'], 'saved': bool(alpha['saved']), 'read': bool(alpha['read']),
            'hidden': bool(alpha['hidden']), 'creator_blocked': False, 'my_vote': alpha.get('my_vote') or 0,
            'unread_comments': alpha.get('unread_comments') or 0}


def get_post_response(alpha: dict) -> dict:
    """GetPostResponse, from alpha's (views.post_view variant 3)."""
    return {'post_view': post_view(alpha['post_view']), 'community_view': community_view(alpha['community_view']),
            'moderators': [moderator_view(row) for row in alpha['moderators']],
            'cross_posts': [post_view(row) for row in alpha['cross_posts']]}


def comment(alpha: dict) -> dict:
    """Comment, from alpha's (views.reply_view variant 1), or a stub's: a comment the viewer may not see keeps its id
    and place in the thread and nothing else (alpha's D18), so its text, author and date are Lemmy's empty ones."""
    return {'id': alpha['id'], 'creator_id': alpha.get('user_id') or 0, 'post_id': alpha['post_id'],
            'content': alpha.get('body') or '', 'removed': bool(alpha['removed']),
            'published': alpha.get('published') or EPOCH, 'updated': alpha.get('updated'),
            'deleted': bool(alpha['deleted']), 'ap_id': alpha.get('ap_id') or '', 'local': bool(alpha['local']),
            'path': alpha['path'], 'distinguished': bool(alpha.get('distinguished')),
            'language_id': alpha.get('language_id') or 0}


def comment_aggregates(alpha: dict) -> dict:
    return {'comment_id': alpha['comment_id'], 'score': alpha['score'] or 0, 'upvotes': alpha['upvotes'] or 0,
            'downvotes': alpha['downvotes'] or 0, 'published': alpha['published'],
            'child_count': alpha.get('child_count') or 0}


def comment_view(alpha: dict) -> dict:
    """CommentView, from alpha's (variant 3, or a stub)."""
    return {'comment': comment(alpha['comment']), 'creator': person(alpha['creator']), 'post': post(alpha['post']),
            'community': community(alpha['community']), 'counts': comment_aggregates(alpha['counts']),
            'creator_banned_from_community': bool(alpha['creator_banned_from_community']),
            'banned_from_community': bool(alpha['banned_from_community']),
            'creator_is_moderator': bool(alpha['creator_is_moderator']),
            'creator_is_admin': bool(alpha['creator_is_admin']), 'subscribed': alpha.get('subscribed') or 'NotSubscribed',
            'saved': bool(alpha['saved']), 'creator_blocked': bool(alpha['creator_blocked']),
            'my_vote': alpha.get('my_vote') or 0}


def inbox_view(alpha: dict, kind: str) -> dict:
    """CommentReplyView (`kind` comment_reply) or PersonMentionView (person_mention), from alpha's reply or mention:
    a CommentView with the inbox entry and its recipient. Alpha's entry is keyed by the comment, so its id is the
    comment's, which is what marking it read names."""
    entry = alpha['comment_reply']
    return {**comment_view(alpha), kind: {'id': entry['id'], 'recipient_id': entry['recipient_id'],
                                          'comment_id': entry['comment_id'], 'read': bool(entry['read']),
                                          'published': entry['published']},
            'recipient': person(alpha['recipient'])}


def private_message_view(alpha: dict) -> dict:
    """PrivateMessageView, from alpha's (views.private_message_view variant 1), already Lemmy's but for the people."""
    message = alpha['private_message']
    return {'private_message': {**message, 'deleted': bool(message['deleted']), 'read': bool(message['read'])},
            'creator': person(alpha['creator']), 'recipient': person(alpha['recipient'])}
