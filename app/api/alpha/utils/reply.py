from flask import g, current_app
from sqlalchemy import text, or_

from app import db
from app.api.alpha.views import reply_view, reply_stub_view, reply_removal_ack_view, reply_report_view, post_view, community_view, user_view
from app.constants import *
from app.models import Notification, PostReply, Post, PostReplyVote, Report, Community, User
from app.shared.reply import vote_for_reply, bookmark_reply, remove_bookmark_reply, subscribe_reply, make_reply, \
    edit_reply, \
    delete_reply, restore_reply, report_reply, mod_remove_reply, mod_restore_reply, lock_post_reply, choose_answer, \
    unchoose_answer
from app.local_only import found_here, prime_reporters, without_hidden_reports, without_local_only_actors
from app.api.alpha.utils.found import a_community, a_post, a_reply
from app.visibility import can_view
from app.utils import can_moderate_at_all, authorise_api_user, site_language_id, \
    communities_banned_from, in_sorted_list, user_access, reachable_community_ids, \
    can_moderate
import app.reads.replies as reads_replies
from app.reads.viewer import Restrictions


def get_reply_list(auth, data, user_details=None):
    if auth:
        user_details = authorise_api_user(auth, return_type='dict')

    if user_details:
        user_id = user_details['id']
    else:
        user_id = None

    # user_id: the logged in user
    # person_id: the author of the posts being requested
    page = int(data['page']) if 'page' in data else 1
    limit = int(data['limit']) if 'limit' in data else 10
    sort = data['sort'] if 'sort' in data else 'New'

    if limit > current_app.config["PAGE_LENGTH"]:
        limit = current_app.config["PAGE_LENGTH"]

    def optional_int(name):
        return int(data[name]) if name in data else None

    liked_only = bool('liked_only' in data and data['liked_only'])
    saved_only = bool('saved_only' in data and data['saved_only'])
    person_id = optional_int('person_id')
    community_id = optional_int('community_id')
    listing = reads_replies.CommentListing(
        type=data['type_'] if 'type_' in data else 'All', sort=sort, query=data.get("q", None),
        liked_only=liked_only, saved_only=saved_only, person_id=person_id, community_id=community_id,
        post_id=optional_int('post_id'), parent_id=optional_int('parent_id'),
        max_depth=optional_int('max_depth'), depth_first=bool('depth_first' in data and data['depth_first']),
        page=page, limit=limit)
    # the viewer's blocks, but not on their own profile listing
    restrictions = Restrictions(user_id) if user_id and (person_id is None or user_id != person_id) \
        else Restrictions.none()
    rows = reads_replies.comment_listing(
        user_id, restrictions, listing,   # an anonymous liked or saved listing is refused there
        liked_reply_ids=user_details['upvoted_reply_ids'] if liked_only and user_id else (),
        saved_reply_ids=user_details['bookmarked_reply_ids'] if saved_only and user_id else ())

    # LIKED_ONLY
    vote_effect = 1 if liked_only else None
    by_liked_only = liked_only

    # SAVED_ONLY
    is_reply_bookmarked = True if saved_only else None
    by_saved_only = saved_only

    # PERSON_ID
    add_creator_in_view = True
    is_creator_blocked = None
    is_creator_admin = None
    by_person_id = False
    if person_id is not None:
        add_creator_in_view = False
        is_creator_blocked = person_id in user_details['blocked_creator_ids'] if user_details else False
        is_creator_admin = person_id in g.admin_ids
        by_person_id = True

    # COMMUNITY_ID
    add_community_in_view = True
    is_user_banned_from_community = None
    is_user_following_community = None
    is_user_moderator = None
    by_community_id = False
    if community_id is not None:
        add_community_in_view = False
        is_user_banned_from_community = community_id in user_details['user_ban_community_ids'] if user_details else False
        is_user_following_community = community_id in user_details['followed_community_ids'] if user_details else False
        is_user_moderator = community_id in user_details['moderated_community_ids'] if user_details else False
        by_community_id = True

    is_creator_banned_from_community = None
    is_creator_moderator = None

    # a conversation, and a listing narrowed to one post, carry the post and community once rather than on each reply
    add_post_in_view = True
    if rows.conversation or listing.post_id is not None:
        add_community_in_view = False
        add_post_in_view = False

    replies = rows.replies
    next_page = rows.next_page
    if not rows.one_page:
        replies = replies.paginate(page=page, per_page=limit, error_out=False)
        next_page = str(replies.next_num) if replies.next_num is not None else None

    reply_list = []
    inner_creator_view = inner_community_view = inner_post_view = None
    stub_neutrals = {}  # D7: one neutral community and post per post, however many of its replies are stubs
    for reply in replies:
        if not can_view(reply, user_id):
            reply_list.append(reply_stub_view(reply, stub_neutrals))  # D18
            continue
        if add_creator_in_view == False and add_community_in_view == False:
            if is_creator_banned_from_community is None:
                is_creator_banned_from_community = reply.community_id in communities_banned_from(reply.user_id)
            if is_creator_moderator is None:
                is_creator_moderator = reply.community.is_moderator(reply.author)

        if by_liked_only == False:
            vote_effect = 0
            if user_details and in_sorted_list(user_details['upvoted_reply_ids'], reply.id):
                vote_effect = 1
            elif user_details and in_sorted_list(user_details['downvoted_reply_ids'], reply.id):
                vote_effect = -1

        if by_saved_only == False:
            is_reply_bookmarked = reply.id in user_details['bookmarked_reply_ids'] if user_details else False

        if by_person_id == False:
            is_creator_blocked = reply.user_id in user_details['blocked_creator_ids'] if user_details else False

        if by_community_id == False:
            if add_community_in_view == False:
                if is_user_banned_from_community is None:
                    is_user_banned_from_community = reply.community_id in user_details['user_ban_community_ids'] if user_details else False
                if is_user_following_community is None:
                    is_user_following_community = reply.community_id in user_details['followed_community_ids'] if user_details else False
                if is_user_moderator is None:
                    is_user_moderator = reply.community_id in user_details['moderated_community_ids'] if user_details else False
            else:
                is_user_banned_from_community = reply.community_id in user_details['user_ban_community_ids'] if user_details else False
                is_user_following_community = reply.community_id in user_details['followed_community_ids'] if user_details else False
                is_user_moderator = reply.community_id in user_details['moderated_community_ids'] if user_details else False

        is_reply_subscribed = reply.id in user_details['subscribed_reply_ids'] if user_details else False

        reply_json = reply_view(reply=reply, variant=3, user_id=user_id,
                                is_user_banned_from_community=is_user_banned_from_community,
                                is_user_following_community=is_user_following_community,
                                is_reply_bookmarked=is_reply_bookmarked,
                                is_creator_blocked=is_creator_blocked,
                                vote_effect=vote_effect,
                                is_reply_subscribed=is_reply_subscribed,
                                is_creator_banned_from_community=is_creator_banned_from_community,
                                is_creator_moderator=is_creator_moderator,
                                is_creator_admin=is_creator_admin,
                                is_user_moderator=is_user_moderator,
                                add_creator_in_view=add_creator_in_view,
                                add_post_in_view=add_post_in_view,
                                add_community_in_view=add_community_in_view)

        if add_creator_in_view == False:
            if inner_creator_view is None:
                inner_creator_view = user_view(user=reply.author, variant=1, stub=True,
                                               flair_community_id=reply.community_id, user_id=user_id)
            reply_json['creator'] = inner_creator_view
        if add_community_in_view == False:
            if inner_community_view is None:
                inner_community_view = community_view(community=reply.community, variant=1, stub=True)
            reply_json['community'] = inner_community_view
        if add_post_in_view == False:
            if inner_post_view is None:
                inner_post_view = post_view(post=reply.post, variant=1, user_id=user_id)
            reply_json['post'] = inner_post_view

        reply_list.append(reply_json)

    list_json = {
        "comments": reply_list,
        "next_page": next_page
    }

    return list_json


