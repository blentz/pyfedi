"""Every alpha API endpoint that takes an object's id or name answers an onion row off the onion exactly as one nobody
holds (follow-up F6, rounds 4 and 5). Every field of every endpoint's request schema is classified: CASES names the
kind of row it names, EXEMPT says why it is not looked up, NON_OBJECT_FIELDS why it names no row. A walk of the URL map
fails on any field in none of them, as tests/test_hidden_routes_table.py does for the web's route arguments, so a new
endpoint or field cannot skip the shared finders unnoticed."""
import itertools
import weakref

import pytest
from sqlalchemy import text

import app.storefront as storefront
from app import db
from app.models import ChatMessage, CommunityFlair, CommunityMember, Conversation, Notification, Report, \
    UserRegistration
from tests.conftest import forget_login
from tests.factories import grant_permission, make_post, make_post_reply
from tests.test_api_routes_gate_sweep import documented_routes, minimal_payload
from tests.test_hidden_routes import talking  # noqa: F401 -- a fixture
from tests.test_instance_visible_here import HOSTS, scene  # noqa: F401 -- scene is a fixture
from tests.test_local_only_actors_hidden import ONION_ART, ONION_BOB, world  # noqa: F401 -- world is a fixture
from tests.test_onion_hidden_api_ids import NOBODY, alpha, onion_in_reply, reports  # noqa: F401 -- fixtures
from tests.test_onion_hidden_on_clearnet import onion  # noqa: F401 -- a fixture
from tests.test_per_domain_ap_routes import onion_proxy, twins  # noqa: F401
from tests.two_domains import FIRST, ONION, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')

