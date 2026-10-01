from __future__ import annotations

from sqlalchemy import desc, or_, text, Integer

from app import db, current_app
from app.api.alpha.views import private_message_view, conversation_report_view
from app.constants import NOTIF_MESSAGE, NOTIF_REPORT, REPORT_TYPE_MESSAGE, REPORT_STATE_RESOLVED, REPORT_STATE_NEW
# The module, not the names: app.chat.util reaches this file through
# app.activitypub.signature before they are defined (import cycle: chat.util)
import app.chat.util as chat_util
from app.models import ChatMessage, Conversation, User, Notification, Report, Site
from app.utils import authorise_api_user, markdown_to_html, user_access
from app.shared.tasks import task_selector


def get_private_message_list(auth, data):
    page = int(data['page']) if 'page' in data else 1
    limit = int(data['limit']) if 'limit' in data else 10
    unread_only = data['unread_only'] if 'unread_only' in data else False

    if limit > current_app.config["PAGE_LENGTH"]:
        limit = current_app.config["PAGE_LENGTH"]

    user_id = authorise_api_user(auth)

    # Get the list of conversation ids that the user has still joined
    joined_conversations = db.session.execute(text(
        "SELECT conversation_id FROM conversation_member WHERE user_id = :user_id AND joined = :state"),
        {"user_id": user_id, "state": True}).scalars()

    if unread_only:
        private_messages = ChatMessage.query.filter_by(recipient_id=user_id, read=False). \
            order_by(desc(ChatMessage.created_at))
    else:
        private_messages = ChatMessage.query.filter(or_(ChatMessage.recipient_id == user_id,
                    ChatMessage.sender_id == user_id)).order_by(desc(ChatMessage.created_at))
    
    # Only return conversations that the user hasn't left
    private_messages = private_messages.filter(ChatMessage.conversation_id.in_(joined_conversations))

    private_messages = private_messages.paginate(page=page, per_page=limit, error_out=False)

    pm_list = []
    for private_message in private_messages.items:
        pm_list.append(private_message_view(private_message, variant=1))

    pm_json = {
        "private_messages": pm_list,
        'next_page': str(private_messages.next_num) if private_messages.next_num else None
    }
    return pm_json


def get_private_message_conversation(auth, data):
    page = int(data['page']) if 'page' in data else 1
    limit = int(data['limit']) if 'limit' in data else 10

    if limit > current_app.config["PAGE_LENGTH"]:
        limit = current_app.config["PAGE_LENGTH"]

    user_id = authorise_api_user(auth)

    conversation = None
    conversation_ids = []

    # D1168. `person_id = int(data['person_id'])` stood at the top, ten lines
    # above the `if 'person_id' in data` that was meant to guard it, so EVERY
    # call that did not pass one -- including the `conversation_id` form this
    # endpoint documents -- was `KeyError: 'person_id'`. Measured:
    #
    #     PROBE ba3 outcome: KeyError: 'person_id'   (conversation_id given)
    #     PROBE ba6 outcome: KeyError: 'person_id'   (neither given)
    if 'person_id' in data:
        conversation_ids = db.session.execute(text(
            "SELECT conversation_id FROM conversation_member WHERE user_id = :person_id"),
            {"person_id": int(data['person_id'])}).scalars()

    # `.scalars()` answers a ScalarResult, which is a ONE-SHOT iterator: the
    # membership test below consumes it, and the query after that filters on
    # it again. A list is read as many times as it is asked for.
    joined_conversations = db.session.execute(text(
        "SELECT conversation_id FROM conversation_member WHERE user_id = :user_id AND joined = :state"),
        {"user_id": user_id, "state": True}).scalars().all()

    if 'conversation_id' in data:
        conversation = db.session.get(Conversation, data['conversation_id'])
        if conversation is None:
            raise Exception("User is not a member of this conversation")
        conversation_ids = [conversation.id]
        if conversation.id not in joined_conversations:
            raise Exception("User is not a member of this conversation")
    
    pm_list = []
    next_page = None
    if conversation_ids and joined_conversations:
        private_messages = ChatMessage.query.filter(ChatMessage.conversation_id.in_(conversation_ids),
                                                    ChatMessage.conversation_id.in_(joined_conversations),
                                                    or_(ChatMessage.recipient_id == user_id,
                                                        ChatMessage.sender_id == user_id)).\
                                            filter(ChatMessage.recipient_id != None).\
                                            order_by(desc(ChatMessage.created_at))
        private_messages = private_messages.paginate(page=page, per_page=limit, error_out=False)
        for private_message in private_messages:
            pm_list.append(private_message_view(private_message, variant=1))

        next_page = str(private_messages.next_num) if private_messages.next_num else None

    pm_json = {
        "private_messages": pm_list,
        'next_page': next_page
    }
    return pm_json