# D1194. Ten endpoints in this module read attributes straight off a
# `db.session.get(...)`, which answers None for an id nobody holds -- so a
# caller naming a comment, post or report that does not exist got an
# AttributeError rather than an answer. Measured, one per endpoint:
#
#     PROBE bm get_reply: AttributeError: 'NoneType' object has no attribute 'community_id'
#     PROBE bm put_reply: ... 'language_id'
#     PROBE bm post_reply_report: ... 'user_id'
#     PROBE bm post_reply_mark_as_read: ... 'id'
#     PROBE bm post_reply_mark_as_answer: ... 'user_id'
#     PROBE bm post_reply_distinguish: ... 'author'
#     PROBE bm get_reply_like_list: ... 'community'
#     PROBE bm put_reply_report_resolve: ... 'suspect_post_reply_id'
#     PROBE bm get_reply_report_list: ... 'community'
#
# One helper, so the ten cannot drift apart again.
def a_visible_reply(reply_id, auth):
    """a_reply for an action: a comment the caller may not see is `comment not found`, as if nobody held the id.
    Checked before the action runs. Moderators get no exemption (D19)."""
    reply = a_reply(reply_id)
    user_id = authorise_api_user(auth) if auth else None
    if not can_view(reply, user_id):
        raise Exception('comment not found')
    return reply