# Every field of every request schema that is not in CASES or EXEMPT is here, by name, with why it names no account,
# community, post, reply, conversation, message, report, notification or flair: an onion row could not be told apart
# by it. A field nowhere fails the walk: none is passed for its shape (re-review 3, N5).
NON_OBJECT_FIELDS = {
    'language_id': 'a language', 'language': 'a language', 'discussion_languages': 'languages',
    'topic_id': 'a topic: an onion community in it is filtered from the posts listed',
    'feed_id': 'a feed: feeds owned by onion accounts are deferred to per-domain feeds Task 7 (ruling N-m8)',
    'parent_feed_id': 'a feed (ruling N-m8)',
    'instance_id': 'an instance: site/block is accepted by ruling',
    'choice_id': 'a poll choice, read only after its post went through the finder',
    'domain': 'a link domain (/d/), never a hosted domain',
    'display_name': "the caller's own display name",
    # what to list, and how
    'page': 'paging', 'limit': 'paging', 'sort': 'order', 'type_': 'a kind', 'listing_type': 'a kind',
    'status': 'a state', 'kind': 'a kind', 'feature_type': 'a kind', 'post_url_type': 'a kind',
    'max_depth': 'paging', 'depth_first': 'order', 'message_history_limit': 'paging', 'include_content': 'a switch',
    'include_communities': 'a switch', 'mine_only': 'a switch', 'saved_only': 'a switch', 'liked_only': 'a switch',
    'unread_only': 'a switch', 'unresolved_only': 'a switch', 'pending_only': 'a switch',
    'show_child_posts': 'a switch',
    'ignore_sticky': 'a switch', 'minimum_upvotes': 'a filter', 'debug': 'a switch', 'is_instance_feed': 'a switch',
    # the action's own values
    'added': 'a flag', 'approve': 'a flag', 'block': 'a flag', 'deleted': 'a flag', 'distinguished': 'a flag',
    'featured': 'a flag', 'follow': 'a flag', 'hidden': 'a flag', 'locked': 'a flag', 'read': 'a flag',
    'read_state': 'a flag', 'removed': 'a flag', 'resolved': 'a flag', 'save': 'a flag', 'subscribe': 'a flag',
    'answer': 'a flag or an answer', 'score': 'a vote', 'nsfw_status': 'a flag', 'ban_ip_address': 'a flag',
    'purge_content': 'a flag', 'permanent': 'a flag', 'expires_at': 'a time', 'report_remote': 'a flag',
    'reason': 'text', 'note': 'text', 'body': 'text', 'content': 'text', 'title': 'text', 'description': 'text',
    'rules': 'text', 'bio': 'text', 'flair_text': 'text', 'flair_title': 'text', 'flair': 'text', 'alt_text': 'text',
    'posting_warning': 'text', 'tags': 'text', 'question_answer': 'text', 'captcha_answer': 'text',
    'captcha_uuid': 'a token', 'honeypot': 'a trap', 'url': 'a link the post carries', 'banner_url': 'an image',
    'icon_url': 'an image', 'avatar': 'an image', 'cover': 'an image', 'file': 'an upload', 'poll': 'a poll',
    'event': 'an event', 'emoji': 'an emoji', 'text_color': 'a colour', 'background_color': 'a colour',
    'blur_images': 'a flag', 'theme': 'a setting',
    # the caller's own settings, and a new community's or feed's
    'accept_private_messages': 'a setting', 'ai_generated': 'a flag', 'bot': 'a setting',
    'bot_visibility': 'a setting', 'community_keyword_filter': 'a setting', 'default_comment_sort_type': 'a setting',
    'default_layout': 'a setting', 'default_post_type': 'a setting', 'default_sort_type': 'a setting',
    'downvote_accept_mode': 'a setting', 'email': "the caller's own", 'email_unread': 'a setting',
    'extra_fields': "the caller's own", 'federate_votes': 'a setting', 'feed_auto_follow': 'a setting',
    'feed_auto_leave': 'a setting', 'genai_visibility': 'a setting', 'hide_low_quality': 'a setting',
    'indexable': 'a setting', 'invitations': 'a setting', 'local_only': 'a setting', 'new_mods_wanted': 'a setting',
    'newbie': 'a setting', 'newsletter': 'a setting', 'nsfl': 'a flag', 'nsfl_visibility': 'a setting',
    'nsfw': 'a flag', 'nsfw_visibility': 'a setting', 'password': 'a secret', 'password_verify': 'a secret',
    'podcast_category': 'a setting', 'private': 'a setting', 'public': 'a setting',
    'reply_collapse_threshold': 'a setting', 'reply_hide_threshold': 'a setting', 'restricted_to_mods': 'a setting',
    'searchable': 'a setting', 'show_nsfl': 'a setting', 'show_nsfw': 'a setting', 'show_read_posts': 'a setting',
    'visibility': 'a setting',
}
# Endpoint fields that name an object but are not looked up by a caller's id or name
EXEMPT = {
    ('GET', '/api/alpha/feed', 'id'): 'a feed (ruling N-m8)',
    ('GET', '/api/alpha/feed', 'name'): 'a feed (ruling N-m8)',
    ('POST', '/api/alpha/feed', 'name'): 'the name of a feed being created',
    ('POST', '/api/alpha/feed', 'communities'): 'a feed being created (ruling N-m8)',
    ('PUT', '/api/alpha/feed', 'communities'): 'a feed being edited (ruling N-m8)',
    ('POST', '/api/alpha/community', 'name'): 'the name of a community being created, on this storefront',
    ('POST', '/api/alpha/user/login', 'username'): "a login names this storefront's own account",
    ('POST', '/api/alpha/user/verify_credentials', 'username'): "as login: this storefront's own account",
    ('POST', '/api/alpha/user/register', 'username'): 'the name of an account being created, on this storefront',
    ('GET', '/api/alpha/search', 'q'): 'search text: the results go through the hidden-here forms',
    ('GET', '/api/alpha/post/list', 'q'): 'search text, as /search',
    ('GET', '/api/alpha/post/list2', 'q'): 'search text, as /search',
    ('GET', '/api/alpha/site/instance_chooser_search', 'q'): 'an instance (accepted by ruling)',
}

