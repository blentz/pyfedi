from datetime import timedelta

from flask import g, current_app
from sqlalchemy import desc, text, func, cast, Float, exists, and_, or_, any_
from sqlalchemy.orm import aliased

from app import db
from app.api.alpha.views import reply_view, reply_stub_view, reply_removal_ack_view, reply_report_view, post_view, community_view, user_view
from app.constants import *
from app.models import Notification, PostReply, Post, PostReplyVote, Report, Community, utcnow
from app.shared.reply import vote_for_reply, bookmark_reply, remove_bookmark_reply, subscribe_reply, make_reply, \
    edit_reply, \
    delete_reply, restore_reply, report_reply, mod_remove_reply, mod_restore_reply, lock_post_reply, choose_answer, \
    unchoose_answer
from app.visibility import can_view, listable_clause, visible_to_clause
from app.utils import authorise_api_user, blocked_users, blocked_or_banned_instances, site_language_id, \
    communities_banned_from, in_sorted_list, moderating_communities_ids, joined_communities, user_access, \
    can_moderate


def get_reply_list(auth, data, user_details=None):
    replies = None

    if auth:
        user_details = authorise_api_user(auth, return_type='dict')

    if user_details:
        user_id = user_details['id']
    else:
        user_id = None

    # user_id: the logged in user
    # person_id: the author of the posts being requested
    query = data.get("q", None)

    page = int(data['page']) if 'page' in data else 1
    limit = int(data['limit']) if 'limit' in data else 10
    sort = data['sort'] if 'sort' in data else 'New'
    type = data['type_'] if 'type_' in data else 'All'

    if limit > current_app.config["PAGE_LENGTH"]:
        limit = current_app.config["PAGE_LENGTH"]

    # LIKED_ONLY
    vote_effect = None
    by_liked_only = False
    if 'liked_only' in data and data['liked_only']:
        if not user_id:
            raise Exception('Login required for liked_only query')
        replies = PostReply.query.filter(PostReply.id.in_(user_details['upvoted_reply_ids']), PostReply.user_id != user_id)
        vote_effect = 1
        by_liked_only = True

    # SAVED_ONLY
    is_reply_bookmarked = None
    by_saved_only = False
    if 'saved_only' in data and data['saved_only']:
        if not user_id:
            raise Exception('Login required for saved_only query')
        if not replies:
            replies = PostReply.query.filter(PostReply.id.in_(user_details['bookmarked_reply_ids']))
        else:
            replies = replies.filter(PostReply.id.in_(user_details['bookmarked_reply_ids']))
        is_reply_bookmarked = True
        by_saved_only = True
    
    # SEARCH
    if query:
        if not replies:
            replies = PostReply.query.search(query, sort=sort == 'Relevance')
        else:
            replies = replies.search(query, sort=sort == 'Relevance')
        replies = replies.filter(PostReply.indexable == True)
        
    # PERSON_ID
    add_creator_in_view = True
    is_creator_blocked = None
    is_creator_admin = None
    by_person_id = False
    if 'person_id' in data:
        person_id = int(data['person_id'])
        if not replies:
            replies = PostReply.query.filter_by(user_id=person_id)
        else:
            replies = replies.filter_by(user_id=person_id)
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
    if 'community_id' in data:
        community_id = int(data['community_id'])
        if not replies:
            replies = PostReply.query.filter_by(community_id=community_id)
        else:
            replies = replies.filter_by(community_id=community_id)
        add_community_in_view = False
        is_user_banned_from_community = community_id in user_details['user_ban_community_ids'] if user_details else False
        is_user_following_community = community_id in user_details['followed_community_ids'] if user_details else False
        is_user_moderator = community_id in user_details['moderated_community_ids'] if user_details else False
        by_community_id = True

    is_creator_banned_from_community = None
    is_creator_moderator = None

    # ALL REPLIES (NO FILTER)
    if not replies:
        if 'post_id' not in data and 'parent_id' not in data:
            replies = PostReply.query
            if user_id is None and page * limit > 10000:
                raise Exception('unknown') # deliberately vague response

    # LISTING TYPE
    # D1199. This block used to sit ABOVE the person_id, community_id and
    # no-filter blocks, and it is guarded by `if replies:` -- which none of
    # them had assigned yet. So `type_` was silently ignored for every query
    # narrowed by a person, a community or nothing at all, and the
    # `incorrect login` refusals below were skipped with it. Measured:
    #
    #     PROBE bq1 person_id + Local: remote included: True
    #     PROBE bq2 community_id + Local: remote included: True
    #     PROBE bq3 anonymous + Moderating: accepted
    #
    # It still does not apply to the threaded branch, where `replies` is
    # assigned further down: a conversation is not a listing.
    if replies:
        if type == 'Local':
            replies = replies.filter(or_(PostReply.ap_id == None, PostReply.ap_id.startswith('https://' + current_app.config['SERVER_NAME'])))
        elif type == 'Moderating' or type == 'ModeratorView':
            if user_id:
                replies = replies.filter(PostReply.community_id.in_(moderating_communities_ids(user_id=user_id)))
            else:
                raise Exception('incorrect login')
        elif type == 'Subscribed':
            if user_id:
                comms = joined_communities(user_id=user_id)
                comm_ids = [comm.id for comm in comms]
                comm_ids.extend(moderating_communities_ids(user_id=user_id))
                replies = replies.filter(PostReply.community_id.in_(comm_ids))
            else:
                raise Exception('incorrect login')

    add_post_in_view = True
    depth_first = False
    next_page = None
    if replies:
        # if replies isn't None, then response is just a list of comments, not a threaded conversation

        # a listing omits a hidden reply outright (interop D7); only a threaded
        # conversation keeps a stub, to hold the tree together (D18). The person,
        # saved and liked listings are the profile surfaces: they show what the viewer
        # may see and omit the rest, with no stub.
        if by_person_id or by_saved_only or by_liked_only:
            replies = replies.filter(visible_to_clause(PostReply, user_id))
        else:
            replies = replies.filter(listable_clause(PostReply))

        # safe to just remove any replies by blocked users (won't cause gaps in threaded convo)
        if user_id and (add_creator_in_view == True or user_id != data['person_id']):
            blocked_person_ids = blocked_users(user_id)
            if blocked_person_ids:
                replies = replies.filter(PostReply.user_id.not_in(blocked_person_ids))
            blocked_instance_ids = blocked_or_banned_instances(user_id)
            if blocked_instance_ids:
                replies = replies.filter(PostReply.instance_id.not_in(blocked_instance_ids))

        # if 'post_id' is also in data, treat it as an additional filter to liked_only, saved_only, person_id, community_id
        if 'post_id' in data:
            replies = replies.filter_by(post_id=data['post_id'])
            add_community_in_view = False
            add_post_in_view = False

        if 'max_depth' in data:
            replies = replies.filter(PostReply.depth < int(data['max_depth']))
    else:
        # PARENT_ID or POST_ID - threaded conversation
        parent_id = None # stays at None for a post_id query
        max_depth = None

        # max_depth for parent_id query
        if 'parent_id' in data:
            parent_id = int(data['parent_id'])
            parent_depth = db.session.execute(text('SELECT depth FROM "post_reply" WHERE id = :id'),
                                              {"id": parent_id}).scalar()
            if parent_depth is None:
                raise Exception('Comment with parent_id not found.')
            if 'max_depth' in data:
                relative_depth = int(data['max_depth'])
                max_depth = parent_depth + relative_depth

        # max_depth for post_id query
        elif 'post_id' in data:
            post_id = int(data['post_id'])
            if 'max_depth' in data:
                max_depth = int(data['max_depth'])

        # blocked users aren't filtered out for a threaded convo to avoid creating gaps
        # get_comment_branch() isn't used here for the same reason

        if 'post_id' in data or 'parent_id' in data:
            # some apps paginate through all replies to get them all in one session and build the tree client-side
            # replies.paginate() can be used for those (it won't matter if the parent of a reply on page 1 is on page 2)

            # others expect to be able to render each page as they get it, so each page needs the complete info (no missing parents)
            # allowing for this means that the number of replies returned may exceed the limit for a page
            # 'depth_first' can be sent by these apps (although they're likely better off using the /post/replies route)
            # note: actually doing something like 'ORDER BY depth, posted_at' gives boring results (all depth=0 on page 1)
            if 'depth_first' in data and data['depth_first']:
                depth_first = True
                if parent_id is not None:
                    if parent_depth == 0:
                        where_query = f' WHERE root_id = {parent_id}'
                    else:
                        where_query = f' WHERE path @> ARRAY[{parent_id}]'
                else:
                    where_query = f' WHERE post_id = {post_id}'

                if max_depth is not None:
                    # D1198. This was `' AND depth <= {max_depth}'` with no
                    # f-prefix, so the braces themselves reached Postgres:
                    # `psycopg2.errors.SyntaxError: syntax error at or near
                    # "{"`, measured as PROBE bp1. Every depth-first comment
                    # query carrying a max_depth failed, and the aborted
                    # transaction took the next query in the request with it.
                    #
                    # The value is interpolated rather than bound because the
                    # surrounding clauses are too, and all three come from
                    # `int(...)` a few lines above -- see `parent_id`,
                    # `post_id` and `max_depth`.
                    depth_query = f' AND depth <= {max_depth}'
                else:
                    depth_query = ''

                if sort == 'Hot':
                    sort_query = ' ORDER BY ranking DESC, posted_at DESC'
                elif sort == 'Top':
                    sort_query = ' ORDER BY up_votes - down_votes DESC'
                elif sort == 'Old':
                    sort_query = ' ORDER BY posted_at'
                else:
                    sort_query = ' ORDER BY posted_at DESC'

                query = 'SELECT path from "post_reply"' + where_query + depth_query + sort_query
                reply_paths = db.session.execute(text(query)).scalars()
                processed = {0}
                page_array = [None, []] # not really an array, I know
                page_index = 1
                for reply_path in reply_paths:
                    for element in reply_path:
                        # element == 0 means new branch of comments, not dependent on this branch
                        if element == 0 and len(page_array[page_index]) >= limit:
                            page_index += 1
                            page_array.append([])
                        if element not in processed:
                            page_array[page_index].append(element)
                        processed.add(element)
                replies = PostReply.query.filter(PostReply.id.in_(page_array[page]))
                if page_index > page and len(page_array[page+1]) > 0:
                    next_page = str(page + 1)
            else:
                # 'depth_first' == false, so it'll be more straight-forward to query and paginate
                if parent_id is not None:
                    # 'parent_id' query
                    if parent_depth == 0:
                        replies = PostReply.query.filter_by(root_id=parent_id)
                    else:
                        reply_ids = db.session.execute(text('SELECT id FROM "post_reply" WHERE path @> ARRAY[:id]'),
                                                       {"id": parent_id}).scalars()
                        replies = PostReply.query.filter(PostReply.id.in_(reply_ids))
                else:
                    # 'post_id' query
                    replies = PostReply.query.filter_by(post_id=post_id)

                if max_depth is not None:
                    replies = replies.filter(PostReply.depth <= max_depth)

            add_community_in_view = False
            add_post_in_view = False

        if user_id:
            # filter out blocked users' comments and all their replies
            blocked_person_ids = blocked_users(user_id)
            if blocked_person_ids:
                parent_alias = aliased(PostReply)
                replies = replies.filter(
                    ~or_(
                        PostReply.user_id.in_(blocked_person_ids),
                        exists().where(
                            and_(
                                parent_alias.id == any_(PostReply.path),
                                parent_alias.user_id.in_(blocked_person_ids)
                            )
                        )
                    )
                )
            # filter out blocked instances' comments and all their replies
            blocked_instance_ids = blocked_or_banned_instances(user_id)
            if blocked_instance_ids:
                parent_alias = aliased(PostReply)
                replies = replies.filter(
                    ~or_(
                        PostReply.instance_id.in_(blocked_instance_ids),
                        exists().where(
                            and_(
                                parent_alias.id == any_(PostReply.path),
                                parent_alias.instance_id.in_(blocked_instance_ids)
                            )
                        )
                    )
                )

    if replies:
        # sort == 'Relevance' handled above when query.search was executed
        if sort == 'Hot' or sort == 'Scaled':
            replies = replies.order_by(desc(PostReply.ranking)).order_by(desc(PostReply.posted_at))
        elif sort == 'Active':
            replies = replies.order_by(desc(PostReply.posted_at))
        elif sort == 'Top' or sort == 'TopAll':
            replies = replies.order_by(desc(PostReply.up_votes - PostReply.down_votes))
        elif sort == 'TopHour':
            replies = replies.filter(PostReply.posted_at > utcnow() - timedelta(hours=1)).order_by(
                desc(PostReply.up_votes - PostReply.down_votes))
        elif sort == 'TopSixHour':
            replies = replies.filter(PostReply.posted_at > utcnow() - timedelta(hours=6)).order_by(
                desc(PostReply.up_votes - PostReply.down_votes))
        elif sort == 'TopTwelveHour':
            replies = replies.filter(PostReply.posted_at > utcnow() - timedelta(hours=12)).order_by(
                desc(PostReply.up_votes - PostReply.down_votes))
        elif sort == 'TopDay':
            replies = replies.filter(PostReply.posted_at > utcnow() - timedelta(days=1)).order_by(
                desc(PostReply.up_votes - PostReply.down_votes))
        elif sort == 'TopWeek':
            replies = replies.filter(PostReply.posted_at > utcnow() - timedelta(days=7)).order_by(
                desc(PostReply.up_votes - PostReply.down_votes))
        elif sort == 'TopMonth':
            replies = replies.filter(PostReply.posted_at > utcnow() - timedelta(days=28)).order_by(
                desc(PostReply.up_votes - PostReply.down_votes))
        elif sort == 'TopThreeMonths':
            replies = replies.filter(PostReply.posted_at > utcnow() - timedelta(days=90)).order_by(
                desc(PostReply.up_votes - PostReply.down_votes))
        elif sort == 'TopSixMonths':
            replies = replies.filter(PostReply.posted_at > utcnow() - timedelta(days=180)).order_by(
                desc(PostReply.up_votes - PostReply.down_votes))
        elif sort == 'TopNineMonths':
            replies = replies.filter(PostReply.posted_at > utcnow() - timedelta(days=270)).order_by(
                desc(PostReply.up_votes - PostReply.down_votes))
        elif sort == 'TopYear':
            replies = replies.filter(PostReply.posted_at > utcnow() - timedelta(days=365)).order_by(
                desc(PostReply.up_votes - PostReply.down_votes))
        elif sort == 'Old':
            replies = replies.order_by(PostReply.posted_at)
        elif sort == 'Relevance':
            pass  # already done as part of the search query
        elif sort == 'Controversial':
            # Pulled from the reddit algorithm: https://github.com/reddit-archive/reddit/blob/753b17407e9a9dca09558526805922de24133d53/r2/r2/lib/db/_sorts.pyx#L60
            replies = replies.order_by(desc(
                func.coalesce(func.pow(
                    func.coalesce(PostReply.up_votes, 0) * func.coalesce(PostReply.down_votes, 0),
                    cast(func.least(func.coalesce(PostReply.up_votes, 0), func.coalesce(PostReply.down_votes, 0)), Float) /
                    # D1200. The divisor was `coalesce(greatest(up, down), 1)`,
                    # which guards NULL and not ZERO -- and a comment with no
                    # votes at all has `greatest(0, 0)`, so the whole listing
                    # was `psycopg2.errors.DivisionByZero: division by zero`.
                    # `greatest(..., 1)` cannot be either.
                    cast(func.greatest(func.coalesce(PostReply.up_votes, 0),
                                       func.coalesce(PostReply.down_votes, 0), 1), Float)), 0)))
        else:
            replies = replies.order_by(desc(PostReply.posted_at))

        if depth_first == False:
            replies = replies.paginate(page=page, per_page=limit, error_out=False)
            next_page = str(replies.next_num) if replies.next_num is not None else None
    else:
        replies = [] # shouldn't happen

    reply_list = []
    inner_creator_view = inner_community_view = inner_post_view = None
    for reply in replies:
        if not can_view(reply, user_id):
            reply_list.append(reply_stub_view(reply))  # D18
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
def a_reply(reply_id):
    reply = db.session.get(PostReply, reply_id)
    if reply is None:
        raise Exception('comment not found')
    return reply


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
    if user is not None and can_moderate(reply.community, user):
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
    post = db.session.get(Post, post_id)
    if post is None or not can_view(post, authorise_api_user(auth)):
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

        if user.id in mod_ids or user_access('administer all communities', user.id):
            reports = Report.query.filter(Report.suspect_post_reply_id == comment_id)
        else:
            raise Exception('incorrect login')
    elif community_id:
        # Just get reports for a single community
        community = db.session.get(Community, community_id)
        if community is None:
            raise Exception('community not found')
        mods = community.moderators()
        mod_ids = [mod.user_id for mod in mods]

        if user.id in mod_ids or user_access("administer all communities", user.id):
            reports = Report.query.filter(Report.in_community_id == community_id, Report.suspect_post_reply_id != None)
        else:
            raise Exception('incorrect login')
    else:
        # Don't restrict reports to single comment or community
        if user_access('administer all communities', user.id):
            # Privileged user, don't filter by community
            reports = Report.query.filter(Report.suspect_post_reply_id != None)
        else:
            modded_comm_ids = moderating_communities_ids(user.id)
            reports = Report.query.filter(Report.suspect_post_reply_id != None,
                                          Report.in_community_id.in_(modded_comm_ids))
    
    if unresolved_only:
        reports = reports.filter(Report.status < REPORT_STATE_RESOLVED)
    
    reports = reports.paginate(page=page, per_page=limit, error_out=False)

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
    
    report = db.session.get(Report, report_id)

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

    if user.id in mod_ids or user_access('administer all communities', user.id):
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

    if post_reply.community.is_moderator(user) or user.is_admin() or user.is_staff():
        banned_from_site_user_ids = list(db.session.execute(text('SELECT id FROM "user" WHERE banned = true')).scalars())
        banned_from_community_user_ids = list(db.session.execute(text
            ('SELECT user_id from "community_ban" WHERE community_id = :community_id'), {"community_id": post_reply.community_id}).scalars())
        likes = PostReplyVote.query.filter(
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