def _get_single_conversation_history(conversation: int | Conversation, limit: int = 5):
    """
    Fetch the most recent `limit` number of messages from a conversation.
    No auth checking, use only after checking for read privileges.
    """
    
    if isinstance(conversation, int):
        conversation = db.session.get(Conversation, conversation)
    
    private_messages = ChatMessage.query.filter(ChatMessage.conversation_id == conversation.id).\
                            order_by(desc(ChatMessage.created_at)).limit(limit).all()
    
    return private_messages


def post_leave_conversation(auth, data):
    user = authorise_api_user(auth, return_type="model")
    conversation_id = data["conversation_id"]
    conversation = db.session.get(Conversation, conversation_id)

    # D1170. A second `if conversation.is_member(user):` wrapped the body,
    # immediately below the guard that has already refused everyone it would
    # have excluded: an arm that could not be false.
    if not conversation or not conversation.is_member(user):
        raise Exception("You are not a part of this conversation")

    db.session.execute(text("UPDATE conversation_member SET joined = :state WHERE user_id = :person_id AND conversation_id = :conversation_id"),
                       {"state": False, "person_id": user.id, "conversation_id": conversation_id})
    db.session.commit()

    conversation.delete_if_abandoned()

    return


def post_private_message(auth, data):
    sender = authorise_api_user(auth, return_type='model')
    recipient = User.query.filter_by(id=data['recipient_id']).one()

    if not sender.can_send_pm_to(recipient):
        raise Exception(
            "You are not permitted to send a private message at this time to this recipient, likely because your "
            "account is too new.")

    existing_conversation = Conversation.find_existing_conversation(recipient=recipient, sender=sender)
    if not existing_conversation:
        existing_conversation = Conversation(user_id=sender.id)
        existing_conversation.members.append(recipient)
        existing_conversation.members.append(sender)
        db.session.add(existing_conversation)
        db.session.commit()

    private_message = chat_util.send_message(data['content'], existing_conversation.id, user=sender)

    pm_json = private_message_view(private_message, variant=2)
    return pm_json


def post_private_message_mark_as_read(auth, data):
    user = authorise_api_user(auth, return_type='model')
    message_id = data['private_message_id']
    read = data['read']

    private_message = ChatMessage.query.filter_by(id=message_id, recipient_id=user.id).one()
    private_message.read = read

    notif_read = not read
    notifications = Notification.query.filter_by(user_id=user.id, notif_type=NOTIF_MESSAGE,
                                                 subtype='chat_message', read=notif_read)
    for notification in notifications:
        if 'message_id' in notification.targets and notification.targets['message_id'] == message_id:
            notification.read = read
            if read == True and user.unread_notifications > 0:
                user.unread_notifications -= 1
            elif read == False:
                user.unread_notifications += 1
            break
    db.session.commit()

    return private_message_view(private_message, variant=2)


def put_private_message(auth, data):
    chat_message_id = int(data['private_message_id'])
    content = data['content']

    user_id = authorise_api_user(auth)
    # User may only edit own messages
    private_message = ChatMessage.query.filter_by(sender_id=user_id, id=chat_message_id, deleted=False).one()
    private_message.body = content
    private_message.body_html = markdown_to_html(content)
    db.session.commit()

    chat_util.update_message(private_message)

    return private_message_view(private_message, variant=2)