# The cases whose visible twin answers as an id nobody holds does, so the positive control cannot tell, and why
SAME_FOR_A_FRESH_ROW = {
    ('GET', '/api/alpha/modlog', 'mod_person_id'): 'a fresh row is in no modlog entry',
    ('GET', '/api/alpha/modlog', 'other_person_id'): 'a fresh row is in no modlog entry',
    ('GET', '/api/alpha/modlog', 'community_id'): 'a fresh row is in no modlog entry',
    ('GET', '/api/alpha/modlog', 'post_id'): 'a fresh row is in no modlog entry',
    ('GET', '/api/alpha/modlog', 'comment_id'): 'a fresh row is in no modlog entry',
    ('GET', '/api/alpha/search', 'community_id'): "the schema's minimal search text matches nothing in it",
    ('GET', '/api/alpha/search', 'community_name'): "the schema's minimal search text matches nothing in it",
    ('POST', '/api/alpha/post/mark_as_read', 'post_ids'): 'it answers success for any list of ids',
    ('GET', '/api/alpha/private_message/conversation', 'person_id'): 'a fresh account is in no conversation',
}


# The modlog's nine categories are nine paginated queries; one is enough to reach the lookup
ONE_CATEGORY = {'type_': ('literal', 'ModRemovePost')}


