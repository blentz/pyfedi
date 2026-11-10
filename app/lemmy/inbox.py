"""A person's inbox in Lemmy's API (spec 2026-11-10-lemmy-v3-design §2): the unread counts behind Jerboa's badge,
replies, mentions and private messages, and marking them read, over alpha's handlers."""
from flask import jsonify, request
from marshmallow import EXCLUDE

from app import limiter
from app.api.alpha.routes import MESSAGE_LIMIT, MESSAGE_SCOPE, is_trusted_request
from app.api.alpha.schema import CreatePrivateMessageRequest, ListPrivateMessagesRequest, MarkCommentAsReadRequest, \
    MarkPrivateMessageAsReadRequest, UserMentionsRequest, UserRepliesRequest
from app.api.alpha.utils.private_message import get_private_message_list, post_private_message, \
    post_private_message_mark_as_read
from app.api.alpha.utils.reply import post_reply_mark_as_read
from app.api.alpha.utils.user import get_user_replies, get_user_unread_count, post_user_mark_all_as_read
from app.lemmy import arguments, bp, shapes

LISTED = ('sort', 'page', 'limit', 'unread_only')



def _query(schema) -> dict:
    raw = {name: request.args[name] for name in LISTED if request.args.get(name) not in (None, '')}
    return schema().load(raw, unknown=EXCLUDE)


@bp.route('/api/v3/user/unread_count')
def unread_count():
    counts = get_user_unread_count(arguments.auth())
    return jsonify(replies=counts['replies'], mentions=counts['mentions'], private_messages=counts['private_messages'])


@bp.route('/api/v3/user/replies')
def replies():
    alpha = get_user_replies(arguments.auth(), _query(UserRepliesRequest))
    return jsonify(replies=[shapes.inbox_view(view, 'comment_reply') for view in alpha['replies']])


@bp.route('/api/v3/user/mention')
def mentions():
    alpha = get_user_replies(arguments.auth(), _query(UserMentionsRequest), mentions=True)
    return jsonify(mentions=[shapes.inbox_view(view, 'person_mention') for view in alpha['replies']])


@bp.route('/api/v3/comment/mark_as_read', methods=['POST'])
def mark_reply_read():
    alpha = post_reply_mark_as_read(arguments.auth(), arguments.body(MarkCommentAsReadRequest))
    return jsonify(comment_reply_view=shapes.inbox_view(alpha['comment_reply_view'], 'comment_reply'))


@bp.route('/api/v3/user/mention/mark_as_read', methods=['POST'])
def mark_mention_read():
    """A mention is marked read as alpha marks a reply: by its comment, whose notification it is."""
    data = arguments.body(MarkCommentAsReadRequest, {'person_mention_id': 'comment_reply_id'})
    alpha = post_reply_mark_as_read(arguments.auth(), data)
    return jsonify(person_mention_view=shapes.inbox_view(alpha['comment_reply_view'], 'person_mention'))


@bp.route('/api/v3/user/mark_all_as_read', methods=['POST'])
def mark_all_read():
    post_user_mark_all_as_read(arguments.auth())
    return jsonify(replies=[])


@bp.route('/api/v3/private_message/list')
def private_messages():
    raw = {name: request.args[name] for name in ('page', 'limit', 'unread_only')
           if request.args.get(name) not in (None, '')}
    alpha = get_private_message_list(arguments.auth(), ListPrivateMessagesRequest().load(raw, unknown=EXCLUDE))
    return jsonify(private_messages=[shapes.private_message_view(view) for view in alpha['private_messages']])


@bp.route('/api/v3/private_message', methods=['POST'])
def send_private_message():
    data = arguments.body(CreatePrivateMessageRequest)
    with limiter.shared_limit(MESSAGE_LIMIT, scope=MESSAGE_SCOPE, exempt_when=is_trusted_request):   # alpha's bucket
        alpha = post_private_message(arguments.auth(), data)
    return jsonify(private_message_view=shapes.private_message_view(alpha['private_message_view']))


@bp.route('/api/v3/private_message/mark_as_read', methods=['POST'])
def mark_private_message_read():
    alpha = post_private_message_mark_as_read(arguments.auth(), arguments.body(MarkPrivateMessageAsReadRequest))
    return jsonify(private_message_view=shapes.private_message_view(alpha['private_message_view']))