def post_private_message_delete(auth, data):
    chat_message_id = int(data['private_message_id'])
    deleted = data['deleted']

    user_id = authorise_api_user(auth)
    private_message = ChatMessage.query.filter_by(sender_id=user_id, id=chat_message_id).one()
    private_message.deleted = deleted
    db.session.commit()

    if deleted:
        task_selector('delete_pm', message_id=private_message.id)
    else:
        task_selector('restore_pm', message_id=private_message.id)

    return private_message_view(private_message, variant=2)


def post_private_message_report(auth, data):
    chat_message_id = data['private_message_id']
    reason = data['reason']

    user_id = authorise_api_user(auth)

    # user may only report received messages
    private_message = ChatMessage.query.filter_by(recipient_id=user_id, id=chat_message_id).one()
    private_message.reported = True

    targets_data = {
            "gen": '0',
            "suspect_conversation_id": private_message.conversation_id,
            "reporter_id": user_id,
            "suspect_message_id": chat_message_id
    }
    report = Report(reasons=reason,
                    description='',
                    type=REPORT_TYPE_MESSAGE,
                    reporter_id=user_id,
                    suspect_conversation_id=private_message.conversation_id,
                    source_instance_id=1,
                    targets=targets_data)
    db.session.add(report)

    # D1171. An `already_notified = set()` stood here with `if admin.id not in
    # already_notified:` around the body -- and nothing ever added to the set,
    # so the test was always true. A de-duplication that de-duplicates nothing
    # reads like the question has been dealt with; `Site.admins()` answers
    # distinct rows, so there is nothing here to de-duplicate.
    for admin in Site.admins():
        notify = Notification(title='Reported conversation with user', url='/admin/reports',
                              user_id=admin.id,
                              author_id=user_id, notif_type=NOTIF_REPORT,
                              subtype='chat_conversation_reported',
                              targets=targets_data)
        db.session.add(notify)
        admin.unread_notifications += 1
    db.session.commit()

    return {"private_message_report_view": private_message_view(private_message, variant=3, report=report)}


def post_private_message_conversation_report(auth, data):
    user = authorise_api_user(auth, return_type="model")
    conversation_id = data["conversation_id"]
    reason = data["reason"]
    conversation = db.session.get(Conversation, conversation_id)

    # D1167. This was
    #
    #     if not (conversation or conversation.is_member(user) or
    #             user_access("administer all users", user.id)):
    #
    # -- `or` where each arm was meant to be required. A conversation that
    # exists makes the whole disjunction true, so `not` is false and NOTHING
    # was refused: any account could report any conversation by id, and the
    # admin report list hands back that conversation's message history.
    # Measured:
    #
    #     PROBE ba1 outcome: accepted | reports filed: 1
    #     PROBE ba2 1 reports, bodies: ['SECRETBODY']
    #
    # So a stranger could put any two people's private messages in front of
    # the administrators, one conversation id at a time. The other half of the
    # same expression: with no such conversation, `conversation.is_member`
    # was an AttributeError on None.
    if not conversation:
        raise Exception("You are not a part of this conversation")

    if not (conversation.is_member(user) or user_access("administer all users", user.id)):
        raise Exception("You are not a part of this conversation")

    # Create the report
    targets_data = {'gen': "0", 'suspect_conversation_id': conversation_id, 'reporter_id': user.id}
    report = Report(
        reasons=reason,
        type=REPORT_TYPE_MESSAGE,
        reporter_id=user.id,
        suspect_conversation_id=conversation_id,
        source_instance_id=1,
        targets=targets_data)
    db.session.add(report)

    # Create the notifications
    for admin in Site.admins():
        notify = Notification(title='Reported conversation with user',
                              url='/admin/reports',
                              user_id=admin.id,
                              author_id=user.id,
                              notif_type=NOTIF_REPORT,
                              subtype='chat_conversation_reported',
                              targets=targets_data)
        db.session.add(notify)
        admin.unread_notifications += 1
    db.session.commit()
    
    return