def _cases():
    """(method, path, field) -> the kind of row it names, and the fields the request must also carry to reach it."""
    p = '/api/alpha'
    on_first_art = {'community_id': 'first_art'}
    rows = {
        ('GET', '/search', 'community_name'): ('community_name', {}),
        ('GET', '/search', 'community_id'): ('community', {}),
        ('GET', '/modlog', 'mod_person_id'): ('person', ONE_CATEGORY),
        ('GET', '/modlog', 'other_person_id'): ('person', ONE_CATEGORY),
        ('GET', '/modlog', 'community_id'): ('community', ONE_CATEGORY),
        ('GET', '/modlog', 'post_id'): ('post', ONE_CATEGORY),
        ('GET', '/modlog', 'comment_id'): ('reply', ONE_CATEGORY),
        ('GET', '/community', 'id'): ('community', {}),
        ('GET', '/community', 'name'): ('community_name', {}),
        ('POST', '/community/follow', 'community_id'): ('community', {}),
        ('POST', '/community/block', 'community_id'): ('community', {}),
        ('PUT', '/community', 'community_id'): ('community', {}),
        ('PUT', '/community/subscribe', 'community_id'): ('community', {}),
        ('POST', '/community/delete', 'community_id'): ('community', {}),
        ('POST', '/community/mod', 'community_id'): ('community', {'person_id': 'admin'}),
        ('POST', '/community/mod', 'person_id'): ('person', on_first_art),
        ('GET', '/community/moderate/bans', 'community_id'): ('community', {}),
        ('PUT', '/community/moderate/unban', 'community_id'): ('community', {'user_id': 'first_bob'}),
        ('PUT', '/community/moderate/unban', 'user_id'): ('person', on_first_art),
        ('POST', '/community/moderate/ban', 'community_id'): ('community', {'user_id': 'first_bob'}),
        ('POST', '/community/moderate/ban', 'user_id'): ('person', on_first_art),
        ('POST', '/community/moderate/post/nsfw', 'post_id'): ('post', {}),
        ('POST', '/community/flair', 'community_id'): ('community', {}),
        ('PUT', '/community/flair', 'flair_id'): ('flair', {}),
        ('POST', '/community/flair/delete', 'flair_id'): ('flair', {}),
        ('GET', '/post/list', 'community_name'): ('community_name', {}),
        ('GET', '/post/list', 'community_id'): ('community', {}),
        ('GET', '/post/list', 'person_id'): ('person', {}),
        ('GET', '/post/list2', 'community_name'): ('community_name', {}),
        ('GET', '/post/list2', 'community_id'): ('community', {}),
        ('GET', '/post/list2', 'person_id'): ('person', {}),
        ('GET', '/post', 'id'): ('post', {}),
        ('GET', '/post/replies', 'post_id'): ('post', {}),
        ('GET', '/post/replies', 'parent_id'): ('reply', {}),
        ('POST', '/post/like', 'post_id'): ('post', {}),
        ('PUT', '/post/save', 'post_id'): ('post', {}),
        ('PUT', '/post/subscribe', 'post_id'): ('post', {}),
        ('POST', '/post', 'community_id'): ('community', {}),
        ('PUT', '/post', 'post_id'): ('post', {}),
        ('POST', '/post/delete', 'post_id'): ('post', {}),
        ('POST', '/post/report', 'post_id'): ('post', {}),
        ('GET', '/post/report/list', 'community_id'): ('community', {}),
        ('GET', '/post/report/list', 'post_id'): ('post', {}),
        ('PUT', '/post/report/resolve', 'report_id'): ('post_report', {}),
        ('POST', '/post/lock', 'post_id'): ('post', {}),
        ('POST', '/post/hide', 'post_id'): ('post', {}),
        ('POST', '/post/feature', 'post_id'): ('post', {}),
        ('POST', '/post/remove', 'post_id'): ('post', {}),
        ('POST', '/post/mark_as_read', 'post_id'): ('post', {}),
        ('POST', '/post/mark_as_read', 'post_ids'): ('posts', {}),
        ('GET', '/post/like/list', 'post_id'): ('post', {}),
        ('POST', '/post/assign_flair', 'post_id'): ('post', {}),
        ('POST', '/post/poll_vote', 'post_id'): ('post', {}),
        ('GET', '/comment/list', 'person_id'): ('person', {}),
        ('GET', '/comment/list', 'community_id'): ('community', {}),
        ('GET', '/comment/list', 'post_id'): ('post', {}),
        ('GET', '/comment/list', 'parent_id'): ('reply', {}),
        ('POST', '/comment/like', 'comment_id'): ('reply', {}),
        ('PUT', '/comment/save', 'comment_id'): ('reply', {}),
        ('PUT', '/comment/subscribe', 'comment_id'): ('reply', {}),
        ('POST', '/comment', 'post_id'): ('post', {}),
        ('POST', '/comment', 'parent_id'): ('reply', {'post_id': 'first_post'}),
        ('PUT', '/comment', 'comment_id'): ('reply', {}),
        ('POST', '/comment/delete', 'comment_id'): ('reply', {}),
        ('POST', '/comment/report', 'comment_id'): ('reply', {}),
        ('GET', '/comment/report/list', 'comment_id'): ('reply', {}),
        ('GET', '/comment/report/list', 'community_id'): ('community', {}),
        ('PUT', '/comment/report/resolve', 'report_id'): ('reply_report', {}),
        ('POST', '/comment/remove', 'comment_id'): ('reply', {}),
        ('POST', '/comment/mark_as_read', 'comment_reply_id'): ('reply', {}),
        ('POST', '/comment/mark_as_answer', 'comment_reply_id'): ('reply', {}),
        ('POST', '/comment/distinguish', 'comment_reply_id'): ('reply', {}),
        ('GET', '/comment', 'id'): ('reply', {}),
        ('POST', '/comment/lock', 'comment_id'): ('reply', {}),
        ('GET', '/comment/like/list', 'comment_id'): ('reply', {}),
        ('GET', '/private_message/conversation', 'person_id'): ('person', {}),
        ('GET', '/private_message/conversation', 'conversation_id'): ('conversation', {}),
        ('POST', '/private_message/conversation/leave', 'conversation_id'): ('conversation', {}),
        ('POST', '/private_message', 'recipient_id'): ('person', {}),
        ('PUT', '/private_message', 'private_message_id'): ('message', {}),
        ('POST', '/private_message/mark_as_read', 'private_message_id'): ('message', {}),
        ('POST', '/private_message/delete', 'private_message_id'): ('message', {}),
        ('POST', '/private_message/report', 'private_message_id'): ('message', {}),
        ('POST', '/private_message/conversation/report', 'conversation_id'): ('conversation', {}),
        ('GET', '/private_message/report/list', 'conversation_id'): ('conversation', {}),
        ('GET', '/private_message/report/list', 'private_message_id'): ('message', {}),
        ('GET', '/private_message/conversation/report/list', 'conversation_id'): ('conversation', {}),
        ('PUT', '/private_message/report/resolve', 'report_id'): ('message_report', {}),
        ('PUT', '/private_message/conversation/report/resolve', 'report_id'): ('message_report', {}),
        ('GET', '/user', 'person_id'): ('person', {}),
        ('GET', '/user', 'username'): ('person_name', {}),
        ('GET', '/user', 'community_id'): ('community', {'person_id': 'first_bob'}),
        ('POST', '/user/block', 'person_id'): ('person', {}),
        ('PUT', '/user/subscribe', 'person_id'): ('person', {}),
        ('PUT', '/user/notification_state', 'notif_id'): ('notification', {}),
        ('POST', '/user/set_flair', 'community_id'): ('community', {}),
        ('POST', '/user/follow', 'user_id'): ('person', {}),
        ('POST', '/user/unfollow', 'user_id'): ('person', {}),
        ('POST', '/user/note', 'person_id'): ('person', {}),
        ('POST', '/user/ban', 'person_id'): ('person', {}),
        ('POST', '/user/unban', 'person_id'): ('person', {}),
        ('PUT', '/admin/registration_application/approve', 'user_id'): ('person', {}),
        ('POST', '/post/assign_flair', 'flair_id_list'): ('flairs', {'post_id': 'first_post'}),
        ('GET', '/resolve_object', 'q'): ('post_url', {}),
        ('GET', '/suggest_completion', 'q'): ('person_prefix', {}),
    }
    return {(method, p + path, field): case for (method, path, field), case in rows.items()}