def a_moderatable_reply(reply_id, auth):
    """a_visible_reply for a delete or removal. D19 restricts seeing, not enforcement: a moderator or admin of the
    reply's community may act on a followers-only reply they cannot view."""
    reply = a_reply(reply_id)
    user = authorise_api_user(auth, return_type='model') if auth else None
    if user is not None and can_moderate_at_all(reply.community, user):
        return reply
    return a_visible_reply(reply_id, auth)


def get_reply(auth, data):
    id = int(data['id'])
    a_reply(id)

    user_id = authorise_api_user(auth) if auth else None

    reply_json = reply_view(reply=id, variant=4, user_id=user_id)
    return reply_json


def post_reply_like(auth, data):
    user = authorise_api_user(auth, return_type="model")
    # D1197. An `if not user: raise Exception("incorrect login")` stood here,
    # and `authorise_api_user` raises for every way authorisation can fail --
    # it never answers a falsy user. Three of these, plus a fourth spelled
    # `if not user_id`.

    a_visible_reply(data['comment_id'], auth)
    score = data['score']
    reply_id = data['comment_id']
    emoji = data['emoji'] if 'emoji' in data else None
    if score == 1:
        direction = 'upvote'
    elif score == -1:
        direction = 'downvote'
    else:
        score = 0
        direction = 'reversal'
    private = data['private'] if 'private' in data else bool(user.vote_privately)

    user_id = vote_for_reply(reply_id, direction, not private, emoji, SRC_API, auth)
    reply_json = reply_view(reply=reply_id, variant=4, user_id=user_id)
    return reply_json


def put_reply_save(auth, data):
    reply_id = a_visible_reply(data['comment_id'], auth).id
    save = data['save']

    user_id = bookmark_reply(reply_id, SRC_API, auth) if save else remove_bookmark_reply(reply_id, SRC_API, auth)
    reply_json = reply_view(reply=reply_id, variant=4, user_id=user_id, is_reply_bookmarked=save)
    return reply_json


def put_reply_subscribe(auth, data):
    reply_id = a_visible_reply(data['comment_id'], auth).id
    subscribe = data['subscribe']

    user_id = subscribe_reply(reply_id, subscribe, SRC_API, auth)
    reply_json = reply_view(reply=reply_id, variant=4, user_id=user_id, is_reply_subscribed=subscribe)
    return reply_json


def post_reply(auth, data):
    body = data['body']
    post_id = data['post_id']
    parent_id = data['parent_id'] if 'parent_id' in data else None
    # D1195. `if language_id < 2` on a value that can be None: the default is
    # `site_language_id()`, which answers None when the Site carries no
    # language and the Language table has no 'en' row -- a fresh instance
    # before its languages are seeded. `None < 2` is `TypeError: '<' not
    # supported between instances of 'NoneType' and 'int'`, measured as PROBE
    # bm post_reply. `put_reply`, the sibling below, already writes
    # `if language_id is None or language_id < 2` (fact 478).
    language_id = data['language_id'] if 'language_id' in data else site_language_id()
    if language_id is None or language_id < 2:
        language_id = site_language_id()

    input = {'body': body, 'notify_author': True, 'language_id': language_id}
    post = a_post(post_id)   # off its own domain an onion post is as unknown as an id nobody holds (follow-up F6)
    if not can_view(post, authorise_api_user(auth)):
        raise Exception('post not found')
    if parent_id is not None:
        a_visible_reply(parent_id, auth)

    user_id, reply = make_reply(input, post, parent_id, SRC_API, auth)

    reply_json = reply_view(reply=reply, variant=4, user_id=user_id)
    return reply_json