def get_private_message_report_list(auth, data):
    conversation_id = data['conversation_id'] if 'conversation_id' in data else None
    private_message_id = data['private_message_id'] if 'private_message_id' in data else None
    limit = data['limit'] if 'limit' in data else 20
    page = data['page'] if 'page' in data else 1
    unresolved_only = data['unresolved_only'] if 'unresolved_only' in data else True

    user = authorise_api_user(auth, return_type="model")

    if not user_access('administer all users', user.id):
        raise Exception("incorrect login")

    if private_message_id:
        reports = Report.query.filter(Report.targets.op("->>")("suspect_message_id").cast(Integer) == private_message_id)
    elif conversation_id:
        reports = Report.query.filter(Report.suspect_conversation_id == conversation_id,
                                      Report.targets.op("->")("suspect_message_id") != None)
    else:
        reports = Report.query.filter(Report.targets.op("->")("suspect_message_id") != None)
    
    if unresolved_only:
        reports = reports.filter(Report.status < REPORT_STATE_RESOLVED)
    
    reports = reports.paginate(page=page, per_page=limit, error_out=False)

    report_list = []
    for report in reports.items:
        private_message = db.session.get(ChatMessage, int(report.targets["suspect_message_id"]))
        report_list.append(private_message_view(private_message, variant=3, report=report))

    reply_json = dict()
    reply_json["private_message_reports"] = report_list
    reply_json["next_page"] = str(reports.next_num) if reports.next_num else None

    return reply_json


def get_private_message_conversation_report_list(auth, data):
    conversation_id = data['conversation_id'] if 'conversation_id' in data else None
    limit = data['limit'] if 'limit' in data else 20
    page = data['page'] if 'page' in data else 1
    unresolved_only = data['unresolved_only'] if 'unresolved_only' in data else True
    message_history_limit = data['message_history_limit'] if 'message_history_limit' in data else 5

    user = authorise_api_user(auth, return_type="model")

    if not user_access('administer all users', user.id):
        raise Exception("incorrect login")
    
    if conversation_id:
        reports = Report.query.filter(Report.suspect_conversation_id == conversation_id)
    else:
        reports = Report.query.filter(Report.suspect_conversation_id != None)
    
    if unresolved_only:
        reports = reports.filter(Report.status < REPORT_STATE_RESOLVED)
    
    reports = reports.paginate(page=page, per_page=limit, error_out=False)

    report_list = []
    for report in reports.items:
        conversation = db.session.get(Conversation, report.suspect_conversation_id)
        message_history = _get_single_conversation_history(conversation, limit=message_history_limit)
        message_history = [private_message_view(message, variant=1) for message in message_history]

        report_json = conversation_report_view(report=report, variant=2)
        report_json["message_history"] = message_history

        report_list.append(report_json)
    
    reply_json = dict()
    reply_json["conversation_reports"] = report_list
    reply_json["next_page"] = str(reports.next_num) if reports.next_num else None

    return reply_json

def put_private_message_report_resolve(auth, data):
    report_id = data['report_id']
    resolved = data['resolved']

    user = authorise_api_user(auth, return_type="model")

    if not user_access("administer all users", user.id):
        raise Exception("incorrect login")
    
    # D1169. `db.session.get` answers None for an id that does not resolve,
    # and the line below it read `.targets` off that None: `AttributeError:
    # 'NoneType' object has no attribute 'targets'`, measured as PROBE ba7.
    report = db.session.get(Report, report_id)

    if report is None or "suspect_message_id" not in report.targets:
        raise Exception("invalid target of resolution")
    
    if resolved:
        report.status = REPORT_STATE_RESOLVED
    else:
        report.status = REPORT_STATE_NEW
    
    db.session.commit()

    private_message = db.session.get(ChatMessage, report.targets["suspect_message_id"])

    return {"private_message_report_view": private_message_view(private_message, variant=3, report=report)}


def put_private_message_conversation_report_resolve(auth, data):
    report_id = data['report_id']
    resolved = data['resolved']

    user = authorise_api_user(auth, return_type="model")

    if not user_access("administer all users", user.id):
        raise Exception("incorrect login")
    
    report = db.session.get(Report, report_id)  # D1169's twin

    if report is None or not report.suspect_conversation_id:
        raise Exception("invalid target of resolution")
    
    if resolved:
        report.status = REPORT_STATE_RESOLVED
    else:
        report.status = REPORT_STATE_NEW
    
    db.session.commit()

    return