CASES = _cases()


def fields(app):
    """(method, path, field) for every field of every documented alpha API endpoint's request schemas."""
    found = set()
    for method, path, parameters in documented_routes(app):
        for parameter in parameters:
            schema = parameter['schema']
            schema = schema() if isinstance(schema, type) else schema
            found |= {(method, path, field.data_key or name) for name, field in schema.fields.items()}
    return found


def unclassified(keys) -> list:
    """The (method, path, field)s of `keys` that are in no table."""
    return sorted(key for key in set(keys) - set(CASES) - set(EXEMPT) if key[2] not in NON_OBJECT_FIELDS)


def test_a_field_in_no_table_fails_the_walk():
    """Whatever its name: `community`, `target`, a `*_list`, or a field an object case already names elsewhere."""
    new = {('GET', '/api/alpha/post', 'community'), ('POST', '/api/alpha/post/like', 'target'),
           ('GET', '/api/alpha/post', 'flair_id_list'), ('GET', '/api/alpha/comment', 'post_id')}
    assert unclassified(new) == sorted(new)


def test_every_field_an_endpoint_takes_is_classified(app):
    """A new endpoint, or a new field, fails here until it is in CASES (and goes through a finder), EXEMPT, or on the
    allowlist of fields that name no row (N5)."""
    keys = fields(app)
    unknown = unclassified(keys)
    assert unknown == [], '\n'.join(map(str, unknown))
    assert sorted((set(CASES) | set(EXEMPT) | set(SAME_FOR_A_FRESH_ROW)) - keys) == []   # no stale entry
    assert not {key[2] for key in CASES} & set(NON_OBJECT_FIELDS)