def put_reply(auth, data):
    reply_id = data['comment_id']
    reply = a_visible_reply(reply_id, auth)

    body = data['body'] if 'body' in data else reply.body
    language_id = data['language_id'] if 'language_id' in data else reply.language_id
    distinguished = data['distinguished'] if 'distinguished' in data else reply.distinguished
    if language_id is None or language_id < 2:
        language_id = site_language_id()
    if distinguished is None:
        distinguished = False

    input = {'body': body, 'notify_author': True, 'language_id': language_id, 'distinguished': distinguished}
    post = db.session.get(Post, reply.post_id)

    user_id, reply = edit_reply(input, reply, post, SRC_API, auth)

    reply_json = reply_view(reply=reply, variant=4, user_id=user_id)
    return reply_json


def post_reply_delete(auth, data):
    reply_id = a_visible_reply(data['comment_id'], auth).id
    deleted = data['deleted']

    if deleted == True:
        user_id, reply = delete_reply(reply_id, SRC_API, auth)
    else:
        user_id, reply = restore_reply(reply_id, SRC_API, auth)

    reply_json = reply_view(reply=reply, variant=4, user_id=user_id)
    return reply_json


def post_reply_report(auth, data):
    reply_id = data['comment_id']
    reason = data['reason']
    description =data['description'] if 'description' in data else ''
    report_remote = data['report_remote'] if 'report_remote' in data else True
    input = {'reason': reason, 'description': description, 'report_remote': report_remote}

    reply = a_visible_reply(reply_id, auth)
    user_id, report = report_reply(reply, input, SRC_API, auth)

    reply_json = reply_report_view(report=report, reply_id=reply_id, user_id=user_id)
    return reply_json


def get_reply_report_list(auth, data):
    user = authorise_api_user(auth, return_type="model")
    # D1197. An `if not user: raise Exception("incorrect login")` stood here,
    # and `authorise_api_user` raises for every way authorisation can fail --
    # it never answers a falsy user. Three of these, plus a fourth spelled
    # `if not user_id`.

    comment_id = data['comment_id'] if 'comment_id' in data else None
    community_id = data['community_id'] if 'community_id' in data else None
    limit = data['limit'] if 'limit' in data else 20
    page = data['page'] if 'page' in data else 1
    unresolved_only = data['unresolved_only'] if 'unresolved_only' in data else True

    if comment_id:
        # Just get reports for a single comment
        reply = a_reply(comment_id)
        mods = reply.community.moderators()
        mod_ids = [mod.user_id for mod in mods]

        if user.id in mod_ids or user_access('moderate all communities', user.id, on=reply):
            reports = Report.query.filter(Report.suspect_post_reply_id == comment_id)
        else:
            raise Exception('incorrect login')
    elif community_id:
        # Just get reports for a single community
        community = a_community(community_id)   # hidden here or nobody's: one answer (follow-up F6, R2)
        mods = community.moderators()
        mod_ids = [mod.user_id for mod in mods]

        if user.id in mod_ids or user_access("moderate all communities", user.id, on=community):
            reports = Report.query.filter(Report.in_community_id == community_id, Report.suspect_post_reply_id != None)
        else:
            raise Exception('incorrect login')
    else:
        # Don't restrict reports to single comment or community
        reports = Report.query.filter(Report.suspect_post_reply_id != None)
        reachable = reachable_community_ids('moderate all communities', user.id)
        if reachable is not None:   # those they moderate, and those on the domains they hold it on (RBAC step 8)
            reports = reports.filter(Report.in_community_id.in_(sorted(reachable)))
    
    if unresolved_only:
        reports = reports.filter(Report.status < REPORT_STATE_RESOLVED)
    reports = without_hidden_reports(reports)   # none about a row hidden here (follow-up F6)

    reports = reports.paginate(page=page, per_page=limit, error_out=False)
    prime_reporters(reports.items)   # one query for the page's reporters (follow-up F6, m5)

    report_list = []
    for report in reports.items:
        report_list.append(reply_report_view(report=report,
                                             reply_id=report.suspect_post_reply_id,
                                             user_id=user.id,
                                             variant=2))
    
    reply_json = dict()
    reply_json['comment_reports'] = report_list
    reply_json['next_page'] = str(reports.next_num) if reports.next_num else None

    return reply_json