def hidden_and_missing(world, viewer, kind):
    """The value naming a row of `kind` hidden here, and the one naming no row, for `viewer`."""
    values = {
        'post': (world.post.id, NOBODY), 'posts': ([world.post.id], [NOBODY]), 'reply': (world.reply.id, NOBODY),
        'community': (world.onion_art.id, NOBODY), 'community_name': (ONION_ART, 'nosuchart'),
        'person': (world.onion_bob.id, NOBODY), 'person_name': (ONION_BOB, 'nosuchbob'),
        'conversation': (world.with_onion.id, NOBODY), 'message': (world.onion_message.id, NOBODY),
        'post_report': (world.reports[0].id, NOBODY), 'reply_report': (world.reports[1].id, NOBODY),
        'message_report': (world.reports[2].id, NOBODY), 'flair': (world.flair.id, NOBODY),
        'flairs': ([world.flair.id], [NOBODY]), 'notification': (world.notifications[viewer.id], NOBODY),
        'post_url': (f'{HOSTS[ONION]}/post/{world.post.id}', f'{HOSTS[ONION]}/post/{NOBODY}'),
        'person_prefix': ('@onlyonion', '@nosuchbob'),
    }
    return values[kind]


def visible(world, viewer, kind, key):
    """A fresh first-domain row of `kind`, as the case's field names it: the positive control (N6). Fresh, so a control
    that acts on it changes nothing a later case reads."""
    first, fresh = world.two_domains.domains[0], next(world.fresh)
    if kind in ('post', 'posts', 'post_url'):
        post = make_post(world.first_art, world.first_bob, f'https://{FIRST}/post/fresh{fresh}', title=f'FRESH{fresh}')
        make_post_reply(post, world.first_bob, f'FRESH{fresh}')   # for the lists of a post's replies
        return {'post': post.id, 'posts': [post.id], 'post_url': f'{HOSTS[FIRST]}/post/{post.id}'}[kind]
    if kind == 'reply':
        return make_post_reply(world.first_post, world.first_bob, f'FRESH{fresh}').id
    if kind in ('community', 'community_name'):
        community = world.two_domains.local_community(f'fresh{fresh}', first)
        db.session.add(CommunityMember(user_id=viewer.id, community_id=community.id, is_moderator=True))
        db.session.commit()
        post = make_post(community, world.first_bob, f'https://{FIRST}/post/freshc{fresh}', title=f'FRESH{fresh}')
        make_post_reply(post, world.first_bob, f'FRESH{fresh}')   # for the lists filtered by community
        return community.id if kind == 'community' else community.name
    if kind in ('person', 'person_name', 'person_prefix'):
        user = world.two_domains.local_user(f'fresh{fresh}', first)
        post = make_post(world.first_art, user, f'https://{FIRST}/post/freshu{fresh}', title=f'FRESH{fresh}')
        make_post_reply(post, user, f'FRESH{fresh}')   # for the lists filtered by author
        if kind == 'person_prefix':
            return f'@{user.user_name}'
        if kind == 'person':
            db.session.add(UserRegistration(user_id=user.id, answer='FRESH', status=0))
            db.session.commit()
        return user.id if kind == 'person' else user.user_name
    if kind in ('conversation', 'message', 'message_report'):
        other = world.admin if viewer is world.first_bob else world.first_bob
        conversation = Conversation(user_id=viewer.id)
        conversation.members.extend([viewer, other])
        db.session.add(conversation)
        db.session.commit()
        sent_by_viewer = key[1].endswith(('/private_message', '/private_message/delete'))   # an edit or a delete
        sender, recipient = (viewer, other) if sent_by_viewer else (other, viewer)
        message = ChatMessage(conversation_id=conversation.id, sender_id=sender.id, recipient_id=recipient.id,
                              body=f'FRESH{fresh}', body_html=f'<p>FRESH{fresh}</p>')
        db.session.add(message)
        db.session.commit()
        report = Report(reasons='spam', description='FRESH', type=4, reporter_id=world.first_bob.id, status=0,
                        suspect_conversation_id=conversation.id, source_instance_id=1, suspect_user_id=other.id,
                        targets={'suspect_message_id': message.id})   # for the report lists
        db.session.add(report)
        db.session.commit()
        return {'conversation': conversation.id, 'message': message.id, 'message_report': report.id}[kind]
    elif kind == 'post_report':
        report = Report(reasons='spam', description='FRESH', type=1, reporter_id=world.first_bob.id, status=0,
                        suspect_post_id=world.first_post.id, in_community_id=world.first_art.id, source_instance_id=1,
                        suspect_user_id=world.first_bob.id, targets={'suspect_post_id': world.first_post.id})
    elif kind == 'reply_report':
        reply = make_post_reply(world.first_post, world.first_bob, f'FRESH{fresh}')
        report = Report(reasons='spam', description='FRESH', type=2, reporter_id=world.first_bob.id, status=0,
                        suspect_post_id=world.first_post.id, suspect_post_reply_id=reply.id,
                        in_community_id=world.first_art.id, source_instance_id=1, suspect_user_id=world.first_bob.id,
                        targets={'suspect_comment_id': reply.id})
    elif kind in ('flair', 'flairs'):
        flair = CommunityFlair(community_id=world.first_art.id, flair=f'FRESH{fresh}')
        db.session.add(flair)
        db.session.commit()
        return flair.id if kind == 'flair' else [flair.id]
    else:   # a notification of the viewer's about the first domain's post
        notification = Notification(user_id=viewer.id, title='t', url=f'/post/{world.first_post.id}', notif_type=4,
                                    subtype='new_reply_on_followed_comment', author_id=world.first_bob.id,
                                    targets={'post_id': world.first_post.id})
        db.session.add(notification)
        db.session.commit()
        return notification.id
    db.session.add(report)
    db.session.commit()
    return report.id


def snapshot(world):
    """What a hidden or missing id must leave as it was: the rows of the tables an action writes, and the onion rows'
    own state."""
    tables = ('post', 'post_reply', 'community', '"user"', 'user_block', 'user_note', 'user_follower',
              'community_member',
              'community_block', 'community_ban', 'community_flair', 'post_vote', 'post_reply_vote', 'post_bookmark',
              'post_reply_bookmark', 'notification_subscription', 'report', 'chat_message', 'conversation_member',
              'user_flair', 'user_registration', 'read_posts', 'post_flair', 'mod_log')
    counts = ', '.join(f'(SELECT count(*) FROM {table})' for table in tables)
    state = db.session.execute(text(
        f'SELECT {counts}, (SELECT row(banned, deleted)::text FROM "user" WHERE id = :bob), '
        '(SELECT row(banned, nsfw)::text FROM community WHERE id = :art), '
        '(SELECT row(deleted, sticky, nsfw, status)::text FROM post WHERE id = :post), '
        '(SELECT row(deleted, body)::text FROM post_reply WHERE id = :reply), '
        '(SELECT flair FROM community_flair WHERE id = :flair), '
        '(SELECT string_agg(read::text, \',\' ORDER BY id) FROM notification WHERE id = ANY(:notes)), '
        '(SELECT string_agg(status::text, \',\' ORDER BY id) FROM report)'),
        {'bob': world.onion_bob.id, 'art': world.onion_art.id, 'post': world.post.id, 'reply': world.reply.id,
         'flair': world.flair.id, 'notes': list(world.notifications.values())}).one()
    return tuple(state)


@pytest.fixture
def walk(reports, two_domains, monkeypatch):  # noqa: F811
    """The onion world. Its hosted domains do not change while it is walked, so their rows are read once, as
    production's cache has them, not on every request as under the suite's null cache."""
    rows = storefront.domain_rows()
    monkeypatch.setattr(storefront, 'domain_rows', lambda: rows)
    onion = reports
    onion.two_domains, onion.fresh = two_domains, itertools.count()
    grant_permission(onion.admin, 'approve registrations')
    two_domains.local_user('onlyonion', next(domain for domain in two_domains.domains if not domain.federates))
    onion.onion_message = ChatMessage.query.filter_by(conversation_id=onion.with_onion.id,
                                                      recipient_id=onion.first_bob.id).first()
    onion.flair = CommunityFlair(community_id=onion.onion_art.id, flair='ONIONFLAIR')
    db.session.add(onion.flair)
    db.session.add(UserRegistration(user_id=onion.onion_bob.id, answer='ONIONANSWER', status=0))
    onion.notifications = {}
    for viewer in (onion.admin, onion.first_bob):
        notification = Notification(user_id=viewer.id, title='t', url=f'/comment/{onion.reply.id}', notif_type=4,
                                    subtype='new_reply_on_followed_comment', author_id=onion.onion_bob.id,
                                    targets={'comment_id': onion.reply.id, 'post_id': onion.post.id})
        db.session.add(notification)
        db.session.flush()
        onion.notifications[viewer.id] = notification.id
    db.session.commit()
    return onion