def put_reply_report_resolve(auth, data):
    report_id = data['report_id']
    resolved = data['resolved']

    user = authorise_api_user(auth, return_type="model")
    # D1197. An `if not user: raise Exception("incorrect login")` stood here,
    # and `authorise_api_user` raises for every way authorisation can fail --
    # it never answers a falsy user. Three of these, plus a fourth spelled
    # `if not user_id`.
    
    report = found_here(Report, report_id)   # hidden here or nobody's: one answer (follow-up F6)

    # The community is tested BEFORE the lookup: `db.session.get(Community,
    # None)` warns `SAWarning: fully NULL primary key identity cannot load
    # any object`, and a report that names no community is exactly what a
    # report about a conversation looks like. `report_in_community_id_fkey`
    # means the id cannot dangle, so a set id always resolves.
    if report is None or not report.suspect_post_reply_id \
            or not report.in_community_id:
        raise Exception("invalid target of resolution")

    community = db.session.get(Community, report.in_community_id)
    mods = community.moderators()
    mod_ids = [mod.user_id for mod in mods]

    if user.id in mod_ids or user_access('moderate all communities', user.id, on=community):
        if resolved:
            report.status = REPORT_STATE_RESOLVED
        else:
            report.status = REPORT_STATE_NEW

        db.session.commit()
    else:
        raise Exception("incorrect login")
    
    reply_json = reply_report_view(report=report, reply_id=report.suspect_post_reply_id, user_id=user.id)
    return reply_json


def post_reply_remove(auth, data):
    reply_id = a_moderatable_reply(data['comment_id'], auth).id
    removed = data['removed']

    if removed == True:
        reason = data['reason'] if 'reason' in data else 'Removed by mod'
        user_id, reply = mod_remove_reply(reply_id, reason, SRC_API, auth)
    else:
        reason = data['reason'] if 'reason' in data else 'Restored by mod'
        user_id, reply = mod_restore_reply(reply_id, reason, SRC_API, auth)

    if not can_view(reply, user_id):  # a moderator who may not see the reply gets an acknowledgement, not its content (D19)
        return {'comment_view': reply_removal_ack_view(reply)}
    reply_json = reply_view(reply=reply, variant=4, user_id=user_id)
    return reply_json


def post_reply_mark_as_read(auth, data):
    reply_id = data['comment_reply_id']
    read = data['read']

    user_details = authorise_api_user(auth, return_type='dict')
    user_id = user_details['id']

    # no real support for this. Just marking the Notification for the reply really
    # notification has its own id, which would be handy, but reply_view is currently just returning the reply.id for that
    reply = a_visible_reply(reply_id, auth)

    reply_url = '#comment_' + str(reply.id)
    mention_url = '/comment/' + str(reply.id)
    notification = Notification.query.filter(Notification.user_id == user_id, Notification.read == (not read),
                                             or_(Notification.url.ilike(f"%{reply_url}%"),
                                                 Notification.url.ilike(f"%{mention_url}%"))).first()
    if notification:
        notification.read = read
        if read == True:
            db.session.execute(text(
                'UPDATE "user" SET unread_notifications = unread_notifications - 1 WHERE id = :id AND unread_notifications > 0'),
                {"id": user_id})
        elif read == False:
            db.session.execute(text(
                'UPDATE "user" SET unread_notifications = unread_notifications + 1 WHERE id = :id'),
                {"id": user_id})
        db.session.commit()

    recipient = user_view(user=user_id, variant=1)
    vote_effect = 0
    if in_sorted_list(user_details['upvoted_reply_ids'], reply_id):
        vote_effect = 1
    elif in_sorted_list(user_details['downvoted_reply_ids'], reply_id):
        vote_effect = -1
    reply_json = reply_view(reply=reply, variant=3, user_id=user_id,
        is_user_banned_from_community=reply.community_id in user_details['user_ban_community_ids'],
        is_user_following_community=reply.community_id in user_details['followed_community_ids'],
        is_reply_bookmarked=reply.id in user_details['bookmarked_reply_ids'],
        is_creator_blocked=reply.user_id in user_details['blocked_creator_ids'],
        vote_effect=vote_effect,
        is_reply_subscribed=reply.id in user_details['subscribed_reply_ids'],
        is_user_moderator=reply.community_id in user_details['moderated_community_ids'])
    reply_json['comment_reply'] = reply_view(reply=reply, variant=6, user_id=user_id, read_comment_ids=[reply_id] if read else [])
    reply_json['recipient'] = recipient
    return {'comment_reply_view': reply_json}