def request_for(routes, world, method, path, field, value, also):
    """The query string or body naming `value` in `field`: every required field, the rows `also` names, then it.
    `routes` maps each endpoint to the required fields its schemas take."""
    payload = dict(routes[(method, path)])
    named = {'first_art': world.first_art.id, 'first_bob': world.first_bob.id, 'admin': world.admin.id,
             'first_post': world.first_post.id}
    payload.update({key: name[1] if isinstance(name, tuple) else named[name] for key, name in also.items()})
    payload[field] = value
    return payload


_HEADERS = weakref.WeakKeyDictionary()   # a viewer's token, made once: the rows are each test's own


def ask(app, viewer, method, path, payload):
    forget_login()
    if viewer not in _HEADERS:
        _HEADERS[viewer] = {'Authorization': f'Bearer {viewer.encode_jwt_token()}'}
    headers = _HEADERS[viewer]
    client = app.test_client()
    if method == 'GET':
        answer = client.get(path, base_url=HOSTS[FIRST], headers=headers, query_string=payload)
    else:
        answer = client.open(path, method=method, base_url=HOSTS[FIRST], headers=headers, json=payload)
    return answer.status_code, answer.get_data()


def spelled(value) -> bytes:
    return str(value[0] if isinstance(value, list) else value).encode()


def test_a_hidden_id_answers_as_one_nobody_holds_on_every_endpoint(app, walk, subtests):
    """Byte for byte, but for the id or name itself where an answer spells it back, as the admin and as a user; the
    world is built once (N7). A hidden or missing id writes nothing, so no case changes the next. A fresh visible row
    of the same kind answers differently from the missing one, for one of the viewers at least, so each case reaches
    its lookup (N6)."""
    routes = {(method, path): {name: value for parameter in parameters
                               for name, value in minimal_payload(parameter['schema']).items()}
              for method, path, parameters in documented_routes(app)}
    for key in sorted(CASES):
        method, path, field = key
        kind, also = CASES[key]
        with subtests.test(msg=f'{method} {path} {field}'):
            before, missing_answers = snapshot(walk), {}
            for viewer in (walk.admin, walk.first_bob):
                hidden, missing = hidden_and_missing(walk, viewer, kind)
                found = ask(app, viewer, method, path, request_for(routes, walk, method, path, field, hidden, also))
                absent = ask(app, viewer, method, path, request_for(routes, walk, method, path, field, missing, also))
                missing_answers[viewer] = (missing, absent)
                absent = (absent[0], absent[1].replace(spelled(missing), spelled(hidden)))
                assert found == absent, (key, viewer.user_name, found[0], found[1][:300], absent[0], absent[1][:300])
                assert found[0] < 500 and found[0] != 422, (key, found[1][:300])
            assert snapshot(walk) == before, (key, 'a hidden or missing id wrote something')
            if key in SAME_FOR_A_FRESH_ROW:
                continue
            reached = False
            for viewer in (walk.admin, walk.first_bob):
                missing, absent = missing_answers[viewer]
                twin = visible(walk, viewer, kind, key)
                shown = ask(app, viewer, method, path, request_for(routes, walk, method, path, field, twin, also))
                if shown[0] != absent[0] or shown[1].replace(spelled(twin), b'') != absent[1].replace(spelled(missing),
                                                                                                     b''):
                    reached = True
                    break
            assert reached, (key, 'a visible row answers as a missing one')