def post_reply_mark_as_answer(auth, data):
    reply_id = data['comment_reply_id']
    answer = data['answer']

    user_details = authorise_api_user(auth, return_type='dict')
    user_id = user_details['id']

    a_visible_reply(reply_id, auth)

    if answer:
        choose_answer(reply_id, SRC_API, auth)
    else:
        unchoose_answer(reply_id, SRC_API, auth)

    reply = db.session.get(PostReply, reply_id)
    recipient = user_view(user=user_id, variant=1)
    vote_effect = 0
    if in_sorted_list(user_details['upvoted_reply_ids'], reply_id):
        vote_effect = 1
    elif in_sorted_list(user_details['downvoted_reply_ids'], reply_id):
        vote_effect = -1
    reply_json = reply_view(reply=reply, variant=3, user_id=user_id,
        is_user_banned_from_community=reply.community_id in user_details['user_ban_community_ids'],
        is_user_following_community=reply.community_id in user_details['followed_community_ids'],
        is_reply_bookmarked=reply.id in user_details['bookmarked_reply_ids'],
        is_creator_blocked=reply.user_id in user_details['blocked_creator_ids'],
        vote_effect=vote_effect,
        is_reply_subscribed=reply.id in user_details['subscribed_reply_ids'],
        is_user_moderator=reply.community_id in user_details['moderated_community_ids'])
    reply_json['comment_reply'] = reply_view(reply=reply, variant=6, user_id=user_id, read_comment_ids=[reply_id])
    reply_json['recipient'] = recipient
    return {'comment_reply_view': reply_json}


def post_reply_distinguish(auth, data):
    reply_id = data['comment_reply_id']
    distinguished = data['distinguished']

    user = authorise_api_user(auth, return_type='model')
    user_id = user.id

    reply = a_visible_reply(reply_id, auth)
    author = reply.author

    if not author.id == user_id:
        raise Exception('incorrect login')

    # D551: the same predicate edit_reply asks before applying distinguished
    if not can_moderate(reply.community, user):
        raise Exception('insufficient permission')
    
    reply.distinguished = distinguished
    db.session.commit()

    reply_json = reply_view(reply=reply, variant=4, user_id=user_id)
    
    return reply_json


def post_reply_lock(auth, data):
    comment_id = a_moderatable_reply(data['comment_id'], auth).id
    locked = data['locked']

    user_id, reply = lock_post_reply(comment_id, locked, SRC_API, auth)

    if not can_view(reply, user_id):  # as post_reply_remove: an acknowledgement, not the content (D19)
        return {'comment_view': reply_removal_ack_view(reply)}
    reply_json = reply_view(reply=reply, variant=4, user_id=user_id)
    return reply_json


def get_reply_like_list(auth, data):
    comment_id = data['comment_id']
    page = data['page'] if 'page' in data else 1
    limit = data['limit'] if 'limit' in data else 50

    if limit > current_app.config["PAGE_LENGTH"]:
        limit = current_app.config["PAGE_LENGTH"]

    user = authorise_api_user(auth, return_type='model')
    post_reply = a_visible_reply(comment_id, auth)

    if post_reply.community.is_moderator(user) or user_access('moderate all communities', user.id, on=post_reply):
        banned_from_site_user_ids = list(db.session.execute(text('SELECT id FROM "user" WHERE banned = true')).scalars())
        banned_from_community_user_ids = list(db.session.execute(text
            ('SELECT user_id from "community_ban" WHERE community_id = :community_id'), {"community_id": post_reply.community_id}).scalars())
        voters = without_local_only_actors(PostReplyVote.query.join(User, User.id == PostReplyVote.user_id), User,
                                           True)   # no voter hidden here (follow-up F6)
        likes = voters.filter(
            PostReplyVote.post_reply_id == comment_id, PostReplyVote.effect != 0).order_by(PostReplyVote.effect).order_by(
            PostReplyVote.created_at).paginate(page=page, per_page=limit, error_out=False)
        comment_likes = []
        for like in likes:
            comment_likes.append({
                'score': like.effect,
                'creator_banned_from_community': like.user_id in banned_from_community_user_ids,
                'creator_banned': like.user_id in banned_from_site_user_ids,
                'creator': user_view(user=like.user_id, variant=1, stub=True, user_id=user.id)
            })
        response_json = {
            'next_page': str(likes.next_num) if likes.next_num is not None else None,
            'comment_likes': comment_likes
        }
        return response_json
    else:
        raise Exception('Not a moderator')
